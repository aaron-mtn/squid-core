"""Focused coverage for the shared file-reader invocation contract."""

from pathlib import Path

from squid_core.file_readers.context import FileReadContext
from squid_core.file_readers.docx import read_docx
from squid_core.file_readers.epub import read_epub
from squid_core.file_readers.image import read_image
from squid_core.file_readers.ods import read_ods
from squid_core.file_readers.pdf import read_pdf
from squid_core.file_readers.registry import (
    read_with_registered_reader,
    reader_for_suffix,
)
from squid_core.file_readers.text import read_text
from squid_core.file_readers.xlsx import read_xlsx
from squid_core.settings import ReadFileSettings
from squid_core.tools.builtins.read_file import execute as read_file


def test_registry_selects_builtin_readers_by_suffix() -> None:
    assert reader_for_suffix(".docx") is read_docx
    assert reader_for_suffix(".DOCX") is read_docx
    assert reader_for_suffix(".epub") is read_epub
    assert reader_for_suffix(".EPUB") is read_epub
    assert reader_for_suffix(".xlsx") is read_xlsx
    assert reader_for_suffix(".XLSX") is read_xlsx
    assert reader_for_suffix(".ods") is read_ods
    assert reader_for_suffix(".ODS") is read_ods
    assert reader_for_suffix(".pdf") is read_pdf
    assert reader_for_suffix(".PDF") is read_pdf
    for suffix in (".png", ".jpg", ".jpeg", ".webp"):
        assert reader_for_suffix(suffix) is read_image


def test_unknown_suffix_falls_back_to_text_through_public_tool(tmp_path: Path) -> None:
    source = tmp_path / "notes.future"
    source.write_text("ordinary UTF-8 text \N{SQUID}\n", encoding="utf-8")

    result = read_file(tmp_path, str(source))

    assert reader_for_suffix(source.suffix) is read_text
    assert result["ok"] is True
    assert result["content"] == "ordinary UTF-8 text \N{SQUID}\n"
    assert result["ranged"] is False


def test_registry_invokes_registered_reader_with_unchanged_context(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A newly registered reader must not require a dispatcher branch."""

    from squid_core.file_readers import registry

    context = FileReadContext(
        file_path=tmp_path / "sample.future",
        path=str(tmp_path / "sample.future"),
        ranges=None,
        pages=None,
        sheet=None,
        rows=None,
        settings=ReadFileSettings(),
        snapshot_asset=None,
        ensure_generated_image=None,
    )
    seen = []

    def read_future(received: FileReadContext) -> dict:
        seen.append(received)
        return {"ok": True, "format": "future"}

    monkeypatch.setitem(registry._READERS_BY_SUFFIX, ".future", read_future)

    assert read_with_registered_reader(context) == {
        "ok": True,
        "format": "future",
    }
    assert seen == [context]
