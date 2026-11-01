"""Skill parsing and discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

PROMPT_SECTION_MODES = {"replace", "append"}
SUPPORTED_PROMPT_SECTIONS = {
    "AGENT_IDENTITY",
    "TOOL_USAGE_GUIDANCE",
    "FINAL_SUMMARY_REQUIREMENTS",
}
UNSUPPORTED_PROMPT_SECTIONS = {
    "VISIBLE_NARRATION_REQUIREMENTS",
    "REQUIRED_RESPONSE_FORMAT",
    "CORRECTION_MESSAGES",
    "COMPACTION_PROMPT",
}

# These names describe Core-owned text, never application extension slots.
PROTECTED_PROMPT_SECTIONS = UNSUPPORTED_PROMPT_SECTIONS | {
    "EXECUTION_ENVIRONMENT", "RESPONSE_PROTOCOL", "VERIFICATION_GUIDANCE",
    "RESPONSE_EXAMPLES", "PROTECTED_RESPONSE_PROTOCOL",
    "PROTECTED_VERIFICATION_GUIDANCE", "PROTECTED_RESPONSE_EXAMPLES", "ACTIVE_SKILL",
}


@dataclass(frozen=True)
class PromptSectionOverride:
    section: str
    mode: str
    content: str


@dataclass(frozen=True)
class Skill:
    """Instructions and permitted tools; an omitted tool list retains host defaults."""

    name: str
    description: str
    instructions: str
    allowed_tools: tuple[str, ...]
    path: Path
    allowed_tools_explicit: bool = False
    source: str = "user"
    editable: bool = True
    prompt_sections: dict[str, PromptSectionOverride] = field(default_factory=dict)
    disabled_prompt_sections: tuple[str, ...] = ()


def select_skill_tools(skill: Skill | None, available_tools: dict) -> dict:
    """Restrict a supplied registry to a Skill's permissions without adding tools."""
    if skill is None or (not skill.allowed_tools and not skill.allowed_tools_explicit):
        return available_tools

    # Empty explicit lists disable tools. Non-empty programmatic lists also
    # constrain permissions even when no frontmatter provenance was supplied.
    for name in skill.allowed_tools:
        if name not in available_tools:
            raise ValueError(f"{skill.path}: unknown Tool {name!r}")
    # A registry already resolved by the host needs no copy or reordering.
    if tuple(available_tools) == skill.allowed_tools:
        return available_tools
    return {name: available_tools[name] for name in skill.allowed_tools}


def parse_skill_file(
    path: Path, *, source: str = "user", editable: bool = True,
    supported_disable_sections: frozenset[str] | None = None,
) -> Skill:
    text = path.read_text(encoding="utf-8")
    frontmatter, instructions = _split_frontmatter(text, path)
    data = _parse_simple_frontmatter(frontmatter, path)

    name = _frontmatter_scalar(data, "name").strip()
    description = _frontmatter_scalar(data, "description").strip()
    if not name:
        raise ValueError(f"{path}: missing required frontmatter field: name")
    if not description:
        raise ValueError(f"{path}: missing required frontmatter field: description")

    allowed_tools_explicit = "allowed-tools" in data
    allowed_tools = _parse_allowed_tools(data.get("allowed-tools", ""), path)

    instructions, disabled = _extract_disabled_prompt_sections(instructions, path)
    instructions, prompt_sections = _parse_prompt_section_overrides(instructions, path)
    validate_disabled_prompt_sections(disabled, prompt_sections, path, supported_disable_sections)

    return Skill(
        name=name,
        description=description,
        instructions=instructions.strip(),
        allowed_tools=allowed_tools,
        path=path,
        allowed_tools_explicit=allowed_tools_explicit,
        source=source,
        editable=editable,
        prompt_sections=prompt_sections,
        disabled_prompt_sections=disabled,
    )


def discover_skills(
    *,
    app_home: Path | str,
    supported_disable_sections: frozenset[str] | None = None,
) -> dict[str, Skill]:
    """Discover standalone Skills under the supplied application home."""

    return _discover_skills_in(
        Path(app_home) / "skills",
        source="user",
        editable=True,
        supported_disable_sections=supported_disable_sections,
    )


def _discover_skills_in(directory: Path, *, source: str, editable: bool,
                        supported_disable_sections: frozenset[str] | None = None) -> dict[str, Skill]:
    if not directory.exists():
        return {}

    skills = {}
    for skill_file in sorted(directory.glob("*/SKILL.md")):
        skill = parse_skill_file(skill_file, source=source, editable=editable,
                                 supported_disable_sections=supported_disable_sections)
        skills[skill.name] = skill
    return skills


def validate_disabled_prompt_sections(disabled, overrides, path, supported=None) -> None:
    """Validate text omission independently of tool permissions and execution.

    Hosts supply their explicitly named text slots; file parsing and programmatic
    Agent construction use this same guard so neither path bypasses the contract.
    """
    supported = SUPPORTED_PROMPT_SECTIONS if supported is None else supported
    seen = set()
    for name in disabled:
        if not isinstance(name, str) or name not in supported:
            if isinstance(name, str) and name in PROTECTED_PROMPT_SECTIONS:
                raise ValueError(f"{path}: protected prompt section cannot be disabled: {name}")
            raise ValueError(f"{path}: unknown disabled prompt section: {name}")
        if name in PROTECTED_PROMPT_SECTIONS:
            raise ValueError(f"{path}: protected prompt section cannot be disabled: {name}")
        if name in seen:
            raise ValueError(f"{path}: duplicate disabled prompt section: {name}")
        if name in overrides:
            raise ValueError(f"{path}: prompt section cannot be disabled and overridden: {name}")
        seen.add(name)


def _extract_disabled_prompt_sections(instructions: str, path: Path) -> tuple[str, tuple[str, ...]]:
    """Remove only the exact directive and its list, leaving override parsing intact.

    A following Markdown heading closes the directive. Fenced examples are ordinary
    text, not directives. A directive inside an override closes that override too;
    extraction leaves its heading as a boundary for the existing override parser.
    """
    output = []
    disabled = []
    active = False
    found = False
    fence = None
    for number, line in enumerate(instructions.splitlines(), 1):
        stripped = line.strip()
        if not active and stripped.startswith(("```", "~~~")):
            marker = stripped[0]
            fence = None if fence == marker else (marker if fence is None else fence)
        if fence is None and line == "## DISABLE_PROMPT_SECTIONS":
            if found:
                raise ValueError(f"{path}:{number}: duplicate DISABLE_PROMPT_SECTIONS directive")
            found = active = True
            output.append(line)
            continue
        if active:
            if line.startswith("#"):
                active = False
            elif not stripped:
                continue
            elif line.startswith("- ") and len(line[2:].split()) == 1:
                disabled.append(line[2:].strip())
                continue
            else:
                raise ValueError(f"{path}:{number}: DISABLE_PROMPT_SECTIONS requires '- SECTION_NAME' entries")
        output.append(line)
    return "\n".join(output), tuple(disabled)


def _split_frontmatter(text: str, path: Path) -> tuple[str, str]:
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: SKILL.md must start with YAML frontmatter")

    end_marker = "\n---\n"
    end = text.find(end_marker, 4)
    if end == -1:
        raise ValueError(f"{path}: SKILL.md frontmatter must end with ---")

    return text[4:end], text[end + len(end_marker) :]


def _parse_simple_frontmatter(frontmatter: str, path: Path) -> dict[str, str | list[str]]:
    data: dict[str, str | list[str]] = {}
    current_list_key: str | None = None
    for line_number, line in enumerate(frontmatter.splitlines(), start=2):
        stripped = line.strip()
        if not stripped:
            continue

        if current_list_key is not None and line[:1].isspace():
            if not stripped.startswith("- "):
                raise ValueError(f"{path}:{line_number}: invalid frontmatter list item")
            item = stripped[2:].strip().strip("\"'")
            if not item:
                raise ValueError(f"{path}:{line_number}: invalid empty frontmatter list item")
            current_value = data[current_list_key]
            if not isinstance(current_value, list):
                current_value = []
                data[current_list_key] = current_value
            current_value.append(item)
            continue

        current_list_key = None
        if ":" not in stripped:
            raise ValueError(f"{path}:{line_number}: invalid frontmatter line")
        key, value = stripped.split(":", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"{path}:{line_number}: invalid empty frontmatter key")
        data[key] = value.strip().strip("\"'")
        if not data[key]:
            current_list_key = key
    return data


def _frontmatter_scalar(data: dict[str, str | list[str]], key: str) -> str:
    value = data.get(key, "")
    if isinstance(value, list):
        return ""
    return value


def _parse_allowed_tools(value: str | list[str], path: Path) -> tuple[str, ...]:
    if isinstance(value, list):
        invalid_items = [item for item in value if len(item.split()) != 1]
        if invalid_items:
            raise ValueError(f"{path}: allowed-tools list items must be single tool names")
        return tuple(value)

    allowed_tools_text = value.strip()
    if allowed_tools_text == "[]":
        return ()
    return tuple(allowed_tools_text.split()) if allowed_tools_text else ()


def _parse_prompt_section_overrides(
    instructions: str,
    path: Path,
) -> tuple[str, dict[str, PromptSectionOverride]]:
    lines = instructions.splitlines()
    normal_lines = []
    prompt_sections = {}
    current_section = None
    current_mode = None
    current_content = []
    directive_fence = None

    for line_number, line in enumerate(lines, start=1):
        if line.strip().startswith(("```", "~~~")):
            marker = line.strip()[0]
            directive_fence = None if directive_fence == marker else (marker if directive_fence is None else directive_fence)
        if directive_fence is None and line == "## DISABLE_PROMPT_SECTIONS":
            if current_section is not None:
                prompt_sections[current_section] = PromptSectionOverride(
                    current_section, current_mode, "\n".join(current_content).strip(),
                )
            current_section = current_mode = None
            current_content = []
            continue
        heading = _parse_prompt_section_heading(line, path, line_number)
        if heading is None:
            if current_section is None:
                normal_lines.append(line)
            else:
                current_content.append(line)
            continue

        if current_section is not None:
            prompt_sections[current_section] = PromptSectionOverride(
                section=current_section,
                mode=current_mode,
                content="\n".join(current_content).strip(),
            )

        current_section, current_mode = heading
        current_content = []

    if current_section is not None:
        prompt_sections[current_section] = PromptSectionOverride(
            section=current_section,
            mode=current_mode,
            content="\n".join(current_content).strip(),
        )

    return "\n".join(normal_lines).strip(), prompt_sections


def _parse_prompt_section_heading(
    line: str,
    path: Path,
    line_number: int,
) -> tuple[str, str] | None:
    if not line.startswith("## "):
        return None

    parts = line[3:].strip().split()
    if len(parts) != 2:
        return None

    section, mode = parts
    looks_like_prompt_section = section.isupper() and "_" in section
    if not looks_like_prompt_section:
        return None

    if section in UNSUPPORTED_PROMPT_SECTIONS:
        raise ValueError(f"{path}:{line_number}: prompt section override is not supported: {section}")
    if section not in SUPPORTED_PROMPT_SECTIONS:
        raise ValueError(f"{path}:{line_number}: unknown prompt section override: {section}")
    if mode not in PROMPT_SECTION_MODES:
        raise ValueError(f"{path}:{line_number}: unsupported prompt section override mode: {mode}")

    return section, mode
