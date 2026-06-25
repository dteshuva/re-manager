from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import queries
from app.db import get_db
from app.deps import get_current_user
from app.models import User
from app.schemas import MonthlyPnL

router = APIRouter(tags=["pnl"])


@router.get("/portfolio/monthly", response_model=list[MonthlyPnL])
def portfolio_monthly(
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Portfolio P&L by month (rent, opex, NOI, below-line, cash flow).

    NOI and cash flow are computed in SQL from current classifications, never stored.
    """
    return queries.portfolio_monthly(db, date_from, date_to)


@router.get("/properties/{property_id}/monthly", response_model=list[MonthlyPnL])
def property_monthly(
    property_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Property P&L by month, including property-tier-only items (capex, debt service)."""
    return queries.property_monthly(db, property_id, date_from, date_to)
