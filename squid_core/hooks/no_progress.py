"""Conservative repeated-failure detection for transient model guidance."""

from __future__ import annotations

import json
from typing import Any

from .runner import HookResponse


DEFAULT_REPEATED_FAILURE_THRESHOLD = 3
_MUTATION_TOOLS = {"apply_patch", "write_file"}
_SHELL_OR_LAUNCH_FAILURES = {
    "command_not_found",
    "path_not_found",
    "permission_denied",
    "shell_syntax_error",
    "unknown_failure",
}


class RepeatedFailureNudge:
    """Offer soft guidance after deterministic clusters of failed Tool actions.

    The hook deliberately compares normalized actions exactly. It does not try
    to infer intent or semantic similarity: a changed action, a successful
    result, or a failure without a stable error category breaks that streak.
    When command guidance is configured, a separate counter recognizes
    repeated shell/launch failures or timeouts regardless of command strings.
    Ordinary completed-process failures remain diagnostics for the model.
    """

    def __init__(
        self,
        guidance: str,
        *,
        run_command_guidance: str | None = None,
        run_command_timeout_guidance: str | None = None,
        threshold: int = DEFAULT_REPEATED_FAILURE_THRESHOLD,
        source: str = "runtime",
    ) -> None:
        if threshold < 2:
            raise ValueError("repeated-failure threshold must be at least 2")

        self.guidance = guidance
        self.run_command_guidance = run_command_guidance
        self.run_command_timeout_guidance = run_command_timeout_guidance
        self.threshold = threshold
        # Hosts inspect Hook sources as configuration metadata. It no longer
        # participates in prompt rendering now that nudges are plain guidance.
        self.source = source
        self._fingerprint: tuple[str, str, str] | None = None
        self._repeat_count = 0
        self._run_command_failure_count = 0
        self._run_command_failure_class: str | None = None
        self._run_command_environment: tuple[str, str] | None = None

    def before_run(self, _ctx) -> None:
        """Start each run with an empty, run-local repetition history."""
        self._reset()

    def tool_result(self, ctx) -> HookResponse | None:
        """Observe the final result that will be exposed to the model."""
        if (
            ctx.action.get("tool") in _MUTATION_TOOLS
            and ctx.result.get("ok") is True
        ):
            # File Tool success is the strongest mutation fact currently
            # exposed to Hooks. H6 may later distinguish actual edits from
            # successful no-ops; until then both conservatively break a streak.
            self._reset_run_command_failures()

        command_response = self._observe_run_command(ctx.action, ctx.result)
        if (
            command_response is not None
            or ctx.action.get("tool") == "run_command"
            and isinstance(ctx.result.get("failure_class"), str)
        ):
            # Classified results came from a real command execution and are
            # wholly owned by the command streak. Do not also feed them to the
            # exact-action detector or reset them as an uncategorized failure.
            self._reset_repeated_failure()
            return command_response

        fingerprint = _failed_action_fingerprint(ctx.action, ctx.result)
        if fingerprint is None:
            self._reset_repeated_failure()
            return

        if fingerprint == self._fingerprint:
            self._repeat_count += 1
        else:
            self._fingerprint = fingerprint
            self._repeat_count = 1

        if self._repeat_count >= self.threshold:
            # A nudge is an opportunity to recover, not a latched warning.
            # Another nudge requires a complete new threshold of failures.
            self._reset_repeated_failure()
            return HookResponse(additional_context=self.guidance)

        return None

    def _reset(self) -> None:
        self._reset_repeated_failure()
        self._reset_run_command_failures()

    def _reset_repeated_failure(self) -> None:
        self._fingerprint = None
        self._repeat_count = 0

    def _observe_run_command(
        self,
        action: dict[str, Any],
        result: dict[str, Any],
    ) -> HookResponse | None:
        """Track repeated command failures only when their structured cause matches."""
        if action.get("tool") != "run_command":
            return None

        if result.get("ok") is True:
            self._reset_run_command_failures()
            return None

        failure_class = result.get("failure_class")
        if result.get("ok") is not False or not isinstance(failure_class, str):
            # Denials, schema failures, and other pre-execution errors are not
            # process failures and must remain in their existing recovery path.
            return None

        guidance = self._command_guidance(failure_class)
        if guidance is None:
            # A launched process reporting a project failure is not evidence of
            # shell trouble. Cancellation is likewise not a recovery pattern.
            self._reset_run_command_failures()
            return None

        if failure_class == self._run_command_failure_class:
            self._run_command_failure_count += 1
        else:
            # Do not combine distinct structured causes into an inferred one.
            self._reset_run_command_failures()
            self._run_command_failure_class = failure_class
            self._run_command_failure_count = 1
        platform = result.get("platform")
        shell = result.get("shell")
        if isinstance(platform, str) and isinstance(shell, str):
            self._run_command_environment = (platform, shell)

        if self._run_command_failure_count < self.threshold:
            return None

        details = []
        if self._run_command_environment is not None:
            platform, shell = self._run_command_environment
            details.append(f"Configured environment: {platform} / {shell}")
        details.append(f"Repeated failure class: {failure_class}")
        rendered_guidance = f"{guidance.rstrip()}\n\n" + "\n".join(details)
        # Keep command recovery advisory and one-turn-only. A later nudge needs
        # a complete fresh threshold of actual failed command executions.
        self._reset_run_command_failures()
        return HookResponse(additional_context=rendered_guidance)

    def _command_guidance(self, failure_class: str) -> str | None:
        """Select recovery text only for a cause supported by command facts."""
        if failure_class == "timeout":
            return self.run_command_timeout_guidance
        if failure_class in _SHELL_OR_LAUNCH_FAILURES:
            return self.run_command_guidance
        return None

    def _reset_run_command_failures(self) -> None:
        self._run_command_failure_count = 0
        self._run_command_failure_class = None
        self._run_command_environment = None


def _failed_action_fingerprint(
    action: dict[str, Any],
    result: dict[str, Any],
) -> tuple[str, str, str] | None:
    """Return an exact, compact fingerprint only for categorized failures."""
    if result.get("ok") is not False:
        return None

    tool_name = action.get("tool")
    error = result.get("error")
    if not isinstance(tool_name, str) or not tool_name:
        return None
    if not isinstance(error, (str, int, float, bool)) or str(error) == "":
        return None

    args = action.get("args", {})
    try:
        args_fingerprint = json.dumps(
            args,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except (TypeError, ValueError):
        # Tool actions are normally JSON data. An unusual non-JSON action is
        # skipped because an unstable representation could create false nudges.
        return None

    return tool_name, args_fingerprint, str(error)
