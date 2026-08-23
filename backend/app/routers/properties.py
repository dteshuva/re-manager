"""CRUD for properties and their units."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import Scope, get_scope
from app.models import Property, Unit
from app.schemas import (
    PropertyCreate,
    PropertyOut,
    PropertyUpdate,
    UnitCreate,
    UnitOut,
    UnitUpdate,
)
from app.scoping import get_property_or_404

router = APIRouter(prefix="/properties", tags=["properties"])


@router.get("", response_model=list[PropertyOut])
def list_properties(
    q: str | None = Query(
        default=None, description="Case-insensitive filter on property name/address"
    ),
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    stmt = (
        select(Property)
        .where(Property.account_id == scope.account_id)
        .order_by(Property.name)
    )
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(or_(Property.name.ilike(pattern), Property.address.ilike(pattern)))
    return db.scalars(stmt).all()


@router.post("", response_model=PropertyOut, status_code=status.HTTP_201_CREATED)
def create_property(
    payload: PropertyCreate,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    prop = Property(account_id=scope.account_id, **payload.model_dump())
    db.add(prop)
    db.commit()
    db.refresh(prop)
    return prop


@router.get("/{property_id}", response_model=PropertyOut)
def get_property(
    property_id: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)
):
    return get_property_or_404(db, scope, property_id)


@router.patch("/{property_id}", response_model=PropertyOut)
def update_property(
    property_id: str,
    payload: PropertyUpdate,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    prop = get_property_or_404(db, scope, property_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(prop, field, value)
    db.commit()
    db.refresh(prop)
    return prop


@router.delete("/{property_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_property(
    property_id: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)
):
    # Cascades remove units, monthly_records, line_items, and period_status.
    db.delete(get_property_or_404(db, scope, property_id))
    db.commit()


# ---- Units nested under a property ----
@router.get("/{property_id}/units", response_model=list[UnitOut])
def list_units(
    property_id: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)
):
    get_property_or_404(db, scope, property_id)
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
    scope: Scope = Depends(get_scope),
):
    prop = get_property_or_404(db, scope, property_id)
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
