"""lease.concession_monthly: split concessions out of the waterfall's collections_loss line

Why:
    The rent waterfall's ``collections_loss`` line (GPR -> loss-to-lease -> vacancy loss ->
    collections loss -> actual collected, migration 0015) combines two economically
    different things into one opaque number: concessions (free/discounted rent granted to
    a tenant, a leasing DECISION) and bad debt (delinquency — rent owed but not paid, a
    COLLECTIONS problem). An analyst reading "collections loss: $99k" can't tell how much
    of that is "we gave a concession" vs. "a tenant stopped paying" — this follow-up
    (waterfall-followups.md item 3) splits them.

    Modeling choice (documented, per the task's suggested options: `concession_months`
    (int, free-rent months at move-in) vs. `concession_monthly` (a standing $/month
    discount) — picked the latter): a one-time "N free months at move-in" concession would
    almost always land BEFORE this app's fixed 2024-2025 actuals window (every real lease's
    `start_date` is anchored Oct-Dec 2023 — see `scripts/reanchor_lease_timeline.py`'s
    docstring — deliberately close to, but before, the window so escalation math stays
    hand-computable), so it would never actually show up as non-zero in any waterfall query
    scoped to 2024-2025. `concession_monthly` instead models a STANDING negotiated rent
    discount that keeps applying for as long as the lease is in force — e.g. a retention
    concession, or a below-market rent granted at signing that never got re-papered. It
    reliably produces a non-zero, demonstrable `concessions` line for any period a
    concession-bearing lease is in force during, not just a narrow month-of-move-in window.

    Reference data, same invariant as `contract_rent`/`security_deposit`/`escalation_pct`
    before it: NEVER feeds NOI/cash-flow (which stays driven solely by
    monthly_records/line_items) or the ACTUAL rent collected (`unit_month_summary.
    gross_rent`, which this migration does not touch) — it only affects how
    `app.queries._unit_waterfall` DECOMPOSES the pre-existing, already-reconciled
    `collections_loss` (`scheduled - actual`) into a `concessions` piece (capped, per
    occupied month, at that month's own shortfall — never manufacturing a loss that wasn't
    already there) and a `bad_debt` remainder (`collections_loss - concessions`). The
    portfolio/property `actual_collected` total is UNCHANGED by this migration or the
    query logic built on top of it.

    Nullable (NULL/0 = no standing concession, the overwhelming majority of leases) so
    existing rows are unaffected; a handful of leases get a non-zero value via
    `scripts/backfill_lease_concessions.py` (idempotent, live-DB) and via `app/seed.py`
    (fresh installs) for demo realism.

Revision ID: 0016_lease_concession
Revises: 0015_unit_market_rent
Create Date: 2026-07-21

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0016_lease_concession"
down_revision: Union[str, None] = "0015_unit_market_rent"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE lease ADD COLUMN concession_monthly NUMERIC(14, 2);")
    op.execute(
        "ALTER TABLE lease ADD CONSTRAINT lease_concession_monthly_nonneg "
        "CHECK (concession_monthly IS NULL OR concession_monthly >= 0);"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE lease DROP COLUMN concession_monthly;")
