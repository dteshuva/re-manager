"""Bulk import endpoints + the 'what's missing' month view.

Two producers feed the same import core:
  * ``POST /import/rows`` — structured rows as JSON (the automation seam; what a future
    document parser would call).
  * ``POST /import/file`` — CSV/Excel upload + a column mapping, parsed then applied.
  * ``POST /import/statement/extract[-batch]`` — PDF statements parsed locally into a review
    preview, which the UI then applies through ``/import/rows``. One PDF can hold one
    property's statement or a whole portfolio's rent roll; the batch form returns the
    latter as one statement per property.

Both support ``dry_run`` (validate + report, write nothing) so a user can preview, fix,
and re-upload. Idempotency and lock protection live in :mod:`app.importer`.
"""

import json
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import Scope, get_scope
from app.importer import apply_import
from app.models import Category, MonthlyRecord, Property, StatementFormat, Unit
from app.parsing import TEMPLATE_CSV, parse_table
from app.schemas import (
    ImportReport,
    ImportRow,
    MissingScope,
    StatementBatchItem,
    StatementBatchPreview,
    StatementFileNote,
    StatementFormatIn,
    StatementFormatOut,
    StatementPreview,
    StatementRow,
    UnknownCategory,
)
from app.statements import StatementFile, extract_statements, layout_similarity

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


def _resolve_preview(ex, prop_by_name: dict, cat_by_name: dict, fmt: str = "single") -> StatementPreview:
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
                kind=r.get("kind", "line"),
                note=r.get("note"),
                raw_category=(r.get("raw_category") or r["category"]).strip(),
            )
        )
    return StatementPreview(
        backend=ex.backend,
        format=fmt,
        detected_property=ex.property_name,
        raw_property=ex.raw_property_name or ex.property_name,
        property_id=matched_prop.id if matched_prop else None,
        property_unknown=bool(ex.property_name) and matched_prop is None,
        detected_month=ex.month,
        rows=rows,
        unknown_categories=list(unknown.values()),
        warnings=ex.warnings,
    )


# --------------------------------------------------------------- saved statement formats
# What a format knows is what a PARSER cannot: that this sender's "Mgt Fee" is the account's
# "Management Fee", that "64 King Edward Street" is the property stored as "64 King Edward
# St, Gateshead", that this sender's statements should post to the tenancy month rather than
# the one it declares. None of that is readable off a single statement and all of it is the
# same in the next one, so it is remembered against the layout's fingerprint — see migration
# 0028 and :func:`app.statements.fingerprint`.
#
# Everything here happens between extraction and the review screen. A saved format changes
# what is PROPOSED; the operator still reads it and still presses the button, so a mapping
# that has gone stale stays visible and one edit away rather than quietly posting for months.

_CLOSE_ENOUGH = 0.75  # boilerplate overlap at which two layouts are the same sender


def _find_format(
    db: Session, account_id: str, result: StatementFile
) -> tuple[StatementFormat | None, str | None]:
    """The saved format for this layout, and how it was matched ("exact" or "close").

    An exact fingerprint is the normal case. The near match exists because a fingerprint is
    all-or-nothing about boilerplate, and an agent that adds a line to its covering note has
    not become a different agent — losing the saved mappings over that would hand the
    operator back a format they had already taught.
    """
    if not result.fingerprint:
        return None, None
    saved = db.scalars(
        select(StatementFormat).where(StatementFormat.account_id == account_id)
    ).all()
    for fmt in saved:
        if fmt.fingerprint == result.fingerprint:
            return fmt, "exact"
    best, best_score = None, 0.0
    for fmt in saved:
        if fmt.shape != result.format or not fmt.sample:
            continue
        score = layout_similarity(result.sample, fmt.sample)
        if score > best_score:
            best, best_score = fmt, score
    if best is not None and best_score >= _CLOSE_ENOUGH:
        return best, "close"
    return None, None


def _apply_format(fmt: StatementFormat, result: StatementFile, props, cats) -> None:
    """Rewrite an extraction's labels with what this account decided they mean.

    Every id stored in the format is resolved against the caller's OWN categories and
    properties here. The maps are JSONB and carry no foreign keys, so an id that no longer
    exists — or never belonged to this account — resolves to nothing and the row simply stays
    as the statement printed it, which is the same path a brand new label takes.
    """
    cat_by_id = {c.id: c for c in cats}
    prop_by_id = {p.id: p for p in props}
    cat_aliases = fmt.category_aliases or {}
    prop_aliases = fmt.property_aliases or {}
    overrides = {k.strip().lower(): v for k, v in (fmt.classification_overrides or {}).items()}

    for ex in result.extractions:
        # Look up what the statement PRINTED first, then what the parser made of it. The
        # printed word is what the operator was shown and what they taught; the parser's own
        # tidying ("Mgt Fee" → "Management Fee") is a second chance, not the first.
        for key in (ex.raw_property_name, ex.property_name):
            target = prop_by_id.get(prop_aliases.get((key or "").strip().lower(), ""))
            if target is not None:
                ex.property_name = target.name
                break
        for row in ex.rows:
            raw = str(row.get("raw_category") or row.get("category", "")).strip()
            row.setdefault("raw_category", raw)
            for key in (raw, str(row.get("category", ""))):
                target = cat_by_id.get(cat_aliases.get(key.strip().lower(), ""))
                if target is not None:
                    row["category"] = target.name
                    break
            override = overrides.get(str(row["category"]).strip().lower())
            if override:
                row["classification"] = override
        if fmt.month_rule == "rent_period" and ex.alt_month is not None:
            ex.month, ex.alt_month = ex.alt_month, ex.month


def _note_format(
    result: StatementFile, fmt: StatementFormat | None, match: str | None
) -> None:
    """Say which format was used, in the file-level notes the review screen already shows."""
    if fmt is None:
        if result.fingerprint:
            result.warnings.append(
                "This is a layout you haven't taught yet. Correct anything below that is "
                "wrong, then save it as a format and the next statement from this sender "
                "will come in already corrected."
            )
        return
    if match == "close":
        result.warnings.append(
            f"Read using your saved format “{fmt.label}”. Its layout is close to, but not "
            "identical to, the one you saved — check the rows below, and re-save the format "
            "to record the new version."
        )
    else:
        result.warnings.append(f"Read using your saved format “{fmt.label}”.")


def _use_format(db: Session, fmt: StatementFormat | None) -> None:
    if fmt is None:
        return
    fmt.times_used = (fmt.times_used or 0) + 1
    fmt.last_used_at = datetime.now(timezone.utc)
    db.commit()


def _format_out(fmt: StatementFormat, props, cats) -> StatementFormatOut:
    """A saved format resolved to NAMES. A list of UUIDs tells an operator nothing about what
    the format will do to their next upload."""
    cat_by_id = {c.id: c.name for c in cats}
    prop_by_id = {p.id: p.name for p in props}
    return StatementFormatOut(
        id=fmt.id,
        label=fmt.label,
        shape=fmt.shape,
        fingerprint=fmt.fingerprint,
        month_rule=fmt.month_rule,
        times_used=fmt.times_used or 0,
        last_used_at=fmt.last_used_at,
        category_aliases={
            raw: cat_by_id[cid]
            for raw, cid in (fmt.category_aliases or {}).items()
            if cid in cat_by_id
        },
        property_aliases={
            raw: prop_by_id[pid]
            for raw, pid in (fmt.property_aliases or {}).items()
            if pid in prop_by_id
        },
        classification_overrides=dict(fmt.classification_overrides or {}),
    )


def _extract_file(content: bytes, props, cats, settings) -> StatementFile:
    """Parse one uploaded PDF into the statement(s) it contains — one for an ordinary
    single-property statement, one per property for a portfolio rent roll."""
    return extract_statements(
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
    """Parse a single PDF statement into a review preview.

    Reads locally — the rules, or a local Ollama model if one is running, both free — applies
    any format you have already taught for this sender's layout, and flags anything that must
    be created first. Nothing of yours is written: the only write is the saved format's own
    usage counter. The UI applies the reviewed rows via ``POST /import/rows``."""
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
        result = _extract_file(content, props, cats, settings)
    except Exception as exc:  # pdfplumber raises varied errors on malformed PDFs
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Could not read this PDF: {exc}"
        )

    fmt, match = _find_format(db, scope.account_id, result)
    if fmt is not None:
        _apply_format(fmt, result, props, cats)
    _note_format(result, fmt, match)
    _use_format(db, fmt)

    preview = _resolve_preview(
        result.extractions[0],
        {p.name.strip().lower(): p for p in props},
        {c.name.strip().lower(): c for c in cats},
        result.format,
    )
    # This endpoint's contract is ONE property, so a multi-property rent roll can only be
    # answered in part here. Say so rather than let the caller assume the file held one
    # property; extract-batch returns every one of them.
    preview.warnings = [*result.warnings, *preview.warnings]
    preview.fingerprint = result.fingerprint
    preview.sample = result.sample
    preview.format_id = fmt.id if fmt else None
    preview.format_label = fmt.label if fmt else None
    preview.format_match = match
    if len(result.extractions) > 1:
        preview.warnings.insert(
            0,
            f"This statement covers {len(result.extractions)} properties and only the first "
            "is shown here — upload it through the batch endpoint to import them all.",
        )
    return preview


@router.post("/statement/extract-batch", response_model=StatementBatchPreview)
async def extract_statements_batch(
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Parse many PDF statements at once (mixed months/properties). Each file is parsed
    independently — a bad PDF becomes a per-file error, never a failed batch — and the
    unknown properties/categories are de-duplicated across the whole batch so the UI can
    resolve each new item ONCE. Writes nothing.

    A file that turns out to be a multi-property RENT ROLL (one page, a line per property)
    contributes one item per property, tagged with the ``source_file`` it was split from, plus
    one entry in ``files`` carrying the file-level review — whether the rents add back to the
    stated total, which portfolio charges were split across the properties, and which figures
    named nobody. The caller needs no new code path: the items are ordinary statements."""
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
    notes: list[StatementFileNote] = []
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
            result = _extract_file(content, props, cats, settings)
            fmt, match = _find_format(db, scope.account_id, result)
            if fmt is not None:
                _apply_format(fmt, result, props, cats)
            _note_format(result, fmt, match)
            _use_format(db, fmt)
            previews = [
                _resolve_preview(ex, prop_by_name, cat_by_name, result.format)
                for ex in result.extractions
            ]
        except Exception as exc:
            items.append(StatementBatchItem(filename=fname, error=f"Could not read this PDF: {exc}"))
            continue

        notes.append(
            StatementFileNote(
                filename=fname,
                format=result.format,
                statements=len(previews),
                warnings=result.warnings,
                fingerprint=result.fingerprint,
                sample=result.sample,
                format_id=fmt.id if fmt else None,
                format_label=fmt.label if fmt else None,
                format_match=match,
            )
        )

        # A rent roll becomes one ITEM PER PROPERTY. From here on it is indistinguishable
        # from having uploaded twelve ordinary statements at once, which is exactly the
        # point: the batch flow already resolves mixed properties, dedupes their new
        # categories, and applies each through the import core.
        split = len(previews) > 1
        for preview in previews:
            label = (
                f"{fname} — {preview.detected_property or 'unidentified property'}"
                if split
                else fname
            )
            items.append(
                StatementBatchItem(
                    filename=label,
                    preview=preview,
                    source_file=fname if split else None,
                )
            )
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
        files=notes,
        unknown_properties=list(unknown_props.values()),
        unknown_categories=list(unknown_cats.values()),
    )


@router.get("/statement/formats", response_model=list[StatementFormatOut])
def list_statement_formats(
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Every statement format this account has taught, most recently used first."""
    props = db.scalars(select(Property).where(Property.account_id == scope.account_id)).all()
    cats = db.scalars(select(Category).where(Category.account_id == scope.account_id)).all()
    formats = db.scalars(
        select(StatementFormat)
        .where(StatementFormat.account_id == scope.account_id)
        .order_by(StatementFormat.last_used_at.desc().nullslast(), StatementFormat.label)
    ).all()
    return [_format_out(f, props, cats) for f in formats]


@router.post("/statement/formats", response_model=StatementFormatOut, status_code=status.HTTP_201_CREATED)
def save_statement_format(
    body: StatementFormatIn,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Teach (or re-teach) the format of one sender's statements.

    Called from the review screen once the operator has corrected it: the corrections are
    what gets saved. Re-teaching the same layout MERGES into the saved format rather than
    replacing it, so a month that only corrects one new charge doesn't drop everything
    learned before it; an alias sent with no target removes that one mapping.

    Every id is checked against this account's own categories and properties before it is
    stored — the alias maps are JSONB and the database cannot do that check itself.
    """
    props = db.scalars(select(Property).where(Property.account_id == scope.account_id)).all()
    cats = db.scalars(select(Category).where(Category.account_id == scope.account_id)).all()
    prop_ids = {p.id for p in props}
    cat_ids = {c.id for c in cats}

    fmt = db.scalars(
        select(StatementFormat).where(
            StatementFormat.account_id == scope.account_id,
            StatementFormat.fingerprint == body.fingerprint,
        )
    ).first()
    if fmt is None:
        fmt = StatementFormat(
            account_id=scope.account_id,
            fingerprint=body.fingerprint,
            label=body.label.strip(),
            shape=body.shape,
            sample=body.sample,
            category_aliases={},
            property_aliases={},
            classification_overrides={},
        )
        db.add(fmt)
    else:
        fmt.label = body.label.strip()
        fmt.shape = body.shape
        if body.sample:
            fmt.sample = body.sample

    categories = dict(fmt.category_aliases or {})
    properties = dict(fmt.property_aliases or {})
    overrides = dict(fmt.classification_overrides or {})

    for alias in body.categories:
        raw = alias.raw.strip().lower()
        if not raw:
            continue
        if alias.target_id is None:
            categories.pop(raw, None)
        elif alias.target_id in cat_ids:
            categories[raw] = alias.target_id
        else:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"Category {alias.target_id} does not belong to this account.",
            )
        # The override is keyed by the category the row ENDS UP as, so it survives the sender
        # renaming its own label next month.
        name = next((c.name for c in cats if c.id == alias.target_id), None)
        key = (name or alias.raw).strip().lower()
        if alias.classification:
            overrides[key] = alias.classification
        else:
            overrides.pop(key, None)

    for alias in body.properties:
        raw = alias.raw.strip().lower()
        if not raw:
            continue
        if alias.target_id is None:
            properties.pop(raw, None)
        elif alias.target_id in prop_ids:
            properties[raw] = alias.target_id
        else:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"Property {alias.target_id} does not belong to this account.",
            )

    fmt.category_aliases = categories
    fmt.property_aliases = properties
    fmt.classification_overrides = overrides
    fmt.month_rule = body.month_rule
    db.commit()
    db.refresh(fmt)
    return _format_out(fmt, props, cats)


@router.delete("/statement/formats/{format_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_statement_format(
    format_id: str,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Forget a format. The statements it was taught from are untouched; the next upload of
    that layout is simply read as if it had never been seen."""
    fmt = db.scalars(
        select(StatementFormat).where(
            StatementFormat.account_id == scope.account_id,
            StatementFormat.id == format_id,
        )
    ).first()
    if fmt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such statement format.")
    db.delete(fmt)
    db.commit()


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
