"""flat annual budget per property, for actual-vs-plan variance

Why:
    The dashboard/attention feed can say "NOI fell 58%" but not "NOI is 12% below plan" — the
    single highest-priority gap from the analyst field report. This adds a minimal, honest
    parallel table: one row per (property, year) with an annual budgeted gross rent and
    operating-expense figure. Budgeted NOI (rent - opex) and the monthly/pro-rated plan are
    always computed on read, never stored, matching the actuals convention. This migration
    does NOT touch monthly_records/line_items or any actuals math.

Revision ID: 0009_property_budget
Revises: 0008_property_investment
Create Date: 2026-07-09

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0009_property_budget"
down_revision: Union[str, None] = "0008_property_investment"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE property_budget (
            property_id                 UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
            year                         INT NOT NULL,
            budgeted_gross_rent          NUMERIC(14, 2) NOT NULL,
            budgeted_operating_expenses  NUMERIC(14, 2) NOT NULL,
            created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (property_id, year),
            CONSTRAINT property_budget_year_range CHECK (year BETWEEN 2000 AND 2100),
            CONSTRAINT property_budget_rent_nonneg CHECK (budgeted_gross_rent >= 0),
            CONSTRAINT property_budget_opex_nonneg CHECK (budgeted_operating_expenses >= 0)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE property_budget;")
