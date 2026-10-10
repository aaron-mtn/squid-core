# Hooks

A Squid Core hook is an in-process Python object with methods named for runtime
hook points. Implement only the methods you need; each receives one context
object and returns `HookResponse` or `None`. Notification methods normally
return `None`. No base class or decorator is required.

Core owns this generic mechanism. It does not install Code Squid-style behavioral
nudges itself; host applications may supply concrete hooks implementing their
own policy or guidance. Types and loading helpers are exported by
`squid_core.hooks`.

## Lifecycle and contexts

The following describes `AgentLoop` integration, rather than direct calls to
`HookRunner`. Contexts are frozen dataclasses, but dictionaries inside them are
not deep-copied or immutable. Use response fields to request changes rather than
mutating shared payloads.

Most contexts carry `session_id` (or `None` without a session store) and
`workspace_root` (a string). The exceptions and remaining fields are listed below.

| Method / context | When it runs and context fields beyond the common fields |
| --- | --- |
| `before_run(BeforeRunContext)` | After initial transcript seeding/system-prompt reconciliation and tool-definition construction, before the main loop. Receives `task`, `skill_name`, and `allowed_tools` (tuple of advertised tool names). |
| `before_model_turn(BeforeModelTurnContext)` | After that iteration's optional compaction, before request rendering and the main model request. Receives `task`, `messages` (tuple of current effective session-message dictionaries), and `tool_steps_used`. This excludes transient request additions and request-only trimming. |
| `before_tool_execute(BeforeToolExecuteContext)` | For each action entering the execution pipeline, before preflight, approval, and execution. Receives `action` and `tool_steps_used` (attempt count before this action). Blocked/denied attempts also consume a step. |
| `tool_result(ToolResultContext)` | After execution, or after a hook block/approval denial produces a result, before display and transcript result recording. Receives `action` and `result`. Untouched actions skipped after an earlier failure or step limit do not pass through this method. |
| `before_accept_final(BeforeAcceptFinalContext)` | For a valid reply with no tool actions, before final display and accepted-message recording. Receives `message`, `content`, sorted `files_read`/`files_written` tuples, and `tool_steps_used`. |
| `run_finished(RunFinishedContext)` | At completion, explicit run failure, cancellation, or exception cleanup. Receives `status` (`complete`, `failed`, or `interrupted`), `reason`, `final_message`, sorted file tuples, and `tool_steps_used`. `final_message` is supplied on the normal completion path. |
| `compaction_applied(CompactionAppliedContext)` | Notification after the compaction persistence callback succeeds and the compacted transcript replaces the active session messages. Contains only the common fields. |
| `read_result_trimmed(ReadResultTrimmedContext)` | Notification during main-request rendering when an older `read_file` result is trimmed. Has **no common fields**: receives `tool_call_id`, `path` (string or `None`), and `retained_line_ranges` (tuple of source-line start/end pairs). |

`before_model_turn` and trimming notifications are not rerun for internal retries
of the same model request. Compaction's own model requests do not use
`before_model_turn`. Trimming is a request-only view of canonical results; the
same old read can produce another notification on a later turn. These
notifications let stateful hooks track which source remains visible.

## Responses and effects

`HookResponse` is a frozen dataclass with empty/false defaults. Effects are
specific to the hook point; other fields are ignored by that runner method.

| Hook point | Supported response fields and effect |
| --- | --- |
| `before_run` | `context_blocks` apply throughout this run. `block` stops the run as failed; `reason`, then `guidance`, supplies its failure explanation. |
| `before_model_turn` | `context_blocks` apply to this turn only. A returned `block` does not block the model request. |
| `before_tool_execute` | `action` replaces the current action, preserving its original `tool_call_id` when present. `approval_reason` adds a reason to preflight/approval processing, subject to the host's approval policy. `block` prevents execution and produces an unsuccessful `blocked_by_hook` result with `reason`/`guidance`. |
| `tool_result` | `updated_tool_output` replaces the result dictionary; `additional_context` supplies transient guidance for the next model turn. This changes reported output, not the already-performed tool side effects. |
| `before_accept_final` | `block` rejects the final and supplies `guidance`, falling back to `reason` or a default instruction, for another model turn. Otherwise `report_note` is appended to the accepted final text. |
| `run_finished` | `follow_up_requests` and `notes` are collected into `RunResult.follow_up_requests` and `RunResult.hook_notes`; the loop filters out blank/non-string notes. |
| Both notification methods | Return values are not applied. |

`PromptContextBlock(title, body, source="")` is part of the public surface.
Run blocks precede turn blocks in a temporary user-role message headed
`# Additional Context For This Turn`. Each nonempty stripped body appears under
its title (blank titles become `Context`), with an optional source label.

Context-block messages, `additional_context`, and rejected-final guidance are
not added to canonical session history. Run blocks are held in memory for the
run; turn blocks and pending guidance accompany the next request, including
its retries. Tool-result guidance is combined across a batch with exact-string
deduplication. Rejected finals are not accepted transcript messages; raw model
replies can still be logged by a configured session store.

Updated tool outputs and accepted final text with report notes **are** recorded
in session history and durable message storage when configured. Action
replacement affects execution and downstream result hooks; it does not rewrite
the model's already-recorded assistant tool-call message. Replacements must
remain valid action/result dictionaries: there is no hook-specific schema
validation or automatic adapter revalidation of replacement actions.

The loop fails after three consecutive hook-rejected finals. A successful tool
action resets that streak; a blocked, denied, or unsuccessful action does not.
Final hooks run before final display/recording, but streamed text may already
have been observed. `run_finished` cannot veto or rewrite an accepted final.

`FollowUpRunRequest(task, reason="", context_blocks=(),
require_user_approval=True)` describes proposed further work. Core returns these
requests to the host; it does not automatically execute or approve them. Notes
likewise belong to the returned result, not model-visible transcript messages.

## Supplying and ordering hooks

`HookRunner` invokes available methods synchronously in supplied list order.
It does not await coroutine handlers; use ordinary `def` methods. Missing
methods and `None` responses are skipped. There is no priority or deduplication
mechanism. A block short-circuits the remaining hooks at `before_run`,
`before_tool_execute`, or `before_accept_final`.

Context blocks, notes, and follow-up requests concatenate in order. Approval
reasons join with `; `; tool-result additional context and final report notes
join with blank lines. Action replacements and output replacements feed into
subsequent hooks at the same point, so later replacements take precedence.

Pass a runner to `AgentLoop(..., hook_runner=runner)` or to the run-construction
helpers `build_agent_run` / `build_agent_loop` in `squid_core.spawner`. Omitting
it constructs an empty runner; Core does not automatically discover host hooks.

```python
from squid_core.hooks import HookResponse, HookRunner, PromptContextBlock


class WorkspaceHint:
    def before_run(self, ctx):
        return HookResponse(context_blocks=(
            PromptContextBlock("Workspace", ctx.workspace_root, source="host"),
        ))


runner = HookRunner([WorkspaceHint()])
# Pass hook_runner=runner when constructing the host's Core run.
```

Saved user hooks can be loaded with `load_user_hooks(hooks_dir=...)` or
`load_user_hooks(app_home=...)` (using `<app_home>/hooks`). Without either path,
loading returns an empty list. Each hook directory contains `hook.yaml` and
`hook.py`; metadata supplies `name`, optional `description`, `enabled` (default
false), and `version` (default 1). The metadata reader handles simple scalar
`key: value` lines, not general YAML. Enabled hooks expose a callable
`create_hook()` or `create_hook(config=...)`; the loader passes metadata when
the factory accepts `config` or keyword arguments. User directories sort by
`(name.lower(), name)`; `load_hook_infos` preserves supplied metadata order while
skipping disabled entries. Metadata/import/factory failures use `HookLoadError`.

Mods may package hooks. The Mod runtime selects metadata and loads the declared
hooks into `ResolvedMod.hooks` in manifest order, enabling explicitly selected
hooks even if their saved metadata says disabled. Packaged hooks take precedence
over same-named loose hooks. The host supplies this resolved set via, for example,
`HookRunner(list(resolved_mod.hooks))`.

## Errors and diagnostics

`HookRunner` reports invocation failures and re-raises them; it does not isolate
a failing hook and continue. Handlers must return `HookResponse` or `None`;
malformed responses may fail during diagnostic inspection or effect collection.
In `AgentLoop`, ordinary hook exceptions generally fail the run, repair pending
tool-call results, invoke finish hooks during cleanup, and propagate the original
exception. An ordinary `run_finished` exception on normal completion instead
returns a failed `RunResult`. During exception unwinding, ordinary finish-hook
errors are suppressed to preserve the original error. Cancellation and
`KeyboardInterrupt` follow the loop's interruption handling.

Runner methods accept an optional `event_sink`. Each invoked handler emits a
`hook_execution` diagnostic with its class name, seam, status
(`effect`, `no_effect`, or `error`), effect categories, and an error type on
failure, without copying returned payloads. Categories describe returned fields,
even when that seam ignores them. Ordinary sink exceptions are suppressed.
The loop also emits `hooks_configured` with hook class names, forwards these
diagnostics to runtime observers, and persists them when storage is configured.
Compaction also exposes a separate runtime `compaction_applied` event; the hook
receives its small context, not that event's full payload.

See [the architecture documentation](../../docs/ARCHITECTURE.md) for ownership
and the wider runtime and Mods system.
