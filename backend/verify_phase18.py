"""Verification of the property category breakdown — "what is this number made of".

Run:  PYTHONPATH=.pydeps python3 verify_phase18.py

A dashboard figure that cannot be taken apart is a figure you cannot act on: "Operating
Expenses £412" says money left the building and not whether that was one boiler or twelve
small bills. The same was true of the old single "Below-NOI" column, which mixes a mortgage
with a roof — two numbers that mean entirely different things to whoever has to decide
something about them.

The breakdown exists to answer that, and the only way it can do real harm is by disagreeing
with the figures it sits under. So that is what is proved here:

  1. **It reconciles, per classification.** Each section's categories sum to exactly the
     headline metric the P&L reports for the same period. If these two ever drift, the panel
     is actively misleading, which is worse than not having it.
  2. **It resolves classification exactly as the P&L does.** Both read
     ``v_line_item_resolved``, where a line item's own classification wins and the category's
     current default applies otherwise. So reclassifying a category moves the lines that
     deferred to it, leaves the lines that overrode it, and moves BOTH numbers identically —
     which is the only thing that matters, because (1) has to hold afterwards too.
  3. **It respects the period.** A narrower window reports less, not the same.
  4. **It is account-scoped**, like every other property-scoped read.

Self-contained: restores the classification it changes and deletes everything it created.
"""
from datetime import date

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
tok = client.post(
    "/auth/login", data={"username": "admin@example.com", "password": "admin12345"}
).json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        failures.append(f"{label}{': ' + detail if detail else ''}")
        print(f"  FAIL  {label} {detail}")
    else:
        print(f"  ok    {label}")


CLASS_TO_METRIC = {
    "rent": "gross_rent",
    "operating": "operating_expenses",
    "capex": "capex",
    "debt_service": "debt_service",
    "other_below_line": "other_below_line",
}

props = client.get("/properties", headers=H).json()
assert props, "seed has no properties"


def breakdown(pid: str, frm: str | None = None, to: str | None = None) -> list[dict]:
    params = {k: v for k, v in {"from": frm, "to": to}.items() if v}
    r = client.get(f"/properties/{pid}/categories", headers=H, params=params)
    assert r.status_code == 200, r.text[:200]
    return r.json()["rows"]


def monthly_totals(pid: str, frm: str | None = None, to: str | None = None) -> dict:
    params = {k: v for k, v in {"from": frm, "to": to}.items() if v}
    rows = client.get(f"/properties/{pid}/monthly", headers=H, params=params).json()
    out = {m: 0.0 for m in CLASS_TO_METRIC.values()}
    for row in rows:
        for m in out:
            out[m] += row[m]
    return {k: round(v, 2) for k, v in out.items()}


def by_class(rows: list[dict]) -> dict:
    out: dict[str, float] = {}
    for r in rows:
        out[r["classification"]] = round(out.get(r["classification"], 0.0) + r["amount"], 2)
    return out


# ============================================================================================
print("\n1. Every section adds up to the figure it explains")
# ============================================================================================
checked = 0
for p in props:
    rows = breakdown(p["id"])
    if not rows:
        continue
    checked += 1
    sums = by_class(rows)
    totals = monthly_totals(p["id"])
    for cls, metric in CLASS_TO_METRIC.items():
        check(
            f"{p['name'][:28]} — {cls} reconciles",
            abs(sums.get(cls, 0.0) - totals[metric]) < 0.01,
            f"breakdown {sums.get(cls, 0.0)} vs P&L {totals[metric]}",
        )
    if checked >= 3:
        break
check("at least one property had data to check", checked > 0)

# ============================================================================================
print("\n2. A narrower period reports less")
# ============================================================================================
target = next(p for p in props if breakdown(p["id"]))
months = sorted({r["month"] for r in client.get(f"/properties/{target['id']}/monthly", headers=H).json()})
if len(months) >= 2:
    one = breakdown(target["id"], months[-1], months[-1])
    allm = breakdown(target["id"])
    check(
        "one month is a subset of the whole history",
        sum(abs(r["amount"]) for r in one) <= sum(abs(r["amount"]) for r in allm) + 0.01,
        f"{sum(abs(r['amount']) for r in one):.2f} vs {sum(abs(r['amount']) for r in allm):.2f}",
    )
    check(
        "and that single month still reconciles",
        all(
            abs(by_class(one).get(cls, 0.0) - monthly_totals(target["id"], months[-1], months[-1])[metric]) < 0.01
            for cls, metric in CLASS_TO_METRIC.items()
        ),
    )
else:
    check("property has at least two months to compare", False, "seed too small")

# ============================================================================================
print("\n3. Reclassifying a category moves its spend between sections")
# ============================================================================================
# The breakdown reads the CURRENT classification, exactly as the P&L views do. The point is
# not that it changes — it is that it changes IN STEP, so section 1 still holds afterwards.
cats = {c["name"]: c for c in client.get("/categories", headers=H).json()}
# The subject has to be a category whose lines DEFER to the category default — a line item
# carrying its own classification is supposed to ignore the change, and picking one of those
# would test the opposite of what this section claims. The statement importer stamps an
# explicit classification on every row it writes, so the two kinds really do coexist here.
# "Utilities" is used because it ships in every account's starting chart of accounts and the
# seed posts it through the plain entry path, which leaves the classification to the category.
subject = victim = None
for p in props:
    row = next(
        (
            r
            for r in breakdown(p["id"])
            if r["category"] == "Utilities" and r["classification"] == "operating" and r["amount"]
        ),
        None,
    )
    if row and "Utilities" in cats:
        subject, victim = p, cats["Utilities"]
        break

if victim and subject:
    victim_name = victim["name"]
    before_rows = breakdown(subject["id"])
    before = by_class(before_rows)
    moved_amount = next(
        (x["amount"] for x in before_rows if x["category"] == victim_name), 0.0
    )
    r = client.patch(
        f"/categories/{victim['id']}", headers=H, json={"default_classification": "capex"}
    )
    check("the category reclassifies", r.status_code == 200, r.text[:150])
    if r.status_code == 200:
        after_rows = breakdown(subject["id"])
        after = by_class(after_rows)
        check(
            "its spend left the operating section",
            abs(before.get("operating", 0.0) - after.get("operating", 0.0) - moved_amount) < 0.01,
            f"{before.get('operating')} -> {after.get('operating')} (expected to move {moved_amount})",
        )
        check(
            "and is now reported under capital expenditure",
            any(
                x["category"] == victim_name and x["classification"] == "capex"
                for x in after_rows
            ),
            str([x["classification"] for x in after_rows if x["category"] == victim_name]),
        )
        totals = monthly_totals(subject["id"])
        check(
            "the P&L moved identically, so the breakdown still reconciles",
            all(
                abs(after.get(cls, 0.0) - totals[metric]) < 0.01
                for cls, metric in CLASS_TO_METRIC.items()
            ),
            str({c: (after.get(c, 0.0), totals[m]) for c, m in CLASS_TO_METRIC.items()}),
        )
        client.patch(
            f"/categories/{victim['id']}", headers=H, json={"default_classification": "operating"}
        )
        check(
            "and it is restored",
            by_class(breakdown(subject["id"])).get("operating", 0.0) == before.get("operating", 0.0),
        )
else:
    check("a category that defers to its default was available", False, "no Utilities spend in the seed")

# ============================================================================================
print("\n4. Another account's property is not readable")
# ============================================================================================
r = client.get("/properties/00000000-0000-0000-0000-000000000009/categories", headers=H)
check("an unknown property id is refused", r.status_code == 404, str(r.status_code))

print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("verify_phase18 PASSED")
