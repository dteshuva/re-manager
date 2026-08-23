"""units.is_shell: mark synthetic/shell units so they're excluded from occupancy + GPR +
unit-count aggregates.

Why:
    Migration 0013's live backfill (``scripts/backfill_lease_v2_fields.py``) planted ONE
    synthetic unit — Cedar Plaza Retail's ``unit_number='RETAIL'`` — purely to hang a
    percentage-rent lease off (``lease.unit_id`` is a NOT NULL FK, and Cedar Plaza Retail is
    a unit-less "single" property whose real financials post entirely at the property tier,
    ``unit_id IS NULL``). That shell unit has no ``monthly_records``/``unit_month_summary``
    rows of its own, so it entered the rent-roll's occupancy math as a phantom occupied unit
    with $5,200 contract rent and NULL actual rent:
      - portfolio ``total_units`` inflated 1002 -> 1003 (a unit that doesn't physically
        exist counted in the physical-occupancy denominator).
      - Cedar Plaza Retail's OWN rent roll reported ``economic_occupancy = 0.0%`` (its only
        "unit" has $5,200 potential rent and $0 collected, by construction — a false read on
        an asset that is, in reality, fully occupied and paying).
      - portfolio ``economic_occupancy`` was dragged ~0.30pp below its true value by that
        phantom $5,200-potential/$0-collected row.

    Fix (analyst's fix #1, option (ii)): mark the shell unit and EXCLUDE it from
    ``_occupancy_summary``'s aggregates (total/occupied unit counts, GPR, economic
    occupancy, rent realization, avg vacancy downtime) in app/queries.py, while still
    keeping it in the rent-roll ROWS list so Cedar's percentage-rent lease/terms stay
    visible and editable — this is a display/aggregate-scoping fix, not a data deletion.
    Cedar Plaza Retail's actual dashboard/NOI figures are untouched (they never came from
    this unit — see the docstring above — this migration only affects the units-based
    occupancy summary shown on the rent roll).

    Data backfill (this migration, not a separate script — same convention as 0013's
    lease_type backfill): marks the ALREADY-PLANTED Cedar RETAIL unit ``is_shell = true`` on
    the live DB directly, so a plain `alembic upgrade head` self-heals without depending on
    script execution order. `scripts/backfill_lease_v2_fields.py` is updated in the same
    commit to set `is_shell=True` on any future/fresh-install creation of this unit, and to
    self-heal an existing one on re-run, so the two paths can't drift.

Revision ID: 0014_unit_is_shell
Revises: 0013_lease_v2_fields
Create Date: 2026-07-16

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0014_unit_is_shell"
down_revision: Union[str, None] = "0013_lease_v2_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE units ADD COLUMN is_shell BOOLEAN NOT NULL DEFAULT false;"
    )
    # Self-heal the already-planted Cedar Plaza Retail shell unit on the live DB (idempotent
    # — a no-op if it's already flagged, and harmless/no-op on a DB where it doesn't exist
    # yet, since the fresh-install seed path is updated in this same commit to set the flag
    # at creation time instead).
    op.execute(
        """
        UPDATE units SET is_shell = true
        WHERE unit_number = 'RETAIL'
          AND property_id IN (SELECT id FROM properties WHERE name = 'Cedar Plaza Retail');
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE units DROP COLUMN is_shell;")
