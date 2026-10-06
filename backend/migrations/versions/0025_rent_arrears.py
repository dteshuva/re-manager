"""Rent arrears: lease.opening_arrears + the arrears_adjustment ledger

Why:
    A landlord's single most operationally urgent number is not NOI — it is "who owes me
    money, and how much has it built up to". Until now this app could not answer it. It
    held rent DUE (``lease.contract_rent`` and the escalation/increase schedule around it,
    reference data) and rent COLLECTED (``unit_month_summary.gross_rent``, derived from the
    actual line items), and it even reported the single-period gap between them as the rent
    variance — but nothing carried that gap FORWARD. Arrears is a BALANCE: it accrues month
    after month, and it is settled when a tenant pays more than one month's rent.

    MODELLING CHOICE — arrears is DERIVED, not entered (the architectural decision here,
    and the reason this migration adds so little):

        movement(month)  = rent_due(month) - rent_collected(month) + adjustments(month)
        balance(month)   = opening_arrears + sum of movement over the tenancy to date

    Both inputs are already on file, so there is NO new monthly data-entry burden and —
    more importantly — no second ledger to drift out of agreement with the first. A tenant
    who underpays by £600 in February and pays £1,800 in March shows +600 then -600, and
    the balance returns to zero on its own, with no "clear the arrears" action for anyone to
    forget. This is the same rule the rest of the app lives by (the P&L is computed, never
    stored; see ``app.queries``): had arrears been a stored per-month figure instead, every
    rent correction would have silently invalidated it.

    Derivation can't express two things, which is exactly what this migration stores:

        ``lease.opening_arrears``   the balance brought forward at ``start_date`` — debt
                                    (or credit, hence signed) that predates this app's
                                    records entirely. Without it, importing a tenancy that
                                    was ALREADY £2k down starts them at zero. NULL/0 for
                                    the overwhelming majority of tenancies.

        ``arrears_adjustment``      a signed, dated movement that is NOT a rent shortfall:
                                    a write-off (arrears forgiven, or recovered through the
                                    deposit at check-out), a charge the rent schedule does
                                    not describe, or a correction. Without it, a written-off
                                    balance is uncollectable forever yet shows as owing in
                                    perpetuity — and the only alternative, editing
                                    ``contract_rent`` to make the arithmetic come out, would
                                    corrupt the rent schedule to fix the balance.

    SCOPE OF THE BALANCE — per TENANCY, not per unit. The running sum resets at each lease:
    an outgoing tenant's unpaid rent is a debt of that tenant, and carrying it onto the next
    tenant of the same unit would be wrong in both directions (they'd inherit a debt; the
    real debtor would vanish from the report). Hence ``arrears_adjustment.lease_id`` rather
    than ``unit_id``, and hence ``opening_arrears`` living on ``lease``.

    MONTHS THAT COUNT — only months with an actual record on file (plus any month carrying
    an adjustment). A month with rent due but NO monthly_record is MISSING DATA, not
    arrears: we don't know what was collected. Treating it as arrears would turn every
    un-entered month into fictional debt and bury the real cases. Missing data is already
    its own attention-feed item type (migration 0011), and the rent-variance engine already
    draws this exact line (``app.queries._unit_expected_actual``); arrears reuses it.

    This migration touches NOTHING existing: a new nullable column and a new parallel
    table, in the same mould as ``property_certificate``/``property_tag``/``property_budget``
    before it. NOI and cash flow are unaffected, and so is the rent waterfall — an arrears
    balance is the CUMULATIVE view of shortfalls the waterfall already reports for a single
    period as ``bad_debt``, not a new charge against income.

    Ownership is transitive, like ``property_certificate``'s: ``arrears_adjustment`` carries
    no ``account_id`` of its own and is scoped by resolving its lease via
    ``app.scoping.get_lease_or_404`` (lease -> unit -> property -> account).

Revision ID: 0025_rent_arrears
Revises: 0024_lease_rent_increase
Create Date: 2026-10-04

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0025_rent_arrears"
down_revision: Union[str, None] = "0024_lease_rent_increase"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Signed deliberately: a tenancy can begin in CREDIT (rent paid ahead of the start
    # date, common where a guarantor pays a term up front), which a >= 0 constraint would
    # make unrepresentable and force someone to fake as a negative adjustment instead.
    op.execute("ALTER TABLE lease ADD COLUMN opening_arrears NUMERIC(14, 2);")

    op.execute(
        """
        CREATE TABLE arrears_adjustment (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            lease_id    UUID NOT NULL REFERENCES lease(id) ON DELETE CASCADE,
            month       DATE NOT NULL,
            amount      NUMERIC(14, 2) NOT NULL,
            kind        TEXT NOT NULL,
            note        TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            -- Month grain, same convention as monthly_records: always the 1st.
            CONSTRAINT arrears_adjustment_month_first
                CHECK (date_trunc('month', month) = month),
            CONSTRAINT arrears_adjustment_kind_check
                CHECK (kind IN ('write_off', 'charge', 'correction')),
            -- A zero adjustment is a row that says nothing while still appearing in the
            -- ledger as if something happened.
            CONSTRAINT arrears_adjustment_amount_nonzero CHECK (amount <> 0),
            -- The sign IS the meaning, so it can't be left to the writer's discretion: a
            -- write-off always REDUCES the balance and a charge always increases it. Only
            -- 'correction' is free to go either way, which is what makes it the honest
            -- escape hatch rather than a second spelling of the other two.
            CONSTRAINT arrears_adjustment_kind_sign CHECK (
                (kind = 'write_off' AND amount < 0)
                OR (kind = 'charge' AND amount > 0)
                OR kind = 'correction'
            )
        );
        """
    )
    # The ledger is always read for one tenancy in month order (and the attention feed reads
    # every tenancy's up to a given month), so lease+month is the only access path.
    op.execute("CREATE INDEX arrears_adjustment_lease_month_idx ON arrears_adjustment (lease_id, month);")


def downgrade() -> None:
    op.execute("DROP TABLE arrears_adjustment;")
    op.execute("ALTER TABLE lease DROP COLUMN opening_arrears;")
