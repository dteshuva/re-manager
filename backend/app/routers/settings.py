"""Per-account settings (INSIGHT_DASHBOARD_SPEC sub-step 6).

Currently just the attention-feed thresholds, stored in the single-row ``attention_settings``
table. Anyone can read them (the feed uses them); only an admin can change them.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user, require_admin
from app.models import AttentionSettings, User
from app.schemas import AttentionSettingsIn, AttentionSettingsOut

router = APIRouter(prefix="/settings", tags=["settings"])


def _get_or_create(db: Session) -> AttentionSettings:
    row = db.get(AttentionSettings, 1)
    if row is None:  # defensive: the migration seeds it, but never 500 if missing
        row = AttentionSettings(
            id=1,
            noi_drop_min_abs=0, noi_drop_min_pct=15,
            expense_spike_min_abs=0, expense_spike_min_pct=100,
            unit_noi_drop_min_abs=0, unit_noi_drop_min_pct=15,
            unit_expense_spike_min_abs=0, unit_expense_spike_min_pct=100,
            vacancy_min_occupancy_drop_pct=2,
            vacancy_high_absolute_pct=20,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


@router.get("/attention", response_model=AttentionSettingsOut)
def get_attention_settings(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Current attention-feed thresholds."""
    return _get_or_create(db)


@router.put("/attention", response_model=AttentionSettingsOut)
def update_attention_settings(
    payload: AttentionSettingsIn,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Update the attention-feed thresholds (admin only). Takes effect on the next feed load."""
    row = _get_or_create(db)
    for field, value in payload.model_dump().items():
        setattr(row, field, value)
    db.commit()
    db.refresh(row)
    return row
