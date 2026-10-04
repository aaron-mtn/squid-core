"""Tests for request-only trimming of older canonical tool results."""

import copy
from pathlib import Path
from types import SimpleNamespace

from squid_core.agent_loop import AgentLoop
from squid_core.hooks import (
    HookRunner,
    ReadResultTrimmedContext,
    RepeatedReadNudge,
    ToolResultContext,
)
from squid_core.settings import ToolResultTrimmingSettings
from squid_core.tool_result_trimming import (
    render_messages_for_model,
    render_tool_result_for_model,
)
from squid_core.tools import ToolResults


def _tool_turn(call_id: str, name: str, result: dict) -> list[dict]:
    action = {
        "tool": name,
        "tool_call_id": call_id,
        "args": {},
    }
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                }
            ],
        },
        ToolResults().message_from_action_result(action, result),
    ]


def _tool_batch(calls: list[tuple[str, str, dict]]) -> list[dict]:
    """Build one assistant turn with all corresponding Tool results."""
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": "{}"},
            }
            for call_id, name, _result in calls
        ],
    }
    tool_results = ToolResults()
    return [
        assistant,
        *[
            tool_results.message_from_action_result(
                {"tool": name, "tool_call_id": call_id, "args": {}},
                result,
            )
            for call_id, name, result in calls
        ],
    ]


def _after_exposure(messages: list[dict]) -> list[dict]:
    """Add the later assistant turn that proves a Tool batch was exposed."""
    return [*messages, {"role": "assistant", "content": "continue"}]


def _observation(message: dict) -> str:
    return message["content"]


def _read_result(
    path: str,
    content: str,
    *,
    start_line: int,
    end_line: int,
    returned_line_ranges: list[dict] | None = None,
) -> dict:
    result = {
        "ok": True,
        "tool": "read_file",
        "path": path,
        "content": content,
        "start_line": start_line,
        "end_line": end_line,
    }
    if returned_line_ranges is not None:
        result["returned_line_ranges"] = returned_line_ranges
    return result


def _observe_read(
    runner: HookRunner,
    path: str,
    call_id: str,
    result: dict,
):
    return runner.tool_result(
        ToolResultContext(
            session_id=None,
            repo_root=str(Path(path).parent),
            action={
                "tool": "read_file",
                "tool_call_id": call_id,
                "args": {"path": path},
            },
            result=result,
        )
    )


def _apply_trim_event(runner: HookRunner, event: ReadResultTrimmedContext) -> None:
    runner.read_result_trimmed(event)


def test_newest_results_are_textual_without_changing_pairing() -> None:
    messages = [
        *_tool_turn(
            "old",
            "read_file",
            _read_result("C:/repo/old.py", "old\n", start_line=1, end_line=1),
        ),
        *_tool_turn(
            "recent-1",
            "read_file",
            _read_result("C:/repo/new.py", "alpha\nbeta\n", start_line=7, end_line=8),
        ),
        *_tool_turn(
            "recent-2",
            "run_command",
            {"ok": True, "tool": "run_command", "output": "b" * 40},
        ),
    ]
    settings = ToolResultTrimmingSettings(
        recent_full_results=2,
        read_old_content_chars=10,
    )

    rendered = render_messages_for_model(messages, settings)

    assert rendered[-3]["content"] == (
        "READ_FILE OK\npath: C:/repo/new.py\n\n"
        "<<< CODE SQUID SOURCE: new.py lines 7-8 >>>\nalpha\nbeta"
    )
    assert rendered[-1]["content"].startswith("RUN_COMMAND OK\n\n<<< OUTPUT >>>")
    assert rendered[-1]["tool_call_id"] == messages[-1]["tool_call_id"]


def test_large_recent_spreadsheet_read_keeps_content_on_first_exposure() -> None:
    content = (
        "Workbook: spreadsheet (xlsx)\n"
        "Sheets (2): Orders, Summary\n\n"
        "<<< SHEET: Orders >>>\n"
        "Used range: A1:J101\n"
        + "2\tPB-100001\t[formula: =G2*H2]\n" * 300
    )
    canonical_result = {
        "ok": True,
        "tool": "read_file",
        "path": "C:/repo/orders.xlsx",
        "media_type": "spreadsheet",
        "format": "xlsx",
        "sheet_names": ["Orders", "Summary"],
        "content": content,
        "content_truncated": False,
        "content_original_chars": len(content),
        "content_returned_chars": len(content),
        "total_lines": len(content.splitlines()),
        "ranged": False,
    }
    messages = _tool_turn("read", "read_file", canonical_result)
    before = copy.deepcopy(messages)

    rendered = render_messages_for_model(
        messages,
        ToolResultTrimmingSettings(recent_full_results=0, read_old_content_chars=0),
    )

    assert content.rstrip() in rendered[-1]["content"]
    assert "Orders, Summary" in rendered[-1]["content"]
    assert "[formula: =G2*H2]" in rendered[-1]["content"]
    assert messages == before


def test_old_range_less_read_content_still_uses_the_configured_bound() -> None:
    content = "Workbook: spreadsheet (ods)\n" + "sheet payload\n" * 100
    canonical_result = {
        "ok": True,
        "tool": "read_file",
        "path": "C:/repo/orders.ods",
        "media_type": "spreadsheet",
        "content": content,
        "content_truncated": False,
        "total_lines": len(content.splitlines()),
    }
    messages = _after_exposure(_tool_turn("read", "read_file", canonical_result))
    before = copy.deepcopy(messages)

    rendered = render_messages_for_model(
        messages,
        ToolResultTrimmingSettings(
            recent_full_results=0,
            read_old_content_chars=80,
        ),
    )

    observation = rendered[-2]["content"]
    assert observation.startswith(
        "READ_FILE OK\npath: C:/repo/orders.ods\ntruncated: yes\n\n<<< CONTENT >>>"
    )
    assert "[older payload truncated]" in observation
    assert len(observation) < len(content)
    assert messages == before


def test_large_current_batch_preserves_first_exposure_for_mixed_results() -> None:
    read_content = "first source line\nsecond source line\n"
    calls = [
        (
            "read-early",
            "read_file",
            _read_result("C:/repo/a.py", read_content, start_line=1, end_line=2),
        ),
        *[
            (
                f"command-{index}",
                "run_command",
                {"ok": True, "tool": "run_command", "output": f"output {index}"},
            )
            for index in range(5)
        ],
    ]
    messages = _tool_batch(calls)
    settings = ToolResultTrimmingSettings(
        recent_full_results=2,
        read_old_content_chars=0,
        command_old_output_chars=0,
    )

    rendered = render_messages_for_model(messages, settings)

    assert read_content.rstrip() in rendered[1]["content"]
    assert "SOURCE OMITTED FROM OLDER RESULT" not in rendered[1]["content"]
    assert "output 0" in rendered[2]["content"]
    assert [message["tool_call_id"] for message in rendered[1:]] == [
        call_id for call_id, _name, _result in calls
    ]


def test_exposed_large_batch_becomes_eligible_for_normal_trimming() -> None:
    calls = [
        (
            f"command-{index}",
            "run_command",
            {"ok": True, "tool": "run_command", "output": f"payload {index}"},
        )
        for index in range(4)
    ]
    settings = ToolResultTrimmingSettings(
        recent_full_results=2,
        command_old_output_chars=0,
    )

    first_exposure = render_messages_for_model(_tool_batch(calls), settings)
    later_request = render_messages_for_model(
        _after_exposure(_tool_batch(calls)),
        settings,
    )

    assert "payload 0" in first_exposure[1]["content"]
    assert "[older command output omitted; rerun if needed]" in later_request[1][
        "content"
    ]
    assert "payload 2" in later_request[3]["content"]


def test_single_tool_turn_keeps_existing_recent_result_semantics() -> None:
    messages = [
        *_tool_turn(
            "older",
            "run_command",
            {"ok": True, "tool": "run_command", "output": "older payload"},
        ),
        *_tool_turn(
            "current",
            "run_command",
            {"ok": True, "tool": "run_command", "output": "current payload"},
        ),
    ]

    rendered = render_messages_for_model(
        messages,
        ToolResultTrimmingSettings(
            recent_full_results=1,
            command_old_output_chars=0,
        ),
    )

    assert "[older command output omitted; rerun if needed]" in rendered[1]["content"]
    assert "current payload" in rendered[3]["content"]


def test_failed_batch_keeps_skipped_pairing_without_payload_expansion() -> None:
    calls = [
        (
            "failed",
            "run_command",
            {"ok": False, "tool": "run_command", "error": "process failed"},
        ),
        (
            "skipped",
            "read_file",
            {
                "ok": False,
                "tool": "read_file",
                "error": "earlier_action_failed",
                "reason": "an earlier tool action in this batch did not succeed",
                "skipped": True,
            },
        ),
    ]

    rendered = render_messages_for_model(
        _tool_batch(calls),
        ToolResultTrimmingSettings(recent_full_results=0),
    )

    assert rendered[1]["tool_call_id"] == "failed"
    assert rendered[2]["tool_call_id"] == "skipped"
    assert rendered[2]["content"].startswith("READ_FILE FAILED")
    assert "reason: earlier_action_failed" in rendered[2]["content"]


def test_large_read_batch_keeps_coverage_truthful_until_later_trimming(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "a.py")
    content = "alpha\nbeta\n"
    read = _read_result(path, content, start_line=1, end_line=2)
    runner = HookRunner([RepeatedReadNudge()])
    _observe_read(runner, path, "read-first", read)
    masked = _observe_read(runner, path, "read-again", read).updated_tool_output
    calls = [
        ("read-first", "read_file", read),
        *[
            (
                f"command-{index}",
                "run_command",
                {"ok": True, "tool": "run_command", "output": str(index)},
            )
            for index in range(5)
        ],
        ("read-again", "read_file", masked),
    ]
    settings = ToolResultTrimmingSettings(
        recent_full_results=2,
        read_old_content_chars=0,
    )

    first_request = render_messages_for_model(
        _tool_batch(calls),
        settings,
        on_read_result_trimmed=lambda event: _apply_trim_event(runner, event),
    )

    assert content.rstrip() in first_request[1]["content"]
    assert first_request[-1]["content"].startswith("READ_FILE SKIPPED")
    assert _observe_read(runner, path, "still-covered", read).updated_tool_output[
        "already_covered"
    ] is True

    render_messages_for_model(
        _after_exposure(_tool_batch(calls)),
        settings,
        on_read_result_trimmed=lambda event: _apply_trim_event(runner, event),
    )

    assert _observe_read(runner, path, "after-trim", read).updated_tool_output == read


def test_disjoint_read_regions_have_exact_headers_and_gap_marker() -> None:
    result = _read_result(
        "C:/repo/script.js",
        "one\ntwo\n<<< CODE SQUID: lines 3-7 omitted from this read >>>\neight\nnine\n",
        start_line=1,
        end_line=9,
        returned_line_ranges=[
            {"start_line": 1, "end_line": 2},
            {"start_line": 8, "end_line": 9},
        ],
    )

    observation = _observation(
        render_messages_for_model(
            _tool_turn("read", "read_file", result),
            ToolResultTrimmingSettings(),
        )[-1]
    )

    assert "<<< CODE SQUID SOURCE: script.js lines 1-2 >>>\none\ntwo" in observation
    assert "<<< CODE SQUID OMITTED: script.js lines 3-7 >>>" in observation
    assert "<<< CODE SQUID SOURCE: script.js lines 8-9 >>>\neight\nnine" in observation


def test_contiguous_read_does_not_gain_a_fake_gap_marker() -> None:
    result = _read_result(
        "C:/repo/script.js",
        "four\nfive\nsix\n",
        start_line=4,
        end_line=6,
    )

    observation = _observation(
        render_messages_for_model(
            _tool_turn("read", "read_file", result),
            ToolResultTrimmingSettings(),
        )[-1]
    )

    assert "<<< CODE SQUID SOURCE: script.js lines 4-6 >>>" in observation
    assert "CODE SQUID OMITTED" not in observation


def test_partial_and_suppressed_reads_are_clear_text_observations() -> None:
    partial = _read_result(
        "C:/repo/script.js",
        "fresh\n",
        start_line=501,
        end_line=501,
        returned_line_ranges=[{"start_line": 501, "end_line": 501}],
    )
    partial.update(
        {
            "partially_redundant": True,
            "omitted_line_ranges": [{"start_line": 420, "end_line": 500}],
        }
    )
    skipped = {
        "ok": True,
        "tool": "read_file",
        "path": "C:/repo/script.js",
        "already_covered": True,
        "start_line": 420,
        "end_line": 620,
        "returned_line_ranges": [{"start_line": 420, "end_line": 620}],
        "message": "The previous result is still current.",
    }

    partial_observation = _observation(
        render_messages_for_model(
            _tool_turn("partial", "read_file", partial),
            ToolResultTrimmingSettings(),
        )[-1]
    )
    skipped_observation = _observation(
        render_messages_for_model(
            _tool_turn("skipped", "read_file", skipped),
            ToolResultTrimmingSettings(),
        )[-1]
    )

    assert partial_observation.startswith("READ_FILE PARTIAL")
    assert (
        "<<< CODE SQUID OMITTED: script.js lines 420-500 already available unchanged >>>"
        in partial_observation
    )
    assert "<<< CODE SQUID SOURCE: script.js lines 501-501 >>>\nfresh" in partial_observation
    assert skipped_observation.startswith("READ_FILE SKIPPED")
    assert "lines: 420-620" in skipped_observation
    assert "reason: unchanged source already available in context" in skipped_observation


def test_recent_patch_failure_keeps_recovery_identifiers_and_text() -> None:
    result = {
        "ok": False,
        "tool": "apply_patch",
        "path": "C:/repo/example.py",
        "error": "old_text_not_found",
        "candidates": [
            {
                "match_id": "m7",
                "start_line": 40,
                "end_line": 42,
                "text": "current\nsource\ntext\n",
                "similarity": 0.94,
            }
        ],
    }

    observation = _observation(
        render_messages_for_model(
            _tool_turn("patch", "apply_patch", result),
            ToolResultTrimmingSettings(),
        )[-1]
    )

    assert "reason: old_text_not_found" in observation
    assert "<<< CANDIDATE 1 m7 lines 40-42 >>>" in observation
    assert "current\nsource\ntext" in observation


def test_old_read_content_is_bounded_with_exact_source_identity() -> None:
    content = "".join(f"line {line:02}\n" for line in range(1, 21))
    canonical_result = {
        "ok": True,
        "tool": "read_file",
        "path": "C:/repo/example.py",
        "content": content,
        "content_truncated": True,
        "content_original_chars": 400,
        "content_returned_chars": len(content),
        "start_line": 1,
        "end_line": 20,
        "total_lines": 40,
        "returned_line_ranges": [{"start_line": 1, "end_line": 20}],
    }
    messages = _after_exposure(_tool_turn("read", "read_file", canonical_result))

    rendered = render_messages_for_model(
        messages,
        ToolResultTrimmingSettings(
            recent_full_results=0,
            read_old_content_chars=len("\n[older payload truncated]\n") + 16,
        ),
    )
    observation = _observation(rendered[-2])

    assert observation.startswith("READ_FILE OK\npath: C:/repo/example.py\ntruncated: yes")
    assert "<<< CODE SQUID SOURCE: example.py lines 1-1 >>>\nline 01" in observation
    assert "<<< CODE SQUID SOURCE: example.py lines 20-20 >>>\nline 20" in observation
    assert "<<< CODE SQUID OMITTED: example.py lines 2-19 older result content trimmed >>>" in observation
    assert canonical_result["content"] == content


def test_zero_read_limit_omits_source_but_preserves_location() -> None:
    messages = _tool_turn(
        "read",
        "read_file",
        {
            "ok": True,
            "tool": "read_file",
            "path": "C:/repo/secret.py",
                "content": "seven\neight\nnine\n",
            "start_line": 7,
            "end_line": 9,
            "total_lines": 12,
        },
    )
    messages = _after_exposure(messages)

    observation = _observation(
        render_messages_for_model(
            messages,
            ToolResultTrimmingSettings(recent_full_results=0, read_old_content_chars=0),
        )[-2]
    )

    assert "seven" not in observation
    assert "SOURCE OMITTED FROM OLDER RESULT" in observation
    assert "path: C:/repo/secret.py" in observation


def test_old_command_output_is_bounded_with_failure_metadata() -> None:
    messages = _tool_turn(
        "command",
        "run_command",
        {
            "ok": False,
            "tool": "run_command",
            "command": "pytest",
            "output": "failure\n" * 100,
            "returncode": 2,
            "timed_out": False,
            "failure_class": "process_failed",
            "shell": "powershell",
            "platform": "windows",
        },
    )
    messages = _after_exposure(messages)

    observation = _observation(
        render_messages_for_model(
            messages,
            ToolResultTrimmingSettings(
                recent_full_results=0, command_old_output_chars=45
            ),
        )[-2]
    )

    assert observation.startswith(
        "RUN_COMMAND FAILED\ncommand: pytest\nexit_code: 2\n"
        "failure_class: process_failed\ntruncated: yes"
    )
    assert "<<< OUTPUT >>>\n" in observation
    assert "[older payload truncated]" in observation


def test_command_denial_keeps_its_failure_reason() -> None:
    observation = _observation(
        render_messages_for_model(
            _tool_turn(
                "command",
                "run_command",
                {
                    "ok": False,
                    "tool": "run_command",
                    "error": "user denied tool action",
                },
            ),
            ToolResultTrimmingSettings(),
        )[-1]
    )

    assert observation == "RUN_COMMAND FAILED\nreason: user denied tool action"


def test_old_failed_patch_bounds_diagnostics_without_losing_candidate_ids() -> None:
    messages = _tool_turn(
        "patch",
        "apply_patch",
        {
            "ok": False,
            "tool": "apply_patch",
            "path": "C:/repo/example.py",
            "error": "old_text_not_unique",
            "candidates": [
                {
                    "match_id": "m1",
                    "start_line": 10,
                    "end_line": 12,
                    "context": "large diagnostic " * 100,
                    "text": "candidate source " * 100,
                }
            ],
        },
    )
    messages = _after_exposure(messages)

    observation = _observation(
        render_messages_for_model(
            messages,
            ToolResultTrimmingSettings(
                recent_full_results=0, patch_old_diagnostic_chars=35
            ),
        )[-2]
    )
    assert observation.startswith(
        "APPLY_PATCH FAILED\npath: C:/repo/example.py\nreason: old_text_not_unique"
    )
    assert "<<< CANDIDATE 1 m1 lines 10-12 >>>" in observation
    assert "[older payload truncated]" in observation
    assert "<<< OLDER PATCH DIAGNOSTIC TRUNCATED >>>" in observation


def test_successful_patch_preview_is_visible_to_the_model() -> None:
    observation = _observation(
        render_messages_for_model(
            _tool_turn(
                "patch",
                "apply_patch",
                {
                    "ok": True,
                    "tool": "apply_patch",
                    "path": "C:/repo/example.py",
                    "start_line": 2,
                    "end_line_after": 2,
                    "before_preview": "old exact text",
                    "after_preview": "new exact text",
                },
            ),
            ToolResultTrimmingSettings(),
        )[-1]
    )

    assert observation.endswith(
        "before_preview: old exact text\nafter_preview: new exact text"
    )


def test_old_successful_patch_drops_only_previews_without_mutating_result() -> None:
    canonical_result = {
        "ok": True,
        "tool": "apply_patch",
        "path": "C:/repo/example.py",
        "start_line": 2,
        "end_line_after": 3,
        "match_id": "m1",
        "before_preview": "old exact text",
        "after_preview": "new exact text",
    }
    before = copy.deepcopy(canonical_result)

    rendered_result = render_tool_result_for_model(
        canonical_result,
        is_recent=False,
        settings=ToolResultTrimmingSettings(recent_full_results=0),
    )

    observation = _observation(
        render_messages_for_model(
            _after_exposure(_tool_turn("patch", "apply_patch", canonical_result)),
            ToolResultTrimmingSettings(recent_full_results=0),
        )[-2]
    )

    assert observation == (
        "APPLY_PATCH OK\npath: C:/repo/example.py\nchanged: lines 2-3\nmatch_id: m1"
    )
    assert rendered_result == {
        "ok": True,
        "tool": "apply_patch",
        "path": "C:/repo/example.py",
        "start_line": 2,
        "end_line_after": 3,
        "match_id": "m1",
    }
    assert canonical_result == before


def test_rendering_never_mutates_canonical_messages_or_results() -> None:
    canonical_result = {
        "ok": True,
        "tool": "read_file",
        "path": "C:/repo/example.py",
        "content": "unchanged canonical source" * 20,
    }
    messages = _tool_turn("read", "read_file", canonical_result)
    before = copy.deepcopy(messages)

    rendered = render_messages_for_model(
        messages,
        ToolResultTrimmingSettings(recent_full_results=0, read_old_content_chars=10),
    )

    assert messages == before
    assert canonical_result["content"] == "unchanged canonical source" * 20
    assert rendered[-1] is not messages[-1]


def test_agent_loop_sends_trimmed_view_but_keeps_session_history_canonical() -> None:
    messages = _tool_turn(
        "read",
        "read_file",
        {"ok": True, "tool": "read_file", "content": "canonical source" * 50},
    )
    messages = _after_exposure(messages)
    before = copy.deepcopy(messages)

    class RecordingAdapter:
        def respond(self, messages: list[dict], tool_definitions: list[dict]) -> dict:
            self.messages = messages
            return {"message": {"role": "assistant", "content": "done"}}

    adapter = RecordingAdapter()
    loop = AgentLoop(
        session=SimpleNamespace(messages=messages, repo_root="C:/repo"),
        model_adapter=adapter,
        tool_executor=object(),
        tool_results=ToolResults(),
        agent=object(),
        display=object(),
        action_preflight=object(),
        tool_result_trimming=ToolResultTrimmingSettings(
            recent_full_results=0, read_old_content_chars=25
        ),
    )

    loop._respond([])

    assert messages == before
    assert adapter.messages[-2]["content"].startswith("READ_FILE OK")
    assert "<<< CONTENT >>>" in adapter.messages[-2]["content"]
    assert "[older payload truncated" in adapter.messages[-2]["content"]


def test_agent_loop_sends_complete_recent_range_less_read_to_adapter() -> None:
    content = "Workbook: spreadsheet (xlsx)\n" + "formula: =A1*B1\n" * 500
    messages = _tool_turn(
        "read",
        "read_file",
        {
            "ok": True,
            "tool": "read_file",
            "path": "C:/repo/orders.xlsx",
            "media_type": "spreadsheet",
            "content": content,
            "content_truncated": False,
            "total_lines": len(content.splitlines()),
        },
    )
    before = copy.deepcopy(messages)

    class RecordingAdapter:
        def respond(self, messages: list[dict], tool_definitions: list[dict]) -> dict:
            self.messages = messages
            return {"message": {"role": "assistant", "content": "done"}}

    adapter = RecordingAdapter()
    loop = AgentLoop(
        session=SimpleNamespace(messages=messages, repo_root="C:/repo"),
        model_adapter=adapter,
        tool_executor=object(),
        tool_results=ToolResults(),
        agent=object(),
        display=object(),
        action_preflight=object(),
        tool_result_trimming=ToolResultTrimmingSettings(
            recent_full_results=0,
            read_old_content_chars=0,
        ),
    )

    loop._respond([])

    assert content.rstrip() in adapter.messages[-1]["content"]
    assert messages == before


def test_agent_loop_exposes_entire_just_completed_batch_once() -> None:
    calls = [
        (
            "read-early",
            "read_file",
            _read_result("C:/repo/a.py", "needed source\n", start_line=1, end_line=1),
        ),
        *[
            (
                f"command-{index}",
                "run_command",
                {"ok": True, "tool": "run_command", "output": f"result {index}"},
            )
            for index in range(4)
        ],
    ]
    messages = _tool_batch(calls)
    before = copy.deepcopy(messages)

    class RecordingAdapter:
        def respond(self, messages: list[dict], tool_definitions: list[dict]) -> dict:
            self.messages = messages
            return {"message": {"role": "assistant", "content": "done"}}

    adapter = RecordingAdapter()
    loop = AgentLoop(
        session=SimpleNamespace(messages=messages, repo_root="C:/repo"),
        model_adapter=adapter,
        tool_executor=object(),
        tool_results=ToolResults(),
        agent=object(),
        display=object(),
        action_preflight=object(),
        tool_result_trimming=ToolResultTrimmingSettings(
            recent_full_results=1,
            read_old_content_chars=0,
            command_old_output_chars=0,
        ),
    )

    loop._respond([])

    assert "needed source" in adapter.messages[1]["content"]
    assert "result 0" in adapter.messages[2]["content"]
    assert messages == before


def test_disabled_trimming_still_uses_textual_model_format() -> None:
    messages = _tool_turn(
        "read",
        "read_file",
        _read_result("C:/repo/full.py", "full source\n", start_line=1, end_line=1),
    )

    rendered = render_messages_for_model(
        messages,
        ToolResultTrimmingSettings(enabled=False, recent_full_results=0),
    )

    assert rendered[0] == messages[0]
    assert rendered[-1]["content"].endswith(
        "<<< CODE SQUID SOURCE: full.py lines 1-1 >>>\nfull source"
    )


def test_trimming_preserves_assistant_tool_result_pairing() -> None:
    messages = _tool_turn(
        "paired-call",
        "run_command",
        {"ok": True, "tool": "run_command", "output": "x" * 100},
    )
    messages = _after_exposure(messages)

    rendered = render_messages_for_model(
        messages,
        ToolResultTrimmingSettings(
            recent_full_results=0, command_old_output_chars=20
        ),
    )

    assert rendered[0]["tool_calls"][0]["id"] == "paired-call"
    assert rendered[1]["tool_call_id"] == "paired-call"
    assert "RUN_COMMAND OK" in rendered[1]["content"]
    assert "truncated: yes" in rendered[1]["content"]


def test_old_write_payload_is_omitted_when_present_but_status_survives() -> None:
    messages = _tool_turn(
        "write",
        "write_file",
        {
            "ok": True,
            "tool": "write_file",
            "path": "C:/repo/example.py",
            "characters_written": 500,
            "content": "written source" * 40,
        },
    )

    observation = _observation(
        render_messages_for_model(
            messages,
            ToolResultTrimmingSettings(recent_full_results=0),
        )[-1]
    )

    assert observation == "WRITE_FILE OK\npath: C:/repo/example.py"


def test_unknown_tools_are_left_unchanged() -> None:
    messages = _tool_turn(
        "future",
        "future_tool",
        {"ok": True, "tool": "future_tool", "payload": "x" * 10_000},
    )

    rendered = render_messages_for_model(
        messages,
        ToolResultTrimmingSettings(recent_full_results=0),
    )

    assert rendered == messages
    assert rendered[-1] is messages[-1]


def test_untrimmed_old_read_remains_covered(tmp_path: Path) -> None:
    path = str(tmp_path / "script.js")
    content = "".join(f"line {line:03}\n" for line in range(1, 11))
    result = _read_result(path, content, start_line=1, end_line=10)
    runner = HookRunner([RepeatedReadNudge()])
    _observe_read(runner, path, "read-1", result)

    render_messages_for_model(
        _after_exposure(_tool_turn("read-1", "read_file", result)),
        ToolResultTrimmingSettings(
            recent_full_results=0,
            read_old_content_chars=len(content),
        ),
        on_read_result_trimmed=lambda event: _apply_trim_event(runner, event),
    )

    assert _observe_read(runner, path, "read-2", result).updated_tool_output[
        "already_covered"
    ] is True


def test_fully_trimmed_read_can_be_read_again_and_restores_coverage(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "script.js")
    lines = [f"line {line:03}\n" for line in range(1, 501)]
    result = _read_result(path, "".join(lines), start_line=1, end_line=500)
    runner = HookRunner([RepeatedReadNudge()])
    _observe_read(runner, path, "read-1", result)
    _observe_read(runner, path, "read-redundant", result)
    assert runner.hooks[0]._redundant_counts

    render_messages_for_model(
        _after_exposure(_tool_turn("read-1", "read_file", result)),
        ToolResultTrimmingSettings(recent_full_results=0, read_old_content_chars=0),
        on_read_result_trimmed=lambda event: _apply_trim_event(runner, event),
    )

    assert runner.hooks[0]._redundant_counts == {}
    recovery = _read_result(path, "".join(lines[:250]), start_line=1, end_line=250)
    reread = _observe_read(runner, path, "read-2", recovery)
    assert reread.updated_tool_output == recovery
    assert _observe_read(runner, path, "read-3", recovery).updated_tool_output[
        "already_covered"
    ] is True


def test_partial_read_retention_updates_only_visible_line_coverage(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "script.js")
    content = "".join(f"line {line:03}\n" for line in range(1, 11))
    result = _read_result(path, content, start_line=1, end_line=10)
    runner = HookRunner([RepeatedReadNudge()])
    hook = runner.hooks[0]
    _observe_read(runner, path, "read-1", result)
    marker_chars = len("\n[older payload truncated]\n")

    render_messages_for_model(
        _after_exposure(_tool_turn("read-1", "read_file", result)),
        ToolResultTrimmingSettings(
            recent_full_results=0,
            # Two complete nine-character source lines survive at each end.
            read_old_content_chars=marker_chars + 36,
        ),
        on_read_result_trimmed=lambda event: _apply_trim_event(runner, event),
    )

    assert list(hook._coverage.values()) == [[(1, 2), (9, 10)]]
    retained = _read_result(path, content[:18], start_line=1, end_line=2)
    assert _observe_read(runner, path, "read-2", retained).updated_tool_output[
        "already_covered"
    ] is True
    trimmed = _read_result(path, content[18:72], start_line=3, end_line=8)
    assert _observe_read(runner, path, "read-3", trimmed).updated_tool_output == trimmed


def test_trimmed_truncated_full_read_keeps_each_retained_source_range(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "large.py")
    all_lines = [f"line {line:03}\n" for line in range(1, 21)]
    content = "".join((*all_lines[:5], *all_lines[15:]))
    ranges = [
        {"start_line": 1, "end_line": 5},
        {"start_line": 16, "end_line": 20},
    ]
    result = _read_result(
        path,
        content,
        start_line=1,
        end_line=20,
        returned_line_ranges=ranges,
    )
    runner = HookRunner([RepeatedReadNudge()])
    hook = runner.hooks[0]
    _observe_read(runner, path, "read-1", result)

    render_messages_for_model(
        _after_exposure(_tool_turn("read-1", "read_file", result)),
        ToolResultTrimmingSettings(
            recent_full_results=0,
            read_old_content_chars=len("\n[older payload truncated]\n") + 36,
        ),
        on_read_result_trimmed=lambda event: _apply_trim_event(runner, event),
    )

    assert list(hook._coverage.values()) == [[(1, 2), (19, 20)]]
    middle = _read_result(path, "".join(all_lines[2:5]), start_line=3, end_line=5)
    assert _observe_read(runner, path, "read-2", middle).updated_tool_output == middle


def test_trimmed_read_metadata_does_not_preserve_source_coverage(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "script.js")
    content = "".join(f"line {line:03}\n" for line in range(1, 11))
    result = _read_result(path, content, start_line=1, end_line=10)
    runner = HookRunner([RepeatedReadNudge()])
    _observe_read(runner, path, "read-1", result)

    rendered = render_messages_for_model(
        _after_exposure(_tool_turn("read-1", "read_file", result)),
        ToolResultTrimmingSettings(recent_full_results=0, read_old_content_chars=0),
        on_read_result_trimmed=lambda event: _apply_trim_event(runner, event),
    )
    observation = _observation(rendered[-2])

    assert "path: " + path in observation
    assert "SOURCE OMITTED FROM OLDER RESULT" in observation
    assert _observe_read(runner, path, "read-2", result).updated_tool_output == result


def test_trimming_one_read_preserves_overlapping_visible_result(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "script.js")
    lines = [f"line {line:03}\n" for line in range(1, 11)]
    first = _read_result(path, "".join(lines[:5]), start_line=1, end_line=5)
    second = _read_result(path, "".join(lines[3:]), start_line=4, end_line=10)
    runner = HookRunner([RepeatedReadNudge()])
    _observe_read(runner, path, "read-1", first)
    _observe_read(runner, path, "read-2", second)

    render_messages_for_model(
        _after_exposure(_tool_turn("read-1", "read_file", first)),
        ToolResultTrimmingSettings(recent_full_results=0, read_old_content_chars=0),
        on_read_result_trimmed=lambda event: _apply_trim_event(runner, event),
    )

    reread = _observe_read(runner, path, "read-3", second).updated_tool_output
    assert reread["content"] == "".join(lines[3:5])
    assert reread["returned_line_ranges"] == [
        {"start_line": 4, "end_line": 5}
    ]


def test_multi_range_read_omits_covered_regions_but_preserves_novel_regions(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "script.js")
    lines = [f"line {line:03}\n" for line in range(1, 21)]
    first = _read_result(path, "".join(lines[1:4]), start_line=2, end_line=4)
    second_ranges = [
        {"start_line": 2, "end_line": 5},
        {"start_line": 15, "end_line": 17},
    ]
    second = _read_result(
        path,
        "".join((*lines[1:5], *lines[14:17])),
        start_line=2,
        end_line=17,
        returned_line_ranges=second_ranges,
    )
    runner = HookRunner([RepeatedReadNudge()])
    _observe_read(runner, path, "read-1", first)

    shaped = _observe_read(runner, path, "read-2", second).updated_tool_output

    assert shaped["content"] == "".join(
        (
            lines[4],
            "<<< CODE SQUID: lines 6-14 omitted from this read >>>\n",
            *lines[14:17],
        )
    )
    assert shaped["returned_line_ranges"] == [
        {"start_line": 5, "end_line": 5},
        {"start_line": 15, "end_line": 17},
    ]
    assert shaped["omitted_line_ranges"] == [
        {"start_line": 2, "end_line": 4}
    ]


def test_fully_redundant_multi_range_read_is_masked(tmp_path: Path) -> None:
    path = str(tmp_path / "script.js")
    lines = [f"line {line:03}\n" for line in range(1, 21)]
    ranges = [
        {"start_line": 2, "end_line": 4},
        {"start_line": 15, "end_line": 17},
    ]
    result = _read_result(
        path,
        "".join((*lines[1:4], *lines[14:17])),
        start_line=2,
        end_line=17,
        returned_line_ranges=ranges,
    )
    runner = HookRunner([RepeatedReadNudge()])
    _observe_read(runner, path, "read-1", result)

    masked = _observe_read(runner, path, "read-2", result).updated_tool_output

    assert masked["already_covered"] is True
    assert masked["returned_line_ranges"] == ranges
    assert "content" not in masked


def test_trim_notification_stays_aligned_after_patch_line_shift(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "script.js")
    content = "".join(f"line {line:03}\n" for line in range(1, 11))
    result = _read_result(path, content, start_line=1, end_line=10)
    runner = HookRunner([RepeatedReadNudge()])
    _observe_read(runner, path, "read-1", result)
    runner.tool_result(
        ToolResultContext(
            session_id=None,
            repo_root=str(tmp_path),
            action={"tool": "apply_patch", "args": {"path": path}},
            result={
                "ok": True,
                "tool": "apply_patch",
                "path": path,
                "start_line": 3,
                "end_line_before": 3,
                "end_line_after": 4,
            },
        )
    )

    render_messages_for_model(
        _after_exposure(_tool_turn("read-1", "read_file", result)),
        ToolResultTrimmingSettings(
            recent_full_results=0,
            # Retain original lines 1-2 and 9-10; the latter now map to 10-11.
            read_old_content_chars=len("\n[older payload truncated]\n") + 36,
        ),
        on_read_result_trimmed=lambda event: _apply_trim_event(runner, event),
    )

    retained_tail = _read_result(path, "tail a\ntail b\n", start_line=10, end_line=11)
    assert _observe_read(
        runner, path, "read-2", retained_tail
    ).updated_tool_output["already_covered"] is True
    removed_shifted = _read_result(path, "fresh a\nfresh b\n", start_line=5, end_line=6)
    removed_response = _observe_read(runner, path, "read-3", removed_shifted)
    assert removed_response.updated_tool_output == removed_shifted
