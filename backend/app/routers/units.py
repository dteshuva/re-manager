"""CRUD for individual units (creation/listing live under /properties)."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import Scope, get_scope, require_admin_scope
from app.schemas import UnitOut, UnitUpdate
from app.scoping import get_unit_or_404

router = APIRouter(prefix="/units", tags=["units"])


@router.get("/{unit_id}", response_model=UnitOut)
def get_unit(unit_id: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    return get_unit_or_404(db, scope, unit_id)


@router.patch("/{unit_id}", response_model=UnitOut)
def update_unit(
    unit_id: str,
    payload: UnitUpdate,
    db: Session = Depends(get_db),
    # Admin-gated (waterfall-followups item 1): `market_rent` is a portfolio-shaping
    # reference figure (same class of mutation as leases/budgets/tags, all admin-only).
    # This endpoint was previously open to any authed user for unit_number/label, but
    # nothing in the app calls it yet (no UI wired it up) — gating the whole endpoint is
    # simpler than a field-level split and costs nothing today.
    scope: Scope = Depends(require_admin_scope),
):
    unit = get_unit_or_404(db, scope, unit_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(unit, field, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="Unit number already exists for this property."
        )
    db.refresh(unit)
    return unit


@router.delete("/{unit_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_unit(unit_id: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    db.delete(get_unit_or_404(db, scope, unit_id))
    db.commit()
