"""Verification that rent booked at the property tier counts as occupancy for a one-unit property.

Run:  PYTHONPATH=.pydeps python3 verify_phase17.py

The bug this locks down (migration 0029): ``occupied_units`` counted only UNIT-tier rent. That
is the right question for a building and the wrong one for a house. A single-asset property has
one unit record and its rent may legitimately be booked at either tier — every statement parser
emits the property-tier kind, because a landlord statement names a property and not a flat, and
``/import/rows`` documents a blank unit as meaning exactly that.

So a house let continuously to one tenant, whose rent moved from the unit tier to the property
tier between two months, read 100% occupied and then 0% occupied on identical rent. Both vacancy
detectors believed it.

What has to be true, and stay true:

  1. **The tier the rent is booked at does not change the occupancy** of a one-unit property.
  2. **No vacancy item is raised** for a month like that — neither the change-based occupancy
     drop nor the level-based high-vacancy check.
  3. **A genuinely empty month is still caught.** This is the half that matters most: a fix that
     silenced the false alarm by silencing the detector would pass (1) and (2) and be worthless.
  4. **A multi-unit property is untouched.** Property-tier rent must NOT be read as "every unit
     is let" — with more than one unit there is no way to know, and guessing would hide a real
     vacancy.
  5. **The fallback never overrides unit-tier truth.** Where units DO report their own rent,
     that count stands, whatever else is booked at the property tier.

Self-contained: creates its own properties and units in months after the seed's range, and
deletes everything it wrote.
"""
from datetime import date

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
tok = client.post(
    "/auth/login", data={"username": "admin@example.com", "password": "admin12345"}
).json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        failures.append(f"{label}{': ' + detail if detail else ''}")
        print(f"  FAIL  {label} {detail}")
    else:
        print(f"  ok    {label}")


created: list[str] = []


def new_property(name: str, kind: str, units: list[str]) -> str:
    r = client.post("/properties", headers=H, json={"name": name, "type": kind, "address": None})
    assert r.status_code == 201, r.text[:200]
    pid = r.json()["id"]
    created.append(pid)
    for number in units:
        u = client.post(
            f"/properties/{pid}/units",
            headers=H,
            json={"unit_number": number, "beds": 2, "baths": 1, "sqft": 700},
        )
        assert u.status_code == 201, u.text[:200]
    return pid


def post_rows(rows: list[dict]) -> None:
    r = client.post("/import/rows", headers=H, json=rows)
    assert r.status_code == 200, r.text[:300]
    assert r.json()["committed"], r.text[:300]


def rent_row(pid: str, month: str, amount: float, unit: str | None) -> dict:
    return {
        "property_id": pid,
        "unit": unit,
        "month": month,
        "category": "Rent",
        "amount": amount,
    }


def summary(pid: str, month: str) -> dict:
    """That month's figures as the property dashboard reports them — occupancy included,
    which is the number under test."""
    r = client.get(
        f"/properties/{pid}/dashboard", headers=H, params={"from": month, "to": month}
    )
    assert r.status_code == 200, r.text[:200]
    return r.json()["current"]


def items_of(pid: str, month: str, *types: str) -> list[dict]:
    feed = client.get(
        f"/properties/{pid}/attention", headers=H, params={"from": month, "to": month}
    ).json()
    return [i for i in feed.get("items", []) if i["type"] in types]


def vacancy_items(pid: str, month: str) -> list[dict]:
    feed = client.get(
        f"/properties/{pid}/attention", headers=H, params={"from": month, "to": month}
    ).json()
    return [
        i
        for i in feed.get("items", [])
        if i["type"] in {"occupancy_drop", "high_vacancy", "unit_vacancy"}
    ]


def cleanup() -> None:
    for pid in created:
        client.delete(f"/properties/{pid}", headers=H)


# Months well clear of the seed's Jan-2024..Dec-2025 range.
M1, M2, M3 = "2027-01-01", "2027-02-01", "2027-03-01"

# ============================================================================================
print("\n1. A one-unit property whose rent moves from the unit tier to the property tier")
# ============================================================================================
house = new_property("ZZ Phase17 House", "single", ["1"])
post_rows(
    [
        rent_row(house, M1, 725.00, "1"),     # booked at the unit, as a lease-driven entry is
        rent_row(house, M2, 725.00, None),    # booked at the property, as a statement import is
    ]
)

s1, s2 = summary(house, M1), summary(house, M2)
check("the unit-tier month is fully occupied", s1["occupancy"] == 1.0, str(s1["occupancy"]))
check(
    "the property-tier month is ALSO fully occupied — same tenant, same rent",
    s2 and s2["occupancy"] == 1.0,
    str(s2 and s2["occupancy"]),
)
check("and the rent is unchanged by any of it", s2["gross_rent"] == 725.00, str(s2["gross_rent"]))
check(
    "no vacancy item is raised for the property-tier month",
    vacancy_items(house, M2) == [],
    str([i["type"] for i in vacancy_items(house, M2)]),
)

# ============================================================================================
print("\n2. A genuinely empty month is still caught")
# ============================================================================================
# The half that matters most: silencing the detector would have passed every check above.
post_rows([{"property_id": house, "unit": None, "month": M3, "category": "Insurance", "amount": 12.00}])
s3 = summary(house, M3)
check("a month with no rent at either tier reads as vacant", s3["occupancy"] == 0.0, str(s3["occupancy"]))
raised = vacancy_items(house, M3)
check(
    "and the vacancy detectors still fire on it",
    raised != [],
    "no vacancy item was raised for a month with no rent — the detector has been silenced",
)

# ============================================================================================
print("\n3. A multi-unit property is not told that one payment let every flat")
# ============================================================================================
block = new_property("ZZ Phase17 Block", "multifamily", ["1", "2", "3", "4"])
post_rows([rent_row(block, M1, 4_000.00, None)])  # one property-tier figure, four units
b1 = summary(block, M1)
check(
    "property-tier rent does NOT mark a four-unit block fully occupied",
    b1 and b1["occupancy"] == 0.0,
    str(b1 and b1["occupancy"]),
)

# ============================================================================================
print("\n4. Unit-tier truth always wins")
# ============================================================================================
post_rows(
    [
        rent_row(block, M2, 1_000.00, "1"),
        rent_row(block, M2, 1_000.00, "2"),
        rent_row(block, M2, 500.00, None),  # a property-tier figure alongside the units'
    ]
)
b2 = summary(block, M2)
check(
    "two of four units let reads as 50%, whatever else is booked at the property",
    b2 and b2["occupancy"] == 0.5,
    str(b2 and b2["occupancy"]),
)

# A one-unit property with BOTH tiers populated must not count its single unit twice.
both = new_property("ZZ Phase17 Both", "single", ["1"])
post_rows([rent_row(both, M1, 600.00, "1"), rent_row(both, M1, 50.00, None)])
sb = summary(both, M1)
check(
    "a one-unit property with rent at both tiers is 100%, not 200%",
    sb and sb["occupancy"] == 1.0,
    str(sb and sb["occupancy"]),
)

# ============================================================================================
print("\n5. 'has no posted record this month' asks the same question")
# ============================================================================================
# The unit-grain twin of the same defect: a one-unit property that filed its month at the
# property tier has no unit row, which is a fact about WHERE the rent was filed and not about
# whether it arrived.
check(
    "no missing-data item for the month filed at the property tier",
    items_of(house, M2, "missing_data") == [],
    str([i.get("label") for i in items_of(house, M2, "missing_data")]),
)
check(
    "but a month with no rent filed ANYWHERE is still reported missing",
    items_of(house, M3, "missing_data") != [],
    "a unit that genuinely posted nothing must still be flagged",
)

# ============================================================================================
print("\n6. A silent unit in a block is still a silent unit")
# ============================================================================================
# The rule must not leak into multi-unit properties: there, a property-tier figure says
# nothing about any particular door, and suppressing the alert would hide a real blackout.
pair = new_property("ZZ Phase17 Pair", "multifamily", ["1", "2"])
post_rows([rent_row(pair, M1, 500.00, "1"), rent_row(pair, M1, 500.00, "2")])
post_rows([rent_row(pair, M2, 500.00, "1"), rent_row(pair, M2, 500.00, None)])
missing = items_of(pair, M2, "missing_data")
check(
    "unit 2 going silent is still reported, despite property-tier rent that month",
    any(i.get("unit_number") == "2" for i in missing),
    str([(i.get("unit_number"), i.get("label")) for i in missing]),
)
check(
    "and unit 1, which did post, is not",
    not any(i.get("unit_number") == "1" for i in missing),
    str([i.get("unit_number") for i in missing]),
)

# ---- done -------------------------------------------------------------------------------
cleanup()
print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("verify_phase17 PASSED")
