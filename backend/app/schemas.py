from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field

# The classification enum that drives all the math (mirrors the DB enum). A per-line
# override is optional; NULL means "use the category's default_classification".
_CLASS_PATTERN = "^(rent|operating|capex|debt_service|other_below_line)$"
_PROP_TYPE_PATTERN = "^(multifamily|single)$"
_STATUS_PATTERN = "^(draft|posted|locked)$"


# ---- Auth ----
class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    role: str = Field(default="member", pattern="^(admin|member)$")


class UserOut(BaseModel):
    id: str
    email: EmailStr
    role: str
    is_active: bool

    class Config:
        from_attributes = True


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---- Periods ----
class PeriodStatusOut(BaseModel):
    id: str
    property_id: str
    month: date
    status: str

    class Config:
        from_attributes = True


class PeriodStatusUpsert(BaseModel):
    """Set a property-month's workflow status. Moving *out* of ``locked`` is not allowed
    here — that requires the admin-only ``POST /periods/{id}/unlock`` (which audits)."""

    property_id: str
    month: date
    status: str = Field(pattern=_STATUS_PATTERN)


# ---- Properties (CRUD) ----
class PropertyCreate(BaseModel):
    name: str = Field(min_length=1)
    type: str = Field(pattern=_PROP_TYPE_PATTERN)
    address: str | None = None


class PropertyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    type: str | None = Field(default=None, pattern=_PROP_TYPE_PATTERN)
    address: str | None = None


class PropertyOut(BaseModel):
    id: str
    name: str
    type: str
    address: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True


# ---- Units (CRUD) ----
class UnitCreate(BaseModel):
    unit_number: str = Field(min_length=1)
    label: str | None = None


class UnitUpdate(BaseModel):
    unit_number: str | None = Field(default=None, min_length=1)
    label: str | None = None


class UnitOut(BaseModel):
    id: str
    property_id: str
    unit_number: str
    label: str | None = None

    class Config:
        from_attributes = True


# ---- Categories (CRUD) ----
class CategoryCreate(BaseModel):
    name: str = Field(min_length=1)
    default_classification: str = Field(pattern=_CLASS_PATTERN)
    active: bool = True


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    default_classification: str | None = Field(default=None, pattern=_CLASS_PATTERN)
    active: bool | None = None


class CategoryOut(BaseModel):
    id: str
    name: str
    default_classification: str
    active: bool

    class Config:
        from_attributes = True


# ---- Line items (CRUD) ----
class LineItemCreate(BaseModel):
    category_id: str
    # Optional per-line override of the category's default_classification.
    classification: str | None = Field(default=None, pattern=_CLASS_PATTERN)
    amount: float = 0


class LineItemUpdate(BaseModel):
    classification: str | None = Field(default=None, pattern=_CLASS_PATTERN)
    amount: float | None = None


class LineItemOut(BaseModel):
    id: str
    monthly_record_id: str
    category_id: str
    category_name: str | None = None  # convenience for entry UIs
    classification: str | None = None  # the per-line override, if any
    amount: float


# ---- Monthly records (CRUD) ----
class MonthlyRecordCreate(BaseModel):
    """Upsert a property/unit month. ``unit_id`` NULL ⇒ property-tier record.
    ``line_items`` replaces the record's items wholesale (idempotent save)."""

    property_id: str
    unit_id: str | None = None
    month: date
    notes: str | None = None
    line_items: list[LineItemCreate] = []


class MonthlyRecordUpdate(BaseModel):
    notes: str | None = None


class MonthlyRecordOut(BaseModel):
    id: str
    property_id: str
    unit_id: str | None = None
    month: date
    notes: str | None = None
    line_items: list[LineItemOut] = []


# ---- Import (CSV/Excel + the structured-row automation seam) ----
class ImportRow(BaseModel):
    """One structured, validated import row — the canonical shape the import core
    consumes. The CSV/Excel parser is just one producer of these; a future
    rent-statement/PDF parser would emit the same rows into the same endpoint.

    A property is identified by ``property_id`` or by ``property`` (name). A unit by
    ``unit_id`` or ``unit`` (number); both blank ⇒ a property-tier item. A category by
    ``category_id`` or ``category`` (name). ``classification`` optionally overrides the
    category default. Idempotency is keyed on (property, unit, month, category).
    """

    property: str | None = None
    property_id: str | None = None
    unit: str | None = None
    unit_id: str | None = None
    month: date
    category: str | None = None
    category_id: str | None = None
    classification: str | None = Field(default=None, pattern=_CLASS_PATTERN)
    amount: float
    source_row: int | None = None  # 1-based row in the source file, for error reporting


class ImportIssue(BaseModel):
    row: int | None = None  # 1-based source row (None = file-level problem)
    field: str | None = None
    message: str


class ImportReport(BaseModel):
    dry_run: bool
    on_error: str  # "abort" | "skip"
    committed: bool
    total_rows: int
    valid_rows: int
    invalid_rows: int
    applied_line_items: int
    records_touched: int
    errors: list[ImportIssue] = []


class MissingScope(BaseModel):
    """A property/unit-month with no posted data — for the 'what's missing' view."""

    property_id: str
    property_name: str
    type: str
    unit_id: str | None = None
    unit_number: str | None = None


# ---- P&L (computed, never stored) ----
class PnLMetrics(BaseModel):
    """The eight financial metrics for a scope/period. NOI and cash flow are
    computed in SQL from current classifications, never stored."""

    gross_rent: float
    operating_expenses: float
    noi: float
    capex: float
    debt_service: float
    other_below_line: float
    below_noi: float
    cash_flow: float


class MonthlyPnL(PnLMetrics):
    month: date


class PropertyMonthlyPnL(MonthlyPnL):
    """Property P&L by month with an *honest* rollup split:

    - the top-level metrics are the property total
    - ``units`` is the additive sum across this property's units
    - ``property_tier`` is unit_id-NULL items (shared capex, debt service) recorded
      at the property tier only and NOT allocated down to units

    So ``total = units + property_tier`` per metric, and for capex / debt service
    (and therefore NOI / cash flow) the property total deliberately does NOT equal
    the sum of the units.
    """

    units: PnLMetrics
    property_tier: PnLMetrics


class UnitMonthlyPnL(MonthlyPnL):
    unit_id: str
    unit_number: str
    label: str | None = None


# ---- Breakdown (period totals across the unit → property → portfolio hierarchy) ----
class UnitBreakdown(PnLMetrics):
    unit_id: str
    unit_number: str
    label: str | None = None


class PropertyBreakdown(PnLMetrics):
    """Per-property period totals with the same honest split as the monthly view:
    ``units`` (additive) + ``property_tier`` (unit_id-NULL, not allocated) = the top-level
    total. ``units`` list is the per-unit period totals (empty for single-asset)."""

    property_id: str
    property_name: str
    type: str
    units: list[UnitBreakdown] = []
    property_tier: PnLMetrics


class PortfolioBreakdown(BaseModel):
    """Portfolio total for the period plus a per-property (and per-unit) breakdown."""

    total: PnLMetrics
    properties: list[PropertyBreakdown] = []
