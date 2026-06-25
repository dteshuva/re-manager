from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_admin
from app.models import AuditLog, PeriodStatus, User
from app.schemas import PeriodStatusOut

router = APIRouter(prefix="/periods", tags=["periods"])


@router.post("/{period_id}/unlock", response_model=PeriodStatusOut)
def unlock_period(
    period_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Unlock a locked property-month. Admin role only; every unlock is audited.

    Unlocking returns the period to ``posted`` so it can be edited and re-locked.
    """
    period = db.get(PeriodStatus, period_id)
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
            user_id=admin.id,
            action="unlock_period",
            entity="period_status",
            entity_id=str(period.id),
            before=before,
            after=after,
        )
    )
    db.commit()
    db.refresh(period)
    return period
