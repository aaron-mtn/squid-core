"""Focused coverage for the repeated-read runtime nudge."""

from dataclasses import dataclass
from pathlib import Path

from squid_core.hooks import (
    CompactionAppliedContext,
    HookResponse,
    HookRunner,
    ReadResultTrimmedContext,
    RepeatedReadNudge,
    ToolResultContext,
)
from squid_core.hooks.redundant_read import _partially_redundant_result
from squid_core.tools.builtins.apply_patch import execute as apply_patch
from squid_core.tools.builtins.read_file import execute as read_file


GUIDANCE = (
    "You already read this unchanged source and it is still available in context. "
    "The already read content is being omitted rather than returned again. "
    "Check the info you already have in your history. If the next edit is already "
    "clear, make it now. "
)
ESCALATED_ALREADY_COVERED_MESSAGE = (
    "HIGH PRIORITY: Stop rereading this source. The requested unchanged content "
    "is already available in context and will not be returned again. "
    "Use the existing context and take the next non-read action now."
)


def test_partial_redundant_shaping_preserves_disjoint_read_markers() -> None:
    result = {
        "content": (
            "line 1\n"
            "line 2\n"
            "<<< CODE SQUID: lines 3-8 omitted from this read >>>\n"
            "line 9\n"
            "line 10\n"
        ),
        "returned_line_ranges": [
            {"start_line": 1, "end_line": 2},
            {"start_line": 9, "end_line": 10},
        ],
    }

    shaped = _partially_redundant_result(
        result,
        returned_ranges=[(1, 2), (9, 10)],
        covered_ranges=[(2, 2), (9, 9)],
        novel_ranges=[(1, 1), (10, 10)],
    )

    assert shaped is not None
    assert shaped["content"] == (
        "line 1\n"
        "<<< CODE SQUID: lines 2-9 omitted from this read >>>\n"
        "line 10\n"
    )
    assert shaped["returned_line_ranges"] == [
        {"start_line": 1, "end_line": 1},
        {"start_line": 10, "end_line": 10},
    ]


def _source(start: int, end: int) -> str:
    """Build predictable one-line source records for slicing assertions."""
    return "".join(f"line {line}\n" for line in range(start, end + 1))


@dataclass(frozen=True)
class _Context:
    action: dict
    result: dict


def _read(
    hook: RepeatedReadNudge,
    path: Path,
    *,
    returned_start: int = 1,
    returned_end: int = 100,
    requested_start: int | None = None,
    requested_end: int | None = None,
    returned_ranges: list[dict] | None = None,
    ok: bool = True,
    content: str = "FILE CONTENTS",
) -> HookResponse | None:
    args = {"path": str(path)}
    if requested_start is not None or requested_end is not None:
        assert requested_start is not None and requested_end is not None
        args["ranges"] = [
            {"start_line": requested_start, "end_line": requested_end}
        ]

    result = {"ok": ok, "tool": "read_file", "path": str(path)}
    if ok:
        result.update(
            {
                "content": content,
                "start_line": returned_start,
                "end_line": returned_end,
            }
        )
    if returned_ranges is not None:
        result["returned_line_ranges"] = returned_ranges
    return hook.tool_result(
        _Context(action={"tool": "read_file", "args": args}, result=result)
    )


def _mutate(
    hook: RepeatedReadNudge,
    tool: str,
    path: Path,
    *,
    ok: bool = True,
    start_line: int | None = None,
    end_line_before: int | None = None,
    end_line_after: int | None = None,
) -> HookResponse | None:
    result = {"ok": ok, "tool": tool, "path": str(path)}
    if start_line is not None:
        result["start_line"] = start_line
    if end_line_before is not None:
        result["end_line_before"] = end_line_before
    if end_line_after is not None:
        result["end_line_after"] = end_line_after
    return hook.tool_result(
        _Context(
            action={"tool": tool, "args": {"path": str(path)}},
            result=result,
        )
    )


def _assert_first_reread_is_masked(
    response: HookResponse | None,
) -> dict:
    assert response is not None
    assert response.additional_context == GUIDANCE
    assert response.updated_tool_output is not None
    assert response.updated_tool_output["already_covered"] is True
    assert response.updated_tool_output["message"].startswith(
        "Already read unchanged content:"
    )
    assert response.updated_tool_output["message"].endswith(
        "The previous result is still current."
    )
    assert "content" not in response.updated_tool_output
    return response.updated_tool_output


def test_exact_repeated_returned_range_is_masked_on_first_reread(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "a.py"

    assert _read(hook, path) is None
    output = _assert_first_reread_is_masked(_read(hook, path))
    assert output["message"] == (
        f"Already read unchanged content: {path} lines 1-100. "
        "The previous result is still current."
    )


def test_external_file_change_invalidates_coverage_until_fresh_reread(
    tmp_path: Path,
) -> None:
    """A mutation without a structured Tool event must make old coverage unusable."""
    runner = HookRunner([RepeatedReadNudge()])
    path = tmp_path / "external.py"
    path.write_text("old first\nold second\n", encoding="utf-8")
    args = {
        "path": str(path),
        "ranges": [{"start_line": 1, "end_line": 2}],
    }

    def observe(result: dict) -> HookResponse:
        return runner.tool_result(
            ToolResultContext(
                session_id=None,
                repo_root=str(tmp_path),
                action={"tool": "read_file", "args": args},
                result=result,
            )
        )

    first_result = read_file(tmp_path, **args)
    assert observe(first_result).updated_tool_output == first_result
    assert observe(read_file(tmp_path, **args)).updated_tool_output[
        "already_covered"
    ] is True

    # This stands in for run_command, a user edit, or any other mutation path
    # that does not notify the structured mutation Hook seam.
    path.write_text("new first\nnew second with a size change\n", encoding="utf-8")
    changed_result = read_file(tmp_path, **args)
    changed = observe(changed_result)
    assert changed.updated_tool_output == changed_result
    assert "new second with a size change" in changed.updated_tool_output["content"]

    # The successful reread establishes coverage against the new fingerprint.
    repeated = observe(read_file(tmp_path, **args))
    assert repeated.updated_tool_output["already_covered"] is True
    assert "content" not in repeated.updated_tool_output


def test_first_redundant_read_replaces_contents_at_runner_seam(
    tmp_path: Path,
) -> None:
    runner = HookRunner([RepeatedReadNudge()])
    path = tmp_path / "a.py"
    action = {"tool": "read_file", "args": {"path": str(path)}}
    result = {
        "ok": True,
        "tool": "read_file",
        "path": str(path),
        "content": "normal file contents",
        "start_line": 1,
        "end_line": 10,
    }

    context = ToolResultContext(
        session_id=None,
        repo_root=str(tmp_path),
        action=action,
        result=result,
    )
    first = runner.tool_result(context)
    assert first.updated_tool_output == result
    assert first.additional_context == ""

    redundant = runner.tool_result(context)
    assert redundant.updated_tool_output == {
        "ok": True,
        "tool": "read_file",
        "path": str(path),
        "already_covered": True,
        "message": (
            f"Already read unchanged content: {path} lines 1-10. "
            "The previous result is still current."
        ),
        "start_line": 1,
        "end_line": 10,
    }
    assert redundant.additional_context == GUIDANCE
    assert "content" not in redundant.updated_tool_output


def test_repeated_read_nudge_has_no_verification_dependency(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge()
    runner = HookRunner([hook])
    path = tmp_path / "a.py"
    action = {"tool": "read_file", "args": {"path": str(path)}}
    result = {
        "ok": True,
        "tool": "read_file",
        "path": str(path),
        "content": "source",
        "start_line": 1,
        "end_line": 10,
    }
    context = ToolResultContext(None, str(tmp_path), action, result)

    runner.tool_result(context)
    redundant = runner.tool_result(context)

    assert redundant.additional_context == GUIDANCE


def test_redundant_read_uses_normal_guidance_after_verification_command(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge()
    runner = HookRunner([hook])
    path = tmp_path / "a.py"
    read_context = ToolResultContext(
        None,
        str(tmp_path),
        {"tool": "read_file", "args": {"path": str(path)}},
        {
            "ok": True,
            "tool": "read_file",
            "path": str(path),
            "content": "source",
            "start_line": 1,
            "end_line": 10,
        },
    )
    verification_context = ToolResultContext(
        None,
        str(tmp_path),
        {
            "tool": "run_command",
            "args": {"command": "check", "verification": True},
        },
        {"ok": True, "tool": "run_command", "verification": True},
    )

    runner.tool_result(read_context)
    runner.tool_result(verification_context)
    redundant = runner.tool_result(read_context)

    assert redundant.additional_context == GUIDANCE


def test_redundant_read_after_later_mutation_returns_to_normal_guidance(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge()
    runner = HookRunner([hook])
    path = tmp_path / "a.py"

    def context(tool: str, result: dict) -> ToolResultContext:
        return ToolResultContext(
            None,
            str(tmp_path),
            {"tool": tool, "args": {"path": str(path)}},
            result,
        )

    runner.tool_result(
        context("run_command", {"ok": True, "verification": True})
    )
    runner.tool_result(
        context(
            "write_file",
            {"ok": True, "tool": "write_file", "path": str(path)},
        )
    )
    read = context(
        "read_file",
        {
            "ok": True,
            "tool": "read_file",
            "path": str(path),
            "content": "changed source",
            "start_line": 1,
            "end_line": 10,
        },
    )

    runner.tool_result(read)
    redundant = runner.tool_result(read)

    assert redundant.additional_context == GUIDANCE


def test_non_overlapping_range_keeps_normal_contents_at_runner_seam(
    tmp_path: Path,
) -> None:
    runner = HookRunner([RepeatedReadNudge()])
    path = tmp_path / "a.py"

    def context(start: int, end: int, content: str) -> ToolResultContext:
        action = {
            "tool": "read_file",
            "args": {
                "path": str(path),
                "ranges": [{"start_line": start, "end_line": end}],
            },
        }
        return ToolResultContext(
            session_id=None,
            repo_root=str(tmp_path),
            action=action,
            result={
                "ok": True,
                "tool": "read_file",
                "path": str(path),
                "content": content,
                "start_line": start,
                "end_line": end,
            },
        )

    runner.tool_result(context(1, 100, "first range"))
    new_range = runner.tool_result(context(101, 200, "new range"))

    assert new_range.updated_tool_output["content"] == "new range"
    assert new_range.additional_context == ""


def test_second_redundant_read_masks_file_content(tmp_path: Path) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "a.py"

    assert _read(hook, path) is None
    _assert_first_reread_is_masked(_read(hook, path))

    response = _read(
        hook,
        path,
        requested_start=10,
        requested_end=20,
        returned_start=1,
        returned_end=100,
        content="DUPLICATE SOURCE MUST NOT BE EXPOSED",
    )
    assert response is not None
    assert response.additional_context == ""
    assert response.updated_tool_output == {
        "ok": True,
        "tool": "read_file",
        "path": str(path),
        "already_covered": True,
        "message": ESCALATED_ALREADY_COVERED_MESSAGE,
        "requested_line_ranges": [{"start_line": 10, "end_line": 20}],
        "start_line": 1,
        "end_line": 100,
    }
    assert "content" not in response.updated_tool_output


def test_threshold_one_is_explicitly_supported(tmp_path: Path) -> None:
    hook = RepeatedReadNudge(threshold=1)
    path = tmp_path / "a.py"

    assert _read(hook, path) is None
    assert _read(hook, path) is not None


def test_nudge_is_emitted_only_at_configured_threshold(tmp_path: Path) -> None:
    hook = RepeatedReadNudge(threshold=3)
    path = tmp_path / "a.py"
    _read(hook, path)

    assert _read(hook, path) is None
    assert _read(hook, path) is None
    _assert_first_reread_is_masked(_read(hook, path))
    assert _read(hook, path).updated_tool_output["already_covered"] is True


def test_partially_overlapping_result_is_not_fully_masked_when_mapping_is_unsafe(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "a.py"
    _read(hook, path, returned_start=1, returned_end=100)

    response = _read(hook, path, returned_start=31, returned_end=130)

    assert response is not None
    assert response.updated_tool_output["content"] == "FILE CONTENTS"
    assert "already_covered" not in response.updated_tool_output
    assert hook._redundant_counts


def test_any_partial_overlap_counts_as_redundant(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "a.py"
    _read(hook, path, returned_start=1, returned_end=70)

    assert _read(hook, path, returned_start=1, returned_end=100) is not None


def test_low_overlap_returned_range_counts_as_partial_redundancy(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "a.py"
    _read(hook, path, returned_start=1, returned_end=100)

    assert _read(hook, path, returned_start=100, returned_end=199) is not None
    assert hook._redundant_counts


def test_partial_overlap_at_start_returns_only_trailing_novel_lines(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "start.py"
    _read(hook, path, returned_end=3, content=_source(1, 3))

    response = _read(hook, path, returned_end=6, content=_source(1, 6))

    output = response.updated_tool_output
    assert output["content"] == _source(4, 6)
    assert (output["start_line"], output["end_line"]) == (4, 6)
    assert output["returned_line_ranges"] == [
        {"start_line": 4, "end_line": 6}
    ]
    assert output["omitted_line_ranges"] == [
        {"start_line": 1, "end_line": 3}
    ]
    assert output["content_returned_chars"] == len(_source(4, 6))
    assert "Lines 1-3 were already available unchanged" in output["message"]


def test_partial_overlap_at_end_returns_only_leading_novel_lines(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "end.py"
    _read(hook, path, returned_start=4, returned_end=6, content=_source(4, 6))

    output = _read(
        hook, path, returned_end=6, content=_source(1, 6)
    ).updated_tool_output

    assert output["content"] == _source(1, 3)
    assert output["returned_line_ranges"] == [
        {"start_line": 1, "end_line": 3}
    ]


def test_middle_and_multiple_covered_ranges_produce_novel_fragments(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "fragments.py"
    _read(hook, path, returned_start=2, returned_end=3, content=_source(2, 3))
    _read(hook, path, returned_start=5, returned_end=5, content=_source(5, 5))

    output = _read(
        hook, path, returned_end=6, content=_source(1, 6)
    ).updated_tool_output

    assert output["content"] == (
        _source(1, 1)
        + "<<< CODE SQUID: lines 2-3 omitted from this read >>>\n"
        + _source(4, 4)
        + "<<< CODE SQUID: lines 5-5 omitted from this read >>>\n"
        + _source(6, 6)
    )
    assert output["returned_line_ranges"] == [
        {"start_line": 1, "end_line": 1},
        {"start_line": 4, "end_line": 4},
        {"start_line": 6, "end_line": 6},
    ]
    assert output["omitted_line_ranges"] == [
        {"start_line": 2, "end_line": 3},
        {"start_line": 5, "end_line": 5},
    ]


def test_truncated_multi_range_result_filters_each_segment_independently(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "truncated.py"
    _read(
        hook,
        path,
        returned_end=10,
        returned_ranges=[
            {"start_line": 1, "end_line": 2},
            {"start_line": 9, "end_line": 10},
        ],
        content=_source(1, 2) + _source(9, 10),
    )
    result_content = _source(1, 4) + _source(7, 10)

    output = _read(
        hook,
        path,
        returned_end=10,
        returned_ranges=[
            {"start_line": 1, "end_line": 4},
            {"start_line": 7, "end_line": 10},
        ],
        content=result_content,
    ).updated_tool_output

    assert output["content"] == (
        _source(3, 4)
        + "<<< CODE SQUID: lines 5-6 omitted from this read >>>\n"
        + _source(7, 8)
    )
    assert output["returned_line_ranges"] == [
        {"start_line": 3, "end_line": 4},
        {"start_line": 7, "end_line": 8},
    ]
    assert (output["start_line"], output["end_line"]) == (3, 8)


def test_partial_read_expands_coverage_and_preserves_its_strike(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "expanded.py"
    _read(hook, path, returned_end=3, content=_source(1, 3))

    partial = _read(hook, path, returned_end=6, content=_source(1, 6))
    assert partial.updated_tool_output["content"] == _source(4, 6)
    assert hook._redundant_counts

    # A fully useful read adds coverage without forgiving the earlier partial.
    assert _read(
        hook,
        path,
        returned_start=7,
        returned_end=8,
        content=_source(7, 8),
    ) is None
    assert hook._redundant_counts
    repeated = _read(hook, path, returned_end=8, content=_source(1, 8))
    assert repeated.updated_tool_output["already_covered"] is True
    assert repeated.updated_tool_output["message"] == (
        ESCALATED_ALREADY_COVERED_MESSAGE
    )


def test_trimmed_partial_result_retracts_only_its_remaining_visible_source(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "trimmed.py"
    _read(hook, path, returned_end=3, content=_source(1, 3))
    action = {
        "tool": "read_file",
        "tool_call_id": "partial-read",
        "args": {"path": str(path)},
    }
    result = {
        "ok": True,
        "tool": "read_file",
        "path": str(path),
        "content": _source(1, 6),
        "start_line": 1,
        "end_line": 6,
    }
    response = hook.tool_result(_Context(action=action, result=result))
    assert response.updated_tool_output["returned_line_ranges"] == [
        {"start_line": 4, "end_line": 6}
    ]

    hook.read_result_trimmed(
        ReadResultTrimmedContext(
            tool_call_id="partial-read",
            path=str(path),
            retained_line_ranges=((5, 5),),
        )
    )
    reread = _read(
        hook,
        path,
        returned_start=4,
        returned_end=6,
        content=_source(4, 6),
    )
    assert reread.updated_tool_output["content"] == (
        _source(4, 4)
        + "<<< CODE SQUID: lines 5-5 omitted from this read >>>\n"
        + _source(6, 6)
    )
    assert reread.updated_tool_output["returned_line_ranges"] == [
        {"start_line": 4, "end_line": 4},
        {"start_line": 6, "end_line": 6},
    ]


def test_useful_read_preserves_pending_escalation(tmp_path: Path) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "a.py"
    _read(hook, path, returned_start=1, returned_end=100)
    _assert_first_reread_is_masked(
        _read(hook, path, returned_start=1, returned_end=100)
    )

    assert _read(hook, path, returned_start=101, returned_end=200) is None
    response = _read(hook, path, returned_start=101, returned_end=200)

    assert response is not None
    assert response.updated_tool_output["message"] == (
        ESCALATED_ALREADY_COVERED_MESSAGE
    )
    assert hook._redundant_counts


def test_overlap_uses_actual_result_range_not_requested_range(tmp_path: Path) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "a.py"
    _read(hook, path, returned_start=1, returned_end=100)

    assert _read(
        hook,
        path,
        requested_start=500,
        requested_end=600,
        returned_start=1,
        returned_end=100,
    ) is not None


def test_expanded_read_metadata_drives_repeated_read_coverage(tmp_path: Path) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "expanded.py"
    path.write_text("".join(f"line {line}\n" for line in range(1, 301)), encoding="utf-8")
    action = {
        "tool": "read_file",
        "args": {
            "path": str(path),
            "ranges": [{"start_line": 100, "end_line": 109}],
        },
    }

    first_result = read_file(tmp_path, **action["args"])
    assert (first_result["start_line"], first_result["end_line"]) == (30, 179)
    assert hook.tool_result(_Context(action=action, result=first_result)) is None

    second_result = read_file(
        tmp_path,
        str(path),
        ranges=[{"start_line": 40, "end_line": 160}],
    )
    response = hook.tool_result(
        _Context(
            action={
                "tool": "read_file",
                "args": {
                    "path": str(path),
                    "ranges": [{"start_line": 40, "end_line": 160}],
                },
            },
            result=second_result,
        )
    )
    assert response is not None
    output = response.updated_tool_output
    assert output["content"] == "".join(
        f"line {line}\n" for line in range(26, 30)
    )
    assert output["returned_line_ranges"] == [
        {"start_line": 26, "end_line": 29}
    ]


def test_truncated_full_file_tracks_each_returned_range_separately(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "large.py"
    path.write_text(_source(1, 2000), encoding="utf-8")
    returned_ranges = [
        {"start_line": 1, "end_line": 500},
        {"start_line": 1501, "end_line": 2000},
    ]
    _read(hook, path, returned_end=2000, returned_ranges=returned_ranges)

    # The omitted middle must not become remembered coverage merely because
    # The result's broad summary span must not replace its exact returned ranges.
    assert _read(hook, path, returned_start=700, returned_end=800) is None
    assert _read(
        hook, path, returned_end=2000, returned_ranges=returned_ranges
    ) is not None


def test_successful_apply_patch_preserves_coverage_before_edit(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "a.py"
    _read(hook, path, returned_end=500)

    _mutate(
        hook,
        "apply_patch",
        path,
        start_line=57,
        end_line_before=62,
        end_line_after=63,
    )

    assert _read(hook, path, returned_start=1, returned_end=56) is not None


def test_successful_apply_patch_preserves_and_shifts_coverage_after_addition(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "a.py"
    _read(hook, path, returned_start=100, returned_end=200)

    _mutate(
        hook,
        "apply_patch",
        path,
        start_line=57,
        end_line_before=62,
        end_line_after=63,
    )

    assert _read(hook, path, returned_start=101, returned_end=201) is not None


def test_successful_apply_patch_shifts_coverage_backward_after_removal(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "a.py"
    _read(hook, path, returned_start=100, returned_end=200)

    _mutate(
        hook,
        "apply_patch",
        path,
        start_line=57,
        end_line_before=63,
        end_line_after=62,
    )

    assert _read(hook, path, returned_start=99, returned_end=199) is not None


def test_coverage_crossing_patch_is_split_and_shifted(tmp_path: Path) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "a.py"
    _read(hook, path, returned_end=500)

    _mutate(
        hook,
        "apply_patch",
        path,
        start_line=57,
        end_line_before=62,
        end_line_after=63,
    )

    assert list(hook._coverage.values()) == [[(1, 56), (64, 501)]]


def test_changed_patch_lines_are_not_known_coverage(tmp_path: Path) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "a.py"
    _read(hook, path, returned_end=100)

    _mutate(
        hook,
        "apply_patch",
        path,
        start_line=40,
        end_line_before=50,
        end_line_after=52,
    )

    assert _read(hook, path, returned_start=40, returned_end=52) is None


def test_patch_verification_read_is_returned_once_then_masked(
    tmp_path: Path,
) -> None:
    """A narrow edit must defeat overlap masking for its first verification."""
    runner = HookRunner([RepeatedReadNudge()])
    path = tmp_path / "script.js"
    lines = [f"const line{i} = {i};" for i in range(1, 501)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def context(tool: str, args: dict, result: dict) -> ToolResultContext:
        return ToolResultContext(
            session_id=None,
            repo_root=str(tmp_path),
            action={"tool": tool, "args": args},
            result=result,
        )

    initial_args = {
        "path": str(path),
        "ranges": [{"start_line": 1, "end_line": 500}],
    }
    initial_result = read_file(tmp_path, **initial_args)
    assert runner.tool_result(
        context("read_file", initial_args, initial_result)
    ).updated_tool_output["content"] == initial_result["content"]

    patch_args = {
        "path": str(path),
        "old_text": lines[164],
        "new_text": "const line165 = 'changed';",
    }
    patch_result = apply_patch(tmp_path, **patch_args)
    assert patch_result["ok"] is True
    runner.tool_result(context("apply_patch", patch_args, patch_result))

    verify_args = {
        "path": str(path),
        "ranges": [{"start_line": 158, "end_line": 175}],
    }
    verify_result = read_file(tmp_path, **verify_args)
    first_verification = runner.tool_result(
        context("read_file", verify_args, verify_result)
    )
    assert "const line165 = 'changed';" in first_verification.updated_tool_output[
        "content"
    ]
    assert first_verification.additional_context == GUIDANCE
    assert first_verification.updated_tool_output["returned_line_ranges"] == [
        {"start_line": 165, "end_line": 165}
    ]

    repeated_verification = runner.tool_result(
        context("read_file", verify_args, verify_result)
    )
    assert repeated_verification.updated_tool_output["already_covered"] is True
    assert "content" not in repeated_verification.updated_tool_output


def test_partial_patch_preserves_both_sides_but_stale_overlap_is_readable(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "a.py"
    _read(hook, path, returned_start=100, returned_end=250)

    _mutate(
        hook,
        "apply_patch",
        path,
        start_line=160,
        end_line_before=170,
        end_line_after=170,
    )

    assert list(hook._coverage.values()) == [[(100, 159), (171, 250)]]
    assert list(hook._stale_ranges.values()) == [[(160, 170)]]
    # Most of this range remains covered, but it includes changed source.
    verification = _read(hook, path, returned_start=150, returned_end=180)
    assert verification is not None
    assert "already_covered" not in verification.updated_tool_output
    assert hook._stale_ranges == {}
    repeated = _read(hook, path, returned_start=150, returned_end=180)
    assert repeated.updated_tool_output["already_covered"] is True
    assert repeated.updated_tool_output["message"] == (
        ESCALATED_ALREADY_COVERED_MESSAGE
    )


def test_line_count_patch_shifts_coverage_and_uncovers_replacement(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "a.py"
    _read(hook, path, returned_start=100, returned_end=250)

    _mutate(
        hook,
        "apply_patch",
        path,
        start_line=160,
        end_line_before=170,
        end_line_after=173,
    )

    assert list(hook._coverage.values()) == [[(100, 159), (174, 253)]]
    assert list(hook._stale_ranges.values()) == [[(160, 173)]]
    verification = _read(hook, path, returned_start=150, returned_end=180)
    assert verification is not None
    assert "already_covered" not in verification.updated_tool_output
    repeated = _read(hook, path, returned_start=174, returned_end=253)
    assert repeated.updated_tool_output["already_covered"] is True
    assert repeated.updated_tool_output["message"] == (
        ESCALATED_ALREADY_COVERED_MESSAGE
    )


def test_successful_apply_patch_resets_pending_escalation(tmp_path: Path) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "a.py"
    _read(hook, path)
    assert _read(hook, path) is not None
    assert hook._redundant_counts

    _mutate(
        hook,
        "apply_patch",
        path,
        start_line=40,
        end_line_before=50,
        end_line_after=50,
    )

    assert hook._redundant_counts == {}
    assert _read(hook, path, returned_start=40, returned_end=50) is None


def test_successful_write_file_invalidates_prior_reads(tmp_path: Path) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "a.py"
    _read(hook, path)
    _mutate(
        hook,
        "apply_patch",
        path,
        start_line=40,
        end_line_before=45,
        end_line_after=45,
    )
    assert hook._stale_ranges

    _mutate(hook, "write_file", path)

    assert hook._coverage == {}
    assert hook._stale_ranges == {}
    assert _read(hook, path) is None


def test_mutation_of_one_file_preserves_other_file_coverage(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge()
    changed_path = tmp_path / "changed.py"
    other_path = tmp_path / "other.py"
    _read(hook, changed_path)
    _read(hook, other_path)

    _mutate(
        hook,
        "apply_patch",
        changed_path,
        start_line=40,
        end_line_before=45,
        end_line_after=45,
    )

    _assert_first_reread_is_masked(_read(hook, other_path))
    changed = _read(hook, changed_path, returned_start=35, returned_end=50)
    assert changed is not None
    assert "already_covered" not in changed.updated_tool_output


def test_successful_write_file_resets_pending_escalation(tmp_path: Path) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "a.py"
    _read(hook, path)
    assert _read(hook, path) is not None
    assert hook._redundant_counts

    _mutate(hook, "write_file", path)

    assert hook._redundant_counts == {}
    assert _read(hook, path) is None


def test_failed_reads_do_not_advance_pending_escalation(tmp_path: Path) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "a.py"
    _read(hook, path)
    _assert_first_reread_is_masked(_read(hook, path))

    assert _read(hook, path, ok=False) is None
    response = _read(hook, path)
    assert response is not None
    assert response.updated_tool_output["already_covered"] is True


def test_redundant_streaks_are_independent_per_file(tmp_path: Path) -> None:
    hook = RepeatedReadNudge()
    first_path = tmp_path / "a.py"
    second_path = tmp_path / "b.py"
    _read(hook, first_path)
    _read(hook, second_path)

    _assert_first_reread_is_masked(_read(hook, first_path))
    _assert_first_reread_is_masked(_read(hook, second_path))
    assert _read(hook, first_path).updated_tool_output["already_covered"] is True
    assert _read(hook, second_path).updated_tool_output["already_covered"] is True


def test_useful_reads_preserve_strikes_independently_per_file(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge()
    first_path = tmp_path / "a.py"
    second_path = tmp_path / "b.py"
    _read(hook, first_path)
    _read(hook, second_path)
    _assert_first_reread_is_masked(_read(hook, first_path))
    _assert_first_reread_is_masked(_read(hook, second_path))

    assert _read(hook, first_path, returned_start=101, returned_end=200) is None
    first_escalated = _read(
        hook, first_path, returned_start=101, returned_end=200
    )
    assert first_escalated.updated_tool_output["message"] == (
        ESCALATED_ALREADY_COVERED_MESSAGE
    )

    # Activity for the first file must not advance or forgive the second file.
    second_escalated = _read(hook, second_path)
    assert second_escalated.updated_tool_output["message"] == (
        ESCALATED_ALREADY_COVERED_MESSAGE
    )


def test_failed_mutations_do_not_invalidate_prior_reads(tmp_path: Path) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "a.py"
    _read(hook, path)

    _mutate(hook, "apply_patch", path, ok=False)

    _assert_first_reread_is_masked(_read(hook, path))

    _mutate(hook, "write_file", path, ok=False)

    response = _read(hook, path)
    assert response is not None
    assert response.updated_tool_output["already_covered"] is True


def test_before_run_clears_history(tmp_path: Path) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "a.py"
    _read(hook, path)
    _assert_first_reread_is_masked(_read(hook, path))
    assert hook._redundant_counts

    hook.before_run(None)

    assert hook._redundant_counts == {}
    assert _read(hook, path) is None


def test_successful_compaction_allows_fresh_read_then_masks_immediate_reread(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge()
    runner = HookRunner([hook])
    path = tmp_path / "a.py"
    action = {
        "tool": "read_file",
        "args": {
            "path": str(path),
            "ranges": [{"start_line": 100, "end_line": 180}],
        },
    }

    def observe(content: str) -> HookResponse:
        return runner.tool_result(
            ToolResultContext(
                session_id="session-1",
                repo_root=str(tmp_path),
                action=action,
                result={
                    "ok": True,
                    "tool": "read_file",
                    "path": str(path),
                    "content": content,
                    "start_line": 100,
                    "end_line": 180,
                },
            )
        )

    assert observe("ORIGINAL SOURCE").updated_tool_output["content"] == (
        "ORIGINAL SOURCE"
    )
    before_compaction = observe("REDUNDANT SOURCE")
    assert before_compaction.updated_tool_output["already_covered"] is True
    assert "content" not in before_compaction.updated_tool_output

    runner.compaction_applied(
        CompactionAppliedContext(session_id="session-1", repo_root=str(tmp_path))
    )

    fresh = observe("SOURCE RETURNED AFTER COMPACTION")
    assert fresh.updated_tool_output["content"] == "SOURCE RETURNED AFTER COMPACTION"
    immediate_reread = observe("REDUNDANT SOURCE AFTER COMPACTION")
    assert immediate_reread.updated_tool_output["already_covered"] is True
    assert "content" not in immediate_reread.updated_tool_output


def test_successful_compaction_clears_all_files_and_redundant_read_state(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge(threshold=3)
    first_path = tmp_path / "first.py"
    second_path = tmp_path / "second.py"

    _read(hook, first_path)
    _read(hook, second_path)
    assert _read(hook, first_path) is None
    _mutate(
        hook,
        "apply_patch",
        second_path,
        start_line=40,
        end_line_before=50,
        end_line_after=50,
    )
    assert hook._coverage
    assert hook._stale_ranges
    assert hook._redundant_counts

    hook.compaction_applied(None)

    assert hook._coverage == {}
    assert hook._stale_ranges == {}
    assert hook._redundant_counts == {}
    assert _read(hook, first_path) is None
    assert _read(hook, second_path) is None


def test_escalated_mask_persists_until_coverage_is_invalidated(
    tmp_path: Path,
) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "a.py"
    _read(hook, path)
    _assert_first_reread_is_masked(_read(hook, path))
    assert hook._redundant_counts

    escalated = _read(hook, path)
    assert escalated.updated_tool_output["already_covered"] is True
    assert escalated.updated_tool_output["message"] == ESCALATED_ALREADY_COVERED_MESSAGE
    assert escalated.additional_context == ""
    assert hook._redundant_counts

    still_escalated = _read(hook, path)
    assert still_escalated.updated_tool_output["message"] == (
        ESCALATED_ALREADY_COVERED_MESSAGE
    )


def test_expansion_only_overlap_filters_without_a_strike(tmp_path: Path) -> None:
    """New requested lines remain useful even when Tool-added context overlaps."""
    hook = RepeatedReadNudge()
    path = tmp_path / "nearby.py"
    path.write_text(_source(1, 700), encoding="utf-8")

    def read(start: int):
        action = {"tool": "read_file", "args": {
            "path": str(path),
            "ranges": [{"start_line": start, "end_line": start + 9}],
        }}
        return hook.tool_result(_Context(
            action=action, result=read_file(tmp_path, **action["args"])
        ))

    assert read(100) is None  # Returned coverage is 30-179.
    for start, novel_start, novel_end in [(200, 180, 279), (300, 280, 379)]:
        response = read(start)
        assert response.additional_context == ""
        assert response.updated_tool_output["content"] == _source(novel_start, novel_end)
        assert response.updated_tool_output["returned_line_ranges"] == [
            {"start_line": novel_start, "end_line": novel_end}
        ]
        assert not hook._redundant_counts

    # Deliberately asking for visible source still earns the first strike,
    # then escalates. Incidental overlaps must not have advanced that state.
    _assert_first_reread_is_masked(read(200))
    assert read(200).updated_tool_output["message"] == ESCALATED_ALREADY_COVERED_MESSAGE


def test_disjoint_request_with_one_deliberate_overlap_earns_strike(tmp_path: Path) -> None:
    hook = RepeatedReadNudge()
    path = tmp_path / "multiple.py"
    path.write_text(_source(1, 700), encoding="utf-8")
    _read(hook, path, returned_start=1, returned_end=150, content=_source(1, 150))
    action = {"tool": "read_file", "args": {
        "path": str(path), "ranges": [
            {"start_line": 100, "end_line": 109},
            {"start_line": 500, "end_line": 509},
        ],
    }}
    response = hook.tool_result(_Context(
        action=action, result=read_file(tmp_path, **action["args"])
    ))
    assert response.additional_context == GUIDANCE
    assert response.updated_tool_output["returned_line_ranges"] == [
        {"start_line": 151, "end_line": 179},
        {"start_line": 430, "end_line": 579},
    ]
