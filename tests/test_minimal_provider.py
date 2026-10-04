"""The same provider boundary serves minimal objects and optional subclasses."""

import pytest

from squid_core.model_adapter import ModelAdapter
from squid_core.model_response import ModelResponse
from squid_core.providers import BaseProvider
from squid_core.settings import CompactionSettings, RuntimeSettings
from squid_core.spawner import build_agent_run


class MinimalProvider:
    """A backend with only the operation needed for ordinary inference."""

    def create_response(self, model, messages, tools) -> ModelResponse:
        assert model == "test-model"
        assert messages[-1] == {"role": "user", "content": "Hello"}
        assert tools == []
        return ModelResponse(content="Hello back")


class MinimalBaseProvider(MinimalProvider, BaseProvider):
    """Opt into the existing defaults without implementing more operations."""


@pytest.mark.parametrize("provider_type", [MinimalProvider, MinimalBaseProvider])
def test_minimal_provider_normal_and_text_inference(provider_type):
    adapter = ModelAdapter(provider_type(), "test-model")
    messages = [{"role": "user", "content": "Hello"}]

    assert adapter.supports_streaming() is False
    assert adapter.supports_image_input() is False
    assert adapter.respond(messages, [])["message"]["content"] == "Hello back"
    assert adapter.complete_text(messages, should_cancel=lambda: False) == {
        "content": "Hello back", "usage": None,
    }
    deltas = []
    assert adapter.complete_text_streaming(messages, deltas.append) == "Hello back"
    assert deltas == ["Hello back"]


@pytest.mark.parametrize("provider_type", [MinimalProvider, MinimalBaseProvider])
@pytest.mark.parametrize("stream_assistant_text", [False, True])
def test_default_agent_loop_uses_minimal_provider(
    tmp_path, provider_type, stream_assistant_text,
):
    settings = RuntimeSettings(
        approval_mode="never",
        max_tool_steps=1,
        stream_assistant_text=stream_assistant_text,
        compaction=CompactionSettings(enabled=False),
    )
    run = build_agent_run(
        tmp_path,
        "Hello",
        settings=settings,
        provider=provider_type(),
        model_name="test-model",
        sessions_dir=tmp_path / "sessions",
        undo_dir=tmp_path / "undo",
    )

    # Exercise the default Agent, adapter, and loop; discovery/readiness and
    # streaming are unnecessary even when the host requests streamed display.
    assert run.loop.run().status == "complete"
    assert run.session.messages[-1] == {
        "role": "assistant", "content": "Hello back",
    }
