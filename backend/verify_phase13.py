"""Verification of rate-stated line items — "property management: 8% of rent".

Run:  PYTHONPATH=.pydeps python3 verify_phase13.py

The feature exists because a management fee is a RATE, not a figure. Typed as an amount it
has to be recomputed by hand every month, and the moment rent moves — a renewal, a vacancy, a
door re-let at a different price — the typed figure quietly stops matching the contract and
NOI is wrong by the difference. So the two properties worth proving are:

  1. **The derived figure is right, and stays right.** The fee equals the rate applied to the
     rent basis, to the cent, and it is REWRITTEN when the rent it depends on changes later —
     including when that rent lives on a different record (unit rents versus the property-tier
     fee) edited weeks apart. A rate that only computes once would be no better than typing
     the number.

  2. **A rate never quietly overwrites a stated figure.** Percentage lines are recomputed on
     every write to a property-month, so anything that states an amount outright — a hand
     edit, an import, a shared-expense posting — must take the line back to a fixed amount
     rather than have the recompute clobber it moments later.

Also covers the basis scope rule (unit-tier = that unit's rent, property-tier = the whole
property's), cent rounding, the ``GET /rent-basis`` preview agreeing with what is stored, the
P&L rollups seeing the derived money, period locks, and the two incoherent requests that are
refused (a rate AND an amount; a rate on a line that classifies as rent).

Self-contained: works in months after the seed's range, then deletes everything it wrote.
"""
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import SessionLocal
from app.main import app

client = TestClient(app)
db = SessionLocal()
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


def cents(x) -> int:
    return int((Decimal(str(x)) * 100).to_integral_value())


def _pid(name: str) -> str:
    pid = db.scalar(text("SELECT id::text FROM properties WHERE name = :n"), {"n": name})
    assert pid, f"seed fixture missing property: {name}"
    return pid


def _cat(name: str) -> str:
    cid = db.scalar(text("SELECT id::text FROM categories WHERE name = :n"), {"n": name})
    assert cid, f"seed fixture missing category: {name}"
    return cid


MF = _pid("Oak Ridge Residences")      # multifamily: rent per unit, one fee at the property tier
SF = _pid("Birch Street House")        # single: rent and fee on the same property-tier record
RENT, MGMT, INS = _cat("Rent"), _cat("Property Management"), _cat("Insurance")

UNITS = [
    r[0]
    for r in db.execute(
        text("SELECT id::text FROM units WHERE property_id = :p ORDER BY unit_number LIMIT 2"),
        {"p": MF},
    ).all()
]
assert len(UNITS) == 2, "seed fixture: expected at least two units on the multifamily"
U1, U2 = UNITS

# Deliberately after the seed's Jan-2024..Dec-2025 range, so these property-months start empty
# and this script is the only thing writing to them.
M = date(2026, 7, 1)
M2 = date(2026, 8, 1)
TEST_MONTHS = [M, M2]


def post(property_id: str, unit_id: str | None, month: date, items: list[dict]) -> dict:
    r = client.post(
        "/records",
        headers=H,
        json={
            "property_id": property_id,
            "unit_id": unit_id,
            "month": str(month),
            "line_items": items,
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


def amount_of(property_id: str, unit_id: str | None, month: date, category_id: str):
    """The stored amount (and rate) of one category on one record, read back from the API."""
    recs = client.get(
        f"/records?property_id={property_id}&month={month}", headers=H
    ).json()
    rec = next((r for r in recs if r["unit_id"] == unit_id), None)
    if rec is None:
        return None, None
    li = next((li for li in rec["line_items"] if li["category_id"] == category_id), None)
    return (li["amount"], li["rate_pct"]) if li else (None, None)


def cleanup() -> None:
    # Every record and status row in these months was created here (they post-date the seed),
    # so removing them restores the fixture exactly. Line items cascade.
    for table in ("monthly_records", "period_status"):
        db.execute(
            text(
                f"DELETE FROM {table} WHERE month = ANY(CAST(:ms AS date[])) "
                "AND property_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ms": TEST_MONTHS, "ids": [MF, SF]},
        )
    db.commit()


cleanup()  # in case a previous run died before its own teardown

print("\n== 1. The property-tier basis is the property's WHOLE rent ==")
post(MF, U1, M, [{"category_id": RENT, "amount": 2000.00}])
post(MF, U2, M, [{"category_id": RENT, "amount": 1000.00}])
post(MF, None, M, [{"category_id": MGMT, "rate_pct": 8}])

fee, rate = amount_of(MF, None, M, MGMT)
check("8% of 3,000 rent across two units = 240.00", cents(fee) == cents(240.00), str(fee))
check("the rate is stored alongside the derived figure", rate == 8.0, str(rate))

basis = client.get(f"/rent-basis?property_id={MF}&month={M}", headers=H).json()
check("GET /rent-basis previews the same basis that was used",
      cents(basis["rent"]) == cents(3000.00), str(basis))

print("\n== 2. The figure FOLLOWS rent — the whole point of storing a rate ==")
# A renewal on one door, entered weeks later, on a DIFFERENT record than the fee.
post(MF, U2, M, [{"category_id": RENT, "amount": 1500.00}])
fee, _ = amount_of(MF, None, M, MGMT)
check("raising one unit's rent to 1,500 moves the fee to 280.00 with no re-entry",
      cents(fee) == cents(280.00), str(fee))

# ...and downwards, which is the case that silently overstates NOI when done by hand.
post(MF, U2, M, [{"category_id": RENT, "amount": 0}])
fee, _ = amount_of(MF, None, M, MGMT)
check("a vacancy on that unit drops the fee to 160.00", cents(fee) == cents(160.00), str(fee))
post(MF, U2, M, [{"category_id": RENT, "amount": 1500.00}])

print("\n== 3. A unit-tier rate is charged on THAT unit's rent, not the property's ==")
post(MF, U1, M, [{"category_id": RENT, "amount": 2000.00}, {"category_id": MGMT, "rate_pct": 10}])
unit_fee, _ = amount_of(MF, U1, M, MGMT)
check("10% on a unit-tier record = 200.00 (that unit's rent, not 3,500)",
      cents(unit_fee) == cents(200.00), str(unit_fee))
tier_fee, _ = amount_of(MF, None, M, MGMT)
check("and the property-tier fee is unchanged by it", cents(tier_fee) == cents(280.00), str(tier_fee))

u_basis = client.get(f"/rent-basis?property_id={MF}&month={M}&unit_id={U1}", headers=H).json()
check("GET /rent-basis scoped to a unit returns that unit's rent",
      cents(u_basis["rent"]) == cents(2000.00), str(u_basis))

print("\n== 4. A fee is never part of its own basis ==")
# The unit-tier fee (200.00) sits in the same property-month as the property-tier fee. If
# rate lines fed the basis, the property-tier fee would have moved off 3,500.
check("the property-tier basis still excludes both fees",
      cents(client.get(f"/rent-basis?property_id={MF}&month={M}", headers=H).json()["rent"])
      == cents(3500.00))
r = client.post("/records", headers=H, json={
    "property_id": SF, "unit_id": None, "month": str(M),
    "line_items": [{"category_id": RENT, "rate_pct": 5}],
})
check("a rate on a rent-classified line is refused", r.status_code == 400, r.text[:160])

print("\n== 5. A rate and an amount are mutually exclusive ==")
r = client.post("/records", headers=H, json={
    "property_id": SF, "unit_id": None, "month": str(M),
    "line_items": [{"category_id": MGMT, "amount": 99.00, "rate_pct": 8}],
})
check("sending both is refused rather than silently ignoring one", r.status_code == 400, r.text[:160])
r = client.post("/records", headers=H, json={
    "property_id": SF, "unit_id": None, "month": str(M),
    "line_items": [{"category_id": MGMT, "rate_pct": 140}],
})
check("a rate above 100% is refused", r.status_code == 422, str(r.status_code))

print("\n== 6. Cent rounding is half-up, and the rollups see the derived money ==")
post(SF, None, M, [
    {"category_id": RENT, "amount": 1666.65},
    {"category_id": MGMT, "rate_pct": 3},
    {"category_id": INS, "amount": 100.00},
])
fee, _ = amount_of(SF, None, M, MGMT)
check("3% of 1,666.65 = 49.9995 rounds up to 50.00", cents(fee) == cents(50.00), str(fee))

monthly = client.get(f"/properties/{SF}/monthly?from={M}&to={M}", headers=H).json()
tier = monthly[0]["property_tier"]
check("the derived fee lands in operating expenses like any other line",
      cents(tier["operating_expenses"]) == cents(150.00), str(tier["operating_expenses"]))
check("and NOI is rent minus it", cents(tier["noi"]) == cents(1516.65), str(tier["noi"]))

print("\n== 7. Stating an amount takes the line back to a fixed figure ==")
recs = client.get(f"/records?property_id={SF}&month={M}", headers=H).json()
li_id = next(li["id"] for li in recs[0]["line_items"] if li["category_id"] == MGMT)
r = client.patch(f"/line-items/{li_id}", headers=H, json={"amount": 75.00})
check("PATCHing an amount clears the rate", r.status_code == 200 and r.json()["rate_pct"] is None, r.text[:160])
post(SF, None, M, [
    {"category_id": RENT, "amount": 3000.00},
    {"category_id": MGMT, "amount": 75.00},
    {"category_id": INS, "amount": 100.00},
])
fee, rate = amount_of(SF, None, M, MGMT)
check("doubling rent no longer moves it — it is a typed figure again",
      cents(fee) == cents(75.00) and rate is None, f"{fee} / {rate}")

# The upsert above replaced the record's items wholesale, so re-read the current id.
recs = client.get(f"/records?property_id={SF}&month={M}", headers=H).json()
li_id = next(li["id"] for li in recs[0]["line_items"] if li["category_id"] == MGMT)
r = client.patch(f"/line-items/{li_id}", headers=H, json={"amount": 75.00, "rate_pct": 8})
check("PATCHing both at once is refused", r.status_code == 400, r.text[:160])

# ...and the reverse conversion. The zero amount a client sends alongside the rate is the
# field's default, not a stated figure, so it must not silently win over the rate.
r = client.patch(f"/line-items/{li_id}", headers=H, json={"rate_pct": 6, "amount": 0})
check("PATCHing a rate converts the line back and derives 6% of 3,000 = 180.00",
      r.status_code == 200 and r.json()["rate_pct"] == 6.0 and cents(r.json()["amount"]) == cents(180.00),
      r.text[:160])

print("\n== 8. An import states a figure and wins over a rate ==")
post(SF, None, M2, [{"category_id": RENT, "amount": 2000.00}, {"category_id": MGMT, "rate_pct": 8}])
fee, _ = amount_of(SF, None, M2, MGMT)
check("the rate line starts at 8% of 2,000 = 160.00", cents(fee) == cents(160.00), str(fee))
r = client.post("/import/rows", headers=H, json=[
    {"property_id": SF, "month": str(M2), "category_id": MGMT, "amount": 172.40},
])
check("the import applies", r.status_code in (200, 201) and r.json()["committed"], r.text[:200])
fee, rate = amount_of(SF, None, M2, MGMT)
check("the imported figure stands and the rate is cleared",
      cents(fee) == cents(172.40) and rate is None, f"{fee} / {rate}")
post(SF, None, M2, [{"category_id": RENT, "amount": 4000.00}, {"category_id": MGMT, "amount": 172.40}])
fee, _ = amount_of(SF, None, M2, MGMT)
check("...and it survives a later rent change", cents(fee) == cents(172.40), str(fee))

print("\n== 9. A locked month is closed to rate lines like everything else ==")
client.put("/periods", headers=H, json={"property_id": SF, "month": str(M2), "status": "locked"})
r = client.post("/records", headers=H, json={
    "property_id": SF, "unit_id": None, "month": str(M2),
    "line_items": [{"category_id": MGMT, "rate_pct": 12}],
})
check("posting a rate into a locked month is refused", r.status_code == 423, str(r.status_code))
fee, _ = amount_of(SF, None, M2, MGMT)
check("the locked month's figure is untouched", cents(fee) == cents(172.40), str(fee))

# ---- cleanup ------------------------------------------------------------------------------
cleanup()

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("verify_phase13: ALL CHECKS PASSED")
