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


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = UUID_PK()
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="member")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (CheckConstraint("role IN ('admin','member')", name="users_role_check"),)


class Property(Base):
    __tablename__ = "properties"

    id: Mapped[str] = UUID_PK()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    units: Mapped[list[Unit]] = relationship(back_populates="property", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("type IN ('multifamily','single')", name="properties_type_check"),
        UniqueConstraint("id", "type", name="properties_id_type_key"),
    )


class Unit(Base):
    __tablename__ = "units"

    id: Mapped[str] = UUID_PK()
    property_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("properties.id", ondelete="CASCADE"), nullable=False
    )
    unit_number: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    property: Mapped[Property] = relationship(back_populates="units")

    __table_args__ = (
        UniqueConstraint("property_id", "unit_number", name="units_property_unit_number_key"),
        # Target for the composite FK on monthly_records that keeps unit_id tied to property_id.
        UniqueConstraint("property_id", "id", name="units_property_id_id_key"),
    )


class Category(Base):
    """Global, shared category list. Reclassifying a category (changing
    ``default_classification``) recomputes the financials with no migration."""

    __tablename__ = "categories"

    id: Mapped[str] = UUID_PK()
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    default_classification: Mapped[str] = mapped_column(
        String, nullable=False
    )  # DB type: classification enum
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MonthlyRecord(Base):
    """One record per (property, [unit], month). ``unit_id IS NULL`` ⇒ a property-tier
    record where shared / property-only items (capex, debt service) live and are NOT
    allocated down to units."""

    __tablename__ = "monthly_records"

    id: Mapped[str] = UUID_PK()
    property_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("properties.id", ondelete="CASCADE"), nullable=False
    )
    unit_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("units.id", ondelete="CASCADE")
    )
    month: Mapped[date] = mapped_column(Date, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    line_items: Mapped[list[LineItem]] = relationship(
        back_populates="monthly_record", cascade="all, delete-orphan"
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
    )


class LineItem(Base):
    __tablename__ = "line_items"

    id: Mapped[str] = UUID_PK()
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

    monthly_record: Mapped[MonthlyRecord] = relationship(back_populates="line_items")
    category: Mapped[Category] = relationship()

    __table_args__ = (
        # Idempotent import key: re-importing the same month+category overwrites in place.
        UniqueConstraint("monthly_record_id", "category_id", name="line_items_record_category_key"),
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


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = UUID_PK()
    user_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str | None] = mapped_column(Text)
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
