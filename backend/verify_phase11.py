"""Verification of portfolio (bulk) acquisitions — one deal, costs allocated across properties.

Run:  PYTHONPATH=.pydeps python3 verify_phase11.py

Checks the property this feature exists to protect: **the allocated shares sum back to the
deal's stated totals to the cent**, so that a bulk purchase modelled as N per-property rows
produces exactly the portfolio-level figures it would have produced as a single entity.

Covers the allocation math (pro-rata, equal, custom, and the pathological rounding cases),
the persistence path (members written to ``property_investment``, re-allocation on update,
ungroup-not-erase on delete), cross-account isolation, and the fact that every pre-existing
investment metric is unchanged for properties NOT in a deal.

Creates its own deal over three seed properties and cleans up after itself, restoring any
acquisition rows it overwrote.
"""
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.allocation import allocate
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


# Three seed properties with deliberately awkward prices: they do NOT divide evenly, so the
# leftover-cent handling is actually exercised rather than getting lucky on round numbers.
A, B, C = _pid("Oak Ridge Residences"), _pid("Ash Grove Apartments"), _pid("Birch Street House")
NAMES = {A: "Oak Ridge Residences", B: "Ash Grove Apartments", C: "Birch Street House"}
PRICES = {A: 333333.33, B: 216666.67, C: 100000.00}   # combined 650,000.00
TOTAL_CLOSING = 18750.55                               # prime-ish, won't split evenly by 3
TOTAL_LOAN = 487500.00

# Snapshot whatever acquisition data these properties already have, to restore at the end.
_snapshot = db.execute(
    text(
        "SELECT property_id::text AS pid, purchase_price, closing_costs, loan_amount, "
        "purchase_date FROM property_investment WHERE property_id = ANY(CAST(:ids AS uuid[]))"
    ),
    {"ids": [A, B, C]},
).mappings().all()


def restore() -> None:
    db.execute(
        text("DELETE FROM property_investment WHERE property_id = ANY(CAST(:ids AS uuid[]))"),
        {"ids": [A, B, C]},
    )
    for r in _snapshot:
        db.execute(
            text(
                "INSERT INTO property_investment "
                "(property_id, purchase_price, closing_costs, loan_amount, purchase_date) "
                "VALUES (:pid, :pp, :cc, :la, :pd)"
            ),
            {
                "pid": r["pid"], "pp": r["purchase_price"], "cc": r["closing_costs"],
                "la": r["loan_amount"], "pd": r["purchase_date"],
            },
        )
    db.commit()


def body(method="price", members=None, **over):
    payload = {
        "name": "Verify Bulk Portfolio",
        "purchase_date": "2024-01-15",
        "total_closing_costs": TOTAL_CLOSING,
        "total_loan_amount": TOTAL_LOAN,
        "allocation_method": method,
        "members": members
        if members is not None
        else [{"property_id": p, "purchase_price": PRICES[p]} for p in (A, B, C)],
    }
    payload.update(over)
    return payload


def cents(x: float) -> int:
    """Compare money as integer cents — float sums of allocated shares are not exactly equal."""
    return int(round(x * 100))


print("\n== 1. Allocation math: shares always sum to the whole ==")
# The pure function, on the cases that break naive round-and-hope implementations.
for total, weights in [
    (Decimal("10000"), [Decimal(1)] * 3),            # 1/3 each: 0.01 leftover
    (Decimal("18750.55"), [Decimal(1)] * 7),         # odd total, 7 ways
    (Decimal("0.01"), [Decimal(1)] * 4),             # less than one cent each
    (Decimal("0"), [Decimal(5), Decimal(3)]),        # nothing to split
    (Decimal("999999.99"), [Decimal(333333), Decimal(216667), Decimal(1)]),
]:
    parts = allocate(total, weights)
    check(
        f"sum exact for {total} over {len(weights)}",
        sum(parts) == total.quantize(Decimal("0.01")),
        f"got {sum(parts)}",
    )
check(
    "zero weights fall back to an equal split (no divide-by-zero)",
    sum(allocate(Decimal("1000"), [Decimal(0)] * 3)) == Decimal("1000.00"),
)

print("\n== 2. Preview: pro-rata by price, without saving ==")
r = client.post("/acquisitions/preview", headers=H, json=body())
check("preview 200", r.status_code == 200, r.text)
pv = r.json()
by_id = {m["property_id"]: m for m in pv["members"]}
check(
    "preview closing shares sum to the stated total",
    cents(sum(m["closing_costs"] for m in pv["members"])) == cents(TOTAL_CLOSING),
)
check(
    "preview loan shares sum to the stated total",
    cents(sum(m["loan_amount"] for m in pv["members"])) == cents(TOTAL_LOAN),
)
# Birch is 100,000 / 650,000 of the deal, so it should carry that fraction of the loan.
expected_birch_loan = TOTAL_LOAN * (PRICES[C] / sum(PRICES.values()))
check(
    "pro-rata share matches price weight",
    abs(by_id[C]["loan_amount"] - expected_birch_loan) < 0.01,
    f"{by_id[C]['loan_amount']} vs {expected_birch_loan:.2f}",
)
check(
    "price_share is the property's fraction of combined price",
    abs(by_id[C]["price_share"] - PRICES[C] / sum(PRICES.values())) < EPS,
)
check(
    "equity = price - loan + closing, per member",
    all(
        abs(m["equity_invested"] - (m["purchase_price"] - m["loan_amount"] + m["closing_costs"])) < EPS
        for m in pv["members"]
    ),
)
check("preview persisted nothing", client.get("/acquisitions", headers=H).json() == [])

print("\n== 3. Create: the split is written onto the member properties ==")
r = client.post("/acquisitions", headers=H, json=body())
check("create 201", r.status_code == 201, r.text)
deal = r.json()
DEAL_ID = deal["id"]
check("member count", deal["property_count"] == 3, str(deal["property_count"]))
check("combined price is the sum of agreed prices", cents(deal["total_purchase_price"]) == cents(sum(PRICES.values())))
check("no closing drift on a fresh deal", cents(deal["closing_costs_drift"]) == 0, str(deal["closing_costs_drift"]))
check("no loan drift on a fresh deal", cents(deal["loan_amount_drift"]) == 0, str(deal["loan_amount_drift"]))

# The rows the metrics actually read.
rows = db.execute(
    text(
        "SELECT property_id::text pid, purchase_price, closing_costs, loan_amount, "
        "purchase_date, acquisition_id::text acq FROM property_investment "
        "WHERE acquisition_id = CAST(:d AS uuid)"
    ),
    {"d": DEAL_ID},
).mappings().all()
check("three property_investment rows linked to the deal", len(rows) == 3, str(len(rows)))
check(
    "stored closing costs sum to the deal total",
    sum(int(r["closing_costs"] * 100) for r in rows) == cents(TOTAL_CLOSING),
)
check(
    "stored loans sum to the deal total",
    sum(int(r["loan_amount"] * 100) for r in rows) == cents(TOTAL_LOAN),
)
check("every member shares the deal's purchase date", {str(r["purchase_date"]) for r in rows} == {"2024-01-15"})

print("\n== 4. The point of pro-rata: portfolio figures match a single-entity model ==")
# Σ equity over the members must equal (Σ price - one loan + one closing) — i.e. modelling the
# deal as N rows gives the same equity base as modelling it as one purchase.
pf = client.get("/investments", headers=H).json()
member_metrics = [p for p in pf["properties"] if p["property_id"] in (A, B, C)]
check("all three appear in portfolio investment", len(member_metrics) == 3)
single_entity_equity = sum(PRICES.values()) - TOTAL_LOAN + TOTAL_CLOSING
check(
    "Σ member equity == single-entity equity",
    cents(sum(p["equity_invested"] for p in member_metrics)) == cents(single_entity_equity),
    f"{sum(p['equity_invested'] for p in member_metrics)} vs {single_entity_equity}",
)
check(
    "members carry the deal name for the UI",
    all(p["acquisition_name"] == "Verify Bulk Portfolio" for p in member_metrics),
)
check(
    "cap rate is unaffected by allocation (uses the real agreed price)",
    all(p["purchase_price"] == PRICES[p["property_id"]] for p in member_metrics),
)
# A property outside the deal must be untouched by any of this.
other = [p for p in pf["properties"] if p["property_id"] not in (A, B, C)]
check("non-member properties carry no acquisition link", all(p["acquisition_id"] is None for p in other))

print("\n== 5. Equal and custom allocation ==")
r = client.post("/acquisitions/preview", headers=H, json=body(method="equal"))
eq = r.json()
check(
    "equal: shares sum to the total",
    cents(sum(m["loan_amount"] for m in eq["members"])) == cents(TOTAL_LOAN),
)
check(
    "equal: within a cent of each other",
    max(m["loan_amount"] for m in eq["members"]) - min(m["loan_amount"] for m in eq["members"]) <= 0.01,
)

custom_members = [
    {"property_id": A, "purchase_price": PRICES[A], "closing_costs": 10000, "loan_amount": 250000},
    {"property_id": B, "purchase_price": PRICES[B], "closing_costs": 5000.55, "loan_amount": 150000},
    {"property_id": C, "purchase_price": PRICES[C], "closing_costs": 3750, "loan_amount": 87500},
]
r = client.post("/acquisitions/preview", headers=H, json=body(method="custom", members=custom_members))
cu = r.json()
check("custom: totals are the sum of the supplied shares", cents(cu["total_closing_costs"]) == cents(18750.55))
check("custom: loan total is the sum of supplied shares", cents(cu["total_loan_amount"]) == cents(487500))
check(
    "custom: shares are stored verbatim, not re-derived",
    cents({m["property_id"]: m for m in cu["members"]}[B]["closing_costs"]) == cents(5000.55),
)
r = client.post(
    "/acquisitions/preview",
    headers=H,
    json=body(method="custom", members=[{"property_id": A, "purchase_price": 1}, {"property_id": B, "purchase_price": 2}]),
)
check("custom rejects missing shares", r.status_code == 400, f"{r.status_code} {r.text[:120]}")

print("\n== 6. Update re-allocates; dropped members keep their data ==")
NEW_CLOSING, NEW_LOAN = 22000.33, 500000.00
r = client.put(
    f"/acquisitions/{DEAL_ID}",
    headers=H,
    json=body(
        total_closing_costs=NEW_CLOSING,
        total_loan_amount=NEW_LOAN,
        members=[{"property_id": p, "purchase_price": PRICES[p]} for p in (A, B)],  # C dropped
    ),
)
check("update 200", r.status_code == 200, r.text)
upd = r.json()
check("deal now has two members", upd["property_count"] == 2, str(upd["property_count"]))
check(
    "re-allocated closing costs sum to the NEW total",
    cents(sum(m["closing_costs"] for m in upd["members"])) == cents(NEW_CLOSING),
)
check("no drift after re-allocation", cents(upd["closing_costs_drift"]) == 0)
dropped = db.execute(
    text(
        "SELECT purchase_price, acquisition_id FROM property_investment WHERE property_id = CAST(:p AS uuid)"
    ),
    {"p": C},
).mappings().first()
check("dropped member keeps its acquisition row", dropped is not None)
check("dropped member is unlinked from the deal", dropped and dropped["acquisition_id"] is None)
check("dropped member keeps its price", dropped and int(dropped["purchase_price"] * 100) == cents(PRICES[C]))

print("\n== 7. Drift is reported, not silently reconciled ==")
# Hand-edit one member on its own property page — the parts should stop summing to the deal,
# and the API should say so rather than quietly rewriting either side.
client.put(
    f"/properties/{A}/investment",
    headers=H,
    json={"purchase_price": PRICES[A], "closing_costs": 1.00, "loan_amount": 1.00, "purchase_date": "2024-01-15"},
)
drifted = client.get(f"/acquisitions/{DEAL_ID}", headers=H).json()
check("closing drift is now non-zero", cents(drifted["closing_costs_drift"]) != 0, str(drifted["closing_costs_drift"]))
check("loan drift is now non-zero", cents(drifted["loan_amount_drift"]) != 0)
check(
    "stated total is preserved (the deal is not rewritten by the edit)",
    cents(drifted["total_closing_costs"]) == cents(NEW_CLOSING),
)
still = client.get(f"/properties/{A}/investment/metrics", headers=H).json()
check("hand-edited member stays linked to the deal", still["acquisition_id"] == DEAL_ID)

print("\n== 8. Validation ==")
r = client.post("/acquisitions", headers=H, json=body(members=[{"property_id": A, "purchase_price": 1}]))
check("a one-property 'portfolio' is rejected", r.status_code == 422, str(r.status_code))
r = client.post(
    "/acquisitions",
    headers=H,
    json=body(members=[{"property_id": A, "purchase_price": 1}, {"property_id": A, "purchase_price": 2}]),
)
check("duplicate property is rejected", r.status_code == 400, f"{r.status_code} {r.text[:100]}")
r = client.post(
    "/acquisitions",
    headers=H,
    json=body(members=[
        {"property_id": A, "purchase_price": 1},
        {"property_id": "00000000-0000-0000-0000-000000000000", "purchase_price": 2},
    ]),
)
check("unknown property 404s (never 403 — no existence disclosure)", r.status_code == 404, str(r.status_code))
r = client.post("/acquisitions", headers=H, json=body(method="bogus"))
check("unknown allocation method is rejected", r.status_code == 422, str(r.status_code))
r = client.post("/acquisitions", headers=H, json=body(total_loan_amount=-5))
check("negative loan is rejected", r.status_code == 422, str(r.status_code))

print("\n== 9. Cross-account isolation ==")
other_email = "acqverify@example.com"
db.execute(text("DELETE FROM users WHERE email = :e"), {"e": other_email})
db.commit()
signup = client.post(
    "/auth/signup", json={"email": other_email, "password": "outsider12345", "account_name": "Outsider"}
)
check("second account created", signup.status_code in (200, 201), signup.text[:120])
OH = {"Authorization": f"Bearer {signup.json()['access_token']}"}
check("outsider sees no deals of ours", client.get("/acquisitions", headers=OH).json() == [])
check("outsider cannot read our deal", client.get(f"/acquisitions/{DEAL_ID}", headers=OH).status_code == 404)
check("outsider cannot update our deal", client.put(f"/acquisitions/{DEAL_ID}", headers=OH, json=body()).status_code == 404)
check("outsider cannot delete our deal", client.delete(f"/acquisitions/{DEAL_ID}", headers=OH).status_code == 404)
r = client.post("/acquisitions", headers=OH, json=body())
check("outsider cannot pull OUR properties into THEIR deal", r.status_code == 404, str(r.status_code))

print("\n== 10. Delete ungroups by default, purges only on request ==")
r = client.delete(f"/acquisitions/{DEAL_ID}", headers=H)
check("delete 204", r.status_code == 204, str(r.status_code))
survivors = db.execute(
    text(
        "SELECT property_id::text pid, acquisition_id FROM property_investment "
        "WHERE property_id = ANY(CAST(:ids AS uuid[]))"
    ),
    {"ids": [A, B]},
).mappings().all()
check("members keep their acquisition data after ungroup", len(survivors) == 2, str(len(survivors)))
check("members are unlinked (FK SET NULL)", all(s["acquisition_id"] is None for s in survivors))
check("their return metrics still resolve", client.get(f"/properties/{A}/investment/metrics", headers=H).json()["purchase_price"] is not None)

r = client.post("/acquisitions", headers=H, json=body())
purge_id = r.json()["id"]
check("purge delete 204", client.delete(f"/acquisitions/{purge_id}?purge=true", headers=H).status_code == 204)
left = db.scalar(
    text("SELECT count(*) FROM property_investment WHERE property_id = ANY(CAST(:ids AS uuid[]))"),
    {"ids": [A, B, C]},
)
check("purge cleared the members' acquisition rows", left == 0, str(left))

print("\n== 11. A property moved from one deal to another ==")
# Self-contained: two fresh deals, the second poaching a member of the first. A property_investment
# row can only belong to ONE deal, so the move must relocate it rather than duplicate or error —
# and the deal it LEFT must report the resulting shortfall instead of quietly shrinking its totals.
d1 = client.post(
    "/acquisitions",
    headers=H,
    json=body(
        name="Verify Move One",
        total_closing_costs=10000,
        total_loan_amount=200000,
        members=[{"property_id": A, "purchase_price": 300000}, {"property_id": B, "purchase_price": 200000}],
    ),
).json()
d2 = client.post(
    "/acquisitions",
    headers=H,
    json=body(
        name="Verify Move Two",
        total_closing_costs=6000,
        total_loan_amount=90000,
        members=[{"property_id": B, "purchase_price": 200000}, {"property_id": C, "purchase_price": 100000}],
    ),
)
check("second deal may claim a property from the first", d2.status_code == 201, d2.text[:120])
d2 = d2.json()
check("moved property now reports the NEW deal", client.get(f"/properties/{B}/investment/metrics", headers=H).json()["acquisition_name"] == "Verify Move Two")
d1_after = client.get(f"/acquisitions/{d1['id']}", headers=H).json()
check("the deal it left is down to one member", d1_after["property_count"] == 1, str(d1_after["property_count"]))
check(
    "the deal it left reports the shortfall as drift (B took 40% of 10,000 with it)",
    cents(d1_after["closing_costs_drift"]) == cents(-4000),
    str(d1_after["closing_costs_drift"]),
)
check("its stated total is NOT silently reduced", cents(d1_after["total_closing_costs"]) == cents(10000))
client.delete(f"/acquisitions/{d2['id']}", headers=H)
moved = client.get(f"/properties/{B}/investment/metrics", headers=H).json()
check("deleting the new deal leaves the property's data intact", moved["purchase_price"] == 200000)
check("and unlinked rather than returned to the old deal", moved["acquisition_id"] is None)
client.delete(f"/acquisitions/{d1['id']}", headers=H)

# ---- cleanup -------------------------------------------------------------------------------
db.execute(
    text("DELETE FROM portfolio_acquisition WHERE name IN "
         "('Verify Bulk Portfolio', 'Verify Move One', 'Verify Move Two')")
)
db.execute(text("DELETE FROM users WHERE email = :e"), {"e": other_email})
db.commit()
restore()

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("verify_phase11 PASSED")
