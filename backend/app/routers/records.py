"""Monthly records + line items — the manual data-entry surface.

A monthly_record is keyed by (property_id, unit_id, month); ``unit_id IS NULL`` is the
property-tier record where shared items (capex, debt service) live. ``POST /records`` is
an idempotent upsert that replaces the record's line items wholesale, so re-saving a
month cleanly overwrites it rather than duplicating. Every write is blocked when the
property-month is locked (see :mod:`app.locks`).

A line item may be stated as a RATE instead of a figure — "management: 8% of rent"
(``rate_pct``, migration 0023). The server derives its ``amount`` from the rent in that
property-month and keeps it there: see :mod:`app.percent_lines`, and ``GET /rent-basis``
below for the basis the entry form previews against.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import Scope, get_scope
from app.locks import assert_unlocked, ensure_draft_period
from app.models import Category, LineItem, MonthlyRecord, Unit
from app.percent_lines import rent_basis
from app.scoping import get_property_or_404, get_record_or_404
from app.summaries import refresh_month
from app.schemas import (
    LineItemCreate,
    LineItemOut,
    LineItemUpdate,
    MonthlyRecordCreate,
    MonthlyRecordOut,
    MonthlyRecordUpdate,
    RentBasisOut,
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
        rate_pct=float(li.rate_pct) if li.rate_pct is not None else None,
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


def _validate_rates(db: Session, scope: Scope, items: list[LineItemCreate]) -> None:
    """Reject the two ways a rate-stated line could be incoherent.

    1. **A rate AND an amount.** The server derives the amount from rent and rewrites it on
       every write to the month, so a submitted amount is a second opinion about the same
       money that would be overwritten moments later. Refusing is honest; silently ignoring
       the figure the operator typed is not. A zero alongside a rate is fine — that is the
       field's default, not a figure anyone stated.
    2. **A rate on a line that counts as rent.** Its own amount would then be part of the
       basis it is a percentage of. :mod:`app.percent_lines` excludes rate lines from the
       basis so the arithmetic can never actually run away, but the request still expresses
       something meaningless — a fee on itself — and is far more likely a mis-picked category.
    """
    rated = [i for i in items if i.rate_pct is not None]
    if not rated:
        return

    with_amount = [i for i in rated if i.amount]
    if with_amount:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=(
                "A line item is stated either as an amount or as a rate (rate_pct), not "
                "both — the amount of a rate-stated line is derived from rent."
            ),
        )

    defaults = dict(
        db.execute(
            select(Category.id, Category.default_classification).where(
                Category.id.in_([i.category_id for i in rated]),
                Category.account_id == scope.account_id,
            )
        ).all()
    )
    for item in rated:
        effective = item.classification or defaults.get(item.category_id)
        if effective == "rent":
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=(
                    "A rate (rate_pct) cannot be used on a line that classifies as rent — "
                    "it would be a percentage of itself."
                ),
            )


# ---------------------------------------------------------------- record endpoints
@router.get("/rent-basis", response_model=RentBasisOut)
def get_rent_basis(
    property_id: str = Query(..., description="Property whose rent forms the basis."),
    month: date = Query(..., description="Month (any day; snapped to the first)."),
    unit_id: str | None = Query(
        None,
        description=(
            "Omit for the property-tier basis (the property's WHOLE rent for the month, "
            "every unit included). Give a unit to get that unit's own rent."
        ),
    ),
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """The rent a percentage line is charged on, so a form can preview "8% of X = Y".

    Read-only and derived from the same query the save path uses, so the previewed figure is
    the figure that will be stored — including rent on units the operator is not currently
    looking at, which is the part they cannot work out by hand.
    """
    first = _first_of_month(month)
    _validate_scope(db, scope, property_id, unit_id)
    basis = rent_basis(db, property_id, first)
    return RentBasisOut(
        property_id=property_id,
        unit_id=unit_id,
        month=first,
        rent=float(basis.get(unit_id, 0)),
    )


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
    _validate_rates(db, scope, payload.line_items)
    assert_unlocked(db, payload.property_id, month)
    ensure_draft_period(db, payload.property_id, month)

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
                # A rate-stated line starts at 0 and is filled in by the percentage pass
                # inside refresh_month below, off this month's rent.
                amount=0 if item.rate_pct is not None else item.amount,
                rate_pct=item.rate_pct,
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
    _validate_rates(db, scope, [payload])

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
    li.rate_pct = payload.rate_pct
    # A rate-stated line's amount is derived below, inside refresh_month.
    li.amount = 0 if payload.rate_pct is not None else payload.amount
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
    # A patch names ONE of the two ways of stating the line. Naming both is ambiguous rather
    # than merely redundant, and the order the fields happened to be applied in would decide
    # the answer — so refuse, exactly as _validate_rates does on create.
    rate = data.get("rate_pct")
    if rate is not None and data.get("amount"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=(
                "A line item is stated either as an amount or as a rate (rate_pct), not "
                "both — the amount of a rate-stated line is derived from rent."
            ),
        )
    if "classification" in data:
        li.classification = data["classification"]
    if "rate_pct" in data:
        li.rate_pct = rate
        _validate_rates(
            db,
            scope,
            [
                LineItemCreate(
                    category_id=li.category_id,
                    classification=li.classification,
                    rate_pct=rate,
                )
            ],
        )
    if "amount" in data and rate is None:
        # Typing a figure converts the line back to a fixed amount: leaving the rate in place
        # would just have refresh_month overwrite what was typed on the next write. Guarded on
        # `rate is None` so a rate in the same patch is never clobbered by the amount it sends
        # alongside it (which the check above has already limited to zero).
        li.amount = data["amount"]
        li.rate_pct = None
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
