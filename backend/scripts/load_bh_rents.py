"""One-off: load the BH Group portfolio's current rents and tenancy start dates.

Source is the owner's own schedule (address -> current rent). Every tenancy is created PERIODIC
(no end date — a rolling AST, the normal English arrangement) with a common start date, because
the schedule carries a rent but not a per-tenancy start.

WHAT IT WRITES
    For each property, on its single unit: the CURRENT tenancy's ``contract_rent`` and
    ``start_date``. An existing tenancy is UPDATED in place (its id, and therefore any arrears
    adjustments hanging off it, survive); a property with none gets one created.

    Every other field is preserved on an update and left null on a create — tenant name, deposit,
    opening arrears, rent-increase history. The schedule doesn't carry them, and a loader that
    blanked them would undo anything already entered by hand.

PROPERTIES SHOWING £0 ARE TREATED AS VOID, NOT AS A £0 TENANCY
    A tenancy recorded at £0/month would report the flat as OCCUPIED with nothing owed. That is
    worse than silence: it feeds physical and economic occupancy, Gross Potential Rent and the
    void statistics — the very numbers the rent roll exists to produce — with a let that isn't
    happening. A unit with NO tenancy already reads as a void everywhere, which is what a £0 line
    on a rent schedule means. The £0 entries are therefore reported and skipped, not loaded.

    If one of them really is let at zero (a family arrangement, say), create its tenancy by hand
    on the Rent Roll tab — the skip list below names them.

Safe to re-run: it writes the same figures, so a second run reports the same result.

Run (from backend/):
    PYTHONPATH=.pydeps:. python3 scripts/load_bh_rents.py --email you@example.com
    PYTHONPATH=.pydeps:. python3 scripts/load_bh_rents.py --email you@example.com --apply
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal  # noqa: E402

# The schedule, verbatim. Keys are the property names as stored; the owner's spreadsheet writes
# some of them differently ("125 Allendale road", "209 Windsor Ave"), so the mapping is explicit
# rather than fuzzy — a near-match that silently picked the wrong flat would be invisible.
START_DATE = date(2026, 2, 1)
RENTS: dict[str, Decimal] = {
    "22 Chatsworth Gardens": Decimal("0"),
    "24 Chatsworth Gardens": Decimal("700"),
    "125 Allendale Road": Decimal("700"),
    "127 Allendale Road": Decimal("550"),
    "145 Allendale Road": Decimal("0"),
    "147 Allendale Road": Decimal("500"),
    "151 Allendale Road": Decimal("600"),
    "173 Allendale Road": Decimal("700"),
    "209 Windsor Avenue": Decimal("600"),
    "125 Westbourne Avenue": Decimal("600"),
    "90 Rawling Road": Decimal("550"),
    "90a Rawling Road": Decimal("550"),
    "64 King Edward": Decimal("725"),
    "9 Sandhoe Gardens": Decimal("600"),
    "10 Sandhoe Gardens": Decimal("500"),
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--email", required=True, help="any user in the account to load")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        account_id = db.scalar(
            text("SELECT account_id::text FROM users WHERE email = :e"), {"e": args.email}
        )
        if account_id is None:
            raise SystemExit(f"No user with email {args.email!r}")
        print(f"Account: {account_id}")
        print(f"Mode:    {'APPLY' if args.apply else 'DRY RUN (nothing will be written)'}")
        print(f"Start date for every tenancy: {START_DATE}\n")

        rows = (
            db.execute(
                text(
                    """
                    SELECT p.name, u.id::text AS unit_id,
                           l.id::text AS lease_id, l.contract_rent, l.start_date, l.tenant_name
                    FROM properties p
                    JOIN units u ON u.property_id = p.id
                    LEFT JOIN LATERAL (
                        -- The unit's CURRENT tenancy, by the same rule the app uses everywhere:
                        -- one covering today wins, else the most recently started.
                        SELECT l.* FROM lease l
                        WHERE l.unit_id = u.id
                        ORDER BY (l.status IN ('active','notice')
                                  AND l.start_date <= CURRENT_DATE
                                  AND (l.end_date IS NULL OR l.end_date >= CURRENT_DATE)) DESC,
                                 l.start_date DESC
                        LIMIT 1
                    ) l ON TRUE
                    WHERE p.account_id = :a
                    ORDER BY p.name
                    """
                ),
                {"a": account_id},
            )
            .mappings()
            .all()
        )
        by_name = {r["name"]: r for r in rows}

        missing = sorted(set(RENTS) - set(by_name))
        if missing:
            raise SystemExit(
                "These schedule entries match no property (or no unit) in the account, so "
                "nothing was written — fix the names first:\n  " + "\n  ".join(missing)
            )
        unlisted = sorted(set(by_name) - set(RENTS))

        created = updated = unchanged = 0
        voids: list[str] = []
        for name in sorted(RENTS, key=lambda n: by_name[n]["name"]):
            rent = RENTS[name]
            row = by_name[name]
            if rent == 0:
                # See the module docstring: a £0 line on a rent schedule means "empty", and a unit
                # with no tenancy already reads as a void everywhere in the app.
                voids.append(name)
                note = "has a tenancy on file — NOT removed, review by hand" if row["lease_id"] else ""
                print(f"  {name:<24} £0 -> VOID, no tenancy created  {note}")
                continue

            if row["lease_id"] is None:
                print(f"  {name:<24} create  rent £{rent}, start {START_DATE}")
                created += 1
                if args.apply:
                    db.execute(
                        text(
                            """
                            INSERT INTO lease (unit_id, tenant_name, start_date, end_date,
                                               contract_rent, status, lease_type)
                            VALUES (:u, NULL, :s, NULL, :r, 'active', 'mtm')
                            """
                        ),
                        {"u": row["unit_id"], "s": START_DATE, "r": rent},
                    )
            else:
                same = (
                    Decimal(str(row["contract_rent"])) == rent and row["start_date"] == START_DATE
                )
                if same:
                    print(f"  {name:<24} unchanged  rent £{rent}, start {START_DATE}")
                    unchanged += 1
                    continue
                print(
                    f"  {name:<24} update  rent £{row['contract_rent']} -> £{rent}, "
                    f"start {row['start_date']} -> {START_DATE}"
                )
                updated += 1
                if args.apply:
                    # Only these two columns. Everything else on the tenancy stays as it is.
                    db.execute(
                        text(
                            "UPDATE lease SET contract_rent = :r, start_date = :s WHERE id = :id"
                        ),
                        {"r": rent, "s": START_DATE, "id": row["lease_id"]},
                    )

        if unlisted:
            print("\n  Not in the schedule, left untouched: " + ", ".join(unlisted))

        print(
            f"\n{created} tenancy(ies) to create, {updated} to update, {unchanged} already correct, "
            f"{len(voids)} left as voids ({', '.join(voids) if voids else 'none'})."
        )
        if not args.apply:
            print("\nDry run only. Re-run with --apply to write.")
            return
        db.commit()
        print("Applied.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
