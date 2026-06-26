"""Import core — the automation seam.

This module consumes already-structured :class:`ImportRow` rows and applies them to the
database. It is deliberately decoupled from *how* those rows were produced: the CSV/Excel
parser (:mod:`app.parsing`) is one producer, and a future rent-statement/PDF/LLM parser
would be another — both emit the same rows into :func:`apply_import`, and the storage +
computation path never changes.

Idempotency is keyed on **(property_id, unit_id, month, category_id)**: each import upserts
one line item per category, leaving other categories on the same monthly_record untouched.
So re-importing June overwrites June's affected rows rather than duplicating them.

Locked property-months are never written; rows targeting them are reported as errors.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Category, LineItem, MonthlyRecord, Property, Unit
from app.schemas import ImportIssue, ImportReport, ImportRow


class _Resolver:
    """Caches the global lookup tables so a batch import is a handful of queries."""

    def __init__(self, db: Session):
        self.db = db
        props = db.scalars(select(Property)).all()
        self.prop_by_id = {p.id: p for p in props}
        self.prop_by_name = {p.name.strip().lower(): p for p in props}

        units = db.scalars(select(Unit)).all()
        self.unit_by_id = {u.id: u for u in units}
        # (property_id, unit_number.lower()) -> unit
        self.unit_by_key = {(u.property_id, u.unit_number.strip().lower()): u for u in units}

        cats = db.scalars(select(Category)).all()
        self.cat_by_id = {c.id: c for c in cats}
        self.cat_by_name = {c.name.strip().lower(): c for c in cats}

        # property_id -> set of months that are locked
        self.locked: dict[str, set[date]] = defaultdict(set)
        from app.models import PeriodStatus

        for ps in db.scalars(select(PeriodStatus).where(PeriodStatus.status == "locked")).all():
            self.locked[ps.property_id].add(ps.month)


class _ResolvedRow:
    __slots__ = ("property_id", "unit_id", "month", "category_id", "classification", "amount")

    def __init__(self, property_id, unit_id, month, category_id, classification, amount):
        self.property_id = property_id
        self.unit_id = unit_id
        self.month = month
        self.category_id = category_id
        self.classification = classification
        self.amount = amount


def _resolve_row(r: _Resolver, row: ImportRow, n: int) -> tuple[_ResolvedRow | None, list[ImportIssue]]:
    errs: list[ImportIssue] = []

    # property
    prop = None
    if row.property_id:
        prop = r.prop_by_id.get(row.property_id)
        if prop is None:
            errs.append(ImportIssue(row=n, field="property_id", message=f"Unknown property_id '{row.property_id}'"))
    elif row.property:
        prop = r.prop_by_name.get(row.property.strip().lower())
        if prop is None:
            errs.append(ImportIssue(row=n, field="property", message=f"Unknown property '{row.property}'"))
    else:
        errs.append(ImportIssue(row=n, field="property", message="Missing property (name or id)"))

    # unit (optional; blank ⇒ property-tier)
    unit_id = None
    has_unit = bool(row.unit_id or (row.unit and str(row.unit).strip()))
    if has_unit:
        unit = None
        if row.unit_id:
            unit = r.unit_by_id.get(row.unit_id)
        elif prop is not None:
            unit = r.unit_by_key.get((prop.id, str(row.unit).strip().lower()))
        if unit is None:
            errs.append(ImportIssue(row=n, field="unit", message=f"Unknown unit '{row.unit or row.unit_id}' for this property"))
        elif prop is not None and unit.property_id != prop.id:
            errs.append(ImportIssue(row=n, field="unit", message="Unit does not belong to property"))
        else:
            unit_id = unit.id

    # category (must exist and be active for new entry)
    cat = None
    if row.category_id:
        cat = r.cat_by_id.get(row.category_id)
        if cat is None:
            errs.append(ImportIssue(row=n, field="category_id", message=f"Unknown category_id '{row.category_id}'"))
    elif row.category:
        cat = r.cat_by_name.get(row.category.strip().lower())
        if cat is None:
            errs.append(ImportIssue(row=n, field="category", message=f"Unknown category '{row.category}'"))
    else:
        errs.append(ImportIssue(row=n, field="category", message="Missing category (name or id)"))
    if cat is not None and not cat.active:
        errs.append(ImportIssue(row=n, field="category", message=f"Category '{cat.name}' is inactive"))

    month = row.month.replace(day=1)

    # lock check
    if prop is not None and month in r.locked.get(prop.id, set()):
        errs.append(ImportIssue(row=n, field="month", message=f"Period {month:%Y-%m} is locked"))

    if errs:
        return None, errs
    return _ResolvedRow(prop.id, unit_id, month, cat.id, row.classification, row.amount), []


def apply_import(
    db: Session,
    rows: list[ImportRow],
    *,
    dry_run: bool = False,
    on_error: str = "abort",
    extra_errors: list[ImportIssue] | None = None,
) -> ImportReport:
    """Validate and (optionally) apply structured rows.

    ``on_error="abort"`` (default): if any row is invalid, nothing is written.
    ``on_error="skip"``: valid rows are applied and invalid rows are reported.
    ``dry_run=True``: validate and compute what *would* happen, then roll back.
    ``extra_errors``: row issues from an upstream producer (e.g. the CSV parser) that
    are merged in and count toward the abort decision, so a bad cell in a CSV blocks an
    abort-mode commit just like a bad reference does.
    """
    resolver = _Resolver(db)
    errors: list[ImportIssue] = list(extra_errors or [])
    resolved: list[_ResolvedRow] = []

    seen: dict[tuple, int] = {}
    for i, row in enumerate(rows, start=1):
        n = row.source_row or i
        rr, errs = _resolve_row(resolver, row, n)
        if errs:
            errors.extend(errs)
            continue
        key = (rr.property_id, rr.unit_id, rr.month, rr.category_id)
        if key in seen:
            errors.append(
                ImportIssue(row=n, field="category", message=f"Duplicate of row {seen[key]} (same property/unit/month/category)")
            )
            continue
        seen[key] = n
        resolved.append(rr)

    invalid = len({e.row for e in errors if e.row is not None})
    valid = len(resolved)

    proceed = not (errors and on_error == "abort")
    applied_items = 0
    records_touched: set = set()

    if proceed and resolved:
        # Cache existing monthly_records for the touched scopes so we get-or-create once.
        rec_cache: dict[tuple, MonthlyRecord] = {}
        for rr in resolved:
            rkey = (rr.property_id, rr.unit_id, rr.month)
            rec = rec_cache.get(rkey)
            if rec is None:
                rec = db.scalar(
                    select(MonthlyRecord).where(
                        MonthlyRecord.property_id == rr.property_id,
                        MonthlyRecord.unit_id.is_(None)
                        if rr.unit_id is None
                        else MonthlyRecord.unit_id == rr.unit_id,
                        MonthlyRecord.month == rr.month,
                    )
                )
                if rec is None:
                    rec = MonthlyRecord(property_id=rr.property_id, unit_id=rr.unit_id, month=rr.month)
                    db.add(rec)
                    db.flush()
                rec_cache[rkey] = rec
            records_touched.add(rec.id if rec.id else rkey)

            li = db.scalar(
                select(LineItem).where(
                    LineItem.monthly_record_id == rec.id,
                    LineItem.category_id == rr.category_id,
                )
            )
            if li is None:
                li = LineItem(monthly_record_id=rec.id, category_id=rr.category_id)
                db.add(li)
            li.classification = rr.classification
            li.amount = rr.amount
            applied_items += 1

    committed = False
    if dry_run or not proceed:
        db.rollback()
    elif resolved:
        db.commit()
        committed = True
    else:
        db.rollback()  # nothing to do

    return ImportReport(
        dry_run=dry_run,
        on_error=on_error,
        committed=committed,
        total_rows=len(rows),
        valid_rows=valid,
        invalid_rows=invalid,
        applied_line_items=applied_items if (proceed or dry_run) else 0,
        records_touched=len(records_touched) if (proceed or dry_run) else 0,
        errors=errors,
    )
