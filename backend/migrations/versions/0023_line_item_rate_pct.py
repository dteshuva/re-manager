"""line items stated as a percentage of rent (property management fees)

Why:
    A management fee is almost never a fixed figure — it is a rate: "8% of collected rent".
    Entering it as an amount means the operator recomputes it by hand every month, and
    silently carries the stale number forward whenever rent changes mid-year (a renewal, a
    vacancy, a unit let at a different price). The fee then no longer matches the contract,
    and NOI is wrong by however much rent moved.

    ``line_items.rate_pct`` records the RATE the operator actually agreed, and the machine
    derives ``amount`` from the rent in that property-month. ``amount`` stays the single
    source of truth for every downstream reader: the P&L views, the summary rollups, exports,
    variance and attention all keep reading ``line_items.amount`` and need no knowledge that
    a percentage exists. The column is a statement of INTENT ("this figure is derived at this
    rate"), not a second place the money is kept.

    NULL — every pre-existing row — means "a fixed amount, typed by hand or imported", which
    is exactly the old behaviour. Nothing recomputes a NULL-rate line.

    The basis is deliberately rent in the record's own scope: a fee on a unit-tier record is
    a percentage of that unit's rent, one on the property-tier record (where shared costs
    already live) is a percentage of the whole property's rent for the month. See
    :mod:`app.percent_lines` for the basis query and the recompute.

Revision ID: 0023_line_item_rate_pct
Revises: 0022_shared_expense
Create Date: 2026-08-30

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0023_line_item_rate_pct"
down_revision: Union[str, None] = "0022_shared_expense"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NUMERIC(7,4): rates are quoted to a fraction of a percent (8%, 10.75%, 4.5%), and the
    # extra digits keep the derived amount exact rather than pre-rounded. The range check is
    # the honest domain of a fee rate — a negative rate is meaningless, and >100% of rent is
    # a typo (a decimal fraction entered where a percent was meant), not an arrangement.
    op.execute(
        """
        ALTER TABLE line_items
            ADD COLUMN rate_pct NUMERIC(7,4),
            ADD CONSTRAINT line_items_rate_pct_range
                CHECK (rate_pct IS NULL OR (rate_pct >= 0 AND rate_pct <= 100));
        """
    )
    # Partial: percentage lines are a small minority of line_items, and the only query that
    # needs them is "which lines in this property-month must be recomputed?", run on every
    # write to a property-month.
    op.execute(
        "CREATE INDEX line_items_rate_pct_idx "
        "ON line_items (monthly_record_id) WHERE rate_pct IS NOT NULL;"
    )


def downgrade() -> None:
    # Dropping the column leaves the derived amounts in place, which is the correct
    # reversal: the money was really charged, only the "keep this in step with rent"
    # intent goes away.
    op.execute("DROP INDEX IF EXISTS line_items_rate_pct_idx;")
    op.execute(
        "ALTER TABLE line_items "
        "DROP CONSTRAINT IF EXISTS line_items_rate_pct_range, "
        "DROP COLUMN IF EXISTS rate_pct;"
    )
