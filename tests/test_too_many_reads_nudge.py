"""Focused coverage for the behavioral too-many-reads nudge."""

from dataclasses import dataclass

import pytest

from squid_core.hooks import (
    BeforeModelTurnContext,
    BeforeRunContext,
    CompactionAppliedContext,
    DEFAULT_FIRST_NUDGE_READS,
    DEFAULT_STRONG_NUDGE_READS,
    HookRunner,
    TOO_MANY_READS_GUIDANCE,
    TOO_MANY_READS_STRONG_GUIDANCE,
    TooManyReadsNudge,
)


@dataclass(frozen=True)
class _Context:
    action: dict
    result: dict


def _result(
    hook: TooManyReadsNudge,
    tool: str,
    *,
    ok: bool = True,
    output: str = "source",
    result_metadata: dict | None = None,
):
    return hook.tool_result(
        _Context(
            action={"tool": tool, "args": {}},
            result={
                "ok": ok,
                "tool": tool,
                "output": output,
                **(result_metadata or {}),
            },
        )
    )


def _successful_reads(hook: TooManyReadsNudge, count: int):
    return [_result(hook, "read_file") for _ in range(count)]


def _guidance(hook: TooManyReadsNudge) -> str:
    response = hook.before_model_turn(
        BeforeModelTurnContext(None, ".", "task", (), 0)
    )
    if response is None:
        return ""
    return "\n\n".join(block.body for block in response.context_blocks)


def test_fifth_successful_read_renders_first_nudge_at_model_boundary() -> None:
    hook = TooManyReadsNudge()
    responses = _successful_reads(hook, DEFAULT_FIRST_NUDGE_READS)

    assert responses == [None] * DEFAULT_FIRST_NUDGE_READS
    assert _guidance(hook) == TOO_MANY_READS_GUIDANCE
    assert _guidance(hook) == ""


@pytest.mark.parametrize("mutation", ["apply_patch", "write_file"])
def test_later_mutation_discards_armed_read_guidance(mutation: str) -> None:
    hook = TooManyReadsNudge()
    _successful_reads(hook, DEFAULT_FIRST_NUDGE_READS)

    assert _result(hook, mutation) is None
    assert _guidance(hook) == ""


def test_eighth_and_later_successful_reads_produce_strong_nudge() -> None:
    hook = TooManyReadsNudge()
    assert _successful_reads(hook, DEFAULT_STRONG_NUDGE_READS) == (
        [None] * DEFAULT_STRONG_NUDGE_READS
    )
    assert _guidance(hook) == TOO_MANY_READS_STRONG_GUIDANCE

    assert _result(hook, "read_file") is None
    assert _guidance(hook) == TOO_MANY_READS_STRONG_GUIDANCE


def test_broad_reads_nudge_earlier_by_returned_line_volume() -> None:
    hook = TooManyReadsNudge()

    first = _result(
        hook,
        "read_file",
        result_metadata={"start_line": 1, "end_line": 1_000},
    )
    second = _result(
        hook,
        "read_file",
        result_metadata={"start_line": 1, "end_line": 500},
    )

    assert first is None
    assert second is None
    assert _guidance(hook) == TOO_MANY_READS_GUIDANCE


def test_only_novel_ranges_from_shaped_result_add_read_volume() -> None:
    hook = TooManyReadsNudge()
    ranges = [{"start_line": 1, "end_line": 200}]

    results = [
        _result(
            hook,
            "read_file",
            result_metadata={"returned_line_ranges": ranges},
        )
        for _ in range(DEFAULT_FIRST_NUDGE_READS)
    ]

    # The action-count threshold still applies even though shaped novel coverage
    # has not reached the independent 1,500-line threshold.
    assert results == [None] * DEFAULT_FIRST_NUDGE_READS
    assert _guidance(hook) == TOO_MANY_READS_GUIDANCE


def test_already_covered_result_counts_as_an_action_but_not_source_volume() -> None:
    hook = TooManyReadsNudge(first_nudge_reads=4, strong_nudge_reads=6)

    assert _result(
        hook,
        "read_file",
        result_metadata={"start_line": 1, "end_line": 1_000},
    ) is None
    assert _result(
        hook,
        "read_file",
        result_metadata={
            "already_covered": True,
            "start_line": 1,
            "end_line": 1_000,
        },
    ) is None
    third = _result(
        hook,
        "read_file",
        result_metadata={"start_line": 1, "end_line": 500},
    )

    assert third is None
    assert _guidance(hook) == TOO_MANY_READS_GUIDANCE


def test_broad_reads_reach_strong_volume_guidance_before_fifth_read() -> None:
    hook = TooManyReadsNudge()
    results = [
        _result(
            hook,
            "read_file",
            result_metadata={"start_line": 1, "end_line": 1_000},
        )
        for _ in range(3)
    ]

    assert results == [None, None, None]
    # One batch gets one logical warning, using the strongest final state.
    assert _guidance(hook) == TOO_MANY_READS_STRONG_GUIDANCE


def test_successful_mutation_resets_returned_line_volume() -> None:
    hook = TooManyReadsNudge()
    broad_result = {"start_line": 1, "end_line": 1_000}

    assert _result(hook, "read_file", result_metadata=broad_result) is None
    assert _result(hook, "apply_patch") is None
    assert _result(hook, "read_file", result_metadata=broad_result) is None


def test_neutral_commands_do_not_make_reads_consecutive() -> None:
    hook = TooManyReadsNudge()
    for _ in range(DEFAULT_FIRST_NUDGE_READS - 1):
        assert _result(hook, "read_file") is None
        assert _result(hook, "run_command") is None
    assert _result(hook, "read_file") is None
    assert _guidance(hook) == TOO_MANY_READS_GUIDANCE


@pytest.mark.parametrize("mutation", ["apply_patch", "write_file"])
def test_successful_mutation_resets_read_count(mutation: str) -> None:
    hook = TooManyReadsNudge()
    _successful_reads(hook, DEFAULT_FIRST_NUDGE_READS - 1)
    assert _result(hook, mutation) is None

    assert _successful_reads(hook, DEFAULT_FIRST_NUDGE_READS - 1) == (
        [None] * (DEFAULT_FIRST_NUDGE_READS - 1)
    )
    assert _result(hook, "read_file") is None
    assert _guidance(hook) == TOO_MANY_READS_GUIDANCE


def test_failed_mutation_does_not_reset_read_count() -> None:
    hook = TooManyReadsNudge()
    _successful_reads(hook, DEFAULT_FIRST_NUDGE_READS - 1)
    assert _result(hook, "apply_patch", ok=False) is None

    assert _result(hook, "read_file") is None
    assert _guidance(hook) == TOO_MANY_READS_GUIDANCE


def test_failed_read_neither_increments_nor_resets() -> None:
    hook = TooManyReadsNudge()
    _successful_reads(hook, DEFAULT_FIRST_NUDGE_READS - 1)
    assert _result(hook, "read_file", ok=False) is None

    assert _result(hook, "read_file") is None
    assert _guidance(hook) == TOO_MANY_READS_GUIDANCE


def test_compaction_does_not_reset_read_count() -> None:
    hook = TooManyReadsNudge()
    runner = HookRunner([hook])
    _successful_reads(hook, DEFAULT_FIRST_NUDGE_READS - 1)
    runner.compaction_applied(CompactionAppliedContext(None, "."))

    assert _result(hook, "read_file") is None
    assert _guidance(hook) == TOO_MANY_READS_GUIDANCE


def test_before_run_resets_read_count() -> None:
    hook = TooManyReadsNudge()
    _successful_reads(hook, DEFAULT_FIRST_NUDGE_READS - 1)
    hook.before_run(BeforeRunContext(None, ".", "task"))

    assert _successful_reads(hook, DEFAULT_FIRST_NUDGE_READS - 1) == (
        [None] * (DEFAULT_FIRST_NUDGE_READS - 1)
    )


def test_hook_does_not_modify_read_tool_output() -> None:
    hook = TooManyReadsNudge(first_nudge_reads=1, strong_nudge_reads=2)
    result = {"ok": True, "tool": "read_file", "output": "unaltered source"}

    response = hook.tool_result(
        _Context(action={"tool": "read_file", "args": {}}, result=result)
    )

    assert response is None
    assert _guidance(hook) == TOO_MANY_READS_GUIDANCE
    assert result == {
        "ok": True,
        "tool": "read_file",
        "output": "unaltered source",
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"first_nudge_reads": 0, "strong_nudge_reads": 2},
        {"first_nudge_reads": 2, "strong_nudge_reads": 2},
        {"first_nudge_reads": 3, "strong_nudge_reads": 2},
        {"first_nudge_lines": 0, "strong_nudge_lines": 2},
        {"first_nudge_lines": 2, "strong_nudge_lines": 2},
        {"first_nudge_lines": 3, "strong_nudge_lines": 2},
    ],
)
def test_thresholds_are_validated(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        TooManyReadsNudge(**kwargs)


def test_read_guidance_uses_current_model_facing_contract() -> None:
    assert TOO_MANY_READS_GUIDANCE == (
        "hey, dummy. do you think you have read enough yet? I think you have "
        "consumed the whole repo at least once since I asked you to make this "
        "simple patch."
    )
    assert TOO_MANY_READS_STRONG_GUIDANCE == (
        "HIGH PRIORITY: Stop. I am getting impatient. If you read one more file "
        "without making an edit, I am going to report you to management. Make the "
        "edit now."
    )
