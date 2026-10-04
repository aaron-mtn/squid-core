"""Focused regressions for self-contained post-verification guidance."""

from dataclasses import dataclass

import pytest

from squid_core.hooks import (
    BeforeModelTurnContext,
    BeforeRunContext,
    CURRENT_VERIFICATION_STATE_GUIDANCE,
    HookRunner,
    POST_VERIFICATION_GUIDANCE,
    PostVerificationNudge,
    RepeatedReadNudge,
    ToolResultContext,
)


@dataclass(frozen=True)
class _Context:
    action: dict
    result: dict


def _context(
    tool: str,
    *,
    ok: bool = True,
    command: str | None = None,
    verification: bool | None = None,
    **result_fields,
) -> _Context:
    args = {}
    result = {"ok": ok, "tool": tool, **result_fields}
    if command is not None:
        args["command"] = command
        result["command"] = command
    if verification is not None:
        args["verification"] = verification
        result["verification"] = verification
    return _Context(action={"tool": tool, "args": args}, result=result)


def _runner(threshold: int = 2) -> tuple[HookRunner, PostVerificationNudge]:
    nudge = PostVerificationNudge(threshold=threshold)
    return HookRunner([nudge]), nudge


def _observe(runner: HookRunner, context: _Context):
    return runner.tool_result(
        ToolResultContext(None, ".", context.action, context.result)
    )


def _guidance(runner: HookRunner) -> str:
    response = runner.before_model_turn(
        BeforeModelTurnContext(None, ".", "task", (), 0)
    )
    return "\n\n".join(block.body for block in response.context_blocks)


def test_successful_explicit_verification_is_visible_at_next_model_boundary() -> None:
    runner, nudge = _runner()
    response = _observe(
        runner,
        _context("run_command", command="pytest focused.py", verification=True),
    )
    assert nudge._verification_current is True
    assert nudge._post_verification_action_count == 0
    assert response.additional_context == ""
    # No intervening read or other Tool action is needed to expose the receipt.
    assert _guidance(runner) == CURRENT_VERIFICATION_STATE_GUIDANCE


def test_failed_explicit_verification_disables_tracking() -> None:
    runner, nudge = _runner()
    _observe(
        runner,
        _context("run_command", command="pytest focused.py", verification=True),
    )
    _observe(runner, _context("read_file"))
    response = _observe(
        runner,
        _context(
            "run_command",
            ok=False,
            command="pytest broader.py",
            verification=True,
        ),
    )
    assert nudge._verification_current is False
    assert nudge._post_verification_action_count == 0
    assert response.additional_context == ""
    assert _guidance(runner) == ""


@pytest.mark.parametrize("tool", ["apply_patch", "write_file"])
def test_successful_mutation_invalidates_verification(tool: str) -> None:
    runner, nudge = _runner()
    _observe(
        runner,
        _context("run_command", command="pytest focused.py", verification=True),
    )
    _observe(runner, _context("read_file"))
    _observe(runner, _context(tool))
    assert nudge._verification_current is False
    assert nudge._post_verification_action_count == 0
    assert _guidance(runner) == ""


def test_failed_mutation_does_not_invalidate_verification() -> None:
    runner, nudge = _runner()
    _observe(
        runner,
        _context("run_command", command="pytest focused.py", verification=True),
    )
    _observe(runner, _context("apply_patch", ok=False))
    assert nudge._verification_current is True
    assert nudge._post_verification_action_count == 0


def test_differently_worded_successful_verification_counts_without_reset() -> None:
    runner, nudge = _runner()
    _observe(
        runner,
        _context("run_command", command="pytest focused.py", verification=True),
    )
    response = _observe(
        runner,
        _context("run_command", command="pytest", verification=True),
    )
    assert response.additional_context == ""
    assert nudge._post_verification_action_count == 1

    response = _observe(runner, _context("read_file"))
    assert response.additional_context == ""
    assert _guidance(runner) == POST_VERIFICATION_GUIDANCE


def test_repeated_successful_verification_increments_action_count() -> None:
    runner, nudge = _runner()
    _observe(
        runner,
        _context("run_command", command="  pytest focused.py\r\n", verification=True),
    )
    response = _observe(
        runner,
        _context("run_command", command="pytest focused.py", verification=True),
    )
    assert nudge._post_verification_action_count == 1
    assert response.additional_context == ""
    guidance = _guidance(runner)
    assert guidance == CURRENT_VERIFICATION_STATE_GUIDANCE
    assert guidance.count("Latest explicit verification succeeded") == 1


@pytest.mark.parametrize(
    "tool",
    ["read_file", "search_files", "run_command", "inspect_symbols"],
)
def test_successful_non_mutating_actions_after_verification_count(tool: str) -> None:
    runner, nudge = _runner()
    _observe(
        runner,
        _context("run_command", command="pytest focused.py", verification=True),
    )
    response = _observe(runner, _context(tool, command="git status"))
    assert response.additional_context == ""
    assert nudge._post_verification_action_count == 1


def test_second_non_mutating_action_triggers_guidance() -> None:
    runner, nudge = _runner()
    _observe(
        runner,
        _context("run_command", command="pytest focused.py", verification=True),
    )
    assert _observe(runner, _context("read_file")).additional_context == ""

    response = _observe(runner, _context("search_files"))

    assert response.additional_context == ""
    assert _guidance(runner) == POST_VERIFICATION_GUIDANCE
    assert nudge._post_verification_action_count == 0


def test_immediate_state_does_not_consume_stronger_guidance_threshold() -> None:
    runner, nudge = _runner(threshold=2)
    _observe(
        runner,
        _context("run_command", command="pytest focused.py", verification=True),
    )
    assert _guidance(runner) == CURRENT_VERIFICATION_STATE_GUIDANCE

    _observe(runner, _context("read_file"))
    assert _guidance(runner) == CURRENT_VERIFICATION_STATE_GUIDANCE
    assert nudge._post_verification_action_count == 1

    _observe(runner, _context("search_files"))
    assert _guidance(runner) == POST_VERIFICATION_GUIDANCE
    assert nudge._post_verification_action_count == 0


@pytest.mark.parametrize("mutation", ["apply_patch", "write_file"])
def test_later_mutation_discards_armed_post_verification_guidance(
    mutation: str,
) -> None:
    runner, _nudge = _runner()
    _observe(
        runner,
        _context("run_command", command="pytest focused.py", verification=True),
    )
    _observe(runner, _context("read_file"))
    _observe(runner, _context("search_files"))

    _observe(runner, _context(mutation))

    assert _guidance(runner) == ""


def test_already_covered_read_counts_without_stacking_guidance() -> None:
    runner, nudge = _runner(threshold=1)
    _observe(
        runner,
        _context("run_command", command="pytest focused.py", verification=True),
    )
    response = _observe(runner, _context("read_file", already_covered=True))
    assert nudge._post_verification_action_count == 1
    assert response.additional_context == ""


def test_later_action_uses_count_accumulated_by_already_covered_read() -> None:
    runner, nudge = _runner(threshold=2)
    verification = _context(
        "run_command", command="pytest focused.py", verification=True
    )
    _observe(runner, verification)
    _observe(runner, _context("read_file", already_covered=True))
    response = _observe(runner, verification)
    assert response.additional_context == ""
    assert _guidance(runner) == POST_VERIFICATION_GUIDANCE
    assert nudge._post_verification_action_count == 0


def test_repeated_read_guidance_is_not_stacked_with_post_verification() -> None:
    post_verification = PostVerificationNudge(threshold=2)
    runner = HookRunner([RepeatedReadNudge(), post_verification])
    verification = _context(
        "run_command", command="pytest focused.py", verification=True
    )
    read = _Context(
        action={"tool": "read_file", "args": {"path": "a.py"}},
        result={
            "ok": True,
            "tool": "read_file",
            "path": "a.py",
            "content": "source",
            "start_line": 1,
            "end_line": 10,
        },
    )
    _observe(runner, verification)
    _observe(runner, read)

    repeated_read = _observe(runner, read)

    assert repeated_read.updated_tool_output["already_covered"] is True
    assert (
        "You already read this unchanged source"
        in repeated_read.additional_context
    )
    assert POST_VERIFICATION_GUIDANCE not in repeated_read.additional_context
    assert post_verification._post_verification_action_count == 2

    later = _observe(runner, verification)
    assert later.additional_context == ""
    assert _guidance(runner) == POST_VERIFICATION_GUIDANCE
    assert post_verification._post_verification_action_count == 0


def test_threshold_guidance_and_reset_remain_equivalent() -> None:
    runner, nudge = _runner(threshold=3)
    _observe(
        runner,
        _context("run_command", command="pytest focused.py", verification=True),
    )
    assert _observe(runner, _context("read_file")).additional_context == ""
    assert _observe(runner, _context("read_file")).additional_context == ""
    response = _observe(runner, _context("read_file"))
    assert response.additional_context == ""
    assert response.updated_tool_output["ok"] is True
    assert nudge._post_verification_action_count == 3
    assert _guidance(runner) == POST_VERIFICATION_GUIDANCE
    assert nudge._post_verification_action_count == 0


def test_verification_after_mutation_starts_fresh_window() -> None:
    runner, nudge = _runner()
    verification = _context(
        "run_command", command="pytest focused.py", verification=True
    )
    _observe(runner, verification)
    _observe(runner, _context("read_file"))
    _observe(runner, _context("apply_patch"))

    response = _observe(runner, verification)

    assert response.additional_context == ""
    assert nudge._verification_current is True
    assert nudge._post_verification_action_count == 0
    assert _observe(runner, _context("read_file")).additional_context == ""
    assert _observe(runner, _context("search_files")).additional_context == ""
    assert _guidance(runner) == POST_VERIFICATION_GUIDANCE


def test_new_run_resets_all_post_verification_state() -> None:
    runner, nudge = _runner()
    _observe(
        runner,
        _context("run_command", command="pytest focused.py", verification=True),
    )
    _observe(runner, _context("read_file"))
    runner.before_run(BeforeRunContext(None, ".", "test"))
    assert nudge._verification_current is False
    assert nudge._post_verification_action_count == 0


def test_failed_non_verification_action_does_not_count() -> None:
    runner, nudge = _runner()
    _observe(
        runner,
        _context("run_command", command="pytest focused.py", verification=True),
    )
    response = _observe(
        runner,
        _context(
            "run_command",
            ok=False,
            command="custom command",
            verification=False,
        ),
    )
    assert response.additional_context == ""
    assert nudge._verification_current is True
    assert nudge._post_verification_action_count == 0


def test_escalation_guidance_uses_current_model_facing_contract() -> None:
    assert POST_VERIFICATION_GUIDANCE == (
        "I’m getting impatient. The tests passed. Stop wasting time running more "
        "checks or I will have to get corporate involved. If there isn’t a specific "
        "remaining problem, finish the task now."
    )


def test_current_state_guidance_is_advisory_and_limited_to_known_facts() -> None:
    assert CURRENT_VERIFICATION_STATE_GUIDANCE == (
        "* Latest explicit verification succeeded.\n"
        "* No successful built-in file edit has occurred since.\n"
        "* No unresolved explicit verification failure is known.\n\n"
        "If the task is complete, finalize it now."
    )
