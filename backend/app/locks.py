"""Period-status guards shared by the mutating CRUD routers.

A property-month is *locked* when its ``period_status`` row has status ``locked``.
Locked months are protected from edits; only an admin can reopen one via
``POST /periods/{id}/unlock`` (which writes an audit_log row). Every write to a
monthly_record or its line items must pass through :func:`assert_unlocked` first.

:func:`ensure_draft_period` is the other half: anything that creates a property-month from
scratch must also give it a status row, so the month shows up in the workflow rather than
existing invisibly.
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


def ensure_draft_period(db: Session, property_id: str, month: date) -> None:
    """Give a brand-new property-month a ``draft`` status row if it has none.

    Without this a freshly created record has no period_status at all, so
    ``GET /periods?property_id=&month=`` returns ``[]`` right after the write and the Entry
    screen's status badge has nothing to show. Only inserts when no row exists yet; it never
    downgrades an existing posted/locked status.

    Caller owns the transaction — this only adds to the session.
    """
    exists = db.scalar(
        select(PeriodStatus.id).where(
            PeriodStatus.property_id == property_id, PeriodStatus.month == month
        )
    )
    if exists is None:
        db.add(PeriodStatus(property_id=property_id, month=month, status="draft"))
