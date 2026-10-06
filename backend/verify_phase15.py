"""Verification of periodic tenancies and rent arrears (migrations 0024 + 0025).

Run:  PYTHONPATH=.pydeps python3 verify_phase15.py

Two features, one subject: what a tenant OWES, and whether this app can say it without lying.

Everything before this treated rent as a US fixed-term figure — a start, an end, and a
contractual escalation percentage — and reported the gap between scheduled and collected rent
for ONE period at a time. An English tenancy is neither. It rolls with no end date, its rent
rises in discrete steps on a stated date, and the number the landlord actually chases is a
BALANCE that accrues month after month and is settled when someone pays extra. So the
properties worth proving are the ones where being approximately right is being wrong:

  1. **The rent schedule knows when the rent changed.** On a rolling tenancy, months before
     the last increase are priced at the rent that was actually in force then. Get this wrong
     and a tenant who paid in full every month for two years is reported as being in arrears
     by the size of their own rent rise, backdated across the whole history — the single worst
     failure this feature can have, because it accuses the wrong person.

  2. **Monthly and accumulated reconcile, in both directions.** The per-month movement and the
     running balance are the same arithmetic seen twice, so they must agree to the penny; and
     a tenant who pays double next month must clear the balance BY DOING THAT, with no
     "mark as settled" step for anyone to forget. Paying ahead must read as credit, not as
     zero — a floored balance silently eats a real prepayment.

  3. **Only unpaid rent is arrears.** Four things look like a shortfall and are not: a month
     nobody has entered yet (missing data — we don't know what was collected), an empty unit
     (vacancy loss), a discount the landlord granted (a concession), and a debt that was
     written off. Each one booked as debt would invent money owed, and the first would invent
     it for every un-entered month in the portfolio at once.

  4. **A debt belongs to a tenant, not a door.** The balance resets at each tenancy. Carrying
     it forward would saddle an incoming tenant with their predecessor's arrears and lose the
     real debtor at the same time.

  5. **The dashboard surfaces it without drowning in it.** A standing balance qualifies every
     month it goes unpaid, so the feed must report each debtor once — and must still report
     one at all in a window containing no unit records, because a cumulative balance does not
     stop existing when nobody has entered this month yet.

Self-contained: builds its own property and units, works in months after the seed's range, and
deletes everything it wrote.
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
    return int((Decimal(str(x or 0)) * 100).to_integral_value())


PROP_NAME = "ZZ Arrears Verification House"
# Deliberately after the seed's Jan-2024..Dec-2025 range, so nothing here can perturb a
# fixture any earlier verify script asserts against.
MONTHS = [date(2026, m, 1) for m in range(1, 7)]  # Jan-Jun 2026
RENT_OLD = Decimal("1000")
RENT_NEW = Decimal("1100")
# The rise takes effect in April — three months priced at the old rent, three at the new.
INCREASE_MONTH = MONTHS[3]

# Read through the API, not the table: categories are per-account (migration 0017), so a bare
# "WHERE name = 'Rent'" could hand back another account's row and every record post would 400.
RENT_CAT = next(
    c["id"] for c in client.get("/categories", headers=H).json() if c["name"] == "Rent"
)


def cleanup() -> None:
    """Remove the whole property this script built. Records, units, leases and arrears
    adjustments all cascade from it, so one delete restores the fixture exactly."""
    db.execute(text("DELETE FROM properties WHERE name = :n"), {"n": PROP_NAME})
    db.commit()


cleanup()  # in case a previous run died before its own teardown

PID = client.post(
    "/properties", headers=H, json={"name": PROP_NAME, "type": "multifamily", "address": "1 Test Way"}
).json()["id"]


def make_unit(number: str) -> str:
    return client.post(f"/properties/{PID}/units", headers=H, json={"unit_number": number}).json()["id"]


def post_rent(unit_id: str, month: date, amount, *, is_vacant: bool = False) -> None:
    """One unit-month with a rent line — the ACTUAL collected rent the arrears engine reads
    back out of unit_month_summary."""
    r = client.post(
        "/records",
        headers=H,
        json={
            "property_id": PID,
            "unit_id": unit_id,
            "month": str(month),
            "is_vacant": is_vacant,
            "line_items": [{"category_id": RENT_CAT, "amount": float(amount)}],
        },
    )
    assert r.status_code in (200, 201), r.text


def make_lease(unit_id: str, **fields) -> str:
    body = {
        "tenant_name": "Test Tenant",
        "start_date": "2025-12-01",
        "end_date": None,  # periodic/rolling by default — the case this script is about
        "contract_rent": float(RENT_OLD),
        "status": "active",
    }
    body.update(fields)
    r = client.post(f"/units/{unit_id}/leases", headers=H, json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def ledger(unit_id: str, lease_id: str | None = None) -> dict:
    """One tenancy's ledger block from GET /units/{id}/arrears (the current lease by default)."""
    data = client.get(f"/units/{unit_id}/arrears", headers=H).json()
    lid = lease_id or data["current_lease_id"]
    return next(L for L in data["leases"] if L["lease_id"] == lid)


def roll_row(unit_id: str) -> dict:
    """This unit's rent-roll row over the full test window."""
    data = client.get(
        f"/properties/{PID}/rent-roll",
        headers=H,
        params={"from": str(MONTHS[0]), "to": str(MONTHS[-1])},
    ).json()
    return next(r for r in data["rows"] if r["unit_id"] == unit_id)


print("\n== 1. A rolling tenancy's rent increase prices the months BEFORE it correctly ==")
# The tenant pays exactly what is due throughout: 1,000 for Jan-Mar, 1,100 from April. The
# only thing on file saying the rent used to be 1,000 is `rent_before_increase`.
U_PAID = make_unit("101")
make_lease(
    U_PAID,
    contract_rent=float(RENT_NEW),
    last_rent_increase_date=str(INCREASE_MONTH),
    rent_before_increase=float(RENT_OLD),
)
for m in MONTHS:
    post_rent(U_PAID, m, RENT_NEW if m >= INCREASE_MONTH else RENT_OLD)

L = ledger(U_PAID)
due_by_month = {m["month"]: m["rent_due"] for m in L["months"]}
check(
    "pre-increase months are priced at the rent that was actually in force (1,000)",
    cents(due_by_month[str(MONTHS[0])]) == cents(RENT_OLD),
    str(due_by_month.get(str(MONTHS[0]))),
)
check(
    "post-increase months are priced at the current rent (1,100)",
    cents(due_by_month[str(INCREASE_MONTH)]) == cents(RENT_NEW),
    str(due_by_month.get(str(INCREASE_MONTH))),
)
check(
    "a tenant who paid in full every month owes NOTHING "
    "(the failure this field exists to prevent: 300 of backdated phantom debt)",
    cents(L["closing_balance"]) == 0,
    str(L["closing_balance"]),
)
row = roll_row(U_PAID)
check(
    "the rent roll reports the increase date and the prior rent",
    row["last_rent_increase_date"] == str(INCREASE_MONTH)
    and cents(row["rent_before_increase"]) == cents(RENT_OLD),
    f"{row['last_rent_increase_date']} / {row['rent_before_increase']}",
)
check("...and the tenancy reads as periodic, with no expiry horizon",
      row["lease_type"] == "mtm" and row["lease_end"] is None and row["months_to_expiry"] is None,
      f"{row['lease_type']} / {row['lease_end']} / {row['months_to_expiry']}")

# Without the prior figure, the same payments must be reported as a shortfall — otherwise the
# field above is doing nothing and check 1 proves nothing.
lid = row["lease_id"]
client.patch(
    f"/leases/{lid}",
    headers=H,
    json={
        "tenant_name": "Test Tenant", "start_date": "2025-12-01", "end_date": None,
        "contract_rent": float(RENT_NEW), "status": "active",
        "last_rent_increase_date": str(INCREASE_MONTH), "rent_before_increase": None,
    },
)
check(
    "dropping the prior rent DOES produce the 300 phantom debt (so the field is load-bearing)",
    cents(ledger(U_PAID)["closing_balance"]) == cents(Decimal("300")),
    str(ledger(U_PAID)["closing_balance"]),
)
client.patch(
    f"/leases/{lid}",
    headers=H,
    json={
        "tenant_name": "Test Tenant", "start_date": "2025-12-01", "end_date": None,
        "contract_rent": float(RENT_NEW), "status": "active",
        "last_rent_increase_date": str(INCREASE_MONTH), "rent_before_increase": float(RENT_OLD),
    },
)

print("\n== 2. Monthly movement and the accumulated balance are the same arithmetic ==")
# Pays in full in January, short by 400 in February, nothing at all in March, then catches the
# whole thing up in April.
U_CATCH = make_unit("102")
make_lease(U_CATCH, tenant_name="Catch Up")
paid = [RENT_OLD, Decimal("600"), Decimal("0"), Decimal("2400")]
for m, amount in zip(MONTHS, paid):
    post_rent(U_CATCH, m, amount)

L = ledger(U_CATCH)
mv = {m["month"]: m for m in L["months"]}
check("a month paid in full moves the balance by nothing",
      cents(mv[str(MONTHS[0])]["movement"]) == 0, str(mv[str(MONTHS[0])]["movement"]))
check("a 400 shortfall moves it +400 and the balance to 400",
      cents(mv[str(MONTHS[1])]["movement"]) == cents(400)
      and cents(mv[str(MONTHS[1])]["balance"]) == cents(400),
      str(mv[str(MONTHS[1])]))
check("a month paid in FULL ARREARS (0 collected) takes the balance to 1,400",
      cents(mv[str(MONTHS[2])]["balance"]) == cents(1400), str(mv[str(MONTHS[2])]["balance"]))
check("paying 2,400 against 1,000 due clears the balance BY ITSELF — no settle step",
      cents(mv[str(MONTHS[3])]["movement"]) == cents(-1400)
      and cents(mv[str(MONTHS[3])]["balance"]) == 0,
      str(mv[str(MONTHS[3])]))
check("the ledger's movements sum to its closing balance",
      cents(sum(m["movement"] for m in L["months"])) == cents(L["closing_balance"]))

row = roll_row(U_CATCH)
check("the rent roll's monthly and accumulated figures reconcile exactly "
      "(opening + movement == balance)",
      cents(row["arrears_opening_balance"]) + cents(row["arrears_movement"])
      == cents(row["arrears_balance"]))
# A window that stops INSIDE the debt must show it, and must attribute what came before it.
mid = client.get(
    f"/properties/{PID}/rent-roll", headers=H,
    params={"from": str(MONTHS[2]), "to": str(MONTHS[2])},
).json()
mid_row = next(r for r in mid["rows"] if r["unit_id"] == U_CATCH)
check("a single-month window reports the balance AS AT that month, not the latest one",
      cents(mid_row["arrears_balance"]) == cents(1400), str(mid_row["arrears_balance"]))
check("...with the 400 it was already carrying attributed to before the window",
      cents(mid_row["arrears_opening_balance"]) == cents(400),
      str(mid_row["arrears_opening_balance"]))

print("\n== 3. Paying ahead is CREDIT, not zero ==")
U_CREDIT = make_unit("103")
make_lease(U_CREDIT, tenant_name="Pays Ahead")
post_rent(U_CREDIT, MONTHS[0], RENT_OLD * 3)  # three months up front
post_rent(U_CREDIT, MONTHS[1], Decimal("0"))
L = ledger(U_CREDIT)
check("a prepayment leaves a negative (credit) balance rather than a floored zero",
      cents(L["closing_balance"]) == cents(-1000), str(L["closing_balance"]))
check("and the following unpaid month draws the credit down instead of accruing debt",
      cents(L["months"][1]["balance"]) == cents(-1000), str(L["months"][1]["balance"]))

print("\n== 4. Four things that look like a shortfall and are not ==")
# (a) MISSING DATA. Rent is due but no record exists — we don't know what was collected.
U_GAP = make_unit("104")
make_lease(U_GAP, tenant_name="Not Entered Yet")
post_rent(U_GAP, MONTHS[0], RENT_OLD)
# ...MONTHS[1] deliberately never entered...
post_rent(U_GAP, MONTHS[2], RENT_OLD)
L = ledger(U_GAP)
check("a month with NO record accrues nothing — missing data is not debt",
      cents(L["closing_balance"]) == 0 and len(L["months"]) == 2,
      f"balance {L['closing_balance']}, {len(L['months'])} months")
check("...and the un-entered month is absent from the ledger entirely",
      str(MONTHS[1]) not in {m["month"] for m in L["months"]})

# (b) VACANCY. An explicitly vacant month has no tenant to owe anything.
U_VAC = make_unit("105")
make_lease(U_VAC, tenant_name="Moved Out")
post_rent(U_VAC, MONTHS[0], RENT_OLD)
post_rent(U_VAC, MONTHS[1], Decimal("0"), is_vacant=True)
check("an explicitly vacant month accrues nothing — that gap is vacancy loss",
      cents(ledger(U_VAC)["closing_balance"]) == 0, str(ledger(U_VAC)["closing_balance"]))

# ...but £0 collected WITHOUT the vacant flag is the most important arrears case there is, and
# must never be silently reclassified as vacancy.
U_ZERO = make_unit("106")
make_lease(U_ZERO, tenant_name="Stopped Paying")
post_rent(U_ZERO, MONTHS[0], RENT_OLD)
post_rent(U_ZERO, MONTHS[1], Decimal("0"))
check("0 collected on an OCCUPIED month is full arrears, not inferred vacancy",
      cents(ledger(U_ZERO)["closing_balance"]) == cents(RENT_OLD),
      str(ledger(U_ZERO)["closing_balance"]))

# (c) CONCESSION. A discount the landlord granted is not rent the tenant failed to pay.
U_CONC = make_unit("107")
make_lease(U_CONC, tenant_name="Discounted", concession_monthly=150.0)
for m in MONTHS[:3]:
    post_rent(U_CONC, m, RENT_OLD - Decimal("150"))
L = ledger(U_CONC)
check("a tenant paying rent-less-concession owes nothing",
      cents(L["closing_balance"]) == 0, str(L["closing_balance"]))
check("...and the concession is reported rather than hidden inside rent_due",
      cents(L["total_concessions"]) == cents(450)
      and cents(L["total_rent_due"]) == cents(2550),
      f"conc {L['total_concessions']}, due {L['total_rent_due']}")

# (d) A WRITE-OFF. Forgiven arrears must stop being reported as owing.
U_WO = make_unit("108")
wo_lease = make_lease(U_WO, tenant_name="Written Off")
post_rent(U_WO, MONTHS[0], Decimal("0"))
check("the debt is there to begin with",
      cents(ledger(U_WO)["closing_balance"]) == cents(RENT_OLD))
r = client.post(
    f"/leases/{wo_lease}/arrears-adjustments",
    headers=H,
    json={"month": str(MONTHS[1]), "amount": -1000.0, "kind": "write_off",
          "note": "recovered from deposit at check-out"},
)
check("a write-off posts", r.status_code == 201, r.text[:160])
L = ledger(U_WO)
check("...and clears the balance without touching the rent schedule",
      cents(L["closing_balance"]) == 0 and cents(L["total_rent_due"]) == cents(RENT_OLD),
      f"balance {L['closing_balance']}, due {L['total_rent_due']}")
check("the write-off's own month appears in the ledger even with no rent record there",
      any(m["month"] == str(MONTHS[1]) and not m["has_record"] for m in L["months"]))

print("\n== 5. A debt belongs to a tenant, not a door ==")
U_TURN = make_unit("109")
old_lease = make_lease(
    U_TURN, tenant_name="Left Owing", start_date="2025-12-01", end_date=str(MONTHS[1])
)
post_rent(U_TURN, MONTHS[0], Decimal("0"))  # owes a full month
post_rent(U_TURN, MONTHS[1], RENT_OLD)
new_lease = make_lease(U_TURN, tenant_name="New Tenant", start_date=str(MONTHS[2]))
for m in MONTHS[2:4]:
    post_rent(U_TURN, m, RENT_OLD)

data = client.get(f"/units/{U_TURN}/arrears", headers=H).json()
by_lease = {L["lease_id"]: L for L in data["leases"]}
check("the outgoing tenant's debt stays on their own tenancy",
      cents(by_lease[old_lease]["closing_balance"]) == cents(RENT_OLD),
      str(by_lease[old_lease]["closing_balance"]))
check("the incoming tenant starts at zero and is not charged for it",
      cents(by_lease[new_lease]["closing_balance"]) == 0,
      str(by_lease[new_lease]["closing_balance"]))
check("the ledger reports BOTH tenancies, so the real debtor isn't lost",
      len(data["leases"]) == 2 and data["current_lease_id"] == new_lease)
check("the rent roll's row shows the CURRENT tenancy's balance (0), not the door's history",
      cents(roll_row(U_TURN)["arrears_balance"]) == 0,
      str(roll_row(U_TURN)["arrears_balance"]))

print("\n== 6. A balance brought forward from before the records ==")
U_OPEN = make_unit("110")
make_lease(U_OPEN, tenant_name="Arrived Owing", opening_arrears=750.0)
post_rent(U_OPEN, MONTHS[0], RENT_OLD)  # pays correctly from day one
L = ledger(U_OPEN)
check("opening arrears carries, even though every entered month was paid in full",
      cents(L["opening_arrears"]) == cents(750) and cents(L["closing_balance"]) == cents(750),
      f"{L['opening_arrears']} / {L['closing_balance']}")
check("...and it is NOT double-counted as a shortfall in the first month",
      cents(L["months"][0]["movement"]) == 0, str(L["months"][0]["movement"]))

print("\n== 7. Incoherent input is refused, not stored ==")
cases = [
    ("a write-off that INCREASES the balance",
     {"month": str(MONTHS[0]), "amount": 100.0, "kind": "write_off"}),
    ("a charge that reduces it",
     {"month": str(MONTHS[0]), "amount": -100.0, "kind": "charge"}),
    ("a zero adjustment (a row claiming something happened while saying nothing)",
     {"month": str(MONTHS[0]), "amount": 0.0, "kind": "correction"}),
    ("an adjustment dated mid-month",
     {"month": "2026-01-15", "amount": -100.0, "kind": "correction"}),
    ("an unrecognised kind",
     {"month": str(MONTHS[0]), "amount": -100.0, "kind": "forgiven"}),
]
for label, body in cases:
    r = client.post(f"/leases/{wo_lease}/arrears-adjustments", headers=H, json=body)
    check(f"{label} is refused", r.status_code in (400, 422), str(r.status_code))

base = {"tenant_name": "X", "start_date": "2025-12-01", "end_date": None,
        "contract_rent": 1000.0, "status": "active"}
bad_leases = [
    ("a prior rent with no increase date to place it at",
     {**base, "rent_before_increase": 900.0}),
    ("a rent increase dated before the tenancy began",
     {**base, "last_rent_increase_date": "2025-01-01"}),
    ("a rent increase dated after the tenancy ended",
     {**base, "end_date": "2026-03-31", "last_rent_increase_date": "2026-06-01"}),
]
for label, body in bad_leases:
    r = client.post(f"/units/{U_OPEN}/leases", headers=H, json=body)
    check(f"{label} is refused", r.status_code in (400, 422), str(r.status_code))

print("\n== 8. A correction may go either way — the honest escape hatch ==")
r = client.post(
    f"/leases/{wo_lease}/arrears-adjustments", headers=H,
    json={"month": str(MONTHS[2]), "amount": 250.0, "kind": "charge", "note": "court costs"},
)
check("a non-rent charge posts and raises the balance",
      r.status_code == 201 and cents(ledger(U_WO)["closing_balance"]) == cents(250),
      f"{r.status_code} / {ledger(U_WO)['closing_balance']}")
adj_id = r.json()["id"]
r = client.patch(f"/arrears-adjustments/{adj_id}", headers=H,
                 json={"month": str(MONTHS[2]), "amount": -50.0, "kind": "correction"})
check("a correction may be negative where a charge may not",
      r.status_code == 200 and cents(ledger(U_WO)["closing_balance"]) == cents(-50),
      f"{r.status_code} / {ledger(U_WO)['closing_balance']}")
check("deleting an adjustment reverts the balance it was moving",
      client.delete(f"/arrears-adjustments/{adj_id}", headers=H).status_code == 204
      and cents(ledger(U_WO)["closing_balance"]) == 0,
      str(ledger(U_WO)["closing_balance"]))

print("\n== 9. The rollup answers exposure and headcount separately ==")
data = client.get(
    f"/properties/{PID}/rent-roll", headers=H,
    params={"from": str(MONTHS[0]), "to": str(MONTHS[-1])},
).json()
roll = data["arrears"]
rows = data["rows"]
owing = [r for r in rows if (r["arrears_balance"] or 0) > 0]
credit = [r for r in rows if (r["arrears_balance"] or 0) < 0]
check("units_in_arrears counts the doors actually owing",
      roll["units_in_arrears"] == len(owing), f"{roll['units_in_arrears']} vs {len(owing)}")
check("units_in_credit is reported separately rather than folded in",
      roll["units_in_credit"] == len(credit), f"{roll['units_in_credit']} vs {len(credit)}")
check("total_balance is the NET position, credits included",
      cents(roll["total_balance"]) == cents(sum(r["arrears_balance"] or 0 for r in rows)),
      str(roll["total_balance"]))
check("largest_balance is the worst single tenancy",
      cents(roll["largest_balance"]) == cents(max(r["arrears_balance"] or 0 for r in rows)),
      str(roll["largest_balance"]))
check("a unit with no lease on file reports 0 owed, not null — nothing owed is a fact",
      all(r["arrears_balance"] == 0 for r in rows if r["lease_id"] is None))

print("\n== 10. The attention feed names each debtor once, and does not lose standing debt ==")
feed = client.get(
    f"/properties/{PID}/attention", headers=H,
    params={"from": str(MONTHS[0]), "to": str(MONTHS[-1])},
).json()
arr = [i for i in feed["items"] if i["type"] == "arrears"]
named = [i for i in arr if not i["rolled_up"]]
check("the feed raises arrears items for this property", len(arr) > 0, str(len(arr)))
check("each unit appears at most once, however many months it was over threshold",
      len({i["unit_id"] for i in named}) == len(named), str(len(named)))
check("every named item carries the door and the tenant — what you need to act",
      all(i["unit_number"] and i["detail"].get("tenant_name") for i in named))
check("the balance is the ranking magnitude, and the movement into it is the change",
      all(i["magnitude"] == i["current"] and i["change"] is not None for i in named))
check("a tenant in CREDIT is never flagged",
      not any(i["magnitude"] <= 0 for i in arr), str([i["magnitude"] for i in arr]))

# The real regression risk: a window containing no unit records at all. The property rollups
# can run past the unit ones (a shared expense posted forward), and a balance of 1,000 is
# still owed in a month nobody has entered.
far = client.get(
    f"/properties/{PID}/attention", headers=H,
    params={"from": "2026-11-01", "to": "2026-11-01"},
).json()
far_arr = [i for i in far["items"] if i["type"] == "arrears"]
check("a window with no unit records still reports the standing debt",
      len(far_arr) > 0, str(len(far_arr)))
check("...flagged as carried forward, dated to the month the debt last moved",
      all(i["detail"]["carried_forward"] and i["month"] <= "2026-06-01" for i in far_arr),
      str([(i["month"], i["detail"]["carried_forward"]) for i in far_arr]))

print("\n== 11. The rent schedule is editable from the property page, and agrees everywhere ==")
# The three figures an operator changes most (start date / rent / last increase) are editable on
# the property page as well as the rent roll. The hazard that makes this worth a test: PATCH
# /leases/{id} is a FULL REPLACE, so a compact three-field editor that posted only its own
# fields would wipe the tenant, the deposit, the discount and the brought-forward arrears every
# time someone corrected a rent. `TenancyEditor` loads the whole tenancy and merges; this proves
# the merge, and proves all three screens then read the same stored record.
U_RICH = make_unit("111")
rich_lease = make_lease(
    U_RICH, tenant_name="Rich Record", contract_rent=float(RENT_OLD),
    security_deposit=1200.0, concession_monthly=25.0, opening_arrears=300.0,
)
post_rent(U_RICH, MONTHS[0], RENT_OLD)
stored = next(l for l in client.get(f"/units/{U_RICH}/leases", headers=H).json() if l["id"] == rich_lease)

# Exactly the body TenancyEditor builds: every stored field, with its own four overwritten.
merged = {
    "tenant_name": stored["tenant_name"], "status": stored["status"], "end_date": stored["end_date"],
    "security_deposit": stored["security_deposit"], "escalation_pct": stored["escalation_pct"],
    "escalation_frequency_months": stored["escalation_frequency_months"],
    "pct_rent_rate": stored["pct_rent_rate"], "pct_rent_breakpoint": stored["pct_rent_breakpoint"],
    "concession_monthly": stored["concession_monthly"], "opening_arrears": stored["opening_arrears"],
    "start_date": stored["start_date"],
    "contract_rent": float(RENT_NEW),
    "last_rent_increase_date": str(INCREASE_MONTH),
    "rent_before_increase": float(RENT_OLD),
}
r = client.patch(f"/leases/{rich_lease}", headers=H, json=merged)
saved = r.json()
check("a rent edit from the property page applies", r.status_code == 200
      and cents(saved["contract_rent"]) == cents(RENT_NEW), r.text[:160])
check("...and preserves every field the compact editor doesn't show "
      "(tenant, deposit, discount, opening arrears, term)",
      saved["tenant_name"] == stored["tenant_name"]
      and cents(saved["security_deposit"]) == cents(stored["security_deposit"])
      and cents(saved["concession_monthly"]) == cents(stored["concession_monthly"])
      and cents(saved["opening_arrears"]) == cents(stored["opening_arrears"])
      and saved["end_date"] == stored["end_date"],
      str({k: saved[k] for k in ("tenant_name", "security_deposit", "concession_monthly", "opening_arrears")}))

roster = client.get(
    f"/properties/{PID}/units/roster", headers=H, params={"month": str(MONTHS[0]), "limit": 200}
).json()
r_row = next(x for x in roster["rows"] if x["unit_id"] == U_RICH)
rr_row = roll_row(U_RICH)
check("the property page's roster carries the schedule it just edited",
      cents(r_row["lease_contract_rent"]) == cents(RENT_NEW)
      and r_row["last_rent_increase_date"] == str(INCREASE_MONTH)
      and r_row["lease_start"] == stored["start_date"],
      str({k: r_row[k] for k in ("lease_contract_rent", "last_rent_increase_date", "lease_start")}))
check("the rent roll reports the identical figures — one stored record, two screens",
      cents(rr_row["contract_rent"]) == cents(r_row["lease_contract_rent"])
      and rr_row["last_rent_increase_date"] == r_row["last_rent_increase_date"]
      and rr_row["months_since_last_increase"] == r_row["months_since_last_increase"],
      f"{rr_row['contract_rent']}/{r_row['lease_contract_rent']}")
# ...and the arrears ledger prices off the edit immediately, since nothing is cached.
L = ledger(U_RICH)
jan = next(m for m in L["months"] if m["month"] == str(MONTHS[0]))
check("the arrears ledger prices the edited schedule at once (pre-increase month, net of discount)",
      cents(jan["rent_due"]) == cents(RENT_OLD - Decimal("25")), str(jan))

print("\n== 12. A single-let property's one unit is a full citizen of the roster ==")
# The roster used to be gated on `type === "multifamily"`, which left a portfolio of houses with
# no way to see or edit a tenancy from the property page at all.
single_pid = client.post(
    "/properties", headers=H, json={"name": PROP_NAME + " (single)", "type": "single"}
).json()["id"]
single_unit = client.post(
    f"/properties/{single_pid}/units", headers=H, json={"unit_number": "1"}
).json()["id"]
client.post(f"/units/{single_unit}/leases", headers=H, json={
    "tenant_name": None, "start_date": "2025-12-01", "end_date": None,
    "contract_rent": float(RENT_OLD), "status": "active",
})
check("a SECOND unit on a single-let is still refused (the type still means something)",
      client.post(f"/properties/{single_pid}/units", headers=H, json={"unit_number": "2"}).status_code
      == 409)
single_roster = client.get(
    f"/properties/{single_pid}/units/roster", headers=H, params={"month": str(MONTHS[0])}
).json()
check("a 'single' property's roster returns its one unit with the tenancy attached",
      single_roster["total"] == 1 and single_roster["rows"][0]["lease_id"] is not None
      and cents(single_roster["rows"][0]["lease_contract_rent"]) == cents(RENT_OLD),
      str(single_roster["rows"][:1]))
check("a tenancy with NO tenant name on file is accepted and reads back as null",
      single_roster["rows"][0]["lease_tenant_name"] is None,
      str(single_roster["rows"][0]["lease_tenant_name"]))
db.execute(text("DELETE FROM properties WHERE name = :n"), {"n": PROP_NAME + " (single)"})
db.commit()

print("\n== 13. A handover month is excluded from arrears, not reported as the tenant's debt ==")
# When a property changes hands the rent in hand is apportioned at completion, and an English
# tenancy is rarely paid on the 1st — so the new owner's first collection is a part period.
# Measured against a full month's rent that is indistinguishable from a tenant who underpaid.
U_HAND = make_unit("112")
hand_lease = make_lease(U_HAND, tenant_name="Bought Mid-Cycle")
post_rent(U_HAND, MONTHS[0], Decimal("278.80"))  # the apportioned handover month
for m in MONTHS[1:3]:
    post_rent(U_HAND, m, RENT_OLD)               # ...then paying in full
check("without an arrears start, the handover month reads as a 721.20 debt",
      cents(ledger(U_HAND)["closing_balance"]) == cents(Decimal("721.20")),
      str(ledger(U_HAND)["closing_balance"]))

base = {
    "tenant_name": "Bought Mid-Cycle", "start_date": "2025-12-01", "end_date": None,
    "contract_rent": float(RENT_OLD), "status": "active",
}
r = client.patch(f"/leases/{hand_lease}", headers=H,
                 json={**base, "arrears_from_month": str(MONTHS[1])})
check("setting an arrears start applies", r.status_code == 200
      and r.json()["arrears_from_month"] == str(MONTHS[1]), r.text[:160])
L = ledger(U_HAND)
check("...and the handover month leaves the ledger entirely — not 'paid', not 'owed'",
      str(MONTHS[0]) not in {m["month"] for m in L["months"]},
      str([m["month"] for m in L["months"]]))
check("...leaving a tenant who paid in full owing nothing",
      cents(L["closing_balance"]) == 0, str(L["closing_balance"]))
check("the ledger says which month it measures from, so an excluded period is visible",
      L["arrears_from_month"] == str(MONTHS[1]), str(L["arrears_from_month"]))
check("the rent roll echoes it on the row",
      roll_row(U_HAND)["arrears_from_month"] == str(MONTHS[1]),
      str(roll_row(U_HAND)["arrears_from_month"]))

# A REAL shortfall after the start month must still be caught — the exclusion must not become a
# blanket amnesty.
post_rent(U_HAND, MONTHS[3], Decimal("400"))
check("a genuine shortfall AFTER the start month is still reported",
      cents(ledger(U_HAND)["closing_balance"]) == cents(RENT_OLD - Decimal("400")),
      str(ledger(U_HAND)["closing_balance"]))
# ...and an adjustment can't smuggle an excluded month back in.
client.post(f"/leases/{hand_lease}/arrears-adjustments", headers=H,
            json={"month": str(MONTHS[0]), "amount": 250.0, "kind": "charge"})
check("an adjustment dated into an excluded month does not reopen it",
      str(MONTHS[0]) not in {m["month"] for m in ledger(U_HAND)["months"]})
check("a mid-month arrears start is refused",
      client.patch(f"/leases/{hand_lease}", headers=H,
                   json={**base, "arrears_from_month": "2026-02-15"}).status_code == 400)

print("\n== 14. The portfolio waterfall counts each property once ==")
# A 'single' property with no units books rent at the property tier and is folded into the
# waterfall as a pass-through leg. Once it HAS a unit, the unit path already counts that rent —
# counting it both ways listed every property twice AND double-counted the portfolio totals.
wf = client.get("/portfolio/rent-waterfall", headers=H).json()
ids = [p["property_id"] for p in wf["properties"]]
check("no property appears twice in the per-property breakdown",
      len(ids) == len(set(ids)), f"{len(ids)} rows, {len(set(ids))} distinct")
check("the waterfall identity still balances to the cent",
      abs(wf["residual"]) < 0.01, str(wf["residual"]))

print("\n== 15. Arrears never touches NOI or cash flow ==")
before = db.execute(
    text("SELECT noi, cash_flow FROM property_month_summary WHERE property_id = :p AND month = :m"),
    {"p": PID, "m": MONTHS[0]},
).mappings().first()
client.post(
    f"/leases/{wo_lease}/arrears-adjustments", headers=H,
    json={"month": str(MONTHS[0]), "amount": 5000.0, "kind": "charge", "note": "should not hit the P&L"},
)
after = db.execute(
    text("SELECT noi, cash_flow FROM property_month_summary WHERE property_id = :p AND month = :m"),
    {"p": PID, "m": MONTHS[0]},
).mappings().first()
check("a 5,000 arrears charge moves neither NOI nor cash flow",
      cents(before["noi"]) == cents(after["noi"])
      and cents(before["cash_flow"]) == cents(after["cash_flow"]),
      f"{dict(before)} -> {dict(after)}")

# ---- cleanup ------------------------------------------------------------------------------
cleanup()

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("verify_phase15: ALL CHECKS PASSED")
