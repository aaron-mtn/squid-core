"""Minimal in-process interceptor hook support."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


HookEventSink = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class PromptContextBlock:
    title: str
    body: str
    source: str = ""


@dataclass(frozen=True)
class BeforeRunContext:
    session_id: str | None
    repo_root: str
    task: str
    skill_name: str | None = None
    allowed_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class BeforeModelTurnContext:
    session_id: str | None
    repo_root: str
    task: str
    messages: tuple[dict[str, Any], ...]
    tool_steps_used: int


@dataclass(frozen=True)
class CompactionAppliedContext:
    """Identify a session whose model-visible transcript was compacted."""

    session_id: str | None
    repo_root: str


@dataclass(frozen=True)
class ReadResultTrimmedContext:
    """Report source ranges still visible from one trimmed read result."""

    tool_call_id: str
    path: str | None
    retained_line_ranges: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class BeforeToolExecuteContext:
    session_id: str | None
    repo_root: str
    action: dict[str, Any]
    tool_steps_used: int


@dataclass(frozen=True)
class ToolResultContext:
    session_id: str | None
    repo_root: str
    action: dict[str, Any]
    result: dict[str, Any]


@dataclass(frozen=True)
class BeforeAcceptFinalContext:
    session_id: str | None
    repo_root: str
    message: dict[str, Any]
    content: str
    files_read: tuple[str, ...] = ()
    files_written: tuple[str, ...] = ()
    tool_steps_used: int = 0


@dataclass(frozen=True)
class FollowUpRunRequest:
    task: str
    reason: str = ""
    context_blocks: tuple[PromptContextBlock, ...] = ()
    require_user_approval: bool = True


@dataclass(frozen=True)
class RunFinishedContext:
    session_id: str | None
    repo_root: str
    status: str
    reason: str = ""
    final_message: str = ""
    files_read: tuple[str, ...] = ()
    files_written: tuple[str, ...] = ()
    tool_steps_used: int = 0


@dataclass(frozen=True)
class HookResponse:
    """Describe effects requested by a hook at any lifecycle seam."""

    block: bool = False
    reason: str = ""
    guidance: str = ""
    context_blocks: tuple[PromptContextBlock, ...] = ()
    action: dict[str, Any] | None = None
    approval_reason: str = ""
    updated_tool_output: dict[str, Any] | None = None
    additional_context: str = ""
    report_note: str = ""
    follow_up_requests: tuple[FollowUpRunRequest, ...] = ()
    notes: tuple[str, ...] = ()


class HookRunner:
    def __init__(self, hooks: list[object] | None = None) -> None:
        self.hooks = hooks or []

    def hook_names(self) -> tuple[str, ...]:
        """Return compact identities suitable for runtime diagnostics."""
        return tuple(type(hook).__name__ for hook in self.hooks)

    def before_run(
        self,
        ctx: BeforeRunContext,
        *,
        event_sink: HookEventSink | None = None,
    ) -> HookResponse:
        context_blocks: list[PromptContextBlock] = []

        for hook in self.hooks:
            handler = getattr(hook, "before_run", None)
            if handler is None:
                continue

            decision = _invoke_hook(hook, "before_run", handler, ctx, event_sink)
            if decision is None:
                continue

            context_blocks.extend(decision.context_blocks)
            if decision.block:
                return HookResponse(
                    block=True,
                    reason=decision.reason,
                    guidance=decision.guidance,
                    context_blocks=tuple(context_blocks),
                )

        return HookResponse(context_blocks=tuple(context_blocks))

    def before_model_turn(
        self,
        ctx: BeforeModelTurnContext,
        *,
        event_sink: HookEventSink | None = None,
    ) -> HookResponse:
        context_blocks: list[PromptContextBlock] = []

        for hook in self.hooks:
            handler = getattr(hook, "before_model_turn", None)
            if handler is None:
                continue

            response = _invoke_hook(
                hook, "before_model_turn", handler, ctx, event_sink
            )
            if response is None:
                continue

            context_blocks.extend(response.context_blocks)

        return HookResponse(context_blocks=tuple(context_blocks))

    def compaction_applied(
        self,
        ctx: CompactionAppliedContext,
        *,
        event_sink: HookEventSink | None = None,
    ) -> None:
        """Notify hooks only after compaction replaces the active transcript."""
        for hook in self.hooks:
            handler = getattr(hook, "compaction_applied", None)
            if handler is None:
                continue

            _invoke_hook(hook, "compaction_applied", handler, ctx, event_sink)

    def read_result_trimmed(
        self,
        ctx: ReadResultTrimmedContext,
        *,
        event_sink: HookEventSink | None = None,
    ) -> None:
        """Notify hooks when request rendering removes old read source."""
        for hook in self.hooks:
            handler = getattr(hook, "read_result_trimmed", None)
            if handler is None:
                continue
            _invoke_hook(hook, "read_result_trimmed", handler, ctx, event_sink)

    def before_tool_execute(
        self,
        ctx: BeforeToolExecuteContext,
        *,
        event_sink: HookEventSink | None = None,
    ) -> HookResponse:
        action = ctx.action
        approval_reasons: list[str] = []

        for hook in self.hooks:
            handler = getattr(hook, "before_tool_execute", None)
            if handler is None:
                continue

            decision = _invoke_hook(
                hook,
                "before_tool_execute",
                handler,
                BeforeToolExecuteContext(
                    session_id=ctx.session_id,
                    repo_root=ctx.repo_root,
                    action=action,
                    tool_steps_used=ctx.tool_steps_used,
                ),
                event_sink,
            )
            if decision is None:
                continue

            if decision.approval_reason:
                approval_reasons.append(decision.approval_reason)
            if decision.block:
                return HookResponse(
                    block=True,
                    reason=decision.reason,
                    guidance=decision.guidance,
                    action=(
                        _preserve_tool_call_id(action, decision.action)
                        if decision.action is not None
                        else action
                    ),
                    approval_reason="; ".join(approval_reasons),
                )
            if decision.action is not None:
                action = _preserve_tool_call_id(action, decision.action)

        return HookResponse(
            action=action,
            approval_reason="; ".join(approval_reasons),
        )

    def tool_result(
        self,
        ctx: ToolResultContext,
        *,
        event_sink: HookEventSink | None = None,
    ) -> HookResponse:
        result = ctx.result
        additional_context: list[str] = []

        for hook in self.hooks:
            handler = getattr(hook, "tool_result", None)
            if handler is None:
                continue

            response = _invoke_hook(
                hook,
                "tool_result",
                handler,
                ToolResultContext(
                    session_id=ctx.session_id,
                    repo_root=ctx.repo_root,
                    action=ctx.action,
                    result=result,
                ),
                event_sink,
            )
            if response is None:
                continue

            if response.updated_tool_output is not None:
                result = response.updated_tool_output
            if response.additional_context:
                additional_context.append(response.additional_context)

        return HookResponse(
            updated_tool_output=result,
            additional_context="\n\n".join(additional_context),
        )

    def before_accept_final(
        self,
        ctx: BeforeAcceptFinalContext,
        *,
        event_sink: HookEventSink | None = None,
    ) -> HookResponse:
        report_notes: list[str] = []

        for hook in self.hooks:
            handler = getattr(hook, "before_accept_final", None)
            if handler is None:
                continue

            decision = _invoke_hook(
                hook, "before_accept_final", handler, ctx, event_sink
            )
            if decision is None:
                continue

            if decision.report_note:
                report_notes.append(decision.report_note)
            if decision.block:
                return HookResponse(
                    block=True,
                    reason=decision.reason,
                    guidance=decision.guidance,
                    report_note="\n\n".join(report_notes),
                )

        return HookResponse(report_note="\n\n".join(report_notes))

    def run_finished(
        self,
        ctx: RunFinishedContext,
        *,
        event_sink: HookEventSink | None = None,
    ) -> HookResponse:
        follow_up_requests: list[FollowUpRunRequest] = []
        notes: list[str] = []

        for hook in self.hooks:
            handler = getattr(hook, "run_finished", None)
            if handler is None:
                continue

            decision = _invoke_hook(
                hook, "run_finished", handler, ctx, event_sink
            )
            if decision is None:
                continue

            follow_up_requests.extend(decision.follow_up_requests)
            notes.extend(decision.notes)

        return HookResponse(
            follow_up_requests=tuple(follow_up_requests),
            notes=tuple(notes),
        )


def _invoke_hook(
    hook: object,
    seam: str,
    handler: Callable[[Any], HookResponse | None],
    context: Any,
    event_sink: HookEventSink | None,
) -> HookResponse | None:
    """Call one Hook and report only compact, non-payload outcome metadata."""
    hook_name = type(hook).__name__
    try:
        response = handler(context)
        effects = _response_effects(response)
    except BaseException as error:
        _emit_hook_event(
            event_sink,
            {
                "type": "hook_execution",
                "hook": hook_name,
                "seam": seam,
                "status": "error",
                "produced_effect": False,
                "effects": [],
                "error_type": type(error).__name__,
            },
        )
        raise

    _emit_hook_event(
        event_sink,
        {
            "type": "hook_execution",
            "hook": hook_name,
            "seam": seam,
            "status": "effect" if effects else "no_effect",
            "produced_effect": bool(effects),
            "effects": effects,
        },
    )
    return response


def _response_effects(response: HookResponse | None) -> list[str]:
    """Describe effect categories without copying Hook-returned payloads."""
    if response is None:
        return []

    effects = []
    effect_fields = (
        ("block", response.block),
        ("reason", response.reason),
        ("guidance", response.guidance),
        ("context_blocks", response.context_blocks),
        ("updated_action", response.action is not None),
        ("approval_reasons", response.approval_reason),
        ("updated_tool_output", response.updated_tool_output is not None),
        ("additional_context", response.additional_context),
        ("report_note", response.report_note),
        ("follow_up_requests", response.follow_up_requests),
        ("notes", response.notes),
    )
    for name, value in effect_fields:
        if value:
            effects.append(name)
    return effects


def _emit_hook_event(
    event_sink: HookEventSink | None,
    event: dict[str, Any],
) -> None:
    """Keep diagnostics observational even if their sink becomes unavailable."""
    if event_sink is None:
        return
    try:
        event_sink(event)
    except Exception:
        # Hook diagnostics must not change Hook or run behavior.
        pass


def _preserve_tool_call_id(previous: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    if "tool_call_id" not in previous:
        return action

    return {**action, "tool_call_id": previous["tool_call_id"]}
