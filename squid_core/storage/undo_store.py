"""Store the latest-only Squid Core undo buffer on disk."""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from squid_core.storage.atomic_json import atomic_write_json


class UndoStore:
    """Persist one latest-only undo buffer at an explicit path."""

    def __init__(self, undo_dir: Path) -> None:
        self.undo_dir = undo_dir
        self.metadata_path = undo_dir / "metadata.json"
        self.index_path = undo_dir / "index.json"
        self.files_dir = undo_dir / "files"

    @classmethod
    def create(
        cls,
        undo_dir: Path,
        session_id: str | None = None,
        *,
        workspace_root: Path | None = None,
    ) -> "UndoStore":
        """Replace and initialize the latest-only buffer at ``undo_dir``."""

        undo_dir = Path(undo_dir)
        if undo_dir.exists():
            shutil.rmtree(undo_dir)

        files_dir = undo_dir / "files"
        files_dir.mkdir(parents=True)

        now = _utc_now()
        store = cls(undo_dir)
        metadata: dict[str, Any] = {
            "created_at": now,
            "updated_at": now,
            "session_id": session_id,
            "status": "active",
        }
        if workspace_root is not None:
            metadata["repo_root"] = str(Path(workspace_root).resolve())
        store._write_metadata(metadata)
        store._write_index([])
        return store

    @classmethod
    def open(cls, undo_dir: Path) -> "UndoStore":
        """Open an existing undo buffer at ``undo_dir``."""

        undo_dir = Path(undo_dir)
        store = cls(undo_dir)

        if not store.metadata_path.is_file():
            raise FileNotFoundError(f"Missing undo metadata: {store.metadata_path}")

        if not store.index_path.is_file():
            raise FileNotFoundError(f"Missing undo index: {store.index_path}")

        if not store.files_dir.is_dir():
            raise FileNotFoundError(f"Missing undo files folder: {store.files_dir}")

        return store

    # Capture the file's current bytes once.
    #
    # Repeated calls for the same path return the original record so one undo
    # buffer keeps the first pre-change state for each file.
    def snapshot_file(self, path: str) -> dict:
        original_path = Path(path)
        if not original_path.is_absolute():
            raise ValueError(f"Undo snapshots require an absolute path: {path}")

        index = self.load_index()
        for record in index:
            if record.get("path") == path:
                return record

        if original_path.exists():
            snapshot_name = f"{len(index) + 1:06d}"
            snapshot_path = self.files_dir / snapshot_name
            shutil.copyfile(original_path, snapshot_path)
            record = {
                "path": path,
                "existed": True,
                "snapshot_path": f"files/{snapshot_name}",
            }
        else:
            record = {
                "path": path,
                "existed": False,
                "snapshot_path": None,
            }

        index.append(record)
        self._write_index(index)
        self._touch_metadata()
        return record

    def load_index(self) -> list[dict]:
        with self.index_path.open(encoding="utf-8") as file:
            return json.load(file)

    def has_snapshot(self, path: str) -> bool:
        return any(record.get("path") == path for record in self.load_index())

    def discard_snapshot(self, path: str) -> bool:
        index = self.load_index()

        for position, record in enumerate(index):
            if record.get("path") != path:
                continue

            snapshot_path = record.get("snapshot_path")
            if snapshot_path is not None:
                (self.undo_dir / snapshot_path).unlink(missing_ok=True)

            index.pop(position)
            self._write_index(index)
            self._touch_metadata()
            return True

        return False

    def discard_snapshot_if_unchanged(self, path: str) -> bool:
        record = next(
            (record for record in self.load_index() if record.get("path") == path),
            None,
        )
        if record is None:
            return False

        original_path = Path(path)
        if record["existed"]:
            try:
                snapshot_path = self.undo_dir / record["snapshot_path"]
                unchanged = original_path.read_bytes() == snapshot_path.read_bytes()
            except OSError:
                return False
        else:
            unchanged = not original_path.exists()

        if not unchanged:
            return False
        return self.discard_snapshot(path)

    def restore(self, *, workspace_root: Path) -> list[dict]:
        """Restore one active buffer owned by ``workspace_root`` exactly once.

        Every record is validated before the buffer is claimed or any target is
        touched. Once claimed, a failed or interrupted restore remains in the
        ``restoring`` state: replaying a partly applied buffer could overwrite
        user work created after the failure.
        """

        metadata = self._read_metadata()
        if not isinstance(metadata, dict):
            raise ValueError("Undo metadata must contain an object")
        if metadata.get("status") != "active":
            raise ValueError(
                "Undo buffer is not available: "
                f"status is {metadata.get('status')!r}"
            )

        current_root = _resolve_path(Path(workspace_root).expanduser(), "workspace")
        recorded_root_value = metadata.get("repo_root")
        if not isinstance(recorded_root_value, str) or not recorded_root_value:
            raise ValueError("Undo buffer has no valid recorded workspace root")
        recorded_root_path = Path(recorded_root_value).expanduser()
        if not recorded_root_path.is_absolute():
            raise ValueError("Undo buffer has a non-absolute recorded workspace root")
        recorded_root = _resolve_path(recorded_root_path, "recorded workspace")
        if recorded_root != current_root:
            raise ValueError(
                "Undo buffer belongs to a different workspace: "
                f"recorded {recorded_root}, current {current_root}"
            )

        records = self._validate_restore_records(current_root)

        # Claim the buffer durably only after all validation succeeds, but before
        # target mutation. A crash or partial failure must never make it replayable.
        metadata["status"] = "restoring"
        metadata["updated_at"] = _utc_now()
        self._write_metadata(metadata)

        results = []
        for record, original_path, snapshot_path in records:
            result = {
                "path": record["path"],
                "restored": False,
                "deleted": False,
            }

            if record["existed"]:
                assert snapshot_path is not None
                original_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(snapshot_path, original_path)
                result["restored"] = True
            elif original_path.exists():
                original_path.unlink()
                result["deleted"] = True

            results.append(result)

        metadata["status"] = "restored"
        metadata["updated_at"] = _utc_now()
        self._write_metadata(metadata)
        return results

    def _validate_restore_records(
        self, workspace_root: Path
    ) -> list[tuple[dict, Path, Path | None]]:
        """Validate every target and snapshot before restoration mutates files."""

        index = self.load_index()
        if not isinstance(index, list):
            raise ValueError("Undo index must contain a list of records")

        files_root = _resolve_path(self.files_dir, "undo files folder")
        validated = []
        for record in index:
            if not isinstance(record, dict):
                raise ValueError("Undo index contains an invalid record")

            path_value = record.get("path")
            if not isinstance(path_value, str) or not path_value:
                raise ValueError("Undo record has no valid target path")
            original_path = Path(path_value)
            if not original_path.is_absolute():
                raise ValueError(f"Undo target is not an absolute path: {path_value}")
            resolved_target = _resolve_path(original_path, "undo target")
            if not resolved_target.is_relative_to(workspace_root):
                raise ValueError(
                    f"Undo target is outside the current workspace: {path_value}"
                )

            existed = record.get("existed")
            if not isinstance(existed, bool):
                raise ValueError(
                    f"Undo record has invalid existence state: {path_value}"
                )

            snapshot_path = None
            snapshot_value = record.get("snapshot_path")
            if existed:
                if not isinstance(snapshot_value, str) or not snapshot_value:
                    raise ValueError(f"Undo record has no valid snapshot: {path_value}")
                snapshot_path = _resolve_path(
                    self.undo_dir / snapshot_value, "undo snapshot"
                )
                if not snapshot_path.is_relative_to(files_root):
                    raise ValueError(
                        f"Undo snapshot is outside its files folder: {path_value}"
                    )
                if not snapshot_path.is_file():
                    raise FileNotFoundError(f"Missing undo snapshot: {snapshot_path}")
            elif snapshot_value is not None:
                raise ValueError(
                    f"Undo record for a newly created file has a snapshot: {path_value}"
                )

            validated.append((record, original_path, snapshot_path))

        return validated

    def _read_metadata(self) -> dict[str, Any]:
        with self.metadata_path.open(encoding="utf-8") as file:
            return json.load(file)

    def _write_metadata(self, metadata: dict[str, Any]) -> None:
        atomic_write_json(self.metadata_path, metadata)

    def _write_index(self, index: list[dict]) -> None:
        atomic_write_json(self.index_path, index)

    def _touch_metadata(self) -> None:
        metadata = self._read_metadata()
        metadata["updated_at"] = _utc_now()
        self._write_metadata(metadata)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_path(path: Path, description: str) -> Path:
    """Resolve a persisted path with an explicit validation failure."""

    try:
        return path.resolve()
    except (OSError, RuntimeError) as error:
        raise ValueError(f"Unable to resolve {description}: {path}") from error
