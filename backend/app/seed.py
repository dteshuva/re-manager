"""Seed sample data at realistic scale, with a few HAND-COMPUTED anomalies planted as a
ground-truth answer key for the attention feed (INSIGHT_DASHBOARD_SPEC sub-step 3).

Run after migrations:  PYTHONPATH=.pydeps python3 -m app.seed

Scale: 15 multifamily (40-100 units) + 2 single-asset, 24 months (Jan 2024 - Dec 2025),
~1,000 units. Operating expenses (Repairs & Maintenance, Utilities) carry a small,
deterministic seasonal wiggle (±12%) so charts aren't ruler-flat — kept far below every
attention threshold so it trips nothing. Rent is flat (contractual) so vacancy / NOI math
stays exactly hand-computable.

Planted anomalies, all in **June 2025** (prior month May 2025), each sized to trip exactly
one detector and nothing else (see ANSWER KEY printed at the end):
  1. NOI cliff      — Oak Ridge Residences: June rent/unit 1650 -> 800 (concession).
                      Rent -52,700 => NOI drop ~ -52,235 (-58%). No expense/occupancy change.
  2. Expense spike  — Cedar Commons: +$30,000 property-tier "Repairs & Maintenance" in June.
                      Category 6,000 -> 36,000 vs ~6,568 T3M avg (+~448%). NOI dip ~29,437
                      stays below the NOI-drop $ floor, so it shows ONLY as an expense spike.
  3. Vacancy        — Maple Court Apartments unit 105 vacant from June (no record).
                      Occupancy 45/45 -> 44/45; lost rent $1,500. NOI dip tiny (below floor).
  4. Missing data   — Ash Grove Apartments: no June 2025 records at all.

Re-running wipes domain data and reseeds, then rebuilds the summary layer.
"""

from __future__ import annotations

import math
import uuid
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
from app.summaries import rebuild_all

# 24 months: Jan 2024 .. Dec 2025
MONTHS = [date(2024 + i // 12, i % 12 + 1, 1) for i in range(24)]
ANCHOR_IDX = MONTHS.index(date(2025, 6, 1))  # planted-anomaly month (= 17)

# --- planted-anomaly knobs (the answer key) ---------------------------------
NOI_CLIFF_PROP = "Oak Ridge Residences"
NOI_CLIFF_JUNE_RENT = Decimal("800")  # normal 1650
SPIKE_PROP = "Cedar Commons"
SPIKE_CATEGORY = "Repairs & Maintenance"
SPIKE_AMOUNT = Decimal("30000")  # extra property-tier repair in June
VACANCY_PROP = "Maple Court Apartments"
VACANCY_UNIT = "105"
MISSING_PROP = "Ash Grove Apartments"

# Categories seasonality applies to (small, bounded, below all thresholds).
SEASONAL_CATS = {"Repairs & Maintenance", "Utilities"}

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

CAT: dict[str, Category] = {}


def _season(idx: int) -> Decimal:
    """Deterministic ±12% seasonal factor by calendar month (peak Mar, trough Sep)."""
    m = idx % 12 + 1
    return Decimal(str(round(1 + 0.12 * math.sin(2 * math.pi * m / 12), 6)))


def _q(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"))


def _wipe(db) -> None:
    # property_*_summary cascade from properties; portfolio_month_summary is rebuilt later.
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
    """Create a monthly_record and its line items. ``items`` = {category_name: amount}.
    Ids generated client-side to avoid a flush per record."""
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


def _create_multifamily_property(db, name, address, num_units, base_rent):
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
        # Anomaly 4: this property simply has no records for the anchor month.
        if name == MISSING_PROP and idx == ANCHOR_IDX:
            continue

        f = _season(idx)
        for u in units:
            # Anomaly 3: unit goes vacant from the anchor month on (no record at all).
            if name == VACANCY_PROP and u.unit_number == VACANCY_UNIT and idx >= ANCHOR_IDX:
                continue
            # Anomaly 1: rent concession at the anchor month only.
            rent = (
                NOI_CLIFF_JUNE_RENT
                if (name == NOI_CLIFF_PROP and idx == ANCHOR_IDX)
                else base_rent
            )
            _add_record(
                db,
                property_id=prop.id,
                unit_id=u.id,
                month=month,
                items={
                    "Rent": rent,
                    "Repairs & Maintenance": _q(Decimal("80") * f),
                    "Utilities": _q(Decimal("45") * f),
                },
            )

        # property-tier-only items (flat; not seasonal)
        tier_items = {
            "Property Tax": Decimal(num_units) * Decimal("35"),
            "Insurance": Decimal(num_units) * Decimal("12"),
            "Property Management": Decimal(num_units) * Decimal("15"),
            "Mortgage": Decimal(num_units) * Decimal("55"),
        }
        if idx % 12 == 2:  # one-time capex each March (below NOI; trips no detector)
            tier_items["Roof Replacement"] = Decimal(num_units) * Decimal("200")
        # Anomaly 2: a single operating category spikes at the property tier in June.
        if name == SPIKE_PROP and idx == ANCHOR_IDX:
            tier_items[SPIKE_CATEGORY] = tier_items.get(SPIKE_CATEGORY, Decimal("0")) + SPIKE_AMOUNT
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

        for name, classification in CATEGORIES.items():
            cat = Category(name=name, default_classification=classification)
            db.add(cat)
            CAT[name] = cat
        db.flush()

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
            properties.append(_create_multifamily_property(db, name, address, num_units, base_rent))

        # --- single-asset properties (no units; no planted anomalies) ---
        birch = Property(name="Birch Street House", type="single", address="45 Birch St")
        db.add(birch)
        db.flush()
        for idx, month in enumerate(MONTHS):
            f = _season(idx)
            items = {
                "Rent": Decimal("2400"),
                "Property Tax": Decimal("350"),
                "Insurance": Decimal("110"),
                "Repairs & Maintenance": _q(Decimal("90") * f),
                "Mortgage": Decimal("1500"),
            }
            if idx % 12 == 6:  # capex in July
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
            if idx % 12 == 10:  # capex in November
                items["Roof Replacement"] = Decimal("5000")
            _add_record(db, property_id=cedar.id, unit_id=None, month=month, items=items)

        # --- period_status: post every (property, month) that has records ---
        for prop in properties:
            for idx, month in enumerate(MONTHS):
                if prop.name == MISSING_PROP and idx == ANCHOR_IDX:
                    continue
                db.add(PeriodStatus(property_id=prop.id, month=month, status="posted"))
        for prop in (birch, cedar):
            for month in MONTHS:
                db.add(PeriodStatus(property_id=prop.id, month=month, status="posted"))

        db.commit()

        print("Rebuilding summary layer from seeded line items...")
        rebuild_all(db)

        total_units = sum(c[2] for c in multifamily_configs)
        print(f"Done. 15 multifamily ({total_units} units) + 2 single-asset, "
              f"{len(MONTHS)} months (Jan 2024 - Dec 2025).")
        print("\n  ANSWER KEY — attention feed at 2025-06 (relative thresholds + linking):")
        print(f"    NOI drop      : {NOI_CLIFF_PROP} (~-$52,235, -58%; rent-driven)")
        print(f"    Expense spike : {SPIKE_PROP} / {SPIKE_CATEGORY} (~+$29,432, +448%) —")
        print(f"                    its ~-26% NOI drop reconciles (~1:1) and MERGES into this item")
        print(f"    Vacancy       : {VACANCY_PROP} unit {VACANCY_UNIT} (occupancy -2.2pp, -$1,500 rent)")
        print(f"    Missing data  : {MISSING_PROP} (no June 2025 records)")
        print("    ...exactly 4 items, nothing else.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
