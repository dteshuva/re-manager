"""Request-scoped auth dependencies.

``get_scope`` is the one place the tenancy boundary is established. Every endpoint that
touches tenant data takes ``scope: Scope = Depends(get_scope)`` and passes
``scope.account_id`` down into the query layer — the query functions in ``app.queries`` /
``app.attention`` take ``account_id`` as a REQUIRED first argument precisely so that
forgetting to scope a call is a TypeError at import/call time rather than a silent
cross-account read.
"""

from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jwt import PyJWTError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security import decode_access_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
    except PyJWTError:
        raise credentials_error
    if not user_id:
        raise credentials_error

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise credentials_error
    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required"
        )
    return current_user


@dataclass(frozen=True)
class Scope:
    """Who the request is acting as, and — the part that matters — which account's data it
    is allowed to see. ``account_id`` is read from the authenticated user, never from the
    request: a client cannot ask to act for another account."""

    account_id: str
    user: User

    @property
    def is_admin(self) -> bool:
        return self.user.role == "admin"


def get_scope(current_user: User = Depends(get_current_user)) -> Scope:
    return Scope(account_id=current_user.account_id, user=current_user)


def require_admin_scope(scope: Scope = Depends(get_scope)) -> Scope:
    """Admin-gated endpoints that also need the account (settings, unlock, audit)."""
    if not scope.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required"
        )
    return scope
