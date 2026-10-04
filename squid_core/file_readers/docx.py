"""Read DOCX semantic content into deterministic line-oriented text."""

from dataclasses import dataclass
import re
from typing import Any

from .container import ContainerSafetyError, inspect_zip_container
from .context import FileReadContext
from .text import read_text_content


TOOL_NAME = "read_file"
MAX_DOCX_BYTES = 64 * 1024 * 1024
MAX_DOCX_EXPANDED_BYTES = 256 * 1024 * 1024
_HEADING_STYLE = re.compile(r"^Heading\s+([1-9][0-9]*)$", re.IGNORECASE)


@dataclass(frozen=True)
class DocxBlock:
    """A small parser-neutral block used by deterministic DOCX rendering."""

    kind: str
    text: str = ""
    level: int | None = None
    marker: str | None = None
    rows: tuple[tuple[str, ...], ...] = ()


def read_docx(context: FileReadContext) -> dict:
    """Validate, normalize, and render one DOCX through text range semantics."""

    container_error = _check_docx_container(context)
    if container_error is not None:
        return container_error

    try:
        from docx import Document

        document = Document(context.file_path)
        blocks = _parse_document(document)
        content = _render_document(blocks)
    except Exception:
        # python-docx can surface several ZIP, relationship, and XML exception
        # types. None should escape the public Tool boundary or leak internals.
        return _docx_error(context.path, "invalid or unreadable DOCX document")

    result = read_text_content(context, content)
    result["media_type"] = "document"
    result["format"] = "docx"
    return result


def _check_docx_container(context: FileReadContext) -> dict | None:
    """Apply shared ZIP safety limits with DOCX-specific Tool wording."""

    try:
        inspect_zip_container(
            context.file_path,
            max_file_bytes=MAX_DOCX_BYTES,
            max_expanded_bytes=MAX_DOCX_EXPANDED_BYTES,
        )
    except ContainerSafetyError as error:
        if error.reason == "unreadable_file":
            return _docx_error(context.path, "unreadable DOCX file")
        if error.reason == "invalid_container":
            return _docx_error(context.path, "invalid or unreadable DOCX container")
        if error.reason == "file_too_large":
            assert error.actual_bytes is not None
            assert error.maximum_bytes is not None
            return _docx_error(
                context.path,
                f"DOCX is {error.actual_bytes} bytes; maximum supported size is "
                f"{error.maximum_bytes} bytes",
            )
        if error.reason == "too_many_members":
            assert error.actual_members is not None
            assert error.maximum_members is not None
            return _docx_error(
                context.path,
                f"DOCX contains {error.actual_members} ZIP members; maximum supported "
                f"count is {error.maximum_members}",
            )
        assert error.reason == "expanded_too_large"
        assert error.actual_bytes is not None
        assert error.maximum_bytes is not None
        return _docx_error(
            context.path,
            f"expanded DOCX is {error.actual_bytes} bytes; maximum supported size is "
            f"{error.maximum_bytes} bytes",
        )
    return None


def _parse_document(document: Any) -> tuple[DocxBlock, ...]:
    """Normalize body paragraphs and tables while retaining document order."""

    from docx.table import Table
    from docx.text.paragraph import Paragraph

    blocks: list[DocxBlock] = []
    for element in document.element.body.iterchildren():
        element_kind = _local_name(element.tag)
        if element_kind == "p":
            paragraph = Paragraph(element, document)
            text = _paragraph_text(paragraph).strip()
            if not text:
                continue
            heading_level = _heading_level(paragraph)
            marker, list_level = _list_marker(paragraph)
            blocks.append(
                DocxBlock(
                    kind="heading" if heading_level is not None else "paragraph",
                    text=text,
                    level=heading_level if heading_level is not None else list_level,
                    marker=marker,
                )
            )
        elif element_kind == "tbl":
            table = Table(element, document)
            rows = tuple(
                tuple(_cell_text(cell) for cell in row.cells) for row in table.rows
            )
            blocks.append(DocxBlock(kind="table", rows=rows))
    return tuple(blocks)


def _paragraph_text(paragraph: Any) -> str:
    """Render text and external/internal hyperlinks from one paragraph."""

    parts: list[str] = []
    for child in paragraph._p.iterchildren():
        kind = _local_name(child.tag)
        if kind == "r":
            parts.append(_run_text(child))
        elif kind == "hyperlink":
            label = "".join(
                _run_text(run)
                for run in child.iterchildren()
                if _local_name(run.tag) == "r"
            )
            destination = _hyperlink_destination(paragraph, child)
            if label and destination:
                parts.append(f"[{label}]({destination})")
            else:
                parts.append(label)
        elif kind == "fldSimple":
            parts.append("".join(_run_text(run) for run in child.iterchildren()))
    return "".join(parts).replace("\r\n", "\n").replace("\r", "\n")


def _run_text(run: Any) -> str:
    """Extract the visible text controls supported by python-docx paragraphs."""

    parts: list[str] = []
    for node in run.iter():
        kind = _local_name(node.tag)
        if kind == "t" and node.text:
            parts.append(node.text)
        elif kind == "tab":
            parts.append("\t")
        elif kind in {"br", "cr"}:
            parts.append("\n")
        elif kind == "noBreakHyphen":
            parts.append("-")
        elif kind == "softHyphen":
            parts.append("\N{SOFT HYPHEN}")
    return "".join(parts)


def _hyperlink_destination(paragraph: Any, hyperlink: Any) -> str | None:
    """Resolve a hyperlink relationship, falling back to an internal anchor."""

    from docx.oxml.ns import qn

    relationship_id = hyperlink.get(qn("r:id"))
    if relationship_id:
        relationship = paragraph.part.rels.get(relationship_id)
        if relationship is not None:
            return relationship.target_ref
    anchor = hyperlink.get(qn("w:anchor"))
    return f"#{anchor}" if anchor else None


def _heading_level(paragraph: Any) -> int | None:
    style_name = paragraph.style.name if paragraph.style is not None else ""
    match = _HEADING_STYLE.fullmatch(style_name or "")
    return int(match.group(1)) if match else None


def _list_marker(paragraph: Any) -> tuple[str | None, int | None]:
    """Recognize common list styles and direct numbering definitions."""

    style_name = (paragraph.style.name if paragraph.style is not None else "") or ""
    lowered = style_name.lower()
    if "list bullet" in lowered:
        return "-", _numbering_level(paragraph)
    if "list number" in lowered:
        return "1.", _numbering_level(paragraph)

    number_format = _number_format(paragraph)
    if number_format is None:
        return None, None
    return ("-" if number_format == "bullet" else "1."), _numbering_level(paragraph)


def _numbering_level(paragraph: Any) -> int:
    properties = paragraph._p.pPr
    if properties is None or properties.numPr is None or properties.numPr.ilvl is None:
        return 0
    return int(properties.numPr.ilvl.val)


def _number_format(paragraph: Any) -> str | None:
    """Resolve the small part of Word numbering needed to distinguish bullets."""

    from docx.oxml.ns import qn

    properties = paragraph._p.pPr
    if properties is None or properties.numPr is None or properties.numPr.numId is None:
        return None
    num_id = str(properties.numPr.numId.val)
    numbering = paragraph.part.numbering_part.element
    abstract_id = None
    for node in numbering:
        if _local_name(node.tag) != "num" or node.get(qn("w:numId")) != num_id:
            continue
        for child in node:
            if _local_name(child.tag) == "abstractNumId":
                abstract_id = child.get(qn("w:val"))
                break
    if abstract_id is None:
        return "decimal"

    level = str(_numbering_level(paragraph))
    for node in numbering:
        if (
            _local_name(node.tag) != "abstractNum"
            or node.get(qn("w:abstractNumId")) != abstract_id
        ):
            continue
        for child in node:
            if _local_name(child.tag) != "lvl" or child.get(qn("w:ilvl")) != level:
                continue
            for setting in child:
                if _local_name(setting.tag) == "numFmt":
                    return setting.get(qn("w:val"))
    return "decimal"


def _cell_text(cell: Any) -> str:
    parts = []
    for paragraph in cell.paragraphs:
        text = _paragraph_text(paragraph).strip()
        if text:
            parts.append(text)
    return "<br>".join(parts)


def _render_document(blocks: tuple[DocxBlock, ...]) -> str:
    """Render normalized blocks as stable Markdown-like source lines."""

    rendered: list[str] = []
    for block in blocks:
        if block.kind == "heading":
            assert block.level is not None
            rendered.append(f"{'#' * block.level} {block.text}\n")
        elif block.kind == "paragraph":
            if block.marker is not None:
                indent = "  " * (block.level or 0)
                rendered.append(f"{indent}{block.marker} {block.text}\n")
            else:
                rendered.append(f"{block.text}\n")
        elif block.kind == "table":
            rendered.extend(_render_table(block.rows))
    return "".join(rendered)


def _render_table(rows: tuple[tuple[str, ...], ...]) -> list[str]:
    if not rows:
        return []
    width = max(len(row) for row in rows)
    normalized = [(*row, *("" for _ in range(width - len(row)))) for row in rows]
    lines = ["| " + " | ".join(_escape_cell(value) for value in normalized[0]) + " |\n"]
    lines.append("| " + " | ".join("---" for _ in range(width)) + " |\n")
    lines.extend(
        "| " + " | ".join(_escape_cell(value) for value in row) + " |\n"
        for row in normalized[1:]
    )
    return lines


def _escape_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _docx_error(path: str, error: str) -> dict:
    return {"ok": False, "tool": TOOL_NAME, "path": path, "error": error}
