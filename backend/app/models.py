"""SQLAlchemy ORM models.

The physical schema (tables, enums, constraints, and the P&L views) is created by
the Alembic migration in ``migrations/versions/0001_initial.py``. These ORM models
mirror that schema for application code and the seed script. Enum-typed columns are
mapped as plain strings here; the database enforces the real Postgres enums.

Design note on classification (the heart of the spec):
    The math is driven by a category's *current* classification, resolved at query
    time, so reclassifying a category recomputes NOI/cash flow with **no migration**.
    ``line_items.classification`` is an optional per-line *override*; the effective
    classification is ``COALESCE(line_items.classification, categories.default_classification)``.
    See the ``v_line_item_resolved`` / ``v_monthly_pnl`` views.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

UUID_PK = lambda: mapped_column(  # noqa: E731
    UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()")
)


class Account(Base):
    """The tenancy boundary (migration 0017). An account owns its properties, categories,
    attention thresholds and rollups; users belong to exactly one account and can only ever
    see their own account's data.

    Ownership is rooted at ``properties.account_id`` — units, leases, budgets, tags,
    investments and period status hang off a property by FK CASCADE, so resolving a property
    by ``(id, account_id)`` scopes all of them. The tables that are NOT property-derived
    (``users``, ``categories``, ``attention_settings``, ``portfolio_month_summary``,
    ``audit_log``) carry ``account_id`` in their own right, and ``monthly_records`` /
    ``line_items`` / the summary tables carry a denormalized copy — see the migration's
    docstring for the composite FKs that make a cross-account P&L row unrepresentable."""

    __tablename__ = "accounts"

    id: Mapped[str] = UUID_PK()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Display currency for this account's figures (migration 0019). A pure symbol/locale
    # toggle — the stored amounts never change and this is NOT a conversion. Account-tier so
    # the consolidated portfolio P&L stays summable (you can't add £ and $ into one NOI).
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default="USD")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="accounts_name_nonempty"),
        CheckConstraint("currency IN ('USD', 'GBP')", name="accounts_currency_check"),
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = UUID_PK()
    # Email stays globally unique: it is the login identifier, so it has to resolve to one
    # account before we know which account is being logged into.
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    account_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    hashed_password: Mapped[str] = mapped_column(Text, nullable=False)
    # Role is scoped WITHIN the account: 'admin' is the account owner (can unlock locked
    # periods and invite members), not a cross-account superuser.
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="member")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (CheckConstraint("role IN ('admin','member')", name="users_role_check"),)


class Property(Base):
    __tablename__ = "properties"

    id: Mapped[str] = UUID_PK()
    account_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    units: Mapped[list[Unit]] = relationship(back_populates="property", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("type IN ('multifamily','single')", name="properties_type_check"),
        UniqueConstraint("id", "type", name="properties_id_type_key"),
        # Target for the composite FK on monthly_records that keeps a record's account
        # tied to its property's account.
        UniqueConstraint("account_id", "id", name="properties_account_id_id_key"),
    )


class Unit(Base):
    __tablename__ = "units"

    id: Mapped[str] = UUID_PK()
    property_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("properties.id", ondelete="CASCADE"), nullable=False
    )
    unit_number: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    # Migration 0014: flags a synthetic unit planted purely to satisfy the NOT NULL
    # lease.unit_id FK for a property-tier lease on a unit-less "single" property (Cedar
    # Plaza Retail's percentage-rent lease is the only current case). Excluded from the
    # rent roll's occupancy/GPR/unit-count aggregates (see app/queries.py's
    # `_occupancy_summary`) — it has no monthly_records/unit_month_summary of its own, so
    # counting it there would corrupt those aggregates with a phantom vacant/occupied unit.
    is_shell: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # Migration 0015: the unit's market/asking rent — a reference figure independent of any
    # in-place lease (`lease.contract_rent`), used as the "GPR" line of the rent waterfall
    # (app/queries.py's `_rent_waterfall_*`). Nullable: NULL means "no market-rent figure on
    # file yet" (excluded from GPR, not silently treated as $0 or as the in-place rent —
    # see the migration's docstring). Reference-only, same invariant as `contract_rent`/
    # `is_shell`: never feeds NOI/cash-flow.
    market_rent: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    property: Mapped[Property] = relationship(back_populates="units")

    __table_args__ = (
        UniqueConstraint("property_id", "unit_number", name="units_property_unit_number_key"),
        # Target for the composite FK on monthly_records that keeps unit_id tied to property_id.
        UniqueConstraint("property_id", "id", name="units_property_id_id_key"),
        CheckConstraint(
            "market_rent IS NULL OR market_rent >= 0", name="units_market_rent_nonneg"
        ),
    )


class Lease(Base):
    """One tenancy on a unit (migration 0012). A unit keeps FULL history — every lease it
    has ever had is a row here — so the unit's *current* lease is resolved at query time
    (see app/queries.py) as the lease covering today, or, absent one, the most recent lease
    on file. ``contract_rent`` is reference data (what the lease says is owed); it never
    feeds NOI/cash-flow, which stays driven solely by monthly_records/line_items.

    ``status`` is this lease's own authoritative state and is the source of truth for the
    rent roll's occupancy column — distinct from (but meant to agree with) the unit-month
    occupied/vacant/missing status derived from monthly_records.

    Migration 0013 adds six v2 fields, all reference/terms data (same invariant as
    ``contract_rent`` — none feed NOI/cash-flow):
        ``security_deposit``            what the lease collected upfront (nullable).
        ``escalation_pct``               annual (or ``escalation_frequency_months``-cadence)
                                          contract-rent bump, as a percent (nullable).
        ``escalation_frequency_months``  cadence of the bump above; defaults to 12.
        ``lease_type``                   'fixed' | 'mtm' — kept consistent with (derived
                                          from) the pre-existing NULL-``end_date``-means-MTM
                                          convention, not an independent fact.
        ``pct_rent_rate`` /
        ``pct_rent_breakpoint``          percentage-rent terms (retail leases only): the
                                          overage rate and the annual-sales breakpoint above
                                          which it applies. NULL for residential leases.

    Migration 0016 adds ``concession_monthly`` (waterfall follow-up: split concessions out
    of the rent waterfall's ``collections_loss`` line, same reference-data invariant —
    never feeds NOI/cash-flow or the actual collected rent). A standing $/month rent
    discount/concession, distinct from bad debt (delinquency): NULL/unset for the
    overwhelming majority of leases. See the migration's docstring for why this is modeled
    as an ongoing monthly discount rather than a move-in "N free months" figure.

    Migration 0024 adds ``last_rent_increase_date`` / ``rent_before_increase``, which make a
    PERIODIC (rolling, no-end-date) tenancy pricable: an English tenancy's rent rises in
    discrete steps on a stated date (section 13 / by agreement), not on a contractual
    percentage, so "when did the current rent take effect, and what was it before" is the
    schedule — not ``escalation_pct``. See the migration's docstring for how the two
    coexist, and ``app.queries._rent_due_for_month`` for the one place the rule is applied.

    Migration 0025 adds ``opening_arrears`` — the arrears balance brought forward at
    ``start_date``. Arrears itself is DERIVED (rent due vs. rent collected, accumulated over
    the tenancy); this and :class:`ArrearsAdjustment` are the only two stored inputs to it.
    """

    __tablename__ = "lease"

    id: Mapped[str] = UUID_PK()
    unit_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("units.id", ondelete="CASCADE"), nullable=False
    )
    # Optional as of migration 0026: a tenancy onboarded from agent statements often has no
    # name on file, and nothing here computes from it. NULL = not recorded; "" is still rejected
    # so the two can't be confused.
    tenant_name: Mapped[str | None] = mapped_column(Text)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    # NULL end_date = month-to-month (no fixed term, so no rollover horizon applies).
    end_date: Mapped[date | None] = mapped_column(Date)
    contract_rent: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    security_deposit: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    escalation_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    escalation_frequency_months: Mapped[int] = mapped_column(nullable=False, server_default=text("12"))
    # 'fixed' | 'mtm' — see migration 0013's docstring for why this mirrors end_date rather
    # than being independently settable.
    lease_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="fixed")
    pct_rent_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    pct_rent_breakpoint: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    # Migration 0016: standing $/month concession (see class docstring + the migration's
    # docstring for why this beats a "free months at move-in" model for this app's fixed
    # actuals window).
    concession_monthly: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    # Migration 0024 (periodic/rolling tenancies): the date ``contract_rent`` took effect.
    # NULL = never increased, so the rent has been ``contract_rent`` since ``start_date``
    # (every pre-0024 row). When set it also becomes the ESCALATION ANCHOR in place of
    # ``start_date`` — see app/queries.py's ``_rent_due_for_month``.
    last_rent_increase_date: Mapped[date | None] = mapped_column(Date)
    # Migration 0024: the rent immediately BEFORE ``last_rent_increase_date``. NULL = an
    # increase happened but the prior figure isn't held (``contract_rent`` is used for the
    # earlier months). Only meaningful alongside a date — enforced in the DB.
    rent_before_increase: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    # Migration 0025: arrears balance brought forward at ``start_date`` — debt predating
    # this app's records. SIGNED: negative = the tenancy began in credit. NULL/0 for the
    # overwhelming majority. The only stored input to an otherwise fully DERIVED balance
    # (see the migration's docstring and app/queries.py's ``_unit_arrears_ledger``).
    opening_arrears: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    # Migration 0027: the month from which this tenancy's arrears are MEANINGFUL. NULL = from
    # the beginning (every pre-0027 row). Set it to exclude a handover month, where an
    # apportioned-at-completion rent looks identical to a tenant who underpaid. Months before it
    # contribute nothing at all — not "paid", not "owed", simply outside the measurement.
    arrears_from_month: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active','notice','expired','vacant')", name="lease_status_check"
        ),
        CheckConstraint("contract_rent >= 0", name="lease_contract_rent_nonneg"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="lease_end_after_start"),
        CheckConstraint(
            "tenant_name IS NULL OR length(trim(tenant_name)) > 0",
            name="lease_tenant_name_nonempty",
        ),
        CheckConstraint(
            "security_deposit IS NULL OR security_deposit >= 0", name="lease_security_deposit_nonneg"
        ),
        CheckConstraint(
            "escalation_pct IS NULL OR (escalation_pct >= 0 AND escalation_pct <= 100)",
            name="lease_escalation_pct_range",
        ),
        CheckConstraint("escalation_frequency_months > 0", name="lease_escalation_frequency_pos"),
        CheckConstraint("lease_type IN ('fixed','mtm')", name="lease_type_check"),
        CheckConstraint(
            "pct_rent_rate IS NULL OR (pct_rent_rate >= 0 AND pct_rent_rate <= 100)",
            name="lease_pct_rent_rate_range",
        ),
        CheckConstraint(
            "pct_rent_breakpoint IS NULL OR pct_rent_breakpoint >= 0",
            name="lease_pct_rent_breakpoint_nonneg",
        ),
        CheckConstraint(
            "concession_monthly IS NULL OR concession_monthly >= 0",
            name="lease_concession_monthly_nonneg",
        ),
        # Migration 0024: a rent increase can neither predate the tenancy nor postdate its
        # end, and a prior rent with no date for it is uninterpretable (there'd be no month
        # at which it stopped applying) — so it's rejected rather than guessed at.
        CheckConstraint(
            "last_rent_increase_date IS NULL OR last_rent_increase_date >= start_date",
            name="lease_last_increase_after_start",
        ),
        CheckConstraint(
            "last_rent_increase_date IS NULL OR end_date IS NULL "
            "OR last_rent_increase_date <= end_date",
            name="lease_last_increase_within_term",
        ),
        CheckConstraint(
            "rent_before_increase IS NULL OR rent_before_increase >= 0",
            name="lease_rent_before_increase_nonneg",
        ),
        CheckConstraint(
            "rent_before_increase IS NULL OR last_rent_increase_date IS NOT NULL",
            name="lease_rent_before_increase_needs_date",
        ),
        CheckConstraint(
            "arrears_from_month IS NULL "
            "OR date_trunc('month', arrears_from_month) = arrears_from_month",
            name="lease_arrears_from_month_first",
        ),
    )


class ArrearsAdjustment(Base):
    """A signed, dated movement on a tenancy's arrears balance that is NOT a rent shortfall
    (migration 0025).

    Arrears itself is DERIVED — ``rent_due - rent_collected``, accumulated over the tenancy
    (see the migration's docstring for why it is computed rather than stored). Two things
    that derivation cannot express are stored instead: ``lease.opening_arrears`` (the
    balance brought forward from before this app's records) and this table.

    ``kind`` is 'write_off' (arrears forgiven, or recovered from the deposit at check-out —
    always negative), 'charge' (a sum owed that the rent schedule doesn't describe — always
    positive), or 'correction' (either sign; the honest escape hatch). The sign is enforced
    against the kind in the DB, because the sign IS the meaning and can't be left to the
    writer's discretion.

    Keyed to a LEASE, not a unit: an outgoing tenant's debt is theirs, and the running
    balance resets at each tenancy. Carries no ``account_id`` — ownership is transitive
    through lease -> unit -> property, resolved by ``app.scoping.get_lease_or_404``, exactly
    as ``property_certificate``/``property_tag`` resolve theirs through ``property_id``.

    Like every other reference entity here, this NEVER feeds NOI/cash-flow: it moves a
    balance that is reported alongside the P&L, not an amount of income or expense. A
    write-off that should also hit the P&L as bad-debt expense is an ordinary line item,
    entered as such.
    """

    __tablename__ = "arrears_adjustment"

    id: Mapped[str] = UUID_PK()
    lease_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("lease.id", ondelete="CASCADE"), nullable=False
    )
    month: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "date_trunc('month', month) = month", name="arrears_adjustment_month_first"
        ),
        CheckConstraint(
            "kind IN ('write_off','charge','correction')", name="arrears_adjustment_kind_check"
        ),
        CheckConstraint("amount <> 0", name="arrears_adjustment_amount_nonzero"),
        CheckConstraint(
            "(kind = 'write_off' AND amount < 0) OR (kind = 'charge' AND amount > 0) "
            "OR kind = 'correction'",
            name="arrears_adjustment_kind_sign",
        ),
    )


class Category(Base):
    """Per-account category list (account-scoped as of migration 0017). Reclassifying a
    category (changing ``default_classification``) recomputes the financials with no
    migration — which is precisely why the list cannot be shared across accounts: one
    account's reclassification would otherwise move another account's NOI. A new account
    gets its own copy of ``app.defaults.DEFAULT_CATEGORIES`` at signup."""

    __tablename__ = "categories"

    id: Mapped[str] = UUID_PK()
    account_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    default_classification: Mapped[str] = mapped_column(
        String, nullable=False
    )  # DB type: classification enum
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # Two accounts must both be able to have a category called "Rent".
        UniqueConstraint("account_id", "name", name="categories_account_name_key"),
        # Target for line_items' composite FK (a line item may only use its own
        # account's categories).
        UniqueConstraint("account_id", "id", name="categories_account_id_id_key"),
    )


class MonthlyRecord(Base):
    """One record per (property, [unit], month). ``unit_id IS NULL`` ⇒ a property-tier
    record where shared / property-only items (capex, debt service) live and are NOT
    allocated down to units."""

    __tablename__ = "monthly_records"

    id: Mapped[str] = UUID_PK()
    account_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    property_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("properties.id", ondelete="CASCADE"), nullable=False
    )
    unit_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("units.id", ondelete="CASCADE")
    )
    month: Mapped[date] = mapped_column(Date, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    # Explicit "this unit is vacant this month" flag (migration 0011), distinct from having
    # no monthly_record at all. Meaningless for property-tier records (unit_id IS NULL).
    is_vacant: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # foreign_keys is required as of migration 0017: line_items now reaches this table by
    # TWO paths (the plain monthly_record_id FK and the composite account-scoped one), so
    # SQLAlchemy can't infer which to join on. The plain id FK is the relationship; the
    # composite one exists purely as an integrity constraint.
    line_items: Mapped[list[LineItem]] = relationship(
        back_populates="monthly_record",
        cascade="all, delete-orphan",
        foreign_keys="LineItem.monthly_record_id",
    )

    __table_args__ = (
        CheckConstraint("date_trunc('month', month) = month", name="monthly_records_month_first_check"),
        # Keep unit_id honest: it must belong to property_id (NULL is allowed = property tier).
        ForeignKeyConstraint(
            ["property_id", "unit_id"],
            ["units.property_id", "units.id"],
            name="monthly_records_unit_in_property_fk",
            ondelete="CASCADE",
        ),
        # Same idea one level up (migration 0017): the record's account must be the
        # property's account, so a record can never be filed under another tenant.
        ForeignKeyConstraint(
            ["account_id", "property_id"],
            ["properties.account_id", "properties.id"],
            name="monthly_records_property_in_account_fk",
            ondelete="CASCADE",
        ),
        # Target for line_items' composite FK.
        UniqueConstraint("account_id", "id", name="monthly_records_account_id_id_key"),
    )


class LineItem(Base):
    __tablename__ = "line_items"

    id: Mapped[str] = UUID_PK()
    account_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), nullable=False
    )
    monthly_record_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("monthly_records.id", ondelete="CASCADE"), nullable=False
    )
    category_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("categories.id"), nullable=False
    )
    # Optional per-line override. NULL ⇒ use the category's default_classification.
    classification: Mapped[str | None] = mapped_column(String)  # DB type: classification enum
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default=text("0"))
    # Migration 0023: this line is stated as a RATE, not a figure — "8% of rent" (a management
    # fee, most often). NULL = an ordinary fixed amount, which every pre-existing row is.
    # When set, ``amount`` is DERIVED from the rent in this record's scope and rewritten by
    # :func:`app.percent_lines.apply_percentage_lines` on every write to the property-month,
    # so the fee follows rent instead of going stale. ``amount`` remains the only thing the
    # P&L views, rollups and exports read; this column is intent, not a second ledger.
    rate_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    # Migration 0022: which shared expense POSTED this line, if any. NULL = hand-entered or
    # imported (every pre-existing row). This is what makes a shared expense re-postable: it
    # identifies exactly the lines that arrangement owns, so a re-post updates them in place,
    # an un-post removes them, and a hand-entered line in the same category is detected as a
    # conflict rather than silently overwritten.
    shared_expense_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("shared_expense.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # See MonthlyRecord.line_items for why foreign_keys is spelled out on both sides.
    monthly_record: Mapped[MonthlyRecord] = relationship(
        back_populates="line_items", foreign_keys=[monthly_record_id]
    )
    category: Mapped[Category] = relationship(foreign_keys=[category_id])

    __table_args__ = (
        # Idempotent import key: re-importing the same month+category overwrites in place.
        UniqueConstraint("monthly_record_id", "category_id", name="line_items_record_category_key"),
        CheckConstraint(
            "rate_pct IS NULL OR (rate_pct >= 0 AND rate_pct <= 100)",
            name="line_items_rate_pct_range",
        ),
        # Migration 0017's core isolation invariant: a line item's record AND its category
        # must both belong to the line item's account. Because reclassifying a category
        # recomputes NOI, letting account A's line item point at account B's category would
        # let B move A's financials — these two FKs make that unrepresentable rather than
        # merely rejected by an app-level check.
        ForeignKeyConstraint(
            ["account_id", "monthly_record_id"],
            ["monthly_records.account_id", "monthly_records.id"],
            name="line_items_record_in_account_fk",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["account_id", "category_id"],
            ["categories.account_id", "categories.id"],
            name="line_items_category_in_account_fk",
        ),
        # Same invariant one entity further out (migration 0022): a line item can only be
        # attributed to a shared expense of its OWN account.
        ForeignKeyConstraint(
            ["account_id", "shared_expense_id"],
            ["shared_expense.account_id", "shared_expense.id"],
            name="line_items_shared_expense_in_account_fk",
            ondelete="SET NULL",
        ),
    )


class PropertyInvestment(Base):
    """Per-property acquisition inputs (migration 0008). Nullable 1:1 with ``properties``:
    a property may have no investment row, in which case return metrics are unavailable.

    Only the raw inputs are stored. ``equity_invested`` (= purchase_price - loan_amount +
    closing_costs) and all return metrics are computed on read from these inputs plus the
    ``property_month_summary`` rollup, never persisted — so they always reflect the current
    P&L. ``loan_amount = 0`` means an all-cash purchase."""

    __tablename__ = "property_investment"

    property_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("properties.id", ondelete="CASCADE"), primary_key=True
    )
    purchase_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    closing_costs: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default=text("0"))
    loan_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default=text("0"))
    purchase_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Set when this row was produced by a bulk/portfolio purchase (migration 0021): the
    # closing costs and loan above are then this property's ALLOCATED share of one deal-wide
    # total, not figures entered for it alone. NULL = an ordinary standalone purchase.
    acquisition_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("portfolio_acquisition.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("purchase_price >= 0", name="property_investment_purchase_price_nonneg"),
        CheckConstraint("closing_costs >= 0", name="property_investment_closing_costs_nonneg"),
        CheckConstraint("loan_amount >= 0", name="property_investment_loan_amount_nonneg"),
    )


class PortfolioAcquisition(Base):
    """A bulk ("portfolio") purchase: one deal covering several properties (migration 0021).

    Each property in such a deal has its own agreed price, but the closing costs are a single
    settlement figure and the debt is one blanket loan over the whole package — costs that
    belong to N properties at once and have nowhere to live on a per-property row. This table
    holds the deal as the deal: the shared ``purchase_date``, the ONE ``total_closing_costs``,
    the ONE ``total_loan_amount``, and the ``allocation_method`` that says how to spread them.

    The split is still WRITTEN DOWN onto each member's ``property_investment`` row (linked back
    by ``acquisition_id``), so every downstream metric reads exactly one place and needs no
    knowledge of bulk deals. Because pro-rata shares sum back to the true totals, the portfolio
    aggregates come out identical to modelling the deal as one entity.

    ``allocation_method``:
      * ``price``  — pro-rata by each property's agreed purchase price (the default, and what
        lenders and accountants use for blanket debt).
      * ``equal``  — split evenly across members, for deals where price is a poor proxy.
      * ``custom`` — the operator supplies each share directly (e.g. the lender's per-property
        release prices); the totals here are then the SUM of those shares, so they stay true.

    Deleting a deal UNGROUPS it — ``ON DELETE SET NULL`` leaves each member's allocated figures
    in place, since removing a bulk purchase should never wipe the acquisition data the return
    metrics depend on."""

    __tablename__ = "portfolio_acquisition"

    id: Mapped[str] = UUID_PK()
    account_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    purchase_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_closing_costs: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, server_default=text("0")
    )
    total_loan_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, server_default=text("0")
    )
    allocation_method: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'price'")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="portfolio_acquisition_name_nonempty"),
        CheckConstraint("total_closing_costs >= 0", name="portfolio_acquisition_closing_nonneg"),
        CheckConstraint("total_loan_amount >= 0", name="portfolio_acquisition_loan_nonneg"),
        CheckConstraint(
            "allocation_method IN ('price', 'equal', 'custom')",
            name="portfolio_acquisition_method_known",
        ),
    )


class SharedExpense(Base):
    """A cost incurred for several properties at once, stored once and split across them
    (migration 0022) — a blanket-loan payment, a multi-property insurance policy, one
    management retainer.

    The ledger is per-property by construction: a ``line_item`` hangs off a ``monthly_record``
    keyed to exactly one property. A bill that belongs to N properties therefore has nowhere
    to live, and the operator had to divide it by hand and re-type the share onto every
    property, every month. This table holds the ARRANGEMENT — the category it posts to, the
    periodic ``amount``, the members, and how to spread it — so the division is entered once
    and repeated by the machine.

    Posting is not a special case downstream. It writes an ordinary line item onto each
    member's property-tier record (``unit_id IS NULL``, where shared costs already live), so
    the P&L views, NOI, cash flow, exports and variance keep reading exactly one place and
    need no knowledge that shared expenses exist. Because :mod:`app.allocation` guarantees the
    shares sum back to ``amount`` to the cent, the portfolio total is exactly the bill.

    ``allocation_method``:
      * ``equal``  — every member carries the same share (the default: what a portfolio loan
        payment or a blanket policy is usually apportioned as when nothing better is known).
      * ``price``  — pro-rata by each member's ``property_investment.purchase_price``; the
        conventional basis for debt service on a blanket loan.
      * ``units``  — pro-rata by rentable unit count, for costs that scale with doors. A
        unit-less "single" property counts as one dwelling, never zero.
      * ``custom`` — the operator supplies each member's share directly (e.g. the insurer's
        per-building premium); ``amount`` is then the SUM of those shares, so it stays true.

    ``price`` and ``units`` weights are resolved AT POST TIME from the current portfolio, so a
    property that has since been repriced or gained doors carries its current share rather
    than one frozen when the arrangement was created.
    """

    __tablename__ = "shared_expense"

    id: Mapped[str] = UUID_PK()
    account_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    category_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    # Optional per-line override, carried onto every line item this arrangement posts —
    # same semantics as LineItem.classification. NULL ⇒ the category's default.
    classification: Mapped[str | None] = mapped_column(String)  # DB type: classification enum
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default=text("0"))
    allocation_method: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'equal'")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    members: Mapped[list[SharedExpenseMember]] = relationship(
        back_populates="shared_expense",
        cascade="all, delete-orphan",
        foreign_keys="SharedExpenseMember.shared_expense_id",
    )

    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="shared_expense_name_nonempty"),
        CheckConstraint("amount >= 0", name="shared_expense_amount_nonneg"),
        CheckConstraint(
            "allocation_method IN ('equal', 'price', 'units', 'custom')",
            name="shared_expense_method_known",
        ),
        # Migration 0017's invariant: an arrangement may only post into its own account's
        # category, because reclassifying a category moves NOI.
        ForeignKeyConstraint(
            ["account_id", "category_id"],
            ["categories.account_id", "categories.id"],
            name="shared_expense_category_in_account_fk",
        ),
        # Target for the composite FKs on shared_expense_member and line_items.
        UniqueConstraint("account_id", "id", name="shared_expense_account_id_id_key"),
    )


class SharedExpenseMember(Base):
    """One property covered by a :class:`SharedExpense` (migration 0022).

    ``custom_share`` is meaningful only under ``allocation_method = 'custom'``, where the
    operator states each property's share outright; under the computed methods it is ignored.

    ``account_id`` is carried rather than resolved through the parent purely so the composite
    FKs can exist: they make a member whose property belongs to a DIFFERENT account than the
    arrangement unrepresentable at the database level, not merely rejected by app code."""

    __tablename__ = "shared_expense_member"

    shared_expense_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("shared_expense.id", ondelete="CASCADE"),
        primary_key=True,
    )
    property_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    account_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    custom_share: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # foreign_keys is spelled out for the same reason as MonthlyRecord.line_items: this table
    # reaches shared_expense by two paths (the plain FK and the composite account-scoped one).
    shared_expense: Mapped[SharedExpense] = relationship(
        back_populates="members", foreign_keys=[shared_expense_id]
    )

    __table_args__ = (
        CheckConstraint(
            "custom_share IS NULL OR custom_share >= 0",
            name="shared_expense_member_share_nonneg",
        ),
        ForeignKeyConstraint(
            ["account_id", "shared_expense_id"],
            ["shared_expense.account_id", "shared_expense.id"],
            name="shared_expense_member_expense_in_account_fk",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["account_id", "property_id"],
            ["properties.account_id", "properties.id"],
            name="shared_expense_member_property_in_account_fk",
            ondelete="CASCADE",
        ),
    )


class PropertyBudget(Base):
    """Flat annual plan per property (migration 0009), used for actual-vs-plan variance.

    Deliberately minimal: one row per (property, year) with an annual budgeted gross rent
    and operating-expense figure. Budgeted NOI = rent - opex (computed, never stored, same
    rule as actuals). The monthly plan is the annual figure ÷ 12, pro-rated across whatever
    date range a variance query covers. This is a NEW parallel table — it does not touch
    ``monthly_records``/``line_items`` and has no effect on actual NOI/cash-flow math."""

    __tablename__ = "property_budget"

    property_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("properties.id", ondelete="CASCADE"), primary_key=True
    )
    year: Mapped[int] = mapped_column(primary_key=True)
    budgeted_gross_rent: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    budgeted_operating_expenses: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("year >= 2000 AND year <= 2100", name="property_budget_year_range"),
        CheckConstraint("budgeted_gross_rent >= 0", name="property_budget_rent_nonneg"),
        CheckConstraint(
            "budgeted_operating_expenses >= 0", name="property_budget_opex_nonneg"
        ),
    )


class PropertyTag(Base):
    """Free-text portfolio segmentation tag (migration 0010). A property can carry any
    number of tags (region, fund/entity, asset class, submarket, ...); this is a pure
    filter/segmentation axis — it never touches actuals math. Tags are case-sensitive,
    exact-match strings; the portfolio dashboard/breakdown/attention endpoints accept an
    optional ``tags`` filter with OR semantics (a property matches if it carries ANY of
    the requested tags)."""

    __tablename__ = "property_tag"

    property_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("properties.id", ondelete="CASCADE"), primary_key=True
    )
    tag: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (CheckConstraint("length(trim(tag)) > 0", name="property_tag_tag_nonempty"),)


class PropertyCertificate(Base):
    """A statutory compliance certificate / licence held on a property (migration 0020).

    UK rentals must keep current certificates — EICR (electrical), Gas Safety / CP12, EPC,
    HMO or selective licences, PAT, Legionella and fire-risk assessments. ``cert_type`` is
    free-text (the UI offers a preset list) so any jurisdiction's requirements can be recorded
    without a migration per kind. ``expiry_date`` is the only required date; the valid /
    expiring / expired STATUS is derived from it at read time (never stored), so it is always
    correct as the calendar advances. Like ``property_tag`` / ``property_budget`` this is a
    pure compliance axis and never touches monthly_records/line_items or any actuals math.

    Ownership is transitive through ``property_id`` (FK CASCADE), so a certificate is scoped
    to an account by resolving its property via ``get_property_or_404`` — it carries no
    ``account_id`` of its own, same as ``property_tag``."""

    __tablename__ = "property_certificate"

    id: Mapped[str] = UUID_PK()
    property_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("properties.id", ondelete="CASCADE"), nullable=False
    )
    cert_type: Mapped[str] = mapped_column(Text, nullable=False)
    reference: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(Text)
    issue_date: Mapped[date | None] = mapped_column(Date)
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("length(trim(cert_type)) > 0", name="property_certificate_type_nonempty"),
        CheckConstraint(
            "issue_date IS NULL OR expiry_date >= issue_date",
            name="property_certificate_dates_ordered",
        ),
    )


class PeriodStatus(Base):
    """Per property-month workflow state: draft → posted → locked.
    Locked months are unlockable by an admin only (see app/routers/periods.py)."""

    __tablename__ = "period_status"

    id: Mapped[str] = UUID_PK()
    property_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("properties.id", ondelete="CASCADE"), nullable=False
    )
    month: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="draft")  # enum
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("property_id", "month", name="period_status_property_month_key"),)


class PropertyMonthSummary(Base):
    """Pre-aggregated property-month rollup (INSIGHT_DASHBOARD_SPEC sub-step 1).

    DERIVED from ``v_monthly_pnl`` and written only by the SQL refresh functions
    (``refresh_property_month_summary`` / ``rebuild_all_summaries``), never by the
    ORM. Raw line items stay the source of truth; this table makes dashboards fast
    and reconciles exactly with the live P&L queries. Aggregates unit rows AND the
    property-tier row, but ``occupied_units`` / ``total_units`` are unit-scoped."""

    __tablename__ = "property_month_summary"

    property_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("properties.id", ondelete="CASCADE"), primary_key=True
    )
    month: Mapped[date] = mapped_column(Date, primary_key=True)
    # Denormalized from properties (migration 0017) so an account's dashboard read is one
    # indexed scan rather than a join back through properties. Written by the SQL refresh
    # functions, same as every other column here.
    account_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    gross_rent: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    operating_expenses: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    noi: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    capex: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    debt_service: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    other_below_line: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    below_noi: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    cash_flow: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    occupied_units: Mapped[int] = mapped_column(nullable=False)
    total_units: Mapped[int] = mapped_column(nullable=False)
    occupancy: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UnitMonthSummary(Base):
    """Pre-aggregated unit-month rollup (INSIGHT_DASHBOARD_SPEC sub-step 4).

    Powers the property's unit roster (server-sortable/paginated) and the property-scoped
    unit attention feed (which units dropped / went vacant) without scanning raw line items.
    DERIVED from ``v_monthly_pnl`` (unit rows only); a unit with no record in a month simply
    has no row here (= vacant). Carries ``property_id`` so a property's units query is a single
    indexed read. Written only by the SQL refresh functions."""

    __tablename__ = "unit_month_summary"

    unit_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("units.id", ondelete="CASCADE"), primary_key=True
    )
    month: Mapped[date] = mapped_column(Date, primary_key=True)
    property_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("properties.id", ondelete="CASCADE"), nullable=False
    )
    # Denormalized from properties (migration 0017) — see PropertyMonthSummary.account_id.
    account_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    gross_rent: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    operating_expenses: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    noi: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    capex: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    debt_service: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    other_below_line: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    below_noi: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    cash_flow: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # Migration 0011: copied from the underlying monthly_records row so a row's PRESENCE
    # here (this unit had a record posted) can be told apart from an explicit vacancy
    # (a record exists, is_vacant=true, $0 rent) vs no record at all (row absent entirely).
    is_vacant: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PortfolioMonthSummary(Base):
    """Pre-aggregated portfolio-month rollup, summed from the property rows above
    (so it can never disagree with the property tier). See PropertyMonthSummary.

    "Portfolio" means *this account's* portfolio as of migration 0017: the primary key is
    (account_id, month), and ``refresh_portfolio_month_summary`` takes the account as a
    required argument — the old month-only form that summed every property in the database
    was dropped outright."""

    __tablename__ = "portfolio_month_summary"

    account_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    month: Mapped[date] = mapped_column(Date, primary_key=True)
    gross_rent: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    operating_expenses: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    noi: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    capex: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    debt_service: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    other_below_line: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    below_noi: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    cash_flow: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    occupied_units: Mapped[int] = mapped_column(nullable=False)
    total_units: Mapped[int] = mapped_column(nullable=False)
    occupancy: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))
    property_count: Mapped[int] = mapped_column(nullable=False)
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AttentionSettings(Base):
    """Attention-feed thresholds, one row per account (sub-step 6; made genuinely
    per-account by migration 0017, which replaced the ``id = 1`` single-row pin with an
    ``account_id`` primary key). A row is created for an account at signup."""

    __tablename__ = "attention_settings"

    account_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    noi_drop_min_abs: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    noi_drop_min_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    expense_spike_min_abs: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    expense_spike_min_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    unit_noi_drop_min_abs: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    unit_noi_drop_min_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    unit_expense_spike_min_abs: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    unit_expense_spike_min_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    # Size-independent vacancy trigger: flag when occupancy falls by >= this many pp.
    vacancy_min_occupancy_drop_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    # Absolute vacancy trigger: flag any property whose vacancy rate is >= this %, regardless
    # of month-over-month change (a persistently very-empty property is a standing problem).
    vacancy_high_absolute_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = UUID_PK()
    account_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str | None] = mapped_column(Text)
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StatementFormat(Base):
    """One sender's statement layout, and what this account's operator decided its words mean.

    The parser reads figures off the page without a template (see :mod:`app.statements`);
    this is where the MEANINGS live — that this agent's "Mgt Fee" is the account's
    "Management Fee", that "64 King Edward Street" is the property stored as "64 King Edward
    St, Gateshead". Keyed by :func:`app.statements.fingerprint`, which hashes the layout's
    stable parts, so next month's statement from the same agent finds the same answers.

    Applied when building the review PREVIEW, never at import: a saved format changes what is
    proposed, and a human still presses the button. See migration 0028.
    """

    __tablename__ = "statement_format"

    id: Mapped[str] = UUID_PK()
    account_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    shape: Mapped[str] = mapped_column(Text, nullable=False)
    sample: Mapped[str | None] = mapped_column(Text)
    # raw label (lowercased, as printed) -> categories.id / properties.id. JSONB, so the ids
    # are validated against the caller's own account on the way in AND on the way out.
    category_aliases: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    property_aliases: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    classification_overrides: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    month_rule: Mapped[str | None] = mapped_column(Text)
    times_used: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("account_id", "fingerprint", name="statement_format_account_fingerprint_key"),
    )
