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
from sqlalchemy import select, text

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
MONTH = r.json()[1]["month"]  # 2nd seeded month; the seed window is Jan 2024 - Dec 2025
print(f"   {len(r.json())} months; {MONTH} NOI={r.json()[1]['noi']} cash_flow={r.json()[1]['cash_flow']}")

print("\n2) PROPERTY MONTHLY + HONEST SPLIT (Maple, multifamily) -------------")
r = client.get(f"/properties/{maple.id}/monthly", headers=H)
assert r.status_code == 200, r.text
# Pick a month that actually carries property-tier CAPEX — that's what makes the
# unit-vs-tier split visible. Derived instead of hard-coded (this used to assume a
# specific calendar month and a specific $ amount, both of which the seed has since
# changed: capex now lands in March at units x $200, not February at a flat $8,000).
months = r.json()
feb = next((x for x in months if x["property_tier"]["capex"] > 0), None)
assert feb is not None, "no month with property-tier capex — seed fixture changed"
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
# Capex/debt are property-tier facts; assert they're PRESENT and tier-only rather than
# pinning exact dollars, so re-scaling the seed doesn't break the invariant being tested.
assert feb["property_tier"]["capex"] > 0
assert feb["property_tier"]["debt_service"] > 0
print("   OK: total == units + property_tier; capex/debt only at property tier (honest)")

print("\n3) PER-UNIT BREAKDOWN (Maple) ---------------------------------------")
r = client.get(f"/properties/{maple.id}/units/monthly", headers=H)
assert r.status_code == 200, r.text
rows = r.json()
# Derived, not hard-coded: this said "3 units x 3 months = 9" from a much smaller seed.
n_units = db.scalar(text("SELECT count(*) FROM units WHERE property_id = :p"), {"p": maple.id})
n_months = len(months)
print(f"   {len(rows)} unit-month rows ({n_units} units x {n_months} months expected)")
assert len(rows) == n_units * n_months, (len(rows), n_units, n_months)
sample = rows[0]
print(f"   {sample['unit_number']} {sample['month']}: rent={sample['gross_rent']} "
      f"opex={sample['operating_expenses']} NOI={sample['noi']} capex={sample['capex']}")
assert all(x["capex"] == 0 and x["debt_service"] == 0 for x in rows), "units carry no tier items"

print("\n4) SINGLE-UNIT MONTHLY (Maple 101) ----------------------------------")
r = client.get(f"/units/{unit101.id}/monthly", headers=H)
assert r.status_code == 200, r.text
first_um = r.json()[0]
print(f"   {len(r.json())} months; {first_um['month']} rent={first_um['gross_rent']} "
      f"NOI={first_um['noi']} unit={first_um['unit_number']}")
assert r.json()[0]["unit_id"] == unit101.id

print("\n5) FROM/TO RANGE FILTER ---------------------------------------------")
r = client.get(f"/portfolio/monthly?from={MONTH}&to={MONTH}", headers=H)
assert r.status_code == 200 and len(r.json()) == 1 and r.json()[0]["month"] == MONTH
print(f"   ?from={MONTH}&to={MONTH} -> {len(r.json())} month ({MONTH} only)")

print("\n6) 404s -------------------------------------------------------------")
missing = "00000000-0000-0000-0000-000000000000"
assert client.get(f"/properties/{missing}/monthly", headers=H).status_code == 404
assert client.get(f"/units/{missing}/monthly", headers=H).status_code == 404
print("   unknown property/unit -> 404")

# Single-asset property: no units, so per-unit breakdown is empty but property total works.
print("\n7) SINGLE-ASSET (Birch) has no units ---------------------------------")
assert client.get(f"/properties/{birch.id}/units/monthly", headers=H).json() == []
birch_months = client.get(f"/properties/{birch.id}/monthly", headers=H).json()
assert len(birch_months) == n_months, (len(birch_months), n_months)
print(f"   per-unit breakdown empty; property monthly still returns {len(birch_months)} months")

print("\n8) PORTFOLIO BREAKDOWN (portfolio -> property -> unit) ----------------")
import math
bd = client.get("/portfolio/breakdown", headers=H).json()
pm = client.get("/portfolio/monthly", headers=H).json()
tot_noi, tot_cf = sum(r["noi"] for r in pm), sum(r["cash_flow"] for r in pm)
assert math.isclose(bd["total"]["noi"], tot_noi) and math.isclose(bd["total"]["cash_flow"], tot_cf)
print(f"   portfolio total reconciles with /portfolio/monthly: NOI={bd['total']['noi']}")
mp = next(p for p in bd["properties"] if p["property_name"] == "Maple Court Apartments")
assert len(mp["units"]) == n_units, (len(mp["units"]), n_units)  # was hard-coded 3
# honest: property total = units sum + property-tier (NOT just the unit sum)
units_noi = sum(u["noi"] for u in mp["units"])
assert math.isclose(mp["noi"], units_noi + mp["property_tier"]["noi"])
assert mp["property_tier"]["capex"] > 0 and all(u["capex"] == 0 for u in mp["units"])
print(f"   Maple: total NOI {mp['noi']} = units {units_noi} + tier {mp['property_tier']['noi']}; tier-only capex held at property")

db.close()
print("\nALL PHASE 2 CHECKS PASSED.")
