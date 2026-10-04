"""Expose current verification state and nudge post-verification wandering."""

from __future__ import annotations

from typing import Any

from .runner import HookResponse, PromptContextBlock


DEFAULT_POST_VERIFICATION_THRESHOLD = 2
CURRENT_VERIFICATION_STATE_GUIDANCE = (
    "* Latest explicit verification succeeded.\n"
    "* No successful built-in file edit has occurred since.\n"
    "* No unresolved explicit verification failure is known.\n\n"
    "If the task is complete, finalize it now."
)
POST_VERIFICATION_GUIDANCE = (
    "I’m getting impatient. The tests passed. Stop wasting time running more checks or I will have to get corporate involved. If there isn’t a specific remaining problem, finish the task now."
)
_MUTATION_TOOLS = {"apply_patch", "write_file"}
_CURRENT_STATE_CONTEXT_TITLE = "Current Verification State"
_ESCALATION_CONTEXT_TITLE = "Post-Verification Guidance"


class PostVerificationNudge:
    """Report current verification and nudge a bounded subsequent action streak."""

    def __init__(
        self,
        *,
        threshold: int = DEFAULT_POST_VERIFICATION_THRESHOLD,
        source: str = "runtime",
    ) -> None:
        if threshold < 1:
            raise ValueError("post-verification threshold must be at least 1")
        self.threshold = threshold
        self.source = source
        self._verification_current = False
        self._post_verification_action_count = 0
        self._guidance_ready = False

    def before_run(self, _ctx: Any) -> None:
        """Start each run without observations from an earlier run."""
        self._reset_for_implementation_change()

    def before_model_turn(self, _ctx: Any) -> HookResponse | None:
        """Render the final verification state left by the complete Tool batch."""
        if not self._verification_current:
            return None

        if self._guidance_ready:
            # Preserve the existing escalation cadence: another strong warning
            # requires a complete fresh threshold of post-verification activity.
            self._reset_action_count()
            title = _ESCALATION_CONTEXT_TITLE
            guidance = POST_VERIFICATION_GUIDANCE
        else:
            # This compact receipt is intentionally repeatable while current.
            # It reports known state without consuming the escalation counter.
            title = _CURRENT_STATE_CONTEXT_TITLE
            guidance = CURRENT_VERIFICATION_STATE_GUIDANCE

        return HookResponse(
            context_blocks=(
                PromptContextBlock(
                    title,
                    guidance,
                    self.source,
                ),
            )
        )

    def tool_result(self, ctx: Any) -> None:
        """Observe every result while deferring current-state guidance."""
        result = ctx.result
        tool_name = ctx.action.get("tool")

        if tool_name in _MUTATION_TOOLS and result.get("ok") is True:
            # Verification applies only to the implementation that was checked.
            self._reset_for_implementation_change()
            return None

        if result.get("verification") is True:
            self._observe_verification(result)
            return None

        if not self._verification_current or result.get("ok") is not True:
            return None

        self._record_post_verification_action()
        # RepeatedReadNudge runs first and already provides more specific
        # guidance when it masks an unchanged range. Count that action but
        # do not stack post-verification guidance on the same result.
        if result.get("already_covered") is True:
            return None
        self._mark_guidance_if_ready()
        return None

    def _observe_verification(
        self,
        result: dict[str, Any],
    ) -> None:
        """Classify one explicitly marked verification result."""
        if result.get("ok") is not True:
            self._verification_current = False
            self._reset_action_count()
            return None

        if self._verification_current:
            # Rechecking an unchanged implementation is continued activity,
            # regardless of the verification command text.
            self._record_post_verification_action()
            self._mark_guidance_if_ready()
            return None

        self._verification_current = True
        self._reset_action_count()
        return None

    def _mark_guidance_if_ready(self) -> None:
        """Remember that current state warrants guidance at the next boundary."""
        if self._post_verification_action_count >= self.threshold:
            self._guidance_ready = True

    def _record_post_verification_action(self) -> None:
        """Record one successful action after current verification."""
        self._post_verification_action_count += 1

    def _reset_action_count(self) -> None:
        """Start a fresh post-verification action window."""
        self._post_verification_action_count = 0
        self._guidance_ready = False

    def _reset_for_implementation_change(self) -> None:
        """Invalidate observations tied to the prior implementation."""
        self._verification_current = False
        self._reset_action_count()
