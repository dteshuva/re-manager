"""portfolio segmentation: property_tag (flexible property tagging)

Why:
    Properties today only carry name/type/address — there's no way to slice the portfolio
    by region, fund/entity, or asset class (analyst High-priority gap). This adds a minimal
    parallel join table: a property can carry any number of free-text tags (e.g.
    "Northeast", "Fund II", "multifamily-value-add"). Tags are purely a filter/segmentation
    axis — they never touch monthly_records/line_items or any actuals math.

Revision ID: 0010_property_tag
Revises: 0009_property_budget
Create Date: 2026-07-09

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0010_property_tag"
down_revision: Union[str, None] = "0009_property_budget"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE property_tag (
            property_id  UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
            tag          TEXT NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (property_id, tag),
            CONSTRAINT property_tag_tag_nonempty CHECK (length(trim(tag)) > 0)
        );
        """
    )
    # Portfolio filtering queries look up "which properties carry tag X" (the join direction
    # opposite the PK's leading column), so a plain index on tag alone is needed too.
    op.execute("CREATE INDEX property_tag_tag_idx ON property_tag (tag);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS property_tag;")
