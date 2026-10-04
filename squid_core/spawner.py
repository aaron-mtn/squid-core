"""Generic construction helpers for a Squid Core agent run.

Applications select model collaborators, prompts, tools, presentation, and
any application-specific discovery policy before calling this module.  ``AgentLoop`` remains available
for callers that want to construct every collaborator themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from squid_core.safety import ActionPreflight
from squid_core.agent_loop import AgentLoop, RunResult
from squid_core.safety import ApprovalPolicy
from squid_core.compaction import Compactor
from squid_core.compaction.token_estimate import estimate_messages_tokens
from squid_core.events import EventCallback
from squid_core.hooks import HookRunner
from squid_core.model_adapter import ModelAdapter
from squid_core.platform_context import PlatformContext, detect_platform_context
from squid_core.session import Session
from squid_core.settings import RuntimeSettings
from squid_core.tools import ToolExecutor, ToolResults
from squid_core.agent import Agent
from squid_core.storage import SessionStore, UndoStore


SessionStoreFactory = Callable[[Path, str], SessionStore]
UndoStoreFactory = Callable[[Path, str], UndoStore]


class NullDisplay:
    """Small UI-neutral presentation collaborator used by generic runs."""

    debug = False

    def run_started(self) -> None:
        pass

    def thinking(self) -> None:
        pass

    def debug_reply(self, reply: dict) -> None:
        pass

    def final_message(self, content: str) -> None:
        pass

    def assistant_message(self, content: str) -> None:
        pass

    def reply_rejected(self, reason: str) -> None:
        pass

    def repeated_invalid_replies(self, reason: str, count: int) -> None:
        pass

    def action_started(self, action: dict) -> None:
        pass

    def tool_result(self, action: dict, result: dict) -> None:
        pass

    def step_limit_reached(self, max_tool_steps: int) -> None:
        pass


@dataclass(frozen=True)
class AgentRun:
    session: Session
    session_store: SessionStore
    loop: AgentLoop


def _wire_model_view_observer(model_adapter, session_store: SessionStore) -> None:
    """Connect generic request observation to best-effort session diagnostics."""
    set_request_observer = getattr(model_adapter, "set_request_observer", None)
    append_model_view = getattr(session_store, "append_model_view", None)
    if not callable(set_request_observer) or not callable(append_model_view):
        # Legacy/custom adapters remain valid; only adapters exposing the new
        # finalized-request seam participate in automatic request capture.
        return

    def append_final_request(request: dict) -> None:
        messages = request.get("messages", [])
        record = {
            "provider": getattr(model_adapter, "provider_name", None),
            "reasoning_mode": getattr(model_adapter, "reasoning_mode", None),
            "reasoning_token_budget": getattr(
                model_adapter,
                "reasoning_token_budget",
                None,
            ),
            "message_count": len(messages),
            "estimated_request_tokens": estimate_messages_tokens(messages),
            **request,
        }
        append_model_view(
            {name: value for name, value in record.items() if value is not None}
        )

    set_request_observer(append_final_request)


def run_agent_task(
    repo_root: Path | str,
    task: str,
    *,
    settings: RuntimeSettings,
    **kwargs,
) -> RunResult:
    """Build and run a generic task with optional selective replacements."""

    run = build_agent_run(repo_root, task, settings=settings, **kwargs)
    run.loop.display.run_started()
    return run.loop.run()


def build_agent_run(
    repo_root: Path | str,
    task: str,
    *,
    settings: RuntimeSettings,
    prompt_defaults=None,
    skill=None,
    tools: dict | None = None,
    agent=None,
    provider=None,
    model_adapter=None,
    model_name: str | None = None,
    provider_name: str | None = None,
    tool_call_content_mode: str = "empty",
    compactor=None,
    compaction_context_provider: Callable[[list[dict]], str | None] | None = None,
    tool_executor=None,
    tool_results=None,
    approval_policy=None,
    action_preflight=None,
    hook_runner=None,
    display=None,
    approval_prompter=None,
    session: Session | None = None,
    sessions_dir: Path | None = None,
    undo_dir: Path | None = None,
    session_store: SessionStore | None = None,
    undo_store: UndoStore | None = None,
    session_store_factory: SessionStoreFactory | None = None,
    undo_store_factory: UndoStoreFactory | None = None,
    on_event: EventCallback | None = None,
    should_cancel: Callable[[], bool] | None = None,
    platform_context: PlatformContext | None = None,
) -> AgentRun:
    """Construct a generic run without application discovery or terminal UI.

    Supplied high-level collaborators are used directly.  In particular, a
    supplied model adapter bypasses adapter construction and a supplied tool
    executor bypasses default undo-store and executor construction.
    With a provider, callers must also supply ``model_name`` and may select
    ``provider_name`` and ``tool_call_content_mode`` explicitly. Default
    stores require explicit ``sessions_dir`` and ``undo_dir`` paths; store
    factories continue to receive the workspace root and own their path policy.
    """

    if session_store is not None and session_store_factory is not None:
        raise ValueError("session_store and session_store_factory are mutually exclusive")
    if undo_store is not None and undo_store_factory is not None:
        raise ValueError("undo_store and undo_store_factory are mutually exclusive")

    repo_root = Path(repo_root).resolve()
    session = session if session is not None else Session(repo_root=repo_root)
    if session_store is None:
        if session_store_factory is not None:
            session_store = session_store_factory(repo_root, task)
        else:
            if sessions_dir is None:
                raise ValueError(
                    "sessions_dir is required unless a session store or factory "
                    "is supplied"
                )
            session_store = SessionStore.create(
                sessions_dir,
                task,
                workspace_root=repo_root,
            )

    if undo_store is None and tool_executor is None:
        if undo_store_factory is not None:
            undo_store = undo_store_factory(repo_root, session_store.session_id)
        else:
            if undo_dir is None:
                raise ValueError(
                    "undo_dir is required unless an undo store or factory is supplied"
                )
            undo_store = UndoStore.create(
                undo_dir,
                session_store.session_id,
                workspace_root=repo_root,
            )
    elif undo_store is None and undo_store_factory is not None:
        undo_store = undo_store_factory(repo_root, session_store.session_id)

    selected_tools = {} if tools is None else tools
    needs_platform = agent is None or tool_executor is None
    if platform_context is None and needs_platform:
        platform_context = detect_platform_context()
    if agent is None:
        agent = Agent(
            task=task,
            skill=skill,
            tools=selected_tools,
            platform_context=platform_context,
            workspace=str(repo_root),
            prompt_defaults=prompt_defaults,
        )

    loop = build_agent_loop(
        repo_root=repo_root,
        session=session,
        session_store=session_store,
        undo_store=undo_store,
        settings=settings,
        agent=agent,
        tools=selected_tools,
        provider=provider,
        model_adapter=model_adapter,
        model_name=model_name,
        provider_name=provider_name,
        tool_call_content_mode=tool_call_content_mode,
        compactor=compactor,
        compaction_context_provider=compaction_context_provider,
        tool_executor=tool_executor,
        tool_results=tool_results,
        approval_policy=approval_policy,
        action_preflight=action_preflight,
        hook_runner=hook_runner,
        display=display,
        approval_prompter=approval_prompter,
        on_event=on_event,
        should_cancel=should_cancel,
        platform_context=platform_context,
    )
    return AgentRun(session=session, session_store=session_store, loop=loop)


def build_agent_loop(
    repo_root: Path,
    session: Session,
    session_store: SessionStore,
    agent,
    undo_store: UndoStore | None,
    settings: RuntimeSettings,
    *,
    tools: dict | None = None,
    display=None,
    approval_prompter=None,
    provider=None,
    model_adapter=None,
    model_name: str | None = None,
    provider_name: str | None = None,
    tool_call_content_mode: str = "empty",
    compactor=None,
    compaction_context_provider: Callable[[list[dict]], str | None] | None = None,
    tool_executor=None,
    tool_results=None,
    approval_policy=None,
    action_preflight=None,
    hook_runner=None,
    on_event: EventCallback | None = None,
    should_cancel: Callable[[], bool] | None = None,
    platform_context: PlatformContext | None = None,
) -> AgentLoop:
    """Wire Core collaborators using a ready adapter or provider plus model name.

    ``provider_name`` labels diagnostics/capabilities; ``tool_call_content_mode``
    controls replay. The caller chooses these values, independently of settings.
    """

    repo_root = Path(repo_root).resolve()
    display = NullDisplay() if display is None else display
    if model_adapter is None:
        # Backend selection and replay policy belong to the embedding application.
        # Runtime settings deliberately contain no provider configuration.
        if provider is None:
            raise ValueError(
                "provider or model_adapter must be supplied; Core does not choose a provider"
            )
        if model_name is None:
            raise ValueError("model_name is required when supplying a provider")
        model_adapter = ModelAdapter(
            provider=provider,
            model_name=model_name,
            tool_call_content_mode=tool_call_content_mode,
            provider_name=provider_name,
        )
    set_asset_loader = getattr(model_adapter, "set_asset_loader", None)
    if callable(set_asset_loader):
        set_asset_loader(session_store.load_asset)
    if compactor is None:
        compactor = Compactor(
            settings.compaction,
            model_adapter,
            context_provider=compaction_context_provider,
            should_cancel=should_cancel,
        )
    _wire_model_view_observer(model_adapter, session_store)
    if tool_executor is None:
        tool_executor = ToolExecutor(
            repo_root=repo_root,
            session=session,
            undo_store=undo_store,
            allow_outside_workspace=settings.approval_mode == "never",
            tools={} if tools is None else tools,
            platform_context=platform_context,
            read_file_settings=settings.read_file,
            snapshot_asset=session_store.snapshot_asset,
            ensure_generated_image=session_store.ensure_generated_image,
            supports_image_input=getattr(model_adapter, "supports_image_input", None),
            should_cancel=should_cancel,
        )
    if tool_results is None:
        tool_results = ToolResults()
    if approval_policy is None:
        approval_policy = ApprovalPolicy(
            mode=settings.approval_mode,
            prompter=approval_prompter,
        )
    if action_preflight is None:
        action_preflight = ActionPreflight(
            workspace_root=repo_root,
        )
    if hook_runner is None:
        hook_runner = HookRunner()

    return AgentLoop(
        session=session,
        model_adapter=model_adapter,
        tool_executor=tool_executor,
        tool_results=tool_results,
        agent=agent,
        display=display,
        session_store=session_store,
        compactor=compactor,
        max_tool_steps=settings.max_tool_steps,
        approval_policy=approval_policy,
        action_preflight=action_preflight,
        stream_assistant_text=settings.stream_assistant_text,
        on_event=on_event,
        should_cancel=should_cancel,
        hook_runner=hook_runner,
        tool_result_trimming=settings.tool_result_trimming,
        model_request_max_retries=settings.model_request_max_retries,
        model_request_retry_backoff_ms=settings.model_request_retry_backoff_ms,
    )
