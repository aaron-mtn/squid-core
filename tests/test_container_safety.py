"""Focused coverage for shared ZIP-container safety limits."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from squid_core.file_readers import docx, epub, spreadsheet
from squid_core.file_readers.container import (
    MAX_ZIP_MEMBERS,
    ContainerSafetyError,
    _validate_zip_member_count,
    inspect_zip_container,
)
from squid_core.tools.builtins.read_file import execute as read_file


def _write_zip(path: Path, entries: dict[str, str]) -> None:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


def test_ordinary_container_passes_all_shared_limits(tmp_path: Path) -> None:
    source = tmp_path / "ordinary.zip"
    _write_zip(source, {"first.txt": "alpha", "second.txt": "beta"})

    file_bytes, expanded_bytes = inspect_zip_container(
        source,
        max_file_bytes=source.stat().st_size,
        max_expanded_bytes=9,
    )

    assert file_bytes == source.stat().st_size
    assert expanded_bytes == 9


def test_member_count_ceiling_allows_exact_limit_and_rejects_one_more() -> None:
    # Exercise the numeric boundary directly instead of building two enormous ZIPs.
    assert MAX_ZIP_MEMBERS == 100_000
    _validate_zip_member_count(MAX_ZIP_MEMBERS)

    with pytest.raises(ContainerSafetyError) as raised:
        _validate_zip_member_count(MAX_ZIP_MEMBERS + 1)

    assert raised.value.reason == "too_many_members"
    assert raised.value.actual_members == 100_001
    assert raised.value.maximum_members == 100_000


def test_existing_compressed_and_expanded_size_limits_remain_intact(
    tmp_path: Path,
) -> None:
    source = tmp_path / "bounded.zip"
    _write_zip(source, {"content.txt": "content"})

    with pytest.raises(ContainerSafetyError) as compressed:
        inspect_zip_container(
            source,
            max_file_bytes=source.stat().st_size - 1,
            max_expanded_bytes=7,
        )
    with pytest.raises(ContainerSafetyError) as expanded:
        inspect_zip_container(
            source,
            max_file_bytes=source.stat().st_size,
            max_expanded_bytes=6,
        )

    assert compressed.value.reason == "file_too_large"
    assert expanded.value.reason == "expanded_too_large"


@pytest.mark.parametrize(
    ("reader_module", "suffix", "expected_error"),
    [
        (
            docx,
            ".docx",
            "DOCX contains 100001 ZIP members; maximum supported count is 100000",
        ),
        (
            epub,
            ".epub",
            "EPUB contains 100001 ZIP members; maximum supported count is 100000",
        ),
        (
            spreadsheet,
            ".xlsx",
            "spreadsheet contains 100001 ZIP members; maximum supported count is "
            "100000",
        ),
    ],
)
def test_readers_translate_member_limit_to_sanitized_format_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reader_module,
    suffix: str,
    expected_error: str,
) -> None:
    source = tmp_path / f"pathological{suffix}"
    source.write_bytes(b"contents never reach a format parser")

    def reject_member_count(*args, **kwargs) -> None:
        raise ContainerSafetyError(
            "too_many_members",
            actual_members=100_001,
            maximum_members=100_000,
        )

    monkeypatch.setattr(reader_module, "inspect_zip_container", reject_member_count)

    result = read_file(tmp_path, str(source))

    assert result == {
        "ok": False,
        "tool": "read_file",
        "path": str(source),
        "error": expected_error,
    }
