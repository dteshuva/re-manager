"""Idempotent backfill for the lease v2 fields (migration 0013) on the CURRENTLY RUNNING
dev DB: security_deposit, escalation_pct, and Cedar Plaza Retail's percentage-rent terms.

Why this exists (instead of `python -m app.seed`): the full seed WIPES and reseeds the
whole database, destroying validated demo data other phases planted. This script only
UPDATEs existing `lease` rows in place (never inserts/deletes a residential lease), using
the exact same deterministic formula as the fresh-install path (`app.seed._v2_terms`), so
the live DB and a fresh install can't drift apart. It also plants Cedar Plaza Retail's one
percentage-rent lease if it isn't there yet.

``lease_type`` and ``escalation_frequency_months`` are NOT touched here: migration 0013
itself already backfilled every existing row (lease_type derived from end_date via a plain
SQL UPDATE at migration time; escalation_frequency_months via its NOT NULL DEFAULT 12,
which Postgres applies to existing rows on an ADD COLUMN). Only security_deposit and
escalation_pct — genuinely new derived VALUES, not just typed columns — need this script.

Safe to re-run: any lease with `security_deposit` already set is left completely untouched
(skip-if-set), and Cedar Plaza Retail's retail lease is only created if it doesn't already
have a unit + lease on file. Re-running should always report 0 changes on a DB this has
already been run against.

Run (from backend/):
    PYTHONPATH=.pydeps:. python3 scripts/backfill_lease_v2_fields.py
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Lease, Property, Unit  # noqa: E402
from app.seed import _retail_lease_plan, _v2_terms  # noqa: E402

CEDAR_PROPERTY_NAME = "Cedar Plaza Retail"
CEDAR_MONTHLY_RENT = Decimal("5200")  # matches app/seed.py's fresh-install figure


def _backfill_residential(db) -> tuple[int, int]:
    """Set security_deposit/escalation_pct on every lease that doesn't have them yet, in
    the same deterministic (property name, unit_number) order used by seed_leases.py /
    fix_lease_stagger.py, so `i` (and therefore the deposit-factor/escalation formula in
    `_v2_terms`) lines up with what a fresh install would have produced."""
    rows = (
        db.execute(
            text(
                """
                SELECT l.id::text AS lease_id, l.contract_rent, l.end_date,
                       l.security_deposit
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
    updated = skipped = 0
    for i, row in enumerate(rows):
        if row["security_deposit"] is not None:
            skipped += 1
            continue
        terms = _v2_terms(i, row["contract_rent"], row["end_date"])
        lease = db.get(Lease, row["lease_id"])
        lease.security_deposit = terms["security_deposit"]
        lease.escalation_pct = terms["escalation_pct"]
        updated += 1
    return updated, skipped


def _backfill_cedar_retail(db) -> str:
    """Plant Cedar Plaza Retail's shell unit + percentage-rent lease if it doesn't already
    exist. See app/seed.py's `_retail_lease_plan` docstring for why a shell unit is used.

    `is_shell` (migration 0014) is force-set true on every path here — including an
    ALREADY-EXISTING unit from a prior run of this script (or the pre-0014 live DB) that
    predates the flag — so this script alone is enough to self-heal a DB regardless of
    which migration state it was originally planted under. Idempotent: a no-op UPDATE if
    already true."""
    cedar = db.execute(
        text("SELECT id::text AS id FROM properties WHERE name = :name"),
        {"name": CEDAR_PROPERTY_NAME},
    ).mappings().first()
    if cedar is None:
        return f"'{CEDAR_PROPERTY_NAME}' not found — skipped (is the DB seeded?)."

    existing_unit = db.execute(
        text("SELECT id::text AS id FROM units WHERE property_id = :pid"),
        {"pid": cedar["id"]},
    ).mappings().first()
    if existing_unit is not None:
        db.execute(
            text("UPDATE units SET is_shell = true WHERE id = :id AND is_shell = false"),
            {"id": existing_unit["id"]},
        )
        has_lease = db.execute(
            text("SELECT 1 FROM lease WHERE unit_id = :uid"), {"uid": existing_unit["id"]}
        ).first()
        if has_lease:
            return "already has a unit + lease — left untouched (is_shell confirmed true)."
        # Shell unit exists (e.g. a prior partial run) but no lease yet — add it.
        db.add(Lease(unit_id=existing_unit["id"], **_retail_lease_plan(CEDAR_MONTHLY_RENT)))
        return "added the percentage-rent lease to its existing shell unit."

    unit = Unit(property_id=cedar["id"], unit_number="RETAIL", label="Retail Space", is_shell=True)
    db.add(unit)
    db.flush()
    db.add(Lease(unit_id=unit.id, **_retail_lease_plan(CEDAR_MONTHLY_RENT)))
    return "created its shell unit + percentage-rent lease."


def main() -> None:
    db = SessionLocal()
    try:
        updated, skipped = _backfill_residential(db)
        cedar_msg = _backfill_cedar_retail(db)
        db.commit()
        print(
            f"Residential leases: backfilled {updated} lease(s) with security_deposit/"
            f"escalation_pct ({skipped} already had them and were left untouched)."
        )
        print(f"Cedar Plaza Retail: {cedar_msg}")

        counts = db.execute(
            text(
                "SELECT count(*) FILTER (WHERE security_deposit IS NOT NULL) AS with_deposit, "
                "count(*) FILTER (WHERE escalation_pct IS NOT NULL) AS with_escalation, "
                "count(*) FILTER (WHERE pct_rent_rate IS NOT NULL) AS with_pct_rent, "
                "count(*) AS total FROM lease"
            )
        ).mappings().first()
        print(
            f"Lease table now: {counts['total']} total, {counts['with_deposit']} with a "
            f"deposit, {counts['with_escalation']} with an escalation, "
            f"{counts['with_pct_rent']} with percentage-rent terms."
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
