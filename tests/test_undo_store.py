"""Tests for the latest-only undo buffer."""

import json
import shutil
from pathlib import Path

import pytest

from squid_core.storage import UndoStore


def read_metadata(store: UndoStore) -> dict:
    return json.loads(store.metadata_path.read_text(encoding="utf-8"))


def test_create_creates_metadata_index_and_files_folder(tmp_path: Path) -> None:
    undo_dir = tmp_path / "undo"
    store = UndoStore.create(
        undo_dir,
        session_id="session-1",
        workspace_root=tmp_path,
    )
    metadata = read_metadata(store)

    assert store.undo_dir == undo_dir
    assert store.metadata_path.is_file()
    assert store.index_path.is_file()
    assert store.files_dir.is_dir()
    assert json.loads(store.index_path.read_text(encoding="utf-8")) == []
    assert metadata["repo_root"] == str(tmp_path.resolve())
    assert metadata["session_id"] == "session-1"
    assert metadata["status"] == "active"
    assert metadata["created_at"]
    assert metadata["updated_at"]


def test_create_replaces_previous_undo_folder(tmp_path: Path) -> None:
    first = UndoStore.create(tmp_path)
    leftover = first.files_dir / "000001"
    leftover.write_bytes(b"old")

    second = UndoStore.create(tmp_path)

    assert second.undo_dir == first.undo_dir
    assert not leftover.exists()
    assert second.load_index() == []


def test_create_without_workspace_omits_application_metadata(tmp_path: Path) -> None:
    store = UndoStore.create(tmp_path / "undo")

    assert "repo_root" not in read_metadata(store)


def test_open_opens_existing_undo_folder(tmp_path: Path) -> None:
    created = UndoStore.create(tmp_path)

    opened = UndoStore.open(tmp_path)

    assert opened.undo_dir == created.undo_dir
    assert opened.metadata_path == created.metadata_path
    assert opened.index_path == created.index_path
    assert opened.files_dir == created.files_dir


def test_open_raises_file_not_found_error_when_required_files_are_missing(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError):
        UndoStore.open(tmp_path)

    missing_index = UndoStore.create(tmp_path)
    missing_index.index_path.unlink()

    with pytest.raises(FileNotFoundError):
        UndoStore.open(tmp_path)

    missing_metadata = UndoStore.create(tmp_path)
    missing_metadata.metadata_path.unlink()

    with pytest.raises(FileNotFoundError):
        UndoStore.open(tmp_path)

    missing_files = UndoStore.create(tmp_path)
    missing_files.files_dir.rmdir()

    with pytest.raises(FileNotFoundError):
        UndoStore.open(tmp_path)


def test_snapshot_file_rejects_relative_paths(tmp_path: Path) -> None:
    store = UndoStore.create(tmp_path)

    with pytest.raises(ValueError):
        store.snapshot_file("relative.py")


def test_snapshot_file_snapshots_existing_file_bytes(tmp_path: Path) -> None:
    store = UndoStore.create(tmp_path)
    target = tmp_path / "target.bin"
    target.write_bytes(b"\x00original\xff")

    record = store.snapshot_file(str(target))

    assert record == {
        "path": str(target),
        "existed": True,
        "snapshot_path": "files/000001",
    }
    assert (store.files_dir / "000001").read_bytes() == b"\x00original\xff"
    assert store.load_index() == [record]


def test_snapshot_file_records_missing_files_with_existed_false(tmp_path: Path) -> None:
    store = UndoStore.create(tmp_path)
    target = tmp_path / "new-file.py"

    record = store.snapshot_file(str(target))

    assert record == {
        "path": str(target),
        "existed": False,
        "snapshot_path": None,
    }
    assert store.load_index() == [record]
    assert list(store.files_dir.iterdir()) == []


def test_snapshot_file_does_not_duplicate_snapshot_for_same_path(tmp_path: Path) -> None:
    store = UndoStore.create(tmp_path)
    target = tmp_path / "target.py"
    target.write_bytes(b"first")

    first = store.snapshot_file(str(target))
    target.write_bytes(b"second")
    second = store.snapshot_file(str(target))

    assert second == first
    assert store.load_index() == [first]
    assert (store.files_dir / "000001").read_bytes() == b"first"
    assert not (store.files_dir / "000002").exists()


def test_load_index_returns_records(tmp_path: Path) -> None:
    store = UndoStore.create(tmp_path)
    target = tmp_path / "target.py"
    target.write_text("content", encoding="utf-8")
    record = store.snapshot_file(str(target))

    assert store.load_index() == [record]


def test_restore_restores_original_bytes_for_existing_files(tmp_path: Path) -> None:
    store = UndoStore.create(tmp_path / "undo", workspace_root=tmp_path)
    target = tmp_path / "target.bin"
    target.write_bytes(b"before")
    store.snapshot_file(str(target))
    target.write_bytes(b"after")

    results = store.restore(workspace_root=tmp_path)

    assert target.read_bytes() == b"before"
    assert results == [
        {
            "path": str(target),
            "restored": True,
            "deleted": False,
        }
    ]


def test_restore_deletes_files_that_did_not_exist_before_snapshot(tmp_path: Path) -> None:
    store = UndoStore.create(tmp_path / "undo", workspace_root=tmp_path)
    target = tmp_path / "new-file.py"
    store.snapshot_file(str(target))
    target.write_text("created later", encoding="utf-8")

    results = store.restore(workspace_root=tmp_path)

    assert not target.exists()
    assert results == [
        {
            "path": str(target),
            "restored": False,
            "deleted": True,
        }
    ]


def test_restore_updates_metadata_status_to_restored(tmp_path: Path) -> None:
    store = UndoStore.create(tmp_path / "undo", workspace_root=tmp_path)
    before = read_metadata(store)["updated_at"]

    store.restore(workspace_root=tmp_path)
    metadata = read_metadata(store)

    assert metadata["status"] == "restored"
    assert metadata["updated_at"] >= before


def test_successful_restore_is_one_shot_and_preserves_later_user_edits(
    tmp_path: Path,
) -> None:
    store = UndoStore.create(tmp_path / "undo", workspace_root=tmp_path)
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    store.snapshot_file(str(target))
    target.write_text("agent edit", encoding="utf-8")

    store.restore(workspace_root=tmp_path)
    target.write_text("later user edit", encoding="utf-8")

    with pytest.raises(ValueError, match="not available"):
        store.restore(workspace_root=tmp_path)

    assert target.read_text(encoding="utf-8") == "later user edit"
    assert read_metadata(store)["status"] == "restored"


def test_second_restore_does_not_delete_manually_recreated_file(tmp_path: Path) -> None:
    store = UndoStore.create(tmp_path / "undo", workspace_root=tmp_path)
    target = tmp_path / "created.txt"
    store.snapshot_file(str(target))
    target.write_text("agent content", encoding="utf-8")

    store.restore(workspace_root=tmp_path)
    assert not target.exists()
    target.write_text("user recreated content", encoding="utf-8")

    with pytest.raises(ValueError, match="not available"):
        store.restore(workspace_root=tmp_path)

    assert target.read_text(encoding="utf-8") == "user recreated content"


def test_moved_workspace_rejects_stale_absolute_targets(tmp_path: Path) -> None:
    old_root = tmp_path / "old-project"
    old_root.mkdir()
    target = old_root / "target.txt"
    target.write_text("before", encoding="utf-8")
    store = UndoStore.create(
        old_root / ".code-squid" / "undo", workspace_root=old_root
    )
    store.snapshot_file(str(target))
    target.write_text("agent edit", encoding="utf-8")

    new_root = tmp_path / "new-project"
    old_root.rename(new_root)
    moved_store = UndoStore.open(new_root / ".code-squid" / "undo")

    with pytest.raises(ValueError, match="different workspace"):
        moved_store.restore(workspace_root=new_root)

    assert (new_root / "target.txt").read_text(encoding="utf-8") == "agent edit"
    assert not old_root.exists()
    assert read_metadata(moved_store)["status"] == "active"


def test_copied_workspace_cannot_mutate_original_workspace(tmp_path: Path) -> None:
    original_root = tmp_path / "original-project"
    original_root.mkdir()
    target = original_root / "target.txt"
    target.write_text("before", encoding="utf-8")
    store = UndoStore.create(
        original_root / ".code-squid" / "undo", workspace_root=original_root
    )
    store.snapshot_file(str(target))
    target.write_text("agent edit", encoding="utf-8")

    copied_root = tmp_path / "copied-project"
    shutil.copytree(original_root, copied_root)
    copied_store = UndoStore.open(copied_root / ".code-squid" / "undo")

    with pytest.raises(ValueError, match="different workspace"):
        copied_store.restore(workspace_root=copied_root)

    assert target.read_text(encoding="utf-8") == "agent edit"
    assert (copied_root / "target.txt").read_text(encoding="utf-8") == "agent edit"


def test_all_targets_are_validated_before_any_restore_mutation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    nonnormalized_outside = workspace / ".." / "outside.txt"
    inside = workspace / "inside.txt"
    inside.write_text("inside before", encoding="utf-8")
    outside.write_text("outside before", encoding="utf-8")
    store = UndoStore.create(workspace / "undo", workspace_root=workspace)
    store.snapshot_file(str(inside))
    store.snapshot_file(str(nonnormalized_outside))
    inside.write_text("inside after", encoding="utf-8")
    outside.write_text("outside after", encoding="utf-8")

    with pytest.raises(ValueError, match="outside the current workspace"):
        store.restore(workspace_root=workspace)

    assert inside.read_text(encoding="utf-8") == "inside after"
    assert outside.read_text(encoding="utf-8") == "outside after"
    assert read_metadata(store)["status"] == "active"


def test_partial_restore_failure_is_not_replayable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = UndoStore.create(workspace / "undo", workspace_root=workspace)
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    for target in (first, second):
        target.write_text("before", encoding="utf-8")
        store.snapshot_file(str(target))
        target.write_text("after", encoding="utf-8")

    original_copyfile = shutil.copyfile
    copy_count = 0

    def fail_second_copy(source: Path, destination: Path) -> str:
        nonlocal copy_count
        copy_count += 1
        if copy_count == 2:
            raise OSError("injected restore failure")
        return str(original_copyfile(source, destination))

    monkeypatch.setattr(
        "squid_core.storage.undo_store.shutil.copyfile", fail_second_copy
    )

    with pytest.raises(OSError, match="injected restore failure"):
        store.restore(workspace_root=workspace)

    assert first.read_text(encoding="utf-8") == "before"
    assert second.read_text(encoding="utf-8") == "after"
    assert read_metadata(store)["status"] == "restoring"
    first.write_text("later user edit", encoding="utf-8")

    with pytest.raises(ValueError, match="not available"):
        store.restore(workspace_root=workspace)

    assert first.read_text(encoding="utf-8") == "later user edit"
