"""Shared rendering and result shaping for read-only spreadsheet readers."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from .container import ContainerSafetyError, inspect_zip_container
from .context import FileReadContext


TOOL_NAME = "read_file"
MAX_SPREADSHEET_BYTES = 64 * 1024 * 1024
MAX_SPREADSHEET_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_SHEET_CELLS = 1_000_000


@dataclass(frozen=True)
class SpreadsheetSheet:
    """A normalized rectangular sheet ready for model-facing text rendering."""

    name: str
    start_row: int | None
    start_column: int | None
    rows: tuple[tuple[object, ...], ...]
    # A selected slice still reports the sheet's complete used range so the
    # caller can understand where the returned rows sit in the source.
    source_used_range: tuple[int, int, int, int] | None = None


def spreadsheet_error(path: str, error: str) -> dict:
    """Return the ordinary structured Tool failure used by both formats."""

    return {"ok": False, "tool": TOOL_NAME, "path": path, "error": error}


def reject_spreadsheet_ranges(context: FileReadContext) -> dict | None:
    """Keep text line ranges separate from spreadsheet source-row selection."""

    if context.ranges is not None:
        return spreadsheet_error(
            context.path,
            "line ranges are not supported for spreadsheet files; use sheet and rows",
        )
    return None


def check_spreadsheet_size(context: FileReadContext) -> dict | None:
    """Reject unusually large containers before a parser expands their contents."""

    try:
        inspect_zip_container(
            context.file_path,
            max_file_bytes=MAX_SPREADSHEET_BYTES,
            max_expanded_bytes=MAX_SPREADSHEET_EXPANDED_BYTES,
        )
    except ContainerSafetyError as error:
        if error.reason == "unreadable_file":
            return spreadsheet_error(
                context.path, f"unreadable spreadsheet: {error.detail}"
            )
        if error.reason == "invalid_container":
            return spreadsheet_error(
                context.path,
                f"invalid or unreadable spreadsheet container: {error.detail}",
            )
        if error.reason == "file_too_large":
            assert error.actual_bytes is not None
            assert error.maximum_bytes is not None
            return spreadsheet_error(
                context.path,
                f"spreadsheet is {error.actual_bytes} bytes; maximum supported size is "
                f"{error.maximum_bytes} bytes",
            )
        if error.reason == "too_many_members":
            assert error.actual_members is not None
            assert error.maximum_members is not None
            return spreadsheet_error(
                context.path,
                f"spreadsheet contains {error.actual_members} ZIP members; maximum "
                f"supported count is {error.maximum_members}",
            )
        assert error.reason == "expanded_too_large"
        assert error.actual_bytes is not None
        assert error.maximum_bytes is not None
        return spreadsheet_error(
            context.path,
            f"expanded spreadsheet is {error.actual_bytes} bytes; maximum supported "
            f"size is {error.maximum_bytes} bytes",
        )
    return None


def spreadsheet_result(
    context: FileReadContext,
    format_name: str,
    sheets: list[SpreadsheetSheet],
) -> dict:
    """Render workbook data and apply the ordinary full-read line ceiling."""

    full_content = _render_workbook(format_name, sheets, context.sheet, context.rows)
    content = full_content
    lines = full_content.splitlines(keepends=True)
    total_lines = len(lines)
    max_lines = context.settings.default_max_lines
    returned_ranges: list[dict[str, int]] | None = None
    if total_lines > max_lines:
        head_count = max_lines // 2
        tail_count = max_lines - head_count
        tail_start = total_lines - tail_count + 1
        content = (
            "".join(lines[:head_count])
            + f"<<< CODE SQUID: lines {head_count + 1}-{tail_start - 1} "
            "omitted from this spreadsheet read >>>\n"
            + "".join(lines[-tail_count:])
        )
        returned_ranges = [
            {"start_line": 1, "end_line": head_count},
            {"start_line": tail_start, "end_line": total_lines},
        ]

    result = {
        "ok": True,
        "tool": TOOL_NAME,
        "path": context.path,
        "media_type": "spreadsheet",
        "format": format_name,
        "sheet_names": [sheet.name for sheet in sheets],
        "content": content,
        "content_truncated": returned_ranges is not None,
        "content_original_chars": len(full_content),
        "content_returned_chars": len(content),
        "total_lines": total_lines,
        "ranged": False,
    }
    if context.sheet is not None:
        result["selected_sheet"] = context.sheet
    if context.rows is not None:
        result["selected_row_range"] = dict(context.rows)
    if returned_ranges is not None:
        result["returned_line_ranges"] = returned_ranges
        result["truncation_guidance"] = (
            f"Spreadsheet text was truncated to {max_lines} of {total_lines} lines; "
            "select a sheet and narrower rows to recover omitted workbook content."
        )
    return result


def select_spreadsheet_content(
    context: FileReadContext, sheets: list[SpreadsheetSheet]
) -> list[SpreadsheetSheet] | dict:
    """Apply the validated exact-sheet and optional source-row selection."""

    if context.sheet is None:
        return sheets
    selected = next((item for item in sheets if item.name == context.sheet), None)
    if selected is None:
        return spreadsheet_error(
            context.path, f"unknown spreadsheet sheet: {context.sheet!r}"
        )
    if context.rows is None:
        return [selected]
    return [_slice_sheet(selected, context.rows["start_row"], context.rows["end_row"])]


def _slice_sheet(
    sheet: SpreadsheetSheet, start_row: int, end_row: int
) -> SpreadsheetSheet:
    """Return populated cells intersecting one inclusive source-row interval."""

    used_range = _used_range(sheet)
    if used_range is None:
        return SpreadsheetSheet(sheet.name, None, None, (), None)
    used_start_row, used_end_row, used_start_column, _used_end_column = used_range
    returned_start = max(start_row, used_start_row)
    returned_end = min(end_row, used_end_row)
    if returned_start > returned_end:
        return SpreadsheetSheet(sheet.name, None, None, (), used_range)

    first_offset = returned_start - used_start_row
    last_offset = returned_end - used_start_row + 1
    return SpreadsheetSheet(
        sheet.name,
        returned_start,
        used_start_column,
        sheet.rows[first_offset:last_offset],
        used_range,
    )


def _used_range(sheet: SpreadsheetSheet) -> tuple[int, int, int, int] | None:
    if not sheet.rows:
        return sheet.source_used_range
    assert sheet.start_row is not None and sheet.start_column is not None
    return (
        sheet.start_row,
        sheet.start_row + len(sheet.rows) - 1,
        sheet.start_column,
        sheet.start_column + max(len(row) for row in sheet.rows) - 1,
    )


def _render_workbook(
    format_name: str,
    sheets: list[SpreadsheetSheet],
    selected_sheet: str | None,
    selected_rows: dict[str, int] | None,
) -> str:
    """Produce stable TSV-like text with explicit workbook and sheet boundaries."""

    names = ", ".join(_quote_name(sheet.name) for sheet in sheets) or "(none)"
    parts = [
        f"Workbook: spreadsheet ({format_name})\n",
        f"Sheets ({len(sheets)}): {names}\n",
    ]
    if selected_sheet is not None:
        parts.append(f"Selected sheet: {_render_cell(selected_sheet)}\n")
    if selected_rows is not None:
        parts.append(
            f"Selected source rows: {selected_rows['start_row']}-"
            f"{selected_rows['end_row']} (inclusive)\n"
        )
    for sheet in sheets:
        parts.append(f"\n<<< SHEET: {_render_cell(sheet.name)} >>>\n")
        if not sheet.rows:
            source_range = sheet.source_used_range
            if source_range is None:
                parts.extend(["Used range: empty\n", "[empty sheet]\n"])
            else:
                parts.extend(
                    [
                        f"Used range: {_format_used_range(source_range)}\n",
                        "[no populated cells in selected rows]\n",
                    ]
                )
            continue

        assert sheet.start_row is not None and sheet.start_column is not None
        end_row = sheet.start_row + len(sheet.rows) - 1
        end_column = sheet.start_column + max(len(row) for row in sheet.rows) - 1
        returned_range = (
            sheet.start_row,
            end_row,
            sheet.start_column,
            end_column,
        )
        parts.append(
            "Used range: "
            f"{_format_used_range(sheet.source_used_range or returned_range)}\n"
        )
        if sheet.source_used_range is not None:
            parts.append(f"Returned range: {_format_used_range(returned_range)}\n")
        headers = ["Row"] + [
            _column_name(column)
            for column in range(sheet.start_column, end_column + 1)
        ]
        parts.append("\t".join(headers) + "\n")
        for offset, row in enumerate(sheet.rows):
            values = [_render_cell(value) for value in row]
            values.extend([""] * (end_column - sheet.start_column + 1 - len(values)))
            parts.append(
                "\t".join([str(sheet.start_row + offset), *values]).rstrip("\t")
                + "\n"
            )
    return "".join(parts)


def _format_used_range(bounds: tuple[int, int, int, int]) -> str:
    start_row, end_row, start_column, end_column = bounds
    return (
        f"{_column_name(start_column)}{start_row}:"
        f"{_column_name(end_column)}{end_row}"
    )


def _render_cell(value: object) -> str:
    """Render common scalar types without allowing cells to break TSV rows."""

    if value is None:
        return ""
    if isinstance(value, (datetime, date, time)):
        text = value.isoformat()
    elif isinstance(value, timedelta):
        text = str(value)
    elif isinstance(value, Decimal):
        text = format(value, "f")
    else:
        text = str(value)
    return (
        text.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def _quote_name(name: str) -> str:
    rendered = _render_cell(name)
    return repr(rendered) if "," in rendered else rendered


def _column_name(number: int) -> str:
    """Return an Excel-style 1-based column label without format dependencies."""

    label = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        label = chr(65 + remainder) + label
    return label
