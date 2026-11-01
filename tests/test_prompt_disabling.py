"""Exercise optional text omission through Skills, prompts, and Mod resolution.

These tests protect Core's generic host boundary, not application policy names.
Runtime tools remain independently restricted by the existing Agent/executor.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from squid_core.agent import Agent
from squid_core.agent.prompts import PromptDefaults, system_prompt
from squid_core.mods import discover_mods, load_mod, resolve_mod
from squid_core.skills import parse_skill_file
from squid_core.tools.executor import ToolExecutor


def skill_file(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nname: sample\ndescription: Sample\nallowed-tools: read_file\n---\n\n" + body,
                    encoding="utf-8")
    return path


@pytest.mark.parametrize("disabled", [(), ("HOST_POLICY",), ("HOST_POLICY", "HOST_EXAMPLES")])
def test_named_components_preserve_default_prompt_and_omit_independently(tmp_path, disabled):
    baseline = {"HOST_POLICY": "Host policy.", "HOST_EXAMPLES": "Host examples."}
    legacy = PromptDefaults(baseline_policy="\n\n".join(baseline.values()))
    named = replace(legacy, baseline_sections=baseline)
    path = skill_file(tmp_path / "SKILL.md", "Ordinary instructions.\n")
    plain = parse_skill_file(path)
    assert system_prompt(legacy, plain) == system_prompt(named, plain)
    body = "Ordinary instructions.\n\n## DISABLE_PROMPT_SECTIONS\n\n" + "".join(f"- {n}\n" for n in disabled)
    skill = parse_skill_file(skill_file(path, body), supported_disable_sections=frozenset(baseline))
    assert skill.instructions == plain.instructions
    assert skill.disabled_prompt_sections == disabled
    prompt = system_prompt(named, skill)
    for name, text in baseline.items():
        assert (text in prompt) == (name not in disabled)
    assert "## Response protocol" in prompt
    assert "## Verification honesty" in prompt
    agent = Agent("task", skill=skill, prompt_defaults=named)
    assert tuple(agent.tools) == ("read_file",)
    assert ToolExecutor(tmp_path, None, tools=agent.tools).execute({"tool": "write_file", "args": {}})["error"] == "Unknown tool: write_file"


@pytest.mark.parametrize("body, error", [
    ("- UNKNOWN_SECTION", "unknown disabled prompt section"),
    ("- RESPONSE_PROTOCOL", "protected prompt section"),
    ("- AGENT_IDENTITY\n- AGENT_IDENTITY", "duplicate disabled prompt section"),
    ("- AGENT_IDENTITY\n\n## AGENT_IDENTITY replace\nCustom", "disabled and overridden"),
    ("- AGENT_IDENTITY\n\n## DISABLE_PROMPT_SECTIONS\n", "duplicate DISABLE_PROMPT_SECTIONS"),
    ("AGENT_IDENTITY", "requires '- SECTION_NAME'"),
])
def test_invalid_directives(tmp_path, body, error):
    with pytest.raises(ValueError, match=error):
        parse_skill_file(skill_file(tmp_path / "SKILL.md", "## DISABLE_PROMPT_SECTIONS\n\n" + body))


def test_directive_boundaries_and_existing_override_semantics(tmp_path):
    skill = parse_skill_file(skill_file(tmp_path / "SKILL.md", """Ordinary.
## AGENT_IDENTITY append
Identity addition.
## DISABLE_PROMPT_SECTIONS
- FINAL_SUMMARY_REQUIREMENTS
## More instructions
Remain ordinary.
## TOOL_USAGE_GUIDANCE replace
Replacement guidance.
"""))
    prompt = system_prompt(PromptDefaults(tool_usage_guidance="Old guidance", final_summary_requirements="Old final"), skill)
    assert "You are an agent working on a user-authored task.\n\nIdentity addition." in prompt
    assert "Replacement guidance." in prompt and "Old guidance" not in prompt
    assert "Old final" not in prompt
    assert "## More instructions\nRemain ordinary." in skill.instructions
    assert "DISABLE_PROMPT_SECTIONS" not in prompt


def test_only_exact_unfenced_directive_is_interpreted(tmp_path):
    body = "## Disable prompt sections\nOrdinary.\n```markdown\n## DISABLE_PROMPT_SECTIONS\n- UNKNOWN_SECTION\n```\n"
    skill = parse_skill_file(skill_file(tmp_path / "SKILL.md", body))
    assert skill.instructions == body.strip()
    assert skill.disabled_prompt_sections == ()


def test_mod_discovery_validation_and_resolution_share_host_targets(tmp_path):
    root = tmp_path / "mods" / "sample"
    skill_file(root / "skills" / "sample" / "SKILL.md", "## DISABLE_PROMPT_SECTIONS\n- HOST_POLICY\n")
    (root / "mod.yaml").write_text("---\nname: sample\ndescription: Sample\ndefault_skill: sample\nhooks: []\n---\n")
    assert not load_mod("sample", tmp_path).valid
    options = {"supported_disable_sections": frozenset({"HOST_POLICY"})}
    assert discover_mods(tmp_path, **options)["sample"].valid
    assert load_mod("sample", tmp_path, **options).valid
    resolved = resolve_mod("sample", tmp_path, **options)
    assert resolved.skill.disabled_prompt_sections == ("HOST_POLICY",)
    assert tuple(resolved.tools) == ("read_file",)
    # Programmatic construction is checked again at the prompt boundary.
    bad = replace(resolved.skill, disabled_prompt_sections=("RESPONSE_PROTOCOL",))
    with pytest.raises(ValueError, match="protected prompt section"):
        system_prompt(PromptDefaults(), bad)
