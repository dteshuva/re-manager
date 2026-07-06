"""add an absolute (level-based) high-vacancy attention trigger

Why:
    The existing vacancy trigger is a CHANGE detector — it only fires when occupancy DROPS
    month-over-month. A property that is already sitting at, say, 45% vacant and simply stays
    there generates no alert, even though it's a standing problem. This adds a size-independent
    ABSOLUTE trigger: flag any property whose vacancy rate (100 - occupancy%) is at or above the
    configured threshold, regardless of change. Per-account configurable; default 20%.

Revision ID: 0007_absolute_vacancy
Revises: 0006_relative_thresholds
Create Date: 2026-07-03

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0007_absolute_vacancy"
down_revision: Union[str, None] = "0006_relative_thresholds"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE attention_settings "
        "ADD COLUMN vacancy_high_absolute_pct NUMERIC(6, 2) NOT NULL DEFAULT 20;"
    )
    op.execute("UPDATE attention_settings SET vacancy_high_absolute_pct = 20 WHERE id = 1;")


def downgrade() -> None:
    op.execute("ALTER TABLE attention_settings DROP COLUMN vacancy_high_absolute_pct;")
