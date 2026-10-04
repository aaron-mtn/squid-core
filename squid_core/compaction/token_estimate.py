"""Rough token estimates for transcript-sized message payloads."""

from __future__ import annotations

import json
from typing import Any


def estimate_text_tokens(text: str) -> int:
    if text == "":
        return 0

    return max(1, len(text) // 4)


def estimate_message_tokens(message: dict[str, Any]) -> int:
    serialized = json.dumps(message, ensure_ascii=False)
    return estimate_text_tokens(serialized)


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)
