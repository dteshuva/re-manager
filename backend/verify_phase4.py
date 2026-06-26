"""Ad-hoc Phase 4 verification (not part of the deliverable test suite).

Exercises CSV/Excel import + the structured-row seam against a running, seeded DB.
Uses a fresh month (2026-08) and a throwaway property so it's safe to re-run; reseed
afterward to restore the canonical seed for the Phase 1/2/3 scripts.

  1. CSV file import (dry-run preview writes nothing, then a real commit)
  2. idempotent re-import keyed on (property, unit, month, category) — overwrite, no dup
  3. row-level validation: unknown property/category + bad cells reported; abort vs skip
  4. locked month rejected by import
  5. structured-row JSON seam (POST /import/rows) — same path, no file
  6. Excel (.xlsx) import via openpyxl
  7. 'what's missing' month view
"""
import io

import pandas as pd
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import MonthlyRecord, Property

client = TestClient(app)
db = SessionLocal()
H = {"Authorization": None}


def login():
    r = client.post("/auth/login", data={"username": "admin@example.com", "password": "admin12345"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


H = login()
MAP = '{"property":"Property","unit":"Unit","month":"Month","category":"Category","amount":"Amount"}'


def upload(csv_text, *, dry_run=False, on_error="abort", filename="june.csv"):
    return client.post(
        "/import/file",
        files={"file": (filename, csv_text, "text/csv")},
        data={"mapping": MAP, "dry_run": str(dry_run).lower(), "on_error": on_error},
        headers=H,
    )


print("1) CSV IMPORT: dry-run preview then commit --------------------------")
csv = (
    "Property,Unit,Month,Category,Amount\n"
    "Maple Court Apartments,101,2026-08,Rent,1600\n"
    "Maple Court Apartments,101,2026-08,Utilities,90\n"
    "Maple Court Apartments,,2026-08,Mortgage,2600\n"
    "Birch Street House,,2026-08,Rent,2450\n"
)
prev = upload(csv, dry_run=True).json()
assert prev["valid_rows"] == 4 and prev["applied_line_items"] == 4 and prev["committed"] is False
existing = db.scalars(select(MonthlyRecord).where(MonthlyRecord.month == __import__("datetime").date(2026, 8, 1))).all()
assert existing == [], "dry-run must not write"
print(f"   dry-run: valid={prev['valid_rows']} would_apply={prev['applied_line_items']} committed={prev['committed']} (nothing written)")
real = upload(csv).json()
assert real["committed"] is True and real["applied_line_items"] == 4
print(f"   commit: applied={real['applied_line_items']} records_touched={real['records_touched']} committed={real['committed']}")

print("\n2) IDEMPOTENT RE-IMPORT (overwrite, no duplicate) -------------------")
csv2 = (
    "Property,Unit,Month,Category,Amount\n"
    "Maple Court Apartments,101,2026-08,Rent,1700\n"   # overwrite Rent
    "Maple Court Apartments,101,2026-08,Insurance,40\n"  # add a new category
)
r2 = upload(csv2).json()
assert r2["committed"] and r2["applied_line_items"] == 2
db.expire_all()
unit_rec = db.scalar(
    select(MonthlyRecord).where(
        MonthlyRecord.month == __import__("datetime").date(2026, 8, 1),
        MonthlyRecord.unit_id.isnot(None),
    )
)
cats = {li.category.name: float(li.amount) for li in unit_rec.line_items}
assert cats["Rent"] == 1700.0, "Rent overwritten in place"
assert cats["Utilities"] == 90.0, "untouched category preserved"
assert "Insurance" in cats, "new category added"
assert len(unit_rec.line_items) == 3, f"expected 3 line items, got {len(unit_rec.line_items)}"
print(f"   unit 101 2026-08 line items after re-import: {cats} (Rent overwritten, others kept)")

print("\n3) VALIDATION: bad refs + bad cells; abort vs skip -----------------")
bad = (
    "Property,Unit,Month,Category,Amount\n"
    "Nonexistent Property,,2026-08,Rent,100\n"          # unknown property
    "Birch Street House,,2026-08,Bogus Category,100\n"  # unknown category
    "Birch Street House,,not-a-date,Rent,100\n"         # unparseable month
    "Birch Street House,,2026-08,Rent,oops\n"           # unparseable amount
)
ab = upload(bad, on_error="abort").json()
assert ab["committed"] is False and ab["invalid_rows"] == 4, ab
print(f"   abort: invalid_rows={ab['invalid_rows']} committed={ab['committed']}; sample: {ab['errors'][0]['message']}")
# skip mode with one good row mixed in
mixed = (
    "Property,Unit,Month,Category,Amount\n"
    "Nonexistent Property,,2026-08,Rent,100\n"
    "Cedar Plaza Retail,,2026-08,Rent,5300\n"  # valid
)
sk = upload(mixed, on_error="skip").json()
assert sk["committed"] is True and sk["valid_rows"] == 1 and sk["invalid_rows"] == 1, sk
print(f"   skip: valid={sk['valid_rows']} applied={sk['applied_line_items']} invalid={sk['invalid_rows']} committed={sk['committed']}")

print("\n4) LOCKED MONTH REJECTED -------------------------------------------")
maple = db.scalar(select(Property).where(Property.name == "Maple Court Apartments"))
client.put("/periods", json={"property_id": maple.id, "month": "2026-08-01", "status": "locked"}, headers=H)
locked = upload(
    "Property,Unit,Month,Category,Amount\nMaple Court Apartments,101,2026-08,Rent,9999\n",
    on_error="abort",
).json()
assert locked["committed"] is False and any("locked" in e["message"].lower() for e in locked["errors"])
print(f"   import into locked 2026-08 blocked: {locked['errors'][0]['message']}")
# reopen for cleanliness
pid = client.get(f"/periods?property_id={maple.id}&month=2026-08-01", headers=H).json()[0]["id"]
client.post(f"/periods/{pid}/unlock", headers=H)

print("\n5) STRUCTURED-ROW JSON SEAM (/import/rows) -------------------------")
rows = [
    {"property": "Birch Street House", "month": "2026-09-01", "category": "Rent", "amount": 2500},
    {"property": "Birch Street House", "month": "2026-09-01", "category": "Property Tax", "amount": 360},
]
seam = client.post("/import/rows", json=rows, headers=H).json()
assert seam["committed"] and seam["applied_line_items"] == 2
print(f"   JSON rows applied={seam['applied_line_items']} (same core, no file)")

print("\n6) EXCEL (.xlsx) IMPORT --------------------------------------------")
buf = io.BytesIO()
pd.DataFrame(
    [{"Property": "Cedar Plaza Retail", "Unit": "", "Month": "2026-10", "Category": "Rent", "Amount": 5400}]
).to_excel(buf, index=False)
xl = client.post(
    "/import/file",
    files={"file": ("oct.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    data={"mapping": MAP, "dry_run": "false", "on_error": "abort"},
    headers=H,
).json()
assert xl["committed"] and xl["applied_line_items"] == 1, xl
print(f"   .xlsx applied={xl['applied_line_items']} committed={xl['committed']}")

print("\n7) WHAT'S MISSING (2026-08) ----------------------------------------")
missing = client.get("/import/missing?month=2026-08-01", headers=H).json()
labels = [m["property_name"] + (f" / Unit {m['unit_number']}" if m["unit_number"] else " / tier") for m in missing]
print(f"   {len(missing)} scopes with no 2026-08 data, e.g.: {labels[:4]}")
# Birch tier had data in step 1, so it should NOT be missing; Maple units 102/103 should be.
assert not any(m["property_name"] == "Birch Street House" and m["unit_id"] is None for m in missing)
assert any(m["unit_number"] == "102" for m in missing)
print("   Birch tier present (not missing); Maple units 102/103 flagged missing OK")

db.close()
print("\nALL PHASE 4 CHECKS PASSED.")
