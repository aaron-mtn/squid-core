"""Generic run construction requires caller-owned model collaborators."""

import json
from dataclasses import replace
from pathlib import Path
from squid_core.model_response import ModelResponse
from types import SimpleNamespace

import pytest

import squid_core.spawner as core_spawner
from squid_core.settings import (
    CompactionSettings,
    ReadFileSettings,
    RuntimeSettings,
)
from squid_core.session import Session
from squid_core.agent import Agent
from squid_core.storage import SessionStore, UndoStore
from squid_core.tools import ToolResults
from squid_core.tools.builtins import TOOLS
from squid_core.skills import parse_skill_file


@pytest.mark.parametrize("allowed,expected", [
    ("", tuple(TOOLS)),
    ("allowed-tools: []\n", ()),
    ("allowed-tools: read_file\n", ("read_file",)),
])
def test_standalone_skill_limits_default_execution_and_exposure(tmp_path, allowed, expected):
    path = tmp_path / "SKILL.md"
    path.write_text(
        f"---\nname: catalog\ndescription: Catalog resources\n{allowed}---\nCatalog.\n",
        encoding="utf-8",
    )
    skill = parse_skill_file(path)
    run = core_spawner.build_agent_run(
        workspace_root=tmp_path, task="catalog", settings=_settings(),
        skill=skill, model_adapter=object(), **_storage_paths(tmp_path),
    )
    assert tuple(run.loop.agent.tools) == expected
    assert tuple(run.loop.tool_executor.tools) == expected
    if "write_file" not in expected:
        assert run.loop.tool_executor.execute({"tool": "write_file"})["ok"] is False


def test_lower_level_loop_enforces_standalone_skill_permissions(tmp_path):
    from squid_core.skills import Skill

    skill = Skill("catalog", "Catalog", "Catalog", (), tmp_path / "SKILL.md", True)
    store = SessionStore.create(tmp_path / "sessions", "catalog", workspace_root=tmp_path)
    loop = core_spawner.build_agent_loop(
        workspace_root=tmp_path, session=Session(workspace_root=tmp_path),
        session_store=store, agent=Agent("catalog", skill=skill),
        undo_store=None, settings=_settings(), tools=TOOLS, model_adapter=object(),
    )
    assert loop.tool_executor.tools == {}


def _settings() -> RuntimeSettings:
    return RuntimeSettings(
        approval_mode="never",
        max_tool_steps=None,
        stream_assistant_text=False,
        compaction=CompactionSettings(
            enabled=False,
            trigger_input_tokens=25_000,
            compact_fraction=0.8,
            min_recent_chunks=2,
        ),
    )


def _storage_paths(tmp_path: Path) -> dict:
    return {
        "sessions_dir": tmp_path / "sessions",
        "undo_dir": tmp_path / "undo",
    }


def _provider_response(content: str = "done") -> ModelResponse:
    return ModelResponse(content, finish_reason="stop")


def test_generic_module_has_no_coding_or_terminal_ui_dependency() -> None:
    source = Path(core_spawner.__file__).read_text(encoding="utf-8")

    assert "code_squid.agent.coding" not in source
    assert "code_squid.agent.prompts" not in source
    assert "code_squid.console.ui.display" not in source
    assert "TerminalDisplay" not in source
    assert "squid_core.mods" not in source


def test_generic_run_builds_default_agent_and_ui_neutral_display(tmp_path: Path) -> None:
    adapter = object()

    run = core_spawner.build_agent_run(
        tmp_path,
        "Generic task",
        settings=_settings(),
        model_adapter=adapter,
        **_storage_paths(tmp_path),
    )

    assert type(run.loop.agent) is Agent
    assert run.loop.agent.task == "Generic task"
    assert run.loop.agent.tools is TOOLS
    assert run.loop.model_adapter is adapter
    assert type(run.loop.display) is core_spawner.NullDisplay


def test_generic_run_passes_read_file_sizing_to_default_executor(tmp_path: Path) -> None:
    read_file_settings = ReadFileSettings(
        default_max_lines=80,
        minimum_range_lines=20,
    )

    run = core_spawner.build_agent_run(
        tmp_path,
        "Sized reads",
        settings=replace(_settings(), read_file=read_file_settings),
        model_adapter=object(),
        **_storage_paths(tmp_path),
    )

    assert run.loop.tool_executor.read_file_settings is read_file_settings


def test_generic_run_passes_model_request_retry_settings(tmp_path: Path) -> None:
    settings = replace(
        _settings(),
        model_request_max_retries=4,
        model_request_retry_backoff_ms=75,
    )

    run = core_spawner.build_agent_run(
        tmp_path,
        "Retry settings",
        settings=settings,
        model_adapter=object(),
        **_storage_paths(tmp_path),
    )

    assert run.loop.model_request_max_retries == 4
    assert run.loop.model_request_retry_backoff_ms == 75


def test_generic_run_requires_explicit_storage_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sessions_dir is required"):
        core_spawner.build_agent_run(
            tmp_path,
            "Generic task",
            settings=_settings(),
            model_adapter=object(),
        )




def test_supplied_provider_is_used_without_default_provider(
    tmp_path: Path
) -> None:
    supplied_provider = object()
    assert not hasattr(core_spawner, "build_provider")

    run = core_spawner.build_agent_run(
        tmp_path,
        "Provider task",
        settings=_settings(),
        provider=supplied_provider,
        model_name="test-model",
        **_storage_paths(tmp_path),
    )

    assert run.loop.model_adapter.provider is supplied_provider


def test_runtime_wires_finalized_requests_to_session_model_view(tmp_path: Path) -> None:
    class Provider:
        reasoning_mode = "high"
        reasoning_token_budget = 321

        def __init__(self) -> None:
            self.requests = []

        def create_response(self, **request):
            self.requests.append(request)
            return _provider_response("summary")

    provider = Provider()
    settings = replace(
        _settings(),
        compaction=CompactionSettings(
            enabled=True,
            trigger_input_tokens=1,
            compact_fraction=0.8,
            min_recent_chunks=2,
        ),
    )
    run = core_spawner.build_agent_run(
        tmp_path,
        "Observed requests",
        settings=settings,
        provider=provider,
        model_name="test-model",
        provider_name="openai_compatible",
        **_storage_paths(tmp_path),
    )
    prepared = run.loop.model_adapter.prepare_request(
        [{"role": "user", "content": "ordinary request"}],
        [{"type": "function", "function": {"name": "inspect"}}],
    )

    run.loop.model_adapter.respond_prepared(prepared)
    run.session.messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "finding " * 80},
        {"role": "tool", "content": "result " * 80},
        {"role": "assistant", "content": "change " * 80},
        {"role": "user", "content": "continue " * 80},
    ]
    run.loop.compactor.maybe_compact(run.session)

    records = [
        json.loads(line)
        for line in run.session_store.model_view_path.read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [record["request_index"] for record in records] == [1, 2]
    assert records[0]["messages"] == provider.requests[0]["messages"]
    assert records[0]["tools"] == provider.requests[0]["tools"]
    assert records[1]["messages"] == provider.requests[1]["messages"]
    assert records[1]["tools"] == []
    assert records[0]["reasoning_mode"] == "high"
    assert records[0]["reasoning_token_budget"] == 321


def test_model_view_records_textual_tool_observation_not_canonical_json(
    tmp_path: Path,
) -> None:
    class Provider:
        def create_response(self, **request):
            return _provider_response()

    run = core_spawner.build_agent_run(
        tmp_path,
        "Observe a read",
        settings=_settings(),
        provider=Provider(),
        model_name="test-model",
        **_storage_paths(tmp_path),
    )
    canonical_result = {
        "ok": True,
        "tool": "read_file",
        "path": str(tmp_path / "script.py"),
        "content": "first\nsecond\n",
        "start_line": 10,
        "end_line": 11,
        "returned_line_ranges": [{"start_line": 10, "end_line": 11}],
    }
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "read-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }
        ],
    }
    canonical_tool_message = ToolResults().message_from_action_result(
        {"tool_call_id": "read-1"}, canonical_result
    )
    run.session.messages = [assistant, canonical_tool_message]

    run.loop._respond([])

    record = json.loads(
        run.session_store.model_view_path.read_text(encoding="utf-8").splitlines()[-1]
    )
    model_tool_message = record["messages"][-1]
    assert model_tool_message["tool_call_id"] == "read-1"
    assert model_tool_message["content"].startswith("READ_FILE OK")
    assert (
        "<<< SQUID CORE SOURCE: script.py lines 10-11 >>>"
        in model_tool_message["content"]
    )
    assert '"content": "first' not in model_tool_message["content"]
    assert json.loads(canonical_tool_message["content"]) == canonical_result


def test_each_real_retry_attempt_gets_its_own_model_view_record(
    tmp_path: Path,
) -> None:
    class RetryProvider:
        def __init__(self) -> None:
            self.requests = []

        def create_response(self, **request):
            self.requests.append(request)
            if len(self.requests) == 1:
                raise TimeoutError("try again")
            return _provider_response()

    class FinishingAgent:
        def initial_messages(self):
            return [{"role": "system", "content": "system"}]

        def tool_definitions(self):
            return []

        def should_stop(self, session):
            return any(message.get("content") == "done" for message in session.messages)

        def validate_reply(self, _reply):
            return {"ok": True, "reason": ""}

    provider = RetryProvider()
    run = core_spawner.build_agent_run(
        tmp_path,
        "Retry capture",
        settings=replace(
            _settings(),
            model_request_max_retries=1,
            model_request_retry_backoff_ms=0,
        ),
        provider=provider,
        model_name="test-model",
        provider_name="openai_compatible",
        agent=FinishingAgent(),
        tool_executor=object(),
        tool_results=ToolResults(),
        **_storage_paths(tmp_path),
    )

    result = run.loop.run()

    records = [
        json.loads(line)
        for line in run.session_store.model_view_path.read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert result.status == "complete"
    assert len(provider.requests) == 2
    assert [record["request_index"] for record in records] == [1, 2]
    assert records[0]["messages"] == records[1]["messages"]


def test_supplied_model_adapter_bypasses_provider_and_adapter_construction(
    monkeypatch, tmp_path: Path
) -> None:
    adapter = object()
    assert not hasattr(core_spawner, "build_provider")
    monkeypatch.setattr(
        core_spawner,
        "ModelAdapter",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("adapter built")),
    )

    run = core_spawner.build_agent_run(
        tmp_path,
        "Adapter task",
        settings=_settings(),
        model_adapter=adapter,
        provider=object(),
        **_storage_paths(tmp_path),
    )

    assert run.loop.model_adapter is adapter


def test_supplied_runtime_collaborators_are_used_directly(tmp_path: Path) -> None:
    collaborators = {
        "agent": object(),
        "model_adapter": object(),
        "compactor": object(),
        "tool_executor": object(),
        "tool_results": object(),
        "approval_policy": object(),
        "action_preflight": object(),
        "hook_runner": object(),
        "display": object(),
    }

    run = core_spawner.build_agent_run(
        tmp_path,
        "Custom task",
        settings=_settings(),
        **_storage_paths(tmp_path),
        **collaborators,
    )

    for name, collaborator in collaborators.items():
        assert getattr(run.loop, name) is collaborator


def test_supplied_tool_executor_skips_default_undo_and_executor(
    monkeypatch, tmp_path: Path
) -> None:
    executor = object()
    monkeypatch.setattr(
        core_spawner.UndoStore,
        "create",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("undo built")),
    )
    monkeypatch.setattr(
        core_spawner,
        "ToolExecutor",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("executor built")),
    )

    run = core_spawner.build_agent_run(
        tmp_path,
        "Executor task",
        settings=_settings(),
        model_adapter=object(),
        tool_executor=executor,
        **_storage_paths(tmp_path),
    )

    assert run.loop.tool_executor is executor


def test_store_factories_preserve_order_and_session_id(tmp_path: Path) -> None:
    session_store = SessionStore.create(tmp_path / "session-host", "Stored task")
    undo_store = UndoStore.create(tmp_path / "undo-host", session_store.session_id)
    calls = []

    def session_factory(workspace_root, task):
        calls.append(("session", workspace_root, task))
        return session_store

    def undo_factory(workspace_root, session_id):
        calls.append(("undo", workspace_root, session_id))
        return undo_store

    run = core_spawner.build_agent_run(
        tmp_path,
        "Stored task",
        settings=_settings(),
        model_adapter=object(),
        session_store_factory=session_factory,
        undo_store_factory=undo_factory,
    )

    assert calls == [
        ("session", tmp_path.resolve(), "Stored task"),
        ("undo", tmp_path.resolve(), session_store.session_id),
    ]
    assert run.session_store is session_store
    assert run.loop.tool_executor.undo_store is undo_store


def test_precreated_session_and_stores_skip_default_construction(
    monkeypatch, tmp_path: Path
) -> None:
    session = Session(tmp_path)
    session_store = SessionStore.create(tmp_path / "session-host", "Existing")
    undo_store = UndoStore.create(tmp_path / "undo-host", session_store.session_id)

    monkeypatch.setattr(
        core_spawner.SessionStore,
        "create",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("session store built")),
    )
    monkeypatch.setattr(
        core_spawner.UndoStore,
        "create",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("undo store built")),
    )

    run = core_spawner.build_agent_run(
        tmp_path,
        "Existing",
        settings=_settings(),
        model_adapter=object(),
        session=session,
        session_store=session_store,
        undo_store=undo_store,
    )

    assert run.session is session
    assert run.session_store is session_store
    assert run.loop.session_store is session_store
    assert run.loop.tool_executor.undo_store is undo_store


def test_direct_agent_loop_construction_remains_available(tmp_path: Path) -> None:
    from squid_core.agent_loop import AgentLoop

    loop = AgentLoop(
        session=Session(tmp_path),
        model_adapter=object(),
        tool_executor=object(),
        tool_results=object(),
        agent=object(),
        display=core_spawner.NullDisplay(),
    )

    assert isinstance(loop, AgentLoop)


@pytest.mark.parametrize("entrypoint", ["build_agent_run", "build_agent_loop"])
def test_core_requires_injected_model_collaborator(tmp_path, entrypoint) -> None:
    kwargs = dict(workspace_root=tmp_path, settings=_settings())
    if entrypoint == "build_agent_run":
        kwargs.update(task="No provider", **_storage_paths(tmp_path))
    else:
        kwargs.update(
            session=Session(tmp_path), session_store=object(),
            agent=object(), undo_store=None,
        )
    with pytest.raises(ValueError, match="provider or model_adapter must be supplied"):
        getattr(core_spawner, entrypoint)(**kwargs)


def test_explicit_provider_inputs_are_independent_of_provider_settings(tmp_path) -> None:
    provider = object()
    run = core_spawner.build_agent_run(
        tmp_path, "Custom transport", settings=_settings(),
        provider=provider, model_name="custom-model", provider_name="custom",
        tool_call_content_mode="omit", **_storage_paths(tmp_path),
    )
    adapter = run.loop.model_adapter
    assert adapter.provider is provider
    assert adapter.model_name == "custom-model"
    assert adapter.provider_name == "custom"
    assert adapter.tool_call_content_mode == "omit"


def test_provider_requires_explicit_model_name(tmp_path) -> None:
    with pytest.raises(ValueError, match="model_name is required"):
        core_spawner.build_agent_run(
            tmp_path, "No model", settings=_settings(), provider=object(),
            **_storage_paths(tmp_path),
        )


@pytest.mark.parametrize("tools", [None, {}, {"custom": {"definition": lambda: {"name": "custom"}}}])
def test_run_preserves_tool_registry_for_agent_and_executor(tmp_path: Path, tools) -> None:
    expected = TOOLS if tools is None else tools
    run = core_spawner.build_agent_run(
        tmp_path, "task", settings=_settings(), model_adapter=object(), tools=tools,
        **_storage_paths(tmp_path),
    )
    assert run.loop.agent.tools is expected
    assert run.loop.tool_executor.tools is expected
    assert run.loop.agent.tool_definitions() == [entry["definition"]() for entry in expected.values()]


@pytest.mark.parametrize("tools", [None, {}, {"custom": {}}])
def test_loop_preserves_tool_registry(tmp_path: Path, tools) -> None:
    store = SessionStore.create(tmp_path / "sessions", "task", workspace_root=tmp_path)
    loop = core_spawner.build_agent_loop(
        tmp_path, Session(workspace_root=tmp_path), store, Agent("task", tools=tools),
        None, _settings(), tools=tools, model_adapter=object(),
    )
    assert loop.tool_executor.tools is (TOOLS if tools is None else tools)
