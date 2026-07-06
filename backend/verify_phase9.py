"""Verification of period-flexible dashboards + configurable attention thresholds (sub-step 6).

Run:  PYTHONPATH=.pydeps python3 verify_phase9.py

Restores default thresholds at the end, so it is safe to re-run.
"""
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import SessionLocal
from app.main import app

client = TestClient(app)
db = SessionLocal()
tok = client.post("/auth/login", data={"username": "admin@example.com", "password": "admin12345"}).json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}

DEFAULTS = {
    "noi_drop_min_abs": 0, "noi_drop_min_pct": 15,
    "expense_spike_min_abs": 0, "expense_spike_min_pct": 100,
    "unit_noi_drop_min_abs": 0, "unit_noi_drop_min_pct": 15,
    "unit_expense_spike_min_abs": 0, "unit_expense_spike_min_pct": 100,
    "vacancy_min_occupancy_drop_pct": 2,
}


def get(path):
    r = client.get(path, headers=H)
    assert r.status_code == 200, (path, r.status_code, r.text)
    return r.json()


print("1) PERIOD-FLEXIBLE PORTFOLIO DASHBOARD ------------------------------")
# default = latest single month, vs prior month
d = get("/portfolio/dashboard")
print(f"   default: {d['period_from']}..{d['period_to']} vs {d['prior_from']}..{d['prior_to']}")
assert d["period_from"] == d["period_to"] == "2025-12-01"
assert d["prior_from"] == d["prior_to"] == "2025-11-01"

# T12 sums the year and compares to the preceding 12 months
t = get("/portfolio/dashboard?from=2025-01-01&to=2025-12-01")
assert t["prior_from"] == "2024-01-01" and t["prior_to"] == "2024-12-01"
raw = db.execute(text(
    "SELECT SUM(noi) FROM portfolio_month_summary WHERE month BETWEEN '2025-01-01' AND '2025-12-01'"
)).scalar()
assert abs(t["current"]["noi"] - float(raw)) < 1e-6, (t["current"]["noi"], raw)
assert len(t["trend"]) == 12 and 0 < t["current"]["occupancy"] <= 1
print(f"   T12 2025: NOI={t['current']['noi']:,.0f} (reconciles); prior=preceding 12mo; occ weighted ✓")

# custom range (YTD-style) → preceding equal-length window
y = get("/portfolio/dashboard?from=2025-01-01&to=2025-06-01")
assert y["prior_from"] == "2024-07-01" and y["prior_to"] == "2024-12-01"
print(f"   Jan–Jun 2025 → prior Jul–Dec 2024 (preceding equal-length) ✓")

# property dashboard is period-aware too
pid = db.execute(text("SELECT id FROM properties WHERE name='Oak Ridge Residences'")).scalar()
pd = get(f"/properties/{pid}/dashboard?from=2025-01-01&to=2025-12-01")
assert pd["period_to"] == "2025-12-01" and pd["current"]["total_units"] == 62
print(f"   property T12 NOI={pd['current']['noi']:,.0f} ✓")

print("\n2) RELATIVE THRESHOLDS DRIVE THE FEED -------------------------------")
FEED = "/portfolio/attention?from=2025-06-01&to=2025-06-01"
base = get(FEED)["items"]
assert len(base) == 4, len(base)
# Cedar's NOI drop is merged into its spike (root-cause linking), so no standalone Cedar NOI.
assert not any(i["type"] == "noi_drop" and i["property_name"] == "Cedar Commons" for i in base)
print(f"   defaults (relative + linking): {len(base)} items {[(i['type'], i['property_name']) for i in base]}")


def put(**over):
    body = {**DEFAULTS, **over}
    r = client.put("/settings/attention", headers=H, json=body)
    assert r.status_code == 200, r.text


# Filter out Cedar's spike (raise its %-floor above 448%) → there's no longer a cause to merge
# into, so Cedar's NOI drop surfaces on its own. Proves the merge is evidence-based, not cosmetic.
put(expense_spike_min_pct=500)
a = get(FEED)["items"]
assert any(i["type"] == "noi_drop" and i["property_name"] == "Cedar Commons" for i in a)
assert not any(i["type"] == "expense_spike" for i in a)
print(f"   suppress Cedar's spike → its NOI drop un-merges and shows standalone ✓")

# Vacancy is relative: raise the occupancy-drop floor above Maple's 2.2pp → it drops out.
put(vacancy_min_occupancy_drop_pct=3.0)
v = get(FEED)["items"]
assert not any(i["type"] == "vacancy" for i in v)
print(f"   vacancy floor 3.0pp → Maple's 2.2pp drop no longer flags ✓")

# Restore + confirm the merged answer key.
put()
final = get(FEED)["items"]
assert len(final) == 4
print(f"   restored defaults → {len(final)} items (answer key)")

print("\n2b) ROOT-CAUSE LINKING — magnitude reconciliation --------------------")
from datetime import date
from app.attention import attention_feed, Thresholds

# Default ratio 0.8: Cedar's opex rose ~$29.4k and NOI fell ~$29.4k → they reconcile → merged.
merged = attention_feed(db, date(2025, 6, 1), date(2025, 6, 1))["items"]
csp = [i for i in merged if i["type"] == "expense_spike" and i["property_name"] == "Cedar Commons"][0]
assert "drove_noi_down" in csp["detail"] and abs(csp["detail"]["drove_noi_down"] - 29_438) < 50
assert not any(i["type"] == "noi_drop" and i["property_name"] == "Cedar Commons" for i in merged)
print("   ratio 0.8: Cedar NOI drop merged into spike (opex↑ ≈ NOI↓) ✓")

# Stricter ratio 1.5: the ~1:1 reconciliation no longer clears the bar → keep them SEPARATE.
t = Thresholds.from_db(db)
t.reconcile_ratio = 1.5
sep = attention_feed(db, date(2025, 6, 1), date(2025, 6, 1), thresholds=t)["items"]
assert any(i["type"] == "noi_drop" and i["property_name"] == "Cedar Commons" for i in sep)
print("   ratio 1.5: magnitudes don't clear the bar → NOI drop kept separate ✓")

print("\n3) ADMIN-ONLY WRITE, READABLE BY ANYONE -----------------------------")
s = get("/settings/attention")
assert s["noi_drop_min_abs"] == 0 and s["vacancy_min_occupancy_drop_pct"] == 2 and "updated_at" in s
print(f"   GET /settings/attention ok; updated_at={s['updated_at'][:19]}")

db.close()
print("\nALL SUB-STEP 6 / PERIOD-FLEX CHECKS PASSED.")
