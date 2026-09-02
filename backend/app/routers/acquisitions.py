"""Portfolio (bulk) acquisitions: one purchase covering several properties.

A bulk deal has an agreed price per property, but ONE closing-cost settlement figure and ONE
blanket loan across the whole package. Those shared costs have nowhere to live on a
per-property row, so this router records the deal itself and spreads the shared costs across
its members — by default pro-rata on purchase price, the convention lenders and accountants
use for blanket debt.

The split is not kept as a special case: it is WRITTEN DOWN onto each member's
``property_investment`` row (linked back by ``acquisition_id``). Everything downstream — cap
rate, cash-on-cash, DSCR, the value-weighted portfolio aggregates, the benchmarks table —
keeps reading that one table and needs no knowledge that bulk purchases exist. Because the
allocation sums back to the stated totals to the cent (see :mod:`app.allocation`), the
portfolio-level figures are identical to modelling the deal as a single entity.

What that means for the per-property numbers: cap rate stays exactly true (it uses only the
agreed price, which is real), while cash-on-cash and DSCR become share-based — each property
carrying its proportional slice of the blanket debt. That is a modelling convention, and the
UI says so rather than implying the split is a measured fact.

Scoping: a deal is a top-level entity carrying its own ``account_id``, and every member is
resolved through ``get_property_or_404``, so a deal can never reach across accounts. Reads are
open to any member of the account; writes are admin-gated, matching the other
portfolio-shaping mutations (properties, budgets, certificates).
"""

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.allocation import allocate, allocation_weights
from app.db import get_db
from app.deps import Scope, get_scope, require_admin_scope
from app.models import PortfolioAcquisition, PropertyInvestment
from app.schemas import (
    AcquisitionPreview,
    PortfolioAcquisitionIn,
    PortfolioAcquisitionOut,
)
from app.scoping import get_property_or_404

router = APIRouter(tags=["acquisitions"])


# ---- Resolving a payload into a concrete, per-property split -------------------------------
def _resolve(db: Session, scope: Scope, payload: PortfolioAcquisitionIn) -> list[dict]:
    """Validate the deal and compute each member's share of the shared costs.

    Returns one dict per member with the figures to store. Raises 404 for a property outside
    this account (never 403 — same non-disclosure rule as the rest of the app) and 400 for a
    payload that cannot produce an honest split.
    """
    seen: set[str] = set()
    names: list[str] = []
    prices: list[Decimal] = []
    for m in payload.members:
        if m.property_id in seen:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="A property can appear only once in a portfolio purchase.",
            )
        seen.add(m.property_id)
        prop = get_property_or_404(db, scope, m.property_id)
        names.append(prop.name)
        prices.append(Decimal(str(m.purchase_price)))

    if payload.allocation_method == "custom":
        # The operator supplies both shares directly (e.g. the lender's per-property release
        # prices). Partial input would silently allocate zero to the blanks, so require all.
        missing = [
            n
            for n, m in zip(names, payload.members)
            if m.closing_costs is None or m.loan_amount is None
        ]
        if missing:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Custom allocation needs an explicit closing-cost and loan share for every "
                    f"property; missing for: {', '.join(missing)}."
                ),
            )
        closings = [Decimal(str(m.closing_costs)) for m in payload.members]
        loans = [Decimal(str(m.loan_amount)) for m in payload.members]
    else:
        weights = allocation_weights(payload.allocation_method, prices)
        closings = allocate(Decimal(str(payload.total_closing_costs)), weights)
        loans = allocate(Decimal(str(payload.total_loan_amount)), weights)

    return [
        {
            "property_id": m.property_id,
            "property_name": name,
            "purchase_price": price,
            "closing_costs": closing,
            "loan_amount": loan,
        }
        for m, name, price, closing, loan in zip(payload.members, names, prices, closings, loans)
    ]


def _totals(resolved: list[dict]) -> tuple[Decimal, Decimal, Decimal]:
    """Combined price, closing costs and loan across the members, as actually allocated.

    Under ``custom`` these ARE the deal's totals — which is why the stored totals are taken
    from here rather than from the request, so they can never contradict the split.
    """
    return (
        sum((r["purchase_price"] for r in resolved), Decimal(0)),
        sum((r["closing_costs"] for r in resolved), Decimal(0)),
        sum((r["loan_amount"] for r in resolved), Decimal(0)),
    )


def _member_rows(resolved: list[dict]) -> list[dict]:
    """Shape the resolved split for the API, adding equity and each member's price weight."""
    total_price = sum((r["purchase_price"] for r in resolved), Decimal(0))
    out = []
    for r in resolved:
        price = float(r["purchase_price"])
        closing = float(r["closing_costs"])
        loan = float(r["loan_amount"])
        out.append(
            {
                "property_id": r["property_id"],
                "property_name": r["property_name"],
                "purchase_price": price,
                "closing_costs": closing,
                "loan_amount": loan,
                "equity_invested": price - loan + closing,
                "price_share": (price / float(total_price)) if total_price > 0 else 0.0,
            }
        )
    return out


# ---- Persisting the split onto the per-property rows ---------------------------------------
def _write_members(
    db: Session, acq_id: str, resolved: list[dict], purchase_date: date
) -> None:
    """Upsert each member's ``property_investment`` row from the allocation, and unlink any
    property that was a member but is no longer.

    A property joining a deal has its existing standalone acquisition figures REPLACED — the
    deal is now the authority for them. A property leaving keeps its last allocated figures
    and simply stops being linked, so dropping a member never destroys its return metrics.
    """
    member_ids = [r["property_id"] for r in resolved]

    db.execute(
        text(
            "UPDATE property_investment SET acquisition_id = NULL "
            "WHERE acquisition_id = :acq AND property_id <> ALL(CAST(:ids AS uuid[]))"
        ),
        {"acq": acq_id, "ids": member_ids},
    )

    for r in resolved:
        inv = db.get(PropertyInvestment, r["property_id"])
        if inv is None:
            inv = PropertyInvestment(property_id=r["property_id"])
            db.add(inv)
        inv.purchase_price = r["purchase_price"]
        inv.closing_costs = r["closing_costs"]
        inv.loan_amount = r["loan_amount"]
        inv.purchase_date = purchase_date
        inv.acquisition_id = acq_id


def _load_out(db: Session, account_id: str, acq: PortfolioAcquisition) -> dict:
    """Read a deal back with its members as they are ACTUALLY stored.

    Members come from ``property_investment`` rather than from what was just written, so the
    response reflects any later hand-editing on a property's own page. The ``*_drift`` figures
    report exactly that: allocated-sum minus stated total, non-zero once the parts have stopped
    summing to the deal.
    """
    rows = db.execute(
        text(
            "SELECT p.id::text AS property_id, p.name AS property_name, i.purchase_price, "
            "       i.closing_costs, i.loan_amount "
            "FROM property_investment i JOIN properties p ON p.id = i.property_id "
            "WHERE i.acquisition_id = :acq AND p.account_id = :account_id "
            "ORDER BY p.name"
        ),
        {"acq": acq.id, "account_id": account_id},
    ).mappings().all()

    resolved = [
        {
            "property_id": r["property_id"],
            "property_name": r["property_name"],
            "purchase_price": r["purchase_price"],
            "closing_costs": r["closing_costs"],
            "loan_amount": r["loan_amount"],
        }
        for r in rows
    ]
    members = _member_rows(resolved)
    total_price, alloc_closing, alloc_loan = _totals(resolved)

    stated_closing = float(acq.total_closing_costs)
    stated_loan = float(acq.total_loan_amount)
    return {
        "id": acq.id,
        "name": acq.name,
        "purchase_date": acq.purchase_date,
        "total_closing_costs": stated_closing,
        "total_loan_amount": stated_loan,
        "allocation_method": acq.allocation_method,
        "notes": acq.notes,
        "created_at": acq.created_at,
        "updated_at": acq.updated_at,
        "members": members,
        "property_count": len(members),
        "total_purchase_price": float(total_price),
        "total_equity_invested": sum(m["equity_invested"] for m in members),
        "allocated_closing_costs": float(alloc_closing),
        "allocated_loan_amount": float(alloc_loan),
        "closing_costs_drift": float(alloc_closing) - stated_closing,
        "loan_amount_drift": float(alloc_loan) - stated_loan,
    }


def _get_acquisition_or_404(db: Session, scope: Scope, acquisition_id: str) -> PortfolioAcquisition:
    acq = db.scalar(
        select(PortfolioAcquisition).where(
            PortfolioAcquisition.id == acquisition_id,
            PortfolioAcquisition.account_id == scope.account_id,
        )
    )
    if acq is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Portfolio purchase not found")
    return acq


# ---- Endpoints -----------------------------------------------------------------------------
@router.get("/acquisitions", response_model=list[PortfolioAcquisitionOut])
def list_acquisitions(db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    """Every bulk purchase recorded in this account, newest deal first."""
    deals = db.scalars(
        select(PortfolioAcquisition)
        .where(PortfolioAcquisition.account_id == scope.account_id)
        .order_by(PortfolioAcquisition.purchase_date.desc(), PortfolioAcquisition.name)
    ).all()
    return [_load_out(db, scope.account_id, a) for a in deals]


@router.post("/acquisitions/preview", response_model=AcquisitionPreview)
def preview_acquisition(
    payload: PortfolioAcquisitionIn,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    """Compute the split these inputs would produce, WITHOUT saving anything.

    Exists so the entry form's live allocation table comes from the same code that will
    persist it — a client-side reimplementation could round the leftover cents differently and
    show a total that the saved deal then contradicts.
    """
    resolved = _resolve(db, scope, payload)
    members = _member_rows(resolved)
    total_price, total_closing, total_loan = _totals(resolved)
    return {
        "members": members,
        "total_purchase_price": float(total_price),
        "total_closing_costs": float(total_closing),
        "total_loan_amount": float(total_loan),
        "total_equity_invested": sum(m["equity_invested"] for m in members),
    }


@router.get("/acquisitions/{acquisition_id}", response_model=PortfolioAcquisitionOut)
def get_acquisition(
    acquisition_id: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)
):
    """One bulk purchase with its members' current allocated figures."""
    return _load_out(db, scope.account_id, _get_acquisition_or_404(db, scope, acquisition_id))


@router.post(
    "/acquisitions", response_model=PortfolioAcquisitionOut, status_code=status.HTTP_201_CREATED
)
def create_acquisition(
    payload: PortfolioAcquisitionIn,
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    """Record a bulk purchase and write its allocation onto every member property.

    Any member that already had standalone acquisition data has it replaced — joining a deal
    makes the deal the authority for that property's price, closing costs and loan.
    """
    resolved = _resolve(db, scope, payload)
    _, total_closing, total_loan = _totals(resolved)

    acq = PortfolioAcquisition(
        account_id=scope.account_id,
        name=payload.name.strip(),
        purchase_date=payload.purchase_date,
        # Totals are taken from the RESOLVED split, not the request, so that under custom
        # allocation the stored totals always describe the shares actually written.
        total_closing_costs=total_closing,
        total_loan_amount=total_loan,
        allocation_method=payload.allocation_method,
        notes=payload.notes,
    )
    db.add(acq)
    db.flush()  # need the generated id before linking the member rows

    _write_members(db, acq.id, resolved, payload.purchase_date)
    db.commit()
    db.refresh(acq)
    return _load_out(db, scope.account_id, acq)


@router.put("/acquisitions/{acquisition_id}", response_model=PortfolioAcquisitionOut)
def update_acquisition(
    acquisition_id: str,
    payload: PortfolioAcquisitionIn,
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    """Replace a bulk purchase and RE-ALLOCATE it across the (possibly changed) member list.

    This is the reason the deal is stored as a deal: when the final settlement statement comes
    in with different closing costs, or a property is added to the package, the split is
    recomputed for every member at once instead of being re-divided by hand.
    """
    acq = _get_acquisition_or_404(db, scope, acquisition_id)
    resolved = _resolve(db, scope, payload)
    _, total_closing, total_loan = _totals(resolved)

    acq.name = payload.name.strip()
    acq.purchase_date = payload.purchase_date
    acq.total_closing_costs = total_closing
    acq.total_loan_amount = total_loan
    acq.allocation_method = payload.allocation_method
    acq.notes = payload.notes

    _write_members(db, acq.id, resolved, payload.purchase_date)
    db.commit()
    db.refresh(acq)
    return _load_out(db, scope.account_id, acq)


@router.delete("/acquisitions/{acquisition_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_acquisition(
    acquisition_id: str,
    purge: bool = Query(
        False,
        description=(
            "Also delete the members' acquisition data. By default the deal is only "
            "UNGROUPED: each property keeps its allocated figures and its return metrics."
        ),
    ),
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    """Delete a bulk purchase.

    By default this ungroups rather than erases — the FK is ``ON DELETE SET NULL``, so each
    member keeps its allocated price, closing costs and loan and simply becomes a standalone
    acquisition again. Deleting a grouping should not destroy the data every return metric on
    those properties depends on. ``?purge=true`` is the explicit opt-in to clear them too.
    """
    acq = _get_acquisition_or_404(db, scope, acquisition_id)
    if purge:
        db.execute(
            text("DELETE FROM property_investment WHERE acquisition_id = :acq"),
            {"acq": acq.id},
        )
    db.delete(acq)
    db.commit()
