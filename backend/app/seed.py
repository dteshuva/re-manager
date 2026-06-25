"""Seed sample data so the dashboard shows numbers immediately.

Run after migrations:  python -m app.seed

Creates:
  * an admin user (from SEED_ADMIN_EMAIL / SEED_ADMIN_PASSWORD)
  * 3 properties: one multifamily with 3 units + two single-asset properties
  * a global category list spanning every classification
  * 3 months (Jan–Mar 2026) of unit-level and property-tier line items
  * period_status rows, including one locked month to exercise admin unlock

Re-running wipes the domain data (properties/units/categories/records/line items/
periods/audit) and reseeds; the admin user is upserted.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import delete, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    AuditLog,
    Category,
    LineItem,
    MonthlyRecord,
    PeriodStatus,
    Property,
    Unit,
    User,
)
from app.security import hash_password

MONTHS = [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)]

# name -> default_classification
CATEGORIES = {
    "Rent": "rent",
    "Property Tax": "operating",
    "Insurance": "operating",
    "Repairs & Maintenance": "operating",
    "Property Management": "operating",
    "Utilities": "operating",
    "Roof Replacement": "capex",
    "Mortgage": "debt_service",
    "Owner Distribution": "other_below_line",
}


def _wipe(db) -> None:
    # Order respects FKs (line_items -> monthly_records -> units -> properties).
    for model in (AuditLog, LineItem, MonthlyRecord, PeriodStatus, Unit, Property, Category):
        db.execute(delete(model))
    db.commit()


def _upsert_admin(db) -> User:
    settings = get_settings()
    admin = db.scalar(select(User).where(User.email == settings.seed_admin_email))
    if admin is None:
        admin = User(
            email=settings.seed_admin_email,
            hashed_password=hash_password(settings.seed_admin_password),
            role="admin",
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)
        print(f"  created admin user: {settings.seed_admin_email}")
    else:
        print(f"  admin user already exists: {settings.seed_admin_email}")
    return admin


def _add_record(db, *, property_id, unit_id, month, items, notes=None) -> None:
    """Create a monthly_record and its line items. `items` = {category_name: amount}."""
    record = MonthlyRecord(property_id=property_id, unit_id=unit_id, month=month, notes=notes)
    db.add(record)
    db.flush()
    for name, amount in items.items():
        db.add(
            LineItem(
                monthly_record_id=record.id,
                category_id=CAT[name].id,
                amount=Decimal(str(amount)),
            )
        )


CAT: dict[str, Category] = {}


def main() -> None:
    db = SessionLocal()
    try:
        print("Seeding sample data...")
        _wipe(db)
        _upsert_admin(db)

        # --- categories ---
        for name, classification in CATEGORIES.items():
            cat = Category(name=name, default_classification=classification)
            db.add(cat)
            CAT[name] = cat
        db.flush()

        # --- Property 1: multifamily with 3 units ---
        maple = Property(name="Maple Court Apartments", type="multifamily", address="120 Maple Ct")
        db.add(maple)
        db.flush()
        units = []
        for n in ("101", "102", "103"):
            u = Unit(property_id=maple.id, unit_number=n, label=f"Unit {n}")
            db.add(u)
            units.append(u)
        db.flush()

        for i, month in enumerate(MONTHS):
            bump = Decimal(i) * Decimal("25")  # small month-over-month variation
            for u in units:
                _add_record(
                    db,
                    property_id=maple.id,
                    unit_id=u.id,
                    month=month,
                    items={
                        "Rent": Decimal("1500") + bump,
                        "Repairs & Maintenance": Decimal("120"),
                        "Utilities": Decimal("80"),
                    },
                )
            # property-tier-only items (NOT allocated to units): shared opex, capex, debt
            tier_items = {
                "Property Tax": Decimal("700"),
                "Insurance": Decimal("300"),
                "Property Management": Decimal("450"),
                "Mortgage": Decimal("2600"),
            }
            if month == MONTHS[1]:  # one-time capex in February
                tier_items["Roof Replacement"] = Decimal("8000")
            _add_record(
                db,
                property_id=maple.id,
                unit_id=None,
                month=month,
                items=tier_items,
                notes="Property-tier shared items",
            )

        # --- Property 2: single-asset (residential) ---
        birch = Property(name="Birch Street House", type="single", address="45 Birch St")
        db.add(birch)
        db.flush()
        for month in MONTHS:
            _add_record(
                db,
                property_id=birch.id,
                unit_id=None,
                month=month,
                items={
                    "Rent": Decimal("2400"),
                    "Property Tax": Decimal("350"),
                    "Insurance": Decimal("110"),
                    "Repairs & Maintenance": Decimal("90"),
                    "Mortgage": Decimal("1500"),
                },
            )

        # --- Property 3: single-asset (commercial) ---
        cedar = Property(name="Cedar Plaza Retail", type="single", address="900 Cedar Ave")
        db.add(cedar)
        db.flush()
        for month in MONTHS:
            _add_record(
                db,
                property_id=cedar.id,
                unit_id=None,
                month=month,
                items={
                    "Rent": Decimal("5200"),
                    "Property Tax": Decimal("1100"),
                    "Insurance": Decimal("400"),
                    "Property Management": Decimal("520"),
                    "Mortgage": Decimal("3100"),
                    "Owner Distribution": Decimal("1000"),
                },
            )

        # --- period_status: post everything; lock one month to demo admin unlock ---
        for prop in (maple, birch, cedar):
            for month in MONTHS:
                status = "locked" if (prop.id == maple.id and month == MONTHS[0]) else "posted"
                db.add(PeriodStatus(property_id=prop.id, month=month, status=status))

        db.commit()
        print("Done. 3 properties, 3 units, "
              f"{len(CATEGORIES)} categories, {len(MONTHS)} months seeded.")
        print("One Maple Court month (2026-01) is LOCKED to demo admin unlock.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
