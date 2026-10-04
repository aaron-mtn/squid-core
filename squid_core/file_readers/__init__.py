"""Shared, provider-neutral readers used by Core file-reading adapters."""

from .context import FileReadContext, GeneratedImageStore, SnapshotAsset
from .registry import read_with_registered_reader

__all__ = [
    "FileReadContext",
    "GeneratedImageStore",
    "SnapshotAsset",
    "read_with_registered_reader",
]
