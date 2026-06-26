"""Month-locking guard shared by the mutating CRUD routers.

A property-month is *locked* when its ``period_status`` row has status ``locked``.
Locked months are protected from edits; only an admin can reopen one via
``POST /periods/{id}/unlock`` (which writes an audit_log row). Every write to a
monthly_record or its line items must pass through :func:`assert_unlocked` first.
"""

from datetime import date

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PeriodStatus


def assert_unlocked(db: Session, property_id: str, month: date) -> None:
    ps = db.scalar(
        select(PeriodStatus).where(
            PeriodStatus.property_id == property_id, PeriodStatus.month == month
        )
    )
    if ps is not None and ps.status == "locked":
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=f"Period {month:%Y-%m} is locked; an admin must unlock it to edit.",
        )
