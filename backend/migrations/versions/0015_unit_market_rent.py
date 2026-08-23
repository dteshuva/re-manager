"""units.market_rent: the asking/market rent used by the rent waterfall's GPR line.

Why:
    The rent-waterfall feature (IC-grade decomposition of GPR -> actual collected rent)
    needs a "what should this unit rent for at market" figure that is independent of the
    unit's in-place lease. ``lease.contract_rent`` already captures what a tenant is
    actually contracted to pay (in-place rent); ``market_rent`` is a NEW, separate
    reference figure on ``units`` — what the SAME unit would rent for today, at market,
    regardless of whether it's currently under a below-market in-place lease (loss-to-lease)
    or sitting vacant (vacancy loss, priced at full market rent).

    Nullable: existing units get a value only via the idempotent backfill script
    (``scripts/backfill_unit_market_rent.py``), not this migration, so the migration itself
    stays a pure schema change (same convention as 0011's ``is_vacant``/0014's ``is_shell``
    — schema here, data backfill in a script or a follow-up self-heal). A NULL
    ``market_rent`` means "no market-rent figure on file yet" and is treated as $0 GPR
    contribution by the waterfall query (excluded from ``gpr_known`` bookkeeping, mirroring
    ``_occupancy_summary``'s "unknown potential rent" handling) rather than silently
    defaulting to the in-place rent, which would hide loss-to-lease.

    Reference-only data, same invariant as ``contract_rent``/``is_shell``: never feeds
    NOI/cash-flow, which stays driven solely by monthly_records/line_items.

Revision ID: 0015_unit_market_rent
Revises: 0014_unit_is_shell
Create Date: 2026-07-17

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0015_unit_market_rent"
down_revision: Union[str, None] = "0014_unit_is_shell"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE units ADD COLUMN market_rent NUMERIC(14, 2);")
    op.execute(
        "ALTER TABLE units ADD CONSTRAINT units_market_rent_nonneg "
        "CHECK (market_rent IS NULL OR market_rent >= 0);"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE units DROP COLUMN market_rent;")
