"""Own bounded recursive filename search for Core's built-in tool registry.

The executor supplies the workspace; this tool owns traversal and containment,
independently of approval policy. Applications own tool preferences and Skill
exposure. This module neither reads contents nor changes other file tools.
"""

import fnmatch
import json
import os
from pathlib import Path
import stat

from squid_core.safety.workspace_boundary import is_inside_workspace


TOOL_NAME = "find_files"
MAX_RESULTS = 500
MAX_VISITED_ENTRIES = 20_000
MAX_DIRECTORIES = 1_000
MAX_RESPONSE_BYTES = 32_000
MAX_PATTERN_CHARS = 256
EXCLUDED_DIRECTORIES = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
})


def tool_definition() -> dict:
    """Expose a single filename pattern and optional absolute search root."""
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "Find files recursively by name or filename pattern, similar to find. "
                "Use this tool when you know all or part of a filename but don't know "
                "where it is located. Searches filenames, not file contents. Use "
                "list_files to inspect a specific directory. Matching is case-sensitive "
                "on all platforms; only * and ? wildcards are supported. Skips links "
                "and common generated directories. Search is workspace-contained and "
                f"bounded to {MAX_VISITED_ENTRIES} entries, {MAX_DIRECTORIES} directories, "
                f"{MAX_RESULTS} results and {MAX_RESPONSE_BYTES} UTF-8 JSON bytes. "
                "Truncated results do not represent a complete search."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string", "minLength": 1, "maxLength": MAX_PATTERN_CHARS,
                        "description": "Filename or pattern, e.g. README.md, *.py, or *session*; no path separators",
                    },
                    "path": {
                        "type": "string",
                        "description": "Absolute search directory; defaults to the workspace root",
                    },
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    }


def _is_link(info: os.stat_result) -> bool:
    """Treat Windows reparse points conservatively, including junctions."""
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _has_linked_ancestor(path: Path, workspace: Path) -> bool:
    """Reject linked path components as well as the final directory itself.

    An explicit root or a queued descendant can have an ordinary final component
    under a linked ancestor. Checking only the leaf would still traverse it.
    The selected workspace is already canonicalized by the caller.
    """
    for component in (path, *path.parents):
        if _is_link(component.lstat()):
            return True
        if component == workspace:
            return False
    return False


def execute(workspace_root: Path, pattern: str | None = None, path: str | None = None) -> dict:
    """Return bounded matches; descendant failures produce explicitly partial results."""
    def error(message: str) -> dict:
        # OS exception strings may contain external targets or arbitrary amounts
        # of user-controlled text, so failures use fixed messages throughout.
        return {"ok": False, "tool": TOOL_NAME, "error": message}

    if (not isinstance(pattern, str) or not pattern or len(pattern) > MAX_PATTERN_CHARS
            or pattern in {".", ".."} or any(char in pattern for char in "/\\\0[]")):
        return error("pattern must be a filename pattern of 1-256 characters using only * and ? wildcards, without paths or brackets")
    if path is not None and (not isinstance(path, str) or not path or "\0" in path):
        return error("path must be a nonempty absolute directory path")
    try:
        workspace = Path(workspace_root).resolve(strict=True)
        requested = Path(path) if path is not None else workspace
        if not requested.is_absolute():
            return error("find_files requires an absolute path")
        root = requested.resolve(strict=True)
        if not is_inside_workspace(root, workspace):
            return error("Requested directory is outside the selected workspace")
        if _has_linked_ancestor(requested, workspace):
            return error("Search root must not be a symbolic link or junction")
        if not root.is_dir():
            return error("Requested path is not a directory")
    except FileNotFoundError:
        return error("Search directory does not exist")
    except NotADirectoryError:
        return error("Requested path is not a directory")
    except PermissionError:
        return error("Access denied to search directory")
    except (OSError, ValueError, RuntimeError):
        return error("Unable to access search directory due to a filesystem error")

    files = []
    reasons = set()
    pending = [root]
    visited_entries = 0
    visited_directories = 0
    stop = False
    while pending and not stop:
        if visited_directories >= MAX_DIRECTORIES:
            reasons.add("directory_limit")
            break
        directory = pending.pop()
        visited_directories += 1
        try:
            # Recheck queued descendants immediately before scanning. Resolving
            # containment alone would allow internal links; lstat also prevents
            # following a directory replaced by a link since it was queued.
            if _has_linked_ancestor(directory, workspace) or not is_inside_workspace(directory, workspace):
                reasons.add("traversal_error")
                continue
            children = []
            try:
                with os.scandir(directory) as scan:
                    for child in scan:
                        if visited_entries >= MAX_VISITED_ENTRIES:
                            reasons.add("entry_limit")
                            stop = True
                            break
                        visited_entries += 1
                        children.append(child)
            except (OSError, ValueError, RuntimeError):
                if directory == root and not children:
                    return error("Unable to list search directory due to a filesystem or access error")
                # Iterator failure must retain the prefix already enumerated,
                # as well as matches collected in other directories.
                reasons.add("traversal_error")
            # Sorting is bounded by the entry budget, including when a huge
            # directory has no matches. An interrupted scan's collected prefix
            # is still processed, but is never presented as exhaustive.
            children.sort(key=lambda child: child.name)
            subdirectories = []
            for child in children:
                try:
                    info = child.stat(follow_symlinks=False)
                    if _is_link(info):
                        continue
                    candidate = Path(child.path)
                    if _has_linked_ancestor(candidate, workspace) or not is_inside_workspace(candidate, workspace):
                        reasons.add("traversal_error")
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        if child.name not in EXCLUDED_DIRECTORIES:
                            subdirectories.append(candidate)
                    elif stat.S_ISREG(info.st_mode) and fnmatch.fnmatchcase(child.name, pattern):
                        if len(files) >= MAX_RESULTS:
                            reasons.add("result_limit")
                            stop = True
                            break
                        files.append(str(candidate))
                except (OSError, ValueError, RuntimeError):
                    reasons.add("traversal_error")
            # Reverse the push order for a stable depth-first traversal. Avoid
            # Python recursion so deeply nested trees cannot exhaust its stack.
            pending.extend(reversed(subdirectories))
        except (OSError, ValueError, RuntimeError):
            if directory == root:
                return error("Unable to list search directory due to a filesystem or access error")
            reasons.add("traversal_error")

    result = {
        "ok": True, "tool": TOOL_NAME, "path": str(root), "pattern": pattern,
        "files": sorted(files), "returned_count": len(files),
        "truncated": bool(reasons), "truncation_reason": ", ".join(sorted(reasons)) or None,
    }
    # Account for the whole JSON envelope, not just filename characters. Trim
    # only a suffix so returned matches remain deterministically sorted.
    while len(json.dumps(result).encode("utf-8")) > MAX_RESPONSE_BYTES:
        reasons.add("response_size_limit")
        result.update(truncated=True, truncation_reason=", ".join(sorted(reasons)))
        if not result["files"]:
            return error("Search metadata exceeds the response-size limit")
        result["files"].pop()
        result["returned_count"] = len(result["files"])
    return result
