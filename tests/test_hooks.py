"""Exercise Hook aggregation, lifecycle effects, and AgentLoop integration."""

import pytest

from squid_core.agent_loop import AgentLoop, RunResult
from squid_core.hooks import (
    BeforeAcceptFinalContext,
    BeforeModelTurnContext,
    BeforeRunContext,
    BeforeToolExecuteContext,
    CompactionAppliedContext,
    FollowUpRunRequest,
    HookRunner,
    HookResponse,
    PromptContextBlock,
    RepeatedFailureNudge,
    RunFinishedContext,
    ToolResultContext,
)
from squid_core.session import Session


class FakeAgent:
    task = "Do the task"
    skill_name = "skill"

    def initial_messages(self) -> list[dict]:
        return [{"role": "system", "content": "system"}]

    def tool_definitions(self) -> list[dict]:
        return [
            {"function": {"name": "read_file"}},
            {"name": "run_command"},
        ]

    def should_stop(self, session: Session) -> bool:
        return any(message["role"] == "assistant" for message in session.messages)

    def validate_reply(self, reply: dict) -> dict:
        return {"ok": True, "reason": ""}


class FakeModelAdapter:
    def __init__(self, reply: dict | None = None) -> None:
        self.calls = []
        self.replies = [reply or {
            "message": {"role": "assistant", "content": "TASK COMPLETE"},
            "actions": [],
        }]

    def respond(self, messages: list[dict], tool_definitions: list[dict]) -> dict:
        self.calls.append(
            {
                "messages": list(messages),
                "tool_definitions": tool_definitions,
            }
        )
        if len(self.calls) <= len(self.replies):
            reply = self.replies[len(self.calls) - 1]
        else:
            reply = self.replies[-1]
        reply.setdefault(
            "raw_reply",
            {
                "assistant": {
                    "content": reply["message"].get("content"),
                    "raw_tool_calls": reply["message"].get("tool_calls", []),
                }
            },
        )
        return reply


class SequenceModelAdapter(FakeModelAdapter):
    def __init__(self, replies: list[dict]) -> None:
        super().__init__(replies[0])
        self.replies = replies


class FakeToolExecutor:
    def __init__(self) -> None:
        self.actions = []

    def execute(self, action: dict) -> dict:
        self.actions.append(action)
        return {"ok": True, "tool": action["tool"]}


class FakeToolResults:
    def messages_from_action_results(self, actions: list[dict], results: list[dict]) -> list[dict]:
        return []


class FakeSessionStore:
    session_id = "session-1"

    def __init__(self) -> None:
        self.messages = []
        self.statuses = []

    def append_message(self, message: dict, usage: dict | None = None) -> None:
        self.messages.append(message)

    def append_model_reply(self, raw_reply: dict) -> dict:
        return {"id": "raw_reply"}

    def append_reply_rejection(self, reply: dict, **kwargs) -> dict:
        return kwargs

    def update_status(self, status: str) -> None:
        self.statuses.append(status)


class NullDisplay:
    debug = False

    def __init__(self) -> None:
        self.final_messages = []
        self.rejected_replies = []
        self.tool_results = []

    def thinking(self) -> None:
        pass

    def debug_reply(self, reply: dict) -> None:
        pass

    def final_message(self, content: str) -> None:
        self.final_messages.append(content)

    def assistant_message(self, content: str) -> None:
        pass

    def reply_rejected(self, reason: str) -> None:
        self.rejected_replies.append(reason)

    def action_started(self, action: dict) -> None:
        pass

    def tool_result(self, action: dict, result: dict) -> None:
        self.tool_results.append((action, result))


def build_loop(
    hook_runner=None,
    on_event=None,
    model_reply=None,
    agent=None,
    should_cancel=None,
):
    session = Session(repo_root=None)
    model_adapter = FakeModelAdapter(model_reply)
    session_store = FakeSessionStore()
    tool_executor = FakeToolExecutor()
    loop = AgentLoop(
        session=session,
        model_adapter=model_adapter,
        tool_executor=tool_executor,
        tool_results=FakeToolResults(),
        agent=agent or FakeAgent(),
        display=NullDisplay(),
        session_store=session_store,
        on_event=on_event,
        should_cancel=should_cancel,
        hook_runner=hook_runner,
    )
    return loop, session, model_adapter, session_store, tool_executor


def build_loop_with_model(model_adapter, hook_runner=None, on_event=None, agent=None):
    session = Session(repo_root=None)
    session_store = FakeSessionStore()
    tool_executor = FakeToolExecutor()
    loop = AgentLoop(
        session=session,
        model_adapter=model_adapter,
        tool_executor=tool_executor,
        tool_results=FakeToolResults(),
        agent=agent or FakeAgent(),
        display=NullDisplay(),
        session_store=session_store,
        on_event=on_event,
        hook_runner=hook_runner,
    )
    return loop, session, session_store, tool_executor


def test_hook_runner_noop_returns_empty_decisions() -> None:
    runner = HookRunner()

    before_run = runner.before_run(
        BeforeRunContext(session_id=None, repo_root="", task="")
    )
    before_model_turn = runner.before_model_turn(
        BeforeModelTurnContext(
            session_id=None,
            repo_root="",
            task="",
            messages=(),
            tool_steps_used=0,
        )
    )
    before_tool_execute = runner.before_tool_execute(
        BeforeToolExecuteContext(
            session_id=None,
            repo_root="",
            action={"tool": "read_file"},
            tool_steps_used=0,
        )
    )
    before_accept_final = runner.before_accept_final(
        BeforeAcceptFinalContext(
            session_id=None,
            repo_root="",
            message={"role": "assistant", "content": "done"},
            content="done",
        )
    )
    run_finished = runner.run_finished(
        RunFinishedContext(session_id=None, repo_root="", status="complete")
    )
    compaction_applied = runner.compaction_applied(
        CompactionAppliedContext(session_id=None, repo_root="")
    )

    assert before_run == HookResponse()
    assert before_model_turn == HookResponse()
    assert compaction_applied is None
    assert before_tool_execute == HookResponse(
        action={"tool": "read_file"},
        approval_reason="",
    )
    assert before_accept_final == HookResponse()
    assert run_finished == HookResponse()


def test_hook_runner_reports_compact_outcomes_at_every_lifecycle_seam() -> None:
    events = []
    context_block = PromptContextBlock("title", "sensitive body")
    follow_up = FollowUpRunRequest(task="sensitive follow-up")

    class EverySeamHook:
        def before_run(self, ctx):
            return HookResponse(context_blocks=(context_block,))

        def before_model_turn(self, ctx):
            return None

        def compaction_applied(self, ctx):
            return None

        def before_tool_execute(self, ctx):
            return HookResponse(action={"tool": "changed"}, approval_reason="why")

        def tool_result(self, ctx):
            return HookResponse(
                updated_tool_output={"ok": True, "secret": "value"},
                additional_context="private guidance",
            )

        def before_accept_final(self, ctx):
            return HookResponse(report_note="private note")

        def run_finished(self, ctx):
            return HookResponse(follow_up_requests=(follow_up,), notes=("note",))

    runner = HookRunner([EverySeamHook()])
    runner.before_run(
        BeforeRunContext(None, "repo", "task"), event_sink=events.append
    )
    runner.before_model_turn(
        BeforeModelTurnContext(None, "repo", "task", (), 0),
        event_sink=events.append,
    )
    runner.compaction_applied(
        CompactionAppliedContext(None, "repo"), event_sink=events.append
    )
    runner.before_tool_execute(
        BeforeToolExecuteContext(None, "repo", {"tool": "original"}, 0),
        event_sink=events.append,
    )
    runner.tool_result(
        ToolResultContext(None, "repo", {"tool": "original"}, {"ok": True}),
        event_sink=events.append,
    )
    runner.before_accept_final(
        BeforeAcceptFinalContext(None, "repo", {}, "done"),
        event_sink=events.append,
    )
    runner.run_finished(
        RunFinishedContext(None, "repo", "complete"), event_sink=events.append
    )

    assert [event["seam"] for event in events] == [
        "before_run",
        "before_model_turn",
        "compaction_applied",
        "before_tool_execute",
        "tool_result",
        "before_accept_final",
        "run_finished",
    ]
    assert events[1]["status"] == "no_effect"
    assert events[3]["effects"] == ["updated_action", "approval_reasons"]
    assert events[4]["effects"] == [
        "updated_tool_output",
        "additional_context",
    ]
    assert events[6]["effects"] == ["follow_up_requests", "notes"]
    assert "sensitive body" not in str(events)
    assert "private guidance" not in str(events)
    assert "secret" not in str(events)


def test_hook_error_is_reported_without_swallowing_it() -> None:
    events = []

    class FailingHook:
        def before_run(self, ctx):
            raise RuntimeError("do not copy this detail")

    with pytest.raises(RuntimeError, match="do not copy this detail"):
        HookRunner([FailingHook()]).before_run(
            BeforeRunContext(None, "repo", "task"), event_sink=events.append
        )

    assert events == [
        {
            "type": "hook_execution",
            "hook": "FailingHook",
            "seam": "before_run",
            "status": "error",
            "produced_effect": False,
            "effects": [],
            "error_type": "RuntimeError",
        }
    ]


def test_before_run_aggregates_context_and_carries_it_into_a_block() -> None:
    first = PromptContextBlock("First", "One")
    second = PromptContextBlock("Second", "Two")
    calls: list[str] = []

    class FirstHook:
        def before_run(self, ctx: BeforeRunContext) -> HookResponse:
            calls.append("first")
            return HookResponse(context_blocks=(first,))

    class BlockingHook:
        def before_run(self, ctx: BeforeRunContext) -> HookResponse:
            calls.append("block")
            return HookResponse(
                block=True,
                reason="stop",
                guidance="change course",
                context_blocks=(second,),
            )

    class LaterHook:
        def before_run(self, ctx: BeforeRunContext) -> HookResponse:
            calls.append("later")
            return HookResponse()

    response = HookRunner([FirstHook(), BlockingHook(), LaterHook()]).before_run(
        BeforeRunContext(None, "", "")
    )

    assert calls == ["first", "block"]
    assert response == HookResponse(
        block=True,
        reason="stop",
        guidance="change course",
        context_blocks=(first, second),
    )


def test_run_finished_aggregates_follow_ups_and_notes_in_hook_order() -> None:
    first_request = FollowUpRunRequest(task="Run architecture review.")
    second_request = FollowUpRunRequest(
        task="Update docs/dev-log.md.",
        reason="Keep handoff notes current.",
    )

    class FirstHook:
        def run_finished(self, ctx: RunFinishedContext) -> HookResponse:
            return HookResponse(
                follow_up_requests=(first_request,),
                notes=("first note",),
            )

    class NoopHook:
        def run_finished(self, ctx: RunFinishedContext) -> None:
            return None

    class SecondHook:
        def run_finished(self, ctx: RunFinishedContext) -> HookResponse:
            return HookResponse(
                follow_up_requests=(second_request,),
                notes=("second note",),
            )

    decision = HookRunner([FirstHook(), NoopHook(), SecondHook()]).run_finished(
        RunFinishedContext(session_id=None, repo_root="", status="complete")
    )

    assert decision.follow_up_requests == (first_request, second_request)
    assert decision.notes == ("first note", "second note")


def test_before_accept_final_block_stops_later_hooks() -> None:
    calls = []

    class NoteHook:
        def before_accept_final(self, ctx: BeforeAcceptFinalContext) -> HookResponse:
            calls.append("note")
            return HookResponse(report_note="first note")

    class BlockingHook:
        def before_accept_final(self, ctx: BeforeAcceptFinalContext) -> HookResponse:
            calls.append("block")
            return HookResponse(
                block=True,
                reason="blocked",
                guidance="continue",
                report_note="second note",
            )

    class LaterHook:
        def before_accept_final(self, ctx: BeforeAcceptFinalContext) -> HookResponse:
            calls.append("later")
            return HookResponse()

    decision = HookRunner([NoteHook(), BlockingHook(), LaterHook()]).before_accept_final(
        BeforeAcceptFinalContext(
            session_id=None,
            repo_root="",
            message={"role": "assistant", "content": "done"},
            content="done",
        )
    )

    assert calls == ["note", "block"]
    assert decision == HookResponse(
        block=True,
        reason="blocked",
        guidance="continue",
        report_note="first note\n\nsecond note",
    )


def test_before_run_block_fails_without_calling_model() -> None:
    events = []

    class BlockingHook:
        def before_run(self, ctx: BeforeRunContext) -> HookResponse:
            return HookResponse(block=True, reason="blocked")

    loop, _, model_adapter, session_store, _ = build_loop(
        hook_runner=HookRunner([BlockingHook()]),
        on_event=events.append,
    )

    result = loop.run()

    assert result == RunResult(status="failed", reason="blocked")
    assert model_adapter.calls == []
    assert session_store.statuses == ["failed"]
    assert [event["type"] for event in events] == [
        "run_started",
        "hooks_configured",
        "hook_execution",
        "run_failed",
        "run_finished",
    ]
    assert events[3]["reason"] == "blocked"


def test_run_finished_complete_receives_context_after_final_is_stored() -> None:
    request = FollowUpRunRequest(task="Run user-review skill next.")

    class FinishedHook:
        def __init__(self) -> None:
            self.contexts = []
            self.session_store = None

        def run_finished(self, ctx: RunFinishedContext) -> HookResponse:
            self.contexts.append(ctx)
            assert self.session_store.statuses == []
            assert self.session_store.messages[-1] == {
                "role": "assistant",
                "content": "TASK COMPLETE",
            }
            return HookResponse(follow_up_requests=(request,))

    hook = FinishedHook()
    loop, _, _, session_store, _ = build_loop(hook_runner=HookRunner([hook]))
    hook.session_store = session_store

    result = loop.run()

    assert result == RunResult(
        status="complete",
        follow_up_requests=(request,),
    )
    assert hook.contexts[0].status == "complete"
    assert hook.contexts[0].final_message == "TASK COMPLETE"
    assert session_store.statuses == ["complete"]


def test_run_finished_failure_marks_otherwise_successful_run_failed() -> None:
    events = []

    class FailingFinishedHook:
        def __init__(self) -> None:
            self.calls = 0

        def run_finished(self, ctx: RunFinishedContext) -> HookResponse:
            self.calls += 1
            assert ctx.status == "complete"
            raise RuntimeError("notification unavailable")

    hook = FailingFinishedHook()
    loop, _, _, session_store, _ = build_loop(
        hook_runner=HookRunner([hook]),
        on_event=events.append,
    )

    result = loop.run()

    assert result == RunResult(
        status="failed",
        reason="run_finished hook failed: notification unavailable",
    )
    assert hook.calls == 1
    assert session_store.statuses == ["failed"]
    hook_event = next(
        event
        for event in events
        if event["type"] == "hook_execution"
    )
    assert {key: value for key, value in hook_event.items() if key != "timestamp"} == {
        "session_id": "session-1",
        "type": "hook_execution",
        "hook": "FailingFinishedHook",
        "seam": "run_finished",
        "status": "error",
        "produced_effect": False,
        "effects": [],
        "error_type": "RuntimeError",
    }
    assert [event["type"] for event in events[-2:]] == [
        "run_failed",
        "run_finished",
    ]
    assert events[-2]["reason"] == result.reason
    assert events[-1]["status"] == "failed"
    assert not any(
        event["type"] == "run_finished" and event["status"] == "complete"
        for event in events
    )


def test_run_finished_notes_are_carried_on_run_result() -> None:
    class FinishedHook:
        def run_finished(self, ctx: RunFinishedContext) -> HookResponse:
            return HookResponse(notes=("first note", "", "  ", "second note"))

    loop, _, _, _, _ = build_loop(hook_runner=HookRunner([FinishedHook()]))

    result = loop.run()

    assert result == RunResult(
        status="complete",
        hook_notes=("first note", "second note"),
    )


def test_run_finished_before_run_failure_receives_reason_and_returns_follow_up() -> None:
    request = FollowUpRunRequest(task="Ask user whether to continue.")

    class BlockingHook:
        def before_run(self, ctx: BeforeRunContext) -> HookResponse:
            return HookResponse(block=True, reason="blocked")

        def run_finished(self, ctx: RunFinishedContext) -> HookResponse:
            assert ctx.status == "failed"
            assert ctx.reason == "blocked"
            return HookResponse(follow_up_requests=(request,))

    loop, _, _, _, _ = build_loop(hook_runner=HookRunner([BlockingHook()]))

    result = loop.run()

    assert result == RunResult(
        status="failed",
        reason="blocked",
        follow_up_requests=(request,),
    )


def test_run_finished_interrupted_returns_follow_up() -> None:
    request = FollowUpRunRequest(task="Retry this task with a smaller scope.")

    class FinishedHook:
        def __init__(self) -> None:
            self.contexts = []

        def run_finished(self, ctx: RunFinishedContext) -> HookResponse:
            self.contexts.append(ctx)
            return HookResponse(follow_up_requests=(request,))

    hook = FinishedHook()
    loop, _, model_adapter, session_store, _ = build_loop(
        hook_runner=HookRunner([hook]),
        should_cancel=lambda: True,
    )

    result = loop.run()

    assert result == RunResult(
        status="interrupted",
        reason="cancelled",
        follow_up_requests=(request,),
    )
    assert model_adapter.calls == []
    assert session_store.statuses == ["interrupted"]
    assert hook.contexts[0].status == "interrupted"
    assert hook.contexts[0].reason == "cancelled"


def test_agent_loop_final_hook_blocks_and_retries_without_storing_final() -> None:
    events = []

    class BlockingOnceHook:
        def __init__(self) -> None:
            self.contents = []

        def before_accept_final(self, ctx: BeforeAcceptFinalContext) -> HookResponse:
            self.contents.append(ctx.content)
            if ctx.content == "not enough yet":
                return HookResponse(
                    block=True,
                    reason="missing verification",
                    guidance="Run the verification before finalizing.",
                )
            return HookResponse()

    hook = BlockingOnceHook()
    model_adapter = SequenceModelAdapter(
        [
            {
                "message": {"role": "assistant", "content": "not enough yet"},
                "actions": [],
            },
            {
                "message": {"role": "assistant", "content": "TASK COMPLETE"},
                "actions": [],
            },
        ]
    )
    loop, session, session_store, _ = build_loop_with_model(
        model_adapter,
        hook_runner=HookRunner([hook]),
        on_event=events.append,
    )

    result = loop.run()

    assert result == RunResult(status="complete")
    assert hook.contents == ["not enough yet", "TASK COMPLETE"]
    assert loop.display.final_messages == ["TASK COMPLETE"]
    assert session.messages == [
        {"role": "system", "content": "system"},
        {"role": "assistant", "content": "TASK COMPLETE"},
    ]
    assert session_store.messages == session.messages
    assert model_adapter.calls[1]["messages"][-1] == {
        "role": "user",
        "content": "Run the verification before finalizing.",
    }
    assert [event["type"] for event in events] == [
        "run_started",
        "hooks_configured",
        "model_request_started",
        "model_request_finished",
        "hook_execution",
        "reply_rejected",
        "model_request_started",
        "model_request_finished",
        "hook_execution",
        "final_message",
        "run_finished",
    ]
    rejected = next(event for event in events if event["type"] == "reply_rejected")
    assert rejected["reason"] == "final_blocked_by_hook"
    assert rejected["detail"] == "Run the verification before finalizing."


def test_agent_loop_final_hook_allows_existing_final_behavior() -> None:
    seen = []

    class AllowingHook:
        def before_accept_final(self, ctx: BeforeAcceptFinalContext) -> HookResponse:
            seen.append(ctx)
            return HookResponse()

    loop, session, _, _, _ = build_loop(hook_runner=HookRunner([AllowingHook()]))
    session.mark_file_read("b.txt")
    session.mark_file_read("a.txt")
    session.mark_file_written("out.txt")

    result = loop.run()

    assert result == RunResult(status="complete")
    assert loop.display.final_messages == ["TASK COMPLETE"]
    assert session.messages == [
        {"role": "system", "content": "system"},
        {"role": "assistant", "content": "TASK COMPLETE"},
    ]
    assert seen[0].content == "TASK COMPLETE"
    assert seen[0].files_read == ("a.txt", "b.txt")
    assert seen[0].files_written == ("out.txt",)


def test_before_accept_final_does_not_run_for_invalid_no_tool_reply() -> None:
    class InvalidThenValidAgent(FakeAgent):
        def __init__(self) -> None:
            self.validation_calls = 0

        def validate_reply(self, reply: dict) -> dict:
            self.validation_calls += 1
            if self.validation_calls == 1:
                return {"ok": False, "reason": "invalid narration"}
            return {"ok": True, "reason": ""}

        def correction_message_for(self, reason: str) -> dict:
            return {"role": "user", "content": f"Correction: {reason}"}

    class RecordingHook:
        def __init__(self) -> None:
            self.contents = []

        def before_accept_final(self, ctx: BeforeAcceptFinalContext) -> HookResponse:
            self.contents.append(ctx.content)
            return HookResponse()

    hook = RecordingHook()
    model_adapter = SequenceModelAdapter(
        [
            {
                "message": {"role": "assistant", "content": "just chatting"},
                "actions": [],
            },
            {
                "message": {"role": "assistant", "content": "TASK COMPLETE"},
                "actions": [],
            },
        ]
    )
    loop, session, _, _ = build_loop_with_model(
        model_adapter,
        hook_runner=HookRunner([hook]),
        agent=InvalidThenValidAgent(),
    )

    result = loop.run()

    assert result == RunResult(status="complete")
    assert hook.contents == ["TASK COMPLETE"]
    assert loop.display.rejected_replies == ["invalid narration"]
    assert session.messages == [
        {"role": "system", "content": "system"},
        {"role": "assistant", "content": "TASK COMPLETE"},
    ]
    assert model_adapter.calls[1]["messages"][-1] == {
        "role": "user",
        "content": "Correction: invalid narration",
    }


def test_before_accept_final_report_note_is_appended_once() -> None:
    class NoteHook:
        def before_accept_final(self, ctx: BeforeAcceptFinalContext) -> HookResponse:
            return HookResponse(report_note="Report note.")

    loop, session, _, _, _ = build_loop(hook_runner=HookRunner([NoteHook()]))

    result = loop.run()

    assert result == RunResult(status="complete")
    assert loop.display.final_messages == ["TASK COMPLETE\n\nReport note."]
    assert session.messages == [
        {"role": "system", "content": "system"},
        {"role": "assistant", "content": "TASK COMPLETE\n\nReport note."},
    ]


def test_before_run_block_uses_guidance_when_reason_is_empty() -> None:
    events = []

    class BlockingHook:
        def before_run(self, ctx: BeforeRunContext) -> HookResponse:
            return HookResponse(block=True, guidance="Fix setup first.")

    loop, _, _, _, _ = build_loop(
        hook_runner=HookRunner([BlockingHook()]),
        on_event=events.append,
    )

    result = loop.run()

    assert result == RunResult(status="failed", reason="Fix setup first.")
    failed = next(event for event in events if event["type"] == "run_failed")
    assert failed["reason"] == "Fix setup first."


def test_before_run_context_is_visible_to_model_but_not_persisted() -> None:
    class ContextHook:
        def before_run(self, ctx: BeforeRunContext) -> HookResponse:
            assert ctx.allowed_tools == ("read_file", "run_command")
            return HookResponse(
                context_blocks=(
                    PromptContextBlock("Run Hint", "Use the narrow path.", "test"),
                )
            )

    loop, session, model_adapter, session_store, _ = build_loop(
        hook_runner=HookRunner([ContextHook()])
    )

    result = loop.run()

    assert result == RunResult(status="complete")
    model_messages = model_adapter.calls[0]["messages"]
    assert model_messages[-1] == {
        "role": "user",
        "content": (
            "# Additional Context For This Turn\n"
            "\n"
            "## Run Hint (test)\n"
            "Use the narrow path."
        ),
    }
    assert model_messages[-1] not in session.messages
    assert model_messages[-1] not in session_store.messages


def test_before_model_turn_context_is_visible_to_model_but_not_persisted() -> None:
    class TurnHook:
        def before_model_turn(self, ctx: BeforeModelTurnContext) -> HookResponse:
            assert ctx.tool_steps_used == 0
            return HookResponse(
                context_blocks=(
                    PromptContextBlock("Turn Hint", "Check the current transcript."),
                )
            )

    loop, session, model_adapter, session_store, _ = build_loop(
        hook_runner=HookRunner([TurnHook()])
    )

    result = loop.run()

    assert result == RunResult(status="complete")
    model_messages = model_adapter.calls[0]["messages"]
    assert model_messages[-1]["content"] == (
        "# Additional Context For This Turn\n"
        "\n"
        "## Turn Hint\n"
        "Check the current transcript."
    )
    assert model_messages[-1] not in session.messages
    assert model_messages[-1] not in session_store.messages


def test_before_model_turn_aggregates_context_blocks_in_hook_order() -> None:
    first = PromptContextBlock("First", "One")
    second = PromptContextBlock("Second", "Two")

    class FirstHook:
        def before_model_turn(self, ctx: BeforeModelTurnContext) -> HookResponse:
            return HookResponse(context_blocks=(first,))

    class SecondHook:
        def before_model_turn(self, ctx: BeforeModelTurnContext) -> HookResponse:
            return HookResponse(context_blocks=(second,))

    response = HookRunner([FirstHook(), SecondHook()]).before_model_turn(
        BeforeModelTurnContext(None, "", "", (), 0)
    )

    assert response.context_blocks == (first, second)


def test_no_hook_runner_keeps_basic_loop_behavior() -> None:
    loop, session, model_adapter, _, _ = build_loop()

    result = loop.run()

    assert result == RunResult(status="complete")
    assert model_adapter.calls[0]["messages"] == [
        {"role": "system", "content": "system"}
    ]
    assert session.messages == [
        {"role": "system", "content": "system"},
        {"role": "assistant", "content": "TASK COMPLETE"},
    ]


def test_repeated_failure_nudge_reaches_model_without_blocking_tool_calls() -> None:
    class CompletingAgent(FakeAgent):
        def should_stop(self, session: Session) -> bool:
            return any(
                message.get("role") == "assistant"
                and message.get("content") == "TASK COMPLETE"
                for message in session.messages
            )

    class FailingExecutor(FakeToolExecutor):
        def execute(self, action: dict) -> dict:
            self.actions.append(action)
            return {"ok": False, "tool": action["tool"], "error": "same_failure"}

    action = {"tool": "read_file", "args": {"path": "same.py"}}
    model_adapter = SequenceModelAdapter(
        [
            *[
                {
                    "message": {"role": "assistant", "content": "Trying."},
                    "actions": [action],
                }
                for _ in range(3)
            ],
            {
                "message": {"role": "assistant", "content": "TASK COMPLETE"},
                "actions": [],
            },
        ]
    )
    session = Session(repo_root=None)
    executor = FailingExecutor()
    loop = AgentLoop(
        session=session,
        model_adapter=model_adapter,
        tool_executor=executor,
        tool_results=FakeToolResults(),
        agent=CompletingAgent(),
        display=NullDisplay(),
        hook_runner=HookRunner([RepeatedFailureNudge("Change course.")]),
    )

    result = loop.run()

    assert result == RunResult(status="complete")
    assert executor.actions == [action, action, action]
    assert "Change course." in model_adapter.calls[3]["messages"][-1]["content"]
    assert all(
        "Change course." not in str(call["messages"])
        for call in model_adapter.calls[:3]
    )
    assert all("Change course." not in str(message) for message in session.messages)


def test_command_failure_nudge_is_transient_and_next_command_still_runs() -> None:
    class CompletingAgent(FakeAgent):
        def should_stop(self, session: Session) -> bool:
            return any(
                message.get("role") == "assistant"
                and message.get("content") == "TASK COMPLETE"
                for message in session.messages
            )

    class FailingCommandExecutor(FakeToolExecutor):
        def execute(self, action: dict) -> dict:
            self.actions.append(action)
            return {
                "ok": False,
                "tool": "run_command",
                "failure_class": "command_not_found",
                "platform": "Windows",
                "shell": "PowerShell",
            }

    actions = [
        {"tool": "run_command", "args": {"command": command}}
        for command in ("grep x", "wc -l", "missing-tool", "Get-ChildItem")
    ]
    model_adapter = SequenceModelAdapter(
        [
            *[
                {
                    "message": {"role": "assistant", "content": "Trying."},
                    "actions": [action],
                }
                for action in actions
            ],
            {
                "message": {"role": "assistant", "content": "TASK COMPLETE"},
                "actions": [],
            },
        ]
    )
    session = Session(repo_root=None)
    executor = FailingCommandExecutor()
    loop = AgentLoop(
        session=session,
        model_adapter=model_adapter,
        tool_executor=executor,
        tool_results=FakeToolResults(),
        agent=CompletingAgent(),
        display=NullDisplay(),
        hook_runner=HookRunner(
            [
                RepeatedFailureNudge(
                    "Change course.",
                    run_command_guidance="Reassess commands.",
                )
            ]
        ),
    )

    result = loop.run()

    assert result == RunResult(status="complete")
    assert executor.actions == actions
    assert "Reassess commands." in model_adapter.calls[3]["messages"][-1]["content"]
    assert all(
        "Reassess commands." not in str(call["messages"])
        for call in (*model_adapter.calls[:3], model_adapter.calls[4])
    )
    assert all("Reassess commands." not in str(message) for message in session.messages)


def test_before_tool_execute_mutation_chains_to_later_hooks() -> None:
    seen = []

    class MutatingHook:
        def before_tool_execute(self, ctx: BeforeToolExecuteContext) -> HookResponse:
            return HookResponse(
                action={**ctx.action, "args": {"path": "changed.txt"}},
                approval_reason="needs review",
            )

    class SeeingHook:
        def before_tool_execute(self, ctx: BeforeToolExecuteContext) -> HookResponse:
            seen.append(ctx.action)
            return HookResponse(approval_reason="second reason")

    decision = HookRunner([MutatingHook(), SeeingHook()]).before_tool_execute(
        BeforeToolExecuteContext(
            session_id=None,
            repo_root="",
            action={"tool": "read_file", "args": {"path": "original.txt"}},
            tool_steps_used=0,
        )
    )

    assert seen == [{"tool": "read_file", "args": {"path": "changed.txt"}}]
    assert decision.action == {"tool": "read_file", "args": {"path": "changed.txt"}}
    assert decision.approval_reason == "needs review; second reason"


def test_before_tool_execute_block_stops_later_hooks() -> None:
    calls = []

    class MutatingHook:
        def before_tool_execute(self, ctx: BeforeToolExecuteContext) -> HookResponse:
            calls.append("mutate")
            return HookResponse(
                action={"tool": "read_file", "args": {"path": "changed.txt"}},
                approval_reason="first reason",
            )

    class BlockingHook:
        def before_tool_execute(self, ctx: BeforeToolExecuteContext) -> HookResponse:
            calls.append("block")
            assert ctx.action["tool_call_id"] == "call_1"
            return HookResponse(
                block=True,
                reason="blocked",
                action={"tool": "read_file", "args": {"path": "blocked.txt"}},
                approval_reason="second reason",
            )

    class LaterHook:
        def before_tool_execute(self, ctx: BeforeToolExecuteContext) -> HookResponse:
            calls.append("later")
            return HookResponse()

    decision = HookRunner([MutatingHook(), BlockingHook(), LaterHook()]).before_tool_execute(
        BeforeToolExecuteContext(
            session_id=None,
            repo_root="",
            action={"tool": "read_file", "tool_call_id": "call_1"},
            tool_steps_used=0,
        )
    )

    assert calls == ["mutate", "block"]
    assert decision.block is True
    assert decision.reason == "blocked"
    assert decision.action == {
        "tool": "read_file",
        "args": {"path": "blocked.txt"},
        "tool_call_id": "call_1",
    }
    assert decision.approval_reason == "first reason; second reason"


def test_tool_result_mutation_chains_to_later_hooks() -> None:
    seen = []

    class FirstHook:
        def tool_result(self, ctx: ToolResultContext) -> HookResponse:
            return HookResponse(updated_tool_output={**ctx.result, "first": True})

    class SecondHook:
        def tool_result(self, ctx: ToolResultContext) -> HookResponse:
            seen.append(ctx.result)
            return HookResponse(updated_tool_output={**ctx.result, "second": True})

    result = HookRunner([FirstHook(), SecondHook()]).tool_result(
        ToolResultContext(
            session_id=None,
            repo_root="",
            action={"tool": "read_file"},
            result={"ok": True},
        )
    )

    assert seen == [{"ok": True, "first": True}]
    assert result.updated_tool_output == {
        "ok": True,
        "first": True,
        "second": True,
    }


def test_tool_result_aggregates_additional_context_in_hook_order() -> None:
    class FirstHook:
        def tool_result(self, ctx: ToolResultContext) -> HookResponse:
            return HookResponse(additional_context="First hint.")

    class SecondHook:
        def tool_result(self, ctx: ToolResultContext) -> HookResponse:
            return HookResponse(additional_context="Second hint.")

    response = HookRunner([FirstHook(), SecondHook()]).tool_result(
        ToolResultContext(None, "", {"tool": "read_file"}, {"ok": True})
    )

    assert response.updated_tool_output == {"ok": True}
    assert response.additional_context == "First hint.\n\nSecond hint."


def test_agent_loop_tool_started_uses_mutated_tool_action() -> None:
    events = []

    class MutatingHook:
        def before_tool_execute(self, ctx: BeforeToolExecuteContext) -> HookResponse:
            return HookResponse(
                action={
                    **ctx.action,
                    "args": {"path": "changed.txt"},
                }
            )

    loop, _, _, _, tool_executor = build_loop(
        hook_runner=HookRunner([MutatingHook()]),
        on_event=events.append,
        model_reply={
            "message": {"role": "assistant", "content": "Reading."},
            "actions": [
                {
                    "tool": "read_file",
                    "tool_call_id": "call_1",
                    "args": {"path": "original.txt"},
                }
            ],
        },
    )

    result = loop.run()

    assert result == RunResult(status="complete")
    assert tool_executor.actions == [
        {
            "tool": "read_file",
            "tool_call_id": "call_1",
            "args": {"path": "changed.txt"},
        }
    ]
    tool_started = next(event for event in events if event["type"] == "tool_started")
    tool_finished = next(event for event in events if event["type"] == "tool_finished")
    assert tool_started["path"] == "changed.txt"
    assert tool_finished["path"] == "changed.txt"


def test_agent_loop_tool_hook_blocks_execution() -> None:
    events = []

    class BlockingHook:
        def before_tool_execute(self, ctx: BeforeToolExecuteContext) -> HookResponse:
            assert ctx.action == {"tool": "read_file", "tool_call_id": "call_1"}
            return HookResponse(block=True)

    loop, _, _, _, tool_executor = build_loop(
        hook_runner=HookRunner([BlockingHook()]),
        on_event=events.append,
        model_reply={
            "message": {"role": "assistant", "content": "Reading."},
            "actions": [{"tool": "read_file", "tool_call_id": "call_1"}],
        },
    )

    result = loop.run()

    assert result == RunResult(status="complete")
    assert tool_executor.actions == []
    assert loop.display.tool_results == [
        (
            {"tool": "read_file", "tool_call_id": "call_1"},
            {
                "ok": False,
                "error": "blocked_by_hook",
                "reason": "blocked by before_tool_execute hook",
                "guidance": "",
            },
        )
    ]
    assert "tool_started" not in [event["type"] for event in events]
    assert events[-2]["type"] == "tool_finished"
    assert events[-2]["ok"] is False
    assert events[-2]["tool_call_id"] == "call_1"
