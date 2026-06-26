"""CRUD for individual units (creation/listing live under /properties)."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import Unit, User
from app.schemas import UnitOut, UnitUpdate

router = APIRouter(prefix="/units", tags=["units"])


def get_unit_or_404(db: Session, unit_id: str) -> Unit:
    unit = db.get(Unit, unit_id)
    if unit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unit not found")
    return unit


@router.get("/{unit_id}", response_model=UnitOut)
def get_unit(unit_id: str, db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return get_unit_or_404(db, unit_id)


@router.patch("/{unit_id}", response_model=UnitOut)
def update_unit(
    unit_id: str,
    payload: UnitUpdate,
    db: Session = Depends(get_db),
    _u: User = Depends(get_current_user),
):
    unit = get_unit_or_404(db, unit_id)
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
def delete_unit(unit_id: str, db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    db.delete(get_unit_or_404(db, unit_id))
    db.commit()
