"""Own bounded literal UTF-8 search, coordinated by Core's built-in executor.

Applications own preferences and Skill exposure. This module owns streaming
text matching and partial-result reporting, not file readers or agent policy.
It reuses find_files' internal link checks and exclusions without changing that
tool or introducing a generalized traversal framework.
"""

import codecs
import fnmatch
import json
import os
from pathlib import Path
import stat

from squid_core.safety.workspace_boundary import is_inside_workspace


# These internal helpers implement the same conservative no-link policy for
# both exploration tools; importing them does not execute a filename search.
from .find_files import EXCLUDED_DIRECTORIES, _has_linked_ancestor, _is_link


TOOL_NAME = "search_for_text"
MAX_RESULTS = 500
MAX_VISITED_ENTRIES = 20_000
MAX_DIRECTORIES = 1_000
MAX_RESPONSE_BYTES = 32_000
MAX_PATTERN_CHARS = 256
MAX_TEXT_CHARS = 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024
MAX_LINE_BYTES = 16 * 1024
MAX_EXCERPT_CHARS = 512


def tool_definition() -> dict:
    """Expose literal, single-line text matching and an optional filename filter."""
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "Search for literal text inside files, similar to grep or rg. Returns "
                "matching file paths, line numbers, and excerpts. Use this tool to "
                "locate code, symbols, configuration settings, or phrases when you "
                "don't know which file contains them. Use find_files to search "
                "filenames and read_file to inspect larger sections. Searches UTF-8 "
                "text with optional BOM, skipping binary/undecodable and special "
                "files, links, junctions and common generated directories. "
                f"Limits: {MAX_VISITED_ENTRIES} entries, {MAX_DIRECTORIES} directories, "
                f"{MAX_TOTAL_BYTES} scanned bytes, {MAX_FILE_BYTES} bytes per file, "
                f"{MAX_LINE_BYTES} bytes per line, {MAX_RESULTS} matching lines, "
                f"{MAX_EXCERPT_CHARS}-character excerpts and {MAX_RESPONSE_BYTES} "
                "UTF-8 JSON bytes. Limits and descendant errors mark partial results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT_CHARS,
                             "description": "Literal single-line text; no regex or multiline matching"},
                    "path": {"type": "string", "description": "Absolute directory; defaults to workspace root"},
                    "file_pattern": {"type": "string", "minLength": 1, "maxLength": MAX_PATTERN_CHARS,
                                     "description": "Optional case-sensitive filename filter using * and ?"},
                    "case_sensitive": {"type": "boolean", "default": True},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    }


def _excerpt(line: str, offset: int, case_sensitive: bool) -> str:
    """Center a bounded excerpt near the first hit, including Unicode case folds."""
    if not case_sensitive and len(line) > MAX_EXCERPT_CHARS:
        # Case folding can expand characters (sharp-s -> ss); convert the folded
        # offset back to a source character offset before slicing the excerpt.
        folded_position = 0
        for index, char in enumerate(line):
            folded_position += len(char.casefold())
            if folded_position > offset:
                offset = index
                break
    start = max(0, offset - 80) if len(line) > MAX_EXCERPT_CHARS else 0
    return line[start:start + MAX_EXCERPT_CHARS]


def _search_file(path: Path, needle: str, case_sensitive: bool,
                 remaining_bytes: int, match_limit: int) -> tuple[list, int, set]:
    """Stream one file, withholding hits until its text classification succeeds.

    Per-file hits and decoding buffers are bounded. Invalid UTF-8 or binary
    controls discard all tentative hits, including those preceding bad bytes.
    Long lines are drained in bounded segments to preserve later line numbers.
    """
    matches = []
    reasons = set()
    consumed = 0
    line_number = 1
    long_line = False
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    try:
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return [], 0, set()
            if info.st_size > MAX_FILE_BYTES:
                return [], 0, {"file_size_limit"}
            while True:
                available = min(remaining_bytes - consumed, MAX_FILE_BYTES + 1 - consumed)
                if available <= 0:
                    # Exhausting the budget is not truncation if this regular
                    # file is complete. Recheck size without reading beyond the
                    # scan budget, and finalize UTF-8 before releasing its hits.
                    if consumed == os.fstat(stream.fileno()).st_size:
                        decoder.decode(b"", final=True)
                        return matches, consumed, reasons
                    reason = "byte_limit" if consumed >= remaining_bytes else "file_size_limit"
                    return [], consumed, {reason}
                raw = stream.readline(min(MAX_LINE_BYTES + 1, available))
                consumed += len(raw)
                if consumed > MAX_FILE_BYTES:
                    return [], consumed, {"file_size_limit"}
                if not raw:
                    decoder.decode(b"", final=True)
                    return matches, consumed, reasons
                if any(byte < 32 and byte not in (9, 10, 12, 13) for byte in raw):
                    return [], consumed, set()
                decoded = decoder.decode(raw)
                if long_line or len(raw) > MAX_LINE_BYTES:
                    reasons.add("line_length_limit")
                    long_line = not raw.endswith(b"\n")
                else:
                    line = decoded.rstrip("\r\n")
                    comparable = line if case_sensitive else line.casefold()
                    offset = comparable.find(needle)
                    if offset >= 0:
                        if len(matches) < match_limit:
                            matches.append({"path": str(path), "line_number": line_number,
                                            "text": _excerpt(line, offset, case_sensitive)})
                        else:
                            reasons.add("match_limit")
                if raw.endswith(b"\n"):
                    line_number += 1
    except UnicodeError:
        return [], consumed, set()
    except (OSError, ValueError, RuntimeError):
        return [], consumed, {"traversal_error"}


def execute(workspace_root: Path, text: str | None = None, path: str | None = None,
            file_pattern: str | None = None, case_sensitive: bool = True) -> dict:
    """Return bounded matches; descendant failures produce explicitly partial results."""
    def error(message: str) -> dict:
        # OS exception strings may contain external targets or arbitrary amounts
        # of user-controlled text, so failures use fixed messages throughout.
        return {"ok": False, "tool": TOOL_NAME, "error": message}

    if (not isinstance(text, str) or not text or len(text) > MAX_TEXT_CHARS
            or any(char in text for char in "\r\n\0")):
        return error("text must be nonempty single-line literal text of at most 1024 characters")
    if not isinstance(case_sensitive, bool):
        return error("case_sensitive must be a boolean")
    if file_pattern is not None and (
        not isinstance(file_pattern, str) or not file_pattern or len(file_pattern) > MAX_PATTERN_CHARS
        or file_pattern in {".", ".."} or any(char in file_pattern for char in "/\\\0[]")
    ):
        return error("file_pattern must be a filename pattern of 1-256 characters using * and ?, without paths or brackets")
    pattern = file_pattern if file_pattern is not None else "*"
    needle = text if case_sensitive else text.casefold()
    if path is not None and (not isinstance(path, str) or not path or "\0" in path):
        return error("path must be a nonempty absolute directory path")
    try:
        workspace = Path(workspace_root).resolve(strict=True)
        requested = Path(path) if path is not None else workspace
        if not requested.is_absolute():
            return error("search_for_text requires an absolute path")
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

    matches = []
    scanned_bytes = 0
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
                        if info.st_size > MAX_FILE_BYTES:
                            reasons.add("file_size_limit")
                            continue
                        hits, used, file_reasons = _search_file(
                            candidate, needle, case_sensitive,
                            MAX_TOTAL_BYTES - scanned_bytes, MAX_RESULTS - len(matches),
                        )
                        scanned_bytes += used
                        matches.extend(hits)
                        reasons.update(file_reasons)
                        if "byte_limit" in file_reasons or "match_limit" in file_reasons:
                            stop = True
                            break
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
        "ok": True, "tool": TOOL_NAME, "path": str(root), "text": text,
        "matches": sorted(matches, key=lambda hit: (hit["path"], hit["line_number"])),
        "returned_count": len(matches),
        "truncated": bool(reasons), "truncation_reason": ", ".join(sorted(reasons)) or None,
    }
    # Account for the whole JSON envelope, not just filename characters. Trim
    # only a suffix so returned matches remain deterministically sorted.
    while len(json.dumps(result).encode("utf-8")) > MAX_RESPONSE_BYTES:
        reasons.add("response_size_limit")
        result.update(truncated=True, truncation_reason=", ".join(sorted(reasons)))
        if not result["matches"]:
            return error("Search metadata exceeds the response-size limit")
        result["matches"].pop()
        result["returned_count"] = len(result["matches"])
    return result
