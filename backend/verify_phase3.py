"""Ad-hoc Phase 3 verification (not part of the deliverable test suite).

Exercises manual CRUD + the month workflow against a running, seeded DB. Creates its
own throwaway properties and deletes them at the end, so it is safe to re-run and does
not disturb the seed data the Phase 1/2 scripts assert on.

  1. property + unit CRUD (incl. units only on multifamily)
  2. idempotent record upsert (re-saving a month replaces, never duplicates)
  3. entry flows through to the computed P&L aggregation
  4. draft → posted → locked workflow; locked months reject edits (423)
  5. admin unlock reopens the month and edits work again
  6. granular line-item add / patch / delete
  7. cascade delete cleans everything up
"""
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app

client = TestClient(app)
db = SessionLocal()


def login(email, password):
    r = client.post("/auth/login", data={"username": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


H = {"Authorization": f"Bearer {login('admin@example.com', 'admin12345')}"}


def post(path, json, expect=201):
    r = client.post(path, json=json, headers=H)
    assert r.status_code == expect, f"{path} -> {r.status_code} {r.text}"
    return r.json()


print("1) PROPERTY + UNIT CRUD ---------------------------------------------")
prop = post("/properties", {"name": "ZZ Test Fourplex", "type": "multifamily", "address": "1 Test Ln"})
single = post("/properties", {"name": "ZZ Test Cottage", "type": "single"})
unit = post(f"/properties/{prop['id']}/units", {"unit_number": "A", "label": "Unit A"})
print(f"   created property {prop['name']} + unit {unit['unit_number']}, and single {single['name']}")
r = client.post(f"/properties/{single['id']}/units", json={"unit_number": "X"}, headers=H)
assert r.status_code == 409, r.text
print("   units rejected on single-asset property -> 409 OK")

# Grab two seeded categories to build line items.
cats = {c["name"]: c for c in client.get("/categories", headers=H).json()}
rent, tax, mortgage = cats["Rent"], cats["Property Tax"], cats["Mortgage"]

print("\n2) IDEMPOTENT RECORD UPSERT -----------------------------------------")
rec = post("/records", {
    "property_id": prop["id"], "unit_id": unit["id"], "month": "2026-05-01",
    "line_items": [
        {"category_id": rent["id"], "amount": 2000},
        {"category_id": tax["id"], "amount": 300},
    ],
})
assert len(rec["line_items"]) == 2
# Re-save the SAME month with different items -> replaces, no duplicate record/items.
rec2 = post("/records", {
    "property_id": prop["id"], "unit_id": unit["id"], "month": "2026-05-15",  # not first-of-month
    "line_items": [{"category_id": rent["id"], "amount": 2100}],
})
assert rec2["id"] == rec["id"], "re-save must hit the same record (keyed on property/unit/month)"
assert rec2["month"] == "2026-05-01", "month normalized to first-of-month"
assert len(rec2["line_items"]) == 1 and rec2["line_items"][0]["amount"] == 2100.0
recs = client.get(f"/records?property_id={prop['id']}", headers=H).json()
assert len(recs) == 1, f"expected 1 record after re-save, got {len(recs)}"
print("   re-saving 2026-05 replaced items in place (1 record, 1 item, amount=2100) OK")

print("\n3) ENTRY -> COMPUTED AGGREGATION ------------------------------------")
# Add a property-tier mortgage (unit_id NULL), then read the property P&L.
post("/records", {
    "property_id": prop["id"], "unit_id": None, "month": "2026-05-01",
    "line_items": [{"category_id": mortgage["id"], "amount": 1800}],
})
pnl = client.get(f"/properties/{prop['id']}/monthly", headers=H).json()[0]
print(f"   property 2026-05: rent={pnl['gross_rent']} debt={pnl['debt_service']} "
      f"NOI={pnl['noi']} cash_flow={pnl['cash_flow']}")
assert pnl["gross_rent"] == 2100.0 and pnl["units"]["gross_rent"] == 2100.0
assert pnl["property_tier"]["debt_service"] == 1800.0 and pnl["units"]["debt_service"] == 0.0
print("   unit rent + property-tier debt rolled up honestly OK")

print("\n4) WORKFLOW: draft -> posted -> locked, edits blocked ---------------")
client.put("/periods", json={"property_id": prop["id"], "month": "2026-05-01", "status": "posted"}, headers=H)
locked = client.put("/periods", json={"property_id": prop["id"], "month": "2026-05-01", "status": "locked"}, headers=H)
assert locked.status_code == 200 and locked.json()["status"] == "locked"
blocked = client.post("/records", json={
    "property_id": prop["id"], "unit_id": unit["id"], "month": "2026-05-01",
    "line_items": [{"category_id": rent["id"], "amount": 9999}],
}, headers=H)
assert blocked.status_code == 423, blocked.text
reopen = client.put("/periods", json={"property_id": prop["id"], "month": "2026-05-01", "status": "posted"}, headers=H)
assert reopen.status_code == 409, "cannot leave locked via PUT; must use admin unlock"
print("   locked month: edit -> 423, PUT-to-posted -> 409 OK")

print("\n5) ADMIN UNLOCK REOPENS ---------------------------------------------")
period_id = client.get(f"/periods?property_id={prop['id']}", headers=H).json()[0]["id"]
assert client.post(f"/periods/{period_id}/unlock", headers=H).json()["status"] == "posted"
ok = client.post("/records", json={
    "property_id": prop["id"], "unit_id": unit["id"], "month": "2026-05-01",
    "line_items": [{"category_id": rent["id"], "amount": 2200}],
}, headers=H)
assert ok.status_code == 201, ok.text
print("   admin unlock -> posted; edit now succeeds (rent=2200) OK")

print("\n6) GRANULAR LINE-ITEM CRUD ------------------------------------------")
rid = ok.json()["id"]
li = post(f"/records/{rid}/line-items", {"category_id": tax["id"], "amount": 333})
patched = client.patch(f"/line-items/{li['id']}", json={"amount": 444}, headers=H)
assert patched.json()["amount"] == 444.0
assert client.delete(f"/line-items/{li['id']}", headers=H).status_code == 204
print("   line item add(333) -> patch(444) -> delete OK")

print("\n7) CASCADE CLEANUP --------------------------------------------------")
for pid in (prop["id"], single["id"]):
    assert client.delete(f"/properties/{pid}", headers=H).status_code == 204
assert client.get(f"/properties/{prop['id']}", headers=H).status_code == 404
assert client.get(f"/records?property_id={prop['id']}", headers=H).json() == []
print("   properties deleted; records gone via cascade OK")

db.close()
print("\nALL PHASE 3 CHECKS PASSED.")
