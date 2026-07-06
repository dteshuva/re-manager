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


def _shift_month(d: date, n: int) -> date:
    total = d.year * 12 + d.month - 1 + n
    return date(total // 12, total % 12 + 1, 1)


def _months_inclusive(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month) + 1


def _period_metrics(db: Session, table: str, date_from: date, date_to: date, where: str, params: dict):
    """Aggregate one summary table over a month range into a single KPI block.

    Financial metrics SUM over the period (they're flows). Occupancy is the
    period's unit-month-weighted average; unit counts / property_count are the
    period-end snapshot (stocks, shown as of the last month with data). Returns
    None when the period has no rows.
    """
    has_pcount = table == "portfolio_month_summary"
    sums = ", ".join(f"COALESCE(SUM({m}), 0) AS {m}" for m in METRICS)
    p = {**params, "f": date_from, "t": date_to}
    agg = db.execute(
        text(
            f"SELECT count(*) AS n, {sums}, "
            "COALESCE(SUM(occupied_units), 0) AS occ_sum, "
            "COALESCE(SUM(total_units), 0) AS tot_sum "
            f"FROM {table} WHERE {where} AND month BETWEEN :f AND :t"
        ),
        p,
    ).mappings().first()
    if not agg or agg["n"] == 0:
        return None
    snap_cols = "occupied_units, total_units" + (", property_count" if has_pcount else "")
    snap = db.execute(
        text(
            f"SELECT {snap_cols} FROM {table} WHERE {where} AND month BETWEEN :f AND :t "
            "ORDER BY month DESC LIMIT 1"
        ),
        p,
    ).mappings().first()
    out = {m: float(agg[m]) for m in METRICS}
    tot = float(agg["tot_sum"])
    out["occupancy"] = (float(agg["occ_sum"]) / tot) if tot > 0 else None
    out["occupied_units"] = snap["occupied_units"]
    out["total_units"] = snap["total_units"]
    if has_pcount:
        out["property_count"] = snap["property_count"]
    return out


def portfolio_dashboard(
    db: Session, date_from: date | None = None, date_to: date | None = None
) -> dict:
    """Landing-page KPI band + T12 sparklines, read STRICTLY from ``portfolio_month_summary``.

    Period-aware: with no params it defaults to the latest single month (current vs prior
    month). Given a range (a month, YTD, T12, or a custom span), it sums the period and
    compares against the immediately preceding equal-length period for the KPI deltas. The
    sparklines stay a trailing-12 ending at the period's last month.
    """
    return _dashboard_payload(db, "portfolio_month_summary", "TRUE", {}, date_from, date_to)


def _dashboard_payload(db, table, where, params, date_from, date_to):
    latest_sql = f"SELECT max(month) FROM {table} WHERE {where}"
    if date_to is None:
        date_to = db.execute(text(latest_sql), params).scalar()
    if date_to is None:
        return {"period_from": None, "period_to": None, "prior_from": None,
                "prior_to": None, "current": None, "prior": None, "trend": []}
    if date_from is None:
        date_from = date_to

    length = _months_inclusive(date_from, date_to)
    prior_to = _shift_month(date_from, -1)
    prior_from = _shift_month(date_from, -length)

    current = _period_metrics(db, table, date_from, date_to, where, params)
    prior = _period_metrics(db, table, prior_from, prior_to, where, params)
    trend = db.execute(
        text(
            "SELECT month, gross_rent, operating_expenses, noi, cash_flow, occupancy "
            f"FROM {table} WHERE {where} AND month <= :t ORDER BY month DESC LIMIT 12"
        ),
        {**params, "t": date_to},
    ).mappings().all()

    return {
        "period_from": date_from,
        "period_to": date_to,
        "prior_from": prior_from if prior else None,
        "prior_to": prior_to if prior else None,
        "current": current,
        "prior": prior,
        "trend": [dict(t) for t in reversed(trend)],
    }


def property_dashboard(
    db: Session, property_id: str, date_from: date | None = None, date_to: date | None = None
) -> dict | None:
    """Property-scoped, period-aware KPI band + T12 sparklines, from ``property_month_summary``.

    Mirrors :func:`portfolio_dashboard` scoped to one property. Returns None if the property
    does not exist.
    """
    prop = db.execute(
        text("SELECT id::text AS id, name, type FROM properties WHERE id = :id"),
        {"id": property_id},
    ).mappings().first()
    if prop is None:
        return None

    payload = _dashboard_payload(
        db, "property_month_summary", "property_id = :id", {"id": property_id}, date_from, date_to
    )
    return {
        "property_id": prop["id"],
        "property_name": prop["name"],
        "type": prop["type"],
        **payload,
    }


# Whitelist of sortable roster columns -> SQL expression (guards against injection).
_ROSTER_SORT = {
    "unit_number": "u.unit_number",
    "gross_rent": "gross_rent",
    "operating_expenses": "operating_expenses",
    "noi": "noi",
    "cash_flow": "cash_flow",
    "noi_change": "noi_change",
    "status": "status",
}


def unit_roster(
    db: Session,
    property_id: str,
    month: date,
    *,
    sort: str = "unit_number",
    order: str = "asc",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Server-paginated/sortable unit roster for a property-month (from unit_month_summary).

    Every unit in the roster appears (LEFT JOIN), so units that are vacant for the month
    show as status='vacant' with zeros, not as missing rows. ``noi_change`` is vs the prior
    month (NULL when the unit has no prior-month data).
    """
    prior = date(month.year + (month.month - 2) // 12, (month.month - 2) % 12 + 1, 1)
    sort_col = _ROSTER_SORT.get(sort, "u.unit_number")
    direction = "DESC" if order.lower() == "desc" else "ASC"

    total = db.execute(
        text("SELECT count(*) FROM units WHERE property_id = :pid"), {"pid": property_id}
    ).scalar()

    metric_cols = ",\n            ".join(f"COALESCE(cur.{m}, 0) AS {m}" for m in METRICS)
    rows = db.execute(
        text(
            f"""
            SELECT
                u.id::text AS unit_id, u.unit_number, u.label,
                {metric_cols},
                CASE WHEN COALESCE(cur.gross_rent, 0) > 0 THEN 'occupied' ELSE 'vacant' END AS status,
                CASE WHEN prev.unit_id IS NOT NULL
                     THEN COALESCE(cur.noi, 0) - prev.noi ELSE NULL END AS noi_change
            FROM units u
            LEFT JOIN unit_month_summary cur ON cur.unit_id = u.id AND cur.month = :month
            LEFT JOIN unit_month_summary prev ON prev.unit_id = u.id AND prev.month = :prior
            WHERE u.property_id = :pid
            ORDER BY {sort_col} {direction}, u.unit_number ASC
            LIMIT :limit OFFSET :offset
            """
        ),
        {"pid": property_id, "month": month, "prior": prior, "limit": limit, "offset": offset},
    )
    return {
        "month": month,
        "prior_month": prior,
        "total": total,
        "rows": _rows(rows),
    }


def unit_detail(db: Session, unit_id: str) -> dict | None:
    """Level 3 unit detail: identity + full monthly P&L from ``unit_month_summary``.

    The month series is spined on the property's summarized months and LEFT JOINed to the
    unit, so months the unit was vacant show as zeros with status='vacant' (e.g. a unit that
    went vacant mid-year visibly drops to 0), instead of silently disappearing. Returns None
    if the unit does not exist.
    """
    u = db.execute(
        text(
            """
            SELECT u.id::text AS uid, u.unit_number, u.label,
                   u.property_id::text AS pid, p.name AS pname
            FROM units u JOIN properties p ON p.id = u.property_id
            WHERE u.id = :id
            """
        ),
        {"id": unit_id},
    ).mappings().first()
    if u is None:
        return None

    metric_cols = ",\n            ".join(f"COALESCE(ums.{m}, 0) AS {m}" for m in METRICS)
    rows = db.execute(
        text(
            f"""
            SELECT pm.month,
                {metric_cols},
                CASE WHEN COALESCE(ums.gross_rent, 0) > 0 THEN 'occupied' ELSE 'vacant' END AS status
            FROM property_month_summary pm
            LEFT JOIN unit_month_summary ums ON ums.unit_id = :uid AND ums.month = pm.month
            WHERE pm.property_id = :pid
            ORDER BY pm.month
            """
        ),
        {"uid": unit_id, "pid": u["pid"]},
    ).mappings().all()
    months = [dict(r) for r in rows]
    return {
        "unit_id": u["uid"],
        "unit_number": u["unit_number"],
        "label": u["label"],
        "property_id": u["pid"],
        "property_name": u["pname"],
        "status": months[-1]["status"] if months else "vacant",
        "months": months,
    }


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
