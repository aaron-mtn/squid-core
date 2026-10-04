"""Own disk persistence mechanics for one Squid Core session."""

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from squid_core.image_content import inspect_image_bytes
from squid_core.storage.atomic_json import atomic_write_bytes, atomic_write_json
from squid_core.storage.model_transcript import (
    TRANSCRIPT_TITLE,
    write_model_transcript,
)
from squid_core.storage.session_jsonl import load_session_jsonl


# Status values allowed in metadata.json.
# Keep this list small until the app has real UX that needs more states.
ALLOWED_STATUSES = {"active", "complete", "failed", "abandoned", "interrupted"}
RUNTIME_LIFECYCLE_EVENTS = {
    "hooks_configured",
    "hook_execution",
    "model_request_started",
    "model_request_retry_scheduled",
    "model_request_retry_started",
    "model_request_finished",
    "model_request_failed",
    "run_failed",
    "run_interrupted",
}
GENERATED_IMAGE_CACHE_VERSION = 1
GENERATED_IMAGE_CACHE_FILENAME = ".cache-key"


def _safe_asset_component(value: object) -> bool:
    """Accept one ordinary path component, never a path or dot directory."""

    return (
        isinstance(value, str)
        and value not in {"", ".", ".."}
        and Path(value).name == value
        and "/" not in value
        and "\\" not in value
    )


class SessionStore:
    """Persist one Squid Core session to an explicitly supplied directory."""

    # Store the paths for one session folder.
    #
    # A session folder currently contains:
    # - metadata.json: run-level facts like status, task, and active leaf
    # - session.jsonl: append-only transcript entries
    # - model-view.jsonl: the final model-visible representation per request
    # - model-transcript.txt: a readable, deduplicated projection of both logs
    # - assets/: immutable image snapshots and lazily generated session images
    def __init__(self, session_dir: Path) -> None:
        self.session_dir = session_dir
        self.metadata_path = session_dir / "metadata.json"
        self.jsonl_path = session_dir / "session.jsonl"
        self.model_view_path = session_dir / "model-view.jsonl"
        self.model_transcript_path = session_dir / "model-transcript.txt"
        self.assets_dir = session_dir / "assets"

    @classmethod
    def create(
        cls,
        sessions_dir: Path,
        task: str,
        *,
        workspace_root: Path | None = None,
    ) -> "SessionStore":
        """Create a session below ``sessions_dir`` without app path policy."""

        sessions_dir = Path(sessions_dir)
        sessions_dir.mkdir(parents=True, exist_ok=True)

        # Session IDs are human-readable so users can browse folders without
        # decoding UUID soup.
        session_id = _unique_session_id(sessions_dir, task)
        session_dir = sessions_dir / session_id
        session_dir.mkdir()

        # Store machine-friendly timestamp truth in metadata.
        # Later CLI rendering can decide whether to show 12-hour display time.
        now = _utc_now()
        metadata: dict[str, Any] = {
            "session_id": session_id,
            "created_at": now,
            "updated_at": now,
            "task": task,
            "status": "active",
            "active_leaf_id": None,
        }
        if workspace_root is not None:
            # The host may retain workspace identity for viewers without Core
            # needing to know where that host stores sessions.
            metadata["repo_root"] = str(Path(workspace_root).resolve())

        # Write the initial metadata, append-only logs, and derived readable view.
        store = cls(session_dir)
        store._write_metadata(metadata)
        store.jsonl_path.write_text("", encoding="utf-8")
        store.model_view_path.write_text("", encoding="utf-8")
        store.model_transcript_path.write_text(
            f"{TRANSCRIPT_TITLE}\n", encoding="utf-8"
        )
        return store

    # Open an existing session folder without creating or modifying files.
    @classmethod
    def open(cls, session_dir: Path) -> "SessionStore":
        store = cls(session_dir)

        if not store.metadata_path.is_file():
            raise FileNotFoundError(f"Missing session metadata: {store.metadata_path}")

        if not store.jsonl_path.is_file():
            raise FileNotFoundError(f"Missing session transcript: {store.jsonl_path}")

        return store

    # Append one transcript message as a JSONL entry.
    #
    # The optional usage field is reserved for future token/cost tracking.
    # For now most entries will store usage as None.
    def append_message(self, message: dict, usage: dict | None = None) -> dict:
        # Read metadata so the new entry can link to the previous active leaf.
        metadata = self._read_metadata()
        previous_leaf_id = metadata["active_leaf_id"]

        # Entry IDs are intentionally boring and linear for v1.
        # Branching can use the parent_id chain later, but today it is just
        # a simple linked list through the transcript.
        entry_id = f"e_{self._next_entry_number():06d}"
        entry = {
            "id": entry_id,
            "parent_id": previous_leaf_id,
            "type": "message",
            "timestamp": _utc_now(),
            "message": message,
            "usage": usage,
        }

        # JSONL keeps appends simple and avoids rewriting the whole transcript.
        with self.jsonl_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry) + "\n")

        # Move the active leaf to the newly appended entry.
        metadata["active_leaf_id"] = entry_id
        metadata["updated_at"] = _utc_now()
        self._write_metadata(metadata)
        self._refresh_model_transcript()

        return entry

    # Append a provider-derived assistant reply snapshot. This is diagnostic
    # only: load_messages() deliberately ignores it, so it is never replayed.
    def append_model_reply(self, reply: dict) -> dict:
        entry = self._append_non_message_entry("model_reply", {"reply": reply})
        self._refresh_model_transcript()
        return entry

    def append_model_view(self, request: dict) -> dict:
        """Append the exact finalized representation used for one model request."""
        record = {
            "request_index": self._next_model_request_number(),
            "timestamp": _utc_now(),
            **request,
        }
        # This log deliberately has its own linear sequence: unlike transcript
        # events, model requests do not participate in the active-leaf chain.
        with self.model_view_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record) + "\n")
            file.flush()
        self._refresh_model_transcript()
        return record

    # Append validation/rejection details linked to the model_reply snapshot.
    def append_reply_rejection(
        self,
        reply: dict,
        *,
        reason: str,
        consecutive_invalid_attempt: int,
        raw_reply_id: str,
    ) -> dict:
        fields = {
            "reason": reason,
            "error": reply.get("format_error") or reason,
            "consecutive_invalid_attempt": consecutive_invalid_attempt,
            "raw_reply_id": raw_reply_id,
        }
        for name in ("recoverable_tool_name", "recoverable_raw_arguments"):
            if name in reply:
                fields[name] = reply[name]
        return self._append_non_message_entry("reply_rejection", fields)

    def append_compaction_event(self, event: dict) -> dict:
        """Append one first-class compaction lifecycle event."""
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type.startswith("compaction_"):
            raise ValueError("compaction event type must start with 'compaction_'")

        # Keep lifecycle fields at the top level like other diagnostic events;
        # this makes chronological JSONL inspection and filtering straightforward.
        entry = self._append_non_message_entry(
            event_type,
            {name: value for name, value in event.items() if name != "type"},
        )
        if event_type in {
            "compaction_request",
            "compaction_reasoning",
            "compaction_summary",
            "compaction_applied",
        }:
            self._refresh_model_transcript()
        return entry

    def append_runtime_event(self, event: dict) -> dict:
        """Append one supported runtime lifecycle event to the session log."""
        event_type = event.get("type")
        if event_type not in RUNTIME_LIFECYCLE_EVENTS:
            raise ValueError(f"unsupported runtime lifecycle event: {event_type!r}")

        # These records use the same linked JSONL spine as transcript and
        # compaction diagnostics, but load_messages() intentionally ignores them.
        return self._append_non_message_entry(
            event_type,
            {name: value for name, value in event.items() if name != "type"},
        )

    # Append one non-transcript run-context event as a JSONL entry.
    def append_run_context(
        self,
        *,
        skill_name: str | None,
        allowed_tools: list[str],
        prompt_overrides: list[str],
        mod_id: str | None = None,
    ) -> dict:
        session_metadata = self._read_metadata()
        previous_leaf_id = session_metadata["active_leaf_id"]

        entry_id = f"e_{self._next_entry_number():06d}"
        entry = {
            "id": entry_id,
            "parent_id": previous_leaf_id,
            "type": "run_context",
            "timestamp": _utc_now(),
            "skill_name": skill_name,
            "allowed_tools": sorted(allowed_tools),
            "prompt_overrides": sorted(prompt_overrides),
        }

        # Mod identity is optional wrapper metadata. Core persists only the
        # stable identifier and remains unaware of presentation concerns.
        if mod_id is not None:
            entry["mod_id"] = mod_id

        with self.jsonl_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry) + "\n")

        session_metadata["active_leaf_id"] = entry_id
        session_metadata["updated_at"] = _utc_now()
        self._write_metadata(session_metadata)

        return entry

    def append_system_message_reconciliation(self, message: dict) -> dict:
        """Persist the system message that supersedes earlier run specializations."""
        return self._append_non_message_entry(
            "system_message_reconciliation",
            {"message": message},
        )

    def load_messages(self) -> list[dict]:
        """Load the effective replay transcript, including applied compactions."""
        return self._load_messages(replay_compactions=True)

    def load_canonical_messages(self) -> list[dict]:
        """Load original messages without applying compaction transformations."""
        return self._load_messages(replay_compactions=False)

    # Reconciliation events replace only the active system message in both
    # views. Other non-message events remain outside provider-style history.
    def _load_messages(self, *, replay_compactions: bool) -> list[dict]:
        messages = []

        for entry in load_session_jsonl(self.jsonl_path):
            if entry.get("type") == "message":
                messages.append(entry["message"])
            elif entry.get("type") == "system_message_reconciliation":
                # Keep storage append-only while making the newest run's
                # specialization prompt durable across process restarts.
                messages = [
                    entry["message"],
                    *(
                        message
                        for message in messages
                        if message.get("role") != "system"
                    ),
                ]
            elif replay_compactions and entry.get("type") == "compaction_applied":
                messages = _replay_applied_compaction(messages, entry)

        trailing = messages[-1] if messages else None
        tool_calls = (
            trailing.get("tool_calls")
            if isinstance(trailing, dict) and trailing.get("role") == "assistant"
            else None
        )
        if tool_calls:
            # Older interrupted runs could commit an assistant call without its
            # required result. Repair only the genuinely trailing case so load
            # never guesses about malformed history in the middle of a session.
            for tool_call in tool_calls:
                function = tool_call.get("function") or {}
                result = {
                    "ok": False,
                    "tool": function.get("name", ""),
                    "error": "tool action interrupted",
                    "reason": "recovered trailing tool call from an interrupted session",
                    "interrupted": True,
                    "recovered": True,
                }
                message = {
                    "role": "tool",
                    "tool_call_id": tool_call.get("id", ""),
                    "content": json.dumps(result, indent=2),
                }
                self.append_message(message)
                messages.append(message)

        return messages

    # Update the run-level session status in metadata.json.
    #
    # This is intentionally strict so typos do not silently create new states.
    def update_status(self, status: str) -> None:
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"Unknown session status: {status}")

        metadata = self._read_metadata()
        metadata["status"] = status
        metadata["updated_at"] = _utc_now()
        self._write_metadata(metadata)

    @property
    def session_id(self) -> str:
        return self._read_metadata()["session_id"]

    def snapshot_asset(self, data: bytes, extension: str) -> str:
        """Store exact immutable bytes once and return their safe session asset ID."""

        if not isinstance(data, bytes):
            raise TypeError("session asset data must be bytes")
        if extension not in {".png", ".jpg", ".webp"}:
            raise ValueError(f"unsupported session asset extension: {extension!r}")
        digest = hashlib.sha256(data).hexdigest()
        asset_id = f"{digest}{extension}"
        asset_path = self._asset_path(asset_id)
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        if asset_path.exists():
            if self.load_asset(asset_id) != data:
                raise OSError(f"session asset content mismatch: {asset_id}")
            return asset_id
        atomic_write_bytes(asset_path, data)
        return asset_id

    def ensure_generated_image(
        self,
        folder_name: str,
        filename: str,
        cache_key: str,
        generate: Callable[[], bytes],
        folder_identity: str | None = None,
    ) -> tuple[str, bool]:
        """Materialize one named PNG once for a deterministic generation key.

        Named images are model-readable files, while ``snapshot_asset`` remains
        the immutable content-addressed storage used for provider injection.
        Keeping both below the session asset root gives generated pages the same
        containment and durable session lifetime as ordinary image snapshots.

        One folder-level metadata file records both folder ownership and the
        generation key for each lazily produced image. Keeping per-image keys
        in that single file matters because changing render settings for one
        requested page must not validate stale, unrequested sibling pages.
        """

        if not isinstance(cache_key, str) or not cache_key:
            raise ValueError("generated image cache key must be a non-empty string")
        if folder_identity is not None and (
            not isinstance(folder_identity, str) or not folder_identity
        ):
            raise ValueError("generated image folder identity must be a non-empty string")
        if not _safe_asset_component(folder_name):
            raise ValueError(f"invalid generated image folder: {folder_name!r}")
        if not _safe_asset_component(filename) or not filename.lower().endswith(".png"):
            raise ValueError(f"invalid generated image filename: {filename!r}")

        resolved_session = self.session_dir.resolve()
        resolved_assets = self.assets_dir.resolve()
        if not resolved_assets.is_relative_to(resolved_session):
            raise ValueError("session asset directory escapes the session directory")
        folder = self._generated_image_folder(
            resolved_assets,
            folder_name,
            folder_identity,
        )
        image_path = (folder / filename).resolve()
        if not image_path.is_relative_to(folder):
            raise ValueError("generated image path escapes its asset folder")
        folder.mkdir(parents=True, exist_ok=True)
        metadata_path = folder / GENERATED_IMAGE_CACHE_FILENAME
        metadata = self._generated_image_metadata(
            metadata_path,
            folder_identity,
            initialize_missing=True,
        )

        try:
            existing_data = image_path.read_bytes()
            inspect_image_bytes(existing_data, ".png")
            if metadata["images"].get(filename) == cache_key:
                return str(image_path), True
        except (FileNotFoundError, OSError, UnicodeError, ValueError):
            # A partial or corrupt cache is safely regenerated below.
            pass

        data = generate()
        inspect_image_bytes(data, ".png")
        atomic_write_bytes(image_path, data)
        metadata["images"][filename] = cache_key
        atomic_write_json(metadata_path, metadata)
        return str(image_path), False

    @staticmethod
    def _generated_image_metadata(
        metadata_path: Path,
        folder_identity: str | None,
        *,
        initialize_missing: bool = False,
    ) -> dict[str, Any]:
        """Load strict folder cache metadata, optionally initializing a new folder."""

        try:
            value = json.loads(metadata_path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            if initialize_missing:
                return {
                    "version": GENERATED_IMAGE_CACHE_VERSION,
                    "folder_identity": folder_identity,
                    "images": {},
                }
            raise ValueError("missing generated image cache metadata") from error
        except (json.JSONDecodeError, OSError, UnicodeError) as error:
            raise ValueError("invalid generated image cache metadata") from error

        if (
            not isinstance(value, dict)
            or value.get("version") != GENERATED_IMAGE_CACHE_VERSION
            or value.get("folder_identity") != folder_identity
            or not isinstance(value.get("images"), dict)
            or any(
                not _safe_asset_component(filename)
                or not filename.lower().endswith(".png")
                or not isinstance(key, str)
                or not key
                for filename, key in value["images"].items()
            )
        ):
            raise ValueError("invalid generated image cache metadata")
        return value

    @staticmethod
    def _generated_image_folder(
        resolved_assets: Path,
        folder_name: str,
        folder_identity: str | None,
    ) -> Path:
        """Choose the first unused or already-owned readable image folder."""

        index = 1
        while True:
            candidate_name = folder_name if index == 1 else f"{folder_name} {index}"
            folder = (resolved_assets / candidate_name).resolve()
            if not folder.is_relative_to(resolved_assets):
                raise ValueError("generated image folder escapes the asset directory")
            if folder_identity is None or not folder.exists():
                return folder
            if folder.is_dir():
                metadata_path = folder / GENERATED_IMAGE_CACHE_FILENAME
                try:
                    metadata = SessionStore._generated_image_metadata(
                        metadata_path,
                        folder_identity,
                    )
                except ValueError:
                    # Invalid metadata cannot prove that this caller owns the
                    # occupied folder, so preserve it and keep searching.
                    pass
                else:
                    return folder
            # Unknown and differently owned folders are left untouched.
            index += 1

    def load_asset(self, asset_id: str) -> bytes:
        """Load and verify one content-addressed asset without accepting paths."""

        if not isinstance(asset_id, str):
            raise ValueError(f"invalid session asset ID: {asset_id!r}")
        match = re.fullmatch(r"([0-9a-f]{64})\.(png|jpg|webp)", asset_id)
        if match is None:
            raise ValueError(f"invalid session asset ID: {asset_id!r}")
        asset_path = self._asset_path(asset_id)
        try:
            data = asset_path.read_bytes()
        except FileNotFoundError as error:
            raise FileNotFoundError(f"Missing session image asset: {asset_id}") from error
        if hashlib.sha256(data).hexdigest() != match.group(1):
            raise OSError(f"Corrupt session image asset: {asset_id}")
        return data

    def _asset_path(self, asset_id: str) -> Path:
        """Resolve a fixed-format asset ID below this session, including symlinks."""

        resolved_session = self.session_dir.resolve()
        resolved_assets = self.assets_dir.resolve()
        if not resolved_assets.is_relative_to(resolved_session):
            raise ValueError("session asset directory escapes the session directory")
        asset_path = (resolved_assets / asset_id).resolve()
        if not asset_path.is_relative_to(resolved_assets):
            raise ValueError(f"session asset path escapes asset directory: {asset_id!r}")
        return asset_path

    # Return the next linear entry number for session.jsonl.
    #
    # This scans the JSONL file for now because it is simple and reliable.
    # If sessions get huge later, metadata can grow a next_entry_number field.
    def _next_entry_number(self) -> int:
        # A resumed session may end in an interrupted append. This is the point
        # where read-only recovery must become durable: remove only that proven
        # torn tail before assigning an ID and appending new valid history.
        entries = load_session_jsonl(self.jsonl_path, truncate_torn_tail=True)
        return len(entries) + 1

    def _next_model_request_number(self) -> int:
        """Return the next request number, including requests from prior runs."""
        if not self.model_view_path.exists():
            return 1
        with self.model_view_path.open(encoding="utf-8") as file:
            return sum(1 for line in file if line.strip()) + 1

    def _append_non_message_entry(self, entry_type: str, fields: dict) -> dict:
        """Append a linked event that is not itself a transcript message."""
        metadata = self._read_metadata()
        entry_id = f"e_{self._next_entry_number():06d}"
        entry = {
            "id": entry_id,
            "parent_id": metadata["active_leaf_id"],
            "type": entry_type,
            "timestamp": _utc_now(),
            **fields,
        }

        with self.jsonl_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry) + "\n")

        metadata["active_leaf_id"] = entry_id
        metadata["updated_at"] = _utc_now()
        self._write_metadata(metadata)
        return entry

    def _refresh_model_transcript(self) -> None:
        """Best-effort refresh of the derived human-readable model view."""
        try:
            write_model_transcript(
                self.jsonl_path,
                self.model_view_path,
                self.model_transcript_path,
            )
        except Exception:
            # A convenience diagnostic must never make canonical persistence or
            # an otherwise valid provider request fail. Its sources remain intact.
            pass

    # Read metadata.json as a dictionary.
    def _read_metadata(self) -> dict[str, Any]:
        with self.metadata_path.open(encoding="utf-8") as file:
            return json.load(file)

    # Write metadata.json in a stable, readable format.
    def _write_metadata(self, metadata: dict[str, Any]) -> None:
        atomic_write_json(self.metadata_path, metadata)


def _replay_applied_compaction(messages: list[dict], entry: dict) -> list[dict]:
    """Apply one durable effective-transcript boundary, or fail closed.

    Older sessions have no replay payload and retain their historical replay
    behavior. New payloads are accepted only when their expected message counts
    match the effective transcript constructed so far; a partial or malformed
    diagnostic event must never discard canonical history.
    """
    replay = entry.get("replay")
    if not isinstance(replay, dict) or replay.get("version") != 1:
        return messages

    messages_before = entry.get("messages_before")
    messages_after = entry.get("messages_after")
    protected_count = replay.get("protected_message_count")
    preserved_count = replay.get("preserved_message_count")
    compacted_message = replay.get("compacted_message")
    counts = (
        messages_before,
        messages_after,
        protected_count,
        preserved_count,
    )
    if any(
        isinstance(value, bool) or not isinstance(value, int) for value in counts
    ):
        return messages
    if (
        messages_before != len(messages)
        or protected_count < 0
        or preserved_count < 0
        or protected_count + preserved_count > len(messages)
        or messages_after != protected_count + 1 + preserved_count
        or not isinstance(compacted_message, dict)
        or compacted_message.get("role") != "assistant"
        or not isinstance(compacted_message.get("content"), str)
    ):
        return messages

    preserved = messages[-preserved_count:] if preserved_count else []
    return [
        *messages[:protected_count],
        compacted_message,
        *preserved,
    ]


# Build a unique, human-readable session ID.
#
# Format:
#   YYYYMMDD-HHMMSS-<task-slug>
#
# If two sessions start in the same second with the same task slug, append a
# short random suffix instead of overwriting the existing folder.
def _unique_session_id(sessions_dir: Path, task: str) -> str:
    base_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{_slugify(task)}"
    session_id = base_id

    while (sessions_dir / session_id).exists():
        session_id = f"{base_id}-{secrets.token_hex(2)}"

    return session_id


# Convert the task text into a filesystem-friendly slug.
#
# Examples:
#   "Fix Formatter Underscore Bug" -> "fix-formatter-underscore-bug"
#   "!!!" -> "session"
def _slugify(task: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")

    if not slug:
        return "session"

    # Cap long tasks so session folder names stay readable.
    return slug[:50].strip("-") or "session"


# Return a timezone-aware UTC timestamp for durable metadata.
#
# User-facing display can later render this however it wants.
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
