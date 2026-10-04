"""Stateless generation uses ready adapters without selecting transports."""

import inspect

import pytest

import squid_core.generation as generation
from squid_core.model_response import ModelResponse
from squid_core.model_adapter import ModelAdapter, ModelRequestError


FakeResponse = ModelResponse


class FakeProvider:
    def __init__(self, content: str = "generated text") -> None:
        self.content = content
        self.requests = []

    def create_response(self, model: str, messages: list[dict], tools: list[dict]):
        self.requests.append({"model": model, "messages": messages, "tools": tools})
        return FakeResponse(self.content)


class StreamingProvider(FakeProvider):
    def __init__(self, content: str, deltas: list[str]) -> None:
        super().__init__(content)
        self.deltas = deltas

    def supports_streaming(self) -> bool:
        return True

    def stream_chat(self, model: str, messages: list[dict], tools: list[dict], on_text_delta):
        self.requests.append({"model": model, "messages": messages, "tools": tools})
        for delta in self.deltas:
            on_text_delta(delta)
        return FakeResponse(self.content)


class FailingProvider:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def create_response(self, model: str, messages: list[dict], tools: list[dict]):
        raise self.error


def test_string_input_becomes_user_message_and_request_uses_no_tools() -> None:
    provider = FakeProvider("a short title")

    text = generation.generate_text("Give me a title.", model_adapter=ModelAdapter(provider, "generation-model"))

    assert text == "a short title"
    assert provider.requests == [
        {
            "model": "generation-model",
            "messages": [{"role": "user", "content": "Give me a title."}],
            "tools": [],
        }
    ]


def test_reasoning_control_defaults_to_false() -> None:
    assert inspect.signature(generation.generate_text).parameters[
        "please_stop_contemplating_the_void"
    ].default is False
    assert inspect.signature(generation.stream_text).parameters[
        "please_stop_contemplating_the_void"
    ].default is False


def test_generate_text_forwards_requested_reasoning_control() -> None:
    calls = []

    class CapturingAdapter:
        def complete_text(self, messages, **options):
            calls.append((messages, options))
            return {"content": "done", "usage": None}


    generation.generate_text(
        "Hello",
        model_adapter=CapturingAdapter(),
        please_stop_contemplating_the_void=True,
    )

    assert calls[0][1] == {"please_stop_contemplating_the_void": True}


def test_message_list_is_passed_through_to_model_adapter() -> None:
    messages = [
        {"role": "system", "content": "Be helpful."},
        {"role": "user", "content": "Hello"},
    ]
    calls = []

    class CapturingAdapter:
        def complete_text(self, supplied_messages: list[dict]) -> dict:
            calls.append(supplied_messages)
            return {"content": "Hello!", "usage": None}


    assert generation.generate_text(messages, model_adapter=CapturingAdapter()) == "Hello!"
    assert calls == [messages]
    assert calls[0] is messages


def test_generation_rejects_settings_only_construction() -> None:
    with pytest.raises(TypeError, match="settings"):
        generation.generate_text("Hello", settings=object())
    with pytest.raises(TypeError, match="settings"):
        generation.stream_text("Hello", lambda _delta: None, settings=object())


def test_generation_requires_explicit_adapter() -> None:
    with pytest.raises(TypeError, match="model_adapter"):
        generation.generate_text("Hello")
    with pytest.raises(TypeError, match="model_adapter"):
        generation.stream_text("Hello", lambda _delta: None)


def test_supplied_adapter_bypasses_all_construction(monkeypatch) -> None:
    class Adapter:
        def complete_text(self, messages, **_options):
            return {"content": messages[0]["content"], "usage": None}

    monkeypatch.setattr(generation, "ModelAdapter", lambda **kwargs: pytest.fail("adapter built"))
    assert generation.generate_text("direct", model_adapter=Adapter()) == "direct"


def test_model_request_failure_preserves_model_request_error() -> None:
    provider_error = RuntimeError("provider unavailable")
    adapter = ModelAdapter(FailingProvider(provider_error), "generation-model")
    with pytest.raises(ModelRequestError) as exc_info:
        generation.generate_text("Hello", model_adapter=adapter)
    assert str(exc_info.value) == "Model request failed: RuntimeError: provider unavailable"
    assert exc_info.value.__cause__ is provider_error


def test_stream_text_converts_string_input_forwards_deltas_and_returns_content() -> None:
    provider = StreamingProvider("a short title", ["a short ", "title"])
    deltas = []

    text = generation.stream_text(
        "Give me a title.",
        deltas.append,
        model_adapter=ModelAdapter(provider, "generation-model"),
    )

    assert text == "a short title"
    assert deltas == ["a short ", "title"]
    assert provider.requests == [
        {
            "model": "generation-model",
            "messages": [{"role": "user", "content": "Give me a title."}],
            "tools": [],
        }
    ]


def test_stream_text_forwards_requested_reasoning_control() -> None:
    calls = []

    class CapturingAdapter:
        def complete_text_streaming(self, messages, callback, **options):
            calls.append((messages, options))
            callback("done")
            return "done"


    generation.stream_text(
        "Hello",
        lambda _delta: None,
        model_adapter=CapturingAdapter(),
        please_stop_contemplating_the_void=True,
    )

    assert calls[0][1] == {"please_stop_contemplating_the_void": True}


def test_stream_text_passes_message_list_through_unchanged() -> None:
    messages = [
        {"role": "system", "content": "Be helpful."},
        {"role": "user", "content": "Hello"},
    ]
    calls = []

    class CapturingAdapter:
        def complete_text_streaming(self, supplied_messages, on_text_delta) -> str:
            calls.append(supplied_messages)
            on_text_delta("Hello!")
            return "Hello!"

    deltas = []

    assert generation.stream_text(messages, deltas.append, model_adapter=CapturingAdapter()) == "Hello!"
    assert deltas == ["Hello!"]
    assert calls == [messages]
    assert calls[0] is messages


def test_stream_text_falls_back_when_provider_does_not_support_streaming() -> None:
    provider = FakeProvider("completed response")
    deltas = []

    text = generation.stream_text("Hello", deltas.append, model_adapter=ModelAdapter(provider, "generation-model"))

    assert text == "completed response"
    assert deltas == ["completed response"]


def test_stream_text_preserves_model_request_error_chaining() -> None:
    provider_error = RuntimeError("provider unavailable")

    class FailingStreamingProvider:
        def supports_streaming(self):
            return True

        def stream_chat(self, **kwargs):
            raise provider_error

    adapter = ModelAdapter(FailingStreamingProvider(), "generation-model")
    with pytest.raises(ModelRequestError) as exc_info:
        generation.stream_text("Hello", lambda _delta: None, model_adapter=adapter)
    assert str(exc_info.value) == "Model request failed: RuntimeError: provider unavailable"
    assert exc_info.value.__cause__ is provider_error


def test_generation_module_has_no_agent_or_session_dependencies() -> None:
    assert "AgentLoop" not in generation.__dict__
    assert "CodingAgent" not in generation.__dict__
    assert "Session" not in generation.__dict__
    assert "ToolExecutor" not in generation.__dict__
