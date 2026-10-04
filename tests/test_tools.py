import importlib
from pathlib import Path
import shlex
import sys

import pytest

from squid_core.tools.builtins.read_file import execute as read_file
from squid_core.tools.builtins.read_file import tool_definition as read_file_definition
from squid_core.tools.builtins._truncation import truncate_head_tail
from squid_core.tools.builtins import TOOLS
from squid_core.tools.builtins.run_command import execute as run_command
from squid_core.tools.builtins.apply_patch import execute as apply_patch
from squid_core.tools.builtins.write_file import execute as write_file
from squid_core.settings import ReadFileSettings


run_command_module = importlib.import_module("squid_core.tools.builtins.run_command")


def _python_script_command(script_path: Path) -> str:
    """Return a shell-safe command that runs one Python regression fixture."""
    if sys.platform == "win32":
        executable = sys.executable.replace("'", "''")
        path = str(script_path).replace("'", "''")
        return f"& '{executable}' '{path}'"
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script_path))}"


def test_read_file_rejects_relative_path_and_succeeds_with_absolute_path(tmp_path: Path) -> None:
    file_path = tmp_path / "input.txt"
    file_path.write_text("hello", encoding="utf-8")

    relative_result = read_file(tmp_path, "input.txt")
    absolute_result = read_file(tmp_path, str(file_path))

    assert relative_result == {
        "ok": False,
        "tool": "read_file",
        "path": "input.txt",
        "error": "read_file requires an absolute path",
    }
    assert absolute_result == {
        "ok": True,
        "tool": "read_file",
        "path": str(file_path),
        "content": "hello",
        "content_truncated": False,
        "content_original_chars": 5,
        "content_returned_chars": 5,
        "start_line": 1,
        "end_line": 1,
        "total_lines": 1,
        "ranged": False,
    }


def test_run_command_uses_selected_repo_root_as_cwd(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = (
        f"& '{sys.executable}'"
        if sys.platform == "win32"
        else shlex.quote(sys.executable)
    )

    result = run_command(
        workspace,
        f'{executable} -c "from pathlib import Path; print(Path.cwd())"',
        verification=False,
    )

    assert result["ok"] is True
    assert result["output"] == f"{workspace.resolve()}\n"
    assert result["verification"] is False


def test_run_command_exposes_and_returns_explicit_verification_intent(
    tmp_path: Path,
) -> None:
    parameters = TOOLS["run_command"]["definition"]()["function"]["parameters"]

    assert parameters["properties"]["verification"] == {
        "type": "boolean",
        "description": (
            "REQUIRED. True when this command checks whether the current "
            "implementation is correct; false otherwise."
        ),
    }
    assert parameters["required"] == ["command", "verification"]

    executable = (
        f"& '{sys.executable}'"
        if sys.platform == "win32"
        else shlex.quote(sys.executable)
    )
    result = run_command(
        tmp_path,
        f'{executable} -c "print(\'verified\')"',
        verification=True,
    )

    assert result["ok"] is True
    assert result["verification"] is True


def test_run_command_preserves_explicit_non_verification_intent(tmp_path: Path) -> None:
    executable = (
        f"& '{sys.executable}'"
        if sys.platform == "win32"
        else shlex.quote(sys.executable)
    )
    result = run_command(
        tmp_path,
        f'{executable} -c "print(\'inspected\')"',
        verification=False,
    )

    assert result["ok"] is True
    assert result["verification"] is False


def test_run_command_captures_utf8_stdout_stderr_and_nonzero_exit(
    tmp_path: Path,
) -> None:
    script_path = tmp_path / "unicode_output.py"
    script_path.write_text(
        """import sys

sys.stdout.buffer.write("日本語🦑—\\n".encode("utf-8"))
sys.stdout.buffer.flush()
sys.stderr.buffer.write("標準エラー🦑—\\n".encode("utf-8"))
sys.stderr.buffer.flush()
raise SystemExit(1)
""",
        encoding="utf-8",
    )

    result = run_command(
        tmp_path,
        _python_script_command(script_path),
        verification=False,
    )

    assert result["ok"] is False
    assert result["returncode"] == 1
    assert result["output"] == "日本語🦑—\n標準エラー🦑—\n"
    assert result["failure_class"] == "process_failed"


def test_run_command_replaces_malformed_utf8_without_crashing(tmp_path: Path) -> None:
    script_path = tmp_path / "malformed_output.py"
    script_path.write_text(
        """import sys

sys.stdout.buffer.write(b"valid\\xfftail\\n")
sys.stdout.buffer.flush()
""",
        encoding="utf-8",
    )

    result = run_command(
        tmp_path,
        _python_script_command(script_path),
        verification=False,
    )

    assert result["ok"] is True
    assert result["returncode"] == 0
    assert result["output"] == "valid\ufffdtail\n"


def test_read_file_supports_one_targeted_range(tmp_path: Path) -> None:
    file_path = tmp_path / "input.txt"
    file_path.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

    result = read_file(
        tmp_path,
        str(file_path),
        ranges=[{"start_line": 2, "end_line": 3}],
    )

    assert result["content"] == "one\ntwo\nthree\nfour\n"
    assert result["start_line"] == 1
    assert result["end_line"] == 4
    assert result["returned_line_ranges"] == [
        {"start_line": 1, "end_line": 4}
    ]
    assert result["total_lines"] == 4
    assert result["ranged"] is True


@pytest.mark.parametrize("line_count", [999, 1000])
def test_read_file_returns_full_content_at_or_under_line_limit(
    tmp_path: Path, line_count: int
) -> None:
    file_path = tmp_path / f"{line_count}-lines.txt"
    # Exceed the old shared character limit so this also proves that line count
    # is now the only limit on full-file reads.
    original_content = "".join(
        f"line {number}: {'x' * 20}\n" for number in range(1, line_count + 1)
    )
    file_path.write_text(original_content, encoding="utf-8")

    result = read_file(tmp_path, str(file_path))

    assert result["content"] == original_content
    assert result["content_truncated"] is False
    assert result["content_original_chars"] == len(original_content)
    assert result["content_returned_chars"] == len(original_content)
    assert "returned_line_ranges" not in result
    assert "truncation_guidance" not in result


def test_read_file_truncates_large_full_read_by_lines_and_reports_ranges(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "1001-lines.txt"
    lines = [f"line {number}\n" for number in range(1, 1002)]
    original_content = "".join(lines)
    separator = "<<< CODE SQUID: lines 501-501 omitted from this read >>>\n"
    expected_content = "".join((*lines[:500], separator, *lines[-500:]))
    file_path.write_text(original_content, encoding="utf-8")

    result = read_file(tmp_path, str(file_path))

    assert result["content"] == expected_content
    assert len(result["content"].splitlines()) == 1001
    assert "line 500\n" + separator + "line 502\n" in result["content"]
    assert result["content_truncated"] is True
    assert result["content_original_chars"] == len(original_content)
    assert result["content_returned_chars"] == len(expected_content)
    assert result["start_line"] == 1
    assert result["end_line"] == 1001
    assert result["total_lines"] == 1001
    assert result["returned_line_ranges"] == [
        {"start_line": 1, "end_line": 500},
        {"start_line": 502, "end_line": 1001},
    ]
    assert result["truncation_guidance"] == (
        "Full-file read truncated to 1000 of 1001 lines; returned lines 1-500 and "
        "502-1001. Use ranges to inspect the missing relevant section."
    )


def test_read_file_rejects_one_range_over_total_line_budget(tmp_path: Path) -> None:
    file_path = tmp_path / "large-range.txt"
    lines = [f"line {number}\n" for number in range(1, 1201)]
    file_path.write_text("".join(lines), encoding="utf-8")

    result = read_file(
        tmp_path,
        str(file_path),
        ranges=[{"start_line": 1, "end_line": 1200}],
    )

    assert result["ok"] is False
    assert result["error"] == (
        "targeted read covers 1200 lines after normalization; "
        "maximum total coverage is 1000 lines"
    )


def test_small_middle_range_expands_with_balanced_context(tmp_path: Path) -> None:
    file_path = tmp_path / "middle.txt"
    lines = [f"line {number}\n" for number in range(1, 1001)]
    file_path.write_text("".join(lines), encoding="utf-8")

    result = read_file(
        tmp_path,
        str(file_path),
        ranges=[{"start_line": 520, "end_line": 560}],
    )

    assert result["start_line"] == 466
    assert result["end_line"] == 615
    assert len(result["content"].splitlines()) == 150
    assert result["content"].startswith("line 466\n")
    assert result["content"].endswith("line 615\n")


def test_small_range_near_beginning_shifts_expansion_inside_file(tmp_path: Path) -> None:
    file_path = tmp_path / "beginning.txt"
    lines = [f"line {number}\n" for number in range(1, 301)]
    file_path.write_text("".join(lines), encoding="utf-8")

    result = read_file(
        tmp_path,
        str(file_path),
        ranges=[{"start_line": 10, "end_line": 20}],
    )

    assert (result["start_line"], result["end_line"]) == (1, 150)
    assert result["content"] == "".join(lines[:150])


def test_small_range_near_end_shifts_expansion_inside_file(tmp_path: Path) -> None:
    file_path = tmp_path / "end.txt"
    lines = [f"line {number}\n" for number in range(1, 301)]
    file_path.write_text("".join(lines), encoding="utf-8")

    result = read_file(
        tmp_path,
        str(file_path),
        ranges=[{"start_line": 290, "end_line": 300}],
    )

    assert (result["start_line"], result["end_line"]) == (151, 300)
    assert result["content"] == "".join(lines[150:])


def test_range_at_or_above_minimum_is_unchanged(tmp_path: Path) -> None:
    file_path = tmp_path / "large-enough.txt"
    lines = [f"line {number}\n" for number in range(1, 401)]
    file_path.write_text("".join(lines), encoding="utf-8")

    result = read_file(
        tmp_path,
        str(file_path),
        ranges=[{"start_line": 100, "end_line": 249}],
    )

    assert (result["start_line"], result["end_line"]) == (100, 249)
    assert result["content"] == "".join(lines[99:249])


def test_read_file_returns_disjoint_ranges_in_source_order(tmp_path: Path) -> None:
    file_path = tmp_path / "scattered.txt"
    lines = [f"line {number}\n" for number in range(1, 21)]
    file_path.write_text("".join(lines), encoding="utf-8")

    result = read_file(
        tmp_path,
        str(file_path),
        ranges=[
            {"start_line": 15, "end_line": 17},
            {"start_line": 2, "end_line": 4},
        ],
        settings=ReadFileSettings(default_max_lines=10, minimum_range_lines=1),
    )

    assert result["content"] == "".join(
        (
            *lines[1:4],
            "<<< CODE SQUID: lines 5-14 omitted from this read >>>\n",
            *lines[14:17],
        )
    )
    assert result["returned_line_ranges"] == [
        {"start_line": 2, "end_line": 4},
        {"start_line": 15, "end_line": 17},
    ]
    assert (result["start_line"], result["end_line"]) == (2, 17)
    assert result["ranged"] is True


@pytest.mark.parametrize(
    ("ranges", "expected"),
    [
        (
            [
                {"start_line": 2, "end_line": 6},
                {"start_line": 5, "end_line": 9},
            ],
            {"start_line": 2, "end_line": 9},
        ),
        (
            [
                {"start_line": 2, "end_line": 6},
                {"start_line": 7, "end_line": 9},
            ],
            {"start_line": 2, "end_line": 9},
        ),
    ],
)
def test_read_file_merges_overlapping_and_adjacent_ranges(
    tmp_path: Path,
    ranges: list[dict[str, int]],
    expected: dict[str, int],
) -> None:
    file_path = tmp_path / "merged.txt"
    lines = [f"line {number}\n" for number in range(1, 21)]
    file_path.write_text("".join(lines), encoding="utf-8")

    result = read_file(
        tmp_path,
        str(file_path),
        ranges=ranges,
        settings=ReadFileSettings(default_max_lines=8, minimum_range_lines=1),
    )

    assert result["ok"] is True
    assert result["content"] == "".join(lines[1:9])
    assert "CODE SQUID" not in result["content"]
    assert result["returned_line_ranges"] == [expected]


def test_read_file_separates_every_disjoint_returned_range(tmp_path: Path) -> None:
    file_path = tmp_path / "three-regions.txt"
    lines = [f"line {number}\n" for number in range(1, 31)]
    file_path.write_text("".join(lines), encoding="utf-8")

    result = read_file(
        tmp_path,
        str(file_path),
        ranges=[
            {"start_line": 2, "end_line": 3},
            {"start_line": 12, "end_line": 13},
            {"start_line": 25, "end_line": 26},
        ],
        settings=ReadFileSettings(default_max_lines=10, minimum_range_lines=1),
    )

    assert result["content"].count("omitted from this read") == 2
    assert "lines 4-11 omitted" in result["content"]
    assert "lines 14-24 omitted" in result["content"]
    assert result["returned_line_ranges"] == [
        {"start_line": 2, "end_line": 3},
        {"start_line": 12, "end_line": 13},
        {"start_line": 25, "end_line": 26},
    ]


def test_read_file_multi_range_budget_allows_exact_limit_and_rejects_excess(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "budget.txt"
    lines = [f"line {number}\n" for number in range(1, 31)]
    file_path.write_text("".join(lines), encoding="utf-8")
    settings = ReadFileSettings(default_max_lines=10, minimum_range_lines=1)

    exact = read_file(
        tmp_path,
        str(file_path),
        ranges=[
            {"start_line": 1, "end_line": 5},
            {"start_line": 21, "end_line": 25},
        ],
        settings=settings,
    )
    excess = read_file(
        tmp_path,
        str(file_path),
        ranges=[
            {"start_line": 1, "end_line": 5},
            {"start_line": 20, "end_line": 25},
        ],
        settings=settings,
    )

    assert exact["ok"] is True
    assert len(exact["content"].splitlines()) == 11
    assert "lines 6-20 omitted" in exact["content"]
    assert excess == {
        "ok": False,
        "tool": "read_file",
        "path": str(file_path),
        "error": (
            "targeted read covers 11 lines after normalization; "
            "maximum total coverage is 10 lines"
        ),
    }


def test_read_file_accepts_exactly_eight_ranges_within_budget(tmp_path: Path) -> None:
    file_path = tmp_path / "eight-ranges.txt"
    lines = [f"line {number}\n" for number in range(1, 17)]
    file_path.write_text("".join(lines), encoding="utf-8")
    ranges = [
        {"start_line": line, "end_line": line}
        for line in range(1, 16, 2)
    ]

    result = read_file(
        tmp_path,
        str(file_path),
        ranges=ranges,
        settings=ReadFileSettings(default_max_lines=8, minimum_range_lines=1),
    )

    assert result["ok"] is True
    assert result["returned_line_ranges"] == ranges


def test_read_file_rejects_more_than_eight_ranges(tmp_path: Path) -> None:
    file_path = tmp_path / "nine-ranges.txt"
    file_path.write_text("one\n", encoding="utf-8")

    result = read_file(
        tmp_path,
        str(file_path),
        ranges=[
            {"start_line": line, "end_line": line}
            for line in range(1, 10)
        ],
    )

    assert result["ok"] is False
    assert result["error"] == "ranges may contain at most 8 items"


def test_read_file_expands_multi_ranges_before_merging_and_budgeting(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "expanded.txt"
    lines = [f"line {number}\n" for number in range(1, 501)]
    file_path.write_text("".join(lines), encoding="utf-8")

    result = read_file(
        tmp_path,
        str(file_path),
        ranges=[
            {"start_line": 400, "end_line": 400},
            {"start_line": 100, "end_line": 100},
        ],
    )

    assert result["returned_line_ranges"] == [
        {"start_line": 26, "end_line": 175},
        {"start_line": 326, "end_line": 475},
    ]
    assert len(result["content"].splitlines()) == 301
    assert "lines 176-325 omitted" in result["content"]


def test_read_file_uses_supplied_sizing_settings_and_keeps_truncation_metadata(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "custom-limit.txt"
    lines = [f"line {number}\n" for number in range(1, 12)]
    original_content = "".join(lines)
    file_path.write_text(original_content, encoding="utf-8")

    result = read_file(
        tmp_path,
        str(file_path),
        settings=ReadFileSettings(default_max_lines=10, minimum_range_lines=4),
    )

    assert result["content"] == "".join(
        (
            *lines[:5],
            "<<< CODE SQUID: lines 6-6 omitted from this read >>>\n",
            *lines[-5:],
        )
    )
    assert result["content_truncated"] is True
    assert result["content_original_chars"] == len(original_content)
    assert result["content_returned_chars"] == len(result["content"])
    assert result["returned_line_ranges"] == [
        {"start_line": 1, "end_line": 5},
        {"start_line": 7, "end_line": 11},
    ]


def test_run_command_keeps_shared_character_truncation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = "x" * 12001

    class Process:
        returncode = 0

        def communicate(self, timeout=None):
            return output.encode(), None

    monkeypatch.setattr(
        run_command_module.subprocess,
        "Popen",
        lambda *args, **kwargs: Process(),
    )

    result = run_command(tmp_path, "generate lots of output", verification=False)
    expected_output, expected_metadata = truncate_head_tail(output)

    assert result["output"] == expected_output
    assert result["output_truncated"] is True
    assert result["output_original_chars"] == expected_metadata["original_chars"]
    assert result["output_returned_chars"] == expected_metadata["returned_chars"]


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        (
            {"ranges": [{"start_line": 0, "end_line": 2}]},
            "ranges[0]: start_line must be >= 1",
        ),
        (
            {"ranges": [{"start_line": 3, "end_line": 2}]},
            "ranges[0]: end_line must be >= start_line",
        ),
        (
            {"ranges": [{"start_line": 1}]},
            "ranges[0] requires start_line and end_line",
        ),
        ({"ranges": []}, "ranges must be a non-empty list"),
        ({"ranges": ["1-2"]}, "ranges[0] must be an object"),
        (
            {"ranges": [{"start_line": 1, "end_line": 2, "extra": 3}]},
            "ranges[0] may contain only start_line and end_line",
        ),
        (
            {"ranges": [{"start_line": True, "end_line": 2}]},
            "ranges[0] start_line and end_line must be integers",
        ),
    ],
)
def test_read_file_rejects_invalid_multi_range_forms(
    tmp_path: Path,
    kwargs: dict,
    error: str,
) -> None:
    file_path = tmp_path / "input.txt"
    file_path.write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = read_file(tmp_path, str(file_path), **kwargs)

    assert result["ok"] is False
    assert result["error"] == error


def test_read_file_range_past_eof_is_empty_and_sets_empty_range_end_line(tmp_path: Path) -> None:
    file_path = tmp_path / "input.txt"
    file_path.write_text("one\ntwo\n", encoding="utf-8")

    result = read_file(
        tmp_path,
        str(file_path),
        ranges=[{"start_line": 5, "end_line": 8}],
    )

    assert result == {
        "ok": True,
        "tool": "read_file",
        "path": str(file_path),
        "content": "",
        "content_truncated": False,
        "content_original_chars": 0,
        "content_returned_chars": 0,
        "start_line": 5,
        "end_line": 4,
        "returned_line_ranges": [],
        "total_lines": 2,
        "ranged": True,
    }


def test_read_file_definition_exposes_targeted_read_shapes() -> None:
    function = read_file_definition()["function"]
    parameters = function["parameters"]

    assert "Omit ranges only when file-wide context is needed" in function["description"]
    assert "DOCX document" in function["description"]
    assert "deterministic DOCX text" in function["description"]
    assert "EPUB book" in function["description"]
    assert parameters["required"] == ["path"]
    assert set(parameters["properties"]) == {
        "path",
        "ranges",
        "pages",
        "sheet",
        "rows",
    }
    assert parameters["additionalProperties"] is False
    ranges = parameters["properties"]["ranges"]
    assert ranges["type"] == "array"
    assert ranges["minItems"] == 1
    assert ranges["maxItems"] == 8
    assert ranges["items"]["required"] == ["start_line", "end_line"]
    assert ranges["items"]["additionalProperties"] is False
    pages = parameters["properties"]["pages"]
    assert pages["type"] == "array"
    assert pages["minItems"] == 1
    assert pages["maxItems"] == 8
    assert pages["items"]["required"] == ["start_page", "end_page"]
    assert pages["items"]["additionalProperties"] is False
    assert parameters["properties"]["sheet"]["minLength"] == 1
    rows = parameters["properties"]["rows"]
    assert rows["required"] == ["start_row", "end_row"]
    assert rows["additionalProperties"] is False


def test_read_file_execute_no_longer_accepts_top_level_line_bounds(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "input.txt"
    file_path.write_text("one\n", encoding="utf-8")

    with pytest.raises(TypeError, match="unexpected keyword argument 'start_line'"):
        read_file(tmp_path, str(file_path), start_line=1, end_line=1)


def test_write_file_rejects_relative_path_succeeds_with_absolute_path_and_is_compact(tmp_path: Path) -> None:
    file_path = tmp_path / "output.txt"

    relative_result = write_file(tmp_path, "output.txt", "written")
    absolute_result = write_file(tmp_path, str(file_path), "written")

    assert relative_result == {
        "ok": False,
        "tool": "write_file",
        "path": "output.txt",
        "error": "write_file requires an absolute path",
    }
    assert absolute_result["ok"] is True
    assert absolute_result["tool"] == "write_file"
    assert absolute_result["path"] == str(file_path)
    assert absolute_result["summary"] == "Wrote 7 characters"
    assert absolute_result["characters_written"] == 7
    assert "content" not in absolute_result
    assert file_path.read_text(encoding="utf-8") == "written"


def test_apply_patch_replaces_one_exact_occurrence_and_is_compact(tmp_path: Path) -> None:
    file_path = tmp_path / "input.txt"
    file_path.write_text("before middle after", encoding="utf-8")

    result = apply_patch(tmp_path, str(file_path), "middle", "center")

    assert result == {
        "ok": True,
        "tool": "apply_patch",
        "path": str(file_path),
        "replacements": 1,
        "start_line": 1,
        "end_line_before": 1,
        "end_line_after": 1,
        "chars_before": 19,
        "chars_after": 19,
        "before_preview": "middle",
        "after_preview": "center",
    }
    assert file_path.read_text(encoding="utf-8") == "before center after"
    assert "content" not in result


def test_apply_patch_bounds_long_replacement_previews(tmp_path: Path) -> None:
    file_path = tmp_path / "input.txt"
    old_text = "a" * 200 + "b" * 200
    new_text = "c" * 200 + "d" * 200
    file_path.write_text(old_text, encoding="utf-8")

    result = apply_patch(tmp_path, str(file_path), old_text, new_text)

    assert len(result["before_preview"]) == 300
    assert len(result["after_preview"]) == 300
    assert " ... omitted ... " in result["before_preview"]
    assert " ... omitted ... " in result["after_preview"]
    assert result["before_preview"].startswith("a")
    assert result["before_preview"].endswith("b")
    assert result["after_preview"].startswith("c")
    assert result["after_preview"].endswith("d")


def test_apply_patch_fails_when_old_text_missing_and_does_not_modify_file(tmp_path: Path) -> None:
    file_path = tmp_path / "input.txt"
    file_path.write_text("original", encoding="utf-8")

    result = apply_patch(tmp_path, str(file_path), "missing", "replacement")

    assert result == {
        "ok": False,
        "tool": "apply_patch",
        "path": str(file_path),
        "error": "old_text_not_found",
        "occurrences": 0,
    }
    assert file_path.read_text(encoding="utf-8") == "original"


def test_apply_patch_fails_when_old_text_is_not_unique_and_does_not_modify_file(tmp_path: Path) -> None:
    file_path = tmp_path / "input.txt"
    file_path.write_text("same different same", encoding="utf-8")

    result = apply_patch(tmp_path, str(file_path), "same", "changed")

    assert result["ok"] is False
    assert result["tool"] == "apply_patch"
    assert result["path"] == str(file_path)
    assert result["error"] == "old_text_not_unique"
    assert result["occurrences"] == 2
    assert len(result["matches"]) == 2
    assert file_path.read_text(encoding="utf-8") == "same different same"


def test_apply_patch_missing_text_returns_closest_same_sized_window(tmp_path: Path) -> None:
    file_path = tmp_path / "input.txt"
    original = "header\nalpha = 1\nbeta = 20\ngamma = 3\nfooter\n"
    file_path.write_text(original, encoding="utf-8")

    result = apply_patch(
        tmp_path,
        str(file_path),
        "alpha = 1\nbeta = 2\ngamma = 3",
        "replacement",
    )

    assert result["error"] == "old_text_not_found"
    assert result["closest_match"] == {
        "start_line": 2,
        "end_line": 4,
        "similarity": pytest.approx(0.982, abs=0.001),
        "content": "alpha = 1\nbeta = 20\ngamma = 3\n",
    }
    assert file_path.read_text(encoding="utf-8") == original


def test_apply_patch_missing_text_omits_low_confidence_context(tmp_path: Path) -> None:
    file_path = tmp_path / "input.txt"
    original = "one\ntwo\nthree\n"
    file_path.write_text(original, encoding="utf-8")

    result = apply_patch(tmp_path, str(file_path), "completely\nunrelated", "replacement")

    assert result["error"] == "old_text_not_found"
    assert "closest_match" not in result
    assert file_path.read_text(encoding="utf-8") == original


def test_apply_patch_duplicate_diagnostics_have_exact_bounded_ranges(tmp_path: Path) -> None:
    file_path = tmp_path / "input.txt"
    original = "\n".join(
        [
            "line 1",
            "target",
            "value",
            "line 4",
            "line 5",
            "target",
            "value",
            "line 8",
            "target",
            "value",
            "line 11",
            "line 12",
            "target",
            "value",
        ]
    )
    file_path.write_text(original, encoding="utf-8")

    result = apply_patch(tmp_path, str(file_path), "target\nvalue", "changed")

    assert result["error"] == "old_text_not_unique"
    assert result["occurrences"] == 4
    assert len(result["matches"]) == 3
    assert [
        (match["start_line"], match["end_line"]) for match in result["matches"]
    ] == [(2, 3), (6, 7), (9, 10)]
    assert result["matches"][0] == {
        "start_line": 2,
        "end_line": 3,
        "context_start_line": 1,
        "context_end_line": 5,
        "context": "line 1\ntarget\nvalue\nline 4\nline 5\n",
    }
    assert all(
        match["context_end_line"] - match["context_start_line"] + 1 <= 6
        for match in result["matches"]
    )
    assert file_path.read_text(encoding="utf-8") == original


def test_apply_patch_success_reports_affected_multiline_ranges(tmp_path: Path) -> None:
    file_path = tmp_path / "input.txt"
    file_path.write_text("before\nold one\nold two\nafter\n", encoding="utf-8")

    result = apply_patch(
        tmp_path,
        str(file_path),
        "old one\nold two",
        "new one\nnew two\nnew three",
    )

    assert result["start_line"] == 2
    assert result["end_line_before"] == 3
    assert result["end_line_after"] == 4
    assert file_path.read_text(encoding="utf-8") == (
        "before\nnew one\nnew two\nnew three\nafter\n"
    )


def test_builtin_tool_descriptions_contain_only_mechanical_guidance() -> None:
    descriptions = [entry["definition"]()["function"]["description"] for entry in TOOLS.values()]

    assert len(descriptions) == 4
    assert all("action note" not in description.lower() for description in descriptions)
    assert all("before calling" not in description.lower() for description in descriptions)


def test_apply_patch_requires_absolute_path(tmp_path: Path) -> None:
    result = apply_patch(tmp_path, "input.txt", "old", "new")

    assert result == {
        "ok": False,
        "tool": "apply_patch",
        "path": "input.txt",
        "error": "path_not_absolute",
    }


def test_apply_patch_schema_distinguishes_normal_and_candidate_calls() -> None:
    parameters = TOOLS["apply_patch"]["definition"]()["function"]["parameters"]

    assert parameters["properties"] == {
        "path": {
            "type": "string",
            "description": "Absolute path to the file",
        },
        "old_text": {
            "type": "string",
            "description": "Exact text currently in the file",
        },
        "new_text": {
            "type": "string",
            "description": "Replacement text",
        },
        "match_id": {
            "type": "string",
            "description": "Run-local candidate ID returned by a failed patch",
        },
    }
    assert parameters["anyOf"] == [
        {"required": ["path", "old_text", "new_text"]},
        {"required": ["match_id"]},
    ]
    assert parameters["additionalProperties"] is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"path": "x", "old_text": "old"},
    ],
)
def test_apply_patch_rejects_malformed_request_forms(
    tmp_path: Path,
    kwargs: dict,
) -> None:
    result = apply_patch(tmp_path, **kwargs)

    assert result["ok"] is False
    assert result["error"] == "invalid_arguments"


def test_run_command_timeout_returns_normal_failure_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(run_command_module, "DEFAULT_TIMEOUT_SECONDS", 0.05)
    executable = f"& '{sys.executable}'" if sys.platform == "win32" else shlex.quote(sys.executable)
    command = (
        f'{executable} -c "import time; print(\'partial output\', flush=True); time.sleep(5)"'
    )
    result = run_command(
        tmp_path,
        command,
        verification=True,
    )

    assert result["ok"] is False
    assert result["timed_out"] is True
    assert result["tool"] == "run_command"
    assert result["command"] == command
    assert result["verification"] is True
    assert result["failure_class"] == "timeout"


def test_run_command_cancellation_stops_promptly(tmp_path: Path) -> None:
    executable = f"& '{sys.executable}'" if sys.platform == "win32" else shlex.quote(sys.executable)
    started = run_command_module.time.monotonic()
    result = run_command(
        tmp_path,
        f'{executable} -c "import time; time.sleep(5)"',
        verification=False,
        should_cancel=lambda: run_command_module.time.monotonic() - started > 0.1,
    )

    assert result["ok"] is False
    assert result["cancelled"] is True
    assert result["timed_out"] is False
    assert result["failure_class"] == "cancelled"
    assert run_command_module.time.monotonic() - started < 2


def test_run_command_keyboard_interrupt_cleans_up_owned_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from squid_core.platform_context import PlatformContext

    cleanups = []

    class InterruptingProcess:
        returncode = None

        def communicate(self, timeout=None):
            raise KeyboardInterrupt

    process = InterruptingProcess()
    monkeypatch.setattr(
        run_command_module.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(
        run_command_module,
        "_cleanup_owned_process",
        lambda owned, is_windows, windows_job=None: cleanups.append(
            (owned, is_windows, windows_job)
        ) or b"",
    )

    with pytest.raises(KeyboardInterrupt):
        run_command(
            tmp_path,
            "sleep forever",
            verification=False,
            platform_context=PlatformContext("Linux", "sh", "/bin/sh", "/", False),
        )

    assert cleanups == [(process, False, None)]


def test_run_command_cleanup_closes_retained_pipe_without_unbounded_wait(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from squid_core.platform_context import PlatformContext

    class Pipe:
        closed = False

        def close(self):
            self.closed = True

    class RetainedPipeProcess:
        pid = 1234
        returncode = None

        def __init__(self) -> None:
            self.stdout = Pipe()
            self.stderr = None
            self.stdin = None
            self.killed = False
            self.wait_timeouts = []

        def communicate(self, timeout=None):
            raise run_command_module.subprocess.TimeoutExpired(
                "retained-pipe",
                timeout,
                output=b"partial output\n",
            )

        def poll(self):
            return None

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            raise run_command_module.subprocess.TimeoutExpired("retained-pipe", timeout)

    process = RetainedPipeProcess()
    monkeypatch.setattr(
        run_command_module.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(run_command_module, "_terminate_process_tree", lambda *args: None)
    monkeypatch.setattr(run_command_module, "PROCESS_DRAIN_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(run_command_module, "PROCESS_WAIT_TIMEOUT_SECONDS", 0.01)

    started = run_command_module.time.monotonic()
    result = run_command(
        tmp_path,
        "retains pipe",
        verification=False,
        should_cancel=lambda: True,
        platform_context=PlatformContext("Linux", "sh", "/bin/sh", "/", False),
    )

    assert run_command_module.time.monotonic() - started < 0.5
    assert result["cancelled"] is True
    assert result["output"] == "partial output\n"
    assert process.stdout.closed is True
    assert process.killed is True
    assert process.wait_timeouts == [0.01]


def test_run_command_timeout_uses_bounded_cleanup_and_keeps_classification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from squid_core.platform_context import PlatformContext

    class Process:
        returncode = None

    process = Process()
    cleanups = []
    monkeypatch.setattr(
        run_command_module.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(run_command_module, "DEFAULT_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(
        run_command_module,
        "_cleanup_owned_process",
        lambda owned, is_windows, windows_job=None: cleanups.append(owned) or b"",
    )

    result = run_command(
        tmp_path,
        "times out",
        verification=True,
        platform_context=PlatformContext("Linux", "sh", "/bin/sh", "/", False),
    )

    assert cleanups == [process]
    assert result["cancelled"] is False
    assert result["timed_out"] is True
    assert result["failure_class"] == "timeout"


def test_taskkill_fallback_has_bounded_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    class HangingTaskkill:
        def __init__(self) -> None:
            self.killed = False
            self.waits = []

        def wait(self, timeout=None):
            self.waits.append(timeout)
            raise run_command_module.subprocess.TimeoutExpired("taskkill", timeout)

        def kill(self):
            self.killed = True

    taskkill = HangingTaskkill()
    monkeypatch.setattr(
        run_command_module.subprocess,
        "Popen",
        lambda *args, **kwargs: taskkill,
    )
    monkeypatch.setattr(run_command_module, "TASKKILL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(run_command_module, "PROCESS_WAIT_TIMEOUT_SECONDS", 0.01)

    started = run_command_module.time.monotonic()
    run_command_module._run_taskkill_bounded(1234)

    assert run_command_module.time.monotonic() - started < 0.5
    assert taskkill.killed is True
    assert taskkill.waits == [0.01, 0.01]


def test_taskkill_launch_failure_is_non_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_launch(*args, **kwargs):
        raise OSError("taskkill unavailable")

    monkeypatch.setattr(run_command_module.subprocess, "Popen", fail_launch)

    run_command_module._run_taskkill_bounded(1234)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group regression")
def test_posix_cleanup_kills_surviving_group_after_leader_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals = []

    class ExitedLeader:
        pid = 4321

        def poll(self):
            return 0

    monkeypatch.setattr(run_command_module, "PROCESS_TREE_GRACE_SECONDS", 0)
    monkeypatch.setattr(run_command_module, "_process_group_exists", lambda _pid: True)
    monkeypatch.setattr(
        run_command_module.os,
        "killpg",
        lambda pid, sent_signal: signals.append((pid, sent_signal)),
    )

    run_command_module._terminate_process_tree(ExitedLeader(), is_windows=False)

    assert signals == [
        (4321, run_command_module.signal.SIGTERM),
        (4321, run_command_module.signal.SIGKILL),
    ]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process-tree regression")
def test_run_command_timeout_terminates_descendant_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = tmp_path / "descendant-finished.txt"
    child = tmp_path / "child.py"
    child.write_text(
        "import pathlib, time\ntime.sleep(1)\npathlib.Path(r'"
        + str(marker)
        + "').write_text('late', encoding='utf-8')\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, r'{child}'])\n"
        "time.sleep(5)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(run_command_module, "DEFAULT_TIMEOUT_SECONDS", 0.2)

    result = run_command(
        tmp_path,
        _python_script_command(parent),
        verification=False,
    )
    run_command_module.time.sleep(1.2)

    assert result["timed_out"] is True
    assert marker.exists() is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process-tree regression")
def test_run_command_repeated_cancellation_terminates_descendants(
    tmp_path: Path,
) -> None:
    markers = []
    for attempt in range(5):
        marker = tmp_path / f"descendant-finished-{attempt}.txt"
        markers.append(marker)
        child = tmp_path / f"child-{attempt}.py"
        child.write_text(
            "import pathlib, time\ntime.sleep(1)\npathlib.Path(r'"
            + str(marker)
            + "').write_text('late', encoding='utf-8')\n",
            encoding="utf-8",
        )
        parent = tmp_path / f"parent-{attempt}.py"
        parent.write_text(
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, r'{child}'])\n"
            "time.sleep(5)\n",
            encoding="utf-8",
        )
        started = run_command_module.time.monotonic()

        result = run_command(
            tmp_path,
            _python_script_command(parent),
            verification=False,
            should_cancel=lambda: run_command_module.time.monotonic() - started > 0.2,
        )

        assert result["cancelled"] is True
        assert run_command_module.time.monotonic() - started < 2

    # Each child would create its marker one second after launch if any tree
    # member survived cancellation.
    run_command_module.time.sleep(1.2)
    assert not any(marker.exists() for marker in markers)


def test_run_command_classifies_powershell_invalid_separator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from squid_core.platform_context import PlatformContext

    output = (
        "At line:1 char:10\n"
        "+ Get-Date && Get-Date\n"
        "+          ~~\n"
        "The token '&&' is not a valid statement separator in this version.\n"
    )

    context = PlatformContext("Windows", "PowerShell", "powershell", "\\", True)
    assert run_command_module._classify_failure(context, 1, output) == "shell_syntax_error"


def test_run_command_classifies_recognizable_missing_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from squid_core.platform_context import PlatformContext

    output = (
        "missing-tool : The term 'missing-tool' is not recognized as the name "
        "of a cmdlet, function, script file, or operable program.\n"
        "    + CategoryInfo : ObjectNotFound: (missing-tool:String) [], "
        "CommandNotFoundException\n"
    )

    context = PlatformContext("Windows", "PowerShell", "powershell", "\\", True)
    assert run_command_module._classify_failure(context, 1, output) == "command_not_found"


def test_run_command_keeps_ordinary_project_failure_generic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from squid_core.platform_context import PlatformContext

    output = "FAILED tests/test_widget.py - AssertionError: command not found in message\n"

    context = PlatformContext("Linux", "bash", "/bin/bash", "/", False)
    assert run_command_module._classify_failure(context, 1, output) == "process_failed"


def test_run_command_uses_selected_powershell_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from squid_core.platform_context import PlatformContext

    calls = []

    class Process:
        returncode = 0

        def communicate(self, timeout=None):
            return b"ok\n", None

    monkeypatch.setattr(
        run_command_module,
        "detect_platform_context",
        lambda: PlatformContext("Windows", "PowerShell", r"C:\Program Files\PowerShell\pwsh.exe", "\\", True),
    )
    monkeypatch.setattr(
        run_command_module.subprocess,
        "Popen",
        lambda *args, **kwargs: calls.append((args, kwargs)) or Process(),
    )

    result = run_command(tmp_path, "Get-ChildItem", verification=False)

    args, kwargs = calls[0]
    assert args[0] == [
        r"C:\Program Files\PowerShell\pwsh.exe",
        "-NoProfile",
        "-Command",
        "Get-ChildItem",
    ]
    assert kwargs["shell"] is False
    assert kwargs["cwd"] == tmp_path
    assert kwargs["creationflags"] == getattr(run_command_module.subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    assert result["ok"] is True


def test_run_command_uses_supplied_context_without_redetecting(monkeypatch, tmp_path: Path) -> None:
    from squid_core.platform_context import PlatformContext

    calls = []

    class Process:
        returncode = 0

        def communicate(self, timeout=None):
            return b"ok\n", None

    def fail_detection():
        raise AssertionError("platform should not be redetected")

    context = PlatformContext("Linux", "bash", "/bin/bash", "/", False)
    monkeypatch.setattr(run_command_module, "detect_platform_context", fail_detection)
    monkeypatch.setattr(
        run_command_module.subprocess,
        "Popen",
        lambda *args, **kwargs: calls.append((args, kwargs)) or Process(),
    )

    result = run_command(
        tmp_path,
        "printf ok",
        verification=False,
        platform_context=context,
    )

    assert calls[0][0][0] == "printf ok"
    assert calls[0][1]["shell"] is True
    assert calls[0][1]["start_new_session"] is True
    assert result["ok"] is True
