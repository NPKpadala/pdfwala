"""
PDFWala V10.0
utils/office_utils.py — Excel / Office document helpers.
"""

from datetime import datetime, date
from decimal import Decimal
from typing import Any, List, Optional


def coerce_cell_value(value: Any) -> Any:
    """
    Coerce an Excel cell value to a JSON-serialisable Python type.
    Formerly _coerce_cell_value().
    """
    if value is None:                         return None
    if isinstance(value, bool):               return value
    if isinstance(value, int):                return value
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e15:
            return int(value)
        return value
    if isinstance(value, (datetime, date)):   return value.isoformat()
    if isinstance(value, Decimal):
        f = float(value)
        return int(f) if f == int(f) else f
    return str(value)


def coerce_cell_for_csv(value: Any) -> str:
    """
    Coerce an Excel cell value to a CSV-safe string.
    Formerly _coerce_cell_for_csv().
    """
    if value is None:                         return ""
    if isinstance(value, bool):               return str(value)
    if isinstance(value, int):                return str(value)
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e15:
            return str(int(value))
        return f"{value:.10g}"
    if isinstance(value, (datetime, date)):   return value.isoformat()
    if isinstance(value, Decimal):            return str(value)
    return str(value)


def detect_excel_headers(rows: List[tuple]) -> Optional[List[str]]:
    """
    Heuristically detect whether the first row of an Excel sheet is a header.
    Returns a normalised list of header names, or None if no header detected.
    """
    if not rows or not rows[0]:
        return None
    first = rows[0]
    # If first row is all strings and subsequent rows contain non-strings → header
    first_all_str = all(v is None or isinstance(v, str) for v in first)
    if not first_all_str:
        return None
    seen: dict = {}
    headers = []
    for i, h in enumerate(first):
        base = str(h).strip() if h is not None else ""
        if not base:
            base = f"col_{i}"
        cnt = seen.get(base, 0)
        seen[base] = cnt + 1
        headers.append(base if cnt == 0 else f"{base}_{cnt}")
    return headers


def is_excel_formula(value: Any) -> bool:
    """Return True if the cell value looks like an Excel formula."""
    return isinstance(value, str) and value.startswith("=")


def clean_excel_data(rows: List[tuple]) -> List[tuple]:
    """
    Remove fully empty rows and trailing empty columns from a row-list.
    """
    if not rows:
        return []
    # Drop fully empty rows
    filtered = [r for r in rows if any(v is not None and v != "" for v in r)]
    if not filtered:
        return []
    # Find rightmost non-empty column
    max_col = 0
    for row in filtered:
        for i, v in enumerate(row):
            if v is not None and v != "":
                max_col = max(max_col, i)
    return [row[: max_col + 1] for row in filtered]
