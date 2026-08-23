"""Idempotent backfill for lease.concession_monthly (migration 0016) on the CURRENTLY
RUNNING dev DB — splits the rent waterfall's `collections_loss` line into `concessions`
(this field) and `bad_debt` (the remainder). See app/queries.py's `_unit_waterfall` for how
the split is computed, and the migration's docstring for why a standing $/month concession
was chosen over a "N free months at move-in" model.

Why this exists (instead of `python -m app.seed`): the full seed WIPES and reseeds the
whole database, destroying validated demo data other phases planted. This script only sets
`lease.concession_monthly` where it's currently NULL (never touches a lease that already
has one), using the exact same deterministic rule as the fresh-install path
(`app.seed._lease_concession`), so the live DB and a fresh install can't drift apart.

Gives a "handful" of leases (~2.5%, every 40th by a stable (property name, unit_number)
ordered index) a modest ~3%-of-contract-rent standing concession, rounded to the nearest
$5 — big enough to read clearly as a non-zero `concessions` bridge line without dominating
the portfolio's collections story. Every other lease is left NULL (no concession).

Reference-only data, same invariant as `contract_rent`/`market_rent`/`security_deposit`
before it: never feeds NOI/cash-flow or the actual collected rent
(`unit_month_summary.gross_rent`, untouched by this script) — it only changes how
`app.queries._unit_waterfall` DECOMPOSES the already-existing, already-reconciled
`collections_loss` into `concessions` + `bad_debt`.

Safe to re-run: any lease with `concession_monthly` already set (including explicitly $0,
were that ever written) is left completely untouched (skip-if-not-null) — re-running
should always report 0 newly-backfilled leases.

Run (from backend/):
    PYTHONPATH=.pydeps:. python3 scripts/backfill_lease_concessions.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.seed import _lease_concession  # noqa: E402


def main() -> None:
    db = SessionLocal()
    try:
        # Same stable (property name, unit_number) ordering every other backfill script in
        # this repo uses (backfill_unit_market_rent.py, backfill_lease_v2_fields.py) — not
        # the seed's internal creation-order `lease_counter`, but the same deterministic
        # RULE (`_lease_concession`'s modulo/percentage formula) applied consistently.
        rows = (
            db.execute(
                text(
                    """
                    SELECT l.id::text AS lease_id, l.contract_rent, l.concession_monthly,
                           u.is_shell
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

        updated = skipped_set = skipped_shell = 0
        for i, row in enumerate(rows):
            if row["is_shell"]:
                # Cedar Plaza Retail's shell-unit percentage-rent lease — excluded from the
                # waterfall entirely (is_shell), a concession here would never be used.
                skipped_shell += 1
                continue
            if row["concession_monthly"] is not None:
                skipped_set += 1
                continue
            concession = _lease_concession(i, row["contract_rent"])
            if concession is None:
                continue
            db.execute(
                text("UPDATE lease SET concession_monthly = :c WHERE id = :id"),
                {"c": concession, "id": row["lease_id"]},
            )
            updated += 1

        db.commit()
        print(
            f"Backfilled concession_monthly on {updated} lease(s); {skipped_set} already had "
            f"a value on file (possibly $0), {skipped_shell} shell-unit lease(s) skipped "
            f"(excluded from the waterfall)."
        )
        counts = db.execute(
            text(
                "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE concession_monthly IS NOT NULL) AS with_concession "
                "FROM lease"
            )
        ).mappings().first()
        print(
            f"lease table now: {counts['total']} total, {counts['with_concession']} with a "
            f"concession_monthly on file."
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
