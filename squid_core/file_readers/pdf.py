"""Extract PDF text and lazily render selected pages into session assets."""

import hashlib
import math
from typing import Sequence

from .context import FileReadContext


TOOL_NAME = "read_file"
MAX_PDF_BYTES = 64 * 1024 * 1024
MAX_PDF_PAGES_PER_READ = 20
MAX_PDF_RENDER_PIXELS = 40_000_000
PDF_RENDER_SCALE = 2.0


def read_pdf(context: FileReadContext) -> dict:
    """Extract and lazily render only the selected pages of one PDF."""

    if context.ranges is not None:
        return _pdf_error(
            context.path,
            "line ranges are not supported for PDF files; use pages",
        )
    if context.ensure_generated_image is None:
        return _pdf_error(
            context.path,
            "PDF reads require active session asset storage",
        )

    try:
        size_bytes = context.file_path.stat().st_size
        if size_bytes > MAX_PDF_BYTES:
            return _pdf_error(
                context.path,
                f"PDF is {size_bytes} bytes; maximum supported size is {MAX_PDF_BYTES} bytes",
            )
        pdf_data = context.file_path.read_bytes()
        if len(pdf_data) > MAX_PDF_BYTES:
            return _pdf_error(
                context.path,
                f"PDF is {len(pdf_data)} bytes; maximum supported size is "
                f"{MAX_PDF_BYTES} bytes",
            )
        import pymupdf

        document = pymupdf.open(stream=pdf_data, filetype="pdf")
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        return _pdf_error(context.path, f"invalid or unreadable PDF: {error}")

    try:
        if document.needs_pass:
            return _pdf_error(context.path, "encrypted PDFs are not supported")
        total_pages = document.page_count
        selected_ranges, selection_error = _selected_pdf_ranges(
            context.pages,
            total_pages,
        )
        if selection_error is not None:
            return _pdf_error(context.path, selection_error)

        source_digest = hashlib.sha256(pdf_data).hexdigest()
        folder_name = f"{context.file_path.stem} rendered images"
        folder_identity = f"pdf-sha256={source_digest}"
        rendered_pages = []
        text_parts = []
        for start_page, end_page in selected_ranges:
            for page_number in range(start_page, end_page + 1):
                page = document.load_page(page_number - 1)
                extracted = page.get_text("text")
                text_parts.append(f"<<< PDF PAGE {page_number} >>>\n")
                text_parts.append(
                    extracted if extracted.strip() else "[no extractable text on this page]\n"
                )
                if extracted and not extracted.endswith("\n"):
                    text_parts.append("\n")

                filename = f"page-{page_number}.png"
                # Folder metadata already establishes the source PDF, and the
                # filename establishes the page. Keep only render-affecting
                # settings in each lazy page entry to avoid repeating both.
                cache_key = (
                    f"renderer={pymupdf.VersionBind};scale={PDF_RENDER_SCALE};"
                    f"max-pixels={MAX_PDF_RENDER_PIXELS}"
                )
                rendered_path, reused = context.ensure_generated_image(
                    folder_name,
                    filename,
                    cache_key,
                    lambda page=page: _render_pdf_page(page, pymupdf),
                    folder_identity,
                )
                rendered_pages.append(
                    {"page": page_number, "path": rendered_path, "reused": reused}
                )
    except (OSError, RuntimeError, ValueError) as error:
        return _pdf_error(
            context.path,
            f"could not read or render PDF: {error}",
        )
    finally:
        document.close()

    content = "".join(text_parts)
    return {
        "ok": True,
        "tool": TOOL_NAME,
        "path": context.path,
        "media_type": "pdf",
        "content": content,
        "content_truncated": False,
        "content_original_chars": len(content),
        "content_returned_chars": len(content),
        "total_pages": total_pages,
        "returned_page_ranges": [
            {"start_page": start, "end_page": end} for start, end in selected_ranges
        ],
        "rendered_pages": rendered_pages,
    }


def _render_pdf_page(page, pymupdf_module) -> bytes:
    """Render a PDF page within the same pixel ceiling used for image ingest."""

    page_pixels = page.rect.width * page.rect.height
    if page_pixels <= 0:
        raise ValueError("PDF page dimensions must be positive")
    scale = min(PDF_RENDER_SCALE, math.sqrt(MAX_PDF_RENDER_PIXELS / page_pixels))
    pixmap = page.get_pixmap(
        matrix=pymupdf_module.Matrix(scale, scale),
        alpha=False,
    )
    if pixmap.width * pixmap.height > MAX_PDF_RENDER_PIXELS:
        raise ValueError("rendered PDF page exceeds the maximum supported pixel count")
    return pixmap.tobytes("png")


def _selected_pdf_ranges(
    requested: list[dict[str, int]] | None,
    total_pages: int,
) -> tuple[list[tuple[int, int]], str | None]:
    """Normalize PDF page selection without applying text-line expansion rules."""

    if total_pages < 1:
        return [], "PDF contains no pages"
    if requested is None:
        if total_pages > MAX_PDF_PAGES_PER_READ:
            return [], (
                f"PDF has {total_pages} pages; request pages explicitly with at most "
                f"{MAX_PDF_PAGES_PER_READ} pages per read"
            )
        return [(1, total_pages)], None

    normalized = []
    for item in requested:
        if item["start_page"] > total_pages:
            return [], (
                f"start_page {item['start_page']} exceeds PDF page count {total_pages}"
            )
        normalized.append((item["start_page"], min(item["end_page"], total_pages)))
    merged = _merge_ranges(normalized)
    selected_count = sum(end - start + 1 for start, end in merged)
    if selected_count > MAX_PDF_PAGES_PER_READ:
        return [], (
            f"requested PDF read covers {selected_count} pages; maximum is "
            f"{MAX_PDF_PAGES_PER_READ} pages"
        )
    return merged, None


def _merge_ranges(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge inclusive overlapping or adjacent page ranges in source order."""

    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _pdf_error(path: str, error: str) -> dict:
    return {"ok": False, "tool": TOOL_NAME, "path": path, "error": error}
