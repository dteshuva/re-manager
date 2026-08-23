"""Investment insights: per-property acquisition inputs + computed return metrics.

Inputs are stored (``property_investment``); every metric (cap rate, cash-on-cash, DSCR,
average cash-on-cash) is computed on read in :mod:`app.queries` from those inputs plus the
``property_month_summary`` rollup, so they always reflect the current P&L.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import queries
from app.db import get_db
from app.deps import Scope, get_scope
from app.models import PropertyInvestment
from app.schemas import (
    InvestmentMetrics,
    PortfolioInvestment,
    PropertyInvestmentIn,
    PropertyInvestmentOut,
)
from app.scoping import get_property_or_404

router = APIRouter(tags=["investments"])



@router.get("/investments", response_model=PortfolioInvestment)
def portfolio_investment(
    db: Session = Depends(get_db), scope: Scope = Depends(get_scope)
):
    """Every property with investment inputs + value-weighted portfolio aggregates."""
    return queries.portfolio_investment(db, scope.account_id)


@router.get("/properties/{property_id}/investment/metrics", response_model=InvestmentMetrics)
def property_investment_metrics(
    property_id: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)
):
    """One property's acquisition inputs + computed return metrics (nulls if no inputs yet)."""
    result = queries.investment_metrics(db, scope.account_id, property_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Property not found")
    return result


@router.put("/properties/{property_id}/investment", response_model=PropertyInvestmentOut)
def upsert_property_investment(
    property_id: str,
    payload: PropertyInvestmentIn,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Create or replace a property's acquisition inputs."""
    get_property_or_404(db, scope, property_id)
    inv = db.get(PropertyInvestment, property_id)
    if inv is None:
        inv = PropertyInvestment(property_id=property_id, **payload.model_dump())
        db.add(inv)
    else:
        for field, value in payload.model_dump().items():
            setattr(inv, field, value)
    db.commit()
    db.refresh(inv)
    return _investment_out(inv)


@router.delete("/properties/{property_id}/investment", status_code=status.HTTP_204_NO_CONTENT)
def delete_property_investment(
    property_id: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)
):
    """Clear a property's acquisition inputs (metrics become unavailable)."""
    get_property_or_404(db, scope, property_id)
    inv = db.get(PropertyInvestment, property_id)
    if inv is not None:
        db.delete(inv)
        db.commit()


def _investment_out(inv: PropertyInvestment) -> dict:
    price, closing, loan = float(inv.purchase_price), float(inv.closing_costs), float(inv.loan_amount)
    return {
        "property_id": inv.property_id,
        "purchase_price": price,
        "closing_costs": closing,
        "loan_amount": loan,
        "purchase_date": inv.purchase_date,
        "equity_invested": price - loan + closing,
        "updated_at": inv.updated_at,
    }
