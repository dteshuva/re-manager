"""Idempotent lease seeder for the CURRENTLY RUNNING dev DB.

Why this exists (instead of just running `python -m app.seed`): the full seed WIPES and
reseeds the whole database — properties, units, records, categories, everything — which
would destroy demo data other phases planted and validated live (budgets, tags, investment
inputs, etc.). Rent roll / lease-level data is a NEW parallel table (`lease`), so it can be
populated in place with a targeted insert instead: this script adds exactly one lease per
unit that doesn't already have one, using the SAME deterministic plan logic as the
fresh-install path in app/seed.py (see `_lease_plan` / `_forced_status` there — kept in one
place so the two paths can't drift apart).

Safe to re-run: a unit with ANY existing lease row is left completely untouched, so this
only fills gaps (e.g. after `alembic upgrade head` adds the table to an existing DB, or if a
unit was added after the first pass). It never deletes or modifies an existing lease.

Run (from backend/):
    PYTHONPATH=.pydeps:. python3 scripts/seed_leases.py
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

# `app` lives at backend/app; make sure backend/ is importable regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Lease  # noqa: E402
from app.seed import MULTIFAMILY_CONFIGS, _forced_status, _lease_plan  # noqa: E402

_BASE_RENT_BY_PROPERTY = {name: rent for name, _address, _units, rent in MULTIFAMILY_CONFIGS}
_DEFAULT_BASE_RENT = Decimal("1500")  # fallback for any property not in MULTIFAMILY_CONFIGS


def main() -> None:
    db = SessionLocal()
    try:
        # Deterministic global order (property name, then unit number) so `i` — and
        # therefore _forced_status's sparse vacant/notice/expired picks, including the
        # Maple Court unit 105 tie-in — lines up with app/seed.py's fresh-install path.
        # The per-unit "most recent recorded gross rent" (if any) is read back here so
        # contract_rent tracks what a unit actually rents for, not just a property average.
        units = (
            db.execute(
                text(
                    """
                    SELECT u.id::text AS unit_id, u.unit_number, p.name AS property_name,
                           (SELECT ums.gross_rent FROM unit_month_summary ums
                            WHERE ums.unit_id = u.id ORDER BY ums.month DESC LIMIT 1) AS actual_rent,
                           (SELECT count(*) FROM lease l WHERE l.unit_id = u.id) AS existing_leases
                    FROM units u
                    JOIN properties p ON p.id = u.property_id
                    ORDER BY p.name, u.unit_number
                    """
                )
            )
            .mappings()
            .all()
        )

        if not units:
            print("No units found — nothing to seed (is the DB up and migrated?).")
            return

        created = 0
        skipped = 0
        for i, row in enumerate(units):
            if row["existing_leases"] > 0:
                skipped += 1
                continue
            base_rent = _BASE_RENT_BY_PROPERTY.get(row["property_name"], _DEFAULT_BASE_RENT)
            forced = _forced_status(row["property_name"], row["unit_number"], i)
            plan = _lease_plan(
                i, base_rent, row["actual_rent"], force_status=forced,
                property_name=row["property_name"], unit_number=row["unit_number"],
            )
            db.add(Lease(unit_id=row["unit_id"], **plan))
            created += 1

        db.commit()
        print(
            f"Seeded {created} lease(s) across {len(units)} unit(s) "
            f"({skipped} unit(s) already had a lease and were left untouched)."
        )
        status_counts = db.execute(
            text("SELECT status, count(*) FROM lease GROUP BY status ORDER BY status")
        ).all()
        print("Lease status breakdown:", ", ".join(f"{s}={n}" for s, n in status_counts))
    finally:
        db.close()


if __name__ == "__main__":
    main()
