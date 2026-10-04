"""Enforce reusable Core dependency and provider ownership boundaries."""

from pathlib import Path
import subprocess
import sys
import squid_core


def test_core_imports_without_application_or_transports():
    script = """
import sys
class BlockImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"code_squid", "squid_providers", "openai", "httpx"}:
            raise AssertionError(fullname)
sys.meta_path.insert(0, BlockImports())
import squid_core.spawner
import squid_core.generation
from squid_core.providers import BaseProvider, StreamCancelled
from squid_core.model_response import ModelResponse, ToolCall, TokenUsage
assert all((BaseProvider, StreamCancelled, ModelResponse, ToolCall, TokenUsage))
from squid_core.hooks import HookRunner, HookResponse, ToolResultContext
class HostHook:
    def tool_result(self, ctx):
        return HookResponse(updated_tool_output={**ctx.result, "host": True})
result = HookRunner([HostHook()]).tool_result(
    ToolResultContext(None, ".", {"tool": "custom"}, {"ok": True})
)
assert result.updated_tool_output == {"ok": True, "host": True}
"""
    subprocess.run([sys.executable, "-c", script], check=True)


def test_core_contains_no_provider_catalog_or_transport_dependencies():
    forbidden = ("ollama", "lm_studio", "openrouter", "squid_models", "openai_compatible",
                 "squid_providers", "OpenAICompatibleProvider", "OllamaProvider")
    for path in Path(squid_core.__file__).parent.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert not any(name in source for name in forbidden), path


def test_core_hooks_contain_only_generic_mechanisms():
    """Application policy must not creep back into Core's public hook package."""
    import squid_core.hooks as hooks

    forbidden = ("RepeatedFailureNudge", "TooManyReadsNudge", "PostVerificationNudge")
    assert not any(hasattr(hooks, name) for name in forbidden)
    for path in Path(hooks.__file__).parent.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "code_squid" not in source, path
        assert not any(name in source for name in forbidden), path
