"""account-level display currency (USD / GBP)

Why:
    The app hard-codes ``$`` everywhere (frontend ``fmtCurrency``, the attention-feed
    labels). A UK landlord wants figures shown in ``£``. Currency is a pure DISPLAY concern:
    the stored numbers never change, so this is a per-account symbol/locale toggle, not a
    conversion. Kept at the ACCOUNT tier (not per-property) so the portfolio dashboard's
    consolidated P&L stays summable — you cannot add £ and $ into one NOI figure.

Revision ID: 0019_account_currency
Revises: 0018_shell_units_excluded_from_occupancy
Create Date: 2026-07-24

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0019_account_currency"
down_revision: Union[str, None] = "0018_shell_occupancy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE accounts
            ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD',
            ADD CONSTRAINT accounts_currency_check CHECK (currency IN ('USD', 'GBP'));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE accounts
            DROP CONSTRAINT IF EXISTS accounts_currency_check,
            DROP COLUMN IF EXISTS currency;
        """
    )
