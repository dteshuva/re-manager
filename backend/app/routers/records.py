"""Monthly records + line items — the manual data-entry surface.

A monthly_record is keyed by (property_id, unit_id, month); ``unit_id IS NULL`` is the
property-tier record where shared items (capex, debt service) live. ``POST /records`` is
an idempotent upsert that replaces the record's line items wholesale, so re-saving a
month cleanly overwrites it rather than duplicating. Every write is blocked when the
property-month is locked (see :mod:`app.locks`).
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import Scope, get_scope
from app.locks import assert_unlocked
from app.models import Category, LineItem, MonthlyRecord, PeriodStatus, Unit
from app.scoping import get_property_or_404, get_record_or_404
from app.summaries import refresh_month
from app.schemas import (
    LineItemCreate,
    LineItemOut,
    LineItemUpdate,
    MonthlyRecordCreate,
    MonthlyRecordOut,
    MonthlyRecordUpdate,
)

router = APIRouter(tags=["records"])


# ---------------------------------------------------------------- serializers
def _li_out(li: LineItem) -> LineItemOut:
    return LineItemOut(
        id=li.id,
        monthly_record_id=li.monthly_record_id,
        category_id=li.category_id,
        category_name=li.category.name if li.category else None,
        classification=li.classification,
        amount=float(li.amount),
    )


def _record_out(rec: MonthlyRecord) -> MonthlyRecordOut:
    items = sorted(rec.line_items, key=lambda li: (li.category.name if li.category else ""))
    return MonthlyRecordOut(
        id=rec.id,
        property_id=rec.property_id,
        unit_id=rec.unit_id,
        month=rec.month,
        notes=rec.notes,
        is_vacant=rec.is_vacant,
        line_items=[_li_out(li) for li in items],
    )


# ---------------------------------------------------------------- validation helpers
def _first_of_month(d: date) -> date:
    return d.replace(day=1)


def _validate_scope(db: Session, scope: Scope, property_id: str, unit_id: str | None) -> None:
    # 404s on another account's property (get_property_or_404's contract), so the unit
    # check below can only ever see units of a property this account owns.
    get_property_or_404(db, scope, property_id)
    if unit_id is not None:
        unit = db.get(Unit, unit_id)
        if unit is None or unit.property_id != property_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="unit_id does not belong to property_id",
            )


def _ensure_draft_period(db: Session, property_id: str, month: date) -> None:
    """A brand-new record should start life as ``draft`` rather than with no period-status
    row at all — otherwise ``GET /periods?property_id=&month=`` returns ``[]`` right after a
    ``POST /records`` and the Entry screen's status badge has nothing to show (analyst gap).
    Only inserts when no row exists yet; never downgrades an existing posted/locked status.
    """
    exists = db.scalar(
        select(PeriodStatus.id).where(
            PeriodStatus.property_id == property_id, PeriodStatus.month == month
        )
    )
    if exists is None:
        db.add(PeriodStatus(property_id=property_id, month=month, status="draft"))


def _validate_categories(db: Session, scope: Scope, items: list[LineItemCreate]) -> None:
    ids = [i.category_id for i in items]
    if len(set(ids)) != len(ids):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Duplicate category in line items; one row per category per record.",
        )
    if ids:
        # Scoped to this account: another account's category id reads as unknown, not as a
        # usable reference. The composite FK from migration 0017 would reject it at flush
        # anyway — this turns that 500-shaped IntegrityError into a clean 400.
        found = set(
            db.scalars(
                select(Category.id).where(
                    Category.id.in_(ids), Category.account_id == scope.account_id
                )
            ).all()
        )
        missing = set(ids) - found
        if missing:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail=f"Unknown category id(s): {sorted(missing)}"
            )


# ---------------------------------------------------------------- record endpoints
@router.get("/records", response_model=list[MonthlyRecordOut])
def list_records(
    property_id: str | None = None,
    unit_id: str | None = None,
    month: date | None = None,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """List monthly records, optionally filtered by property / unit / month."""
    stmt = select(MonthlyRecord).where(MonthlyRecord.account_id == scope.account_id)
    if property_id:
        stmt = stmt.where(MonthlyRecord.property_id == property_id)
    if unit_id:
        stmt = stmt.where(MonthlyRecord.unit_id == unit_id)
    if month:
        stmt = stmt.where(MonthlyRecord.month == _first_of_month(month))
    stmt = stmt.order_by(MonthlyRecord.month, MonthlyRecord.unit_id)
    return [_record_out(r) for r in db.scalars(stmt).all()]


@router.get("/records/{record_id}", response_model=MonthlyRecordOut)
def get_record(
    record_id: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)
):
    return _record_out(get_record_or_404(db, scope, record_id))


@router.post("/records", response_model=MonthlyRecordOut, status_code=status.HTTP_201_CREATED)
def upsert_record(
    payload: MonthlyRecordCreate,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Create or replace a property/unit month and its line items (idempotent).

    Keyed on (property_id, unit_id, month): re-posting the same month overwrites its
    line items instead of duplicating. Blocked if the property-month is locked.
    """
    month = _first_of_month(payload.month)
    _validate_scope(db, scope, payload.property_id, payload.unit_id)
    _validate_categories(db, scope, payload.line_items)
    assert_unlocked(db, payload.property_id, month)
    _ensure_draft_period(db, payload.property_id, month)

    rec = db.scalar(
        select(MonthlyRecord).where(
            MonthlyRecord.account_id == scope.account_id,
            MonthlyRecord.property_id == payload.property_id,
            MonthlyRecord.unit_id.is_(None)
            if payload.unit_id is None
            else MonthlyRecord.unit_id == payload.unit_id,
            MonthlyRecord.month == month,
        )
    )
    if rec is None:
        rec = MonthlyRecord(
            account_id=scope.account_id,
            property_id=payload.property_id,
            unit_id=payload.unit_id,
            month=month,
        )
        db.add(rec)
    rec.notes = payload.notes
    rec.is_vacant = payload.is_vacant
    # Replace line items wholesale (the form sends the complete set for this record).
    rec.line_items.clear()
    db.flush()
    for item in payload.line_items:
        rec.line_items.append(
            LineItem(
                account_id=scope.account_id,
                category_id=item.category_id,
                classification=item.classification,
                amount=item.amount,
            )
        )
    db.flush()  # write line items so the rollup refresh (below) reads the new values
    refresh_month(db, payload.property_id, month)
    db.commit()
    db.refresh(rec)
    return _record_out(rec)


@router.patch("/records/{record_id}", response_model=MonthlyRecordOut)
def update_record(
    record_id: str,
    payload: MonthlyRecordUpdate,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Update record-level fields (notes, is_vacant). Line items are managed separately."""
    rec = get_record_or_404(db, scope, record_id)
    assert_unlocked(db, rec.property_id, rec.month)
    if payload.notes is not None:
        rec.notes = payload.notes
    if payload.is_vacant is not None:
        rec.is_vacant = payload.is_vacant
        db.flush()
        refresh_month(db, rec.property_id, rec.month)
    db.commit()
    db.refresh(rec)
    return _record_out(rec)


@router.delete("/records/{record_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_record(
    record_id: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)
):
    rec = get_record_or_404(db, scope, record_id)
    assert_unlocked(db, rec.property_id, rec.month)
    property_id, month = rec.property_id, rec.month
    db.delete(rec)
    db.flush()  # apply the delete so the rollup refresh reflects the removed record
    refresh_month(db, property_id, month)
    db.commit()


# ---------------------------------------------------------------- line-item endpoints
@router.post(
    "/records/{record_id}/line-items",
    response_model=LineItemOut,
    status_code=status.HTTP_201_CREATED,
)
def add_line_item(
    record_id: str,
    payload: LineItemCreate,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Add or update a single line item on a record (upsert by category)."""
    rec = get_record_or_404(db, scope, record_id)
    assert_unlocked(db, rec.property_id, rec.month)
    _validate_categories(db, scope, [payload])

    li = db.scalar(
        select(LineItem).where(
            LineItem.account_id == scope.account_id,
            LineItem.monthly_record_id == record_id,
            LineItem.category_id == payload.category_id,
        )
    )
    if li is None:
        li = LineItem(
            account_id=scope.account_id,
            monthly_record_id=record_id,
            category_id=payload.category_id,
        )
        db.add(li)
    li.classification = payload.classification
    li.amount = payload.amount
    db.flush()
    refresh_month(db, rec.property_id, rec.month)
    db.commit()
    db.refresh(li)
    return _li_out(li)


@router.patch("/line-items/{line_item_id}", response_model=LineItemOut)
def update_line_item(
    line_item_id: str,
    payload: LineItemUpdate,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    li = db.scalar(
        select(LineItem).where(
            LineItem.id == line_item_id, LineItem.account_id == scope.account_id
        )
    )
    if li is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Line item not found")
    rec = li.monthly_record
    assert_unlocked(db, rec.property_id, rec.month)
    data = payload.model_dump(exclude_unset=True)
    if "classification" in data:
        li.classification = data["classification"]
    if "amount" in data:
        li.amount = data["amount"]
    db.flush()
    refresh_month(db, rec.property_id, rec.month)
    db.commit()
    db.refresh(li)
    return _li_out(li)


@router.delete("/line-items/{line_item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_line_item(
    line_item_id: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)
):
    li = db.scalar(
        select(LineItem).where(
            LineItem.id == line_item_id, LineItem.account_id == scope.account_id
        )
    )
    if li is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Line item not found")
    rec = li.monthly_record
    assert_unlocked(db, rec.property_id, rec.month)
    property_id, month = rec.property_id, rec.month
    db.delete(li)
    db.flush()
    refresh_month(db, property_id, month)
    db.commit()
