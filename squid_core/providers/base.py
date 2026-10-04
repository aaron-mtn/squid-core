"""Generic provider contract and cancellation signal shared with transports."""

from squid_core.model_response import ModelResponse


class StreamCancelled(Exception):
    """Signal cooperative cancellation; preserve one identity across transports."""


class BaseProvider:
    """Optional base for the single provider contract.

    Normal inference requires only ``create_response(model, messages, tools)``
    returning Core's ``ModelResponse``. Subclassing is optional; plain objects
    implementing that operation use the same ModelAdapter path. Providers own
    native API translation, never agent policy, loop control, or tool execution.
    """

    def supports_streaming(self) -> bool:
        """Opt in only when ``stream_chat`` is implemented."""
        return False

    def supports_image_input(self, model: str) -> bool:
        """Fail closed unless a transport can verify model vision support."""

        return False

    def is_ready(self) -> bool:
        """Optional host readiness check; Core inference never calls this."""
        raise NotImplementedError

    def list_models(self) -> list[str]:
        """Optional host discovery; Core uses the caller's explicit model name."""
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
        """Translate a Core request into completed, provider-neutral output.

        Core passes the prepared request as keyword arguments, not a new
        request wrapper. The first three fields suffice for normal inference;
        remaining options apply only to callers requesting those capabilities.
        """
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
        """Optional streaming transport returning the same completed contract."""
        raise NotImplementedError
