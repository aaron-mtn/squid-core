"""Core protocol and protected application-policy prompt regressions."""

from pathlib import Path

import pytest

from squid_core.agent.prompts import PromptDefaults, PROTECTED_RESPONSE_EXAMPLES, system_prompt
from squid_core.platform_context import PlatformContext
from squid_core.tools.builtins import TOOLS

from squid_core.skills import PromptSectionOverride, Skill, parse_skill_file
from squid_core.agent import Agent


def _skill_with_replacements() -> Skill:
    return Skill(
        name="review",
        description="Review a change.",
        instructions="Focus on architecture.",
        allowed_tools=("read_file",),
        path=Path("SKILL.md"),
        prompt_sections={
            "AGENT_IDENTITY": PromptSectionOverride(
                section="AGENT_IDENTITY",
                mode="replace",
                content="Specialized intro.",
            ),
            "TOOL_USAGE_GUIDANCE": PromptSectionOverride(
                section="TOOL_USAGE_GUIDANCE",
                mode="replace",
                content="Use review tools carefully.",
            ),
            "FINAL_SUMMARY_REQUIREMENTS": PromptSectionOverride(
                section="FINAL_SUMMARY_REQUIREMENTS",
                mode="replace",
                content="Return review findings.",
            ),
        },
    )




def test_core_agent_starts_with_system_and_raw_authored_task() -> None:
    messages = Agent("  keep authored spacing  ").initial_messages()

    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[1] == {"role": "user", "content": "  keep authored spacing  "}


def test_core_agent_follow_up_is_raw_authored_user_message() -> None:
    message = Agent("first").follow_up_message("  follow up exactly  ")

    assert message == {"role": "user", "content": "  follow up exactly  "}


def test_active_skill_name_is_exposed() -> None:
    agent = Agent("review", skill=_skill_with_replacements())

    assert agent.skill_name == "review"


def test_skill_replacements_cannot_erase_core_protocol() -> None:
    prompt = Agent(
        "review", skill=_skill_with_replacements()
    ).initial_messages()[0]["content"]

    assert "Batch multiple tool calls when it is efficient and the arguments are known." in prompt
    assert "could eliminate the need for a later call or materially change" in prompt
    assert "wait for the earlier result before issuing the later call" in prompt
    assert "A final response contains no tool call" in prompt
    assert "Prefer a brief natural note or narration before tool calls when useful." not in prompt
    assert "A non-final turn uses one or more real tool calls." in prompt
    assert "[one or more independent tool calls]" in prompt


def test_default_prompt_allows_efficient_batches_without_speculative_calls() -> None:
    prompt = Agent("inspect it").initial_messages()[0]["content"]

    assert "A non-final turn uses one or more real tool calls." in prompt
    assert "Batch multiple tool calls when it is efficient and the arguments are known." in prompt
    assert (
        "If an earlier call's result could eliminate the need for a later call "
        "or materially change what that later call should do, wait for the "
        "earlier result before issuing the later call."
    ) in prompt
    assert "[one or more independent tool calls]" in prompt
    assert "each can be fully determined from the current context" not in prompt
    assert "If a later call depends on an earlier result" not in prompt




def test_core_does_not_prescribe_narration_or_require_it() -> None:
    agent = Agent("change it")
    prompt = agent.initial_messages()[0]["content"]
    reply = {
        "message": {"content": "   "},
        "actions": [{"tool": "read_file", "args": {}, "tool_call_id": "call_1"}],
    }

    assert "Prefer a brief natural note or narration before tool calls when useful." not in prompt
    assert agent.validate_reply(reply) == {"ok": True, "reason": ""}


@pytest.mark.parametrize("tools", [None, {}, {"custom": {"definition": lambda: {"name": "custom"}}}])
def test_agent_preserves_tool_selection(tools) -> None:
    expected = TOOLS if tools is None else tools
    agent = Agent("task", tools=tools)
    assert agent.tools is expected
    assert agent.tool_definitions() == [entry["definition"]() for entry in expected.values()]


@pytest.mark.parametrize("allowed,expected", [
    ("", tuple(TOOLS)),
    ("allowed-tools: []\n", ()),
    ("allowed-tools: read_file\n", ("read_file",)),
])
def test_standalone_skill_permissions(tmp_path, allowed, expected) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(
        f"---\nname: catalog\ndescription: Catalog resources\n{allowed}---\nCatalog resources.\n",
        encoding="utf-8",
    )
    agent = Agent("catalog", skill=parse_skill_file(path))
    assert tuple(agent.tools) == expected
    assert agent.tool_definitions() == [TOOLS[name]["definition"]() for name in expected]
    if not allowed:
        assert agent.tools is TOOLS


def test_standalone_skill_rejects_unavailable_tool_without_adding_builtins(tmp_path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(
        "---\nname: catalog\ndescription: Catalog resources\nallowed-tools: read_file\n---\nCatalog.\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown Tool 'read_file'"):
        Agent("catalog", skill=parse_skill_file(path), tools={})


def test_programmatic_skill_nonempty_tools_are_permissions() -> None:
    agent = Agent("review", skill=_skill_with_replacements())
    assert tuple(agent.tools) == ("read_file",)


@pytest.mark.parametrize("context", [
    PlatformContext("Windows", "PowerShell", "pwsh", "\\", True),
    PlatformContext("Linux", "bash", "/bin/bash", "/", False),
])
@pytest.mark.parametrize("names", [(), ("custom",), ("read_file",), ("apply_patch",),
                                  ("write_file",), ("run_command",), tuple(TOOLS)])
def test_environment_guidance_matches_resolved_tools(context, names) -> None:
    tools = {name: TOOLS.get(name, {}) for name in names}
    prompt = Agent("task", tools=tools, platform_context=context,
                   workspace="workspace").initial_messages()[0]["content"]
    assert f"Operating system: {context.os_name}" in prompt
    assert f"Preferred shell: {context.shell_name}" in prompt
    assert f"Path separator: {context.path_separator}" in prompt
    assert "Workspace: workspace" in prompt
    assert ("Use commands, quoting, paths, and utilities valid for this environment." in prompt) == (
        "run_command" in names
    )
    for name in TOOLS:
        assert (name in prompt) == (name in names)
    assert ("Write PowerShell commands directly" in prompt) == (
        context.is_windows and "run_command" in names
    )
    assert ("Write commands directly for that shell" in prompt) == (
        not context.is_windows and "run_command" in names
    )


@pytest.mark.parametrize("context", [
    PlatformContext("Windows", "PowerShell", "pwsh", "\\", True),
    PlatformContext("Linux", "bash", "/bin/bash", "/", False),
])
def test_default_tools_include_environment_capabilities(context) -> None:
    prompt = system_prompt(platform_context=context, workspace="workspace", tools=None)
    assert prompt == (
        Agent("task", platform_context=context, workspace="workspace").initial_messages()[0]["content"]
    )
    assert "Use commands, quoting, paths, and utilities valid for this environment." in prompt
    assert ("Write PowerShell commands directly" in prompt) == context.is_windows
    assert ("Write commands directly for that shell" in prompt) == (not context.is_windows)


@pytest.mark.parametrize("section,field", [
    ("AGENT_IDENTITY", "agent_identity"),
    ("TOOL_USAGE_GUIDANCE", "tool_usage_guidance"),
    ("FINAL_SUMMARY_REQUIREMENTS", "final_summary_requirements"),
])
@pytest.mark.parametrize("mode", ["append", "replace"])
def test_fixed_prompt_sections_keep_literal_override_semantics(section, field, mode) -> None:
    defaults = PromptDefaults(**{field: "Original section."})
    skill = _skill_with_replacements()
    skill.prompt_sections.clear()
    skill.prompt_sections[section] = PromptSectionOverride(section, mode, "Custom section.")
    prompt = system_prompt(defaults, skill, tools={})
    assert "Custom section." in prompt
    assert ("Original section." in prompt) == (mode == "append")


def test_core_prompt_is_task_neutral() -> None:
    prompt = Agent("task", tools={}).initial_messages()[0]["content"]
    assert "Code Squid" not in prompt
    assert "Visible action notes" not in prompt
    assert "source reading" not in prompt
    assert "Code Squid final report" not in prompt
    for text in ("code", "tests", "browser", "fix"):
        assert text not in PROTECTED_RESPONSE_EXAMPLES.lower()
