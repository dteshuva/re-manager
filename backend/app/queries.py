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

Account scoping (migration 0017):
    Every public function here takes ``account_id`` as a REQUIRED positional argument
    immediately after ``db``, and every statement filters on it — including the
    property-scoped and unit-scoped ones, where the id alone would already narrow the
    result. That redundancy is deliberate: it means a router that forgets to verify
    ownership still cannot read another account's data, and a *new* call site that
    forgets to scope is a TypeError rather than a silent leak. "Portfolio" throughout
    this module means one account's portfolio, never the whole database.
"""

import calendar
from datetime import date, timedelta

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


# ---- Portfolio segmentation (tags) ----------------------------------------------------
#
# property_tag is a pure filter axis (property_id, tag): a property may carry any number of
# tags. The dashboard/breakdown/attention/monthly queries accept an optional ``tags`` list
# with OR semantics — a property matches if it carries ANY of the requested tags. Passing
# ``tags=None`` (the default) is a no-op filter (every property matches), so every call site
# below can unconditionally include this clause and just always bind the ``tags`` param.
def _tag_where(alias: str = "property_id") -> str:
    # NOTE: SQLAlchemy's text() bind-parameter parser doesn't handle ":name::type" (the
    # Postgres cast shorthand right after a bound name) — CAST(:name AS type) instead.
    return (
        f"(CAST(:tags AS text[]) IS NULL OR {alias} IN "
        f"(SELECT property_id FROM property_tag WHERE tag = ANY(CAST(:tags AS text[]))))"
    )


def portfolio_monthly(
    db: Session,
    account_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
    tags: list[str] | None = None,
) -> list[dict]:
    """Portfolio P&L by month across the account's properties and units (and property-tier rows).

    ``tags`` optionally scopes to properties carrying ANY of the given tags (OR semantics).
    """
    sql = f"""
        SELECT month, {_sum_cols()}
        FROM v_monthly_pnl
        WHERE account_id = :account_id AND {_range()} AND {_tag_where()}
        GROUP BY month
        ORDER BY month
    """
    return _rows(
        db.execute(
            text(sql),
            {
                "account_id": account_id,
                "date_from": date_from,
                "date_to": date_to,
                "tags": tags,
            },
        )
    )


def property_monthly(
    db: Session,
    account_id: str,
    property_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
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
        WHERE account_id = :account_id AND property_id = :property_id AND {_range()}
        GROUP BY month
        ORDER BY month
    """
    rows = _rows(
        db.execute(
            text(sql),
            {
                "account_id": account_id,
                "property_id": property_id,
                "date_from": date_from,
                "date_to": date_to,
            },
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
    db: Session,
    account_id: str,
    property_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
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
        WHERE p.account_id = :account_id
          AND p.property_id = :property_id
          AND p.unit_id IS NOT NULL
          AND {_range()}
        GROUP BY u.id, u.unit_number, u.label, p.month
        ORDER BY u.unit_number, p.month
    """
    return _rows(
        db.execute(
            text(sql),
            {
                "account_id": account_id,
                "property_id": property_id,
                "date_from": date_from,
                "date_to": date_to,
            },
        )
    )


def unit_monthly(
    db: Session,
    account_id: str,
    unit_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict]:
    """Single-unit P&L by month."""
    sql = f"""
        SELECT month, {_sum_cols()}
        FROM v_monthly_pnl
        WHERE account_id = :account_id AND unit_id = :unit_id AND {_range()}
        GROUP BY month
        ORDER BY month
    """
    return _rows(
        db.execute(
            text(sql),
            {
                "account_id": account_id,
                "unit_id": unit_id,
                "date_from": date_from,
                "date_to": date_to,
            },
        )
    )


def _shift_month(d: date, n: int) -> date:
    total = d.year * 12 + d.month - 1 + n
    return date(total // 12, total % 12 + 1, 1)


def _months_inclusive(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month) + 1


def _period_metrics(
    db: Session,
    table: str,
    date_from: date,
    date_to: date,
    where: str,
    params: dict,
    *,
    multi_scope: bool = False,
) -> dict | None:
    """Aggregate one summary table over a month range into a single KPI block.

    Financial metrics SUM over the period (they're flows). Occupancy is the
    period's unit-month-weighted average; unit counts / property_count are the
    period-end snapshot (stocks, shown as of the last month with data). Returns
    None when the period has no rows.

    ``multi_scope=True`` is for a tag-filtered *portfolio* view read from
    ``property_month_summary`` (i.e. ``where`` can match several properties' rows sharing
    the same month) — the snapshot then SUMs every matching row at the latest month and
    counts the distinct properties, rather than assuming one row per month (which holds for
    ``portfolio_month_summary`` and for a single property's own rows, but not for an
    arbitrary multi-property subset).
    """
    has_pcount = table == "portfolio_month_summary" or multi_scope
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
    if multi_scope:
        snap = db.execute(
            text(
                f"""
                WITH latest AS (
                    SELECT max(month) AS m FROM {table} WHERE {where} AND month BETWEEN :f AND :t
                )
                SELECT COALESCE(SUM(occupied_units), 0) AS occupied_units,
                       COALESCE(SUM(total_units), 0) AS total_units,
                       COUNT(*) AS property_count
                FROM {table}, latest
                WHERE {where} AND month = latest.m
                """
            ),
            p,
        ).mappings().first()
    else:
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
    db: Session,
    account_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Landing-page KPI band + T12 sparklines, read STRICTLY from ``portfolio_month_summary``.

    Period-aware: with no params it defaults to the latest single month (current vs prior
    month). Given a range (a month, YTD, T12, or a custom span), it sums the period and
    compares against the immediately preceding equal-length period for the KPI deltas. The
    sparklines stay a trailing-12 ending at the period's last month. ``tags`` optionally
    scopes to properties carrying ANY of the given tags (OR semantics) — the whole KPI band,
    prior-period comparison, and sparklines are recomputed over just that subset. Unfiltered
    (the common case) still reads STRICTLY from the precomputed ``portfolio_month_summary``;
    a tag filter re-aggregates ``property_month_summary`` for just the matching properties
    (there's no precomputed rollup for an arbitrary tag subset).
    """
    if tags:
        return _dashboard_payload(
            db,
            "property_month_summary",
            f"account_id = :account_id AND {_tag_where()}",
            {"account_id": account_id, "tags": tags},
            date_from,
            date_to,
            multi_scope=True,
        )
    return _dashboard_payload(
        db,
        "portfolio_month_summary",
        "account_id = :account_id",
        {"account_id": account_id},
        date_from,
        date_to,
    )


def current_month() -> date:
    """The first of the current calendar month — the boundary between actuals and the future."""
    return date.today().replace(day=1)


def latest_actual_month(db: Session, table: str, where: str, params: dict) -> date | None:
    """The most recent summarized month for a scope that is **not in the future**.

    "Latest month on file" is the anchor for nearly everything the app shows by default: the
    dashboard's period, a property's headline figures, the attention feed, the rent roll's
    "actual" column, and the trailing-twelve window behind cap rate / cash-on-cash / DSCR.

    A plain ``max(month)`` was fine while every row came from someone typing up a month that
    had already happened. It stopped being fine once a recurring cost could be posted ahead
    (see :mod:`app.routers.shared_expenses`): posting two years of a loan payment creates two
    years of property-months that hold the expense but not the rent nobody has collected yet.
    Anchoring on those made the default view land in 2028 and dragged expense-only months into
    the trailing-twelve window, so cap rate and cash-on-cash reported against costs with no
    matching income.

    So the rule is: **a month that hasn't happened yet is not an actual.** Scheduling a cost
    ahead is a feature; letting it masquerade as performance is not. Future months stay in the
    ledger and are shown in full to anyone who explicitly asks for a range covering them — this
    only governs what an *unspecified* period defaults to, and what the return metrics measure.
    """
    return db.execute(
        text(f"SELECT max(month) FROM {table} WHERE {where} AND month <= :as_of_month"),
        {**params, "as_of_month": current_month()},
    ).scalar()


def _resolve_range(
    db: Session, table: str, where: str, params: dict, date_from: date | None, date_to: date | None
) -> tuple[date | None, date | None]:
    """Fill in an unspecified range the same way every period-aware endpoint does: an
    unspecified ``date_to`` defaults to the scope's latest actual (non-future) summarized
    month, and an unspecified ``date_from`` defaults to that same month (a single-month
    period). An explicitly requested range is honoured as given, future months included —
    see :func:`latest_actual_month`. Returns ``(None, None)`` when the scope has no
    summarized data at all."""
    if date_to is None:
        date_to = latest_actual_month(db, table, where, params)
    if date_to is None:
        return None, None
    if date_from is None:
        date_from = date_to
    return date_from, date_to


def _dashboard_payload(db, table, where, params, date_from, date_to, *, multi_scope: bool = False):
    date_from, date_to = _resolve_range(db, table, where, params, date_from, date_to)
    if date_to is None:
        return {"period_from": None, "period_to": None, "prior_from": None,
                "prior_to": None, "current": None, "prior": None, "trend": []}

    length = _months_inclusive(date_from, date_to)
    # A very early `date_from` can push the equal-length prior period below year 1, which
    # `date()` rejects. That just means there's no prior period to compare against — skip it
    # rather than 500 the whole dashboard.
    try:
        prior_to = _shift_month(date_from, -1)
        prior_from = _shift_month(date_from, -length)
        prior = _period_metrics(db, table, prior_from, prior_to, where, params, multi_scope=multi_scope)
    except (ValueError, OverflowError):
        prior_from = prior_to = prior = None

    current = _period_metrics(db, table, date_from, date_to, where, params, multi_scope=multi_scope)
    if multi_scope:
        # Several properties can share a month, so the sparkline sums each month's matching
        # rows rather than reading one row per month straight off the table.
        trend = db.execute(
            text(
                f"""
                SELECT month, SUM(gross_rent) AS gross_rent, SUM(operating_expenses) AS operating_expenses,
                       SUM(noi) AS noi, SUM(debt_service) AS debt_service, SUM(cash_flow) AS cash_flow,
                       CASE WHEN SUM(total_units) > 0
                            THEN SUM(occupied_units)::numeric / SUM(total_units) ELSE NULL END AS occupancy
                FROM {table} WHERE {where} AND month <= :t
                GROUP BY month ORDER BY month DESC LIMIT 12
                """
            ),
            {**params, "t": date_to},
        ).mappings().all()
    else:
        trend = db.execute(
            text(
                "SELECT month, gross_rent, operating_expenses, noi, debt_service, cash_flow, occupancy "
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


def property_categories(
    db: Session, account_id: str, property_id: str, date_from: date | None, date_to: date | None
) -> list[dict]:
    """Every category this property posted in the period, with its total and its classification.

    What this answers is "what IS the operating expense figure" — a single number on a
    dashboard tells an owner that £412 left the building and nothing about whether it was one
    boiler or twelve small bills, which is the difference between a month to investigate and a
    month to ignore.

    Read from ``v_line_item_resolved`` rather than from the summary tables, for the same reason
    the P&L views are: the classification there is the category's CURRENT one, so reclassifying
    a category moves its spend between sections here with no backfill — the number in this
    table and the number in the KPI band can never disagree.

    Returns every classification, not just ``operating``. The caller decides what to show; the
    query is the same work either way, and the below-NOI section needs exactly this breakdown
    to stop being one opaque figure.
    """
    rows = db.execute(
        text(
            f"""
            SELECT category_id::text AS category_id,
                   category_name     AS category,
                   classification::text AS classification,
                   SUM(amount)       AS amount
            FROM v_line_item_resolved
            WHERE account_id = :account_id AND property_id = :property_id AND {_range()}
            GROUP BY category_id, category_name, classification
            HAVING SUM(amount) <> 0
            ORDER BY classification, SUM(amount) DESC
            """
        ),
        {"account_id": account_id, "property_id": property_id,
         "date_from": date_from, "date_to": date_to},
    ).mappings().all()
    return [{**r, "amount": float(r["amount"])} for r in rows]


def property_dashboard(
    db: Session,
    account_id: str,
    property_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict | None:
    """Property-scoped, period-aware KPI band + T12 sparklines, from ``property_month_summary``.

    Mirrors :func:`portfolio_dashboard` scoped to one property. Returns None if the property
    does not exist **in this account** — another account's property is indistinguishable
    from a nonexistent one, which is what the router turns into a 404.
    """
    prop = db.execute(
        text(
            "SELECT id::text AS id, name, type FROM properties "
            "WHERE id = :id AND account_id = :account_id"
        ),
        {"id": property_id, "account_id": account_id},
    ).mappings().first()
    if prop is None:
        return None

    payload = _dashboard_payload(
        db,
        "property_month_summary",
        "account_id = :account_id AND property_id = :id",
        {"account_id": account_id, "id": property_id},
        date_from,
        date_to,
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
    account_id: str,
    property_id: str,
    month: date,
    *,
    sort: str = "unit_number",
    order: str = "asc",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Server-paginated/sortable unit roster for a property-month (from unit_month_summary).

    Every unit in the roster appears (LEFT JOIN). ``status`` is a 3-way state: 'occupied',
    'vacant' (a unit-month record WAS posted, explicitly flagged vacant or $0 rent), or
    'missing' (no record posted for this unit-month at all — distinct from a real vacancy).
    ``noi_change`` is vs the prior month (NULL when the unit has no prior-month data).

    ``lease_status`` (migration 0012) is added ALONGSIDE ``status``, not in place of it: it's
    the unit's CURRENT lease state (today, not this row's month) — a read-only, additive
    cross-reference so an analyst can see the record-driven monthly status and the
    lease-driven current status side by side. The two are independent signals (one is a
    historical per-month fact, the other a live snapshot) and can legitimately disagree — a
    unit vacant per its June-2025 record can show an 'active' lease_status if it was re-let
    since. No existing math (occupancy %, attention thresholds) changes; see migration
    0012's docstring / the rent-roll changelog for why a full merge isn't attempted here.
    """
    prior = date(month.year + (month.month - 2) // 12, (month.month - 2) % 12 + 1, 1)
    sort_col = _ROSTER_SORT.get(sort, "u.unit_number")
    direction = "DESC" if order.lower() == "desc" else "ASC"

    total = db.execute(
        text(
            "SELECT count(*) FROM units u JOIN properties p ON p.id = u.property_id "
            "WHERE u.property_id = :pid AND p.account_id = :account_id"
        ),
        {"pid": property_id, "account_id": account_id},
    ).scalar()

    metric_cols = ",\n            ".join(f"COALESCE(cur.{m}, 0) AS {m}" for m in METRICS)
    rows = db.execute(
        text(
            f"""
            WITH {_CURRENT_LEASE_CTE}
            SELECT
                u.id::text AS unit_id, u.unit_number, u.label,
                {metric_cols},
                CASE
                    WHEN cur.unit_id IS NULL THEN 'missing'
                    WHEN cur.is_vacant OR cur.gross_rent = 0 THEN 'vacant'
                    ELSE 'occupied'
                END AS status,
                CASE WHEN prev.unit_id IS NOT NULL
                     THEN COALESCE(cur.noi, 0) - prev.noi ELSE NULL END AS noi_change,
                cl.status AS lease_status, cl.tenant_name AS lease_tenant_name,
                -- The tenancy's own terms, surfaced here so the property page can show (and
                -- edit) the rent schedule next to the month's actuals instead of sending the
                -- operator to the rent roll for the three figures they change most.
                cl.id::text AS lease_id, cl.contract_rent AS lease_contract_rent,
                cl.start_date AS lease_start, cl.end_date AS lease_end,
                cl.last_rent_increase_date, cl.rent_before_increase
            FROM units u
            JOIN properties p ON p.id = u.property_id
            LEFT JOIN unit_month_summary cur ON cur.unit_id = u.id AND cur.month = :month
            LEFT JOIN unit_month_summary prev ON prev.unit_id = u.id AND prev.month = :prior
            LEFT JOIN current_lease cl ON cl.unit_id = u.id
            WHERE u.property_id = :pid AND p.account_id = :account_id
            ORDER BY {sort_col} {direction}, u.unit_number ASC
            LIMIT :limit OFFSET :offset
            """
        ),
        {
            "pid": property_id,
            "account_id": account_id,
            "month": month,
            "prior": prior,
            "limit": limit,
            "offset": offset,
        },
    )
    out_rows = _rows(rows)
    today = date.today()
    for r in out_rows:
        r["lease_contract_rent"] = (
            float(r["lease_contract_rent"]) if r["lease_contract_rent"] is not None else None
        )
        r["rent_before_increase"] = (
            float(r["rent_before_increase"]) if r["rent_before_increase"] is not None else None
        )
        # Same derivation (and the same fallback to the tenancy's start when the rent has never
        # been increased) as the rent roll's own column, so the two pages can't disagree about
        # whether a review is overdue. See `_rent_roll_row`.
        r["months_since_last_increase"] = (
            _months_elapsed(r["last_rent_increase_date"] or r["lease_start"], today)
            if r["lease_id"] is not None else None
        )
    return {
        "month": month,
        "prior_month": prior,
        "total": total,
        "rows": out_rows,
    }


def unit_detail(db: Session, account_id: str, unit_id: str) -> dict | None:
    """Level 3 unit detail: identity + full monthly P&L from ``unit_month_summary``.

    The month series is spined on the property's summarized months and LEFT JOINed to the
    unit. ``status`` is 3-way: 'occupied', 'vacant' (a record WAS posted for that unit-month,
    explicitly flagged vacant or $0 rent), or 'missing' (no record posted for that unit-month
    at all — distinct from a real vacancy). Returns None if the unit does not exist.

    ``lease_status``/``tenant_name`` (migration 0012) are the unit's CURRENT lease — today's
    live snapshot, added ALONGSIDE (not replacing) the per-month record-driven ``status``
    series above. The two are independent signals that can legitimately disagree (e.g. a
    unit vacant per its last posted record but re-let since, or vice versa); see
    ``unit_roster``'s docstring and the rent-roll changelog for why they aren't merged.
    """
    u = db.execute(
        text(
            f"""
            WITH {_CURRENT_LEASE_CTE}
            SELECT u.id::text AS uid, u.unit_number, u.label,
                   u.property_id::text AS pid, p.name AS pname,
                   cl.status AS lease_status, cl.tenant_name AS lease_tenant_name
            FROM units u
            JOIN properties p ON p.id = u.property_id
            LEFT JOIN current_lease cl ON cl.unit_id = u.id
            WHERE u.id = :id AND p.account_id = :account_id
            """
        ),
        {"id": unit_id, "account_id": account_id},
    ).mappings().first()
    if u is None:
        return None

    metric_cols = ",\n            ".join(f"COALESCE(ums.{m}, 0) AS {m}" for m in METRICS)
    rows = db.execute(
        text(
            f"""
            SELECT pm.month,
                {metric_cols},
                CASE
                    WHEN ums.unit_id IS NULL THEN 'missing'
                    WHEN ums.is_vacant OR ums.gross_rent = 0 THEN 'vacant'
                    ELSE 'occupied'
                END AS status
            FROM property_month_summary pm
            LEFT JOIN unit_month_summary ums ON ums.unit_id = :uid AND ums.month = pm.month
            WHERE pm.property_id = :pid AND pm.account_id = :account_id
            ORDER BY pm.month
            """
        ),
        {"uid": unit_id, "pid": u["pid"], "account_id": account_id},
    ).mappings().all()
    months = [dict(r) for r in rows]
    return {
        "unit_id": u["uid"],
        "unit_number": u["unit_number"],
        "label": u["label"],
        "property_id": u["pid"],
        "property_name": u["pname"],
        "status": months[-1]["status"] if months else "missing",
        "lease_status": u["lease_status"] or "vacant",
        "lease_tenant_name": u["lease_tenant_name"],
        "months": months,
    }


def portfolio_breakdown(
    db: Session,
    account_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Period totals across the whole hierarchy: portfolio total + per-property breakdown
    (each with its unit list and the honest unit-vs-property-tier split).

    Two grouped queries (properties, units) over ``v_monthly_pnl``, assembled into a tree.
    LEFT JOINs keep units with no data in the period (as zeros), but properties with no
    entries in the period are dropped from the breakdown. All figures stay computed from
    current classifications. ``tags`` optionally scopes to properties carrying ANY of the
    given tags (OR semantics); the portfolio ``total`` is summed over only those properties.
    """
    params = {
        "account_id": account_id,
        "date_from": date_from,
        "date_to": date_to,
        "tags": tags,
    }

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
        WHERE p.account_id = :account_id AND {_tag_where("p.id")}
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
        JOIN properties p ON p.id = u.property_id
        LEFT JOIN v_monthly_pnl v
               ON v.unit_id = u.id AND {_range("v.month")}
        WHERE p.account_id = :account_id AND {_tag_where("u.property_id")}
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


def _load_investment_months(
    db: Session, account_id: str, property_id: str, purchase_date: date
) -> list[dict]:
    """Months this property has actually TRADED in, at/after the purchase month, ascending
    (noi, cash_flow, debt_service).

    Two filters, both there for the same reason: every figure downstream is a *measurement of
    what happened*, and the trailing-twelve window is anchored on the last month in this list.

    ``month <= current_month`` — a cost scheduled into next year (a shared expense posted
    ahead) would otherwise move the anchor forward and fill the window with months holding
    expenses and no income, reporting a cap rate against costs whose matching rent hasn't been
    earned yet. See :func:`latest_actual_month`.

    ``EXISTS a line item that isn't a scheduled posting`` — the same problem one month wide.
    Posting a recurring cost into the current month creates a property-month containing the
    mortgage and nothing else, weeks before anyone types the rent. Counting that as a traded
    month yields a small NEGATIVE cap rate, which reads as a real (bad) result rather than as
    the absence of one. A month whose only content was put there by a schedule has no
    performance to measure, so it is left out until something else is recorded in it — at which
    point it starts counting on its own, with no action needed."""
    first = purchase_date.replace(day=1)
    rows = db.execute(
        text(
            "SELECT s.month, s.noi, s.cash_flow, s.debt_service "
            "FROM property_month_summary s "
            "WHERE s.account_id = :account_id AND s.property_id = :id "
            "  AND s.month >= :first AND s.month <= :as_of_month "
            "  AND EXISTS ("
            "      SELECT 1 FROM monthly_records mr "
            "      JOIN line_items li ON li.monthly_record_id = mr.id "
            "      WHERE mr.property_id = s.property_id AND mr.month = s.month "
            "        AND li.shared_expense_id IS NULL"
            "  ) "
            "ORDER BY s.month"
        ),
        {
            "account_id": account_id,
            "id": property_id,
            "first": first,
            "as_of_month": current_month(),
        },
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
        # Present when these inputs came from a bulk purchase, i.e. closing costs and loan are
        # this property's allocated share of a deal-wide total (see app/routers/acquisitions).
        "acquisition_id": inv.get("acquisition_id"),
        "acquisition_name": inv.get("acquisition_name"),
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


def investment_metrics(db: Session, account_id: str, property_id: str) -> dict | None:
    """One property's acquisition inputs + computed return metrics.

    Returns None if the property does not exist in this account. If the property exists but
    has no investment row yet, returns the identity with all inputs/metrics None (so the UI
    can prompt for input).
    """
    prop = db.execute(
        text(
            "SELECT id::text AS id, name, type FROM properties "
            "WHERE id = :id AND account_id = :account_id"
        ),
        {"id": property_id, "account_id": account_id},
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
        "acquisition_id": None,
        "acquisition_name": None,
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
            "SELECT i.purchase_price, i.closing_costs, i.loan_amount, i.purchase_date, "
            "       i.acquisition_id::text AS acquisition_id, a.name AS acquisition_name "
            "FROM property_investment i JOIN properties p ON p.id = i.property_id "
            "LEFT JOIN portfolio_acquisition a ON a.id = i.acquisition_id "
            "WHERE i.property_id = :id AND p.account_id = :account_id"
        ),
        {"id": property_id, "account_id": account_id},
    ).mappings().first()
    if inv is None:
        return base

    months = _load_investment_months(db, account_id, property_id, inv["purchase_date"])
    base.update(_investment_figures(inv, months))
    return base


def portfolio_investment(db: Session, account_id: str) -> dict:
    """Every property with investment inputs + value-weighted portfolio aggregates.

    Aggregates are component sums (Σ annualized-NOI / Σ price, Σ T12 cash flow / Σ equity,
    Σ T12 NOI / Σ T12 debt), i.e. the natural value-weighted average — never a mean of
    per-property rates. Each aggregate is scoped to only the properties eligible for that
    metric (cash-on-cash: >= 12 months; DSCR: debt recorded), reported via ``*_property_count``.
    """
    rows = db.execute(
        text(
            "SELECT p.id::text AS id, p.name, p.type, i.purchase_price, i.closing_costs, "
            "       i.loan_amount, i.purchase_date, "
            "       i.acquisition_id::text AS acquisition_id, a.name AS acquisition_name "
            "FROM property_investment i JOIN properties p ON p.id = i.property_id "
            "LEFT JOIN portfolio_acquisition a ON a.id = i.acquisition_id "
            "WHERE p.account_id = :account_id "
            "ORDER BY p.name"
        ),
        {"account_id": account_id},
    ).mappings().all()

    # Adoption-gap nudge: which properties have NO investment row at all yet.
    all_props = db.execute(
        text(
            "SELECT id::text AS id, name, type FROM properties "
            "WHERE account_id = :account_id ORDER BY name"
        ),
        {"account_id": account_id},
    ).mappings().all()
    have_ids = {r["id"] for r in rows}
    missing_properties = [
        {"property_id": p["id"], "property_name": p["name"], "type": p["type"]}
        for p in all_props
        if p["id"] not in have_ids
    ]

    props: list[dict] = []
    sum_noi_annual = sum_price = 0.0
    sum_cf = sum_equity = 0.0
    sum_t12_noi = sum_debt = 0.0
    cap_n = coc_n = dscr_n = 0
    total_price = total_equity = 0.0

    for r in rows:
        months = _load_investment_months(db, account_id, r["id"], r["purchase_date"])
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
        "total_property_count": len(all_props),
        "missing_property_count": len(missing_properties),
        "missing_properties": missing_properties,
    }


# ---- Portfolio benchmarking (cross-property comparison against the portfolio average) ------
#
# Pure read-side aggregation over metrics computed elsewhere — NOTHING here is recomputed from
# raw line items. NOI/opex/physical-occupancy come from ``property_month_summary`` via the same
# ``_period_metrics`` helper ``property_dashboard`` uses; cap rate / cash-on-cash come from
# ``investment_metrics`` (T12, acquisition-based, unchanged); economic occupancy reuses the rent
# roll's current lease-status snapshot (``_rent_roll_sql`` / ``_rent_roll_row`` /
# `_occupancy_summary`), grouped per property in Python from one shared query.
#
# Unlike ``portfolio_investment``'s value-weighted aggregates (Σ / Σ, so a big property
# dominates), the "portfolio average" here is a SIMPLE MEAN across properties — each property
# counts once, which is the point of a benchmarking view (compare Property A to "the book",
# not to a book that's secretly mostly Property A). The median is reported alongside it since
# these per-property ratios can be skewed by one outlier.
_BENCHMARK_METRICS: tuple[tuple[str, bool], ...] = (
    ("noi_per_unit", True),
    ("opex_ratio", False),          # lower opex ratio is better
    ("physical_occupancy", True),
    ("economic_occupancy", True),
    ("cap_rate", True),
    ("cash_on_cash", True),
)


def _mean_median(values: list[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    median = s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2
    return sum(s) / n, median


def _rank_and_percentile(
    values_by_property: dict[str, float | None], higher_is_better: bool
) -> dict[str, tuple[int, float]]:
    """Dense rank (1 = best) + percentile (0..100, 100 = best) among properties with a
    non-null value. Ties share a rank. A lone property gets percentile 100."""
    present = [(pid, v) for pid, v in values_by_property.items() if v is not None]
    present.sort(key=lambda kv: kv[1], reverse=higher_is_better)
    n = len(present)
    out: dict[str, tuple[int, float]] = {}
    rank = 0
    prev_v = None
    for i, (pid, v) in enumerate(present):
        if prev_v is None or v != prev_v:
            rank = i + 1
        prev_v = v
        pct = 100.0 if n <= 1 else (n - rank) / (n - 1) * 100.0
        out[pid] = (rank, pct)
    return out


def portfolio_benchmarks(
    db: Session,
    account_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Benchmark every property against the portfolio's simple-mean average on NOI/unit,
    operating expense ratio, physical + economic occupancy, cap rate, and cash-on-cash — with
    each property's delta vs the mean and its rank/percentile. See the module comment above
    for the mean-vs-value-weighted and occupancy-scoping notes. ``tags`` scopes both the rows
    and the average (OR semantics).
    """
    if tags:
        date_from, date_to = _resolve_range(
            db,
            "property_month_summary",
            f"account_id = :account_id AND {_tag_where()}",
            {"account_id": account_id, "tags": tags},
            date_from,
            date_to,
        )
    else:
        date_from, date_to = _resolve_range(
            db,
            "portfolio_month_summary",
            "account_id = :account_id",
            {"account_id": account_id},
            date_from,
            date_to,
        )

    props = _rows(
        db.execute(
            text(
                "SELECT id::text AS id, name, type FROM properties "
                f"WHERE account_id = :account_id AND {_tag_where('id')} ORDER BY name"
            ),
            {"account_id": account_id, "tags": tags},
        )
    )

    stats_shell = {name: {"mean": None, "median": None, "count": 0, "higher_is_better": hib} for name, hib in _BENCHMARK_METRICS}
    if date_to is None or not props:
        return {
            "period_from": date_from, "period_to": date_to, "tags": tags,
            "property_count": len(props), "properties": [], **stats_shell,
        }

    # NOI/opex/physical-occupancy: one call per property to the exact helper property_dashboard
    # uses, so this always agrees with that endpoint for the same period.
    per_property: dict[str, dict] = {}
    for p in props:
        pm = _period_metrics(
            db,
            "property_month_summary",
            date_from,
            date_to,
            "account_id = :account_id AND property_id = :id",
            {"account_id": account_id, "id": p["id"]},
        )
        per_property[p["id"]] = {"property_name": p["name"], "type": p["type"], "period": pm}

    # Cap rate / cash-on-cash: the existing per-property investment-metrics function (T12,
    # acquisition-based; None when a property has no investment inputs on file yet).
    for p in props:
        per_property[p["id"]]["invest"] = investment_metrics(db, account_id, p["id"])

    # Economic occupancy: one shared rent-roll query for every matching property, grouped in
    # Python and fed through the same `_occupancy_summary` the rent-roll endpoints use.
    today = date.today()
    roll_rows = [
        _rent_roll_row(r, today)
        for r in _rows(
            db.execute(
                text(_rent_roll_sql(f"p.account_id = :account_id AND {_tag_where('p.id')}")),
                {"account_id": account_id, "tags": tags, "as_of_month": current_month()},
            )
        )
    ]
    by_prop_rows: dict[str, list[dict]] = {}
    for r in roll_rows:
        by_prop_rows.setdefault(r["property_id"], []).append(r)
    for pid, rows in by_prop_rows.items():
        if pid in per_property:
            per_property[pid]["economic_occupancy"] = _occupancy_summary(rows)["economic_occupancy"]

    # Raw per-metric values, keyed by property id.
    raw: dict[str, dict[str, float | None]] = {name: {} for name, _ in _BENCHMARK_METRICS}
    for pid, d in per_property.items():
        pm = d["period"]
        gross_rent = pm["gross_rent"] if pm else None
        opex = pm["operating_expenses"] if pm else None
        noi = pm["noi"] if pm else None
        total_units = pm["total_units"] if pm else None
        inv = d["invest"]
        raw["noi_per_unit"][pid] = (noi / total_units) if (pm and total_units) else None
        raw["opex_ratio"][pid] = (opex / gross_rent) if (pm and gross_rent) else None
        raw["physical_occupancy"][pid] = pm["occupancy"] if pm else None
        raw["economic_occupancy"][pid] = d.get("economic_occupancy")
        raw["cap_rate"][pid] = inv["cap_rate"] if inv else None
        raw["cash_on_cash"][pid] = inv["cash_on_cash"] if inv else None

    stats: dict[str, dict] = {}
    ranks: dict[str, dict[str, tuple[int, float]]] = {}
    for name, hib in _BENCHMARK_METRICS:
        values = [v for v in raw[name].values() if v is not None]
        mean, median = _mean_median(values)
        stats[name] = {"mean": mean, "median": median, "count": len(values), "higher_is_better": hib}
        ranks[name] = _rank_and_percentile(raw[name], hib)

    properties = []
    for p in props:
        pid = p["id"]
        row = {"property_id": pid, "property_name": p["name"], "type": p["type"]}
        for name, _hib in _BENCHMARK_METRICS:
            v = raw[name].get(pid)
            mean = stats[name]["mean"]
            rank_pct = ranks[name].get(pid)
            row[name] = {
                "value": v,
                "delta_vs_mean": (v - mean) if (v is not None and mean is not None) else None,
                "rank": rank_pct[0] if rank_pct else None,
                "percentile": rank_pct[1] if rank_pct else None,
            }
        properties.append(row)

    return {
        "period_from": date_from, "period_to": date_to, "tags": tags,
        "property_count": len(props), "properties": properties, **stats,
    }


# ---- Budget / variance (flat annual plan, pro-rated to actual-vs-plan) ---------------------
#
# ``property_budget`` holds one flat annual figure per (property, year); nothing here is
# stored beyond that raw input. The monthly plan (annual / 12) and every variance number are
# computed on read against ``property_month_summary`` / ``portfolio_month_summary``, so budget
# entry never has to be kept in sync with actuals math and reclassifications flow through
# automatically, same as every other computed metric in this module.


def _year_overlap_months(date_from: date, date_to: date, year: int) -> int:
    """How many months of ``[date_from, date_to]`` fall inside calendar ``year``."""
    start = max(date_from, date(year, 1, 1))
    end = min(date_to, date(year, 12, 1))
    if start > end:
        return 0
    return _months_inclusive(start, end)


def _prorated_plan(
    db: Session, account_id: str, property_id: str, date_from: date, date_to: date
) -> dict:
    """Sum the flat annual budget for every year ``[date_from, date_to]`` touches, pro-rated
    to the months of that year inside the range (annual / 12 * months-in-range-for-that-year).
    A year with no ``property_budget`` row contributes nothing to the plan OR to
    ``coverage_months`` — callers use ``coverage_months`` (vs. the period's total months) to
    tell a fully-planned period from a partially- or un-planned one, instead of silently
    treating "no budget" as a budget of $0."""
    plan_rent = plan_opex = 0.0
    coverage = 0
    for year in range(date_from.year, date_to.year + 1):
        months = _year_overlap_months(date_from, date_to, year)
        if months == 0:
            continue
        row = db.execute(
            text(
                "SELECT b.budgeted_gross_rent, b.budgeted_operating_expenses "
                "FROM property_budget b JOIN properties p ON p.id = b.property_id "
                "WHERE b.property_id = :pid AND b.year = :yr AND p.account_id = :account_id"
            ),
            {"pid": property_id, "yr": year, "account_id": account_id},
        ).mappings().first()
        if row is None:
            continue
        plan_rent += float(row["budgeted_gross_rent"]) / 12 * months
        plan_opex += float(row["budgeted_operating_expenses"]) / 12 * months
        coverage += months
    return {
        "plan_gross_rent": plan_rent,
        "plan_operating_expenses": plan_opex,
        "coverage_months": coverage,
    }


_EMPTY_VARIANCE = {
    "total_months": 0,
    "plan_coverage_months": 0,
    "actual_gross_rent": 0.0,
    "actual_operating_expenses": 0.0,
    "actual_noi": 0.0,
    "plan_gross_rent": None,
    "plan_operating_expenses": None,
    "plan_noi": None,
    "variance_gross_rent": None,
    "variance_operating_expenses": None,
    "variance_noi": None,
    "variance_noi_pct": None,
}


def _variance_block(actual_rent: float, actual_opex: float, actual_noi: float, plan: dict, total_months: int) -> dict:
    coverage = plan["coverage_months"]
    out = {
        **_EMPTY_VARIANCE,
        "total_months": total_months,
        "plan_coverage_months": coverage,
        "actual_gross_rent": actual_rent,
        "actual_operating_expenses": actual_opex,
        "actual_noi": actual_noi,
    }
    if coverage == 0:
        return out
    plan_rent = plan["plan_gross_rent"]
    plan_opex = plan["plan_operating_expenses"]
    plan_noi = plan_rent - plan_opex
    var_noi = actual_noi - plan_noi
    out.update(
        plan_gross_rent=plan_rent,
        plan_operating_expenses=plan_opex,
        plan_noi=plan_noi,
        variance_gross_rent=actual_rent - plan_rent,
        variance_operating_expenses=actual_opex - plan_opex,
        variance_noi=var_noi,
        variance_noi_pct=(var_noi / abs(plan_noi) * 100) if plan_noi != 0 else None,
    )
    return out


def property_variance(
    db: Session,
    account_id: str,
    property_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict | None:
    """Actual vs. pro-rated plan for one property over a period. Returns None if the property
    does not exist in this account; an empty-but-shaped payload if it exists but has no
    summarized data and no explicit range was given (nothing to compare)."""
    prop = db.execute(
        text(
            "SELECT id::text AS id, name FROM properties "
            "WHERE id = :id AND account_id = :account_id"
        ),
        {"id": property_id, "account_id": account_id},
    ).mappings().first()
    if prop is None:
        return None

    date_from, date_to = _resolve_range(
        db,
        "property_month_summary",
        "account_id = :account_id AND property_id = :id",
        {"account_id": account_id, "id": property_id},
        date_from,
        date_to,
    )
    if date_to is None:
        return {
            "property_id": prop["id"],
            "property_name": prop["name"],
            "period_from": None,
            "period_to": None,
            **_EMPTY_VARIANCE,
        }

    actual = _period_metrics(
        db,
        "property_month_summary",
        date_from,
        date_to,
        "account_id = :account_id AND property_id = :id",
        {"account_id": account_id, "id": property_id},
    )
    actual_rent = actual["gross_rent"] if actual else 0.0
    actual_opex = actual["operating_expenses"] if actual else 0.0
    actual_noi = actual["noi"] if actual else 0.0

    plan = _prorated_plan(db, account_id, property_id, date_from, date_to)
    total_months = _months_inclusive(date_from, date_to)
    return {
        "property_id": prop["id"],
        "property_name": prop["name"],
        "period_from": date_from,
        "period_to": date_to,
        **_variance_block(actual_rent, actual_opex, actual_noi, plan, total_months),
    }


def portfolio_variance(
    db: Session, account_id: str, date_from: date | None = None, date_to: date | None = None
) -> dict:
    """Actual vs. pro-rated plan across the portfolio, scoped to the SAME set of properties on
    BOTH sides of the comparison: only properties with budget coverage for the period
    contribute to the plan, AND actual NOI is summed over that identical property set (from
    ``property_month_summary``, filtered to those ids) rather than the whole portfolio (from
    ``portfolio_month_summary``). Mixing all-portfolio actuals against a single budgeted
    property's plan produced a meaningless variance % (actual >> plan because the plan side
    covered a fraction of the properties the actual side did). ``budgeted_property_count`` /
    ``total_property_count`` report the coverage so a partially-budgeted portfolio is still
    visibly flagged, same "be honest about partial coverage" rule as the per-property version.
    """
    date_from, date_to = _resolve_range(
        db,
        "portfolio_month_summary",
        "account_id = :account_id",
        {"account_id": account_id},
        date_from,
        date_to,
    )
    property_ids = db.scalars(
        text("SELECT id::text FROM properties WHERE account_id = :account_id"),
        {"account_id": account_id},
    ).all()
    if date_to is None:
        return {
            "period_from": None, "period_to": None, **_EMPTY_VARIANCE,
            "budgeted_property_count": 0, "total_property_count": len(property_ids),
        }

    total_months = _months_inclusive(date_from, date_to)
    plan_rent = plan_opex = 0.0
    coverage_months = 0
    budgeted_ids: list[str] = []
    for pid in property_ids:
        p = _prorated_plan(db, account_id, pid, date_from, date_to)
        if p["coverage_months"] > 0:
            plan_rent += p["plan_gross_rent"]
            plan_opex += p["plan_operating_expenses"]
            budgeted_ids.append(pid)
            coverage_months = total_months  # at least one property has full/partial coverage

    if budgeted_ids:
        # Same property set as the plan side: property_month_summary filtered to the budgeted
        # ids, summed via the multi_scope aggregation path (several properties can share a
        # month) rather than the whole-portfolio portfolio_month_summary rollup.
        actual = _period_metrics(
            db, "property_month_summary", date_from, date_to,
            "account_id = :account_id AND property_id::text = ANY(:pids)",
            {"account_id": account_id, "pids": budgeted_ids}, multi_scope=True,
        )
    else:
        actual = None
    actual_rent = actual["gross_rent"] if actual else 0.0
    actual_opex = actual["operating_expenses"] if actual else 0.0
    actual_noi = actual["noi"] if actual else 0.0

    plan = {"plan_gross_rent": plan_rent, "plan_operating_expenses": plan_opex, "coverage_months": coverage_months}
    return {
        "period_from": date_from,
        "period_to": date_to,
        **_variance_block(actual_rent, actual_opex, actual_noi, plan, total_months),
        "budgeted_property_count": len(budgeted_ids),
        "total_property_count": len(property_ids),
    }


# ---- Rent roll / lease-level data (migration 0012) ------------------------------------
#
# ``lease`` is a NEW parallel table (a unit's full tenancy history); ``contract_rent`` is
# reference data and never feeds NOI/cash-flow, which stays driven solely by
# monthly_records/line_items. A unit's CURRENT lease is resolved here as the lease that
# covers today, or — absent one — the most recently started lease on file (so a unit
# between tenants still shows its last-known tenant/status rather than nothing).
#
# ``lease.status`` is treated as AUTHORITATIVE for "is this unit occupied" on the rent
# roll (the analyst report's "distinguish vacant from missing data" ask): a unit with no
# lease row at all is reported vacant; any status other than 'vacant' (active/notice/
# expired — a holdover tenant is still physically in the unit) counts as occupied.
# ``missing_data`` separately flags an occupied unit with no monthly_record for the
# property's latest summarized month — a data gap, not a real vacancy.
_CURRENT_LEASE_CTE = """
    current_lease AS (
        SELECT DISTINCT ON (l.unit_id)
            l.id, l.unit_id, l.tenant_name, l.start_date, l.end_date,
            l.contract_rent, l.status,
            l.security_deposit, l.escalation_pct, l.escalation_frequency_months,
            l.lease_type, l.pct_rent_rate, l.pct_rent_breakpoint, l.concession_monthly,
            l.last_rent_increase_date, l.rent_before_increase, l.opening_arrears,
            l.arrears_from_month
        FROM lease l
        ORDER BY l.unit_id,
            (l.status IN ('active', 'notice')
                AND l.start_date <= CURRENT_DATE
                AND (l.end_date IS NULL OR l.end_date >= CURRENT_DATE)) DESC,
            l.start_date DESC
    )
"""


def _months_to_expiry(end_date: date | None, today: date) -> int | None:
    """Whole calendar months from ``today`` to ``end_date`` (negative if already past).
    ``None`` for a month-to-month lease (no end_date ⇒ no rollover horizon applies)."""
    if end_date is None:
        return None
    months = (end_date.year - today.year) * 12 + (end_date.month - today.month)
    if end_date.day < today.day:
        months -= 1
    return months


def _months_elapsed(since: date | None, today: date) -> int | None:
    """Whole calendar months from ``since`` to ``today`` — the mirror of
    :func:`_months_to_expiry`, looking backwards. Used for "months since the rent last went
    up" on a periodic tenancy (migration 0024). None when there's no date to count from."""
    if since is None:
        return None
    months = (today.year - since.year) * 12 + (today.month - since.month)
    if today.day < since.day:
        months -= 1
    return max(months, 0)


def _add_months_preserve_day(d: date, months: int) -> date:
    """Shift ``d`` forward by ``months``, preserving its day-of-month (clamped to the
    target month's length) — unlike :func:`_shift_month`, which resets to the 1st (fine
    for month-grain P&L keys, wrong for an anniversary-style escalation date)."""
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _next_escalation_date(
    start_date: date | None,
    frequency_months: int | None,
    escalation_pct,
    today: date,
    end_date: date | None,
) -> date | None:
    """First scheduled-bump date on/after ``today``: ``start_date``'s anniversary shifted
    forward by whole multiples of ``frequency_months`` (default 12) until it reaches
    today. Null when there's no escalation on file at all (``escalation_pct`` unset) or no
    start_date to anchor on.

    Fix #3 (analyst report): also null when the computed bump date does not fall STRICTLY
    BEFORE ``end_date`` — e.g. a 12-month fixed lease with an annual escalation has its one
    scheduled bump land exactly ON the lease's own end date, which is illusory: the lease
    terminates before any bump ever takes effect. A month-to-month lease (``end_date`` is
    None) has no term boundary to check against, so its next bump is never suppressed by
    this rule."""
    if escalation_pct is None or start_date is None:
        return None
    freq = frequency_months or 12
    candidate = start_date
    while candidate < today:
        candidate = _add_months_preserve_day(candidate, freq)
    if end_date is not None and candidate >= end_date:
        return None
    return candidate


# ---- Expected-vs-actual rent variance (analyst report: "connect lease economics to the
# P&L — at minimum expected/escalated rent feeding an expected-vs-actual rent variance").
#
# `expected_rent` is REFERENCE data, exactly like `contract_rent` already is — it is a
# summary of the lease's own terms over a period, computed on read here and NEVER fed into
# NOI/cash-flow (those stay driven solely by monthly_records/line_items, unchanged by any
# of this). "Actual" is the SAME source the rent roll already uses for a unit's rent
# (`unit_month_summary.gross_rent`), just summed over the requested period instead of read
# for a single latest month.
def _month_index(d: date) -> int:
    return d.year * 12 + d.month


def _months_in_range(date_from: date, date_to: date) -> list[date]:
    """Every first-of-month date from `date_from` to `date_to` inclusive (both floored to
    the 1st, defensively — callers are expected to pass YYYY-MM-01 per the app's existing
    period convention, same as the budget/dashboard `from`/`to` params)."""
    a = date(date_from.year, date_from.month, 1)
    b = date(date_to.year, date_to.month, 1)
    n = _months_inclusive(a, b)
    return [_shift_month(a, i) for i in range(n)] if n > 0 else []


def _escalated_rent(contract_rent, escalation_pct, escalation_frequency_months, start_date: date, as_of_month: date) -> float:
    """Escalated rent for one lease, as of `as_of_month` (a first-of-month date): starting
    from `contract_rent` at `start_date`, bump by `escalation_pct` every
    `escalation_frequency_months` FULL cadence periods elapsed since `start_date`.

    E.g. a $1,000/mo lease at 3%/yr (frequency 12) starting Jan 2025: elapsed=0..11 months
    (Jan-Dec 2025) -> 0 bumps -> $1,000; elapsed=12..23 (Jan-Dec 2026) -> 1 bump -> $1,030.

    `escalation_pct` unset (NULL) means flat rent forever — this is also how a
    month-to-month lease is priced (MTM leases are seeded with no escalation_pct at all),
    satisfying "MTM uses current contract rent" directly: there is only ever the one
    `contract_rent` figure, which IS the current rent.

    Passing the lease's own `end_date` as `as_of_month` (instead of the month actually
    being priced) freezes the calculation at whatever the rent was when the lease's term
    ended — used for holdover months (see `_unit_expected_actual`), so a lapsed lease's
    expected rent doesn't keep silently escalating after it's off-contract.
    """
    base = float(contract_rent)
    if escalation_pct is None:
        return base
    freq = escalation_frequency_months or 12
    elapsed = _month_index(as_of_month) - _month_index(start_date)
    if elapsed < 0:
        elapsed = 0
    n_bumps = elapsed // freq
    if n_bumps <= 0:
        return base
    return base * ((1 + float(escalation_pct) / 100) ** n_bumps)


def _rent_due_for_month(lease: dict, as_of_month: date, *, freeze_at: date | None = None) -> float:
    """THE rent schedule: what `lease` says is owed for `as_of_month` (a first-of-month
    date). Every rent-vs-actual surface in this app goes through here — the expected-vs-
    actual variance, the arrears ledger — so there is exactly one answer to "what was due".

    Three cases, in order:

    1. **No increase on file** (`last_rent_increase_date` IS NULL — every pre-migration-0024
       lease): unchanged behaviour, `_escalated_rent` anchored on `start_date`.
    2. **A month BEFORE the last increase**: `rent_before_increase` if we hold it, else
       `contract_rent`. Priced FLAT — no escalation is back-applied. A periodic tenancy's
       rent rises in discrete steps (section 13 / by agreement), not on a compounding rate,
       and inventing a rate behind a step is exactly the phantom-shortfall mistake the
       lease-coherence fix in app/seed.py removed once already. Falling back to
       `contract_rent` when the prior figure is unknown is deliberately the pre-0024
       behaviour: it never makes an existing lease's history worse than it already was.
    3. **A month ON OR AFTER the last increase**: `contract_rent`, escalated from
       `last_rent_increase_date` rather than `start_date`. The increase date re-anchors the
       escalation clock, which is both the right reading of "the rent last actually moved
       then" and (for England) the right reading of the 12-month rule on further increases.

    `freeze_at` prices the lease as of a DIFFERENT month than the one being reported — used
    for holdover months, where the schedule freezes at the lease's own `end_date` so a
    lapsed contract doesn't keep escalating after it's off-term (see `_unit_expected_actual`).
    """
    price_month = freeze_at if freeze_at is not None else as_of_month
    increase = lease.get("last_rent_increase_date")
    if increase is not None and _month_index(price_month) < _month_index(increase):
        prior = lease.get("rent_before_increase")
        return float(prior if prior is not None else lease["contract_rent"])
    anchor = increase if increase is not None else lease["start_date"]
    return _escalated_rent(
        lease["contract_rent"], lease["escalation_pct"],
        lease["escalation_frequency_months"], anchor, price_month,
    )


def _governing_lease_for_month(
    ordered_leases: list[dict], month: date, actual: float | None
) -> tuple[dict | None, bool]:
    """Which of a unit's leases prices `month`, and whether that's a HOLDOVER.

    Extracted so the expected-vs-actual variance and the arrears ledger can't disagree about
    whose tenancy a month belongs to — they'd report contradictory shortfalls for the same
    month if they did. `ordered_leases` must be sorted by `start_date` DESCENDING.

    The lease in force is the one whose [start_date, end_date] covers the month; ties
    (overlapping leases, not expected in practice) break toward the most-recently-started
    one, mirroring `_CURRENT_LEASE_CTE`'s own tie-break. Absent one, a HOLDOVER is the unit's
    most recent lapsed lease when rent is still being collected that month (`actual > 0`) —
    returned with True, and priced frozen at its own `end_date` by the caller. Otherwise
    `(None, False)`: a month with no lease basis at all (e.g. before the first lease on file)
    is not part of any tenancy and is reported by neither surface.
    """
    m_idx = _month_index(month)
    governing = next(
        (
            l for l in ordered_leases
            if _month_index(l["start_date"]) <= m_idx
            and (l["end_date"] is None or _month_index(l["end_date"]) >= m_idx)
        ),
        None,
    )
    if governing is not None:
        return governing, False
    prev = next((l for l in ordered_leases if _month_index(l["start_date"]) <= m_idx), None)
    if prev is not None and prev["end_date"] is not None and actual is not None and actual > 0:
        return prev, True
    return None, False


def _unit_expected_actual(leases: list[dict], actual_by_month: dict[date, float], months: list[date]) -> tuple[float, float]:
    """Sum of expected + actual rent for ONE unit across `months`, given its FULL lease
    history (any order) and its actual gross rent by month (only months on file).

    Expected and actual are summed over the SAME month set: the intersection of {months a
    lease basis is in force} and {months that have an actual record on file}. In other
    words, only months where the unit was on a lease (or in holdover) AND we have a
    statement count toward BOTH totals — so the two legs of the variance are always
    like-for-like. This is deliberate: summing actual over every month in the requested
    period while expected only covers lease months produced garbage at every mid-period
    lease boundary (a unit whose lease started mid-window showed a full period of actuals
    against a few months of expected). Months with a lease but no record (missing data),
    or a record but no lease basis (e.g. before the first lease on file), are dropped from
    both sides.

    Per-month governing lease: the lease (if any) whose [start_date, end_date] covers that
    month — ties (overlapping leases, not expected in practice) broken toward the
    most-recently-started one, mirroring `_CURRENT_LEASE_CTE`'s own tie-break. A month with
    NO lease in force contributes nothing UNLESS it's a HOLDOVER: the unit's most recent
    past lease has lapsed (`end_date` in the past) but actual rent is still being collected
    that month — then expected freezes at that lease's rent as of its own end_date (no
    further escalation accrues once the contract is off-term). This mirrors the point-in-time
    `holdover` flag in `_rent_roll_row`, applied per-month instead of just to "today".
    """
    ordered = sorted(leases, key=lambda l: _month_index(l["start_date"]), reverse=True)
    total_expected = 0.0
    total_actual = 0.0
    for m in months:
        # Only months with an actual record on file are eligible (the actuals leg of the
        # intersection); a missing month contributes to neither total.
        if m not in actual_by_month:
            continue
        actual = actual_by_month[m]
        governing, holdover = _governing_lease_for_month(ordered, m, actual)
        if governing is None:
            # Record exists but no lease basis in force (e.g. pre-first-lease months) — not
            # part of the lease∩actuals intersection, so skip both legs.
            continue
        # Holdover freezes the schedule at the lapsed lease's own end_date (migration 0024's
        # rent-increase handling included — see `_rent_due_for_month`).
        expected = _rent_due_for_month(
            governing, m, freeze_at=governing["end_date"] if holdover else None
        )
        total_expected += expected
        total_actual += actual
    return total_expected, total_actual


def _resolve_rent_variance_period(
    db: Session, unit_scope_where: str, params: dict, date_from: date | None, date_to: date | None
) -> tuple[date | None, date | None]:
    """Default the rent-variance window the same way every other period-aware endpoint
    does (see `_resolve_range`): an unspecified `date_to` is the latest NON-FUTURE month with
    ANY actual rent data on file for this scope (property or tag-filtered portfolio), and an
    unspecified `date_from` defaults to that same month (a single-month window). Returns
    `(None, None)` when the scope has no unit-month data at all yet."""
    if date_to is None:
        date_to = db.execute(
            text(
                f"""
                SELECT max(ums.month) FROM unit_month_summary ums
                JOIN units u ON u.id = ums.unit_id
                JOIN properties p ON p.id = u.property_id
                WHERE {unit_scope_where} AND ums.month <= :as_of_month
                """
            ),
            {**params, "as_of_month": current_month()},
        ).scalar()
    if date_to is None:
        return None, None
    if date_from is None:
        date_from = date_to
    return date_from, date_to


def _rent_variance_for_units(
    db: Session, account_id: str, unit_ids: list[str], date_from: date, date_to: date
) -> dict[str, dict]:
    """Expected vs. actual rent per unit, summed over [date_from, date_to] — the numbers
    behind the rent-roll's `expected_rent`/`period_actual_rent`/`variance` fields. Every ID
    in `unit_ids` gets an entry (zeros when the unit has neither lease nor actual data in
    range) so callers can merge with a plain dict lookup."""
    months = _months_in_range(date_from, date_to)
    result: dict[str, dict] = {}
    if not unit_ids or not months:
        for unit_id in unit_ids:
            result[unit_id] = {
                "expected_rent": 0.0, "period_actual_rent": 0.0, "variance": 0.0, "variance_pct": None,
            }
        return result

    # `_LEASE_SCHEDULE_SQL` (see the arrears section) is shared with the arrears engine so
    # both price from literally the same columns.
    lease_rows = _rows(
        db.execute(
            text(_LEASE_SCHEDULE_SQL), {"unit_ids": unit_ids, "account_id": account_id}
        )
    )
    actual_rows = _rows(
        db.execute(
            text(
                "SELECT unit_id::text AS unit_id, month, gross_rent FROM unit_month_summary "
                "WHERE unit_id::text = ANY(:unit_ids) AND account_id = :account_id "
                "AND month BETWEEN :f AND :t"
            ),
            {
                "unit_ids": unit_ids,
                "account_id": account_id,
                "f": months[0],
                "t": months[-1],
            },
        )
    )

    leases_by_unit: dict[str, list[dict]] = {}
    for r in lease_rows:
        leases_by_unit.setdefault(r["unit_id"], []).append(r)
    actual_by_unit: dict[str, dict[date, float]] = {}
    for r in actual_rows:
        actual_by_unit.setdefault(r["unit_id"], {})[r["month"]] = float(r["gross_rent"])

    for unit_id in unit_ids:
        expected, actual = _unit_expected_actual(
            leases_by_unit.get(unit_id, []), actual_by_unit.get(unit_id, {}), months
        )
        variance = actual - expected
        result[unit_id] = {
            "expected_rent": expected,
            "period_actual_rent": actual,
            "variance": variance,
            # Already ×100 (a percent, e.g. -1.4 for -1.4%), matching the budget feature's
            # `variance_noi_pct` convention so the two variance surfaces agree. NULL when
            # there's no expected basis to divide by.
            "variance_pct": (variance / expected * 100) if expected else None,
        }
    return result


def _rent_variance_rollup(rows: list[dict]) -> dict:
    """Property/portfolio rollup: total expected vs. actual rent over the period, and the
    $ / % variance. Excludes shell/synthetic units (same exclusion, and for the same
    reason, as `_occupancy_summary` — e.g. Cedar Plaza Retail's shell unit has no actual
    rent of its own to compare against; it would otherwise show up as a phantom 100%
    shortfall)."""
    real = [r for r in rows if not r.get("is_shell") and r.get("expected_rent") is not None]
    total_expected = sum(r["expected_rent"] for r in real)
    total_actual = sum(r["period_actual_rent"] for r in real)
    variance = total_actual - total_expected
    return {
        "total_expected_rent": total_expected,
        "total_actual_rent": total_actual,
        "variance": variance,
        # Already ×100 (percent), matching `variance_noi_pct` / the per-unit field above.
        "variance_pct": (variance / total_expected * 100) if total_expected else None,
        "unit_count": len(real),
    }


# ---- Rent arrears (migration 0025: lease.opening_arrears + arrears_adjustment) ---------
#
# Arrears is the CUMULATIVE view of the same shortfall the rent variance above reports for a
# single period, and the balance a landlord actually chases:
#
#     movement(month) = rent_due(month) - rent_collected(month) + adjustments(month)
#     balance(month)  = opening_arrears + Σ movement over the TENANCY to date
#
# DERIVED, never stored (see migration 0025's docstring for the full reasoning). `rent_due`
# is the lease's own schedule (`_rent_due_for_month` — the same function the variance leg
# uses, so the two can never disagree); `rent_collected` is `unit_month_summary.gross_rent`,
# the same actuals source the rent roll already reads. Only two inputs are stored, because
# only two can't be derived: `lease.opening_arrears` (debt predating this app's records) and
# `arrears_adjustment` (write-offs and non-rent charges).
#
# Three rules that make the number honest, each of which is a decision the obvious
# implementation gets wrong:
#
#   1. PER TENANCY, not per unit. The running sum resets at each lease: an outgoing tenant's
#      unpaid rent is their debt, so carrying it onto the next tenant of the same door would
#      both saddle an innocent tenant and lose the real debtor.
#   2. ONLY MONTHS WITH A RECORD ON FILE (plus any month carrying an adjustment) accrue. A
#      month with rent due and NO monthly_record is MISSING DATA — we don't know what was
#      collected — and booking it as arrears would turn every un-entered month into fictional
#      debt and bury the real cases underneath. This is the same lease∩actuals intersection
#      `_unit_expected_actual` draws, reused via `_governing_lease_for_month`.
#   3. NOT FLOORED AT ZERO. A tenant who pays ahead is in CREDIT, and a negative balance says
#      so. Clamping at zero would silently discard a real prepayment and then double-count
#      the next month's shortfall against it.
#
# Reference data, same invariant as the lease fields it reads: never feeds NOI/cash-flow.

# Shared by the rent-variance and arrears engines so the schedule they price from can't
# drift apart (they'd otherwise report contradictory shortfalls for the same month).
_LEASE_SCHEDULE_SQL = """
    SELECT l.id::text AS lease_id, l.unit_id::text AS unit_id, l.tenant_name,
           l.start_date, l.end_date, l.contract_rent, l.status,
           l.escalation_pct, l.escalation_frequency_months, l.concession_monthly,
           l.last_rent_increase_date, l.rent_before_increase, l.opening_arrears,
           l.arrears_from_month
    FROM lease l
    JOIN units u ON u.id = l.unit_id
    JOIN properties p ON p.id = u.property_id
    WHERE l.unit_id::text = ANY(:unit_ids) AND p.account_id = :account_id
"""


def _empty_arrears() -> dict:
    """The all-zero arrears summary: a unit with no lease on file has no tenancy to owe
    anything, which is a $0 balance rather than an unknown one. `arrears_months_of_rent`
    stays None — "how many months' rent is owed" has no meaning with no rent schedule."""
    return {
        "arrears_balance": 0.0,
        "arrears_opening_balance": 0.0,
        "arrears_movement": 0.0,
        "arrears_rent_due": 0.0,
        "arrears_rent_collected": 0.0,
        "arrears_concessions": 0.0,
        "arrears_adjustments": 0.0,
        "arrears_months_of_rent": None,
    }


def _unit_arrears_ledgers(
    leases: list[dict],
    actual_by_month: dict[date, float],
    adjustments: dict[tuple[str, date], float],
    through_month: date,
) -> list[dict]:
    """One unit's arrears ledger, per tenancy, month by month up to `through_month`.

    `leases` is the unit's FULL lease history (any order), `actual_by_month` its
    `(gross_rent, is_vacant)` by month (only months on file), `adjustments` the signed
    arrears_adjustment totals keyed by (lease_id, month). Returns one ledger per lease, oldest
    tenancy first, each with its own running balance starting from that lease's
    `opening_arrears`.

    Two things are deliberately NOT arrears, because arrears is unpaid rent — not every gap
    between the schedule and the cash:

      * A **standing CONCESSION** (`lease.concession_monthly`, migration 0016) is rent the
        landlord agreed not to charge. What's owed is `contract_rent - concession`, so the
        concession is netted off `rent_due` here (floored at zero) and reported alongside it.
        Without this every concession-bearing tenant would appear to be in arrears by exactly
        the discount they were granted, every month, forever. The rent waterfall already draws
        this same line between `concessions` (a leasing decision) and `bad_debt` (a collections
        problem) — arrears is the cumulative form of the latter, so it has to draw it too.
      * A month before the tenancy's **`arrears_from_month`** (migration 0027) is outside the
        measurement — the handover month after a purchase, where rent apportioned at completion
        is indistinguishable from a tenant who underpaid. NULL (the default) tracks from the
        beginning.
      * An **explicitly VACANT month** (`unit_month_summary.is_vacant`) has no tenant in place
        to owe anything; that gap is vacancy loss, which the waterfall reports separately.
        Note this tests the FLAG ONLY and never infers vacancy from £0 collected — a tenant
        who paid nothing at all is the single most important arrears case there is, and
        migration 0011 added that flag precisely so "empty" and "nothing came in" could stop
        being conflated.

    A lease with no eligible months still gets a ledger: a tenancy that began carrying debt
    still owes it before its first month is entered, so the balance is its opening figure
    rather than nothing. An adjustment lands on its own lease's month regardless of whether
    a rent record exists there — a write-off is a fact of the tenancy, not an observation
    about a month's collections.
    """
    ordered = sorted(leases, key=lambda l: _month_index(l["start_date"]), reverse=True)
    through_idx = _month_index(through_month)
    entries: dict[str, dict[date, dict]] = {}

    def _entry(lease_id: str, month: date) -> dict:
        by_month = entries.setdefault(lease_id, {})
        e = by_month.get(month)
        if e is None:
            e = {
                "month": month, "rent_due": 0.0, "rent_collected": 0.0, "concession": 0.0,
                "adjustments": 0.0, "has_record": False, "holdover": False,
            }
            by_month[month] = e
        return e

    for month, (actual, is_vacant) in actual_by_month.items():
        if _month_index(month) > through_idx or is_vacant:
            # Explicitly vacant: no tenant to owe rent. See the docstring — the flag only,
            # never a £0-collected inference.
            continue
        governing, holdover = _governing_lease_for_month(ordered, month, actual)
        if governing is None:
            # No tenancy in force and no holdover: this month belongs to no one's balance.
            continue
        # Migration 0027: months before the tenancy's own arrears start are outside the
        # measurement entirely — a handover month's apportioned rent is not a tenant's debt.
        start = governing.get("arrears_from_month")
        if start is not None and _month_index(month) < _month_index(start):
            continue
        scheduled = _rent_due_for_month(
            governing, month, freeze_at=governing["end_date"] if holdover else None
        )
        # Rent the landlord agreed not to charge isn't rent the tenant failed to pay.
        concession = (
            float(governing["concession_monthly"])
            if governing["concession_monthly"] is not None else 0.0
        )
        concession = min(concession, scheduled)
        e = _entry(governing["lease_id"], month)
        e["rent_due"] += scheduled - concession
        e["concession"] += concession
        e["rent_collected"] += actual
        e["has_record"] = True
        e["holdover"] = holdover

    lease_by_id = {l["lease_id"]: l for l in leases}
    for (lease_id, month), amount in adjustments.items():
        if _month_index(month) > through_idx:
            continue
        # An adjustment dated into an excluded month would quietly put that month back into the
        # ledger. A write-off genuinely belonging to the handover period should be dated to the
        # first tracked month instead, where it is visible.
        start = lease_by_id.get(lease_id, {}).get("arrears_from_month")
        if start is not None and _month_index(month) < _month_index(start):
            continue
        _entry(lease_id, month)["adjustments"] += amount

    ledgers = []
    for lease in sorted(leases, key=lambda l: _month_index(l["start_date"])):
        opening = float(lease["opening_arrears"]) if lease["opening_arrears"] is not None else 0.0
        balance = opening
        months: list[dict] = []
        for month in sorted(entries.get(lease["lease_id"], {})):
            e = entries[lease["lease_id"]][month]
            movement = e["rent_due"] - e["rent_collected"] + e["adjustments"]
            balance += movement
            months.append({**e, "movement": movement, "balance": balance})
        ledgers.append({
            "lease_id": lease["lease_id"],
            "tenant_name": lease["tenant_name"],
            "lease_start": lease["start_date"],
            "lease_end": lease["end_date"],
            "status": lease["status"],
            "opening_arrears": opening,
            "arrears_from_month": lease.get("arrears_from_month"),
            "months": months,
            "total_rent_due": sum(m["rent_due"] for m in months),
            "total_rent_collected": sum(m["rent_collected"] for m in months),
            "total_concessions": sum(m["concession"] for m in months),
            "total_adjustments": sum(m["adjustments"] for m in months),
            "closing_balance": balance,
        })
    return ledgers


def _arrears_window_summary(ledger: dict, date_from: date, date_to: date) -> dict:
    """Collapse one tenancy's ledger into the rent roll's figures: the CUMULATIVE balance at
    `date_to`, plus the window's own movement and the rent due/collected/adjusted behind it.

    `arrears_opening_balance + arrears_movement == arrears_balance` always holds, so the
    monthly figure and the accumulated one reconcile on the row itself rather than the reader
    having to take both on trust.

    `arrears_months_of_rent` expresses the balance in months of the CURRENT rent (the last
    priced month at or before `date_to`) — the figure a letting agent actually quotes, and
    the one that makes £900 owed comparable between a £450 and an £1,800 door. None when
    there is no priced month to divide by.
    """
    from_idx, to_idx = _month_index(date_from), _month_index(date_to)
    window = [m for m in ledger["months"] if from_idx <= _month_index(m["month"]) <= to_idx]
    balance = ledger["closing_balance"]
    movement = sum(m["movement"] for m in window)
    priced = [
        m for m in ledger["months"]
        if _month_index(m["month"]) <= to_idx and m["rent_due"] > 0
    ]
    monthly_rent = priced[-1]["rent_due"] if priced else None
    return {
        "arrears_balance": balance,
        # What the tenancy was already carrying when the window opened.
        "arrears_opening_balance": balance - movement,
        "arrears_movement": movement,
        # NET of any standing concession (see `_unit_arrears_ledgers`) — what was actually
        # owed, not what the lease's headline rent says.
        "arrears_rent_due": sum(m["rent_due"] for m in window),
        "arrears_rent_collected": sum(m["rent_collected"] for m in window),
        "arrears_concessions": sum(m["concession"] for m in window),
        "arrears_adjustments": sum(m["adjustments"] for m in window),
        "arrears_months_of_rent": (balance / monthly_rent) if monthly_rent else None,
    }


def _arrears_inputs(
    db: Session, account_id: str, unit_ids: list[str], through_month: date
) -> tuple[
    dict[str, list[dict]], dict[str, dict[date, float]], dict[str, dict[tuple[str, date], float]]
]:
    """Fetch everything the ledger needs for a set of units, in three queries: lease history,
    collected rent by month, and arrears adjustments.

    Deliberately NOT limited to the reporting window on the actuals/adjustments legs — a
    cumulative balance is only correct if it has seen the tenancy's whole history up to
    `through_month`. Only the future is excluded.
    """
    leases_by_unit: dict[str, list[dict]] = {}
    for r in _rows(
        db.execute(text(_LEASE_SCHEDULE_SQL), {"unit_ids": unit_ids, "account_id": account_id})
    ):
        leases_by_unit.setdefault(r["unit_id"], []).append(r)

    actual_by_unit: dict[str, dict[date, float]] = {}
    for r in _rows(
        db.execute(
            text(
                "SELECT unit_id::text AS unit_id, month, gross_rent, is_vacant "
                "FROM unit_month_summary "
                "WHERE unit_id::text = ANY(:unit_ids) AND account_id = :account_id "
                "AND month <= :through"
            ),
            {"unit_ids": unit_ids, "account_id": account_id, "through": through_month},
        )
    ):
        actual_by_unit.setdefault(r["unit_id"], {})[r["month"]] = (
            float(r["gross_rent"]), bool(r["is_vacant"])
        )

    adj_by_unit: dict[str, dict[tuple[str, date], float]] = {}
    for r in _rows(
        db.execute(
            text(
                """
                SELECT l.unit_id::text AS unit_id, a.lease_id::text AS lease_id,
                       a.month, a.amount
                FROM arrears_adjustment a
                JOIN lease l ON l.id = a.lease_id
                JOIN units u ON u.id = l.unit_id
                JOIN properties p ON p.id = u.property_id
                WHERE l.unit_id::text = ANY(:unit_ids) AND p.account_id = :account_id
                  AND a.month <= :through
                """
            ),
            {"unit_ids": unit_ids, "account_id": account_id, "through": through_month},
        )
    ):
        key = (r["lease_id"], r["month"])
        by_key = adj_by_unit.setdefault(r["unit_id"], {})
        by_key[key] = by_key.get(key, 0.0) + float(r["amount"])

    return leases_by_unit, actual_by_unit, adj_by_unit


def _arrears_for_units(
    db: Session, account_id: str, unit_ids: list[str], date_from: date, date_to: date
) -> dict[str, dict[str, dict]]:
    """Arrears summary per (unit, lease) over [date_from, date_to], for merging into rent-roll
    rows. Keyed unit -> lease so the caller can pick the tenancy its row is actually showing
    (the CURRENT lease, per `_CURRENT_LEASE_CTE`) rather than this function re-deriving that
    choice and risking disagreement with it."""
    if not unit_ids:
        return {}
    leases_by_unit, actual_by_unit, adj_by_unit = _arrears_inputs(
        db, account_id, unit_ids, date_to
    )
    out: dict[str, dict[str, dict]] = {}
    for unit_id, leases in leases_by_unit.items():
        ledgers = _unit_arrears_ledgers(
            leases, actual_by_unit.get(unit_id, {}), adj_by_unit.get(unit_id, {}), date_to
        )
        out[unit_id] = {
            ledger["lease_id"]: _arrears_window_summary(ledger, date_from, date_to)
            for ledger in ledgers
        }
    return out


def _arrears_rollup(rows: list[dict]) -> dict:
    """Property/portfolio arrears rollup. Excludes shell/synthetic units for the same reason
    `_rent_variance_rollup` does (their rent books at the property tier, so they have no
    tenancy of their own to owe anything).

    `units_in_arrears` counts only units actually OWING at `period_to`; units in credit are
    kept out of that count but DO net against `total_balance`. The portfolio's real exposure
    is the net position, while "how many doors do I have to chase" is a headcount of debtors —
    reporting one number for both questions is what makes an arrears report useless.
    """
    real = [r for r in rows if not r.get("is_shell") and r.get("arrears_balance") is not None]
    balances = [r["arrears_balance"] for r in real]
    return {
        "total_balance": sum(balances),
        "total_movement": sum(r["arrears_movement"] for r in real),
        "total_rent_due": sum(r["arrears_rent_due"] for r in real),
        "total_rent_collected": sum(r["arrears_rent_collected"] for r in real),
        "total_concessions": sum(r["arrears_concessions"] for r in real),
        "total_adjustments": sum(r["arrears_adjustments"] for r in real),
        "units_in_arrears": sum(1 for b in balances if b > 0),
        "units_in_credit": sum(1 for b in balances if b < 0),
        "unit_count": len(real),
        "largest_balance": max(balances) if balances else None,
    }


def unit_arrears(db: Session, account_id: str, unit_id: str) -> dict:
    """The full month-by-month arrears ledger for one unit, one block per tenancy — the
    "monthly basis" view behind the rent roll's accumulated balance.

    Every tenancy the unit has ever had is returned (oldest first), each with its own running
    balance, because a prior tenant's unpaid rent doesn't disappear just because they left —
    it stops being collectable from the CURRENT tenant, which is a different statement.
    `current_balance` is the balance of the lease the rent roll calls current, so the two
    surfaces always agree.
    """
    through = current_month()
    leases_by_unit, actual_by_unit, adj_by_unit = _arrears_inputs(
        db, account_id, [unit_id], through
    )
    leases = leases_by_unit.get(unit_id, [])
    ledgers = _unit_arrears_ledgers(
        leases, actual_by_unit.get(unit_id, {}), adj_by_unit.get(unit_id, {}), through
    )
    # Same current-lease rule as `_CURRENT_LEASE_CTE`: a lease covering today wins, otherwise
    # the most recently started one. Re-stated here rather than re-queried so a unit with no
    # summarized months still reports its current tenancy's opening balance.
    today = date.today()
    by_recency = sorted(leases, key=lambda l: _month_index(l["start_date"]), reverse=True)
    current = next(
        (
            l for l in by_recency
            if l["status"] in ("active", "notice")
            and l["start_date"] <= today
            and (l["end_date"] is None or l["end_date"] >= today)
        ),
        None,
    ) or (by_recency[0] if by_recency else None)
    current_id = current["lease_id"] if current else None
    return {
        "unit_id": unit_id,
        "as_of": through,
        "current_lease_id": current_id,
        "current_balance": next(
            (l["closing_balance"] for l in ledgers if l["lease_id"] == current_id), 0.0
        ),
        "leases": ledgers,
    }


def arrears_by_month(
    db: Session,
    account_id: str,
    date_from: date,
    date_to: date,
    tags: list[str] | None = None,
    property_id: str | None = None,
) -> tuple[dict[date, list[dict]], dict[str, dict]]:
    """Per-unit arrears positions for the attention feed: `(by_month, final_positions)`.

    Computed ONCE per feed request over the whole period and indexed, rather than per month: a
    cumulative balance has to walk each tenancy's full history to be correct, so a detector
    that recomputed it inside the feed's month loop would redo that walk for every month in a
    YTD or trailing-12 window. Each entry carries the running `balance` at that month and the
    `movement` into it, plus the identity the feed needs to label an item.

    `by_month` holds only entries INSIDE [date_from, date_to]. `final_positions` holds every
    tenancy's LAST entry at or before `date_to`, in or out of the window, because a cumulative
    balance must not disappear just because the requested window happens to contain no unit
    records — which is exactly what the default single-month feed hits whenever
    `property_month_summary` runs past `unit_month_summary` (a shared expense posted forward,
    say). £5,000 outstanding is still outstanding in a month nobody has entered yet; the
    detector falls back to this so it reports the standing debt, dated to the month it last
    moved rather than to an empty month.
    """
    scope = "p.account_id = :account_id"
    params: dict = {"account_id": account_id}
    if property_id is not None:
        scope += " AND u.property_id = :pid"
        params["pid"] = property_id
    else:
        scope += f" AND {_tag_where('p.id')}"
        params["tags"] = tags
    units = _rows(
        db.execute(
            text(
                f"""
                SELECT u.id::text AS unit_id, u.unit_number,
                       p.id::text AS property_id, p.name AS property_name
                FROM units u JOIN properties p ON p.id = u.property_id
                WHERE {scope} AND NOT u.is_shell
                """
            ),
            params,
        )
    )
    if not units:
        return {}, {}
    unit_ids = [u["unit_id"] for u in units]
    identity = {u["unit_id"]: u for u in units}
    leases_by_unit, actual_by_unit, adj_by_unit = _arrears_inputs(
        db, account_id, unit_ids, date_to
    )

    from_idx, to_idx = _month_index(date_from), _month_index(date_to)
    by_month: dict[date, list[dict]] = {}
    final: dict[str, dict] = {}
    for unit_id, leases in leases_by_unit.items():
        ledgers = _unit_arrears_ledgers(
            leases, actual_by_unit.get(unit_id, {}), adj_by_unit.get(unit_id, {}), date_to
        )
        for ledger in ledgers:
            for m in ledger["months"]:
                entry = {
                    **identity[unit_id],
                    "lease_id": ledger["lease_id"],
                    "tenant_name": ledger["tenant_name"],
                    "month": m["month"],
                    "balance": m["balance"],
                    "movement": m["movement"],
                    "rent_due": m["rent_due"],
                    "rent_collected": m["rent_collected"],
                }
                if from_idx <= _month_index(m["month"]) <= to_idx:
                    by_month.setdefault(m["month"], []).append(entry)
                # `months` is month-ordered and the ledgers are tenancy-ordered, so the last
                # write wins and `final` ends up holding the unit's most recent position
                # across its whole lease history.
                final[unit_id] = entry
    return by_month, final


def _rent_roll_row(row: dict, today: date) -> dict:
    has_lease = row["lease_status"] is not None
    status = row["lease_status"] if has_lease else "vacant"
    occupied = status != "vacant"
    actual_rent = row["actual_rent"]
    contract_rent = row["contract_rent"]
    lease_end = row["lease_end"]

    # Holdover (analyst report ask): a lapsed lease (end_date in the past, status
    # 'expired' — current_lease already guarantees no newer lease has taken over, since
    # it's the DISTINCT ON winner) whose unit still has a recent actual rent record on
    # file — the tenant is still there and paying, just off-contract. Distinct from both a
    # clean active lease and a true vacancy; still counts "occupied" (status is unchanged).
    holdover = (
        status == "expired"
        and lease_end is not None
        and lease_end < today
        and actual_rent is not None
        and actual_rent > 0
    )

    # Vacancy downtime: days since the PRIOR lease's end_date, for vacant rows only — a
    # unit with no lease on file at all (has_lease False) has no end_date to count from.
    vacant_days = (today - lease_end).days if (not occupied and lease_end is not None) else None

    return {
        "unit_id": row["unit_id"],
        "unit_number": row["unit_number"],
        "label": row["label"],
        # Migration 0014: internal-only flag (not part of the public RentRollRow schema —
        # pydantic silently drops unknown dict keys on serialization) consumed by
        # `_occupancy_summary` to exclude synthetic/shell units from its aggregates. Still
        # kept in the ROW itself so a shell unit's lease (e.g. Cedar Plaza Retail's
        # percentage-rent terms) stays visible in the rent-roll table.
        "is_shell": bool(row["is_shell"]),
        "property_id": row["property_id"],
        "property_name": row["property_name"],
        "lease_id": row["lease_id"],
        # A vacant unit's PRIOR tenant is not a current tenancy — presenting that name as
        # if it were an active tenant is misleading (fix #3), so null it out here. The
        # unit's contract_rent is DELIBERATELY kept (for both occupied and vacant rows):
        # it's the reference "potential rent" figure the GPR-based economic occupancy
        # calc needs (see _occupancy_summary) and the UI surfaces it as "asking rent" for
        # a vacant row rather than implying there's a live lease.
        "tenant_name": row["tenant_name"] if occupied else None,
        "lease_start": row["lease_start"],
        "lease_end": lease_end,
        "contract_rent": float(contract_rent) if contract_rent is not None else None,
        "status": status,
        "actual_rent": float(actual_rent) if actual_rent is not None else None,
        "actual_month": row["actual_month"],
        # An occupied unit (per the lease) with no recorded rent for the latest month is a
        # DATA GAP, not a vacancy — the whole point of keeping lease state separate from
        # monthly_records presence (see migration 0011's is_vacant flag for the same split
        # one level down, at the monthly-record grain).
        "missing_data": occupied and actual_rent is None,
        # A vacant unit has no current lease TERM to roll over — a "months to expiry"
        # figure there would just be the prior tenant's stale end date read as if it were
        # an upcoming/overdue expiration (fix #3). Only meaningful for occupied units.
        "months_to_expiry": _months_to_expiry(lease_end, today) if occupied else None,
        "security_deposit": float(row["security_deposit"]) if row["security_deposit"] is not None else None,
        "escalation_pct": float(row["escalation_pct"]) if row["escalation_pct"] is not None else None,
        "escalation_frequency_months": row["escalation_frequency_months"] if has_lease else None,
        # Anchored on the last ACTUAL rent increase when there is one (migration 0024),
        # else on lease_start as before: once the rent has moved, the next scheduled bump is
        # a cadence from THAT date, not from a start date the schedule has already left
        # behind. Same anchor `_rent_due_for_month` prices from, so the date shown and the
        # rent charged can't disagree.
        "next_escalation_date": _next_escalation_date(
            row["last_rent_increase_date"] or row["lease_start"],
            row["escalation_frequency_months"], row["escalation_pct"], today, lease_end
        ),
        "lease_type": row["lease_type"] if has_lease else None,
        "holdover": holdover,
        "vacant_days": vacant_days,
        "pct_rent_rate": float(row["pct_rent_rate"]) if row["pct_rent_rate"] is not None else None,
        "pct_rent_breakpoint": (
            float(row["pct_rent_breakpoint"]) if row["pct_rent_breakpoint"] is not None else None
        ),
        # The unit's own market_rent (migration 0015) — waterfall-followups item 1.
        # Independent of lease/status; admin-editable via `PATCH /units/{id}`.
        "market_rent": float(row["market_rent"]) if row["market_rent"] is not None else None,
        # Standing $/month concession (migration 0016) on the CURRENT lease, if any — null
        # when there's no lease on file at all, or the lease has no concession.
        "concession_monthly": (
            float(row["concession_monthly"]) if row["concession_monthly"] is not None else None
        ),
        # Periodic-tenancy rent history (migration 0024). `last_rent_increase_date` is when
        # the current `contract_rent` took effect and `rent_before_increase` what it replaced
        # (null = not held). `months_since_last_increase` is the operational read on an
        # England-style rolling tenancy — rises are typically annual and no sooner, so "14
        # months since the last one" is the review that's overdue; it counts from
        # `lease_start` when the rent has never been increased, which is the same question
        # asked of a tenancy still on its original rent.
        "last_rent_increase_date": row["last_rent_increase_date"],
        "rent_before_increase": (
            float(row["rent_before_increase"]) if row["rent_before_increase"] is not None else None
        ),
        # Surfaced for the same reason `concession_monthly` is: the rent-roll's lease editor
        # saves through a FULL-REPLACE PATCH, so a field it can't read back is a field any
        # unrelated edit silently erases. This is the stored half of the arrears balance below
        # (`arrears_balance` includes it), not a second copy of it.
        "opening_arrears": (
            float(row["opening_arrears"]) if row["opening_arrears"] is not None else None
        ),
        "arrears_from_month": row["arrears_from_month"],
        "months_since_last_increase": (
            _months_elapsed(row["last_rent_increase_date"] or row["lease_start"], today)
            if has_lease else None
        ),
        # Filled in by property_rent_roll/portfolio_rent_roll (needs the whole unit list +
        # the resolved period first) via `_rent_variance_for_units`. Left as None here so
        # the dict shape is complete even if that merge step is ever skipped.
        "expected_rent": None,
        "period_actual_rent": None,
        "variance": None,
        "variance_pct": None,
        # Arrears (migration 0025), likewise merged in later by `_apply_arrears`. Zeros, not
        # nulls, are the right default for a unit with no lease on file: no tenancy means
        # nothing owed — a fact, not a gap. (A SHELL unit is the exception and is nulled back
        # out by `_apply_arrears`, having no tenancy of its own at all.)
        **_empty_arrears(),
    }


def _avg_contract_rent_by_property(rows: list[dict]) -> dict[str, float]:
    """Average contract rent among a property's OCCUPIED units — the market-rent proxy
    used for Gross Potential Rent (see ``_occupancy_summary``) when a vacant unit has
    never had a lease on file at all, so there's no asking/prior rent of its own to use."""
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for r in rows:
        if r["status"] != "vacant" and r["contract_rent"] is not None:
            sums[r["property_id"]] = sums.get(r["property_id"], 0.0) + r["contract_rent"]
            counts[r["property_id"]] = counts.get(r["property_id"], 0) + 1
    return {pid: sums[pid] / counts[pid] for pid in sums}


def _occupancy_summary(rows: list[dict]) -> dict:
    """Physical occupancy (unit count) + two DIFFERENT rent-based ratios, kept under
    clearly separate names (analyst-report fix #1):

    - ``economic_occupancy`` = Σ (actual collected rent, capped per-unit at that unit's own
      contract rent) ÷ Gross Potential Rent (GPR), where GPR is the full "every unit
      rented" ceiling — occupied units' contract rent PLUS vacant units' potential rent
      (the property's current average in-place contract rent, falling back to the vacant
      unit's own last-known/asking rent only when there's no occupied comparable at all).
      This is genuine economic occupancy: it captures vacancy loss, so it comes in AT OR
      BELOW physical occupancy whenever there's vacancy, and never exceeds ~100% — never
      because vacant units were silently excluded from the denominator. The per-unit cap
      on the numerator (verified against live data) matters here: without it, a handful of
      units collecting slightly ABOVE their own contract rent (fees, a bump not yet
      re-papered — see ``rent_realization`` below, ~100.1% on this data) can, combined with
      a small vacant sample, push the ratio a hair above physical occupancy on a pure
      collections technicality unrelated to vacancy — capping keeps the metric strictly
      about occupancy/vacancy, not collections variance.
    - ``rent_realization`` = Σ actual ÷ Σ contract rent, over OCCUPIED units with both
      figures on file. This is the metric the endpoint used to mislabel "economic
      occupancy": it structurally excludes vacant units from the denominator, so it can
      read ABOVE both physical occupancy and 100% — it says nothing about vacancy loss,
      only about how actual collections compare to contract rent for tenants already in
      place (a collections/loss-to-lease style signal, not an occupancy metric).

    Synthetic/shell units (analyst report fix #1 — e.g. Cedar Plaza Retail's ``RETAIL``
    unit, planted only to hang a percentage-rent lease off a unit-less property) are
    EXCLUDED here entirely: they have no monthly_records/unit_month_summary of their own,
    so they'd otherwise show up as a phantom occupied unit with contract rent but no actual
    rent, inflating ``total_units`` and dragging ``economic_occupancy`` down on a fictional
    vacancy-loss signal. They still appear in the rent-roll ROWS list (so the lease terms
    stay visible/editable) — only this aggregate is scoped to real, physical units.
    """
    rows = [r for r in rows if not r.get("is_shell")]
    total = len(rows)
    occupied_rows = [r for r in rows if r["status"] != "vacant"]
    occupied = len(occupied_rows)

    by_property_avg = _avg_contract_rent_by_property(rows)
    occupied_contract_sum = sum(r["contract_rent"] for r in occupied_rows if r["contract_rent"] is not None)
    occupied_contract_n = sum(1 for r in occupied_rows if r["contract_rent"] is not None)
    portfolio_avg_rent = (occupied_contract_sum / occupied_contract_n) if occupied_contract_n else None

    gpr_total = 0.0
    gpr_known = 0
    for r in rows:
        if r["status"] != "vacant":
            potential = r["contract_rent"]
        else:
            # Deliberately prefer the property's CURRENT average in-place rent over the
            # vacant unit's own (possibly stale/below-market) last-known contract_rent: a
            # unit that's been sitting vacant since an old, lower-rent lease would
            # otherwise understate GPR and could push economic occupancy above physical
            # occupancy — exactly the bug this fix corrects. Fall back to the vacant
            # unit's own contract_rent only if there's no occupied comparable at all
            # (property- or portfolio-wide) to average.
            potential = by_property_avg.get(r["property_id"])
            if potential is None:
                potential = portfolio_avg_rent
            if potential is None:
                potential = r["contract_rent"]
        if potential is not None:
            gpr_total += potential
            gpr_known += 1

    # Capped at each unit's own contract rent: a unit collecting slightly MORE than its
    # contract (fees, a rent bump not yet re-papered, etc.) is still just ONE fully-
    # occupied unit, not "more than 100% occupied" — letting overage flow through
    # uncapped would let a handful of over-collecting units mask real vacancy loss
    # elsewhere and push economic occupancy above physical occupancy purely on a
    # collections technicality (verified against this app's own seed data — see the
    # rent-roll fixes changelog). The UNCAPPED actual/contract ratio is exactly what
    # `rent_realization` below reports instead, so that signal isn't lost.
    econ_actual = sum(
        min(r["actual_rent"], r["contract_rent"]) if r["contract_rent"] is not None else r["actual_rent"]
        for r in occupied_rows
        if r["actual_rent"] is not None
    )

    realization_rows = [r for r in occupied_rows if r["actual_rent"] is not None and r["contract_rent"]]
    realization_actual = sum(r["actual_rent"] for r in realization_rows)
    realization_contract = sum(r["contract_rent"] for r in realization_rows)

    # Average downtime: vacant rows with a known `vacant_days` (i.e. the unit has SOME
    # lease history to count from — a never-leased unit has no end_date to anchor on).
    vacant_day_values = [r["vacant_days"] for r in rows if r["status"] == "vacant" and r["vacant_days"] is not None]
    avg_vacant_days = (sum(vacant_day_values) / len(vacant_day_values)) if vacant_day_values else None

    return {
        "total_units": total,
        "occupied_units": occupied,
        "physical_occupancy": (occupied / total) if total else None,
        "gross_potential_rent": gpr_total if gpr_known else None,
        "economic_occupancy": (econ_actual / gpr_total) if gpr_total else None,
        "rent_realization": (realization_actual / realization_contract) if realization_contract else None,
        "avg_vacant_days": avg_vacant_days,
    }


def _rent_roll_sql(where: str) -> str:
    return f"""
        WITH {_CURRENT_LEASE_CTE},
        latest_month AS (
            -- Deliberately sourced from unit_month_summary (has real unit-level data),
            -- NOT property_month_summary: a property-tier-only record with no unit rows
            -- posted for that month (e.g. a draft period touched but never filled in)
            -- would otherwise anchor "latest month" on a month with zero unit data,
            -- making every unit look like a data gap even when its actual history is fine.
            -- Clamped to the present for the same reason as `latest_actual_month`: a month
            -- that hasn't happened yet has no "actual rent" to show against a lease.
            SELECT property_id, max(month) AS month
            FROM unit_month_summary
            WHERE month <= :as_of_month
            GROUP BY property_id
        )
        SELECT
            u.id::text AS unit_id, u.unit_number, u.label, u.is_shell, u.market_rent,
            p.id::text AS property_id, p.name AS property_name,
            cl.id::text AS lease_id, cl.tenant_name, cl.start_date AS lease_start,
            cl.end_date AS lease_end, cl.contract_rent, cl.status AS lease_status,
            cl.security_deposit, cl.escalation_pct, cl.escalation_frequency_months,
            cl.lease_type, cl.pct_rent_rate, cl.pct_rent_breakpoint, cl.concession_monthly,
            cl.last_rent_increase_date, cl.rent_before_increase, cl.opening_arrears,
            cl.arrears_from_month,
            lm.month AS actual_month, ums.gross_rent AS actual_rent
        FROM units u
        JOIN properties p ON p.id = u.property_id
        LEFT JOIN current_lease cl ON cl.unit_id = u.id
        LEFT JOIN latest_month lm ON lm.property_id = u.property_id
        LEFT JOIN unit_month_summary ums ON ums.unit_id = u.id AND ums.month = lm.month
        WHERE {where}
        ORDER BY p.name, u.unit_number
    """


def _apply_rent_variance(
    db: Session, account_id: str, out_rows: list[dict], unit_scope_where: str, params: dict,
    date_from: date | None, date_to: date | None,
) -> tuple[date | None, date | None, dict]:
    """Shared by property_rent_roll/portfolio_rent_roll: resolve the variance window
    (default: latest month with actual data, single-month — same convention as
    `_resolve_range`), compute expected/actual/variance per unit, merge the per-unit
    figures into each row IN PLACE, and return the resolved window + the rollup."""
    period_from, period_to = _resolve_rent_variance_period(db, unit_scope_where, params, date_from, date_to)
    if period_to is None:
        rollup = _rent_variance_rollup(out_rows)  # all None `expected_rent` -> zeros, unit_count 0
        return None, None, rollup
    by_unit = _rent_variance_for_units(
        db, account_id, [r["unit_id"] for r in out_rows], period_from, period_to
    )
    for row in out_rows:
        # Shell/synthetic units (e.g. Cedar Plaza Retail's placeholder) have no actual
        # unit-month records of their own — their rent books at the property tier — so any
        # expected-vs-actual comparison is meaningless. Leave the variance fields null so
        # the row renders "—" instead of a phantom shortfall, and keep them out of the
        # rollup (already excluded in `_rent_variance_rollup`).
        if row.get("is_shell"):
            continue
        v = by_unit.get(row["unit_id"])
        if v is not None:
            row.update(v)
    return period_from, period_to, _rent_variance_rollup(out_rows)


def _apply_arrears(
    db: Session, account_id: str, out_rows: list[dict], period_from: date | None,
    period_to: date | None,
) -> dict:
    """Merge each row's arrears figures in place and return the rollup (migration 0025).

    Runs AFTER `_apply_rent_variance` and reuses the window it resolved, so the monthly
    movement shown next to the variance covers exactly the same months — a reader comparing
    the two columns is comparing like with like, and `arrears_movement` is the variance's own
    shortfall with the adjustments added and the sign flipped to "owed".

    Each row takes the summary for the tenancy IT is showing (`lease_id`), not the unit's
    worst or latest: the rent roll's row is the current tenancy, so its balance must be that
    tenancy's. A row whose current lease has no ledger keeps the zeros from `_rent_roll_row`.
    """
    if period_to is None:
        # No summarized month anywhere in scope: nothing has been collected or missed yet, so
        # every row keeps its zeros and the rollup is empty rather than fabricated.
        return _arrears_rollup([])
    by_unit = _arrears_for_units(
        db, account_id, [r["unit_id"] for r in out_rows], period_from, period_to
    )
    for row in out_rows:
        if row.get("is_shell"):
            # Same exclusion, same reason, as the variance merge: a shell unit's rent books at
            # the property tier, so it has no tenancy of its own and no balance to report.
            # Nulled (not zeroed) so the UI renders "—" rather than a confident £0.
            row.update({k: None for k in _empty_arrears()})
            continue
        summary = by_unit.get(row["unit_id"], {}).get(row.get("lease_id"))
        if summary is not None:
            row.update(summary)
    return _arrears_rollup(out_rows)


def property_rent_roll(
    db: Session,
    account_id: str,
    property_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict:
    """Rent roll for one property: one row per unit with lease + actual rent + rollover
    horizon, plus a physical/economic occupancy summary derived from lease status, plus
    expected-vs-actual rent variance (reference data — see `_rent_variance_for_units`) for
    the given/default period."""
    today = date.today()
    scope_where = "u.property_id = :pid AND p.account_id = :account_id"
    params = {"pid": property_id, "account_id": account_id}
    rows = _rows(db.execute(text(_rent_roll_sql(scope_where)), {**params, "as_of_month": current_month()}))
    out_rows = [_rent_roll_row(r, today) for r in rows]
    period_from, period_to, rent_variance = _apply_rent_variance(
        db, account_id, out_rows, scope_where, params, date_from, date_to
    )
    arrears = _apply_arrears(db, account_id, out_rows, period_from, period_to)
    return {
        "property_id": property_id, "as_of": today, "rows": out_rows, "occupancy": _occupancy_summary(out_rows),
        "period_from": period_from, "period_to": period_to, "rent_variance": rent_variance,
        "arrears": arrears,
    }


def portfolio_rent_roll(
    db: Session,
    account_id: str,
    tags: list[str] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict:
    """Portfolio-wide rent roll across every multifamily unit. ``tags`` optionally scopes
    to properties carrying ANY of the given tags (OR semantics). Also computes
    expected-vs-actual rent variance for the given/default period, honoring the same tag
    filter on both sides (numerator and denominator scoped to the same property set)."""
    today = date.today()
    scope_where = f"p.account_id = :account_id AND {_tag_where('p.id')}"
    params = {"account_id": account_id, "tags": tags}
    rows = _rows(db.execute(text(_rent_roll_sql(scope_where)), {**params, "as_of_month": current_month()}))
    out_rows = [_rent_roll_row(r, today) for r in rows]
    period_from, period_to, rent_variance = _apply_rent_variance(
        db, account_id, out_rows, scope_where, params, date_from, date_to
    )
    arrears = _apply_arrears(db, account_id, out_rows, period_from, period_to)
    return {
        "property_id": None, "as_of": today, "rows": out_rows, "occupancy": _occupancy_summary(out_rows),
        "period_from": period_from, "period_to": period_to, "rent_variance": rent_variance,
        "arrears": arrears,
    }


def lease_expirations(
    db: Session, account_id: str, within_months: int, tags: list[str] | None = None
) -> dict:
    """Leases expiring within ``within_months`` of today, soonest first — the rollover
    horizon. Only 'active'/'notice' leases with a fixed end_date qualify: month-to-month
    leases (no end_date) never roll over, and an already-'expired'/'vacant' lease's
    end_date is in the past, not an upcoming expiration (a lapsed-holdover feed is a
    different concern from this one). ``tags`` optionally scopes to properties carrying
    ANY of the given tags (OR semantics)."""
    today = date.today()
    cutoff = today + timedelta(days=within_months * 30)
    sql = f"""
        WITH {_CURRENT_LEASE_CTE}
        SELECT
            u.id::text AS unit_id, u.unit_number, u.label,
            p.id::text AS property_id, p.name AS property_name,
            cl.tenant_name, cl.end_date AS lease_end, cl.contract_rent
        FROM current_lease cl
        JOIN units u ON u.id = cl.unit_id
        JOIN properties p ON p.id = u.property_id
        WHERE cl.end_date IS NOT NULL
          AND cl.status IN ('active', 'notice')
          AND cl.end_date BETWEEN :today AND :cutoff
          AND p.account_id = :account_id
          AND {_tag_where("p.id")}
        ORDER BY cl.end_date ASC, p.name, u.unit_number
    """
    rows = _rows(
        db.execute(
            text(sql),
            {"today": today, "cutoff": cutoff, "tags": tags, "account_id": account_id},
        )
    )
    for r in rows:
        r["contract_rent"] = float(r["contract_rent"])
        r["months_to_expiry"] = _months_to_expiry(r["lease_end"], today)

    # Rollup fields (analyst report fix #2): what fraction of in-place portfolio rent is
    # exposed to rollover in this window. The denominator is total in-place contract rent
    # across every CURRENTLY occupied unit (any non-'vacant' lease status), honoring the
    # same ``tags`` filter as the numerator so a tag-filtered call stays internally
    # consistent (both sides scoped to the same property set).
    total_contract_rent_expiring = sum(r["contract_rent"] for r in rows)
    in_place_sql = f"""
        WITH {_CURRENT_LEASE_CTE}
        SELECT COALESCE(SUM(cl.contract_rent), 0) AS total
        FROM current_lease cl
        JOIN units u ON u.id = cl.unit_id
        JOIN properties p ON p.id = u.property_id
        WHERE cl.status != 'vacant'
          AND p.account_id = :account_id
          AND {_tag_where("p.id")}
    """
    total_portfolio_rent = float(
        db.execute(text(in_place_sql), {"tags": tags, "account_id": account_id}).scalar() or 0
    )
    pct_of_portfolio_rent = (
        (total_contract_rent_expiring / total_portfolio_rent) if total_portfolio_rent else None
    )
    return {
        "within_months": within_months,
        "as_of": today,
        "items": rows,
        "total_contract_rent_expiring": total_contract_rent_expiring,
        "pct_of_portfolio_rent": pct_of_portfolio_rent,
    }


# ---- Rent waterfall (migration 0015: units.market_rent) -------------------------------
#
# Decomposes the gap between Gross Potential Rent (every real unit priced at market) and
# actual collected rent into the standard CRE bridge: GPR -> loss-to-lease -> vacancy loss
# -> collections loss (& concessions) -> actual collected. `market_rent` is reference data,
# exactly like `contract_rent`/`is_shell` before it — it never feeds NOI/cash-flow.
#
# BASIS (analyst-reviewed rework, option (a) — see rent-waterfall-rework-changelog.md): the
# waterfall is computed over EVERY unit-month in the period, for every real unit, driven by
# LEASE STATUS — never gated on whether an actual record happens to be on file. This is a
# deliberate change from the earlier {lease-in-force-or-holdover} ∩ {actual-on-file}
# intersection basis (which is still what the SEPARATE rent-variance feature,
# `_unit_expected_actual`, uses): that intersection silently dropped any unit-month lacking
# either leg, so a unit's genuine vacancy (lease lapsed, nothing collected, so naturally no
# actual record) was invisible to it, and `actual_collected` under-counted whenever lease
# coverage had gaps. Root cause of those gaps (leases anchored to a today-relative date that
# drifts away from this seed's fixed 2024-2025 actuals window) is fixed at the source in
# `app/seed.py`/`scripts/reanchor_lease_timeline.py` — with that fix, this function now sees
# a governing-or-vacant classification for essentially every unit-month, and:
#   - `actual_collected` sums `unit_month_summary.gross_rent` (0 if no record) over EVERY
#     unit-month in scope, so it reconciles exactly to the unit-tier P&L gross rent for the
#     same scope/period (see `property_rent_waterfall`/`portfolio_rent_waterfall`).
#   - `vacancy_loss` populates for real lease-gap vacancies (e.g. Maple Court unit 105).
#
# Waterfall-followups item 3: `collections_loss` is further split into `concessions` (a
# leasing decision — the governing lease's `concession_monthly`, migration 0016) and
# `bad_debt` (the remainder — delinquency). See `_unit_waterfall`'s docstring for the
# per-month capping rule that keeps `concessions + bad_debt == collections_loss` exact.
def _unit_waterfall(
    leases: list[dict], actual_by_month: dict[date, float], months: list[date], market_rent: float | None
) -> dict:
    """One unit's waterfall components, summed across `months`. `market_rent` is this
    unit's GPR figure (None — a unit with no `market_rent` on file — contributes nothing,
    same "unknown, don't guess" stance as `_occupancy_summary`'s GPR gaps).

    Every month in `months` is classified as either OCCUPIED (a governing lease, or a
    holdover — same resolution/freeze rule as `_unit_expected_actual`) or VACANT (no lease
    in force, no holdover) — there is no third "dropped" case; `actual` defaults to 0 for a
    month with no `unit_month_summary` record on file. This is what keeps the identity exact
    by construction:
      - occupied month: `gpr - loss_to_lease - collections_loss` = `market_rent -
        (market_rent - scheduled) - (scheduled - actual)` = `actual`.
      - vacant month: `gpr - vacancy_loss` = `market_rent - market_rent` = `0`. The identity
        only holds here if `actual` is also 0 — true whenever lease coverage is continuous
        (item 1's fix): a month classified vacant has no lease basis (or a lapsed one with
        nothing collected), so there is nothing to have paid.

    Concessions (item 3): for each OCCUPIED month, the governing (or, for a holdover, the
    lapsed) lease's `concession_monthly` is attributed as that month's `concessions`
    contribution, CAPPED at `max(0, scheduled - actual)` — the month's own shortfall. This
    never manufactures a loss that wasn't already there (a lease with a concession but no
    real shortfall that month contributes $0) and keeps `concessions <= collections_loss`
    per month, so the remainder (`bad_debt`, computed by the caller as `collections_loss -
    concessions`) never needs its own separate accumulation — it falls out of the same
    per-month arithmetic automatically. No vacant-month contribution: a vacant unit has no
    lease-granted concession to speak of.
    """
    empty = {
        "gpr": 0.0, "loss_to_lease": 0.0, "vacancy_loss": 0.0,
        "collections_loss": 0.0, "concessions": 0.0, "actual_collected": 0.0,
    }
    if market_rent is None:
        return empty
    ordered = sorted(leases, key=lambda l: _month_index(l["start_date"]), reverse=True)
    out = dict(empty)
    for m in months:
        actual = actual_by_month.get(m, 0.0)
        m_idx = _month_index(m)
        governing = next(
            (
                l for l in ordered
                if _month_index(l["start_date"]) <= m_idx
                and (l["end_date"] is None or _month_index(l["end_date"]) >= m_idx)
            ),
            None,
        )
        if governing is not None:
            scheduled = _escalated_rent(
                governing["contract_rent"], governing["escalation_pct"],
                governing["escalation_frequency_months"], governing["start_date"], m,
            )
            concession_monthly = float(governing.get("concession_monthly") or 0.0)
        else:
            prev = next((l for l in ordered if _month_index(l["start_date"]) <= m_idx), None)
            if prev is not None and prev["end_date"] is not None and actual > 0:
                # Holdover: lapsed lease, rent still collected -> occupied, frozen rent.
                scheduled = _escalated_rent(
                    prev["contract_rent"], prev["escalation_pct"],
                    prev["escalation_frequency_months"], prev["start_date"], prev["end_date"],
                )
                concession_monthly = float(prev.get("concession_monthly") or 0.0)
            else:
                # Vacant: no lease in force, and either no prior lease at all, or a lapsed
                # one with nothing collected this month (a genuine between-tenant gap).
                out["gpr"] += market_rent
                out["vacancy_loss"] += market_rent
                out["actual_collected"] += actual
                continue
        out["gpr"] += market_rent
        out["loss_to_lease"] += market_rent - scheduled
        out["collections_loss"] += scheduled - actual
        out["concessions"] += min(concession_monthly, max(0.0, scheduled - actual))
        out["actual_collected"] += actual
    return out


_WATERFALL_KEYS = (
    "gpr", "loss_to_lease", "vacancy_loss", "collections_loss", "concessions", "actual_collected",
)
# Every bridge line EXCEPT gpr itself gets a %-of-GPR figure (rework item 4, extended by
# item 3 to cover the concessions/bad_debt split) — IC reads a rent bridge as "loss-to-lease
# X%, vacancy Y%, concessions Z%, bad debt W%" at least as often as in raw dollars. Computed
# on read, purely derived (component / gpr); no schema/migration needed.
_WATERFALL_PCT_KEYS = (
    "loss_to_lease", "vacancy_loss", "collections_loss", "concessions", "bad_debt", "actual_collected",
)


def _sum_waterfall(components: list[dict]) -> dict:
    """Sum a list of per-unit (or per-property, for single-asset properties — see
    `_single_asset_property_components`) waterfall dicts into one totals block, plus
    `residual` — the identity's balance check (`gpr - loss_to_lease - vacancy_loss -
    collections_loss - actual_collected`), which should be ~0 to the cent by construction
    (see `_unit_waterfall`'s docstring); returned explicitly so callers/tests/curl can
    verify it rather than trusting the math blindly. Also computes each line's %-of-GPR
    (`<line>_pct_of_gpr`; null when `gpr` is 0 — nothing to divide by, not "0%").

    `bad_debt` (waterfall-followups item 3) is NOT one of `_WATERFALL_KEYS` — it's not
    accumulated per-unit in `_unit_waterfall`, it's derived HERE as `collections_loss -
    concessions` after summing. This is mathematically identical to summing a per-unit
    `bad_debt` (both `collections_loss` and `concessions` are themselves per-month-capped
    sums, and subtraction distributes over a sum), so `concessions + bad_debt ==
    collections_loss` holds exactly at every level (unit/property/portfolio) without
    needing a second accumulator threaded through `_unit_waterfall`."""
    totals = {k: sum(c[k] for c in components) for k in _WATERFALL_KEYS}
    totals["bad_debt"] = totals["collections_loss"] - totals["concessions"]
    totals["residual"] = (
        totals["gpr"] - totals["loss_to_lease"] - totals["vacancy_loss"]
        - totals["collections_loss"] - totals["actual_collected"]
    )
    gpr = totals["gpr"]
    for k in _WATERFALL_PCT_KEYS:
        totals[f"{k}_pct_of_gpr"] = (totals[k] / gpr) if gpr else None
    return totals


def _rent_waterfall_units(db: Session, unit_scope_where: str, params: dict) -> list[dict]:
    """Real (non-shell) units in scope, with their `market_rent`, owning property's
    id/name (for the portfolio endpoint's per-property breakdown), and `unit_number`/
    `label` (for the property endpoint's per-unit drill-down — waterfall-followups item
    4)."""
    sql = f"""
        SELECT u.id::text AS unit_id, u.property_id::text AS property_id,
               p.name AS property_name, u.market_rent, u.unit_number, u.label
        FROM units u
        JOIN properties p ON p.id = u.property_id
        WHERE NOT u.is_shell AND {unit_scope_where}
    """
    return _rows(db.execute(text(sql), params))


def _rent_waterfall_for_units(
    db: Session, account_id: str, units: list[dict], date_from: date, date_to: date
) -> dict[str, dict]:
    """Per-unit waterfall components (see `_unit_waterfall`) for every unit in `units`
    (each needs `unit_id`/`market_rent`), over `[date_from, date_to]`. Every unit gets an
    entry (all-zero when it has no `market_rent` on file, or no data in range) so callers
    can merge with a plain dict lookup."""
    months = _months_in_range(date_from, date_to)
    unit_ids = [u["unit_id"] for u in units]
    empty = {
        "gpr": 0.0, "loss_to_lease": 0.0, "vacancy_loss": 0.0,
        "collections_loss": 0.0, "concessions": 0.0, "actual_collected": 0.0,
    }
    if not unit_ids or not months:
        return {u["unit_id"]: dict(empty) for u in units}

    lease_rows = _rows(
        db.execute(
            text(
                "SELECT l.unit_id::text AS unit_id, l.start_date, l.end_date, l.contract_rent, "
                "l.escalation_pct, l.escalation_frequency_months, l.concession_monthly "
                "FROM lease l "
                "JOIN units u ON u.id = l.unit_id "
                "JOIN properties p ON p.id = u.property_id "
                "WHERE l.unit_id::text = ANY(:unit_ids) AND p.account_id = :account_id"
            ),
            {"unit_ids": unit_ids, "account_id": account_id},
        )
    )
    actual_rows = _rows(
        db.execute(
            text(
                "SELECT unit_id::text AS unit_id, month, gross_rent FROM unit_month_summary "
                "WHERE unit_id::text = ANY(:unit_ids) AND account_id = :account_id "
                "AND month BETWEEN :f AND :t"
            ),
            {
                "unit_ids": unit_ids,
                "account_id": account_id,
                "f": months[0],
                "t": months[-1],
            },
        )
    )

    leases_by_unit: dict[str, list[dict]] = {}
    for r in lease_rows:
        leases_by_unit.setdefault(r["unit_id"], []).append(r)
    actual_by_unit: dict[str, dict[date, float]] = {}
    for r in actual_rows:
        actual_by_unit.setdefault(r["unit_id"], {})[r["month"]] = float(r["gross_rent"])

    result: dict[str, dict] = {}
    for u in units:
        mr = float(u["market_rent"]) if u["market_rent"] is not None else None
        result[u["unit_id"]] = _unit_waterfall(
            leases_by_unit.get(u["unit_id"], []), actual_by_unit.get(u["unit_id"], {}), months, mr
        )
    return result


def _single_asset_property_components(
    db: Session, account_id: str, prop_where: str, params: dict, date_from: date, date_to: date
) -> list[dict]:
    """Single-asset properties with NO REAL UNIT of their own: they book rent directly at the
    property tier, and
    `PropertyMonthSummary.gross_rent` for them already IS the property's total rent (that
    model aggregates unit rows AND the property-tier row — see its docstring — but a
    single-asset property has only the latter). Rework item 3: without folding these in,
    the portfolio waterfall's `actual_collected` structurally excludes 3 of 18 properties
    and can never reconcile to portfolio P&L gross rent.

    Modeled as a simple pass-through leg (documented choice, not a placeholder): GPR =
    actual, no loss_to_lease/vacancy_loss/collections_loss split — there is no unit-level
    lease/market-rent basis on a unitless property to decompose one from. (Cedar Plaza
    Retail does carry a percentage-rent lease with a `contract_rent`, hung off a shell unit
    purely for that lease to exist on — see `app/seed.py`'s `_retail_lease_plan` — but that
    unit is excluded from every unit-level aggregate via `is_shell`, by design, so it's not
    reused as a decomposition basis here either; keeping ALL single-asset properties on one
    consistent, simple model beats a one-off exception for just this one.) Every property
    matching `prop_where` (alias `p`, `properties` table) gets an entry, `actual=0` when it
    has no `property_month_summary` rows in `[date_from, date_to]` (e.g. an investment
    property acquired after the period) — never silently dropped."""
    sql = f"""
        SELECT p.id::text AS property_id, p.name AS property_name,
               COALESCE(SUM(pms.gross_rent), 0) AS actual
        FROM properties p
        LEFT JOIN property_month_summary pms
               ON pms.property_id = p.id AND pms.month BETWEEN :f AND :t
        WHERE p.type = 'single' AND p.account_id = :account_id AND {prop_where}
          -- ...and ONLY while it has no real unit to speak for it.
          --
          -- This pass-through leg exists because a unitless property's rent is invisible to
          -- every unit-level aggregate. The moment such a property HAS a unit (a single-let
          -- house given its one dwelling, so it can hold a tenancy), the unit path already
          -- counts its rent — and counting it here as well listed every property twice in the
          -- per-property breakdown AND double-counted GPR and actual_collected in the portfolio
          -- totals. The unit path is also strictly better: it has a lease basis, so it can
          -- decompose loss-to-lease, voids and arrears, which this leg deliberately cannot.
          -- Shell units don't count (they're excluded from every unit-level aggregate by
          -- design), so a property carrying only a shell still needs this leg.
          AND NOT EXISTS (
              SELECT 1 FROM units u WHERE u.property_id = p.id AND NOT u.is_shell
          )
        GROUP BY p.id, p.name
    """
    rows = _rows(
        db.execute(
            text(sql), {**params, "account_id": account_id, "f": date_from, "t": date_to}
        )
    )
    out = []
    for r in rows:
        actual = float(r["actual"])
        out.append({
            "property_id": r["property_id"], "property_name": r["property_name"],
            "gpr": actual, "loss_to_lease": 0.0, "vacancy_loss": 0.0,
            "collections_loss": 0.0, "concessions": 0.0, "actual_collected": actual,
        })
    return out


def property_rent_waterfall(
    db: Session,
    account_id: str,
    property_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict | None:
    """Rent waterfall (GPR -> loss-to-lease -> vacancy loss -> collections loss & conces-
    sions -> actual collected) for one property over the given/default period. Returns
    None if the property does not exist. Shell/synthetic units excluded (same exclusion,
    same reason, as the rent roll/occupancy summary). A single-asset property (no units —
    rework item 3) gets the simple property-tier pass-through leg instead — see
    `_single_asset_property_components`."""
    prop = db.execute(
        text(
            "SELECT id::text AS id, name, type FROM properties "
            "WHERE id = :id AND account_id = :account_id"
        ),
        {"id": property_id, "account_id": account_id},
    ).mappings().first()
    if prop is None:
        return None

    if prop["type"] == "single":
        date_from, date_to = _resolve_range(
            db,
            "property_month_summary",
            "account_id = :account_id AND property_id = :pid",
            {"account_id": account_id, "pid": property_id},
            date_from,
            date_to,
        )
        if date_to is None:
            return {
                "property_id": prop["id"], "property_name": prop["name"],
                "period_from": None, "period_to": None, "unit_count": 0,
                **_sum_waterfall([]), "units": [],
            }
        comps = _single_asset_property_components(
            db, account_id, "p.id = :pid", {"pid": property_id}, date_from, date_to
        )
        return {
            "property_id": prop["id"], "property_name": prop["name"],
            "period_from": date_from, "period_to": date_to, "unit_count": 0,
            **_sum_waterfall(comps), "units": [],
        }

    unit_scope_where = "u.property_id = :pid AND p.account_id = :account_id"
    unit_scope_params = {"pid": property_id, "account_id": account_id}
    date_from, date_to = _resolve_rent_variance_period(
        db, unit_scope_where, unit_scope_params, date_from, date_to
    )
    units = _rent_waterfall_units(db, unit_scope_where, unit_scope_params)
    if date_to is None:
        return {
            "property_id": prop["id"], "property_name": prop["name"],
            "period_from": None, "period_to": None, "unit_count": 0,
            **_sum_waterfall([]), "units": [],
        }
    by_unit = _rent_waterfall_for_units(db, account_id, units, date_from, date_to)
    # Per-unit drill-down (waterfall-followups item 4, optional): the SAME per-unit
    # components already computed above for the property total, just surfaced individually
    # instead of only summed — so `units` sums exactly to this response's own totals block,
    # the same "parts sum to the whole" guarantee `portfolio_rent_waterfall.properties` has.
    # Ranked by total rent leakage (loss-to-lease + vacancy + collections), biggest first,
    # same convention as the portfolio endpoint's per-property ranking.
    unit_rows = [
        {"unit_id": u["unit_id"], "unit_number": u["unit_number"], "label": u["label"],
         **_sum_waterfall([by_unit[u["unit_id"]]])}
        for u in units
    ]
    unit_rows.sort(
        key=lambda r: r["loss_to_lease"] + r["vacancy_loss"] + r["collections_loss"], reverse=True
    )
    return {
        "property_id": prop["id"], "property_name": prop["name"],
        "period_from": date_from, "period_to": date_to, "unit_count": len(units),
        **_sum_waterfall(list(by_unit.values())), "units": unit_rows,
    }


def portfolio_rent_waterfall(
    db: Session,
    account_id: str,
    tags: list[str] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict:
    """Portfolio-wide rent waterfall, honoring the same tag filter (OR semantics) as every
    other portfolio-scoped endpoint, PLUS a per-property breakdown so an analyst can rank
    which assets carry the most vacancy loss / loss-to-lease / collections loss rather than
    just seeing the blended total. Each property's own components sum exactly to the
    portfolio total (verified live — see the changelog). Includes the 3 single-asset
    properties (rework item 3 — see `_single_asset_property_components`) alongside the 15
    multifamily properties' unit-level breakdown, so `actual_collected` reconciles to
    portfolio P&L gross rent rather than structurally excluding them."""
    where = f"p.account_id = :account_id AND {_tag_where('p.id')}"
    params: dict = {"account_id": account_id, "tags": tags}
    date_from, date_to = _resolve_rent_variance_period(db, where, params, date_from, date_to)
    if date_to is None:
        # No multifamily unit data in scope — a tag filter matching ONLY single-asset
        # properties would otherwise return empty even though those properties have real
        # property-tier rent. Fall back to resolving the period from property_month_summary.
        date_from, date_to = _resolve_range(
            db, "property_month_summary",
            f"account_id = :account_id "
            f"AND property_id IN (SELECT p.id FROM properties p WHERE {where})",
            params, date_from, date_to,
        )
    units = _rent_waterfall_units(db, where, params)
    if date_to is None:
        return {
            "period_from": None, "period_to": None, "unit_count": 0,
            **_sum_waterfall([]), "properties": [],
        }
    by_unit = _rent_waterfall_for_units(db, account_id, units, date_from, date_to)

    by_property: dict[str, list[dict]] = {}
    property_names: dict[str, str] = {}
    for u in units:
        by_property.setdefault(u["property_id"], []).append(by_unit[u["unit_id"]])
        property_names[u["property_id"]] = u["property_name"]
    properties = [
        {
            "property_id": pid, "property_name": property_names[pid], "unit_count": len(comps),
            **_sum_waterfall(comps),
        }
        for pid, comps in by_property.items()
    ]

    single_components = _single_asset_property_components(
        db, account_id, where, params, date_from, date_to
    )
    properties += [
        {
            "property_id": c["property_id"], "property_name": c["property_name"], "unit_count": 0,
            **_sum_waterfall([c]),
        }
        for c in single_components
    ]
    properties.sort(key=lambda r: r["property_name"])

    return {
        "period_from": date_from, "period_to": date_to, "unit_count": len(units),
        **_sum_waterfall(list(by_unit.values()) + single_components),
        "properties": properties,
    }
