"""Verification of account isolation (migration 0017 — multi-tenancy).

Run:  PYTHONPATH=.pydeps python3 verify_isolation.py

The premise: app-level scoping is only as good as its worst-covered endpoint, so rather
than trusting that every query got an ``account_id``, this drives the real API and asserts
that account B can never see or touch account A's data.

What it does:
  1. Signs up TWO fresh accounts (B and C) via the public POST /auth/signup.
  2. Gives account B a small portfolio of its own (property, unit, category, record, lease,
     budget, tag, investment) through the ordinary write endpoints.
  3. Reads every listing endpoint as account C and asserts B's rows are absent.
  4. Probes every by-id endpoint as C using B's ids and asserts 404 (never 200, and never
     403 — a 403 would confirm the id exists).
  5. Attempts cross-account WRITES and asserts they fail.
  6. Checks the DB-level invariant directly: a line item pointing at another account's
     category must be rejected by the composite FK, not merely by the app.
  7. Confirms the seeded demo account still sees its own data (scoping didn't just break
     every read), and that B's own portfolio reads back correctly.

Safe and re-runnable: it only ever creates and deletes its OWN two accounts (emails are
uuid-suffixed), and touches no pre-existing account's data. Deletes both accounts at the end.
"""
from __future__ import annotations

import uuid
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import SessionLocal
from app.main import app

client = TestClient(app)
db = SessionLocal()

PASSWORD = "isolation-test-pw"
failures: list[str] = []
checks = 0


def check(cond: bool, label: str) -> None:
    global checks
    checks += 1
    if not cond:
        failures.append(label)
        print(f"  FAIL  {label}")


def signup(tag: str) -> tuple[str, dict]:
    email = f"iso-{tag}-{uuid.uuid4().hex[:10]}@example.com"
    r = client.post(
        "/auth/signup",
        json={"email": email, "password": PASSWORD, "account_name": f"Isolation {tag}"},
    )
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    return email, {"Authorization": f"Bearer {token}"}


def get(headers, path, expect=200):
    r = client.get(path, headers=headers)
    check(r.status_code == expect, f"GET {path} -> {r.status_code}, expected {expect}")
    return r


def ids_in(payload) -> set[str]:
    """Every UUID-looking string anywhere in a response body — a deliberately blunt
    instrument, so a leak through an unexpected field still trips the check."""
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str) and len(node) == 36 and node.count("-") == 4:
            found.add(node)

    walk(payload)
    return found


print("=" * 72)
print("ACCOUNT ISOLATION VERIFICATION")
print("=" * 72)

# ---------------------------------------------------------------- 1. two accounts
email_b, B = signup("b")
email_c, C = signup("c")
me_b = get(B, "/auth/me").json()
me_c = get(C, "/auth/me").json()
acct_b, acct_c = me_b["account_id"], me_c["account_id"]
print(f"\n[1] signed up two accounts: {acct_b[:8]}… and {acct_c[:8]}…")
check(acct_b != acct_c, "signup created two DISTINCT accounts")
check(me_b["role"] == "admin", "signup's first user is the account admin")

# A new account starts empty and self-sufficient.
check(get(B, "/properties").json() == [], "new account starts with NO properties")
cats_b = get(B, "/categories").json()
check(len(cats_b) > 0, "new account gets its own default category list")
check(get(B, "/settings/attention").status_code == 200, "new account has attention settings")

# ---------------------------------------------------------------- 2. B builds a portfolio
print("\n[2] building account B's portfolio…")
prop_b = client.post(
    "/properties", headers=B, json={"name": "B Tower", "type": "multifamily", "address": "1 B St"}
).json()
pid_b = prop_b["id"]
unit_b = client.post(
    f"/properties/{pid_b}/units", headers=B, json={"unit_number": "101", "label": "B Unit"}
).json()
uid_b = unit_b["id"]
cat_b = client.post(
    "/categories", headers=B, json={"name": "B Only Category", "default_classification": "rent"}
).json()
cid_b = cat_b["id"]

month = date(2025, 6, 1).isoformat()
rec_b = client.post(
    "/records",
    headers=B,
    json={
        "property_id": pid_b,
        "unit_id": uid_b,
        "month": month,
        "line_items": [{"category_id": cid_b, "amount": 4321.0}],
    },
)
check(rec_b.status_code == 201, f"B can post its own record ({rec_b.status_code})")
rid_b = rec_b.json()["id"]
li_b = rec_b.json()["line_items"][0]["id"]

lease_b = client.post(
    f"/units/{uid_b}/leases",
    headers=B,
    json={
        "tenant_name": "B Tenant",
        "start_date": "2025-01-01",
        "end_date": "2025-12-31",
        "contract_rent": 4321.0,
        "status": "active",
    },
)
check(lease_b.status_code == 201, f"B can create its own lease ({lease_b.status_code})")
lease_id_b = lease_b.json()["id"] if lease_b.status_code == 201 else None

client.put(
    f"/properties/{pid_b}/budgets/2025",
    headers=B,
    json={"budgeted_gross_rent": 50000, "budgeted_operating_expenses": 20000},
)
client.post(f"/properties/{pid_b}/tags", headers=B, json={"tag": "b-only-tag"})
client.put(
    f"/properties/{pid_b}/investment",
    headers=B,
    json={
        "purchase_price": 1000000,
        "closing_costs": 20000,
        "loan_amount": 700000,
        "purchase_date": "2024-01-01",
    },
)
client.put("/periods", headers=B, json={"property_id": pid_b, "month": month, "status": "posted"})

b_ids = {pid_b, uid_b, cid_b, rid_b, li_b}
if lease_id_b:
    b_ids.add(lease_id_b)

# ---------------------------------------------------------------- 3. C's listings are clean
print("\n[3] account C's listing endpoints must not contain any of B's rows…")
LIST_ENDPOINTS = [
    "/properties",
    "/categories",
    "/records",
    "/periods",
    "/tags",
    "/investments",
    "/portfolio/monthly",
    "/portfolio/dashboard",
    "/portfolio/breakdown",
    "/portfolio/attention",
    "/portfolio/worst-units",
    "/portfolio/benchmarks",
    "/portfolio/variance",
    "/portfolio/rent-roll",
    "/portfolio/rent-waterfall",
    "/portfolio/lease-expirations",
]
for path in LIST_ENDPOINTS:
    r = client.get(path, headers=C)
    if r.status_code != 200:
        check(False, f"GET {path} as C -> {r.status_code} (expected 200)")
        continue
    leaked = ids_in(r.json()) & b_ids
    check(not leaked, f"GET {path} as C leaked B's ids: {leaked}")

check("b-only-tag" not in get(C, "/tags").json(), "GET /tags as C does not show B's tag")
c_cat_names = {c["name"] for c in get(C, "/categories").json()}
check("B Only Category" not in c_cat_names, "GET /categories as C does not show B's category")

# ---------------------------------------------------------------- 4. by-id probes -> 404
print("\n[4] account C probing B's ids by id must 404 (never 200, never 403)…")
BY_ID_GETS = [
    f"/properties/{pid_b}",
    f"/properties/{pid_b}/units",
    f"/properties/{pid_b}/monthly",
    f"/properties/{pid_b}/dashboard",
    f"/properties/{pid_b}/attention",
    f"/properties/{pid_b}/units/roster",
    f"/properties/{pid_b}/units/monthly",
    f"/properties/{pid_b}/investment/metrics",
    f"/properties/{pid_b}/budgets",
    f"/properties/{pid_b}/variance",
    f"/properties/{pid_b}/tags",
    f"/properties/{pid_b}/rent-roll",
    f"/properties/{pid_b}/rent-waterfall",
    f"/units/{uid_b}",
    f"/units/{uid_b}/detail",
    f"/units/{uid_b}/monthly",
    f"/units/{uid_b}/leases",
    f"/records/{rid_b}",
]
for path in BY_ID_GETS:
    r = client.get(path, headers=C)
    check(r.status_code == 404, f"GET {path} as C -> {r.status_code} (expected 404)")

# ---------------------------------------------------------------- 5. cross-account writes
print("\n[5] account C attempting to WRITE into B's portfolio must fail…")
WRITE_PROBES = [
    ("PATCH", f"/properties/{pid_b}", {"name": "hijacked"}),
    ("DELETE", f"/properties/{pid_b}", None),
    ("POST", f"/properties/{pid_b}/units", {"unit_number": "999"}),
    ("PATCH", f"/units/{uid_b}", {"label": "hijacked"}),
    ("DELETE", f"/units/{uid_b}", None),
    ("PATCH", f"/records/{rid_b}", {"notes": "hijacked"}),
    ("DELETE", f"/records/{rid_b}", None),
    ("POST", f"/records/{rid_b}/line-items", {"category_id": cid_b, "amount": 1.0}),
    ("PATCH", f"/line-items/{li_b}", {"amount": 999999.0}),
    ("DELETE", f"/line-items/{li_b}", None),
    ("PATCH", f"/categories/{cid_b}", {"default_classification": "capex"}),
    ("POST", f"/units/{uid_b}/leases",
     {"tenant_name": "X", "start_date": "2025-01-01", "contract_rent": 1.0, "status": "active"}),
    ("PUT", f"/properties/{pid_b}/budgets/2025",
     {"budgeted_gross_rent": 1, "budgeted_operating_expenses": 1}),
    ("DELETE", f"/properties/{pid_b}/budgets/2025", None),
    ("POST", f"/properties/{pid_b}/tags", {"tag": "hijacked"}),
    ("DELETE", f"/properties/{pid_b}/tags/b-only-tag", None),
    ("PUT", f"/properties/{pid_b}/investment",
     {"purchase_price": 1, "closing_costs": 0, "loan_amount": 0, "purchase_date": "2024-01-01"}),
    ("DELETE", f"/properties/{pid_b}/investment", None),
    ("PUT", "/periods", {"property_id": pid_b, "month": month, "status": "locked"}),
]
if lease_id_b:
    WRITE_PROBES.append(
        ("PATCH", f"/leases/{lease_id_b}",
         {"tenant_name": "X", "start_date": "2025-01-01", "contract_rent": 1.0, "status": "active"})
    )
    WRITE_PROBES.append(("DELETE", f"/leases/{lease_id_b}", None))

for method, path, body in WRITE_PROBES:
    r = client.request(method, path, headers=C, json=body)
    check(r.status_code >= 400, f"{method} {path} as C -> {r.status_code} (expected an error)")

# A record naming another account's property must not be creatable either.
r = client.post(
    "/records",
    headers=C,
    json={"property_id": pid_b, "unit_id": None, "month": month, "line_items": []},
)
check(r.status_code == 404, f"POST /records naming B's property as C -> {r.status_code} (expected 404)")

# ...nor a line item borrowing another account's category on C's OWN record.
prop_c = client.post(
    "/properties", headers=C, json={"name": "C House", "type": "single", "address": "2 C St"}
).json()
pid_c = prop_c["id"]
r = client.post(
    "/records",
    headers=C,
    json={
        "property_id": pid_c,
        "unit_id": None,
        "month": month,
        "line_items": [{"category_id": cid_b, "amount": 1.0}],
    },
)
check(
    r.status_code == 400,
    f"POST /records with B's category id as C -> {r.status_code} (expected 400 unknown category)",
)

# ---------------------------------------------------------------- 6. the DB-level invariant
print("\n[6] the composite FK must reject a cross-account line item even below the API…")
rec_c = client.post(
    "/records", headers=C, json={"property_id": pid_c, "unit_id": None, "month": month, "line_items": []}
).json()
rid_c = rec_c["id"]
try:
    db.execute(
        text(
            "INSERT INTO line_items (account_id, monthly_record_id, category_id, amount) "
            "VALUES (:acct, :rec, :cat, 1)"
        ),
        {"acct": acct_c, "rec": rid_c, "cat": cid_b},  # C's record, B's category
    )
    db.commit()
    check(False, "raw INSERT of a cross-account line item was ACCEPTED (FK missing!)")
except Exception:
    db.rollback()
    check(True, "raw INSERT of a cross-account line item rejected by the FK")

# ...and a monthly_record filed under the wrong account likewise.
try:
    db.execute(
        text(
            "INSERT INTO monthly_records (account_id, property_id, month) "
            "VALUES (:acct, :prop, :m)"
        ),
        {"acct": acct_c, "prop": pid_b, "m": month},  # C's account, B's property
    )
    db.commit()
    check(False, "raw INSERT of a cross-account monthly_record was ACCEPTED (FK missing!)")
except Exception:
    db.rollback()
    check(True, "raw INSERT of a cross-account monthly_record rejected by the FK")

# ---------------------------------------------------------------- 7. scoping didn't break reads
print("\n[7] each account still sees its OWN data…")
b_props = get(B, "/properties").json()
check({p["id"] for p in b_props} == {pid_b}, "B sees exactly its own property")
check(get(B, f"/properties/{pid_b}").status_code == 200, "B can read its own property")
check(get(B, f"/units/{uid_b}/detail").status_code == 200, "B can read its own unit detail")
check(get(B, f"/records/{rid_b}").status_code == 200, "B can read its own record")
check("b-only-tag" in get(B, "/tags").json(), "B sees its own tag")

b_dash = get(B, "/portfolio/dashboard").json()
check(
    b_dash["current"] is not None and float(b_dash["current"]["gross_rent"]) == 4321.0,
    f"B's portfolio dashboard reflects only its own $4,321 of rent ({b_dash['current']})",
)
b_bd = get(B, "/portfolio/breakdown").json()
check(len(b_bd["properties"]) == 1, "B's breakdown contains exactly one property")

# The pre-existing seeded demo account is untouched and still works.
seed_login = client.post(
    "/auth/login", data={"username": "admin@example.com", "password": "admin12345"}
)
if seed_login.status_code == 200:
    S = {"Authorization": f"Bearer {seed_login.json()['access_token']}"}
    seed_props = client.get("/properties", headers=S).json()
    check(len(seed_props) > 1, f"seeded demo account still sees its portfolio ({len(seed_props)} properties)")
    leaked = {p["id"] for p in seed_props} & b_ids
    check(not leaked, "seeded demo account does not see the test accounts' properties")
    sd = client.get("/portfolio/dashboard", headers=S).json()
    check(sd.get("current") is not None, "seeded demo account's dashboard still computes")
else:
    print("  (seed admin not present — skipping the seeded-account checks)")

# Duplicate-email signup is still refused (email is the global login key).
r = client.post("/auth/signup", json={"email": email_b, "password": PASSWORD})
check(r.status_code == 409, f"signup with an existing email -> {r.status_code} (expected 409)")

# ---------------------------------------------------------------- cleanup
# Deleted in dependency order rather than by cascading from `accounts`: line_items ->
# categories is a plain (non-cascading) FK, so an account-level cascade can reach
# `categories` before the line items referencing them are gone.
for stmt in (
    "DELETE FROM property_category_month_summary WHERE account_id IN (:b, :c)",
    "DELETE FROM line_items WHERE account_id IN (:b, :c)",
    "DELETE FROM monthly_records WHERE account_id IN (:b, :c)",
    "DELETE FROM categories WHERE account_id IN (:b, :c)",
    "DELETE FROM accounts WHERE id IN (:b, :c)",
):
    db.execute(text(stmt), {"b": acct_b, "c": acct_c})
db.commit()
db.close()

print("\n" + "=" * 72)
if failures:
    print(f"FAILED — {len(failures)} of {checks} checks did not pass:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print(f"PASSED — all {checks} isolation checks green.")
print("=" * 72)
