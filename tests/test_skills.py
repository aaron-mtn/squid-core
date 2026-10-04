from pathlib import Path

import pytest

from squid_core.skills import discover_skills, parse_skill_file


def _write_skill(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_valid_skill_file(tmp_path: Path) -> None:
    path = _write_skill(
        tmp_path / "SKILL.md",
        """---
name: bug-fix
description: Fix bugs with small targeted edits.
allowed-tools: read_file apply_patch run_command
---

Focus on the requested bug.
""",
    )

    skill = parse_skill_file(path)

    assert skill.name == "bug-fix"
    assert skill.description == "Fix bugs with small targeted edits."
    assert skill.allowed_tools == ("read_file", "apply_patch", "run_command")
    assert skill.allowed_tools_explicit is True
    assert skill.instructions == "Focus on the requested bug."
    assert skill.path == path
    assert skill.source == "user"
    assert skill.editable is True


def test_missing_required_skill_fields_fail(tmp_path: Path) -> None:
    path = _write_skill(
        tmp_path / "SKILL.md",
        """---
name:
---

Instructions.
""",
    )

    try:
        parse_skill_file(path)
    except ValueError as error:
        assert "missing required frontmatter field: name" in str(error)
    else:
        raise AssertionError("Expected missing name to fail")


def test_allowed_tools_may_be_missing(tmp_path: Path) -> None:
    path = _write_skill(
        tmp_path / "SKILL.md",
        """---
name: review
description: Review only.
---

Review the code.
""",
    )

    skill = parse_skill_file(path)

    assert skill.allowed_tools == ()
    assert skill.allowed_tools_explicit is False


def test_allowed_tools_empty_block_is_explicit(tmp_path: Path) -> None:
    path = _write_skill(
        tmp_path / "SKILL.md",
        """---
name: review
description: Review only.
allowed-tools:
---

Review the code.
""",
    )

    skill = parse_skill_file(path)

    assert skill.allowed_tools == ()
    assert skill.allowed_tools_explicit is True


def test_allowed_tools_empty_inline_list_is_explicit(tmp_path: Path) -> None:
    path = _write_skill(
        tmp_path / "SKILL.md",
        """---
name: review
description: Review only.
allowed-tools: []
---

Review the code.
""",
    )

    skill = parse_skill_file(path)

    assert skill.allowed_tools == ()
    assert skill.allowed_tools_explicit is True


def test_allowed_tools_parse_yaml_style_list(tmp_path: Path) -> None:
    path = _write_skill(
        tmp_path / "SKILL.md",
        """---
name: review
description: Review only.
allowed-tools:
  - read_file
  - run_command
---

Review the code.
""",
    )

    skill = parse_skill_file(path)

    assert skill.allowed_tools == ("read_file", "run_command")


def test_allowed_tools_list_item_must_be_list_syntax(tmp_path: Path) -> None:
    path = _write_skill(
        tmp_path / "SKILL.md",
        """---
name: review
description: Review only.
allowed-tools:
  read_file
---

Review the code.
""",
    )

    with pytest.raises(ValueError, match="invalid frontmatter list item"):
        parse_skill_file(path)


def test_allowed_tools_list_item_must_be_single_tool_name(tmp_path: Path) -> None:
    path = _write_skill(
        tmp_path / "SKILL.md",
        """---
name: review
description: Review only.
allowed-tools:
  - read_file run_command
---

Review the code.
""",
    )

    with pytest.raises(ValueError, match="allowed-tools list items must be single tool names"):
        parse_skill_file(path)


def test_invalid_frontmatter_line_fails(tmp_path: Path) -> None:
    path = _write_skill(
        tmp_path / "SKILL.md",
        """---
name: review
description Review only.
---

Review the code.
""",
    )

    with pytest.raises(ValueError, match="invalid frontmatter line"):
        parse_skill_file(path)


def test_parse_skill_prompt_section_overrides(tmp_path: Path) -> None:
    path = _write_skill(
        tmp_path / "SKILL.md",
        """---
name: review
description: Review only.
---

Review instructions.

## FINAL_SUMMARY_REQUIREMENTS replace

Write a focused review.

## TOOL_USAGE_GUIDANCE append

Use inspection tools only.
""",
    )

    skill = parse_skill_file(path)

    assert skill.instructions == "Review instructions."
    assert skill.prompt_sections["FINAL_SUMMARY_REQUIREMENTS"].mode == "replace"
    assert skill.prompt_sections["FINAL_SUMMARY_REQUIREMENTS"].content == "Write a focused review."
    assert skill.prompt_sections["TOOL_USAGE_GUIDANCE"].mode == "append"
    assert skill.prompt_sections["TOOL_USAGE_GUIDANCE"].content == "Use inspection tools only."


def test_discover_skills_loads_home_user_skills(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_skill(
        home / "skills" / "my-skill" / "SKILL.md",
        """---
name: my-skill
description: User skill.
allowed-tools: run_command
---

Use my workflow.
""",
    )

    skills = discover_skills(app_home=home)

    assert set(skills) == {"my-skill"}
    assert skills["my-skill"].source == "user"
    assert skills["my-skill"].editable is True
    assert skills["my-skill"].path == home / "skills" / "my-skill" / "SKILL.md"


def test_discover_skills_honors_explicit_app_home(
    tmp_path: Path,
) -> None:
    home = tmp_path / "custom-code-squid-home"
    _write_skill(
        home / "skills" / "home-only" / "SKILL.md",
        """---
name: home-only
description: Home skill.
---

Home instructions.
""",
    )

    skills = discover_skills(app_home=home)

    assert "home-only" in skills
    assert skills["home-only"].path == home / "skills" / "home-only" / "SKILL.md"


def test_missing_home_user_skills_directory_is_harmless(tmp_path: Path) -> None:
    assert discover_skills(app_home=tmp_path / "missing-home") == {}


def test_unknown_prompt_section_override_fails(tmp_path: Path) -> None:
    path = _write_skill(
        tmp_path / "SKILL.md",
        """---
name: review
description: Review only.
---

## MADE_UP_SECTION replace

Nope.
""",
    )

    try:
        parse_skill_file(path)
    except ValueError as error:
        assert "unknown prompt section override: MADE_UP_SECTION" in str(error)
    else:
        raise AssertionError("Expected unknown prompt section override to fail")


def test_unsupported_prompt_section_override_fails(tmp_path: Path) -> None:
    path = _write_skill(
        tmp_path / "SKILL.md",
        """---
name: review
description: Review only.
---

## REQUIRED_RESPONSE_FORMAT replace

Nope.
""",
    )

    try:
        parse_skill_file(path)
    except ValueError as error:
        assert "prompt section override is not supported: REQUIRED_RESPONSE_FORMAT" in str(error)
    else:
        raise AssertionError("Expected unsupported prompt section override to fail")


def test_unsupported_prompt_section_mode_fails(tmp_path: Path) -> None:
    path = _write_skill(
        tmp_path / "SKILL.md",
        """---
name: review
description: Review only.
---

## TOOL_USAGE_GUIDANCE overwrite

Nope.
""",
    )

    try:
        parse_skill_file(path)
    except ValueError as error:
        assert "unsupported prompt section override mode: overwrite" in str(error)
    else:
        raise AssertionError("Expected unsupported prompt section mode to fail")
