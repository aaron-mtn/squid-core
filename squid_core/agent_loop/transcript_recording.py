"""Transcript message construction, recording, and failed-run repair."""

from typing import Any, Iterable


def record_message(
    session,
    session_store,
    message: dict[str, Any],
    usage: dict | None = None,
) -> None:
    """Record one accepted message, committing durable storage before memory."""
    # Rejected assistant replies intentionally bypass this central write path
    # so normal transcript history contains accepted messages only.
    if session_store is not None:
        session_store.append_message(message, usage=usage)
    session.add_message(message)


def record_messages(session, session_store, *messages: dict[str, Any]) -> None:
    """Record accepted transcript messages in their supplied order."""
    for message in messages:
        record_message(session, session_store, message)


def reconcile_system_message(
    session,
    session_store,
    intended_message: dict[str, Any],
) -> None:
    """Make one current system message active without disturbing conversation history."""
    system_messages = [
        message for message in session.messages if message.get("role") == "system"
    ]
    if (
        system_messages == [intended_message]
        and session.messages[0] == intended_message
    ):
        return

    # Build the replacement before persistence so a failed durable append cannot
    # leave memory claiming that a reconciliation was committed. Keep every
    # non-system message object, and its relative order, untouched.
    reconciled_messages = [
        intended_message,
        *(message for message in session.messages if message.get("role") != "system"),
    ]
    if session_store is not None:
        session_store.append_system_message_reconciliation(intended_message)
    session.messages[:] = reconciled_messages


def user_instruction_message(instruction: str) -> dict[str, str]:
    """Build a user-role instruction inserted by the runtime."""
    return {"role": "user", "content": instruction}


def turn_context_message(blocks: Iterable) -> dict[str, str] | None:
    """Build the temporary user message containing hook context blocks."""
    blocks = list(blocks)
    if not blocks:
        return None

    parts = ["# Additional Context For This Turn"]
    for block in blocks:
        title = block.title.strip() or "Context"
        source = f" ({block.source})" if block.source else ""
        body = block.body.strip()
        if body:
            parts.append(f"\n## {title}{source}\n{body}")

    content = "\n".join(parts).strip()
    if content == "# Additional Context For This Turn":
        return None
    return {"role": "user", "content": content}


def record_pending_tool_results(
    session,
    session_store,
    tool_results,
    pending_tool_actions: list[dict],
    pending_tool_results: list[dict],
    *,
    interrupted: bool = True,
) -> None:
    """Close every call in a committed tool turn before a run unwinds."""
    if not pending_tool_actions:
        return

    results = list(pending_tool_results)
    first_missing_index = len(results)
    for action_index, action in enumerate(
        pending_tool_actions[first_missing_index:],
        start=first_missing_index,
    ):
        if interrupted:
            result = {
                "ok": False,
                "tool": action.get("tool", ""),
                "error": "tool action interrupted",
                "reason": (
                    "interrupted before a tool result was available"
                    if action_index == first_missing_index
                    else "batch interrupted before this tool action was attempted"
                ),
                "interrupted": True,
            }
        else:
            result = {
                "ok": False,
                "tool": action.get("tool", ""),
                "error": "tool action failed",
                "reason": (
                    "run failed before a tool result was available"
                    if action_index == first_missing_index
                    else "run failed before this tool action was attempted"
                ),
                "failed": True,
            }
        if action_index > first_missing_index:
            result["skipped"] = True
        results.append(result)
    record_messages(
        session,
        session_store,
        *tool_results.messages_from_action_results(
            pending_tool_actions,
            results,
        ),
    )
