# AgentLoop

`AgentLoop` is Squid Core's generic runtime state machine. It coordinates a
`Session`, `Agent`, `ModelAdapter`, `ToolExecutor`, `ToolResults`, and `Display`,
with optional `SessionStore` and `Compactor`. It also coordinates
`ApprovalPolicy` / `ActionPreflight`, `HookRunner`, cooperative cancellation,
runtime observers, tool-result trimming, and model request retries.

The loop does not own provider-specific transport, application coding policy,
tool definitions, prompt content, or a UI implementation. Those come from its
collaborators. See the repository [architecture](../../docs/ARCHITECTURE.md) for
the broader ownership and dependency boundaries.

## Public API and construction

The package exports `AgentLoop` and `RunResult`:

```python
from squid_core.agent_loop import AgentLoop, RunResult
```

`AgentLoop(...).run()` returns a frozen `RunResult` on normal completion,
controlled failure, or ordinary requested cancellation:

| Field | Meaning |
| --- | --- |
| `status` | `"complete"`, `"failed"`, or `"interrupted"` |
| `reason` | Explanation of failure/interruption; defaults to `""` |
| `follow_up_requests` | Tuple of `FollowUpRunRequest` values from the finishing hooks; defaults to `()` |
| `hook_notes` | Tuple of nonblank string notes from the finishing hooks; defaults to `()` |

Completion follows the Agent's stop rule. Controlled failures include a
`before_run` block, repeated invalid replies, repeated blocked finals, a tool
step limit, and an ordinary exception from the success-path `run_finished`
hook. Other ordinary exceptions normally clean up and re-raise; they do not
become failed `RunResult` values. `KeyboardInterrupt` and other `BaseException`
interruption paths also clean up and re-raise.

Construction requires `session`, `model_adapter`, `tool_executor`,
`tool_results`, `agent`, and `display`. The constructor stores these supplied
collaborators; it does not choose a model or assemble an application.
Optional arguments configure the following seams:

| Concern | Arguments and direct-constructor defaults |
| --- | --- |
| Persistence and context size | `session_store=None`, `compactor=None` |
| Tool budget | `max_tool_steps=None` means no loop-imposed limit |
| Safety | `approval_policy=None` creates `ApprovalPolicy("never")`; `action_preflight=None` derives preflight from the executor's `workspace_root` |
| Presentation and observation | `stream_assistant_text=False`, `on_event=None` |
| Cancellation | `should_cancel=None` installs a callback that always returns false |
| Hooks | `hook_runner=None` creates an empty `HookRunner` |
| Model-visible tool results | `tool_result_trimming=None` creates `ToolResultTrimmingSettings()` |
| Request retries | `model_request_max_retries=2`, `model_request_retry_backoff_ms=250` |

The default trimming settings enable age-based reduction and retain the newest
10 tool results by count, with separate payload limits for older results. The
latest complete assistant tool batch is protected for its first model exposure.
These settings affect the request view, not canonical stored results.

The default approval mode approves without prompting. A host can supply a
different policy and prompting collaborator. The run-construction helpers in
`squid_core/spawner.py` wire collaborators and pass settings into the loop;
their construction choices are separate from these direct-constructor defaults.

## Run lifecycle

The major phases of `run()` are:

1. Check cancellation and seed or reconcile the initial transcript.
2. Publish `run_started`, build the Agent's tool definitions, and run
   `before_run` hooks. A block here returns a controlled failure.
3. While `agent.should_stop(session)` is false, check cancellation and the tool
   budget, optionally compact, then run `before_model_turn` hooks.
4. Assemble temporary request context, render the model-visible tool results,
   and request a model reply, with eligible request retries.
5. Validate the reply. Reject invalid replies before accepting transcript
   history; otherwise take the final-acceptance or tool-action path.
6. Repeat as needed, then invoke `run_finished` hooks and settle terminal
   status, events, and the result.

Failure and interruption paths have their own finishing flow described below.
An Agent whose stop rule already holds can complete without a new model turn.

## Startup and system reconciliation

An empty session receives every message returned by `Agent.initial_messages()`
in order. The current generic Agent returns a system prompt and user task.

A nonempty session keeps its conversation and adopts this run's system message.
For this reconciliation, `Agent.initial_messages()` must contain exactly one
system message; the loop raises `ValueError` otherwise. This check is applied
to resumed sessions, not the empty-session seeding branch. Other initial
messages are not appended during reconciliation; adding a new follow-up task
to an existing conversation is a host responsibility.

If the intended system message is already the sole system message at the
front, reconciliation does nothing. Otherwise it replaces all system messages
with the intended one at the front, preserving every non-system message object
and its relative order. With storage, the reconciliation event is appended
before replacing `session.messages` in memory. Storage replay applies that
replacement without rewriting earlier log entries.

This lets a session resume with a different run specialization while retaining
prior conversation history. The current run's prompt, tools, and hooks can
change together without rewriting all previous messages.

## Model turn preparation

Optional compaction happens before `before_model_turn`, so those hooks see the
current effective `session.messages`, including any applied compaction. The
loop combines run-scoped `PromptContextBlock` values from `before_run` with
turn-scoped blocks into one temporary user-role message headed
`Additional Context For This Turn`. Empty bodies contribute no context.

Pending runtime guidance follows that context message: invalid-reply
corrections, blocked-final guidance, and collected tool-result guidance. These
messages belong only to the next request, not accepted transcript
history. Pending guidance is cleared after a successful model response;
run-scoped blocks are assembled again for subsequent turns.

`render_messages_for_model` then produces the request view without mutating
the active transcript or canonical stored history, applying tool-result trimming.
Its `read_result_trimmed` notifications are forwarded to hooks so their read
coverage can follow what the model actually sees. Details belong in the [hooks](../hooks/README.md)
and [compaction](../compaction/README.md) subsystem documentation.

## Requests, streaming, and retries

Without streaming support, the loop calls
`model_adapter.respond(messages=..., tool_definitions=...)`. When the adapter's
`supports_streaming()` reports support, it calls `respond_streaming(...)`,
forwarding text, reasoning, tool-preparation callbacks, and `should_cancel`.
`StreamCancelled` becomes loop cancellation.

Transport streaming and presentation are separate: `stream_assistant_text`
controls publication of assistant deltas, reasoning deltas, and tool-preparing
events, not the choice to use supported streaming transport. The completed
reply is still presented through Display and completed-message events.

The request gets an initial attempt plus up to
`model_request_max_retries` additional attempts: three attempts by default.
Only `ModelRequestError` failures classified as transient by
`model_request_error_details` are retried. A transient classification alone is
insufficient for a different exception type. Nontransient errors, unknown
failures, and exhausted retries propagate to run cleanup.

Retry waits use
`min(model_request_retry_backoff_ms * 2 ** (attempt - 1), 10_000)` milliseconds
after a failed attempt: 250 ms then 500 ms with the defaults. During a nonzero
wait, cancellation is checked at intervals of at most 50 ms. Retries reuse
the assembled request view; they do not rerun model-turn hooks or compaction.
Lifecycle evidence includes `model_request_started`, `model_request_failed`,
`model_request_retry_scheduled`, `model_request_retry_started`, and, on success,
`model_request_finished`. Not all provider failures are retryable.

## Reply validation and final acceptance

The loop may persist `reply["raw_reply"]` diagnostically before validation and
calls `display.debug_reply`. With the current ModelAdapter, this historical
`raw_reply` key holds normalized provider diagnostics, not a raw transport
payload. `agent.validate_reply(reply)` owns the validity policy; the generic
Agent rejects malformed tool calls and empty final replies.

Invalid replies are not added to normal assistant transcript history. The loop
records rejection diagnostics when storage is present, presents/emits the
rejection, and supplies `agent.correction_message_for(reason)` temporarily on
the next request. Valid replies reset the invalid streak. At
`MAX_CONSECUTIVE_INVALID_REPLIES = 3`, the loop records the Agent-supplied
`failure_message_for_invalid_replies(reason, count)` and returns a failed run.
That generated failure message is distinct from the rejected model reply.

A valid reply without actions passes through `before_accept_final`. A hook
block keeps that final out of accepted history and supplies temporary user-role
guidance for another turn. The guidance falls back from hook guidance to hook
reason, then to a runtime continuation instruction.

Blocked finals have a separate streak and guard:
`MAX_CONSECUTIVE_BLOCKED_FINALS = 3`. Reaching it fails the run. At least one
successful result in a tool batch resets this streak; blocked, denied, or
unsuccessful tool results alone do not. A hook's `report_note` is appended to
accepted final content. The loop presents the accepted final, emits
`final_message`, tracks that content as the finishing hook's `final_message`,
and records the assistant message. Completion still depends on
`Agent.should_stop(session)`; the generic Agent stops after that accepted final.
The success-path `run_finished` hook runs before terminal success status and
the complete result. See [hooks](../hooks/README.md) for hook semantics.

## Tool-turn commit and per-action pipeline

A valid assistant tool-call message is recorded before any tool execution.
`pending_tool_actions` retains the full emitted batch and
`pending_tool_results` retains its completed prefix. Repair tracking is
established before the assistant persistence call so failures around that
boundary can also be handled.

The intended transcript invariant is that, once assistant tool-call IDs are
committed, cleanup tries to provide a matching tool-result message for every
call. Synthetic interruption/failure results close missing calls. This is
repair ownership, not an atomic transaction across storage, memory, and tools.

For each action, the actual pipeline is:

1. Check the step limit and cancellation, then consume one tool step on entry.
   `before_tool_execute` receives the count before this increment.
2. Run `before_tool_execute`, allowing action replacement or a hook block.
   The supplied HookRunner preserves the original tool-call ID on replacement.
3. If blocked, construct an unsuccessful `blocked_by_hook` result, bypassing
   preflight, approval, and execution. Otherwise call `ActionPreflight.check`,
   including any hook-provided approval reason.
4. Call `ApprovalPolicy.check` on the preflight approval action. On approval,
   present/publish action start and execute the preflight execution action.
   On denial, construct `denied_result` and collect any denial instruction.
5. Run `tool_result` hooks on execution, hook-block, or denial results. Their
   updated output becomes the result used for presentation, transcript, and
   success checks; their additional context becomes transient guidance.
6. Present the result, emit `tool_finished`, accumulate it, and stop attempting
   later actions if the updated result is unsuccessful (`ok` is false/missing).

Blocked and denied actions consume steps because they entered this pipeline.
Synthetic skipped siblings do not enter it and do not receive `tool_result`
hooks. See [hooks](../hooks/README.md), [safety](../safety/README.md), and
[tools](../tools/README.md) for the collaborators' detailed contracts.

## Batch execution, denial instructions, and limits

One model response can emit multiple actions. They execute sequentially until
an unsuccessful result or the step limit stops the batch. Untouched siblings
receive unsuccessful synthetic results with `skipped=True`, identifying an
earlier failure or the exhausted budget. The accepted assistant transcript
message retains the complete emitted tool-call batch. All result messages are
recorded in original action order using `ToolResults` and their call IDs.

Later actions cannot consume earlier results through a new model decision
within the same response: the model has already emitted all sibling actions.
Tool-result hook guidance is collected across attempted actions, with exact
duplicate nonblank guidance strings deduplicated within the batch. Joined
guidance becomes a temporary user-role instruction for the next model turn.

`ApprovalDecision.instruction` may accompany a denial. Nonblank instructions
are recorded as user-role messages **after** all batch tool-result messages.
Unlike transient hook guidance, these are durable conversation history. Denial
itself initially produces a failed tool result, which still passes through
result hooks. Approval details remain in [safety](../safety/README.md).

`max_tool_steps=None` leaves the loop unbounded. A configured budget counts
each action entering the pipeline, including blocks and denials. The limit
can stop before a model turn or midway through a committed batch; remaining
calls in that batch receive skipped results. Hitting the limit returns
`RunResult(status="failed", reason="reached tool step limit: ...")`.
It limits attempted actions, not model requests or elapsed time.

## Cancellation, failure, and unwinding

Cancellation is cooperative. The loop checks `should_cancel` at major
boundaries around startup, turns, compaction, display, approval, execution,
and result recording, and during retry waits. Supported streaming transport
receives the callback. The loop does not forcibly interrupt synchronous
collaborator work; collaborators must provide their own responsiveness.

Normal requested cancellation, including streaming `StreamCancelled`, repairs
pending calls, records interruption evidence, updates status, invokes
`run_finished`, and returns
`RunResult(status="interrupted", reason="cancelled")` with finishing-hook
requests and notes. Errors in that cancellation cleanup can themselves
propagate: this path does not suppress cleanup errors.

For pending batches, repair preserves completed-prefix results. The first
missing result says a tool action failed/interrupted before a result was
available; later missing actions say the run/batch ended before they were
attempted and carry `skipped=True`. Repair formats and records the whole batch
in order. This does not imply that a tool with no captured result had no side
effects.

Controlled failures return failed results after lifecycle/status handling and
finishing hooks. Ordinary `Exception` paths invoke incomplete-run repair,
attempt `run_failed` evidence and failed status, invoke `run_finished`, and
re-raise the original exception. `KeyboardInterrupt` and other `BaseException`
paths do the corresponding interrupted cleanup and re-raise.

While preserving an original exception, cleanup operations are attempted
individually and their ordinary `Exception` failures are suppressed. Finishing
hook failure then falls back to an empty hook response. This suppression does
not catch a new `BaseException` raised by cleanup. On the normal success path,
an ordinary exception from `run_finished` instead produces a controlled failed
result with a `run_finished hook failed: ...` reason.

Repair is best effort. Message appends happen individually, and the repair
helper does not inspect already recorded tool-result messages before recording
the tracked batch. Partial persistence failures can therefore leave incomplete
or repeated evidence; there is no rollback or exactly-once batch guarantee.

## Persistence ordering

The current implementation preserves these local ordering boundaries:

- Accepted messages: `SessionStore.append_message` returns before
  `Session.add_message` mutates memory, including initial and tool-result
  messages. The assistant usage value is passed when recording its reply.
- System reconciliation: its durable event is appended before in-memory
  replacement of system messages.
- Runtime lifecycle facts: `_emit_runtime_event` calls a callable
  `SessionStore.append_runtime_event`, when available, before publishing to
  `on_event`.
- Compaction: persistence and post-apply callbacks are separate. The current
  Compactor persists its applied replay event before replacing the active
  transcript; only afterward does the loop notify `compaction_applied` hooks
  and observers.

These local sequences do not form a transaction spanning the entire run.
Storage diagnostics are separate from accepted provider-style transcript
history. See [storage](../storage/README.md) and
[compaction](../compaction/README.md).

## Events and Display

`on_event` is a compact machine-facing observer seam for wrappers and UIs.
Events include `session_id` from the store when available (otherwise `None`);
the callback helper copies events, supplies a missing UTC timestamp, and
suppresses ordinary callback exceptions. `Display` is the separate human-facing
presentation collaborator. Core requires no particular UI, and observers need
not scrape Display output.

Major event families include:

- `run_started` / `run_finished`, plus failure/interruption lifecycle evidence.
- Model request started, failed, retry scheduled/started, and finished.
- Assistant message starts/deltas, completed narration/final messages, and
  reasoning deltas/completed reasoning.
- `tool_preparing`, `tool_started`, and `tool_finished`.
- `reply_rejected` and hook diagnostics.
- Compaction boundary notifications: started, failed, and applied.

Action event helpers project useful fields such as `tool`, `tool_call_id`,
`path`, and `command`, rather than entire argument/result payloads.
The intended observer contract is compact runtime facts, without raw file
contents, provider payloads, prompts, credentials, or headers. It is not a
universal sanitization boundary: completed assistant text/reasoning and command
strings are forwarded, and request-error diagnostics can include bounded,
credential-redacted response excerpts and selected metadata. Hosts should not
assume that every event is content-free or fully scrubbed.

Durable runtime events and observer events are related but not identical.
Request lifecycle, failure/interruption, and hook evidence use runtime-event
persistence; ordinary message/tool and run-start/finish observation uses
`_emit_event`. Compaction persists its complete lifecycle through a separate
callback, while observers see selected boundaries. Diagnostic persistence
errors are suppressed in hook-event and original-error-preservation paths,
not in every runtime event path.

## Agent, adapter, and host responsibilities

AgentLoop owns orchestration and state transitions. Agent owns run-facing model
behavior through `initial_messages()`, `tool_definitions()`,
`validate_reply(reply)`, `correction_message_for(reason)`,
`failure_message_for_invalid_replies(reason, count)`, and
`should_stop(session)`. They are distinct collaborators, not interchangeable
names for the same subsystem.

AgentLoop asks ModelAdapter for provider-neutral replies. The adapter prepares
requests and normalizes responses/tool calls; provider-specific transport sits
below it. The loop owns the retry and orchestration policy around requests.

Core owns the generic state machine, lifecycle ordering, retry orchestration,
tool pipeline, cancellation handling, transcript repair, and coordination of
hooks, safety, compaction, and trimming. The host owns collaborator construction,
provider/model choice, concrete Display, approval prompting, storage locations,
hook selection, application policy defaults, event consumption, and optional
limits/settings.

`run_finished` hooks may return follow-up requests and notes. The loop exposes
the requests in `RunResult` and filters out blank/non-string notes, preserving
the accepted note strings. Core returns follow-up requests to the host; it
does not automatically execute them. The accepted final content is passed to
the finishing hook, rather than being a field on `RunResult`.
