from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app import queries
from app.db import get_db
from app.deps import get_current_user
from app.models import Property, Unit, User
from app.schemas import (
    MonthlyPnL,
    PortfolioBreakdown,
    PropertyMonthlyPnL,
    UnitMonthlyPnL,
)

router = APIRouter(tags=["pnl"])

# Spec uses ?from=&to= ; `from` is a Python keyword, so accept it via an alias.
FromParam = Query(default=None, alias="from", description="First month (inclusive), YYYY-MM-01")
ToParam = Query(default=None, alias="to", description="Last month (inclusive), YYYY-MM-01")


@router.get("/portfolio/monthly", response_model=list[MonthlyPnL])
def portfolio_monthly(
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Portfolio P&L by month (rent, opex, NOI, below-line, cash flow).

    NOI and cash flow are computed in SQL from current classifications, never stored.
    """
    return queries.portfolio_monthly(db, date_from, date_to)


@router.get("/portfolio/breakdown", response_model=PortfolioBreakdown)
def portfolio_breakdown(
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Portfolio total for the period plus a per-property → per-unit breakdown.

    Each property carries its own total, its per-unit period totals, and the
    property-tier-only items (not allocated to units), so the unit → property → portfolio
    hierarchy is visible at a glance. NOI/cash flow are computed, never stored.
    """
    return queries.portfolio_breakdown(db, date_from, date_to)


@router.get("/properties/{property_id}/monthly", response_model=list[PropertyMonthlyPnL])
def property_monthly(
    property_id: str,
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Property P&L by month with an honest rollup split.

    The top-level metrics are the property total; ``units`` is the additive sum across
    this property's units; ``property_tier`` is the property-tier-only items (shared
    capex, debt service) that are NOT allocated down to units. For those metrics the
    property total deliberately does not equal the sum of the units.
    """
    _get_property(db, property_id)
    return queries.property_monthly(db, property_id, date_from, date_to)


@router.get(
    "/properties/{property_id}/units/monthly", response_model=list[UnitMonthlyPnL]
)
def property_units_monthly(
    property_id: str,
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Per-unit monthly breakdown for a property (property-tier-only items excluded)."""
    _get_property(db, property_id)
    return queries.property_units_monthly(db, property_id, date_from, date_to)


@router.get("/units/{unit_id}/monthly", response_model=list[UnitMonthlyPnL])
def unit_monthly(
    unit_id: str,
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Single-unit P&L by month."""
    unit = db.get(Unit, unit_id)
    if unit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unit not found")
    rows = queries.unit_monthly(db, unit_id, date_from, date_to)
    # Stamp unit identity even on months with no data so the caller always knows the unit.
    for r in rows:
        r.update(unit_id=unit.id, unit_number=unit.unit_number, label=unit.label)
    return rows


def _get_property(db: Session, property_id: str) -> Property:
    prop = db.get(Property, property_id)
    if prop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Property not found")
    return prop
