"""Read-only query layer over the P&L views.

All financial aggregation reads ``v_monthly_pnl`` (per property/unit/month grain) and
sums it for the requested scope. NOI and cash flow are computed in SQL, never stored,
and the SQL sums **by classification**, so new categories and reclassifications are
picked up automatically with no code change here.

Rollup honesty (the heart of the spec):
    Rent and operating expenses are additive (unit → property → portfolio). But
    property-tier-only items (``unit_id IS NULL``: shared capex, debt service) are NOT
    allocated down to units. So ``property total ≠ sum of units`` for those metrics.
    :func:`property_monthly` keeps this honest by returning the unit rollup and the
    property-tier subtotal as distinct blocks alongside the combined total.
"""

from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

# Order matters only for readability; every scope returns this exact metric set.
METRICS = (
    "gross_rent",
    "operating_expenses",
    "noi",
    "capex",
    "debt_service",
    "other_below_line",
    "below_noi",
    "cash_flow",
)


def _sum_cols(prefix: str = "", where: str | None = None) -> str:
    """SQL fragment summing every metric, optionally FILTERed, with a name prefix."""
    flt = f" FILTER (WHERE {where})" if where else ""
    return ",\n        ".join(
        f"COALESCE(SUM({m}){flt}, 0) AS {prefix}{m}" for m in METRICS
    )


def _metrics(row: dict, prefix: str = "") -> dict:
    return {m: row[f"{prefix}{m}"] for m in METRICS}


def _rows(result) -> list[dict]:
    return [dict(r._mapping) for r in result]


def _range(col: str = "month") -> str:
    return (
        f"(:date_from IS NULL OR {col} >= :date_from) "
        f"AND (:date_to IS NULL OR {col} <= :date_to)"
    )


def portfolio_monthly(
    db: Session, date_from: date | None = None, date_to: date | None = None
) -> list[dict]:
    """Portfolio P&L by month across every property and unit (and property-tier rows)."""
    sql = f"""
        SELECT month, {_sum_cols()}
        FROM v_monthly_pnl
        WHERE {_range()}
        GROUP BY month
        ORDER BY month
    """
    return _rows(db.execute(text(sql), {"date_from": date_from, "date_to": date_to}))


def property_monthly(
    db: Session, property_id: str, date_from: date | None = None, date_to: date | None = None
) -> list[dict]:
    """Property P&L by month with an honest unit-rollup vs property-tier split.

    Returns rows shaped for ``PropertyMonthlyPnL``: combined total at the top level
    plus ``units`` (sum of unit rows) and ``property_tier`` (unit_id IS NULL rows).
    """
    sql = f"""
        SELECT
            month,
            {_sum_cols()},
            {_sum_cols(prefix="unit_", where="unit_id IS NOT NULL")},
            {_sum_cols(prefix="tier_", where="unit_id IS NULL")}
        FROM v_monthly_pnl
        WHERE property_id = :property_id AND {_range()}
        GROUP BY month
        ORDER BY month
    """
    rows = _rows(
        db.execute(
            text(sql),
            {"property_id": property_id, "date_from": date_from, "date_to": date_to},
        )
    )
    return [
        {
            "month": r["month"],
            **_metrics(r),
            "units": _metrics(r, prefix="unit_"),
            "property_tier": _metrics(r, prefix="tier_"),
        }
        for r in rows
    ]


def property_units_monthly(
    db: Session, property_id: str, date_from: date | None = None, date_to: date | None = None
) -> list[dict]:
    """Per-unit monthly P&L for a property (property-tier-only rows excluded).

    One row per (unit, month). Property-tier-only items are intentionally absent here;
    they belong to the property scope, not to any unit.
    """
    sql = f"""
        SELECT
            u.id::text    AS unit_id,
            u.unit_number AS unit_number,
            u.label       AS label,
            p.month       AS month,
            {_sum_cols()}
        FROM v_monthly_pnl p
        JOIN units u ON u.id = p.unit_id
        WHERE p.property_id = :property_id
          AND p.unit_id IS NOT NULL
          AND {_range()}
        GROUP BY u.id, u.unit_number, u.label, p.month
        ORDER BY u.unit_number, p.month
    """
    return _rows(
        db.execute(
            text(sql),
            {"property_id": property_id, "date_from": date_from, "date_to": date_to},
        )
    )


def unit_monthly(
    db: Session, unit_id: str, date_from: date | None = None, date_to: date | None = None
) -> list[dict]:
    """Single-unit P&L by month."""
    sql = f"""
        SELECT month, {_sum_cols()}
        FROM v_monthly_pnl
        WHERE unit_id = :unit_id AND {_range()}
        GROUP BY month
        ORDER BY month
    """
    return _rows(
        db.execute(
            text(sql),
            {"unit_id": unit_id, "date_from": date_from, "date_to": date_to},
        )
    )


def portfolio_breakdown(
    db: Session, date_from: date | None = None, date_to: date | None = None
) -> dict:
    """Period totals across the whole hierarchy: portfolio total + per-property breakdown
    (each with its unit list and the honest unit-vs-property-tier split).

    Two grouped queries (properties, units) over ``v_monthly_pnl``, assembled into a tree.
    LEFT JOINs keep properties/units with no data in the period (as zeros). All figures
    stay computed from current classifications.
    """
    params = {"date_from": date_from, "date_to": date_to}

    prop_sql = f"""
        SELECT
            p.id::text AS property_id,
            p.name     AS property_name,
            p.type     AS type,
            {_sum_cols()},
            {_sum_cols(prefix="tier_", where="v.unit_id IS NULL")}
        FROM properties p
        LEFT JOIN v_monthly_pnl v
               ON v.property_id = p.id AND {_range("v.month")}
        GROUP BY p.id, p.name, p.type
        ORDER BY p.name
    """
    prop_rows = _rows(db.execute(text(prop_sql), params))

    unit_sql = f"""
        SELECT
            u.property_id::text AS property_id,
            u.id::text          AS unit_id,
            u.unit_number       AS unit_number,
            u.label             AS label,
            {_sum_cols()}
        FROM units u
        LEFT JOIN v_monthly_pnl v
               ON v.unit_id = u.id AND {_range("v.month")}
        GROUP BY u.property_id, u.id, u.unit_number, u.label
        ORDER BY u.unit_number
    """
    units_by_prop: dict[str, list[dict]] = {}
    for u in _rows(db.execute(text(unit_sql), params)):
        units_by_prop.setdefault(u["property_id"], []).append(
            {"unit_id": u["unit_id"], "unit_number": u["unit_number"], "label": u["label"], **_metrics(u)}
        )

    properties = []
    total = {m: 0.0 for m in METRICS}
    for r in prop_rows:
        for m in METRICS:
            total[m] += float(r[m])
        properties.append(
            {
                "property_id": r["property_id"],
                "property_name": r["property_name"],
                "type": r["type"],
                **_metrics(r),
                "units": units_by_prop.get(r["property_id"], []),
                "property_tier": _metrics(r, prefix="tier_"),
            }
        )

    return {"total": total, "properties": properties}
