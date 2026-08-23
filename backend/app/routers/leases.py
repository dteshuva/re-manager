"""Rent roll / lease-level data (migration 0012).

``lease`` is a NEW parallel table: a unit's tenancy history (tenant, term, contract rent,
status). ``contract_rent`` is reference data — it never feeds NOI/cash-flow math, which
stays driven solely by monthly_records/line_items (see app/queries.py's rent-roll helpers
for how a unit's CURRENT lease is resolved and how ``status`` becomes the rent roll's
authoritative occupancy signal). Reads are open to any authed user (same convention as
investments/budgets/tags); writing a lease is admin-gated, matching the other
portfolio-shaping mutations.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app import queries
from app.db import get_db
from app.deps import Scope, get_scope, require_admin_scope
from app.models import Lease, Property, Unit, User
from app.schemas import (
    LeaseExpirations,
    LeaseIn,
    LeaseOut,
    PercentageRentCalc,
    PortfolioRentWaterfall,
    RentRoll,
    RentWaterfall,
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
    if payload.end_date is not None and payload.end_date < payload.start_date:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="end_date must be on or after start_date")
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
    if payload.end_date is not None and payload.end_date < payload.start_date:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="end_date must be on or after start_date")
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
