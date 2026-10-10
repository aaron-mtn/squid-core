# Squid Core storage

Squid Core uses explicit filesystem persistence for sessions, transcript events,
finalized model requests, session image assets, a latest-only undo buffer, and
diagnostic tracebacks. It does not require SQLite or another database. The host
supplies storage locations; Core supplies serialization, replay, and file-write
mechanics. See the [architecture overview](../../docs/ARCHITECTURE.md).

`squid_core.storage` exports `SessionStore`, `UndoStore`, and `write_error_log`.
Discovery helpers live in `squid_core.storage.session_index`, JSONL loading in
`squid_core.storage.session_jsonl`, and atomic writes in
`squid_core.storage.atomic_json`.

## Session directories and sources of truth

`SessionStore.create(sessions_dir, task, *, workspace_root=None)` creates one
session beneath the caller-selected root:

```text
<sessions_dir>/
└── <session_id>/
    ├── metadata.json
    ├── session.jsonl
    ├── model-view.jsonl
    ├── model-transcript.txt
    └── assets/                       # created when needed
        ├── <sha256>.png              # or .jpg / .webp snapshots
        └── <generated folder>/      # when named images are requested
            ├── .cache-key
            └── <name>.png
```

`metadata.json` stores current session facts. `session.jsonl` is the canonical
event/message log; `model-view.jsonl` is the canonical request-observation log.
Immutable asset bytes support image references in those records.
`model-transcript.txt` is a derived convenience view rebuilt from the two logs,
not a third source of truth. Named generated images and their `.cache-key`
metadata are a generation cache, distinct from immutable snapshots.

Creation initializes metadata, both empty logs, and the readable transcript
title. Session IDs use a UTC `YYYYMMDD-HHMMSS-<task-slug>` name, with a short
random suffix on collision. The lowercase task slug is capped at 50 characters
and falls back to `session` when no usable slug remains.

Initial metadata contains `session_id`, `created_at`, `updated_at`, `task`,
`status` (`active`), and `active_leaf_id` (`null`). Supplying `workspace_root`
adds its resolved absolute path. Timestamps are timezone-aware UTC ISO strings.
The `session_id` property reads the ID from metadata.
`update_status(status)` accepts only `active`, `complete`, `failed`, `abandoned`,
or `interrupted` and updates `updated_at`.

`SessionStore.open(session_dir)` checks that `metadata.json` and `session.jsonl`
exist as files and returns a store without creating or modifying files. It does
not require the model-view log or readable transcript, nor validate all contents
at open time. Each store operates on its explicitly supplied session directory.

## Append-only event spine

Session entries have linear IDs such as `e_000001`, a `parent_id` linking to the
previous active leaf, a `type`, and a UTC `timestamp`. Appending an entry advances
metadata's active leaf and update timestamp. The current implementation is a
linear history, not a branching history store.

| Entry kind | Persisted purpose | Conversation replay |
| --- | --- | --- |
| `message` | Accepted provider-style message, with optional `usage` | Appended in order |
| `model_reply` | Provider-derived assistant reply snapshot, including diagnostic reply information | Ignored |
| `reply_rejection` | Rejection reason/error, invalid-attempt count, linked raw reply ID, and available recoverable tool details | Ignored |
| `run_context` | Skill name, sorted allowed tools and prompt overrides, optional mod ID | Ignored |
| `system_message_reconciliation` | Current system message superseding earlier run specialization | Replaces prior system messages |
| `compaction_*` | Compaction lifecycle evidence and, for applied compaction, a replay boundary | Only valid `compaction_applied` replay transforms history |
| Supported runtime lifecycle events | Hook, request, and terminal run diagnostics | Ignored |

Durable history includes more than model conversation history. Raw replies,
rejections, and lifecycle diagnostics are retained for inspection without being
injected as provider-style messages by replay.

AgentLoop's accepted-message recording writes storage before adding the message
to the in-memory session. Rejected replies bypass that accepted-message path;
the loop records raw reply snapshots and rejection diagnostics separately.
System-message reconciliation is persisted before replacing the in-memory
system context. On terminal failure/interruption, the loop attempts to close
committed tool calls with result messages and record status/lifecycle evidence.

## Replay

`load_messages()` returns the effective provider-style conversation: ordinary
messages, system-message reconciliation, and valid applied compaction transforms.
Each reconciliation replaces all earlier system-role messages with its recorded
message at the front, preserving the order of non-system messages.

`load_canonical_messages()` returns original messages without applying compaction
transforms. It still applies system-message reconciliation and the trailing-call
repair below. For raw events without these interpretations, use
`load_session_entries` or `load_session_jsonl`.

Both message loaders inspect the final replay message. If it is an assistant
message with tool calls, they append a synthetic failure result for each call to
`session.jsonl` and include those results in the return value. The results mark
the action as interrupted and recovered. **Loading messages can therefore write
to the session.** This repairs only a genuinely trailing assistant tool-call
message with no following result. It does not repair missing results in the
middle, or complete a partially recorded batch whose last message is already a
tool result.

## Torn final JSONL append

`load_session_jsonl(path, *, truncate_torn_tail=False)` reads bytes, skips blank
lines, decodes UTF-8, and parses JSON records in append order. A complete valid
JSON record can be loaded even without a final newline. The loader parses JSON;
it does not validate the session event schema.

It tolerates one malformed final nonblank record only when at least one valid
record precedes it. A JSON parse failure at that boundary is treated as a torn
append. A UTF-8 failure is tolerated only for an incomplete code point at the end
of that final record's bytes. Invalid UTF-8 of another kind, malformed records
with later records, and a malformed log without a valid prefix still raise.

Default loading leaves the file untouched and returns the valid prefix.
`truncate_torn_tail=True` truncates at the bad final record's byte offset before
returning. SessionStore uses this mode when assigning the next session entry ID,
so continued append removes the proven torn tail instead of turning it into
middle corruption. This is narrow final-append recovery, not general corruption
repair. The model-view log and session-root registry do not use this recovery
path.

## Model request observations and readable transcript

`append_model_view(request)` records the exact finalized model-visible request
representation supplied to it, plus a timestamp and a separate linear
`request_index`. These records do not participate in the session active-leaf
chain. The append is flushed, without an explicit `fsync`.

The generic run builder wires ModelAdapter's request observer to this method
when both collaborators support that seam. ModelAdapter observes the finalized
request immediately before provider transport, including ordinary and text-only
compaction requests. The builder adds provider/reasoning metadata, message count,
and an estimated token count. This captures the Core provider-bound
representation (including image asset references), not a provider SDK's eventual
wire payload. Observation can also record a request whose transport later fails.
Custom adapters without the observation seam do not automatically produce these
records. ModelAdapter treats observer failures as best-effort diagnostics and
continues transport.

`write_model_transcript` rebuilds `model-transcript.txt` from `session.jsonl` and
`model-view.jsonl`. The readable projection orders observations and events by
timestamp, avoids repeating accumulated request context, includes assistant
replies, and displays context changes and compaction summaries. Summarizer
requests are distinguished from ordinary turns.

SessionStore refreshes this projection after messages, raw model replies, model
views, and selected compaction events. Refresh catches exceptions: a rebuild
failure must not invalidate already written canonical data or an otherwise valid
provider request. The readable file can consequently be stale. Its writer uses
a temporary sibling and replacement, but does not use the fsynced atomic-write
helpers described below.

## Durable compaction boundaries

The compactor emits `compaction_check`, `compaction_started`,
`compaction_request`, `compaction_reasoning`, `compaction_summary`,
`compaction_response_meta`, `compaction_applied`, and `compaction_failed` as
applicable. `append_compaction_event` requires a `compaction_` type prefix and
stores lifecycle fields at the entry's top level.

An applied event includes `messages_before`, `messages_after`, and a `replay`
object with version `1`, `protected_message_count`, `preserved_message_count`,
and the exact inserted assistant `compacted_message`. The compactor persists
this boundary before installing the transformed in-memory conversation and
notifying post-apply observers. Original messages remain in the append-only log.

`load_messages()` applies a boundary only when its version, integer counts
(excluding booleans), current effective-history length, count bounds, output
count, and assistant message/string content are consistent. It keeps the
protected prefix, inserts the recorded compacted message, and keeps the preserved
suffix. Malformed or inconsistent payloads fail closed by retaining the current
history rather than discarding messages. Older applied events without a supported
replay payload retain historical behavior: they do not transform replay.
`load_canonical_messages()` ignores these transforms entirely.

## Runtime diagnostics

`append_runtime_event` accepts only these currently supported types:

- `hooks_configured`, `hook_execution`
- `model_request_started`, `model_request_retry_scheduled`,
  `model_request_retry_started`, `model_request_finished`, `model_request_failed`
- `run_failed`, `run_interrupted`

They use the same linked event spine, but both message loaders ignore them.
AgentLoop persists lifecycle facts before publishing them to optional observers;
hook diagnostics and terminal cleanup have best-effort paths to avoid masking
another failure. This storage method is a restricted diagnostic sink, not an
open-ended event bus.

## Session assets

`snapshot_asset(data, extension)` stores exact bytes under their SHA-256 hex digest
plus extension. Supported snapshot extensions are `.png`, `.jpg`, and `.webp`.
It requires bytes and a supported extension; it does not itself decode the image.
An existing snapshot is verified and reused rather than rewritten.

`load_asset(asset_id)` accepts only the fixed lowercase digest/extension format,
reads the bytes, and verifies their SHA-256 digest. Missing or mismatched bytes
raise. Resolved paths, including symlinks, must keep `assets/` inside the session
and the asset inside `assets/`.

`ensure_generated_image(folder_name, filename, cache_key, generate,
folder_identity=None)` lazily materializes a named PNG beneath `assets/` and
returns `(path_string, cache_hit)`. Names must be single safe path components;
the filename must end in `.png`. Cached and generated bytes are inspected as PNG
images. Folder `.cache-key` JSON records a version, optional folder identity, and
per-image generation keys. A valid image with a matching key is reused;
otherwise it is generated and atomically replaced, followed by the cache
metadata. With a folder identity, unknown or differently owned folders are left
untouched and a numbered folder is selected. Cache metadata is validated
strictly; unreadable or inconsistent metadata is not universally repaired.
Resolved generated paths must remain contained within the session asset tree.

## Explicit session discovery

The helpers in `session_index` operate only on supplied roots and registry paths:

| Helper | Behavior |
| --- | --- |
| `register_session_root(index_path, sessions_dir)` | Appends a resolved root to a caller-selected JSONL registry unless already registered |
| `list_registered_session_roots(index_path)` | Returns unique recorded roots; a missing registry yields an empty list, malformed JSON lines and invalid root fields are skipped |
| `list_all_sessions(index_path)` | Combines registered roots and sorts newest first by metadata `updated_at` |
| `list_sessions(sessions_dir)` | Reads metadata objects from immediate child directories, skipping missing metadata, JSON parse failures, and filesystem read errors; sorts by `updated_at` descending |
| `latest_session_id(sessions_dir)` | Returns the newest listed metadata's session ID, or `None` |
| `session_dir_for(sessions_dir, session_id)` | Validates an exact ID, checks resolved containment and directory existence, then returns its path |
| `load_session_entries(sessions_dir, session_id)` | Resolves a validated session and loads raw `session.jsonl` records in append order |

Roots are explicit, not globally auto-discovered. `list_all_sessions` skips roots
raising `OSError`, so a stale or temporarily unreadable root does not prevent
discovery from others. This does not promise recovery from every registry or
metadata error. `session_dir_for` accepts IDs matching
`[A-Za-z0-9][A-Za-z0-9_-]*` before constructing a session path and rejects resolved
escapes. Listing metadata is not a full session-integrity check.

## Atomic writes and durability limits

`atomic_write_json` and `atomic_write_bytes` write through a unique temporary file
in the destination's directory, flush and `fsync` it, then use `os.replace`.
Replacement retries only `PermissionError`, with short delays of 20, 40, and
80 milliseconds before one final attempt, accommodating transient Windows file
locks. On failure, temporary-file cleanup is attempted without masking the
original exception.

Session metadata, undo metadata/index, image snapshots, and generated-image/cache
state use these helpers. The root registry is an ordinary JSONL append; it is not
an atomically rewritten index. Session JSONL appends and their subsequent metadata
updates are separate writes, not a transaction. These logs are not a general
database, and the helpers do not make the full multi-file operation transactional.

## Latest-only undo

`UndoStore.create(undo_dir, session_id=None, *, workspace_root=None)` removes an
existing buffer at the supplied path and initializes `metadata.json`,
`index.json`, and `files/`. There is one latest buffer, not arbitrary multi-level
history. `open(undo_dir)` requires those files and the snapshots directory.

`snapshot_file(path)` requires an absolute path string. For a previously existing
file it copies the bytes to `files/<number>` and indexes the original path. For a
nonexistent file it records `existed=False` and no snapshot, so restore can remove
a subsequently created file. Repeated snapshots with the same recorded path
string return the first record, preserving its pre-change state; callers should
use consistent path spelling.

`discard_snapshot(path)` removes a record and its stored copy if present.
`discard_snapshot_if_unchanged(path)` does so only when current bytes equal the
snapshot, or a previously nonexistent target still does not exist. ToolExecutor
snapshots `write_file` and `apply_patch` targets before execution and uses this
conditional discard for newly captured snapshots after failed actions. It does
not snapshot arbitrary command side effects. See [tools](../tools/README.md) for
tool execution details.

`restore(*, workspace_root)` requires an `active` buffer with a valid recorded
absolute workspace root resolving to the same current workspace. Although
creation allows omitted workspace metadata, such a buffer cannot be restored.
Before claiming the buffer or mutating any target, restore validates every index
record: absolute target paths, boolean existence flags, resolved target
containment inside the current workspace, and required snapshot files contained
inside the undo `files/` directory.

After validation, restore durably changes status from `active` to `restoring`,
then copies saved bytes back or deletes newly created targets. Successful
completion records `restored`. Once the buffer is claimed, interruption or
failure leaves it non-replayable rather than allowing a partial restore to
overwrite later user work. Restoration is not an all-or-nothing filesystem
transaction; it can partially mutate targets before failing.

## Error logs and host ownership

`write_error_log(log_path, exc)` creates parent directories and overwrites the
caller-selected file with a UTC timestamp and the latest formatted exception
traceback, returning the path. It does not deliberately collect prompts,
payloads, tool outputs, file contents, headers, or API keys. It does not redact
arbitrary exception text; formatted tracebacks can include source lines and
sensitive information already embedded in an exception.

Core owns storage mechanics, session serialization/replay, undo mechanics, and
the explicit storage helpers. The host/application owns session-root locations,
whether to use storage, supplied paths, session presentation/UI, retention and
deletion policy, and when/how users invoke undo. The generic run builder requires
explicit paths or supplied stores/factories for its default storage construction;
AgentLoop's recording paths allow an absent session store. Core does not choose
application storage locations or automatically discover a host's sessions.
