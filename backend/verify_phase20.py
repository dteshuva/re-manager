"""Verification that imported rent on a single-let property is filed at its unit.

Run:  PYTHONPATH=.pydeps python3 verify_phase20.py

Every statement parser emits rent with no unit, because a landlord statement names a house,
not a flat. Filed at the property tier that rent is invisible to the rent roll and to arrears,
which read unit-tier rent; scripts/rent_to_unit_tier.py moved the history down, and every new
import re-opened the gap. The importer now files it where it belongs.

What has to be true:

  1. **Unit-less rent on a one-unit property lands on that unit.**
  2. **Only rent moves.** A management fee, insurance, a mortgage stay at the property tier.
  3. **A multi-unit property is left alone** — nothing in the row says which flat paid.
  4. **An explicit unit is never overridden.**
  5. **Shell units don't count**, matching occupancy (0029): one lettable unit + a shell is
     still single-let.
  6. **Re-importing a month an earlier import filed at the property tier replaces it** rather
     than leaving the same rent at both levels (double-counted in NOI).
  7. **A classification override decides**, as in the migration script: a row overridden to
     "rent" moves, a Rent row overridden to something else doesn't.

Self-contained: creates its own properties in months after the seed's range and deletes them.
"""
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import SessionLocal
from app.main import app

client = TestClient(app)
tok = client.post(
    "/auth/login", data={"username": "admin@example.com", "password": "admin12345"}
).json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}

failures: list[str] = []
created: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        failures.append(f"{label}{': ' + detail if detail else ''}")
        print(f"  FAIL  {label} {detail}")
    else:
        print(f"  ok    {label}")


def add_unit(pid: str, number: str, shell: bool = False) -> None:
    u = client.post(f"/properties/{pid}/units", headers=H, json={"unit_number": number})
    assert u.status_code == 201, u.text[:200]
    if shell:  # not settable through the API (seed/migration-only), so set it directly
        with SessionLocal() as db:
            db.execute(
                text("UPDATE units SET is_shell = true WHERE property_id = :p AND unit_number = :n"),
                {"p": pid, "n": number},
            )
            db.commit()


def new_property(name: str, kind: str, units: list[str]) -> str:
    r = client.post("/properties", headers=H, json={"name": name, "type": kind, "address": None})
    assert r.status_code == 201, r.text[:200]
    pid = r.json()["id"]
    created.append(pid)
    # ``units`` is the set the property ends up with. A single-let is created with unit "1"
    # already; an empty list removes it, to stage a house as it was before that rule — the
    # only way property-tier rent on a house can still arise.
    have = {u["unit_number"]: u["id"] for u in client.get(f"/properties/{pid}/units", headers=H).json()}
    for number, uid in have.items():
        if number not in units:
            assert client.delete(f"/units/{uid}", headers=H).status_code == 204
    for number in units:
        if number not in have:
            add_unit(pid, number)
    return pid


def post_rows(rows: list[dict]) -> dict:
    r = client.post("/import/rows", headers=H, json=rows)
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["committed"], r.text[:300]
    return body


def row(pid, month, category, amount, unit=None, classification=None) -> dict:
    out = {"property_id": pid, "unit": unit, "month": month, "category": category, "amount": amount}
    if classification:
        out["classification"] = classification
    return out


def filed(pid: str, month: str) -> list[tuple[str, str, float]]:
    """(tier, category, amount) for every line in that property-month."""
    with SessionLocal() as db:
        rows = db.execute(
            text(
                """
                SELECT CASE WHEN m.unit_id IS NULL THEN 'property' ELSE u.unit_number END,
                       c.name, li.amount
                FROM line_items li
                JOIN monthly_records m ON m.id = li.monthly_record_id
                JOIN categories c ON c.id = li.category_id
                LEFT JOIN units u ON u.id = m.unit_id
                WHERE m.property_id = :p AND m.month = :m
                ORDER BY 2, 1
                """
            ),
            {"p": pid, "m": month},
        ).all()
    return [(t, c, float(a)) for t, c, a in rows]


M1, M2, M3 = "2027-04-01", "2027-05-01", "2027-06-01"

try:
    print("\n1-2. a single-let house: rent to the unit, everything else stays")
    house = new_property("ZZ Phase20 House", "single", ["1"])
    post_rows([
        row(house, M1, "Rent", 725.00),
        row(house, M1, "Insurance", 12.00),
        row(house, M1, "Property Management", 65.25),
    ])
    f = filed(house, M1)
    check("rent filed at unit 1", ("1", "Rent", 725.0) in f, str(f))
    check("no rent left at the property tier", not any(t == "property" and c == "Rent" for t, c, _ in f), str(f))
    check(
        "insurance and the fee stay at the property tier",
        ("property", "Insurance", 12.0) in f and ("property", "Property Management", 65.25) in f,
        str(f),
    )

    print("\n3. a multi-unit block is left alone")
    block = new_property("ZZ Phase20 Block", "multifamily", ["1", "2"])
    post_rows([row(block, M1, "Rent", 1_500.00)])
    f = filed(block, M1)
    check("block rent stays at the property tier", f == [("property", "Rent", 1500.0)], str(f))

    print("\n4. an explicit unit is never overridden")
    post_rows([row(block, M2, "Rent", 700.00, unit="2")])
    check("rent named for unit 2 stays on unit 2", filed(block, M2) == [("2", "Rent", 700.0)], str(filed(block, M2)))

    print("\n5. a shell unit doesn't make a house multi-unit")
    # A single-let property may hold only one unit, so the shell case is a "multifamily"
    # with one lettable unit plus a shell — the rule counts lettable units, not the type.
    shelled = new_property("ZZ Phase20 Shelled", "multifamily", ["1"])
    add_unit(shelled, "Common", shell=True)
    post_rows([row(shelled, M1, "Rent", 600.00)])
    f = filed(shelled, M1)
    check("rent goes to the one lettable unit, not the shell", f == [("1", "Rent", 600.0)], str(f))

    print("\n6. re-importing a month left at the property tier replaces it")
    legacy = new_property("ZZ Phase20 Legacy", "single", [])
    post_rows([row(legacy, M1, "Rent", 550.00), row(legacy, M1, "Insurance", 10.00)])
    check("before: rent at the property tier (no unit yet)", ("property", "Rent", 550.0) in filed(legacy, M1))
    add_unit(legacy, "1")
    post_rows([row(legacy, M1, "Rent", 550.00)])
    f = filed(legacy, M1)
    check("after: the rent is on the unit, once", [x for x in f if x[1] == "Rent"] == [("1", "Rent", 550.0)], str(f))
    check("the property tier's other lines are untouched", ("property", "Insurance", 10.0) in f, str(f))
    nets = client.get(
        f"/properties/{legacy}/dashboard", headers=H, params={"from": M1, "to": M1}
    ).json()["current"]
    check("gross rent counts it once, not twice", nets["gross_rent"] == 550.0, str(nets["gross_rent"]))

    print("\n7. the effective classification decides")
    post_rows([
        row(house, M3, "Utilities", 40.00, classification="rent"),   # a recharge treated as rent
        row(house, M3, "Rent", 25.00, classification="other_below_line"),
    ])
    f = filed(house, M3)
    check("a row overridden to rent moves to the unit", ("1", "Utilities", 40.0) in f, str(f))
    check("a Rent row overridden away stays at the property", ("property", "Rent", 25.0) in f, str(f))
finally:
    for pid in created:
        client.delete(f"/properties/{pid}", headers=H)

print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("verify_phase20 PASSED")
