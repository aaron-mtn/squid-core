"""Compact event construction and forwarding for the agent loop."""

from typing import Any

from squid_core.events import EventCallback, emit_event


def emit_agent_event(
    callback: EventCallback | None,
    session_id: str | None,
    event: dict[str, Any],
) -> None:
    """Attach loop-wide metadata and forward one compact observer event."""
    # Runtime events are intentionally compact: callers should never receive
    # raw file contents, provider payloads, prompts, credentials, or headers.
    emit_event(callback, {"session_id": session_id, **event})


def action_event_fields(action: dict) -> dict[str, Any]:
    """Project the safe, useful subset of a tool action into an event."""
    event = {"tool": action.get("tool", "")}
    if "tool_call_id" in action:
        event["tool_call_id"] = action["tool_call_id"]

    args = action.get("args", {})
    if isinstance(args, dict):
        if "path" in args:
            event["path"] = args["path"]
        if "command" in args:
            event["command"] = args["command"]
    return event


def tool_preparing_event(tool_call: dict[str, Any]) -> dict[str, Any]:
    """Build the compact event emitted while a streamed tool call forms."""
    event = {
        "type": "tool_preparing",
        "tool": tool_call.get("tool", ""),
    }
    if "tool_call_index" in tool_call:
        event["tool_call_index"] = tool_call["tool_call_index"]
    if "tool_call_id" in tool_call:
        event["tool_call_id"] = tool_call["tool_call_id"]
    return event


def compact_event_text(text: object, max_chars: int) -> str:
    """Return stripped event text, truncating it to the requested bound."""
    value = str(text).strip()
    if len(value) <= max_chars:
        return value

    suffix = "... [truncated]"
    return f"{value[: max_chars - len(suffix)]}{suffix}"
