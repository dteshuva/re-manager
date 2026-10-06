"""lease.last_rent_increase_date / rent_before_increase: price a PERIODIC (rolling) tenancy

Why:
    This app's rent schedule was built around the US fixed-term lease: a ``start_date``, an
    ``end_date``, and a contractual ``escalation_pct`` that bumps the rent every
    ``escalation_frequency_months`` from the start date (migration 0013). An English
    tenancy mostly isn't that. An AST runs a short fixed term and then becomes a STATUTORY
    PERIODIC tenancy — it rolls month to month with no end date at all — and the rent does
    not rise on a contractual percentage. It rises in DISCRETE STEPS, by agreement or by a
    section 13 notice, on a date the landlord has to be able to state (not least because a
    further increase generally can't take effect within 12 months of the last one).

    "No end date" was already representable: ``end_date IS NULL`` ⇒ ``lease_type = 'mtm'``,
    which is the same arrangement under its US name. What was NOT representable is WHEN the
    rent last went up, and what it was before — so a periodic tenancy could only ever be
    priced at one flat figure across all of history, and "when is this tenant next due a
    review?" was unanswerable. These two columns close that gap:

        ``last_rent_increase_date``  the date the CURRENT rent (``contract_rent``) took
                                     effect. NULL = never increased, so the rent has been
                                     ``contract_rent`` since ``start_date`` — which is
                                     exactly today's behaviour, hence every pre-existing
                                     row is unaffected.
        ``rent_before_increase``     what the rent was immediately BEFORE that date.
                                     Nullable: NULL means "an increase happened, we don't
                                     hold the prior figure" (``contract_rent`` is then used
                                     for the earlier months, same as today).

    Why the prior figure matters at all, given this is reference data: arrears
    (migration 0025) is CUMULATIVE. It reaches back through the whole tenancy, so pricing
    pre-increase months at today's higher rent would manufacture historical arrears for a
    tenant who paid in full every month. One prior figure makes the rent exact on both
    sides of the most recent increase, which is as far back as a cumulative balance that
    starts at ``opening_arrears`` ever needs to look.

    How these interact with ``escalation_pct`` (unchanged, still supported): when
    ``last_rent_increase_date`` is set it becomes the ANCHOR of the escalation clock in
    place of ``start_date`` — which is also the correct reading for a contractual
    escalation that was last actually applied on that date. Months before the increase are
    priced FLAT at ``rent_before_increase`` (no escalation back-applied): a periodic
    tenancy's rises are discrete events, not a compounding rate, and inventing a rate
    behind them is precisely the phantom-shortfall mistake the lease-coherence fix in
    app/seed.py already removed once. See ``app.queries._rent_due_for_month``, the single
    place that rule lives.

    Reference data, same invariant as every lease field before it (``contract_rent``,
    ``escalation_pct``, ``concession_monthly``): NEVER feeds NOI/cash-flow, which stays
    driven solely by monthly_records/line_items. ``lease_type`` is deliberately NOT given a
    third value here — a "periodic" tenancy and an "mtm" one are the same arrangement
    (no end date) under two countries' names, and a third enum value that no data could
    distinguish from the second would only invite drift. The UI labels an end-date-less
    lease "periodic" instead.

Revision ID: 0024_lease_rent_increase
Revises: 0023_line_item_rate_pct
Create Date: 2026-10-04

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0024_lease_rent_increase"
down_revision: Union[str, None] = "0023_line_item_rate_pct"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE lease ADD COLUMN last_rent_increase_date DATE;")
    op.execute("ALTER TABLE lease ADD COLUMN rent_before_increase NUMERIC(14, 2);")
    # An increase can't predate the tenancy.
    op.execute(
        "ALTER TABLE lease ADD CONSTRAINT lease_last_increase_after_start "
        "CHECK (last_rent_increase_date IS NULL OR last_rent_increase_date >= start_date);"
    )
    # ...nor postdate its end: a rise taking effect after the tenancy ended never happened.
    op.execute(
        "ALTER TABLE lease ADD CONSTRAINT lease_last_increase_within_term "
        "CHECK (last_rent_increase_date IS NULL OR end_date IS NULL "
        "OR last_rent_increase_date <= end_date);"
    )
    op.execute(
        "ALTER TABLE lease ADD CONSTRAINT lease_rent_before_increase_nonneg "
        "CHECK (rent_before_increase IS NULL OR rent_before_increase >= 0);"
    )
    # A prior rent with no increase date is uninterpretable — there is no month at which it
    # stopped applying, so the rent schedule couldn't place it. Reject rather than guess.
    op.execute(
        "ALTER TABLE lease ADD CONSTRAINT lease_rent_before_increase_needs_date "
        "CHECK (rent_before_increase IS NULL OR last_rent_increase_date IS NOT NULL);"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE lease DROP COLUMN rent_before_increase;")
    op.execute("ALTER TABLE lease DROP COLUMN last_rent_increase_date;")
