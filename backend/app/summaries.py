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

from app.percent_lines import apply_percentage_lines


def refresh_month(db: Session, property_id: str, month: date) -> None:
    """Settle derived line amounts, then recompute the property-month and portfolio-month
    summaries.

    Caller owns the transaction (commit happens with the surrounding write). Refresh
    the property row first, then the portfolio row that rolls it up — handled inside
    ``refresh_month_summaries``.

    The percentage pass (migration 0023) runs FIRST and deliberately lives here rather than
    in each caller: a rate-stated line — "management: 8% of rent" — depends on rent that may
    be entered on a *different* record of the same property-month (unit rents versus the
    property-tier fee) and edited long afterwards. Every write path in the app already calls
    this function as its last step before commit, so hanging the recompute here is what
    guarantees the fee follows rent no matter which door the edit came in through — manual
    entry, import, a shared-expense posting, or a category reclassification. It writes only
    when a figure is actually stale, so this stays a no-op on unchanged months.
    """
    apply_percentage_lines(db, property_id, month)
    db.execute(
        text("SELECT refresh_month_summaries(:property_id, :month)"),
        {"property_id": property_id, "month": month.replace(day=1)},
    )


def rebuild_all(db: Session) -> None:
    """Full rebuild of every summary row from raw line items (seed / backfill).

    Pure SQL, and therefore does NOT re-derive rate-stated amounts — it rebuilds the
    summaries from whatever the line items currently say. That is the right split: this is a
    backfill of derived *aggregates*, and the line items it reads are the source of truth.
    """
    db.execute(text("SELECT rebuild_all_summaries()"))
    db.commit()
