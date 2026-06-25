"""Read-only query layer over the P&L views.

All financial aggregation reads ``v_monthly_pnl`` (per property/unit/month grain) and
sums it for the requested scope. NOI and cash flow are computed in SQL, never stored,
and the SQL sums **by classification**, so new categories and reclassifications are
picked up automatically with no code change here.

The full set of scope endpoints (property / unit drill-down) is Phase 2; Phase 1 ships
the portfolio rollup to demonstrate the views end to end.
"""

from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

_METRIC_COLUMNS = """
    COALESCE(SUM(gross_rent), 0)        AS gross_rent,
    COALESCE(SUM(operating_expenses), 0) AS operating_expenses,
    COALESCE(SUM(noi), 0)               AS noi,
    COALESCE(SUM(capex), 0)             AS capex,
    COALESCE(SUM(debt_service), 0)      AS debt_service,
    COALESCE(SUM(other_below_line), 0)  AS other_below_line,
    COALESCE(SUM(below_noi), 0)         AS below_noi,
    COALESCE(SUM(cash_flow), 0)         AS cash_flow
"""


def _rows_to_dicts(result) -> list[dict]:
    return [dict(r._mapping) for r in result]


def portfolio_monthly(
    db: Session, date_from: date | None = None, date_to: date | None = None
) -> list[dict]:
    """Portfolio P&L by month across every property and unit (and property-tier rows)."""
    sql = f"""
        SELECT month, {_METRIC_COLUMNS}
        FROM v_monthly_pnl
        WHERE (:date_from IS NULL OR month >= :date_from)
          AND (:date_to   IS NULL OR month <= :date_to)
        GROUP BY month
        ORDER BY month
    """
    return _rows_to_dicts(
        db.execute(text(sql), {"date_from": date_from, "date_to": date_to})
    )


def property_monthly(
    db: Session, property_id: str, date_from: date | None = None, date_to: date | None = None
) -> list[dict]:
    """Property P&L by month: unit rows + property-tier (unit_id IS NULL) rows combined."""
    sql = f"""
        SELECT month, {_METRIC_COLUMNS}
        FROM v_monthly_pnl
        WHERE property_id = :property_id
          AND (:date_from IS NULL OR month >= :date_from)
          AND (:date_to   IS NULL OR month <= :date_to)
        GROUP BY month
        ORDER BY month
    """
    return _rows_to_dicts(
        db.execute(
            text(sql),
            {"property_id": property_id, "date_from": date_from, "date_to": date_to},
        )
    )
