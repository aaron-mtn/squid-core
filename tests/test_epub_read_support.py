"""Focused coverage for semantic, bounded EPUB reads."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

from squid_core.file_readers.epub import read_epub
from squid_core.file_readers.registry import reader_for_suffix
from squid_core.settings import ReadFileSettings
from squid_core.tools.builtins.read_file import execute as read_file


def test_registry_dispatches_epub_and_renders_epub3_semantics(tmp_path: Path) -> None:
    source = tmp_path / "semantic.epub"
    _write_epub3(source)

    first = read_file(tmp_path, str(source))
    second = read_file(tmp_path, str(source))

    assert reader_for_suffix(".epub") is read_epub
    assert reader_for_suffix(".EPUB") is read_epub
    assert first == second
    assert first["ok"] is True
    assert first["media_type"] == "document"
    assert first["format"] == "epub"
    assert first["content"] == (
        "# EPUB\n"
        "EPUB version: 3.0\n"
        "Title: Fixture Book\n"
        "Creator: Ada Example; Ben Example\n"
        "Language: en\n"
        "Publisher: Squid Press\n"
        "Identifier: urn:test:fixture\n"
        "\n"
        "## Section 1: z-first.xhtml\n"
        "### First Chapter\n"
        "Opening [reference](second.xhtml#target) with `inline code`.\n"
        "- Alpha\n"
        "- Beta\n"
        "  1. Nested one\n"
        "> Quoted thought.\n"
        "```\n"
        "line one\n"
        "line two\n"
        "```\n"
        "| Name | Value |\n"
        "| --- | --- |\n"
        "| alpha | one \\| two |\n"
        "![Cover detail](images/detail.png)\n"
        "\n"
        "## Section 2: a-second.xhtml\n"
        "#### Second Chapter\n"
        "Second paragraph.\n"
    )
    assert "Navigation duplicate" not in first["content"]
    assert "Nonlinear appendix" not in first["content"]
    assert first["content"].index("First Chapter") < first["content"].index(
        "Second Chapter"
    )


def test_epub2_metadata_and_spine_are_supported(tmp_path: Path) -> None:
    source = tmp_path / "legacy.epub"
    _write_epub2(source)

    result = read_file(tmp_path, str(source))

    assert result["ok"] is True
    assert "EPUB version: 2.0" in result["content"]
    assert "Title: Legacy Fixture" in result["content"]
    assert "Creator: EPUB Two Author" in result["content"]
    assert "### Legacy Chapter" in result["content"]
    assert "NCX duplicate" not in result["content"]


def test_epub_uses_text_truncation_and_targeted_ranges(tmp_path: Path) -> None:
    source = tmp_path / "long.epub"
    paragraphs = "".join(f"<p>paragraph-{number}</p>" for number in range(1, 21))
    _write_epub3(source, first_body=paragraphs, second_body="")
    settings = ReadFileSettings(default_max_lines=6, minimum_range_lines=1)

    whole = read_file(tmp_path, str(source), settings=settings)
    targeted = read_file(
        tmp_path,
        str(source),
        ranges=[{"start_line": 10, "end_line": 11}],
        settings=settings,
    )

    assert whole["content_truncated"] is True
    assert whole["returned_line_ranges"] == [
        {"start_line": 1, "end_line": 3},
        {"start_line": whole["total_lines"] - 2, "end_line": whole["total_lines"]},
    ]
    assert targeted["content"] == "paragraph-1\nparagraph-2\n"
    assert targeted["returned_line_ranges"] == [{"start_line": 10, "end_line": 11}]
    assert targeted["ranged"] is True


def test_epub_rejects_malformed_container_and_document(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.epub"
    malformed.write_bytes(b"not a zip")
    not_epub = tmp_path / "not-epub.epub"
    with ZipFile(not_epub, "w", ZIP_DEFLATED) as archive:
        archive.writestr("content.txt", "ordinary zip")

    assert read_file(tmp_path, str(malformed))["error"] == (
        "invalid or unreadable EPUB container"
    )
    assert read_file(tmp_path, str(not_epub))["error"] == (
        "invalid or unreadable EPUB document"
    )


def test_epub_rejects_oversized_file_and_expanded_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from squid_core.file_readers import epub

    source = tmp_path / "bounded.epub"
    _write_epub3(source)
    monkeypatch.setattr(epub, "MAX_EPUB_BYTES", source.stat().st_size - 1)
    compressed_result = read_file(tmp_path, str(source))
    monkeypatch.setattr(epub, "MAX_EPUB_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(epub, "MAX_EPUB_EXPANDED_BYTES", 2)
    expanded_result = read_file(tmp_path, str(source))

    assert compressed_result["error"].startswith("EPUB is ")
    assert "expanded EPUB" not in compressed_result["error"]
    assert expanded_result["error"].startswith("expanded EPUB is ")
    assert "maximum supported size is 2 bytes" in expanded_result["error"]


def test_epub_rejects_non_line_selectors(tmp_path: Path) -> None:
    source = tmp_path / "selectors.epub"
    _write_epub3(source)

    pages = read_file(
        tmp_path,
        str(source),
        pages=[{"start_page": 1, "end_page": 1}],
    )
    sheet = read_file(tmp_path, str(source), sheet="Sheet1")
    rows = read_file(
        tmp_path,
        str(source),
        sheet="Sheet1",
        rows={"start_row": 1, "end_row": 2},
    )

    assert pages["error"] == "page ranges are supported only for PDF files"
    assert sheet["error"] == "sheet and rows are supported only for XLSX and ODS files"
    assert rows["error"] == "sheet and rows are supported only for XLSX and ODS files"


def test_epub_parser_exceptions_become_structured_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import epub_utils

    source = tmp_path / "parser.epub"
    _write_epub3(source)

    class BrokenDocument:
        def __init__(self, _path: Path) -> None:
            raise RuntimeError("private parser detail")

    monkeypatch.setattr(epub_utils, "Document", BrokenDocument)

    assert read_file(tmp_path, str(source)) == {
        "ok": False,
        "tool": "read_file",
        "path": str(source),
        "error": "invalid or unreadable EPUB document",
    }


def _write_epub3(
    path: Path,
    *,
    first_body: str | None = None,
    second_body: str | None = None,
) -> None:
    """Write a small EPUB 3 fixture with deliberately non-alphabetic spine order."""

    first_body = first_body if first_body is not None else (
        "<h1>First Chapter</h1>"
        '<p>Opening <a href="second.xhtml#target">reference</a> with '
        "<code>inline code</code>.</p>"
        "<ul><li>Alpha</li><li>Beta<ol><li>Nested one</li></ol></li></ul>"
        "<blockquote><p>Quoted thought.</p></blockquote>"
        "<pre>line one\nline two</pre>"
        "<table><tr><th>Name</th><th>Value</th></tr>"
        "<tr><td>alpha</td><td>one | two</td></tr></table>"
        '<p><img src="images/detail.png" alt="Cover detail"/></p>'
    )
    second_body = second_body if second_body is not None else (
        '<h2 id="target">Second Chapter</h2><p>Second paragraph.</p>'
    )
    package = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">urn:test:fixture</dc:identifier>
    <dc:title>Fixture Book</dc:title>
    <dc:creator>Ada Example</dc:creator><dc:creator>Ben Example</dc:creator>
    <dc:language>en</dc:language><dc:publisher>Squid Press</dc:publisher>
  </metadata>
  <manifest>
    <item id="second" href="a-second.xhtml" media-type="application/xhtml+xml"/>
    <item id="first" href="z-first.xhtml" media-type="application/xhtml+xml"/>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml"
      properties="nav"/>
    <item id="appendix" href="appendix.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="first"/><itemref idref="nav"/>
    <itemref idref="appendix" linear="no"/><itemref idref="second"/></spine>
</package>"""
    files = {
        "OEBPS/content.opf": package,
        "OEBPS/z-first.xhtml": _xhtml(first_body),
        "OEBPS/a-second.xhtml": _xhtml(second_body),
        "OEBPS/nav.xhtml": _xhtml(
            '<nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc">'
            "<h1>Navigation duplicate</h1><ol><li>First</li></ol></nav>"
        ),
        "OEBPS/appendix.xhtml": _xhtml("<h1>Nonlinear appendix</h1>"),
    }
    _write_epub(path, files)


def _write_epub2(path: Path) -> None:
    package = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">urn:test:legacy</dc:identifier>
    <dc:title>Legacy Fixture</dc:title><dc:creator>EPUB Two Author</dc:creator>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  </manifest>
  <spine toc="ncx"><itemref idref="chapter"/></spine>
</package>"""
    ncx = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head/><docTitle><text>NCX duplicate</text></docTitle>
  <navMap><navPoint id="n1" playOrder="1">
    <navLabel><text>NCX duplicate</text></navLabel>
    <content src="chapter.xhtml"/></navPoint></navMap>
</ncx>"""
    files = {
        "OEBPS/content.opf": package,
        "OEBPS/chapter.xhtml": _xhtml(
            "<h1>Legacy Chapter</h1><p>Legacy paragraph.</p>"
        ),
        "OEBPS/toc.ncx": ncx,
    }
    _write_epub(path, files)


def _write_epub(path: Path, files: dict[str, str]) -> None:
    container = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf"
    media-type="application/oebps-package+xml"/></rootfiles>
</container>"""
    with ZipFile(path, "w") as archive:
        # The EPUB mimetype member is conventionally first and uncompressed.
        archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        archive.writestr(
            "META-INF/container.xml", container, compress_type=ZIP_DEFLATED
        )
        for name, content in files.items():
            archive.writestr(name, content, compress_type=ZIP_DEFLATED)


def _xhtml(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Fixture</title></head>'
        f"<body>{body}</body></html>"
    )
