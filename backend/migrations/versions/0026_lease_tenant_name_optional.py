"""lease.tenant_name becomes optional — a tenancy you can record before you name anybody

Why:
    ``tenant_name`` has been NOT NULL with a non-empty CHECK since migration 0012, on the
    reasonable-sounding assumption that a tenancy obviously has a tenant. The assumption breaks
    the moment someone sets this app up from the records they actually hold.

    A landlord onboarding a portfolio works from agent statements and bank lines. Those carry an
    address, a period and an amount; they very often do not carry the tenant's name. Everything
    this app does with a tenancy — the rent schedule, the rent roll, expected-vs-actual
    variance, the whole cumulative arrears balance — needs the START DATE and the RENT, and not
    one of them needs the name. Forcing a name anyway achieves nothing except making the
    operator type a placeholder, and a column full of "Tenant"/"TBC"/"-" is strictly worse than
    a NULL: it can't be distinguished from a real name, so nothing can ever prompt them to fill
    the real one in, and the rent roll prints the placeholder as though it were fact.

    So the name becomes what it actually is: a useful detail, recorded when known. NULL means
    "not recorded", which the rent roll renders as "—" and the arrears feed simply omits from
    its label. An EMPTY STRING is still rejected — the retained CHECK now reads "NULL, or
    something non-blank", so the two states stay distinguishable and nobody can smuggle a
    placeholder back in as "".

    Nothing else changes. No existing row is touched (every one has a name), no computation
    reads the column, and the ordering/tie-break rules that resolve a unit's current tenancy key
    off dates and status, never the name.

Revision ID: 0026_lease_tenant_name_optional
Revises: 0025_rent_arrears
Create Date: 2026-10-04

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0026_lease_tenant_name_optional"
down_revision: Union[str, None] = "0025_rent_arrears"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE lease ALTER COLUMN tenant_name DROP NOT NULL;")
    # Keep rejecting the blank string: "not recorded" is NULL, and allowing "" as well would
    # give the same meaning two spellings that sort and compare differently.
    op.execute("ALTER TABLE lease DROP CONSTRAINT lease_tenant_name_nonempty;")
    op.execute(
        "ALTER TABLE lease ADD CONSTRAINT lease_tenant_name_nonempty "
        "CHECK (tenant_name IS NULL OR length(trim(tenant_name)) > 0);"
    )


def downgrade() -> None:
    # A row with no name can't satisfy NOT NULL; name it explicitly rather than failing the
    # migration, so the downgrade is always applicable.
    op.execute("UPDATE lease SET tenant_name = 'Unknown tenant' WHERE tenant_name IS NULL;")
    op.execute("ALTER TABLE lease DROP CONSTRAINT lease_tenant_name_nonempty;")
    op.execute(
        "ALTER TABLE lease ADD CONSTRAINT lease_tenant_name_nonempty "
        "CHECK (length(trim(tenant_name)) > 0);"
    )
    op.execute("ALTER TABLE lease ALTER COLUMN tenant_name SET NOT NULL;")
