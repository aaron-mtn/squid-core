"""Tests for the human-readable model-view transcript projection."""

from pathlib import Path

from squid_core.compaction.compactor import COMPACTED_CONTEXT_PREAMBLE
from squid_core.storage import SessionStore
from squid_core.storage.model_transcript import render_model_transcript


def _request(index: int, timestamp: str, messages: list[dict]) -> dict:
    return {
        "request_index": index,
        "timestamp": timestamp,
        "provider": "test-provider",
        "model": "test-model",
        "messages": messages,
        "tools": [],
    }


def test_transcript_deduplicates_requests_and_orders_model_visible_changes() -> None:
    system = {"role": "system", "content": "system rules"}
    user = {"role": "user", "content": "initial task"}
    assistant = {
        "role": "assistant",
        "content": "I will inspect it.",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
            }
        ],
    }
    tool_result = {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "READ_FILE OK\nsource text",
    }
    hook_context = {
        "role": "user",
        "content": "# Additional Context For This Turn\n\n## TooManyReadsNudge\nEdit now.",
    }
    final = {"role": "assistant", "content": "Done."}
    requests = [
        _request(1, "2026-01-01T00:00:01+00:00", [system, user]),
        _request(
            2,
            "2026-01-01T00:00:03+00:00",
            [system, user, assistant, tool_result, hook_context],
        ),
        # A transport retry resends the same accumulated request and must add no
        # duplicate context blocks to the readable projection.
        _request(
            3,
            "2026-01-01T00:00:03.500000+00:00",
            [system, user, assistant, tool_result, hook_context],
        ),
    ]
    entries = [
        {
            "type": "model_reply",
            "timestamp": "2026-01-01T00:00:02+00:00",
            "reply": {
                "assistant": {
                    "reasoning": "Need source first.",
                    "content": "I will inspect it.",
                    "raw_tool_calls": assistant["tool_calls"],
                }
            },
        },
        {
            "type": "hook_execution",
            "timestamp": "2026-01-01T00:00:02.500000+00:00",
            "hook": "NoEffectHook",
            "seam": "before_model_turn",
        },
        {
            "type": "model_reply",
            "timestamp": "2026-01-01T00:00:04+00:00",
            "reply": {"assistant": {"content": "Done.", "raw_tool_calls": []}},
        },
        {
            "type": "message",
            "timestamp": "2026-01-01T00:00:05+00:00",
            "message": final,
        },
    ]

    transcript = render_model_transcript(entries, requests)

    assert transcript.count("system rules") == 1
    assert transcript.count("initial task") == 1
    assert transcript.count("Need source first.") == 1
    assert transcript.count("I will inspect it.") == 1
    assert transcript.count("read_file") == 1
    assert transcript.count("READ_FILE OK") == 1
    assert transcript.count("TooManyReadsNudge") == 1
    assert "NoEffectHook" not in transcript
    assert transcript.count("Done.") == 1
    assert "==================== FINAL ====================" in transcript
    assert transcript.index("Need source first.") < transcript.index("read_file")
    assert transcript.index("read_file") < transcript.index("READ_FILE OK")
    assert transcript.index("READ_FILE OK") < transcript.index("TooManyReadsNudge")


def test_compaction_is_an_explicit_transformation_with_exact_inserted_context() -> None:
    system = {"role": "system", "content": "system"}
    user = {"role": "user", "content": "task"}
    old = {"role": "assistant", "content": "old work"}
    inserted = f"{COMPACTED_CONTEXT_PREAMBLE}\nsummary text\n\nauthoritative facts"
    requests = [
        _request(1, "2026-01-01T00:00:01+00:00", [system, user, old]),
        _request(
            2,
            "2026-01-01T00:00:03+00:00",
            [
                {"role": "system", "content": "summarizer-only prompt"},
                {"role": "user", "content": "summarize old work"},
            ],
        ),
        _request(
            3,
            "2026-01-01T00:00:07+00:00",
            [system, user, {"role": "assistant", "content": inserted}],
        ),
    ]
    entries = [
        {
            "type": "compaction_request",
            "timestamp": "2026-01-01T00:00:02+00:00",
        },
        {
            "type": "compaction_reasoning",
            "timestamp": "2026-01-01T00:00:04+00:00",
            "content": "Condense the history.",
        },
        {
            "type": "compaction_summary",
            "timestamp": "2026-01-01T00:00:05+00:00",
            "content": "summary text",
        },
        {
            "type": "compaction_applied",
            "timestamp": "2026-01-01T00:00:06+00:00",
        },
    ]

    transcript = render_model_transcript(entries, requests)

    assert "CONTEXT TRANSFORMATION" in transcript
    assert "[COMPACTION]" in transcript
    assert "[SUMMARY INSERTED]" in transcript
    assert inserted in transcript
    assert transcript.count("summary text") == 1
    assert "Condense the history." in transcript
    assert "summarizer-only prompt" not in transcript


def test_session_store_writes_transcript_beside_existing_logs(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path, "readable transcript")

    store.append_model_view(
        {
            "provider": "test-provider",
            "model": "test-model",
            "messages": [{"role": "user", "content": "hello model"}],
            "tools": [],
        }
    )

    assert store.model_transcript_path.parent == store.session_dir
    assert store.model_transcript_path.name == "model-transcript.txt"
    assert "hello model" in store.model_transcript_path.read_text(encoding="utf-8")
    assert store.jsonl_path.exists()
    assert store.model_view_path.exists()
