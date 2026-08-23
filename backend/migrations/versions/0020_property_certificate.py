"""compliance tracking: property_certificate (UK licensing / safety certificates)

Why:
    UK rental properties must hold current statutory certificates — an EICR (electrical, 5y),
    a Gas Safety record / CP12 (annual), an EPC (10y), plus HMO/selective licences, PAT,
    Legionella and fire-risk assessments. A lapsed certificate is a legal and safety problem,
    so the portfolio needs a place to record each one's expiry and surface any that have
    expired or are about to. This is a pure compliance/reference axis — like property_tag /
    property_budget it hangs off a property and never touches monthly_records/line_items or
    any actuals math.

    ``cert_type`` is free-text (with a UI preset list) so a landlord can record whatever their
    jurisdiction requires without a migration per certificate kind. ``expiry_date`` is the one
    required date — status (valid / expiring / expired) is derived from it at read time, never
    stored, so it's always correct as the calendar advances.

Revision ID: 0020_property_certificate
Revises: 0019_account_currency
Create Date: 2026-07-24

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0020_property_certificate"
down_revision: Union[str, None] = "0019_account_currency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE property_certificate (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            property_id  UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
            cert_type    TEXT NOT NULL,
            reference    TEXT,
            provider     TEXT,
            issue_date   DATE,
            expiry_date  DATE NOT NULL,
            notes        TEXT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT property_certificate_type_nonempty CHECK (length(trim(cert_type)) > 0),
            CONSTRAINT property_certificate_dates_ordered
                CHECK (issue_date IS NULL OR expiry_date >= issue_date)
        );
        """
    )
    # The compliance dashboard and alerts sort/filter by expiry across a whole account, so an
    # index on expiry (the hot column) keeps "what expires soonest / has expired" fast.
    op.execute("CREATE INDEX property_certificate_property_idx ON property_certificate (property_id);")
    op.execute("CREATE INDEX property_certificate_expiry_idx ON property_certificate (expiry_date);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS property_certificate;")
