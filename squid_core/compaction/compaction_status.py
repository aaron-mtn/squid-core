"""Backend-only context usage status for reporting surfaces."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from squid_core.compaction.token_estimate import estimate_messages_tokens


@dataclass(frozen=True)
class ContextStatus:
    estimated_tokens: int
    limit_tokens: int | None
    percent: int | None
    label: str
    compaction_enabled: bool
    trigger_input_tokens: int | None
    compact_fraction: float | None
    min_recent_chunks: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def format_token_count(tokens: int) -> str:
    if tokens < 1000:
        return str(tokens)

    thousands = tokens / 1000
    if tokens % 1000 == 0:
        return f"{int(thousands)}k"

    formatted = f"{thousands:.1f}".rstrip("0").rstrip(".")
    return f"{formatted}k"


def build_context_status(
    messages: list[dict[str, Any]], compaction_settings: Any
) -> ContextStatus:
    estimated_tokens = estimate_messages_tokens(messages)
    compaction_enabled = bool(getattr(compaction_settings, "enabled", False))
    trigger_input_tokens = getattr(compaction_settings, "trigger_input_tokens", None)
    compact_fraction = getattr(compaction_settings, "compact_fraction", None)
    min_recent_chunks = getattr(compaction_settings, "min_recent_chunks", None)

    has_trigger_limit = (
        compaction_enabled
        and isinstance(trigger_input_tokens, int)
        and not isinstance(trigger_input_tokens, bool)
        and trigger_input_tokens > 0
    )

    if has_trigger_limit:
        percent = round(estimated_tokens / trigger_input_tokens * 100)
        label = (
            f"{format_token_count(estimated_tokens)} / "
            f"{format_token_count(trigger_input_tokens)}"
        )
        limit_tokens = trigger_input_tokens
    else:
        percent = None
        label = format_token_count(estimated_tokens)
        limit_tokens = None

    return ContextStatus(
        estimated_tokens=estimated_tokens,
        limit_tokens=limit_tokens,
        percent=percent,
        label=label,
        compaction_enabled=compaction_enabled,
        trigger_input_tokens=trigger_input_tokens,
        compact_fraction=compact_fraction,
        min_recent_chunks=min_recent_chunks,
    )
