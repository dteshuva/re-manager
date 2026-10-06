"""count property-tier rent as occupancy when the property has exactly one unit

The bug:
    ``occupied_units`` has only ever counted UNIT-tier rent:

        SELECT count(DISTINCT v.unit_id) ... WHERE v.unit_id IS NOT NULL AND v.gross_rent > 0

    That is the right question for a building — how many of its five flats paid — but it is
    the wrong one for a house. A single-asset property has one unit record, and its rent may
    legitimately be booked at EITHER tier: at the unit, or at the property with no unit named.
    Every statement parser in :mod:`app.statements` emits the second kind, because a landlord
    statement names a property and not a flat, and ``/import/rows`` accepts it (a blank unit
    is documented as meaning property-tier). Nothing was wrong with the data.

    But a month booked that way counted zero occupied units against a total of one, so a
    property let continuously to the same tenant read:

        Sep 2026   rent £725   occupied 1/1   occupancy 100%     (booked at the unit)
        Oct 2026   rent £725   occupied 0/1   occupancy   0%     (booked at the property)

    — a 100-point occupancy collapse, in a month whose rent was identical and whose tenant
    never moved. Both vacancy detectors read that number, so the feed showed an occupancy
    drop and, below the materiality floor, a high-vacancy item, for three houses that were
    fully let. The alert is the symptom; the real cost is that the occupancy series, the
    portfolio roll-up and economic-occupancy comparisons were all wrong for those months.

    This is the same class of defect migration 0018 fixed for shell units — a unit counted in
    the denominator that could never reach the numerator — arriving by the other door. 0018
    removed a unit that should never have been counted; this adds rent that should always
    have counted.

The fix:
    When a property has exactly ONE non-shell unit and no unit-tier rent for the month, its
    property-tier rent is taken to occupy that unit. With one unit there is no ambiguity about
    whose rent it is: the property IS the unit.

    Deliberately limited to the one-unit case. For a property with several units, rent booked
    once at the property tier genuinely does not say how many of them were let, and inventing
    an answer would turn a visible gap into an invisible one. That case is left reading as it
    does today; see "Known gap" below.

    Deliberately fixed HERE rather than by making the parsers post to the sole unit. The
    hazard belongs to every producer — the PDF statement parsers, a CSV with the unit column
    blank, a property-tier entry typed by hand — so the arithmetic is where it is cheapest to
    be right once. Fixing it in a parser would also have left the months already imported
    wrong, and risked a second rent row for a month that already has one.

Effect on existing data:
    Derived rows only: ``rebuild_all_summaries()`` recomputes ``property_month_summary`` from
    the untouched ``monthly_records`` / ``line_items``. Months whose rent was already booked
    at the unit tier produce byte-identical rows. A single-unit property-tier month moves from
    ``occupied = 0`` to ``occupied = 1``, which raises that property's occupancy to 100% and
    withdraws the false vacancy items. No financial figure changes: rent, opex, NOI and cash
    flow have never referenced the unit count.

Known gap (deliberately not addressed here):
    A property with MORE than one unit whose rent is booked only at the property tier still
    reports 0% occupancy. That is a real false alarm of the same family, but the honest answer
    there is "no per-unit basis to measure" rather than a number, and choosing between
    reporting NULL and reporting the old 0% changes behaviour for data this repository has no
    example of. It wants its own change, with its own test.

Revision ID: 0029_property_tier_occupancy
Revises: 0028_statement_format
Create Date: 2026-10-05

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0029_property_tier_occupancy"
down_revision: Union[str, None] = "0028_statement_format"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_METRICS = (
    "gross_rent", "operating_expenses", "noi", "capex",
    "debt_service", "other_below_line", "below_noi", "cash_flow",
)

# The property-tier fallback. Runs only when the unit-tier count came back empty AND the
# property has exactly one unit, so a building's occupancy is computed exactly as before.
_SOLE_UNIT_FALLBACK = """
            IF v_occupied = 0 AND v_total_units = 1 THEN
                SELECT CASE WHEN COALESCE(SUM(v.gross_rent), 0) > 0 THEN 1 ELSE 0 END
                  INTO v_occupied
                FROM v_monthly_pnl v
                WHERE v.property_id = p_property_id
                  AND v.month = p_month
                  AND v.unit_id IS NULL;
            END IF;
"""


def _refresh_property_month_summary(*, sole_unit_fallback: bool) -> None:
    """(Re)create the property-month refresh function. Identical to migration 0018's version
    apart from the fallback; ``sole_unit_fallback=False`` restores it for downgrade."""
    metrics_csv = ", ".join(_METRICS)
    sum_metrics = ",\n            ".join(f"COALESCE(SUM(v.{m}), 0)" for m in _METRICS)
    upsert_metrics = ",\n            ".join(f"{m} = EXCLUDED.{m}" for m in _METRICS)
    fallback = _SOLE_UNIT_FALLBACK if sole_unit_fallback else ""

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

            SELECT count(*) INTO v_total_units
            FROM units
            WHERE property_id = p_property_id AND NOT is_shell;

            SELECT count(DISTINCT v.unit_id) INTO v_occupied
            FROM v_monthly_pnl v
            JOIN units u ON u.id = v.unit_id
            WHERE v.property_id = p_property_id
              AND v.month = p_month
              AND v.unit_id IS NOT NULL
              AND v.gross_rent > 0
              AND NOT u.is_shell;
{fallback}
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
    _refresh_property_month_summary(sole_unit_fallback=True)
    op.execute("SELECT rebuild_all_summaries();")


def downgrade() -> None:
    _refresh_property_month_summary(sole_unit_fallback=False)
    op.execute("SELECT rebuild_all_summaries();")
