"""unit-grain rollup: unit_month_summary

Why (INSIGHT_DASHBOARD_SPEC sub-step 4):
    Property detail needs a unit roster (unit #, status, rent, NOI, cash flow, change vs
    prior) that is server-sortable/paginated, and a property-scoped attention feed at unit
    grain (which units dropped / went vacant). Both must be fast and rollup-based rather
    than scanning raw line items. This unit-month rollup mirrors property_month_summary at
    unit grain.

    Derived from ``v_monthly_pnl`` (unit rows only: unit_id IS NOT NULL). A unit with no
    record for a month gets no row (= vacant that month). Property-tier-only items are not
    here by design (they live at the property tier, not any unit).

Revision ID: 0004_unit_rollup
Revises: 0003_category_rollup
Create Date: 2026-06-30

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0004_unit_rollup"
down_revision: Union[str, None] = "0003_category_rollup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_METRICS = (
    "gross_rent", "operating_expenses", "noi", "capex",
    "debt_service", "other_below_line", "below_noi", "cash_flow",
)


def upgrade() -> None:
    money_cols = ",\n            ".join(f"{m} NUMERIC(14, 2) NOT NULL DEFAULT 0" for m in _METRICS)
    op.execute(
        f"""
        CREATE TABLE unit_month_summary (
            unit_id      UUID NOT NULL REFERENCES units (id) ON DELETE CASCADE,
            month        DATE NOT NULL,
            property_id  UUID NOT NULL REFERENCES properties (id) ON DELETE CASCADE,
            {money_cols},
            refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (unit_id, month),
            CONSTRAINT unit_month_summary_month_first_check
                CHECK (date_trunc('month', month) = month)
        );
        """
    )
    # A property's roster for a month, and its prior month, are indexed reads.
    op.execute(
        "CREATE INDEX unit_month_summary_property_month_idx "
        "ON unit_month_summary (property_id, month);"
    )

    sum_metrics = ",\n            ".join(f"COALESCE(SUM(v.{m}), 0)" for m in _METRICS)
    # Refresh all of a property's unit rows for a month. DELETE+INSERT because the set of
    # units with data can change (a unit going vacant should lose its row for that month).
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION refresh_unit_month_summary(
            p_property_id UUID, p_month DATE
        ) RETURNS void
        LANGUAGE plpgsql AS $$
        BEGIN
            DELETE FROM unit_month_summary
            WHERE property_id = p_property_id AND month = p_month;

            INSERT INTO unit_month_summary
                (unit_id, month, property_id, {", ".join(_METRICS)}, refreshed_at)
            SELECT v.unit_id, v.month, v.property_id, {sum_metrics}, now()
            FROM v_monthly_pnl v
            WHERE v.property_id = p_property_id AND v.month = p_month
              AND v.unit_id IS NOT NULL
            GROUP BY v.unit_id, v.month, v.property_id;
        END;
        $$;
        """
    )

    # Fold into the on-post wrapper.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION refresh_month_summaries(
            p_property_id UUID, p_month DATE
        ) RETURNS void
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM refresh_property_month_summary(p_property_id, p_month);
            PERFORM refresh_property_category_month_summary(p_property_id, p_month);
            PERFORM refresh_unit_month_summary(p_property_id, p_month);
            PERFORM refresh_portfolio_month_summary(p_month);
        END;
        $$;
        """
    )

    # And into the full rebuild.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION rebuild_all_summaries()
        RETURNS void
        LANGUAGE plpgsql AS $$
        DECLARE r RECORD;
        BEGIN
            TRUNCATE property_month_summary;
            TRUNCATE portfolio_month_summary;
            TRUNCATE property_category_month_summary;
            TRUNCATE unit_month_summary;
            FOR r IN SELECT DISTINCT property_id, month FROM monthly_records LOOP
                PERFORM refresh_property_month_summary(r.property_id, r.month);
                PERFORM refresh_property_category_month_summary(r.property_id, r.month);
                PERFORM refresh_unit_month_summary(r.property_id, r.month);
            END LOOP;
            FOR r IN SELECT DISTINCT month FROM monthly_records LOOP
                PERFORM refresh_portfolio_month_summary(r.month);
            END LOOP;
        END;
        $$;
        """
    )

    op.execute("SELECT rebuild_all_summaries();")


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION refresh_month_summaries(
            p_property_id UUID, p_month DATE
        ) RETURNS void
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM refresh_property_month_summary(p_property_id, p_month);
            PERFORM refresh_property_category_month_summary(p_property_id, p_month);
            PERFORM refresh_portfolio_month_summary(p_month);
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION rebuild_all_summaries()
        RETURNS void
        LANGUAGE plpgsql AS $$
        DECLARE r RECORD;
        BEGIN
            TRUNCATE property_month_summary;
            TRUNCATE portfolio_month_summary;
            TRUNCATE property_category_month_summary;
            FOR r IN SELECT DISTINCT property_id, month FROM monthly_records LOOP
                PERFORM refresh_property_month_summary(r.property_id, r.month);
                PERFORM refresh_property_category_month_summary(r.property_id, r.month);
            END LOOP;
            FOR r IN SELECT DISTINCT month FROM monthly_records LOOP
                PERFORM refresh_portfolio_month_summary(r.month);
            END LOOP;
        END;
        $$;
        """
    )
    op.execute("DROP FUNCTION IF EXISTS refresh_unit_month_summary(UUID, DATE);")
    op.execute("DROP TABLE IF EXISTS unit_month_summary;")
