"""Packaged Mod discovery, Skill selection, and runtime resource boundaries."""

from pathlib import Path
import json

import pytest

from squid_core.hooks import list_user_hooks, load_user_hooks
from squid_core.mods import ModResolutionError, discover_mods, load_mod, resolve_mod, validate_mod
from squid_core.settings import RuntimeSettings as CoreRuntimeSettings, CompactionSettings
from squid_core.skills import discover_skills
from squid_core.tools.user_tools import discover_user_tools
from squid_core import spawner as generic_spawner
from squid_core.tools.builtins import TOOLS
from squid_core.session import Session
from squid_core.storage import SessionStore


def _write_skill(path: Path, name: str, tools: tuple[str, ...], description: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    allowed = "allowed-tools: []" if not tools else "allowed-tools:\n" + "".join(
        f"  - {tool}\n" for tool in tools
    ).rstrip()
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n{allowed}\n---\n\nInstructions for {name}.\n",
        encoding="utf-8",
    )


def _write_tool(root: Path, name: str, marker: Path | None = None, result: str = "tool") -> None:
    tool_dir = root / name
    tool_dir.mkdir(parents=True, exist_ok=True)
    (tool_dir / "tool.yaml").write_text(
        f"name: {name}\ndescription: {result}\n",
        encoding="utf-8",
    )
    import_marker = "" if marker is None else f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n"
    (tool_dir / "tool.py").write_text(
        import_marker + f"def run(args, context=None):\n    return {result!r}\n",
        encoding="utf-8",
    )


def _write_hook(root: Path, name: str, marker: Path | None = None, source: str = "hook") -> None:
    hook_dir = root / name
    hook_dir.mkdir(parents=True, exist_ok=True)
    (hook_dir / "hook.yaml").write_text(
        f"name: {name}\nenabled: false\n",
        encoding="utf-8",
    )
    import_marker = "" if marker is None else f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n"
    (hook_dir / "hook.py").write_text(
        import_marker
        + f"class SelectedHook:\n    source = {source!r}\n\ndef create_hook(config=None):\n    return SelectedHook()\n",
        encoding="utf-8",
    )


def _write_mod(
    home: Path,
    name: str = "review-pack",
    *,
    skill_name: str | None = None,
    tools: tuple[str, ...] = (),
    hooks: tuple[str, ...] = (),
    local_skill: bool = True,
    skill_description: str = "Mod-local skill",
) -> Path:
    skill_name = name if skill_name is None else skill_name
    mod_dir = home / "mods" / name
    mod_dir.mkdir(parents=True, exist_ok=True)
    hook_lines = "hooks: []" if not hooks else "hooks:\n" + "".join(f"  - {item}\n" for item in hooks).rstrip()
    (mod_dir / "mod.yaml").write_text(
        f"---\nname: {name}\ndescription: Test Mod\ndefault_skill: {skill_name}\naccent: purple\n{hook_lines}\n---\n",
        encoding="utf-8",
    )
    if local_skill:
        _write_skill(mod_dir / "skills" / skill_name / "SKILL.md", skill_name, tools, skill_description)
    return mod_dir


class _Display:
    debug = False

    def thinking(self) -> None:
        pass

    def debug_reply(self, reply: dict) -> None:
        pass

    def final_message(self, content: str) -> None:
        pass


class _CompletingAdapter:
    def __init__(self) -> None:
        self.requests = []

    def respond(self, messages: list[dict], tool_definitions: list[dict]) -> dict:
        self.requests.append((list(messages), list(tool_definitions)))
        return {
            "message": {"role": "assistant", "content": "complete"},
            "actions": [],
            "raw_reply": {},
        }

    def supports_streaming(self) -> bool:
        return False


def test_canonical_discovery_uses_supplied_home_and_ignores_legacy_layout(tmp_path: Path) -> None:
    selected_home = tmp_path / "selected"
    other_home = tmp_path / "other"
    _write_mod(selected_home, "canonical")
    _write_mod(other_home, "elsewhere")
    legacy = selected_home / "extensions" / "legacy"
    legacy.mkdir(parents=True)
    (legacy / "extension.yaml").write_text("name: legacy\n", encoding="utf-8")

    assert set(discover_mods(selected_home)) == {"canonical"}
    assert discover_mods(other_home)["elsewhere"].path == other_home / "mods" / "elsewhere"
    with pytest.raises(ModResolutionError, match="Unknown Mod: legacy"):
        load_mod("legacy", selected_home)


@pytest.mark.parametrize("registry_kind", ["default", "app", "empty"])
def test_discovery_load_validation_and_resolution_share_registry(tmp_path, registry_kind) -> None:
    home = tmp_path / "home"
    custom = {"catalog_record": {
        "execute": lambda workspace_root: {"ok": True, "message": "catalog"},
        "definition": lambda: {"type": "function", "function": {"name": "catalog_record"}},
    }}
    registry = {"default": None, "app": custom, "empty": {}}[registry_kind]
    requested = "read_file" if registry_kind == "default" else "catalog_record"
    _write_mod(home, tools=(requested,))
    discovered = discover_mods(home, built_in_tools=registry)["review-pack"]
    loaded = load_mod("review-pack", home, built_in_tools=registry)
    assert discovered.valid == loaded.valid == (registry_kind != "empty")
    assert discovered.errors == loaded.errors == validate_mod(loaded, home, built_in_tools=registry)
    if registry_kind == "empty":
        with pytest.raises(ModResolutionError, match="unknown Tool 'catalog_record'"):
            resolve_mod("review-pack", home, built_in_tools=registry)
    else:
        resolved = resolve_mod("review-pack", home, built_in_tools=registry)
        assert resolved.metadata.valid
        assert tuple(resolved.tools) == (requested,)
        assert resolved.tools[requested] is (TOOLS if registry is None else registry)[requested]
    if registry_kind == "app":
        assert not discover_mods(home)["review-pack"].valid


def test_standalone_resources_remain_discoverable_with_explicit_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_skill(home / "skills" / "loose-skill" / "SKILL.md", "loose-skill", (), "Loose")
    _write_tool(home / "tools", "loose-tool")
    _write_hook(home / "hooks", "loose-hook")

    assert "loose-skill" in discover_skills(app_home=home)
    assert "loose-tool" in discover_user_tools(app_home=home)
    assert [hook.name for hook in list_user_hooks(app_home=home)] == ["loose-hook"]
    assert load_user_hooks(app_home=home) == []


def test_discovery_and_validation_do_not_import_tool_or_hook_source(tmp_path: Path) -> None:
    home = tmp_path / "home"
    tool_marker = tmp_path / "tool-imported"
    hook_marker = tmp_path / "hook-imported"
    mod_dir = _write_mod(home, tools=("custom-tool",), hooks=("custom-hook",))
    _write_tool(mod_dir / "tools", "custom-tool", tool_marker)
    _write_hook(mod_dir / "hooks", "custom-hook", hook_marker)

    mod = discover_mods(home)["review-pack"]

    assert mod.valid is True
    assert validate_mod(mod, home) == ()
    assert not tool_marker.exists()
    assert not hook_marker.exists()


def test_runtime_resolves_local_skill_lazy_tool_and_loaded_hook(tmp_path: Path) -> None:
    home = tmp_path / "home"
    tool_marker = tmp_path / "tool-imported"
    hook_marker = tmp_path / "hook-imported"
    mod_dir = _write_mod(home, tools=("custom-tool",), hooks=("custom-hook",))
    _write_tool(mod_dir / "tools", "custom-tool", tool_marker, "mod-tool")
    _write_hook(mod_dir / "hooks", "custom-hook", hook_marker, "mod-hook")

    resolved = resolve_mod("review-pack", home, workspace_root=tmp_path)

    assert resolved.metadata.name == "review-pack"
    assert resolved.skill.source == "mod"
    assert resolved.skill.name == "review-pack"
    assert list(resolved.tools) == ["custom-tool"]
    assert resolved.hooks[0].source == "mod-hook"
    assert hook_marker.exists()
    assert not tool_marker.exists()
    assert resolved.tools["custom-tool"]["execute"](tmp_path)["message"] == "mod-tool"
    assert tool_marker.exists()


def test_mod_local_resources_win_over_same_named_loose_resources(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_skill(home / "skills" / "review-pack" / "SKILL.md", "review-pack", ("custom-tool",), "Loose skill")
    _write_tool(home / "tools", "custom-tool", result="loose-tool")
    _write_hook(home / "hooks", "custom-hook", source="loose-hook")
    mod_dir = _write_mod(home, tools=("custom-tool",), hooks=("custom-hook",), skill_description="Local skill")
    _write_tool(mod_dir / "tools", "custom-tool", result="mod-tool")
    _write_hook(mod_dir / "hooks", "custom-hook", source="mod-hook")

    resolved = resolve_mod("review-pack", home, workspace_root=tmp_path)

    assert resolved.skill.description == "Local skill"
    assert resolved.tools["custom-tool"]["execute"](tmp_path)["message"] == "mod-tool"
    assert resolved.hooks[0].source == "mod-hook"


def test_mod_can_reference_shared_tools_and_hooks(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_skill(home / "skills" / "shared-skill" / "SKILL.md", "shared-skill", ("shared-tool",), "Shared skill")
    _write_tool(home / "tools", "shared-tool", result="shared-tool")
    _write_hook(home / "hooks", "shared-hook", source="shared-hook")
    _write_mod(
        home,
        skill_name="shared-skill",
        tools=("shared-tool",),
        hooks=("shared-hook",),
    )

    resolved = resolve_mod("review-pack", home, workspace_root=tmp_path)

    assert resolved.skill.source == "mod"
    assert resolved.tools["shared-tool"]["execute"](tmp_path)["message"] == "shared-tool"
    assert resolved.hooks[0].source == "shared-hook"


def test_protected_builtin_tool_cannot_be_replaced_by_mod(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mod_dir = _write_mod(home, tools=("read_file",))
    _write_tool(mod_dir / "tools", "read_file", result="replacement")

    mod = load_mod("review-pack", home)
    assert any("cannot replace protected built-in" in error for error in mod.errors)
    with pytest.raises(ModResolutionError, match="cannot replace protected built-in"):
        resolve_mod("review-pack", home)


def test_loose_builtin_collision_still_uses_protected_builtin(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_tool(home / "tools", "read_file", result="replacement")
    _write_mod(home, tools=("read_file",))

    resolved = resolve_mod("review-pack", home)

    assert resolved.tools["read_file"] is TOOLS["read_file"]


def test_generic_spawner_remains_mod_agnostic() -> None:
    assert "mod_name" not in generic_spawner.build_agent_run.__annotations__
    assert "mod_name" not in generic_spawner.build_agent_run.__code__.co_varnames


def test_two_packaged_skills_resolve_one_profile_and_share_mod_hooks(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mod_dir = _write_mod(home, skill_name="review", tools=("read_file",), hooks=("audit",))
    _write_skill(mod_dir / "skills" / "edit" / "SKILL.md", "edit", ("run_command", "custom-tool"), "Edit")
    _write_tool(mod_dir / "tools", "custom-tool", result="local")
    _write_hook(mod_dir / "hooks", "audit", source="mod-wide")

    mod = discover_mods(home)["review-pack"]
    assert mod.valid
    assert set(mod.skills) == {"review", "edit"}
    assert validate_mod(mod, home) == ()
    assert not hasattr(mod, "tools")
    default = resolve_mod("review-pack", home)
    selected = resolve_mod("review-pack", home, selected_skill="edit")
    assert default.skill.name == "review"
    assert selected.skill.name == "edit"
    assert tuple(default.tools) == ("read_file",)
    assert tuple(selected.tools) == ("run_command", "custom-tool")
    assert [hook.source for hook in default.hooks] == ["mod-wide"]
    assert [hook.source for hook in selected.hooks] == ["mod-wide"]

    # Exercise the ordinary Core construction seam, so package validation's
    # broader dependencies cannot quietly reach the active ToolExecutor.
    run = generic_spawner.build_agent_run(
        tmp_path, "edit", settings=CoreRuntimeSettings(
            approval_mode="never", max_tool_steps=None,
            stream_assistant_text=False, compaction=CompactionSettings(enabled=False),
        ),
        skill=selected.skill, tools=selected.tools, model_adapter=object(),
        sessions_dir=tmp_path / "sessions", undo_dir=tmp_path / "undo",
    )
    assert tuple(run.loop.tool_executor.tools) == ("run_command", "custom-tool")
    assert run.loop.tool_executor.execute({"tool": "read_file"})["ok"] is False


def test_empty_selected_allowed_tools_stays_empty(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mod_dir = _write_mod(home, tools=("read_file",))
    _write_skill(mod_dir / "skills" / "no-tools" / "SKILL.md", "no-tools", (), "No tools")
    selected = resolve_mod("review-pack", home, selected_skill="no-tools")
    run = generic_spawner.build_agent_run(
        tmp_path, "think", settings=CoreRuntimeSettings(
            approval_mode="never", max_tool_steps=None,
            stream_assistant_text=False, compaction=CompactionSettings(enabled=False),
        ),
        skill=selected.skill, tools=selected.tools, model_adapter=object(),
        sessions_dir=tmp_path / "sessions", undo_dir=tmp_path / "undo",
    )
    assert selected.tools == {}
    assert run.loop.tool_executor.tools == {}


def test_missing_default_cannot_fall_back_to_standalone_skill(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mod_dir = _write_mod(home, skill_name="missing", local_skill=False)
    _write_skill(home / "skills" / "missing" / "SKILL.md", "missing", (), "Standalone")
    _write_skill(mod_dir / "skills" / "other" / "SKILL.md", "other", (), "Other")
    mod = load_mod("review-pack", home)
    assert not mod.valid
    assert any("unknown default Skill" in error for error in mod.errors)
    with pytest.raises(ModResolutionError, match="unknown default Skill"):
        resolve_mod("review-pack", home, selected_skill="other")


def test_mod_requires_at_least_one_direct_packaged_skill(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mod_dir = _write_mod(home, local_skill=False)
    _write_skill(mod_dir / "skills" / "nested" / "review-pack" / "SKILL.md", "review-pack", (), "Nested")
    mod = load_mod("review-pack", home)
    assert not mod.valid
    assert any("at least one packaged Skill" in error for error in mod.errors)


@pytest.mark.parametrize("selected_skill", ["missing", "", "../review-pack"])
def test_missing_explicit_skill_errors_without_standalone_fallback(tmp_path: Path, selected_skill: str) -> None:
    home = tmp_path / "home"
    _write_mod(home)
    _write_skill(home / "skills" / "missing" / "SKILL.md", "missing", (), "Standalone")
    with pytest.raises(ModResolutionError, match="unknown selected Skill"):
        resolve_mod("review-pack", home, selected_skill=selected_skill)


def test_same_local_skill_and_tool_ids_are_scoped_to_each_mod(tmp_path: Path) -> None:
    home = tmp_path / "home"
    for name in ("first", "second"):
        mod_dir = _write_mod(home, name, skill_name="review", tools=("custom-tool",))
        _write_tool(mod_dir / "tools", "custom-tool", result=name)
    assert all(mod.valid for mod in discover_mods(home).values())
    for name in ("first", "second"):
        resolved = resolve_mod(name, home)
        assert resolved.skill.name == "review"
        assert resolved.tools["custom-tool"]["execute"](tmp_path)["message"] == name


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("name: other\ndescription: Bad\nallowed-tools: []", "must match directory"),
        ("name: edit\ndescription: Bad", "explicitly declare allowed-tools"),
        ("name: edit\ndescription: Bad\nallowed-tools: missing-tool", "unknown Tool"),
        ("name: edit\ndescription: Bad\nallowed-tools: read_file read_file", "duplicate allowed-tools"),
        ("name: edit\nallowed-tools: []", "missing required frontmatter field"),
    ],
)
def test_invalid_non_default_skill_invalidates_entire_mod(tmp_path: Path, content: str, error: str) -> None:
    home = tmp_path / "home"
    mod_dir = _write_mod(home)
    path = mod_dir / "skills" / "edit" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\n{content}\n---\nInstructions.\n", encoding="utf-8")
    mod = discover_mods(home)["review-pack"]
    assert not mod.valid
    assert any(error in message for message in mod.errors)
    with pytest.raises(ModResolutionError, match=error):
        resolve_mod("review-pack", home)


def test_all_non_default_skills_are_validated_after_parse_failure(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mod_dir = _write_mod(home)
    broken = mod_dir / "skills" / "broken" / "SKILL.md"
    broken.parent.mkdir(parents=True)
    broken.write_text("not frontmatter", encoding="utf-8")
    _write_skill(mod_dir / "skills" / "other" / "SKILL.md", "wrong", (), "Wrong name")
    errors = load_mod("review-pack", home).errors
    assert any("must start with YAML frontmatter" in error for error in errors)
    assert any("must match directory" in error for error in errors)


def test_legacy_mod_tools_field_is_rejected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mod_dir = _write_mod(home)
    path = mod_dir / "mod.yaml"
    path.write_text(path.read_text().replace("hooks: []", "tools: []\nhooks: []"), encoding="utf-8")
    assert not load_mod("review-pack", home).valid
    with pytest.raises(ModResolutionError, match="unsupported mod.yaml field: tools"):
        resolve_mod("review-pack", home)


def test_default_skill_field_remains_required(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mod_dir = _write_mod(home)
    path = mod_dir / "mod.yaml"
    path.write_text(path.read_text().replace("default_skill: review-pack\n", ""), encoding="utf-8")
    with pytest.raises(ModResolutionError, match="missing required mod.yaml field: default_skill"):
        resolve_mod("review-pack", home)


def test_non_default_protected_builtin_collision_invalidates_package(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mod_dir = _write_mod(home)
    _write_skill(mod_dir / "skills" / "edit" / "SKILL.md", "edit", ("read_file",), "Edit")
    _write_tool(mod_dir / "tools", "read_file", result="replacement")
    with pytest.raises(ModResolutionError, match="cannot replace protected built-in"):
        resolve_mod("review-pack", home)
