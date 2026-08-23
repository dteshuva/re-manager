"""lease table: rent-roll / lease-level data (tenant, term, contract rent, status)

Why:
    The analyst report's top remaining gap: "no way to see upcoming rollover risk, no
    distinction between a vacant unit and a unit that's simply missing data." Today
    occupancy is inferred purely from whether a monthly_record exists for a unit-month
    (see 0011_unit_vacancy_flag). That tells you WHETHER rent was collected, but nothing
    about the underlying tenancy: who the tenant is, what they're contracted to pay, when
    the lease ends, or whether a unit sitting without a recent record is actually vacant
    (no lease) vs. simply missing a posted statement (an active lease, no record yet).

    This adds a NEW parallel table, ``lease``, keeping full history: a unit can have many
    leases over time (one row per tenancy), and the unit's CURRENT lease is resolved at
    query time (see app/queries.py rent-roll helpers) as the lease covering today, or —
    absent one — the most recent lease on file. ``contract_rent`` is reference data (what
    the lease says is owed); it is NOT a replacement for the actual recorded gross rent in
    monthly_records/line_items, which stays the sole input to NOI/cash-flow. This migration
    does not touch monthly_records, line_items, or any actuals math.

    ``status`` is the lease's own authoritative state (active / notice / expired / vacant),
    distinct from (but meant to agree with) the unit-month "occupied/vacant/missing" status
    derived from monthly_records — see the rent-roll endpoints for how the two are
    reconciled: the rent roll treats ``lease.status`` as authoritative for "is this unit
    occupied," and separately flags "missing_data" when an active lease has no monthly
    record posted for the latest month (so a real rollover doesn't get mislabeled a data
    gap, and vice versa).

Revision ID: 0012_lease
Revises: 0011_unit_vacancy_flag
Create Date: 2026-07-13

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0012_lease"
down_revision: Union[str, None] = "0011_unit_vacancy_flag"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE lease (
            id             UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
            unit_id        UUID NOT NULL REFERENCES units(id) ON DELETE CASCADE,
            tenant_name    TEXT NOT NULL,
            start_date     DATE NOT NULL,
            end_date       DATE,
            contract_rent  NUMERIC(14, 2) NOT NULL,
            status         TEXT NOT NULL DEFAULT 'active',
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT lease_status_check CHECK (status IN ('active', 'notice', 'expired', 'vacant')),
            CONSTRAINT lease_contract_rent_nonneg CHECK (contract_rent >= 0),
            CONSTRAINT lease_end_after_start CHECK (end_date IS NULL OR end_date >= start_date),
            CONSTRAINT lease_tenant_name_nonempty CHECK (length(trim(tenant_name)) > 0)
        );
        """
    )
    # Rent-roll/expiration queries always filter/sort by unit and by end_date; the composite
    # index also serves the "most recent lease per unit" DISTINCT ON lookup.
    op.execute("CREATE INDEX lease_unit_start_idx ON lease (unit_id, start_date DESC);")
    op.execute("CREATE INDEX lease_end_date_idx ON lease (end_date);")


def downgrade() -> None:
    op.execute("DROP TABLE lease;")
