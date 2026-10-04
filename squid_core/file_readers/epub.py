"""Read EPUB metadata and spine content into deterministic semantic text."""

from dataclasses import dataclass
import re
from typing import Any, Iterable

from .container import ContainerSafetyError, inspect_zip_container
from .context import FileReadContext
from .text import read_text_content


TOOL_NAME = "read_file"
MAX_EPUB_BYTES = 64 * 1024 * 1024
MAX_EPUB_EXPANDED_BYTES = 256 * 1024 * 1024
_SKIPPED_ELEMENTS = {"audio", "canvas", "iframe", "script", "style", "svg", "video"}
_CONTAINER_ELEMENTS = {
    "article",
    "aside",
    "body",
    "div",
    "figure",
    "figcaption",
    "footer",
    "header",
    "main",
    "nav",
    "section",
}
_METADATA_FIELDS = (
    ("title", "Title"),
    ("creator", "Creator"),
    ("language", "Language"),
    ("publisher", "Publisher"),
    ("date", "Date"),
    ("identifier", "Identifier"),
)


@dataclass(frozen=True)
class EpubBlock:
    """One EPUB-specific semantic block, detached from parser objects."""

    kind: str
    text: str = ""
    level: int | None = None
    marker: str | None = None
    rows: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class EpubSection:
    """One primary-reading-order spine resource and its semantic blocks."""

    href: str
    blocks: tuple[EpubBlock, ...]


@dataclass(frozen=True)
class EpubBook:
    """The small normalized EPUB representation needed by the text renderer."""

    version: str
    metadata: tuple[tuple[str, tuple[str, ...]], ...]
    sections: tuple[EpubSection, ...]


def read_epub(context: FileReadContext) -> dict:
    """Validate, normalize, and render one EPUB through text range semantics."""

    container_error = _check_epub_container(context)
    if container_error is not None:
        return container_error

    try:
        from epub_utils import Document

        document = Document(context.file_path)
        book = _parse_book(document)
        content = _render_book(book)
    except Exception:
        # epub-utils exposes parser, ZIP, decoding, and relationship failures from
        # several layers. Keep all of them behind one stable Tool boundary.
        return _epub_error(context.path, "invalid or unreadable EPUB document")

    result = read_text_content(context, content)
    result["media_type"] = "document"
    result["format"] = "epub"
    return result


def _check_epub_container(context: FileReadContext) -> dict | None:
    """Apply shared ZIP safety limits with EPUB-specific Tool wording."""

    try:
        inspect_zip_container(
            context.file_path,
            max_file_bytes=MAX_EPUB_BYTES,
            max_expanded_bytes=MAX_EPUB_EXPANDED_BYTES,
        )
    except ContainerSafetyError as error:
        if error.reason == "unreadable_file":
            return _epub_error(context.path, "unreadable EPUB file")
        if error.reason == "invalid_container":
            return _epub_error(context.path, "invalid or unreadable EPUB container")
        if error.reason == "file_too_large":
            assert error.actual_bytes is not None
            assert error.maximum_bytes is not None
            return _epub_error(
                context.path,
                f"EPUB is {error.actual_bytes} bytes; maximum supported size is "
                f"{error.maximum_bytes} bytes",
            )
        if error.reason == "too_many_members":
            assert error.actual_members is not None
            assert error.maximum_members is not None
            return _epub_error(
                context.path,
                f"EPUB contains {error.actual_members} ZIP members; maximum supported "
                f"count is {error.maximum_members}",
            )
        assert error.reason == "expanded_too_large"
        assert error.actual_bytes is not None
        assert error.maximum_bytes is not None
        return _epub_error(
            context.path,
            f"expanded EPUB is {error.actual_bytes} bytes; maximum supported size is "
            f"{error.maximum_bytes} bytes",
        )
    return None


def _parse_book(document: Any) -> EpubBook:
    """Normalize metadata and primary spine resources in package reading order."""

    package = document.package
    metadata_items: list[tuple[str, tuple[str, ...]]] = []
    for field, label in _METADATA_FIELDS:
        values = _metadata_values(getattr(package.metadata, field, None))
        if values:
            metadata_items.append((label, values))

    sections: list[EpubSection] = []
    for itemref in package.spine.itemrefs:
        # EPUB marks auxiliary content with linear="no". A nav document may also
        # appear in the spine, but emitting it would duplicate structural labels.
        if not itemref["linear"]:
            continue
        manifest_item = package.manifest.find_by_id(itemref["idref"])
        if manifest_item is None or "nav" in manifest_item["properties"]:
            continue
        if manifest_item["media_type"] not in {"application/xhtml+xml", "text/html"}:
            continue
        xhtml = document.find_content_by_id(itemref["idref"])
        sections.append(
            EpubSection(
                href=manifest_item["href"],
                blocks=tuple(_parse_xhtml(xhtml.tree)),
            )
        )

    return EpubBook(
        version=str(package.version),
        metadata=tuple(metadata_items),
        sections=tuple(sections),
    )


def _metadata_values(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    candidates = value if isinstance(value, list) else [value]
    return tuple(
        text
        for item in candidates
        if (text := _collapse_whitespace(str(item)))
    )


def _parse_xhtml(tree: Any) -> list[EpubBlock]:
    """Extract supported semantic blocks from an epub-utils XHTML tree."""

    bodies = tree.xpath('//*[local-name()="body"]')
    root = bodies[0] if bodies else tree
    return _parse_children(root)


def _parse_children(parent: Any, *, list_level: int = 0) -> list[EpubBlock]:
    blocks: list[EpubBlock] = []
    for element in parent:
        kind = _local_name(element.tag)
        if kind in _SKIPPED_ELEMENTS:
            continue
        if kind in {f"h{level}" for level in range(1, 7)}:
            text = _inline_text(element)
            if text:
                blocks.append(EpubBlock(kind="heading", text=text, level=int(kind[1])))
        elif kind == "p":
            text = _inline_text(element)
            if text:
                blocks.append(EpubBlock(kind="paragraph", text=text))
        elif kind in {"ul", "ol"}:
            blocks.extend(_parse_list(element, list_level=list_level))
        elif kind == "blockquote":
            quote_blocks = _parse_children(element)
            if quote_blocks:
                blocks.append(
                    EpubBlock(
                        kind="blockquote",
                        text=_render_blocks(quote_blocks).rstrip(),
                    )
                )
            else:
                text = _inline_text(element)
                if text:
                    blocks.append(EpubBlock(kind="blockquote", text=text))
        elif kind == "pre":
            text = (
                "".join(element.itertext())
                .replace("\r\n", "\n")
                .replace("\r", "\n")
                .strip("\n")
            )
            if text:
                blocks.append(EpubBlock(kind="code", text=text))
        elif kind == "table":
            rows = _parse_table(element)
            if rows:
                blocks.append(EpubBlock(kind="table", rows=rows))
        elif kind == "img":
            text = _image_text(element)
            if text:
                blocks.append(EpubBlock(kind="paragraph", text=text))
        elif kind in _CONTAINER_ELEMENTS:
            blocks.extend(_parse_children(element, list_level=list_level))
        else:
            # Unknown wrappers are traversed so common EPUB producer-specific
            # elements do not hide ordinary semantic descendants.
            nested = _parse_children(element, list_level=list_level)
            if nested:
                blocks.extend(nested)
            else:
                text = _inline_text(element)
                if text:
                    blocks.append(EpubBlock(kind="paragraph", text=text))
    return blocks


def _parse_list(element: Any, *, list_level: int) -> list[EpubBlock]:
    blocks: list[EpubBlock] = []
    marker = "-" if _local_name(element.tag) == "ul" else "1."
    for item in element:
        if _local_name(item.tag) != "li":
            continue
        text = _inline_text(item, excluded={"ol", "ul"})
        if text:
            blocks.append(
                EpubBlock(kind="list_item", text=text, level=list_level, marker=marker)
            )
        for child in item:
            if _local_name(child.tag) in {"ol", "ul"}:
                blocks.extend(_parse_list(child, list_level=list_level + 1))
    return blocks


def _parse_table(table: Any) -> tuple[tuple[str, ...], ...]:
    rows: list[tuple[str, ...]] = []
    for row in table.xpath('.//*[local-name()="tr"]'):
        cells = tuple(
            _inline_text(cell)
            for cell in row
            if _local_name(cell.tag) in {"td", "th"}
        )
        if cells:
            rows.append(cells)
    return tuple(rows)


def _inline_text(element: Any, *, excluded: set[str] | None = None) -> str:
    """Render safe inline semantics without activating EPUB resources."""

    parts: list[str] = [element.text or ""]
    for child in element:
        kind = _local_name(child.tag)
        if kind in _SKIPPED_ELEMENTS or (excluded is not None and kind in excluded):
            parts.append(child.tail or "")
            continue
        if kind == "a":
            label = _inline_text(child)
            destination = child.get("href")
            parts.append(
                f"[{label}]({destination})" if label and destination else label
            )
        elif kind == "img":
            parts.append(_image_text(child))
        elif kind == "br":
            parts.append("<br>")
        elif kind == "code":
            code = _collapse_whitespace("".join(child.itertext()))
            escaped_code = code.replace("`", "``")
            parts.append(f"`{escaped_code}`" if code else "")
        else:
            parts.append(_inline_text(child))
        parts.append(child.tail or "")
    return _collapse_whitespace("".join(parts))


def _image_text(element: Any) -> str:
    source = element.get("src")
    alt = _collapse_whitespace(element.get("alt", ""))
    if source and alt:
        return f"![{alt}]({source})"
    if source:
        return f"[Image]({source})"
    return f"[Image: {alt}]" if alt else ""


def _collapse_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _render_book(book: EpubBook) -> str:
    lines = ["# EPUB\n", f"EPUB version: {book.version}\n"]
    for label, values in book.metadata:
        lines.append(f"{label}: {'; '.join(values)}\n")
    for index, section in enumerate(book.sections, start=1):
        lines.append(f"\n## Section {index}: {section.href}\n")
        lines.append(_render_blocks(section.blocks))
    return "".join(lines)


def _render_blocks(blocks: Iterable[EpubBlock]) -> str:
    rendered: list[str] = []
    for block in blocks:
        if block.kind == "heading":
            assert block.level is not None
            rendered.append(f"{'#' * min(block.level + 2, 6)} {block.text}\n")
        elif block.kind == "paragraph":
            rendered.append(f"{block.text}\n")
        elif block.kind == "list_item":
            rendered.append(f"{'  ' * (block.level or 0)}{block.marker} {block.text}\n")
        elif block.kind == "blockquote":
            rendered.extend(
                f"> {line}\n" if line else ">\n"
                for line in block.text.splitlines()
            )
        elif block.kind == "code":
            rendered.append(f"```\n{block.text}\n```\n")
        elif block.kind == "table":
            rendered.extend(_render_table(block.rows))
    return "".join(rendered)


def _render_table(rows: tuple[tuple[str, ...], ...]) -> list[str]:
    width = max(len(row) for row in rows)
    normalized = [(*row, *("" for _ in range(width - len(row)))) for row in rows]
    lines = [
        "| " + " | ".join(_escape_cell(value) for value in normalized[0]) + " |\n"
    ]
    lines.append("| " + " | ".join("---" for _ in range(width)) + " |\n")
    lines.extend(
        "| " + " | ".join(_escape_cell(value) for value in row) + " |\n"
        for row in normalized[1:]
    )
    return lines


def _escape_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def _local_name(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _epub_error(path: str, error: str) -> dict:
    return {"ok": False, "tool": TOOL_NAME, "path": path, "error": error}
