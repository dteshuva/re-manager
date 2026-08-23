from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import Scope, get_scope, require_admin_scope
from app.models import AuditLog, PeriodStatus, Property
from app.schemas import PeriodStatusOut, PeriodStatusUpsert
from app.scoping import get_property_or_404
from app.summaries import refresh_month

router = APIRouter(prefix="/periods", tags=["periods"])


def _first_of_month(d: date) -> date:
    return d.replace(day=1)


@router.get("", response_model=list[PeriodStatusOut])
def list_periods(
    property_id: str | None = None,
    month: date | None = None,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """List property-month statuses, optionally filtered by property and/or month."""
    # period_status has no account_id of its own — it hangs off a property — so the scope
    # comes from a join, and an unfiltered list returns only this account's rows.
    stmt = select(PeriodStatus).join(
        Property, Property.id == PeriodStatus.property_id
    ).where(Property.account_id == scope.account_id)
    if property_id:
        stmt = stmt.where(PeriodStatus.property_id == property_id)
    if month:
        stmt = stmt.where(PeriodStatus.month == _first_of_month(month))
    return db.scalars(stmt.order_by(PeriodStatus.month)).all()


@router.put("", response_model=PeriodStatusOut)
def set_period_status(
    payload: PeriodStatusUpsert,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Set a property-month's workflow status (draft → posted → locked), upserting the row.

    Reopening a locked month is intentionally *not* allowed here — it must go through the
    admin-only ``POST /periods/{id}/unlock`` so the change is audited.
    """
    month = _first_of_month(payload.month)
    get_property_or_404(db, scope, payload.property_id)

    ps = db.scalar(
        select(PeriodStatus).where(
            PeriodStatus.property_id == payload.property_id, PeriodStatus.month == month
        )
    )
    if ps is not None and ps.status == "locked" and payload.status != "locked":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Period is locked; use POST /periods/{id}/unlock (admin only) to reopen.",
        )
    if ps is None:
        ps = PeriodStatus(property_id=payload.property_id, month=month, status=payload.status)
        db.add(ps)
    else:
        ps.status = payload.status
    # Posting/locking a month is the trigger to refresh its pre-aggregated summary.
    if payload.status in ("posted", "locked"):
        refresh_month(db, payload.property_id, month)
    db.commit()
    db.refresh(ps)
    return ps


@router.post("/{period_id}/unlock", response_model=PeriodStatusOut)
def unlock_period(
    period_id: str,
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    """Unlock a locked property-month. Admin role only; every unlock is audited.

    Unlocking returns the period to ``posted`` so it can be edited and re-locked.
    """
    period = db.scalar(
        select(PeriodStatus)
        .join(Property, Property.id == PeriodStatus.property_id)
        .where(PeriodStatus.id == period_id, Property.account_id == scope.account_id)
    )
    if period is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Period not found")
    if period.status != "locked":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Period is '{period.status}', only locked periods can be unlocked",
        )

    before = {"status": period.status}
    period.status = "posted"
    after = {"status": period.status}

    db.add(
        AuditLog(
            account_id=scope.account_id,
            user_id=scope.user.id,
            action="unlock_period",
            entity="period_status",
            entity_id=str(period.id),
            before=before,
            after=after,
        )
    )
    # Reopening returns the month to 'posted'; keep its summary current.
    refresh_month(db, period.property_id, period.month)
    db.commit()
    db.refresh(period)
    return period
