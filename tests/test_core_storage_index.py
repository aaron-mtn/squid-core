"""Tests for generic Squid Core session-directory inspection."""

import json
from pathlib import Path

from squid_core.storage.session_index import (
    latest_session_id,
    list_all_sessions,
    list_registered_session_roots,
    list_sessions,
    load_session_entries,
    register_session_root,
    session_dir_for,
)


def _write_session(sessions_dir: Path, session_id: str, updated_at: str) -> Path:
    session_dir = sessions_dir / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "metadata.json").write_text(
        json.dumps({"session_id": session_id, "updated_at": updated_at}),
        encoding="utf-8",
    )
    (session_dir / "session.jsonl").write_text(
        json.dumps({"id": "e_000001", "type": "message"}) + "\n",
        encoding="utf-8",
    )
    return session_dir


def test_helpers_inspect_an_explicit_sessions_directory(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "host-selected-sessions"
    _write_session(sessions_dir, "older", "2026-01-01T00:00:00+00:00")
    newest = _write_session(sessions_dir, "newer", "2026-01-02T00:00:00+00:00")

    assert [item["session_id"] for item in list_sessions(sessions_dir)] == [
        "newer",
        "older",
    ]
    assert latest_session_id(sessions_dir) == "newer"
    assert session_dir_for(sessions_dir, "newer") == newest
    assert load_session_entries(sessions_dir, "newer") == [
        {"id": "e_000001", "type": "message"}
    ]


def test_registry_skips_malformed_and_duplicate_roots(tmp_path: Path) -> None:
    index_path = tmp_path / "registry" / "sessions.jsonl"
    sessions_dir = tmp_path / "sessions"

    register_session_root(index_path, sessions_dir)
    register_session_root(index_path, sessions_dir / ".." / "sessions")
    with index_path.open("a", encoding="utf-8") as file:
        file.write("{broken\n")
        file.write(json.dumps({"sessions_dir": str(sessions_dir.resolve())}) + "\n")
        file.write(json.dumps({"sessions_dir": 42}) + "\n")

    assert list_registered_session_roots(index_path) == [sessions_dir.resolve()]


def test_cross_root_discovery_skips_missing_roots_and_sorts_newest_first(
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "registry.jsonl"
    older_root = tmp_path / "older"
    newer_root = tmp_path / "newer"
    missing_root = tmp_path / "missing"
    _write_session(older_root, "older", "2026-01-01T00:00:00+00:00")
    _write_session(newer_root, "newer", "2026-01-02T00:00:00+00:00")
    for sessions_dir in (older_root, missing_root, newer_root):
        register_session_root(index_path, sessions_dir)

    assert [item["session_id"] for item in list_all_sessions(index_path)] == [
        "newer",
        "older",
    ]
