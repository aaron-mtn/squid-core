"""Own apply_patch diagnostics and exact, revalidated replacement behavior."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import os
from pathlib import Path


TOOL_NAME = "apply_patch"
MAX_REPORTED_MATCHES = 3
CONTEXT_LINES = 2
CLOSEST_MATCH_THRESHOLD = 0.8
PREVIEW_LIMIT = 300
PREVIEW_OMISSION = " ... omitted ... "


@dataclass(frozen=True)
class PatchCandidate:
    """Retain one failed request's exact current match for a later selection."""

    path: str
    path_identity: str
    old_text: str
    new_text: str
    candidate_text: str
    start: int


class PatchCandidateStore:
    """Hold short-lived patch candidates for one ToolExecutor run."""

    def __init__(self) -> None:
        self._next_id = 1
        self._candidates: dict[str, PatchCandidate] = {}

    def add(
        self,
        *,
        path: str,
        old_text: str,
        new_text: str,
        candidate_text: str,
        start: int,
    ) -> str:
        """Store a candidate and return its run-local opaque identifier."""
        match_id = f"m{self._next_id}"
        self._next_id += 1
        self._candidates[match_id] = PatchCandidate(
            path=path,
            path_identity=_path_identity(path),
            old_text=old_text,
            new_text=new_text,
            candidate_text=candidate_text,
            start=start,
        )
        return match_id

    def get(self, match_id: str) -> PatchCandidate | None:
        """Return a candidate from this run, if it is still valid."""
        return self._candidates.get(match_id)

    def path_for(self, match_id: str) -> str | None:
        """Expose a stored target so ToolExecutor can snapshot before selection."""
        candidate = self.get(match_id)
        return None if candidate is None else candidate.path

    def invalidate_path(self, path: str) -> None:
        """Invalidate only candidates targeting the successfully mutated file."""
        identity = _path_identity(path)
        self._candidates = {
            match_id: candidate
            for match_id, candidate in self._candidates.items()
            if candidate.path_identity != identity
        }


# Return the tool definition shown to the model.
def tool_definition() -> dict:
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "Replace exactly one occurrence of old_text in an existing UTF-8 file. "
                "After an ambiguous or close-match diagnostic, select its exact, "
                "revalidated candidate with match_id."
            ),
            "parameters": {
                "type": "object",
                # Keep properties at the root for OpenAI-compatible providers
                # that require this conventional object-schema shape.
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "Exact text currently in the file",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "Replacement text",
                    },
                    "match_id": {
                        "type": "string",
                        "description": (
                            "Run-local candidate ID returned by a failed patch"
                        ),
                    },
                },
                "anyOf": [
                    {
                        "required": ["path", "old_text", "new_text"],
                    },
                    {
                        "required": ["match_id"],
                    },
                ],
                "additionalProperties": False,
            },
        },
    }


# Apply exactly one exact text replacement and return compact metadata.
def execute(
    _repo_root: Path,
    path: str | None = None,
    old_text: str | None = None,
    new_text: str | None = None,
    match_id: str | None = None,
    *,
    candidate_store: PatchCandidateStore | None = None,
) -> dict:
    """Apply a normal exact patch or select and revalidate a stored candidate."""
    if match_id is not None:
        if not isinstance(match_id, str):
            return _invalid_arguments("match_id must be a string.")
        candidate = None if candidate_store is None else candidate_store.get(match_id)
        if candidate is not None:
            return _apply_candidate(match_id, path, candidate_store)

    supplied_normal_fields = (
        path is not None,
        old_text is not None,
        new_text is not None,
    )
    valid_normal_fields = all(supplied_normal_fields) and all(
        isinstance(value, str) for value in (path, old_text, new_text)
    )
    if not valid_normal_fields:
        # An unusable string ID retains the stable candidate error unless the
        # request is independently valid as an ordinary exact patch.
        if isinstance(match_id, str):
            return _apply_candidate(match_id, path, candidate_store)
        return _invalid_arguments(
            "Provide path, old_text, and new_text together, or provide match_id alone."
        )

    # The validation above narrows these values to strings for runtime purposes.
    assert isinstance(path, str)
    assert isinstance(old_text, str)
    assert isinstance(new_text, str)
    file_path = Path(path)
    if not file_path.is_absolute():
        return {
            "ok": False,
            "tool": TOOL_NAME,
            "path": path,
            "error": "path_not_absolute",
        }

    content = file_path.read_text(encoding="utf-8")
    occurrences = content.count(old_text)

    if occurrences == 0:
        result = {
            "ok": False,
            "tool": TOOL_NAME,
            "path": path,
            "error": "old_text_not_found",
            "occurrences": occurrences,
        }
        closest_match = _find_closest_match(content, old_text)
        if closest_match is not None:
            result["closest_match"] = closest_match
            if candidate_store is not None:
                match_id = candidate_store.add(
                    path=path,
                    old_text=old_text,
                    new_text=new_text,
                    candidate_text=closest_match["content"],
                    start=closest_match["_start"],
                )
                result["candidates"] = [
                    {
                        "match_id": match_id,
                        "start_line": closest_match["start_line"],
                        "end_line": closest_match["end_line"],
                        "text": closest_match["content"],
                        "similarity": closest_match["similarity"],
                    }
                ]
                result["guidance"] = (
                    "If this is the text you intended, retry apply_patch with "
                    f"match_id {match_id}."
                )
            # Character offsets are internal revalidation details, not diagnostics.
            closest_match.pop("_start", None)
        return result

    if occurrences > 1:
        matches = _describe_matches(content, old_text)
        result = {
            "ok": False,
            "tool": TOOL_NAME,
            "path": path,
            "error": "old_text_not_unique",
            "occurrences": occurrences,
            "matches": matches,
        }
        if candidate_store is not None:
            candidates = []
            for match in matches:
                match_id = candidate_store.add(
                    path=path,
                    old_text=old_text,
                    new_text=new_text,
                    candidate_text=old_text,
                    start=match["_start"],
                )
                candidate = {
                    key: value for key, value in match.items() if key != "_start"
                }
                candidate["match_id"] = match_id
                candidates.append(candidate)
            result["candidates"] = candidates
            result["guidance"] = (
                "Choose the intended candidate by retrying apply_patch with its match_id."
            )
        for match in matches:
            match.pop("_start", None)
        return result

    match_start = content.index(old_text)
    start_line, end_line_before = _line_range(content, match_start, old_text)
    _, end_line_after = _line_range(content, match_start, new_text)
    updated = content.replace(old_text, new_text, 1)
    file_path.write_text(updated, encoding="utf-8")

    return {
        "ok": True,
        "tool": TOOL_NAME,
        "path": path,
        "replacements": 1,
        "start_line": start_line,
        "end_line_before": end_line_before,
        "end_line_after": end_line_after,
        # Keep the original compact size metadata for existing consumers.
        "chars_before": len(content),
        "chars_after": len(updated),
        "before_preview": _replacement_preview(old_text),
        "after_preview": _replacement_preview(new_text),
    }


def _apply_candidate(
    match_id: str,
    supplied_path: str | None,
    candidate_store: PatchCandidateStore | None,
) -> dict:
    """Replace the stored text only if it remains exact at its original offset."""
    candidate = None if candidate_store is None else candidate_store.get(match_id)
    if candidate is None:
        return {
            "ok": False,
            "tool": TOOL_NAME,
            "error": "patch_candidate_unavailable",
            "match_id": match_id,
        }

    if supplied_path is not None:
        if not isinstance(supplied_path, str):
            return _invalid_arguments("Candidate path must be a string when supplied.")
        if _path_identity(supplied_path) != candidate.path_identity:
            return {
                "ok": False,
                "tool": TOOL_NAME,
                "path": supplied_path,
                "error": "patch_candidate_path_conflict",
                "match_id": match_id,
                "message": "Supplied path does not match the stored candidate target.",
            }

    file_path = Path(candidate.path)
    try:
        content = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        # Disappearance or loss of readable UTF-8 content invalidates the exact
        # location just as surely as changed text does.
        return _stale_candidate_result(candidate, match_id)
    end = candidate.start + len(candidate.candidate_text)
    if content[candidate.start:end] != candidate.candidate_text:
        return _stale_candidate_result(candidate, match_id)

    start_line, end_line_before = _line_range(
        content, candidate.start, candidate.candidate_text
    )
    _, end_line_after = _line_range(content, candidate.start, candidate.new_text)
    updated = (
        content[: candidate.start] + candidate.new_text + content[end:]
    )
    file_path.write_text(updated, encoding="utf-8")
    return {
        "ok": True,
        "tool": TOOL_NAME,
        "path": candidate.path,
        "match_id": match_id,
        "replacements": 1,
        "start_line": start_line,
        "end_line_before": end_line_before,
        "end_line_after": end_line_after,
        "chars_before": len(content),
        "chars_after": len(updated),
        "before_preview": _replacement_preview(candidate.candidate_text),
        "after_preview": _replacement_preview(candidate.new_text),
    }


def _stale_candidate_result(candidate: PatchCandidate, match_id: str) -> dict:
    """Return the consistent result for any failed exact revalidation."""
    return {
        "ok": False,
        "tool": TOOL_NAME,
        "path": candidate.path,
        "error": "patch_candidate_stale",
        "match_id": match_id,
    }


def _invalid_arguments(message: str) -> dict:
    """Return a stable error for malformed normal/selection combinations."""
    return {
        "ok": False,
        "tool": TOOL_NAME,
        "error": "invalid_arguments",
        "message": message,
    }


def _path_identity(path: str) -> str:
    """Normalize path spellings so aliases cannot evade file invalidation."""
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _replacement_preview(text: str) -> str:
    """Keep short replacement text whole and visibly bound longer text."""
    if len(text) <= PREVIEW_LIMIT:
        return text
    retained = PREVIEW_LIMIT - len(PREVIEW_OMISSION)
    head = (retained + 1) // 2
    tail = retained - head
    return text[:head] + PREVIEW_OMISSION + text[-tail:]


def _line_range(content: str, start: int, matched_text: str) -> tuple[int, int]:
    """Return the 1-based inclusive line range occupied by text at start."""
    start_line = content.count("\n", 0, start) + 1
    occupied_lines = max(1, len(matched_text.splitlines()))
    return start_line, start_line + occupied_lines - 1


def _describe_matches(content: str, old_text: str) -> list[dict]:
    """Describe the first few exact matches without returning broad file content."""
    lines = content.splitlines(keepends=True)
    matches = []
    search_start = 0
    while len(matches) < MAX_REPORTED_MATCHES:
        match_start = content.find(old_text, search_start)
        if match_start < 0:
            break

        start_line, end_line = _line_range(content, match_start, old_text)
        context_start_line = max(1, start_line - CONTEXT_LINES)
        context_end_line = min(len(lines), end_line + CONTEXT_LINES)
        matches.append(
            {
                "_start": match_start,
                "start_line": start_line,
                "end_line": end_line,
                "context_start_line": context_start_line,
                "context_end_line": context_end_line,
                "context": "".join(lines[context_start_line - 1 : context_end_line]),
            }
        )
        # Empty old_text retains Python's exact-count semantics and still advances.
        search_start = match_start + max(1, len(old_text))

    return matches


def _find_closest_match(content: str, old_text: str) -> dict | None:
    """Return one high-confidence same-sized line window for diagnostics only."""
    old_lines = old_text.splitlines()
    current_lines = content.splitlines(keepends=True)
    window_size = len(old_lines)
    if window_size == 0 or len(current_lines) < window_size:
        return None

    expected = "\n".join(old_lines)
    best_start = 0
    best_content = ""
    best_similarity = 0.0
    for start in range(len(current_lines) - window_size + 1):
        window = current_lines[start : start + window_size]
        comparison = "\n".join(line.rstrip("\r\n") for line in window)
        similarity = SequenceMatcher(None, expected, comparison).ratio()
        if similarity > best_similarity:
            best_start = start
            best_content = "".join(window)
            best_similarity = similarity

    if best_similarity < CLOSEST_MATCH_THRESHOLD:
        return None

    return {
        "_start": sum(len(line) for line in current_lines[:best_start]),
        "start_line": best_start + 1,
        "end_line": best_start + window_size,
        "similarity": round(best_similarity, 3),
        "content": best_content,
    }
