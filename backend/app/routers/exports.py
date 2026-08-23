"""Excel/CSV export of the existing P&L and variance reports.

No new aggregation logic lives here — every route calls the same :mod:`app.queries`
functions the dashboard/property-detail/variance screens already use, then serializes
the exact same rows to a file. Numbers are written as numbers (never pre-formatted
currency strings) so the file is directly usable in Excel/Sheets. Reads are open to
any authed user, same as the JSON endpoints they mirror.
"""

import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from openpyxl import Workbook
from sqlalchemy.orm import Session

from app import queries
from app.db import get_db
from app.deps import Scope, get_scope
from app.scoping import get_property_or_404

router = APIRouter(prefix="/export", tags=["export"])

FromParam = Query(default=None, alias="from", description="First month (inclusive), YYYY-MM-01")
ToParam = Query(default=None, alias="to", description="Last month (inclusive), YYYY-MM-01")
TagsParam = Query(default=None, description="Optional tag filter (OR semantics); repeatable")
FormatParam = Query(default="xlsx", pattern="^(xlsx|csv)$", description="xlsx or csv")

_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_CSV_MEDIA_TYPE = "text/csv"



def _write_csv(columns: list[str], rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for r in rows:
        writer.writerow([r.get(c) for c in columns])
    return buf.getvalue()


def _write_xlsx(columns: list[str], rows: list[dict], sheet_title: str = "Export") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title[:31]  # Excel sheet-name length limit
    ws.append(columns)
    for r in rows:
        ws.append([r.get(c) for c in columns])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _respond(columns: list[str], rows: list[dict], filename_base: str, fmt: str) -> Response:
    if fmt == "csv":
        content = _write_csv(columns, rows)
        return Response(
            content=content,
            media_type=_CSV_MEDIA_TYPE,
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.csv"'},
        )
    content = _write_xlsx(columns, rows, sheet_title=filename_base)
    return Response(
        content=content,
        media_type=_XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename_base}.xlsx"'},
    )


def _range_suffix(date_from: date | None, date_to: date | None) -> str:
    f = date_from.isoformat() if date_from else "all"
    t = date_to.isoformat() if date_to else "all"
    return f"{f}_{t}"


# ---- Portfolio monthly P&L (mirrors the Dashboard's monthly table) -------------------------
@router.get("/portfolio/monthly")
def export_portfolio_monthly(
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    tags: list[str] | None = TagsParam,
    format: str = FormatParam,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Portfolio P&L by month — same rows as ``GET /portfolio/monthly``, as a file."""
    rows = queries.portfolio_monthly(db, scope.account_id, date_from, date_to, tags)
    columns = ["month", *queries.METRICS]
    filename = f"portfolio-monthly-pnl_{_range_suffix(date_from, date_to)}"
    return _respond(columns, rows, filename, format)


# ---- Property monthly P&L (mirrors PropertyDetail's monthly table, unit-vs-tier split) -----
@router.get("/properties/{property_id}/monthly")
def export_property_monthly(
    property_id: str,
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    format: str = FormatParam,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Property P&L by month — combined total plus the honest unit-rollup vs property-tier
    split, flattened into ``unit_*`` / ``tier_*`` columns alongside the combined metrics."""
    prop = get_property_or_404(db, scope, property_id)
    pnl_rows = queries.property_monthly(db, scope.account_id, property_id, date_from, date_to)
    flat_rows = []
    for r in pnl_rows:
        row = {"month": r["month"]}
        for m in queries.METRICS:
            row[m] = r[m]
        for m in queries.METRICS:
            row[f"unit_{m}"] = r["units"][m]
        for m in queries.METRICS:
            row[f"tier_{m}"] = r["property_tier"][m]
        flat_rows.append(row)
    columns = (
        ["month"]
        + list(queries.METRICS)
        + [f"unit_{m}" for m in queries.METRICS]
        + [f"tier_{m}" for m in queries.METRICS]
    )
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in prop.name).strip() or property_id
    filename = f"{safe_name}-monthly-pnl_{_range_suffix(date_from, date_to)}"
    return _respond(columns, flat_rows, filename, format)


# ---- Variance (actual vs. pro-rated budget plan) -------------------------------------------
_VARIANCE_COLUMNS = [
    "period_from",
    "period_to",
    "total_months",
    "plan_coverage_months",
    "actual_gross_rent",
    "actual_operating_expenses",
    "actual_noi",
    "plan_gross_rent",
    "plan_operating_expenses",
    "plan_noi",
    "variance_gross_rent",
    "variance_operating_expenses",
    "variance_noi",
    "variance_noi_pct",
]


@router.get("/portfolio/variance")
def export_portfolio_variance(
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    format: str = FormatParam,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Portfolio actual-vs-plan variance for the period — same figures as ``GET
    /portfolio/variance``, as a one-row file (plus coverage counts)."""
    v = queries.portfolio_variance(db, scope.account_id, date_from, date_to)
    columns = _VARIANCE_COLUMNS + ["budgeted_property_count", "total_property_count"]
    filename = f"portfolio-variance_{_range_suffix(date_from, date_to)}"
    return _respond(columns, [v], filename, format)


@router.get("/properties/{property_id}/variance")
def export_property_variance(
    property_id: str,
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    format: str = FormatParam,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Property actual-vs-plan variance for the period — same figures as ``GET
    /properties/{id}/variance``, as a one-row file."""
    prop = get_property_or_404(db, scope, property_id)
    v = queries.property_variance(db, scope.account_id, property_id, date_from, date_to)
    columns = ["property_id", "property_name"] + _VARIANCE_COLUMNS
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in prop.name).strip() or property_id
    filename = f"{safe_name}-variance_{_range_suffix(date_from, date_to)}"
    return _respond(columns, [v], filename, format)
