"""Ad-hoc verification + benchmark for the rollup/summary layer (sub-step 1).

Run:  PYTHONPATH=.pydeps python3 verify_phase5.py

Proves the three things the spec asks for:
  1. RECONCILIATION — every summary row equals the raw line-item aggregation exactly
     (property-month and portfolio-month), and the rollup-honesty identity holds.
  2. SAMPLE — prints a sample portfolio-month and property-month row.
  3. SPEED — times a dashboard-style query (all properties for a month; full T12
     portfolio trend) raw-aggregation vs. summary-table, and reports the speedup.
"""
import time
from decimal import Decimal

from sqlalchemy import text

from app.db import SessionLocal

db = SessionLocal()

MONEY = (
    "gross_rent", "operating_expenses", "noi", "capex",
    "debt_service", "other_below_line", "below_noi", "cash_flow",
)


def _bench(label, sql, params=None, runs=5):
    params = params or {}
    db.execute(text(sql), params).fetchall()  # warm
    best = min(
        (lambda t0: (db.execute(text(sql), params).fetchall(), time.perf_counter() - t0)[1])(
            time.perf_counter()
        )
        for _ in range(runs)
    )
    print(f"   {label:<46} {best * 1000:8.2f} ms (best of {runs})")
    return best


print("1) RECONCILIATION — summary == raw line-item aggregation -------------")

# Per-property-month: summary vs. a fresh aggregation straight off v_monthly_pnl.
prop_mismatch = db.execute(
    text(
        f"""
        WITH raw AS (
            SELECT property_id, month, {", ".join(f"SUM({m}) AS {m}" for m in MONEY)}
            FROM v_monthly_pnl GROUP BY property_id, month
        )
        SELECT count(*) FROM property_month_summary s
        JOIN raw r ON r.property_id = s.property_id AND r.month = s.month
        WHERE {" OR ".join(f"s.{m} <> r.{m}" for m in MONEY)}
        """
    )
).scalar()
# No summary row should be missing a raw counterpart and vice-versa.
prop_count_s = db.execute(text("SELECT count(*) FROM property_month_summary")).scalar()
prop_count_r = db.execute(
    text("SELECT count(*) FROM (SELECT DISTINCT property_id, month FROM v_monthly_pnl) x")
).scalar()
assert prop_mismatch == 0, f"{prop_mismatch} property-month rows disagree with raw math"
assert prop_count_s == prop_count_r, f"row count {prop_count_s} != {prop_count_r}"
print(f"   property_month_summary: {prop_count_s} rows, 0 mismatches vs raw v_monthly_pnl")

# Per-portfolio-month: summary vs. raw aggregation across the whole portfolio.
port_mismatch = db.execute(
    text(
        f"""
        WITH raw AS (
            SELECT month, {", ".join(f"SUM({m}) AS {m}" for m in MONEY)}
            FROM v_monthly_pnl GROUP BY month
        )
        SELECT count(*) FROM portfolio_month_summary s
        JOIN raw r ON r.month = s.month
        WHERE {" OR ".join(f"s.{m} <> r.{m}" for m in MONEY)}
        """
    )
).scalar()
assert port_mismatch == 0, f"{port_mismatch} portfolio-month rows disagree with raw math"
port_count = db.execute(text("SELECT count(*) FROM portfolio_month_summary")).scalar()
print(f"   portfolio_month_summary: {port_count} rows, 0 mismatches vs raw v_monthly_pnl")

# Identity checks: NOI = rent - opex; cash_flow = NOI - below_noi (in the summary itself).
ident = db.execute(
    text(
        """
        SELECT count(*) FROM portfolio_month_summary
        WHERE noi <> gross_rent - operating_expenses
           OR cash_flow <> noi - below_noi
        """
    )
).scalar()
assert ident == 0
print("   identities hold: NOI = rent - opex; cash_flow = NOI - below_noi")

# Rollup honesty: portfolio == SUM(property rows) per month, every metric.
honest = db.execute(
    text(
        f"""
        WITH p AS (
            SELECT month, {", ".join(f"SUM({m}) AS {m}" for m in MONEY)},
                   SUM(occupied_units) AS occ, SUM(total_units) AS tot
            FROM property_month_summary GROUP BY month
        )
        SELECT count(*) FROM portfolio_month_summary s JOIN p ON p.month = s.month
        WHERE {" OR ".join(f"s.{m} <> p.{m}" for m in MONEY)}
           OR s.occupied_units <> p.occ OR s.total_units <> p.tot
        """
    )
).scalar()
assert honest == 0
print("   portfolio = SUM(property rows) for every metric incl. occupancy counts")

print("\n2) SAMPLE ROWS -------------------------------------------------------")
pm = db.execute(
    text(
        """
        SELECT month, gross_rent, operating_expenses, noi, capex, debt_service,
               cash_flow, occupied_units, total_units, occupancy, property_count
        FROM portfolio_month_summary ORDER BY month DESC LIMIT 1
        """
    )
).mappings().one()
print(f"   portfolio-month {pm['month']}:")
print(f"     rent={pm['gross_rent']}  opex={pm['operating_expenses']}  NOI={pm['noi']}")
print(f"     capex={pm['capex']}  debt={pm['debt_service']}  cash_flow={pm['cash_flow']}")
occ = f"{float(pm['occupancy']) * 100:.1f}%" if pm["occupancy"] is not None else "n/a"
print(f"     occupancy={occ} ({pm['occupied_units']}/{pm['total_units']} units across "
      f"{pm['property_count']} properties)")

ps = db.execute(
    text(
        """
        SELECT p.name, s.gross_rent, s.noi, s.cash_flow, s.occupancy,
               s.occupied_units, s.total_units
        FROM property_month_summary s JOIN properties p ON p.id = s.property_id
        WHERE s.month = :m AND s.total_units > 0
        ORDER BY s.noi DESC LIMIT 1
        """
    ),
    {"m": pm["month"]},
).mappings().one()
occ = f"{float(ps['occupancy']) * 100:.1f}%" if ps["occupancy"] is not None else "n/a"
print(f"   top property-month {pm['month']}: {ps['name']}")
print(f"     rent={ps['gross_rent']}  NOI={ps['noi']}  cash_flow={ps['cash_flow']}  "
      f"occupancy={occ} ({ps['occupied_units']}/{ps['total_units']})")

print("\n3) SPEED — raw aggregation vs. summary table -------------------------")
latest = pm["month"]

print("   (a) portfolio composition: all properties for one month")
raw_a = _bench(
    "raw: GROUP BY over v_monthly_pnl",
    f"""
    SELECT property_id, {", ".join(f"SUM({m}) AS {m}" for m in MONEY)}
    FROM v_monthly_pnl WHERE month = :m GROUP BY property_id
    """,
    {"m": latest},
)
sum_a = _bench(
    "summary: read property_month_summary",
    "SELECT * FROM property_month_summary WHERE month = :m",
    {"m": latest},
)
print(f"   -> {raw_a / sum_a:.1f}x faster\n")

print("   (b) portfolio T12 trend: 12 portfolio-month totals")
raw_b = _bench(
    "raw: GROUP BY month over v_monthly_pnl",
    f"""
    SELECT month, {", ".join(f"SUM({m}) AS {m}" for m in MONEY)}
    FROM v_monthly_pnl GROUP BY month ORDER BY month DESC LIMIT 12
    """,
)
sum_b = _bench(
    "summary: read portfolio_month_summary",
    "SELECT * FROM portfolio_month_summary ORDER BY month DESC LIMIT 12",
)
print(f"   -> {raw_b / sum_b:.1f}x faster")

db.close()
print("\nALL SUB-STEP 1 CHECKS PASSED.")
