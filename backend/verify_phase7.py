"""Verification of property detail (sub-step 4): scoped KPI band, property-scoped unit
feed, and the unit roster — checked against the planted answer key (June 2025).

Run:  PYTHONPATH=.pydeps python3 verify_phase7.py
"""
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import Property

client = TestClient(app)
db = SessionLocal()
tok = client.post("/auth/login", data={"username": "admin@example.com", "password": "admin12345"}).json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}

pid = {p.name: p.id for p in db.scalars(select(Property)).all()}
M = "2025-06-01"


def get(path):
    r = client.get(path, headers=H)
    assert r.status_code == 200, (path, r.status_code, r.text)
    return r.json()


print("1) SCOPED KPI BAND (Oak Ridge, the NOI-cliff property) --------------")
d = get(f"/properties/{pid['Oak Ridge Residences']}/dashboard?from={M}&to={M}")
assert d["property_name"] == "Oak Ridge Residences" and d["type"] == "multifamily"
cur, pri = d["current"], d["prior"]
noi_change = cur["noi"] - pri["noi"]
print(f"   {d['period_to']} vs {d['prior_to']}: NOI {pri['noi']:,.0f} -> {cur['noi']:,.0f} "
      f"(Δ {noi_change:,.0f}); occupancy {cur['occupancy']}; trend pts {len(d['trend'])}")
assert -53_000 <= noi_change <= -51_500, noi_change
assert cur["total_units"] == 62 and cur["occupied_units"] == 62  # rent dropped but >0 → still occupied
assert len(d["trend"]) == 12
# reconcile scoped band against the property monthly P&L (live view)
pm = get(f"/properties/{pid['Oak Ridge Residences']}/monthly?from={M}&to={M}")[0]
assert abs(cur["noi"] - pm["noi"]) < 1e-6 and abs(cur["gross_rent"] - pm["gross_rent"]) < 1e-6
print("   scoped KPI band reconciles with /properties/:id/monthly ✓")

print("\n2) PROPERTY-SCOPED FEED ---------------------------------------------")


def change_items(items: list[dict]) -> list[dict]:
    """Everything except `arrears` (migration 0025). The planted anomalies this section is the
    answer key for are CHANGE events — something moved versus the prior month. Arrears is a
    LEVEL: the seed's delinquent units are in arrears in every month of the window by design,
    so they are correctly present in each of these feeds and would break an exact count of a
    different thing. See verify_phase15.py for the arrears answer key itself."""
    return [i for i in items if i["type"] != "arrears"]


# Cedar Commons: exactly one CHANGE item — the property-tier Repairs & Maintenance spike.
cedar_all = get(f"/properties/{pid['Cedar Commons']}/attention?from={M}&to={M}")["items"]
cedar = change_items(cedar_all)
print(f"   Cedar Commons: {len(cedar)} change item(s) -> {[(i['type'], i['category'], i['unit_number']) for i in cedar]}")
assert len(cedar) == 1, cedar
assert cedar[0]["type"] == "expense_spike" and cedar[0]["category"] == "Repairs & Maintenance"
assert cedar[0]["unit_id"] is None  # property/tier-scoped, not a unit
assert 29_000 <= cedar[0]["change"] <= 30_000
# ...and the property-scoped feed carries arrears too, at unit grain (the operator shouldn't
# have to go back to the portfolio view to see who owes money in the building they opened).
cedar_arrears = [i for i in cedar_all if i["type"] == "arrears" and not i["rolled_up"]]
assert cedar_arrears and all(i["unit_number"] and i["magnitude"] > 0 for i in cedar_arrears)
print(f"   Cedar Commons: {len(cedar_arrears)} unit(s) in arrears alongside it ✓")

# Maple Court: ONE item for the whole move-out event. Two root-cause links collapse what
# used to be three separate rows (unit vacancy + unit NOI drop + property occupancy drop):
#   `_link_vacancy_to_noi_drop`            — a vacated unit's NOI drop is the same event,
#                                            so it folds into the vacancy (NOI in detail);
#   `_fold_vacancies_into_occupancy_drops` — the surviving vacancy folds into the
#                                            property-level occupancy drop that explains it.
maple = change_items(get(f"/properties/{pid['Maple Court Apartments']}/attention?from={M}&to={M}")["items"])
print(f"   Maple Court:   {len(maple)} change item(s) -> {[(i['type'], i['unit_number']) for i in maple]}")
assert len(maple) == 1, maple
item = maple[0]
assert item["type"] == "occupancy_drop", item
assert item["detail"]["vacated_units"] == ["105"], item["detail"]
assert item["detail"]["units_lost"] == 1, item["detail"]
assert abs(item["magnitude"] - 1500) < 1, item["magnitude"]

# Oak Ridge: the rent concession drops EVERY unit's NOI by the same amount — a
# building-wide event. `_cluster_and_rollup` (ROLLUP_MIN_COUNT=3) collapses that tight
# cluster of near-identical per-unit items into ONE rolled-up row rather than flooding the
# feed with 62 duplicates. This assertion used to expect the 62 individual items, from
# before the roll-up existed.
oak = change_items(get(f"/properties/{pid['Oak Ridge Residences']}/attention?from={M}&to={M}")["items"])
types = {i["type"] for i in oak}
print(f"   Oak Ridge:     {len(oak)} change item(s); types={types}")
assert len(oak) == 1, oak
assert types == {"noi_drop"}
rolled = oak[0]
assert rolled.get("rolled_up") is True, rolled
assert rolled["count"] == 62, rolled["count"]

# A non-anomaly property is quiet of change events (it may still carry standing arrears —
# a tenant owing money is not an anomaly of the month, which is the whole distinction here).
pine = change_items(get(f"/properties/{pid['Pine Valley Tower']}/attention?from={M}&to={M}")["items"])
print(f"   Pine Valley:   {len(pine)} change item(s) (expected 0)")
assert len(pine) == 0
print("   property-scoped feeds match the answer key ✓")

print("\n3) UNIT ROSTER (Maple Court, with the vacant unit) ------------------")
roster = get(f"/properties/{pid['Maple Court Apartments']}/units/roster?month={M}&limit=100")
assert roster["total"] == 45 and len(roster["rows"]) == 45
vacant = [r for r in roster["rows"] if r["status"] == "vacant"]
print(f"   {roster['total']} units; {len(vacant)} vacant -> "
      f"unit {vacant[0]['unit_number']} rent={vacant[0]['gross_rent']} noiΔ={vacant[0]['noi_change']:.0f}")
assert len(vacant) == 1 and vacant[0]["unit_number"] == "105"
assert vacant[0]["gross_rent"] == 0 and vacant[0]["noi_change"] < 0

# sort + paginate: top NOI unit first, page size honored
page = get(f"/properties/{pid['Maple Court Apartments']}/units/roster?month={M}&sort=noi&order=desc&limit=5")
nois = [r["noi"] for r in page["rows"]]
assert len(page["rows"]) == 5 and nois == sorted(nois, reverse=True)
print(f"   sort=noi desc, limit=5 -> {len(page['rows'])} rows, NOIs descending ✓")

print("\n4) 404s -------------------------------------------------------------")
missing = "00000000-0000-0000-0000-000000000000"
assert client.get(f"/properties/{missing}/dashboard", headers=H).status_code == 404
assert client.get(f"/properties/{missing}/attention", headers=H).status_code == 404
assert client.get(f"/properties/{missing}/units/roster", headers=H).status_code == 404
print("   unknown property -> 404 on all three endpoints")

db.close()
print("\nALL SUB-STEP 4 CHECKS PASSED.")
