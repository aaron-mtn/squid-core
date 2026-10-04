"""Small atomic JSON file writes for persistence state."""

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


# Windows can briefly deny replacement while another process (for example, an
# indexer or antivirus scanner) has the destination open. Keep this window
# deliberately short: durable permission problems still need to reach callers.
_REPLACE_RETRY_DELAYS = (0.02, 0.04, 0.08)


def _replace_with_permission_retry(source: Path, destination: Path) -> None:
    """Atomically replace ``destination``, retrying only transient denial."""
    for delay in _REPLACE_RETRY_DELAYS:
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            time.sleep(delay)

    # Use one final ordinary replace so an exhausted retry window re-raises the
    # filesystem's original exception type and details without wrapping it.
    os.replace(source, destination)


def atomic_write_json(path: Path, value: Any) -> None:
    """Write indented JSON through a same-directory temporary file."""
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)

    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as file:
            json.dump(value, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        _replace_with_permission_retry(temporary_path, path)
    except BaseException:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            # Cleanup must not hide the serialization or replacement failure
            # that explains why the requested write did not complete.
            pass
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes through a flushed same-directory temporary file."""

    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        _replace_with_permission_retry(temporary_path, path)
    except BaseException:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
