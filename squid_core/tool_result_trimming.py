"""Render canonical tool results into a compact, model-visible history view.

Canonical transcript messages remain JSON so Hooks, storage, and replay retain
the complete machine-readable result.  This module owns the request-only seam
where those dictionaries are trimmed and then presented to the model as text.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Callable

from squid_core.hooks.runner import ReadResultTrimmedContext
from squid_core.image_content import (
    image_descriptor,
    image_marker,
    image_reference_block,
)
from squid_core.settings import ToolResultTrimmingSettings


_OMITTED_MARKERS = {
    "read_file": "[older read content omitted; reread if needed]",
    "run_command": "[older command output omitted; rerun if needed]",
    "apply_patch": "[older patch diagnostic omitted]",
    "write_file": "[older written content omitted; reread if needed]",
}


def render_messages_for_model(
    messages: list[dict],
    settings: ToolResultTrimmingSettings,
    *,
    on_read_result_trimmed: Callable[[ReadResultTrimmedContext], None] | None = None,
) -> list[dict]:
    """Return a request-only history view without mutating canonical messages."""
    tool_indexes = [
        index for index, message in enumerate(messages) if message.get("role") == "tool"
    ]
    # The latest completed assistant batch has not yet been visible to the model.
    # Protect the whole batch rather than letting later siblings make its early
    # results look historical. Existing Tool-level bounds still define the
    # ordinary rendered form; this only postpones age-based reduction once.
    first_exposure_indexes = _latest_completed_tool_batch_indexes(messages)
    old_count = (
        max(0, len(tool_indexes) - settings.recent_full_results)
        if settings.enabled
        else 0
    )
    old_indexes = set(tool_indexes[:old_count]) - first_exposure_indexes

    tool_names = _tool_names_by_call_id(messages)
    image_injections = _image_injections(messages)
    rendered = []
    for index, message in enumerate(messages):
        if message.get("role") != "tool":
            rendered.append(message)
            rendered.extend(image_injections.get(index, ()))
            continue

        rendered.append(
            _render_tool_message(
                message,
                tool_names.get(message.get("tool_call_id")),
                settings,
                on_read_result_trimmed,
                is_recent=index not in old_indexes,
            )
        )
        rendered.extend(image_injections.get(index, ()))
    return rendered


def _image_injections(messages: list[dict]) -> dict[int, list[dict]]:
    """Place image messages after every complete sibling Tool-result batch."""

    injections: dict[int, list[dict]] = {}
    for assistant_index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        calls = message.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            continue
        call_ids = [call.get("id") for call in calls if isinstance(call, dict)]
        if len(call_ids) != len(calls) or any(
            not isinstance(item, str) for item in call_ids
        ):
            continue

        results: dict[str, tuple[int, dict]] = {}
        for index in range(assistant_index + 1, len(messages)):
            candidate = messages[index]
            if candidate.get("role") == "assistant":
                break
            call_id = candidate.get("tool_call_id")
            if candidate.get("role") != "tool" or call_id not in call_ids:
                continue
            content = candidate.get("content")
            if not isinstance(content, str):
                continue
            try:
                result = json.loads(content)
            except json.JSONDecodeError:
                continue
            if isinstance(result, dict):
                results[call_id] = (index, result)

        if set(results) != set(call_ids):
            continue
        blocks = []
        for call_id in call_ids:
            result = results[call_id][1]
            reference = image_reference_block(result)
            if reference is not None:
                blocks.extend(
                    (
                        {"type": "text", "text": reference["description"]},
                        reference,
                    )
                )
        if blocks:
            final_result_index = max(index for index, _result in results.values())
            injections.setdefault(final_result_index, []).append(
                {"role": "user", "content": blocks}
            )
    return injections


def _latest_completed_tool_batch_indexes(messages: list[dict]) -> set[int]:
    """Return result indexes for the latest assistant turn when fully paired.

    A completed Tool batch at the transcript tail is awaiting its first model
    exposure. Transient user/context messages may follow its results, but any
    later assistant message proves the earlier batch was already exposed.
    """
    assistant_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].get("role") == "assistant"
        ),
        None,
    )
    if assistant_index is None:
        return set()

    raw_calls = messages[assistant_index].get("tool_calls")
    if not isinstance(raw_calls, list) or not raw_calls:
        return set()

    call_ids: list[str] = []
    for call in raw_calls:
        if not isinstance(call, dict) or not isinstance(call.get("id"), str):
            return set()
        call_ids.append(call["id"])
    if len(set(call_ids)) != len(call_ids):
        return set()

    result_indexes: dict[str, int] = {}
    for index in range(assistant_index + 1, len(messages)):
        message = messages[index]
        if message.get("role") != "tool":
            continue
        call_id = message.get("tool_call_id")
        if call_id in call_ids:
            if call_id in result_indexes:
                return set()
            result_indexes[call_id] = index

    if set(result_indexes) != set(call_ids):
        return set()
    return set(result_indexes.values())


def render_tool_result_for_model(
    canonical_result: dict,
    *,
    is_recent: bool,
    settings: ToolResultTrimmingSettings,
    tool_name: str | None = None,
) -> dict:
    """Return one full or reduced result copy for model-visible rendering."""
    if not settings.enabled or is_recent:
        return canonical_result

    name = tool_name or canonical_result.get("tool")
    if name == "read_file":
        return _trim_payload(
            canonical_result,
            "content",
            settings.read_old_content_chars,
            name,
        )
    if name == "run_command":
        return _trim_payload(
            canonical_result,
            "output",
            settings.command_old_output_chars,
            name,
        )
    if name == "apply_patch":
        if canonical_result.get("ok") is False:
            return _trim_patch_diagnostics(
                canonical_result, settings.patch_old_diagnostic_chars
            )
        rendered = dict(canonical_result)
        rendered.pop("before_preview", None)
        rendered.pop("after_preview", None)
        return rendered
    if name == "write_file":
        return _trim_payload(
            canonical_result,
            "content",
            settings.write_old_content_chars,
            name,
        )

    # Future and caller-defined tools retain all information until they opt in.
    return canonical_result


def _render_tool_message(
    message: dict,
    paired_tool_name: str | None,
    settings: ToolResultTrimmingSettings,
    on_read_result_trimmed: Callable[[ReadResultTrimmedContext], None] | None,
    *,
    is_recent: bool,
) -> dict:
    content = message.get("content")
    if not isinstance(content, str):
        return message
    try:
        result = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return message
    if not isinstance(result, dict):
        return message

    rendered_result = render_tool_result_for_model(
        result,
        tool_name=paired_tool_name,
        is_recent=is_recent,
        settings=settings,
    )

    name = paired_tool_name or result.get("tool")
    read_was_trimmed = name == "read_file" and rendered_result is not result
    retained_read_ranges = (
        _retained_read_ranges(result, settings.read_old_content_chars)
        if read_was_trimmed
        else _read_ranges(result)
    )
    if read_was_trimmed and on_read_result_trimmed is not None:
        tool_call_id = message.get("tool_call_id")
        if isinstance(tool_call_id, str):
            path = result.get("path")
            on_read_result_trimmed(
                ReadResultTrimmedContext(
                    tool_call_id=tool_call_id,
                    path=path if isinstance(path, str) else None,
                    retained_line_ranges=tuple(retained_read_ranges),
                )
            )

    content = _format_result_for_model(
        rendered_result,
        name if isinstance(name, str) else None,
        source_result=result,
        visible_read_ranges=retained_read_ranges,
        read_was_trimmed=read_was_trimmed,
    )
    if content is None:
        return message

    rendered_message = dict(message)
    rendered_message["content"] = content
    return rendered_message


def _format_result_for_model(
    result: dict,
    tool_name: str | None,
    *,
    source_result: dict,
    visible_read_ranges: list[tuple[int, int]],
    read_was_trimmed: bool,
) -> str | None:
    """Format recognized built-ins; unknown tools keep their canonical JSON."""
    if tool_name == "read_file":
        return _format_read_result(
            result,
            source_result=source_result,
            visible_ranges=visible_read_ranges,
            was_trimmed=read_was_trimmed,
        )
    if tool_name == "run_command":
        return _format_command_result(result)
    if tool_name == "apply_patch":
        return _format_patch_result(result)
    if tool_name == "write_file":
        return _format_write_result(result)
    return None


def _format_read_result(
    result: dict,
    *,
    source_result: dict,
    visible_ranges: list[tuple[int, int]],
    was_trimmed: bool,
) -> str:
    path = _text(result.get("path"), "(unknown)")
    if result.get("ok") is not True:
        return _format_simple_failure("READ_FILE", result, path=path)

    if source_result.get("media_type") == "pdf":
        return _format_pdf_result(result, source_result, path)

    image = image_descriptor(source_result)
    if image is not None:
        return "\n".join(
            (
                "READ_FILE IMAGE OK",
                f"path: {path}",
                image_marker(source_result.get("path"), image),
            )
        )

    returned_ranges = _read_ranges(source_result)
    if result.get("already_covered") is True:
        lines = ["READ_FILE SKIPPED", f"path: {path}"]
        if returned_ranges:
            lines.append(f"lines: {_format_ranges(returned_ranges)}")
        lines.append("reason: unchanged source already available in context")
        message = result.get("message")
        if isinstance(message, str) and message:
            lines.append(f"detail: {message}")
        return "\n".join(lines)

    partial = result.get("partially_redundant") is True
    status = "PARTIAL" if partial else "OK"
    lines = [f"READ_FILE {status}", f"path: {path}"]
    if result.get("content_truncated") is True or was_trimmed:
        lines.append("truncated: yes")

    # Some readers return model-ready text that is not addressable as source
    # lines. Keep that payload visible without inventing line provenance; old
    # results have already passed through the ordinary character bound above.
    if not returned_ranges:
        content = result.get("content")
        if isinstance(content, str) and content:
            lines.extend(("", "<<< CONTENT >>>", content))
        elif was_trimmed:
            lines.extend(("", "<<< CODE SQUID CONTENT OMITTED FROM OLDER RESULT >>>"))
        return "\n".join(lines).rstrip()

    source_regions = _source_regions(source_result, visible_ranges)
    omitted = _read_ranges_from_field(result, "omitted_line_ranges")
    omitted_reason = " already available unchanged" if partial else ""
    events: list[tuple[int, int, str, str | None]] = [
        (start, end, "source", text) for start, end, text in source_regions
    ]
    events.extend((start, end, "omitted", omitted_reason) for start, end in omitted)
    events.sort(key=lambda item: (item[0], item[2] != "omitted"))

    # Any discontinuity between visible regions must remain explicit. This also
    # covers built-in full-file truncation and request-only old-result trimming.
    expanded: list[tuple[int, int, str, str | None]] = []
    previous_end = None
    for event in events:
        start, end, kind, payload = event
        if previous_end is not None and start > previous_end + 1:
            reason = " older result content trimmed" if was_trimmed else ""
            expanded.append((previous_end + 1, start - 1, "omitted", reason))
        expanded.append(event)
        previous_end = max(previous_end or 0, end)

    filename = _path_name(path)
    for start, end, kind, payload in expanded:
        lines.append("")
        if kind == "source":
            lines.append(
                f"<<< CODE SQUID SOURCE: {filename} lines {start}-{end} >>>"
            )
            lines.append(payload or "")
        else:
            lines.append(
                f"<<< CODE SQUID OMITTED: {filename} lines {start}-{end}{payload or ''} >>>"
            )

    if not source_regions and was_trimmed:
        lines.extend(("", "<<< CODE SQUID SOURCE OMITTED FROM OLDER RESULT >>>"))
    return "\n".join(lines).rstrip()


def _format_pdf_result(result: dict, source_result: dict, path: str) -> str:
    """Expose PDF text and durable page-image paths without injecting images."""

    lines = ["READ_FILE PDF OK", f"path: {path}"]
    _append_scalar(lines, "total_pages", source_result.get("total_pages"))
    page_ranges = source_result.get("returned_page_ranges")
    if isinstance(page_ranges, list):
        rendered_ranges = []
        for item in page_ranges:
            if not isinstance(item, dict):
                continue
            start = item.get("start_page")
            end = item.get("end_page")
            if _valid_line_range(start, end):
                rendered_ranges.append(
                    str(start) if start == end else f"{start}-{end}"
                )
        if rendered_ranges:
            lines.append(f"pages: {', '.join(rendered_ranges)}")

    for item in source_result.get("rendered_pages") or []:
        if not isinstance(item, dict):
            continue
        page = item.get("page")
        image_path = item.get("path")
        if (
            isinstance(page, int)
            and not isinstance(page, bool)
            and page > 0
            and isinstance(image_path, str)
            and image_path
        ):
            lines.append(f"rendered_page_{page}: {image_path}")

    if result.get("model_content_truncated") is True:
        lines.append("truncated: yes")
    content = result.get("content")
    if isinstance(content, str) and content:
        lines.extend(("", "<<< PDF TEXT >>>", content))
    return "\n".join(lines).rstrip()


def _source_regions(
    result: dict, visible_ranges: list[tuple[int, int]]
) -> list[tuple[int, int, str]]:
    """Extract exact visible subranges from the original canonical payload."""
    content = result.get("content")
    original_ranges = _read_ranges(result)
    if not isinstance(content, str) or not original_ranges:
        return []
    segments = _read_content_segments(content, original_ranges)
    if segments is None:
        return []

    regions = []
    for (original_start, original_end), segment in zip(original_ranges, segments):
        for visible_start, visible_end in visible_ranges:
            start = max(original_start, visible_start)
            end = min(original_end, visible_end)
            if start > end:
                continue
            first = start - original_start
            last = end - original_start + 1
            regions.append((start, end, "".join(segment[first:last])))
    return regions


def _read_content_segments(
    content: str, ranges: list[tuple[int, int]]
) -> list[list[str]] | None:
    """Split source payloads while accepting the built-in inter-range marker."""
    lines = content.splitlines(keepends=True)
    source_line_count = sum(end - start + 1 for start, end in ranges)
    if len(lines) == source_line_count:
        segments = []
        offset = 0
        for start, end in ranges:
            length = end - start + 1
            segments.append(lines[offset : offset + length])
            offset += length
        return segments

    segments = []
    offset = 0
    previous_end = None
    for start, end in ranges:
        if previous_end is not None and start > previous_end + 1:
            expected = (
                f"<<< CODE SQUID: lines {previous_end + 1}-{start - 1} "
                "omitted from this read >>>"
            )
            if offset >= len(lines) or lines[offset].rstrip("\r\n") != expected:
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


def _format_command_result(result: dict) -> str:
    ok = result.get("ok") is True
    lines = [f"RUN_COMMAND {'OK' if ok else 'FAILED'}"]
    _append_scalar(lines, "command", result.get("command"))
    _append_scalar(lines, "exit_code", result.get("returncode"))
    if result.get("timed_out") is True:
        lines.append("timed_out: yes")
    if not ok:
        _append_scalar(lines, "reason", result.get("error"))
        _append_scalar(lines, "failure_class", result.get("failure_class"))
    if result.get("output_truncated") is True or result.get("model_output_truncated") is True:
        lines.append("truncated: yes")
    output = result.get("output")
    if isinstance(output, str) and output:
        lines.extend(("", "<<< OUTPUT >>>", output))
    return "\n".join(lines).rstrip()


def _format_patch_result(result: dict) -> str:
    ok = result.get("ok") is True
    lines = [f"APPLY_PATCH {'OK' if ok else 'FAILED'}"]
    _append_scalar(lines, "path", result.get("path"))
    if ok:
        start = result.get("start_line")
        end = result.get("end_line_after")
        if _valid_line_range(start, end):
            lines.append(f"changed: lines {start}-{end}")
        _append_scalar(lines, "match_id", result.get("match_id"))
        _append_scalar(lines, "before_preview", result.get("before_preview"))
        _append_scalar(lines, "after_preview", result.get("after_preview"))
        return "\n".join(lines)

    _append_scalar(lines, "reason", result.get("error"))
    _append_scalar(lines, "match_id", result.get("match_id"))
    _append_scalar(lines, "occurrences", result.get("occurrences"))
    _append_scalar(lines, "message", result.get("message"))
    _append_scalar(lines, "guidance", result.get("guidance"))
    candidates = result.get("candidates")
    if isinstance(candidates, list):
        diagnostic_groups = (("candidate", candidates),)
    else:
        _append_patch_diagnostic(
            lines, "closest current text", result.get("closest_match")
        )
        diagnostic_groups = (("current match", result.get("matches")),)
    for label, collection in diagnostic_groups:
        if not isinstance(collection, list):
            continue
        for index, item in enumerate(collection, start=1):
            if isinstance(item, dict):
                _append_patch_diagnostic(lines, f"{label} {index}", item)
    if result.get("model_diagnostic_truncated") is True:
        lines.extend(("", "<<< OLDER PATCH DIAGNOSTIC TRUNCATED >>>"))
    return "\n".join(lines).rstrip()


def _append_patch_diagnostic(lines: list[str], label: str, item: object) -> None:
    if not isinstance(item, dict):
        return
    lines.append("")
    heading = label.upper()
    match_id = item.get("match_id")
    if isinstance(match_id, str):
        heading += f" {match_id}"
    start = item.get("start_line")
    end = item.get("end_line")
    if _valid_line_range(start, end):
        heading += f" lines {start}-{end}"
    lines.append(f"<<< {heading} >>>")
    _append_scalar(lines, "similarity", item.get("similarity"))
    text = item.get("text")
    if not isinstance(text, str):
        text = item.get("content")
    if not isinstance(text, str):
        text = item.get("context")
        context_start = item.get("context_start_line")
        context_end = item.get("context_end_line")
        if isinstance(text, str) and _valid_line_range(context_start, context_end):
            lines.append(f"context_lines: {context_start}-{context_end}")
    if isinstance(text, str) and text:
        lines.append(text)


def _format_write_result(result: dict) -> str:
    ok = result.get("ok") is True
    lines = [f"WRITE_FILE {'OK' if ok else 'FAILED'}"]
    _append_scalar(lines, "path", result.get("path"))
    if not ok:
        _append_scalar(lines, "reason", result.get("error"))
        _append_scalar(lines, "message", result.get("message"))
    return "\n".join(lines)


def _format_simple_failure(prefix: str, result: dict, *, path: str) -> str:
    lines = [f"{prefix} FAILED", f"path: {path}"]
    _append_scalar(lines, "reason", result.get("error"))
    _append_scalar(lines, "message", result.get("message"))
    return "\n".join(lines)


def _append_scalar(lines: list[str], label: str, value: object) -> None:
    if value is not None and not isinstance(value, (dict, list)):
        lines.append(f"{label}: {value}")


def _text(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and value else fallback


def _path_name(path: str) -> str:
    normalized = path.replace("\\", "/").rstrip("/")
    return normalized.rsplit("/", 1)[-1] or path


def _format_ranges(ranges: list[tuple[int, int]]) -> str:
    return ", ".join(
        str(start) if start == end else f"{start}-{end}" for start, end in ranges
    )


def _read_ranges_from_field(result: dict, field: str) -> list[tuple[int, int]]:
    raw_ranges = result.get(field)
    if not isinstance(raw_ranges, list):
        return []
    ranges = []
    for item in raw_ranges:
        if not isinstance(item, dict):
            continue
        start = item.get("start_line")
        end = item.get("end_line")
        if _valid_line_range(start, end):
            ranges.append((start, end))
    return ranges


def _tool_names_by_call_id(messages: list[dict]) -> dict[str, str]:
    names = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or ():
            call_id = call.get("id")
            function = call.get("function") or {}
            name = function.get("name")
            if isinstance(call_id, str) and isinstance(name, str):
                names[call_id] = name
    return names


def _trim_payload(result: dict, field: str, limit: int, tool_name: str) -> dict:
    payload = result.get(field)
    if not isinstance(payload, str) or len(payload) <= limit:
        return result

    rendered = dict(result)
    rendered[field] = _bounded_text(payload, limit, _OMITTED_MARKERS[tool_name])
    rendered[f"model_{field}_truncated"] = True
    rendered[f"model_{field}_original_chars"] = len(payload)
    rendered[f"model_{field}_returned_chars"] = len(rendered[field])
    return rendered


def _retained_read_ranges(result: dict, limit: int) -> list[tuple[int, int]]:
    """Map fully retained payload lines back to their original source ranges."""
    content = result.get("content")
    if not isinstance(content, str) or len(content) <= limit:
        return _read_ranges(result) if isinstance(content, str) else []

    head_chars, tail_chars = _bounded_text_segment_lengths(limit)
    retained_spans = [(0, head_chars)]
    if tail_chars:
        retained_spans.append((len(content) - tail_chars, len(content)))

    payload_lines = content.splitlines(keepends=True)
    has_gap_markers = any(
        line.startswith("<<< CODE SQUID: lines ")
        and line.rstrip().endswith("omitted from this read >>>")
        for line in payload_lines
    )
    source_lines: list[int | None] = []
    previous_end = None
    for start, end in _read_ranges(result):
        # read_file renders one non-source marker line between disjoint ranges.
        # Keep it in the payload coordinate space without claiming coverage.
        if has_gap_markers and previous_end is not None and start > previous_end + 1:
            source_lines.append(None)
        source_lines.extend(range(start, end + 1))
        previous_end = end
    # Metadata from caller-defined read tools may not describe the payload
    # exactly. In that case, forgetting coverage is safer than blocking a read.
    if len(payload_lines) != len(source_lines):
        return []

    retained_lines = []
    offset = 0
    for line_number, line in zip(source_lines, payload_lines):
        line_end = offset + len(line)
        if line_number is not None and any(
            offset >= span_start and line_end <= span_end
            for span_start, span_end in retained_spans
        ):
            retained_lines.append(line_number)
        offset = line_end
    return _line_numbers_to_ranges(retained_lines)


def _read_ranges(result: dict) -> list[tuple[int, int]]:
    raw_ranges = result.get("returned_line_ranges")
    if isinstance(raw_ranges, list):
        ranges = []
        for item in raw_ranges:
            if not isinstance(item, dict):
                continue
            start = item.get("start_line")
            end = item.get("end_line")
            if _valid_line_range(start, end):
                ranges.append((start, end))
        return ranges

    start = result.get("start_line")
    end = result.get("end_line")
    return [(start, end)] if _valid_line_range(start, end) else []


def _valid_line_range(start: object, end: object) -> bool:
    return (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and start >= 1
        and end >= start
    )


def _line_numbers_to_ranges(line_numbers: list[int]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for line_number in line_numbers:
        if not ranges or line_number != ranges[-1][1] + 1:
            ranges.append((line_number, line_number))
        else:
            ranges[-1] = (ranges[-1][0], line_number)
    return ranges


def _trim_patch_diagnostics(result: dict, limit: int) -> dict:
    rendered = deepcopy(result)
    changed = False
    remaining = limit
    for container, field in _patch_diagnostic_fields(rendered):
        value = container.get(field)
        if not isinstance(value, str):
            continue
        if len(value) <= remaining:
            remaining -= len(value)
            continue
        changed = True
        if remaining:
            container[field] = _bounded_text(value, remaining, "")
            remaining = 0
        else:
            container.pop(field)
    if not changed:
        return result
    rendered["model_diagnostic_truncated"] = True
    rendered["model_diagnostic_note"] = _OMITTED_MARKERS["apply_patch"]
    return rendered


def _patch_diagnostic_fields(result: dict):
    closest_match = result.get("closest_match")
    if isinstance(closest_match, dict):
        yield closest_match, "content"
    for collection_name in ("candidates", "matches"):
        collection = result.get(collection_name)
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, dict):
                continue
            for field in ("text", "context", "content"):
                if field in item:
                    yield item, field


def _bounded_text(text: str, limit: int, omitted_marker: str) -> str:
    """Use deterministic head/tail retention with an exact output bound."""
    if limit == 0:
        return omitted_marker
    marker = "\n[older payload truncated]\n"
    if limit <= len(marker):
        return marker[:limit]
    head_chars, tail_chars = _bounded_text_segment_lengths(limit)
    tail = text[-tail_chars:] if tail_chars else ""
    return text[:head_chars] + marker + tail


def _bounded_text_segment_lengths(limit: int) -> tuple[int, int]:
    """Return source head/tail character counts retained by ``_bounded_text``."""
    marker_length = len("\n[older payload truncated]\n")
    if limit <= marker_length:
        return 0, 0
    remaining = limit - marker_length
    return (remaining + 1) // 2, remaining // 2
