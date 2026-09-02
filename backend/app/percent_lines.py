"""Expenses stated as a rate rather than a figure — "management: 8% of rent".

A management fee is a rate, not an amount. Typed as an amount it has to be recomputed by
hand every month, and the moment rent moves — a renewal, a vacancy, a unit re-let at a
different price — the typed figure quietly stops matching the contract and NOI is wrong by
the difference. ``line_items.rate_pct`` (migration 0023) records the rate that was actually
agreed and lets the machine keep the money in step.

Two decisions make this cheap for the rest of the system:

**The derived figure is written into ``line_items.amount``.** Nothing downstream learns a new
concept: ``v_line_item_resolved`` / ``v_monthly_pnl``, the summary rollups, exports, variance
and attention all keep reading exactly the column they already read. A percentage line is an
ordinary line item whose amount happens to be maintained for you.

**The basis is rent in the record's own scope.**
  * unit-tier record  → that unit's rent for the month (a fee on one door's rent);
  * property-tier record → the whole property's rent for the month, every unit included —
    which is what a property-tier record means everywhere else in this codebase (shared costs
    that belong to the property, not to one door).

That scope rule is what makes the feature work for both shapes of property: a single-family
records rent and fee on the same property-tier record; a multifamily records rent per unit
and the one management fee at the property tier, over the sum.

Recompute, not compute-once: :func:`apply_percentage_lines` runs from
:func:`app.summaries.refresh_month`, i.e. on *every* write to a property-month, so editing a
unit's rent in March updates March's fee even though the fee lives on a different record and
was entered weeks earlier. It only writes when the figure actually changes, so re-running it
on an unchanged month is a no-op.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import LineItem, MonthlyRecord

CENTS = Decimal("0.01")
HUNDRED = Decimal(100)

# Rent in one property-month, split by scope. Percentage lines are excluded from their own
# basis (``li.rate_pct IS NULL``): a rate line can never feed the rent it is a percentage of,
# so the computation stays well defined even if someone later reclassifies the fee's category
# to 'rent'. Effective classification is resolved the same way v_line_item_resolved does —
# the per-line override, else the category default — so a reclassification moves the basis
# with no migration, exactly as it moves NOI.
_RENT_BY_SCOPE = text(
    """
    SELECT mr.unit_id::text AS unit_id, COALESCE(SUM(li.amount), 0) AS rent
    FROM line_items li
    JOIN monthly_records mr ON mr.id = li.monthly_record_id
    JOIN categories      c  ON c.id  = li.category_id
    WHERE mr.property_id = :property_id
      AND mr.month       = :month
      AND li.rate_pct IS NULL
      AND COALESCE(li.classification, c.default_classification) = 'rent'
    GROUP BY mr.unit_id
    """
)


def rent_basis(db: Session, property_id: str, month: date) -> dict[str | None, Decimal]:
    """Rent available as a fee basis in one property-month, keyed by scope.

    ``None`` is the property-tier key and holds the property's TOTAL rent for the month (all
    units plus any rent booked at the property tier) — the basis a property-tier fee is
    charged on. Every other key is a unit id holding that unit's own rent. A scope with no
    rent is simply absent; callers should read it as zero.
    """
    per_scope: dict[str | None, Decimal] = {}
    total = Decimal(0)
    for row in db.execute(
        _RENT_BY_SCOPE, {"property_id": property_id, "month": month.replace(day=1)}
    ):
        rent = Decimal(row.rent)
        total += rent
        if row.unit_id is not None:
            per_scope[row.unit_id] = rent
    per_scope[None] = total
    return per_scope


def derive_amount(rate_pct: Decimal, basis: Decimal) -> Decimal:
    """The money a rate comes to on a basis, rounded to the cent (half-up, as money is)."""
    return (Decimal(rate_pct) * Decimal(basis) / HUNDRED).quantize(CENTS, rounding=ROUND_HALF_UP)


def apply_percentage_lines(db: Session, property_id: str, month: date) -> int:
    """Rewrite every rate-stated line in one property-month from current rent.

    Returns how many amounts actually changed (0 when nothing was stale — the common case).
    Flushes so the caller's rollup refresh reads the new figures; the caller owns the
    transaction, as everywhere else in the write path.
    """
    month = month.replace(day=1)
    # The session runs with autoflush off (app.db), so pending edits — a just-imported amount,
    # a rate the caller cleared — are invisible to SQL until they are written. Both the basis
    # query and the "which lines are rate-stated?" query below read through SQL, so flush
    # first or this recomputes from the state before the write it is meant to react to.
    db.flush()
    rows = db.execute(
        select(LineItem, MonthlyRecord.unit_id)
        .join(MonthlyRecord, MonthlyRecord.id == LineItem.monthly_record_id)
        .where(
            MonthlyRecord.property_id == property_id,
            MonthlyRecord.month == month,
            LineItem.rate_pct.is_not(None),
        )
    ).all()
    if not rows:
        return 0

    basis_by_scope = rent_basis(db, property_id, month)
    changed = 0
    for li, unit_id in rows:
        if li.rate_pct is None:
            continue  # the caller cleared the rate in this same transaction
        basis = basis_by_scope.get(unit_id, Decimal(0))
        amount = derive_amount(li.rate_pct, basis)
        if li.amount != amount:
            li.amount = amount
            changed += 1
    if changed:
        db.flush()
    return changed
