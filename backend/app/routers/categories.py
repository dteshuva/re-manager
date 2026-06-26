"""CRUD for the global, shared category list.

Categories are data, not code: the financial math sums line items by *classification*,
so adding a category or changing its ``default_classification`` recomputes NOI/cash flow
with no migration. Deactivating a category affects only future entry — historical line
items keep their category and are never mutated.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import Category, User
from app.schemas import CategoryCreate, CategoryOut, CategoryUpdate

router = APIRouter(prefix="/categories", tags=["categories"])


@router.get("", response_model=list[CategoryOut])
def list_categories(
    active_only: bool = False,
    db: Session = Depends(get_db),
    _u: User = Depends(get_current_user),
):
    stmt = select(Category).order_by(Category.name)
    if active_only:
        stmt = stmt.where(Category.active.is_(True))
    return db.scalars(stmt).all()


@router.post("", response_model=CategoryOut, status_code=status.HTTP_201_CREATED)
def create_category(
    payload: CategoryCreate,
    db: Session = Depends(get_db),
    _u: User = Depends(get_current_user),
):
    cat = Category(**payload.model_dump())
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
    _u: User = Depends(get_current_user),
):
    cat = db.get(Category, category_id)
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
