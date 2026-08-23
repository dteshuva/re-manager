"""Idempotent backfill for units.market_rent (migration 0015) on the CURRENTLY RUNNING dev
DB — the rent waterfall's GPR line (see app/queries.py's `_rent_waterfall_*`).

Why this exists (instead of `python -m app.seed`): the full seed WIPES and reseeds the
whole database, destroying validated demo data other phases planted. This script only sets
`units.market_rent` where it's currently NULL (never touches a unit that already has one),
using the exact same deterministic formula as the fresh-install path
(`app.seed._market_rent`), so the live DB and a fresh install can't drift apart.

Reference figure for each unit: its CURRENT lease's `contract_rent` (the same
"covers-today, else most-recently-started" resolution `app/queries.py`'s
`_CURRENT_LEASE_CTE` uses) — market_rent is set to a modest 2%-8% premium over that
in-place rent (deterministic, varied by a stable (property name, unit_number)-ordered
index), rounded to the nearest $5, so loss-to-lease is non-trivial and demonstrable rather
than uniform across the portfolio. A unit with NO lease on file at all falls back to its
property's average in-place rent among units that DO have one (the same proxy
`_avg_contract_rent_by_property` in app/queries.py already uses for a never-leased vacant
unit's GPR contribution), or the portfolio average if the property itself has none.

Shell/synthetic units (`is_shell`, migration 0014 — e.g. Cedar Plaza Retail's placeholder)
are skipped entirely: they're excluded from the rent waterfall (and every other
occupancy/GPR aggregate) exactly like the rent roll excludes them, so a market_rent figure
for one would never be used and would only be misleading if inspected directly.

`market_rent` is reference-only data, same invariant as `contract_rent`/`is_shell`: it
never feeds NOI/cash-flow, which stays driven solely by monthly_records/line_items.

Safe to re-run: any unit with `market_rent` already set is left completely untouched
(skip-if-set) — re-running should always report 0 newly-backfilled units.

Run (from backend/):
    PYTHONPATH=.pydeps:. python3 scripts/backfill_unit_market_rent.py
"""

from __future__ import annotations

import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.seed import _market_rent  # noqa: E402


def main() -> None:
    db = SessionLocal()
    try:
        # In-place rent per unit = its CURRENT lease's contract_rent (same DISTINCT ON
        # resolution as app/queries.py's `_CURRENT_LEASE_CTE`), read in the same
        # deterministic (property name, unit_number) order every other seed/backfill
        # script uses, so the index-based premium factor lines up with a fresh install.
        rows = (
            db.execute(
                text(
                    """
                    WITH current_lease AS (
                        SELECT DISTINCT ON (l.unit_id)
                            l.unit_id, l.contract_rent
                        FROM lease l
                        ORDER BY l.unit_id,
                            (l.status IN ('active', 'notice')
                                AND l.start_date <= CURRENT_DATE
                                AND (l.end_date IS NULL OR l.end_date >= CURRENT_DATE)) DESC,
                            l.start_date DESC
                    )
                    SELECT u.id::text AS unit_id, u.property_id::text AS property_id,
                           u.is_shell, u.market_rent, cl.contract_rent AS in_place_rent
                    FROM units u
                    JOIN properties p ON p.id = u.property_id
                    LEFT JOIN current_lease cl ON cl.unit_id = u.id
                    ORDER BY p.name, u.unit_number
                    """
                )
            )
            .mappings()
            .all()
        )

        # Property-average in-place rent (units WITH a current lease only) — the fallback
        # reference for a unit with no lease on file at all.
        by_property_sum: dict[str, Decimal] = {}
        by_property_n: dict[str, int] = {}
        for r in rows:
            if r["in_place_rent"] is not None and not r["is_shell"]:
                by_property_sum[r["property_id"]] = (
                    by_property_sum.get(r["property_id"], Decimal("0")) + r["in_place_rent"]
                )
                by_property_n[r["property_id"]] = by_property_n.get(r["property_id"], 0) + 1
        by_property_avg = {pid: by_property_sum[pid] / by_property_n[pid] for pid in by_property_sum}
        portfolio_avg = (
            sum(by_property_sum.values()) / sum(by_property_n.values()) if by_property_n else None
        )

        updated = skipped_set = skipped_shell = skipped_no_ref = 0
        for i, r in enumerate(rows):
            if r["is_shell"]:
                skipped_shell += 1
                continue
            if r["market_rent"] is not None:
                skipped_set += 1
                continue
            ref = r["in_place_rent"]
            if ref is None:
                ref = by_property_avg.get(r["property_id"], portfolio_avg)
            if ref is None:
                skipped_no_ref += 1
                continue
            mr = _market_rent(i, Decimal(ref))
            db.execute(
                text("UPDATE units SET market_rent = :mr WHERE id = :id"),
                {"mr": mr, "id": r["unit_id"]},
            )
            updated += 1

        db.commit()
        print(
            f"Backfilled market_rent on {updated} unit(s); {skipped_set} already had one, "
            f"{skipped_shell} shell unit(s) skipped (excluded from the waterfall), "
            f"{skipped_no_ref} unit(s) skipped (no in-place-rent reference available at all)."
        )
        counts = db.execute(
            text(
                "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE market_rent IS NOT NULL) AS with_rent, "
                "count(*) FILTER (WHERE is_shell) AS shell FROM units"
            )
        ).mappings().first()
        print(
            f"units table now: {counts['total']} total, {counts['with_rent']} with "
            f"market_rent, {counts['shell']} shell (intentionally excluded)."
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
