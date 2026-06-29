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

import uuid
from datetime import date, timedelta
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

# Generate 24 months of data
MONTHS = []
for i in range(24):
    year = 2024 + (i // 12)
    month = (i % 12) + 1
    MONTHS.append(date(year, month, 1))

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
    """Create a monthly_record and its line items. `items` = {category_name: amount}.

    Generates the record id client-side (instead of relying on the DB's
    gen_random_uuid() server_default) so we can add the line items without an
    intermediate flush — seeding thousands of records via per-row flush() was
    taking minutes due to one network round-trip per record.
    """
    record = MonthlyRecord(
        id=str(uuid.uuid4()), property_id=property_id, unit_id=unit_id, month=month, notes=notes
    )
    db.add(record)
    for name, amount in items.items():
        db.add(
            LineItem(
                id=str(uuid.uuid4()),
                monthly_record_id=record.id,
                category_id=CAT[name].id,
                amount=Decimal(str(amount)),
            )
        )


CAT: dict[str, Category] = {}


def _create_multifamily_property(db, name, address, num_units, base_rent):
    """Create a multifamily property with num_units and financial data.

    Flushes once per property (not per record): with thousands of unflushed
    objects in one session, SQLAlchemy's pending-object bookkeeping degrades
    and each later db.add() gets slower, so seeding all 15 properties in a
    single unflushed batch took the better part of a minute.
    """
    prop = Property(name=name, type="multifamily", address=address)
    db.add(prop)
    db.flush()
    units = []
    for i in range(num_units):
        unit_num = str(100 + i)
        u = Unit(property_id=prop.id, unit_number=unit_num, label=f"Unit {unit_num}")
        db.add(u)
        units.append(u)
    db.flush()

    for idx, month in enumerate(MONTHS):
        for u in units:
            _add_record(
                db,
                property_id=prop.id,
                unit_id=u.id,
                month=month,
                items={
                    "Rent": base_rent,
                    "Repairs & Maintenance": Decimal("80"),
                    "Utilities": Decimal("45"),
                },
            )
        # property-tier-only items
        tier_items = {
            "Property Tax": Decimal(num_units) * Decimal("35"),
            "Insurance": Decimal(num_units) * Decimal("12"),
            "Property Management": Decimal(num_units) * Decimal("15"),
            "Mortgage": Decimal(num_units) * Decimal("55"),
        }
        if idx == 2:  # one-time capex in March (month 3)
            tier_items["Roof Replacement"] = Decimal(num_units) * Decimal("200")
        _add_record(
            db,
            property_id=prop.id,
            unit_id=None,
            month=month,
            items=tier_items,
            notes="Property-tier shared items",
        )
    db.flush()

    return prop


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

        # --- 15 Multifamily Properties with 40-100 units ---
        multifamily_configs = [
            ("Maple Court Apartments", "120 Maple Ct", 45, Decimal("1500")),
            ("Oak Ridge Residences", "505 Oak Ridge Dr", 62, Decimal("1650")),
            ("Pine Valley Tower", "777 Pine Valley Ln", 58, Decimal("1550")),
            ("Cedar Commons", "200 Cedar Ave", 75, Decimal("1700")),
            ("Elm Park Gardens", "333 Elm Park Rd", 48, Decimal("1450")),
            ("Spruce Hill Apartments", "899 Spruce Hill Way", 82, Decimal("1800")),
            ("Birch Lane Residential", "456 Birch Ln", 50, Decimal("1550")),
            ("Willow Creek Towers", "1010 Willow Creek Blvd", 95, Decimal("1900")),
            ("Ash Grove Apartments", "222 Ash Grove St", 40, Decimal("1400")),
            ("Hickory Heights", "678 Hickory Heights Ave", 70, Decimal("1750")),
            ("Sycamore Square Lofts", "999 Sycamore Sq", 55, Decimal("1600")),
            ("Magnolia Park Residences", "444 Magnolia Park Dr", 85, Decimal("1850")),
            ("Walnut Hill Towers", "567 Walnut Hill St", 72, Decimal("1775")),
            ("Chestnut Ridge Apartments", "191 Chestnut Ridge Rd", 65, Decimal("1700")),
            ("Dogwood Plaza Apartments", "812 Dogwood Plaza Way", 100, Decimal("1950")),
        ]

        properties = []
        for name, address, num_units, base_rent in multifamily_configs:
            prop = _create_multifamily_property(db, name, address, num_units, base_rent)
            properties.append(prop)

        # --- single-asset properties (for variety) ---
        birch = Property(name="Birch Street House", type="single", address="45 Birch St")
        db.add(birch)
        db.flush()
        for idx, month in enumerate(MONTHS):
            items = {
                "Rent": Decimal("2400"),
                "Property Tax": Decimal("350"),
                "Insurance": Decimal("110"),
                "Repairs & Maintenance": Decimal("90"),
                "Mortgage": Decimal("1500"),
            }
            if idx == 6:  # capex in July
                items["Roof Replacement"] = Decimal("3000")
            _add_record(db, property_id=birch.id, unit_id=None, month=month, items=items)

        cedar = Property(name="Cedar Plaza Retail", type="single", address="900 Cedar Ave")
        db.add(cedar)
        db.flush()
        for idx, month in enumerate(MONTHS):
            items = {
                "Rent": Decimal("5200"),
                "Property Tax": Decimal("1100"),
                "Insurance": Decimal("400"),
                "Property Management": Decimal("520"),
                "Mortgage": Decimal("3100"),
                "Owner Distribution": Decimal("1000"),
            }
            if idx == 10:  # capex in November
                items["Roof Replacement"] = Decimal("5000")
            _add_record(db, property_id=cedar.id, unit_id=None, month=month, items=items)

        # --- period_status: post everything ---
        all_props = properties + [birch, cedar]
        for prop in all_props:
            for month in MONTHS:
                status = "posted"
                db.add(PeriodStatus(property_id=prop.id, month=month, status=status))

        db.commit()
        total_units = sum(config[2] for config in multifamily_configs)
        print("Done. 15 multifamily + 2 single-asset properties seeded.")
        print(f"Total multifamily units: {total_units}")
        print(f"Categories: {len(CATEGORIES)}, Months: {len(MONTHS)} (Jan 2024 - Dec 2025)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
