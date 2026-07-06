"""Ground-truth verification of the attention feed (sub-step 3).

Run:  PYTHONPATH=.pydeps python3 verify_phase6.py

Asserts the feed at the planted-anomaly month (2025-06) surfaces EXACTLY the four
anomalies seeded by app/seed.py — by type, property, category, and hand-computed
magnitude — and NOTHING else. A feed that merely "looks plausible" would pass a weaker
check; this fails unless the engine matches the answer key precisely.
"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
tok = client.post("/auth/login", data={"username": "admin@example.com", "password": "admin12345"}).json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}

r = client.get("/portfolio/attention?from=2025-06-01&to=2025-06-01", headers=H)
assert r.status_code == 200, r.text
feed = r.json()
items = feed["items"]

print(f"period {feed['period_from']}..{feed['period_to']}; thresholds={feed['thresholds']}")
print(f"{len(items)} item(s), ranked by magnitude:")
for it in items:
    cat = f" / {it['category']}" if it["category"] else ""
    print(f"  [{it['type']:<13}] {it['property_name']}{cat:<22} "
          f"mag=${it['magnitude']:>10,.0f}  — {it['label']}")

# ---- the answer key (from app/seed.py): relative thresholds + root-cause linking ----
# Triggers are %-based, so Cedar's spike ALSO shows up as a ~26% NOI drop — but root-cause
# linking recognizes the magnitudes reconcile (opex rose ~$29.4k, NOI fell ~$29.4k) and merges
# the NOI drop INTO the spike. So exactly four items, keyed by (type, property).
by_tp = {(it["type"], it["property_name"]): it for it in items}

assert len(items) == 4, f"expected exactly 4 items, got {len(items)}"
assert set(by_tp) == {
    ("noi_drop", "Oak Ridge Residences"),
    ("expense_spike", "Cedar Commons"),
    ("vacancy", "Maple Court Apartments"),
    ("missing_data", "Ash Grove Apartments"),
}

# 1. NOI cliff — Oak Ridge, rent concession => ~-52,235 (-58%). Rent-driven, no spike → stays.
noi = by_tp[("noi_drop", "Oak Ridge Residences")]
assert -53_000 <= noi["change"] <= -51_500, noi["change"]
assert -60 <= noi["pct_change"] <= -56, noi["pct_change"]

# 2. Expense spike — Cedar / Repairs & Maintenance, +~29,432 (+448%), WITH the merged NOI drop.
sp = by_tp[("expense_spike", "Cedar Commons")]
assert sp["category"] == "Repairs & Maintenance", sp
assert 29_000 <= sp["change"] <= 30_000 and sp["pct_change"] > 400, sp
assert "drove_noi_down" in sp["detail"], "linking should annotate the spike with the NOI effect"
assert abs(sp["detail"]["drove_noi_down"] - 29_438) < 50, sp["detail"]

# Cedar's NOI drop was MERGED into the spike, so it must NOT appear as a standalone item.
assert ("noi_drop", "Cedar Commons") not in by_tp

# 3. Vacancy — Maple Court, occupancy fell 2.2pp (45 -> 44), $1,500 lost rent.
vac = by_tp[("vacancy", "Maple Court Apartments")]
assert vac["detail"]["units_lost"] == 1 and vac["detail"]["total_units"] == 45, vac["detail"]
assert abs(vac["magnitude"] - 1500) < 1, vac["magnitude"]

# 4. Missing data — Ash Grove (no June records) is present (keyed above).

# Ranking is strictly by magnitude, descending.
mags = [it["magnitude"] for it in items]
assert mags == sorted(mags, reverse=True), mags

# Negative control: no property other than the four planted ones appears.
assert {p for _, p in by_tp} == {
    "Oak Ridge Residences", "Cedar Commons", "Maple Court Apartments", "Ash Grove Apartments"
}

print("\nEXACT MATCH: feed == planted answer key (relative + linking; 4 items). PASSED.")

# Sanity: the default (latest) month should be quiet — no planted anomalies in Dec 2025.
latest = client.get("/portfolio/attention", headers=H).json()
print(f"\nlatest month {latest['period_to']}: {len(latest['items'])} item(s) "
      f"(expected 0 — no anomalies planted there)")
assert len(latest["items"]) == 0, latest["items"]
print("Quiet-month control PASSED.")

# Period-wide: viewing the full year surfaces the June anomalies even though Dec is the end
# month — each item is tagged with the month it happened. This is the mid-period case.
ytd = client.get("/portfolio/attention?from=2025-01-01&to=2025-12-01", headers=H).json()
assert len(ytd["items"]) == 4, len(ytd["items"])
assert all(it["month"] == "2025-06-01" for it in ytd["items"])
print(f"Period-wide (Jan–Dec 2025): {len(ytd['items'])} items, all tagged month=2025-06 ✓")
