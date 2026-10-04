"""Canonical Mod metadata and runtime resolution.

Discovery and validation read metadata only.  Tool and Hook Python source is
imported only when an explicitly selected runtime executes a Tool or resolves
its selected Hooks, respectively.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any

from squid_core.hooks import HookInfo, HookLoadError, load_hook_infos, read_hook_info
from squid_core.skills import Skill, parse_skill_file
from squid_core.tools.builtins import TOOLS
from squid_core.tools.user_tools import (
    UserTool,
    UserToolError,
    build_user_tool_registry_from_metadata,
    discover_user_tools,
    load_user_tool,
)
from .model import (
    ALLOWED_MOD_ACCENTS,
    DEFAULT_MOD_ACCENT,
    MOD_FIELDS,
    SAFE_COMPONENT_NAME_RE,
    SAFE_MOD_NAME_RE,
    Mod,
    ModMetadataError,
    ModResolutionError,
    ResolvedMod,
)


def discover_mods(app_home: Path | str) -> dict[str, Mod]:
    """Discover ``mods/*/mod.yaml`` below the supplied application home."""
    root = Path(app_home) / "mods"
    if not root.is_dir():
        return {}

    mods: dict[str, Mod] = {}
    for mod_dir in sorted(
        (path for path in root.iterdir() if path.is_dir()),
        key=lambda path: (path.name.lower(), path.name),
    ):
        mod = _load_mod_dir(mod_dir, app_home=Path(app_home))
        if mod is not None:
            mods[mod_dir.name] = mod
    return mods


def load_mod(name: str, app_home: Path | str) -> Mod:
    """Load one named Mod from the supplied application home."""
    if not isinstance(name, str) or not SAFE_MOD_NAME_RE.fullmatch(name):
        raise ModResolutionError(f"Invalid Mod name: {name!r}")
    root = (Path(app_home) / "mods").resolve()
    mod_dir = (root / name).resolve()
    if mod_dir == root or root not in mod_dir.parents:
        raise ModResolutionError(f"Invalid Mod path: {name!r}")
    mod = _load_mod_dir(mod_dir, app_home=Path(app_home))
    if mod is None:
        raise ModResolutionError(f"Unknown Mod: {name}")
    return mod


def validate_mod(
    mod: Mod,
    app_home: Path | str,
    *,
    built_in_tools: dict | None = None,
) -> tuple[str, ...]:
    """Validate a Mod and referenced metadata without importing source code."""
    # A manifest parse failure produces placeholder metadata. Preserve its
    # actual diagnostic instead of validating the placeholder's empty fields.
    if not mod.default_skill and mod.errors:
        return mod.errors
    errors = list(_validate_manifest(mod))
    if errors:
        return tuple(errors)

    skills, skill_errors = _discover_packaged_skills(mod)
    errors.extend(skill_errors)
    protected_tools = TOOLS if built_in_tools is None else built_in_tools
    # Validate every profile's dependencies, including inactive Skills. Never
    # turn this package-wide inventory into the runtime's active permissions.
    for skill in skills.values():
        try:
            _select_tool_metadata(mod, Path(app_home), protected_tools, skill.allowed_tools)
        except (ModResolutionError, UserToolError) as error:
            errors.append(str(error))
    try:
        _select_hook_metadata(mod, Path(app_home))
    except (ModResolutionError, HookLoadError) as error:
        errors.append(str(error))
    return tuple(errors)


def resolve_mod(
    name: str,
    app_home: Path | str,
    *,
    selected_skill: str | None = None,
    repo_root: Path | str | None = None,
    session_id: str | None = None,
    built_in_tools: dict | None = None,
) -> ResolvedMod:
    """Resolve one packaged Skill (default when omitted) and Mod-wide Hooks."""
    app_home = Path(app_home)
    protected_tools = TOOLS if built_in_tools is None else built_in_tools
    mod = load_mod(name, app_home)
    errors = validate_mod(mod, app_home, built_in_tools=protected_tools)
    if errors:
        raise ModResolutionError(
            f"Mod {name} is invalid:\n" + "\n".join(f"- {error}" for error in errors)
        )

    skill_name = mod.default_skill if selected_skill is None else selected_skill
    if skill_name not in mod.skills:
        raise ModResolutionError(f"Mod {name!r}: unknown selected Skill {skill_name!r}")
    skill = mod.skills[skill_name]
    # Only the selected Skill grants active tools; other profiles are validation
    # inputs, never extra permissions for the ToolExecutor.
    selected_metadata = _select_tool_metadata(mod, app_home, protected_tools, skill.allowed_tools)
    custom_registry = build_user_tool_registry_from_metadata(
        selected_metadata,
        repo_root=repo_root,
        session_id=session_id,
    )
    available_tools = {**custom_registry, **protected_tools}
    tools = {tool_name: available_tools[tool_name] for tool_name in skill.allowed_tools}
    hooks = tuple(load_hook_infos(_select_hook_metadata(mod, app_home)))
    return ResolvedMod(metadata=replace(mod, valid=True, errors=()), skill=skill, tools=tools, hooks=hooks)


def _load_mod_dir(mod_dir: Path, *, app_home: Path) -> Mod | None:
    metadata_path = mod_dir / "mod.yaml"
    if not metadata_path.is_file():
        return None
    try:
        metadata = _parse_mod_file(metadata_path)
        mod = Mod(
            name=_string(metadata, "name", metadata_path),
            description=_string(metadata, "description", metadata_path),
            default_skill=_string(metadata, "default_skill", metadata_path),
            accent=_string(metadata, "accent", metadata_path, default=DEFAULT_MOD_ACCENT),
            hooks=_string_list(metadata, "hooks", metadata_path),
            path=mod_dir,
        )
        skills, _ = _discover_packaged_skills(mod)
        mod = replace(mod, skills=skills)
        errors = validate_mod(mod, app_home)
        return replace(mod, valid=not errors, errors=errors)
    except (ModMetadataError, OSError) as error:
        return Mod(
            name=mod_dir.name,
            description="",
            default_skill="",
            accent=DEFAULT_MOD_ACCENT,
            hooks=(),
            path=mod_dir,
            valid=False,
            errors=(str(error),),
        )


def _validate_manifest(mod: Mod) -> tuple[str, ...]:
    path = mod.path / "mod.yaml"
    errors: list[str] = []
    if not SAFE_MOD_NAME_RE.fullmatch(mod.name):
        errors.append(f"{path}: Mod name must use lowercase letters, numbers, and single hyphens")
    if mod.name != mod.path.name:
        errors.append(f"{path}: Mod name {mod.name!r} must match folder name {mod.path.name!r}")
    if not mod.description.strip():
        errors.append(f"{path}: description must not be empty")
    if not mod.default_skill or not SAFE_MOD_NAME_RE.fullmatch(mod.default_skill):
        errors.append(f"{path}: default_skill must be a safe non-empty name")
    if mod.accent not in ALLOWED_MOD_ACCENTS:
        errors.append(f"{path}: accent {mod.accent!r} must be one of: {', '.join(ALLOWED_MOD_ACCENTS)}")
    for label, names in (("hooks", mod.hooks),):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            errors.append(f"{path}: duplicate {label}: {', '.join(duplicates)}")
        invalid = [name for name in names if not SAFE_COMPONENT_NAME_RE.fullmatch(name)]
        if invalid:
            errors.append(f"{path}: invalid {label}: {', '.join(invalid)}")
    return tuple(errors)


def _discover_packaged_skills(mod: Mod) -> tuple[dict[str, Skill], list[str]]:
    """Read direct packaged children without consulting standalone Skills."""
    skills: dict[str, Skill] = {}
    errors: list[str] = []
    paths = sorted((mod.path / "skills").glob("*/SKILL.md"))
    if not paths:
        errors.append(f"{mod.path}: Mod must contain at least one packaged Skill")
    for path in paths:
        skill_id = path.parent.name
        try:
            skill = parse_skill_file(path, source="mod", editable=False)
        except (OSError, ValueError) as error:
            errors.append(str(error))
            continue
        # Directory identity keeps duplicate names from silently overwriting a
        # sibling and permits the same local ID in unrelated Mod packages.
        if not SAFE_MOD_NAME_RE.fullmatch(skill_id):
            errors.append(f"{path}: Skill directory must be a safe non-empty name")
        if skill.name != skill_id:
            errors.append(f"{path}: Skill name {skill.name!r} must match directory {skill_id!r}")
        if not skill.allowed_tools_explicit:
            errors.append(f"{path}: Mod Skills must explicitly declare allowed-tools")
        allowed = skill.allowed_tools
        duplicates = sorted({name for name in allowed if allowed.count(name) > 1})
        if duplicates:
            errors.append(f"{path}: duplicate allowed-tools: {', '.join(duplicates)}")
        invalid = [name for name in allowed if not SAFE_COMPONENT_NAME_RE.fullmatch(name)]
        if invalid:
            errors.append(f"{path}: invalid allowed-tools: {', '.join(invalid)}")
        skills[skill_id] = skill
    if mod.default_skill not in skills:
        errors.append(
            f"{mod.path / 'mod.yaml'}: unknown default Skill {mod.default_skill!r}; "
            "must name a packaged Skill in this Mod"
        )
    return skills, errors


def _select_tool_metadata(
    mod: Mod,
    app_home: Path,
    built_in_tools: dict,
    tool_names: tuple[str, ...],
) -> dict[str, UserTool]:
    loose = discover_user_tools(app_home=app_home)
    selected: dict[str, UserTool] = {}
    for name in tool_names:
        local_dir = mod.path / "tools" / name
        if name in built_in_tools:
            if local_dir.exists():
                raise ModResolutionError(
                    f"{local_dir}: Mod Tool cannot replace protected built-in Tool {name!r}"
                )
            continue
        if local_dir.is_dir():
            tool = load_user_tool(local_dir, source="mod", editable=False)
            if tool is None:
                raise ModResolutionError(f"{local_dir}: missing tool.yaml")
        else:
            tool = loose.get(name)
            if tool is None:
                raise ModResolutionError(f"{mod.path / 'mod.yaml'}: unknown Tool {name!r}")
        selected[name] = tool
    return selected


def _select_hook_metadata(mod: Mod, app_home: Path) -> list[HookInfo]:
    selected: list[HookInfo] = []
    loose_root = app_home / "hooks"
    for name in mod.hooks:
        local_dir = mod.path / "hooks" / name
        hook_dir = local_dir if local_dir.is_dir() else loose_root / name
        if not hook_dir.is_dir():
            raise ModResolutionError(f"{mod.path / 'mod.yaml'}: unknown Hook {name!r}")
        info = read_hook_info(
            hook_dir,
            source="mod" if hook_dir == local_dir else "user",
            enabled_override=True,
        )
        if info.name != name:
            raise ModResolutionError(
                f"{hook_dir / 'hook.yaml'}: Hook name {info.name!r} must match {name!r}"
            )
        selected.append(info)
    return selected


def _parse_mod_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ModMetadataError(f"{path}: mod.yaml must have opening and closing --- delimiters")
    closing = [index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"]
    if len(closing) != 1 or any(line.strip() for line in lines[closing[0] + 1:]):
        raise ModMetadataError(f"{path}: mod.yaml must have opening and closing --- delimiters")

    data: dict[str, Any] = {}
    current_list: str | None = None
    for line_number, line in enumerate(lines[1:closing[0]], start=2):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if current_list is not None and line.startswith("  - "):
            item = line[4:].strip().strip("\"'")
            if not item:
                raise ModMetadataError(f"{path}:{line_number}: empty list item")
            data[current_list].append(item)
            continue
        current_list = None
        if line[:1].isspace() or ":" not in line:
            raise ModMetadataError(f"{path}:{line_number}: invalid Mod metadata line")
        key, raw = line.split(":", 1)
        key, raw = key.strip(), raw.strip()
        if key not in MOD_FIELDS:
            raise ModMetadataError(f"{path}:{line_number}: unsupported mod.yaml field: {key}")
        if key in data:
            raise ModMetadataError(f"{path}:{line_number}: duplicate mod.yaml field: {key}")
        if key == "hooks":
            if raw == "":
                data[key] = []
                current_list = key
            elif raw == "[]":
                data[key] = []
            else:
                try:
                    data[key] = json.loads(raw)
                except json.JSONDecodeError as error:
                    raise ModMetadataError(f"{path}:{line_number}: {key} must be a list") from error
        else:
            data[key] = raw.strip("\"'")
    return data


def _string(data: dict[str, Any], key: str, path: Path, *, default: str | None = None) -> str:
    if key not in data:
        if default is not None:
            return default
        raise ModMetadataError(f"{path}: missing required mod.yaml field: {key}")
    value = data[key]
    if not isinstance(value, str):
        raise ModMetadataError(f"{path}: {key} must be a string")
    return value.strip()


def _string_list(data: dict[str, Any], key: str, path: Path) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ModMetadataError(f"{path}: {key} must be a list of strings")
    return tuple(item.strip() for item in value)
