"""Own bounded, nonrecursive workspace inspection for the built-in registry.

The executor supplies the selected workspace; this tool enforces containment
itself so approval settings cannot widen read-only exploration permissions.
It does not search files, read contents, or follow child links.
"""

import heapq
import json
import os
from pathlib import Path
import stat

from squid_core.safety.workspace_boundary import is_inside_workspace


TOOL_NAME = "list_files"
MAX_ENTRIES = 500
MAX_RESPONSE_BYTES = 32_000


def tool_definition() -> dict:
    """Expose immediate directory listing with optional workspace default."""
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "List immediate files and folders, similar to ls or dir. Does not "
                "search recursively. Use an absolute directory path inside the "
                "selected workspace, or omit path to list the workspace root. "
                "Returns names, absolute paths, and types, directories first. "
                "Links are identified without listing their targets. Results are "
                f"limited to {MAX_ENTRIES} entries and {MAX_RESPONSE_BYTES} UTF-8 "
                "JSON bytes; truncated results are explicitly marked."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {
                    "type": "string",
                    "description": "Absolute directory path; defaults to the workspace root",
                }},
                "required": [],
                "additionalProperties": False,
            },
        },
    }


def execute(workspace_root: Path, path: str | None = None) -> dict:
    """List one contained directory, returning normalized errors and limits."""
    def error(message: str) -> dict:
        return {"ok": False, "tool": TOOL_NAME, "error": message}

    if path is not None and (not isinstance(path, str) or not path or "\0" in path):
        return error("path must be a nonempty absolute directory path")
    try:
        requested = Path(path) if path is not None else Path(workspace_root).resolve()
        if not requested.is_absolute():
            return error("list_files requires an absolute path")
        # Resolve before enumeration: outside symlinks, junctions, and linked
        # ancestors must fail even when the lexical path is inside the workspace.
        directory = requested.resolve(strict=True)
        if not is_inside_workspace(directory, workspace_root):
            return error("Requested directory is outside the selected workspace")
        result = {
            "ok": True, "tool": TOOL_NAME, "path": str(directory),
            "entries": [], "truncated": False, "truncation_reason": None,
        }

        def entries():
            # lstat semantics classify links/junctions without touching targets.
            # nsmallest retains only a bounded prefix while sorting the whole
            # directory deterministically, independent of scandir ordering.
            with os.scandir(directory) as children:
                for child in children:
                    info = child.stat(follow_symlinks=False)
                    linked = stat.S_ISLNK(info.st_mode) or bool(
                        getattr(info, "st_file_attributes", 0)
                        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                    )
                    kind = ("link" if linked else "directory" if stat.S_ISDIR(info.st_mode)
                            else "file" if stat.S_ISREG(info.st_mode) else "other")
                    yield {"name": child.name, "path": str(directory / child.name), "type": kind}

        selected = heapq.nsmallest(
            MAX_ENTRIES + 1, entries(),
            key=lambda entry: (entry["type"] != "directory", entry["name"].casefold(), entry["name"]),
        )
        if len(selected) > MAX_ENTRIES:
            result.update(truncated=True, truncation_reason="entry_limit")
        for entry in selected[:MAX_ENTRIES]:
            result["entries"].append(entry)
            # Reserve the longer truncation metadata even for an untruncated
            # prefix, so the complete serialized response stays within budget.
            bounded = {**result, "truncated": True, "truncation_reason": "response_size_limit"}
            if len(json.dumps(bounded, ensure_ascii=True).encode("utf-8")) > MAX_RESPONSE_BYTES:
                result["entries"].pop()
                result.update(truncated=True, truncation_reason="response_size_limit")
                break
        if len(json.dumps(result).encode("utf-8")) > MAX_RESPONSE_BYTES:
            return error("Directory path exceeds the response-size limit")
        return result
    except FileNotFoundError:
        return error("Directory does not exist")
    except NotADirectoryError:
        return error("Requested path is not a directory")
    except PermissionError:
        return error("Access denied while listing directory")
    except (OSError, ValueError, RuntimeError):
        # Do not leak partial contents or external link targets in OS messages.
        return error("Unable to list directory due to a filesystem error")
