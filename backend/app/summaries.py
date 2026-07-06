"""Summary-layer refresh helpers (INSIGHT_DASHBOARD_SPEC sub-step 1).

The actual aggregation lives in SQL (the ``refresh_*`` / ``rebuild_all_summaries``
functions from migration 0002) so the math is defined once, next to the data, and is
identical whether invoked from the app, a migration, or psql. These thin wrappers are
the seam the app calls when a property-month is posted or locked.

The summaries are DERIVED from ``v_monthly_pnl``; raw line items remain the source of
truth. A refresh is idempotent: calling it again recomputes the same row.
"""

from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session


def refresh_month(db: Session, property_id: str, month: date) -> None:
    """Recompute the summary for one property-month and its portfolio-month.

    Caller owns the transaction (commit happens with the surrounding write). Refresh
    the property row first, then the portfolio row that rolls it up — handled inside
    ``refresh_month_summaries``.
    """
    db.execute(
        text("SELECT refresh_month_summaries(:property_id, :month)"),
        {"property_id": property_id, "month": month.replace(day=1)},
    )


def rebuild_all(db: Session) -> None:
    """Full rebuild of every summary row from raw line items (seed / backfill)."""
    db.execute(text("SELECT rebuild_all_summaries()"))
    db.commit()
