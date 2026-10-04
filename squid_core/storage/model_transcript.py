"""Render a chronological, human-readable view of model-visible context."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from squid_core.compaction.compactor import COMPACTED_CONTEXT_PREAMBLE
from squid_core.compaction.prompts import COMPACTION_SYSTEM_PROMPT
from squid_core.storage.session_jsonl import load_session_jsonl


TRANSCRIPT_TITLE = "MODEL VIEW TRANSCRIPT"


def write_model_transcript(
    session_path: Path,
    model_view_path: Path,
    transcript_path: Path,
) -> None:
    """Rebuild the readable transcript from the two canonical session logs.

    Rebuilding keeps this file a projection instead of a third source of truth.
    It also lets a later request clarify whether an earlier response was a final
    answer and reveal the exact context message produced by compaction.
    """
    session_entries = load_session_jsonl(session_path)
    request_records = _load_jsonl(model_view_path)
    rendered = render_model_transcript(session_entries, request_records)
    temporary_path = transcript_path.with_name(f".{transcript_path.name}.tmp")
    temporary_path.write_text(rendered, encoding="utf-8")
    temporary_path.replace(transcript_path)


def render_model_transcript(
    session_entries: list[dict],
    request_records: list[dict],
) -> str:
    """Return a readable evolution log without repeated accumulated requests."""
    compaction_request_indexes = _compaction_request_indexes(
        session_entries, request_records
    )
    ordinary_requests = [
        record
        for record in request_records
        if record.get("request_index") not in compaction_request_indexes
    ]
    model_record = ordinary_requests[0] if ordinary_requests else (
        request_records[0] if request_records else {}
    )
    lines = [TRANSCRIPT_TITLE]
    if model_record.get("model"):
        lines.append(f"Model: {model_record['model']}")
    if model_record.get("provider"):
        lines.append(f"Provider: {model_record['provider']}")

    timeline = _timeline(session_entries, request_records)
    accepted_final_replies = _accepted_final_replies(session_entries)
    seen_assistant_messages: list[dict] = []
    previous_messages: list[dict] = []
    pending_summary = ""
    compaction_reset = False
    wrote_initial = False

    for kind, item in timeline:
        if kind == "request":
            if item.get("request_index") in compaction_request_indexes:
                continue
            messages = item.get("messages")
            if not isinstance(messages, list):
                continue
            new_messages, transformations = _request_changes(
                previous_messages, messages, compaction_reset=compaction_reset
            )
            if not wrote_initial:
                _section(lines, "INITIAL CONTEXT")
                wrote_initial = True
            elif new_messages or transformations:
                _section(lines, f"TURN {item.get('request_index', '?')}")
            for description in transformations:
                _block(lines, "CONTEXT TRANSFORMATION", description)
            for message in new_messages:
                if _assistant_was_already_rendered(message, seen_assistant_messages):
                    continue
                _render_message(lines, message)
            previous_messages = messages
            compaction_reset = False
            continue

        entry_type = item.get("type")
        if entry_type == "model_reply":
            reply = item.get("reply") or {}
            assistant = reply.get("assistant") or {}
            seen_assistant_messages.append(assistant)
            _render_reply(
                lines,
                assistant,
                final=id(item) in accepted_final_replies,
            )
        elif entry_type == "compaction_reasoning":
            _block(lines, "COMPACTION REASONING", item.get("content", ""))
        elif entry_type == "compaction_summary":
            pending_summary = str(item.get("content", ""))
        elif entry_type == "compaction_applied":
            inserted = _next_compacted_message(
                item, ordinary_requests, fallback_summary=pending_summary
            )
            _section(lines, "CONTEXT TRANSFORMATION")
            _block(
                lines,
                "COMPACTION",
                "Earlier context was replaced for subsequent model requests by:",
            )
            _block(lines, "SUMMARY INSERTED", inserted)
            compaction_reset = True
            pending_summary = ""

    return "\n".join(lines).rstrip() + "\n"


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def _timeline(
    session_entries: list[dict], request_records: list[dict]
) -> list[tuple[str, dict]]:
    items = [*(('session', entry) for entry in session_entries)]
    items.extend(('request', record) for record in request_records)
    # Timestamps come from the same process clock. Secondary ordering is only
    # for deterministic hand-authored fixtures that reuse one timestamp.
    return sorted(
        items,
        key=lambda pair: (
            str(pair[1].get("timestamp", "")),
            0 if pair[0] == "request" else 1,
        ),
    )


def _compaction_request_indexes(
    session_entries: list[dict], request_records: list[dict]
) -> set[int]:
    """Identify text-only summarizer calls bracketed by compaction events."""
    indexes = {
        record["request_index"]
        for record in request_records
        if isinstance(record.get("request_index"), int)
        and (messages := record.get("messages"))
        and isinstance(messages, list)
        and isinstance(messages[0], dict)
        and messages[0].get("role") == "system"
        and messages[0].get("content") == COMPACTION_SYSTEM_PROMPT
    }
    events = sorted(
        [
            *(('session', entry) for entry in session_entries),
            *(('request', record) for record in request_records),
        ],
        key=lambda pair: str(pair[1].get("timestamp", "")),
    )
    waiting_for_request = False
    for kind, item in events:
        if kind == "session" and item.get("type") == "compaction_request":
            waiting_for_request = True
        elif kind == "request" and waiting_for_request:
            index = item.get("request_index")
            if isinstance(index, int):
                indexes.add(index)
            waiting_for_request = False
        elif kind == "session" and item.get("type") in {
            "compaction_failed",
            "compaction_applied",
        }:
            waiting_for_request = False
    return indexes


def _accepted_final_replies(session_entries: list[dict]) -> set[int]:
    """Match accepted final messages to the provider reply immediately behind them."""
    accepted: set[int] = set()
    candidate: dict | None = None
    for entry in session_entries:
        if entry.get("type") == "model_reply":
            assistant = (entry.get("reply") or {}).get("assistant") or {}
            candidate = entry if not assistant.get("raw_tool_calls") else None
            continue
        if entry.get("type") == "reply_rejection":
            candidate = None
            continue
        message = entry.get("message")
        if (
            entry.get("type") == "message"
            and candidate is not None
            and isinstance(message, dict)
            and message.get("role") == "assistant"
            and not message.get("tool_calls")
            and message.get("content")
            == ((candidate.get("reply") or {}).get("assistant") or {}).get("content")
        ):
            accepted.add(id(candidate))
            candidate = None
    return accepted


def _request_changes(
    previous: list[dict],
    current: list[dict],
    *,
    compaction_reset: bool,
) -> tuple[list[dict], list[str]]:
    if not previous:
        return list(current), []

    previous_counts = Counter(_message_key(message) for message in previous)
    remaining = previous_counts.copy()
    new_messages = []
    for message in current:
        key = _message_key(message)
        if remaining[key]:
            remaining[key] -= 1
        elif not (
            compaction_reset
            and message.get("role") == "assistant"
            and str(message.get("content", "")).startswith(COMPACTED_CONTEXT_PREAMBLE)
        ):
            new_messages.append(message)

    transformations = []
    if not compaction_reset:
        previous_by_identity = {
            identity: message
            for message in previous
            if (identity := _message_identity(message)) is not None
        }
        for message in current:
            identity = _message_identity(message)
            old = previous_by_identity.get(identity)
            if old is not None and _message_key(old) != _message_key(message):
                label = (
                    "Tool result changed"
                    if message.get("role") == "tool"
                    else "Context message changed"
                )
                transformations.append(f"{label}: {_identity_text(identity)}")

        current_counts = Counter(_message_key(item) for item in current)
        removed_transient = [
            message
            for message in previous
            if not current_counts[_message_key(message)]
            and _is_transient_context(message)
        ]
        if removed_transient:
            transformations.append(
                "Temporary harness context from the prior request was removed."
            )
    return new_messages, transformations


def _message_key(message: Any) -> str:
    return json.dumps(message, ensure_ascii=False, sort_keys=True, default=str)


def _message_identity(message: dict) -> tuple[str, str] | None:
    role = message.get("role")
    if role == "tool" and message.get("tool_call_id"):
        return role, str(message["tool_call_id"])
    if role == "system":
        return role, "system"
    return None


def _identity_text(identity: tuple[str, str]) -> str:
    return f"{identity[0]} {identity[1]}"


def _is_transient_context(message: dict) -> bool:
    return message.get("role") == "user" and str(message.get("content", "")).startswith(
        "# Additional Context For This Turn"
    )


def _assistant_was_already_rendered(message: dict, replies: list[dict]) -> bool:
    if message.get("role") != "assistant":
        return False
    message_call_ids = [call.get("id") for call in message.get("tool_calls") or []]
    for reply in replies:
        reply_call_ids = [call.get("id") for call in reply.get("raw_tool_calls") or []]
        if (
            message.get("content") == (reply.get("content") or "")
            and message_call_ids == reply_call_ids
        ):
            return True
    return False


def _render_reply(lines: list[str], assistant: dict, *, final: bool) -> None:
    for name in ("reasoning", "reasoning_content", "thinking"):
        if assistant.get(name) not in (None, ""):
            _block(lines, "REASONING", _readable(assistant[name]))
            break
    content = assistant.get("content")
    if content:
        if final:
            _section(lines, "FINAL")
        _block(lines, "FINAL RESPONSE" if final else "ASSISTANT", _readable(content))
    for call in assistant.get("raw_tool_calls") or []:
        _render_tool_call(lines, call)


def _render_message(lines: list[str], message: dict) -> None:
    role = message.get("role", "unknown")
    content = message.get("content")
    if role == "tool":
        _block(lines, "TOOL RESULT", _readable(content))
        return
    if role == "assistant" and message.get("tool_calls"):
        if content:
            _block(lines, "ASSISTANT", _readable(content))
        for call in message["tool_calls"]:
            _render_tool_call(lines, call)
        return
    if _is_transient_context(message):
        _section(lines, "HARNESS CONTEXT")
        _block(lines, "INJECTED CONTEXT", _readable(content))
        return
    label = {
        "system": "SYSTEM",
        "user": "USER",
        "assistant": "ASSISTANT",
    }.get(role, role.upper())
    _block(lines, label, _readable(content))


def _render_tool_call(lines: list[str], call: dict) -> None:
    function = call.get("function") or {}
    name = function.get("name") or call.get("name") or "unknown"
    arguments = function.get("arguments", call.get("arguments", ""))
    body = name
    if arguments not in (None, ""):
        body = f"{body}\n{_readable(arguments)}"
    _block(lines, "TOOL CALL", body)


def _readable(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        descriptions = [
            block.get("description")
            for block in value
            if isinstance(block, dict)
            and block.get("type") == "image_asset"
            and isinstance(block.get("description"), str)
        ]
        if descriptions:
            return "\n".join(descriptions)
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _next_compacted_message(
    applied_event: dict,
    requests: list[dict],
    *,
    fallback_summary: str,
) -> str:
    applied_at = str(applied_event.get("timestamp", ""))
    for request in requests:
        if str(request.get("timestamp", "")) <= applied_at:
            continue
        for message in request.get("messages", []):
            content = message.get("content")
            if (
                message.get("role") == "assistant"
                and isinstance(content, str)
                and content.startswith(COMPACTED_CONTEXT_PREAMBLE)
            ):
                return content
    return f"{COMPACTED_CONTEXT_PREAMBLE}\n{fallback_summary}".rstrip()


def _section(lines: list[str], title: str) -> None:
    lines.extend(["", f"==================== {title} ====================", ""])


def _block(lines: list[str], label: str, content: str) -> None:
    lines.extend([f"[{label}]", str(content), ""])
