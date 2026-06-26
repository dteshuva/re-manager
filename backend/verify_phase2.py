"""Ad-hoc Phase 2 verification (not part of the deliverable test suite).

Exercises the aggregation endpoints and their rollup guarantees against a seeded DB:
  1. portfolio monthly P&L (rent / opex / NOI / below-line / cash flow)
  2. property monthly P&L with the honest unit-vs-property-tier split
     (total == units + property_tier per metric; capex/debt live only at the tier)
  3. per-unit monthly breakdown for a multifamily property
  4. single-unit monthly P&L
  5. from/to month-range filtering
  6. 404s for unknown property / unit
"""
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import Property, Unit

client = TestClient(app)
db = SessionLocal()

METRICS = (
    "gross_rent", "operating_expenses", "noi", "capex",
    "debt_service", "other_below_line", "below_noi", "cash_flow",
)


def login(email, password):
    r = client.post("/auth/login", data={"username": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


token = login("admin@example.com", "admin12345")
H = {"Authorization": f"Bearer {token}"}

maple = db.scalar(select(Property).where(Property.name == "Maple Court Apartments"))
birch = db.scalar(select(Property).where(Property.name == "Birch Street House"))
unit101 = db.scalar(select(Unit).where(Unit.unit_number == "101", Unit.property_id == maple.id))

print("1) PORTFOLIO MONTHLY ------------------------------------------------")
r = client.get("/portfolio/monthly", headers=H)
assert r.status_code == 200, r.text
print(f"   {len(r.json())} months; 2026-02 NOI={r.json()[1]['noi']} cash_flow={r.json()[1]['cash_flow']}")

print("\n2) PROPERTY MONTHLY + HONEST SPLIT (Maple, multifamily) -------------")
r = client.get(f"/properties/{maple.id}/monthly", headers=H)
assert r.status_code == 200, r.text
feb = next(x for x in r.json() if x["month"] == "2026-02-01")
print(f"   total:        rent={feb['gross_rent']} opex={feb['operating_expenses']} "
      f"capex={feb['capex']} debt={feb['debt_service']} NOI={feb['noi']} cf={feb['cash_flow']}")
print(f"   units:        rent={feb['units']['gross_rent']} capex={feb['units']['capex']} "
      f"debt={feb['units']['debt_service']}")
print(f"   property_tier:rent={feb['property_tier']['gross_rent']} capex={feb['property_tier']['capex']} "
      f"debt={feb['property_tier']['debt_service']}")
# total == units + property_tier, every metric
for m in METRICS:
    assert abs(feb[m] - (feb["units"][m] + feb["property_tier"][m])) < 1e-6, m
# capex / debt service are NOT allocated to units -> live only at the property tier
assert feb["units"]["capex"] == 0 and feb["units"]["debt_service"] == 0
assert feb["property_tier"]["capex"] == 8000.0  # Feb roof replacement
assert feb["property_tier"]["debt_service"] == 2600.0
print("   OK: total == units + property_tier; capex/debt only at property tier (honest)")

print("\n3) PER-UNIT BREAKDOWN (Maple) ---------------------------------------")
r = client.get(f"/properties/{maple.id}/units/monthly", headers=H)
assert r.status_code == 200, r.text
rows = r.json()
print(f"   {len(rows)} unit-month rows (3 units x 3 months = 9 expected: {len(rows)})")
assert len(rows) == 9
sample = rows[0]
print(f"   {sample['unit_number']} {sample['month']}: rent={sample['gross_rent']} "
      f"opex={sample['operating_expenses']} NOI={sample['noi']} capex={sample['capex']}")
assert all(x["capex"] == 0 and x["debt_service"] == 0 for x in rows), "units carry no tier items"

print("\n4) SINGLE-UNIT MONTHLY (Maple 101) ----------------------------------")
r = client.get(f"/units/{unit101.id}/monthly", headers=H)
assert r.status_code == 200, r.text
print(f"   {len(r.json())} months; 2026-01 rent={r.json()[0]['gross_rent']} "
      f"NOI={r.json()[0]['noi']} unit={r.json()[0]['unit_number']}")
assert r.json()[0]["unit_id"] == unit101.id

print("\n5) FROM/TO RANGE FILTER ---------------------------------------------")
r = client.get("/portfolio/monthly?from=2026-02-01&to=2026-02-01", headers=H)
assert r.status_code == 200 and len(r.json()) == 1 and r.json()[0]["month"] == "2026-02-01"
print(f"   ?from=2026-02-01&to=2026-02-01 -> {len(r.json())} month (2026-02 only)")

print("\n6) 404s -------------------------------------------------------------")
missing = "00000000-0000-0000-0000-000000000000"
assert client.get(f"/properties/{missing}/monthly", headers=H).status_code == 404
assert client.get(f"/units/{missing}/monthly", headers=H).status_code == 404
print("   unknown property/unit -> 404")

# Single-asset property: no units, so per-unit breakdown is empty but property total works.
print("\n7) SINGLE-ASSET (Birch) has no units ---------------------------------")
assert client.get(f"/properties/{birch.id}/units/monthly", headers=H).json() == []
assert len(client.get(f"/properties/{birch.id}/monthly", headers=H).json()) == 3
print("   per-unit breakdown empty; property monthly still returns 3 months")

print("\n8) PORTFOLIO BREAKDOWN (portfolio -> property -> unit) ----------------")
import math
bd = client.get("/portfolio/breakdown", headers=H).json()
pm = client.get("/portfolio/monthly", headers=H).json()
tot_noi, tot_cf = sum(r["noi"] for r in pm), sum(r["cash_flow"] for r in pm)
assert math.isclose(bd["total"]["noi"], tot_noi) and math.isclose(bd["total"]["cash_flow"], tot_cf)
print(f"   portfolio total reconciles with /portfolio/monthly: NOI={bd['total']['noi']}")
mp = next(p for p in bd["properties"] if p["property_name"] == "Maple Court Apartments")
assert len(mp["units"]) == 3
# honest: property total = units sum + property-tier (NOT just the unit sum)
units_noi = sum(u["noi"] for u in mp["units"])
assert math.isclose(mp["noi"], units_noi + mp["property_tier"]["noi"])
assert mp["property_tier"]["capex"] > 0 and all(u["capex"] == 0 for u in mp["units"])
print(f"   Maple: total NOI {mp['noi']} = units {units_noi} + tier {mp['property_tier']['noi']}; tier-only capex held at property")

db.close()
print("\nALL PHASE 2 CHECKS PASSED.")
