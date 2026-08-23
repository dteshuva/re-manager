"""Compliance tracking: statutory certificates / licences on a property (migration 0020).

UK rentals must hold current certificates — EICR, Gas Safety / CP12, EPC, HMO/selective
licences, PAT, Legionella and fire-risk assessments. This router is the CRUD surface for
recording them and the two read endpoints the UI needs:

  * ``GET /compliance/certificates`` — every certificate across THIS account's portfolio,
    each with a derived status, soonest-expiry first (the Compliance tab's table).
  * ``GET /compliance/alerts``       — only the certificates that have expired or expire
    within the alert window (the Dashboard's compliance banner).

Certificates hang off a property (no ``account_id`` of their own), so every path is scoped by
resolving the property via ``get_property_or_404`` or by joining to ``properties`` on
``account_id`` — exactly the pattern ``tags``/``budgets`` use. Reads are open to any member;
writes are admin-gated, matching the other portfolio-shaping mutations.

Status is DERIVED from ``expiry_date`` versus today, never stored: 'expired' (past),
'expiring' (within ``window`` days), or 'valid'. That keeps it correct as the calendar moves
without a nightly job.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import Scope, get_scope, require_admin_scope
from app.models import Property, PropertyCertificate
from app.schemas import PropertyCertificateIn, PropertyCertificateOut
from app.scoping import get_property_or_404

router = APIRouter(tags=["compliance"])

# Default "expiring soon" horizon. A certificate inside this many days of its expiry is
# flagged amber (still valid, but renew now); overridable per-request via ?window=.
DEFAULT_EXPIRING_WINDOW_DAYS = 60

# The common UK certificate types, offered by the UI as a preset list. cert_type is free-text,
# so this is a convenience only — a landlord may record anything their jurisdiction requires.
UK_CERTIFICATE_TYPES = [
    "EICR (Electrical Installation)",
    "Gas Safety (CP12)",
    "EPC (Energy Performance)",
    "PAT (Portable Appliance)",
    "Legionella Risk Assessment",
    "Fire Risk Assessment",
    "HMO Licence",
    "Selective Licence",
    "Emergency Lighting",
    "Fire Alarm Servicing",
]


def _status(expiry: date, today: date, window_days: int) -> str:
    days = (expiry - today).days
    if days < 0:
        return "expired"
    if days <= window_days:
        return "expiring"
    return "valid"


def _to_out(cert: PropertyCertificate, property_name: str, today: date, window_days: int) -> PropertyCertificateOut:
    return PropertyCertificateOut(
        id=cert.id,
        property_id=cert.property_id,
        property_name=property_name,
        cert_type=cert.cert_type,
        expiry_date=cert.expiry_date,
        issue_date=cert.issue_date,
        reference=cert.reference,
        provider=cert.provider,
        notes=cert.notes,
        status=_status(cert.expiry_date, today, window_days),
        days_to_expiry=(cert.expiry_date - today).days,
    )


@router.get("/compliance/types", response_model=list[str])
def list_certificate_types():
    """The UK certificate-type preset list for the add-certificate form's dropdown."""
    return UK_CERTIFICATE_TYPES


@router.get("/compliance/certificates", response_model=list[PropertyCertificateOut])
def list_all_certificates(
    window: int = Query(DEFAULT_EXPIRING_WINDOW_DAYS, ge=0, le=365),
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Every certificate across this account's portfolio, soonest-expiry first."""
    today = date.today()
    rows = db.execute(
        select(PropertyCertificate, Property.name)
        .join(Property, Property.id == PropertyCertificate.property_id)
        .where(Property.account_id == scope.account_id)
        .order_by(PropertyCertificate.expiry_date.asc())
    ).all()
    return [_to_out(cert, name, today, window) for cert, name in rows]


@router.get("/compliance/alerts", response_model=list[PropertyCertificateOut])
def list_certificate_alerts(
    window: int = Query(DEFAULT_EXPIRING_WINDOW_DAYS, ge=0, le=365),
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Only the certificates needing attention — expired, or expiring within ``window`` days —
    soonest (most overdue) first. Powers the Dashboard's compliance banner."""
    today = date.today()
    rows = db.execute(
        select(PropertyCertificate, Property.name)
        .join(Property, Property.id == PropertyCertificate.property_id)
        .where(Property.account_id == scope.account_id)
        .order_by(PropertyCertificate.expiry_date.asc())
    ).all()
    out = [_to_out(cert, name, today, window) for cert, name in rows]
    return [c for c in out if c.status in ("expired", "expiring")]


@router.get("/properties/{property_id}/certificates", response_model=list[PropertyCertificateOut])
def list_property_certificates(
    property_id: str,
    window: int = Query(DEFAULT_EXPIRING_WINDOW_DAYS, ge=0, le=365),
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    prop = get_property_or_404(db, scope, property_id)
    today = date.today()
    certs = db.scalars(
        select(PropertyCertificate)
        .where(PropertyCertificate.property_id == property_id)
        .order_by(PropertyCertificate.expiry_date.asc())
    ).all()
    return [_to_out(c, prop.name, today, window) for c in certs]


@router.post(
    "/properties/{property_id}/certificates",
    response_model=PropertyCertificateOut,
    status_code=status.HTTP_201_CREATED,
)
def add_property_certificate(
    property_id: str,
    payload: PropertyCertificateIn,
    window: int = Query(DEFAULT_EXPIRING_WINDOW_DAYS, ge=0, le=365),
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    prop = get_property_or_404(db, scope, property_id)
    cert = PropertyCertificate(property_id=property_id, **payload.model_dump())
    db.add(cert)
    db.commit()
    db.refresh(cert)
    return _to_out(cert, prop.name, date.today(), window)


def _get_cert_or_404(db: Session, scope: Scope, certificate_id: str) -> tuple[PropertyCertificate, str]:
    """A certificate is owned transitively through its property — hence the join on
    account_id. Not-yours == 404, same contract as app/scoping.py."""
    row = db.execute(
        select(PropertyCertificate, Property.name)
        .join(Property, Property.id == PropertyCertificate.property_id)
        .where(
            PropertyCertificate.id == certificate_id,
            Property.account_id == scope.account_id,
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Certificate not found")
    return row[0], row[1]


@router.put("/certificates/{certificate_id}", response_model=PropertyCertificateOut)
def update_certificate(
    certificate_id: str,
    payload: PropertyCertificateIn,
    window: int = Query(DEFAULT_EXPIRING_WINDOW_DAYS, ge=0, le=365),
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    cert, property_name = _get_cert_or_404(db, scope, certificate_id)
    for field, value in payload.model_dump().items():
        setattr(cert, field, value)
    db.commit()
    db.refresh(cert)
    return _to_out(cert, property_name, date.today(), window)


@router.delete("/certificates/{certificate_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_certificate(
    certificate_id: str,
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    cert, _ = _get_cert_or_404(db, scope, certificate_id)
    db.delete(cert)
    db.commit()
