"""CRUD for the account's category list.

Categories are data, not code: the financial math sums line items by *classification*,
so adding a category or changing its ``default_classification`` recomputes NOI/cash flow
with no migration. Deactivating a category affects only future entry — historical line
items keep their category and are never mutated.

Per-ACCOUNT since migration 0017, and necessarily so: because reclassifying recomputes
NOI on read, a shared list would let one account's reclassification move another's
financials. Names are unique within an account, not globally.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.deps import Scope, get_scope, require_admin_scope
from app.models import AuditLog, Category, LineItem, User
from app.schemas import (
    CategoryCreate,
    CategoryMergeIn,
    CategoryMergeOut,
    CategoryOut,
    CategoryUpdate,
)
from app.summaries import refresh_month

router = APIRouter(prefix="/categories", tags=["categories"])


@router.get("", response_model=list[CategoryOut])
def list_categories(
    active_only: bool = False,
    include_usage: bool = False,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    stmt = (
        select(Category)
        .where(Category.account_id == scope.account_id)
        .order_by(Category.name)
    )
    if active_only:
        stmt = stmt.where(Category.active.is_(True))
    cats = db.scalars(stmt).all()
    if not include_usage:
        return cats

    counts = dict(
        db.execute(
            select(LineItem.category_id, func.count(LineItem.id))
            .where(LineItem.account_id == scope.account_id)
            .group_by(LineItem.category_id)
        ).all()
    )
    out: list[CategoryOut] = []
    for c in cats:
        co = CategoryOut.model_validate(c)
        co.usage_count = counts.get(c.id, 0)
        out.append(co)
    return out


@router.post("", response_model=CategoryOut, status_code=status.HTTP_201_CREATED)
def create_category(
    payload: CategoryCreate,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    cat = Category(account_id=scope.account_id, **payload.model_dump())
    db.add(cat)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Category name already exists.")
    db.refresh(cat)
    return cat


@router.patch("/{category_id}", response_model=CategoryOut)
def update_category(
    category_id: str,
    payload: CategoryUpdate,
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    cat = db.scalar(
        select(Category).where(
            Category.id == category_id, Category.account_id == scope.account_id
        )
    )
    if cat is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Category not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(cat, field, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Category name already exists.")
    db.refresh(cat)
    return cat


@router.post("/{source_id}/merge", response_model=CategoryMergeOut)
def merge_category(
    source_id: str,
    payload: CategoryMergeIn,
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    """Merge category ``source_id`` into ``payload.target_id``.

    Every line item currently pointing at the source is reassigned to the target (or,
    if that record already has a target-category line item — the ``UniqueConstraint`` on
    ``(monthly_record_id, category_id)`` — the two amounts are summed into that existing
    row and the source row is dropped, so the total dollar amount is always preserved).
    The source category is then deactivated (never hard-deleted, so history/audit stays
    intact), and every affected property-month's rollups are refreshed the same way
    ``POST /records`` does after a line-item write.

    Cleanup tool for the PDF/statement importer, which creates one category per literal
    statement line (analyst report: 34-category list with near-duplicate junk). Admin
    only, since it mutates the account's reference data. If the source and target
    categories have DIFFERENT classifications, merging is a real reclassification of
    those dollars — allowed, but only with an explicit
    ``allow_classification_change: true`` in the body (409 otherwise), so a direct API
    call can't silently move dollars across the NOI line the way the UI's own confirm
    dialog already warns about.
    """
    target_id = payload.target_id
    if source_id == target_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot merge a category into itself.",
        )

    def _own_category(cid: str, label: str) -> Category:
        cat = db.scalar(
            select(Category).where(
                Category.id == cid, Category.account_id == scope.account_id
            )
        )
        if cat is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail=f"{label} category not found"
            )
        return cat

    source = _own_category(source_id, "Source")
    target = _own_category(target_id, "Target")

    if source.default_classification != target.default_classification and not payload.allow_classification_change:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                f"Source category \"{source.name}\" is classified as "
                f"'{source.default_classification}' but target \"{target.name}\" is "
                f"'{target.default_classification}' — merging would move dollars across the "
                "NOI line. Pass allow_classification_change=true to proceed anyway."
            ),
        )

    items = db.scalars(
        select(LineItem)
        .where(
            LineItem.category_id == source_id,
            LineItem.account_id == scope.account_id,
        )
        .options(selectinload(LineItem.monthly_record))
    ).all()

    affected_months: set[tuple[str, object]] = set()
    reassigned = 0
    for li in items:
        rec = li.monthly_record
        affected_months.add((rec.property_id, rec.month))
        sibling = db.scalar(
            select(LineItem).where(
                LineItem.monthly_record_id == li.monthly_record_id,
                LineItem.category_id == target_id,
            )
        )
        if sibling is not None:
            # Target already has a line item on this record — combine amounts rather
            # than violate the (monthly_record_id, category_id) uniqueness constraint.
            sibling.amount = (sibling.amount or 0) + (li.amount or 0)
            db.delete(li)
        else:
            li.category_id = target_id
        reassigned += 1

    source.active = False

    db.add(
        AuditLog(
            account_id=scope.account_id,
            user_id=scope.user.id,
            action="merge_category",
            entity="category",
            entity_id=source_id,
            before={"name": source.name, "active": True},
            after={
                "merged_into": target_id,
                "target_name": target.name,
                "reassigned_count": reassigned,
            },
        )
    )

    db.flush()  # write reassignments before the rollup refresh reads them
    for property_id, month in affected_months:
        refresh_month(db, property_id, month)
    db.commit()

    return CategoryMergeOut(
        source_id=source_id,
        target_id=target_id,
        reassigned_count=reassigned,
        deactivated=True,
    )
