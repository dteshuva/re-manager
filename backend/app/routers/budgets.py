"""Budget / variance: a flat annual plan per property, and actual-vs-plan variance.

Inputs are stored (``property_budget``, one row per property/year); every plan figure and
variance number is computed on read in :mod:`app.queries` from those inputs plus the existing
summary rollups, so it stays consistent with actuals math with no migration on reclassify.
Reads are open to any authed user (same as investments/records); writing a budget is
admin-gated, matching the other portfolio-shaping mutations (unlock, attention settings).
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app import queries
from app.db import get_db
from app.deps import Scope, get_scope, require_admin_scope
from app.models import PropertyBudget
from app.schemas import (
    PortfolioVariance,
    PropertyBudgetIn,
    PropertyBudgetOut,
    PropertyVariance,
)
from app.scoping import get_property_or_404

router = APIRouter(tags=["budget"])

FromParam = Query(default=None, alias="from", description="First month (inclusive), YYYY-MM-01")
ToParam = Query(default=None, alias="to", description="Last month (inclusive), YYYY-MM-01")



def _budget_out(b: PropertyBudget) -> PropertyBudgetOut:
    rent, opex = float(b.budgeted_gross_rent), float(b.budgeted_operating_expenses)
    return PropertyBudgetOut(
        property_id=b.property_id,
        year=b.year,
        budgeted_gross_rent=rent,
        budgeted_operating_expenses=opex,
        budgeted_noi=rent - opex,
        updated_at=b.updated_at,
    )


@router.get("/properties/{property_id}/budgets", response_model=list[PropertyBudgetOut])
def list_property_budgets(
    property_id: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)
):
    """Every annual budget on file for this property, oldest year first."""
    get_property_or_404(db, scope, property_id)
    rows = (
        db.query(PropertyBudget)
        .filter(PropertyBudget.property_id == property_id)
        .order_by(PropertyBudget.year)
        .all()
    )
    return [_budget_out(b) for b in rows]


@router.put("/properties/{property_id}/budgets/{year}", response_model=PropertyBudgetOut)
def upsert_property_budget(
    property_id: str,
    year: int,
    payload: PropertyBudgetIn,
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    """Create or replace one property's flat annual plan for ``year``. Admin only."""
    if year < 2000 or year > 2100:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="year must be between 2000 and 2100")
    get_property_or_404(db, scope, property_id)
    b = db.get(PropertyBudget, (property_id, year))
    if b is None:
        b = PropertyBudget(property_id=property_id, year=year, **payload.model_dump())
        db.add(b)
    else:
        for field, value in payload.model_dump().items():
            setattr(b, field, value)
    db.commit()
    db.refresh(b)
    return _budget_out(b)


@router.delete("/properties/{property_id}/budgets/{year}", status_code=status.HTTP_204_NO_CONTENT)
def delete_property_budget(
    property_id: str, year: int, db: Session = Depends(get_db), scope: Scope = Depends(require_admin_scope)
):
    """Remove one property's annual plan for ``year``. Admin only."""
    get_property_or_404(db, scope, property_id)
    b = db.get(PropertyBudget, (property_id, year))
    if b is not None:
        db.delete(b)
        db.commit()


@router.get("/properties/{property_id}/variance", response_model=PropertyVariance)
def property_variance(
    property_id: str,
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Actual vs. plan for one property over a period: the flat annual budget(s) covering the
    range are pro-rated to monthly (annual / 12) and summed against the actual NOI/rent/opex
    for the same span. ``plan_coverage_months`` < ``total_months`` means the plan is partial
    (some months in range have no budget row) — the $ and % figures still reflect only the
    covered months, not an assumed $0 plan for the rest.
    """
    result = queries.property_variance(db, scope.account_id, property_id, date_from, date_to)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Property not found")
    return result


@router.get("/portfolio/variance", response_model=PortfolioVariance)
def portfolio_variance(
    date_from: date | None = FromParam,
    date_to: date | None = ToParam,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Portfolio-level actual vs. plan: both actual and plan are scoped to only the properties
    with budget coverage for the period (``budgeted_property_count`` out of
    ``total_property_count``), so the two sides are always comparing the same property set.
    """
    return queries.portfolio_variance(db, scope.account_id, date_from, date_to)
