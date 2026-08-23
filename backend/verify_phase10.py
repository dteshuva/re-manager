"""Verification of investment insights (per-property acquisition inputs + return metrics).

Run:  PYTHONPATH=.pydeps python3 verify_phase10.py

Reconciles cap rate / cash-on-cash / DSCR / average cash-on-cash against raw
``property_month_summary`` sums, checks the data-availability gates, the calendar
trailing-12 window (gap-safe), and the value-weighted portfolio aggregate. Only creates/
deletes its OWN row on Oak Ridge; every other property (incl. any you entered by hand) is
read-only, so it is safe and re-runnable.
"""
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import SessionLocal
from app.main import app

client = TestClient(app)
db = SessionLocal()
tok = client.post("/auth/login", data={"username": "admin@example.com", "password": "admin12345"}).json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}

# Resolved by NAME, not by hard-coded id: property ids are server-generated, so a
# re-seeded database (or a second developer's) gets different UUIDs and every hard-coded
# id 404s. Names are stable fixtures the seed itself defines.
def _pid(name: str) -> str:
    pid = db.scalar(text("SELECT id::text FROM properties WHERE name = :n"), {"n": name})
    assert pid, f"seed fixture missing: {name}"
    return pid


OAK = _pid("Oak Ridge Residences")     # 24 months, no gap (test writes/deletes here)
ASH = _pid("Ash Grove Apartments")     # 23 months (June 2025 MISSING): gap regression
BIRCH = _pid("Birch Street House")     # no investment inputs
EPS = 1e-6


def get(path):
    r = client.get(path, headers=H)
    assert r.status_code == 200, (path, r.status_code, r.text)
    return r.json()


def put_inv(pid, **body):
    r = client.put(f"/properties/{pid}/investment", headers=H, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def raw_sums(pid, date_from, date_to):
    row = db.execute(
        text(
            "SELECT COALESCE(SUM(noi),0) noi, COALESCE(SUM(cash_flow),0) cf, "
            "COALESCE(SUM(debt_service),0) debt FROM property_month_summary "
            "WHERE property_id=:p AND month BETWEEN :f AND :t"
        ),
        {"p": pid, "f": date_from, "t": date_to},
    ).mappings().first()
    return float(row["noi"]), float(row["cf"]), float(row["debt"])


def cap_noi(p):
    if p["t12_noi"] is None:
        return None
    return p["t12_noi"] / p["t12_months"] * 12 if p["annualized"] and p["t12_months"] else p["t12_noi"]


print("1) FULL-HISTORY PROPERTY — all four metrics reconcile --------------------")
PRICE, CLOSING, LOAN = 5_000_000, 150_000, 3_750_000
EQUITY = PRICE - LOAN + CLOSING  # 1,400,000
out = put_inv(OAK, purchase_price=PRICE, closing_costs=CLOSING, loan_amount=LOAN, purchase_date="2023-06-01")
assert abs(out["equity_invested"] - EQUITY) < EPS, out["equity_invested"]

m = get(f"/properties/{OAK}/investment/metrics")
assert m["months_available"] == 24 and m["t12_months"] == 12 and m["annualized"] is False

t12_noi, t12_cf, t12_debt = raw_sums(OAK, date(2025, 1, 1), date(2025, 12, 1))  # trailing 12 = Jan..Dec 2025
all_noi, all_cf, all_debt = raw_sums(OAK, date(2024, 1, 1), date(2025, 12, 1))

assert abs(m["t12_noi"] - t12_noi) < 1e-4 and abs(m["t12_cash_flow"] - t12_cf) < 1e-4
assert abs(m["cap_rate"] - t12_noi / PRICE) < EPS, (m["cap_rate"], t12_noi / PRICE)  # raw sum, full year
assert abs(m["cash_on_cash"] - t12_cf / EQUITY) < EPS, (m["cash_on_cash"], t12_cf / EQUITY)
assert abs(m["dscr"] - t12_noi / t12_debt) < EPS, (m["dscr"], t12_noi / t12_debt)
assert abs(m["avg_cash_on_cash"] - all_cf / EQUITY / 2) < EPS, (m["avg_cash_on_cash"], all_cf / EQUITY / 2)
print(f"   cap={m['cap_rate']*100:.2f}%  CoC={m['cash_on_cash']*100:.2f}%  "
      f"DSCR={m['dscr']:.2f}x  avgCoC={m['avg_cash_on_cash']*100:.2f}%  (all reconcile) ✓")

print("\n2) GAP-SAFE TRAILING-12 WINDOW (Ash Grove is missing June 2025) -----------")
# Calendar T12 = Jan..Dec 2025 (11 present rows) — NOT the last 12 rows (which would pull in
# Dec 2024 and over-count). Read-only: does not touch the row.
g = get(f"/properties/{ASH}/investment/metrics")
if g["purchase_price"] is None:
    print("   (skipped — Ash Grove has no investment inputs entered)")
else:
    cal_noi, cal_cf, cal_debt = raw_sums(ASH, date(2025, 1, 1), date(2025, 12, 1))
    assert g["annualized"] is False and g["t12_months"] == 11, (g["annualized"], g["t12_months"])
    assert abs(g["t12_noi"] - cal_noi) < 1e-4, (g["t12_noi"], cal_noi)
    assert abs(g["t12_cash_flow"] - cal_cf) < 1e-4, (g["t12_cash_flow"], cal_cf)
    assert g["cash_on_cash"] is not None  # a full year of history despite the gap
    print(f"   T12 NOI={g['t12_noi']:,.0f} (calendar Jan–Dec 2025, gap=0) not last-12-rows ✓")

print("\n3) YOUNG HOLD — cap annualized, CoC/avg gated off ------------------------")
# Re-buy Oak mid-2025: only 2025-07..12 (6 months) → data doesn't span a year → annualized.
put_inv(OAK, purchase_price=PRICE, closing_costs=CLOSING, loan_amount=LOAN, purchase_date="2025-07-01")
c = get(f"/properties/{OAK}/investment/metrics")
assert c["months_available"] == 6 and c["t12_months"] == 6 and c["annualized"] is True
assert c["cash_on_cash"] is None and c["avg_cash_on_cash"] is None
h2_noi, _, _ = raw_sums(OAK, date(2025, 7, 1), date(2025, 12, 1))
assert abs(c["cap_rate"] - (h2_noi / 6 * 12) / PRICE) < EPS, (c["cap_rate"], (h2_noi / 6 * 12) / PRICE)
print(f"   6 months of history → CoC/avg hidden, cap annualized ({c['cap_rate']*100:.2f}%) ✓")

print("\n4) NO-INPUTS PROPERTY — identity with null metrics -----------------------")
z = get(f"/properties/{BIRCH}/investment/metrics")
assert z["purchase_price"] is None and z["cap_rate"] is None and z["months_available"] == 0
print("   no investment row → all inputs/metrics None ✓")

print("\n5) PORTFOLIO AGGREGATE is value-weighted ---------------------------------")
put_inv(OAK, purchase_price=PRICE, closing_costs=CLOSING, loan_amount=LOAN, purchase_date="2023-06-01")
port = get("/investments")
ids = {p["property_id"] for p in port["properties"]}
assert OAK in ids and BIRCH not in ids
props = port["properties"]
# Cap-rate aggregate = Σ cap-NOI / Σ price over every property with data.
num = sum(cap_noi(p) for p in props if cap_noi(p) is not None and p["purchase_price"] > 0)
den = sum(p["purchase_price"] for p in props if cap_noi(p) is not None and p["purchase_price"] > 0)
assert abs(port["cap_rate"] - num / den) < EPS, (port["cap_rate"], num / den)
# Cash-on-cash aggregate counts only full-year properties.
elig = [p for p in props if not p["annualized"] and p["t12_months"] > 0 and p["equity_invested"] > 0]
coc = sum(p["t12_cash_flow"] for p in elig) / sum(p["equity_invested"] for p in elig)
assert abs(port["cash_on_cash"] - coc) < EPS and port["cash_on_cash_property_count"] == len(elig)
print(f"   {len(props)} props; cap={port['cap_rate']*100:.2f}% (ΣcapNOI/Σprice), "
      f"CoC={port['cash_on_cash']*100:.2f}% over {len(elig)} full-year props ✓")

print("\n6) CLEANUP (only the test's own Oak row) ---------------------------------")
r = client.delete(f"/properties/{OAK}/investment", headers=H)
assert r.status_code == 204, r.text
assert get(f"/properties/{OAK}/investment/metrics")["purchase_price"] is None
print("   Oak investment row removed; hand-entered rows untouched (re-runnable) ✓")

db.close()
print("\nALL INVESTMENT-INSIGHTS CHECKS PASSED.")
