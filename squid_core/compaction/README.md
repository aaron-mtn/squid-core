# Squid Core compaction

Compaction replaces selected older context in the **effective model transcript**
with a model-generated continuation summary. It is optional and
settings-controlled. With SessionStore persistence, original messages remain in
the append-only history; compacting model context does not delete session history.
See the [architecture overview](../../docs/ARCHITECTURE.md).

The package exports `Compactor`, `COMPACTION_SYSTEM_PROMPT`, and
`compaction_prompt`. Token helpers live in `token_estimate.py`; reporting helpers
live in `compaction_status.py`.

## Trigger and runtime placement

`Compactor.maybe_compact(session, *, event_callback=None,
post_apply_callback=None)` reads `session.messages` and the supplied settings:

| Setting | Meaning | `CompactionSettings` default |
| --- | --- | --- |
| `enabled` | Whether to check and perform compaction | `True` |
| `trigger_input_tokens` | Rough transcript estimate at which compaction can run | `25_000` |
| `compact_fraction` | Target share of unprotected history to summarize | `0.80` |
| `min_recent_chunks` | Minimum recent chunk tail excluded from selection | `2` |

`CompactionSettings` in `squid_core.settings` validates a boolean enable flag, a
positive integer trigger, a fraction greater than zero and at most one, and a
nonnegative integer recent-tail count. Boolean values are rejected for the numeric
settings. The host chooses the effective values.

When disabled, `maybe_compact` immediately returns `None` without emitting a
check event. When enabled, it estimates the entire current effective transcript,
splits the protected prefix, and computes chunk selection. The threshold test is
`estimated_tokens_before >= trigger_input_tokens`: equality triggers eligibility.
It emits `compaction_check` with both `threshold_crossed` and `will_compact`.
Below the threshold, or at/above it with no selectable chunks, it returns `None`
without requesting a summary or replacing the transcript. A retained tail can
leave no selectable chunks even when unprotected history exists.

AgentLoop checks compaction at the beginning of each continuing loop iteration,
after cancellation and tool-step-limit checks and before the `before_model_turn`
hook and normal model request. It only does so when a compactor is installed.
The estimate covers `session.messages`, not the later transient turn context,
tool definitions, or the provider's finalized request. `build_agent_loop` creates
a default Compactor from `settings.compaction` and the normal model adapter when
no custom compactor is supplied, forwarding the optional
`compaction_context_provider` and `should_cancel` callbacks.

## Rough token estimates and context status

`estimate_text_tokens` returns zero for empty text and otherwise
`max(1, len(text) // 4)`. `estimate_message_tokens` first JSON-serializes each
message using `json.dumps(message, ensure_ascii=False)`, including its fields and
JSON overhead. `estimate_messages_tokens` sums those individual estimates.
This is a rough internal character-based estimate, not provider tokenizer
accounting or an exact input-token count.

`build_context_status(messages, compaction_settings)` uses the same estimator
for reporting/UI surfaces and returns a frozen `ContextStatus` with `to_dict()`:

| Field | Reported value |
| --- | --- |
| `estimated_tokens` | Rough estimate of the supplied messages |
| `limit_tokens` | Trigger when enabled and valid; otherwise `None` |
| `percent` | Rounded estimate / trigger × 100 when enabled and valid; otherwise `None` |
| `label` | Formatted `usage / trigger`, or usage alone without a valid enabled trigger |
| `compaction_enabled` | Boolean interpretation of the settings' `enabled` value |
| `trigger_input_tokens` | Supplied trigger, or `None` when absent |
| `compact_fraction` | Supplied fraction, or `None` when absent |
| `min_recent_chunks` | Supplied recent-tail count, or `None` when absent |

For status reporting, a valid trigger is a positive integer excluding booleans.
The percent is not capped at 100. The limit describes progress toward the
compaction trigger, not a provider context-window limit. With compaction disabled
or an invalid/missing trigger, only estimated usage is reported in the label;
the settings fields are still surfaced. `format_token_count` displays counts
below 1,000 as integers and larger counts in `k` units, using at most one decimal
place and removing trailing zeroes.

## Protected prefix and chunk selection

Protection applies only to the initial prefix:

1. Protect the first message if its role is `system`.
2. Protect the message immediately after that prefix if its role is `user`.

If there is no initial system message, a leading user message is still protected.
No scan protects additional system/user messages elsewhere. These protected
messages remain verbatim in the effective transcript. The summarizer receives
them separately as authoritative reference context, not history to compress.

Remaining messages are divided into ordered chunks. Ordinarily each message is
one chunk. An assistant message with truthy `tool_calls` starts a chunk that also
includes every immediately following `tool`-role message. Grouping stops at the
first non-tool message or the end of history. It does not match call IDs or count
expected results. Selecting whole chunks preserves these contiguous call/result
groups instead of splitting them.

Selection estimates each chunk by summing its message estimates:

```text
eligible_token_estimate = sum(all unprotected chunk estimates)
target_compact_token_estimate = ceil(eligible_token_estimate * compact_fraction)
maximum selectable chunks = max(0, chunk count - min_recent_chunks)
```

The eligible estimate includes the recent tail, even though that tail cannot be
selected. The fraction applies to unprotected history, not the full transcript
including the protected prefix. The oldest selectable chunks are accumulated
first, stopping as soon as their token weight reaches/exceeds the target or all
selectable chunks are exhausted. Whole-chunk selection can overshoot the target;
the recent-tail constraint can prevent reaching it. All unselected chunks remain
in order as the preserved recent suffix. There is no subsequent loop that
compacts until a particular post-compaction token count is reached, nor a check
requiring the summary to reduce the estimate.

## Summary request and authoritative host context

Compaction makes a separate text request through
`model_adapter.complete_text(request_messages)`, with `should_cancel` forwarded
as a keyword when supplied. The request has a dedicated compaction system prompt
and one user prompt. Selected chunks are serialized as indented JSON history;
protected system and user messages are serialized into separate reference
sections. The recent tail itself is not included as history in this request.

The prompts ask for continuation state: task context, findings, exact paths and
identifiers, actions actually taken, tool results, verification, corrections,
next steps, and concrete unresolved risks or unknowns. They prohibit invented or
reinterpreted requirements and speculative risks. Later events supersede earlier
ones: later failed verification supersedes earlier success, and later mutations
make earlier verification stale. The requested output is only the summary body,
without a title or top-level heading. These are model instructions, not semantic
checks enforced by Core.

An optional `context_provider(preserved_messages)` lets the host supply
deterministic/authoritative runtime facts without teaching Core application
semantics. It receives the flattened preserved recent messages, is called once
per compaction, and its stripped string result is reused for both attempts and
the final compacted message. Exceptions, non-string results, and empty/whitespace
strings produce no extra context and do not abort compaction.

When nonempty, this string appears separately in the request as authoritative
host/runtime facts that the summary must not contradict. It is kept separate
from model-generated history so host facts need not depend on the summarizer's
recollection. The prompt describes these facts as state at the end of the history
being summarized; the provider nevertheless receives the retained recent tail.
Core neither derives nor validates that cutoff relationship. The host is
responsible for the supplied facts and their intended scope.

## Validation, retry, and inserted context

The adapter response must be a dictionary whose `content` is a string containing
non-whitespace text. Validation is structural: Core does not judge semantic
summary quality. It retains valid content as returned, without stripping it.

Invalid output gets exactly one corrective retry. Before retrying, Core emits
`compaction_failed` for attempt 1 with `stage="response_parse"` and
`will_retry=True`, then another `compaction_started` and `compaction_request` for
attempt 2. The second user prompt adds guidance to return a nonempty summary;
it uses the same selected history and host context. A second invalid response
raises `ValueError`, emits a final failure with attempt 2 and `will_retry=False`,
and leaves the transcript unreplaced. Provider/request, persistence, and rebuild
errors do not receive this validation retry.

On valid output, the replacement message has role `assistant`. Its content is
`COMPACTED_CONTEXT_PREAMBLE`, a newline, and the summary. Nonempty host context
is appended after the summary, separated by two newlines. The preamble identifies
the summary as lossy model-generated context, not a user instruction, and says
it cannot add, replace, or override requirements from real user messages. The
effective transcript becomes:

```text
protected prefix + compacted assistant message + preserved recent messages
```

## Apply, commit, and durable replay

Successful application follows this order:

1. Construct the proposed effective transcript and before/after metadata.
2. Prepare `compaction_applied`, including a replay payload with version `1`,
   protected/preserved message counts, and the exact compacted assistant message.
3. Invoke `event_callback` with that event **before** replacing
   `session.messages`.
4. Assign the proposed transcript to `session.messages`.
5. Invoke `post_apply_callback` with the applied event, after the new transcript
   is active, and return the metadata.

With persistence wired in, the persisted replay boundary is the design's commit
point. The callback owns persistence; Compactor itself does not write storage.
Without an event callback there is no durable commit guarantee. A failure before
assignment leaves the active transcript unreplaced; a post-apply callback failure
propagates after replacement and does not roll back the committed transcript.

AgentLoop forwards lifecycle events to
`session_store.append_compaction_event` when a store is present. It publishes
started/failed notifications through its runtime observer, and notifies applied
hooks and the observer only through the post-apply path.

SessionStore retains original messages in append-only history and persists the
small applied replay transform rather than duplicating replaced history.
`load_messages()` can reconstruct the effective compacted transcript after
restart by applying valid boundaries in sequence. Invalid/inconsistent or
unsupported replay payloads leave the current replay history intact.
`load_canonical_messages()` ignores compaction transforms and retains the
original message sequence subject to its normal system reconciliation and
trailing-tool-call repair behavior. Session-log appends and metadata updates are
separate writes; this ordering does not promise a filesystem transaction or
rollback after a partial persistence failure. See [storage](../storage/README.md)
for replay validation and durability details.

## Lifecycle diagnostics and failures

Events are delivered synchronously to `event_callback` when supplied:

| Event | When and diagnostic fields |
| --- | --- |
| `compaction_check` | Every enabled check: current estimate, trigger, threshold/result flags, and selection details |
| `compaction_started` | Each attempt: attempt number, original token/message counts, selection details; attempt 2 includes `retry=True` |
| `compaction_request` | Before each summary request: attempt and estimated request tokens; `provider`/`model` when adapter `provider_name`/`model_name` are available |
| `compaction_reasoning` | After a valid response, only when its `reasoning` is truthy: attempt and reasoning content |
| `compaction_summary` | Valid summary text exactly as returned, with attempt, before transcript replacement |
| `compaction_response_meta` | When available: attempt, non-`None` `finish_reason`, and non-`None` entries of a dictionary `usage` |
| `compaction_applied` | Before assignment: attempt, before/after token and message counts, summary character count, and replay payload |
| `compaction_failed` | Failure stage, exception type/text; validation failures also identify attempt and whether retry will occur |

Selection diagnostics include `eligible_token_estimate`,
`target_compact_token_estimate`, `selected_compact_token_estimate`,
`selected_chunks`, `preserved_chunks`, `compact_fraction`, and
`min_recent_chunks`. Summary and optional reasoning are emitted only after
structural validation succeeds. Response metadata is diagnostic; reported
provider usage does not replace the rough estimator used for selection.

`StreamCancelled` propagates as cancellation/run control without an ordinary
compaction failure event. Other exceptions propagate after a best-effort
`compaction_failed` event. Stages distinguish `prepare`, `persistence`,
`model_request`, `response_parse`, `session_rebuild`, and `post_commit`. If
failure-event delivery also fails, the original exception remains primary.
Event-callback failures abort further compaction work; callbacks are not generally
best-effort observers. A callback can already have persisted data before raising.
Host context-provider errors are the explicit nonfatal exception to this behavior.

## Host responsibilities and direct use

Core owns token estimation, chunking/selection, request construction, structural
validation and corrective retry, effective transcript rebuilding, and lifecycle
event generation. The embedding application supplies settings, a model adapter,
optional authoritative context, persistence callbacks, post-apply observers, and
presentation of ContextStatus. No Code Squid-specific policy is required.

For an existing Session and adapter implementing `complete_text`, direct use is:

```python
from squid_core.compaction import Compactor
from squid_core.settings import CompactionSettings

# The host supplies session, model_adapter, and session_store.
settings = CompactionSettings(
    enabled=True,
    trigger_input_tokens=25_000,
    compact_fraction=0.80,
    min_recent_chunks=2,
)
compactor = Compactor(settings=settings, model_adapter=model_adapter)
metadata = compactor.maybe_compact(
    session,
    event_callback=session_store.append_compaction_event,
)
```

The event callback is optional for in-memory use. `metadata` is `None` when no
compaction occurs; on success it contains the before/after token and message
counts and `summary_chars`. Failures propagate as described above.
