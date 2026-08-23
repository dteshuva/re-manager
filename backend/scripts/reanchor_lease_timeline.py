"""Idempotent lease-timeline re-anchor for the CURRENTLY RUNNING dev DB (rent-waterfall
rework item 1 — root cause, flagged three times before this pass finally fixed it).

Why this exists: `_lease_plan`'s dates used to be entirely `date.today()`-relative
(`end_date = end_date_prev - 365 days`, or `today - (180+i%400) days` for the start of a
month-to-month lease) — every branch's `start_date` was pinned to within ~1-1.5 years of
"today". That drifts away from the FIXED 2024-01..2025-12 actuals window a little more every
day this demo environment keeps running: by the time this rework landed, "today" had drifted
to mid-2026 and only ~292 of 1,002 real units still had a lease in force as of Dec 2025 (the
actuals window's LAST month) — the rent-variance and rent-waterfall features were computing
over a mostly lease-less window, with vacancy never populating because the one planted
vacancy (Maple Court unit 105) had also drifted: its "vacant" lease's `end_date` (also
today-relative) had pushed past Dec 2025 into 2026, so the lease still looked "in force" for
the whole 2024-2025 window despite the unit having zero actual rent on file from June 2025 on.

Fix (see `app/seed.py`'s `_lease_coverage_start`/`VACANCY_LEASE_END` for the shared logic
this script re-derives): every real (non-shell) unit's start_date now anchors to a fixed,
deterministic PRE-window date (~Jan 2022 - Dec 2023, varied by unit so escalation
anniversaries don't all land on the same calendar month) instead of a today-relative one —
`end_date` is UNCHANGED for every unit except Maple Court 105, whose `end_date` is corrected
to the actual last-paid month on file (2025-05-01) so the anomaly's intentional vacancy falls
back inside the actuals window where it belongs. This is a pure `start_date` (+ that one
`end_date`) backdate — tenant_name, contract_rent, status, and v2 terms are untouched (their
formulas didn't change).

Safe to re-run: recomputes each unit's plan from the SAME deterministic (property name,
unit_number) index used by scripts/seed_leases.py / scripts/fix_lease_stagger.py, and only
writes start_date/end_date where they differ from what's on file.

Run (from backend/):
    PYTHONPATH=.pydeps:. python3 scripts/reanchor_lease_timeline.py
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Lease  # noqa: E402
from app.seed import MULTIFAMILY_CONFIGS, VACANCY_PROP, VACANCY_UNIT, _forced_status, _lease_plan  # noqa: E402

_BASE_RENT_BY_PROPERTY = {name: rent for name, _address, _units, rent in MULTIFAMILY_CONFIGS}
_DEFAULT_BASE_RENT = Decimal("1500")


def main() -> None:
    db = SessionLocal()
    try:
        # Same deterministic global order (property name, then unit_number) as
        # scripts/seed_leases.py, so `_forced_status`'s sparse picks (incl. Maple Court
        # unit 105) line up identically. Real (non-shell) units only — Cedar Plaza Retail's
        # shell unit carries its own separately-seeded percentage-rent lease, untouched.
        units = (
            db.execute(
                text(
                    """
                    SELECT u.id::text AS unit_id, u.unit_number, p.name AS property_name,
                           (SELECT ums.gross_rent FROM unit_month_summary ums
                            WHERE ums.unit_id = u.id ORDER BY ums.month DESC LIMIT 1) AS actual_rent,
                           l.id::text AS lease_id, l.start_date AS cur_start_date,
                           l.end_date AS cur_end_date, l.status AS cur_status
                    FROM units u
                    JOIN properties p ON p.id = u.property_id
                    JOIN lease l ON l.unit_id = u.id
                    WHERE NOT u.is_shell
                    ORDER BY p.name, u.unit_number
                    """
                )
            )
            .mappings()
            .all()
        )

        if not units:
            print("No leases found — nothing to re-anchor (is the DB up and seeded?).")
            return

        updated = 0
        unchanged = 0
        for i, row in enumerate(units):
            forced = _forced_status(row["property_name"], row["unit_number"], i)
            base_rent = _BASE_RENT_BY_PROPERTY.get(row["property_name"], _DEFAULT_BASE_RENT)
            plan = _lease_plan(
                i, base_rent, row["actual_rent"], force_status=forced,
                property_name=row["property_name"], unit_number=row["unit_number"],
            )
            if plan["start_date"] != row["cur_start_date"] or plan["end_date"] != row["cur_end_date"]:
                lease = db.get(Lease, row["lease_id"])
                lease.start_date = plan["start_date"]
                lease.end_date = plan["end_date"]
                updated += 1
            else:
                unchanged += 1

        db.commit()
        print(
            f"Re-anchored {updated} lease(s) start_date/end_date; {unchanged} already matched "
            f"the corrected formula."
        )

        coverage = db.execute(
            text(
                """
                SELECT count(*) FROM lease l
                JOIN units u ON u.id = l.unit_id
                WHERE NOT u.is_shell
                  AND l.start_date <= '2025-12-01' AND (l.end_date IS NULL OR l.end_date >= '2025-12-01')
                """
            )
        ).scalar()
        total = db.execute(text("SELECT count(*) FROM units WHERE NOT is_shell")).scalar()
        print(f"Units with a lease in force as of 2025-12-01: {coverage}/{total}")

        maple = db.execute(
            text(
                """
                SELECT l.start_date, l.end_date, l.status FROM lease l
                JOIN units u ON u.id = l.unit_id JOIN properties p ON p.id = u.property_id
                WHERE p.name = :prop AND u.unit_number = :unit
                """
            ),
            {"prop": VACANCY_PROP, "unit": VACANCY_UNIT},
        ).fetchone()
        print(f"{VACANCY_PROP} unit {VACANCY_UNIT} lease (intentional vacancy, preserved): {maple}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
