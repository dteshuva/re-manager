"""category-grain rollup: property_category_month_summary

Why (INSIGHT_DASHBOARD_SPEC sub-step 3, expense-spike detector):
    The attention feed must surface a *single category* materially above its own
    trailing-3-month average ("Repairs & Maintenance up $30,000"). The property-month
    rollup from 0002 only carries aggregate operating_expenses, which can't name the
    category. This adds a per-(property, month, category) operating/expense rollup so the
    expense-spike detector is still computed from a rollup, not from raw line items at
    request time.

    Derived from ``v_line_item_resolved`` (the effective-classification view), summing a
    category across BOTH unit rows and the property-tier row, so a property-tier repair and
    unit-level repairs collapse into one "Repairs & Maintenance" figure for the property.

Revision ID: 0003_category_rollup
Revises: 0002_summary_layer
Create Date: 2026-06-30

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003_category_rollup"
down_revision: Union[str, None] = "0002_summary_layer"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE property_category_month_summary (
            property_id    UUID NOT NULL REFERENCES properties (id) ON DELETE CASCADE,
            month          DATE NOT NULL,
            category_id    UUID NOT NULL REFERENCES categories (id),
            classification classification NOT NULL,
            amount         NUMERIC(14, 2) NOT NULL DEFAULT 0,
            refreshed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (property_id, month, category_id, classification),
            CONSTRAINT pcms_month_first_check CHECK (date_trunc('month', month) = month)
        );
        """
    )
    op.execute(
        "CREATE INDEX pcms_property_month_idx "
        "ON property_category_month_summary (property_id, month);"
    )
    # Expense-spike detector scans (month, classification='operating') across properties.
    op.execute(
        "CREATE INDEX pcms_month_class_idx "
        "ON property_category_month_summary (month, classification);"
    )

    # Refresh one property-month's category rows. DELETE+INSERT (not upsert) because the
    # set of categories present can change month to month.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION refresh_property_category_month_summary(
            p_property_id UUID, p_month DATE
        ) RETURNS void
        LANGUAGE plpgsql AS $$
        BEGIN
            DELETE FROM property_category_month_summary
            WHERE property_id = p_property_id AND month = p_month;

            INSERT INTO property_category_month_summary
                (property_id, month, category_id, classification, amount, refreshed_at)
            SELECT property_id, month, category_id, classification, SUM(amount), now()
            FROM v_line_item_resolved
            WHERE property_id = p_property_id AND month = p_month
            GROUP BY property_id, month, category_id, classification;
        END;
        $$;
        """
    )

    # Fold the category refresh into the on-post wrapper (property -> category -> portfolio).
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

    op.execute("SELECT rebuild_all_summaries();")


def downgrade() -> None:
    # Restore the 0002 versions of the shared functions (without the category step).
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
    op.execute("DROP FUNCTION IF EXISTS refresh_property_category_month_summary(UUID, DATE);")
    op.execute("DROP TABLE IF EXISTS property_category_month_summary;")
