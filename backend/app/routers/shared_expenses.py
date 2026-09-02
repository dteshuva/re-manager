"""Shared expenses: one bill covering several properties, split across them each month.

Some costs are incurred for the portfolio rather than for a property — the debt service on a
blanket loan, an insurance policy written over several buildings, a single management
retainer. The ledger is per-property by construction (a ``line_item`` hangs off a
``monthly_record`` keyed to exactly one property), so such a bill has nowhere to live: the
operator had to divide it by hand and re-type the resulting share onto every property, every
month.

This router records the ARRANGEMENT once — category, monthly amount, member properties,
allocation basis — and then POSTS it to a month or a range of months, writing one ordinary
line item onto each member's property-tier record (``unit_id IS NULL``, where shared costs
already live). Nothing downstream learns a new concept: the P&L views, NOI, cash flow,
exports and variance keep reading ``line_items`` exactly as before. Because the split comes
from :mod:`app.allocation`, the shares sum back to the bill to the cent, so the portfolio
total is the real bill rather than a rounding of it.

Three rules keep a bulk write from being destructive:

* **Provenance.** Every posted line carries ``line_items.shared_expense_id``. A re-post
  updates exactly those lines in place; an un-post deletes exactly those lines. A figure
  someone typed by hand is never touched by accident.
* **Conflicts are surfaced, not resolved.** If a member's property-month already holds a line
  in this category that this arrangement did not post, the default is to refuse the whole
  post and say where — overwriting someone's hand-entered figure as a side effect of a bulk
  action is the one thing this feature must never do. ``on_conflict`` opts into
  ``replace``/``skip`` explicitly.
* **Locks still bind.** A locked property-month blocks the post. A bulk action that could
  quietly edit locked history would defeat the point of locking.

Scoping matches the other portfolio-shaping routers: the arrangement carries its own
``account_id``, members resolve through ``get_property_or_404`` (404, never 403), reads are
open to any member of the account, and writes are admin-gated.
"""

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.allocation import allocate
from app.db import get_db
from app.deps import Scope, get_scope, require_admin_scope
from app.locks import ensure_draft_period
from app.models import Category, LineItem, MonthlyRecord, SharedExpense, SharedExpenseMember
from app.schemas import (
    SharedExpenseIn,
    SharedExpenseOut,
    SharedExpensePostIn,
    SharedExpensePostPlan,
    SharedExpensePostResult,
    SharedExpenseSplit,
    SharedExpenseUnpostResult,
)
from app.scoping import get_property_or_404
from app.summaries import refresh_month

router = APIRouter(tags=["shared-expenses"])

MAX_POST_MONTHS = 120  # ten years; a wider range is a mistake, not a request


def _first_of_month(d: date) -> date:
    return d.replace(day=1)


# ---- Resolving an arrangement into a concrete, per-property split --------------------------
def _weights(db: Session, method: str, property_ids: list[str]) -> list[Decimal]:
    """The relative weight each member carries under ``method``.

    ``equal`` weights everyone the same — the default, and what a blanket policy or portfolio
    loan payment is usually apportioned as when nothing better is known.

    ``price`` reads each member's ``property_investment.purchase_price``, the conventional
    basis for spreading blanket debt. A member with no acquisition row weighs 0; if that is
    true of ALL of them, :func:`app.allocation.allocate` falls back to an equal split rather
    than dividing by zero.

    ``units`` counts rentable units, for costs that scale with doors. Shell units (migration
    0014's synthetic placeholders) are excluded for the same reason the rent roll excludes
    them — they are not real doors — and a unit-less "single" property counts as ONE dwelling
    rather than zero, which would otherwise exclude every house from a units-based split.

    Weights are read at POST time, so a property that has since been repriced or gained doors
    carries its current share instead of one frozen when the arrangement was created.
    """
    if method == "equal":
        return [Decimal(1)] * len(property_ids)

    if method == "price":
        rows = db.execute(
            text(
                "SELECT property_id::text AS pid, purchase_price FROM property_investment "
                "WHERE property_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": property_ids},
        ).mappings().all()
        by_id = {r["pid"]: Decimal(r["purchase_price"]) for r in rows}
        return [by_id.get(pid, Decimal(0)) for pid in property_ids]

    if method == "units":
        rows = db.execute(
            text(
                "SELECT property_id::text AS pid, count(*) AS n FROM units "
                "WHERE property_id = ANY(CAST(:ids AS uuid[])) AND NOT is_shell "
                "GROUP BY property_id"
            ),
            {"ids": property_ids},
        ).mappings().all()
        by_id = {r["pid"]: Decimal(r["n"]) for r in rows}
        return [max(by_id.get(pid, Decimal(0)), Decimal(1)) for pid in property_ids]

    raise ValueError(f"no computed weights for allocation method {method!r}")


def _resolve(db: Session, scope: Scope, payload: SharedExpenseIn) -> tuple[list[dict], Decimal]:
    """Validate an arrangement and compute each member's monthly share.

    Returns ``(members, total)``. Under ``custom`` the total is the SUM of the stated shares,
    not the submitted ``amount`` — so a stored arrangement can never claim a total its own
    split contradicts. Raises 404 for a property outside this account (never 403 — the same
    non-disclosure rule as the rest of the app) and 400 for a payload that cannot produce an
    honest split.
    """
    seen: set[str] = set()
    names: list[str] = []
    property_ids: list[str] = []
    for m in payload.members:
        if m.property_id in seen:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="A property can appear only once in a shared expense.",
            )
        seen.add(m.property_id)
        prop = get_property_or_404(db, scope, m.property_id)
        names.append(prop.name)
        property_ids.append(m.property_id)

    # Scoped lookup: another account's category id reads as unknown rather than usable. The
    # composite FK from migration 0022 would reject it at flush anyway — this turns that
    # 500-shaped IntegrityError into a clean 400.
    category = db.scalar(
        select(Category).where(
            Category.id == payload.category_id, Category.account_id == scope.account_id
        )
    )
    if category is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"Unknown category id: {payload.category_id}"
        )

    if payload.allocation_method == "custom":
        # Partial input would silently allocate zero to the blanks, so require every share.
        missing = [n for n, m in zip(names, payload.members) if m.custom_share is None]
        if missing:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Custom allocation needs an explicit share for every property; missing "
                    f"for: {', '.join(missing)}."
                ),
            )
        shares = [Decimal(str(m.custom_share)) for m in payload.members]
        weights = list(shares)
    else:
        weights = _weights(db, payload.allocation_method, property_ids)
        shares = allocate(Decimal(str(payload.amount)), weights)

    total = sum(shares, Decimal(0))
    weight_sum = sum(weights, Decimal(0))
    members = [
        {
            "property_id": pid,
            "property_name": name,
            "amount": float(share),
            "basis": float(weight),
            "weight_share": float(weight / weight_sum) if weight_sum > 0 else 1 / len(shares),
            "custom_share": share if payload.allocation_method == "custom" else None,
        }
        for pid, name, share, weight in zip(property_ids, names, shares, weights)
    ]
    return members, total


def _load_out(db: Session, scope: Scope, exp: SharedExpense) -> dict:
    """Read an arrangement back with its members' CURRENT shares.

    The split is recomputed on read rather than stored, for the same reason the investment
    metrics are: ``price``/``units`` weights move as the portfolio changes, and a share cached
    at creation time would quietly stop describing what the next post will write.
    """
    rows = db.execute(
        text(
            "SELECT p.id::text AS property_id, p.name AS property_name, m.custom_share "
            "FROM shared_expense_member m JOIN properties p ON p.id = m.property_id "
            "WHERE m.shared_expense_id = :eid AND p.account_id = :account_id "
            "ORDER BY p.name"
        ),
        {"eid": exp.id, "account_id": scope.account_id},
    ).mappings().all()

    payload = SharedExpenseIn(
        name=exp.name,
        category_id=exp.category_id,
        classification=exp.classification,
        amount=float(exp.amount),
        allocation_method=exp.allocation_method,
        members=[
            {
                "property_id": r["property_id"],
                "custom_share": float(r["custom_share"]) if r["custom_share"] is not None else None,
            }
            for r in rows
        ],
    ) if len(rows) >= 2 else None

    if payload is None:
        # Only reachable if every member but one was deleted out from under the arrangement
        # (FK CASCADE from properties). Report it as it is rather than inventing a split.
        members: list[dict] = []
        allocated = Decimal(0)
    else:
        members, allocated = _resolve(db, scope, payload)

    posted = db.execute(
        text(
            "SELECT DISTINCT mr.month FROM line_items li "
            "JOIN monthly_records mr ON mr.id = li.monthly_record_id "
            "WHERE li.shared_expense_id = :eid AND li.account_id = :account_id "
            "ORDER BY mr.month"
        ),
        {"eid": exp.id, "account_id": scope.account_id},
    ).scalars().all()

    category_name = db.scalar(
        select(Category.name).where(Category.id == exp.category_id)
    )
    return {
        "id": exp.id,
        "name": exp.name,
        "category_id": exp.category_id,
        "category_name": category_name,
        "classification": exp.classification,
        "amount": float(exp.amount),
        "allocation_method": exp.allocation_method,
        "notes": exp.notes,
        "created_at": exp.created_at,
        "updated_at": exp.updated_at,
        "members": members,
        "property_count": len(members),
        "allocated_amount": float(allocated),
        "posted_months": list(posted),
    }


def _get_expense_or_404(db: Session, scope: Scope, expense_id: str) -> SharedExpense:
    exp = db.scalar(
        select(SharedExpense).where(
            SharedExpense.id == expense_id, SharedExpense.account_id == scope.account_id
        )
    )
    if exp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Shared expense not found")
    return exp


def _write_members(db: Session, scope: Scope, exp: SharedExpense, members: list[dict]) -> None:
    """Replace the member list wholesale. ``custom_share`` is stored only under ``custom``
    allocation; under a computed method it is cleared, so a stale hand-entered share can never
    resurface if the method is later switched back."""
    db.execute(
        text("DELETE FROM shared_expense_member WHERE shared_expense_id = :eid"),
        {"eid": exp.id},
    )
    db.flush()
    for m in members:
        db.add(
            SharedExpenseMember(
                shared_expense_id=exp.id,
                property_id=m["property_id"],
                account_id=scope.account_id,
                custom_share=m["custom_share"],
            )
        )


# ---- Posting: turning the arrangement into real line items ---------------------------------
def _month_range(from_month: date, to_month: date | None) -> list[date]:
    start = _first_of_month(from_month)
    end = _first_of_month(to_month) if to_month is not None else start
    if end < start:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="to_month is before from_month."
        )
    months: list[date] = []
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        months.append(date(y, m, 1))
        if len(months) > MAX_POST_MONTHS:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"Range covers more than {MAX_POST_MONTHS} months.",
            )
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def _plan(
    db: Session, scope: Scope, exp: SharedExpense, members: list[dict], months: list[date]
) -> list[dict]:
    """What a post over ``months`` would touch, and what stands there now.

    One query for the whole grid: every (member, month) pair, left-joined to its property-tier
    record, to any existing line item in this arrangement's category, and to the month's
    period status. Doing it per-cell would be N x M round trips for what is a single join.
    """
    property_ids = [m["property_id"] for m in members]
    grid = db.execute(
        text(
            "SELECT p.id::text AS property_id, mth.month AS month, "
            "       mr.id::text AS record_id, li.id::text AS line_item_id, "
            "       li.amount AS existing_amount, "
            "       li.shared_expense_id::text AS existing_expense_id, "
            "       ps.status::text AS period_status "
            "FROM properties p "
            "CROSS JOIN unnest(CAST(:months AS date[])) AS mth(month) "
            "LEFT JOIN monthly_records mr "
            "  ON mr.property_id = p.id AND mr.unit_id IS NULL AND mr.month = mth.month "
            "LEFT JOIN line_items li "
            "  ON li.monthly_record_id = mr.id AND li.category_id = :category_id "
            "LEFT JOIN period_status ps "
            "  ON ps.property_id = p.id AND ps.month = mth.month "
            "WHERE p.id = ANY(CAST(:ids AS uuid[])) AND p.account_id = :account_id"
        ),
        {
            "months": months,
            "ids": property_ids,
            "category_id": exp.category_id,
            "account_id": scope.account_id,
        },
    ).mappings().all()
    by_key = {(r["property_id"], r["month"]): r for r in grid}

    rows: list[dict] = []
    for month in months:
        for m in members:
            cur = by_key.get((m["property_id"], month))
            existing_id = cur["existing_expense_id"] if cur else None
            if cur is None or cur["line_item_id"] is None:
                source = "none"
            elif existing_id == exp.id:
                source = "this"
            elif existing_id is None:
                source = "manual"
            else:
                source = "other_shared"
            rows.append(
                {
                    "month": month,
                    "property_id": m["property_id"],
                    "property_name": m["property_name"],
                    "amount": m["amount"],
                    "existing_amount": (
                        float(cur["existing_amount"])
                        if cur is not None and cur["existing_amount"] is not None
                        else None
                    ),
                    "existing_source": source,
                    "locked": bool(cur and cur["period_status"] == "locked"),
                    # Internal, stripped before the response: saves re-querying on write.
                    "_record_id": cur["record_id"] if cur else None,
                    "_line_item_id": cur["line_item_id"] if cur else None,
                }
            )
    return rows


def _conflicts(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["existing_source"] in ("manual", "other_shared")]


def _plan_out(rows: list[dict], months: list[date], per_month: float, on_conflict: str) -> dict:
    conflicts = _conflicts(rows)
    locked = [r for r in rows if r["locked"]]
    posting = [
        r
        for r in rows
        if not r["locked"] and not (on_conflict == "skip" and r["existing_source"] in ("manual", "other_shared"))
    ]
    return {
        "months": months,
        "rows": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows],
        "amount_per_month": per_month,
        "total_amount": round(sum(r["amount"] for r in posting), 2),
        "conflict_count": len(conflicts),
        "locked_count": len(locked),
        "blocked": bool(locked) or (on_conflict == "fail" and bool(conflicts)),
    }


def _describe(rows: list[dict], limit: int = 8) -> str:
    shown = [f"{r['property_name']} {r['month']:%Y-%m}" for r in rows[:limit]]
    more = len(rows) - len(shown)
    return ", ".join(shown) + (f", and {more} more" if more > 0 else "")


# ---- Endpoints -----------------------------------------------------------------------------
@router.get("/shared-expenses", response_model=list[SharedExpenseOut])
def list_shared_expenses(db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    """Every shared-cost arrangement in this account, by name."""
    rows = db.scalars(
        select(SharedExpense)
        .where(SharedExpense.account_id == scope.account_id)
        .order_by(SharedExpense.name)
    ).all()
    return [_load_out(db, scope, e) for e in rows]


@router.post("/shared-expenses/split", response_model=SharedExpenseSplit)
def preview_split(
    payload: SharedExpenseIn,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """The per-property split these inputs WOULD produce, without saving anything.

    Exists so the form's live allocation table is computed by the same code that will persist
    it — a client-side reimplementation could distribute the leftover cents differently and
    show a split the saved arrangement then contradicts.
    """
    members, total = _resolve(db, scope, payload)
    return {
        "members": members,
        "total_amount": float(total),
        "allocation_method": payload.allocation_method,
    }


@router.get("/shared-expenses/{expense_id}", response_model=SharedExpenseOut)
def get_shared_expense(
    expense_id: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)
):
    return _load_out(db, scope, _get_expense_or_404(db, scope, expense_id))


@router.post(
    "/shared-expenses", response_model=SharedExpenseOut, status_code=status.HTTP_201_CREATED
)
def create_shared_expense(
    payload: SharedExpenseIn,
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    """Record a shared-cost arrangement. This writes NO line items — it defines the split;
    ``POST /shared-expenses/{id}/post`` applies it to a month or a range."""
    members, total = _resolve(db, scope, payload)
    exp = SharedExpense(
        account_id=scope.account_id,
        name=payload.name.strip(),
        category_id=payload.category_id,
        classification=payload.classification,
        # Under custom allocation the total is the sum of the stated shares, not the submitted
        # amount, so the stored figure always describes the split actually held.
        amount=total,
        allocation_method=payload.allocation_method,
        notes=payload.notes,
    )
    db.add(exp)
    db.flush()  # need the generated id before linking members
    _write_members(db, scope, exp, members)
    db.commit()
    db.refresh(exp)
    return _load_out(db, scope, exp)


@router.put("/shared-expenses/{expense_id}", response_model=SharedExpenseOut)
def update_shared_expense(
    expense_id: str,
    payload: SharedExpenseIn,
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    """Replace an arrangement's definition (amount, members, basis, category).

    Already-posted months are NOT rewritten: history stays as it was actually recorded, and
    re-posting a month is the explicit way to bring it in line with the new definition. That
    keeps a renewal — next year's premium, a property joining the policy — from silently
    restating closed periods.

    Changing the category leaves any previously posted lines under the OLD category still
    attributed to this arrangement, so an un-post still cleans them up.
    """
    exp = _get_expense_or_404(db, scope, expense_id)
    members, total = _resolve(db, scope, payload)
    exp.name = payload.name.strip()
    exp.category_id = payload.category_id
    exp.classification = payload.classification
    exp.amount = total
    exp.allocation_method = payload.allocation_method
    exp.notes = payload.notes
    _write_members(db, scope, exp, members)
    db.commit()
    db.refresh(exp)
    return _load_out(db, scope, exp)


@router.delete("/shared-expenses/{expense_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_shared_expense(
    expense_id: str,
    purge: bool = Query(
        False,
        description=(
            "Also delete every line item this arrangement posted. By default the posted "
            "history is KEPT and merely detached — the money was really spent."
        ),
    ),
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    """Delete an arrangement.

    By default this detaches rather than erases: the FK is ``ON DELETE SET NULL``, so every
    line item it posted stays exactly where it is and simply becomes an ordinary hand-entered
    figure. Deleting the arrangement that describes a bill should not retroactively remove the
    spend from the P&L. ``?purge=true`` is the explicit opt-in to remove the postings too, and
    refreshes every affected property-month so the rollups follow.
    """
    exp = _get_expense_or_404(db, scope, expense_id)
    if purge:
        removed = _purge_postings(db, scope, exp, months=None)
        for property_id, month in sorted({(r["property_id"], r["month"]) for r in removed}):
            refresh_month(db, property_id, month)
    db.delete(exp)
    db.commit()


def _purge_postings(
    db: Session, scope: Scope, exp: SharedExpense, months: list[date] | None
) -> list[dict]:
    """Delete the line items this arrangement posted (optionally only in ``months``), and
    return one row per deleted item so the caller can refresh the affected rollups and report
    what went.

    Matching is on ``shared_expense_id`` alone — never on category and amount — so a figure
    someone later edited by hand into the same slot is not collateral damage, and a line this
    arrangement posted under a since-changed category is still cleaned up.
    """
    sql = (
        "DELETE FROM line_items li USING monthly_records mr "
        "WHERE li.monthly_record_id = mr.id AND li.shared_expense_id = :eid "
        "  AND li.account_id = :account_id"
    )
    params: dict = {"eid": exp.id, "account_id": scope.account_id}
    if months is not None:
        sql += " AND mr.month = ANY(CAST(:months AS date[]))"
        params["months"] = months
    sql += " RETURNING mr.property_id::text AS property_id, mr.month AS month, li.amount"
    rows = [dict(r) for r in db.execute(text(sql), params).mappings().all()]
    db.flush()
    return rows


@router.post("/shared-expenses/{expense_id}/post/preview", response_model=SharedExpensePostPlan)
def preview_post(
    expense_id: str,
    payload: SharedExpensePostIn,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Dry-run a post: exactly which property-months it would write, what stands there now,
    and whether anything blocks it. Same code path as the real post, so the plan and the
    write can never disagree."""
    exp = _get_expense_or_404(db, scope, expense_id)
    members = _load_out(db, scope, exp)["members"]
    if not members:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="This shared expense has fewer than two member properties.",
        )
    months = _month_range(payload.from_month, payload.to_month)
    rows = _plan(db, scope, exp, members, months)
    return _plan_out(rows, months, float(exp.amount), payload.on_conflict)


@router.post("/shared-expenses/{expense_id}/post", response_model=SharedExpensePostResult)
def post_shared_expense(
    expense_id: str,
    payload: SharedExpensePostIn,
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    """Apply the arrangement to a month or a range of months.

    For every (member, month) this creates the property-tier record if needed and upserts one
    line item carrying the property's share and this arrangement's provenance. Re-posting the
    same months is idempotent: the lines it already owns are updated in place, so correcting
    the amount and re-posting a year costs one call rather than N x 12 edits.

    The whole range is written in ONE transaction. A partial post is worse than none here —
    the point of the feature is that the shares sum to the bill, and a half-applied split
    silently breaks that.
    """
    exp = _get_expense_or_404(db, scope, expense_id)
    members = _load_out(db, scope, exp)["members"]
    if not members:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="This shared expense has fewer than two member properties.",
        )
    months = _month_range(payload.from_month, payload.to_month)
    rows = _plan(db, scope, exp, members, months)

    locked = [r for r in rows if r["locked"]]
    if locked:
        raise HTTPException(
            status.HTTP_423_LOCKED,
            detail=(
                f"{len(locked)} property-month(s) are locked; an admin must unlock them "
                f"before this can be posted: {_describe(locked)}."
            ),
        )
    conflicts = _conflicts(rows)
    if conflicts and payload.on_conflict == "fail":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                f"{len(conflicts)} property-month(s) already have a '"
                f"{db.scalar(select(Category.name).where(Category.id == exp.category_id))}' "
                f"line this shared expense did not post: {_describe(conflicts)}. "
                "Re-send with on_conflict='replace' to overwrite them, or 'skip' to leave "
                "them and post the rest."
            ),
        )

    written = 0
    created = 0
    skipped = 0
    total = Decimal(0)
    touched: set[tuple[str, date]] = set()

    for r in rows:
        if payload.on_conflict == "skip" and r["existing_source"] in ("manual", "other_shared"):
            skipped += 1
            continue

        rec_id = r["_record_id"]
        if rec_id is None:
            rec = MonthlyRecord(
                account_id=scope.account_id,
                property_id=r["property_id"],
                unit_id=None,
                month=r["month"],
                notes=None,
            )
            db.add(rec)
            db.flush()
            rec_id = rec.id
            created += 1
        ensure_draft_period(db, r["property_id"], r["month"])

        li = db.get(LineItem, r["_line_item_id"]) if r["_line_item_id"] else None
        if li is None:
            li = LineItem(
                account_id=scope.account_id,
                monthly_record_id=rec_id,
                category_id=exp.category_id,
            )
            db.add(li)
        li.classification = exp.classification
        li.amount = Decimal(str(r["amount"]))
        # An arrangement states its own share outright, so posting over a rate-stated line
        # (migration 0023) takes the line back to a fixed amount — otherwise refresh_month
        # would recompute the share away from the split that was just allocated.
        li.rate_pct = None
        li.shared_expense_id = exp.id
        written += 1
        total += Decimal(str(r["amount"]))
        touched.add((r["property_id"], r["month"]))

    db.flush()  # write the line items so the rollup refresh below reads the new values
    for property_id, month in sorted(touched):
        refresh_month(db, property_id, month)
    db.commit()

    return {
        "months": months,
        "rows": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows],
        "line_items_written": written,
        "records_created": created,
        "skipped": skipped,
        "total_posted": float(total),
    }


@router.delete("/shared-expenses/{expense_id}/post", response_model=SharedExpenseUnpostResult)
def unpost_shared_expense(
    expense_id: str,
    from_month: date = Query(..., description="First month to un-post (inclusive)."),
    to_month: date | None = Query(
        None, description="Last month to un-post (inclusive). Defaults to from_month."
    ),
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    """Remove this arrangement's postings from a month or range, leaving everything else.

    Only line items carrying this arrangement's provenance are deleted, so a hand-entered
    figure that happens to sit in the same category is untouched. Locked months are refused
    for the same reason posting into them is.
    """
    exp = _get_expense_or_404(db, scope, expense_id)
    months = _month_range(from_month, to_month)

    locked = db.execute(
        text(
            "SELECT p.name AS property_name, ps.month AS month "
            "FROM line_items li "
            "JOIN monthly_records mr ON mr.id = li.monthly_record_id "
            "JOIN period_status ps ON ps.property_id = mr.property_id AND ps.month = mr.month "
            "JOIN properties p ON p.id = mr.property_id "
            "WHERE li.shared_expense_id = :eid AND li.account_id = :account_id "
            "  AND mr.month = ANY(CAST(:months AS date[])) AND ps.status = 'locked'"
        ),
        {"eid": exp.id, "account_id": scope.account_id, "months": months},
    ).mappings().all()
    if locked:
        raise HTTPException(
            status.HTTP_423_LOCKED,
            detail=(
                f"{len(locked)} property-month(s) are locked; an admin must unlock them "
                f"before this can be un-posted: {_describe([dict(r) for r in locked])}."
            ),
        )

    removed = _purge_postings(db, scope, exp, months)
    for property_id, month in sorted({(r["property_id"], r["month"]) for r in removed}):
        refresh_month(db, property_id, month)
    db.commit()
    return {
        "months": months,
        "line_items_removed": len(removed),
        "total_removed": float(sum((Decimal(r["amount"]) for r in removed), Decimal(0))),
    }
