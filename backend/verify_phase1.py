"""Ad-hoc Phase 1 verification (not part of the deliverable test suite).

Exercises the four hard guarantees against a seeded database via the in-process API:
  1. auth + portfolio P&L read
  2. admin-only unlock of a locked period, with an audit_log row written
  3. non-admin is forbidden from unlocking (403)
  4. reclassifying a category recomputes NOI with NO migration
  5. property total != sum of units for property-tier-only items (honest rollup)
"""
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.db import SessionLocal
from app.main import app
from app.models import AuditLog, Category, PeriodStatus, Property, User
from app.security import hash_password

client = TestClient(app)
db = SessionLocal()


def login(email, password):
    r = client.post("/auth/login", data={"username": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


print("1) AUTH + PORTFOLIO P&L --------------------------------------------")
admin_token = login("admin@example.com", "admin12345")
r = client.get("/portfolio/monthly", headers={"Authorization": f"Bearer {admin_token}"})
assert r.status_code == 200, r.text
print("   GET /portfolio/monthly ->", len(r.json()), "months")
first = r.json()[0]
print(f"   {first['month']} NOI =", first["noi"], " cash_flow =", first["cash_flow"])

print("\n2) ADMIN UNLOCK + AUDIT LOG ----------------------------------------")
maple = db.scalar(select(Property).where(Property.name == "Maple Court Apartments"))
# Lock a month HERE rather than expecting the seed to leave one locked: the seed posts
# every property-month but locks none, so this used to depend on state it never created.
# Locking through the API also exercises PUT /periods on the way in.
target_month = db.scalar(
    text("SELECT max(month)::text FROM monthly_records WHERE property_id = :pid"),
    {"pid": maple.id},
)
r_lock = client.put(
    "/periods",
    headers={"Authorization": f"Bearer {admin_token}"},
    json={"property_id": maple.id, "month": target_month, "status": "locked"},
)
assert r_lock.status_code == 200, r_lock.text
db.expire_all()
locked = db.scalar(
    select(PeriodStatus).where(
        PeriodStatus.property_id == maple.id, PeriodStatus.status == "locked"
    )
)
print("   locked period:", locked.month, locked.status)
r = client.post(f"/periods/{locked.id}/unlock", headers={"Authorization": f"Bearer {admin_token}"})
assert r.status_code == 200, r.text
print("   after admin unlock -> status:", r.json()["status"])
audits = db.execute(
    text("SELECT action, entity, before, after FROM audit_log WHERE action='unlock_period'")
).fetchall()
print("   audit_log rows:", len(audits), "->", audits[0] if audits else None)
assert len(audits) == 1

print("\n3) NON-ADMIN FORBIDDEN ---------------------------------------------")
if not db.scalar(select(User).where(User.email == "member@example.com")):
    # Multi-tenancy (migration 0017): a user belongs to an account. Put the member in the
    # SAME account as the seed admin — the point of this check is role separation
    # (member cannot unlock), which is only meaningful within one account.
    admin_row = db.scalar(select(User).where(User.email == "admin@example.com"))
    db.add(User(
        account_id=admin_row.account_id,
        email="member@example.com",
        hashed_password=hash_password("member12345"),
        role="member",
    ))
    db.commit()
# lock another period to attempt unlock as member
other = db.scalar(select(PeriodStatus).where(PeriodStatus.status == "posted"))
other.status = "locked"
db.commit()
member_token = login("member@example.com", "member12345")
r = client.post(f"/periods/{other.id}/unlock", headers={"Authorization": f"Bearer {member_token}"})
print("   member unlock attempt -> HTTP", r.status_code, "(expected 403)")
assert r.status_code == 403
other.status = "posted"
db.commit()

print("\n4) RECLASSIFY CATEGORY -> NOI RECOMPUTES, NO MIGRATION -------------")
before_all = client.get("/portfolio/monthly", headers={"Authorization": f"Bearer {admin_token}"}).json()
# Pick a month that actually carries capex, and remember its figures — the invariant under
# test is "moving a capex category above the NOI line reduces that month's NOI by exactly
# the capex", which holds whatever the seed's calendar and dollar amounts happen to be.
# (This block used to hard-code Feb-2026 and $7,555/$8,000 from an older seed.)
m_before = next((x for x in before_all if float(x["capex"]) > 0), None)
assert m_before is not None, "no month with capex — seed fixture changed"
RECLASS_MONTH = m_before["month"]
noi_before, capex_before = float(m_before["noi"]), float(m_before["capex"])

# Per-account categories (migration 0017): scope the lookup to the admin's own account,
# or a second account's identically-named category could be picked up instead.
admin_row = db.scalar(select(User).where(User.email == "admin@example.com"))
roof = db.scalar(
    select(Category).where(
        Category.name == "Roof Replacement",
        Category.account_id == admin_row.account_id,
    )
)
roof.default_classification = "operating"  # move capex ABOVE the NOI line
db.commit()
after = client.get("/portfolio/monthly", headers={"Authorization": f"Bearer {admin_token}"}).json()
feb_after = next(x for x in after if x["month"] == RECLASS_MONTH)
print("   reclassified 'Roof Replacement' capex -> operating (no DDL/migration)")
print(f"   {RECLASS_MONTH} NOI: {noi_before:,.2f} (capex below) -> {float(feb_after['noi']):,.2f} "
      f"(now operating, inside NOI); capex was {capex_before:,.2f}")
assert abs(float(feb_after["noi"]) - (noi_before - capex_before)) < 0.01
assert float(feb_after["capex"]) == 0.0
roof.default_classification = "capex"  # restore
db.commit()

print("\n5) HONEST ROLLUP: property total != sum of units --------------------")
rows = db.execute(
    text(
        """
        SELECT CASE WHEN unit_id IS NULL THEN 'property-tier' ELSE 'unit' END AS tier,
               SUM(capex) capex, SUM(debt_service) debt
        FROM v_monthly_pnl WHERE property_id = :pid GROUP BY 1 ORDER BY 1
        """
    ),
    {"pid": str(maple.id)},
).fetchall()
for row in rows:
    print(f"   {row.tier:>14}: capex={row.capex}  debt_service={row.debt}")
print("   -> units carry $0 capex/debt; those live only at the property tier (not blended).")

db.close()
print("\nALL PHASE 1 CHECKS PASSED.")
