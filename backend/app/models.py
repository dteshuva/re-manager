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
    """

    __tablename__ = "lease"

    id: Mapped[str] = UUID_PK()
    unit_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("units.id", ondelete="CASCADE"), nullable=False
    )
    tenant_name: Mapped[str] = mapped_column(Text, nullable=False)
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
        CheckConstraint("length(trim(tenant_name)) > 0", name="lease_tenant_name_nonempty"),
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("purchase_price >= 0", name="property_investment_purchase_price_nonneg"),
        CheckConstraint("closing_costs >= 0", name="property_investment_closing_costs_nonneg"),
        CheckConstraint("loan_amount >= 0", name="property_investment_loan_amount_nonneg"),
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
