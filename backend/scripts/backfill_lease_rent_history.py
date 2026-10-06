"""Idempotent backfill for lease.last_rent_increase_date (migration 0024) and
lease.opening_arrears (migration 0025) on the CURRENTLY RUNNING dev DB.

Why this exists (instead of `python -m app.seed`): the full seed WIPES and reseeds the whole
database, destroying validated demo data other phases planted. This script only writes the two
new columns where they are currently NULL, using the exact same deterministic rules as the
fresh-install path (`app.seed._last_rent_increase` / `_opening_arrears`), so a live DB and a
fresh install can't drift apart.

What it plants, and why each one:

  ``last_rent_increase_date``  on PERIODIC (rolling) tenancies only — the leases with no
                               ``end_date``, which is what a periodic tenancy IS: an
                               England-style AST that has run past its fixed term and now rolls
                               month to month, its rent rising in discrete steps on a stated
                               date rather than by a contractual percentage. Set to the
                               tenancy's first anniversary, which lands inside the fixed
                               2024-2025 actuals window, so ``months_since_last_increase`` (the
                               overdue-rent-review signal) reads against months that have real
                               data behind them.

  ``opening_arrears``          on a sparse ~1% of tenancies, at one month's contract rent — a
                               balance brought forward from before these records begin. This is
                               the one arrears input that CANNOT be derived, so without it the
                               accumulated-balance column only ever shows debt that arose inside
                               the window, and the two independent sources of a balance never
                               appear together in the demo data.

``rent_before_increase`` is deliberately NOT backfilled, for the same reason ``escalation_pct``
isn't fabricated anywhere (see ``_DELINQUENT_UNITS`` in app/seed.py): this dataset's actual rent
series is intentionally FLAT, so planting a prior rent below the current one would price the
pre-increase months below what was actually collected and manufacture a portfolio-wide credit
balance — a seed-data artifact rather than a story. Recording the DATE is honest on its own, and
an analyst who enters a real prior figure on any lease gets exact pre-increase pricing
immediately.

Reference-only data, same invariant as ``contract_rent``/``concession_monthly`` before it: never
feeds NOI/cash-flow. Arrears itself is DERIVED from the rent schedule against the actual
collected rent (``unit_month_summary.gross_rent``, untouched here), so this script changes a
reported balance and nothing in the P&L.

Safe to re-run: a lease with either column already set is left untouched (skip-if-not-null), so
a second run should report 0 newly-backfilled leases.

Run (from backend/):
    PYTHONPATH=.pydeps:. python3 scripts/backfill_lease_rent_history.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.seed import _last_rent_increase, _opening_arrears  # noqa: E402


def main() -> None:
    db = SessionLocal()
    try:
        # Same stable (property name, unit_number) ordering every other backfill script in this
        # repo uses — not the seed's internal creation-order counter, but the same deterministic
        # RULE applied consistently.
        rows = (
            db.execute(
                text(
                    """
                    SELECT l.id::text AS lease_id, l.start_date, l.end_date, l.contract_rent,
                           l.last_rent_increase_date, l.opening_arrears, u.is_shell
                    FROM lease l
                    JOIN units u ON u.id = l.unit_id
                    JOIN properties p ON p.id = u.property_id
                    ORDER BY p.name, u.unit_number
                    """
                )
            )
            .mappings()
            .all()
        )

        increases = openings = skipped_set = skipped_shell = 0
        for i, row in enumerate(rows):
            if row["is_shell"]:
                # Cedar Plaza Retail's shell-unit percentage-rent lease: it has no unit-month
                # records of its own (its rent books at the property tier), so it has no
                # arrears ledger and a rent-increase date on it would never be priced.
                skipped_shell += 1
                continue

            updates: dict = {}
            if row["last_rent_increase_date"] is None:
                increase = _last_rent_increase(i, row["start_date"], row["end_date"])
                if increase is not None:
                    updates["last_rent_increase_date"] = increase
            if row["opening_arrears"] is None:
                opening = _opening_arrears(i, row["contract_rent"])
                if opening is not None:
                    updates["opening_arrears"] = opening

            if row["last_rent_increase_date"] is not None or row["opening_arrears"] is not None:
                skipped_set += 1
            if not updates:
                continue

            assignments = ", ".join(f"{col} = :{col}" for col in updates)
            db.execute(
                text(f"UPDATE lease SET {assignments} WHERE id = :id"),
                {**updates, "id": row["lease_id"]},
            )
            increases += "last_rent_increase_date" in updates
            openings += "opening_arrears" in updates

        db.commit()
        print(
            f"Backfilled last_rent_increase_date on {increases} periodic tenancy(ies) and "
            f"opening_arrears on {openings} lease(s); {skipped_set} already had one of the two "
            f"on file, {skipped_shell} shell-unit lease(s) skipped."
        )
        counts = db.execute(
            text(
                "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE end_date IS NULL) AS periodic, "
                "count(*) FILTER (WHERE last_rent_increase_date IS NOT NULL) AS with_increase, "
                "count(*) FILTER (WHERE opening_arrears IS NOT NULL) AS with_opening "
                "FROM lease"
            )
        ).mappings().first()
        print(
            f"lease table now: {counts['total']} total, {counts['periodic']} periodic (no end "
            f"date), {counts['with_increase']} with a rent-increase date, "
            f"{counts['with_opening']} with an opening arrears balance."
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
