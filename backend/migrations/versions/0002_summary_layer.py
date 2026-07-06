"""rollup/summary layer: pre-aggregated property-month and portfolio-month summaries

Why this exists (INSIGHT_DASHBOARD_SPEC sub-step 1):
    Dashboards must be instant at scale (thousands of units x many months). Naive
    per-request aggregation over line_items is too slow for a landing page that scans
    the whole portfolio. So we precompute monthly rollups into two summary TABLES
    (not a single materialized view) because the refresh contract is *incremental*:
    a summary is refreshed when one property-month is posted/locked, not by rebuilding
    everything.

Source-of-truth & reconciliation:
    Raw line_items remain the source of truth. The summaries are DERIVED entirely from
    ``v_monthly_pnl`` (the same computed P&L view the live endpoints use), so they
    reconcile EXACTLY with the line-item math: NOI = rent - operating; cash flow =
    NOI - below-line. Because the summary reads the view, a category reclassification
    is picked up on the next refresh with no schema change.

Rollup honesty:
    property_month_summary aggregates ALL of a property's v_monthly_pnl rows for the
    month -- unit rows AND the property-tier row (unit_id IS NULL). Property-tier-only
    items (shared capex, debt service) are summed into the property total but are NOT
    allocated to any unit. Occupancy is unit-scoped: occupied_units counts units with
    rent > 0; total_units is the property's unit roster (0 for single-asset).

Revision ID: 0002_summary_layer
Revises: 0001_initial
Create Date: 2026-06-30

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002_summary_layer"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The eight financial metrics carried verbatim from v_monthly_pnl. Kept in one place
# so the table columns and the refresh SUMs cannot drift apart.
_METRICS = (
    "gross_rent",
    "operating_expenses",
    "noi",
    "capex",
    "debt_service",
    "other_below_line",
    "below_noi",
    "cash_flow",
)


def upgrade() -> None:
    money_cols = ",\n            ".join(
        f"{m} NUMERIC(14, 2) NOT NULL DEFAULT 0" for m in _METRICS
    )

    # --- property_month_summary -------------------------------------------
    op.execute(
        f"""
        CREATE TABLE property_month_summary (
            property_id    UUID NOT NULL REFERENCES properties (id) ON DELETE CASCADE,
            month          DATE NOT NULL,
            {money_cols},
            occupied_units INTEGER NOT NULL DEFAULT 0,
            total_units    INTEGER NOT NULL DEFAULT 0,
            -- fraction 0..1; NULL for single-asset properties (no unit roster).
            occupancy      NUMERIC(6, 5),
            refreshed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (property_id, month),
            CONSTRAINT property_month_summary_month_first_check
                CHECK (date_trunc('month', month) = month)
        );
        """
    )
    # "All properties for month M" (the portfolio composition table) hits month alone.
    op.execute(
        "CREATE INDEX property_month_summary_month_idx "
        "ON property_month_summary (month);"
    )

    # --- portfolio_month_summary ------------------------------------------
    op.execute(
        f"""
        CREATE TABLE portfolio_month_summary (
            month          DATE PRIMARY KEY,
            {money_cols},
            occupied_units INTEGER NOT NULL DEFAULT 0,
            total_units    INTEGER NOT NULL DEFAULT 0,
            occupancy      NUMERIC(6, 5),
            property_count INTEGER NOT NULL DEFAULT 0,
            refreshed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT portfolio_month_summary_month_first_check
                CHECK (date_trunc('month', month) = month)
        );
        """
    )

    # ----------------------------------------------------------------------
    #  Refresh functions. These are the ONLY writers of the summary tables.
    # ----------------------------------------------------------------------

    sum_metrics = ",\n            ".join(f"COALESCE(SUM(v.{m}), 0)" for m in _METRICS)
    upsert_metrics = ",\n            ".join(f"{m} = EXCLUDED.{m}" for m in _METRICS)

    # Refresh exactly one property-month from v_monthly_pnl. Always writes a row
    # (zeros when the month has no data) so the dashboard can tell "posted but empty"
    # apart from "never refreshed". occupied_units / total_units are unit-scoped.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION refresh_property_month_summary(
            p_property_id UUID, p_month DATE
        ) RETURNS void
        LANGUAGE plpgsql AS $$
        DECLARE
            v_total_units INTEGER;
            v_occupied    INTEGER;
        BEGIN
            SELECT count(*) INTO v_total_units
            FROM units WHERE property_id = p_property_id;

            SELECT count(DISTINCT v.unit_id) INTO v_occupied
            FROM v_monthly_pnl v
            WHERE v.property_id = p_property_id
              AND v.month = p_month
              AND v.unit_id IS NOT NULL
              AND v.gross_rent > 0;

            INSERT INTO property_month_summary AS s (
                property_id, month, {", ".join(_METRICS)},
                occupied_units, total_units, occupancy, refreshed_at
            )
            SELECT
                p_property_id, p_month,
                {sum_metrics},
                v_occupied,
                v_total_units,
                CASE WHEN v_total_units > 0
                     THEN ROUND(v_occupied::numeric / v_total_units, 5)
                     ELSE NULL END,
                now()
            FROM v_monthly_pnl v
            WHERE v.property_id = p_property_id AND v.month = p_month
            ON CONFLICT (property_id, month) DO UPDATE SET
                {upsert_metrics},
                occupied_units = EXCLUDED.occupied_units,
                total_units    = EXCLUDED.total_units,
                occupancy      = EXCLUDED.occupancy,
                refreshed_at   = EXCLUDED.refreshed_at;
        END;
        $$;
        """
    )

    portfolio_sum = ",\n            ".join(f"COALESCE(SUM({m}), 0)" for m in _METRICS)

    # Roll the portfolio-month up from the already-refreshed property rows, so it can
    # never disagree with the property tier. Call AFTER refreshing the property row.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION refresh_portfolio_month_summary(p_month DATE)
        RETURNS void
        LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO portfolio_month_summary AS s (
                month, {", ".join(_METRICS)},
                occupied_units, total_units, occupancy, property_count, refreshed_at
            )
            SELECT
                p_month,
                {portfolio_sum},
                COALESCE(SUM(occupied_units), 0),
                COALESCE(SUM(total_units), 0),
                CASE WHEN COALESCE(SUM(total_units), 0) > 0
                     THEN ROUND(SUM(occupied_units)::numeric / SUM(total_units), 5)
                     ELSE NULL END,
                count(*),
                now()
            FROM property_month_summary
            WHERE month = p_month
            ON CONFLICT (month) DO UPDATE SET
                {upsert_metrics},
                occupied_units = EXCLUDED.occupied_units,
                total_units    = EXCLUDED.total_units,
                occupancy      = EXCLUDED.occupancy,
                property_count = EXCLUDED.property_count,
                refreshed_at   = EXCLUDED.refreshed_at;
        END;
        $$;
        """
    )

    # Convenience wrapper the app calls on post/lock: refresh the property-month, then
    # the portfolio-month that contains it (order matters; portfolio reads property).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION refresh_month_summaries(
            p_property_id UUID, p_month DATE
        ) RETURNS void
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM refresh_property_month_summary(p_property_id, p_month);
            PERFORM refresh_portfolio_month_summary(p_month);
        END;
        $$;
        """
    )

    # Full rebuild for seeding / backfill / disaster recovery. Drives off the real
    # (property, month) pairs that have records, so it creates no spurious rows.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION rebuild_all_summaries()
        RETURNS void
        LANGUAGE plpgsql AS $$
        DECLARE r RECORD;
        BEGIN
            TRUNCATE property_month_summary;
            TRUNCATE portfolio_month_summary;
            FOR r IN SELECT DISTINCT property_id, month FROM monthly_records LOOP
                PERFORM refresh_property_month_summary(r.property_id, r.month);
            END LOOP;
            FOR r IN SELECT DISTINCT month FROM monthly_records LOOP
                PERFORM refresh_portfolio_month_summary(r.month);
            END LOOP;
        END;
        $$;
        """
    )

    # Populate from whatever data already exists (the seed).
    op.execute("SELECT rebuild_all_summaries();")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS rebuild_all_summaries();")
    op.execute("DROP FUNCTION IF EXISTS refresh_month_summaries(UUID, DATE);")
    op.execute("DROP FUNCTION IF EXISTS refresh_portfolio_month_summary(DATE);")
    op.execute("DROP FUNCTION IF EXISTS refresh_property_month_summary(UUID, DATE);")
    op.execute("DROP TABLE IF EXISTS portfolio_month_summary;")
    op.execute("DROP TABLE IF EXISTS property_month_summary;")
