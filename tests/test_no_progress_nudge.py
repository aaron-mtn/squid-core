"""Focused coverage for the conservative runtime no-progress nudge."""

from dataclasses import dataclass

from squid_core.hooks import HookResponse, RepeatedFailureNudge


GUIDANCE = "Choose a materially different next action."


@dataclass(frozen=True)
class _Context:
    action: dict | None = None
    result: dict | None = None


def _failure(
    hook: RepeatedFailureNudge,
    *,
    args: dict | None = None,
    error: str = "not_found",
) -> HookResponse | None:
    return hook.tool_result(
        _Context(
            action={"tool": "edit", "args": args or {"path": "a.py", "old": "x"}},
            result={"ok": False, "error": error},
        )
    )


def _command_result(
    hook: RepeatedFailureNudge,
    command: str,
    *,
    ok: bool = False,
    failure_class: str = "process_failed",
) -> HookResponse | None:
    result = {"ok": ok, "tool": "run_command"}
    if not ok:
        result.update(
            {
                "failure_class": failure_class,
                "platform": "Windows",
                "shell": "PowerShell",
            }
        )
    return hook.tool_result(
        _Context(
            action={"tool": "run_command", "args": {"command": command}},
            result=result,
        )
    )


def test_one_failure_does_not_trigger_a_nudge() -> None:
    hook = RepeatedFailureNudge(GUIDANCE)

    assert _failure(hook) is None


def test_three_equivalent_failures_trigger_one_transient_nudge() -> None:
    hook = RepeatedFailureNudge(GUIDANCE)

    assert _failure(hook) is None
    assert _failure(hook) is None
    assert _failure(hook) == HookResponse(additional_context=GUIDANCE)


def test_materially_changed_retry_breaks_the_repetition_pattern() -> None:
    hook = RepeatedFailureNudge(GUIDANCE)

    _failure(hook)
    _failure(hook, args={"path": "a.py", "old": "more specific x"})
    _failure(hook)

    assert _failure(hook) is None


def test_different_inspections_and_successful_verification_do_not_trigger() -> None:
    hook = RepeatedFailureNudge(GUIDANCE)

    for start in (1, 20, 40):
        hook.tool_result(
            _Context(
                action={"tool": "read", "args": {"path": "a.py", "start": start}},
                result={"ok": True, "output": "current content"},
            )
        )

    assert hook.tool_result(
        _Context(
            action={"tool": "read", "args": {"path": "a.py", "start": 60}},
            result={"ok": True, "output": "current content"},
        )
    ) is None


def test_successful_progress_resets_a_failure_streak() -> None:
    hook = RepeatedFailureNudge(GUIDANCE)
    _failure(hook)
    _failure(hook)

    hook.tool_result(
        _Context(
            action={"tool": "edit", "args": {"path": "a.py", "old": "x"}},
            result={"ok": True},
        )
    )
    _failure(hook)

    assert _failure(hook) is None


def test_changed_failure_category_breaks_the_repetition_pattern() -> None:
    hook = RepeatedFailureNudge(GUIDANCE)

    _failure(hook)
    _failure(hook, error="permission_denied")
    _failure(hook)

    assert _failure(hook) is None


def test_a_nudge_requires_a_fresh_threshold_before_repeating() -> None:
    hook = RepeatedFailureNudge(GUIDANCE)
    assert _failure(hook) is None
    assert _failure(hook) is None
    assert _failure(hook) is not None


def test_three_ordinary_failed_commands_do_not_trigger_shell_recovery() -> None:
    hook = RepeatedFailureNudge(GUIDANCE, run_command_guidance="Reassess commands.")

    assert _command_result(hook, "pytest tests/test_one.py") is None
    assert _command_result(hook, "pytest tests/test_two.py") is None
    assert _command_result(hook, "pytest tests/test_three.py") is None


def test_repeated_shell_failures_trigger_shell_recovery_nudge() -> None:
    hook = RepeatedFailureNudge(GUIDANCE, run_command_guidance="Reassess commands.")

    assert (
        _command_result(hook, "missing-one", failure_class="command_not_found")
        is None
    )
    assert (
        _command_result(hook, "missing-two", failure_class="command_not_found")
        is None
    )
    response = _command_result(
        hook, "missing-three", failure_class="command_not_found"
    )

    assert response is not None
    assert "Reassess commands." in response.additional_context
    assert (
        "Configured environment: Windows / PowerShell"
        in response.additional_context
    )
    assert "Repeated failure class: command_not_found" in response.additional_context
    assert response.block is False


def test_repeated_timeouts_use_timeout_not_shell_guidance() -> None:
    hook = RepeatedFailureNudge(
        GUIDANCE,
        run_command_guidance="Reassess shell commands.",
        run_command_timeout_guidance="The command exceeded its deadline.",
    )

    assert _command_result(hook, "slow-one", failure_class="timeout") is None
    assert _command_result(hook, "slow-two", failure_class="timeout") is None
    response = _command_result(hook, "slow-three", failure_class="timeout")

    assert response is not None
    assert "exceeded its deadline" in response.additional_context
    assert "Reassess shell commands" not in response.additional_context
    assert "Repeated failure class: timeout" in response.additional_context


def test_different_command_failure_classes_do_not_share_a_streak() -> None:
    hook = RepeatedFailureNudge(
        GUIDANCE,
        run_command_guidance="Reassess commands.",
        run_command_timeout_guidance="The command exceeded its deadline.",
    )

    _command_result(hook, "missing", failure_class="command_not_found")
    _command_result(hook, "slow", failure_class="timeout")
    _command_result(hook, "syntax", failure_class="shell_syntax_error")

    assert (
        _command_result(hook, "syntax-again", failure_class="shell_syntax_error")
        is None
    )


def test_successful_file_mutation_breaks_command_failure_streak() -> None:
    hook = RepeatedFailureNudge(GUIDANCE, run_command_guidance="Reassess commands.")
    _command_result(hook, "missing-one", failure_class="command_not_found")
    _command_result(hook, "missing-two", failure_class="command_not_found")

    # Successful file Tools are the current mutation signal. They do not yet
    # distinguish a real content change from a no-op; H6 owns that refinement.
    hook.tool_result(
        _Context(
            action={"tool": "apply_patch", "args": {"path": "a.py"}},
            result={"ok": True, "tool": "apply_patch", "path": "a.py"},
        )
    )

    assert (
        _command_result(hook, "missing-three", failure_class="command_not_found")
        is None
    )
    assert (
        _command_result(hook, "missing-four", failure_class="command_not_found")
        is None
    )


def test_successful_command_resets_command_failure_streak() -> None:
    hook = RepeatedFailureNudge(GUIDANCE, run_command_guidance="Reassess commands.")
    _command_result(hook, "first", failure_class="command_not_found")
    _command_result(hook, "second", failure_class="command_not_found")

    _command_result(hook, "success", ok=True)

    assert (
        _command_result(hook, "third", failure_class="command_not_found") is None
    )
    assert (
        _command_result(hook, "fourth", failure_class="command_not_found") is None
    )


def test_unrelated_discovery_does_not_increment_or_reset_command_streak() -> None:
    hook = RepeatedFailureNudge(GUIDANCE, run_command_guidance="Reassess commands.")
    _command_result(hook, "first", failure_class="command_not_found")
    assert hook.tool_result(
        _Context(
            action={"tool": "read_file", "args": {"path": "a.py"}},
            result={"ok": True, "tool": "read_file"},
        )
    ) is None
    assert (
        _command_result(hook, "second", failure_class="command_not_found") is None
    )

    response = _command_result(hook, "third", failure_class="command_not_found")

    assert response is not None


def test_command_nudge_resets_and_does_not_block_the_next_command() -> None:
    hook = RepeatedFailureNudge(GUIDANCE, run_command_guidance="Reassess commands.")
    _command_result(hook, "first", failure_class="command_not_found")
    _command_result(hook, "second", failure_class="command_not_found")
    response = _command_result(hook, "third", failure_class="command_not_found")

    assert response is not None
    assert response.block is False
    assert (
        _command_result(hook, "fourth", failure_class="command_not_found") is None
    )

    assert _failure(hook) is None
    assert _failure(hook) is None
    assert _failure(hook) is not None
