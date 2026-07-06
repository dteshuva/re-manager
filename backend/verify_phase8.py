"""Verification of unit detail (sub-step 5, Level 3).

Run:  PYTHONPATH=.pydeps python3 verify_phase8.py
"""
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import Property, Unit

client = TestClient(app)
db = SessionLocal()
tok = client.post("/auth/login", data={"username": "admin@example.com", "password": "admin12345"}).json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}

maple = db.scalar(select(Property).where(Property.name == "Maple Court Apartments"))
oak = db.scalar(select(Property).where(Property.name == "Oak Ridge Residences"))
unit105 = db.scalar(select(Unit).where(Unit.property_id == maple.id, Unit.unit_number == "105"))
oak_unit = db.scalar(select(Unit).where(Unit.property_id == oak.id, Unit.unit_number == "100"))


def get(path):
    r = client.get(path, headers=H)
    assert r.status_code == 200, (path, r.status_code, r.text)
    return r.json()


print("1) VACANT UNIT (Maple 105) — vacancy is visible, not a gap ----------")
d = get(f"/units/{unit105.id}/detail")
assert d["property_name"] == "Maple Court Apartments" and d["unit_number"] == "105"
assert len(d["months"]) == 24, len(d["months"])  # Maple has all 24 months
occ = [m for m in d["months"] if m["status"] == "occupied"]
vac = [m for m in d["months"] if m["status"] == "vacant"]
print(f"   {len(d['months'])} months: {len(occ)} occupied, {len(vac)} vacant; header status={d['status']}")
# Vacant from 2025-06 onward (Jun..Dec = 7 months), occupied before.
assert len(vac) == 7 and len(occ) == 17
assert all(m["status"] == "vacant" for m in d["months"] if m["month"] >= "2025-06-01")
assert d["status"] == "vacant"  # latest month
june = next(m for m in d["months"] if m["month"] == "2025-06-01")
may = next(m for m in d["months"] if m["month"] == "2025-05-01")
assert june["gross_rent"] == 0 and june["noi"] <= 0
assert may["gross_rent"] == 1500
print(f"   May rent={may['gross_rent']} -> June rent={june['gross_rent']} (vacant); "
      f"trend shows the drop instead of ending")

print("\n2) OCCUPIED UNIT (Oak Ridge 100) — NOI cliff visible ----------------")
o = get(f"/units/{oak_unit.id}/detail")
assert o["status"] == "occupied"
ojune = next(m for m in o["months"] if m["month"] == "2025-06-01")
omay = next(m for m in o["months"] if m["month"] == "2025-05-01")
print(f"   rent May {omay['gross_rent']} -> June {ojune['gross_rent']} (concession); "
      f"NOI {omay['noi']:.0f} -> {ojune['noi']:.0f}")
assert omay["gross_rent"] == 1650 and ojune["gross_rent"] == 800
assert ojune["noi"] < omay["noi"]

print("\n3) UNIT SCOPE EXCLUDES PROPERTY-TIER ITEMS --------------------------")
# Units carry no debt service (that's property-tier-only); reconcile with /units/:id/monthly.
mono = {m["month"]: m for m in get(f"/units/{oak_unit.id}/monthly")}
assert all(m["debt_service"] == 0 for m in o["months"]), "unit carries no tier debt service"
# spot-reconcile a month against the live single-unit endpoint
assert abs(omay["noi"] - mono["2025-05-01"]["noi"]) < 1e-6
print("   unit debt_service all 0 (tier-only); NOI reconciles with /units/:id/monthly ✓")

print("\n4) 404 ---------------------------------------------------------------")
assert client.get("/units/00000000-0000-0000-0000-000000000000/detail", headers=H).status_code == 404
print("   unknown unit -> 404")

db.close()
print("\nALL SUB-STEP 5 CHECKS PASSED.")
