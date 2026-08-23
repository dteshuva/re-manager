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

from app.config import get_settings
from app.db import get_db
from app.deps import Scope, get_scope
from app.importer import apply_import
from app.models import Category, MonthlyRecord, Property, Unit
from app.parsing import TEMPLATE_CSV, parse_table
from app.schemas import (
    ImportReport,
    ImportRow,
    MissingScope,
    StatementBatchItem,
    StatementBatchPreview,
    StatementPreview,
    StatementRow,
    UnknownCategory,
)
from app.statements import extract_statement

router = APIRouter(prefix="/import", tags=["import"])

_ON_ERROR = {"abort", "skip"}


def _check_on_error(on_error: str) -> None:
    if on_error not in _ON_ERROR:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"on_error must be one of {sorted(_ON_ERROR)}"
        )


@router.get("/template", response_class=PlainTextResponse)
def import_template(scope: Scope = Depends(get_scope)) -> str:
    """A ready-to-edit CSV template (Property, Unit, Month, Category, Amount)."""
    return TEMPLATE_CSV


@router.post("/rows", response_model=ImportReport)
def import_rows(
    rows: list[ImportRow],
    dry_run: bool = False,
    on_error: str = "abort",
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Apply structured rows directly (no file). This is the seam every parser targets."""
    _check_on_error(on_error)
    return apply_import(db, scope.account_id, rows, dry_run=dry_run, on_error=on_error)


@router.post("/file", response_model=ImportReport)
async def import_file(
    file: UploadFile = File(...),
    mapping: str = Form(..., description='JSON object: canonical field -> source column, e.g. {"property":"Property","month":"Month","category":"Category","amount":"Amount"}'),
    dry_run: bool = Form(False),
    on_error: str = Form("abort"),
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
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
    return apply_import(
        db, scope.account_id, rows, dry_run=dry_run, on_error=on_error,
        extra_errors=parse_errors,
    )


def _resolve_preview(ex, prop_by_name: dict, cat_by_name: dict) -> StatementPreview:
    """Turn a raw extraction into a review preview: match the detected property and each
    category against what exists, flagging the unknowns. Shared by the single + batch
    endpoints so their resolution behaves identically."""
    matched_prop = prop_by_name.get((ex.property_name or "").strip().lower())
    rows: list[StatementRow] = []
    unknown: dict[str, str] = {}  # lower -> original casing
    for r in ex.rows:
        cat = cat_by_name.get(r["category"].strip().lower())
        if cat is None:
            unknown.setdefault(r["category"].strip().lower(), r["category"].strip())
        rows.append(
            StatementRow(
                unit=r.get("unit"),
                category=r["category"].strip(),
                category_id=cat.id if cat else None,
                unknown_category=cat is None,
                classification=r.get("classification"),
                amount=r["amount"],
            )
        )
    return StatementPreview(
        backend=ex.backend,
        detected_property=ex.property_name,
        property_id=matched_prop.id if matched_prop else None,
        property_unknown=bool(ex.property_name) and matched_prop is None,
        detected_month=ex.month,
        rows=rows,
        unknown_categories=list(unknown.values()),
        warnings=ex.warnings,
    )


def _extract_one(content: bytes, props, cats, settings):
    return extract_statement(
        content,
        known_properties=[p.name for p in props],
        known_categories=[c.name for c in cats],
        max_pages=settings.statement_max_pages,
        use_ollama=settings.statement_use_ollama,
        ollama_url=settings.ollama_url,
        ollama_model=settings.ollama_model,
    )


@router.post("/statement/extract", response_model=StatementPreview)
async def extract_statement_pdf(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Parse a single PDF statement into a review preview. Writes nothing; reads locally
    (heuristic, or a local Ollama model if one is running — both free) and flags anything
    that must be created first. The UI applies the reviewed rows via ``POST /import/rows``."""
    name = (file.filename or "").lower()
    if not name.endswith(".pdf"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Please upload a .pdf statement.")
    content = await file.read()
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="The uploaded file is empty.")

    settings = get_settings()
    props = db.scalars(
        select(Property)
        .where(Property.account_id == scope.account_id)
        .order_by(Property.name)
    ).all()
    cats = db.scalars(
        select(Category).where(Category.account_id == scope.account_id)
    ).all()
    try:
        ex = _extract_one(content, props, cats, settings)
    except Exception as exc:  # pdfplumber raises varied errors on malformed PDFs
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Could not read this PDF: {exc}"
        )

    return _resolve_preview(
        ex,
        {p.name.strip().lower(): p for p in props},
        {c.name.strip().lower(): c for c in cats},
    )


@router.post("/statement/extract-batch", response_model=StatementBatchPreview)
async def extract_statements_batch(
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Parse many PDF statements at once (mixed months/properties). Each file is parsed
    independently — a bad PDF becomes a per-file error, never a failed batch — and the
    unknown properties/categories are de-duplicated across the whole batch so the UI can
    resolve each new item ONCE. Writes nothing."""
    settings = get_settings()
    if len(files) > settings.statement_max_files:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"Too many files ({len(files)}). Upload at most {settings.statement_max_files} at once.",
        )

    props = db.scalars(
        select(Property)
        .where(Property.account_id == scope.account_id)
        .order_by(Property.name)
    ).all()
    cats = db.scalars(
        select(Category).where(Category.account_id == scope.account_id)
    ).all()
    prop_by_name = {p.name.strip().lower(): p for p in props}
    cat_by_name = {c.name.strip().lower(): c for c in cats}

    items: list[StatementBatchItem] = []
    unknown_props: dict[str, str] = {}  # lower -> name
    unknown_cats: dict[str, UnknownCategory] = {}  # lower -> {name, suggested_classification}

    for f in files:
        fname = f.filename or "statement.pdf"
        if not fname.lower().endswith(".pdf"):
            items.append(StatementBatchItem(filename=fname, error="Not a PDF file."))
            continue
        content = await f.read()
        if not content:
            items.append(StatementBatchItem(filename=fname, error="The file is empty."))
            continue
        try:
            ex = _extract_one(content, props, cats, settings)
            preview = _resolve_preview(ex, prop_by_name, cat_by_name)
        except Exception as exc:
            items.append(StatementBatchItem(filename=fname, error=f"Could not read this PDF: {exc}"))
            continue

        items.append(StatementBatchItem(filename=fname, preview=preview))
        if preview.property_unknown and preview.detected_property:
            unknown_props.setdefault(preview.detected_property.strip().lower(), preview.detected_property.strip())
        for r in preview.rows:
            if r.unknown_category:
                key = r.category.strip().lower()
                existing = unknown_cats.get(key)
                # Keep the first suggestion, but fill one in if an earlier row had none.
                if existing is None:
                    unknown_cats[key] = UnknownCategory(name=r.category.strip(), suggested_classification=r.classification)
                elif existing.suggested_classification is None and r.classification:
                    existing.suggested_classification = r.classification

    return StatementBatchPreview(
        items=items,
        unknown_properties=list(unknown_props.values()),
        unknown_categories=list(unknown_cats.values()),
    )


@router.get("/missing", response_model=list[MissingScope])
def whats_missing(
    month: date = Query(..., description="Month to check (any day; floored to month start)"),
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
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
        .where(MonthlyRecord.account_id == scope.account_id, MonthlyRecord.month == m)
    ).all()
    have = {(r.property_id, r.unit_id) for r in recs if r.line_items}

    missing: list[MissingScope] = []
    props = db.scalars(
        select(Property)
        .where(Property.account_id == scope.account_id)
        .order_by(Property.name)
    ).all()
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
