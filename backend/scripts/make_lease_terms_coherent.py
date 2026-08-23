"""Idempotent lease-coherence fix for the CURRENTLY RUNNING dev DB (analyst report — the
rent waterfall's `bad_debt` line was almost entirely a seed-data artifact, not real
delinquency).

THE PROBLEM this fixes: lease terms were seeded independently of the actual collected rent
— `contract_rent` got a +/-3% wiggle off a reference rent, then a fabricated flat 3%/yr
`escalation_pct` on top. This seed's actual rent is intentionally FLAT across the whole
2024-2025 window (see app/seed.py's top docstring), so by late in that window the escalated
SCHEDULE had compounded well past the flat ACTUAL — e.g. Maple Court unit 100: contract
$1,470 @ 3%/yr -> scheduled ~$1,559 by Dec 2025, vs. actual $1,500 collected every month.
The rent waterfall computed `bad_debt = scheduled - actual = $59` and called it delinquency,
but the tenant was paying exactly what they always had (even ABOVE their nominal base
contract) — phantom shortfall, not a real one.

THE FIX (Option 2 — "don't fake it", see app/seed.py's `_DELINQUENT_UNITS`/
`_coherent_contract_rent` docstrings for the shared logic this script re-derives): for
every real (non-shell) unit,
  1. `contract_rent` is set to that unit's own actual in-place rent (the latest month it has
     an occupied — gross_rent > 0 — actual record on file), so scheduled ~= actual.
  2. `escalation_pct` is cleared to NULL — the fabricated 3%/yr is dropped everywhere. The
     column/feature stays: an analyst can still enter a REAL escalation on any lease; this
     script (like app/seed.py's fresh-install path) just stops planting a fake one.
  3. `units.market_rent` is reset to a realistic 3%-9% premium over the NEW `contract_rent`
     (`app.seed._market_rent`, same formula app/seed.py's fresh-install path uses), so
     `loss_to_lease` reads positive and material (in-place rent below market) instead of the
     old formula's near-zero/negative "gain to lease" once the fabricated escalation no
     longer prices leases above market.

A SMALL, EXPLICIT set of units (`app.seed._DELINQUENT_UNITS`, 5 units across 5 properties)
is the deliberate exception: their `contract_rent` is set ABOVE their own actual rent by a
real, stated percentage (6%-12%), so the waterfall's `bad_debt` line stays non-zero but is
now a genuine, explainable shortfall for exactly these units — see that dict for the full
list. Every lease's EXISTING `concession_monthly` (already seeded by
scripts/backfill_lease_concessions.py — a real, standing rent concession on ~2.5% of
leases) is read but NOT modified here; for those leases `contract_rent` is set to
`actual + concession_monthly` (not just `actual`) so the shortfall the concession creates is
exactly the concession, not stray bad debt.

Why this exists (instead of `python -m app.seed`): the full seed WIPES and reseeds the
whole database, destroying validated demo data other phases planted. This script only
UPDATEs existing `lease`/`units` rows in place, using the exact same deterministic formulas
as the fresh-install path (`app.seed._coherent_contract_rent` / `_market_rent`), so the live
DB and a fresh install can't drift apart.

NEVER touches `unit_month_summary` / `monthly_records` / any actual rent or line-item data
— those feed NOI, which stays byte-identical. Only `lease.contract_rent`,
`lease.escalation_pct`, and `units.market_rent` (all reference-only, never feed NOI/cash-
flow) are written, and only where they differ from the freshly-computed target — skip-if-
already-coherent, so re-running reports 0 changes.

Run (from backend/):
    PYTHONPATH=.pydeps:. python3 scripts/make_lease_terms_coherent.py
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Lease  # noqa: E402
from app.seed import _DELINQUENT_UNITS, _coherent_contract_rent, _market_rent  # noqa: E402


def main() -> None:
    db = SessionLocal()
    try:
        # One row per real (non-shell) unit, its CURRENT lease (same covers-today-else-most-
        # recently-started resolution as app/queries.py's `_CURRENT_LEASE_CTE` / the other
        # backfill scripts' `current_lease` CTE), and the latest month it has an OCCUPIED
        # (gross_rent > 0) actual on file — the "actual in-place rent" reference. Stable
        # (property name, unit_number) order so the index `i` used for `_market_rent`'s
        # premium factor lines up with app/seed.py's fresh-install path and every other
        # script in this repo.
        rows = (
            db.execute(
                text(
                    """
                    WITH current_lease AS (
                        SELECT DISTINCT ON (l.unit_id)
                            l.id, l.unit_id, l.contract_rent, l.escalation_pct,
                            l.concession_monthly
                        FROM lease l
                        ORDER BY l.unit_id,
                            (l.status IN ('active', 'notice')
                                AND l.start_date <= CURRENT_DATE
                                AND (l.end_date IS NULL OR l.end_date >= CURRENT_DATE)) DESC,
                            l.start_date DESC
                    ),
                    actual_ref AS (
                        SELECT DISTINCT ON (ums.unit_id) ums.unit_id, ums.gross_rent
                        FROM unit_month_summary ums
                        WHERE ums.gross_rent > 0
                        ORDER BY ums.unit_id, ums.month DESC
                    )
                    SELECT u.id::text AS unit_id, u.market_rent, p.name AS property_name,
                           u.unit_number, cl.id::text AS lease_id, cl.contract_rent,
                           cl.escalation_pct, cl.concession_monthly, ar.gross_rent AS actual_ref
                    FROM units u
                    JOIN properties p ON p.id = u.property_id
                    JOIN current_lease cl ON cl.unit_id = u.id
                    LEFT JOIN actual_ref ar ON ar.unit_id = u.id
                    WHERE NOT u.is_shell
                    ORDER BY p.name, u.unit_number
                    """
                )
            )
            .mappings()
            .all()
        )

        if not rows:
            print("No real (non-shell) units with a lease found — nothing to do.")
            return

        lease_updated = market_rent_updated = already_coherent = no_actual_ref = 0
        delinquent_report: list[str] = []
        for i, row in enumerate(rows):
            if row["actual_ref"] is None:
                # Defensive: every real unit in this seed has at least one occupied actual
                # month on file. If one genuinely doesn't (e.g. a brand-new unit with no
                # records yet), there's no actual reference to derive coherence from —
                # leave its lease/market_rent untouched rather than guess.
                no_actual_ref += 1
                continue

            actual_ref = Decimal(row["actual_ref"])
            target_contract = _coherent_contract_rent(
                actual_ref, row["property_name"], row["unit_number"], row["concession_monthly"]
            )
            target_market_rent = _market_rent(i, target_contract)

            lease_changed = (
                row["contract_rent"] != target_contract or row["escalation_pct"] is not None
            )
            market_rent_changed = row["market_rent"] != target_market_rent

            if not lease_changed and not market_rent_changed:
                already_coherent += 1
                continue

            if lease_changed:
                lease = db.get(Lease, row["lease_id"])
                lease.contract_rent = target_contract
                lease.escalation_pct = None
                lease_updated += 1
            if market_rent_changed:
                db.execute(
                    text("UPDATE units SET market_rent = :mr WHERE id = :id"),
                    {"mr": target_market_rent, "id": row["unit_id"]},
                )
                market_rent_updated += 1

            if (row["property_name"], row["unit_number"]) in _DELINQUENT_UNITS:
                markup = _DELINQUENT_UNITS[(row["property_name"], row["unit_number"])]
                shortfall = target_contract - actual_ref
                delinquent_report.append(
                    f"    {row['property_name']} unit {row['unit_number']}: actual ${actual_ref} "
                    f"-> contract ${target_contract} (+{markup * 100}%, ${shortfall} shortfall/mo)"
                )

        db.commit()
        print(
            f"Processed {len(rows)} real unit(s): {lease_updated} lease(s) updated "
            f"(contract_rent/escalation_pct), {market_rent_updated} unit(s) market_rent "
            f"updated, {already_coherent} already coherent (no change), {no_actual_ref} "
            f"skipped (no actual rent on file)."
        )
        if delinquent_report:
            print(f"\n  Deliberate delinquent units ({len(delinquent_report)}):")
            for line in delinquent_report:
                print(line)

        counts = db.execute(
            text(
                "SELECT count(*) FILTER (WHERE escalation_pct IS NOT NULL) AS with_escalation, "
                "count(*) FILTER (WHERE concession_monthly IS NOT NULL) AS with_concession, "
                "count(*) AS total FROM lease l JOIN units u ON u.id = l.unit_id "
                "WHERE NOT u.is_shell"
            )
        ).mappings().first()
        print(
            f"\nReal-unit leases now: {counts['total']} total, {counts['with_escalation']} "
            f"with an escalation (expected: 0 — fabricated escalation fully removed), "
            f"{counts['with_concession']} with a standing concession (untouched by this "
            f"script)."
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
