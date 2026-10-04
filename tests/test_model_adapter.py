"""Core request preparation, generic response validation, and failure handling."""

from squid_core.model_response import ModelResponse, TokenUsage, ToolCall

import pytest
from types import SimpleNamespace

from squid_core.model_adapter import (
    ModelAdapter,
    ModelRequestError,
    model_request_error_details,
)
from squid_core.tools.builtins.apply_patch import tool_definition as apply_patch_definition
from squid_core.tools.builtins.run_command import tool_definition as run_command_definition


FakeToolCall = ToolCall


class FakeMessage:
    def __init__(self, content, tool_calls):
        self.content = content
        self.tool_calls = tool_calls or []
        self.reasoning = None


def FakeResponse(message, usage=None, finish_reason=None):
    return ModelResponse(message.content, reasoning=message.reasoning,
                         tool_calls=message.tool_calls, usage=usage,
                         finish_reason=finish_reason)


class FakeProvider:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests = []

    def create_response(self, model: str, messages: list[dict], tools: list[dict]) -> FakeResponse:
        self.requests.append(
            {
                "model": model,
                "messages": messages,
                "tools": tools,
            }
        )
        return self.response


class StreamingProvider(FakeProvider):
    def __init__(
        self,
        response: FakeResponse,
        deltas: list[str],
        reasoning_deltas: list[str] | None = None,
        tool_preparing_events: list[dict] | None = None,
    ) -> None:
        super().__init__(response)
        self.deltas = deltas
        self.reasoning_deltas = reasoning_deltas or []
        self.tool_preparing_events = tool_preparing_events or []

    def supports_streaming(self) -> bool:
        return True

    def stream_chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        on_text_delta,
        on_reasoning_delta=None,
        on_tool_preparing=None,
        should_cancel=None,
    ) -> FakeResponse:
        self.requests.append(
            {
                "model": model,
                "messages": messages,
                "tools": tools,
            }
        )
        if on_reasoning_delta is not None:
            for delta in self.reasoning_deltas:
                on_reasoning_delta(delta)
        for delta in self.deltas:
            if should_cancel is not None and should_cancel():
                from squid_core.providers.base import StreamCancelled

                raise StreamCancelled
            on_text_delta(delta)
        if on_tool_preparing is not None:
            for event in self.tool_preparing_events:
                on_tool_preparing(event)
        return self.response


class FailingProvider:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def create_response(self, model: str, messages: list[dict], tools: list[dict]) -> FakeResponse:
        raise self.error


def tool_definition(name: str, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {
                "type": "object",
                "properties": {},
                "required": required,
            },
        },
    }


def adapter_response(
    tool_call: FakeToolCall,
    tool_definitions: list[dict],
    content: str | None = None,
) -> dict:
    provider = FakeProvider(FakeResponse(FakeMessage(content=content, tool_calls=[tool_call])))
    adapter = ModelAdapter(provider=provider, model_name="test-model")

    return adapter.respond(messages=[{"role": "user", "content": "hi"}], tool_definitions=tool_definitions)


def test_prepared_request_is_the_representation_handed_to_provider() -> None:
    provider = FakeProvider(FakeResponse(FakeMessage(content="done", tool_calls=[])))
    observed = []
    adapter = ModelAdapter(
        provider=provider,
        model_name="test-model",
        tool_call_content_mode="null",
        request_observer=observed.append,
    )
    tools = [tool_definition("local_tool", [])]
    prepared = adapter.prepare_request(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1"}],
            }
        ],
        tools,
    )

    adapter.respond_prepared(prepared)

    assert prepared["messages"][0]["content"] is None
    assert observed == [prepared]
    assert observed[0] is prepared
    assert provider.requests[0]["messages"] is prepared["messages"]
    assert provider.requests[0]["tools"] is prepared["tools"]


def test_prepared_text_request_is_the_representation_handed_to_provider() -> None:
    provider = FakeProvider(FakeResponse(FakeMessage(content="summary", tool_calls=[])))
    observed = []
    adapter = ModelAdapter(
        provider=provider,
        model_name="test-model",
        request_observer=observed.append,
    )
    prepared = adapter.prepare_request(
        [{"role": "user", "content": "compact this"}],
        [],
    )

    response = adapter.complete_text_prepared(prepared)

    assert response["content"] == "summary"
    assert observed == [prepared]
    assert observed[0] is prepared
    assert provider.requests[0]["messages"] is prepared["messages"]
    assert provider.requests[0]["tools"] is prepared["tools"]


def test_request_observer_failure_does_not_block_provider_transport() -> None:
    provider = FakeProvider(FakeResponse(FakeMessage(content="done", tool_calls=[])))

    def fail_observation(_request: dict) -> None:
        raise OSError("diagnostic storage unavailable")

    adapter = ModelAdapter(
        provider=provider,
        model_name="test-model",
        request_observer=fail_observation,
    )

    reply = adapter.respond(
        messages=[{"role": "user", "content": "continue"}],
        tool_definitions=[],
    )

    assert reply["message"]["content"] == "done"
    assert len(provider.requests) == 1


def test_valid_tool_call_succeeds_when_tool_is_passed_in_definitions() -> None:
    reply = adapter_response(
        FakeToolCall("call_1", "local_tool", '{"path": "/tmp/input.txt"}'),
        [tool_definition("local_tool", ["path"])],
    )

    assert reply["format_error"] == ""
    assert reply["message"]["content"] == ""
    assert reply["actions"] == [
        {
            "tool": "local_tool",
            "args": {"path": "/tmp/input.txt"},
            "tool_call_id": "call_1",
            "raw_arguments": '{"path": "/tmp/input.txt"}',
        }
    ]


def test_tool_only_reply_does_not_get_fallback_narration() -> None:
    reply = adapter_response(
        FakeToolCall("call_1", "read_file", '{"path": "/tmp/input.txt"}'),
        [tool_definition("read_file", ["path"])],
    )

    assert reply["format_error"] == ""
    assert reply["message"]["content"] == ""
    assert reply["message"]["content"] != "Using read_file."


def test_provider_narration_is_preserved_with_tool_call() -> None:
    reply = adapter_response(
        FakeToolCall("call_1", "local_tool", '{"path": "/tmp/input.txt"}'),
        [tool_definition("local_tool", ["path"])],
        content="I need to inspect the input file first.",
    )

    assert reply["format_error"] == ""
    assert reply["message"]["content"] == "I need to inspect the input file first."


def test_unknown_tool_call_is_rejected_when_not_passed_in_definitions() -> None:
    reply = adapter_response(
        FakeToolCall("call_1", "missing_tool", "{}"),
        [tool_definition("local_tool", [])],
    )

    assert reply["actions"] == []
    assert reply["format_error"] == "Unknown tool: missing_tool"


def test_global_registry_tool_is_rejected_when_not_passed_in_definitions() -> None:
    reply = adapter_response(
        FakeToolCall("call_1", "read_file", '{"path": "/tmp/input.txt"}'),
        [tool_definition("local_tool", [])],
    )

    assert reply["actions"] == []
    assert reply["format_error"] == "Unknown tool: read_file"


def test_missing_required_args_are_rejected_from_passed_definitions() -> None:
    reply = adapter_response(
        FakeToolCall("call_1", "local_tool", '{"path": "/tmp/output.txt"}'),
        [tool_definition("local_tool", ["path", "content"])],
    )

    assert reply["actions"] == []
    assert reply["format_error"] == "Missing required arguments for local_tool: content"


def test_run_command_without_verification_is_rejected() -> None:
    reply = adapter_response(
        FakeToolCall("call_1", "run_command", '{"command": "pytest -q"}'),
        [run_command_definition()],
    )

    assert reply["actions"] == []
    assert reply["format_error"] == (
        "Missing required arguments for run_command: verification"
    )


@pytest.mark.parametrize("verification", [True, False])
def test_run_command_accepts_boolean_verification(verification: bool) -> None:
    arguments = f'{{"command": "pytest -q", "verification": {str(verification).lower()}}}'

    reply = adapter_response(
        FakeToolCall("call_1", "run_command", arguments),
        [run_command_definition()],
    )

    assert reply["format_error"] == ""
    assert reply["actions"][0]["args"]["verification"] is verification


@pytest.mark.parametrize(
    "invalid_verification",
    ['"true"', '"false"', "1", "0", "null", "[]", "{}"],
)
def test_run_command_rejects_non_boolean_verification_before_execution(
    invalid_verification: str,
) -> None:
    arguments = (
        '{"command": "must-not-execute", '
        f'"verification": {invalid_verification}}}'
    )

    reply = adapter_response(
        FakeToolCall("call_1", "run_command", arguments),
        [run_command_definition()],
    )

    # The normal malformed-tool-call shape carries no executable action.
    assert reply["actions"] == []
    assert reply["format_error"] == (
        "Invalid argument type for run_command.verification: expected boolean"
    )
    assert reply["recoverable_tool_name"] == "run_command"
    assert reply["recoverable_raw_arguments"] == arguments


@pytest.mark.parametrize(
    "arguments",
    [
        '{"path": "/tmp/input.txt", "old_text": "old", "new_text": "new"}',
        '{"match_id": "m1"}',
        (
            '{"path": "/tmp/input.txt", "old_text": "old", '
            '"new_text": "new", "match_id": "m1"}'
        ),
    ],
)
def test_tool_call_accepts_each_required_argument_variant(arguments: str) -> None:
    reply = adapter_response(
        FakeToolCall("call_1", "apply_patch", arguments),
        [apply_patch_definition()],
    )

    assert reply["format_error"] == ""
    assert len(reply["actions"]) == 1


def test_tool_call_rejects_arguments_missing_every_required_variant() -> None:
    reply = adapter_response(
        FakeToolCall("call_1", "apply_patch", '{"path": "/tmp/input.txt"}'),
        [apply_patch_definition()],
    )

    assert reply["actions"] == []
    assert reply["format_error"] == (
        "Arguments for apply_patch must include one of: "
        "(path, old_text, new_text) or (match_id)"
    )


def test_malformed_tool_arguments_preserve_raw_reply_and_available_metadata() -> None:
    message = FakeMessage(
        content="Trying the tool.",
        tool_calls=[FakeToolCall("call_bad", "local_tool", '{"path":')],
    )
    message.reasoning = "provider-exposed reasoning"
    response = FakeResponse(message, usage=TokenUsage(**{"input_tokens": 7, "output_tokens": 3}))
    response.response_id = "response_123"
    response.finish_reason = "tool_calls"
    adapter = ModelAdapter(
        provider=FakeProvider(response),
        model_name="test-model",
        provider_name="test-provider",
    )

    reply = adapter.respond(
        messages=[{"role": "user", "content": "hi"}],
        tool_definitions=[tool_definition("local_tool", ["path"])],
    )

    assert reply["format_error"].startswith("Tool arguments were not valid JSON:")
    assert reply["recoverable_tool_name"] == "local_tool"
    assert reply["recoverable_raw_arguments"] == '{"path":'
    assert reply["reasoning"] == "provider-exposed reasoning"
    assert reply["raw_reply"] == {
        "provider": "test-provider",
        "model": "test-model",
        "assistant": {
            "content": "Trying the tool.",
            "raw_tool_calls": [
                {
                    "id": "call_bad",
                    "name": "local_tool",
                    "arguments": '{"path":',
                }
            ],
            "reasoning": "provider-exposed reasoning",
        },
        "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": None},
        "finish_reason": "tool_calls",
        "response_id": "response_123",
    }


def test_raw_reply_does_not_invent_unavailable_reasoning_or_metadata() -> None:
    provider = FakeProvider(FakeResponse(FakeMessage(content="done", tool_calls=[])))
    reply = ModelAdapter(provider=provider, model_name="test-model").respond(
        messages=[],
        tool_definitions=[],
    )

    assert reply["raw_reply"]["assistant"] == {
        "content": "done",
        "raw_tool_calls": [],
    }
    assert "finish_reason" not in reply["raw_reply"]
    assert "response_id" not in reply["raw_reply"]
    assert "usage" not in reply["raw_reply"]
    assert "reasoning" not in reply


def test_ordinary_reply_normalizes_provider_reasoning_content() -> None:
    message = FakeMessage(content="done", tool_calls=[])
    message.reasoning = "provider reasoning"
    provider = FakeProvider(FakeResponse(message))

    reply = ModelAdapter(provider=provider, model_name="test-model").respond(
        messages=[],
        tool_definitions=[],
    )

    assert reply["reasoning"] == "provider reasoning"


def test_assistant_tool_calls_metadata_is_preserved_on_normalized_message() -> None:
    reply = adapter_response(
        FakeToolCall("call_1", "local_tool", '{"path": "/tmp/input.txt"}'),
        [tool_definition("local_tool", ["path"])],
    )

    assert reply["message"]["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "local_tool",
                "arguments": '{"path": "/tmp/input.txt"}',
            },
        }
    ]


def test_multiple_raw_tool_calls_preserve_each_argument_payload_when_normalized() -> None:
    raw_arguments = [
        '{"path": "/tmp/index.html", "content": "<main>game</main>"}',
        '{"path": "/tmp/style.css", "content": "main { color: cyan; }"}',
    ]
    response = FakeResponse(
        FakeMessage(
            content="Writing both files.",
            tool_calls=[
                FakeToolCall("call_1", "write_file", raw_arguments[0]),
                FakeToolCall("call_2", "write_file", raw_arguments[1]),
            ],
        )
    )
    adapter = ModelAdapter(
        provider=FakeProvider(response),
        model_name="test-model",
        provider_name="openrouter",
    )

    reply = adapter.respond(
        messages=[{"role": "user", "content": "write files"}],
        tool_definitions=[tool_definition("write_file", ["path", "content"])],
    )

    assert [
        call["arguments"]
        for call in reply["raw_reply"]["assistant"]["raw_tool_calls"]
    ] == raw_arguments
    assert [
        call["function"]["arguments"] for call in reply["message"]["tool_calls"]
    ] == raw_arguments
    assert reply["actions"] == [
        {
            "tool": "write_file",
            "args": {"path": "/tmp/index.html", "content": "<main>game</main>"},
            "tool_call_id": "call_1",
            "raw_arguments": raw_arguments[0],
        },
        {
            "tool": "write_file",
            "args": {"path": "/tmp/style.css", "content": "main { color: cyan; }"},
            "tool_call_id": "call_2",
            "raw_arguments": raw_arguments[1],
        },
    ]


def test_provider_emitted_empty_first_call_is_preserved_as_raw_diagnostic() -> None:
    """Match the observed DeepSeek response without changing malformed-batch policy."""
    response = FakeResponse(
        FakeMessage(
            content="Writing both files.",
            tool_calls=[
                FakeToolCall("call_0", "write_file", "{}"),
                FakeToolCall(
                    "call_1",
                    "write_file",
                    '{"path": "/tmp/style.css", "content": "body {}"}',
                ),
            ],
        )
    )
    adapter = ModelAdapter(
        provider=FakeProvider(response),
        model_name="deepseek/deepseek-v4-flash-0731",
        provider_name="openrouter",
    )

    reply = adapter.respond(
        messages=[{"role": "user", "content": "write files"}],
        tool_definitions=[tool_definition("write_file", ["path", "content"])],
    )

    assert reply["actions"] == []
    assert reply["format_error"] == (
        "Missing required arguments for write_file: path, content"
    )
    assert reply["recoverable_raw_arguments"] == "{}"
    assert [
        call["arguments"]
        for call in reply["raw_reply"]["assistant"]["raw_tool_calls"]
    ] == [
        "{}",
        '{"path": "/tmp/style.css", "content": "body {}"}',
    ]


def test_default_tool_call_replay_mode_sends_empty_content() -> None:
    provider = FakeProvider(FakeResponse(FakeMessage(content="", tool_calls=[])))
    adapter = ModelAdapter(provider=provider, model_name="test-model")

    adapter.respond(
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1"}],
            }
        ],
        tool_definitions=[],
    )

    assert provider.requests[0]["messages"][0]["content"] == ""


def test_null_tool_call_replay_mode_sends_null_content() -> None:
    provider = FakeProvider(FakeResponse(FakeMessage(content="", tool_calls=[])))
    adapter = ModelAdapter(
        provider=provider,
        model_name="test-model",
        tool_call_content_mode="null",
    )

    adapter.respond(
        messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1"}],
            }
        ],
        tool_definitions=[],
    )

    assert provider.requests[0]["messages"][0]["content"] is None


def test_omit_tool_call_replay_mode_removes_blank_content() -> None:
    provider = FakeProvider(FakeResponse(FakeMessage(content="", tool_calls=[])))
    adapter = ModelAdapter(
        provider=provider,
        model_name="test-model",
        tool_call_content_mode="omit",
    )

    adapter.respond(
        messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1"}],
            }
        ],
        tool_definitions=[],
    )

    assert "content" not in provider.requests[0]["messages"][0]


def test_non_empty_tool_call_replay_content_is_preserved() -> None:
    provider = FakeProvider(FakeResponse(FakeMessage(content="", tool_calls=[])))
    adapter = ModelAdapter(
        provider=provider,
        model_name="test-model",
        tool_call_content_mode="null",
    )

    adapter.respond(
        messages=[
            {
                "role": "assistant",
                "content": "I need to inspect the file.",
                "tool_calls": [{"id": "call_1"}],
            }
        ],
        tool_definitions=[],
    )

    assert provider.requests[0]["messages"][0]["content"] == "I need to inspect the file."


def test_provider_exception_is_wrapped_as_model_request_error() -> None:
    provider_error = RuntimeError("provider unavailable")
    adapter = ModelAdapter(provider=FailingProvider(provider_error), model_name="test-model")

    with pytest.raises(ModelRequestError) as exc_info:
        adapter.respond(messages=[{"role": "user", "content": "hi"}], tool_definitions=[])

    assert str(exc_info.value) == (
        "Model request failed: RuntimeError: provider unavailable"
    )
    assert exc_info.value.__cause__ is provider_error


def test_malformed_provider_response_has_precise_failure_class() -> None:
    response = FakeResponse(FakeMessage(content="unused", tool_calls=[]))
    response = object()
    adapter = ModelAdapter(provider=FakeProvider(response), model_name="test-model")

    with pytest.raises(ModelRequestError) as exc_info:
        adapter.respond(messages=[{"role": "user", "content": "hi"}], tool_definitions=[])

    details = model_request_error_details(exc_info.value)
    assert details["failure_class"] == "malformed_response"
    assert details["underlying_error_type"] == "TypeError"
    assert details["_transient"] is False


@pytest.mark.parametrize("response", [
    None,
    {"content": "done"},
    SimpleNamespace(**vars(ModelResponse("done"))),
])
@pytest.mark.parametrize("path", [
    "respond", "complete_text", "respond_streaming", "complete_text_streaming",
])
def test_all_transport_paths_require_model_response(response, path):
    provider = StreamingProvider(response, [])
    adapter = ModelAdapter(provider, "test-model")
    kwargs = {"messages": [{"role": "user", "content": "hi"}]}
    if path.startswith("respond"):
        kwargs["tool_definitions"] = []
    if "streaming" in path:
        kwargs["on_text_delta"] = lambda _delta: None

    with pytest.raises(ModelRequestError, match="Provider must return ModelResponse") as caught:
        getattr(adapter, path)(**kwargs)

    details = model_request_error_details(caught.value)
    assert details["failure_class"] == "malformed_response"
    assert details["underlying_error_type"] == "TypeError"
    assert details["_transient"] is False


def test_complete_text_returns_assistant_content() -> None:
    provider = FakeProvider(FakeResponse(FakeMessage(content="summary", tool_calls=[])))
    adapter = ModelAdapter(provider=provider, model_name="test-model")

    reply = adapter.complete_text(messages=[{"role": "user", "content": "summarize"}])

    assert reply["content"] == "summary"


def test_complete_text_does_not_expose_tools() -> None:
    provider = FakeProvider(FakeResponse(FakeMessage(content="summary", tool_calls=[])))
    adapter = ModelAdapter(provider=provider, model_name="test-model")

    adapter.complete_text(messages=[{"role": "user", "content": "summarize"}])

    assert provider.requests[0]["tools"] == []


def test_respond_includes_normalized_usage_when_provider_usage_exists() -> None:
    provider = FakeProvider(
        FakeResponse(
            FakeMessage(content="done", tool_calls=[]),
            usage=TokenUsage(**{"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}),
        )
    )
    adapter = ModelAdapter(provider=provider, model_name="test-model")

    reply = adapter.respond(messages=[{"role": "user", "content": "hi"}], tool_definitions=[])

    assert reply["usage"] == {
        "input_tokens": 10,
        "output_tokens": 4,
        "total_tokens": 14,
    }


def test_complete_text_includes_normalized_usage_when_provider_usage_exists() -> None:
    provider = FakeProvider(
        FakeResponse(
            FakeMessage(content="summary", tool_calls=[]),
            usage=TokenUsage(**{"input_tokens": 20, "output_tokens": 6, "total_tokens": 26}),
        )
    )
    adapter = ModelAdapter(provider=provider, model_name="test-model")

    reply = adapter.complete_text(messages=[{"role": "user", "content": "summarize"}])

    assert reply["usage"] == {
        "input_tokens": 20,
        "output_tokens": 6,
        "total_tokens": 26,
    }


def test_complete_text_exposes_normalized_reasoning_and_finish_reason() -> None:
    message = FakeMessage(content="summary", tool_calls=[])
    message.reasoning = "provider reasoning"
    provider = FakeProvider(FakeResponse(message, finish_reason="stop"))
    adapter = ModelAdapter(provider=provider, model_name="test-model")

    reply = adapter.complete_text(messages=[{"role": "user", "content": "summarize"}])

    assert reply["reasoning"] == "provider reasoning"
    assert reply["finish_reason"] == "stop"


def test_complete_text_uses_stream_transport_when_cancellation_is_supplied() -> None:
    provider = StreamingProvider(
        FakeResponse(FakeMessage(content="summary", tool_calls=[])),
        deltas=["sum", "mary"],
    )
    adapter = ModelAdapter(provider=provider, model_name="test-model")

    reply = adapter.complete_text(
        messages=[{"role": "user", "content": "summarize"}],
        should_cancel=lambda: False,
    )

    assert reply["content"] == "summary"
    assert provider.requests == [
        {
            "model": "test-model",
            "messages": [{"role": "user", "content": "summarize"}],
            "tools": [],
        }
    ]


def test_complete_text_streaming_forwards_deltas_returns_content_and_disables_tools() -> None:
    provider = StreamingProvider(
        FakeResponse(FakeMessage(content="hello world", tool_calls=[])),
        deltas=["hello ", "world"],
    )
    observed = []
    adapter = ModelAdapter(
        provider=provider,
        model_name="test-model",
        request_observer=observed.append,
    )
    deltas = []

    content = adapter.complete_text_streaming(
        messages=[{"role": "user", "content": "hi"}],
        on_text_delta=deltas.append,
    )

    assert deltas == ["hello ", "world"]
    assert content == "hello world"
    assert provider.requests[0]["tools"] == []
    assert observed == [provider.requests[0]]


def test_complete_text_streaming_falls_back_to_completed_text() -> None:
    provider = FakeProvider(FakeResponse(FakeMessage(content="fallback", tool_calls=[])))
    adapter = ModelAdapter(provider=provider, model_name="test-model")
    deltas = []

    content = adapter.complete_text_streaming(
        messages=[{"role": "user", "content": "hi"}],
        on_text_delta=deltas.append,
    )

    assert content == "fallback"
    assert deltas == ["fallback"]
    assert provider.requests[0]["tools"] == []


def test_complete_text_streaming_wraps_provider_error() -> None:
    provider_error = RuntimeError("provider unavailable")

    class FailingStreamingProvider:
        def supports_streaming(self) -> bool:
            return True

        def stream_chat(self, **kwargs):
            raise provider_error

    adapter = ModelAdapter(provider=FailingStreamingProvider(), model_name="test-model")

    with pytest.raises(ModelRequestError) as exc_info:
        adapter.complete_text_streaming(
            messages=[{"role": "user", "content": "hi"}],
            on_text_delta=lambda _delta: None,
        )

    assert str(exc_info.value) == (
        "Model request failed: RuntimeError: provider unavailable"
    )
    assert exc_info.value.__cause__ is provider_error


def test_supports_streaming_delegates_to_provider() -> None:
    provider = StreamingProvider(
        FakeResponse(FakeMessage(content="done", tool_calls=[])),
        deltas=[],
    )
    adapter = ModelAdapter(provider=provider, model_name="test-model")

    assert adapter.supports_streaming() is True


def test_respond_streaming_emits_deltas_and_returns_normalized_reply() -> None:
    deltas = []
    observed = []
    provider = StreamingProvider(
        FakeResponse(FakeMessage(content="hello world", tool_calls=[])),
        deltas=["hello ", "world"],
    )
    adapter = ModelAdapter(
        provider=provider,
        model_name="test-model",
        request_observer=observed.append,
    )

    reply = adapter.respond_streaming(
        messages=[{"role": "user", "content": "hi"}],
        tool_definitions=[],
        on_text_delta=deltas.append,
    )

    assert deltas == ["hello ", "world"]
    assert reply["message"]["content"] == "hello world"
    assert reply["actions"] == []
    assert observed == [provider.requests[0]]


def test_respond_streaming_forwards_reasoning_deltas_when_provider_supports_them() -> None:
    message = FakeMessage(content="done", tool_calls=[])
    message.reasoning = "first second"
    provider = StreamingProvider(
        FakeResponse(message),
        deltas=["done"],
        reasoning_deltas=["first ", "second"],
    )
    adapter = ModelAdapter(provider=provider, model_name="test-model")
    reasoning_deltas = []

    reply = adapter.respond_streaming(
        messages=[],
        tool_definitions=[],
        on_text_delta=lambda _delta: None,
        on_reasoning_delta=reasoning_deltas.append,
    )

    assert reasoning_deltas == ["first ", "second"]
    assert reply["reasoning"] == "first second"


def test_respond_streaming_forwards_tool_preparing_callback() -> None:
    preparing_events = []
    provider = StreamingProvider(
        FakeResponse(FakeMessage(content="", tool_calls=[])),
        deltas=[],
        tool_preparing_events=[
            {
                "tool": "write_file",
                "tool_call_index": 0,
                "tool_call_id": "call_1",
            }
        ],
    )
    adapter = ModelAdapter(provider=provider, model_name="test-model")

    adapter.respond_streaming(
        messages=[{"role": "user", "content": "hi"}],
        tool_definitions=[],
        on_text_delta=lambda _delta: None,
        on_tool_preparing=preparing_events.append,
    )

    assert preparing_events == [
        {
            "tool": "write_file",
            "tool_call_index": 0,
            "tool_call_id": "call_1",
        }
    ]


def test_core_diagnostics_preserve_normalized_fields_before_whole_batch_validation():
    response = ModelResponse(content="batch", reasoning="thought", tool_calls=[
        ToolCall(id="a", name="read_file", arguments='{"path":"ok"}'),
        ToolCall(id="b", name="read_file", arguments='{"path":'),
    ])
    reply = ModelAdapter(FakeProvider(response), "model").respond([], [tool_definition("read_file", ["path"])])
    assert reply["actions"] == []
    assert reply["format_error"].startswith("Tool arguments were not valid JSON:")
    assert reply["recoverable_raw_arguments"] == '{"path":'
    assert reply["raw_reply"]["assistant"] == {
        "content": "batch", "reasoning": "thought", "raw_tool_calls": [
            {"id": "a", "name": "read_file", "arguments": '{"path":"ok"}'},
            {"id": "b", "name": "read_file", "arguments": '{"path":'},
        ],
    }


def test_plain_text_invalid_generic_response_is_wrapped():
    adapter = ModelAdapter(FakeProvider(object()), "model")
    with pytest.raises(ModelRequestError) as caught:
        adapter.complete_text([])
    assert model_request_error_details(caught.value)["failure_class"] == "malformed_response"
