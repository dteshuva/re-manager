"""lease table v2 fields: deposit, escalation, MTM/holdover typing, percentage rent (retail)

Why:
    The analyst report's "standard rent-roll fields still missing" list, deferred after the
    v1 rent roll (migration 0012) shipped: lease escalations/bumps, security deposits, a
    clean MTM-vs-fixed-term type (so holdover — a lapsed lease whose tenant is still
    physically in place — can be told apart from a clean active lease and from a true
    vacancy), and percentage-rent terms for the retail asset (Cedar Plaza Retail).

    All six new columns are reference/terms data, same invariant as ``contract_rent``:
    none of them feed NOI/cash-flow, which stays driven solely by monthly_records/line_items.

    - ``security_deposit``: what the lease collected upfront. Nullable (older/legacy rows
      may not have one on file).
    - ``escalation_pct`` / ``escalation_frequency_months``: an annual (or other cadence)
      contract-rent bump. ``escalation_frequency_months`` defaults to 12 (the common case)
      so a lease with a bump but an unspecified cadence still has enough to compute a next
      scheduled-bump date.
    - ``lease_type``: 'fixed' | 'mtm', kept CONSISTENT with the pre-existing convention
      (NULL ``end_date`` = month-to-month) rather than as an independent, driftable fact —
      backfilled here from that same rule for every existing row.
    - ``pct_rent_rate`` / ``pct_rent_breakpoint``: percentage-rent terms (retail leases
      only) — annual-sales breakpoint and the overage rate above it. NULL for every
      residential lease; populated only for Cedar Plaza Retail's lease by the backfill
      script (``scripts/backfill_lease_v2_fields.py``), same as every other new column here.

Revision ID: 0013_lease_v2_fields
Revises: 0012_lease
Create Date: 2026-07-16

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0013_lease_v2_fields"
down_revision: Union[str, None] = "0012_lease"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE lease
            ADD COLUMN security_deposit NUMERIC(14, 2),
            ADD COLUMN escalation_pct NUMERIC(5, 2),
            ADD COLUMN escalation_frequency_months INTEGER NOT NULL DEFAULT 12,
            ADD COLUMN lease_type TEXT NOT NULL DEFAULT 'fixed',
            ADD COLUMN pct_rent_rate NUMERIC(5, 2),
            ADD COLUMN pct_rent_breakpoint NUMERIC(14, 2);
        """
    )
    op.execute(
        "ALTER TABLE lease ADD CONSTRAINT lease_security_deposit_nonneg "
        "CHECK (security_deposit IS NULL OR security_deposit >= 0);"
    )
    op.execute(
        "ALTER TABLE lease ADD CONSTRAINT lease_escalation_pct_range "
        "CHECK (escalation_pct IS NULL OR (escalation_pct >= 0 AND escalation_pct <= 100));"
    )
    op.execute(
        "ALTER TABLE lease ADD CONSTRAINT lease_escalation_frequency_pos "
        "CHECK (escalation_frequency_months > 0);"
    )
    op.execute(
        "ALTER TABLE lease ADD CONSTRAINT lease_type_check "
        "CHECK (lease_type IN ('fixed', 'mtm'));"
    )
    op.execute(
        "ALTER TABLE lease ADD CONSTRAINT lease_pct_rent_rate_range "
        "CHECK (pct_rent_rate IS NULL OR (pct_rent_rate >= 0 AND pct_rent_rate <= 100));"
    )
    op.execute(
        "ALTER TABLE lease ADD CONSTRAINT lease_pct_rent_breakpoint_nonneg "
        "CHECK (pct_rent_breakpoint IS NULL OR pct_rent_breakpoint >= 0);"
    )
    # Derive lease_type for every EXISTING row now (not just new inserts going forward),
    # from the same NULL-end_date = MTM convention already in force.
    op.execute(
        "UPDATE lease SET lease_type = CASE WHEN end_date IS NULL THEN 'mtm' ELSE 'fixed' END;"
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE lease
            DROP COLUMN security_deposit,
            DROP COLUMN escalation_pct,
            DROP COLUMN escalation_frequency_months,
            DROP COLUMN lease_type,
            DROP COLUMN pct_rent_rate,
            DROP COLUMN pct_rent_breakpoint;
        """
    )
