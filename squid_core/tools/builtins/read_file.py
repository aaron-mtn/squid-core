"""Expose the model-facing read_file schema, validation, and safe dispatch."""

from pathlib import Path
from typing import Any

from squid_core.file_readers import (
    FileReadContext,
    GeneratedImageStore,
    SnapshotAsset,
    read_with_registered_reader,
)
from squid_core.file_readers.pdf import MAX_PDF_PAGES_PER_READ
from squid_core.image_content import (
    SUPPORTED_IMAGE_SUFFIXES,
    UNSUPPORTED_IMAGE_SUFFIXES,
    VISION_UNAVAILABLE_MESSAGE,
)
from squid_core.settings import ReadFileSettings


TOOL_NAME = "read_file"
DEFAULT_SETTINGS = ReadFileSettings()
MAX_REQUESTED_RANGES = 8
MAX_REQUESTED_PAGE_RANGES = 8


def tool_definition() -> dict:
    """Return the read_file definition shown to the model."""

    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "Read a UTF-8 text file, DOCX document or EPUB book, XLSX or ODS "
                "spreadsheet, PDF, "
                "or supported PNG, JPG/JPEG, or WebP image at an absolute path. Use "
                "ranges for one or several known relevant 1-based inclusive lines of "
                "text, deterministic DOCX text, or EPUB text. Spreadsheet reads "
                "return "
                "formula-preserving workbook text and instead use sheet and optional "
                "rows. PDFs instead use pages for 1-based inclusive page ranges; the "
                "selected pages are text-extracted and lazily rendered to session PNG "
                "files. Images are whole-image reads. Combine regions needed for the "
                "same decision. Omit ranges only when file-wide context is needed; "
                "full-file text and DOCX reads return at most "
                f"{DEFAULT_SETTINGS.default_max_lines} lines. Small ranges may include "
                "surrounding context, and total normalized coverage must stay within "
                f"the {DEFAULT_SETTINGS.default_max_lines}-line limit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file",
                    },
                    "ranges": {
                        "type": "array",
                        "description": (
                            "Optional targeted 1-based inclusive regions in this file"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "start_line": {"type": "integer"},
                                "end_line": {"type": "integer"},
                            },
                            "required": ["start_line", "end_line"],
                            "additionalProperties": False,
                        },
                        "minItems": 1,
                        "maxItems": MAX_REQUESTED_RANGES,
                    },
                    "pages": {
                        "type": "array",
                        "description": (
                            "Optional targeted 1-based inclusive PDF page ranges. "
                            "Valid only for PDF files; omit to read all pages when the "
                            f"document has at most {MAX_PDF_PAGES_PER_READ} pages."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "start_page": {"type": "integer"},
                                "end_page": {"type": "integer"},
                            },
                            "required": ["start_page", "end_page"],
                            "additionalProperties": False,
                        },
                        "minItems": 1,
                        "maxItems": MAX_REQUESTED_PAGE_RANGES,
                    },
                    "sheet": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Optional exact sheet name for an XLSX or ODS read"
                        ),
                    },
                    "rows": {
                        "type": "object",
                        "description": (
                            "Optional 1-based inclusive source row range within the "
                            "selected spreadsheet sheet"
                        ),
                        "properties": {
                            "start_row": {"type": "integer"},
                            "end_row": {"type": "integer"},
                        },
                        "required": ["start_row", "end_row"],
                        "additionalProperties": False,
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    }


def execute(
    _repo_root: Path,
    path: str,
    ranges: list[dict[str, int]] | None = None,
    pages: list[dict[str, int]] | None = None,
    sheet: str | None = None,
    rows: dict[str, int] | None = None,
    *,
    settings: ReadFileSettings = DEFAULT_SETTINGS,
    snapshot_asset: SnapshotAsset | None = None,
    ensure_generated_image: GeneratedImageStore | None = None,
    image_input_supported: bool | None = None,
) -> dict:
    """Validate model-facing arguments and dispatch to the shared reader system."""

    file_path = Path(path)
    if not file_path.is_absolute():
        return _tool_error(path, "read_file requires an absolute path")

    range_error = _validate_read_ranges(ranges)
    if range_error is not None:
        return _tool_error(path, range_error)

    page_error = _validate_page_ranges(pages)
    if page_error is not None:
        return _tool_error(path, page_error)

    spreadsheet_error = _validate_spreadsheet_selection(sheet, rows)
    if spreadsheet_error is not None:
        return _tool_error(path, spreadsheet_error)

    suffix = file_path.suffix.lower()
    is_spreadsheet = suffix in {".xlsx", ".ods"}
    if suffix != ".pdf" and pages is not None:
        return _tool_error(path, "page ranges are supported only for PDF files")
    if not is_spreadsheet and (sheet is not None or rows is not None):
        return _tool_error(
            path, "sheet and rows are supported only for XLSX and ODS files"
        )
    if suffix in UNSUPPORTED_IMAGE_SUFFIXES:
        return _tool_error(path, f"unsupported image extension: {suffix}")
    if suffix in SUPPORTED_IMAGE_SUFFIXES and image_input_supported is False:
        return _tool_error(path, VISION_UNAVAILABLE_MESSAGE)

    return read_with_registered_reader(
        FileReadContext(
            file_path=file_path,
            path=path,
            ranges=ranges,
            pages=pages,
            sheet=sheet,
            rows=rows,
            settings=settings,
            snapshot_asset=snapshot_asset,
            ensure_generated_image=ensure_generated_image,
        )
    )


def _tool_error(path: str, error: str) -> dict:
    return {"ok": False, "tool": TOOL_NAME, "path": path, "error": error}


def _validate_range(start_line: int, end_line: int) -> str | None:
    """Validate one caller-supplied 1-based inclusive line range."""

    if start_line < 1:
        return "start_line must be >= 1"
    if end_line < 1:
        return "end_line must be >= 1"
    if end_line < start_line:
        return "end_line must be >= start_line"
    return None


def _validate_read_ranges(ranges: object) -> str | None:
    """Validate the optional public targeted-read representation."""

    if ranges is None:
        return None
    if not isinstance(ranges, list) or not ranges:
        return "ranges must be a non-empty list"
    if len(ranges) > MAX_REQUESTED_RANGES:
        return f"ranges may contain at most {MAX_REQUESTED_RANGES} items"

    for index, item in enumerate(ranges):
        if not isinstance(item, dict):
            return f"ranges[{index}] must be an object"
        if "start_line" not in item or "end_line" not in item:
            return f"ranges[{index}] requires start_line and end_line"
        if set(item) != {"start_line", "end_line"}:
            return f"ranges[{index}] may contain only start_line and end_line"
        item_start = item["start_line"]
        item_end = item["end_line"]
        if not _is_line_number(item_start) or not _is_line_number(item_end):
            return f"ranges[{index}] start_line and end_line must be integers"
        error = _validate_range(item_start, item_end)
        if error is not None:
            return f"ranges[{index}]: {error}"
    return None


def _validate_page_ranges(pages: object) -> str | None:
    """Validate the PDF-only page selection independently of line ranges."""

    if pages is None:
        return None
    if not isinstance(pages, list) or not pages:
        return "pages must be a non-empty list"
    if len(pages) > MAX_REQUESTED_PAGE_RANGES:
        return f"pages may contain at most {MAX_REQUESTED_PAGE_RANGES} items"

    for index, item in enumerate(pages):
        if not isinstance(item, dict):
            return f"pages[{index}] must be an object"
        if set(item) != {"start_page", "end_page"}:
            return f"pages[{index}] requires only start_page and end_page"
        start_page = item["start_page"]
        end_page = item["end_page"]
        if not _is_line_number(start_page) or not _is_line_number(end_page):
            return f"pages[{index}] start_page and end_page must be integers"
        if start_page < 1:
            return f"pages[{index}]: start_page must be >= 1"
        if end_page < start_page:
            return f"pages[{index}]: end_page must be >= start_page"
    return None


def _validate_spreadsheet_selection(sheet: object, rows: object) -> str | None:
    """Validate the deliberately small spreadsheet-only selection contract."""

    if sheet is not None and (not isinstance(sheet, str) or not sheet):
        return "sheet must be a non-empty string"
    if rows is None:
        return None
    if sheet is None:
        return "rows requires a selected sheet"
    if not isinstance(rows, dict):
        return "rows must be an object"
    if set(rows) != {"start_row", "end_row"}:
        return "rows requires only start_row and end_row"
    start_row = rows["start_row"]
    end_row = rows["end_row"]
    if not _is_line_number(start_row) or not _is_line_number(end_row):
        return "rows start_row and end_row must be integers"
    if start_row < 1:
        return "rows: start_row must be >= 1"
    if end_row < start_row:
        return "rows: end_row must be >= start_row"
    return None


def _is_line_number(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
