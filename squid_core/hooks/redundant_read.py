"""Detect repeated reads and omit unchanged source already visible to the model."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Sequence

from .runner import HookResponse


DEFAULT_REPEATED_READ_THRESHOLD = 1

_MAX_TRACKED_FILES = 100
_MAX_RANGES_PER_FILE = 64
_MAX_TRACKED_READ_RESULTS = _MAX_TRACKED_FILES * _MAX_RANGES_PER_FILE
_MUTATION_TOOLS = {"apply_patch", "write_file"}
_GUIDANCE = (
    "You already read this unchanged source and it is still available in context. "
    "The already read content is being omitted rather than returned again. "
    "Check the info you already have in your history. If the next edit is already clear, make it now. "
)
_ESCALATED_ALREADY_COVERED_MESSAGE = (
    "HIGH PRIORITY: Stop rereading this source. The requested unchanged content "
    "is already available in context and will not be returned again. "
    "Use the existing context and take the next non-read action now."
)


class RepeatedReadNudge:
    """Escalate repeated overlapping reads of one unchanged file range.

    Coverage comes from successful ``read_file`` source results and is reduced
    when request rendering trims that source. Lightweight file fingerprints
    keep that coverage truthful when a mutation bypasses the built-in tools;
    successful built-in mutations still adjust or invalidate it immediately.
    """

    def __init__(
        self,
        *,
        threshold: int = DEFAULT_REPEATED_READ_THRESHOLD,
        source: str = "runtime",
    ) -> None:
        if threshold < 1:
            raise ValueError("repeated-read threshold must be at least 1")

        self.threshold = threshold
        # Hosts inspect Hook sources as configuration metadata.
        self.source = source
        self._coverage: dict[str, list[tuple[int, int]]] = {}
        # Metadata is intentionally cheap: reread suppression needs change
        # detection, not a repository-wide content identity system.
        self._file_versions: dict[str, tuple[int, int] | None] = {}
        # Tool-call provenance lets request rendering retract one trimmed read
        # without discarding overlapping source still visible in another result.
        self._read_result_coverage: dict[
            str, tuple[str, list[tuple[int, int, int, int]]]
        ] = {}
        # Patch-invalidated spans need an explicit marker: trimming them from
        # coverage is not enough when a wider verification read still overlaps
        # unchanged coverage above the substantial-overlap threshold.
        self._stale_ranges: dict[str, list[tuple[int, int]]] = {}
        self._redundant_counts: dict[str, int] = {}

    def before_run(self, _ctx) -> None:
        """Discard all observations from the preceding run."""
        self._reset_state()

    def compaction_applied(self, _ctx) -> None:
        """Forget reads removed from the model-visible transcript."""
        self._reset_state()

    def _reset_state(self) -> None:
        """Clear all coverage and escalation state as one lifecycle unit."""
        self._coverage.clear()
        self._file_versions.clear()
        self._read_result_coverage.clear()
        self._stale_ranges.clear()
        self._redundant_counts.clear()

    def read_result_trimmed(self, ctx) -> None:
        """Replace one result's coverage with only its still-visible source."""
        previous = self._read_result_coverage.get(ctx.tool_call_id)
        if previous is None:
            return
        path, segments = previous
        retained_segments = _retain_original_segments(
            segments, ctx.retained_line_ranges
        )
        if retained_segments:
            self._read_result_coverage[ctx.tool_call_id] = (
                path,
                retained_segments,
            )
        else:
            del self._read_result_coverage[ctx.tool_call_id]
        self._rebuild_coverage_for_path(path)
        self._redundant_counts.pop(path, None)

    def tool_result(self, ctx) -> HookResponse | None:
        """Observe one final tool result and optionally emit an advisory nudge."""
        tool_name = ctx.action.get("tool")
        result = ctx.result

        if tool_name == "read_file":
            return self._observe_read(ctx.action, result)

        if tool_name in _MUTATION_TOOLS and result.get("ok") is True:
            path = _result_path(ctx.action, result)
            if path is not None:
                self._redundant_counts.pop(path, None)
                if tool_name == "apply_patch":
                    self._adjust_coverage_for_patch(path, result)
                    if path in self._file_versions:
                        # Preserve the patch's existing range-aware invalidation
                        # while associating surviving coverage with the new file.
                        self._file_versions[path] = _file_version(path)
                else:
                    self._invalidate_file_state(path)

        return None

    def _observe_read(
        self,
        action: dict[str, Any],
        result: dict[str, Any],
    ) -> HookResponse | None:
        if result.get("ok") is not True or not isinstance(
            result.get("content"), str
        ):
            return None

        path = _result_path(action, result)
        returned_ranges = _returned_ranges(result)
        if path is None or not returned_ranges:
            return None

        current_version = _file_version(path)
        recorded_version = self._file_versions.get(path)
        if path in self._file_versions and recorded_version != current_version:
            # Commands, users, and other processes can mutate files without a
            # structured Tool event. Never use coverage from the old contents.
            self._invalidate_file_state(path)

        # Stale lines are not unchanged coverage even though older results may
        # still contain their pre-patch text. A verification read must return
        # those lines again while unchanged portions can still be omitted.
        unchanged_coverage = _subtract_ranges(
            self._coverage.get(path, ()),
            self._stale_ranges.get(path, ()),
        )
        novel_ranges = _subtract_ranges(returned_ranges, unchanged_coverage)
        covered_ranges = _subtract_ranges(returned_ranges, novel_ranges)

        if not covered_ranges:
            self._record_read_coverage(path, action, returned_ranges)
            self._file_versions[path] = current_version
            self._mark_stale_ranges_fresh(path, returned_ranges)
            return None

        # Filtering follows everything the Tool returned, but a behavioral
        # strike follows the model's request. Automatically added context can
        # overlap a prior read without the model asking to reread any source.
        args = action.get("args", {})
        requested_ranges = (
            _returned_ranges({"returned_line_ranges": args.get("ranges")})
            if isinstance(args, dict) and args.get("ranges") is not None
            else []
        )
        # Full-file requests (or missing usable request metadata) retain the
        # existing returned-range judgment; never infer intent from shell text.
        judgment_ranges = requested_ranges or returned_ranges
        requested_overlap = any(
            start <= covered_end and covered_start <= end
            for start, end in judgment_ranges
            for covered_start, covered_end in covered_ranges
        )

        if novel_ranges:
            shaped_result = _partially_redundant_result(
                result,
                returned_ranges=returned_ranges,
                covered_ranges=covered_ranges,
                novel_ranges=novel_ranges,
            )
            # When content cannot be mapped exactly, preserving the original is
            # safer than attaching incorrect line labels. Its complete visible
            # range then needs provenance for later trim notifications.
            visible_ranges = novel_ranges
            if shaped_result is None:
                shaped_result = result
                visible_ranges = returned_ranges
            self._record_read_coverage(path, action, visible_ranges)
            self._file_versions[path] = current_version
            self._mark_stale_ranges_fresh(path, returned_ranges)

            count = self._record_redundant_strike(path) if requested_overlap else 0
            return HookResponse(
                updated_tool_output=shaped_result,
                additional_context=_GUIDANCE if count == self.threshold else "",
            )

        if not requested_overlap:
            return HookResponse(
                updated_tool_output=_already_covered_result(
                    action, result, message=_current_result_message(action, result)
                )
            )

        count = self._record_redundant_strike(path)
        if count < self.threshold:
            return None

        if count == self.threshold:
            # Keep the first correction useful without rewarding the redundant
            # read with another copy of source that is already in context.
            return HookResponse(
                updated_tool_output=_already_covered_result(
                    action,
                    result,
                    message=_current_result_message(action, result),
                ),
                additional_context=_GUIDANCE,
            )

        # Keep the escalated strike until coverage is invalidated. A useful read
        # can add evidence for this file, but it does not undo earlier rereads.
        # Saturating the count avoids unbounded growth once escalation is active.
        return HookResponse(
            updated_tool_output=_already_covered_result(
                action,
                result,
                message=_ESCALATED_ALREADY_COVERED_MESSAGE,
            )
        )

    def _record_redundant_strike(self, path: str) -> int:
        """Advance one file's persistent redundant-read escalation state."""
        count = min(self._redundant_counts.get(path, 0) + 1, self.threshold + 1)
        self._redundant_counts[path] = count
        return count

    def _record_read_coverage(
        self,
        path: str,
        action: dict[str, Any],
        visible_ranges: list[tuple[int, int]],
    ) -> None:
        """Record only source ranges actually exposed by this hook result."""
        self._record_coverage(path, visible_ranges)
        tool_call_id = action.get("tool_call_id")
        if not isinstance(tool_call_id, str):
            return
        if len(self._read_result_coverage) >= _MAX_TRACKED_READ_RESULTS:
            oldest_call_id = next(iter(self._read_result_coverage))
            oldest_path, _segments = self._read_result_coverage.pop(oldest_call_id)
            self._rebuild_coverage_for_path(oldest_path)
        self._read_result_coverage[tool_call_id] = (
            path,
            [(start, end, start, end) for start, end in visible_ranges],
        )

    def _adjust_coverage_for_patch(
        self,
        path: str,
        result: dict[str, Any],
    ) -> None:
        """Discard changed lines while preserving and shifting known coverage."""
        patch_range = _patch_range(result)
        if patch_range is None:
            # A successful mutation without trustworthy bounds cannot preserve
            # coverage safely, so fall back to whole-file invalidation.
            self._coverage.pop(path, None)
            self._stale_ranges.pop(path, None)
            self._discard_result_coverage(path)
            return

        existing = self._coverage.get(path)
        start_line, end_line_before, end_line_after = patch_range
        delta = end_line_after - end_line_before
        old_patch_span = [(start_line, end_line_before)]
        invalidates_current_context = bool(
            existing and _ranges_intersect(existing, old_patch_span)
        ) or _ranges_intersect(self._stale_ranges.get(path, ()), old_patch_span)
        self._shift_stale_ranges_for_patch(
            path, start_line, end_line_before, end_line_after
        )
        if invalidates_current_context:
            stale = self._stale_ranges.get(path, [])
            self._stale_ranges[path] = _merge_ranges(
                stale + [(start_line, end_line_after)]
            )[-_MAX_RANGES_PER_FILE:]

        if not existing:
            self._adjust_result_coverage_for_patch(
                path, start_line, end_line_before, delta
            )
            return

        adjusted = _adjust_ranges_for_patch(
            existing, start_line, end_line_before, delta
        )

        if adjusted:
            self._coverage[path] = _merge_ranges(adjusted)[-_MAX_RANGES_PER_FILE:]
        else:
            self._coverage.pop(path, None)
        self._adjust_result_coverage_for_patch(
            path, start_line, end_line_before, delta
        )

    def _shift_stale_ranges_for_patch(
        self,
        path: str,
        start_line: int,
        end_line_before: int,
        end_line_after: int,
    ) -> None:
        """Keep unread stale spans aligned across successive patches."""
        existing = self._stale_ranges.get(path)
        if not existing:
            return

        delta = end_line_after - end_line_before
        adjusted: list[tuple[int, int]] = []
        for stale_start, stale_end in existing:
            if stale_start < start_line:
                adjusted.append((stale_start, min(stale_end, start_line - 1)))
            if stale_end > end_line_before:
                adjusted.append(
                    (
                        max(stale_start, end_line_before + 1) + delta,
                        stale_end + delta,
                    )
                )

        if adjusted:
            self._stale_ranges[path] = _merge_ranges(adjusted)[
                -_MAX_RANGES_PER_FILE:
            ]
        else:
            self._stale_ranges.pop(path, None)

    def _mark_stale_ranges_fresh(
        self,
        path: str,
        returned_ranges: list[tuple[int, int]],
    ) -> None:
        """Remove only successfully reread lines from mutation-stale spans."""
        stale = self._stale_ranges.get(path)
        if not stale:
            return

        remaining = _subtract_ranges(stale, returned_ranges)
        if remaining:
            self._stale_ranges[path] = remaining[-_MAX_RANGES_PER_FILE:]
        else:
            self._stale_ranges.pop(path, None)

    def _record_coverage(
        self,
        path: str,
        returned_ranges: list[tuple[int, int]],
    ) -> None:
        # Reinsertion keeps the dict ordered by recent use for simple eviction.
        existing = self._coverage.pop(path, [])
        if not existing and len(self._coverage) >= _MAX_TRACKED_FILES:
            oldest_path = next(iter(self._coverage))
            del self._coverage[oldest_path]
            self._stale_ranges.pop(oldest_path, None)
            self._redundant_counts.pop(oldest_path, None)
            self._file_versions.pop(oldest_path, None)
            self._discard_result_coverage(oldest_path)
        self._coverage[path] = _merge_ranges(existing + returned_ranges)[
            -_MAX_RANGES_PER_FILE:
        ]

    def _rebuild_coverage_for_path(self, path: str) -> None:
        ranges = []
        for result_path, segments in self._read_result_coverage.values():
            if result_path == path:
                ranges.extend(
                    (current_start, current_end)
                    for (
                        _original_start,
                        _original_end,
                        current_start,
                        current_end,
                    ) in segments
                )
        if ranges:
            self._coverage[path] = _merge_ranges(ranges)[-_MAX_RANGES_PER_FILE:]
        else:
            self._coverage.pop(path, None)
            self._file_versions.pop(path, None)

    def _discard_result_coverage(self, path: str) -> None:
        for call_id, (result_path, _ranges) in list(
            self._read_result_coverage.items()
        ):
            if result_path == path:
                del self._read_result_coverage[call_id]

    def _invalidate_file_state(self, path: str) -> None:
        """Discard every observation whose truth depends on one file version."""
        self._coverage.pop(path, None)
        self._file_versions.pop(path, None)
        self._stale_ranges.pop(path, None)
        self._redundant_counts.pop(path, None)
        self._discard_result_coverage(path)

    def _adjust_result_coverage_for_patch(
        self,
        path: str,
        start_line: int,
        end_line_before: int,
        delta: int,
    ) -> None:
        """Apply the same mutation transform to per-result provenance."""
        for call_id, (result_path, segments) in list(
            self._read_result_coverage.items()
        ):
            if result_path != path:
                continue
            adjusted = _adjust_segments_for_patch(
                segments, start_line, end_line_before, delta
            )
            if adjusted:
                self._read_result_coverage[call_id] = (result_path, adjusted)
            else:
                del self._read_result_coverage[call_id]


def _result_path(action: dict[str, Any], result: dict[str, Any]) -> str | None:
    """Return a normalized absolute identity from result or action metadata."""
    path = result.get("path")
    if not isinstance(path, str) or not path:
        args = action.get("args")
        path = args.get("path") if isinstance(args, dict) else None
    if not isinstance(path, str) or not path:
        return None
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _file_version(path: str) -> tuple[int, int] | None:
    """Return cheap metadata that changes for ordinary file replacements."""
    try:
        stat = os.stat(path)
    except OSError:
        # Absence is still a version: creating or restoring the file later will
        # differ and invalidate any synthetic/test coverage recorded for it.
        return None
    return stat.st_mtime_ns, stat.st_size


def _already_covered_result(
    action: dict[str, Any],
    result: dict[str, Any],
    *,
    message: str,
) -> dict[str, Any]:
    """Build a successful model-facing result without duplicate file content."""
    masked = {
        "ok": True,
        "tool": "read_file",
        "path": result.get("path"),
        "already_covered": True,
        "message": message,
    }

    args = action.get("args")
    if isinstance(args, dict):
        if masked["path"] is None:
            masked["path"] = args.get("path")
        # Preserve the caller's uniform targeted-read shape when duplicate
        # content is suppressed; returned_line_ranges still drives coverage.
        if isinstance(args.get("ranges"), list):
            masked["requested_line_ranges"] = args["ranges"]

    for key in ("start_line", "end_line", "returned_line_ranges"):
        if key in result:
            masked[key] = result[key]
    return masked


def _partially_redundant_result(
    result: dict[str, Any],
    *,
    returned_ranges: list[tuple[int, int]],
    covered_ranges: list[tuple[int, int]],
    novel_ranges: list[tuple[int, int]],
) -> dict[str, Any] | None:
    """Return a copy containing only novel lines, or ``None`` if mapping is unsafe."""
    content = result["content"]
    if returned_ranges != sorted(returned_ranges) or any(
        current_start <= previous_end
        for (_previous_start, previous_end), (current_start, _current_end) in zip(
            returned_ranges, returned_ranges[1:]
        )
    ):
        return None
    returned_segments = _read_content_segments(content, returned_ranges)
    if returned_segments is None:
        return None

    kept_regions: list[tuple[int, int, list[str]]] = []
    for (returned_start, returned_end), segment_lines in zip(
        returned_ranges,
        returned_segments,
    ):
        for novel_start, novel_end in novel_ranges:
            kept_start = max(returned_start, novel_start)
            kept_end = min(returned_end, novel_end)
            if kept_start <= kept_end:
                segment_start = kept_start - returned_start
                segment_end = kept_end - returned_start + 1
                kept_regions.append(
                    (
                        kept_start,
                        kept_end,
                        segment_lines[segment_start:segment_end],
                    )
                )

    shaped = dict(result)
    shaped["content"] = _render_read_regions(kept_regions)
    shaped["start_line"] = novel_ranges[0][0]
    shaped["end_line"] = novel_ranges[-1][1]
    shaped["returned_line_ranges"] = _range_dicts(novel_ranges)
    shaped["content_returned_chars"] = len(shaped["content"])
    shaped["partially_redundant"] = True
    shaped["omitted_line_ranges"] = _range_dicts(covered_ranges)
    shaped["message"] = (
        f"Lines {_format_ranges(covered_ranges)} were already available unchanged "
        f"and were omitted. Returning new content from lines "
        f"{_format_ranges(novel_ranges)}."
    )
    if shaped.get("content_truncated") is True:
        shaped["truncation_guidance"] = (
            "The underlying full-file read was truncated; after omitting unchanged "
            f"coverage, this result returns lines {_format_ranges(novel_ranges)}."
        )
    return shaped


def _read_content_segments(
    content: str,
    returned_ranges: list[tuple[int, int]],
) -> list[list[str]] | None:
    """Map rendered read content back to source segments, skipping gap markers."""
    lines = content.splitlines(keepends=True)
    source_line_count = sum(end - start + 1 for start, end in returned_ranges)
    if len(lines) == source_line_count:
        # Preserve compatibility with caller-defined read tools that provide
        # disjoint metadata but have not adopted Code Squid's marker rendering.
        offset = 0
        segments = []
        for start, end in returned_ranges:
            length = end - start + 1
            segments.append(lines[offset : offset + length])
            offset += length
        return segments

    offset = 0
    segments = []
    previous_end = None
    for start, end in returned_ranges:
        if previous_end is not None and start > previous_end + 1:
            if offset >= len(lines) or lines[offset].rstrip("\r\n") != (
                _omitted_lines_marker(previous_end + 1, start - 1).rstrip("\n")
            ):
                return None
            offset += 1
        length = end - start + 1
        segment = lines[offset : offset + length]
        if len(segment) != length:
            return None
        segments.append(segment)
        offset += length
        previous_end = end
    return segments if offset == len(lines) else None


def _render_read_regions(regions: list[tuple[int, int, list[str]]]) -> str:
    """Keep transformed disjoint source regions visibly discontinuous."""
    parts: list[str] = []
    previous_end = None
    for start, end, lines in regions:
        if previous_end is not None and start > previous_end + 1:
            parts.append(_omitted_lines_marker(previous_end + 1, start - 1))
        parts.extend(lines)
        previous_end = end
    return "".join(parts)


def _omitted_lines_marker(start: int, end: int) -> str:
    return f"<<< CODE SQUID: lines {start}-{end} omitted from this read >>>\n"


def _range_dicts(ranges: Sequence[tuple[int, int]]) -> list[dict[str, int]]:
    """Render internal inclusive ranges in read_file result metadata form."""
    return [{"start_line": start, "end_line": end} for start, end in ranges]


def _format_ranges(ranges: Sequence[tuple[int, int]]) -> str:
    """Format inclusive source ranges compactly for model-facing messages."""
    return ", ".join(
        str(start) if start == end else f"{start}-{end}"
        for start, end in ranges
    )


def _current_result_message(
    action: dict[str, Any], result: dict[str, Any]
) -> str:
    """Describe the still-current prior result without repeating its contents."""
    path = result.get("path")
    if not isinstance(path, str) or not path:
        args = action.get("args")
        path = args.get("path") if isinstance(args, dict) else None

    ranges = _returned_ranges(result)
    range_text = ", ".join(
        str(start) if start == end else f"{start}-{end}"
        for start, end in ranges
    )
    return (
        f"Already read unchanged content: {path} lines {range_text}. "
        "The previous result is still current."
    )


def _returned_ranges(result: dict[str, Any]) -> list[tuple[int, int]]:
    """Extract actual returned coverage, preferring the dedicated range list."""
    if "returned_line_ranges" in result:
        raw_ranges = result.get("returned_line_ranges")
        if not isinstance(raw_ranges, list):
            return []
        ranges = []
        for item in raw_ranges:
            if not isinstance(item, dict):
                return []
            line_range = _valid_range(item.get("start_line"), item.get("end_line"))
            if line_range is None:
                return []
            ranges.append(line_range)
        return ranges

    fallback = _valid_range(result.get("start_line"), result.get("end_line"))
    return [fallback] if fallback is not None else []


def _patch_range(result: dict[str, Any]) -> tuple[int, int, int] | None:
    """Extract inclusive before/after line bounds from a successful patch."""
    start = result.get("start_line")
    end_before = result.get("end_line_before")
    end_after = result.get("end_line_after")
    if (
        not _is_line_number(start)
        or not _is_line_number(end_before)
        or not _is_line_number(end_after)
        or end_before < start
        or end_after < start
    ):
        return None
    return start, end_before, end_after


def _valid_range(start: Any, end: Any) -> tuple[int, int] | None:
    if not _is_line_number(start) or not _is_line_number(end) or end < start:
        return None
    return start, end


def _is_line_number(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _ranges_intersect(
    first: Sequence[tuple[int, int]],
    second: Sequence[tuple[int, int]],
) -> bool:
    """Return whether two inclusive range collections share any line."""
    for first_start, first_end in first:
        for second_start, second_end in second:
            if max(first_start, second_start) <= min(first_end, second_end):
                return True
    return False


def _subtract_ranges(
    ranges: Sequence[tuple[int, int]],
    removed: Sequence[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Subtract inclusive ranges while retaining any fragments on either side."""
    remaining = _merge_ranges(ranges)
    for removed_start, removed_end in _merge_ranges(removed):
        fragments: list[tuple[int, int]] = []
        for start, end in remaining:
            if end < removed_start or start > removed_end:
                fragments.append((start, end))
                continue
            if start < removed_start:
                fragments.append((start, removed_start - 1))
            if end > removed_end:
                fragments.append((removed_end + 1, end))
        remaining = fragments
    return remaining


def _adjust_ranges_for_patch(
    ranges: Sequence[tuple[int, int]],
    start_line: int,
    end_line_before: int,
    delta: int,
) -> list[tuple[int, int]]:
    """Discard a changed span and shift source ranges following the patch."""
    adjusted: list[tuple[int, int]] = []
    for range_start, range_end in ranges:
        if range_start < start_line:
            adjusted.append((range_start, min(range_end, start_line - 1)))
        if range_end > end_line_before:
            adjusted.append(
                (
                    max(range_start, end_line_before + 1) + delta,
                    range_end + delta,
                )
            )
    return _merge_ranges(adjusted)[-_MAX_RANGES_PER_FILE:]


def _retain_original_segments(
    segments: Sequence[tuple[int, int, int, int]],
    retained: Sequence[tuple[int, int]],
) -> list[tuple[int, int, int, int]]:
    """Keep current-coordinate portions whose original source remains visible."""
    kept = []
    for original_start, original_end, current_start, _current_end in segments:
        for retained_start, retained_end in retained:
            overlap_start = max(original_start, retained_start)
            overlap_end = min(original_end, retained_end)
            if overlap_start <= overlap_end:
                kept.append(
                    (
                        overlap_start,
                        overlap_end,
                        current_start + overlap_start - original_start,
                        current_start + overlap_end - original_start,
                    )
                )
    return kept


def _adjust_segments_for_patch(
    segments: Sequence[tuple[int, int, int, int]],
    start_line: int,
    end_line_before: int,
    delta: int,
) -> list[tuple[int, int, int, int]]:
    """Preserve original-to-current line mappings around a successful patch."""
    adjusted = []
    for original_start, original_end, current_start, current_end in segments:
        if current_start < start_line:
            kept_current_end = min(current_end, start_line - 1)
            adjusted.append(
                (
                    original_start,
                    original_start + kept_current_end - current_start,
                    current_start,
                    kept_current_end,
                )
            )
        if current_end > end_line_before:
            kept_current_start = max(current_start, end_line_before + 1)
            kept_original_start = (
                original_start + kept_current_start - current_start
            )
            adjusted.append(
                (
                    kept_original_start,
                    original_end,
                    kept_current_start + delta,
                    current_end + delta,
                )
            )
    return adjusted


def _merge_ranges(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge inclusive ranges so repeated coverage is never double-counted."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
            continue
        previous_start, previous_end = merged[-1]
        merged[-1] = (previous_start, max(previous_end, end))
    return merged
