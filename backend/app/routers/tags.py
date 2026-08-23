"""Portfolio segmentation: free-text tags on a property (migration 0010).

A property may carry any number of tags (region, fund/entity, asset class, submarket,
...). Tags are case-sensitive, exact-match strings, stored trimmed. Reads (list all tags,
list a property's tags) are open to any authed user; writes (add/remove a property's tag)
are admin-gated, matching the other portfolio-shaping mutations (budgets, investments).
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import Scope, get_scope, require_admin_scope
from app.models import PropertyTag
from app.schemas import PropertyTagIn
from app.scoping import get_property_or_404

router = APIRouter(tags=["tags"])



@router.get("/tags", response_model=list[str])
def list_all_tags(db: Session = Depends(get_db), scope: Scope = Depends(get_scope)):
    """Every distinct tag in use across THIS ACCOUNT's portfolio (filter dropdown/chip list).

    property_tag has no account_id of its own — it hangs off a property — so the scope
    comes from the join rather than a column on the row."""
    return db.scalars(
        text(
            "SELECT DISTINCT t.tag FROM property_tag t "
            "JOIN properties p ON p.id = t.property_id "
            "WHERE p.account_id = :account_id ORDER BY t.tag"
        ),
        {"account_id": scope.account_id},
    ).all()


@router.get("/properties/{property_id}/tags", response_model=list[str])
def list_property_tags(
    property_id: str, db: Session = Depends(get_db), scope: Scope = Depends(get_scope)
):
    get_property_or_404(db, scope, property_id)
    return db.scalars(
        text("SELECT tag FROM property_tag WHERE property_id = :id ORDER BY tag"),
        {"id": property_id},
    ).all()


@router.post(
    "/properties/{property_id}/tags", response_model=list[str], status_code=status.HTTP_201_CREATED
)
def add_property_tag(
    property_id: str,
    payload: PropertyTagIn,
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    """Attach a tag to a property (idempotent — re-adding an existing tag is a no-op).
    Returns the property's full tag list."""
    get_property_or_404(db, scope, property_id)
    tag = payload.tag.strip()
    if not tag:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Tag must not be blank")
    existing = db.get(PropertyTag, {"property_id": property_id, "tag": tag})
    if existing is None:
        db.add(PropertyTag(property_id=property_id, tag=tag))
        db.commit()
    return db.scalars(
        text("SELECT tag FROM property_tag WHERE property_id = :id ORDER BY tag"),
        {"id": property_id},
    ).all()


@router.delete("/properties/{property_id}/tags/{tag}", response_model=list[str])
def remove_property_tag(
    property_id: str,
    tag: str,
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    """Detach a tag from a property. Returns the property's remaining tag list (404 only if
    the property itself doesn't exist; removing a tag that isn't set is a harmless no-op)."""
    get_property_or_404(db, scope, property_id)
    existing = db.get(PropertyTag, {"property_id": property_id, "tag": tag})
    if existing is not None:
        db.delete(existing)
        db.commit()
    return db.scalars(
        text("SELECT tag FROM property_tag WHERE property_id = :id ORDER BY tag"),
        {"id": property_id},
    ).all()
