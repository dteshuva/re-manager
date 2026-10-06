"""statement_format: remember how one sender's statements are read, so the same corrections
are never made twice.

Why:
    Every managing agent lays a statement out its own way, and the parser in
    :mod:`app.statements` is deliberately format-agnostic — it reads shapes (one property per
    line, one table per property, one property per file) rather than templates. That gets the
    figures off the page, and the figures are the hard part. What it cannot do is know what
    this particular sender's WORDS mean in this particular account:

        the statement says            the account calls it
        ------------------            --------------------
        "Mgt Fee"                     Management Fee
        "Rent 19/09/26 - 18/10/26"    Rent
        "64 King Edward Street"       64 King Edward St, Gateshead
        "Ground Rent"                 Ground Rent  (capex, not operating — this owner's call)

    None of that is guessable from one statement, and all of it is identical in the next one
    the same agent sends. Without somewhere to put the answer the operator re-makes the same
    handful of corrections every month, which is both the tedious part of the job and the
    part where a month eventually gets imported with a category nobody meant.

    So a format is a saved ANSWER, keyed by a fingerprint of the layout, holding only the
    mappings a human confirmed:

        category_aliases          raw label (lowercased) -> categories.id
        property_aliases          raw property label     -> properties.id
        classification_overrides  category name          -> classification enum value
        month_rule                NULL | 'statement_period' | 'rent_period'

    Applied at PREVIEW time, never at import time: a saved format changes what the review
    screen proposes, and the operator still presses the button. That keeps a wrong alias
    visible and one edit away instead of silently posting for months.

    ``fingerprint`` is computed in :func:`app.statements.fingerprint` from the layout's
    stable parts — the shape, the column headings, and the boilerplate lines with every
    digit, date and amount masked out. Two statements from the same sender therefore
    fingerprint alike however their figures differ, and a different sender does not collide.
    It is stored as the hash only; ``sample`` keeps the masked text it was taken from so a
    human can see WHY two files were considered the same format.

Scoping:
    Account-scoped in its own right, like ``categories`` — the table is not reachable from a
    property, and one account's idea of what "Mgt Fee" means must never reach another's.

    The alias maps are JSONB, so the ids inside them cannot carry foreign keys and the
    database cannot refuse a category belonging to another account. That check therefore has
    to be made where the rows are written, and is: the endpoint resolves every id against the
    caller's own account before saving, and the preview resolves them again against the
    caller's own categories and properties before applying. An id that does not belong to the
    account resolves to nothing and the row is offered as unknown — the same path a brand new
    label takes. Nothing here is trusted on the way back out.

Deliberately NOT:
    * a template describing where fields sit on the page. Coordinates are what the parser
      already derives per file, and they move the moment an agent changes its stationery;
      the mappings here are about meaning, which doesn't.

    A file whose fingerprint does not match a saved one exactly is compared against the
    stored ``sample`` as well, because an agent that adds a line to its covering note has not
    become a different agent. A close match is used, and the preview says so by name rather
    than letting it pass as an exact one.

Revision ID: 0028_statement_format
Revises: 0027_lease_arrears_from_month
Create Date: 2026-10-05

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0028_statement_format"
down_revision: Union[str, None] = "0027_lease_arrears_from_month"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE statement_format (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            account_id     UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            fingerprint    TEXT NOT NULL,
            label          TEXT NOT NULL,
            shape          TEXT NOT NULL,
            sample         TEXT,
            category_aliases         JSONB NOT NULL DEFAULT '{}'::jsonb,
            property_aliases         JSONB NOT NULL DEFAULT '{}'::jsonb,
            classification_overrides JSONB NOT NULL DEFAULT '{}'::jsonb,
            month_rule     TEXT,
            times_used     INTEGER NOT NULL DEFAULT 0,
            last_used_at   TIMESTAMPTZ,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

            -- One saved answer per layout per account: a second upload of the same format
            -- updates the mappings rather than creating a rival copy of them.
            CONSTRAINT statement_format_account_fingerprint_key
                UNIQUE (account_id, fingerprint),
            CONSTRAINT statement_format_month_rule_check
                CHECK (month_rule IS NULL OR month_rule IN ('statement_period', 'rent_period')),
            CONSTRAINT statement_format_label_not_blank
                CHECK (btrim(label) <> '')
        );
        """
    )
    op.execute(
        "CREATE INDEX statement_format_account_idx ON statement_format (account_id, label);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS statement_format;")
