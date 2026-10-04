"""Tests for durable Squid Core session storage."""

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from squid_core.storage import SessionStore


def read_metadata(store: SessionStore) -> dict:
    return json.loads(store.metadata_path.read_text(encoding="utf-8"))


def read_entries(store: SessionStore) -> list[dict]:
    return [
        json.loads(line)
        for line in store.jsonl_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def applied_compaction_event(
    *,
    messages_before: int,
    compacted_message: dict,
    protected_count: int,
    preserved_count: int,
) -> dict:
    """Build the durable replay fields emitted by a successful compaction."""
    return {
        "type": "compaction_applied",
        "messages_before": messages_before,
        "messages_after": protected_count + 1 + preserved_count,
        "replay": {
            "version": 1,
            "protected_message_count": protected_count,
            "preserved_message_count": preserved_count,
            "compacted_message": compacted_message,
        },
    }


def test_create_writes_initial_metadata_and_empty_jsonl(tmp_path: Path) -> None:
    store = SessionStore.create(
        tmp_path,
        "Fix Formatter Underscore Bug",
        workspace_root=tmp_path,
    )
    metadata = read_metadata(store)

    assert store.metadata_path.exists()
    assert store.jsonl_path.exists()
    assert store.jsonl_path.read_text(encoding="utf-8") == ""
    assert store.model_view_path == store.jsonl_path.with_name("model-view.jsonl")
    assert store.model_view_path.exists()
    assert store.model_view_path.read_text(encoding="utf-8") == ""
    assert store.model_transcript_path == store.jsonl_path.with_name(
        "model-transcript.txt"
    )
    assert store.model_transcript_path.read_text(encoding="utf-8") == (
        "MODEL VIEW TRANSCRIPT\n"
    )
    assert metadata["status"] == "active"
    assert metadata["task"] == "Fix Formatter Underscore Bug"
    assert metadata["repo_root"] == str(tmp_path.resolve())
    assert metadata["session_id"]
    assert metadata["active_leaf_id"] is None


def test_append_model_view_writes_one_numbered_record_per_request(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "model view")
    request = {
        "provider": "test-provider",
        "model": "test-model",
        "messages": [{"role": "user", "content": "final context"}],
        "tools": [{"function": {"name": "read_file"}}],
    }

    first = store.append_model_view(request)
    second = store.append_model_view(request)
    records = [
        json.loads(line)
        for line in store.model_view_path.read_text(encoding="utf-8").splitlines()
    ]

    assert records == [first, second]
    assert [record["request_index"] for record in records] == [1, 2]
    assert all(record["timestamp"] for record in records)
    assert records[0]["messages"] == request["messages"]
    assert records[0]["tools"] == request["tools"]


def test_session_id_uses_sortable_24_hour_timestamp_and_task_slug(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "Fix Formatter Underscore Bug")
    metadata = read_metadata(store)

    assert re.fullmatch(
        r"\d{8}-\d{6}-fix-formatter-underscore-bug",
        metadata["session_id"],
    )


def test_session_id_property_returns_metadata_session_id(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "property")

    assert store.session_id == read_metadata(store)["session_id"]


def test_append_message_writes_jsonl_entries_and_updates_active_leaf(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "append messages")

    first = store.append_message({"role": "user", "content": "hello"})
    second = store.append_message(
        {"role": "assistant", "content": "hi"},
        usage={"input_tokens": 3, "output_tokens": 2},
    )
    entries = read_entries(store)
    metadata = read_metadata(store)

    assert first == entries[0]
    assert second == entries[1]
    assert entries[0]["id"] == "e_000001"
    assert entries[1]["id"] == "e_000002"
    assert entries[0]["parent_id"] is None
    assert entries[1]["parent_id"] == "e_000001"
    assert metadata["active_leaf_id"] == "e_000002"
    assert entries[0]["usage"] is None
    assert entries[1]["usage"] == {"input_tokens": 3, "output_tokens": 2}


def test_load_messages_returns_message_dicts_in_order(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "load messages")
    messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
    ]

    for message in messages:
        store.append_message(message)

    assert store.load_messages() == messages


def test_load_messages_ignores_compaction_entries(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "compact")
    messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
    ]

    store.append_message(messages[0])
    store.append_compaction_event({"type": "compaction_check", "will_compact": False})
    store.append_message(messages[1])

    assert store.load_messages() == messages


def test_append_run_context_writes_sorted_non_message_entry_and_updates_active_leaf(
    tmp_path: Path,
) -> None:
    store = SessionStore.create(tmp_path, "context")

    entry = store.append_run_context(
        skill_name="user-review",
        allowed_tools=["run_command", "read_file"],
        prompt_overrides=["TOOL_USAGE_GUIDANCE", "FINAL_SUMMARY_REQUIREMENTS"],
    )
    entries = read_entries(store)
    metadata = read_metadata(store)

    assert entry == entries[0]
    assert entries[0]["id"] == "e_000001"
    assert entries[0]["parent_id"] is None
    assert entries[0]["type"] == "run_context"
    assert entries[0]["skill_name"] == "user-review"
    assert entries[0]["allowed_tools"] == ["read_file", "run_command"]
    assert entries[0]["prompt_overrides"] == [
        "FINAL_SUMMARY_REQUIREMENTS",
        "TOOL_USAGE_GUIDANCE",
    ]
    assert metadata["active_leaf_id"] == "e_000001"


def test_load_messages_ignores_run_context_entries(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "context")
    messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
    ]

    store.append_message(messages[0])
    store.append_run_context(
        skill_name=None,
        allowed_tools=["write_file", "read_file"],
        prompt_overrides=[],
    )
    store.append_message(messages[1])

    assert store.load_messages() == messages


def test_load_messages_applies_successful_compaction_and_keeps_later_messages(
    tmp_path: Path,
) -> None:
    store = SessionStore.create(tmp_path, "durable compact")
    system = {"role": "system", "content": "system"}
    task = {"role": "user", "content": "original task"}
    old_assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call_old", "function": {"name": "read_file"}}],
    }
    old_tool = {
        "role": "tool",
        "tool_call_id": "call_old",
        "content": "full old source",
    }
    recent = {"role": "assistant", "content": "recent work"}
    canonical = [system, task, old_assistant, old_tool, recent]
    for message in canonical:
        store.append_message(message)

    compacted = {"role": "assistant", "content": "# compacted\nsummary"}
    store.append_compaction_event(
        applied_compaction_event(
            messages_before=len(canonical),
            compacted_message=compacted,
            protected_count=2,
            preserved_count=1,
        )
    )
    follow_up = {"role": "user", "content": "follow up"}
    store.append_message(follow_up)

    assert SessionStore.open(store.session_dir).load_messages() == [
        system,
        task,
        compacted,
        recent,
        follow_up,
    ]
    assert SessionStore.open(store.session_dir).load_canonical_messages() == [
        *canonical,
        follow_up,
    ]
    assert old_tool in [
        entry["message"]
        for entry in read_entries(store)
        if entry.get("type") == "message"
    ]


def test_load_messages_replays_multiple_compactions_in_order(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "multiple compact")
    system = {"role": "system", "content": "system"}
    task = {"role": "user", "content": "task"}
    old = {"role": "assistant", "content": "old"}
    recent = {"role": "assistant", "content": "recent"}
    for message in (system, task, old, recent):
        store.append_message(message)

    summary_a = {"role": "assistant", "content": "summary A"}
    store.append_compaction_event(
        applied_compaction_event(
            messages_before=4,
            compacted_message=summary_a,
            protected_count=2,
            preserved_count=1,
        )
    )
    later_a = {"role": "assistant", "content": "later A"}
    later_b = {"role": "user", "content": "later B"}
    store.append_message(later_a)
    store.append_message(later_b)

    # Effective history before B is prefix + summary A + recent + two later
    # messages. B replaces all but its protected prefix and newest two messages.
    summary_b = {"role": "assistant", "content": "summary B"}
    store.append_compaction_event(
        applied_compaction_event(
            messages_before=6,
            compacted_message=summary_b,
            protected_count=2,
            preserved_count=2,
        )
    )
    final = {"role": "assistant", "content": "final"}
    store.append_message(final)

    assert SessionStore.open(store.session_dir).load_messages() == [
        system,
        task,
        summary_b,
        later_a,
        later_b,
        final,
    ]


def test_incomplete_and_failed_compactions_do_not_change_replay(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "failed compact")
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "history"},
    ]
    for message in messages:
        store.append_message(message)

    store.append_compaction_event({"type": "compaction_started", "attempt": 1})
    store.append_compaction_event(
        {"type": "compaction_summary", "attempt": 1, "content": "unused"}
    )
    store.append_compaction_event(
        {
            "type": "compaction_failed",
            "attempt": 1,
            "will_retry": True,
            "stage": "response_parse",
        }
    )
    store.append_compaction_event({"type": "compaction_started", "attempt": 2})
    store.append_compaction_event(
        {"type": "compaction_summary", "attempt": 2, "content": "also unused"}
    )
    store.append_compaction_event(
        {"type": "compaction_failed", "attempt": 2, "stage": "response_parse"}
    )

    assert SessionStore.open(store.session_dir).load_messages() == messages


def test_malformed_or_legacy_applied_compaction_fails_closed(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "malformed compact")
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "history"},
    ]
    for message in messages:
        store.append_message(message)

    store.append_compaction_event(
        {"type": "compaction_applied", "messages_before": len(messages)}
    )
    malformed = applied_compaction_event(
        messages_before=999,
        compacted_message={"role": "assistant", "content": "must not apply"},
        protected_count=2,
        preserved_count=0,
    )
    store.append_compaction_event(malformed)

    assert SessionStore.open(store.session_dir).load_messages() == messages


def test_system_reconciliation_after_compaction_replaces_active_system(
    tmp_path: Path,
) -> None:
    store = SessionStore.create(tmp_path, "compact reconciliation")
    old_system = {"role": "system", "content": "old system"}
    task = {"role": "user", "content": "task"}
    history = {"role": "assistant", "content": "history"}
    for message in (old_system, task, history):
        store.append_message(message)

    compacted = {"role": "assistant", "content": "summary"}
    store.append_compaction_event(
        applied_compaction_event(
            messages_before=3,
            compacted_message=compacted,
            protected_count=2,
            preserved_count=0,
        )
    )
    new_system = {"role": "system", "content": "new system"}
    store.append_system_message_reconciliation(new_system)

    assert SessionStore.open(store.session_dir).load_messages() == [
        new_system,
        task,
        compacted,
    ]


def test_torn_final_diagnostic_keeps_applied_compaction_replayable(
    tmp_path: Path,
) -> None:
    store = SessionStore.create(tmp_path, "compact torn diagnostic")
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "old"},
    ]
    for message in messages:
        store.append_message(message)
    compacted = {"role": "assistant", "content": "summary"}
    store.append_compaction_event(
        applied_compaction_event(
            messages_before=3,
            compacted_message=compacted,
            protected_count=2,
            preserved_count=0,
        )
    )
    with store.jsonl_path.open("a", encoding="utf-8") as file:
        file.write('{"type": "compaction_started", "attempt": ')

    assert SessionStore.open(store.session_dir).load_messages() == [
        messages[0],
        messages[1],
        compacted,
    ]


def test_load_messages_ignores_truncated_final_record(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "torn final record")
    messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
    ]
    for message in messages:
        store.append_message(message)
    with store.jsonl_path.open("a", encoding="utf-8") as file:
        file.write('{"id": "e_000003", "type": "message", "message": {')

    assert SessionStore.open(store.session_dir).load_messages() == messages


@pytest.mark.parametrize("character", ["¢", "€", "🦑"])
def test_truncated_final_utf8_record_recovers_and_can_resume(
    tmp_path: Path,
    character: str,
) -> None:
    store = SessionStore.create(tmp_path, "torn final UTF-8 record")
    original = {"role": "user", "content": "complete"}
    original_entry = store.append_message(original)
    torn_record = (
        b'{"id":"e_000002","type":"message","message":'
        b'{"role":"assistant","content":"hello '
        + character.encode("utf-8")[:-1]
    )
    with store.jsonl_path.open("ab") as file:
        file.write(torn_record)

    assert SessionStore.open(store.session_dir).load_messages() == [original]

    continued = {"role": "assistant", "content": f"continued {character}"}
    continued_entry = store.append_message(continued)
    reloaded = SessionStore.open(store.session_dir)

    assert reloaded.load_messages() == [original, continued]
    assert read_entries(reloaded) == [original_entry, continued_entry]
    assert continued_entry["id"] == "e_000002"
    assert continued_entry["parent_id"] == original_entry["id"]


def test_valid_unicode_records_load_normally(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "valid Unicode")
    messages = [
        {"role": "user", "content": "two-byte ¢"},
        {"role": "assistant", "content": "three-byte €"},
        {"role": "user", "content": "four-byte 🦑"},
    ]
    entries = [store.append_message(message) for message in messages]
    store.jsonl_path.write_text(
        "".join(f"{json.dumps(entry, ensure_ascii=False)}\n" for entry in entries),
        encoding="utf-8",
    )

    assert SessionStore.open(store.session_dir).load_messages() == messages


def test_load_messages_rejects_invalid_utf8_in_middle_record(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "invalid UTF-8 middle record")
    store.append_message({"role": "user", "content": "before"})
    store.append_message({"role": "assistant", "content": "after"})
    valid_lines = store.jsonl_path.read_bytes().splitlines()
    corrupt_middle = b'{"type":"message","message":{"content":"\xff"}}'
    store.jsonl_path.write_bytes(
        valid_lines[0] + b"\n" + corrupt_middle + b"\n" + valid_lines[1] + b"\n"
    )

    with pytest.raises(UnicodeDecodeError):
        store.load_messages()


def test_load_messages_rejects_invalid_utf8_before_physical_tail(
    tmp_path: Path,
) -> None:
    store = SessionStore.create(tmp_path, "invalid UTF-8 final record")
    store.append_message({"role": "user", "content": "before"})
    with store.jsonl_path.open("ab") as file:
        file.write(b'{"type":"message","message":{"content":"\xffafter"}}')

    with pytest.raises(UnicodeDecodeError):
        store.load_messages()


def test_load_messages_ignores_malformed_final_record_before_blank_lines(
    tmp_path: Path,
) -> None:
    store = SessionStore.create(tmp_path, "torn final record with blanks")
    message = {"role": "user", "content": "complete"}
    store.append_message(message)
    with store.jsonl_path.open("a", encoding="utf-8") as file:
        file.write("{malformed\n\n  \n")

    assert store.load_messages() == [message]


def test_load_messages_rejects_malformed_middle_record(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "malformed middle record")
    store.append_message({"role": "user", "content": "before"})
    store.append_message({"role": "assistant", "content": "after"})
    valid_lines = store.jsonl_path.read_text(encoding="utf-8").splitlines()
    store.jsonl_path.write_text(
        f"{valid_lines[0]}\n{{malformed\n{valid_lines[1]}\n",
        encoding="utf-8",
    )

    with pytest.raises(json.JSONDecodeError):
        store.load_messages()


def test_load_messages_rejects_only_malformed_record(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "only malformed record")
    store.jsonl_path.write_text("{malformed", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        store.load_messages()


def test_recovered_session_can_append_and_reload_after_truncated_tail(
    tmp_path: Path,
) -> None:
    store = SessionStore.create(tmp_path, "continue after torn tail")
    original_messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
    ]
    original_entries = [store.append_message(message) for message in original_messages]
    with store.jsonl_path.open("a", encoding="utf-8") as file:
        file.write('{"id": "e_000003", "type": "message", "message": {')

    assert SessionStore.open(store.session_dir).load_messages() == original_messages

    new_message = {"role": "user", "content": "continued"}
    new_entry = store.append_message(new_message)
    reloaded = SessionStore.open(store.session_dir)

    assert reloaded.load_messages() == [*original_messages, new_message]
    assert read_entries(reloaded) == [*original_entries, new_entry]
    assert new_entry["id"] == "e_000003"
    assert new_entry["parent_id"] == original_entries[-1]["id"]
    assert read_metadata(reloaded)["active_leaf_id"] == new_entry["id"]


def test_load_messages_repairs_trailing_unmatched_tool_call_once(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "repair trailing tool")
    store.append_message(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_old",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        }
    )

    first_load = store.load_messages()
    second_load = store.load_messages()

    assert first_load == second_load
    assert first_load[-1]["role"] == "tool"
    assert first_load[-1]["tool_call_id"] == "call_old"
    assert json.loads(first_load[-1]["content"])["recovered"] is True


def test_append_compaction_event_writes_first_class_jsonl_entry(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "compact lifecycle")

    entry = store.append_compaction_event(
        {"type": "compaction_summary", "content": "exact summary"}
    )

    assert entry["type"] == "compaction_summary"
    assert entry["content"] == "exact summary"
    assert read_entries(store) == [entry]
    assert read_metadata(store)["active_leaf_id"] == entry["id"]
    assert store.load_messages() == []


def test_system_reconciliation_survives_reload_and_preserves_non_system_history(
    tmp_path: Path,
) -> None:
    store = SessionStore.create(tmp_path, "switch specialization")
    history = [
        {"role": "system", "content": "Skill A instructions"},
        {"role": "user", "content": "original task"},
        {"role": "assistant", "content": "original result"},
        {"role": "user", "content": "follow up"},
    ]
    for message in history:
        store.append_message(message)

    replacement = {"role": "system", "content": "Skill B instructions"}
    event = store.append_system_message_reconciliation(replacement)

    assert event["type"] == "system_message_reconciliation"
    assert SessionStore.open(store.session_dir).load_messages() == [
        replacement,
        *history[1:],
    ]
    assert sum(
        message["role"] == "system"
        for message in SessionStore.open(store.session_dir).load_messages()
    ) == 1


def test_append_run_context_persists_optional_mod_id(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "mod context")

    with_mod = store.append_run_context(
        skill_name="story-maker",
        allowed_tools=[],
        prompt_overrides=[],
        mod_id="story-maker",
    )
    without_mod = store.append_run_context(
        skill_name=None,
        allowed_tools=[],
        prompt_overrides=[],
    )

    assert with_mod["mod_id"] == "story-maker"
    assert "mod_id" not in without_mod


def test_create_without_workspace_omits_application_metadata(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path / "sessions", "Generic session")

    assert "repo_root" not in read_metadata(store)


def test_create_does_not_register_a_code_squid_global_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "global" / "indexes.jsonl"
    monkeypatch.setenv("CODE_SQUID_SESSION_INDEXES", str(index_path))

    SessionStore.create(tmp_path / "sessions", "Unregistered session")

    assert not index_path.exists()


def test_run_contexts_can_persist_different_mod_ids_in_one_session(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "mod sequence")

    for mod_id in ("story-maker", "reviewer", "story-maker"):
        store.append_run_context(
            skill_name="shared-skill",
            allowed_tools=[],
            prompt_overrides=[],
            mod_id=mod_id,
        )

    contexts = [entry for entry in read_entries(store) if entry["type"] == "run_context"]
    assert [entry["mod_id"] for entry in contexts] == [
        "story-maker",
        "reviewer",
        "story-maker",
    ]


def test_diagnostic_entries_are_linked_and_ignored_by_load_messages(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "diagnostics")
    user = store.append_message({"role": "user", "content": "one"})
    raw = store.append_model_reply(
        {
            "provider": "openrouter",
            "model": "example/model",
            "assistant": {
                "content": "",
                "raw_tool_calls": [
                    {
                        "id": "call_bad",
                        "function": {"name": "read_file", "arguments": '{"path":'},
                    }
                ],
            },
        }
    )
    rejection = store.append_reply_rejection(
        {
            "format_error": "Tool arguments were not valid JSON",
            "recoverable_tool_name": "read_file",
            "recoverable_raw_arguments": '{"path":',
        },
        reason="malformed_tool_call",
        consecutive_invalid_attempt=1,
        raw_reply_id=raw["id"],
    )

    assert raw["type"] == "model_reply"
    assert raw["parent_id"] == user["id"]
    assert rejection["type"] == "reply_rejection"
    assert rejection["parent_id"] == raw["id"]
    assert rejection["raw_reply_id"] == raw["id"]
    assert store.load_messages() == [{"role": "user", "content": "one"}]


def test_compaction_parent_id_links_to_previous_active_leaf(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "compact")

    first = store.append_message({"role": "user", "content": "hello"})
    compaction = store.append_compaction_event(
        {"type": "compaction_applied", "summary_chars": 12}
    )

    assert compaction["parent_id"] == first["id"]


def test_open_returns_store_for_existing_session(tmp_path: Path) -> None:
    created = SessionStore.create(tmp_path, "open existing")

    opened = SessionStore.open(created.session_dir)

    assert opened.session_dir == created.session_dir
    assert opened.metadata_path == created.metadata_path
    assert opened.jsonl_path == created.jsonl_path
    assert read_metadata(opened) == read_metadata(created)


def test_open_raises_file_not_found_error_for_missing_session_files(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        SessionStore.open(tmp_path / "missing")

    missing_jsonl = tmp_path / "missing-jsonl"
    missing_jsonl.mkdir()
    (missing_jsonl / "metadata.json").write_text("{}", encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        SessionStore.open(missing_jsonl)

    missing_metadata = tmp_path / "missing-metadata"
    missing_metadata.mkdir()
    (missing_metadata / "session.jsonl").write_text("", encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        SessionStore.open(missing_metadata)


def test_update_status_accepts_known_terminal_statuses(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "status")

    for status in ["complete", "failed", "abandoned", "interrupted"]:
        store.update_status(status)
        assert read_metadata(store)["status"] == status


def test_update_status_rejects_unknown_status(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "status")

    with pytest.raises(ValueError):
        store.update_status("paused")


def test_duplicate_session_id_gets_suffix_instead_of_overwriting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import squid_core.storage.session_store as session_store

    class FixedDatetime:
        @classmethod
        def now(cls, tz):
            return datetime(2026, 5, 14, 15, 30, 12, tzinfo=tz)

    monkeypatch.setattr(session_store, "datetime", FixedDatetime)

    first = SessionStore.create(tmp_path, "Fix Formatter Underscore Bug")
    second = SessionStore.create(tmp_path, "Fix Formatter Underscore Bug")

    assert first.session_dir.exists()
    assert second.session_dir.exists()
    assert first.session_dir != second.session_dir
    assert read_metadata(first)["session_id"] == "20260514-153012-fix-formatter-underscore-bug"
    assert re.fullmatch(
        r"20260514-153012-fix-formatter-underscore-bug-[0-9a-f]{4}",
        read_metadata(second)["session_id"],
    )
