"""Read-only view of the audit log (admin only).

``audit_log`` rows are written today by ``POST /periods/{id}/unlock`` (see
``app.routers.periods``). Until this router existed the data was write-only — captured on
every unlock but with no way to read it back, which is close to useless for a compliance
question like "who reopened a locked, already-reported month and why." This just exposes
the existing table, most-recent first, admin-gated the same way the mutation is.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import Scope, require_admin_scope
from app.models import AuditLog, User
from app.schemas import AuditLogOut

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=list[AuditLogOut])
def list_audit_log(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    entity: str | None = None,
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    """Most-recent-first audit rows (who/when/action/entity/before/after). Admin only."""
    stmt = (
        select(AuditLog, User.email)
        .outerjoin(User, User.id == AuditLog.user_id)
        .where(AuditLog.account_id == scope.account_id)
    )
    if entity:
        stmt = stmt.where(AuditLog.entity == entity)
    stmt = stmt.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
    rows = db.execute(stmt).all()
    out = []
    for log, email in rows:
        out.append(
            AuditLogOut(
                id=log.id,
                user_id=log.user_id,
                user_email=email,
                action=log.action,
                entity=log.entity,
                entity_id=log.entity_id,
                before=log.before,
                after=log.after,
                created_at=log.created_at,
            )
        )
    return out
