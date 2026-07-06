"""attention_settings: per-account (single-row) configurable attention thresholds

Why (INSIGHT_DASHBOARD_SPEC sub-step 6):
    The attention feed's floors (NOI-drop %/$, expense-spike %/$, and their unit-grain
    counterparts) should be settings, not hardcoded. Multi-tenancy is out of scope for this
    phase, so "per account" = one deployment-wide row. The attention engine reads this row
    and falls back to the config defaults if it is somehow absent.

Revision ID: 0005_attention_settings
Revises: 0004_unit_rollup
Create Date: 2026-06-30

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0005_attention_settings"
down_revision: Union[str, None] = "0004_unit_rollup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE attention_settings (
            id                            SMALLINT PRIMARY KEY DEFAULT 1
                                          CHECK (id = 1),  -- singleton guard
            noi_drop_min_abs              NUMERIC(14, 2) NOT NULL DEFAULT 40000,
            noi_drop_min_pct              NUMERIC(6, 2)  NOT NULL DEFAULT 15,
            expense_spike_min_abs         NUMERIC(14, 2) NOT NULL DEFAULT 20000,
            expense_spike_min_pct         NUMERIC(6, 2)  NOT NULL DEFAULT 100,
            unit_noi_drop_min_abs         NUMERIC(14, 2) NOT NULL DEFAULT 500,
            unit_noi_drop_min_pct         NUMERIC(6, 2)  NOT NULL DEFAULT 15,
            unit_expense_spike_min_abs    NUMERIC(14, 2) NOT NULL DEFAULT 1000,
            unit_expense_spike_min_pct    NUMERIC(6, 2)  NOT NULL DEFAULT 100,
            updated_at                    TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    # Seed the singleton row with the defaults.
    op.execute("INSERT INTO attention_settings (id) VALUES (1);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS attention_settings;")
