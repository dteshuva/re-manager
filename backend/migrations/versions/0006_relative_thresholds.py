"""make attention triggers relative (size-independent), not absolute-dollar

Why:
    An absolute $ floor is size-dependent: a $40k NOI-drop floor is trivial noise for a
    100-unit property but hides a catastrophic 50% collapse at a 40-unit one. Materiality
    scales with property size, so the PERCENTAGE is the right primary trigger. This turns
    the absolute-$ floors into an optional "materiality gate" that defaults to 0 (pure
    percentage), and adds a size-independent vacancy trigger (occupancy percentage-point
    drop) in place of "any unit lost".

    Percentages are unchanged (NOI drop 15%, expense spike 100%, unit variants 15%/100%);
    only the $ gates drop to 0. All remain per-account configurable.

Revision ID: 0006_relative_thresholds
Revises: 0005_attention_settings
Create Date: 2026-07-01

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0006_relative_thresholds"
down_revision: Union[str, None] = "0005_attention_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ABS_COLS = (
    "noi_drop_min_abs", "expense_spike_min_abs",
    "unit_noi_drop_min_abs", "unit_expense_spike_min_abs",
)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE attention_settings "
        "ADD COLUMN vacancy_min_occupancy_drop_pct NUMERIC(6, 2) NOT NULL DEFAULT 2;"
    )
    # $ floors become an optional materiality gate, default 0 (percentage is the trigger).
    for col in _ABS_COLS:
        op.execute(f"ALTER TABLE attention_settings ALTER COLUMN {col} SET DEFAULT 0;")
    op.execute(
        """
        UPDATE attention_settings SET
            noi_drop_min_abs = 0,
            expense_spike_min_abs = 0,
            unit_noi_drop_min_abs = 0,
            unit_expense_spike_min_abs = 0,
            vacancy_min_occupancy_drop_pct = 2
        WHERE id = 1;
        """
    )


def downgrade() -> None:
    old = {
        "noi_drop_min_abs": 40000, "expense_spike_min_abs": 20000,
        "unit_noi_drop_min_abs": 500, "unit_expense_spike_min_abs": 1000,
    }
    for col, val in old.items():
        op.execute(f"ALTER TABLE attention_settings ALTER COLUMN {col} SET DEFAULT {val};")
        op.execute(f"UPDATE attention_settings SET {col} = {val} WHERE id = 1;")
    op.execute("ALTER TABLE attention_settings DROP COLUMN vacancy_min_occupancy_drop_pct;")
