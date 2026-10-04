"""Shared invocation data for Core's registered file readers."""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from squid_core.settings import ReadFileSettings


SnapshotAsset = Callable[[bytes, str], str]
GeneratedImageStore = Callable[
    [str, str, str, Callable[[], bytes], str | None],
    tuple[str, bool],
]


@dataclass(frozen=True)
class FileReadContext:
    """Carry one validated read request and its optional storage collaborators."""

    file_path: Path
    path: str
    ranges: list[dict[str, int]] | None
    pages: list[dict[str, int]] | None
    sheet: str | None
    rows: dict[str, int] | None
    settings: ReadFileSettings
    snapshot_asset: SnapshotAsset | None
    ensure_generated_image: GeneratedImageStore | None
