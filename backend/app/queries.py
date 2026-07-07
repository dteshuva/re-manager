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
    # A very early `date_from` can push the equal-length prior period below year 1, which
    # `date()` rejects. That just means there's no prior period to compare against — skip it
    # rather than 500 the whole dashboard.
    try:
        prior_to = _shift_month(date_from, -1)
        prior_from = _shift_month(date_from, -length)
        prior = _period_metrics(db, table, prior_from, prior_to, where, params)
    except (ValueError, OverflowError):
        prior_from = prior_to = prior = None

    current = _period_metrics(db, table, date_from, date_to, where, params)
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
    LEFT JOINs keep units with no data in the period (as zeros), but properties with no
    entries in the period are dropped from the breakdown. All figures stay computed from
    current classifications.
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
        HAVING COUNT(v.month) > 0
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


# ---- Investment insights (acquisition inputs + return metrics from the rollup) -------------
#
# All metrics are derived on read from the acquisition inputs plus ``property_month_summary``
# (noi, cash_flow, debt_service per property-month; cash_flow is already levered/net of debt
# service). Nothing is stored, so reclassifications and new months flow through automatically.
#
# Design rules (see the feature memo):
#   * equity_invested = purchase_price - loan_amount + closing_costs  (cash actually in).
#   * Time math keys off months WITH data, never purchase_date (data may start years after the
#     purchase). purchase_date only clamps the window to months at/after acquisition.
#   * Cap rate annualizes NOI from whatever window exists (NOI is smooth) — flagged when < 12mo.
#   * Cash-on-cash is gated to a full trailing year: cash flow is lumpy (capex), so annualizing
#     a stub produces garbage. Average cash-on-cash needs >= 24 months to average >1 year.


def _load_investment_months(db: Session, property_id: str, purchase_date: date) -> list[dict]:
    """Summarized months at/after the purchase month, ascending (noi, cash_flow, debt_service)."""
    first = purchase_date.replace(day=1)
    rows = db.execute(
        text(
            "SELECT month, noi, cash_flow, debt_service FROM property_month_summary "
            "WHERE property_id = :id AND month >= :first ORDER BY month"
        ),
        {"id": property_id, "first": first},
    ).mappings().all()
    return [dict(r) for r in rows]


def _cap_noi(t12_noi: float | None, t12_months: int, annualized: bool) -> float | None:
    """NOI used as the cap-rate numerator: the raw trailing-12 sum for a property with a full
    year of history, or annualized-from-what-exists for one younger than a year."""
    if t12_noi is None:
        return None
    if annualized and t12_months:
        return t12_noi / t12_months * 12
    return t12_noi


def _investment_figures(inv: dict, months: list[dict]) -> dict:
    """Derive equity + the four return metrics from inputs (``inv``) and the month series.

    ``months`` is ascending and already clamped to >= the purchase month. The trailing-12
    window is the 12 *calendar* months ending at the latest data month (NOT the last 12 rows) —
    an internal gap (a missing month) contributes 0 rather than pulling an older month into the
    window. ``annualized`` is True only when the data doesn't yet *span* a full year (a genuinely
    young hold), which is the only case where we annualize; a full-year property with one missing
    month is summed as-is. Metrics that can't be computed honestly come back as None."""
    price = float(inv["purchase_price"])
    closing = float(inv["closing_costs"])
    loan = float(inv["loan_amount"])
    equity = price - loan + closing
    out = {
        "purchase_price": price,
        "closing_costs": closing,
        "loan_amount": loan,
        "purchase_date": inv["purchase_date"],
        "equity_invested": equity,
        "months_available": len(months),
        "t12_months": 0,
        "t12_noi": None,
        "t12_cash_flow": None,
        "t12_debt_service": None,
        "annualized": False,
        "cap_rate": None,
        "cash_on_cash": None,
        "dscr": None,
        "avg_cash_on_cash": None,
    }
    if not months:
        return out

    earliest, latest = months[0]["month"], months[-1]["month"]
    window_start = _shift_month(latest, -11)  # first month of the trailing-12 calendar window
    window = [m for m in months if m["month"] >= window_start]
    n = len(window)  # present months within the 12-month window (< 12 ⇒ gap or young)
    full_year = earliest <= window_start  # data reaches back a full trailing year
    t12_noi = sum(float(m["noi"]) for m in window)
    t12_cf = sum(float(m["cash_flow"]) for m in window)
    t12_debt = sum(float(m["debt_service"]) for m in window)
    out.update(
        t12_months=n,
        t12_noi=t12_noi,
        t12_cash_flow=t12_cf,
        t12_debt_service=t12_debt,
        annualized=not full_year,
    )

    # Cap rate: raw T12 sum for a full year of history; annualized only for a young hold.
    cap_noi = _cap_noi(t12_noi, n, not full_year)
    if price > 0 and cap_noi is not None:
        out["cap_rate"] = cap_noi / price
    # Cash-on-cash: only with a full trailing year of history (never annualize lumpy cash flow).
    if full_year and equity > 0:
        out["cash_on_cash"] = t12_cf / equity
    # DSCR: does NOI cover debt service? A ratio, so a gap/short window doesn't distort it.
    if t12_debt > 0:
        out["dscr"] = t12_noi / t12_debt
    # Average cash-on-cash: needs the data to SPAN >= 24 months so it averages > 1 year. Years
    # elapsed keys off the span (not the row count), so a gap doesn't inflate the average.
    span = _months_inclusive(earliest, latest)
    if span >= 24 and equity > 0:
        cum_cf = sum(float(m["cash_flow"]) for m in months)
        out["avg_cash_on_cash"] = cum_cf / equity / (span / 12)
    return out


def investment_metrics(db: Session, property_id: str) -> dict | None:
    """One property's acquisition inputs + computed return metrics.

    Returns None if the property does not exist. If the property exists but has no investment
    row yet, returns the identity with all inputs/metrics None (so the UI can prompt for input).
    """
    prop = db.execute(
        text("SELECT id::text AS id, name, type FROM properties WHERE id = :id"),
        {"id": property_id},
    ).mappings().first()
    if prop is None:
        return None

    base = {
        "property_id": prop["id"],
        "property_name": prop["name"],
        "type": prop["type"],
        "purchase_price": None,
        "closing_costs": None,
        "loan_amount": None,
        "purchase_date": None,
        "equity_invested": None,
        "months_available": 0,
        "t12_months": 0,
        "t12_noi": None,
        "t12_cash_flow": None,
        "t12_debt_service": None,
        "annualized": False,
        "cap_rate": None,
        "cash_on_cash": None,
        "dscr": None,
        "avg_cash_on_cash": None,
    }
    inv = db.execute(
        text(
            "SELECT purchase_price, closing_costs, loan_amount, purchase_date "
            "FROM property_investment WHERE property_id = :id"
        ),
        {"id": property_id},
    ).mappings().first()
    if inv is None:
        return base

    months = _load_investment_months(db, property_id, inv["purchase_date"])
    base.update(_investment_figures(inv, months))
    return base


def portfolio_investment(db: Session) -> dict:
    """Every property with investment inputs + value-weighted portfolio aggregates.

    Aggregates are component sums (Σ annualized-NOI / Σ price, Σ T12 cash flow / Σ equity,
    Σ T12 NOI / Σ T12 debt), i.e. the natural value-weighted average — never a mean of
    per-property rates. Each aggregate is scoped to only the properties eligible for that
    metric (cash-on-cash: >= 12 months; DSCR: debt recorded), reported via ``*_property_count``.
    """
    rows = db.execute(
        text(
            "SELECT p.id::text AS id, p.name, p.type, i.purchase_price, i.closing_costs, "
            "       i.loan_amount, i.purchase_date "
            "FROM property_investment i JOIN properties p ON p.id = i.property_id "
            "ORDER BY p.name"
        )
    ).mappings().all()

    props: list[dict] = []
    sum_noi_annual = sum_price = 0.0
    sum_cf = sum_equity = 0.0
    sum_t12_noi = sum_debt = 0.0
    cap_n = coc_n = dscr_n = 0
    total_price = total_equity = 0.0

    for r in rows:
        months = _load_investment_months(db, r["id"], r["purchase_date"])
        fig = _investment_figures(r, months)
        props.append(
            {"property_id": r["id"], "property_name": r["name"], "type": r["type"], **fig}
        )
        total_price += fig["purchase_price"]
        total_equity += fig["equity_invested"]
        cap_noi = _cap_noi(fig["t12_noi"], fig["t12_months"], fig["annualized"])
        if cap_noi is not None and fig["purchase_price"] > 0:
            sum_noi_annual += cap_noi
            sum_price += fig["purchase_price"]
            cap_n += 1
        # Cash-on-cash aggregate counts only properties with a full year of history.
        if not fig["annualized"] and fig["t12_months"] > 0 and fig["equity_invested"] > 0:
            sum_cf += fig["t12_cash_flow"]
            sum_equity += fig["equity_invested"]
            coc_n += 1
        if fig["t12_debt_service"] and fig["t12_debt_service"] > 0:
            sum_t12_noi += fig["t12_noi"]
            sum_debt += fig["t12_debt_service"]
            dscr_n += 1

    return {
        "properties": props,
        "cap_rate": (sum_noi_annual / sum_price) if sum_price > 0 else None,
        "cash_on_cash": (sum_cf / sum_equity) if sum_equity > 0 else None,
        "dscr": (sum_t12_noi / sum_debt) if sum_debt > 0 else None,
        "total_purchase_price": total_price,
        "total_equity_invested": total_equity,
        "cap_rate_property_count": cap_n,
        "cash_on_cash_property_count": coc_n,
        "dscr_property_count": dscr_n,
    }
