"""Account-scoped entity resolution — the single choke point for "does the caller own this?".

Before migration 0017 five routers each carried their own ``_get_property_or_404(db, id)``
that looked a property up by primary key alone. With accounts, a bare ``db.get(Property, id)``
is a cross-tenant read, so those copies are replaced by the scoped resolvers here.

The contract is deliberately "not yours == 404, never 403": a 403 would confirm the id
exists, which leaks the shape of another account's portfolio to anyone who can guess a UUID.
An account can therefore never distinguish another account's property from one that was
never created.
"""

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import Scope
from app.models import Lease, MonthlyRecord, Property, Unit


def get_property_or_404(db: Session, scope: Scope, property_id: str) -> Property:
    prop = db.scalar(
        select(Property).where(
            Property.id == property_id, Property.account_id == scope.account_id
        )
    )
    if prop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Property not found")
    return prop


def get_unit_or_404(db: Session, scope: Scope, unit_id: str) -> Unit:
    """A unit is owned transitively, through its property — hence the join."""
    unit = db.scalar(
        select(Unit)
        .join(Property, Property.id == Unit.property_id)
        .where(Unit.id == unit_id, Property.account_id == scope.account_id)
    )
    if unit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unit not found")
    return unit


def get_lease_or_404(db: Session, scope: Scope, lease_id: str) -> Lease:
    lease = db.scalar(
        select(Lease)
        .join(Unit, Unit.id == Lease.unit_id)
        .join(Property, Property.id == Unit.property_id)
        .where(Lease.id == lease_id, Property.account_id == scope.account_id)
    )
    if lease is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Lease not found")
    return lease


def get_record_or_404(db: Session, scope: Scope, record_id: str) -> MonthlyRecord:
    """monthly_records carries its own account_id (kept honest against the property's by the
    composite FK from migration 0017), so this needs no join."""
    record = db.scalar(
        select(MonthlyRecord).where(
            MonthlyRecord.id == record_id, MonthlyRecord.account_id == scope.account_id
        )
    )
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Monthly record not found")
    return record
