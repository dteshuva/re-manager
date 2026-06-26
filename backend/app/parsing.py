"""CSV/Excel parsing for bulk import.

This is *one* producer of :class:`ImportRow` rows. It reads a spreadsheet plus a
column-mapping (canonical field → source column header) and emits structured rows for the
import core (:mod:`app.importer`). Keeping parsing separate from the core is the whole
point of the import seam: a smarter rent-statement/PDF parser can be dropped in later and
feed the exact same rows into the exact same endpoint.
"""

from __future__ import annotations

import io
from datetime import date, datetime

import pandas as pd
from dateutil import parser as dateparser

from app.schemas import ImportIssue, ImportRow

# Canonical fields a mapping may reference. At least one property key, one category key,
# plus month and amount are required.
CANONICAL_FIELDS = (
    "property",
    "property_id",
    "unit",
    "unit_id",
    "month",
    "category",
    "category_id",
    "classification",
    "amount",
)
_CLASSIFICATIONS = {"rent", "operating", "capex", "debt_service", "other_below_line"}

# A ready-to-edit template users can download and fill in.
TEMPLATE_CSV = (
    "Property,Unit,Month,Category,Amount\n"
    "Maple Court Apartments,101,2026-04,Rent,1525\n"
    "Maple Court Apartments,101,2026-04,Repairs & Maintenance,120\n"
    "Maple Court Apartments,,2026-04,Mortgage,2600\n"
    "Birch Street House,,2026-04,Rent,2400\n"
)
TEMPLATE_MAPPING = {
    "property": "Property",
    "unit": "Unit",
    "month": "Month",
    "category": "Category",
    "amount": "Amount",
}


def _read_dataframe(content: bytes, filename: str) -> pd.DataFrame:
    name = (filename or "").lower()
    buf = io.BytesIO(content)
    if name.endswith(".csv") or name.endswith(".txt"):
        return pd.read_csv(buf, dtype=str, keep_default_na=False)
    if name.endswith(".xlsx") or name.endswith(".xls"):
        return pd.read_excel(buf, dtype=str).fillna("")
    raise ValueError(f"Unsupported file type '{filename}'. Use .csv or .xlsx.")


def _coerce_month(raw: str) -> date:
    # Default missing day/year-parts to the 1st; the importer re-floors to month start.
    dt = dateparser.parse(str(raw), default=datetime(2000, 1, 1))
    return dt.date().replace(day=1)


def _coerce_amount(raw: str) -> float:
    s = str(raw).strip().replace("$", "").replace(",", "")
    neg = s.startswith("(") and s.endswith(")")  # accounting negatives: (500)
    if neg:
        s = s[1:-1]
    val = float(s)
    return -val if neg else val


def parse_table(
    content: bytes, filename: str, mapping: dict[str, str]
) -> tuple[list[ImportRow], list[ImportIssue]]:
    """Parse a CSV/Excel file into structured rows + per-row parse issues."""
    errors: list[ImportIssue] = []

    unknown = set(mapping) - set(CANONICAL_FIELDS)
    if unknown:
        errors.append(ImportIssue(message=f"Unknown mapping field(s): {sorted(unknown)}"))
        return [], errors

    if not (mapping.get("property") or mapping.get("property_id")):
        errors.append(ImportIssue(message="Mapping must include 'property' or 'property_id'"))
    if not (mapping.get("category") or mapping.get("category_id")):
        errors.append(ImportIssue(message="Mapping must include 'category' or 'category_id'"))
    for required in ("month", "amount"):
        if not mapping.get(required):
            errors.append(ImportIssue(message=f"Mapping must include '{required}'"))
    if errors:
        return [], errors

    try:
        df = _read_dataframe(content, filename)
    except Exception as exc:  # noqa: BLE001 — surface any reader failure as a file-level issue
        return [], [ImportIssue(message=f"Could not read file: {exc}")]

    missing_cols = [src for src in mapping.values() if src not in df.columns]
    if missing_cols:
        return [], [ImportIssue(message=f"Columns not found in file: {missing_cols}. Found: {list(df.columns)}")]

    rows: list[ImportRow] = []
    for idx, record in df.iterrows():
        source_row = int(idx) + 2  # +1 for 0-based, +1 for the header row
        cell = {field: str(record[src]).strip() for field, src in mapping.items()}

        # Skip fully blank lines silently.
        if not any(cell.values()):
            continue

        row_errs: list[ImportIssue] = []

        month = None
        try:
            month = _coerce_month(cell["month"])
        except (ValueError, OverflowError, TypeError):
            row_errs.append(ImportIssue(row=source_row, field="month", message=f"Unparseable month '{cell['month']}'"))

        amount = None
        try:
            amount = _coerce_amount(cell["amount"])
        except (ValueError, TypeError):
            row_errs.append(ImportIssue(row=source_row, field="amount", message=f"Unparseable amount '{cell['amount']}'"))

        classification = cell.get("classification") or None
        if classification is not None:
            classification = classification.lower()
            if classification not in _CLASSIFICATIONS:
                row_errs.append(
                    ImportIssue(row=source_row, field="classification", message=f"Invalid classification '{classification}'")
                )
                classification = None

        if row_errs:
            errors.extend(row_errs)
            continue

        rows.append(
            ImportRow(
                property=cell.get("property") or None,
                property_id=cell.get("property_id") or None,
                unit=cell.get("unit") or None,
                unit_id=cell.get("unit_id") or None,
                month=month,
                category=cell.get("category") or None,
                category_id=cell.get("category_id") or None,
                classification=classification,
                amount=amount,
                source_row=source_row,
            )
        )

    return rows, errors
