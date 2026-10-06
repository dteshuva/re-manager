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

# `arrears` (migration 0025) is held apart from the answer key below on purpose. The planted
# anomalies this script is the ground truth for are all CHANGE events — something moved versus
# the prior month — whereas an arrears balance is a LEVEL: it qualifies in every month it goes
# unpaid, and the seed's delinquent units (see `_DELINQUENT_UNITS` in app/seed.py) plus its
# brought-forward `opening_arrears` tenancies are deliberately in arrears all the way through
# the window. Counting them in "exactly four items" would make this test a count of two
# unrelated things. They get their own assertions at the end instead.
change_items = [it for it in items if it["type"] != "arrears"]
arrears_items = [it for it in items if it["type"] == "arrears"]

# ---- the answer key (from app/seed.py): relative thresholds + root-cause linking ----
# Triggers are %-based, so Cedar's spike ALSO shows up as a ~26% NOI drop — but root-cause
# linking recognizes the magnitudes reconcile (opex rose ~$29.4k, NOI fell ~$29.4k) and merges
# the NOI drop INTO the spike. So exactly four items, keyed by (type, property).
by_tp = {(it["type"], it["property_name"]): it for it in change_items}

assert len(change_items) == 4, f"expected exactly 4 change items, got {len(change_items)}"
assert set(by_tp) == {
    ("noi_drop", "Oak Ridge Residences"),
    ("expense_spike", "Cedar Commons"),
    ("occupancy_drop", "Maple Court Apartments"),
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
# Reported at PROPERTY grain as `occupancy_drop`: the change-based detector gated by
# `vacancy_min_occupancy_drop_pct` (2.2pp clears the 2.0pp floor). The unit-grain vacancy
# it explains is folded INTO it, so one move-out is one row that carries both the occupancy
# movement and the affected unit number — see _fold_vacancies_into_occupancy_drops.
vac = by_tp[("occupancy_drop", "Maple Court Apartments")]
assert vac["detail"]["units_lost"] == 1, vac["detail"]
assert vac["detail"]["occupied_before"] == 45 and vac["detail"]["occupied_after"] == 44, vac["detail"]
assert abs(vac["detail"]["occupancy_pp_drop"] - 2.2) < 0.1, vac["detail"]
assert vac["detail"]["vacated_units"] == ["105"], vac["detail"]
assert vac["unit_number"] is None, "property-grain item carries no single unit"
assert abs(vac["magnitude"] - 1500) < 1, vac["magnitude"]

# 4. Missing data — Ash Grove (no June records) is present (keyed above).

# Ranking is strictly by magnitude, descending — across every type, arrears included.
mags = [it["magnitude"] for it in items]
assert mags == sorted(mags, reverse=True), mags

# Negative control: no property other than the four planted ones appears.
assert {p for _, p in by_tp} == {
    "Oak Ridge Residences", "Cedar Commons", "Maple Court Apartments", "Ash Grove Apartments"
}

print("\nEXACT MATCH: feed == planted answer key (relative + linking; 4 items). PASSED.")

# ---- arrears (migration 0025): the seed's own answer key for the LEVEL detector ----
# The seed plants arrears two ways, and both must show here: `_DELINQUENT_UNITS` (five units
# whose contract rent sits a stated % above what they actually pay) and `opening_arrears` (a
# sparse set of tenancies that arrive already owing). Every named item must identify a door and
# a tenant — an arrears row you can't act on is useless — and no unit may appear twice, since a
# standing balance would otherwise emit one row per month it stays unpaid.
named = [it for it in arrears_items if not it["rolled_up"]]
assert named, "the seed's delinquent units should raise arrears items"
assert all(it["unit_number"] and it["detail"]["tenant_name"] for it in named), named
assert len({it["unit_id"] for it in named}) == len(named), "a unit was reported twice"
assert all(it["magnitude"] > 0 for it in arrears_items), "a credit balance must never flag"
# A rolled-up arrears row is the TAIL (the debtors below the named ones), one per property —
# never a magnitude cluster asserting a shared cause, which is what rolls up elsewhere.
assert all(it["unit_id"] is None and it["count"] > 0 for it in arrears_items if it["rolled_up"])
print(f"arrears: {len(named)} named debtor(s) + "
      f"{len(arrears_items) - len(named)} per-property tail row(s) ✓")

# Sanity: the default (latest) month is quiet of CHANGE anomalies — none are planted in Dec
# 2025. Arrears is deliberately NOT expected to go quiet: an unpaid balance is still unpaid in
# a month where nothing moved, and a feed that forgot it the moment the shortfall stopped
# growing would be the bug, not the control.
latest = client.get("/portfolio/attention", headers=H).json()
latest_change = [it for it in latest["items"] if it["type"] != "arrears"]
print(f"\nlatest month {latest['period_to']}: {len(latest_change)} change item(s) "
      f"(expected 0 — no anomalies planted there), "
      f"{len(latest['items']) - len(latest_change)} standing arrears item(s)")
assert len(latest_change) == 0, latest_change
print("Quiet-month control PASSED.")

# Period-wide: viewing the full year surfaces the June anomalies even though Dec is the end
# month — each item is tagged with the month it happened. This is the mid-period case.
ytd = client.get("/portfolio/attention?from=2025-01-01&to=2025-12-01", headers=H).json()
ytd_change = [it for it in ytd["items"] if it["type"] != "arrears"]
assert len(ytd_change) == 4, len(ytd_change)
assert all(it["month"] == "2025-06-01" for it in ytd_change)
print(f"Period-wide (Jan–Dec 2025): {len(ytd_change)} change items, all tagged month=2025-06 ✓")
