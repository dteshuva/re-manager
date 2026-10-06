"""Put single-let properties on the rent roll: give each one a unit, and re-file its RENT from
the property tier down to that unit.

WHY THIS IS NEEDED
------------------
Every monthly record in this app is filed at one of two levels (see ``MonthlyRecord`` in
app/models.py):

    property tier (``unit_id IS NULL``)   obligations of the BUILDING — debt service, insurance,
                                         capex, anything not attributable to one door.
    unit tier                            money belonging to one LETTABLE DOOR, i.e. to a tenancy.

A portfolio of single-let houses imported from agent statements typically lands entirely at the
property tier, rent included: "125 Allendale Road collected £798.34" rather than "the tenancy at
125 Allendale Road paid £798.34". For the mortgage and the insurance that is exactly right. For
rent it is one level too shallow, and two features go dark because of it:

  * the **rent roll** is one row per unit, so a property with no units has no rows at all;
  * **arrears** compares what a TENANT owed (the lease schedule) against what that TENANT paid,
    so the "paid" leg is read from unit-tier rent (``unit_month_summary.gross_rent``). Property-
    tier rent is not attributable to a tenancy and cannot answer the question.

Note that arrears does NOT misreport in the meantime: a month with no unit record is treated as
MISSING DATA rather than as unpaid rent (see ``app.queries._unit_arrears_ledgers``), so nobody
is falsely accused — the column simply stays empty.

WHAT THIS CHANGES, AND WHAT IT DOES NOT
---------------------------------------
Only the LEVEL a rent line is filed at. The amount, the month, the category and the
classification are all untouched, so NOI, cash flow, every rollup, the budget variance and the
P&L views are identical before and after. That is not an aspiration: this script snapshots
``noi``/``gross_rent``/``cash_flow`` for every affected property-month, re-reads them after the
move, and REFUSES TO COMMIT if any figure moved by a penny.

Rate-stated lines (migration 0023 — "management: 8% of rent") are safe by construction: a
property-tier fee's basis is the property's WHOLE rent for the month, unit tiers included, so
moving rent deeper leaves the basis unchanged. ``refresh_month`` re-derives them anyway, and the
reconciliation above would catch it if it ever didn't.

Shared-expense-posted lines (migration 0022 — a portfolio mortgage split across properties) are
never touched: they are property-tier costs and that is where they belong.

SAFETY
------
  * Dry run by DEFAULT. Pass ``--apply`` to write.
  * Scoped to ONE account, named by the email of a user in it — so it cannot wander into the
    demo portfolio.
  * Only properties with ZERO units are given one; a property that already has units is skipped
    entirely, which also makes the script safe to re-run.
  * LOCKED property-months are refused up front (nothing is written if any is locked) rather
    than half-migrated — a locked month is closed to edits by design.
  * Everything happens in one transaction: the reconciliation failing rolls back the lot.

Run (from backend/):
    PYTHONPATH=.pydeps:. python3 scripts/rent_to_unit_tier.py --email you@example.com
    PYTHONPATH=.pydeps:. python3 scripts/rent_to_unit_tier.py --email you@example.com --apply
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.summaries import refresh_month  # noqa: E402

# The app's own rule for "is this line rent": a per-line override wins over the category's
# default (app/queries.py resolves the effective classification exactly this way), so a line
# someone reclassified by hand is treated the way the P&L treats it.
_EFFECTIVE_RENT = "COALESCE(li.classification, c.default_classification) = 'rent'"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--email", required=True, help="any user in the account to migrate")
    ap.add_argument("--unit-number", default="1", help="unit number to create (default: 1)")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        account_id = db.scalar(
            text("SELECT account_id::text FROM users WHERE email = :e"), {"e": args.email}
        )
        if account_id is None:
            raise SystemExit(f"No user with email {args.email!r}")
        account_name = db.scalar(
            text("SELECT name FROM accounts WHERE id = :a"), {"a": account_id}
        )
        print(f"Account: {account_name} ({account_id})")
        print(f"Mode:    {'APPLY' if args.apply else 'DRY RUN (nothing will be written)'}\n")

        # ---- 1. which properties qualify -------------------------------------------------
        targets = (
            db.execute(
                text(
                    """
                    SELECT p.id::text AS property_id, p.name, p.type
                    FROM properties p
                    WHERE p.account_id = :a
                      AND NOT EXISTS (SELECT 1 FROM units u WHERE u.property_id = p.id)
                    ORDER BY p.name
                    """
                ),
                {"a": account_id},
            )
            .mappings()
            .all()
        )
        if not targets:
            print("No unit-less properties — nothing to do (already migrated?).")
            return
        property_ids = [t["property_id"] for t in targets]

        # ---- 2. the rent lines to re-file ------------------------------------------------
        rent_lines = (
            db.execute(
                text(
                    f"""
                    SELECT li.id::text AS line_id, li.amount, c.name AS category,
                           m.id::text AS record_id, m.property_id::text AS property_id,
                           m.month, p.name AS property_name
                    FROM line_items li
                    JOIN monthly_records m ON m.id = li.monthly_record_id
                    JOIN properties p ON p.id = m.property_id
                    JOIN categories c ON c.id = li.category_id
                    WHERE m.property_id::text = ANY(:pids)
                      AND m.unit_id IS NULL
                      AND {_EFFECTIVE_RENT}
                    ORDER BY p.name, m.month
                    """
                ),
                {"pids": property_ids},
            )
            .mappings()
            .all()
        )

        touched = sorted({(r["property_id"], r["month"]) for r in rent_lines})
        print(f"{len(targets)} property(ies) with no units; "
              f"{len(rent_lines)} property-tier rent line(s) across {len(touched)} property-month(s).")

        # ---- 3. refuse rather than half-migrate, if any month is locked ------------------
        locked = (
            db.execute(
                text(
                    """
                    SELECT p.name, ps.month FROM period_status ps
                    JOIN properties p ON p.id = ps.property_id
                    WHERE ps.property_id::text = ANY(:pids) AND ps.status = 'locked'
                    ORDER BY p.name, ps.month
                    """
                ),
                {"pids": property_ids},
            )
            .mappings()
            .all()
        )
        locked_keys = {(str(r["name"]), r["month"]) for r in locked}
        if any((r["property_name"], r["month"]) in locked_keys for r in rent_lines):
            raise SystemExit(
                "Refusing to run: some affected property-months are LOCKED. Unlock them (admin) "
                "or exclude those properties first — a half-migrated portfolio is worse than an "
                "un-migrated one."
            )

        # ---- 4. snapshot the P&L, so the move can be proven to be a no-op ----------------
        def pnl_snapshot() -> dict:
            rows = db.execute(
                text(
                    """
                    SELECT property_id::text AS pid, month, gross_rent, noi, cash_flow
                    FROM property_month_summary
                    WHERE property_id::text = ANY(:pids)
                    """
                ),
                {"pids": property_ids},
            ).mappings().all()
            return {
                (r["pid"], r["month"]): (
                    Decimal(str(r["gross_rent"])), Decimal(str(r["noi"])), Decimal(str(r["cash_flow"]))
                )
                for r in rows
            }

        before = pnl_snapshot()

        # ---- 5. plan ---------------------------------------------------------------------
        by_property: dict[str, list] = {}
        for r in rent_lines:
            by_property.setdefault(r["property_id"], []).append(r)
        print()
        for t in targets:
            lines = by_property.get(t["property_id"], [])
            total = sum(Decimal(str(r["amount"])) for r in lines)
            print(f"  {t['name']:<24} {t['type']:<12} + unit {args.unit_number!r}; "
                  f"{len(lines):>3} rent line(s) to re-file (total {total:,.2f})")

        if not args.apply:
            print("\nDry run only. Re-run with --apply to write.")
            return

        # ---- 6. create the units ---------------------------------------------------------
        unit_by_property: dict[str, str] = {}
        for t in targets:
            unit_id = db.scalar(
                text(
                    "INSERT INTO units (property_id, unit_number) VALUES (:p, :n) "
                    "RETURNING id::text"
                ),
                {"p": t["property_id"], "n": args.unit_number},
            )
            unit_by_property[t["property_id"]] = unit_id

        # ---- 7. move each rent LINE to a unit-tier record of the same property-month -----
        # The line moves, not the record: the property-tier record also holds the mortgage and
        # the insurance, which must stay exactly where they are.
        record_cache: dict[tuple[str, object], str] = {}
        for r in rent_lines:
            key = (r["property_id"], r["month"])
            unit_record_id = record_cache.get(key)
            if unit_record_id is None:
                unit_record_id = db.scalar(
                    text(
                        """
                        INSERT INTO monthly_records (account_id, property_id, unit_id, month)
                        VALUES (:a, :p, :u, :m)
                        RETURNING id::text
                        """
                    ),
                    {
                        "a": account_id,
                        "p": r["property_id"],
                        "u": unit_by_property[r["property_id"]],
                        "m": r["month"],
                    },
                )
                record_cache[key] = unit_record_id
            db.execute(
                text("UPDATE line_items SET monthly_record_id = :rec WHERE id = :lid"),
                {"rec": unit_record_id, "lid": r["line_id"]},
            )

        # ---- 8. recompute the rollups for every property-month we touched ----------------
        # Also re-derives any rate-stated line (migration 0023) off the moved rent.
        for property_id, month in touched:
            refresh_month(db, property_id, month)
        db.flush()

        # ---- 9. prove the P&L did not move, or roll the whole thing back -----------------
        after = pnl_snapshot()
        drift = [
            (pid, month, before.get((pid, month)), after.get((pid, month)))
            for (pid, month) in set(before) | set(after)
            if before.get((pid, month)) != after.get((pid, month))
        ]
        if drift:
            db.rollback()
            print("\nROLLED BACK — re-filing rent changed the P&L, which it must never do:")
            for pid, month, b, a in drift[:10]:
                print(f"  {pid} {month}: gross_rent/noi/cash_flow {b} -> {a}")
            raise SystemExit(1)

        db.commit()
        print(f"\nApplied: {len(unit_by_property)} unit(s) created, {len(rent_lines)} rent line(s) "
              f"re-filed to the unit tier, {len(touched)} property-month(s) recomputed.")
        print("P&L reconciliation: gross_rent / NOI / cash_flow unchanged on every "
              "property-month ✓")
        print("\nNext: open the Rent Roll tab — every property now has a row. Use 'Add lease' on "
              "each to record the tenancy (tenant, start date, rent; leave 'Lease end' blank for "
              "a periodic tenancy). Arrears starts reporting as soon as a lease is on file.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
