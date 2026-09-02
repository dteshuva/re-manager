"""shared expenses: one bill covering several properties, split across them each month

Why:
    Some costs are incurred for the portfolio, not for a property: the debt service on a
    blanket/portfolio loan, an insurance policy written over several buildings, a single
    management retainer. The ledger is per-property by design (``line_items`` hang off a
    ``monthly_records`` row keyed to one property), so the operator had to divide the bill by
    hand and re-type the resulting share onto every property, every month — arithmetic that
    silently stops summing to the real bill and work that repeats forever.

    ``shared_expense`` stores the arrangement ONCE: the category, the periodic amount, the
    member properties, and how to spread it. Posting it to a month writes an ordinary line
    item onto each member's property-tier record, so every existing view — the P&L rollups,
    NOI, cash flow, exports, variance — keeps reading exactly one place and needs no knowledge
    that shared expenses exist. The split uses ``app.allocation``, so the parts sum back to the
    stated bill to the cent.

    ``line_items.shared_expense_id`` is provenance, and it is what makes the arrangement
    editable: it identifies exactly which line items a given shared expense produced, so a
    re-post can update them in place, an un-post can remove them without touching hand-entered
    figures, and a hand-entered line item in the same category can be DETECTED rather than
    silently overwritten.

    ``ON DELETE SET NULL`` on that link is deliberate, mirroring portfolio_acquisition:
    deleting the arrangement must not erase posted history. The money was really spent; the
    line items stay and simply stop being linked.

Revision ID: 0022_shared_expense
Revises: 0021_portfolio_acquisition
Create Date: 2026-08-30

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0022_shared_expense"
down_revision: Union[str, None] = "0021_portfolio_acquisition"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Like portfolio_acquisition, a shared expense is a top-level entity (owned by no single
    # property), so it carries its own account_id. The composite FK to categories is migration
    # 0017's invariant: an arrangement may only post into its OWN account's category, because
    # reclassifying a category moves NOI.
    op.execute(
        """
        CREATE TABLE shared_expense (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            account_id        UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            name              TEXT NOT NULL,
            category_id       UUID NOT NULL,
            classification    classification,
            amount            NUMERIC(14,2) NOT NULL DEFAULT 0,
            allocation_method TEXT NOT NULL DEFAULT 'equal',
            notes             TEXT,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT shared_expense_name_nonempty CHECK (length(trim(name)) > 0),
            CONSTRAINT shared_expense_amount_nonneg CHECK (amount >= 0),
            CONSTRAINT shared_expense_method_known
                CHECK (allocation_method IN ('equal', 'price', 'units', 'custom')),
            CONSTRAINT shared_expense_category_in_account_fk
                FOREIGN KEY (account_id, category_id)
                REFERENCES categories (account_id, id),
            -- Target for the composite FKs from the member and line-item tables below.
            CONSTRAINT shared_expense_account_id_id_key UNIQUE (account_id, id)
        );
        """
    )
    op.execute("CREATE INDEX shared_expense_account_idx ON shared_expense (account_id);")

    # Membership. account_id is carried (rather than resolved through the parent) purely so
    # the two composite FKs below can exist: they make a member that belongs to a DIFFERENT
    # account than the arrangement unrepresentable, rather than merely rejected by app code.
    op.execute(
        """
        CREATE TABLE shared_expense_member (
            shared_expense_id UUID NOT NULL,
            property_id       UUID NOT NULL,
            account_id        UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            -- Only meaningful under allocation_method = 'custom': the operator's explicit
            -- share for this property (e.g. the insurer's per-building premium). The
            -- arrangement's stored `amount` is then the SUM of these, so it can never
            -- contradict the split it describes.
            custom_share      NUMERIC(14,2),
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (shared_expense_id, property_id),
            CONSTRAINT shared_expense_member_share_nonneg
                CHECK (custom_share IS NULL OR custom_share >= 0),
            CONSTRAINT shared_expense_member_expense_fk
                FOREIGN KEY (shared_expense_id) REFERENCES shared_expense (id)
                ON DELETE CASCADE,
            CONSTRAINT shared_expense_member_expense_in_account_fk
                FOREIGN KEY (account_id, shared_expense_id)
                REFERENCES shared_expense (account_id, id) ON DELETE CASCADE,
            CONSTRAINT shared_expense_member_property_in_account_fk
                FOREIGN KEY (account_id, property_id)
                REFERENCES properties (account_id, id) ON DELETE CASCADE
        );
        """
    )
    op.execute(
        "CREATE INDEX shared_expense_member_property_idx "
        "ON shared_expense_member (property_id);"
    )

    # Provenance on the posted line item. NULL = hand-entered or imported, which is what every
    # pre-existing row is. SET NULL on delete: removing the arrangement ungroups its postings,
    # it never erases spend that actually happened.
    op.execute(
        """
        ALTER TABLE line_items
            ADD COLUMN shared_expense_id UUID
                REFERENCES shared_expense(id) ON DELETE SET NULL,
            ADD CONSTRAINT line_items_shared_expense_in_account_fk
                FOREIGN KEY (account_id, shared_expense_id)
                REFERENCES shared_expense (account_id, id) ON DELETE SET NULL;
        """
    )
    # Drives the "which line items did this arrangement post?" lookup behind re-post,
    # un-post and the posted-months listing.
    op.execute(
        "CREATE INDEX line_items_shared_expense_idx "
        "ON line_items (shared_expense_id) WHERE shared_expense_id IS NOT NULL;"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS line_items_shared_expense_idx;")
    op.execute(
        "ALTER TABLE line_items "
        "DROP CONSTRAINT IF EXISTS line_items_shared_expense_in_account_fk, "
        "DROP COLUMN IF EXISTS shared_expense_id;"
    )
    op.execute("DROP TABLE IF EXISTS shared_expense_member;")
    op.execute("DROP TABLE IF EXISTS shared_expense;")
