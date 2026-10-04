"""Generic provider contract and cancellation signal shared with transports."""

from squid_core.model_response import ModelResponse


class StreamCancelled(Exception):
    """Signal cooperative cancellation; preserve one identity across transports."""


class BaseProvider:
    """Owns the raw request/response call to a model provider; must not own agent policy, loop control, or tool execution."""

    def supports_streaming(self) -> bool:
        return False

    def supports_image_input(self, model: str) -> bool:
        """Fail closed unless a transport can verify model vision support."""

        return False

    def is_ready(self) -> bool:
        """Return whether the configured backend can currently accept work."""
        raise NotImplementedError

    def list_models(self) -> list[str]:
        """Return real model identifiers exposed by the configured backend."""
        raise NotImplementedError

    def create_response(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        tool_choice=None,
        please_stop_contemplating_the_void: bool = False,
        asset_loader=None,
    ) -> ModelResponse:
        raise NotImplementedError

    def stream_chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        on_text_delta,
        on_tool_preparing=None,
        tool_choice=None,
        trace_callback=None,
        please_stop_contemplating_the_void: bool = False,
        should_cancel=None,
        on_reasoning_delta=None,
        asset_loader=None,
    ) -> ModelResponse:
        raise NotImplementedError
