"""CRUD for properties and their units."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import Property, Unit, User
from app.schemas import (
    PropertyCreate,
    PropertyOut,
    PropertyUpdate,
    UnitCreate,
    UnitOut,
    UnitUpdate,
)

router = APIRouter(prefix="/properties", tags=["properties"])


def get_property_or_404(db: Session, property_id: str) -> Property:
    prop = db.get(Property, property_id)
    if prop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Property not found")
    return prop


@router.get("", response_model=list[PropertyOut])
def list_properties(db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return db.scalars(select(Property).order_by(Property.name)).all()


@router.post("", response_model=PropertyOut, status_code=status.HTTP_201_CREATED)
def create_property(
    payload: PropertyCreate,
    db: Session = Depends(get_db),
    _u: User = Depends(get_current_user),
):
    prop = Property(**payload.model_dump())
    db.add(prop)
    db.commit()
    db.refresh(prop)
    return prop


@router.get("/{property_id}", response_model=PropertyOut)
def get_property(
    property_id: str, db: Session = Depends(get_db), _u: User = Depends(get_current_user)
):
    return get_property_or_404(db, property_id)


@router.patch("/{property_id}", response_model=PropertyOut)
def update_property(
    property_id: str,
    payload: PropertyUpdate,
    db: Session = Depends(get_db),
    _u: User = Depends(get_current_user),
):
    prop = get_property_or_404(db, property_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(prop, field, value)
    db.commit()
    db.refresh(prop)
    return prop


@router.delete("/{property_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_property(
    property_id: str, db: Session = Depends(get_db), _u: User = Depends(get_current_user)
):
    # Cascades remove units, monthly_records, line_items, and period_status.
    db.delete(get_property_or_404(db, property_id))
    db.commit()


# ---- Units nested under a property ----
@router.get("/{property_id}/units", response_model=list[UnitOut])
def list_units(
    property_id: str, db: Session = Depends(get_db), _u: User = Depends(get_current_user)
):
    get_property_or_404(db, property_id)
    return db.scalars(
        select(Unit).where(Unit.property_id == property_id).order_by(Unit.unit_number)
    ).all()


@router.post(
    "/{property_id}/units", response_model=UnitOut, status_code=status.HTTP_201_CREATED
)
def create_unit(
    property_id: str,
    payload: UnitCreate,
    db: Session = Depends(get_db),
    _u: User = Depends(get_current_user),
):
    prop = get_property_or_404(db, property_id)
    if prop.type != "multifamily":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Units can only be added to multifamily properties.",
        )
    unit = Unit(property_id=property_id, **payload.model_dump())
    db.add(unit)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="Unit number already exists for this property."
        )
    db.refresh(unit)
    return unit
