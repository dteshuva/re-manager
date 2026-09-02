"""Verification of shared expenses — one bill covering several properties, split monthly.

Run:  PYTHONPATH=.pydeps python3 verify_phase12.py

Checks the two properties this feature exists to guarantee:

  1. **The shares sum back to the bill, to the cent, every month.** That is the whole point of
     entering a portfolio loan payment or a blanket insurance policy once instead of dividing
     it by hand: the per-property line items must add up to the real bill, not to a rounding
     of it, or the portfolio P&L quietly stops matching the bank statement.

  2. **A bulk write never destroys a figure someone entered by hand.** Posting touches many
     property-months at once, so it must detect a pre-existing line rather than overwrite it,
     honour period locks, and — on un-post — remove only the lines it actually posted.

Also covers the allocation bases (equal / price / units / custom), idempotent re-posting,
month-range posting, the property-tier placement that keeps the money out of unit-level
figures, cross-account isolation, and the fact that a property NOT in the arrangement is
completely untouched.

Self-contained: creates its own arrangements over seed properties, asserts, then removes
everything it wrote and restores any acquisition rows it planted.
"""
import uuid as _uuid
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

EPS = 1e-9
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        failures.append(f"{label}{': ' + detail if detail else ''}")
        print(f"  FAIL  {label} {detail}")
    else:
        print(f"  ok    {label}")


def _pid(name: str) -> str:
    pid = db.scalar(text("SELECT id::text FROM properties WHERE name = :n"), {"n": name})
    assert pid, f"seed fixture missing: {name}"
    return pid


def _cat(name: str) -> str:
    cid = db.scalar(text("SELECT id::text FROM categories WHERE name = :n"), {"n": name})
    assert cid, f"seed fixture missing category: {name}"
    return cid


def cents(x) -> int:
    return int((Decimal(str(x)) * 100).to_integral_value())


# Three members with deliberately different shapes: two multifamily of unequal size, and a
# unit-less "single" house — the case a units-weighted split would silently zero out.
A, B, C = _pid("Oak Ridge Residences"), _pid("Ash Grove Apartments"), _pid("Birch Street House")
OUTSIDER = _pid("Willow Creek Towers")
MORTGAGE, INSURANCE = _cat("Mortgage"), _cat("Insurance")

# 10,000.00 over 3 does NOT divide evenly, so the leftover-cent handling is exercised rather
# than getting lucky on a round number.
BILL = 10000.00
# Deliberately AFTER the seed's Jan-2024..Dec-2025 range: these property-months start empty,
# so the arrangement is the only thing writing there (and the "create the property-tier record
# from scratch" path is exercised rather than reusing a seeded one).
MONTH = date(2026, 3, 1)
RANGE_FROM, RANGE_MID, RANGE_TO = date(2026, 4, 1), date(2026, 5, 1), date(2026, 6, 1)
TEST_MONTHS = [MONTH, RANGE_FROM, RANGE_MID, RANGE_TO]
created_expenses: list[str] = []


def body(**over) -> dict:
    payload = {
        "name": "Blanket loan - debt service",
        "category_id": MORTGAGE,
        "amount": BILL,
        "allocation_method": "equal",
        "members": [{"property_id": A}, {"property_id": B}, {"property_id": C}],
    }
    payload.update(over)
    return payload


def create(**over) -> dict:
    r = client.post("/shared-expenses", headers=H, json=body(**over))
    assert r.status_code == 201, r.text
    created_expenses.append(r.json()["id"])
    return r.json()


def cleanup() -> None:
    for eid in list(created_expenses):
        client.delete(f"/shared-expenses/{eid}?purge=true", headers=H)
    db.execute(
        text("DELETE FROM property_investment WHERE property_id = ANY(CAST(:ids AS uuid[]))"),
        {"ids": [A, B, C]},
    )
    # Every record and status row in these months was created by this script (they post-date
    # the seed), so removing them restores the fixture exactly. Line items cascade.
    db.execute(
        text("DELETE FROM monthly_records WHERE month = ANY(CAST(:ms AS date[])) "
             "AND property_id = ANY(CAST(:ids AS uuid[]))"),
        {"ms": TEST_MONTHS, "ids": [A, B, C]},
    )
    db.execute(
        text("DELETE FROM period_status WHERE month = ANY(CAST(:ms AS date[])) "
             "AND property_id = ANY(CAST(:ids AS uuid[]))"),
        {"ms": TEST_MONTHS, "ids": [A, B, C]},
    )
    db.commit()


print("\n== 1. The split sums to the bill, to the cent ==")
exp = create()
shares = [m["amount"] for m in exp["members"]]
check("equal split sums to the bill", sum(cents(s) for s in shares) == cents(BILL),
      f"{shares} -> {sum(shares)}")
check("allocated_amount agrees", cents(exp["allocated_amount"]) == cents(BILL))
check("no share is more than a cent from an even third",
      all(abs(cents(s) - cents(BILL) // 3) <= 1 for s in shares), str(shares))
check("three members recorded", exp["property_count"] == 3)

# The pathological case: a bill that cannot be divided three ways at all.
r = client.post("/shared-expenses/split", headers=H, json=body(amount=0.01))
penny = [m["amount"] for m in r.json()["members"]]
check("a one-cent bill still sums exactly", sum(cents(p) for p in penny) == 1, str(penny))

print("\n== 2. Allocation bases ==")
# price: weight by purchase price. The seed has no acquisition rows, so plant them.
for pid, price in ((A, 600000), (B, 300000), (C, 100000)):
    db.execute(
        text(
            "INSERT INTO property_investment (property_id, purchase_price, purchase_date) "
            "VALUES (CAST(:p AS uuid), :v, DATE '2020-01-01') ON CONFLICT (property_id) "
            "DO UPDATE SET purchase_price = EXCLUDED.purchase_price"
        ),
        {"p": pid, "v": price},
    )
db.commit()
r = client.post("/shared-expenses/split", headers=H, json=body(allocation_method="price"))
by_name = {m["property_name"]: m for m in r.json()["members"]}
check("price basis is pro-rata (6:3:1 of 10,000)",
      cents(by_name["Oak Ridge Residences"]["amount"]) == 600000
      and cents(by_name["Ash Grove Apartments"]["amount"]) == 300000
      and cents(by_name["Birch Street House"]["amount"]) == 100000,
      str({k: v["amount"] for k, v in by_name.items()}))
check("price split still sums to the bill",
      sum(cents(m["amount"]) for m in r.json()["members"]) == cents(BILL))

r = client.post("/shared-expenses/split", headers=H, json=body(allocation_method="units"))
by_name = {m["property_name"]: m for m in r.json()["members"]}
# Oak Ridge 62 doors, Ash Grove 40, and the unit-less house counts as ONE dwelling — never
# zero, which would have excluded every single-family property from a units-based split.
check("units basis counts a unit-less house as one dwelling",
      by_name["Birch Street House"]["basis"] == 1.0, str(by_name["Birch Street House"]))
check("units basis reads real door counts",
      by_name["Oak Ridge Residences"]["basis"] == 62.0
      and by_name["Ash Grove Apartments"]["basis"] == 40.0,
      str({k: v["basis"] for k, v in by_name.items()}))
check("units split still sums to the bill",
      sum(cents(m["amount"]) for m in r.json()["members"]) == cents(BILL))
check("units split is ordered by door count",
      by_name["Oak Ridge Residences"]["amount"] > by_name["Ash Grove Apartments"]["amount"]
      > by_name["Birch Street House"]["amount"])

# custom: the stated total is IGNORED and recomputed from the shares, so a stored arrangement
# can never claim a total its own split contradicts.
custom = create(
    name="Insurer's per-building premium",
    allocation_method="custom",
    amount=999999.00,  # deliberately wrong; must be overridden by the sum of the shares
    members=[
        {"property_id": A, "custom_share": 500.00},
        {"property_id": B, "custom_share": 250.50},
        {"property_id": C, "custom_share": 125.25},
    ],
)
check("custom total is the sum of the shares, not the submitted amount",
      cents(custom["amount"]) == cents(875.75), str(custom["amount"]))

r = client.post("/shared-expenses", headers=H, json=body(
    allocation_method="custom",
    members=[{"property_id": A, "custom_share": 10}, {"property_id": B}, {"property_id": C}],
))
check("custom rejects a partially-filled split", r.status_code == 400, r.text[:120])

r = client.post("/shared-expenses", headers=H, json=body(members=[{"property_id": A}]))
check("a one-property 'shared' expense is rejected", r.status_code == 422, str(r.status_code))

r = client.post("/shared-expenses", headers=H, json=body(
    members=[{"property_id": A}, {"property_id": A}, {"property_id": B}]))
check("a duplicated member is rejected", r.status_code == 400, str(r.status_code))

print("\n== 3. Posting writes real, property-tier line items ==")
eid = exp["id"]
before = db.scalar(
    text(
        "SELECT COALESCE(sum(li.amount), 0) FROM line_items li "
        "JOIN monthly_records mr ON mr.id = li.monthly_record_id "
        "WHERE mr.property_id = :p AND mr.month = :m"
    ),
    {"p": OUTSIDER, "m": MONTH},
)

r = client.post(f"/shared-expenses/{eid}/post/preview", headers=H,
                json={"from_month": str(MONTH)})
plan = r.json()
check("preview plans one row per member", len(plan["rows"]) == 3, str(len(plan["rows"])))
check("preview is not blocked on a clean month", plan["blocked"] is False, str(plan)[:200])

r = client.post(f"/shared-expenses/{eid}/post", headers=H, json={"from_month": str(MONTH)})
check("post succeeds", r.status_code == 200, r.text[:200])
res = r.json()
check("post wrote one line item per member", res["line_items_written"] == 3, str(res)[:160])
check("post created the missing property-tier records", res["records_created"] == 3,
      "a member with no record for the month must get one, not be silently skipped")
check("posted total is the bill", cents(res["total_posted"]) == cents(BILL), str(res["total_posted"]))

rows = db.execute(
    text(
        "SELECT mr.property_id::text AS pid, mr.unit_id, li.amount, "
        "       li.shared_expense_id::text AS eid "
        "FROM line_items li JOIN monthly_records mr ON mr.id = li.monthly_record_id "
        "WHERE li.shared_expense_id = :e"
    ),
    {"e": eid},
).mappings().all()
check("the posted line items sum to the bill in the DATABASE",
      sum(cents(r["amount"]) for r in rows) == cents(BILL),
      str([str(r["amount"]) for r in rows]))
check("every posting is property-tier (unit_id IS NULL)",
      all(r["unit_id"] is None for r in rows),
      "a shared cost allocated down to units would corrupt unit-level P&L")
check("every posting carries its provenance", all(r["eid"] == eid for r in rows))
check("members are exactly the three properties", {r["pid"] for r in rows} == {A, B, C})

after = db.scalar(
    text(
        "SELECT COALESCE(sum(li.amount), 0) FROM line_items li "
        "JOIN monthly_records mr ON mr.id = li.monthly_record_id "
        "WHERE mr.property_id = :p AND mr.month = :m"
    ),
    {"p": OUTSIDER, "m": MONTH},
)
check("a property outside the arrangement is untouched", before == after, f"{before} -> {after}")

# The money must actually reach the P&L the app reports, not just the line_items table.
pnl = client.get(f"/properties/{A}/monthly?from={MONTH}&to={MONTH}", headers=H).json()
month_row = next((m for m in pnl if m["month"] == str(MONTH)), None)
share_a = next(m["amount"] for m in exp["members"] if m["property_id"] == A)
if month_row is not None:
    check("the share reaches the property's reported debt_service",
          abs(month_row["property_tier"]["debt_service"] - share_a) < EPS,
          f"property_tier debt_service = {month_row['property_tier']['debt_service']}, "
          f"share = {share_a}")
    check("the share is NOT allocated down to units",
          abs(month_row["units"]["debt_service"]) < EPS,
          "a property-tier cost must stay out of the unit rollup")
    check("the share flows through to cash flow",
          abs(month_row["cash_flow"] - (month_row["noi"] - share_a)) < EPS,
          f"cash_flow = {month_row['cash_flow']}, noi = {month_row['noi']}")
else:
    check("property P&L returned the posted month", False, str(pnl)[:200])

print("\n== 4. Re-posting is idempotent ==")
client.post(f"/shared-expenses/{eid}/post", headers=H, json={"from_month": str(MONTH)})
n, total = db.execute(
    text("SELECT count(*), COALESCE(sum(amount), 0) FROM line_items WHERE shared_expense_id = :e"),
    {"e": eid},
).first()
check("re-posting updates in place rather than duplicating", n == 3, f"{n} line items")
check("re-posting leaves the total at the bill", cents(total) == cents(BILL), str(total))

# Correcting the amount and re-posting is the intended workflow — it must restate, not stack.
client.put(f"/shared-expenses/{eid}", headers=H, json=body(amount=13000.00))
client.post(f"/shared-expenses/{eid}/post", headers=H, json={"from_month": str(MONTH)})
n, total = db.execute(
    text("SELECT count(*), COALESCE(sum(amount), 0) FROM line_items WHERE shared_expense_id = :e"),
    {"e": eid},
).first()
check("a corrected bill restates the month", n == 3 and cents(total) == cents(13000.00), str(total))
client.put(f"/shared-expenses/{eid}", headers=H, json=body())
client.post(f"/shared-expenses/{eid}/post", headers=H, json={"from_month": str(MONTH)})

print("\n== 5. A bulk write never silently overwrites a hand-entered figure ==")
# Plant a manual Insurance line on ONE member, then aim a shared Insurance policy at it.
# The record exists because section 3 posted the mortgage into this month.
rec_id = db.scalar(
    text(
        "SELECT id::text FROM monthly_records "
        "WHERE property_id = :p AND unit_id IS NULL AND month = :m"
    ),
    {"p": B, "m": MONTH},
)
assert rec_id, "expected the mortgage post to have created a property-tier record"
db.execute(
    text(
        "INSERT INTO line_items (account_id, monthly_record_id, category_id, amount) "
        "SELECT account_id, id, CAST(:c AS uuid), 777.77 FROM monthly_records "
        "WHERE id = CAST(:r AS uuid) "
        "ON CONFLICT (monthly_record_id, category_id) DO UPDATE SET amount = 777.77"
    ),
    {"r": rec_id, "c": INSURANCE},
)
db.commit()

policy = create(name="Blanket insurance policy", category_id=INSURANCE, amount=3000.00)
policy_id = policy["id"]

plan = client.post(f"/shared-expenses/{policy_id}/post/preview", headers=H,
                   json={"from_month": str(MONTH)}).json()
manual = [r for r in plan["rows"] if r["existing_source"] == "manual"]
check("preview flags the hand-entered line as a conflict", len(manual) == 1, str(plan["rows"])[:200])
check("preview reports the post as blocked", plan["blocked"] is True)
check("preview shows what stands there now",
      bool(manual) and cents(manual[0]["existing_amount"]) == cents(777.77), str(manual)[:160])

r = client.post(f"/shared-expenses/{policy_id}/post", headers=H, json={"from_month": str(MONTH)})
check("posting over a hand-entered figure is REFUSED by default", r.status_code == 409,
      str(r.status_code))
still = db.scalar(
    text("SELECT amount FROM line_items WHERE monthly_record_id = CAST(:r AS uuid) "
         "AND category_id = CAST(:c AS uuid)"),
    {"r": rec_id, "c": INSURANCE},
)
check("the hand-entered figure survived the refusal", cents(still) == cents(777.77), str(still))

r = client.post(f"/shared-expenses/{policy_id}/post", headers=H,
                json={"from_month": str(MONTH), "on_conflict": "skip"})
check("on_conflict=skip posts the rest", r.status_code == 200 and r.json()["skipped"] == 1,
      r.text[:160])
check("skip leaves the hand-entered property alone",
      cents(db.scalar(
          text("SELECT amount FROM line_items WHERE monthly_record_id = CAST(:r AS uuid) "
               "AND category_id = CAST(:c AS uuid)"),
          {"r": rec_id, "c": INSURANCE})) == cents(777.77))
check("skip reports the reduced total honestly",
      cents(r.json()["total_posted"]) < cents(3000.00), str(r.json()["total_posted"]))

r = client.post(f"/shared-expenses/{policy_id}/post", headers=H,
                json={"from_month": str(MONTH), "on_conflict": "replace"})
check("on_conflict=replace takes ownership", r.status_code == 200 and r.json()["skipped"] == 0,
      r.text[:160])
total = db.scalar(
    text("SELECT sum(amount) FROM line_items WHERE shared_expense_id = :e"), {"e": policy_id}
)
check("after replace the policy sums to its own bill", cents(total) == cents(3000.00), str(total))

print("\n== 6. Locked months still bind ==")
db.execute(
    text(
        "INSERT INTO period_status (property_id, month, status) "
        "VALUES (CAST(:p AS uuid), :m, 'locked') "
        "ON CONFLICT (property_id, month) DO UPDATE SET status = 'locked'"
    ),
    {"p": C, "m": MONTH},
)
db.commit()
r = client.post(f"/shared-expenses/{eid}/post", headers=H, json={"from_month": str(MONTH)})
check("a locked member-month blocks the whole post", r.status_code == 423, str(r.status_code))
check("the refusal names the locked property", "Birch Street House" in r.text, r.text[:160])
r = client.request("DELETE", f"/shared-expenses/{eid}/post", headers=H,
                   params={"from_month": str(MONTH)})
check("un-posting a locked month is refused too", r.status_code == 423, str(r.status_code))
db.execute(
    text("UPDATE period_status SET status = 'draft' WHERE property_id = :p AND month = :m"),
    {"p": C, "m": MONTH},
)
db.commit()

print("\n== 7. Posting a range, and un-posting only what it posted ==")
r = client.post(f"/shared-expenses/{eid}/post", headers=H,
                json={"from_month": str(RANGE_FROM), "to_month": str(RANGE_TO)})
check("a three-month range posts nine line items",
      r.status_code == 200 and r.json()["line_items_written"] == 9, r.text[:200])
check("the range total is three bills",
      cents(r.json()["total_posted"]) == cents(BILL * 3), str(r.json()["total_posted"]))

r = client.post(f"/shared-expenses/{eid}/post", headers=H,
                json={"from_month": str(RANGE_TO), "to_month": str(RANGE_FROM)})
check("a backwards range is rejected", r.status_code == 400, str(r.status_code))

r = client.request("DELETE", f"/shared-expenses/{eid}/post", headers=H,
                   params={"from_month": str(RANGE_FROM), "to_month": str(RANGE_MID)})
check("un-post removes exactly the requested months",
      r.status_code == 200 and r.json()["line_items_removed"] == 6, r.text[:200])
left = db.execute(
    text(
        "SELECT DISTINCT mr.month FROM line_items li "
        "JOIN monthly_records mr ON mr.id = li.monthly_record_id "
        "WHERE li.shared_expense_id = :e ORDER BY mr.month"
    ),
    {"e": eid},
).scalars().all()
check("the un-touched months survive an April-May un-post",
      left == [MONTH, RANGE_TO], str(left))

# The un-post must remove the policy's own lines and leave no stray unowned duplicate behind.
client.request("DELETE", f"/shared-expenses/{policy_id}/post", headers=H,
               params={"from_month": str(MONTH)})
check("un-post removed all of the policy's own lines",
      db.scalar(text("SELECT count(*) FROM line_items WHERE shared_expense_id = :e"),
                {"e": policy_id}) == 0)
strays = db.scalar(
    text(
        "SELECT count(*) FROM line_items li JOIN monthly_records mr ON mr.id = li.monthly_record_id "
        "WHERE mr.month = :m AND li.category_id = CAST(:c AS uuid) "
        "  AND li.shared_expense_id IS NULL AND mr.property_id = ANY(CAST(:ids AS uuid[]))"
    ),
    {"m": MONTH, "c": INSURANCE, "ids": [A, B, C]},
)
check("no stray unowned line is left behind", strays == 0, f"{strays} left")

print("\n== 8. Deleting the arrangement keeps the history ==")
detach = create(name="Temporary arrangement", amount=600.00)
client.post(f"/shared-expenses/{detach['id']}/post", headers=H,
            json={"from_month": str(MONTH), "on_conflict": "replace"})
posted_ids = db.execute(
    text("SELECT id::text FROM line_items WHERE shared_expense_id = :e"), {"e": detach["id"]}
).scalars().all()
check("the temporary arrangement posted", len(posted_ids) == 3, str(len(posted_ids)))
client.delete(f"/shared-expenses/{detach['id']}", headers=H)
created_expenses.remove(detach["id"])
kept = db.execute(
    text("SELECT count(*), COALESCE(sum(amount), 0) FROM line_items "
         "WHERE id = ANY(CAST(:ids AS uuid[]))"),
    {"ids": posted_ids},
).first()
check("deleting the arrangement KEEPS the posted spend",
      kept[0] == 3 and cents(kept[1]) == cents(600.00),
      "the money was really spent; deleting the description must not erase it")
check("the kept lines are detached, not dangling",
      db.scalar(text("SELECT count(*) FROM line_items WHERE id = ANY(CAST(:ids AS uuid[])) "
                     "AND shared_expense_id IS NOT NULL"), {"ids": posted_ids}) == 0)
db.execute(text("DELETE FROM line_items WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": posted_ids})
db.commit()

purge = create(name="Purgeable arrangement", amount=450.00)
client.post(f"/shared-expenses/{purge['id']}/post", headers=H,
            json={"from_month": str(MONTH), "on_conflict": "replace"})
client.delete(f"/shared-expenses/{purge['id']}?purge=true", headers=H)
created_expenses.remove(purge["id"])
check("?purge=true removes the postings too",
      db.scalar(text("SELECT count(*) FROM line_items WHERE shared_expense_id = :e"),
                {"e": purge["id"]}) == 0)

print("\n== 9. Cross-account isolation ==")
email = f"shared-iso-{_uuid.uuid4().hex[:8]}@example.com"
signup = client.post("/auth/signup", json={"email": email, "password": "iso-test-pw-12345",
                                           "account_name": "Shared Expense Isolation"})
assert signup.status_code == 201, signup.text
H2 = {"Authorization": f"Bearer {signup.json()['access_token']}"}

r = client.get(f"/shared-expenses/{eid}", headers=H2)
check("another account cannot read the arrangement (404, never 403)",
      r.status_code == 404, str(r.status_code))
check("another account's listing does not contain it",
      all(e["id"] != eid for e in client.get("/shared-expenses", headers=H2).json()))
r = client.post(f"/shared-expenses/{eid}/post", headers=H2, json={"from_month": str(MONTH)})
check("another account cannot post it", r.status_code == 404, str(r.status_code))

other_cat = client.get("/categories", headers=H2).json()[0]["id"]
r = client.post("/shared-expenses", headers=H2, json={
    "name": "Cross-account grab", "category_id": other_cat, "amount": 100,
    "allocation_method": "equal",
    "members": [{"property_id": A}, {"property_id": B}],
})
check("another account cannot name our properties as members",
      r.status_code == 404, str(r.status_code))
r = client.post("/shared-expenses", headers=H, json=body(category_id=other_cat))
check("we cannot post into another account's category", r.status_code == 400, str(r.status_code))

db.execute(text("DELETE FROM accounts WHERE id = (SELECT account_id FROM users WHERE email = :e)"),
           {"e": email})
db.commit()

print("\n== 10. Scheduling ahead does not masquerade as performance ==")
# The whole point of posting a year of a loan payment in one action is that it sits there until
# each month arrives. A month holding a scheduled cost and nothing else is not a trading month:
# it must not become the default period anyone lands on, and it must not enter the trailing-12
# window behind cap rate / cash-on-cash / DSCR, where it would report costs against income
# nobody has earned yet.
TODAY = date.today()
FUTURE = date(TODAY.year + 1, TODAY.month, 1)  # comfortably beyond any seeded month

base_dash = client.get("/portfolio/dashboard", headers=H).json()
base_metrics = client.get(f"/properties/{C}/investment/metrics", headers=H).json()
base_roster_month = client.get(f"/properties/{C}/units/roster", headers=H).json()["month"]

r = client.post(f"/shared-expenses/{eid}/post", headers=H,
                json={"from_month": str(FUTURE), "to_month": str(date(FUTURE.year, 12, 1))})
check("posting into the future succeeds", r.status_code == 200, r.text[:160])

after_dash = client.get("/portfolio/dashboard", headers=H).json()
check("the default dashboard period does not move into the future",
      after_dash["period_to"] == base_dash["period_to"],
      f"{base_dash['period_to']} -> {after_dash['period_to']}")
check("the default period is not in the future at all",
      after_dash["period_to"] is None or after_dash["period_to"] <= str(TODAY.replace(day=1)),
      str(after_dash["period_to"]))

after_metrics = client.get(f"/properties/{C}/investment/metrics", headers=H).json()
for key in ("t12_months", "t12_noi", "t12_cash_flow", "cap_rate", "months_available"):
    check(f"scheduling ahead leaves {key} unchanged",
          after_metrics[key] == base_metrics[key],
          f"{base_metrics[key]} -> {after_metrics[key]}")

check("the unit roster still defaults to a real month",
      client.get(f"/properties/{C}/units/roster", headers=H).json()["month"] == base_roster_month)

feed = client.get("/portfolio/attention", headers=H).json()
check("the attention feed does not anchor on a scheduled month",
      feed["period_to"] is None or feed["period_to"] <= str(TODAY.replace(day=1)),
      str(feed["period_to"]))

# ...but the money is really there, and asking for those months explicitly must show it in full.
explicit = client.get(
    f"/properties/{C}/monthly?from={FUTURE}&to={FUTURE}", headers=H
).json()
share_c = next(m["amount"] for m in exp["members"] if m["property_id"] == C)
check("an explicitly requested future month still shows the scheduled cost",
      bool(explicit) and abs(explicit[0]["property_tier"]["debt_service"] - share_c) < EPS,
      str(explicit)[:200])

# A month becomes real the moment something OTHER than a schedule is recorded in it — no
# action required beyond ordinary data entry.
rec = client.post("/records", headers=H, json={
    "property_id": C, "unit_id": None, "month": str(MONTH),
    "line_items": [{"category_id": INSURANCE, "amount": 120.00}],
}).json()
promoted = client.get(f"/properties/{C}/investment/metrics", headers=H).json()
check("a hand-entered line promotes that month into the metrics",
      promoted["months_available"] == base_metrics["months_available"] + 1,
      f"{base_metrics['months_available']} -> {promoted['months_available']}")
client.delete(f"/records/{rec['id']}", headers=H)

# ---- cleanup ------------------------------------------------------------------------------
cleanup()

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("verify_phase12: ALL CHECKS PASSED")
