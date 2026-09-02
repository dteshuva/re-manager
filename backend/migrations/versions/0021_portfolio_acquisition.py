"""portfolio acquisitions: bulk purchases whose costs are allocated across properties

Why:
    A portfolio ("bulk") purchase is one deal covering several properties: each house has its
    own agreed price, but the closing costs are a single settlement figure and the debt is one
    blanket loan over the whole package. The existing per-property acquisition inputs
    (``property_investment``) have nowhere to put a cost that belongs to N properties at once,
    so an operator had to split those totals by hand and re-split them every time the deal was
    amended — the sort of arithmetic that silently stops summing to the real total.

    ``portfolio_acquisition`` stores the deal as the deal: the shared date, the ONE closing-cost
    total and the ONE loan, plus how they should be spread. The split itself is still written
    down onto each member's ``property_investment`` row (via the new ``acquisition_id`` link),
    so every existing metric — cap rate, cash-on-cash, DSCR, the value-weighted portfolio
    aggregates — keeps reading exactly one place and needs no knowledge of bulk deals at all.
    Pro-rata-by-price allocations sum back to the true totals, so the portfolio-level figures
    come out identical to modelling the deal as a single entity.

    ``ON DELETE SET NULL`` on the link is deliberate: deleting a deal UNGROUPS it, leaving each
    property's allocated figures intact. Removing a bulk purchase should never silently wipe
    acquisition data the return metrics depend on.

Revision ID: 0021_portfolio_acquisition
Revises: 0020_property_certificate
Create Date: 2026-08-25

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0021_portfolio_acquisition"
down_revision: Union[str, None] = "0020_property_certificate"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A deal is a top-level entity (it is not owned by any one property), so unlike
    # property_certificate / property_tag it carries its own account_id.
    op.execute(
        """
        CREATE TABLE portfolio_acquisition (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            account_id          UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            name                TEXT NOT NULL,
            purchase_date       DATE NOT NULL,
            total_closing_costs NUMERIC(14,2) NOT NULL DEFAULT 0,
            total_loan_amount   NUMERIC(14,2) NOT NULL DEFAULT 0,
            allocation_method   TEXT NOT NULL DEFAULT 'price',
            notes               TEXT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT portfolio_acquisition_name_nonempty
                CHECK (length(trim(name)) > 0),
            CONSTRAINT portfolio_acquisition_closing_nonneg
                CHECK (total_closing_costs >= 0),
            CONSTRAINT portfolio_acquisition_loan_nonneg
                CHECK (total_loan_amount >= 0),
            CONSTRAINT portfolio_acquisition_method_known
                CHECK (allocation_method IN ('price', 'equal', 'custom'))
        );
        """
    )
    op.execute(
        "CREATE INDEX portfolio_acquisition_account_idx ON portfolio_acquisition (account_id);"
    )

    # Membership lives on the per-property row: a property_investment row IS the allocation,
    # and this column records which deal produced it. NULL = an ordinary standalone purchase,
    # which is what every pre-existing row is.
    op.execute(
        """
        ALTER TABLE property_investment
            ADD COLUMN acquisition_id UUID
                REFERENCES portfolio_acquisition(id) ON DELETE SET NULL;
        """
    )
    op.execute(
        "CREATE INDEX property_investment_acquisition_idx "
        "ON property_investment (acquisition_id);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS property_investment_acquisition_idx;")
    op.execute("ALTER TABLE property_investment DROP COLUMN IF EXISTS acquisition_id;")
    op.execute("DROP TABLE IF EXISTS portfolio_acquisition;")
