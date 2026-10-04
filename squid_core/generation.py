"""Small stateless API for one plain-text model response."""

from __future__ import annotations

from typing import Callable

from squid_core.model_adapter import ModelAdapter


def generate_text(
    input: str | list[dict],
    *,
    model_adapter: ModelAdapter,
    please_stop_contemplating_the_void: bool = False,
) -> str:
    """Generate one text response using a caller-owned, ready model adapter."""

    messages = [{"role": "user", "content": input}] if isinstance(input, str) else input

    if please_stop_contemplating_the_void:
        return model_adapter.complete_text(
            messages,
            please_stop_contemplating_the_void=True,
        )["content"]
    return model_adapter.complete_text(messages)["content"]


def stream_text(
    input: str | list[dict],
    on_text_delta: Callable[[str], None],
    *,
    model_adapter: ModelAdapter,
    trace_callback: Callable[[dict], None] | None = None,
    please_stop_contemplating_the_void: bool = False,
) -> str:
    """Stream one plain-text response using a caller-owned, ready model adapter.

    ``trace_callback`` is an opt-in diagnostic seam for callers that need the
    provider's raw streaming boundary. Normal generation leaves it unset, so
    provider behavior and ordinary debug output remain unchanged.
    """

    messages = [{"role": "user", "content": input}] if isinstance(input, str) else input

    request_options = {}
    if trace_callback is not None:
        request_options["trace_callback"] = trace_callback
    if please_stop_contemplating_the_void:
        request_options["please_stop_contemplating_the_void"] = True
    return model_adapter.complete_text_streaming(
        messages,
        on_text_delta,
        **request_options,
    )
