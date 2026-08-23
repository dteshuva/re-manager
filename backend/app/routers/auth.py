from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.accounts import provision_account
from app.db import get_db
from app.deps import Scope, get_current_user, get_scope, require_admin_scope
from app.models import Account, User
from app.schemas import SignupRequest, Token, UserCreate, UserOut
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=Token, status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest, db: Session = Depends(get_db)):
    """Self-serve registration: creates a NEW, empty account and its first admin user.

    This is the only public write endpoint. The new account starts with its own copy of
    the default category list and its own attention thresholds (see app/accounts.py) and
    no properties — it shares nothing with any existing account.
    """
    account_name = (payload.account_name or "").strip() or f"{payload.email}'s portfolio"
    try:
        account, user = provision_account(
            db,
            account_name=account_name,
            email=payload.email,
            password=payload.password,
        )
        db.commit()
    except IntegrityError:
        # Only reachable via the global unique index on users.email.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        )
    return Token(access_token=create_access_token(subject=str(user.id), role=user.role))


@router.post("/login", response_model=Token)
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == form.username))
    if not user or not verify_password(form.password, user.hashed_password) or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password"
        )
    token = create_access_token(subject=str(user.id), role=user.role)
    return Token(access_token=token)


@router.get("/me", response_model=UserOut)
def me(
    db: Session = Depends(get_db),
    scope: Scope = Depends(get_scope),
):
    account = db.get(Account, scope.account_id)
    return UserOut(
        id=scope.user.id,
        email=scope.user.email,
        role=scope.user.role,
        is_active=scope.user.is_active,
        account_id=scope.account_id,
        account_name=account.name if account else "",
        account_currency=account.currency if account else "USD",
    )


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    scope: Scope = Depends(require_admin_scope),
):
    """Admin-only: invite an additional user INTO THE CALLER'S OWN ACCOUNT.

    There is no way to provision a user into another account — the new user inherits
    ``scope.account_id``, which comes from the caller's token. Use ``POST /auth/signup``
    to start a separate account instead.
    """
    if db.scalar(select(User).where(User.email == payload.email)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
    user = User(
        account_id=scope.account_id,
        email=payload.email,
        hashed_password=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    account = db.get(Account, scope.account_id)
    return UserOut(
        id=user.id,
        email=user.email,
        role=user.role,
        is_active=user.is_active,
        account_id=user.account_id,
        account_name=account.name if account else "",
        account_currency=account.currency if account else "USD",
    )
