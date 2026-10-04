from pathlib import Path
import sys

import pytest

from squid_core.session import Session
import squid_core.tools.executor as tool_executor_module
from squid_core.tools import ToolExecutor
from squid_core.platform_context import PlatformContext
from squid_core.storage import UndoStore


class FakeUndoStore:
    def __init__(self) -> None:
        self.paths = []
        self.contents = []

    def snapshot_file(self, path: str) -> None:
        self.paths.append(path)
        self.contents.append(Path(path).read_text(encoding="utf-8"))

    def has_snapshot(self, path: str) -> bool:
        return path in self.paths

    def discard_snapshot(self, path: str) -> bool:
        if path not in self.paths:
            return False
        position = self.paths.index(path)
        self.paths.pop(position)
        self.contents.pop(position)
        return True

    def discard_snapshot_if_unchanged(self, path: str) -> bool:
        if path not in self.paths:
            return False
        position = self.paths.index(path)
        if Path(path).read_text(encoding="utf-8") != self.contents[position]:
            return False
        return self.discard_snapshot(path)


def _patch(executor: ToolExecutor, path: Path, old_text: str, new_text: str) -> dict:
    """Issue a normal apply_patch action through the run-local executor."""
    return executor.execute(
        {
            "tool": "apply_patch",
            "args": {
                "path": str(path),
                "old_text": old_text,
                "new_text": new_text,
            },
        }
    )


def _select(executor: ToolExecutor, match_id: str) -> dict:
    """Select one diagnostic candidate without reconstructing the patch."""
    return executor.execute(
        {"tool": "apply_patch", "args": {"match_id": match_id}}
    )


def test_write_file_snapshots_target_before_write(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    undo_store = FakeUndoStore()
    executor = ToolExecutor(
        repo_root=tmp_path,
        session=Session(repo_root=tmp_path),
        undo_store=undo_store,
    )

    result = executor.execute(
        {
            "tool": "write_file",
            "args": {"path": str(target), "content": "after"},
        }
    )

    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "after"
    assert undo_store.paths == [str(target)]
    assert undo_store.contents == ["before"]


def test_successful_apply_patch_snapshots_and_records_file_written(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    undo_store = FakeUndoStore()
    session = Session(repo_root=tmp_path)
    executor = ToolExecutor(
        repo_root=tmp_path,
        session=session,
        undo_store=undo_store,
    )

    result = executor.execute(
        {
            "tool": "apply_patch",
            "args": {"path": str(target), "old_text": "before", "new_text": "after"},
        }
    )

    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "after"
    assert session.files_written == {str(target)}
    assert undo_store.paths == [str(target)]
    assert undo_store.contents == ["before"]


def test_unique_apply_patch_remains_backward_compatible(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before middle after", encoding="utf-8")
    executor = ToolExecutor(tmp_path, Session(repo_root=tmp_path))

    result = _patch(executor, target, "middle", "center")

    assert result["ok"] is True
    assert "candidates" not in result
    assert target.read_text(encoding="utf-8") == "before center after"


@pytest.mark.parametrize(
    ("candidate_index", "expected"),
    [
        (0, "changed different same"),
        (1, "same different changed"),
    ],
)
def test_duplicate_candidate_selection_edits_only_selected_occurrence(
    tmp_path: Path,
    candidate_index: int,
    expected: str,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("same different same", encoding="utf-8")
    executor = ToolExecutor(tmp_path, Session(repo_root=tmp_path))

    diagnostic = _patch(executor, target, "same", "changed")

    assert diagnostic["error"] == "old_text_not_unique"
    assert [candidate["match_id"] for candidate in diagnostic["candidates"]] == [
        "m1",
        "m2",
    ]
    result = _select(executor, diagnostic["candidates"][candidate_index]["match_id"])
    assert result["ok"] is True
    assert result["before_preview"] == "same"
    assert result["after_preview"] == "changed"
    assert target.read_text(encoding="utf-8") == expected


def test_candidate_selection_accepts_matching_redundant_path(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("same different same", encoding="utf-8")
    executor = ToolExecutor(tmp_path, Session(repo_root=tmp_path))
    diagnostic = _patch(executor, target, "same", "stored replacement")

    result = executor.execute(
        {
            "tool": "apply_patch",
            "args": {
                "match_id": diagnostic["candidates"][0]["match_id"],
                "path": str(target),
            },
        }
    )

    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "stored replacement different same"


def test_candidate_selection_ignores_redundant_patch_text(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("same different same", encoding="utf-8")
    executor = ToolExecutor(tmp_path, Session(repo_root=tmp_path))
    diagnostic = _patch(executor, target, "same", "stored replacement")

    result = executor.execute(
        {
            "tool": "apply_patch",
            "args": {
                "match_id": diagnostic["candidates"][0]["match_id"],
                "path": str(target),
                "old_text": "different",
                "new_text": "caller replacement",
            },
        }
    )

    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "stored replacement different same"


@pytest.mark.parametrize("match_id", ["unknown", ""])
def test_unusable_candidate_id_with_complete_normal_args_uses_exact_patch(
    tmp_path: Path,
    match_id: str,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    undo_store = FakeUndoStore()
    executor = ToolExecutor(
        tmp_path,
        Session(repo_root=tmp_path),
        undo_store=undo_store,
    )

    result = executor.execute(
        {
            "tool": "apply_patch",
            "args": {
                "match_id": match_id,
                "path": str(target),
                "old_text": "before",
                "new_text": "after",
            },
        }
    )

    assert result["ok"] is True
    assert "match_id" not in result
    assert target.read_text(encoding="utf-8") == "after"
    assert undo_store.paths == [str(target)]
    assert undo_store.contents == ["before"]


@pytest.mark.parametrize("match_id", ["unknown", ""])
def test_unusable_candidate_id_without_complete_normal_args_is_unavailable(
    tmp_path: Path,
    match_id: str,
) -> None:
    executor = ToolExecutor(tmp_path, Session(repo_root=tmp_path))

    result = executor.execute(
        {"tool": "apply_patch", "args": {"match_id": match_id}}
    )

    assert result == {
        "ok": False,
        "tool": "apply_patch",
        "error": "patch_candidate_unavailable",
        "match_id": match_id,
    }


def test_candidate_selection_rejects_conflicting_redundant_path(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    other = tmp_path / "other.txt"
    target.write_text("same different same", encoding="utf-8")
    other.write_text("untouched", encoding="utf-8")
    executor = ToolExecutor(tmp_path, Session(repo_root=tmp_path))
    diagnostic = _patch(executor, target, "same", "stored replacement")

    result = executor.execute(
        {
            "tool": "apply_patch",
            "args": {
                "match_id": diagnostic["candidates"][0]["match_id"],
                "path": str(other),
            },
        }
    )

    assert result["ok"] is False
    assert result["error"] == "patch_candidate_path_conflict"
    assert result["message"] == "Supplied path does not match the stored candidate target."
    assert target.read_text(encoding="utf-8") == "same different same"
    assert other.read_text(encoding="utf-8") == "untouched"


def test_close_match_candidate_replaces_exact_current_text(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    original = "header\nalpha = 1\nbeta = 20\ngamma = 3\nfooter\n"
    target.write_text(original, encoding="utf-8")
    executor = ToolExecutor(tmp_path, Session(repo_root=tmp_path))

    diagnostic = _patch(
        executor,
        target,
        "alpha = 1\nbeta = 2\ngamma = 3",
        "replacement\n",
    )

    assert diagnostic["error"] == "old_text_not_found"
    assert diagnostic["candidates"] == [
        {
            "match_id": "m1",
            "start_line": 2,
            "end_line": 4,
            "text": "alpha = 1\nbeta = 20\ngamma = 3\n",
            "similarity": pytest.approx(0.982, abs=0.001),
        }
    ]
    result = _select(executor, "m1")
    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "header\nreplacement\nfooter\n"


def test_low_confidence_close_match_does_not_create_candidate(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    executor = ToolExecutor(tmp_path, Session(repo_root=tmp_path))

    result = _patch(executor, target, "completely\nunrelated", "replacement")

    assert result["error"] == "old_text_not_found"
    assert "candidates" not in result
    assert _select(executor, "m1")["error"] == "patch_candidate_unavailable"


def test_unknown_and_other_run_candidate_ids_are_unavailable(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("same different same", encoding="utf-8")
    first_run = ToolExecutor(tmp_path, Session(repo_root=tmp_path))
    second_run = ToolExecutor(tmp_path, Session(repo_root=tmp_path))
    diagnostic = _patch(first_run, target, "same", "changed")

    assert _select(first_run, "unknown")["error"] == "patch_candidate_unavailable"
    assert diagnostic["candidates"][0]["match_id"] == "m1"
    assert _select(second_run, "m1")["error"] == "patch_candidate_unavailable"
    assert target.read_text(encoding="utf-8") == "same different same"


def test_successful_same_file_mutation_invalidates_candidates(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("same middle same", encoding="utf-8")
    executor = ToolExecutor(tmp_path, Session(repo_root=tmp_path))
    diagnostic = _patch(executor, target, "same", "changed")

    successful = _patch(executor, target, "middle", "center")

    assert successful["ok"] is True
    assert _select(executor, diagnostic["candidates"][0]["match_id"])["error"] == (
        "patch_candidate_unavailable"
    )


def test_successful_same_file_write_invalidates_candidates(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("same different same", encoding="utf-8")
    executor = ToolExecutor(tmp_path, Session(repo_root=tmp_path))
    diagnostic = _patch(executor, target, "same", "changed")

    written = executor.execute(
        {"tool": "write_file", "args": {"path": str(target), "content": "replaced"}}
    )

    assert written["ok"] is True
    assert _select(executor, diagnostic["candidates"][0]["match_id"])["error"] == (
        "patch_candidate_unavailable"
    )


def test_different_file_mutation_preserves_candidates(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    other = tmp_path / "other.txt"
    target.write_text("same different same", encoding="utf-8")
    other.write_text("before", encoding="utf-8")
    executor = ToolExecutor(tmp_path, Session(repo_root=tmp_path))
    diagnostic = _patch(executor, target, "same", "changed")

    other_result = executor.execute(
        {"tool": "write_file", "args": {"path": str(other), "content": "after"}}
    )
    selected = _select(executor, diagnostic["candidates"][1]["match_id"])

    assert other_result["ok"] is True
    assert selected["ok"] is True
    assert target.read_text(encoding="utf-8") == "same different changed"


def test_stale_candidate_is_rejected_at_original_location(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("same different same", encoding="utf-8")
    executor = ToolExecutor(tmp_path, Session(repo_root=tmp_path))
    diagnostic = _patch(executor, target, "same", "changed")
    target.write_text("prefix same different same", encoding="utf-8")

    result = _select(executor, diagnostic["candidates"][0]["match_id"])

    assert result["error"] == "patch_candidate_stale"
    assert target.read_text(encoding="utf-8") == "prefix same different same"


def test_candidate_is_stale_if_target_disappears(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("same different same", encoding="utf-8")
    executor = ToolExecutor(tmp_path, Session(repo_root=tmp_path))
    diagnostic = _patch(executor, target, "same", "changed")
    target.unlink()

    result = _select(executor, diagnostic["candidates"][0]["match_id"])

    assert result["error"] == "patch_candidate_stale"


def test_failed_mutation_does_not_invalidate_unrelated_candidate(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    other = tmp_path / "other.txt"
    target.write_text("same different same", encoding="utf-8")
    other.write_text("untouched", encoding="utf-8")
    executor = ToolExecutor(tmp_path, Session(repo_root=tmp_path))
    diagnostic = _patch(executor, target, "same", "changed")

    failed = _patch(executor, other, "missing", "replacement")
    selected = _select(executor, diagnostic["candidates"][0]["match_id"])

    assert failed["ok"] is False
    assert selected["ok"] is True
    assert target.read_text(encoding="utf-8") == "changed different same"


def test_candidate_selection_snapshots_original_target(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("same different same", encoding="utf-8")
    undo_store = FakeUndoStore()
    executor = ToolExecutor(
        tmp_path,
        Session(repo_root=tmp_path),
        undo_store=undo_store,
    )
    diagnostic = _patch(executor, target, "same", "changed")

    result = _select(executor, diagnostic["candidates"][0]["match_id"])

    assert result["ok"] is True
    assert undo_store.paths == [str(target)]
    assert undo_store.contents == ["same different same"]


def test_successful_write_file_produces_usable_undo_state(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    undo_store = UndoStore.create(tmp_path / ".undo", workspace_root=tmp_path)
    executor = ToolExecutor(
        repo_root=tmp_path,
        session=Session(repo_root=tmp_path),
        undo_store=undo_store,
    )

    result = executor.execute(
        {
            "tool": "write_file",
            "args": {"path": str(target), "content": "after"},
        }
    )

    assert result["ok"] is True
    assert len(undo_store.load_index()) == 1
    undo_store.restore(workspace_root=tmp_path)
    assert target.read_text(encoding="utf-8") == "before"


def test_failed_write_file_discards_new_undo_state(tmp_path: Path) -> None:
    target = tmp_path / "missing-parent" / "target.txt"
    undo_store = UndoStore.create(tmp_path / ".undo", workspace_root=tmp_path)
    executor = ToolExecutor(
        repo_root=tmp_path,
        session=Session(repo_root=tmp_path),
        undo_store=undo_store,
    )

    result = executor.execute(
        {
            "tool": "write_file",
            "args": {"path": str(target), "content": "after"},
        }
    )

    assert result["ok"] is False
    assert not target.exists()
    assert undo_store.load_index() == []
    assert list(undo_store.files_dir.iterdir()) == []


def test_successful_apply_patch_produces_usable_undo_state(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    undo_store = UndoStore.create(tmp_path / ".undo", workspace_root=tmp_path)
    executor = ToolExecutor(
        repo_root=tmp_path,
        session=Session(repo_root=tmp_path),
        undo_store=undo_store,
    )

    result = executor.execute(
        {
            "tool": "apply_patch",
            "args": {"path": str(target), "old_text": "before", "new_text": "after"},
        }
    )

    assert result["ok"] is True
    assert len(undo_store.load_index()) == 1
    undo_store.restore(workspace_root=tmp_path)
    assert target.read_text(encoding="utf-8") == "before"


def test_failed_apply_patch_discards_new_undo_state(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    undo_store = UndoStore.create(tmp_path / ".undo", workspace_root=tmp_path)
    executor = ToolExecutor(
        repo_root=tmp_path,
        session=Session(repo_root=tmp_path),
        undo_store=undo_store,
    )

    result = executor.execute(
        {
            "tool": "apply_patch",
            "args": {"path": str(target), "old_text": "missing", "new_text": "after"},
        }
    )

    assert result["ok"] is False
    assert target.read_text(encoding="utf-8") == "before"
    assert undo_store.load_index() == []
    assert list(undo_store.files_dir.iterdir()) == []


def test_failed_mutation_preserves_previous_successful_undo_state(tmp_path: Path) -> None:
    changed = tmp_path / "changed.txt"
    unchanged = tmp_path / "unchanged.txt"
    changed.write_text("original", encoding="utf-8")
    unchanged.write_text("untouched", encoding="utf-8")
    undo_store = UndoStore.create(tmp_path / ".undo", workspace_root=tmp_path)
    executor = ToolExecutor(
        repo_root=tmp_path,
        session=Session(repo_root=tmp_path),
        undo_store=undo_store,
    )

    successful_result = executor.execute(
        {
            "tool": "write_file",
            "args": {"path": str(changed), "content": "changed"},
        }
    )
    failed_result = executor.execute(
        {
            "tool": "apply_patch",
            "args": {"path": str(unchanged), "old_text": "missing", "new_text": "replacement"},
        }
    )

    assert successful_result["ok"] is True
    assert failed_result["ok"] is False
    assert [record["path"] for record in undo_store.load_index()] == [str(changed)]
    undo_store.restore(workspace_root=tmp_path)
    assert changed.read_text(encoding="utf-8") == "original"
    assert unchanged.read_text(encoding="utf-8") == "untouched"


def test_mutation_exception_retains_snapshot_for_possible_partial_write(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    undo_store = UndoStore.create(tmp_path / ".undo", workspace_root=tmp_path)

    def partially_write_then_raise(_repo_root: Path, **args) -> dict:
        Path(args["path"]).write_text("partial", encoding="utf-8")
        raise OSError("write interrupted")

    executor = ToolExecutor(
        repo_root=tmp_path,
        session=Session(repo_root=tmp_path),
        undo_store=undo_store,
        tools={"write_file": {"execute": partially_write_then_raise}},
    )

    result = executor.execute(
        {
            "tool": "write_file",
            "args": {"path": str(target), "content": "after"},
        }
    )

    assert result["ok"] is False
    assert target.read_text(encoding="utf-8") == "partial"
    assert len(undo_store.load_index()) == 1
    undo_store.restore(workspace_root=tmp_path)
    assert target.read_text(encoding="utf-8") == "before"


def test_read_file_and_run_command_do_not_snapshot(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("content", encoding="utf-8")
    undo_store = FakeUndoStore()
    executor = ToolExecutor(
        repo_root=tmp_path,
        session=Session(repo_root=tmp_path),
        undo_store=undo_store,
    )

    read_result = executor.execute(
        {
            "tool": "read_file",
            "args": {"path": str(target)},
        }
    )
    command_result = executor.execute(
        {
            "tool": "run_command",
            "args": {
                "command": "$true" if sys.platform == "win32" else "true",
                "verification": False,
            },
        }
    )

    assert read_result["ok"] is True
    assert command_result["ok"] is True
    assert command_result["verification"] is False
    assert undo_store.paths == []


def test_blocked_read_file_does_not_call_tool(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    calls = []

    def fake_execute(repo_root: Path, **args) -> dict:
        calls.append((repo_root, args))
        return {"ok": True, "tool": "read_file", "path": args["path"]}

    monkeypatch.setattr(
        tool_executor_module,
        "TOOLS",
        {"read_file": {"execute": fake_execute}},
    )
    executor = ToolExecutor(
        repo_root=workspace,
        session=Session(repo_root=workspace),
    )

    result = executor.execute(
        {
            "tool": "read_file",
            "args": {"path": str(outside)},
        }
    )

    assert result["ok"] is False
    assert "outside the workspace root" in result["error"]
    assert calls == []


def test_blocked_write_file_does_not_snapshot_or_call_tool(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    undo_store = FakeUndoStore()
    calls = []

    def fake_execute(repo_root: Path, **args) -> dict:
        calls.append((repo_root, args))
        return {"ok": True, "tool": "write_file", "path": args["path"]}

    monkeypatch.setattr(
        tool_executor_module,
        "TOOLS",
        {"write_file": {"execute": fake_execute}},
    )
    executor = ToolExecutor(
        repo_root=workspace,
        session=Session(repo_root=workspace),
        undo_store=undo_store,
    )

    result = executor.execute(
        {
            "tool": "write_file",
            "args": {"path": str(outside), "content": "outside"},
        }
    )

    assert result["ok"] is False
    assert "outside the workspace root" in result["error"]
    assert undo_store.paths == []
    assert calls == []


def test_apply_patch_respects_workspace_policy(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    undo_store = FakeUndoStore()
    calls = []

    def fake_execute(repo_root: Path, **args) -> dict:
        calls.append((repo_root, args))
        return {"ok": True, "tool": "apply_patch", "path": args["path"]}

    monkeypatch.setattr(
        tool_executor_module,
        "TOOLS",
        {"apply_patch": {"execute": fake_execute}},
    )
    executor = ToolExecutor(
        repo_root=workspace,
        session=Session(repo_root=workspace),
        undo_store=undo_store,
    )

    result = executor.execute(
        {
            "tool": "apply_patch",
            "args": {"path": str(outside), "old_text": "old", "new_text": "new"},
        }
    )

    assert result["ok"] is False
    assert "outside the workspace root" in result["error"]
    assert undo_store.paths == []
    assert calls == []


def test_run_command_is_not_restricted_by_workspace_boundary(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls = []

    def fake_execute(repo_root: Path, **args) -> dict:
        calls.append((repo_root, args))
        return {"ok": True, "tool": "run_command", "command": args["command"]}

    monkeypatch.setattr(
        tool_executor_module,
        "TOOLS",
        {"run_command": {"execute": fake_execute}},
    )
    executor = ToolExecutor(
        repo_root=tmp_path,
        session=Session(repo_root=tmp_path),
    )

    result = executor.execute(
        {
            "tool": "run_command",
            "args": {"command": "cat /etc/hosts"},
        }
    )

    assert result["ok"] is True
    assert calls == [(tmp_path, {"command": "cat /etc/hosts"})]


def test_only_builtin_run_command_receives_platform_context(monkeypatch, tmp_path: Path) -> None:
    context = PlatformContext("Windows", "PowerShell", "pwsh", "\\", True)
    calls = []

    def builtin_command(repo_root: Path, **args) -> dict:
        calls.append(("command", args))
        return {"ok": True, "tool": "run_command"}

    def other_tool(repo_root: Path, **args) -> dict:
        calls.append(("other", args))
        return {"ok": True, "tool": "other"}

    monkeypatch.setattr(tool_executor_module, "BUILTIN_RUN_COMMAND", builtin_command)
    executor = ToolExecutor(
        repo_root=tmp_path,
        session=Session(repo_root=tmp_path),
        tools={
            "run_command": {"execute": builtin_command},
            "other": {"execute": other_tool},
        },
        platform_context=context,
    )

    executor.execute({"tool": "run_command", "args": {"command": "Get-ChildItem"}})
    executor.execute({"tool": "other", "args": {"value": 1}})

    assert calls[0][0] == "command"
    assert calls[0][1]["command"] == "Get-ChildItem"
    assert calls[0][1]["platform_context"] == context
    assert calls[0][1]["should_cancel"]() is False
    assert calls[1] == ("other", {"value": 1})


def test_allow_outside_workspace_allows_outside_file_paths_through(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside_read = tmp_path / "outside-read.txt"
    outside_write = tmp_path / "outside-write.txt"
    workspace.mkdir()
    calls = []

    def fake_execute(repo_root: Path, **args) -> dict:
        calls.append((repo_root, args))
        return {"ok": True, "path": args["path"]}

    monkeypatch.setattr(
        tool_executor_module,
        "TOOLS",
        {
            "read_file": {"execute": fake_execute},
            "write_file": {"execute": fake_execute},
            "apply_patch": {"execute": fake_execute},
        },
    )
    executor = ToolExecutor(
        repo_root=workspace,
        session=Session(repo_root=workspace),
        allow_outside_workspace=True,
    )

    result = executor.execute(
        {
            "tool": "read_file",
            "args": {"path": str(outside_read)},
        }
    )
    write_result = executor.execute(
        {
            "tool": "write_file",
            "args": {"path": str(outside_write), "content": "outside"},
        }
    )
    patch_result = executor.execute(
        {
            "tool": "apply_patch",
            "args": {"path": str(outside_write), "old_text": "old", "new_text": "new"},
        }
    )

    assert result["ok"] is True
    assert write_result["ok"] is True
    assert patch_result["ok"] is True
    assert calls == [
        (workspace, {"path": str(outside_read)}),
        (workspace, {"path": str(outside_write), "content": "outside"}),
        (workspace, {"path": str(outside_write), "old_text": "old", "new_text": "new"}),
    ]


def test_workspace_boundary_approval_allows_outside_file_path_through(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    calls = []

    def fake_execute(repo_root: Path, **args) -> dict:
        calls.append((repo_root, args))
        return {"ok": True, "tool": "read_file", "path": args["path"]}

    monkeypatch.setattr(
        tool_executor_module,
        "TOOLS",
        {"read_file": {"execute": fake_execute}},
    )
    executor = ToolExecutor(
        repo_root=workspace,
        session=Session(repo_root=workspace),
    )

    result = executor.execute(
        {
            "tool": "read_file",
            "args": {"path": str(outside)},
            "outside_workspace_approved": True,
        }
    )

    assert result["ok"] is True
    assert calls == [(workspace, {"path": str(outside)})]
