"""exclude shell units from the summary layer's occupancy counts

The bug:
    Migration 0014 introduced ``units.is_shell`` for synthetic units planted purely to
    satisfy the NOT NULL ``lease.unit_id`` FK on a unit-less "single" property (Cedar Plaza
    Retail's percentage-rent lease is the only current case). Its stated rule — see the
    column's docstring in app/models.py — is that such a unit must be EXCLUDED from
    occupancy / unit-count aggregates, because "counting it there would corrupt those
    aggregates with a phantom vacant/occupied unit."

    That rule was applied to the rent roll (``app/queries.py``'s ``_occupancy_summary``,
    which filters ``is_shell``) but never to the summary layer.
    ``refresh_property_month_summary`` has counted units with a bare
    ``SELECT count(*) FROM units WHERE property_id = ...`` since migration 0002, so a shell
    unit lands in ``property_month_summary.total_units`` while — having no monthly_records
    of its own — it can never land in ``occupied_units``.

    The visible consequence: Cedar Plaza Retail, a single-asset property collecting
    $5,200/month of property-tier rent, was summarized as ``occupied=0, total=1,
    occupancy=0.00000`` and therefore tripped the absolute-vacancy detector (migration
    0007), publishing a permanent false alarm to the attention feed:

        [high_vacancy] Cedar Plaza Retail — High vacancy: 100% vacant (0/1 occupied)

    This is exactly the corruption 0014 set out to prevent, in the one code path that was
    never updated. (It is also what ``verify_phase6``/``verify_phase9`` have been failing
    on — their "exactly 4 attention items" answer key was right; the 5th item is spurious.)

The fix:
    Both counts now exclude shell units. ``total_units`` gains ``AND NOT is_shell``;
    ``occupied_units`` gains the same exclusion via a join to ``units`` — it is already
    shell-free in practice (a shell has no records, so it never reaches v_monthly_pnl) but
    is made explicit so the two halves of the ratio can't drift apart if a shell ever does
    get a record.

Effect on existing data:
    A property whose ONLY units are shells (Cedar Plaza Retail) now reports
    ``total_units = 0`` and — via the pre-existing ``CASE WHEN v_total_units > 0`` guard —
    ``occupancy = NULL``, i.e. "no unit basis to measure occupancy against", which is the
    honest answer for a property that books its rent at the property tier. The
    absolute-vacancy detector requires ``total_units > 0 AND occupancy IS NOT NULL``, so
    the false alarm stops firing. Portfolio occupancy loses one phantom vacant unit from
    its denominator. NO financial figure changes: rent/opex/NOI/cash flow never referenced
    the unit count.

Revision ID: 0018_shell_occupancy
Revises: 0017_accounts
Create Date: 2026-07-22

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0018_shell_occupancy"
down_revision: Union[str, None] = "0017_accounts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_METRICS = (
    "gross_rent", "operating_expenses", "noi", "capex",
    "debt_service", "other_below_line", "below_noi", "cash_flow",
)


def _refresh_property_month_summary(*, exclude_shells: bool) -> None:
    """(Re)create the property-month refresh function.

    Identical to migration 0017's account-aware version apart from the two unit-count
    queries; ``exclude_shells=False`` restores the 0017 behaviour for downgrade.
    """
    metrics_csv = ", ".join(_METRICS)
    sum_metrics = ",\n            ".join(f"COALESCE(SUM(v.{m}), 0)" for m in _METRICS)
    upsert_metrics = ",\n            ".join(f"{m} = EXCLUDED.{m}" for m in _METRICS)

    total_units_sql = (
        """SELECT count(*) INTO v_total_units
            FROM units
            WHERE property_id = p_property_id AND NOT is_shell;"""
        if exclude_shells
        else """SELECT count(*) INTO v_total_units
            FROM units WHERE property_id = p_property_id;"""
    )
    occupied_sql = (
        """SELECT count(DISTINCT v.unit_id) INTO v_occupied
            FROM v_monthly_pnl v
            JOIN units u ON u.id = v.unit_id
            WHERE v.property_id = p_property_id
              AND v.month = p_month
              AND v.unit_id IS NOT NULL
              AND v.gross_rent > 0
              AND NOT u.is_shell;"""
        if exclude_shells
        else """SELECT count(DISTINCT v.unit_id) INTO v_occupied
            FROM v_monthly_pnl v
            WHERE v.property_id = p_property_id
              AND v.month = p_month
              AND v.unit_id IS NOT NULL
              AND v.gross_rent > 0;"""
    )

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION refresh_property_month_summary(
            p_property_id UUID, p_month DATE
        ) RETURNS void
        LANGUAGE plpgsql AS $$
        DECLARE
            v_total_units INTEGER;
            v_occupied    INTEGER;
            v_account_id  UUID;
        BEGIN
            SELECT account_id INTO v_account_id FROM properties WHERE id = p_property_id;

            {total_units_sql}

            {occupied_sql}

            INSERT INTO property_month_summary AS s (
                account_id, property_id, month, {metrics_csv},
                occupied_units, total_units, occupancy, refreshed_at
            )
            SELECT
                v_account_id, p_property_id, p_month,
                {sum_metrics},
                v_occupied,
                v_total_units,
                CASE WHEN v_total_units > 0
                     THEN ROUND(v_occupied::numeric / v_total_units, 5)
                     ELSE NULL END,
                now()
            FROM v_monthly_pnl v
            WHERE v.property_id = p_property_id AND v.month = p_month
            ON CONFLICT (property_id, month) DO UPDATE SET
                {upsert_metrics},
                occupied_units = EXCLUDED.occupied_units,
                total_units    = EXCLUDED.total_units,
                occupancy      = EXCLUDED.occupancy,
                refreshed_at   = EXCLUDED.refreshed_at;
        END;
        $$;
        """
    )


def upgrade() -> None:
    _refresh_property_month_summary(exclude_shells=True)
    # Recompute every existing row through the corrected function (derived data only —
    # raw monthly_records / line_items are untouched).
    op.execute("SELECT rebuild_all_summaries();")


def downgrade() -> None:
    _refresh_property_month_summary(exclude_shells=False)
    op.execute("SELECT rebuild_all_summaries();")
