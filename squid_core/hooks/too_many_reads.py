"""Advisory guidance for excessive reading without a substantive mutation."""

from __future__ import annotations

from typing import Any

from .runner import HookResponse, PromptContextBlock


DEFAULT_FIRST_NUDGE_READS = 5
DEFAULT_STRONG_NUDGE_READS = 8
DEFAULT_FIRST_NUDGE_LINES = 1_500
DEFAULT_STRONG_NUDGE_LINES = 3_000
TOO_MANY_READS_GUIDANCE = (
    "hey, dummy. do you think you have read enough yet? I think you have consumed the whole repo at least once since I asked you to make this simple patch."
)
TOO_MANY_READS_STRONG_GUIDANCE = (
    "HIGH PRIORITY: Stop. I am getting impatient. If you read one more file without making an edit, I am going to report you to management. Make the edit now."
)
_MUTATION_TOOLS = {"apply_patch", "write_file"}
_CONTEXT_TITLE = "Read Activity Guidance"


class TooManyReadsNudge:
    """Advise after successful read actions or source volume accumulates.

    Command execution and all failed Tool results are deliberately neutral. The
    counters are run-local but otherwise survive lifecycle events such as
    transcript compaction because they describe behavior, not model-visible
    source coverage.
    """

    def __init__(
        self,
        *,
        first_nudge_reads: int = DEFAULT_FIRST_NUDGE_READS,
        strong_nudge_reads: int = DEFAULT_STRONG_NUDGE_READS,
        first_nudge_lines: int = DEFAULT_FIRST_NUDGE_LINES,
        strong_nudge_lines: int = DEFAULT_STRONG_NUDGE_LINES,
        source: str = "runtime",
    ) -> None:
        if first_nudge_reads < 1:
            raise ValueError("first read-nudge threshold must be at least 1")
        if strong_nudge_reads <= first_nudge_reads:
            raise ValueError(
                "strong read-nudge threshold must exceed the first threshold"
            )
        if first_nudge_lines < 1:
            raise ValueError("first read-volume threshold must be at least 1")
        if strong_nudge_lines <= first_nudge_lines:
            raise ValueError(
                "strong read-volume threshold must exceed the first threshold"
            )

        self.first_nudge_reads = first_nudge_reads
        self.strong_nudge_reads = strong_nudge_reads
        self.first_nudge_lines = first_nudge_lines
        self.strong_nudge_lines = strong_nudge_lines
        self.source = source
        self._read_count = 0
        self._returned_lines = 0
        self._first_nudge_emitted = False
        self._guidance_ready = False

    def before_run(self, _ctx: Any) -> None:
        """Start each run with no behavioral history from an earlier run."""
        self._read_count = 0
        self._returned_lines = 0
        self._first_nudge_emitted = False
        self._guidance_ready = False

    def before_model_turn(self, _ctx: Any) -> HookResponse | None:
        """Render a warning only after every sibling Tool has updated state."""
        if not self._guidance_ready:
            return None

        # The warning is one-turn context. A later successful read can arm the
        # strong warning again, matching the prior per-result cadence.
        self._guidance_ready = False
        guidance = self._current_guidance()
        if guidance is None:
            return None
        self._first_nudge_emitted = True
        return HookResponse(
            context_blocks=(
                PromptContextBlock(_CONTEXT_TITLE, guidance, self.source),
            )
        )

    def tool_result(self, ctx: Any) -> HookResponse | None:
        """Observe successful built-in reads and substantive mutations."""
        tool_name = ctx.action.get("tool")
        if ctx.result.get("ok") is not True:
            return None

        if tool_name in _MUTATION_TOOLS:
            self._read_count = 0
            self._returned_lines = 0
            self._first_nudge_emitted = False
            # A later sibling mutation makes any earlier read warning false.
            self._guidance_ready = False
            return None
        if tool_name != "read_file":
            return None

        self._read_count += 1
        self._returned_lines += _returned_source_lines(ctx.result)
        if (
            self._read_count >= self.strong_nudge_reads
            or self._returned_lines >= self.strong_nudge_lines
        ):
            self._guidance_ready = True
            return None
        if not self._first_nudge_emitted and (
            self._read_count >= self.first_nudge_reads
            or self._returned_lines >= self.first_nudge_lines
        ):
            self._guidance_ready = True
        return None

    def _current_guidance(self) -> str | None:
        """Select guidance from counters that are current at model handoff."""
        if (
            self._read_count >= self.strong_nudge_reads
            or self._returned_lines >= self.strong_nudge_lines
        ):
            return TOO_MANY_READS_STRONG_GUIDANCE
        if not self._first_nudge_emitted and (
            self._read_count >= self.first_nudge_reads
            or self._returned_lines >= self.first_nudge_lines
        ):
            return TOO_MANY_READS_GUIDANCE
        return None


def _returned_source_lines(result: dict[str, Any]) -> int:
    """Measure source left visible by earlier Hooks without tracking coverage here."""
    if result.get("already_covered") is True:
        return 0

    ranges = result.get("returned_line_ranges")
    if isinstance(ranges, list):
        total = 0
        for item in ranges:
            if not isinstance(item, dict):
                return 0
            start = item.get("start_line")
            end = item.get("end_line")
            if not _valid_line_range(start, end):
                return 0
            total += end - start + 1
        return total

    start = result.get("start_line")
    end = result.get("end_line")
    return end - start + 1 if _valid_line_range(start, end) else 0


def _valid_line_range(start: Any, end: Any) -> bool:
    """Accept only ordinary positive inclusive line bounds."""
    return (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and start >= 1
        and end >= start
    )
