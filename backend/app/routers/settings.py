"""Per-account settings (INSIGHT_DASHBOARD_SPEC sub-step 6).

Currently just the attention-feed thresholds, one ``attention_settings`` row per account
(migration 0017 replaced the deployment-wide ``id = 1`` single-row pin with an
``account_id`` primary key). Any member of the account can read them (the feed uses them);
only that account's admin can change them.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from fastapi import HTTPException, status

from app.db import get_db
from app.deps import Scope, get_scope, require_admin_scope
from app.accounts import default_thresholds
from app.models import Account, AttentionSettings
from app.schemas import (
    AttentionSettingsIn,
    AttentionSettingsOut,
    GeneralSettingsIn,
    GeneralSettingsOut,
)

router = APIRouter(prefix="/settings", tags=["settings"])


def _get_or_create(db: Session, account_id: str) -> AttentionSettings:
    row = db.get(AttentionSettings, account_id)
    if row is None:
        # Defensive: signup writes this row, but an account provisioned before
        # app/accounts.py existed won't have one. Seeded from the SAME config defaults
        # the attention engine falls back to, so the UI shows the values actually in use.
        row = default_thresholds(account_id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


@router.get("/attention", response_model=AttentionSettingsOut)
def get_attention_settings(
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Current attention-feed thresholds."""
    return _get_or_create(db, scope.account_id)


@router.put("/attention", response_model=AttentionSettingsOut)
def update_attention_settings(
    payload: AttentionSettingsIn,
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    """Update the attention-feed thresholds (admin only). Takes effect on the next feed load."""
    row = _get_or_create(db, scope.account_id)
    for field, value in payload.model_dump().items():
        setattr(row, field, value)
    db.commit()
    db.refresh(row)
    return row


@router.get("/general", response_model=GeneralSettingsOut)
def get_general_settings(
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Account-wide display preferences (currently just the display currency). Any member may
    read them (the whole UI formats figures with the currency)."""
    account = db.get(Account, scope.account_id)
    return GeneralSettingsOut(currency=account.currency if account else "USD")


@router.put("/general", response_model=GeneralSettingsOut)
def update_general_settings(
    payload: GeneralSettingsIn,
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    """Update account display preferences (admin only). Currency is a pure symbol/locale
    toggle — no stored amount is converted or changed."""
    account = db.get(Account, scope.account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Account not found")
    account.currency = payload.currency
    db.commit()
    return GeneralSettingsOut(currency=account.currency)
