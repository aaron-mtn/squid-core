"""Generic AgentLoop state machine and public run result."""

from dataclasses import dataclass
import time
from typing import Callable, Literal

from squid_core.agent_loop.event_emitter import (
    action_event_fields,
    compact_event_text,
    emit_agent_event,
    tool_preparing_event,
)
from squid_core.agent_loop.transcript_recording import (
    record_message,
    record_messages,
    record_pending_tool_results,
    reconcile_system_message,
    turn_context_message,
    user_instruction_message,
)
from squid_core.compaction.token_estimate import estimate_messages_tokens
from squid_core.events import EventCallback
from squid_core.hooks import (
    BeforeAcceptFinalContext,
    BeforeModelTurnContext,
    BeforeRunContext,
    BeforeToolExecuteContext,
    CompactionAppliedContext,
    FollowUpRunRequest,
    HookResponse,
    HookRunner,
    PromptContextBlock,
    ReadResultTrimmedContext,
    RunFinishedContext,
    ToolResultContext,
)
from squid_core.model_adapter import ModelRequestError, model_request_error_details
from squid_core.providers.base import StreamCancelled
from squid_core.safety import ActionPreflight, ApprovalPolicy, denied_result
from squid_core.settings import ToolResultTrimmingSettings
from squid_core.tool_result_trimming import render_messages_for_model

MAX_CONSECUTIVE_INVALID_REPLIES = 3
MAX_CONSECUTIVE_BLOCKED_FINALS = 3
REJECTION_REASON_EVENT_MAX_CHARS = 500


class _CancellationRequested(Exception):
    pass


@dataclass(frozen=True)
class RunResult:
    status: Literal["complete", "failed", "interrupted"]
    reason: str = ""
    follow_up_requests: tuple[FollowUpRunRequest, ...] = ()
    hook_notes: tuple[str, ...] = ()


class AgentLoop:
    """Own the generic loop without coding policy, prompts, or Tool definitions."""

    # Store the parts needed to run the loop.
    # The loop depends on the session, model adapter, tool executor,
    # tool result formatter, coding agent behavior, display layer, and optional
    # durable session storage.
    def __init__(
        self,
        session,
        model_adapter,
        tool_executor,
        tool_results,
        agent,
        display,
        session_store=None,
        compactor=None,
        max_tool_steps: int | None = None,
        approval_policy: ApprovalPolicy | None = None,
        stream_assistant_text: bool = False,
        # Optional observer callback for compact runtime facts. Terminal
        # display remains separate; external Python wrappers should listen here
        # instead of scraping human-facing output.
        on_event: EventCallback | None = None,
        should_cancel: Callable[[], bool] | None = None,
        hook_runner: HookRunner | None = None,
        action_preflight: ActionPreflight | None = None,
        tool_result_trimming: ToolResultTrimmingSettings | None = None,
        model_request_max_retries: int = 2,
        model_request_retry_backoff_ms: int = 250,
    ) -> None:
        self.session = session
        self.model_adapter = model_adapter
        self.tool_executor = tool_executor
        self.tool_results = tool_results
        self.agent = agent
        self.display = display
        self.session_store = session_store
        self.compactor = compactor
        self.max_tool_steps = max_tool_steps
        self.approval_policy = approval_policy or ApprovalPolicy("never")
        self.action_preflight = action_preflight or ActionPreflight.from_runtime(
            tool_executor,
            self.approval_policy,
        )
        self.stream_assistant_text = stream_assistant_text
        self.on_event = on_event
        self.should_cancel = should_cancel or (lambda: False)
        self.hook_runner = hook_runner if hook_runner is not None else HookRunner()
        self.tool_result_trimming = (
            tool_result_trimming
            if tool_result_trimming is not None
            else ToolResultTrimmingSettings()
        )
        self.model_request_max_retries = model_request_max_retries
        self.model_request_retry_backoff_ms = model_request_retry_backoff_ms
        self._run_context_blocks: list[PromptContextBlock] = []

    def _check_cancelled(self) -> None:
        if self.should_cancel():
            raise _CancellationRequested

    def _wait_for_retry(self, backoff_ms: int) -> None:
        """Preserve retry timing while checking cancellation during the wait."""
        deadline = time.monotonic() + (backoff_ms / 1000)
        while True:
            self._check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.05))

    def _unwind_incomplete_run(
        self,
        *,
        status: Literal["failed", "interrupted"],
        reason: str,
        run_stage: str,
        pending_tool_actions: list[dict],
        pending_tool_results: list[dict],
        tool_steps_used: int,
        runtime_event: dict,
        preserve_error: bool,
    ) -> HookResponse:
        """Repair transcript state and publish one incomplete-run lifecycle."""
        def repair() -> None:
            record_pending_tool_results(
                self.session,
                self.session_store,
                self.tool_results,
                pending_tool_actions,
                pending_tool_results,
                interrupted=status == "interrupted",
            )

        def record_lifecycle() -> None:
            self._emit_runtime_event(
                {**runtime_event, "status": status, "stage": run_stage}
            )

        def update_status() -> None:
            self._update_status(status)

        def finish_hook() -> HookResponse:
            decision = self._run_finished_decision(
                status=status,
                reason=reason,
                tool_steps_used=tool_steps_used,
            )
            self._emit_event({"type": "run_finished", "status": status})
            return decision

        if not preserve_error:
            repair()
            record_lifecycle()
            update_status()
            return finish_hook()

        # Cleanup diagnostics must never replace the exception already unwinding.
        for cleanup in (repair, record_lifecycle, update_status):
            try:
                cleanup()
            except Exception:
                pass
        try:
            return finish_hook()
        except Exception:
            return HookResponse()

    # Update durable session status when persistence is wired in.
    def _update_status(self, status: str) -> None:
        if self.session_store is not None:
            self.session_store.update_status(status)

    def _update_status_preserving_error(self, status: str) -> None:
        """Avoid replacing an in-flight runtime exception with a storage error."""
        try:
            self._update_status(status)
        except Exception:
            pass

    def _session_id(self) -> str | None:
        if self.session_store is None:
            return None
        return getattr(self.session_store, "session_id", None)

    def _run_finished_decision(
        self,
        *,
        status: str,
        reason: str = "",
        final_message: str = "",
        tool_steps_used: int = 0,
    ) -> HookResponse:
        return self.hook_runner.run_finished(
            RunFinishedContext(
                session_id=self._session_id(),
                repo_root=str(self.session.repo_root),
                status=status,
                reason=reason,
                final_message=final_message,
                files_read=tuple(sorted(getattr(self.session, "files_read", set()))),
                files_written=tuple(
                    sorted(getattr(self.session, "files_written", set()))
                ),
                tool_steps_used=tool_steps_used,
            ),
            event_sink=self._emit_hook_event,
        )

    def _hook_notes_from_decision(
        self,
        decision: HookResponse,
    ) -> tuple[str, ...]:
        return tuple(
            note
            for note in decision.notes
            if isinstance(note, str) and note.strip()
        )

    def _emit_event(self, event: dict) -> None:
        emit_agent_event(self.on_event, self._session_id(), event)

    def _emit_runtime_event(self, event: dict) -> None:
        """Persist a lifecycle fact before publishing it to optional observers."""
        if self.session_store is not None:
            append_runtime_event = getattr(
                self.session_store,
                "append_runtime_event",
                None,
            )
            if callable(append_runtime_event):
                append_runtime_event(event)
        self._emit_event(event)

    def _emit_runtime_event_preserving_error(self, event: dict) -> None:
        """Record terminal evidence without masking the exception being handled."""
        try:
            self._emit_runtime_event(event)
        except Exception:
            # Preserve the original runtime exception if diagnostic persistence
            # itself fails while the loop is already unwinding.
            pass

    def _emit_hook_event(self, event: dict) -> None:
        """Keep Hook diagnostics from becoming a new runtime failure mode."""
        try:
            self._emit_runtime_event(event)
        except Exception:
            pass

    def _persist_compaction_event(self, event: dict) -> None:
        """Persist compaction lifecycle state before any applied notification."""
        # Persistence retains the complete lifecycle. Presentation observes
        # only boundaries needed to own and settle its transient status line.
        if self.session_store is not None:
            self.session_store.append_compaction_event(event)
        event_type = event.get("type")
        if event_type in {
            "compaction_started",
            "compaction_failed",
        }:
            self._emit_event(event)

    def _notify_compaction_applied(self, event: dict) -> None:
        """Notify Hooks and UI after the compacted transcript becomes active."""
        self.hook_runner.compaction_applied(
            CompactionAppliedContext(
                session_id=self._session_id(),
                repo_root=str(self.session.repo_root),
            ),
            event_sink=self._emit_hook_event,
        )
        self._emit_event(event)

    def _maybe_compact(self) -> None:
        """Run compaction with distinct persistence and post-commit callbacks."""
        self.compactor.maybe_compact(
            self.session,
            event_callback=self._persist_compaction_event,
            post_apply_callback=self._notify_compaction_applied,
        )

    def _should_stream_assistant_text(self) -> bool:
        if not self.stream_assistant_text:
            return False

        supports_streaming = getattr(self.model_adapter, "supports_streaming", None)
        return callable(supports_streaming) and bool(supports_streaming())

    def _respond(self, tool_definitions: list[dict], extra_messages=None) -> dict:
        messages = list(self.session.messages)
        if extra_messages:
            messages = [*messages, *extra_messages]
        messages = render_messages_for_model(
            messages,
            self.tool_result_trimming,
            on_read_result_trimmed=self._handle_trimmed_read_result,
        )
        request_event = {
            "provider": getattr(self.model_adapter, "provider_name", None),
            "model": getattr(self.model_adapter, "model_name", None),
            "reasoning_mode": getattr(self.model_adapter, "reasoning_mode", None),
            "reasoning_token_budget": getattr(
                self.model_adapter,
                "reasoning_token_budget",
                None,
            ),
            "message_count": len(messages),
            "estimated_request_tokens": estimate_messages_tokens(messages),
        }
        request_event = {
            name: value for name, value in request_event.items() if value is not None
        }
        self._emit_runtime_event({"type": "model_request_started", **request_event})
        started_at = time.perf_counter()
        max_attempts = self.model_request_max_retries + 1
        attempt = 1

        while True:
            attempt_started_at = time.perf_counter()
            try:
                supports_streaming_method = getattr(
                    self.model_adapter,
                    "supports_streaming",
                    None,
                )
                supports_streaming = callable(supports_streaming_method) and bool(
                    supports_streaming_method()
                )
                if not supports_streaming:
                    reply = self.model_adapter.respond(
                        messages=messages,
                        tool_definitions=tool_definitions,
                    )
                else:
                    present_stream = self._should_stream_assistant_text()
                    if present_stream:
                        self._emit_event(
                            {
                                "type": "assistant_message_started",
                                "phase": "narration",
                            }
                        )

                    def on_text_delta(delta: str) -> None:
                        if not present_stream:
                            return
                        self._emit_event(
                            {
                                "type": "assistant_delta",
                                "phase": "narration",
                                "delta": delta,
                            }
                        )

                    def on_reasoning_delta(delta: str) -> None:
                        if not present_stream:
                            return
                        self._emit_event(
                            {
                                "type": "assistant_reasoning_delta",
                                "phase": "reasoning",
                                "delta": delta,
                            }
                        )

                    def on_tool_preparing(tool_call: dict) -> None:
                        if not present_stream:
                            return
                        self._emit_event(tool_preparing_event(tool_call))

                    reply = self.model_adapter.respond_streaming(
                        messages=messages,
                        tool_definitions=tool_definitions,
                        on_text_delta=on_text_delta,
                        on_reasoning_delta=on_reasoning_delta,
                        on_tool_preparing=on_tool_preparing,
                        should_cancel=self.should_cancel,
                    )
            except StreamCancelled:
                raise _CancellationRequested
            except BaseException as error:
                details = model_request_error_details(error)
                transient = details.pop("_transient")
                self._emit_runtime_event_preserving_error(
                    {
                        "type": "model_request_failed",
                        **request_event,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "duration_ms": round(
                            (time.perf_counter() - attempt_started_at) * 1000
                        ),
                        **details,
                    }
                )
                if (
                    not isinstance(error, ModelRequestError)
                    or not transient
                    or attempt >= max_attempts
                ):
                    raise

                next_attempt = attempt + 1
                backoff_ms = min(
                    self.model_request_retry_backoff_ms * (2 ** (attempt - 1)),
                    10_000,
                )
                previous_error_type = details.get(
                    "underlying_error_type", details["error_type"]
                )
                previous_error_message = details.get(
                    "underlying_error_message", details["error_message"]
                )
                retry_event = {
                    **request_event,
                    "attempt": next_attempt,
                    "max_attempts": max_attempts,
                    "failure_class": details["failure_class"],
                    "previous_error_type": previous_error_type,
                    "previous_error_message": previous_error_message,
                    "backoff_ms": backoff_ms,
                }
                self._emit_runtime_event(
                    {"type": "model_request_retry_scheduled", **retry_event}
                )
                if backoff_ms:
                    self._wait_for_retry(backoff_ms)
                attempt = next_attempt
                self._emit_runtime_event(
                    {"type": "model_request_retry_started", **retry_event}
                )
                continue

            finished_event = {
                "type": "model_request_finished",
                **request_event,
                "duration_ms": round((time.perf_counter() - started_at) * 1000),
            }
            if attempt > 1:
                finished_event.update(
                    {
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "retry_count": attempt - 1,
                    }
                )
            self._emit_runtime_event(finished_event)
            return reply

    def _present_reasoning(self, reply: dict) -> None:
        """Publish one normalized, non-empty reasoning block before reply output."""

        reasoning = reply.get("reasoning")
        if not isinstance(reasoning, str) or not reasoning.strip():
            return

        # Display is deliberately optional at the reusable Core boundary. The
        # event remains useful to other observers without imposing UI policy.
        reasoning_message = getattr(self.display, "reasoning_message", None)
        if callable(reasoning_message):
            reasoning_message(reasoning)
        self._emit_event(
            {
                "type": "assistant_reasoning",
                "phase": "reasoning",
                "message": reasoning,
                "text": reasoning,
            }
        )

    def _handle_trimmed_read_result(self, trimmed: ReadResultTrimmedContext) -> None:
        """Keep reread coverage aligned with the request-visible transcript."""
        self.hook_runner.read_result_trimmed(
            trimmed,
            event_sink=self._emit_hook_event,
        )

    # Seed a new transcript, or reconcile the one run-scoped message in an
    # existing transcript. A session may change specialization between runs,
    # while prompt, Tools, Hooks, and metadata stay fixed within each run.
    def _ensure_initial_messages(self) -> None:
        initial_messages = self.agent.initial_messages()
        if not self.session.messages:
            record_messages(
                self.session,
                self.session_store,
                *initial_messages,
            )
            return

        intended_system_messages = [
            message for message in initial_messages if message.get("role") == "system"
        ]
        if len(intended_system_messages) != 1:
            raise ValueError(
                "Agent.initial_messages() must contain exactly one system message"
            )
        reconcile_system_message(
            self.session,
            self.session_store,
            intended_system_messages[0],
        )

    def _allowed_tools_from_definitions(self, tool_definitions: list[dict]) -> tuple[str, ...]:
        names = []
        for definition in tool_definitions:
            if not isinstance(definition, dict):
                return ()
            name = definition.get("name")
            if not isinstance(name, str):
                function = definition.get("function")
                if isinstance(function, dict):
                    name = function.get("name")
            if isinstance(name, str):
                names.append(name)
        return tuple(names)

    # Run the agent loop until the agent says it should stop.
    # Each loop iteration:
    # 1. ensures the transcript has initial messages for new sessions
    # 2. asks the model for the next reply and tool actions
    # 3. validates whether that reply counts as a real agent turn
    # 4. rejects invalid replies before they enter assistant history
    # 5. stores valid assistant replies in the session
    # 6. executes each tool action
    # 7. announces each tool result to the display
    # 8. converts tool results into transcript messages
    # 9. stores those result messages in the session
    def run(self) -> RunResult:
        # Once an assistant tool turn is committed, retain the whole ordered
        # batch plus its completed prefix. Recovery can then close every emitted
        # call ID without pretending that an untouched suffix was executed.
        pending_tool_actions: list[dict] = []
        pending_tool_results: list[dict] = []
        tool_steps_used = 0
        run_stage = "startup"

        try:
            self._check_cancelled()
            # Existing sessions keep their conversation but adopt this run's
            # system prompt. This decision stays here beside run startup so a
            # future lifecycle change cannot separate prompt reconciliation
            # from the current run's Tool and Hook configuration.
            self._ensure_initial_messages()
            self._emit_event(
                {
                    "type": "run_started",
                    "repo_root": str(self.session.repo_root),
                    "task": getattr(self.agent, "task", ""),
                }
            )
            tool_definitions = self.agent.tool_definitions()
            self._emit_hook_event(
                {
                    "type": "hooks_configured",
                    "hooks": list(self.hook_runner.hook_names()),
                }
            )
            run_stage = "before_run_hook"
            before_run = self.hook_runner.before_run(
                BeforeRunContext(
                    session_id=self._session_id(),
                    repo_root=str(self.session.repo_root),
                    task=getattr(self.agent, "task", ""),
                    skill_name=getattr(self.agent, "skill_name", None),
                    allowed_tools=self._allowed_tools_from_definitions(tool_definitions),
                ),
                event_sink=self._emit_hook_event,
            )
            if before_run.block:
                reason = (
                    before_run.reason
                    or before_run.guidance
                    or "blocked by before_run hook"
                )
                self._emit_runtime_event(
                    {
                        "type": "run_failed",
                        "status": "failed",
                        "stage": run_stage,
                        "reason": reason,
                    }
                )
                self._update_status("failed")
                run_finished = self._run_finished_decision(
                    status="failed",
                    reason=reason,
                )
                self._emit_event({"type": "run_finished", "status": "failed"})
                return RunResult(
                    status="failed",
                    reason=reason,
                    follow_up_requests=run_finished.follow_up_requests,
                    hook_notes=self._hook_notes_from_decision(run_finished),
                )
            self._run_context_blocks = list(before_run.context_blocks)

            consecutive_invalid_replies = 0
            consecutive_blocked_finals = 0
            stopped_after_repeated_invalid_replies = False
            stopped_after_repeated_blocked_finals = False
            stopped_after_step_limit = False
            failure_reason = ""
            final_message = ""
            pending_runtime_messages = []

            # Keep going until the agent says the task is complete.
            while not self.agent.should_stop(self.session):
                self._check_cancelled()
                if (
                    self.max_tool_steps is not None
                    and tool_steps_used >= self.max_tool_steps
                ):
                    self.display.step_limit_reached(self.max_tool_steps)
                    run_stage = "tool_step_limit"
                    stopped_after_step_limit = True
                    failure_reason = f"reached tool step limit: {self.max_tool_steps}"
                    break

                if self.compactor is not None:
                    run_stage = "compaction"
                    self._check_cancelled()
                    self._maybe_compact()
                    self._check_cancelled()

                # Ask the model for the next assistant reply and any tool actions.
                self.display.thinking()
                self._check_cancelled()
                run_stage = "before_model_turn_hook"
                turn_context = self.hook_runner.before_model_turn(
                    BeforeModelTurnContext(
                        session_id=self._session_id(),
                        repo_root=str(self.session.repo_root),
                        task=getattr(self.agent, "task", ""),
                        messages=tuple(self.session.messages),
                        tool_steps_used=tool_steps_used,
                    ),
                    event_sink=self._emit_hook_event,
                )
                context_message = turn_context_message(
                    [
                        *self._run_context_blocks,
                        *turn_context.context_blocks,
                    ]
                )
                extra_messages = [
                    *([context_message] if context_message is not None else []),
                    *pending_runtime_messages,
                ]
                run_stage = "model_request"
                reply = self._respond(tool_definitions, extra_messages=extra_messages)
                run_stage = "reply_processing"
                pending_runtime_messages = []
                self._check_cancelled()

                raw_reply_entry = None
                if self.session_store is not None:
                    raw_reply_entry = self.session_store.append_model_reply(reply["raw_reply"])

                self.display.debug_reply(reply)

                # Ask the coding agent whether this reply counts as a valid turn.
                # Invalid replies include narration-only turns, malformed tool calls,
                # wrong tool counts, and invalid finals.
                validation = self.agent.validate_reply(reply)

                # If the reply is invalid, do not store the assistant message in
                # the transcript. Instead inject a reason-specific correction and retry.
                if not validation["ok"]:
                    consecutive_invalid_replies += 1
                    if self.session_store is not None:
                        self.session_store.append_reply_rejection(
                            reply,
                            reason=validation["reason"],
                            consecutive_invalid_attempt=consecutive_invalid_replies,
                            raw_reply_id=raw_reply_entry["id"],
                        )
                    if getattr(self.display, "debug", False):
                        self.display.assistant_message(reply["message"]["content"])
                    self.display.reply_rejected(validation["reason"])
                    rejection_reason = compact_event_text(
                        validation["reason"],
                        REJECTION_REASON_EVENT_MAX_CHARS,
                    )
                    self._emit_event(
                        {
                            "type": "reply_rejected",
                            "reason": rejection_reason,
                        }
                    )

                    if consecutive_invalid_replies == MAX_CONSECUTIVE_INVALID_REPLIES:
                        self.display.repeated_invalid_replies(
                            validation["reason"],
                            consecutive_invalid_replies,
                        )
                        record_message(
                            self.session,
                            self.session_store,
                            self.agent.failure_message_for_invalid_replies(
                                validation["reason"],
                                consecutive_invalid_replies,
                            )
                        )
                        stopped_after_repeated_invalid_replies = True
                        failure_reason = validation["reason"]
                        break

                    pending_runtime_messages.append(
                        self.agent.correction_message_for(validation["reason"])
                    )
                    continue

                self._check_cancelled()

                # Only valid assistant replies are added to normal transcript history.
                consecutive_invalid_replies = 0
                assistant_content = reply["message"]["content"]
                self._check_cancelled()
                if not reply["actions"]:
                    final_decision = self.hook_runner.before_accept_final(
                        BeforeAcceptFinalContext(
                            session_id=self._session_id(),
                            repo_root=str(self.session.repo_root),
                            message=reply["message"],
                            content=assistant_content,
                            files_read=tuple(
                                sorted(getattr(self.session, "files_read", set()))
                            ),
                            files_written=tuple(
                                sorted(getattr(self.session, "files_written", set()))
                            ),
                            tool_steps_used=tool_steps_used,
                        ),
                        event_sink=self._emit_hook_event,
                    )
                    if final_decision.block:
                        consecutive_blocked_finals += 1
                        guidance = (
                            final_decision.guidance
                            or final_decision.reason
                            or "Final response was blocked by before_accept_final hook. Continue the task."
                        )
                        self.display.reply_rejected("final_blocked_by_hook")
                        self._emit_event(
                            {
                                "type": "reply_rejected",
                                "reason": "final_blocked_by_hook",
                                "detail": compact_event_text(
                                    guidance,
                                    REJECTION_REASON_EVENT_MAX_CHARS,
                                ),
                                "consecutive_blocked_finals": (
                                    consecutive_blocked_finals
                                ),
                                "limit": MAX_CONSECUTIVE_BLOCKED_FINALS,
                            }
                        )
                        if (
                            consecutive_blocked_finals
                            >= MAX_CONSECUTIVE_BLOCKED_FINALS
                        ):
                            # A structurally valid final can be rejected for runtime
                            # policy reasons, so keep this guard distinct from invalid
                            # reply and Tool-step accounting.
                            reason_detail = compact_event_text(
                                final_decision.reason,
                                REJECTION_REASON_EVENT_MAX_CHARS,
                            )
                            guidance_detail = compact_event_text(
                                final_decision.guidance,
                                REJECTION_REASON_EVENT_MAX_CHARS,
                            )
                            failure_reason = (
                                "before_accept_final rejected "
                                f"{consecutive_blocked_finals} consecutive final responses"
                            )
                            if reason_detail:
                                failure_reason += (
                                    f"; latest hook reason: {reason_detail}"
                                )
                            if guidance_detail:
                                failure_reason += (
                                    f"; latest hook guidance: {guidance_detail}"
                                )
                            run_stage = "before_accept_final_limit"
                            stopped_after_repeated_blocked_finals = True
                            break
                        pending_runtime_messages.append(
                            user_instruction_message(guidance)
                        )
                        continue
                    if final_decision.report_note:
                        assistant_content = (
                            f"{assistant_content.rstrip()}\n\n"
                            f"{final_decision.report_note.strip()}"
                        )
                        reply["message"] = {
                            **reply["message"],
                            "content": assistant_content,
                        }

                if reply["actions"]:
                    self._present_reasoning(reply)
                    self.display.assistant_message(assistant_content)
                    self._check_cancelled()
                    self._emit_event(
                        {
                            "type": "assistant_message",
                            "phase": "narration",
                            "message": assistant_content,
                            "text": assistant_content,
                        }
                    )
                else:
                    self._present_reasoning(reply)
                    self.display.final_message(assistant_content)
                    self._check_cancelled()
                    self._emit_event(
                        {
                            "type": "final_message",
                            "phase": "final",
                            "message": assistant_content,
                            "text": assistant_content,
                        }
                    )
                    final_message = assistant_content
                self._check_cancelled()
                if reply["actions"]:
                    # Establish repair ownership before persistence: the
                    # assistant message commits every emitted tool-call ID.
                    pending_tool_actions = list(reply["actions"])
                    pending_tool_results = []
                record_message(
                    self.session,
                    self.session_store,
                    reply["message"],
                    usage=reply.get("usage"),
                )

                # Execute each requested tool action and collect the raw results.
                results = []
                approval_instructions = []
                successful_tool_action = False
                batch_tool_guidance = []
                seen_tool_guidance = set()
                for action_index, action in enumerate(reply["actions"]):
                    if (
                        self.max_tool_steps is not None
                        and tool_steps_used >= self.max_tool_steps
                    ):
                        self.display.step_limit_reached(self.max_tool_steps)
                        run_stage = "tool_step_limit"
                        stopped_after_step_limit = True
                        failure_reason = (
                            f"reached tool step limit: {self.max_tool_steps}"
                        )
                        break

                    run_stage = "tool_execution"
                    self._check_cancelled()
                    # Entering the per-action pipeline is an attempt, including
                    # actions later blocked by a Hook or denied by approval.
                    hook_tool_steps_used = tool_steps_used
                    tool_steps_used += 1
                    tool_decision = self.hook_runner.before_tool_execute(
                        BeforeToolExecuteContext(
                            session_id=self._session_id(),
                            repo_root=str(self.session.repo_root),
                            action=action,
                            tool_steps_used=hook_tool_steps_used,
                        ),
                        event_sink=self._emit_hook_event,
                    )
                    if tool_decision.block:
                        action = tool_decision.action or action
                        pending_tool_actions[action_index] = action
                        reason = (
                            tool_decision.reason
                            or tool_decision.guidance
                            or "blocked by before_tool_execute hook"
                        )
                        result = {
                            "ok": False,
                            "error": "blocked_by_hook",
                            "reason": reason,
                            "guidance": tool_decision.guidance,
                        }
                        approval_action = action
                    else:
                        action = tool_decision.action or action
                        pending_tool_actions[action_index] = action
                        preflight = self.action_preflight.check(
                            action,
                            additional_approval_reason=tool_decision.approval_reason,
                        )
                        approval_action = preflight.approval_action
                        self._check_cancelled()
                        decision = self.approval_policy.check(approval_action)
                        self._check_cancelled()
                        if decision.approved:
                            self.display.action_started(approval_action)
                            self._check_cancelled()
                            self._emit_event(
                                {
                                    "type": "tool_started",
                                    **action_event_fields(action),
                                }
                            )
                            self._check_cancelled()
                            result = self.tool_executor.execute(
                                preflight.execution_action
                            )
                            pending_tool_results.append(result)
                            self._check_cancelled()
                        else:
                            result = denied_result(action, decision.reason)
                            if decision.instruction.strip():
                                approval_instructions.append(decision.instruction)
                    if len(pending_tool_results) <= action_index:
                        # Preserve blocked and denied results if their result
                        # Hook raises before it can return an updated output.
                        pending_tool_results.append(result)
                    tool_result_response = self.hook_runner.tool_result(
                        ToolResultContext(
                            session_id=self._session_id(),
                            repo_root=str(self.session.repo_root),
                            action=action,
                            result=result,
                        ),
                        event_sink=self._emit_hook_event,
                    )
                    result = tool_result_response.updated_tool_output
                    # A batch postpones the next model decision, so retain every
                    # transient correction produced before control returns to the
                    # model. Exact deduplication prevents a persistent Hook state
                    # from repeating the same message for several sibling actions.
                    additional_context = tool_result_response.additional_context
                    if (
                        additional_context.strip()
                        and additional_context not in seen_tool_guidance
                    ):
                        batch_tool_guidance.append(additional_context)
                        seen_tool_guidance.add(additional_context)
                    pending_tool_results[action_index] = result
                    if result.get("ok", False):
                        successful_tool_action = True
                    self.display.tool_result(approval_action, result)
                    self._check_cancelled()
                    self._emit_event(
                        {
                            "type": "tool_finished",
                            "ok": bool(result.get("ok", False)),
                            **action_event_fields(action),
                        }
                    )
                    results.append(result)
                    if not result.get("ok", False):
                        break

                if batch_tool_guidance:
                    pending_runtime_messages.append(
                        user_instruction_message("\n\n".join(batch_tool_guidance))
                    )

                # The assistant message retains the complete emitted batch.
                # Close untouched call IDs explicitly so the next request and
                # saved transcript remain valid for strict providers.
                for action in pending_tool_actions[len(results) :]:
                    if stopped_after_step_limit:
                        reason = "tool step limit reached before this action"
                        error = "tool_step_limit"
                    else:
                        reason = "an earlier tool action in this batch did not succeed"
                        error = "earlier_action_failed"
                    results.append(
                        {
                            "ok": False,
                            "tool": action.get("tool", ""),
                            "error": error,
                            "reason": reason,
                            "skipped": True,
                        }
                    )
                pending_tool_results = results

                # Convert tool results into transcript messages and store them.
                result_messages = self.tool_results.messages_from_action_results(
                    pending_tool_actions,
                    results,
                )
                self._check_cancelled()
                record_messages(self.session, self.session_store, *result_messages)
                pending_tool_actions = []
                pending_tool_results = []
                record_messages(
                    self.session,
                    self.session_store,
                    *[
                        user_instruction_message(instruction)
                        for instruction in approval_instructions
                    ]
                )

                if reply["actions"]:
                    # Only completed useful work breaks a blocked-final streak;
                    # a Hook block, approval denial, or failed Tool is not progress.
                    if successful_tool_action:
                        consecutive_blocked_finals = 0
                    if stopped_after_step_limit:
                        break

            if (
                stopped_after_repeated_invalid_replies
                or stopped_after_repeated_blocked_finals
                or stopped_after_step_limit
            ):
                self._emit_runtime_event(
                    {
                        "type": "run_failed",
                        "status": "failed",
                        "stage": run_stage,
                        "reason": failure_reason,
                    }
                )
                self._update_status("failed")
                run_finished = self._run_finished_decision(
                    status="failed",
                    reason=failure_reason,
                    tool_steps_used=tool_steps_used,
                )
                self._emit_event({"type": "run_finished", "status": "failed"})
                return RunResult(
                    status="failed",
                    reason=failure_reason,
                    follow_up_requests=run_finished.follow_up_requests,
                    hook_notes=self._hook_notes_from_decision(run_finished),
                )

            self._check_cancelled()
            run_stage = "run_finished_hook"
            try:
                run_finished = self._run_finished_decision(
                    status="complete",
                    final_message=final_message,
                    tool_steps_used=tool_steps_used,
                )
            except Exception as error:
                reason = f"run_finished hook failed: {error or type(error).__name__}"
                self._emit_runtime_event(
                    {
                        "type": "run_failed",
                        "status": "failed",
                        "stage": run_stage,
                        "reason": reason,
                    }
                )
                self._update_status("failed")
                self._emit_event({"type": "run_finished", "status": "failed"})
                return RunResult(status="failed", reason=reason)

            self._update_status("complete")
            self._emit_event({"type": "run_finished", "status": "complete"})
            return RunResult(
                status="complete",
                follow_up_requests=run_finished.follow_up_requests,
                hook_notes=self._hook_notes_from_decision(run_finished),
            )
        except (_CancellationRequested, StreamCancelled):
            run_finished = self._unwind_incomplete_run(
                status="interrupted",
                reason="cancelled",
                run_stage=run_stage,
                pending_tool_actions=pending_tool_actions,
                pending_tool_results=pending_tool_results,
                tool_steps_used=tool_steps_used,
                runtime_event={
                    "type": "run_interrupted",
                    "reason": "cancelled",
                    "requested": True,
                },
                preserve_error=False,
            )
            return RunResult(
                status="interrupted",
                reason="cancelled",
                follow_up_requests=run_finished.follow_up_requests,
                hook_notes=self._hook_notes_from_decision(run_finished),
            )
        except KeyboardInterrupt:
            # Repair any committed tool-call pair and leave durable evidence
            # before preserving the CLI's KeyboardInterrupt control flow.
            self._unwind_incomplete_run(
                status="interrupted",
                reason="KeyboardInterrupt",
                run_stage=run_stage,
                pending_tool_actions=pending_tool_actions,
                pending_tool_results=pending_tool_results,
                tool_steps_used=tool_steps_used,
                runtime_event={
                    "type": "run_interrupted",
                    "interruption_type": "KeyboardInterrupt",
                },
                preserve_error=True,
            )
            raise
        except Exception as error:
            details = model_request_error_details(error)
            details.pop("_transient")
            self._unwind_incomplete_run(
                status="failed",
                reason=details["error_message"],
                run_stage=run_stage,
                pending_tool_actions=pending_tool_actions,
                pending_tool_results=pending_tool_results,
                tool_steps_used=tool_steps_used,
                runtime_event={"type": "run_failed", **details},
                preserve_error=True,
            )
            raise
        except BaseException as error:
            self._unwind_incomplete_run(
                status="interrupted",
                reason=type(error).__name__,
                run_stage=run_stage,
                pending_tool_actions=pending_tool_actions,
                pending_tool_results=pending_tool_results,
                tool_steps_used=tool_steps_used,
                runtime_event={
                    "type": "run_interrupted",
                    "interruption_type": type(error).__name__,
                },
                preserve_error=True,
            )
            raise
