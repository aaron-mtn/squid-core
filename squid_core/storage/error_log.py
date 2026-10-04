"""Write narrow diagnostic tracebacks to an explicitly selected path."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import traceback


def write_error_log(log_path: Path, exc: BaseException) -> Path:
    """Write the latest error traceback to ``log_path`` and return that path."""
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Session files own agent transcript data. This diagnostic contains only
    # traceback details, never prompts, payloads, tool output, file contents,
    # headers, or API keys.
    timestamp = datetime.now(timezone.utc).isoformat()
    log_path.write_text(
        f"Timestamp: {timestamp}\n\n"
        + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        encoding="utf-8",
    )
    return log_path
