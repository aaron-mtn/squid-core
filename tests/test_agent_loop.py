"""Tests for the generic agent-loop runtime and its lifecycle boundaries."""

import inspect
import json
from types import SimpleNamespace

import pytest

import squid_core.agent_loop as agent_loop_module
from squid_core.agent import Agent as CodingAgent
from squid_core.agent_loop.transcript_recording import (
    reconcile_system_message,
    record_message,
    record_messages,
)
from squid_core.safety import ActionPreflightDecision
from squid_core.agent_loop import AgentLoop, RunResult
from squid_core.safety import ApprovalDecision, ApprovalPolicy
from squid_core.hooks import (
    HookResponse,
    HookRunner,
    ToolResultContext,
)
from squid_core.model_adapter import ModelRequestError
from squid_core.session import Session
from squid_core.tools import ToolExecutor, ToolResults
from squid_core.storage import SessionStore


class FakeAgent:
    def initial_messages(self) -> list[dict]:
        return [{"role": "system", "content": "system"}]

    def tool_definitions(self) -> list[dict]:
        return []

    def should_stop(self, session: Session) -> bool:
        return any(
            message["role"] == "assistant" and message["content"] == "TASK COMPLETE"
            for message in session.messages
        )

    def validate_reply(self, reply: dict) -> dict:
        if reply.get("reason"):
            return {"ok": False, "reason": reply["reason"]}
        return {"ok": True, "reason": ""}

    def correction_message_for(self, reason: str) -> dict:
        return {"role": "user", "content": f"correction:{reason}"}

    def failure_message_for_invalid_replies(self, reason: str, count: int) -> dict:
        return {
            "role": "assistant",
            "content": (
                f"Code Squid stopped after {count} consecutive invalid replies.\n"
                f"Last invalid reply reason: {reason}\n"
                "Explanation: the model replied with no tool call and no final content"
            ),
        }


class FakeModelAdapter:
    def __init__(
        self,
        replies: list[dict],
        events: list[str] | None = None,
        streaming: bool = False,
        deltas: list[str] | None = None,
        reasoning_deltas: list[str] | None = None,
        tool_preparing_events: list[dict] | None = None,
    ) -> None:
        self.replies = replies
        self.index = 0
        self.events = events
        self.streaming = streaming
        self.deltas = deltas or []
        self.reasoning_deltas = reasoning_deltas or []
        self.tool_preparing_events = tool_preparing_events or []
        self.message_requests = []
        self.provider_name = "fake-provider"
        self.model_name = "fake-model"

    def respond(self, messages: list[dict], tool_definitions: list[dict]) -> dict:
        self.message_requests.append([dict(message) for message in messages])
        if self.events is not None:
            self.events.append("respond")
        reply = self.replies[self.index]
        self.index += 1
        return reply

    def supports_streaming(self) -> bool:
        return self.streaming

    def respond_streaming(
        self,
        messages: list[dict],
        tool_definitions: list[dict],
        on_text_delta,
        on_reasoning_delta=None,
        on_tool_preparing=None,
        should_cancel=None,
    ) -> dict:
        self.message_requests.append([dict(message) for message in messages])
        if self.events is not None:
            self.events.append("respond_streaming:start")
        if on_reasoning_delta is not None:
            for delta in self.reasoning_deltas:
                on_reasoning_delta(delta)
        self.reasoning_deltas = []
        for delta in self.deltas:
            if should_cancel is not None and should_cancel():
                from squid_core.providers.base import StreamCancelled

                raise StreamCancelled
            on_text_delta(delta)
        self.deltas = []
        if on_tool_preparing is not None:
            for event in self.tool_preparing_events:
                on_tool_preparing(event)
        self.tool_preparing_events = []
        if self.events is not None:
            self.events.append("respond_streaming:end")
        reply = self.replies[self.index]
        self.index += 1
        return reply


class FakeToolExecutor:
    def __init__(self, repo_root=None, events: list[str] | None = None) -> None:
        self.repo_root = repo_root
        self.actions = []
        self.events = events

    def execute(self, action: dict) -> dict:
        if self.events is not None:
            self.events.append(f"execute:{action['tool']}")
        self.actions.append(action)
        return {"ok": True, "tool": action["tool"]}


class FakeToolResults:
    def messages_from_action_results(self, actions: list[dict], results: list[dict]) -> list[dict]:
        return [
            {"role": "tool", "content": result.get("tool", action["tool"])}
            for action, result in zip(actions, results)
        ]


class FakePrompter:
    def __init__(self, decision: ApprovalDecision) -> None:
        self.decision = decision
        self.actions = []

    def approve_action(self, action: dict) -> ApprovalDecision:
        self.actions.append(action)
        return self.decision


class FakeSessionStore:
    def __init__(self, events: list[str] | None = None) -> None:
        self.messages = []
        self.compactions = []
        self.model_replies = []
        self.reply_rejections = []
        self.runtime_events = []
        self.statuses = []
        self.system_reconciliations = []
        self.events = events

    def append_message(self, message: dict, usage: dict | None = None) -> None:
        if self.events is not None:
            self.events.append("append_message")
        self.messages.append({"message": message, "usage": usage})

    def append_compaction_event(self, event: dict) -> None:
        if event["type"] == "compaction_applied":
            self.compactions.append(
                {name: value for name, value in event.items() if name != "type"}
            )

    def append_runtime_event(self, event: dict) -> None:
        self.runtime_events.append(dict(event))

    def append_system_message_reconciliation(self, message: dict) -> None:
        self.system_reconciliations.append(message)

    def append_model_reply(self, raw_reply: dict) -> dict:
        entry = {"id": f"raw_{len(self.model_replies) + 1}", "reply": raw_reply}
        self.model_replies.append(entry)
        return entry

    def append_reply_rejection(
        self,
        reply: dict,
        *,
        reason: str,
        consecutive_invalid_attempt: int,
        raw_reply_id: str,
    ) -> dict:
        entry = {
            "reason": reason,
            "error": reply.get("format_error") or reason,
            "consecutive_invalid_attempt": consecutive_invalid_attempt,
            "raw_reply_id": raw_reply_id,
        }
        for name in ("recoverable_tool_name", "recoverable_raw_arguments"):
            if name in reply:
                entry[name] = reply[name]
        self.reply_rejections.append(entry)
        return entry

    def update_status(self, status: str) -> None:
        self.statuses.append(status)


class TrackingSessionStore(SessionStore):
    """Retain persistence call order while exercising the real JSONL store."""

    def __init__(self, session_dir) -> None:
        super().__init__(session_dir)
        self.operations = []

    def append_runtime_event(self, event: dict) -> dict:
        self.operations.append(f"event:{event['type']}")
        return super().append_runtime_event(event)

    def update_status(self, status: str) -> None:
        self.operations.append(f"status:{status}")
        super().update_status(status)


class NullDisplay:
    def __init__(self, events: list[str] | None = None) -> None:
        self.step_limits_reached = []
        self.assistant_messages = []
        self.reasoning_messages = []
        self.final_messages = []
        self.debug_replies = []
        self.rejected_replies = []
        self.debug = False
        self.events = events

    def thinking(self) -> None:
        if self.events is not None:
            self.events.append("thinking")

    def assistant_message(self, content: str) -> None:
        self.assistant_messages.append(content)

    def reasoning_message(self, content: str) -> None:
        self.reasoning_messages.append(content)

    def final_message(self, content: str) -> None:
        self.final_messages.append(content)

    def debug_reply(self, reply: dict) -> None:
        self.debug_replies.append(reply)

    def reply_rejected(self, reason: str) -> None:
        self.rejected_replies.append(reason)

    def repeated_invalid_replies(self, reason: str, count: int) -> None:
        pass

    def step_limit_reached(self, max_tool_steps: int) -> None:
        self.step_limits_reached.append(max_tool_steps)

    def action_started(self, action: dict) -> None:
        pass

    def tool_result(self, action: dict, result: dict) -> None:
        pass


def reply(content: str, actions: list[dict] | None = None, reason: str = "") -> dict:
    return {
        "message": {"role": "assistant", "content": content},
        "actions": actions or [],
        "reason": reason,
        "raw_reply": {
            "assistant": {"content": content, "raw_tool_calls": []},
        },
    }


def tool_call_reply(
    call_id: str,
    *,
    content: str = "Using read_file.",
    tool: str = "read_file",
    args: dict | None = None,
) -> dict:
    args = args or {}
    return {
        "message": {
            "role": "assistant",
            "content": content,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": tool,
                        "arguments": json.dumps(args),
                    },
                }
            ],
        },
        "actions": [{"tool": tool, "args": args, "tool_call_id": call_id}],
        "reason": "",
        "raw_reply": {"assistant": {"content": content}},
    }


def multi_tool_call_reply(*calls: tuple[str, str]) -> dict:
    """Build one assistant turn whose actions and call metadata share order."""
    tool_calls = [
        {
            "id": call_id,
            "type": "function",
            "function": {"name": tool, "arguments": "{}"},
        }
        for call_id, tool in calls
    ]
    return {
        "message": {
            "role": "assistant",
            "content": "Running independent tools.",
            "tool_calls": tool_calls,
        },
        "actions": [
            {"tool": tool, "args": {}, "tool_call_id": call_id}
            for call_id, tool in calls
        ],
        "reason": "",
        "raw_reply": {"assistant": {"content": "Running independent tools."}},
    }


def multi_tool_call_reply_with_args(*calls: tuple[str, str, dict]) -> dict:
    """Build a tool turn with explicit arguments for execution-path tests."""
    return {
        "message": {
            "role": "assistant",
            "content": "Running independent tools.",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": tool, "arguments": json.dumps(args)},
                }
                for call_id, tool, args in calls
            ],
        },
        "actions": [
            {"tool": tool, "args": args, "tool_call_id": call_id}
            for call_id, tool, args in calls
        ],
        "reason": "",
        "raw_reply": {"assistant": {"content": "Running independent tools."}},
    }


class FakeCompactor:
    def __init__(self, metadata: dict | None = None, events: list[str] | None = None) -> None:
        self.metadata = metadata
        self.events = events
        self.sessions = []

    def maybe_compact(
        self,
        session: Session,
        *,
        event_callback=None,
        post_apply_callback=None,
    ) -> dict | None:
        if self.events is not None:
            self.events.append("compact")
        self.sessions.append(session)
        if self.metadata is not None and event_callback is not None:
            event = {"type": "compaction_applied", **self.metadata}
            event_callback(event)
            post_apply_callback(event)
        return self.metadata


class LifecycleCompactor:
    """Emit a minimal lifecycle for testing the loop's compaction event seam."""

    def maybe_compact(
        self,
        session: Session,
        *,
        event_callback=None,
        post_apply_callback=None,
    ) -> None:
        event_callback({"type": "compaction_check", "will_compact": True})
        event_callback({"type": "compaction_started", "messages_before": 4})
        event = {"type": "compaction_applied", "messages_after": 2}
        event_callback(event)
        post_apply_callback(event)


class StopImmediatelyAgent(FakeAgent):
    def should_stop(self, session: Session) -> bool:
        return True


def test_agent_loop_does_not_import_domain_safety_functions() -> None:
    source = inspect.getsource(agent_loop_module)

    assert "workspace_boundary_reason" not in source
    assert "guarded_run_command_reason" not in source


def run_loop(
    replies: list[dict],
    max_tool_steps: int | None = None,
    approval_policy: ApprovalPolicy | None = None,
    compactor: FakeCompactor | None = None,
    repo_root=None,
    on_event=None,
) -> tuple[Session, FakeToolExecutor, FakeModelAdapter, NullDisplay]:
    session = Session(repo_root=repo_root)
    tool_executor = FakeToolExecutor(repo_root=repo_root)
    model_adapter = FakeModelAdapter(replies)
    display = NullDisplay()
    loop = AgentLoop(
        session=session,
        model_adapter=model_adapter,
        tool_executor=tool_executor,
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=display,
        compactor=compactor,
        max_tool_steps=max_tool_steps,
        approval_policy=approval_policy,
        on_event=on_event,
    )

    loop.run()

    return session, tool_executor, model_adapter, display


def run_loop_with_store(
    replies: list[dict],
    max_tool_steps: int | None = None,
    compactor: FakeCompactor | None = None,
) -> tuple[Session, FakeToolExecutor, FakeSessionStore, FakeModelAdapter, NullDisplay]:
    session = Session(repo_root=None)
    tool_executor = FakeToolExecutor()
    session_store = FakeSessionStore()
    model_adapter = FakeModelAdapter(replies)
    display = NullDisplay()
    loop = AgentLoop(
        session=session,
        model_adapter=model_adapter,
        tool_executor=tool_executor,
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=display,
        session_store=session_store,
        compactor=compactor,
        max_tool_steps=max_tool_steps,
    )

    loop.run()

    return session, tool_executor, session_store, model_adapter, display


def run_loop_with_final_hook(
    replies: list[dict],
    hook: object,
    *,
    max_tool_steps: int | None = None,
    on_event=None,
    should_cancel=None,
) -> tuple[
    RunResult,
    Session,
    FakeToolExecutor,
    FakeSessionStore,
    FakeModelAdapter,
    NullDisplay,
]:
    """Run focused final-acceptance scenarios with observable loop state."""
    session = Session(repo_root=None)
    tool_executor = FakeToolExecutor()
    session_store = FakeSessionStore()
    model_adapter = FakeModelAdapter(replies)
    display = NullDisplay()
    loop = AgentLoop(
        session=session,
        model_adapter=model_adapter,
        tool_executor=tool_executor,
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=display,
        session_store=session_store,
        max_tool_steps=max_tool_steps,
        on_event=on_event,
        should_cancel=should_cancel,
        hook_runner=HookRunner([hook]),
    )

    result = loop.run()
    return (
        result,
        session,
        tool_executor,
        session_store,
        model_adapter,
        display,
    )


@pytest.mark.parametrize("blocked_count", [1, 2])
def test_blocked_finals_below_limit_can_recover(blocked_count: int) -> None:
    class BlockingCandidateHook:
        def before_accept_final(self, context) -> HookResponse:
            if context.content.startswith("blocked"):
                return HookResponse(
                    block=True,
                    reason="missing verification",
                    guidance="Run verification before finalizing.",
                )
            return HookResponse()

    result, session, _, store, adapter, display = run_loop_with_final_hook(
        [
            *[reply(f"blocked {index}") for index in range(blocked_count)],
            reply("TASK COMPLETE"),
        ],
        BlockingCandidateHook(),
    )

    assert result == RunResult(status="complete")
    assert adapter.index == blocked_count + 1
    assert store.statuses == ["complete"]
    assert display.final_messages == ["TASK COMPLETE"]
    assert session.messages[-1] == {"role": "assistant", "content": "TASK COMPLETE"}
    assert adapter.message_requests[-1][-1] == {
        "role": "user",
        "content": "Run verification before finalizing.",
    }


def test_repeated_blocked_finals_stop_at_limit_without_another_request() -> None:
    events = []

    class AlwaysBlockingHook:
        def before_accept_final(self, context) -> HookResponse:
            return HookResponse(
                block=True,
                reason="requirement still unmet",
                guidance="Address the requirement before finalizing.",
            )

    result, session, _, store, adapter, display = run_loop_with_final_hook(
        [reply(f"blocked {index}") for index in range(10)],
        AlwaysBlockingHook(),
        # Blocked finals are deliberately independent of the Tool-step budget.
        max_tool_steps=1,
        on_event=events.append,
    )

    assert result.status == "failed"
    assert "before_accept_final rejected 3 consecutive final responses" in result.reason
    assert "requirement still unmet" in result.reason
    assert "Address the requirement before finalizing." in result.reason
    assert adapter.index == 3
    assert len(adapter.message_requests) == 3
    assert store.statuses == ["failed"]
    assert display.step_limits_reached == []
    assert session.messages == [{"role": "system", "content": "system"}]
    rejected = [event for event in events if event["type"] == "reply_rejected"]
    assert [event["consecutive_blocked_finals"] for event in rejected] == [1, 2, 3]
    assert all(event["limit"] == 3 for event in rejected)
    run_failed = [event for event in events if event["type"] == "run_failed"]
    run_finished = [event for event in events if event["type"] == "run_finished"]
    assert len(run_failed) == 1
    assert run_failed[0]["stage"] == "before_accept_final_limit"
    assert len(run_finished) == 1
    assert run_finished[0]["status"] == "failed"


def test_successful_tool_actions_reset_blocked_final_streak() -> None:
    class BlockingCandidateHook:
        def before_accept_final(self, context) -> HookResponse:
            if context.content.startswith("blocked"):
                return HookResponse(block=True, reason="more work needed")
            return HookResponse()

    replies = [
        reply("blocked 1"),
        reply("work 1", actions=[{"tool": "read_file"}]),
        reply("blocked 2"),
        reply("work 2", actions=[{"tool": "read_file"}]),
        reply("blocked 3"),
        reply("TASK COMPLETE"),
    ]
    result, _, tool_executor, store, adapter, _ = run_loop_with_final_hook(
        replies,
        BlockingCandidateHook(),
    )

    assert result == RunResult(status="complete")
    assert adapter.index == len(replies)
    assert tool_executor.actions == [{"tool": "read_file"}, {"tool": "read_file"}]
    assert store.statuses == ["complete"]


def test_tool_action_blocked_before_execution_does_not_reset_final_streak() -> None:
    class BlockingFinalAndToolHook:
        def before_accept_final(self, context) -> HookResponse:
            return HookResponse(block=True, reason="more work needed")

        def before_tool_execute(self, context) -> HookResponse:
            return HookResponse(block=True, reason="tool unavailable")

    replies = [
        reply("blocked 1"),
        reply("attempt 1", actions=[{"tool": "read_file"}]),
        reply("blocked 2"),
        reply("attempt 2", actions=[{"tool": "read_file"}]),
        reply("blocked 3"),
        reply("unused"),
    ]
    result, _, tool_executor, _, adapter, _ = run_loop_with_final_hook(
        replies,
        BlockingFinalAndToolHook(),
    )

    assert result.status == "failed"
    assert "3 consecutive final responses" in result.reason
    assert adapter.index == 5
    assert tool_executor.actions == []


def test_invalid_reply_limit_is_independent_from_blocked_final_streak() -> None:
    class BlockingCandidateHook:
        def before_accept_final(self, context) -> HookResponse:
            return HookResponse(block=True, reason="more work needed")

    result, _, _, store, adapter, _ = run_loop_with_final_hook(
        [
            reply("blocked final"),
            reply("bad 1", reason="empty_final_reply"),
            reply("bad 2", reason="empty_final_reply"),
            reply("bad 3", reason="empty_final_reply"),
            reply("unused"),
        ],
        BlockingCandidateHook(),
    )

    assert result == RunResult(status="failed", reason="empty_final_reply")
    assert adapter.index == 4
    assert len(store.reply_rejections) == 3
    assert store.statuses == ["failed"]


def test_many_successful_tool_calls_are_not_a_global_request_limit() -> None:
    class AllowingHook:
        def before_accept_final(self, context) -> HookResponse:
            return HookResponse()

    tool_replies = [
        reply(f"work {index}", actions=[{"tool": "read_file"}])
        for index in range(8)
    ]
    result, _, tool_executor, _, adapter, _ = run_loop_with_final_hook(
        [*tool_replies, reply("TASK COMPLETE")],
        AllowingHook(),
    )

    assert result == RunResult(status="complete")
    assert len(tool_executor.actions) == 8
    assert adapter.index == 9


def test_final_hook_exception_keeps_existing_failure_unwind() -> None:
    events = []

    class FailingHook:
        def before_accept_final(self, context) -> HookResponse:
            raise RuntimeError("hook exploded")

    with pytest.raises(RuntimeError, match="hook exploded"):
        run_loop_with_final_hook(
            [reply("candidate")],
            FailingHook(),
            on_event=events.append,
        )

    assert [event["type"] for event in events][-2:] == [
        "run_failed",
        "run_finished",
    ]


def test_cancellation_after_blocked_final_does_not_retry() -> None:
    cancelled = False

    class CancellingBlockingHook:
        def before_accept_final(self, context) -> HookResponse:
            nonlocal cancelled
            cancelled = True
            return HookResponse(block=True, reason="more work needed")

    result, _, _, store, adapter, _ = run_loop_with_final_hook(
        [reply("blocked"), reply("unused")],
        CancellingBlockingHook(),
        should_cancel=lambda: cancelled,
    )

    assert result == RunResult(status="interrupted", reason="cancelled")
    assert adapter.index == 1
    assert store.statuses == ["interrupted"]


def test_blocked_final_limit_leaves_durable_session_replayable(tmp_path) -> None:
    class AlwaysBlockingHook:
        def before_accept_final(self, context) -> HookResponse:
            return HookResponse(block=True, reason="verification missing")

    session = Session(repo_root=tmp_path)
    session_store = SessionStore.create(tmp_path, "blocked finals")
    adapter = FakeModelAdapter([reply("candidate") for _ in range(5)])
    loop = AgentLoop(
        session=session,
        model_adapter=adapter,
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        session_store=session_store,
        hook_runner=HookRunner([AlwaysBlockingHook()]),
    )

    result = loop.run()

    reopened = SessionStore.open(session_store.session_dir)
    assert result.status == "failed"
    assert reopened.load_messages() == [{"role": "system", "content": "system"}]
    assert json.loads(reopened.metadata_path.read_text(encoding="utf-8"))["status"] == (
        "failed"
    )
    assert adapter.index == 3


def test_valid_reply_resets_consecutive_invalid_reply_counter() -> None:
    session, tool_executor, _, _ = run_loop(
        [
            reply("bad 1", reason="empty_final_reply"),
            reply("bad 2", reason="empty_final_reply"),
            reply("valid", actions=[{"tool": "read_file"}]),
            reply("bad 3", reason="empty_final_reply"),
            reply("bad 4", reason="empty_final_reply"),
            reply("TASK COMPLETE"),
        ]
    )

    assert "correction:empty_final_reply" not in [
        message["content"] for message in session.messages
    ]
    assert session.messages[-1]["content"] == "TASK COMPLETE"
    assert tool_executor.actions == [{"tool": "read_file"}]


def test_invalid_replies_get_normal_correction_before_limit() -> None:
    session, _, _, _ = run_loop(
        [
            reply("bad 1", reason="empty_final_reply"),
            reply("bad 2", reason="non_final_too_many_tool_calls"),
            reply("TASK COMPLETE"),
        ]
    )

    assert not any(
        message.get("content", "").startswith("correction:")
        for message in session.messages
    )


def test_loop_stops_after_three_consecutive_invalid_replies() -> None:
    session, _, _, _ = run_loop(
        [
            reply("bad 1", reason="empty_final_reply"),
            reply("bad 2", reason="non_final_too_many_tool_calls"),
            reply("bad 3", reason="empty_final_reply"),
            reply("TASK COMPLETE"),
        ]
    )

    assert session.messages[-1] == {
        "role": "assistant",
        "content": (
            "Code Squid stopped after 3 consecutive invalid replies.\n"
            "Last invalid reply reason: empty_final_reply\n"
            "Explanation: the model replied with no tool call and no final content"
        ),
    }


def test_normal_completion_returns_complete_run_result() -> None:
    session = Session(repo_root=None)
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter([reply("TASK COMPLETE")]),
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
    )

    result = loop.run()

    assert result == RunResult(status="complete")


def test_cancellation_before_model_call_returns_interrupted() -> None:
    events = []
    session_store = FakeSessionStore()
    model_adapter = FakeModelAdapter([reply("TASK COMPLETE")])
    loop = AgentLoop(
        session=Session(repo_root=None),
        model_adapter=model_adapter,
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        session_store=session_store,
        on_event=events.append,
        should_cancel=lambda: True,
    )

    result = loop.run()

    assert result == RunResult(status="interrupted", reason="cancelled")
    assert model_adapter.index == 0
    assert session_store.statuses == ["interrupted"]
    assert [event["type"] for event in events] == ["run_interrupted", "run_finished"]
    assert events[0]["status"] == "interrupted"
    assert events[0]["reason"] == "cancelled"
    assert events[0]["requested"] is True
    assert events[1]["status"] == "interrupted"
    assert not any(
        entry["message"].get("role") == "tool" for entry in session_store.messages
    )


def test_cancellation_after_persisted_tool_call_appends_interrupted_result() -> None:
    session = Session(repo_root=None)
    session_store = FakeSessionStore()
    tool_executor = FakeToolExecutor()
    accepted_reply = tool_call_reply(
        "call_cancelled", args={"path": "README.md"}
    )
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter([accepted_reply]),
        tool_executor=tool_executor,
        tool_results=ToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        session_store=session_store,
        should_cancel=lambda: any(
            message.get("role") == "assistant" and message.get("tool_calls")
            for message in session.messages
        ),
    )

    result = loop.run()

    assert result == RunResult(status="interrupted", reason="cancelled")
    assert tool_executor.actions == []
    assert session.messages[-1]["role"] == "tool"
    assert session.messages[-1]["tool_call_id"] == "call_cancelled"
    assert json.loads(session.messages[-1]["content"]) == {
        "ok": False,
        "tool": "read_file",
        "error": "tool action interrupted",
        "reason": "interrupted before a tool result was available",
        "interrupted": True,
    }
    assert session_store.messages[-1]["message"] == session.messages[-1]


def test_interrupted_tool_pair_survives_saved_transcript_reload(tmp_path) -> None:
    session = Session(repo_root=tmp_path)
    session_store = SessionStore.create(tmp_path, "cancel tool")
    accepted_reply = tool_call_reply("call_saved", args={"path": "README.md"})
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter([accepted_reply]),
        tool_executor=FakeToolExecutor(),
        tool_results=ToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        session_store=session_store,
        should_cancel=lambda: any(
            message.get("role") == "assistant" and message.get("tool_calls")
            for message in session.messages
        ),
    )

    loop.run()

    loaded = SessionStore.open(session_store.session_dir).load_messages()
    assistant_index = next(
        index for index, message in enumerate(loaded) if message.get("tool_calls")
    )
    assert loaded[assistant_index]["tool_calls"][0]["id"] == "call_saved"
    assert loaded[assistant_index + 1]["role"] == "tool"
    assert loaded[assistant_index + 1]["tool_call_id"] == "call_saved"


def test_cancellation_after_tool_returns_persists_real_result() -> None:
    cancelled = False

    class CancellingToolExecutor(FakeToolExecutor):
        def execute(self, action: dict) -> dict:
            nonlocal cancelled
            result = super().execute(action)
            cancelled = True
            return {**result, "content": "actual file contents"}

    session = Session(repo_root=None)
    accepted_reply = tool_call_reply(
        "call_completed", content="Reading.", args={"path": "README.md"}
    )
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter([accepted_reply]),
        tool_executor=CancellingToolExecutor(),
        tool_results=ToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        should_cancel=lambda: cancelled,
    )

    result = loop.run()

    assert result == RunResult(status="interrupted", reason="cancelled")
    assert session.messages[-1]["tool_call_id"] == "call_completed"
    assert json.loads(session.messages[-1]["content"]) == {
        "ok": True,
        "tool": "read_file",
        "content": "actual file contents",
    }


def test_keyboard_interrupt_after_tool_call_persistence_repairs_pair_and_reraises() -> None:
    class InterruptingApprovalPolicy:
        mode = "never"

        def check(self, action: dict):
            raise KeyboardInterrupt

    session = Session(repo_root=None)
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter([tool_call_reply("call_keyboard")]),
        tool_executor=FakeToolExecutor(),
        tool_results=ToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        approval_policy=InterruptingApprovalPolicy(),
    )

    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("KeyboardInterrupt was not re-raised")

    assert session.messages[-1]["role"] == "tool"
    assert session.messages[-1]["tool_call_id"] == "call_keyboard"
    assert json.loads(session.messages[-1]["content"])["interrupted"] is True


def test_ordinary_failure_after_tool_call_persistence_repairs_pair_and_reraises(
    tmp_path,
) -> None:
    class FailingApprovalPolicy:
        mode = "never"

        def check(self, action: dict):
            raise RuntimeError("approval exploded")

    events = []
    session = Session(repo_root=tmp_path)
    session_store = SessionStore.create(tmp_path, "ordinary tool failure")
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter([tool_call_reply("call_failed")]),
        tool_executor=FakeToolExecutor(),
        tool_results=ToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        approval_policy=FailingApprovalPolicy(),
        on_event=events.append,
        session_store=session_store,
    )

    with pytest.raises(RuntimeError, match="approval exploded"):
        loop.run()

    assert session.messages[-1]["tool_call_id"] == "call_failed"
    assert json.loads(session.messages[-1]["content"])["failed"] is True
    loaded = SessionStore.open(session_store.session_dir).load_messages()
    assert loaded[-1] == session.messages[-1]
    assert [event["type"] for event in events][-2:] == ["run_failed", "run_finished"]


def test_cancellation_during_streamed_output_unwinds_as_interrupted() -> None:
    events = []
    loop = AgentLoop(
        session=Session(repo_root=None),
        model_adapter=FakeModelAdapter(
            [reply("unused")],
            streaming=True,
            deltas=["first", "second"],
        ),
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        stream_assistant_text=True,
        on_event=events.append,
        should_cancel=lambda: any(
            event["type"] == "assistant_delta" for event in events
        ),
    )

    result = loop.run()

    assert result == RunResult(status="interrupted", reason="cancelled")
    assert [event["type"] for event in events].count("assistant_delta") == 1
    assert "model_request_failed" not in [event["type"] for event in events]
    assert [event["type"] for event in events][-2:] == [
        "run_interrupted",
        "run_finished",
    ]


def test_normal_successful_tool_execution_still_persists_normal_result() -> None:
    accepted_reply = tool_call_reply("call_success", content="Reading.")
    action = accepted_reply["actions"][0]
    session = Session(repo_root=None)
    tool_executor = FakeToolExecutor()
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter([accepted_reply, reply("TASK COMPLETE")]),
        tool_executor=tool_executor,
        tool_results=ToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
    )

    result = loop.run()

    assert result == RunResult(status="complete")
    assert tool_executor.actions == [action]
    tool_message = next(
        message for message in session.messages if message["role"] == "tool"
    )
    assert tool_message["tool_call_id"] == "call_success"
    assert json.loads(tool_message["content"]) == {"ok": True, "tool": "read_file"}


def test_cancellation_after_validated_model_reply_prevents_tool_turn() -> None:
    events = []
    cancelled = False

    class CancellingAgent(FakeAgent):
        def validate_reply(self, reply: dict) -> dict:
            nonlocal cancelled
            validation = super().validate_reply(reply)
            cancelled = True
            return validation

    session = Session(repo_root=None)
    session_store = FakeSessionStore()
    tool_executor = FakeToolExecutor()
    display = NullDisplay()
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter(
            [reply("Using read_file.", actions=[{"tool": "read_file"}])]
        ),
        tool_executor=tool_executor,
        tool_results=FakeToolResults(),
        agent=CancellingAgent(),
        display=display,
        session_store=session_store,
        on_event=events.append,
        should_cancel=lambda: cancelled,
    )

    result = loop.run()

    event_types = [event["type"] for event in events]
    assert result == RunResult(status="interrupted", reason="cancelled")
    assert "assistant_message" not in event_types
    assert "tool_started" not in event_types
    assert tool_executor.actions == []
    assert display.assistant_messages == []
    assert session.messages == [{"role": "system", "content": "system"}]
    assert [entry["message"] for entry in session_store.messages] == [
        {"role": "system", "content": "system"}
    ]
    assert session_store.statuses == ["interrupted"]
    assert event_types[-2:] == ["run_interrupted", "run_finished"]


def test_repeated_invalid_replies_return_failed_run_result() -> None:
    session = Session(repo_root=None)
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter(
            [
                reply("bad 1", reason="empty_final_reply"),
                reply("bad 2", reason="non_final_too_many_tool_calls"),
                reply("bad 3", reason="empty_final_reply"),
            ]
        ),
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
    )

    result = loop.run()

    assert result == RunResult(
        status="failed",
        reason="empty_final_reply",
    )


def test_initial_and_valid_assistant_messages_are_persisted() -> None:
    _, _, session_store, _, _ = run_loop_with_store([reply("TASK COMPLETE")])

    assert [entry["message"] for entry in session_store.messages] == [
        {"role": "system", "content": "system"},
        {"role": "assistant", "content": "TASK COMPLETE"},
    ]


def test_failed_message_persistence_leaves_session_unchanged() -> None:
    class FailingStore:
        def append_message(self, message: dict, usage=None) -> None:
            raise RuntimeError("message persistence failed")

    session = Session(repo_root=None)
    original = {"role": "system", "content": "already committed"}
    session.add_message(original)

    with pytest.raises(RuntimeError, match="message persistence failed"):
        record_message(
            session,
            FailingStore(),
            {"role": "user", "content": "not committed"},
        )

    assert session.messages == [original]


def test_record_messages_keeps_order_and_commits_only_successful_prefix() -> None:
    class SecondAppendFails:
        def __init__(self) -> None:
            self.messages = []

        def append_message(self, message: dict, usage=None) -> None:
            if len(self.messages) == 1:
                raise RuntimeError("second append failed")
            self.messages.append(message)

    session = Session(repo_root=None)
    store = SecondAppendFails()
    first = {"role": "user", "content": "first"}
    second = {"role": "assistant", "content": "second"}

    with pytest.raises(RuntimeError, match="second append failed"):
        record_messages(session, store, first, second)

    assert store.messages == [first]
    assert session.messages == [first]


def test_failed_system_reconciliation_persistence_leaves_session_unchanged() -> None:
    class FailingStore:
        def append_system_message_reconciliation(self, message: dict) -> None:
            raise RuntimeError("reconciliation persistence failed")

    original_messages = [
        {"role": "system", "content": "old system"},
        {"role": "user", "content": "keep me"},
    ]
    session = Session(repo_root=None)
    session.add_messages(*original_messages)

    with pytest.raises(RuntimeError, match="reconciliation persistence failed"):
        reconcile_system_message(
            session,
            FailingStore(),
            {"role": "system", "content": "new system"},
        )

    assert session.messages == original_messages


def test_new_coding_session_sends_system_prompt_and_raw_task_without_runtime_wrapper() -> None:
    session = Session(repo_root=None)
    session_store = FakeSessionStore()
    model_adapter = FakeModelAdapter([reply("TASK COMPLETE")])
    agent = CodingAgent(task="build a notes app", tools={})
    expected_messages = agent.initial_messages()
    loop = AgentLoop(
        session=session,
        model_adapter=model_adapter,
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=agent,
        display=NullDisplay(),
        session_store=session_store,
    )

    loop.run()

    assert model_adapter.message_requests[0] == expected_messages
    assert session.messages[:2] == expected_messages
    assert [entry["message"] for entry in session_store.messages[:2]] == expected_messages


def test_resumed_coding_session_sends_prior_context_and_raw_follow_up_without_wrapper() -> None:
    session = Session(repo_root=None)
    canonical_messages = [
        {"role": "system", "content": "saved system"},
        {"role": "user", "content": "build a notes app"},
        {
            "role": "assistant",
            "content": "Reading the current implementation.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"notes.py"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "def list_notes(): ...",
        },
        {"role": "assistant", "content": "Initial report"},
        {"role": "user", "content": "add search"},
    ]
    session.add_messages(*canonical_messages)
    session_store = FakeSessionStore()
    session_store.append_message(session.messages[-1])
    model_adapter = FakeModelAdapter([reply("Updated report")])
    agent = CodingAgent(task="add search", tools={})
    loop = AgentLoop(
        session=session,
        model_adapter=model_adapter,
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=agent,
        display=NullDisplay(),
        session_store=session_store,
    )

    loop.run()

    expected_messages = [agent.initial_messages()[0], *canonical_messages[1:]]
    assert model_adapter.message_requests[0] == expected_messages
    assert session.messages[: len(expected_messages)] == expected_messages
    assert session_store.system_reconciliations == [agent.initial_messages()[0]]
    assert session_store.messages[0]["message"] == {
        "role": "user",
        "content": "add search",
    }


def test_preloaded_session_does_not_get_duplicate_initial_messages() -> None:
    session = Session(repo_root=None)
    session.add_messages(
        {"role": "system", "content": "saved system"},
        {"role": "user", "content": "saved follow-up"},
    )
    session_store = FakeSessionStore()
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter([reply("TASK COMPLETE")]),
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        session_store=session_store,
    )

    loop.run()

    assert session.messages == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "saved follow-up"},
        {"role": "assistant", "content": "TASK COMPLETE"},
    ]
    assert session_store.system_reconciliations == [
        {"role": "system", "content": "system"}
    ]
    assert [entry["message"] for entry in session_store.messages] == [
        {"role": "assistant", "content": "TASK COMPLETE"},
    ]


@pytest.mark.parametrize(
    ("previous_specialization", "current_specialization"),
    [
        ("default", "Mod A"),
        ("Mod A", "Mod B"),
        ("Mod A", "default"),
        ("Skill A", "Skill B"),
    ],
)
def test_existing_session_adopts_current_specialization_between_runs(
    previous_specialization: str,
    current_specialization: str,
) -> None:
    class SpecializedAgent(FakeAgent):
        def initial_messages(self) -> list[dict]:
            return [
                {
                    "role": "system",
                    "content": f"instructions for {current_specialization}",
                }
            ]

    prior_history = [
        {"role": "user", "content": "original task"},
        {"role": "assistant", "content": "original result"},
        {"role": "user", "content": "follow up"},
    ]
    session = Session(repo_root=None)
    session.add_messages(
        {
            "role": "system",
            "content": f"instructions for {previous_specialization}",
        },
        *prior_history,
    )
    model_adapter = FakeModelAdapter([reply("TASK COMPLETE")])
    loop = AgentLoop(
        session=session,
        model_adapter=model_adapter,
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=SpecializedAgent(),
        display=NullDisplay(),
    )

    loop.run()

    assert model_adapter.message_requests[0] == [
        {
            "role": "system",
            "content": f"instructions for {current_specialization}",
        },
        *prior_history,
    ]


def test_compactor_is_called_before_thinking_and_next_model_reply() -> None:
    events = []
    session = Session(repo_root=None)
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter([reply("TASK COMPLETE")], events=events),
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(events=events),
        compactor=FakeCompactor(events=events),
    )

    loop.run()

    assert events[:3] == ["compact", "thinking", "respond"]


def test_compaction_metadata_is_persisted_when_store_exists() -> None:
    metadata = {"estimated_tokens_before": 10, "estimated_tokens_after": 4}
    _, _, session_store, _, _ = run_loop_with_store(
        [reply("TASK COMPLETE")],
        compactor=FakeCompactor(metadata),
    )

    assert session_store.compactions == [metadata]


def test_compaction_without_metadata_is_not_persisted() -> None:
    _, _, session_store, _, _ = run_loop_with_store(
        [reply("TASK COMPLETE")],
        compactor=FakeCompactor(None),
    )

    assert session_store.compactions == []


def test_compaction_lifecycle_is_published_through_loop_events() -> None:
    events = []

    run_loop(
        [reply("TASK COMPLETE")],
        compactor=LifecycleCompactor(),
        on_event=events.append,
    )

    compaction_events = [
        event for event in events if event["type"].startswith("compaction_")
    ]
    assert [event["type"] for event in compaction_events] == [
        "compaction_started",
        "compaction_applied",
    ]
    assert compaction_events[0]["messages_before"] == 4
    assert compaction_events[1]["messages_after"] == 2


def test_failed_compaction_boundary_is_published_through_loop_events() -> None:
    events = []
    loop = AgentLoop(
        session=Session(repo_root=None),
        model_adapter=FakeModelAdapter([]),
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=StopImmediatelyAgent(),
        display=NullDisplay(),
        on_event=events.append,
    )

    loop._persist_compaction_event(
        {"type": "compaction_failed", "stage": "model_request"}
    )

    assert [event["type"] for event in events] == ["compaction_failed"]


def test_compaction_hook_observes_already_active_compacted_transcript() -> None:
    class ApplyingCompactor:
        def maybe_compact(
            self,
            session,
            *,
            event_callback=None,
            post_apply_callback=None,
        ) -> None:
            event = {"type": "compaction_applied", "messages_after": 1}
            event_callback(event)
            session.messages[:] = [{"role": "assistant", "content": "summary"}]
            post_apply_callback(event)

    observations = []
    store = FakeSessionStore()

    class ObservingHook:
        def compaction_applied(self, ctx) -> None:
            observations.append((list(store.compactions), list(session.messages)))

    session = Session(repo_root=None)
    session.add_message({"role": "user", "content": "old history"})
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter([]),
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=StopImmediatelyAgent(),
        display=NullDisplay(),
        session_store=store,
        compactor=ApplyingCompactor(),
        hook_runner=HookRunner([ObservingHook()]),
    )

    loop._maybe_compact()

    assert observations == [
        (
            [{"messages_after": 1}],
            [{"role": "assistant", "content": "summary"}],
        )
    ]


def test_compactor_is_not_called_when_session_already_stopped() -> None:
    compactor = FakeCompactor()
    session = Session(repo_root=None)
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter([]),
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=StopImmediatelyAgent(),
        display=NullDisplay(),
        compactor=compactor,
    )

    loop.run()

    assert compactor.sessions == []


def test_compactor_is_not_called_after_step_limit_stops_loop() -> None:
    compactor = FakeCompactor()

    run_loop_with_store(
        [
            reply("valid", actions=[{"tool": "read_file"}]),
            reply("TASK COMPLETE"),
        ],
        max_tool_steps=1,
        compactor=compactor,
    )

    assert len(compactor.sessions) == 1


def test_correction_messages_are_runtime_only_and_rejected_replies_are_not_persisted() -> None:
    _, _, session_store, model_adapter, _ = run_loop_with_store(
        [
            reply("bad 1", reason="empty_final_reply"),
            reply("TASK COMPLETE"),
        ]
    )

    persisted_messages = [entry["message"] for entry in session_store.messages]

    assert {"role": "user", "content": "correction:empty_final_reply"} not in persisted_messages
    assert {"role": "assistant", "content": "bad 1"} not in persisted_messages
    assert model_adapter.message_requests == [
        [{"role": "system", "content": "system"}],
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "correction:empty_final_reply"},
        ],
    ]


def test_malformed_reply_persists_raw_reply_and_exact_rejection_error_only_as_diagnostics() -> None:
    malformed = {
        "message": {"role": "assistant", "content": "Trying."},
        "actions": [],
        "reason": "malformed_tool_call",
        "format_error": "Tool arguments were not valid JSON: expected value",
        "recoverable_tool_name": "read_file",
        "recoverable_raw_arguments": '{"path":',
        "raw_reply": {
            "provider": "openrouter",
            "model": "example/model",
            "assistant": {
                "content": "Trying.",
                "raw_tool_calls": [
                    {
                        "id": "call_bad",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":',
                        },
                    }
                ],
            },
        },
    }
    session, _, session_store, _, _ = run_loop_with_store(
        [malformed, reply("TASK COMPLETE")]
    )

    assert session_store.model_replies[0]["reply"] == malformed["raw_reply"]
    assert session_store.reply_rejections == [
        {
            "reason": "malformed_tool_call",
            "error": "Tool arguments were not valid JSON: expected value",
            "consecutive_invalid_attempt": 1,
            "raw_reply_id": "raw_1",
            "recoverable_tool_name": "read_file",
            "recoverable_raw_arguments": '{"path":',
        }
    ]
    assert {"role": "assistant", "content": "Trying."} not in session.messages


def test_all_three_repeated_invalid_replies_are_preserved_individually() -> None:
    invalid_replies = [
        {
            **reply(f"bad {attempt}", reason="malformed_tool_call"),
            "format_error": f"parse error {attempt}",
            "raw_reply": {
                "assistant": {
                    "content": f"bad {attempt}",
                    "raw_tool_calls": [],
                }
            },
        }
        for attempt in range(1, 4)
    ]

    session, _, session_store, model_adapter, _ = run_loop_with_store(invalid_replies)

    assert len(session_store.model_replies) == 3
    assert [item["consecutive_invalid_attempt"] for item in session_store.reply_rejections] == [
        1,
        2,
        3,
    ]
    assert [item["error"] for item in session_store.reply_rejections] == [
        "parse error 1",
        "parse error 2",
        "parse error 3",
    ]
    assert model_adapter.index == 3
    assert session.messages[-1]["content"].startswith(
        "Code Squid stopped after 3 consecutive invalid replies."
    )


def test_valid_tool_call_reply_remains_canonical_and_keeps_tool_call_id() -> None:
    tool_message = {
        "role": "assistant",
        "content": "Reading.",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
            }
        ],
    }
    session, tool_executor, session_store, _, _ = run_loop_with_store(
        [
                {
                    "message": tool_message,
                    "actions": [
                    {
                        "tool": "read_file",
                        "args": {"path": "README.md"},
                        "tool_call_id": "call_1",
                            "raw_arguments": '{"path":"README.md"}',
                        }
                    ],
                    "raw_reply": {
                        "assistant": {
                            "content": "Reading.",
                            "raw_tool_calls": tool_message["tool_calls"],
                        }
                    },
                },
            reply("TASK COMPLETE"),
        ]
    )

    assert tool_message in session.messages
    assert tool_message in [entry["message"] for entry in session_store.messages]
    assert tool_executor.actions[0]["tool_call_id"] == "call_1"
    assert session_store.reply_rejections == []


def test_invalid_assistant_reply_is_not_displayed_in_normal_mode() -> None:
    _, _, _, display = run_loop(
        [
            reply("bad 1", reason="empty_final_reply"),
            reply("TASK COMPLETE"),
        ]
    )

    assert display.assistant_messages == []
    assert display.final_messages == ["TASK COMPLETE"]
    assert display.rejected_replies == ["empty_final_reply"]
    assert display.debug_replies[0]["message"]["content"] == "bad 1"


def test_no_tool_final_reply_uses_final_display_path() -> None:
    _, _, _, display = run_loop([reply("TASK COMPLETE")])

    assert display.final_messages == ["TASK COMPLETE"]
    assert display.assistant_messages == []


def test_one_tool_reply_uses_normal_assistant_display_path() -> None:
    _, _, _, display = run_loop(
        [
            reply("Using read_file.", actions=[{"tool": "read_file"}]),
            reply("TASK COMPLETE"),
        ]
    )

    assert display.assistant_messages == ["Using read_file."]
    assert display.final_messages == ["TASK COMPLETE"]


def test_reasoning_is_presented_and_emitted_before_matching_narration() -> None:
    events = []
    first_reply = reply(
        "Using read_file.",
        actions=[{"tool": "read_file"}],
    )
    first_reply["reasoning"] = "The relevant code is probably in README.md."

    _, _, _, display = run_loop(
        [first_reply, reply("TASK COMPLETE")],
        on_event=events.append,
    )

    assert display.reasoning_messages == [
        "The relevant code is probably in README.md."
    ]
    event_types = [event["type"] for event in events]
    assert event_types.index("assistant_reasoning") < event_types.index(
        "assistant_message"
    )
    assert event_types.index("assistant_reasoning") < event_types.index("tool_started")


def test_reply_without_reasoning_keeps_existing_display_and_events() -> None:
    events = []

    _, _, _, display = run_loop(
        [reply("TASK COMPLETE")],
        on_event=events.append,
    )

    assert display.reasoning_messages == []
    assert "assistant_reasoning" not in [event["type"] for event in events]


def test_loop_emits_basic_run_events(tmp_path) -> None:
    events = []
    target = tmp_path / "README.md"

    run_loop(
        [
            reply(
                "Reading file.",
                actions=[{"tool": "read_file", "args": {"path": str(target)}}],
            ),
            reply("TASK COMPLETE"),
        ],
        repo_root=tmp_path,
        on_event=events.append,
    )

    event_types = [event["type"] for event in events]
    assert event_types == [
        "run_started",
        "hooks_configured",
        "model_request_started",
        "model_request_finished",
        "assistant_message",
        "tool_started",
        "tool_finished",
        "model_request_started",
        "model_request_finished",
        "final_message",
        "run_finished",
    ]
    assert events[0]["repo_root"] == str(tmp_path)
    assert events[0]["task"] == ""
    assert events[1]["hooks"] == []
    assert events[4]["message"] == "Reading file."
    assert events[4]["text"] == "Reading file."
    assert events[4]["phase"] == "narration"
    assert {key: value for key, value in events[4].items() if key != "timestamp"} == {
        "session_id": None,
        "type": "assistant_message",
        "phase": "narration",
        "message": "Reading file.",
        "text": "Reading file.",
    }
    assert events[5]["tool"] == "read_file"
    assert events[5]["path"] == str(target)
    assert events[6]["ok"] is True
    assert events[6]["path"] == str(target)
    assert events[9]["message"] == "TASK COMPLETE"
    assert events[9]["text"] == "TASK COMPLETE"
    assert events[9]["phase"] == "final"
    assert {key: value for key, value in events[9].items() if key != "timestamp"} == {
        "session_id": None,
        "type": "final_message",
        "phase": "final",
        "message": "TASK COMPLETE",
        "text": "TASK COMPLETE",
    }
    assert events[10]["status"] == "complete"
    assert all("timestamp" in event for event in events)


def test_streaming_enabled_emits_started_deltas_and_full_assistant_message() -> None:
    events = []
    session = Session(repo_root=None)
    model_adapter = FakeModelAdapter(
        [
            reply("Reading file.", actions=[{"tool": "read_file"}]),
            reply("TASK COMPLETE"),
        ],
        streaming=True,
        deltas=["Reading ", "file."],
    )
    loop = AgentLoop(
        session=session,
        model_adapter=model_adapter,
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        stream_assistant_text=True,
        on_event=events.append,
    )

    loop.run()

    event_types = [event["type"] for event in events]
    assert event_types[:5] == [
        "run_started",
        "hooks_configured",
        "model_request_started",
        "assistant_message_started",
        "assistant_delta",
    ]
    assert [event["delta"] for event in events if event["type"] == "assistant_delta"] == [
        "Reading ",
        "file.",
    ]
    started_event = next(
        event for event in events if event["type"] == "assistant_message_started"
    )
    delta_events = [event for event in events if event["type"] == "assistant_delta"]
    assistant_event = next(
        event for event in events if event["type"] == "assistant_message"
    )
    assert started_event["phase"] == "narration"
    assert all(event["phase"] == "narration" for event in delta_events)
    assert assistant_event["phase"] == "narration"
    assert assistant_event["message"] == "Reading file."
    assert {
        key: value for key, value in started_event.items() if key != "timestamp"
    } == {
        "session_id": None,
        "type": "assistant_message_started",
        "phase": "narration",
    }
    assert [
        {key: value for key, value in event.items() if key != "timestamp"}
        for event in delta_events
    ] == [
        {
            "session_id": None,
            "type": "assistant_delta",
            "phase": "narration",
            "delta": "Reading ",
        },
        {
            "session_id": None,
            "type": "assistant_delta",
            "phase": "narration",
            "delta": "file.",
        },
    ]


def test_streaming_reasoning_precedes_narration_and_tool_execution() -> None:
    events = []
    callback_events = []

    def on_event(event: dict) -> None:
        events.append(event)
        callback_events.append(event["type"])

    first_reply = reply("Using read_file.", actions=[{"tool": "read_file"}])
    first_reply["reasoning"] = "Inspect the repository first."
    model_adapter = FakeModelAdapter(
        [first_reply, reply("TASK COMPLETE")],
        events=callback_events,
        streaming=True,
        reasoning_deltas=["Inspect the ", "repository first."],
        deltas=["Using ", "read_file."],
    )
    tool_executor = FakeToolExecutor(events=callback_events)
    loop = AgentLoop(
        session=Session(repo_root=None),
        model_adapter=model_adapter,
        tool_executor=tool_executor,
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        stream_assistant_text=True,
        on_event=on_event,
    )

    loop.run()

    reasoning_deltas = [
        event for event in events if event["type"] == "assistant_reasoning_delta"
    ]
    assert [event["delta"] for event in reasoning_deltas] == [
        "Inspect the ",
        "repository first.",
    ]
    assert all(event["phase"] == "reasoning" for event in reasoning_deltas)
    first_reasoning = callback_events.index("assistant_reasoning_delta")
    first_narration = callback_events.index("assistant_delta")
    completed_reasoning = callback_events.index("assistant_reasoning")
    narration = callback_events.index("assistant_message")
    tool_started = callback_events.index("tool_started")
    assert first_reasoning < first_narration
    assert completed_reasoning < narration
    assert narration < tool_started < callback_events.index("execute:read_file")


def test_streaming_enabled_emits_tool_preparing_before_message_and_tool_started() -> None:
    events = []
    session = Session(repo_root=None)
    model_adapter = FakeModelAdapter(
        [
            reply(
                "Writing file.",
                actions=[
                    {
                        "tool": "write_file",
                        "tool_call_id": "call_1",
                        "args": {"path": "/tmp/a.txt", "content": "secret"},
                    }
                ],
            ),
            reply("TASK COMPLETE"),
        ],
        streaming=True,
        deltas=["Writing file."],
        tool_preparing_events=[
            {
                "tool": "write_file",
                "tool_call_index": 0,
                "tool_call_id": "call_1",
            }
        ],
    )
    loop = AgentLoop(
        session=session,
        model_adapter=model_adapter,
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        stream_assistant_text=True,
        on_event=events.append,
    )

    loop.run()

    event_types = [event["type"] for event in events]
    assert event_types[:10] == [
        "run_started",
        "hooks_configured",
        "model_request_started",
        "assistant_message_started",
        "assistant_delta",
        "tool_preparing",
        "model_request_finished",
        "assistant_message",
        "tool_started",
        "tool_finished",
    ]
    preparing_index = event_types.index("tool_preparing")
    assistant_index = event_types.index("assistant_message")
    started_index = event_types.index("tool_started")
    assert preparing_index < assistant_index
    assert preparing_index < started_index

    tool_preparing = events[preparing_index]
    assert tool_preparing["tool"] == "write_file"
    assert tool_preparing["tool_call_index"] == 0
    assert tool_preparing["tool_call_id"] == "call_1"
    assert "args" not in tool_preparing
    assert "content" not in tool_preparing
    assert "phase" not in tool_preparing
    assert {
        key: value for key, value in tool_preparing.items() if key != "timestamp"
    } == {
        "session_id": None,
        "type": "tool_preparing",
        "tool": "write_file",
        "tool_call_index": 0,
        "tool_call_id": "call_1",
    }


def test_streaming_enabled_does_not_execute_tool_until_full_reply_returns_and_validates() -> None:
    events = []
    callback_events = []

    def on_event(event: dict) -> None:
        events.append(event)
        callback_events.append(event["type"])

    session = Session(repo_root=None)
    model_adapter = FakeModelAdapter(
        [
            reply("Using read_file.", actions=[{"tool": "read_file"}]),
            reply("TASK COMPLETE"),
        ],
        events=callback_events,
        streaming=True,
        deltas=["Using ", "read_file."],
    )
    tool_executor = FakeToolExecutor(events=callback_events)
    loop = AgentLoop(
        session=session,
        model_adapter=model_adapter,
        tool_executor=tool_executor,
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        stream_assistant_text=True,
        on_event=on_event,
    )

    loop.run()

    assert callback_events.index("respond_streaming:end") < callback_events.index(
        "assistant_message"
    )
    assert callback_events.index("assistant_message") < callback_events.index(
        "tool_started"
    )
    assert callback_events.index("tool_started") < callback_events.index(
        "execute:read_file"
    )
    assert tool_executor.actions == [{"tool": "read_file"}]


def test_streaming_enabled_but_unsupported_provider_falls_back_to_non_streaming() -> None:
    events = []
    model_events = []
    session = Session(repo_root=None)
    model_adapter = FakeModelAdapter(
        [reply("TASK COMPLETE")],
        events=model_events,
        streaming=False,
    )
    loop = AgentLoop(
        session=session,
        model_adapter=model_adapter,
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        stream_assistant_text=True,
        on_event=events.append,
    )

    loop.run()

    assert model_events == ["respond"]
    assert "assistant_message_started" not in [event["type"] for event in events]
    assert "assistant_delta" not in [event["type"] for event in events]
    assert "tool_preparing" not in [event["type"] for event in events]


def test_non_streaming_presentation_still_uses_cancellable_provider_stream() -> None:
    events = []
    model_events = []
    loop = AgentLoop(
        session=Session(repo_root=None),
        model_adapter=FakeModelAdapter(
            [reply("TASK COMPLETE")],
            events=model_events,
            streaming=True,
            deltas=["TASK ", "COMPLETE"],
        ),
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        stream_assistant_text=False,
        on_event=events.append,
        should_cancel=lambda: False,
    )

    result = loop.run()

    assert result == RunResult(status="complete")
    assert model_events == ["respond_streaming:start", "respond_streaming:end"]
    event_types = [event["type"] for event in events]
    assert "assistant_message_started" not in event_types
    assert "assistant_delta" not in event_types


def test_assistant_message_event_includes_full_message() -> None:
    events = []
    assistant_text = f"  {'a' * 2100}  "

    _, _, _, display = run_loop(
        [
            reply(
                assistant_text,
                actions=[{"tool": "read_file"}],
            ),
            reply("TASK COMPLETE"),
        ],
        on_event=events.append,
    )

    assistant_event = next(
        event for event in events if event["type"] == "assistant_message"
    )
    assert assistant_event["message"] == assistant_text
    assert assistant_event["text"] == assistant_event["message"]
    assert assistant_event["phase"] == "narration"
    assert assistant_event["message"] == display.assistant_messages[0]


def test_final_message_event_includes_full_message() -> None:
    events = []
    final_text = f"  Long final summary\n{'f' * 4100}  "

    _, _, _, display = run_loop(
        [
            reply(final_text),
            reply("TASK COMPLETE"),
        ],
        on_event=events.append,
    )

    final_event = next(event for event in events if event["type"] == "final_message")
    assert final_event["message"] == final_text
    assert final_event["text"] == final_event["message"]
    assert final_event["phase"] == "final"
    assert final_event["message"] == display.final_messages[0]


def test_loop_emits_reply_rejected_event() -> None:
    events = []

    run_loop(
        [
            reply("bad 1", reason="empty_final_reply"),
            reply("TASK COMPLETE"),
        ],
        on_event=events.append,
    )

    rejected_events = [
        event for event in events if event["type"] == "reply_rejected"
    ]
    assert rejected_events == [
        {
            "type": "reply_rejected",
            "session_id": None,
            "reason": "empty_final_reply",
            "timestamp": rejected_events[0]["timestamp"],
        }
    ]


def test_event_callback_failure_does_not_crash_loop() -> None:
    def raise_error(event: dict) -> None:
        raise RuntimeError("boom")

    session, _, _, display = run_loop(
        [reply("TASK COMPLETE")],
        on_event=raise_error,
    )

    assert session.messages[-1] == {"role": "assistant", "content": "TASK COMPLETE"}
    assert display.final_messages == ["TASK COMPLETE"]


def test_invalid_assistant_reply_is_displayed_in_debug_mode() -> None:
    session = Session(repo_root=None)
    display = NullDisplay()
    display.debug = True
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter(
            [
                reply("bad 1", reason="empty_final_reply"),
                reply("TASK COMPLETE"),
            ]
        ),
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=display,
    )

    loop.run()

    assert display.assistant_messages == ["bad 1"]
    assert display.final_messages == ["TASK COMPLETE"]
    assert display.rejected_replies == ["empty_final_reply"]


def test_failure_message_is_persisted_after_repeated_invalid_replies() -> None:
    _, _, session_store, _, _ = run_loop_with_store(
        [
            reply("bad 1", reason="empty_final_reply"),
            reply("bad 2", reason="non_final_too_many_tool_calls"),
            reply("bad 3", reason="empty_final_reply"),
        ]
    )

    assert session_store.messages[-1]["message"] == {
        "role": "assistant",
        "content": (
            "Code Squid stopped after 3 consecutive invalid replies.\n"
            "Last invalid reply reason: empty_final_reply\n"
            "Explanation: the model replied with no tool call and no final content"
        ),
    }
    assert {"role": "assistant", "content": "bad 3"} not in [
        entry["message"] for entry in session_store.messages
    ]


def test_tool_result_messages_are_persisted() -> None:
    _, _, session_store, _, _ = run_loop_with_store(
        [
            reply("valid", actions=[{"tool": "read_file"}]),
            reply("TASK COMPLETE"),
        ]
    )

    assert {"role": "tool", "content": "read_file"} in [
        entry["message"] for entry in session_store.messages
    ]


def test_approval_denial_does_not_execute_tool_action() -> None:
    events = []
    session, tool_executor, _, _ = run_loop(
        [
            reply(
                "valid",
                actions=[{"tool": "read_file", "tool_call_id": "call_1"}],
            ),
            reply("TASK COMPLETE"),
        ],
        approval_policy=ApprovalPolicy(
            "always",
            prompter=FakePrompter(ApprovalDecision(approved=False)),
        ),
        on_event=events.append,
    )

    assert tool_executor.actions == []
    assert session.messages[-1]["content"] == "TASK COMPLETE"
    event_types = [event["type"] for event in events]
    assert "tool_started" not in event_types
    tool_finished = next(event for event in events if event["type"] == "tool_finished")
    assert tool_finished["ok"] is False
    assert tool_finished["tool_call_id"] == "call_1"


def test_guarded_allows_inside_workspace_read_file_without_approval(tmp_path) -> None:
    target = tmp_path / "README.md"
    target.write_text("hello", encoding="utf-8")
    prompter = FakePrompter(ApprovalDecision(approved=False))

    _, tool_executor, _, _ = run_loop(
        [
            reply("valid", actions=[{"tool": "read_file", "args": {"path": str(target)}}]),
            reply("TASK COMPLETE"),
        ],
        approval_policy=ApprovalPolicy("guarded", prompter=prompter),
        repo_root=tmp_path,
    )

    assert prompter.actions == []
    assert tool_executor.actions == [
        {"tool": "read_file", "args": {"path": str(target)}}
    ]


def test_guarded_allows_inside_workspace_write_file_without_approval(tmp_path) -> None:
    target = tmp_path / "README.md"
    prompter = FakePrompter(ApprovalDecision(approved=False))

    _, tool_executor, _, _ = run_loop(
        [
            reply(
                "valid",
                actions=[
                    {
                        "tool": "write_file",
                        "args": {"path": str(target), "content": "hello"},
                    }
                ],
            ),
            reply("TASK COMPLETE"),
        ],
        approval_policy=ApprovalPolicy("guarded", prompter=prompter),
        repo_root=tmp_path,
    )

    assert prompter.actions == []
    assert tool_executor.actions == [
        {"tool": "write_file", "args": {"path": str(target), "content": "hello"}}
    ]


def test_guarded_allows_inside_workspace_apply_patch_without_approval(tmp_path) -> None:
    target = tmp_path / "README.md"
    target.write_text("hello", encoding="utf-8")
    prompter = FakePrompter(ApprovalDecision(approved=False))

    _, tool_executor, _, _ = run_loop(
        [
            reply(
                "valid",
                actions=[
                    {
                        "tool": "apply_patch",
                        "args": {
                            "path": str(target),
                            "old_text": "hello",
                            "new_text": "goodbye",
                        },
                    }
                ],
            ),
            reply("TASK COMPLETE"),
        ],
        approval_policy=ApprovalPolicy("guarded", prompter=prompter),
        repo_root=tmp_path,
    )

    assert prompter.actions == []
    assert tool_executor.actions == [
        {
            "tool": "apply_patch",
            "args": {
                "path": str(target),
                "old_text": "hello",
                "new_text": "goodbye",
            },
        }
    ]


def test_guarded_asks_for_outside_workspace_file_tool(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    prompter = FakePrompter(ApprovalDecision(approved=False))

    _, tool_executor, _, _ = run_loop(
        [
            reply(
                "valid",
                actions=[{"tool": "read_file", "args": {"path": str(outside)}}],
            ),
            reply("TASK COMPLETE"),
        ],
        approval_policy=ApprovalPolicy("guarded", prompter=prompter),
        repo_root=workspace,
    )

    assert tool_executor.actions == []
    assert len(prompter.actions) == 1
    assert prompter.actions[0]["tool"] == "read_file"
    assert "outside the workspace root" in prompter.actions[0]["approval_reason"]


def test_guarded_asks_for_every_untrusted_run_command(tmp_path) -> None:
    prompter = FakePrompter(ApprovalDecision(approved=False))

    _, tool_executor, _, _ = run_loop(
        [
            reply("valid", actions=[{"tool": "run_command", "args": {"command": "pwd"}}]),
            reply("TASK COMPLETE"),
        ],
        approval_policy=ApprovalPolicy("guarded", prompter=prompter),
        repo_root=tmp_path,
    )

    assert tool_executor.actions == []
    assert prompter.actions[0]["command_identity"] == "pwd"


def test_guarded_trusted_command_runs_without_approval(tmp_path) -> None:
    prompter = FakePrompter(ApprovalDecision(approved=False))

    _, tool_executor, _, _ = run_loop(
        [
            reply(
                "valid",
                actions=[{"tool": "run_command", "args": {"command": "mkdir build"}}],
            ),
            reply("TASK COMPLETE"),
        ],
        approval_policy=ApprovalPolicy(
            "guarded", prompter=prompter, trusted_commands=("mkdir",)
        ),
        repo_root=tmp_path,
    )

    assert prompter.actions == []
    assert tool_executor.actions == [
        {"tool": "run_command", "args": {"command": "mkdir build"}}
    ]


def test_hook_preflight_execution_and_result_order_is_preserved(tmp_path) -> None:
    events: list[str] = []

    class RecordingHook:
        def before_tool_execute(self, context) -> None:
            events.append("before_tool_execute")

        def tool_result(self, context) -> None:
            events.append("tool_result")

    class RecordingPreflight:
        def check(self, action, *, additional_approval_reason=""):
            events.append("preflight")
            return ActionPreflightDecision(action, action)

    loop = AgentLoop(
        session=Session(repo_root=tmp_path),
        model_adapter=FakeModelAdapter(
            [
                reply("valid", actions=[{"tool": "read_file"}]),
                reply("TASK COMPLETE"),
            ]
        ),
        tool_executor=FakeToolExecutor(repo_root=tmp_path, events=events),
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        hook_runner=HookRunner([RecordingHook()]),
        action_preflight=RecordingPreflight(),
    )

    loop.run()

    assert events == [
        "before_tool_execute",
        "preflight",
        "execute:read_file",
        "tool_result",
    ]


def test_tool_result_context_is_transient_and_updated_output_is_persisted(
    tmp_path,
) -> None:
    guidance = "Use the new result to choose a different action."

    class ResultHook:
        def tool_result(self, context: ToolResultContext) -> HookResponse:
            return HookResponse(
                updated_tool_output={**context.result, "hook_updated": True},
                additional_context=guidance,
            )

    session = Session(repo_root=tmp_path)
    session_store = FakeSessionStore()
    model_adapter = FakeModelAdapter(
        [
            tool_call_reply("call_1"),
            reply("TASK COMPLETE"),
        ]
    )
    loop = AgentLoop(
        session=session,
        model_adapter=model_adapter,
        tool_executor=FakeToolExecutor(repo_root=tmp_path),
        tool_results=ToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        session_store=session_store,
        hook_runner=HookRunner([ResultHook()]),
    )

    result = loop.run()

    assert result == RunResult(status="complete")
    assert guidance not in str(model_adapter.message_requests[0])
    assert model_adapter.message_requests[1][-1] == {
        "role": "user",
        "content": guidance,
    }
    assert guidance not in str(session.messages)
    assert guidance not in str(session_store.messages)

    tool_messages = [message for message in session.messages if message["role"] == "tool"]
    assert len(tool_messages) == 1
    assert json.loads(tool_messages[0]["content"]) == {
        "ok": True,
        "tool": "read_file",
        "hook_updated": True,
    }
    assert tool_messages[0] in [entry["message"] for entry in session_store.messages]


def test_successful_model_request_lifecycle_is_persisted(tmp_path) -> None:
    session_store = SessionStore.create(tmp_path, "successful request")
    model_adapter = FakeModelAdapter([reply("TASK COMPLETE")])
    model_adapter.reasoning_mode = "low"
    model_adapter.reasoning_token_budget = 4_000
    loop = AgentLoop(
        session=Session(repo_root=tmp_path),
        model_adapter=model_adapter,
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        session_store=session_store,
    )

    assert loop.run() == RunResult(status="complete")

    entries = [
        json.loads(line)
        for line in session_store.jsonl_path.read_text(encoding="utf-8").splitlines()
    ]
    lifecycle = [entry for entry in entries if entry["type"].startswith("model_request_")]
    assert [entry["type"] for entry in lifecycle] == [
        "model_request_started",
        "model_request_finished",
    ]
    assert lifecycle[0]["provider"] == "fake-provider"
    assert lifecycle[0]["model"] == "fake-model"
    assert lifecycle[0]["reasoning_mode"] == "low"
    assert lifecycle[0]["reasoning_token_budget"] == 4_000
    assert lifecycle[0]["message_count"] == 1
    assert lifecycle[0]["estimated_request_tokens"] > 0
    assert lifecycle[1]["duration_ms"] >= 0
    assert all("content" not in entry for entry in lifecycle)


def test_failed_model_request_leaves_durable_failure_events(tmp_path) -> None:
    class FailingModelAdapter(FakeModelAdapter):
        def respond(self, messages: list[dict], tool_definitions: list[dict]) -> dict:
            raise RuntimeError("inference exploded")

    session_store = TrackingSessionStore.create(tmp_path, "failed request")
    loop = AgentLoop(
        session=Session(repo_root=tmp_path),
        model_adapter=FailingModelAdapter([]),
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        session_store=session_store,
    )

    with pytest.raises(RuntimeError, match="inference exploded"):
        loop.run()

    entries = [
        json.loads(line)
        for line in session_store.jsonl_path.read_text(encoding="utf-8").splitlines()
    ]
    failed_request = next(
        entry for entry in entries if entry["type"] == "model_request_failed"
    )
    run_failed = next(entry for entry in entries if entry["type"] == "run_failed")
    assert failed_request["error_type"] == "RuntimeError"
    assert failed_request["error_message"] == "inference exploded"
    assert failed_request["duration_ms"] >= 0
    assert run_failed["stage"] == "model_request"
    assert run_failed["error_type"] == "RuntimeError"
    assert session_store.operations.index("event:run_failed") < (
        session_store.operations.index("status:failed")
    )
    assert json.loads(session_store.metadata_path.read_text(encoding="utf-8"))["status"] == (
        "failed"
    )


def _wrapped_model_error(error: Exception) -> ModelRequestError:
    wrapped = ModelRequestError(f"Model request failed: {type(error).__name__}: {error}")
    wrapped.__cause__ = error
    return wrapped


class SequencedRequestAdapter(FakeModelAdapter):
    """Return replies or raise failures while retaining every request snapshot."""

    def respond(self, messages: list[dict], tool_definitions: list[dict]) -> dict:
        self.message_requests.append([dict(message) for message in messages])
        outcome = self.replies[self.index]
        self.index += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_transient_model_failure_retries_same_turn_then_succeeds(tmp_path) -> None:
    session_store = SessionStore.create(tmp_path, "retry request")
    model_adapter = SequencedRequestAdapter(
        [_wrapped_model_error(TimeoutError("provider timed out")), reply("TASK COMPLETE")]
    )
    loop = AgentLoop(
        session=Session(repo_root=tmp_path),
        model_adapter=model_adapter,
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        session_store=session_store,
        model_request_retry_backoff_ms=0,
    )

    assert loop.run() == RunResult(status="complete")
    assert model_adapter.message_requests[0] == model_adapter.message_requests[1]
    assert len(model_adapter.message_requests[0]) == 1

    entries = [
        json.loads(line)
        for line in session_store.jsonl_path.read_text(encoding="utf-8").splitlines()
    ]
    lifecycle = [entry for entry in entries if entry["type"].startswith("model_request_")]
    assert [entry["type"] for entry in lifecycle] == [
        "model_request_started",
        "model_request_failed",
        "model_request_retry_scheduled",
        "model_request_retry_started",
        "model_request_finished",
    ]
    assert lifecycle[1]["failure_class"] == "timeout"
    assert lifecycle[2]["attempt"] == 2
    assert lifecycle[-1]["retry_count"] == 1


def test_cancellation_interrupts_retry_backoff_promptly() -> None:
    events = []
    model_adapter = SequencedRequestAdapter(
        [_wrapped_model_error(TimeoutError("provider timed out"))]
    )
    loop = AgentLoop(
        session=Session(repo_root=None),
        model_adapter=model_adapter,
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        on_event=events.append,
        should_cancel=lambda: any(
            event["type"] == "model_request_retry_scheduled" for event in events
        ),
        model_request_retry_backoff_ms=5_000,
    )

    started = __import__("time").monotonic()
    result = loop.run()

    assert result == RunResult(status="interrupted", reason="cancelled")
    assert __import__("time").monotonic() - started < 1
    assert model_adapter.index == 1


def test_transient_model_failure_exhausts_retries_with_diagnostics(tmp_path) -> None:
    response = SimpleNamespace(
        status_code=503,
        headers={"x-request-id": "req_123", "retry-after": "1"},
        text='{"error":"temporarily unavailable"}',
    )

    class ProviderHTTPError(Exception):
        def __init__(self) -> None:
            super().__init__("service unavailable")
            self.response = response

    request_failures = [_wrapped_model_error(ProviderHTTPError()) for _ in range(3)]
    session_store = SessionStore.create(tmp_path, "exhausted request")
    model_adapter = SequencedRequestAdapter(request_failures)
    loop = AgentLoop(
        session=Session(repo_root=tmp_path),
        model_adapter=model_adapter,
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        session_store=session_store,
        model_request_retry_backoff_ms=0,
    )

    with pytest.raises(ModelRequestError, match="service unavailable"):
        loop.run()

    entries = [
        json.loads(line)
        for line in session_store.jsonl_path.read_text(encoding="utf-8").splitlines()
    ]
    failures = [entry for entry in entries if entry["type"] == "model_request_failed"]
    assert len(failures) == 3
    assert failures[-1]["failure_class"] == "provider_server_error"
    assert failures[-1]["http_status"] == 503
    assert failures[-1]["provider_request_id"] == "req_123"
    assert failures[-1]["retry_after"] == "1"
    assert failures[-1]["response_body_excerpt"] == response.text
    run_failed = next(entry for entry in entries if entry["type"] == "run_failed")
    assert run_failed["underlying_error_type"] == "ProviderHTTPError"
    assert run_failed["http_status"] == 503


def test_permanent_model_failure_does_not_retry(tmp_path) -> None:
    class AuthenticationError(Exception):
        status_code = 401

    model_adapter = SequencedRequestAdapter(
        [_wrapped_model_error(AuthenticationError("invalid credentials"))]
    )
    session_store = SessionStore.create(tmp_path, "permanent request")
    loop = AgentLoop(
        session=Session(repo_root=tmp_path),
        model_adapter=model_adapter,
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        session_store=session_store,
        model_request_retry_backoff_ms=0,
    )

    with pytest.raises(ModelRequestError):
        loop.run()

    assert len(model_adapter.message_requests) == 1
    entries = (
        json.loads(line)
        for line in session_store.jsonl_path.read_text(encoding="utf-8").splitlines()
    )
    assert not any(
        entry["type"].startswith("model_request_retry_") for entry in entries
    )


def test_model_failure_log_redacts_credentials(tmp_path) -> None:
    secret = "sk-super-secret"

    class RateLimitError(Exception):
        status_code = 429
        body = {"error": "slow down", "api_key": secret}

    model_adapter = SequencedRequestAdapter(
        [_wrapped_model_error(RateLimitError(f"Authorization: Bearer {secret}"))]
    )
    session_store = SessionStore.create(tmp_path, "redacted request")
    loop = AgentLoop(
        session=Session(repo_root=tmp_path),
        model_adapter=model_adapter,
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        session_store=session_store,
        model_request_max_retries=0,
    )

    with pytest.raises(ModelRequestError):
        loop.run()

    log_text = session_store.jsonl_path.read_text(encoding="utf-8")
    assert secret not in log_text
    assert "[REDACTED]" in log_text


def test_keyboard_interrupt_leaves_durable_terminal_event_before_status(tmp_path) -> None:
    class InterruptingModelAdapter(FakeModelAdapter):
        def respond(self, messages: list[dict], tool_definitions: list[dict]) -> dict:
            raise KeyboardInterrupt

    session_store = TrackingSessionStore.create(tmp_path, "interrupted request")
    loop = AgentLoop(
        session=Session(repo_root=tmp_path),
        model_adapter=InterruptingModelAdapter([]),
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        session_store=session_store,
    )

    with pytest.raises(KeyboardInterrupt):
        loop.run()

    entries = [
        json.loads(line)
        for line in session_store.jsonl_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [entry["type"] for entry in entries][-2:] == [
        "model_request_failed",
        "run_interrupted",
    ]
    assert entries[-1]["interruption_type"] == "KeyboardInterrupt"
    assert entries[-1]["stage"] == "model_request"
    assert session_store.operations.index("event:run_interrupted") < (
        session_store.operations.index("status:interrupted")
    )
    assert json.loads(session_store.metadata_path.read_text(encoding="utf-8"))["status"] == (
        "interrupted"
    )
def test_always_still_asks_for_every_tool(tmp_path) -> None:
    target = tmp_path / "README.md"
    target.write_text("hello", encoding="utf-8")
    prompter = FakePrompter(ApprovalDecision(approved=True))

    _, tool_executor, _, _ = run_loop(
        [
            reply("valid", actions=[{"tool": "read_file", "args": {"path": str(target)}}]),
            reply("TASK COMPLETE"),
        ],
        approval_policy=ApprovalPolicy("always", prompter=prompter),
        repo_root=tmp_path,
    )

    assert len(prompter.actions) == 1
    assert tool_executor.actions == [
        {"tool": "read_file", "args": {"path": str(target)}}
    ]


def test_never_still_asks_never_and_allows_outside_workspace(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    prompter = FakePrompter(ApprovalDecision(approved=False))

    _, tool_executor, _, _ = run_loop(
        [
            reply(
                "valid",
                actions=[{"tool": "read_file", "args": {"path": str(outside)}}],
            ),
            reply("TASK COMPLETE"),
        ],
        approval_policy=ApprovalPolicy("never", prompter=prompter),
        repo_root=workspace,
    )

    assert prompter.actions == []
    assert tool_executor.actions == [
        {
            "tool": "read_file",
            "args": {"path": str(outside)},
            "outside_workspace_approved": True,
        }
    ]


def test_empty_approval_decision_executes_tool_action() -> None:
    session, tool_executor, _, _ = run_loop(
        [
            reply(
                "valid",
                actions=[{"tool": "read_file", "tool_call_id": "call_1"}],
            ),
            reply("TASK COMPLETE"),
        ],
        approval_policy=ApprovalPolicy(
            "always",
            prompter=FakePrompter(ApprovalDecision(approved=True, instruction="")),
        ),
    )

    assert tool_executor.actions == [{"tool": "read_file", "tool_call_id": "call_1"}]
    assert all(
        message.get("content") != "user denied tool action"
        for message in session.messages
    )


def test_approval_denial_appends_normal_denied_tool_result_message() -> None:
    session = Session(repo_root=None)
    tool_executor = FakeToolExecutor()
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter(
            [
                reply(
                    "valid",
                    actions=[{"tool": "read_file", "tool_call_id": "call_1"}],
                ),
                reply("TASK COMPLETE"),
            ]
        ),
        tool_executor=tool_executor,
        tool_results=ToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        approval_policy=ApprovalPolicy(
            "always",
            prompter=FakePrompter(ApprovalDecision(approved=False)),
        ),
    )

    loop.run()

    tool_messages = [message for message in session.messages if message["role"] == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_1"
    assert json.loads(tool_messages[0]["content"]) == {
        "ok": False,
        "tool": "read_file",
        "error": "user denied tool action",
    }


def test_approval_comment_is_appended_after_denied_tool_result() -> None:
    session = Session(repo_root=None)
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter(
            [
                {
                    "message": {
                        "role": "assistant",
                        "content": "valid",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path": "/tmp/x"}',
                                },
                            }
                        ],
                    },
                    "actions": [{"tool": "read_file", "tool_call_id": "call_1"}],
                    "reason": "",
                },
                reply("TASK COMPLETE"),
            ]
        ),
        tool_executor=FakeToolExecutor(),
        tool_results=ToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        approval_policy=ApprovalPolicy(
            "always",
            prompter=FakePrompter(
                ApprovalDecision(
                    approved=False,
                    instruction="Please inspect the project tree first.",
                )
            ),
        ),
    )

    loop.run()

    assistant_index = next(
        index
        for index, message in enumerate(session.messages)
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    assert session.messages[assistant_index + 1]["role"] == "tool"
    assert session.messages[assistant_index + 1]["tool_call_id"] == "call_1"
    assert json.loads(session.messages[assistant_index + 1]["content"])["error"] == (
        "user denied tool action"
    )
    assert session.messages[assistant_index + 2] == {
        "role": "user",
        "content": "Please inspect the project tree first.",
    }


def test_multiline_approval_comment_is_appended_after_denied_tool_result() -> None:
    instruction = "Please do this instead:\n1. Read README.md\n2. Keep the patch tiny"
    session = Session(repo_root=None)
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter(
            [
                {
                    "message": {
                        "role": "assistant",
                        "content": "valid",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "run_command",
                                    "arguments": '{"command": "npm test"}',
                                },
                            }
                        ],
                    },
                    "actions": [{"tool": "run_command", "tool_call_id": "call_1"}],
                    "reason": "",
                },
                reply("TASK COMPLETE"),
            ]
        ),
        tool_executor=FakeToolExecutor(),
        tool_results=ToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        approval_policy=ApprovalPolicy(
            "always",
            prompter=FakePrompter(
                ApprovalDecision(approved=False, instruction=instruction)
            ),
        ),
    )

    loop.run()

    assistant_index = next(
        index
        for index, message in enumerate(session.messages)
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    assert session.messages[assistant_index + 1]["role"] == "tool"
    assert session.messages[assistant_index + 2] == {
        "role": "user",
        "content": instruction,
    }


def test_complete_status_is_written_on_normal_completion() -> None:
    _, _, session_store, _, _ = run_loop_with_store([reply("TASK COMPLETE")])

    assert session_store.statuses == ["complete"]


def test_failed_status_is_written_after_repeated_invalid_replies() -> None:
    _, _, session_store, _, _ = run_loop_with_store(
        [
            reply("bad 1", reason="empty_final_reply"),
            reply("bad 2", reason="non_final_too_many_tool_calls"),
            reply("bad 3", reason="empty_final_reply"),
        ]
    )

    assert session_store.statuses == ["failed"]


def test_default_max_tool_steps_does_not_stop_normal_behavior() -> None:
    session, tool_executor, model_adapter, display = run_loop(
        [
            reply("valid", actions=[{"tool": "read_file"}]),
            reply("TASK COMPLETE"),
        ]
    )

    assert session.messages[-1]["content"] == "TASK COMPLETE"
    assert tool_executor.actions == [{"tool": "read_file"}]
    assert model_adapter.index == 2
    assert display.step_limits_reached == []


def test_multiple_successful_tool_calls_execute_and_record_in_order() -> None:
    session = Session(repo_root=None)
    executor = FakeToolExecutor()
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter(
            [
                multi_tool_call_reply(
                    ("call_1", "read_file"),
                    ("call_2", "run_command"),
                    ("call_3", "write_file"),
                ),
                reply("TASK COMPLETE"),
            ]
        ),
        tool_executor=executor,
        tool_results=ToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
    )

    assert loop.run() == RunResult(status="complete")
    assert [action["tool"] for action in executor.actions] == [
        "read_file",
        "run_command",
        "write_file",
    ]
    assistant_index = next(
        index for index, message in enumerate(session.messages) if message.get("tool_calls")
    )
    assistant = session.messages[assistant_index]
    tool_messages = session.messages[assistant_index + 1 : assistant_index + 4]
    assert [call["id"] for call in assistant["tool_calls"]] == [
        "call_1",
        "call_2",
        "call_3",
    ]
    assert [message["tool_call_id"] for message in tool_messages] == [
        "call_1",
        "call_2",
        "call_3",
    ]


def test_canonical_multi_action_arguments_reach_executor_unchanged() -> None:
    actions = (
        (
            "call_1",
            "write_file",
            {"path": "/tmp/index.html", "content": "<main>game</main>"},
        ),
        (
            "call_2",
            "write_file",
            {"path": "/tmp/style.css", "content": "main { color: cyan; }"},
        ),
    )
    executor = FakeToolExecutor()
    loop = AgentLoop(
        session=Session(repo_root=None),
        model_adapter=FakeModelAdapter(
            [multi_tool_call_reply_with_args(*actions), reply("TASK COMPLETE")]
        ),
        tool_executor=executor,
        tool_results=ToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
    )

    assert loop.run() == RunResult(status="complete")
    assert [action["args"] for action in executor.actions] == [
        actions[0][2],
        actions[1][2],
    ]


def test_first_failed_action_stops_batch_and_closes_unattempted_calls() -> None:
    class FailingExecutor(FakeToolExecutor):
        def execute(self, action: dict) -> dict:
            self.actions.append(action)
            return {"ok": False, "tool": action["tool"], "error": "failed"}

    session = Session(repo_root=None)
    executor = FailingExecutor()
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter(
            [
                multi_tool_call_reply(
                    ("call_1", "read_file"),
                    ("call_2", "run_command"),
                ),
                reply("TASK COMPLETE"),
            ]
        ),
        tool_executor=executor,
        tool_results=ToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
    )

    assert loop.run() == RunResult(status="complete")
    assert [action["tool_call_id"] for action in executor.actions] == ["call_1"]
    tool_messages = [message for message in session.messages if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == ["call_1", "call_2"]
    assert json.loads(tool_messages[0]["content"])["ok"] is False
    assert json.loads(tool_messages[1]["content"])["skipped"] is True


def test_denied_action_stops_remaining_batch() -> None:
    class DenyFirstPolicy:
        mode = "never"

        def check(self, action: dict) -> ApprovalDecision:
            return ApprovalDecision(approved=False, reason="denied")

    session = Session(repo_root=None)
    executor = FakeToolExecutor()
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter(
            [
                multi_tool_call_reply(
                    ("call_1", "read_file"),
                    ("call_2", "run_command"),
                ),
                reply("TASK COMPLETE"),
            ]
        ),
        tool_executor=executor,
        tool_results=ToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        approval_policy=DenyFirstPolicy(),
    )

    assert loop.run() == RunResult(status="complete")
    assert executor.actions == []
    tool_messages = [message for message in session.messages if message["role"] == "tool"]
    assert json.loads(tool_messages[0]["content"])["ok"] is False
    assert json.loads(tool_messages[1]["content"])["skipped"] is True


def test_hook_blocked_action_stops_remaining_batch() -> None:
    class BlockingHook:
        def before_tool_execute(self, context) -> HookResponse:
            return HookResponse(block=True, reason="blocked")

    session = Session(repo_root=None)
    executor = FakeToolExecutor()
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter(
            [
                multi_tool_call_reply(
                    ("call_1", "read_file"),
                    ("call_2", "run_command"),
                ),
                reply("TASK COMPLETE"),
            ]
        ),
        tool_executor=executor,
        tool_results=ToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        hook_runner=HookRunner([BlockingHook()]),
    )

    assert loop.run() == RunResult(status="complete")
    assert executor.actions == []
    tool_messages = [message for message in session.messages if message["role"] == "tool"]
    assert json.loads(tool_messages[0]["content"])["error"] == "blocked_by_hook"
    assert json.loads(tool_messages[1]["content"])["skipped"] is True


def test_tool_step_limit_stops_partway_through_batch() -> None:
    session = Session(repo_root=None)
    executor = FakeToolExecutor()
    adapter = FakeModelAdapter(
        [
            multi_tool_call_reply(
                ("call_1", "read_file"),
                ("call_2", "run_command"),
                ("call_3", "write_file"),
            ),
            reply("TASK COMPLETE"),
        ]
    )
    display = NullDisplay()
    loop = AgentLoop(
        session=session,
        model_adapter=adapter,
        tool_executor=executor,
        tool_results=ToolResults(),
        agent=FakeAgent(),
        display=display,
        max_tool_steps=2,
    )

    assert loop.run() == RunResult(
        status="failed", reason="reached tool step limit: 2"
    )
    assert [action["tool_call_id"] for action in executor.actions] == [
        "call_1",
        "call_2",
    ]
    assert adapter.index == 1
    assert display.step_limits_reached == [2]
    tool_messages = [message for message in session.messages if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == [
        "call_1",
        "call_2",
        "call_3",
    ]
    assert json.loads(tool_messages[-1]["content"])["error"] == "tool_step_limit"


def test_multi_action_hooks_keep_intermediate_guidance_for_next_model_turn() -> None:
    before_counts = []
    finished_counts = []

    class RecordingHook:
        def before_tool_execute(self, context) -> None:
            before_counts.append(context.tool_steps_used)

        def tool_result(self, context) -> HookResponse | None:
            if context.action["tool"] == "read_file":
                return HookResponse(additional_context="read guidance")
            return None

        def run_finished(self, context) -> None:
            finished_counts.append(context.tool_steps_used)

    adapter = FakeModelAdapter(
        [
            multi_tool_call_reply(
                ("call_1", "read_file"),
                ("call_2", "run_command"),
            ),
            reply("TASK COMPLETE"),
        ]
    )
    loop = AgentLoop(
        session=Session(repo_root=None),
        model_adapter=adapter,
        tool_executor=FakeToolExecutor(),
        tool_results=ToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        hook_runner=HookRunner([RecordingHook()]),
    )

    assert loop.run() == RunResult(status="complete")
    assert before_counts == [0, 1]
    assert finished_counts == [2]
    assert adapter.message_requests[1][-1] == {
        "role": "user",
        "content": "read guidance",
    }


def test_multi_action_hooks_keep_distinct_guidance_in_execution_order() -> None:
    class GuidanceHook:
        def tool_result(self, context) -> HookResponse:
            return HookResponse(additional_context=f"guidance:{context.action['tool']}")

    adapter = FakeModelAdapter(
        [
            multi_tool_call_reply(
                ("call_1", "read_file"),
                ("call_2", "run_command"),
            ),
            reply("TASK COMPLETE"),
        ]
    )
    loop = AgentLoop(
        session=Session(repo_root=None),
        model_adapter=adapter,
        tool_executor=FakeToolExecutor(),
        tool_results=ToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        hook_runner=HookRunner([GuidanceHook()]),
    )

    assert loop.run() == RunResult(status="complete")
    assert adapter.message_requests[1][-1] == {
        "role": "user",
        "content": "guidance:read_file\n\nguidance:run_command",
    }


def test_multi_action_hooks_deduplicate_exact_repeated_guidance() -> None:
    class RepeatingHook:
        def tool_result(self, _context) -> HookResponse:
            return HookResponse(additional_context="same guidance")

    adapter = FakeModelAdapter(
        [
            multi_tool_call_reply(
                ("call_1", "read_file"),
                ("call_2", "run_command"),
                ("call_3", "write_file"),
            ),
            reply("TASK COMPLETE"),
        ]
    )
    loop = AgentLoop(
        session=Session(repo_root=None),
        model_adapter=adapter,
        tool_executor=FakeToolExecutor(),
        tool_results=ToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        hook_runner=HookRunner([RepeatingHook()]),
    )

    assert loop.run() == RunResult(status="complete")
    assert adapter.message_requests[1][-1] == {
        "role": "user",
        "content": "same guidance",
    }


def test_single_action_transient_guidance_behavior_is_unchanged() -> None:
    class GuidanceHook:
        def tool_result(self, _context) -> HookResponse:
            return HookResponse(additional_context="one-turn guidance")

    adapter = FakeModelAdapter(
        [
            tool_call_reply("call_1"),
            reply("TASK COMPLETE"),
        ]
    )
    session = Session(repo_root=None)
    loop = AgentLoop(
        session=session,
        model_adapter=adapter,
        tool_executor=FakeToolExecutor(),
        tool_results=ToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        hook_runner=HookRunner([GuidanceHook()]),
    )

    assert loop.run() == RunResult(status="complete")
    assert adapter.message_requests[1][-1] == {
        "role": "user",
        "content": "one-turn guidance",
    }
    assert not any(
        message.get("content") == "one-turn guidance"
        for message in session.messages
    )


def _run_read_batch(tmp_path, actions, hooks):
    """Exercise sibling reads through the production executor and Hook pipeline."""
    session = Session(repo_root=tmp_path)
    adapter = FakeModelAdapter(
        [multi_tool_call_reply_with_args(*actions), reply("TASK COMPLETE")]
    )
    loop = AgentLoop(
        session=session,
        model_adapter=adapter,
        tool_executor=ToolExecutor(tmp_path, session),
        tool_results=ToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        hook_runner=HookRunner(hooks),
    )

    assert loop.run() == RunResult(status="complete")
    results = [
        json.loads(message["content"])
        for message in session.messages
        if message["role"] == "tool"
    ]
    return adapter, results


def test_visible_source_character_totals_are_cumulative_within_batch(tmp_path) -> None:
    class VisibleCharacterCounter:
        def __init__(self) -> None:
            self.total = 0
            self.observed_totals = []

        def tool_result(self, context) -> None:
            content = context.result.get("content")
            if context.action["tool"] == "read_file" and isinstance(content, str):
                self.total += len(content)
                self.observed_totals.append(self.total)

    character_counts = (8_000, 12_000, 5_000)
    paths = []
    for index, character_count in enumerate(character_counts):
        path = tmp_path / f"characters_{index}.txt"
        path.write_text("x" * character_count, encoding="utf-8")
        paths.append(path)
    actions = tuple(
        (f"call_{index}", "read_file", {"path": str(path)})
        for index, path in enumerate(paths, start=1)
    )
    counter = VisibleCharacterCounter()

    _adapter, results = _run_read_batch(tmp_path, actions, [counter])

    assert counter.observed_totals == [8_000, 20_000, 25_000]
    assert sum(result["content_returned_chars"] for result in results) == 25_000


def test_cancellation_during_partial_batch_closes_every_tool_call() -> None:
    cancelled = False

    class CancellingExecutor(FakeToolExecutor):
        def execute(self, action: dict) -> dict:
            nonlocal cancelled
            result = super().execute(action)
            cancelled = True
            return result

    session = Session(repo_root=None)
    executor = CancellingExecutor()
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter(
            [
                multi_tool_call_reply(
                    ("call_1", "read_file"),
                    ("call_2", "run_command"),
                    ("call_3", "write_file"),
                )
            ]
        ),
        tool_executor=executor,
        tool_results=ToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        should_cancel=lambda: cancelled,
    )

    assert loop.run() == RunResult(status="interrupted", reason="cancelled")
    assert [action["tool_call_id"] for action in executor.actions] == ["call_1"]
    tool_messages = [message for message in session.messages if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == [
        "call_1",
        "call_2",
        "call_3",
    ]
    assert json.loads(tool_messages[0]["content"])["ok"] is True
    assert json.loads(tool_messages[1]["content"])["interrupted"] is True
    assert json.loads(tool_messages[2]["content"])["skipped"] is True


def test_exception_during_partial_batch_closes_every_tool_call_and_reraises() -> None:
    class ExplodingExecutor(FakeToolExecutor):
        def execute(self, action: dict) -> dict:
            self.actions.append(action)
            if action["tool_call_id"] == "call_2":
                raise RuntimeError("tool exploded")
            return {"ok": True, "tool": action["tool"]}

    session = Session(repo_root=None)
    executor = ExplodingExecutor()
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter(
            [
                multi_tool_call_reply(
                    ("call_1", "read_file"),
                    ("call_2", "run_command"),
                    ("call_3", "write_file"),
                )
            ]
        ),
        tool_executor=executor,
        tool_results=ToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
    )

    with pytest.raises(RuntimeError, match="tool exploded"):
        loop.run()

    assert [action["tool_call_id"] for action in executor.actions] == [
        "call_1",
        "call_2",
    ]
    tool_messages = [message for message in session.messages if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == [
        "call_1",
        "call_2",
        "call_3",
    ]
    assert json.loads(tool_messages[0]["content"])["ok"] is True
    assert json.loads(tool_messages[1]["content"])["failed"] is True
    assert json.loads(tool_messages[2]["content"])["skipped"] is True


def test_max_tool_steps_one_stops_before_next_model_reply() -> None:
    session, tool_executor, session_store, model_adapter, display = run_loop_with_store(
        [
            reply("valid", actions=[{"tool": "read_file"}]),
            reply("TASK COMPLETE"),
        ],
        max_tool_steps=1,
    )

    assert tool_executor.actions == [{"tool": "read_file"}]
    assert {"role": "tool", "content": "read_file"} in session.messages
    assert model_adapter.index == 1
    assert session.messages[-1] == {"role": "tool", "content": "read_file"}
    assert session_store.statuses == ["failed"]
    assert display.step_limits_reached == [1]


def test_invalid_rejected_replies_do_not_count_toward_tool_step_limit() -> None:
    session, tool_executor, _, model_adapter, display = run_loop_with_store(
        [
            reply("bad 1", reason="empty_final_reply"),
            reply("valid", actions=[{"tool": "read_file"}]),
            reply("TASK COMPLETE"),
        ],
        max_tool_steps=1,
    )

    assert {"role": "user", "content": "correction:empty_final_reply"} not in session.messages
    assert tool_executor.actions == [{"tool": "read_file"}]
    assert model_adapter.index == 2
    assert display.step_limits_reached == [1]


def test_final_reply_without_tool_actions_does_not_count_toward_tool_step_limit() -> None:
    session, tool_executor, session_store, model_adapter, display = run_loop_with_store(
        [reply("TASK COMPLETE")],
        max_tool_steps=1,
    )

    assert session.messages[-1]["content"] == "TASK COMPLETE"
    assert tool_executor.actions == []
    assert model_adapter.index == 1
    assert session_store.statuses == ["complete"]
    assert display.step_limits_reached == []


def test_invalid_reply_does_not_emit_tool_started() -> None:
    events = []
    run_loop(
        [
            reply("bad", reason="empty_final_reply"),
            reply("TASK COMPLETE"),
        ],
        on_event=events.append,
    )

    event_types = [event["type"] for event in events]
    assert "tool_started" not in event_types
    assert "reply_rejected" in event_types


def test_final_reply_without_actions_does_not_emit_tool_started() -> None:
    events = []
    run_loop(
        [reply("TASK COMPLETE")],
        on_event=events.append,
    )

    event_types = [event["type"] for event in events]
    assert "tool_started" not in event_types


def test_tool_started_and_finished_include_tool_call_id() -> None:
    events = []
    run_loop(
        [
            reply(
                "valid",
                actions=[{"tool": "read_file", "tool_call_id": "call_123"}],
            ),
            reply("TASK COMPLETE"),
        ],
        on_event=events.append,
    )

    tool_started = next(event for event in events if event["type"] == "tool_started")
    tool_finished = next(event for event in events if event["type"] == "tool_finished")
    assert tool_started["tool_call_id"] == "call_123"
    assert tool_finished["tool_call_id"] == "call_123"


def test_event_order_with_persistence() -> None:
    all_events: list[str] = []
    session = Session(repo_root=None)
    session_store = FakeSessionStore(events=all_events)
    loop = AgentLoop(
        session=session,
        model_adapter=FakeModelAdapter(
            [
                reply(
                    "Using read_file.",
                    actions=[{"tool": "read_file", "tool_call_id": "call_1"}],
                ),
                reply("TASK COMPLETE"),
            ]
        ),
        tool_executor=FakeToolExecutor(),
        tool_results=FakeToolResults(),
        agent=FakeAgent(),
        display=NullDisplay(),
        session_store=session_store,
        on_event=lambda e: all_events.append(e["type"]),
    )

    loop.run()

    # With a session_store, _ensure_initial_messages appends the system message first.
    # Expected order around the tool-call turn:
    #   ... append_message (system), run_started,
    #       assistant_message, append_message (assistant), tool_started,
    #       tool_finished, append_message (tool result),
    #       final_message, append_message (TASK COMPLETE),
    #       run_finished
    event_types = all_events
    # The first turn with actions:
    asst_idx = event_types.index("assistant_message")
    ts_idx = event_types.index("tool_started")
    # The first append_message after assistant_message is the assistant message persist.
    am_idx = next(
        i for i in range(asst_idx + 1, len(event_types)) if event_types[i] == "append_message"
    )
    tf_idx = event_types.index("tool_finished")
    assert asst_idx < ts_idx, "assistant_message must come before tool_started"
    assert am_idx < ts_idx, "assistant message persistence must come before tool_started"
    assert ts_idx < tf_idx, "tool_started must come before tool_finished"
