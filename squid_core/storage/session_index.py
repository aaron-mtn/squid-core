"""Inspect and discover explicitly located session directories."""

import json
import re
from pathlib import Path
from typing import Any

from squid_core.storage.session_jsonl import load_session_jsonl


def register_session_root(index_path: Path, sessions_dir: Path) -> None:
    """Append one explicit sessions directory to an explicit registry file."""
    index_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_root = sessions_dir.resolve()
    if resolved_root in list_registered_session_roots(index_path):
        return

    with index_path.open("a", encoding="utf-8") as file:
        file.write(json.dumps({"sessions_dir": str(resolved_root)}) + "\n")


def list_registered_session_roots(index_path: Path) -> list[Path]:
    """Return unique valid session roots from an explicit registry file."""
    if not index_path.exists():
        return []

    roots = []
    seen = set()
    with index_path.open(encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            root = entry.get("sessions_dir") if isinstance(entry, dict) else None
            if not isinstance(root, str) or not root:
                continue
            path = Path(root)
            if path in seen:
                continue
            seen.add(path)
            roots.append(path)
    return roots


def list_all_sessions(index_path: Path) -> list[dict]:
    """Load sessions across registered roots, newest first."""
    sessions = []
    for sessions_dir in list_registered_session_roots(index_path):
        try:
            sessions.extend(list_sessions(sessions_dir))
        except OSError:
            # Stale or temporarily unreadable roots must not break discovery
            # for every other registered location.
            continue
    return sorted(sessions, key=_updated_at, reverse=True)


def list_sessions(sessions_dir: Path) -> list[dict]:
    """Load valid session metadata below ``sessions_dir``, newest first."""
    if not sessions_dir.exists():
        return []

    sessions = []
    for session_dir in sessions_dir.iterdir():
        if not session_dir.is_dir():
            continue
        metadata_path = session_dir / "metadata.json"
        if not metadata_path.exists():
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(metadata, dict):
            sessions.append(metadata)
    return sorted(sessions, key=_updated_at, reverse=True)


def latest_session_id(sessions_dir: Path) -> str | None:
    """Return the newest session ID, if the directory has saved sessions."""
    sessions = list_sessions(sessions_dir)
    return sessions[0].get("session_id") if sessions else None


def session_dir_for(sessions_dir: Path, session_id: str) -> Path:
    """Return the directory for one validated exact session ID."""
    if not isinstance(session_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]*", session_id
    ):
        raise ValueError(f"Invalid session ID: {session_id!r}")

    resolved_root = sessions_dir.resolve()
    path = sessions_dir / session_id
    if not path.resolve().is_relative_to(resolved_root):
        raise ValueError(f"Session path escapes sessions directory: {session_id!r}")
    if not path.is_dir():
        raise FileNotFoundError(f"Session not found: {session_id}")
    return path


def load_session_entries(sessions_dir: Path, session_id: str) -> list[dict]:
    """Load raw JSONL entries for one saved session in append order."""
    jsonl_path = session_dir_for(sessions_dir, session_id) / "session.jsonl"
    return load_session_jsonl(jsonl_path)


def _updated_at(metadata: dict[str, Any]) -> str:
    updated_at = metadata.get("updated_at")
    return updated_at if isinstance(updated_at, str) else ""
