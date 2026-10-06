"""Rent roll / lease-level data (migration 0012).

``lease`` is a NEW parallel table: a unit's tenancy history (tenant, term, contract rent,
status). ``contract_rent`` is reference data — it never feeds NOI/cash-flow math, which
stays driven solely by monthly_records/line_items (see app/queries.py's rent-roll helpers
for how a unit's CURRENT lease is resolved and how ``status`` becomes the rent roll's
authoritative occupancy signal). Reads are open to any authed user (same convention as
investments/budgets/tags); writing a lease is admin-gated, matching the other
portfolio-shaping mutations.

Migration 0024 adds the periodic-tenancy rent history (``last_rent_increase_date`` /
``rent_before_increase``) to the lease payload, and migration 0025 adds ARREARS: a unit's
month-by-month ledger (``GET /units/{id}/arrears``) plus CRUD for the stored adjustments
(write-offs, non-rent charges, corrections) that are the only hand-entered input to an
otherwise derived balance. Arrears is reference data like everything else here — it never
feeds NOI/cash-flow.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app import queries
from app.db import get_db
from app.deps import Scope, get_scope, require_admin_scope
from app.models import ArrearsAdjustment, Lease, Property, Unit, User
from app.schemas import (
    ArrearsAdjustmentIn,
    ArrearsAdjustmentOut,
    LeaseExpirations,
    LeaseIn,
    LeaseOut,
    PercentageRentCalc,
    PortfolioRentWaterfall,
    RentRoll,
    RentWaterfall,
    UnitArrears,
)
from app.scoping import get_lease_or_404, get_property_or_404, get_unit_or_404

router = APIRouter(tags=["leases"])

TagsParam = Query(default=None, description="Optional tag filter (OR semantics); repeatable")
# Same "from"/"to" alias convention as the budget-variance endpoints (app/routers/budgets.py):
# an unspecified value defaults to the latest month with actual data on file for the scope
# (a single-month window) — see queries._resolve_rent_variance_period.
RentVarianceFromParam = Query(
    default=None, alias="from",
    description="First month (inclusive) of the expected-vs-actual rent variance window, YYYY-MM-01. "
    "Defaults to the latest month with actual data on file.",
)
RentVarianceToParam = Query(
    default=None, alias="to",
    description="Last month (inclusive) of the expected-vs-actual rent variance window, YYYY-MM-01. "
    "Defaults to the latest month with actual data on file (a single-month window) when omitted.",
)




# ---- Lease CRUD (per unit) ----------------------------------------------------------
@router.get("/units/{unit_id}/leases", response_model=list[LeaseOut])
def list_unit_leases(unit_id: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    """Full lease history for a unit, most recent first."""
    get_unit_or_404(db, scope, unit_id)
    return (
        db.query(Lease)
        .filter(Lease.unit_id == unit_id)
        .order_by(Lease.start_date.desc())
        .all()
    )


def _normalise_lease(payload: LeaseIn) -> LeaseIn:
    """Blank tenant name -> None (migration 0026). The form submits "" for an untouched field,
    and the DB rejects "" on purpose: "not recorded" must have exactly one representation, or a
    placeholder becomes indistinguishable from a real name and nothing can ever prompt for the
    real one."""
    if payload.tenant_name is not None and not payload.tenant_name.strip():
        return payload.model_copy(update={"tenant_name": None})
    if payload.tenant_name is not None:
        return payload.model_copy(update={"tenant_name": payload.tenant_name.strip()})
    return payload


def _validate_lease(payload: LeaseIn) -> None:
    """The cross-field rules the DB also enforces, raised here as 400s so the client gets a
    sentence instead of a constraint name. See migration 0024 for why each one exists."""
    if payload.end_date is not None and payload.end_date < payload.start_date:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="end_date must be on or after start_date")
    increase = payload.last_rent_increase_date
    if increase is not None and increase < payload.start_date:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="last_rent_increase_date must be on or after start_date",
        )
    if increase is not None and payload.end_date is not None and increase > payload.end_date:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="last_rent_increase_date must be on or before end_date",
        )
    # A prior rent with no increase date is uninterpretable: there is no month at which it
    # stopped applying, so the rent schedule couldn't place it.
    if payload.rent_before_increase is not None and increase is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="rent_before_increase requires last_rent_increase_date",
        )
    # Month grain (migration 0027). Checked here as well as in the DB so a mid-month value is a
    # sentence the client can show, not a 500 from a constraint violation.
    if payload.arrears_from_month is not None and payload.arrears_from_month.day != 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="arrears_from_month must be the first of the month (YYYY-MM-01)",
        )


def _derive_lease_type(end_date) -> str:
    """'fixed' | 'mtm', from the pre-existing NULL-end_date = MTM convention (migration
    0013) — always SERVER-derived, never trusted from the client, so a lease's stored
    lease_type can never drift from its own end_date."""
    return "mtm" if end_date is None else "fixed"


@router.post("/units/{unit_id}/leases", response_model=LeaseOut, status_code=status.HTTP_201_CREATED)
def create_unit_lease(
    unit_id: str,
    payload: LeaseIn,
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    """Add a new lease for a unit (does not touch any prior lease row — history is kept;
    the CURRENT lease is resolved at read time by app/queries.py, not by this endpoint).
    Admin only."""
    get_unit_or_404(db, scope, unit_id)
    payload = _normalise_lease(payload)
    _validate_lease(payload)
    lease = Lease(
        unit_id=unit_id, lease_type=_derive_lease_type(payload.end_date), **payload.model_dump()
    )
    db.add(lease)
    db.commit()
    db.refresh(lease)
    return lease


@router.patch("/leases/{lease_id}", response_model=LeaseOut)
def update_lease(
    lease_id: str,
    payload: LeaseIn,
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    """Replace a lease's editable fields in place. Admin only."""
    lease = get_lease_or_404(db, scope, lease_id)
    payload = _normalise_lease(payload)
    _validate_lease(payload)
    for field, value in payload.model_dump().items():
        setattr(lease, field, value)
    lease.lease_type = _derive_lease_type(lease.end_date)
    db.commit()
    db.refresh(lease)
    return lease


@router.delete("/leases/{lease_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_lease(lease_id: str, db: Session = Depends(get_db), scope: Scope = Depends(require_admin_scope)):
    """Remove a lease row entirely (e.g. a data-entry mistake). Admin only. Prefer editing
    status to 'expired'/'vacant' over deleting a real historical lease."""
    db.delete(get_lease_or_404(db, scope, lease_id))
    db.commit()


# ---- Percentage rent (retail leases only, migration 0013) ---------------------------
@router.get("/leases/{lease_id}/percentage-rent", response_model=PercentageRentCalc)
def lease_percentage_rent(
    lease_id: str,
    annual_sales: float | None = Query(default=None, ge=0, description="Trailing annual sales, if known"),
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Percentage-rent terms for a lease, plus overage rent if ``annual_sales`` is given.

    Sales figures aren't tracked anywhere in this app — MVP scope is: store/display the
    terms, and compute overage = max(0, (annual_sales - breakpoint) * rate) ON THE FLY from
    a caller-supplied sales figure, never persisted and never fed into NOI/cash-flow.
    ``has_percentage_rent_terms`` is False for the overwhelming majority of leases
    (residential); only Cedar Plaza Retail's lease carries these terms in the seed data.
    """
    lease = get_lease_or_404(db, scope, lease_id)
    has_terms = lease.pct_rent_rate is not None and lease.pct_rent_breakpoint is not None
    overage = None
    if has_terms and annual_sales is not None:
        overage = max(0.0, (annual_sales - float(lease.pct_rent_breakpoint)) * float(lease.pct_rent_rate) / 100)
    return {
        "lease_id": lease_id,
        "has_percentage_rent_terms": has_terms,
        "pct_rent_rate": float(lease.pct_rent_rate) if lease.pct_rent_rate is not None else None,
        "pct_rent_breakpoint": float(lease.pct_rent_breakpoint) if lease.pct_rent_breakpoint is not None else None,
        "annual_sales": annual_sales,
        "overage_rent": overage,
    }


# ---- Rent roll + occupancy ----------------------------------------------------------
@router.get("/properties/{property_id}/rent-roll", response_model=RentRoll)
def property_rent_roll(
    property_id: str,
    date_from: date | None = RentVarianceFromParam,
    date_to: date | None = RentVarianceToParam,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """One row per unit: lease (tenant/term/contract rent/status) + latest actual rent +
    months-to-expiry, plus a physical/economic occupancy summary for the property. Also
    includes each unit's expected (escalated) rent vs. actual collected rent, and a
    property-level rent-variance rollup, for the `from`/`to` window (default: latest month
    on file)."""
    get_property_or_404(db, scope, property_id)
    return queries.property_rent_roll(db, scope.account_id, property_id, date_from, date_to)


@router.get("/portfolio/rent-roll", response_model=RentRoll)
def portfolio_rent_roll(
    tags: list[str] | None = TagsParam,
    date_from: date | None = RentVarianceFromParam,
    date_to: date | None = RentVarianceToParam,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Portfolio-wide rent roll across every multifamily unit. ``tags`` optionally scopes
    to properties carrying ANY of the given tags (OR semantics) — including the rent-
    variance rollup, which honors the same tag filter. See `property_rent_roll` for the
    `from`/`to` window semantics."""
    return queries.portfolio_rent_roll(db, scope.account_id, tags, date_from, date_to)


# ---- Lease-expiration / rollover horizon ---------------------------------------------
@router.get("/portfolio/lease-expirations", response_model=LeaseExpirations)
def portfolio_lease_expirations(
    within_months: int = Query(default=3, ge=1, le=36),
    tags: list[str] | None = TagsParam,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Leases expiring within ``within_months`` of today, soonest first — the rollover risk
    feed. ``tags`` optionally scopes to properties carrying ANY of the given tags."""
    return queries.lease_expirations(db, scope.account_id, within_months, tags)


# ---- Rent waterfall (GPR -> loss-to-lease -> vacancy loss -> collections loss & conces-
# sions -> actual collected; migration 0015: units.market_rent) -----------------------
@router.get("/properties/{property_id}/rent-waterfall", response_model=RentWaterfall)
def property_rent_waterfall(
    property_id: str,
    date_from: date | None = RentVarianceFromParam,
    date_to: date | None = RentVarianceToParam,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """One property's rent waterfall for the `from`/`to` window (default: latest month on
    file, same convention as the rent roll's variance window). Returns the GPR/loss-to-
    lease/vacancy-loss/collections-loss/actual-collected components plus `residual`, the
    identity's balance check (should be ~0 to the cent)."""
    result = queries.property_rent_waterfall(db, scope.account_id, property_id, date_from, date_to)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Property not found")
    return result


@router.get("/portfolio/rent-waterfall", response_model=PortfolioRentWaterfall)
def portfolio_rent_waterfall(
    tags: list[str] | None = TagsParam,
    date_from: date | None = RentVarianceFromParam,
    date_to: date | None = RentVarianceToParam,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Portfolio-wide rent waterfall, honoring the same `tags` filter (OR semantics) as
    every other portfolio-scoped endpoint, plus a per-property breakdown (`properties`) so
    an analyst can rank which assets carry the most vacancy loss / loss-to-lease /
    collections loss rather than only seeing the blended total. See
    `property_rent_waterfall` for the `from`/`to` window semantics."""
    return queries.portfolio_rent_waterfall(db, scope.account_id, tags, date_from, date_to)


# ---- Arrears (migration 0025: lease.opening_arrears + arrears_adjustment) ------------
#
# The rent roll already carries each unit's accumulated balance and the window's movement
# (see `RentRollRow`'s arrears fields). These endpoints are the two things it can't: the
# month-by-month ledger behind a single unit's balance, and the stored adjustments that are
# the only hand-entered input to an otherwise fully derived figure.
@router.get("/units/{unit_id}/arrears", response_model=UnitArrears)
def unit_arrears(unit_id: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    """One unit's arrears ledger — every tenancy it has ever had, each month by month with its
    own running balance.

    This is the MONTHLY basis the rent roll's accumulated balance is built from:
    ``movement = rent_due - rent_collected + adjustments`` per month, accumulated from the
    lease's ``opening_arrears``. Nothing here is stored — it is derived on read from the lease
    schedule and the actual collected rent, so a rent correction entered today restates the
    balance immediately (see `app.queries.unit_arrears`).

    Prior tenancies are included, not just the current one: a former tenant's unpaid rent
    doesn't vanish when they leave, it merely stops being collectable from the current tenant.
    """
    get_unit_or_404(db, scope, unit_id)
    return queries.unit_arrears(db, scope.account_id, unit_id)


@router.get("/leases/{lease_id}/arrears-adjustments", response_model=list[ArrearsAdjustmentOut])
def list_arrears_adjustments(
    lease_id: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)
):
    """Every arrears adjustment on a lease, oldest month first."""
    get_lease_or_404(db, scope, lease_id)
    return (
        db.query(ArrearsAdjustment)
        .filter(ArrearsAdjustment.lease_id == lease_id)
        .order_by(ArrearsAdjustment.month, ArrearsAdjustment.created_at)
        .all()
    )


def _validate_adjustment(payload: ArrearsAdjustmentIn) -> None:
    """The rules migration 0025 also enforces in the DB, as 400s. The kind/sign pairing is
    checked here as well as there because the sign IS the meaning — a 'write_off' that
    increased a balance would be a silently wrong ledger, not a rejected row."""
    if payload.month.day != 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="month must be the first of the month (YYYY-MM-01)"
        )
    if payload.amount == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="amount must not be zero")
    if payload.kind == "write_off" and payload.amount > 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="a write_off must be negative (it reduces arrears)"
        )
    if payload.kind == "charge" and payload.amount < 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="a charge must be positive (it increases arrears)"
        )


@router.post(
    "/leases/{lease_id}/arrears-adjustments",
    response_model=ArrearsAdjustmentOut,
    status_code=status.HTTP_201_CREATED,
)
def create_arrears_adjustment(
    lease_id: str,
    payload: ArrearsAdjustmentIn,
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    """Record a write-off, a non-rent charge, or a correction against a tenancy's arrears.
    Admin only, same gate as editing the lease itself — this moves a balance someone is being
    chased for.

    Takes effect on the next read: the balance is derived, so there is nothing to recompute.
    """
    get_lease_or_404(db, scope, lease_id)
    _validate_adjustment(payload)
    adjustment = ArrearsAdjustment(lease_id=lease_id, **payload.model_dump())
    db.add(adjustment)
    db.commit()
    db.refresh(adjustment)
    return adjustment


def _get_adjustment_or_404(db: Session, scope: Scope, adjustment_id: str) -> ArrearsAdjustment:
    """Ownership is transitive (adjustment -> lease -> unit -> property -> account), so this
    resolves the lease through the account-scoped resolver rather than fetching the adjustment
    by primary key alone — "not yours == 404", same contract as app/scoping.py."""
    adjustment = db.get(ArrearsAdjustment, adjustment_id)
    if adjustment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Arrears adjustment not found")
    get_lease_or_404(db, scope, adjustment.lease_id)
    return adjustment


@router.patch("/arrears-adjustments/{adjustment_id}", response_model=ArrearsAdjustmentOut)
def update_arrears_adjustment(
    adjustment_id: str,
    payload: ArrearsAdjustmentIn,
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    """Replace an adjustment's fields in place. Admin only."""
    adjustment = _get_adjustment_or_404(db, scope, adjustment_id)
    _validate_adjustment(payload)
    for field, value in payload.model_dump().items():
        setattr(adjustment, field, value)
    db.commit()
    db.refresh(adjustment)
    return adjustment


@router.delete("/arrears-adjustments/{adjustment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_arrears_adjustment(
    adjustment_id: str, db: Session = Depends(get_db), scope: Scope = Depends(require_admin_scope)
):
    """Remove an adjustment entirely (e.g. a write-off entered against the wrong tenancy).
    Admin only. The balance it was moving reverts on the next read."""
    db.delete(_get_adjustment_or_404(db, scope, adjustment_id))
    db.commit()
