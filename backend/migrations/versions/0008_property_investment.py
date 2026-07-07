"""per-property acquisition inputs for investment-return metrics

Why:
    Investment insights (cap rate, cash-on-cash, DSCR) need acquisition facts the P&L does
    not carry: what was paid, the financing, and when. This adds a nullable 1:1 table so a
    property can exist without investment data (metrics simply become unavailable) and the
    core ``properties`` table stays about identity, not economics.

    equity_invested = purchase_price - loan_amount + closing_costs  (computed, never stored)
    is the denominator for cash-on-cash / average cash-on-cash; it is the cash actually put
    in, which matches the levered ``cash_flow`` already stored in property_month_summary.

Revision ID: 0008_property_investment
Revises: 0007_absolute_vacancy
Create Date: 2026-07-07

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0008_property_investment"
down_revision: Union[str, None] = "0007_absolute_vacancy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE property_investment (
            property_id    UUID PRIMARY KEY REFERENCES properties(id) ON DELETE CASCADE,
            purchase_price NUMERIC(14, 2) NOT NULL,
            closing_costs  NUMERIC(14, 2) NOT NULL DEFAULT 0,
            loan_amount    NUMERIC(14, 2) NOT NULL DEFAULT 0,
            purchase_date  DATE NOT NULL,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT property_investment_purchase_price_nonneg CHECK (purchase_price >= 0),
            CONSTRAINT property_investment_closing_costs_nonneg  CHECK (closing_costs >= 0),
            CONSTRAINT property_investment_loan_amount_nonneg     CHECK (loan_amount >= 0)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE property_investment;")
