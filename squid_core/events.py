"""Tiny best-effort event callback helper.

This module only normalizes and delivers in-process event dictionaries. It does
not know about terminal display, JSONL, GUI wrappers, host applications, or
external apps.
"""

from datetime import datetime, timezone
from typing import Any, Callable


EventCallback = Callable[[dict[str, Any]], None]


def emit_event(on_event: EventCallback | None, event: dict[str, Any]) -> None:
    """Copy, timestamp, and deliver one event to an optional callback.

    ``on_event=None`` means no listener and no behavior change. The event is
    copied before a missing UTC timestamp is inserted, and callback exceptions
    are swallowed so observers cannot crash an agent run.
    """

    if on_event is None:
        return

    event_to_emit = dict(event)
    if "timestamp" not in event_to_emit:
        event_to_emit["timestamp"] = datetime.now(timezone.utc).isoformat()

    try:
        on_event(event_to_emit)
    except Exception:
        pass
