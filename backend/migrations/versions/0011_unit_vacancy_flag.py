"""explicit unit vacancy flag, distinct from missing data

Why:
    The vacancy detector today infers "vacant" purely from a unit-month row being ABSENT
    from ``unit_month_summary`` — which is itself derived from ``v_monthly_pnl``, an INNER
    join through ``line_items``. So a unit with NO monthly_record at all (a PM who forgot to
    send a statement) looks byte-for-byte identical to a unit that's genuinely vacant: both
    just have no row. That's a real ambiguity for a fund (analyst report, "Missing data" and
    "vacancy" are conflated).

    This migration adds an explicit ``is_vacant`` flag on ``monthly_records`` (so a PM/admin
    can POST a unit-month record that says "this unit is vacant" instead of only being able
    to omit data entirely), and makes ``unit_month_summary`` RECORD-driven instead of
    line-item-driven: a row now exists for every unit that has a ``monthly_record`` this
    month, even one with zero line items (an explicit vacancy has $0 rent and typically no
    line items at all). Absence of a unit_month_summary row now means EXACTLY what it should:
    no record was posted for that unit-month at all (missing data), not "maybe vacant, maybe
    just not sent yet."

    Financial math is untouched: an explicit vacancy still contributes $0 rent (whether via
    zero line items or an explicit $0 rent line item), same as today. This only sharpens what
    "no row" means downstream (attention.py's vacancy/missing-data detectors, the unit
    roster, unit detail) — see the app-code changes in the same pass as this migration.

Revision ID: 0011_unit_vacancy_flag
Revises: 0010_property_tag
Create Date: 2026-07-09

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0011_unit_vacancy_flag"
down_revision: Union[str, None] = "0010_property_tag"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_METRICS = (
    "gross_rent", "operating_expenses", "noi", "capex",
    "debt_service", "other_below_line", "below_noi", "cash_flow",
)


def upgrade() -> None:
    op.execute("ALTER TABLE monthly_records ADD COLUMN is_vacant BOOLEAN NOT NULL DEFAULT false;")
    op.execute("ALTER TABLE unit_month_summary ADD COLUMN is_vacant BOOLEAN NOT NULL DEFAULT false;")

    select_metrics = ",\n            ".join(f"COALESCE(v.{m}, 0)" for m in _METRICS)
    # Record-driven (was: v_monthly_pnl-driven, an INNER join through line_items that
    # silently dropped any unit-month with zero line items). One row per monthly_record
    # (unit_id, month) is unique already, so a plain LEFT JOIN needs no extra GROUP BY.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION refresh_unit_month_summary(
            p_property_id UUID, p_month DATE
        ) RETURNS void
        LANGUAGE plpgsql AS $$
        BEGIN
            DELETE FROM unit_month_summary
            WHERE property_id = p_property_id AND month = p_month;

            INSERT INTO unit_month_summary
                (unit_id, month, property_id, {", ".join(_METRICS)}, is_vacant, refreshed_at)
            SELECT mr.unit_id, mr.month, mr.property_id, {select_metrics}, mr.is_vacant, now()
            FROM monthly_records mr
            LEFT JOIN v_monthly_pnl v
                   ON v.unit_id = mr.unit_id AND v.month = mr.month AND v.property_id = mr.property_id
            WHERE mr.property_id = p_property_id AND mr.month = p_month AND mr.unit_id IS NOT NULL;
        END;
        $$;
        """
    )

    # Backfill: re-derive every existing unit-month row through the new (record-driven)
    # function. Values are unchanged for every unit-month that already had line items (the
    # common case); this only surfaces rows for any unit-month record that has zero line
    # items, which none of the current seed data has.
    op.execute("SELECT rebuild_all_summaries();")


def downgrade() -> None:
    sum_metrics = ",\n            ".join(f"COALESCE(SUM(v.{m}), 0)" for m in _METRICS)
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION refresh_unit_month_summary(
            p_property_id UUID, p_month DATE
        ) RETURNS void
        LANGUAGE plpgsql AS $$
        BEGIN
            DELETE FROM unit_month_summary
            WHERE property_id = p_property_id AND month = p_month;

            INSERT INTO unit_month_summary
                (unit_id, month, property_id, {", ".join(_METRICS)}, refreshed_at)
            SELECT v.unit_id, v.month, v.property_id, {sum_metrics}, now()
            FROM v_monthly_pnl v
            WHERE v.property_id = p_property_id AND v.month = p_month
              AND v.unit_id IS NOT NULL
            GROUP BY v.unit_id, v.month, v.property_id;
        END;
        $$;
        """
    )
    op.execute("SELECT rebuild_all_summaries();")
    op.execute("ALTER TABLE unit_month_summary DROP COLUMN is_vacant;")
    op.execute("ALTER TABLE monthly_records DROP COLUMN is_vacant;")
