"""lease.arrears_from_month: stop the handover month being reported as the tenant's debt

Why:
    Arrears is rent due against rent collected, accumulated. That comparison is only
    meaningful for months the landlord actually owned the property for a full rent cycle, and
    the FIRST month after a purchase is reliably not one of them.

    At completion the rent in hand is APPORTIONED between seller and buyer, and an English
    tenancy is almost never paid on the 1st — so the first payment a new owner receives covers
    a part period, arrives at a different point in the month, or does not arrive at all because
    the seller already took it. Measured against a full month's rent, every one of those looks
    exactly like a tenant who underpaid. In the portfolio this column was written for, thirteen
    properties completed on one day and every one of them showed a first-month "shortfall" of
    between £70 and £700 — none of which any tenant owed, and all of which then settled to the
    agreed rent the following month.

    That is not a bug in the arrears arithmetic; it is a question the arithmetic was asked
    about the wrong month. So the tenancy records the month from which its arrears are
    MEANINGFUL, and the ledger simply doesn't start before it:

        NULL (the default, and every pre-existing row)
            Track from the beginning — unchanged behaviour.
        A month
            Months before it contribute nothing: no rent due, no collection, no movement. They
            are not "paid" and not "owed"; they are outside the measurement, like a month with
            no record at all.

    Deliberately NOT derived automatically from ``property_investment.purchase_date``. The
    handover month in the DATA is a function of the tenant's own payment cycle, not of the
    completion date — a sale completing 24 July produced its distorted figures in AUGUST, so
    any "purchase month + 1" rule would have excluded the wrong month and left the artefact in
    place while looking as though it had dealt with it. A date the operator sets, having looked
    at the first clean month on the statement, is both simpler and correct.

    Deliberately NOT ``opening_arrears`` either, which already exists for a related but
    different fact. ``opening_arrears`` says "the tenant owed £X when my records start"; this
    says "don't read anything into those months at all". Using a balance to cancel out a
    phantom shortfall would require computing the phantom first, and would silently go wrong
    the moment any figure in those months was corrected.

Revision ID: 0027_lease_arrears_from_month
Revises: 0026_lease_tenant_name_optional
Create Date: 2026-10-04

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0027_lease_arrears_from_month"
down_revision: Union[str, None] = "0026_lease_tenant_name_optional"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE lease ADD COLUMN arrears_from_month DATE;")
    # Month grain, same convention as monthly_records: always the 1st.
    op.execute(
        "ALTER TABLE lease ADD CONSTRAINT lease_arrears_from_month_first "
        "CHECK (arrears_from_month IS NULL OR date_trunc('month', arrears_from_month) = arrears_from_month);"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE lease DROP COLUMN arrears_from_month;")
