from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app import attention as attention_engine
from app import queries
from app.db import get_db
from app.deps import get_current_user
from app.models import Property, Unit, User
from app.schemas import (
    AttentionFeed,
    MonthlyPnL,
    PortfolioBreakdown,
    PortfolioDashboard,
    PropertyDashboard,
    PropertyMonthlyPnL,
    UnitDetail,
    UnitMonthlyPnL,
    UnitRoster,
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


@router.get("/portfolio/dashboard", response_model=PortfolioDashboard)
def portfolio_dashboard(
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Portfolio landing-page KPIs + T12 sparklines for the attention-first dashboard.

    Reads STRICTLY from ``portfolio_month_summary`` (the pre-aggregated rollup), never from
    live line-item aggregation. Period-aware: no params → latest single month (vs prior
    month); a ``from``/``to`` range (month, YTD, T12, custom) sums the period and compares
    against the immediately preceding equal-length period.
    """
    return queries.portfolio_dashboard(db, date_from, date_to)


@router.get("/portfolio/attention", response_model=AttentionFeed)
def portfolio_attention(
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Ranked attention feed over the selected period: NOI drops, expense spikes, vacancies,
    missing data — for every month in the period, so anomalies in a mid-period month still
    surface (each tagged with its month). From the rollups; ranked by $ magnitude.
    """
    return attention_engine.attention_feed(db, date_from, date_to, limit=limit)


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


@router.get("/properties/{property_id}/dashboard", response_model=PropertyDashboard)
def property_dashboard(
    property_id: str,
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Property-scoped, period-aware KPI band + T12 sparklines, from property_month_summary."""
    result = queries.property_dashboard(db, property_id, date_from, date_to)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Property not found")
    return result


@router.get("/properties/{property_id}/attention", response_model=AttentionFeed)
def property_attention(
    property_id: str,
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Property-scoped attention feed over the selected period (unit NOI drops / vacancies /
    opex spikes + the property's own category expense spikes), computed from the rollups."""
    result = attention_engine.property_attention(db, property_id, date_from, date_to, limit=limit)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Property not found")
    return result


@router.get("/properties/{property_id}/units/roster", response_model=UnitRoster)
def property_unit_roster(
    property_id: str,
    month: date | None = Query(default=None, description="Anchor month YYYY-MM-01"),
    sort: str = Query(default="unit_number"),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Server-paginated, sortable unit roster for a property-month (never renders thousands).

    Each row carries the unit's month metrics, occupied/vacant status, and NOI change vs the
    prior month. Defaults the month to the property's latest summarized month.
    """
    prop = _get_property(db, property_id)
    if month is None:
        from sqlalchemy import text as _text  # local: latest summarized month for this property
        month = db.execute(
            _text("SELECT max(month) FROM property_month_summary WHERE property_id = :id"),
            {"id": prop.id},
        ).scalar()
    if month is None:
        return {"month": None, "prior_month": None, "total": 0, "rows": []}
    return queries.unit_roster(
        db, property_id, month, sort=sort, order=order, limit=limit, offset=offset
    )


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


@router.get("/units/{unit_id}/detail", response_model=UnitDetail)
def unit_detail(
    unit_id: str,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Level 3 unit detail: identity, current status, and full monthly P&L (from the rollup).

    Unit scope excludes property-tier-only items by design; vacant months show as zeros.
    """
    result = queries.unit_detail(db, unit_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unit not found")
    return result


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
