"""Read OpenDocument spreadsheets into bounded, formula-preserving text."""

from .context import FileReadContext
from .spreadsheet import (
    MAX_SHEET_CELLS,
    SpreadsheetSheet,
    check_spreadsheet_size,
    reject_spreadsheet_ranges,
    select_spreadsheet_content,
    spreadsheet_error,
    spreadsheet_result,
)


def read_ods(context: FileReadContext) -> dict:
    """Read displayed cell values and formula source from an ODS workbook."""

    validation_error = reject_spreadsheet_ranges(context) or check_spreadsheet_size(
        context
    )
    if validation_error is not None:
        return validation_error

    try:
        from odf.opendocument import load
        from odf.table import Table

        document = load(str(context.file_path))
        sheets = [
            _read_table(table)
            for table in document.spreadsheet.getElementsByType(Table)
        ]
    except Exception as error:
        return spreadsheet_error(
            context.path, f"invalid or unreadable ODS spreadsheet: {error}"
        )
    selected = select_spreadsheet_content(context, sheets)
    if isinstance(selected, dict):
        return selected
    return spreadsheet_result(context, "ods", selected)


def _read_table(table) -> SpreadsheetSheet:
    """Expand meaningful ODF repetitions while ignoring repeated trailing blanks."""

    from odf.namespaces import TABLENS
    from odf.table import TableRow

    name = table.getAttribute("name") or "Untitled"
    logical_row = 1
    populated: list[tuple[int, int, object]] = []
    for row in table.getElementsByType(TableRow):
        repeat = _positive_integer(row.getAttribute("numberrowsrepeated"))
        row_values = _row_values(row, TABLENS)
        if row_values:
            if (logical_row + repeat - 1) * len(row_values) > MAX_SHEET_CELLS:
                raise ValueError(
                    f"sheet {name!r} exceeds the maximum supported area of "
                    f"{MAX_SHEET_CELLS} cells"
                )
            for repeated_row in range(logical_row, logical_row + repeat):
                populated.extend(
                    (repeated_row, column, value)
                    for column, value in enumerate(row_values, start=1)
                    if value is not None
                )
        logical_row += repeat

    if not populated:
        return SpreadsheetSheet(name, None, None, ())
    min_row = min(row for row, _, _ in populated)
    max_row = max(row for row, _, _ in populated)
    min_column = min(column for _, column, _ in populated)
    max_column = max(column for _, column, _ in populated)
    if (max_row - min_row + 1) * (max_column - min_column + 1) > MAX_SHEET_CELLS:
        raise ValueError(
            f"sheet {name!r} used range exceeds the maximum supported area of "
            f"{MAX_SHEET_CELLS} cells"
        )
    values = {(row, column): value for row, column, value in populated}
    rows = tuple(
        tuple(
            values.get((row, column))
            for column in range(min_column, max_column + 1)
        )
        for row in range(min_row, max_row + 1)
    )
    return SpreadsheetSheet(name, min_row, min_column, rows)


def _row_values(row, table_namespace: str) -> list[object]:
    """Return cells through the last meaningful cell, honoring column repeats."""

    cells = [
        node
        for node in row.childNodes
        if getattr(node, "qname", None)
        in {
            (table_namespace, "table-cell"),
            (table_namespace, "covered-table-cell"),
        }
    ]
    entries: list[tuple[int, object]] = []
    last_meaningful = -1
    for cell in cells:
        repeat = _positive_integer(cell.getAttribute("numbercolumnsrepeated"))
        value = _ods_cell_value(cell)
        entries.append((repeat, value))
        if value is not None:
            last_meaningful = len(entries) - 1
    if last_meaningful < 0:
        return []

    values: list[object] = []
    for repeat, value in entries[: last_meaningful + 1]:
        if len(values) + repeat > MAX_SHEET_CELLS:
            raise ValueError(
                f"row exceeds the maximum supported width of {MAX_SHEET_CELLS} cells"
            )
        values.extend([value] * repeat)
    return values


def _ods_cell_value(cell) -> object:
    """Prefer displayed ODF text and append the exact formula when one exists."""

    from odf import teletype

    formula = cell.getAttribute("formula")
    displayed = teletype.extractText(cell).strip()
    if not displayed:
        for attribute in (
            "stringvalue",
            "datevalue",
            "timevalue",
            "booleanvalue",
            "value",
        ):
            raw = cell.getAttribute(attribute)
            if raw not in (None, ""):
                displayed = str(raw)
                break
    if formula:
        return (
            f"{displayed} [formula: {formula}]"
            if displayed
            else f"[formula: {formula}]"
        )
    return displayed or None


def _positive_integer(value) -> int:
    """Normalize optional ODF repetition attributes."""

    if value in (None, ""):
        return 1
    number = int(value)
    if number < 1:
        raise ValueError("ODS repetition counts must be positive")
    return number
