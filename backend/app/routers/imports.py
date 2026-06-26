"""Bulk import endpoints + the 'what's missing' month view.

Two producers feed the same import core:
  * ``POST /import/rows`` — structured rows as JSON (the automation seam; what a future
    document parser would call).
  * ``POST /import/file`` — CSV/Excel upload + a column mapping, parsed then applied.

Both support ``dry_run`` (validate + report, write nothing) so a user can preview, fix,
and re-upload. Idempotency and lock protection live in :mod:`app.importer`.
"""

import json
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.importer import apply_import
from app.models import MonthlyRecord, Property, Unit, User
from app.parsing import TEMPLATE_CSV, parse_table
from app.schemas import ImportReport, ImportRow, MissingScope

router = APIRouter(prefix="/import", tags=["import"])

_ON_ERROR = {"abort", "skip"}


def _check_on_error(on_error: str) -> None:
    if on_error not in _ON_ERROR:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"on_error must be one of {sorted(_ON_ERROR)}"
        )


@router.get("/template", response_class=PlainTextResponse)
def import_template(_u: User = Depends(get_current_user)) -> str:
    """A ready-to-edit CSV template (Property, Unit, Month, Category, Amount)."""
    return TEMPLATE_CSV


@router.post("/rows", response_model=ImportReport)
def import_rows(
    rows: list[ImportRow],
    dry_run: bool = False,
    on_error: str = "abort",
    db: Session = Depends(get_db),
    _u: User = Depends(get_current_user),
):
    """Apply structured rows directly (no file). This is the seam every parser targets."""
    _check_on_error(on_error)
    return apply_import(db, rows, dry_run=dry_run, on_error=on_error)


@router.post("/file", response_model=ImportReport)
async def import_file(
    file: UploadFile = File(...),
    mapping: str = Form(..., description='JSON object: canonical field -> source column, e.g. {"property":"Property","month":"Month","category":"Category","amount":"Amount"}'),
    dry_run: bool = Form(False),
    on_error: str = Form("abort"),
    db: Session = Depends(get_db),
    _u: User = Depends(get_current_user),
):
    """Parse a CSV/Excel upload with a column mapping, then apply via the import core."""
    _check_on_error(on_error)
    try:
        mapping_dict = json.loads(mapping)
    except json.JSONDecodeError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="mapping must be valid JSON")
    if not isinstance(mapping_dict, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="mapping must be a JSON object")

    content = await file.read()
    rows, parse_errors = parse_table(content, file.filename or "", mapping_dict)

    # File-level parse problems (bad mapping / unreadable file) → 422, nothing applied.
    if not rows and parse_errors and all(e.row is None for e in parse_errors):
        report = ImportReport(
            dry_run=dry_run, on_error=on_error, committed=False, total_rows=0,
            valid_rows=0, invalid_rows=0, applied_line_items=0, records_touched=0,
            errors=parse_errors,
        )
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=report.model_dump())

    # Parser row-errors are merged into the core so they count toward the abort decision.
    return apply_import(db, rows, dry_run=dry_run, on_error=on_error, extra_errors=parse_errors)


@router.get("/missing", response_model=list[MissingScope])
def whats_missing(
    month: date = Query(..., description="Month to check (any day; floored to month start)"),
    db: Session = Depends(get_db),
    _u: User = Depends(get_current_user),
):
    """List property/unit scopes that have no posted data for the given month.

    Expected scopes: every single-asset property (property-tier), and for each
    multifamily property both each unit and the property tier. A scope counts as having
    data when a monthly_record with at least one line item exists for it that month.
    """
    m = month.replace(day=1)

    # Scopes that already have data this month (record with >=1 line item).
    recs = db.scalars(
        select(MonthlyRecord)
        .where(MonthlyRecord.month == m)
    ).all()
    have = {(r.property_id, r.unit_id) for r in recs if r.line_items}

    missing: list[MissingScope] = []
    props = db.scalars(select(Property).order_by(Property.name)).all()
    for p in props:
        units = (
            db.scalars(select(Unit).where(Unit.property_id == p.id).order_by(Unit.unit_number)).all()
            if p.type == "multifamily"
            else []
        )
        # property-tier scope
        if (p.id, None) not in have:
            missing.append(MissingScope(property_id=p.id, property_name=p.name, type=p.type))
        # unit scopes (multifamily)
        for u in units:
            if (p.id, u.id) not in have:
                missing.append(
                    MissingScope(
                        property_id=p.id, property_name=p.name, type=p.type,
                        unit_id=u.id, unit_number=u.unit_number,
                    )
                )
    return missing
