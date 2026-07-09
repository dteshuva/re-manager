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


# ---- Audit log (read-only; rows are written by admin actions such as unlock) ----
class AuditLogOut(BaseModel):
    id: str
    user_id: str | None = None
    user_email: str | None = None  # resolved for display; None if the user was deleted
    action: str
    entity: str
    entity_id: str | None = None
    before: dict | None = None
    after: dict | None = None
    created_at: datetime

    class Config:
        from_attributes = True


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


# ---- Portfolio dashboard (read STRICTLY from portfolio_month_summary) ----
class OccupancyMetrics(PnLMetrics):
    """The eight financials plus occupancy, as carried by the property/portfolio rollups."""

    occupancy: float | None = None  # fraction 0..1; NULL if no unit roster
    occupied_units: int
    total_units: int


class SummaryMetrics(OccupancyMetrics):
    """A portfolio-month: occupancy metrics + how many properties rolled into it."""

    property_count: int


class TrendPoint(BaseModel):
    """A single point on the T12 sparklines, from ``portfolio_month_summary``."""

    month: date
    gross_rent: float
    operating_expenses: float
    noi: float
    cash_flow: float
    occupancy: float | None = None


class PortfolioDashboard(BaseModel):
    """Landing-page payload: the selected period's KPIs (summed) with the immediately
    preceding equal-length period for the vs-prior deltas, plus a trailing-12 series for the
    sparklines. From the pre-aggregated summary table, never live line-item aggregation."""

    period_from: date | None
    period_to: date | None
    prior_from: date | None
    prior_to: date | None
    current: SummaryMetrics | None
    prior: SummaryMetrics | None
    trend: list[TrendPoint] = []


# ---- Attention feed (ranked exceptions, computed from the rollups) ----
class AttentionItem(BaseModel):
    """One ranked exception. ``magnitude`` is the $ used for ranking; ``type`` selects the
    detector (noi_drop | expense_spike | vacancy | missing_data) and what change/pct mean.
    ``unit_id`` / ``unit_number`` are set for unit-scoped items in the property feed."""

    type: str
    property_id: str
    property_name: str
    unit_id: str | None = None
    unit_number: str | None = None
    category: str | None = None
    month: date | None = None  # the month the exception occurred in (within the period)
    magnitude: float
    current: float | None = None
    prior: float | None = None
    change: float | None = None
    pct_change: float | None = None
    detail: dict = {}
    label: str
    # Set when this item is a roll-up of many near-identical unit-level items (same
    # property/month/type, tightly-clustered magnitude) into one summary line; see
    # attention._cluster_and_rollup. count is the number of underlying units collapsed.
    rolled_up: bool = False
    count: int | None = None


class AttentionFeed(BaseModel):
    """Exceptions occurring anywhere in [period_from, period_to], each tagged with its month."""

    period_from: date | None
    period_to: date | None
    items: list[AttentionItem] = []
    thresholds: dict = {}


# ---- Attention settings (sub-step 6): per-account configurable thresholds ----
class AttentionSettingsIn(BaseModel):
    """Editable attention thresholds. The %-floor is the primary, size-independent trigger;
    the $-floor is an optional materiality gate (0 = pure percentage)."""

    noi_drop_min_abs: float = Field(ge=0)
    noi_drop_min_pct: float = Field(ge=0)
    expense_spike_min_abs: float = Field(ge=0)
    expense_spike_min_pct: float = Field(ge=0)
    unit_noi_drop_min_abs: float = Field(ge=0)
    unit_noi_drop_min_pct: float = Field(ge=0)
    unit_expense_spike_min_abs: float = Field(ge=0)
    unit_expense_spike_min_pct: float = Field(ge=0)
    vacancy_min_occupancy_drop_pct: float = Field(ge=0)
    vacancy_high_absolute_pct: float = Field(ge=0, le=100)


class AttentionSettingsOut(AttentionSettingsIn):
    updated_at: datetime


# ---- Property detail (sub-step 4): scoped KPI band + unit roster ----
class PropertyDashboard(BaseModel):
    """Property-scoped, period-aware analog of PortfolioDashboard, plus property identity."""

    property_id: str
    property_name: str
    type: str
    period_from: date | None
    period_to: date | None
    prior_from: date | None
    prior_to: date | None
    current: OccupancyMetrics | None
    prior: OccupancyMetrics | None
    trend: list[TrendPoint] = []


class UnitRosterRow(PnLMetrics):
    """One row of the unit roster: this month's metrics + status + NOI change vs prior."""

    unit_id: str
    unit_number: str
    label: str | None = None
    status: str  # "occupied" | "vacant"
    noi_change: float | None = None  # vs prior month (None if no prior data)


class UnitRoster(BaseModel):
    """Server-paginated/sortable unit roster for a property-month."""

    month: date | None
    prior_month: date | None
    total: int  # total units (for pagination)
    rows: list[UnitRosterRow] = []


# ---- Unit detail (sub-step 5): Level 3 ----
class UnitDetailMonth(PnLMetrics):
    """One month of a unit's P&L. ``status`` is vacant when the unit had no rent that month.
    Unit scope excludes property-tier-only items by design (capex/debt at the property tier)."""

    month: date
    status: str  # "occupied" | "vacant"


class UnitDetail(BaseModel):
    """Level 3: a unit's identity, current status, and its full month-by-month P&L.

    The monthly series is spined on the property's summarized months (so vacant months show
    as zeros, not gaps). Property-level shared costs are NOT allocated here — unit cash flow
    is not a pro-rata share of property cash flow."""

    unit_id: str
    unit_number: str
    label: str | None = None
    property_id: str
    property_name: str
    status: str
    months: list[UnitDetailMonth] = []


# ---- PDF statement extraction (free, on-machine) ----
class StatementRow(BaseModel):
    """One extracted-and-resolved line from a PDF statement. ``category_id`` /
    ``unknown_category`` are filled by the endpoint after matching against the global
    category list, so the UI knows which categories it must offer to create first."""

    unit: str | None = None
    category: str
    category_id: str | None = None
    unknown_category: bool = False
    classification: str | None = Field(default=None, pattern=_CLASS_PATTERN)
    amount: float


class StatementPreview(BaseModel):
    """Read-only result of parsing an uploaded statement. Nothing is written: the UI shows
    this for review, offers to create any unknown property/categories, then applies the rows
    through the existing ``/import/rows`` seam (idempotent, lock-protected, dry-run-able)."""

    backend: str  # "ollama" | "heuristic"
    detected_property: str | None = None
    property_id: str | None = None  # matched existing property, if any
    property_unknown: bool = False
    detected_month: date | None = None
    rows: list[StatementRow] = []
    unknown_categories: list[str] = []
    warnings: list[str] = []


class UnknownCategory(BaseModel):
    """A category referenced by a statement that doesn't exist yet, with the classification
    we detected for it — so the UI can offer to create it once (shared across a batch)."""

    name: str
    suggested_classification: str | None = Field(default=None, pattern=_CLASS_PATTERN)


class StatementBatchItem(BaseModel):
    """One file in a batch: either a parsed preview or a parse error (isolated so one bad
    PDF never fails the whole batch)."""

    filename: str
    preview: StatementPreview | None = None
    error: str | None = None


class StatementBatchPreview(BaseModel):
    """Result of parsing many statements at once. ``unknown_properties`` /
    ``unknown_categories`` are de-duplicated across every file so the UI resolves each new
    property/category ONCE for the whole batch rather than per statement."""

    items: list[StatementBatchItem] = []
    unknown_properties: list[str] = []
    unknown_categories: list[UnknownCategory] = []


# ---- Investment insights (per-property acquisition inputs + computed return metrics) ----
class PropertyInvestmentIn(BaseModel):
    """Acquisition inputs the operator enters. ``loan_amount = 0`` ⇒ all-cash purchase."""

    purchase_price: float = Field(ge=0)
    closing_costs: float = Field(default=0, ge=0)
    loan_amount: float = Field(default=0, ge=0)
    purchase_date: date


class PropertyInvestmentOut(PropertyInvestmentIn):
    property_id: str
    equity_invested: float  # purchase_price - loan_amount + closing_costs
    updated_at: datetime


class InvestmentMetrics(BaseModel):
    """Computed return metrics for one property, from acquisition inputs + the rollup.

    Metrics are None when they can't be computed honestly:
    - ``cash_on_cash``: needs >= 12 months of data (cash flow is lumpy — never annualize a stub).
    - ``avg_cash_on_cash``: needs >= 24 months (otherwise it just echoes cash-on-cash).
    - ``dscr``: needs recorded debt service (> 0).
    - anything: needs a positive equity / purchase price and at least one summarized month.

    ``annualized`` is True when the trailing window has < 12 months (cap rate is annualized from
    what's there, with this caveat). All time math keys off months WITH data, not purchase_date."""

    property_id: str
    property_name: str
    type: str
    # Echoed inputs (None when the property has no investment row yet).
    purchase_price: float | None = None
    closing_costs: float | None = None
    loan_amount: float | None = None
    purchase_date: date | None = None
    equity_invested: float | None = None
    # Supporting figures.
    months_available: int = 0          # total summarized months since purchase
    t12_months: int = 0                # months in the trailing-12 window (<12 ⇒ annualized)
    t12_noi: float | None = None
    t12_cash_flow: float | None = None
    t12_debt_service: float | None = None
    annualized: bool = False
    # The four headline metrics (fractions, e.g. 0.062 = 6.2% cap; DSCR is a bare ratio).
    cap_rate: float | None = None
    cash_on_cash: float | None = None
    dscr: float | None = None
    avg_cash_on_cash: float | None = None


class PortfolioInvestment(BaseModel):
    """Every property that has investment inputs, plus value-weighted portfolio aggregates.

    Aggregates are component sums (Σ NOI / Σ price, Σ cash flow / Σ equity), i.e. the natural
    value-weighted average — never a mean of per-property percentages. Each aggregate is scoped
    only to the properties eligible for that metric (e.g. portfolio cash-on-cash counts only
    properties with >= 12 months of data), and ``*_property_count`` reports how many rolled in."""

    properties: list[InvestmentMetrics] = []
    cap_rate: float | None = None
    cash_on_cash: float | None = None
    dscr: float | None = None
    total_purchase_price: float = 0
    total_equity_invested: float = 0
    cap_rate_property_count: int = 0
    cash_on_cash_property_count: int = 0
    dscr_property_count: int = 0
