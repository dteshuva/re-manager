"""One-off, idempotent fix for the lease end_date seed-clustering bug (analyst report fix
#7): `_lease_plan`'s `bucket == 0` branch (near-term, "within 30 days" leases) computed its
day offset as `10 + (i % 20)` — but `bucket = i % 20`, and `bucket == 0` is exactly the
condition `i % 20 == 0`, so that offset was ALWAYS 0 for every qualifying unit. Every
"within 30 days" active lease collapsed onto the identical end_date (today + 10 days —
2026-07-23 on this DB), instead of being spread across the 10-30 day window as designed.

`app/seed.py` has been corrected to use `i % 19` (co-prime with the bucket modulus 20, so
it no longer collapses to a constant) for that branch. This script re-derives EVERY unit's
lease plan with the FIXED code, in the same deterministic (property name, unit_number)
order used by scripts/seed_leases.py, and updates `end_date`/`start_date` ONLY where they
differ from what's on file. Every other bucket's formula is unchanged, so this is a no-op
for every unit except the ~47 that hit the collision — safe to re-run any number of times.

Untouched by design: `tenant_name` and `contract_rent` (formulas didn't change), and any
lease with a forced status (vacant/notice/expired — including Maple Court unit 105, the
planted vacancy story), since `_forced_status` short-circuits before the buggy branch is
ever reached.

Run (from backend/):
    PYTHONPATH=.pydeps:. python3 scripts/fix_lease_stagger.py
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Lease  # noqa: E402
from app.seed import MULTIFAMILY_CONFIGS, _forced_status, _lease_plan  # noqa: E402

_BASE_RENT_BY_PROPERTY = {name: rent for name, _address, _units, rent in MULTIFAMILY_CONFIGS}
_DEFAULT_BASE_RENT = Decimal("1500")


def main() -> None:
    db = SessionLocal()
    try:
        # Same deterministic global order (and therefore the same `i` per unit) as
        # scripts/seed_leases.py, so `_forced_status`'s sparse picks (incl. Maple Court
        # unit 105) line up identically.
        units = (
            db.execute(
                text(
                    """
                    SELECT u.id::text AS unit_id, u.unit_number, p.name AS property_name,
                           (SELECT ums.gross_rent FROM unit_month_summary ums
                            WHERE ums.unit_id = u.id ORDER BY ums.month DESC LIMIT 1) AS actual_rent,
                           l.id::text AS lease_id, l.end_date AS cur_end_date,
                           l.start_date AS cur_start_date, l.status AS cur_status
                    FROM units u
                    JOIN properties p ON p.id = u.property_id
                    JOIN lease l ON l.unit_id = u.id
                    ORDER BY p.name, u.unit_number
                    """
                )
            )
            .mappings()
            .all()
        )

        if not units:
            print("No leases found — nothing to fix (is the DB up and seeded?).")
            return

        updated = 0
        unchanged = 0
        skipped_forced = 0
        for i, row in enumerate(units):
            forced = _forced_status(row["property_name"], row["unit_number"], i)
            if forced is not None:
                skipped_forced += 1
                continue
            base_rent = _BASE_RENT_BY_PROPERTY.get(row["property_name"], _DEFAULT_BASE_RENT)
            plan = _lease_plan(
                i, base_rent, row["actual_rent"], force_status=None,
                property_name=row["property_name"], unit_number=row["unit_number"],
            )
            if plan["end_date"] != row["cur_end_date"] or plan["start_date"] != row["cur_start_date"]:
                lease = db.get(Lease, row["lease_id"])
                lease.end_date = plan["end_date"]
                lease.start_date = plan["start_date"]
                updated += 1
            else:
                unchanged += 1

        db.commit()
        print(
            f"Re-staggered {updated} lease end_date(s); {unchanged} already matched the "
            f"corrected formula; {skipped_forced} forced-status leases (vacant/notice/"
            f"expired) left untouched."
        )
        rows = db.execute(
            text(
                """
                SELECT end_date, count(*) c FROM lease
                WHERE end_date IS NOT NULL
                GROUP BY end_date ORDER BY c DESC LIMIT 5
                """
            )
        ).all()
        print("Top 5 most-common end_dates after the fix:")
        for end_date, c in rows:
            print(f"  {end_date}: {c}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
