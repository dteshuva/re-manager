"""Account provisioning — everything a brand-new account needs before it is usable.

An account is not usable the moment its row exists: with no categories you cannot enter a
line item, and with no ``attention_settings`` row the feed silently falls back to config
defaults that the account's admin has no way to see or edit. ``provision_account`` creates
all three together so signup and the seed script cannot drift apart on what "a new account"
means.

The category list is a per-account COPY, not a shared reference. That is forced by the
classification design: a category's ``default_classification`` is resolved at query time,
so reclassifying "Roof Replacement" from capex to operating recomputes NOI — sharing one
row between accounts would let one tenant move another's financials.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Account, AttentionSettings, Category, User
from app.security import hash_password

# The starting chart of accounts handed to every new account. Deliberately the same list
# the seed script uses, so a fresh signup and the demo data classify identically.
DEFAULT_CATEGORIES: dict[str, str] = {
    "Rent": "rent",
    "Property Tax": "operating",
    "Insurance": "operating",
    "Repairs & Maintenance": "operating",
    "Property Management": "operating",
    "Utilities": "operating",
    "Roof Replacement": "capex",
    "Mortgage": "debt_service",
    "Owner Distribution": "other_below_line",
}


def default_thresholds(account_id: str) -> AttentionSettings:
    """A new account's attention thresholds, seeded from the config fallbacks so the
    values the feed actually uses are the values its admin sees in the settings UI."""
    s = get_settings()
    return AttentionSettings(
        account_id=account_id,
        noi_drop_min_abs=s.attention_noi_drop_min_abs,
        noi_drop_min_pct=s.attention_noi_drop_min_pct,
        expense_spike_min_abs=s.attention_expense_spike_min_abs,
        expense_spike_min_pct=s.attention_expense_spike_min_pct,
        unit_noi_drop_min_abs=s.attention_unit_noi_drop_min_abs,
        unit_noi_drop_min_pct=s.attention_unit_noi_drop_min_pct,
        unit_expense_spike_min_abs=s.attention_unit_expense_spike_min_abs,
        unit_expense_spike_min_pct=s.attention_unit_expense_spike_min_pct,
        vacancy_min_occupancy_drop_pct=s.attention_vacancy_min_occupancy_drop_pct,
        vacancy_high_absolute_pct=s.attention_vacancy_high_absolute_pct,
    )


def provision_account(
    db: Session,
    *,
    account_name: str,
    email: str,
    password: str,
) -> tuple[Account, User]:
    """Create an account, its first (admin) user, its category list and its thresholds.

    Does NOT commit — the caller owns the transaction, so a failure part-way through
    (a duplicate email surfacing as an IntegrityError on flush) leaves no half-built
    account behind.
    """
    account = Account(name=account_name)
    db.add(account)
    db.flush()  # need account.id for everything below

    # The account's creator owns it: 'admin' here means account owner (can unlock locked
    # periods, edit thresholds, invite members), never a cross-account superuser.
    user = User(
        account_id=account.id,
        email=email,
        hashed_password=hash_password(password),
        role="admin",
    )
    db.add(user)

    db.add_all(
        Category(account_id=account.id, name=name, default_classification=classification)
        for name, classification in DEFAULT_CATEGORIES.items()
    )
    db.add(default_thresholds(account.id))
    db.flush()
    return account, user
