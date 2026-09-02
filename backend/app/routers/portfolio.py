from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app import attention as attention_engine
from app import queries
from app.db import get_db
from app.deps import Scope, get_scope
from app.scoping import get_property_or_404, get_unit_or_404
from app.schemas import (
    AttentionFeed,
    MonthlyPnL,
    PortfolioBenchmarks,
    PortfolioBreakdown,
    PortfolioDashboard,
    PropertyDashboard,
    PropertyMonthlyPnL,
    UnitDetail,
    UnitMonthlyPnL,
    UnitRoster,
    WorstUnitsLeaderboard,
)

router = APIRouter(tags=["pnl"])

# Spec uses ?from=&to= ; `from` is a Python keyword, so accept it via an alias.
FromParam = Query(default=None, alias="from", description="First month (inclusive), YYYY-MM-01")
ToParam = Query(default=None, alias="to", description="Last month (inclusive), YYYY-MM-01")
# Repeatable ?tags=a&tags=b ; a property matches if it carries ANY of the given tags (OR).
TagsParam = Query(default=None, description="Optional tag filter (OR semantics); repeatable")


@router.get("/portfolio/monthly", response_model=list[MonthlyPnL])
def portfolio_monthly(
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    tags: list[str] | None = TagsParam,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Portfolio P&L by month (rent, opex, NOI, below-line, cash flow).

    NOI and cash flow are computed in SQL from current classifications, never stored.
    """
    return queries.portfolio_monthly(db, scope.account_id, date_from, date_to, tags)


@router.get("/portfolio/dashboard", response_model=PortfolioDashboard)
def portfolio_dashboard(
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    tags: list[str] | None = TagsParam,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Portfolio landing-page KPIs + T12 sparklines for the attention-first dashboard.

    Reads STRICTLY from ``portfolio_month_summary`` (the pre-aggregated rollup), never from
    live line-item aggregation. Period-aware: no params → latest single month (vs prior
    month); a ``from``/``to`` range (month, YTD, T12, custom) sums the period and compares
    against the immediately preceding equal-length period. ``tags`` optionally scopes the
    whole KPI band to properties carrying ANY of the given tags (OR semantics).
    """
    return queries.portfolio_dashboard(db, scope.account_id, date_from, date_to, tags)


@router.get("/portfolio/attention", response_model=AttentionFeed)
def portfolio_attention(
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    limit: int = Query(default=50, ge=1, le=200),
    tags: list[str] | None = TagsParam,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Ranked attention feed over the selected period: NOI drops, expense spikes, vacancies,
    missing data — for every month in the period, so anomalies in a mid-period month still
    surface (each tagged with its month). From the rollups; ranked by $ magnitude. ``tags``
    optionally scopes every detector to properties carrying ANY of the given tags.
    """
    return attention_engine.attention_feed(
        db, scope.account_id, date_from, date_to, limit=limit, tags=tags
    )


@router.get("/portfolio/worst-units", response_model=WorstUnitsLeaderboard)
def portfolio_worst_units(
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    limit: int = Query(default=10, ge=1, le=100),
    tags: list[str] | None = TagsParam,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Portfolio-wide "worst units" leaderboard: the top-N units across EVERY property with
    the biggest NOI drop in the selected period, ranked by $ magnitude — no threshold floor
    (a ranked list, not an alert feed). Complements ``/portfolio/attention`` (worst
    properties) and ``/properties/{id}/attention`` (worst units within ONE property).
    """
    return attention_engine.worst_units(
        db, scope.account_id, date_from, date_to, limit=limit, tags=tags
    )


@router.get("/portfolio/breakdown", response_model=PortfolioBreakdown)
def portfolio_breakdown(
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    tags: list[str] | None = TagsParam,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Portfolio total for the period plus a per-property → per-unit breakdown.

    Each property carries its own total, its per-unit period totals, and the
    property-tier-only items (not allocated to units), so the unit → property → portfolio
    hierarchy is visible at a glance. NOI/cash flow are computed, never stored. ``tags``
    optionally scopes to properties carrying ANY of the given tags (OR semantics).
    """
    return queries.portfolio_breakdown(db, scope.account_id, date_from, date_to, tags)


@router.get("/portfolio/benchmarks", response_model=PortfolioBenchmarks)
def portfolio_benchmarks(
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    tags: list[str] | None = TagsParam,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Benchmark every property against the portfolio's simple-mean average on NOI/unit,
    operating expense ratio, physical + economic occupancy, cap rate, and cash-on-cash, with
    each property's delta vs the average and its rank/percentile among the portfolio.

    NOI/opex-ratio/physical-occupancy are period-aware (same resolution as
    ``/portfolio/dashboard``: no params → latest summarized month). Economic occupancy and
    cap rate/cash-on-cash reuse the rent roll's current lease snapshot and the investment
    metrics' trailing-12 figures respectively — see ``queries.portfolio_benchmarks`` and
    ``PortfolioBenchmarks`` for why those two are not period-scoped. ``tags`` optionally
    scopes the whole comparison (rows AND the average) to properties carrying ANY of the
    given tags (OR semantics). The average here is a simple per-property mean — deliberately
    NOT value-weighted like ``/investments``' aggregates.
    """
    return queries.portfolio_benchmarks(db, scope.account_id, date_from, date_to, tags)


@router.get("/properties/{property_id}/monthly", response_model=list[PropertyMonthlyPnL])
def property_monthly(
    property_id: str,
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Property P&L by month with an honest rollup split.

    The top-level metrics are the property total; ``units`` is the additive sum across
    this property's units; ``property_tier`` is the property-tier-only items (shared
    capex, debt service) that are NOT allocated down to units. For those metrics the
    property total deliberately does not equal the sum of the units.
    """
    get_property_or_404(db, scope, property_id)
    return queries.property_monthly(db, scope.account_id, property_id, date_from, date_to)


@router.get("/properties/{property_id}/dashboard", response_model=PropertyDashboard)
def property_dashboard(
    property_id: str,
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Property-scoped, period-aware KPI band + T12 sparklines, from property_month_summary."""
    result = queries.property_dashboard(db, scope.account_id, property_id, date_from, date_to)
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
    scope: Scope = Depends(get_scope),
):
    """Property-scoped attention feed over the selected period (unit NOI drops / vacancies /
    opex spikes + the property's own category expense spikes), computed from the rollups."""
    result = attention_engine.property_attention(
        db, scope.account_id, property_id, date_from, date_to, limit=limit
    )
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
    scope: Scope = Depends(get_scope),
):
    """Server-paginated, sortable unit roster for a property-month (never renders thousands).

    Each row carries the unit's month metrics, occupied/vacant status, and NOI change vs the
    prior month. Defaults the month to the property's latest month with actual (non-future) data.
    """
    prop = get_property_or_404(db, scope, property_id)
    if month is None:
        # Latest month with actual data, never one scheduled ahead — see
        # queries.latest_actual_month for why the roster must not default into the future.
        month = queries.latest_actual_month(
            db, "property_month_summary",
            "property_id = :id AND account_id = :account_id",
            {"id": prop.id, "account_id": scope.account_id},
        )
    if month is None:
        return {"month": None, "prior_month": None, "total": 0, "rows": []}
    return queries.unit_roster(
        db, scope.account_id, property_id, month,
        sort=sort, order=order, limit=limit, offset=offset,
    )


@router.get(
    "/properties/{property_id}/units/monthly", response_model=list[UnitMonthlyPnL]
)
def property_units_monthly(
    property_id: str,
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Per-unit monthly breakdown for a property (property-tier-only items excluded)."""
    get_property_or_404(db, scope, property_id)
    return queries.property_units_monthly(
        db, scope.account_id, property_id, date_from, date_to
    )


@router.get("/units/{unit_id}/detail", response_model=UnitDetail)
def unit_detail(
    unit_id: str,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Level 3 unit detail: identity, current status, and full monthly P&L (from the rollup).

    Unit scope excludes property-tier-only items by design; vacant months show as zeros.
    """
    result = queries.unit_detail(db, scope.account_id, unit_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unit not found")
    return result


@router.get("/units/{unit_id}/monthly", response_model=list[UnitMonthlyPnL])
def unit_monthly(
    unit_id: str,
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Single-unit P&L by month."""
    unit = get_unit_or_404(db, scope, unit_id)
    rows = queries.unit_monthly(db, scope.account_id, unit_id, date_from, date_to)
    # Stamp unit identity even on months with no data so the caller always knows the unit.
    for r in rows:
        r.update(unit_id=unit.id, unit_number=unit.unit_number, label=unit.label)
    return rows
