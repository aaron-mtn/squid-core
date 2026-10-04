"""Bound file-backed ZIP containers before format-specific parsing."""

from pathlib import Path
from zipfile import BadZipFile, ZipFile


MAX_ZIP_MEMBERS = 100_000


class ContainerSafetyError(Exception):
    """Describe a generic container failure without format-specific wording."""

    def __init__(
        self,
        reason: str,
        *,
        actual_bytes: int | None = None,
        maximum_bytes: int | None = None,
        actual_members: int | None = None,
        maximum_members: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.actual_bytes = actual_bytes
        self.maximum_bytes = maximum_bytes
        self.actual_members = actual_members
        self.maximum_members = maximum_members
        self.detail = detail


def inspect_zip_container(
    path: Path,
    *,
    max_file_bytes: int,
    max_expanded_bytes: int,
) -> tuple[int, int]:
    """Validate generic ZIP size bounds without assigning format semantics.

    ZIP member count and sizes are checked before a higher-level parser expands
    content. Callers deliberately translate these neutral failure reasons into
    their own stable, format-specific Tool errors.
    """

    try:
        file_bytes = path.stat().st_size
    except OSError as error:
        raise ContainerSafetyError(
            "unreadable_file",
            detail=str(error),
        ) from error
    if file_bytes > max_file_bytes:
        raise ContainerSafetyError(
            "file_too_large",
            actual_bytes=file_bytes,
            maximum_bytes=max_file_bytes,
        )

    try:
        with ZipFile(path) as archive:
            members = archive.infolist()
            _validate_zip_member_count(len(members))
            expanded_bytes = sum(member.file_size for member in members)
    except (BadZipFile, OSError) as error:
        raise ContainerSafetyError(
            "invalid_container",
            detail=str(error),
        ) from error
    if expanded_bytes > max_expanded_bytes:
        raise ContainerSafetyError(
            "expanded_too_large",
            actual_bytes=expanded_bytes,
            maximum_bytes=max_expanded_bytes,
        )
    return file_bytes, expanded_bytes


def _validate_zip_member_count(member_count: int) -> None:
    """Reject only absurd member counts without constructing a huge test ZIP."""

    if member_count > MAX_ZIP_MEMBERS:
        raise ContainerSafetyError(
            "too_many_members",
            actual_members=member_count,
            maximum_members=MAX_ZIP_MEMBERS,
        )
