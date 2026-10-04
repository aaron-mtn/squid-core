"""Normalize model-provider requests, responses, and safe failure details."""

import inspect
import json
import re
from collections.abc import Callable
from dataclasses import asdict

from squid_core.model_response import ModelResponse
from squid_core.image_content import VISION_UNAVAILABLE_MESSAGE
from squid_core.providers.base import StreamCancelled

TOOL_CALL_CONTENT_MODES = {"omit", "empty", "null"}


ERROR_TEXT_MAX_CHARS = 500
RESPONSE_EXCERPT_MAX_CHARS = 1000
_SECRET_PATTERNS = (
    re.compile(
        r'(?i)(authorization\s*["\']?\s*[:=]\s*["\']?)([^,"\';\s}]+(?:\s+[^,"\';\s}]+)?)'
    ),
    re.compile(
        r'(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|private[_-]?key|password|secret)\s*["\']?\s*[:=]\s*["\']?)([^"\',;\s}]+)'
    ),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(\b)sk-[A-Za-z0-9_-]{8,}"),
)


def _request_has_image_assets(request: dict) -> bool:
    """Return whether a prepared request contains a request-only image reference."""

    for message in request.get("messages", []):
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "image_asset"
            for block in content
        ):
            return True
    return False


class ModelRequestError(RuntimeError):
    """Raised when provider transport or response handling fails."""


def _safe_text(value, max_chars: int = ERROR_TEXT_MAX_CHARS) -> str:
    """Bound provider text and redact common credential representations."""

    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1[REDACTED]", text)
    if len(text) > max_chars:
        return f"{text[:max_chars]}..."
    return text


def _exception_chain(error: BaseException) -> list[BaseException]:
    """Return a short, cycle-safe cause/context chain, outermost first."""

    chain = []
    current: BaseException | None = error
    while current is not None and len(chain) < 5 and current not in chain:
        chain.append(current)
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    return chain


def _safe_attr(value, name: str):
    try:
        return getattr(value, name, None)
    except Exception:
        return None


def _status_from_chain(chain: list[BaseException]) -> int | None:
    for error in chain:
        response = _safe_attr(error, "response")
        for value in (
            _safe_attr(error, "status_code"),
            _safe_attr(error, "status"),
            _safe_attr(response, "status_code") if response is not None else None,
            _safe_attr(response, "status") if response is not None else None,
        ):
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


def _header_value(headers, name: str):
    if headers is None:
        return None
    try:
        return headers.get(name) or headers.get(name.lower()) or headers.get(name.title())
    except (AttributeError, TypeError):
        return None


def _response_metadata(chain: list[BaseException]) -> dict:
    fields = {}
    for error in chain:
        response = _safe_attr(error, "response")
        headers = _safe_attr(response, "headers") if response is not None else None

        request_id = _safe_attr(error, "request_id")
        if request_id is None:
            for name in ("x-request-id", "request-id", "cf-ray"):
                request_id = _header_value(headers, name)
                if request_id is not None:
                    break
        if request_id is not None and "provider_request_id" not in fields:
            fields["provider_request_id"] = _safe_text(request_id)

        retry_after = _safe_attr(error, "retry_after")
        if retry_after is None:
            retry_after = _header_value(headers, "retry-after")
        if retry_after is not None and "retry_after" not in fields:
            fields["retry_after"] = _safe_text(retry_after)

        body = _safe_attr(error, "body")
        if body is None and response is not None:
            body = _safe_attr(response, "text")
        if body is None and response is not None:
            body = _safe_attr(response, "content")
        if body is not None and "response_body_excerpt" not in fields:
            if isinstance(body, (dict, list)):
                try:
                    body = json.dumps(body, ensure_ascii=False)
                except (TypeError, ValueError):
                    body = str(body)
            fields["response_body_excerpt"] = _safe_text(
                body,
                RESPONSE_EXCERPT_MAX_CHARS,
            )
    return fields


def _failure_class(chain: list[BaseException], http_status: int | None) -> tuple[str, bool]:
    names = {type(error).__name__.lower() for error in chain}
    if http_status == 429 or any("ratelimit" in name for name in names):
        return "rate_limit", True
    if http_status in {401, 403} or any(
        "authentication" in name or "permissiondenied" in name for name in names
    ):
        return "authentication", False
    if http_status is not None and 500 <= http_status <= 599:
        return "provider_server_error", True
    if http_status in {400, 404, 405, 409, 422}:
        return "configuration", False
    if any(isinstance(error, TimeoutError) for error in chain) or any(
        "timeout" in name for name in names
    ):
        return "timeout", True
    if any(isinstance(error, ConnectionError) for error in chain) or any(
        token in name
        for name in names
        for token in ("connectionerror", "connecterror", "networkerror")
    ):
        return "connection_error", True
    if any(
        token in name
        for name in names
        for token in ("streamerror", "incompleteread", "protocolerror")
    ):
        return "stream_error", True
    if any(
        token in name
        for name in names
        for token in ("jsondecodeerror", "responsevalidationerror", "decodeerror")
    ):
        return "malformed_response", False
    if any(
        token in name
        for name in names
        for token in ("badrequest", "invalidrequest", "configurationerror")
    ):
        return "configuration", False
    return "unknown", False


def model_request_error_details(error: BaseException) -> dict:
    """Extract bounded, credential-redacted diagnostics from a request failure."""

    chain = _exception_chain(error)
    underlying = next(
        (item for item in chain[1:] if not isinstance(item, ModelRequestError)),
        None,
    )
    http_status = _status_from_chain(chain)
    override = next(
        (
            _safe_attr(item, "_failure_class_override")
            for item in chain
            if _safe_attr(item, "_failure_class_override") is not None
        ),
        None,
    )
    failure_class, transient = (
        (override, False)
        if override is not None
        else _failure_class(chain, http_status)
    )
    fields = {
        "error_type": type(error).__name__,
        "error_message": _safe_text(error or type(error).__name__),
        "failure_class": failure_class,
    }
    if underlying is not None:
        fields["underlying_error_type"] = type(underlying).__name__
        fields["underlying_error_message"] = _safe_text(
            underlying or type(underlying).__name__
        )
    if len(chain) > 2:
        fields["exception_chain"] = [
            {
                "error_type": type(item).__name__,
                "error_message": _safe_text(item or type(item).__name__),
            }
            for item in chain[1:]
        ]
    if http_status is not None:
        fields["http_status"] = http_status
    fields.update(_response_metadata(chain))
    # Retry policy consumes this flag, but it is not provider evidence worth
    # duplicating in the durable event; failure_class carries that evidence.
    fields["_transient"] = transient
    return fields


def _request_error(
    error: Exception,
    *,
    failure_class: str | None = None,
) -> ModelRequestError:
    details = model_request_error_details(error)
    summary = details["error_message"] or type(error).__name__
    wrapped = ModelRequestError(
        f"Model request failed: {type(error).__name__}: {summary}"
    )
    if failure_class is not None:
        wrapped._failure_class_override = failure_class
    return wrapped


class ModelAdapter:
    """Owns provider communication and tool-call normalization; must not own loop control, session storage, or coding-agent policy."""

    # Store the provider and model name used for requests.
    # The provider handles the raw API call, while this adapter normalizes
    # provider output into the small internal reply shape used by the loop.
    def __init__(
        self,
        provider,
        model_name: str,
        tool_call_content_mode: str = "empty",
        provider_name: str | None = None,
        request_observer: Callable[[dict], object] | None = None,
    ) -> None:
        if tool_call_content_mode not in TOOL_CALL_CONTENT_MODES:
            valid_modes = ", ".join(sorted(TOOL_CALL_CONTENT_MODES))
            raise ValueError(f"tool_call_content_mode must be one of {valid_modes}")

        self.provider = provider
        self.model_name = model_name
        self.tool_call_content_mode = tool_call_content_mode
        self.provider_name = provider_name or type(provider).__name__
        # These normalized values are safe request metadata. Keeping them on
        # the adapter lets the runtime log effective configuration without
        # exposing provider payloads or credentials.
        self.reasoning_mode = getattr(provider, "reasoning_mode", None)
        self.reasoning_token_budget = getattr(
            provider,
            "reasoning_token_budget",
            None,
        )
        self._request_observer = request_observer
        self._asset_loader = None

    def set_asset_loader(self, loader: Callable[[str], bytes] | None) -> None:
        """Set the session-scoped loader used only for provider image transport."""

        self._asset_loader = loader

    def set_request_observer(
        self,
        observer: Callable[[dict], object] | None,
    ) -> None:
        """Set a generic best-effort observer for finalized provider requests."""
        self._request_observer = observer

    def _observe_request(self, request: dict) -> None:
        """Report the exact provider-bound object without affecting transport."""
        if self._request_observer is None:
            return
        try:
            self._request_observer(request)
        except Exception:
            # Diagnostics must never turn an otherwise valid request into a
            # model failure. Hosts choose whether and where observations live.
            pass

    def _send_prepared_request(
        self,
        request: dict,
        *,
        streaming: bool = False,
        **transport_options,
    ) -> ModelResponse:
        """Observe once at the common boundary, then invoke provider transport."""
        self._observe_request(request)
        try:
            transport = (
                self.provider.stream_chat
                if streaming
                else self.provider.create_response
            )
            if _request_has_image_assets(request):
                supports_images = getattr(self.provider, "supports_image_input", None)
                if not callable(supports_images) or not supports_images(request["model"]):
                    raise RuntimeError(
                        f"Provider/model {self.provider_name}/{request['model']} does not "
                        "support image input"
                    )
                if self._asset_loader is None:
                    raise RuntimeError("session image asset loader is unavailable")
                transport_options["asset_loader"] = self._asset_loader
            return transport(**request, **transport_options)
        except StreamCancelled:
            # Cooperative cancellation is loop control, not a provider failure.
            raise
        except Exception as exc:
            # Provider-specific failures are wrapped at this boundary so the
            # host can distinguish transport failures from application bugs.
            raise _request_error(exc) from exc

    def prepare_request(
        self,
        messages: list[dict],
        tool_definitions: list[dict],
    ) -> dict:
        """Finalize the model-visible request before transport or observation."""
        return {
            "model": self.model_name,
            "messages": self._messages_for_provider(messages),
            "tools": tool_definitions,
        }

    # Send a finalized request to the provider, then normalize its reply into
    # the internal shape used by the loop. Keeping preparation separate makes
    # the provider-bound representation observable without reconstructing it.
    def respond_prepared(self, request: dict) -> dict:
        tool_metadata = self._tool_metadata_from_definitions(request["tools"])
        raw_response = self._send_prepared_request(request)

        return self._reply_from_response_or_error(raw_response, tool_metadata)

    # Preserve the convenient public entry point for non-loop callers.
    #
    # If the provider returns malformed tool-call output, convert that into
    # a controlled format_error reply instead of crashing the loop.
    def respond(self, messages: list[dict], tool_definitions: list[dict]) -> dict:
        return self.respond_prepared(self.prepare_request(messages, tool_definitions))

    def supports_streaming(self) -> bool:
        return bool(getattr(self.provider, "supports_streaming", lambda: False)())

    def supports_image_input(self) -> bool:
        """Return whether the active provider can verify vision support."""

        supports_images = getattr(self.provider, "supports_image_input", None)
        if not callable(supports_images):
            return False
        try:
            return bool(supports_images(self.model_name))
        except Exception:
            # Capability checks are guards, not model requests. If discovery is
            # unavailable, fail closed so image bytes never reach an uncertain
            # transport.
            return False

    def respond_streaming(
        self,
        messages: list[dict],
        tool_definitions: list[dict],
        on_text_delta,
        on_tool_preparing=None,
        should_cancel=None,
        on_reasoning_delta=None,
    ) -> dict:
        return self.respond_streaming_prepared(
            self.prepare_request(messages, tool_definitions),
            on_text_delta=on_text_delta,
            on_reasoning_delta=on_reasoning_delta,
            on_tool_preparing=on_tool_preparing,
            should_cancel=should_cancel,
        )

    def respond_streaming_prepared(
        self,
        request: dict,
        on_text_delta,
        on_tool_preparing=None,
        should_cancel=None,
        on_reasoning_delta=None,
    ) -> dict:
        """Stream one already-finalized provider request."""
        tool_metadata = self._tool_metadata_from_definitions(request["tools"])
        transport_options = {
            "on_text_delta": on_text_delta,
            "on_tool_preparing": on_tool_preparing,
            "should_cancel": should_cancel,
        }
        if on_reasoning_delta is not None:
            try:
                parameters = inspect.signature(self.provider.stream_chat).parameters
            except (TypeError, ValueError):
                parameters = {}
            accepts_keywords = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            if "on_reasoning_delta" in parameters or accepts_keywords:
                # Keep older custom streaming providers working while built-in
                # transports adopt the optional reasoning-delta seam.
                transport_options["on_reasoning_delta"] = on_reasoning_delta
        raw_response = self._send_prepared_request(
            request,
            streaming=True,
            **transport_options,
        )

        return self._reply_from_response_or_error(raw_response, tool_metadata)

    def complete_text(
        self,
        messages: list[dict],
        please_stop_contemplating_the_void: bool = False,
        should_cancel=None,
    ) -> dict:
        request_options = self.prepare_request(messages, [])
        if please_stop_contemplating_the_void:
            request_options["please_stop_contemplating_the_void"] = True
        return self.complete_text_prepared(
            request_options,
            should_cancel=should_cancel,
        )

    def complete_text_prepared(self, request: dict, should_cancel=None) -> dict:
        """Complete one text-only request from its finalized representation."""
        if should_cancel is not None and self.supports_streaming():
            # Accumulating the provider stream preserves the completed response
            # shape while making hidden/non-UI generation transport-cancellable.
            raw_response = self._send_prepared_request(
                request,
                streaming=True,
                on_text_delta=lambda _delta: None,
                should_cancel=should_cancel,
            )
        else:
            raw_response = self._send_prepared_request(request)

        try:
            raw_reply = self._raw_reply_from_response(raw_response)
        except (AttributeError, IndexError, KeyError, TypeError) as exc:
            raise _request_error(exc, failure_class="malformed_response") from exc
        assistant = raw_reply["assistant"]
        reply = {
            "content": assistant.get("content") or "",
            "usage": raw_reply.get("usage"),
        }

        # Text-only callers historically received content and usage. Preserve
        # those fields while exposing the same provider-neutral diagnostics
        # already retained for ordinary turns in ``raw_reply``.
        if assistant.get("reasoning"):
            reply["reasoning"] = assistant["reasoning"]
        if "finish_reason" in raw_reply:
            reply["finish_reason"] = raw_reply["finish_reason"]

        return reply

    def complete_text_streaming(
        self,
        messages: list[dict],
        on_text_delta,
        trace_callback=None,
        please_stop_contemplating_the_void: bool = False,
    ) -> str:
        if not self.supports_streaming():
            content = self.complete_text(
                messages,
                please_stop_contemplating_the_void=please_stop_contemplating_the_void,
            )["content"]
            if content:
                on_text_delta(content)
            return content

        request_options = self.prepare_request(messages, [])
        transport_options = {"on_text_delta": on_text_delta}
        if trace_callback is not None:
            transport_options["trace_callback"] = trace_callback
        if please_stop_contemplating_the_void:
            request_options["please_stop_contemplating_the_void"] = True

        raw_response = self._send_prepared_request(
            request_options,
            streaming=True,
            **transport_options,
        )

        try:
            return raw_response.content or ""
        except (AttributeError, IndexError, KeyError, TypeError) as exc:
            raise _request_error(exc, failure_class="malformed_response") from exc

    # Build the tiny metadata map the adapter needs for validation from the
    # active tool definitions passed into this request.
    def _tool_metadata_from_definitions(self, tool_definitions: list[dict]) -> dict:
        metadata = {}
        for definition in tool_definitions:
            function = definition["function"]
            parameters = function.get("parameters", {})
            variants = parameters.get("oneOf", parameters.get("anyOf"))
            required_variants = (
                [variant.get("required", []) for variant in variants]
                if isinstance(variants, list)
                else [parameters.get("required", [])]
            )
            metadata[function["name"]] = {
                "required_variants": required_variants,
                "property_types": {
                    name: property_schema.get("type")
                    for name, property_schema in parameters.get(
                        "properties", {}
                    ).items()
                    if isinstance(property_schema, dict)
                },
            }
        return metadata

    def _messages_for_provider(self, messages: list[dict]) -> list[dict]:
        has_images = _request_has_image_assets({"messages": messages})
        allow_images = not has_images or self.supports_image_input()
        provider_messages = []
        for message in messages:
            provider_message = dict(message)
            content = provider_message.get("content")
            if not allow_images and isinstance(content, list):
                filtered_content = [
                    block
                    for block in content
                    if not (
                        isinstance(block, dict)
                        and block.get("type") == "image_asset"
                    )
                ]
                if len(filtered_content) != len(content):
                    # Historical image results remain canonical in the session.
                    # Only their request-only asset block is replaced, and the
                    # replacement tells the model that no visual inspection
                    # occurred.
                    filtered_content.append(
                        {"type": "text", "text": VISION_UNAVAILABLE_MESSAGE}
                    )
                    provider_message["content"] = filtered_content
            if (
                provider_message.get("role") == "assistant"
                and provider_message.get("tool_calls")
                and not provider_message.get("content")
            ):
                if self.tool_call_content_mode == "omit":
                    provider_message.pop("content", None)
                elif self.tool_call_content_mode == "empty":
                    provider_message["content"] = ""
                elif self.tool_call_content_mode == "null":
                    provider_message["content"] = None

            provider_messages.append(provider_message)

        return provider_messages

    # Convert the raw provider response into a consistent internal format.
    # The result contains:
    # - one assistant message dict
    # - a list of normalized tool actions
    # - an optional format_error field when parsing fails upstream
    #
    # Important:
    # We preserve the assistant tool_calls metadata on the transcript message
    # so stricter model templates can reconstruct valid assistant/tool turns
    # on later requests.
    def _normalize_response(self, raw_response: ModelResponse, tool_metadata: dict) -> dict:
        message = {
            "role": "assistant",
            "content": "",
        }
        actions = []

        raw_tool_calls = raw_response.tool_calls

        if raw_response.content:
            message["content"] = raw_response.content

        # Parse and validate each provider tool call before accepting it as a
        # real internal action. This keeps malformed tool output from slipping
        # into the loop as if it were valid.
        for tool_call in raw_tool_calls:
            tool_name = tool_call.name
            raw_arguments = tool_call.arguments
            try:
                args = self._parse_args(raw_arguments)
                self._validate_tool_action(tool_name, args, tool_metadata)
            except ValueError as error:
                error.recoverable_tool_name = tool_name
                error.recoverable_raw_arguments = raw_arguments
                raise

            actions.append(
                {
                    "tool": tool_name,
                    "args": args,
                    "tool_call_id": tool_call.id,
                    "raw_arguments": raw_arguments,
                }
            )

        # Construct the canonical replay structure from generic tool calls.
        # This is important for replaying transcript history in a format that
        # stricter OpenAI-compatible model templates accept.
        if raw_tool_calls:
            message["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.name,
                        "arguments": tool_call.arguments,
                    },
                }
                for tool_call in raw_tool_calls
            ]

        return {
            "message": message,
            "actions": actions,
            "format_error": "",
            "usage": self._usage_from_response(raw_response),
        }

    def _usage_from_response(self, response: ModelResponse) -> dict | None:
        return asdict(response.usage) if response.usage is not None else None

    def _raw_reply_from_response(self, response: ModelResponse) -> dict:
        """Capture normalized diagnostics before Core validates any tool calls."""
        # This is a historical diagnostic key, not a raw transport payload.
        # Provider extras belong in provider tracing, never in session replies.
        assistant = {
            "content": response.content,
            "raw_tool_calls": [asdict(call) for call in response.tool_calls],
        }
        if response.reasoning is not None:
            assistant["reasoning"] = response.reasoning
        reply = {
            "provider": self.provider_name,
            "model": self.model_name,
            "assistant": assistant,
        }
        if response.usage is not None:
            reply["usage"] = self._usage_from_response(response)
        if response.finish_reason is not None:
            reply["finish_reason"] = response.finish_reason
        if response.response_id is not None:
            reply["response_id"] = response.response_id
        return reply

    def _reply_from_response(self, raw_response: ModelResponse, tool_metadata: dict) -> dict:
        raw_reply = self._raw_reply_from_response(raw_response)
        try:
            reply = self._normalize_response(raw_response, tool_metadata)
        except ValueError as error:
            reply = {
                "message": {
                    "role": "assistant",
                    "content": raw_reply["assistant"].get("content") or "",
                },
                "actions": [],
                "format_error": str(error),
                "usage": raw_reply.get("usage"),
                **self._recoverable_tool_fields(error),
            }

        # Providers have already resolved their reasoning aliases.
        assistant = raw_reply["assistant"]
        if assistant.get("reasoning"):
            reply["reasoning"] = assistant["reasoning"]
        reply["raw_reply"] = raw_reply
        return reply

    def _reply_from_response_or_error(self, raw_response: ModelResponse, tool_metadata: dict) -> dict:
        """Identify structurally unusable provider responses at the adapter seam."""

        try:
            return self._reply_from_response(raw_response, tool_metadata)
        except (AttributeError, IndexError, KeyError, TypeError) as exc:
            raise _request_error(exc, failure_class="malformed_response") from exc

    def _recoverable_tool_fields(self, error: ValueError) -> dict:
        fields = {}
        tool_name = getattr(error, "recoverable_tool_name", None)
        raw_arguments = getattr(error, "recoverable_raw_arguments", None)
        if tool_name is not None:
            fields["recoverable_tool_name"] = tool_name
        if raw_arguments is not None:
            fields["recoverable_raw_arguments"] = raw_arguments
        return fields

    # Parse the provider's tool-call arguments into a normal dictionary.
    # The completed-response contract keeps arguments as raw JSON strings.
    #
    # Reject non-object JSON so that malformed tool arguments are handled as
    # format errors instead of reaching tool execution in a half-valid shape.
    def _parse_args(self, arguments: str) -> dict:
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError as e:
            raise ValueError(f"Tool arguments were not valid JSON: {e}") from e

        if not isinstance(parsed, dict):
            raise ValueError("Tool arguments must decode to a JSON object")

        return parsed

    # Validate that a normalized tool action refers to a known tool and
    # includes one complete required-argument form from that tool's definition.
    #
    # This is intentionally shallow for now:
    # - known tool name
    # - required argument presence
    # - exact JSON boolean values for boolean schema properties
    #
    # Boolean validation is kept at this protocol boundary so malformed model
    # output cannot reach execution. In particular, Python's bool/int
    # relationship makes equality or membership checks unsafe here.
    def _validate_tool_action(
        self,
        tool_name: str,
        args: dict,
        tool_metadata: dict,
    ) -> None:
        if tool_name not in tool_metadata:
            raise ValueError(f"Unknown tool: {tool_name}")

        required_variants = tool_metadata[tool_name]["required_variants"]
        has_required_variant = any(
            all(name in args for name in required)
            for required in required_variants
        )
        if not has_required_variant:
            if len(required_variants) == 1:
                missing_args = [name for name in required_variants[0] if name not in args]
                missing = ", ".join(missing_args)
                raise ValueError(f"Missing required arguments for {tool_name}: {missing}")

            expected = " or ".join(
                f"({', '.join(required)})" for required in required_variants
            )
            raise ValueError(f"Arguments for {tool_name} must include one of: {expected}")

        for name, expected_type in tool_metadata[tool_name]["property_types"].items():
            if (
                expected_type == "boolean"
                and name in args
                and not isinstance(args[name], bool)
            ):
                raise ValueError(
                    f"Invalid argument type for {tool_name}.{name}: expected boolean"
                )
