"""Read UTF-8 text files with Code Squid's established range semantics."""

from typing import Sequence

from squid_core.settings import ReadFileSettings

from .context import FileReadContext


TOOL_NAME = "read_file"


def read_text(context: FileReadContext) -> dict:
    """Read an ordinary UTF-8 file and preserve the public read result shape."""

    content = context.file_path.read_text(encoding="utf-8")
    return read_text_content(context, content)


def read_text_content(context: FileReadContext, content: str) -> dict:
    """Apply established line targeting and truncation to normalized text."""

    lines = content.splitlines(keepends=True)
    total_lines = len(lines)

    if context.ranges is not None:
        return _read_ranges(
            context.path,
            lines,
            total_lines,
            context.ranges,
            context.settings,
        )

    actual_start = 1
    actual_end = total_lines
    original_chars = len(content)
    if total_lines <= context.settings.default_max_lines:
        content_metadata = {
            "truncated": False,
            "original_chars": original_chars,
            "returned_chars": original_chars,
        }
    else:
        # Keep both ends symmetrical so the omitted interval is easy to
        # inspect with a follow-up ranged read.
        head_lines = context.settings.default_max_lines // 2
        tail_lines = context.settings.default_max_lines - head_lines
        tail_start = total_lines - tail_lines + 1
        returned_ranges = [(1, head_lines), (tail_start, total_lines)]
        content = _render_ranges(lines, returned_ranges)
        content_metadata = {
            "truncated": True,
            "original_chars": original_chars,
            "returned_chars": len(content),
        }

    result = {
        "ok": True,
        "tool": TOOL_NAME,
        "path": context.path,
        "content": content,
        "content_truncated": content_metadata["truncated"],
        "content_original_chars": content_metadata["original_chars"],
        "content_returned_chars": content_metadata["returned_chars"],
        "start_line": actual_start,
        "end_line": actual_end,
        "total_lines": total_lines,
        "ranged": False,
    }
    if content_metadata["truncated"]:
        result["returned_line_ranges"] = _range_dicts(returned_ranges)
        result["truncation_guidance"] = (
            f"Full-file read truncated to {context.settings.default_max_lines} of "
            f"{total_lines} lines; returned lines 1-{head_lines} and "
            f"{tail_start}-{total_lines}. Use ranges to inspect the missing "
            "relevant section."
        )
    return result


def _read_ranges(
    path: str,
    lines: list[str],
    total_lines: int,
    ranges: list[dict[str, int]],
    settings: ReadFileSettings,
) -> dict:
    """Return normalized targeted regions through the public range-list contract."""

    normalized = []
    for requested in ranges:
        start_line = requested["start_line"]
        end_line = min(requested["end_line"], total_lines)
        if start_line > total_lines:
            continue
        normalized.append(
            _expanded_range(
                start_line,
                end_line,
                total_lines,
                settings.minimum_range_lines,
            )
        )

    merged = _merge_ranges(normalized)
    source_lines = sum(end - start + 1 for start, end in merged)
    if source_lines > settings.default_max_lines:
        return {
            "ok": False,
            "tool": TOOL_NAME,
            "path": path,
            "error": (
                "targeted read covers "
                f"{source_lines} lines after normalization; maximum total coverage is "
                f"{settings.default_max_lines} lines"
            ),
        }

    content = _render_ranges(lines, merged)
    if merged:
        actual_start = merged[0][0]
        actual_end = merged[-1][1]
    else:
        # An empty interval begins at the earliest requested line and ends
        # immediately before it, preserving useful location metadata.
        actual_start = min(item["start_line"] for item in ranges)
        actual_end = actual_start - 1

    return {
        "ok": True,
        "tool": TOOL_NAME,
        "path": path,
        "content": content,
        "content_truncated": False,
        "content_original_chars": len(content),
        "content_returned_chars": len(content),
        "start_line": actual_start,
        "end_line": actual_end,
        "returned_line_ranges": _range_dicts(merged),
        "total_lines": total_lines,
        "ranged": True,
    }


def _expanded_range(
    start_line: int,
    end_line: int,
    total_lines: int,
    minimum_lines: int,
) -> tuple[int, int]:
    """Expand a valid requested region around its center, shifting at file edges."""

    target_lines = min(minimum_lines, total_lines)
    returned_lines = end_line - start_line + 1
    if returned_lines >= target_lines:
        return start_line, end_line

    missing_lines = target_lines - returned_lines
    expanded_start = max(1, start_line - missing_lines // 2)
    expanded_end = min(
        total_lines,
        end_line + (missing_lines - missing_lines // 2),
    )

    # If one file edge constrained the balanced window, use the available room
    # on the other side so small requests still receive the configured context.
    remaining = target_lines - (expanded_end - expanded_start + 1)
    if remaining and expanded_start == 1:
        expanded_end = min(total_lines, expanded_end + remaining)
    elif remaining:
        expanded_start = max(1, expanded_start - remaining)
    return expanded_start, expanded_end


def _merge_ranges(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge inclusive overlapping or adjacent ranges in source order."""

    merged: list[tuple[int, int]] = []
    for start_line, end_line in sorted(ranges):
        if merged and start_line <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end_line))
        else:
            merged.append((start_line, end_line))
    return merged


def _range_dicts(ranges: Sequence[tuple[int, int]]) -> list[dict[str, int]]:
    return [{"start_line": start, "end_line": end} for start, end in ranges]


def _render_ranges(
    lines: list[str],
    ranges: Sequence[tuple[int, int]],
) -> str:
    """Render source regions with unmistakable markers at every line gap."""

    parts: list[str] = []
    previous_end = None
    for start_line, end_line in ranges:
        if previous_end is not None and start_line > previous_end + 1:
            parts.append(
                f"<<< CODE SQUID: lines {previous_end + 1}-{start_line - 1} "
                "omitted from this read >>>\n"
            )
        parts.extend(lines[start_line - 1 : end_line])
        previous_end = end_line
    return "".join(parts)
