"""Skill parsing and discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

PROMPT_SECTION_MODES = {"replace", "append"}
SUPPORTED_PROMPT_SECTIONS = {
    "SYSTEM_PROMPT_INTRO",
    "TOOL_USAGE_GUIDANCE",
    "FINAL_SUMMARY_REQUIREMENTS",
}
UNSUPPORTED_PROMPT_SECTIONS = {
    "VISIBLE_NARRATION_REQUIREMENTS",
    "REQUIRED_RESPONSE_FORMAT",
    "CORRECTION_MESSAGES",
    "COMPACTION_PROMPT",
}


@dataclass(frozen=True)
class PromptSectionOverride:
    section: str
    mode: str
    content: str


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    instructions: str
    allowed_tools: tuple[str, ...]
    path: Path
    allowed_tools_explicit: bool = False
    source: str = "user"
    editable: bool = True
    prompt_sections: dict[str, PromptSectionOverride] = field(default_factory=dict)


def parse_skill_file(path: Path, *, source: str = "user", editable: bool = True) -> Skill:
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

    instructions, prompt_sections = _parse_prompt_section_overrides(instructions, path)

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
    )


def discover_skills(
    *,
    app_home: Path | str,
) -> dict[str, Skill]:
    """Discover standalone Skills under the supplied application home."""

    return _discover_skills_in(
        Path(app_home) / "skills",
        source="user",
        editable=True,
    )


def _discover_skills_in(directory: Path, *, source: str, editable: bool) -> dict[str, Skill]:
    if not directory.exists():
        return {}

    skills = {}
    for skill_file in sorted(directory.glob("*/SKILL.md")):
        skill = parse_skill_file(skill_file, source=source, editable=editable)
        skills[skill.name] = skill
    return skills


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

    for line_number, line in enumerate(lines, start=1):
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
