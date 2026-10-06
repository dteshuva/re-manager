from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field

# The classification enum that drives all the math (mirrors the DB enum). A per-line
# override is optional; NULL means "use the category's default_classification".
_CLASS_PATTERN = "^(rent|operating|capex|debt_service|other_below_line)$"
_PROP_TYPE_PATTERN = "^(multifamily|single)$"
_STATUS_PATTERN = "^(draft|posted|locked)$"


# ---- Auth ----
class SignupRequest(BaseModel):
    """Self-serve registration. Creates a NEW account, not a user inside an existing one —
    ``POST /auth/users`` is the admin-only path for that."""

    email: EmailStr
    password: str = Field(min_length=8)
    # Optional: defaults to "<email>'s portfolio" so signup is a two-field form.
    account_name: str | None = Field(default=None, max_length=120)


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    role: str = Field(default="member", pattern="^(admin|member)$")


class UserOut(BaseModel):
    id: str
    email: EmailStr
    role: str
    is_active: bool
    # Which account this user acts for — the tenancy boundary, surfaced so the UI can
    # show whose portfolio is on screen.
    account_id: str
    account_name: str
    # Display currency for this account ('USD' | 'GBP', migration 0019). The frontend uses it
    # to pick the currency symbol/locale for every figure it formats.
    account_currency: str = "USD"

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
    # Migration 0015 — the unit's market/asking rent (waterfall follow-up #1): an
    # admin-editable reference figure so analysts can load a real comp/appraisal/market-
    # survey rent instead of the synthetic `backfill_unit_market_rent.py` value. Reference
    # data only — never feeds NOI/cash-flow. `ge=0` mirrors the column's own
    # `units_market_rent_nonneg` CHECK constraint (belt-and-suspenders: a clean 422 here
    # beats a raw DB IntegrityError). Admin-gated at the router (`require_admin`), same
    # convention as every other portfolio-shaping mutation (leases/budgets/tags).
    market_rent: float | None = Field(default=None, ge=0)


class UnitOut(BaseModel):
    id: str
    property_id: str
    unit_number: str
    label: str | None = None
    market_rent: float | None = None

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
    # Only populated when GET /categories?include_usage=true is passed (one extra grouped
    # count query) — how many line items currently reference this category. 0 flags a
    # category as an unused cleanup/merge candidate (e.g. PDF-import junk).
    usage_count: int | None = None

    class Config:
        from_attributes = True


class CategoryMergeIn(BaseModel):
    target_id: str
    # A source/target with DIFFERENT classifications moves real dollars across the NOI line
    # (e.g. a `rent` category merged into `operating`), not just consolidating the breakdown —
    # require an explicit opt-in for that case (the UI's own confirm dialog already warns about
    # it; this is the server-side guard for a direct API call that bypasses the UI).
    allow_classification_change: bool = False


class CategoryMergeOut(BaseModel):
    source_id: str
    target_id: str
    reassigned_count: int
    deactivated: bool


# ---- Line items (CRUD) ----
# A line is stated ONE of two ways: as a figure (``amount``) or as a rate (``rate_pct`` —
# "management: 8% of rent", migration 0023). They are mutually exclusive on input: when
# ``rate_pct`` is given the server DERIVES the amount from that property-month's rent and
# rewrites it whenever rent changes, so any amount sent alongside would be a second, instantly
# stale opinion about the same money. ``amount`` is always populated on output.
class LineItemCreate(BaseModel):
    category_id: str
    # Optional per-line override of the category's default_classification.
    classification: str | None = Field(default=None, pattern=_CLASS_PATTERN)
    amount: float = 0
    rate_pct: float | None = Field(default=None, ge=0, le=100)


class LineItemUpdate(BaseModel):
    classification: str | None = Field(default=None, pattern=_CLASS_PATTERN)
    amount: float | None = None
    # Send a rate to convert the line to a percentage; send an amount to convert it back to a
    # fixed figure (which clears the rate). Sending both is rejected.
    rate_pct: float | None = Field(default=None, ge=0, le=100)


class LineItemOut(BaseModel):
    id: str
    monthly_record_id: str
    category_id: str
    category_name: str | None = None  # convenience for entry UIs
    classification: str | None = None  # the per-line override, if any
    amount: float
    # NULL ⇒ an ordinary typed figure. Set ⇒ ``amount`` above is this rate applied to the
    # rent basis for the line's scope, maintained by the server.
    rate_pct: float | None = None


class RentBasisOut(BaseModel):
    """The rent a percentage line would be charged on, for one scope of a property-month.

    Lets the entry form show "8% of £4,200 = £336" before anything is saved, using the same
    basis the server will use on save — units the operator is not currently looking at
    included, which is precisely the arithmetic they cannot do in their head.
    """

    property_id: str
    unit_id: str | None = None
    month: date
    # Rent in the requested scope: the whole property's rent when unit_id is omitted
    # (the property-tier basis), else that unit's own rent.
    rent: float


# ---- Monthly records (CRUD) ----
class MonthlyRecordCreate(BaseModel):
    """Upsert a property/unit month. ``unit_id`` NULL ⇒ property-tier record.
    ``line_items`` replaces the record's items wholesale (idempotent save). ``is_vacant``
    explicitly marks a unit-month as vacant (distinct from simply never posting a record for
    it) — meaningless for a property-tier record (``unit_id`` NULL) and ignored there."""

    property_id: str
    unit_id: str | None = None
    month: date
    notes: str | None = None
    is_vacant: bool = False
    line_items: list[LineItemCreate] = []


class MonthlyRecordUpdate(BaseModel):
    notes: str | None = None
    is_vacant: bool | None = None


class MonthlyRecordOut(BaseModel):
    id: str
    property_id: str
    unit_id: str | None = None
    month: date
    notes: str | None = None
    is_vacant: bool = False
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
    # Carried so the KPI band's debt-service card can draw the same sparkline the others do.
    # It is the one step between NOI and cash flow that is almost always the largest, and a
    # band that jumped straight from one to the other left the gap unexplained.
    debt_service: float = 0.0
    cash_flow: float
    occupancy: float | None = None


class CategoryAmount(BaseModel):
    """One category's total for a property over a period, with the classification it CURRENTLY
    carries — so a reclassification moves it between sections with no backfill."""

    category_id: str
    category: str
    classification: str = Field(pattern=_CLASS_PATTERN)
    amount: float


class PropertyCategoryBreakdown(BaseModel):
    """What the property's headline figures are actually made of.

    Returned for every classification rather than just operating: the same query answers
    "what is the £412 of operating expenses" and "what is the below-NOI figure", and the
    second question is the reason a single below-NOI number was confusing in the first place.
    """

    property_id: str
    period_from: date | None = None
    period_to: date | None = None
    rows: list[CategoryAmount] = []


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
    detector (noi_drop | expense_spike | vacancy | occupancy_drop | high_vacancy |
    missing_data | arrears) and what change/pct mean. For ``occupancy_drop``,
    ``current``/``prior`` are occupancy PERCENTAGES and ``change`` is the movement in
    percentage points (``pct_change`` is null — the change is already a percentage-point
    figure, not a percent-of-percent).
    ``unit_id`` / ``unit_number`` are set for unit-scoped items in the property feed.

    For ``arrears`` (migration 0025) ``magnitude``/``current`` are the tenancy's CUMULATIVE
    unpaid balance, ``change`` is the movement into the flagged month and ``prior`` the
    balance carried in before it; ``month`` is the unit's WORST month in the period, reported
    once rather than repeated every month the balance stood (see ``attention._arrears``).
    ``detail`` carries the tenant, the lease, that month's rent due/collected, the balance in
    months of rent, and ``months_over_threshold``."""

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


# ---- Portfolio-wide worst-units leaderboard (top-N units by NOI drop, across every
#      property, ranked by $ magnitude — complements the property-scoped attention feed) ----
class WorstUnitItem(BaseModel):
    """One leaderboard row: a single unit's NOI drop, OR — when a tight cluster of
    same-property/same-month/same-magnitude drops collapses via ``_cluster_and_rollup`` — a
    rolled-up summary row for many units at once (``rolled_up=True``, ``unit_id``/``unit_number``
    None, ``count`` set). See ``attention.worst_units``."""

    property_id: str
    property_name: str
    unit_id: str | None = None
    unit_number: str | None = None
    month: date
    current: float | None = None
    prior: float | None = None
    change: float | None = None  # negative (a drop); None for a rolled-up row (see detail)
    pct_change: float | None = None
    magnitude: float  # abs($change), or the cluster total for a rolled-up row; the ranking key
    label: str
    rolled_up: bool = False
    count: int | None = None  # number of units collapsed into this row, when rolled_up


class WorstUnitsLeaderboard(BaseModel):
    period_from: date | None
    period_to: date | None
    items: list[WorstUnitItem] = []


# ---- Portfolio segmentation (tags) ----
class PropertyTagIn(BaseModel):
    tag: str = Field(min_length=1, max_length=64)


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
    # "occupied" | "vacant" (a record exists, explicitly flagged vacant or $0 rent) |
    # "missing" (no monthly_record posted for this unit-month at all — distinct from vacant).
    status: str
    noi_change: float | None = None  # vs prior month (None if no prior data)
    # Migration 0012, additive cross-reference: the unit's CURRENT lease state (today, not
    # this row's month) — independent of `status` above (a historical per-month fact) and
    # can legitimately disagree with it. "vacant" when no lease is on file at all.
    lease_status: str | None = None
    lease_tenant_name: str | None = None
    # The tenancy's own rent schedule (migrations 0012/0024), surfaced here so the property page
    # can show and EDIT the three figures an operator changes most — start date, last rent
    # increase, rent — without sending them to the rent roll for them. Reference data, same
    # invariant as everywhere else: it never feeds the P&L metrics on this very row.
    # All null when the unit has no tenancy on file.
    lease_id: str | None = None
    lease_contract_rent: float | None = None
    lease_start: date | None = None
    lease_end: date | None = None  # null = periodic/rolling (no fixed term)
    last_rent_increase_date: date | None = None
    rent_before_increase: float | None = None
    # Counts from the last increase, or from `lease_start` when the rent has never been raised —
    # identical rule to the rent roll's column of the same name.
    months_since_last_increase: int | None = None


class UnitRoster(BaseModel):
    """Server-paginated/sortable unit roster for a property-month."""

    month: date | None
    prior_month: date | None
    total: int  # total units (for pagination)
    rows: list[UnitRosterRow] = []


# ---- Unit detail (sub-step 5): Level 3 ----
class UnitDetailMonth(PnLMetrics):
    """One month of a unit's P&L. ``status`` distinguishes "occupied", an explicit/inferred
    "vacant" (a record was posted, $0 rent), and "missing" (no record posted at all — the
    spine month has no matching row). Unit scope excludes property-tier-only items by design
    (capex/debt at the property tier)."""

    month: date
    status: str  # "occupied" | "vacant" | "missing"


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
    # Migration 0012, additive cross-reference: the unit's CURRENT lease state (today), NOT
    # a replacement for `status`/`months` above (those stay record-driven). "vacant" when
    # no lease is on file at all. See app/queries.py's unit_detail docstring for why these
    # two independent signals aren't merged into one.
    lease_status: str = "vacant"
    lease_tenant_name: str | None = None
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
    # "line" = read straight off the statement; "rent" = a property's rent on a rent roll;
    # "allocated" = this property's computed share of a charge the statement made once over
    # the whole portfolio. The UI marks allocated rows so a computed figure is never mistaken
    # for one the agent actually printed against this property.
    kind: str = "line"
    note: str | None = None  # what the statement said alongside the figure
    # The label exactly as the statement printed it, kept even after a saved format has
    # renamed the row. Teaching a format means recording "this sender's word for that
    # category", so the sender's word has to survive the renaming.
    raw_category: str | None = None


class StatementPreview(BaseModel):
    """Read-only result of parsing an uploaded statement. Nothing is written: the UI shows
    this for review, offers to create any unknown property/categories, then applies the rows
    through the existing ``/import/rows`` seam (idempotent, lock-protected, dry-run-able)."""

    backend: str  # "ollama" | "heuristic" | "rent_roll" | "sectioned"
    # The SHAPE of the file this came from: "single" (one statement, one property),
    # "rent_roll" (one page listing many properties) or "sectioned" (a table per property).
    format: str = "single"
    detected_property: str | None = None
    # The property as the statement printed it, before it was matched to a stored one. The
    # UI teaches a format with this, not with the matched name.
    raw_property: str | None = None
    property_id: str | None = None  # matched existing property, if any
    property_unknown: bool = False
    detected_month: date | None = None
    rows: list[StatementRow] = []
    unknown_categories: list[str] = []
    warnings: list[str] = []
    # The layout's identity, so the UI can offer to teach this sender's format, and which
    # saved format (if any) was applied. ``sample`` is the masked boilerplate the fingerprint
    # was taken from — it travels with the preview because teaching the format needs it, and
    # because a person deciding whether two files are "the same format" deserves to see what
    # that was decided on.
    fingerprint: str = ""
    sample: str = ""
    format_id: str | None = None
    format_label: str | None = None
    format_match: str | None = None  # "exact" | "close"


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
    # Set when this item is ONE property split out of a multi-property rent roll: the name of
    # the PDF it came from, so the UI can group the twelve cards one upload produced.
    source_file: str | None = None


class StatementFileNote(BaseModel):
    """What one uploaded PDF turned out to be, and what the parser wants to say about the
    file as a whole — as opposed to about one property.

    A rent roll needs this because its important facts are file-level: whether the rents read
    add back to the total the statement states, which portfolio charges were split across the
    properties, and which figures on the page named nobody and were therefore left out. Said
    once per file, those are a review; repeated on twelve property cards they are noise.
    """

    filename: str
    format: str  # "single" | "rent_roll" | "sectioned"
    statements: int  # how many property statements this file produced
    warnings: list[str] = []
    # The layout's identity, and the saved format (if any) whose mappings were applied to
    # this file's rows. ``fingerprint`` is always present so the UI can offer to teach a new
    # format; the rest are set only when one was recognised.
    fingerprint: str = ""
    sample: str = ""
    format_id: str | None = None
    format_label: str | None = None
    format_match: str | None = None  # "exact" | "close"


class StatementFormatAlias(BaseModel):
    """One remembered answer: what this sender's word means in this account."""

    raw: str  # as the statement printed it
    target_id: str | None = None  # categories.id / properties.id, resolved in the account
    classification: str | None = Field(default=None, pattern=_CLASS_PATTERN)


class StatementFormatIn(BaseModel):
    """Teach (or re-teach) a format from a preview the operator has just corrected.

    ``fingerprint`` and ``sample`` come straight back from the extract response. Everything
    else is the corrections: which category each raw label means, which property each raw
    address is, and whether this sender's statements should post to the month of the
    statement period or of the rent period.
    """

    fingerprint: str = Field(min_length=8, max_length=64)
    sample: str = ""
    label: str = Field(min_length=1, max_length=120)
    shape: str = "single"
    categories: list[StatementFormatAlias] = []
    properties: list[StatementFormatAlias] = []
    month_rule: str | None = Field(default=None, pattern=r"^(statement_period|rent_period)$")


class StatementFormatOut(BaseModel):
    """A saved format as the UI lists it. The alias maps are returned resolved to NAMES as
    well as ids, because a list of UUIDs tells an operator nothing about what the format
    will do to their next upload."""

    id: str
    label: str
    shape: str
    fingerprint: str
    month_rule: str | None = None
    times_used: int = 0
    last_used_at: datetime | None = None
    category_aliases: dict[str, str] = {}  # raw label -> category name
    property_aliases: dict[str, str] = {}  # raw label -> property name
    classification_overrides: dict[str, str] = {}


class StatementBatchPreview(BaseModel):
    """Result of parsing many statements at once. ``unknown_properties`` /
    ``unknown_categories`` are de-duplicated across every file so the UI resolves each new
    property/category ONCE for the whole batch rather than per statement."""

    items: list[StatementBatchItem] = []
    files: list[StatementFileNote] = []
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
    # Set when these figures came from a bulk purchase: closing costs and loan are then this
    # property's allocated share of a deal-wide total, not figures entered for it alone.
    acquisition_id: str | None = None
    acquisition_name: str | None = None


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
    # Set when the inputs came from a bulk purchase (``portfolio_acquisition``): closing costs
    # and loan are this property's ALLOCATED share of one deal-wide total. Cap rate is
    # unaffected (it uses only the agreed price); cash-on-cash and DSCR are share-based.
    acquisition_id: str | None = None
    acquisition_name: str | None = None
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


class MissingInvestmentProperty(BaseModel):
    """A property with no acquisition data on file yet — the adoption-gap nudge's payload."""

    property_id: str
    property_name: str
    type: str


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
    # Adoption-gap nudge: how many of the portfolio's properties have NO acquisition data at
    # all yet (vs how many exist total), and which ones — so the UI can prompt completion.
    total_property_count: int = 0
    missing_property_count: int = 0
    missing_properties: list[MissingInvestmentProperty] = []


# ---- Portfolio (bulk) acquisitions: one deal, costs allocated across several properties ----
#
# A bulk purchase has per-property prices but ONE closing-cost figure and ONE blanket loan.
# These schemas describe the deal; the resolved split is written onto each member's
# ``property_investment`` row, so all existing return metrics are unchanged by its existence.
_ALLOCATION_METHOD_PATTERN = "^(price|equal|custom)$"


class AcquisitionMemberIn(BaseModel):
    """One property in a bulk deal: its own agreed price, plus — for ``custom`` allocation
    only — the operator's explicit share of the shared costs. Under ``price``/``equal`` the
    shares are computed and any supplied values are ignored."""

    property_id: str
    purchase_price: float = Field(ge=0)
    closing_costs: float | None = Field(default=None, ge=0)
    loan_amount: float | None = Field(default=None, ge=0)


class PortfolioAcquisitionIn(BaseModel):
    """A bulk purchase to record. ``members`` needs at least two properties — a single-property
    purchase is an ordinary acquisition and belongs on the property's own investment form.

    Under ``custom`` allocation the stated totals are IGNORED and recomputed as the sum of the
    per-member shares, so the stored totals can never disagree with the split they describe."""

    name: str = Field(min_length=1, max_length=160)
    purchase_date: date
    total_closing_costs: float = Field(default=0, ge=0)
    total_loan_amount: float = Field(default=0, ge=0)
    allocation_method: str = Field(default="price", pattern=_ALLOCATION_METHOD_PATTERN)
    notes: str | None = None
    members: list[AcquisitionMemberIn] = Field(min_length=2)


class AcquisitionMemberOut(BaseModel):
    """A member with its resolved share. ``price_share`` is the property's fraction of the
    combined purchase price — the pro-rata weight, surfaced so the split is auditable."""

    property_id: str
    property_name: str
    purchase_price: float
    closing_costs: float
    loan_amount: float
    equity_invested: float
    price_share: float


class PortfolioAcquisitionOut(BaseModel):
    """A recorded bulk purchase with its members' resolved allocations.

    The ``*_drift`` fields compare what is actually stored on the member ``property_investment``
    rows against this deal's stated totals. They are 0 immediately after saving; a non-zero
    value means someone has since hand-edited a member on its own property page, so the parts
    no longer sum to the deal — worth surfacing rather than silently reconciling, because only
    the operator knows which figure is the right one."""

    id: str
    name: str
    purchase_date: date
    total_closing_costs: float
    total_loan_amount: float
    allocation_method: str
    notes: str | None = None
    created_at: datetime
    updated_at: datetime
    members: list[AcquisitionMemberOut] = []
    property_count: int = 0
    total_purchase_price: float = 0
    total_equity_invested: float = 0
    allocated_closing_costs: float = 0
    allocated_loan_amount: float = 0
    closing_costs_drift: float = 0
    loan_amount_drift: float = 0


class AcquisitionPreview(BaseModel):
    """The split a set of inputs WOULD produce, computed without saving anything — so the
    entry form can show the allocation table live from the same code that will persist it,
    rather than a client-side reimplementation that could round differently."""

    members: list[AcquisitionMemberOut] = []
    total_purchase_price: float = 0
    total_closing_costs: float = 0
    total_loan_amount: float = 0
    total_equity_invested: float = 0


# ---- Shared expenses (one bill covering several properties, split across them monthly) ----
_SHARED_METHOD_PATTERN = "^(equal|price|units|custom)$"
_ON_CONFLICT_PATTERN = "^(fail|replace|skip)$"


class SharedExpenseMemberIn(BaseModel):
    """One property covered by the arrangement. ``custom_share`` is read ONLY under
    ``custom`` allocation, where the operator states each property's share outright; under
    ``equal``/``price``/``units`` the shares are computed and any supplied value is ignored."""

    property_id: str
    custom_share: float | None = Field(default=None, ge=0)


class SharedExpenseIn(BaseModel):
    """A shared-cost arrangement to record. ``members`` needs at least two properties — a cost
    borne by one property is an ordinary line item and belongs on that property's entry form.

    ``amount`` is the bill for ONE month; posting to a range of months applies it to each.
    Under ``custom`` allocation the stated ``amount`` is IGNORED and recomputed as the sum of
    the members' shares, so the stored total can never disagree with the split it describes."""

    name: str = Field(min_length=1, max_length=160)
    category_id: str
    classification: str | None = Field(default=None, pattern=_CLASS_PATTERN)
    amount: float = Field(default=0, ge=0)
    allocation_method: str = Field(default="equal", pattern=_SHARED_METHOD_PATTERN)
    notes: str | None = None
    members: list[SharedExpenseMemberIn] = Field(min_length=2)


class SharedExpenseMemberOut(BaseModel):
    """A member with its resolved monthly share. ``basis`` is the raw weighting figure the
    method used (purchase price, unit count, or 1 for an equal split) and ``weight_share`` is
    that figure as a fraction of the total — both surfaced so the split is auditable rather
    than a number the operator has to take on trust."""

    property_id: str
    property_name: str
    amount: float
    basis: float
    weight_share: float


class SharedExpenseOut(BaseModel):
    """A recorded arrangement with its members' current monthly shares.

    ``allocated_amount`` is the sum of the shares, which equals ``amount`` by construction —
    it is returned so a client can assert that rather than assume it. ``posted_months`` lists
    the months this arrangement has actually written line items into, so the UI can show what
    is already applied without a second round trip."""

    id: str
    name: str
    category_id: str
    category_name: str | None = None
    classification: str | None = None
    amount: float
    allocation_method: str
    notes: str | None = None
    created_at: datetime
    updated_at: datetime
    members: list[SharedExpenseMemberOut] = []
    property_count: int = 0
    allocated_amount: float = 0
    posted_months: list[date] = []


class SharedExpenseSplit(BaseModel):
    """The per-property split a set of inputs WOULD produce, computed without saving.

    Exists so the entry form's live allocation table comes from the same code that will
    persist it — a client-side reimplementation could distribute the leftover cents
    differently and show a split the saved arrangement then contradicts."""

    members: list[SharedExpenseMemberOut] = []
    total_amount: float = 0
    allocation_method: str = "equal"


class SharedExpensePostIn(BaseModel):
    """Which months to apply an arrangement to. ``to_month`` defaults to ``from_month`` (a
    single month); both are normalised to the first of the month.

    ``on_conflict`` decides what happens when a member's property-month ALREADY has a line
    item in this category that this arrangement did not post — a hand-entered figure, an
    imported one, or another arrangement's:
      * ``fail``    — refuse the whole post and report every conflict (the default: never
        destroy a figure someone entered by hand as a side effect of a bulk action).
      * ``replace`` — overwrite it and take ownership of the line.
      * ``skip``    — leave that property-month alone and post the rest.
    Lines this arrangement posted before are always updated in place; they are not conflicts."""

    from_month: date
    to_month: date | None = None
    on_conflict: str = Field(default="fail", pattern=_ON_CONFLICT_PATTERN)


class SharedExpensePostRow(BaseModel):
    """One property-month the post would touch, and what stands there now.

    ``existing_source`` is why a figure is already present: ``none`` (nothing there),
    ``this`` (a previous post of this same arrangement — will be updated in place),
    ``manual`` (hand-entered or imported), or ``other_shared`` (a different arrangement).
    Only the last two are conflicts. ``locked`` marks a property-month whose period is
    locked; those block the post until an admin unlocks them, since bypassing the lock in a
    bulk action would defeat the point of locking."""

    month: date
    property_id: str
    property_name: str
    amount: float
    existing_amount: float | None = None
    existing_source: str = "none"
    locked: bool = False


class SharedExpensePostPlan(BaseModel):
    """A dry run of a post: exactly what would be written, and what stands in the way.

    ``blocked`` is the honest bottom line — true when the post as requested cannot go through
    (locked months, or conflicts under ``on_conflict=fail``). The UI shows this before the
    operator commits, so a bulk write across many properties is never a surprise."""

    months: list[date] = []
    rows: list[SharedExpensePostRow] = []
    amount_per_month: float = 0
    total_amount: float = 0
    conflict_count: int = 0
    locked_count: int = 0
    blocked: bool = False


class SharedExpensePostResult(BaseModel):
    """What a post actually wrote. ``skipped`` counts property-months left alone under
    ``on_conflict=skip`` — non-zero means the posted total is deliberately less than
    ``amount x months``, which ``total_posted`` reports truthfully rather than as intended."""

    months: list[date] = []
    rows: list[SharedExpensePostRow] = []
    line_items_written: int = 0
    records_created: int = 0
    skipped: int = 0
    total_posted: float = 0


class SharedExpenseUnpostResult(BaseModel):
    """What an un-post removed. Only line items carrying this arrangement's provenance are
    deleted, so a figure someone later edited by hand into the same slot is never collateral."""

    months: list[date] = []
    line_items_removed: int = 0
    total_removed: float = 0


# ---- Portfolio benchmarking (cross-property comparison against the portfolio average) ----
class BenchmarkValue(BaseModel):
    """One metric for one property: its raw value, delta vs the portfolio average (mean),
    and its rank/percentile among properties where the metric is computable. ``value`` is
    None when the metric can't be honestly computed for this property (e.g. cap rate with
    no acquisition data) — such properties are excluded from the mean/median/rank entirely,
    never treated as zero."""

    value: float | None = None
    delta_vs_mean: float | None = None  # value - portfolio mean; None if either side is None
    rank: int | None = None  # 1 = best-performing property for this metric (ties share a rank)
    percentile: float | None = None  # 0..100, 100 = best; None if value is None


class PropertyBenchmarkRow(BaseModel):
    property_id: str
    property_name: str
    type: str
    noi_per_unit: BenchmarkValue
    opex_ratio: BenchmarkValue
    physical_occupancy: BenchmarkValue
    economic_occupancy: BenchmarkValue
    cap_rate: BenchmarkValue
    cash_on_cash: BenchmarkValue


class BenchmarkMetricStats(BaseModel):
    """Portfolio-wide stats for one metric. ``mean``/``median`` are a SIMPLE average across
    properties with a computable value — deliberately NOT value-weighted (unlike
    ``PortfolioInvestment``'s aggregates), so a benchmarking view isn't dominated by the
    largest asset; each property counts once. ``count`` is the denominator (properties with
    a non-null value for this metric) — may be less than ``property_count``."""

    mean: float | None = None
    median: float | None = None
    count: int = 0
    higher_is_better: bool  # for UI tone: whether an above-average value is "good"


class PortfolioBenchmarks(BaseModel):
    """Every property benchmarked against the portfolio simple-mean average on NOI/unit,
    operating expense ratio, physical + economic occupancy, cap rate, and cash-on-cash.

    NOI/opex-ratio/physical-occupancy are period-scoped (``period_from``/``period_to``,
    resolved the same way as ``PortfolioDashboard``: defaults to the latest summarized
    month). Economic occupancy reuses the rent roll's CURRENT lease-status snapshot (same
    computation as ``RentRoll.occupancy.economic_occupancy``) — it is NOT scoped to the
    period; occupancy derived from lease status is inherently a point-in-time read, unlike
    the flow metrics. Cap rate / cash-on-cash are the same trailing-12, acquisition-based
    figures as ``InvestmentMetrics`` — also not period-scoped. ``tags`` optionally scopes
    both the rows and the average to properties carrying ANY of the given tags (OR
    semantics)."""

    period_from: date | None = None
    period_to: date | None = None
    tags: list[str] | None = None
    property_count: int = 0
    noi_per_unit: BenchmarkMetricStats
    opex_ratio: BenchmarkMetricStats
    physical_occupancy: BenchmarkMetricStats
    economic_occupancy: BenchmarkMetricStats
    cap_rate: BenchmarkMetricStats
    cash_on_cash: BenchmarkMetricStats
    properties: list[PropertyBenchmarkRow] = []


# ---- Budget / variance (flat annual plan per property, actual-vs-plan) ----
class PropertyBudgetIn(BaseModel):
    """Annual plan inputs for one property/year. Monthly plan = annual ÷ 12."""

    budgeted_gross_rent: float = Field(ge=0)
    budgeted_operating_expenses: float = Field(ge=0)


class PropertyBudgetOut(PropertyBudgetIn):
    property_id: str
    year: int
    budgeted_noi: float  # budgeted_gross_rent - budgeted_operating_expenses (computed)
    updated_at: datetime


class VarianceMetrics(BaseModel):
    """Actual vs. plan for a scope/period. Plan is the flat annual budget(s) pro-rated across
    the requested month range (annual ÷ 12 per covered month, summed across any years the
    range spans). ``plan_*``/``variance_*`` are None only when the scope has zero budget
    coverage for the period; a period that's *partially* covered still returns numbers, with
    ``plan_coverage_months`` < ``total_months`` telling the caller the plan is incomplete."""

    period_from: date | None
    period_to: date | None
    total_months: int = 0
    plan_coverage_months: int = 0  # months in the period actually covered by a budget row

    actual_gross_rent: float = 0
    actual_operating_expenses: float = 0
    actual_noi: float = 0

    plan_gross_rent: float | None = None
    plan_operating_expenses: float | None = None
    plan_noi: float | None = None

    variance_gross_rent: float | None = None  # actual - plan ($)
    variance_operating_expenses: float | None = None
    variance_noi: float | None = None
    variance_noi_pct: float | None = None  # variance_noi / |plan_noi|, None if plan_noi == 0


class PropertyVariance(VarianceMetrics):
    property_id: str
    property_name: str


class PortfolioVariance(VarianceMetrics):
    """Portfolio-level variance: BOTH actual and plan are scoped to only the properties that
    have budget coverage for the period (actual is NOT the whole-portfolio total) — comparing
    an all-property actual against a partially-budgeted plan produced a meaningless variance %.
    ``budgeted_property_count`` (out of ``total_property_count``) discloses how much of the
    portfolio this comparison actually covers."""

    budgeted_property_count: int = 0
    total_property_count: int = 0


# ---- Rent roll / lease-level data (migration 0012) ---------------------------------
_LEASE_STATUS_PATTERN = "^(active|notice|expired|vacant)$"


class LeaseIn(BaseModel):
    """Admin-editable lease fields. ``end_date`` null = month-to-month (no fixed term).
    ``contract_rent`` is reference data — it never feeds NOI/cash-flow math.

    v2 fields (migration 0013), all reference/terms data with the same invariant:
    ``security_deposit``, ``escalation_pct``/``escalation_frequency_months`` (a contract-
    rent bump), and ``pct_rent_rate``/``pct_rent_breakpoint`` (percentage rent — retail
    leases only, null otherwise). ``lease_type`` is deliberately NOT here — it's a DERIVED
    fact of ``end_date`` (see ``LeaseOut``/the leases router), not an independently
    settable one, so an edit can never leave it inconsistent with end_date.

    ``concession_monthly`` (migration 0016): a standing $/month rent concession/discount,
    distinct from bad debt — see the migration's docstring. Reference data, same
    invariant. IMPORTANT for `update_lease` (a full-replace PATCH): this field MUST be
    included in every save from the rent-roll editor, or an edit to any other field would
    silently null out an existing concession.

    Periodic-tenancy fields (migration 0024) — what makes an England-style ROLLING tenancy
    pricable, where the rent rises in discrete steps on a stated date rather than on a
    contractual percentage:
        ``last_rent_increase_date``  when the current ``contract_rent`` took effect. Null =
                                     never increased (the rent has been ``contract_rent``
                                     since ``start_date``), which is every pre-0024 lease.
        ``rent_before_increase``     the rent that date replaced. Null = an increase happened
                                     but the prior figure isn't held. Rejected (422/400)
                                     without a date to attach it to, since there would be no
                                     month at which it stopped applying.

    ``opening_arrears`` (migration 0025): the arrears balance brought forward at
    ``start_date`` — debt predating this app's records. SIGNED; negative means the tenancy
    began in credit. Arrears itself is DERIVED (rent due vs. rent collected, accumulated),
    so this and ``arrears_adjustment`` rows are its only stored inputs.

    All four carry the same full-replace-PATCH hazard called out for ``concession_monthly``
    above: they must be included in every save, or an unrelated edit silently erases them."""

    # Optional (migration 0026): a tenancy recorded from statements often has no name on file,
    # and the rent schedule/arrears math never reads it. Blank is normalised to None by the
    # router, so "not recorded" has exactly one representation.
    tenant_name: str | None = None
    start_date: date
    end_date: date | None = None
    contract_rent: float = Field(ge=0)
    status: str = Field(default="active", pattern=_LEASE_STATUS_PATTERN)
    concession_monthly: float | None = Field(default=None, ge=0)
    last_rent_increase_date: date | None = None
    rent_before_increase: float | None = Field(default=None, ge=0)
    opening_arrears: float | None = None
    # Migration 0027: the month from which this tenancy's arrears are meaningful (YYYY-MM-01).
    # None = from the beginning. Set it to exclude a HANDOVER month, where rent apportioned at
    # completion is indistinguishable from a tenant who underpaid — see the migration.
    arrears_from_month: date | None = None
    security_deposit: float | None = Field(default=None, ge=0)
    escalation_pct: float | None = Field(default=None, ge=0, le=100)
    escalation_frequency_months: int = Field(default=12, gt=0)
    pct_rent_rate: float | None = Field(default=None, ge=0, le=100)
    pct_rent_breakpoint: float | None = Field(default=None, ge=0)


class LeaseOut(LeaseIn):
    id: str
    unit_id: str
    lease_type: str  # 'fixed' | 'mtm' — server-derived from end_date, read-only
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class RentRollRow(BaseModel):
    """One rent-roll line: a unit's identity + its current lease (if any) + the latest
    actual recorded rent. ``status`` is the lease's own status (authoritative for
    occupancy here) — 'vacant' when the unit has no lease on file at all. ``missing_data``
    flags an occupied unit with no monthly_record for the property's latest summarized
    month, kept distinct from a real vacancy per the analyst report's ask.

    For a vacant unit (fix #3): ``tenant_name`` and ``months_to_expiry`` are null — the
    prior tenant/term is not a current tenancy, so surfacing them as if it were is
    misleading. ``contract_rent`` IS still populated for a vacant unit (its own
    last-known/asking rent) — needed as the potential-rent figure for the GPR-based
    economic occupancy calc (fix #1); the UI labels it "asking rent" for a vacant row.

    v2 (migration 0013) additions, all reference/terms data:
    - ``security_deposit``, ``escalation_pct``/``escalation_frequency_months``/
      ``next_escalation_date`` (the next scheduled bump, derived from start_date +
      frequency, first occurrence on/after today — null if there's no escalation on file).
    - ``lease_type``: 'fixed' | 'mtm'.
    - ``holdover``: True when this lease's term has lapsed (``end_date`` in the past) but
      the tenant is still recorded as paying rent (a recent actual gross-rent record on
      file) and no newer lease has taken over — distinct from a clean active lease AND
      from a true vacancy. Still counted "occupied" (status stays whatever the lease's own
      status is, typically 'expired') — this is a presentation flag layered on top, not a
      change to the occupancy math.
    - ``vacant_days``: for a vacant row only, days since the prior lease's end_date (its
      "downtime") — null when the unit has never had a lease on file (no end_date to
      count from).
    - ``pct_rent_rate``/``pct_rent_breakpoint``: percentage-rent terms (retail leases
      only; null for every residential lease).
    - ``market_rent``: the unit's own ``units.market_rent`` (migration 0015, admin-
      editable via `UnitUpdate`/`PATCH /units/{id}`) — the rent waterfall's GPR reference
      figure, surfaced here too so it's visible/editable right next to the same unit's
      lease. Independent of `contract_rent`/`status`.
    - ``concession_monthly``: the current lease's standing $/month concession (migration
      0016 — see `LeaseIn`), null when there's no lease on file or no concession."""

    unit_id: str
    unit_number: str
    label: str | None = None
    property_id: str
    property_name: str
    lease_id: str | None = None
    tenant_name: str | None = None
    lease_start: date | None = None
    lease_end: date | None = None
    contract_rent: float | None = None
    status: str  # "active" | "notice" | "expired" | "vacant"
    actual_rent: float | None = None
    actual_month: date | None = None
    missing_data: bool = False
    months_to_expiry: int | None = None  # None = month-to-month, or unit is vacant
    security_deposit: float | None = None
    escalation_pct: float | None = None
    escalation_frequency_months: int | None = None
    next_escalation_date: date | None = None
    lease_type: str | None = None  # "fixed" | "mtm" — null when there's no lease on file
    holdover: bool = False
    vacant_days: int | None = None
    pct_rent_rate: float | None = None
    pct_rent_breakpoint: float | None = None
    market_rent: float | None = None
    # Standing $/month rent concession (migration 0016) — see `LeaseIn.concession_monthly`.
    # Null when the unit has no lease on file at all, OR when its lease has no concession.
    concession_monthly: float | None = None
    # Expected-vs-actual rent variance (analyst report: "connect lease economics to the
    # P&L"). REFERENCE data only — never feeds NOI/cash-flow. Computed over the rent
    # roll's `period_from`/`period_to` window (see `RentRoll`): `expected_rent` sums this
    # unit's escalated contract rent across every covered month (see
    # `app.queries._unit_expected_actual` for the exact governing-lease/holdover rule; a
    # vacant month with no holdover contributes $0); `period_actual_rent` sums the SAME
    # actual-gross-rent source the rent roll already uses (`unit_month_summary.gross_rent`),
    # over the same months. `variance` = actual − expected (positive = collecting more than
    # the lease's own escalated schedule implies; negative = a shortfall / loss-to-lease).
    # `variance_pct` = variance ÷ expected (null when expected is $0, e.g. a vacant unit
    # with no holdover in the whole window). All null only when there is no summarized
    # unit-month data anywhere in scope to default a period from (`period_to` is null).
    expected_rent: float | None = None
    period_actual_rent: float | None = None
    variance: float | None = None
    variance_pct: float | None = None
    # ---- Periodic-tenancy rent history (migration 0024) ----
    # `last_rent_increase_date` is when the current `contract_rent` took effect and
    # `rent_before_increase` what it replaced (null = not held). `months_since_last_increase`
    # counts from that date, or from `lease_start` when the rent has never been increased —
    # the operational read on a rolling tenancy, where a rise is typically annual and no
    # sooner, so a large number is a review that's overdue. Null when there's no lease on file.
    last_rent_increase_date: date | None = None
    rent_before_increase: float | None = None
    months_since_last_increase: int | None = None
    # The current lease's stored brought-forward arrears balance (migration 0025), echoed here
    # so the rent roll's full-replace lease editor can round-trip it instead of nulling it out
    # on an unrelated edit — the same hazard called out for `concession_monthly` in `LeaseIn`.
    # Already included in `arrears_balance` below; this is the input, not a second total.
    opening_arrears: float | None = None
    # ---- Arrears (migration 0025) ----
    # REFERENCE data, like every other lease-derived figure here: never feeds NOI/cash-flow.
    # DERIVED, never stored: `rent_due - rent_collected + adjustments` per month, accumulated
    # over the TENANCY (the running balance resets at each lease — an outgoing tenant's debt
    # is theirs). Only months with an actual record on file accrue; a month with no record is
    # MISSING DATA, not debt. See `app.queries._unit_arrears_ledgers`.
    #
    # `arrears_balance` is the CUMULATIVE figure: everything this tenancy owes as at
    # `period_to`, including `lease.opening_arrears`. `arrears_movement` is the MONTHLY
    # figure: the net change across the rent roll's window only, so
    # `arrears_opening_balance + arrears_movement == arrears_balance` exactly.
    # `arrears_rent_due`/`arrears_rent_collected`/`arrears_adjustments` are the window's three
    # components behind that movement.
    #
    # `arrears_rent_due` is NET of any standing concession (`lease.concession_monthly`), and
    # `arrears_concessions` reports how much was netted off. Rent the landlord agreed not to
    # charge is not rent the tenant failed to pay — the same line the rent waterfall already
    # draws between `concessions` and `bad_debt`. Explicitly VACANT months (`is_vacant`) are
    # excluded entirely for the same reason: no tenant, no debt. £0 collected on an occupied
    # month is NOT treated as vacancy — that's the most important arrears case there is.
    #
    # Negative is real and meaningful throughout: a tenant paid ahead and is in CREDIT. The
    # balance is deliberately NOT floored at zero.
    #
    # `arrears_months_of_rent` restates the balance in months of the current rent — the figure
    # a letting agent quotes, and what makes £900 owed comparable between a £450 and an £1,800
    # door. Null when there's no priced month to divide by.
    #
    # Zeros (not nulls) for a unit with no lease: no tenancy means nothing owed, a fact rather
    # than a gap. All null for a SHELL unit, which has no tenancy of its own at all.
    # The tenancy's `arrears_from_month` (migration 0027), echoed so the editor can round-trip
    # it and the UI can say which month the balance is measured from. None = from the beginning.
    arrears_from_month: date | None = None
    arrears_balance: float | None = None
    arrears_opening_balance: float | None = None
    arrears_movement: float | None = None
    arrears_rent_due: float | None = None
    arrears_rent_collected: float | None = None
    arrears_concessions: float | None = None
    arrears_adjustments: float | None = None
    arrears_months_of_rent: float | None = None


class OccupancySummary(BaseModel):
    total_units: int
    occupied_units: int
    physical_occupancy: float | None = None  # occupied / total, fraction 0..1
    # Gross Potential Rent: occupied units' contract rent + vacant units' potential rent
    # (asking/last-known rent, or a property-average proxy). The denominator behind
    # `economic_occupancy` below — exposed for transparency/auditing.
    gross_potential_rent: float | None = None
    # TRUE economic occupancy (fix #1): Σ actual collected rent ÷ Gross Potential Rent,
    # where GPR includes vacant units' potential rent. Captures vacancy loss, so this
    # comes in AT OR BELOW physical_occupancy whenever there's vacancy (can only exceed
    # ~100% by a small actual-over-contract overage). This is NOT the same metric as the
    # old (mislabeled) calculation — see `rent_realization`.
    economic_occupancy: float | None = None
    # Actual collected rent / contract rent, over OCCUPIED units with both figures on
    # file — i.e. the metric this endpoint used to (incorrectly) call "economic
    # occupancy". Kept under its own name because it excludes vacant units by
    # construction and can therefore read ABOVE physical occupancy / 100%; it measures
    # collections-vs-contract for in-place tenants, not vacancy loss.
    rent_realization: float | None = None
    # Average downtime (days) across vacant units with a known prior lease end_date — the
    # portfolio/property "average vacancy days" stat. Null when there's no vacant unit with
    # a known end_date to average (e.g. no vacancies, or vacancies with no lease history).
    avg_vacant_days: float | None = None


class RentVarianceRollup(BaseModel):
    """Property/portfolio-level expected-vs-actual rent variance for the rent roll's
    `period_from`/`period_to` window. Excludes shell/synthetic units (same exclusion, same
    reason, as `OccupancySummary`'s aggregates). REFERENCE data — see `RentRollRow`'s
    per-unit fields for the exact expected-rent formula; this is just their sum."""

    total_expected_rent: float
    total_actual_rent: float
    variance: float  # total_actual_rent - total_expected_rent
    variance_pct: float | None = None  # null when total_expected_rent is $0
    unit_count: int  # non-shell units included in this rollup


class ArrearsRollup(BaseModel):
    """Property/portfolio arrears rollup for the rent roll's window (migration 0025).
    Excludes shell/synthetic units, same as `RentVarianceRollup`.

    ``total_balance`` is the NET position (units in credit net against units owing) — the
    portfolio's real exposure. ``units_in_arrears`` is a HEADCOUNT of units actually owing at
    `period_to`, which is the different question "how many doors do I have to chase"; a single
    figure can't answer both, so both are reported. ``largest_balance`` is the worst single
    unit, null when there are no units in scope."""

    total_balance: float
    total_movement: float
    total_rent_due: float
    total_rent_collected: float
    total_concessions: float
    total_adjustments: float
    units_in_arrears: int
    units_in_credit: int
    unit_count: int
    largest_balance: float | None = None


class RentRoll(BaseModel):
    property_id: str | None = None  # None for the portfolio-wide rent roll
    as_of: date
    rows: list[RentRollRow] = []
    occupancy: OccupancySummary
    # Expected-vs-actual rent variance window + rollup. `period_from`/`period_to` default
    # to the latest month with any actual data on file for this scope (a single-month
    # window) when the caller doesn't pass `from`/`to` — both null only when the scope has
    # no summarized unit-month data at all yet (a brand-new/empty portfolio).
    period_from: date | None = None
    period_to: date | None = None
    rent_variance: RentVarianceRollup | None = None
    # Arrears rollup over the SAME window (migration 0025). Always present; all-zero when the
    # scope has no summarized months yet.
    arrears: ArrearsRollup | None = None


class ArrearsMonth(BaseModel):
    """One month of one tenancy's arrears ledger (migration 0025) — the MONTHLY basis, where
    the accumulated balance comes from.

    ``movement = rent_due - rent_collected + adjustments``, and ``balance`` is the running
    total after it (starting from the lease's ``opening_arrears``). A negative ``movement`` is
    a tenant catching up; a negative ``balance`` means they are in credit.

    ``rent_due`` is NET of ``concession`` (a standing discount the landlord granted — not rent
    anyone failed to pay); the gross figure is ``rent_due + concession``. Explicitly vacant
    months never appear at all: no tenant, no debt.

    ``has_record`` is False for a month that appears only because an adjustment landed on it —
    a write-off is a fact of the tenancy, recorded whether or not a rent record exists for
    that month. Months with rent due but NO record don't appear at all: that's missing data,
    not debt. ``holdover`` flags a month priced from a lapsed lease frozen at its own end date
    (the tenant stayed on past the term)."""

    month: date
    rent_due: float  # NET of `concession` below — what was actually owed
    rent_collected: float
    concession: float
    adjustments: float
    movement: float
    balance: float
    has_record: bool = False
    holdover: bool = False


class ArrearsLeaseLedger(BaseModel):
    """One tenancy's arrears ledger, month by month, with its own running balance.

    Per-TENANCY by design: the balance resets at each lease, because an outgoing tenant's
    unpaid rent is their debt and not the next tenant's. ``opening_arrears`` is what the
    tenancy was already carrying at ``lease_start`` (debt predating this app's records);
    ``closing_balance`` is ``opening_arrears`` plus every month's movement."""

    lease_id: str
    tenant_name: str | None = None  # None = not recorded (migration 0026)
    lease_start: date
    lease_end: date | None = None
    status: str
    opening_arrears: float
    arrears_from_month: date | None = None  # months before this are outside the measurement
    months: list[ArrearsMonth] = []
    total_rent_due: float
    total_rent_collected: float
    total_concessions: float
    total_adjustments: float
    closing_balance: float


class UnitArrears(BaseModel):
    """Every tenancy a unit has ever had, each with its own arrears ledger, oldest first.

    Prior tenancies are included because a former tenant's unpaid rent doesn't vanish when
    they leave — it stops being collectable from the CURRENT tenant, which is a different
    statement, and one an arrears report has to be able to make. ``current_balance`` is the
    balance of the lease the rent roll calls current, so the ledger and the rent-roll row
    always agree."""

    unit_id: str
    as_of: date
    current_lease_id: str | None = None
    current_balance: float
    leases: list[ArrearsLeaseLedger] = []


class ArrearsAdjustmentIn(BaseModel):
    """A signed, dated movement on a tenancy's arrears balance that is NOT a rent shortfall
    (migration 0025) — the escape hatch a derived balance needs.

    ``kind`` fixes the sign, because the sign IS the meaning: 'write_off' must be negative
    (arrears forgiven, or recovered from the deposit at check-out), 'charge' must be positive
    (a sum owed that the rent schedule doesn't describe), and only 'correction' may go either
    way. Zero is rejected — it's a row that claims something happened while saying nothing.
    ``month`` is month-grain (the 1st), same convention as monthly_records.

    This never touches NOI/cash-flow. A write-off that should ALSO hit the P&L as bad-debt
    expense is an ordinary line item, entered as one."""

    month: date
    amount: float
    kind: str = Field(pattern=r"^(write_off|charge|correction)$")
    note: str | None = None


class ArrearsAdjustmentOut(ArrearsAdjustmentIn):
    id: str
    lease_id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PercentageRentCalc(BaseModel):
    """Percentage-rent overage calc for one lease (migration 0013). Terms
    (``pct_rent_rate``/``pct_rent_breakpoint``) are stored, reference-only lease data;
    sales figures are NOT tracked anywhere in this app, so ``annual_sales`` is a
    caller-supplied, non-persisted input and ``overage_rent`` is computed on the fly —
    never fed into NOI/cash-flow. ``has_percentage_rent_terms`` is False (and
    ``overage_rent`` null) for the overwhelming majority of leases (residential), which
    carry no percentage-rent terms at all."""

    lease_id: str
    has_percentage_rent_terms: bool
    pct_rent_rate: float | None = None
    pct_rent_breakpoint: float | None = None
    annual_sales: float | None = None
    overage_rent: float | None = None


class LeaseExpirationItem(BaseModel):
    unit_id: str
    unit_number: str
    label: str | None = None
    property_id: str
    property_name: str
    tenant_name: str | None = None  # None = not recorded (migration 0026)
    lease_end: date
    contract_rent: float
    months_to_expiry: int


class LeaseExpirations(BaseModel):
    within_months: int
    as_of: date
    items: list[LeaseExpirationItem] = []
    # Rollup (fix #2): total contract rent of the returned (expiring) leases, and that as
    # a fraction of total in-place portfolio contract rent — both honoring the same
    # `tags` filter, so they stay internally consistent with each other.
    total_contract_rent_expiring: float = 0.0
    pct_of_portfolio_rent: float | None = None


class RentWaterfallTotals(BaseModel):
    """The standard CRE rent bridge, GPR stepping down to actual collected rent (migration
    0015: ``units.market_rent``). REFERENCE data — computed on read from
    ``units.market_rent`` + ``lease``/``unit_month_summary`` (plus, for single-asset
    properties, ``property_month_summary`` — rework item 3), exactly like the rent-roll's
    expected-vs-actual variance figures; never feeds NOI/cash-flow.

    Basis (rework item 2): computed over EVERY real (non-shell) unit-month in scope, driven
    by lease status — never gated on whether an actual record happens to be on file — plus
    each single-asset property's property-tier rent as a pass-through leg. This is what
    makes ``actual_collected`` reconcile to the portfolio/property P&L's gross rent for the
    same scope/period (see the rent-waterfall-rework changelog for live proof).

    - ``gpr``: Gross Potential Rent — every real (non-shell) unit-month in scope, priced at
      its own ``market_rent`` (or, for a single-asset property, its actual property-tier
      rent — there's no per-unit market rent to price a unitless property at).
    - ``loss_to_lease``: over OCCUPIED unit-months, ``market_rent - in_place_scheduled``
      (the escalated contract rent). Negative = gain-to-lease (in-place rent above market).
    - ``vacancy_loss``: over VACANT unit-months (no lease in force, no holdover), the full
      ``market_rent`` — nothing was collected because nothing was in force.
    - ``collections_loss``: over OCCUPIED unit-months, ``in_place_scheduled - actual`` —
      contracted-but-uncollected rent. Kept as a line for backward compatibility; it now
      always equals ``concessions + bad_debt`` exactly (waterfall-followups item 3 — see
      those two fields below for the split).
    - ``concessions`` / ``bad_debt`` (waterfall-followups item 3): the split of
      ``collections_loss`` into a leasing-decision piece and a delinquency piece.
      ``concessions`` sums, over occupied unit-months, each lease's modeled
      ``concession_monthly`` (migration 0016) capped at that month's own shortfall
      (``max(0, in_place_scheduled - actual)``) — never attributing more concession than
      the shortfall that actually occurred that month, so a lease with a concession but no
      real collections gap that month contributes $0. ``bad_debt`` is the remainder:
      ``collections_loss - concessions`` (can be negative in the same months
      ``collections_loss`` itself is negative — a collections GAIN, e.g. fees/a rent bump
      not yet re-papered — exactly mirroring ``loss_to_lease``'s own sign convention).
      Computed on read; no change to ``actual_collected`` or NOI.
    - ``actual_collected``: the same ``unit_month_summary.gross_rent`` (unit-level) /
      ``property_month_summary.gross_rent`` (single-asset) source the rest of the app uses,
      summed over the same unit-months / property-months.
    - ``residual``: ``gpr - loss_to_lease - vacancy_loss - collections_loss -
      actual_collected`` — equivalently ``gpr - loss_to_lease - vacancy_loss -
      concessions - bad_debt - actual_collected`` (item 3's restated identity), since
      ``collections_loss = concessions + bad_debt`` exactly. Should be ~0 (to the cent) by
      construction; returned explicitly so it can be verified rather than trusted blindly.
    - ``loss_to_lease_pct_of_gpr`` / ``vacancy_loss_pct_of_gpr`` /
      ``collections_loss_pct_of_gpr`` / ``concessions_pct_of_gpr`` /
      ``bad_debt_pct_of_gpr`` / ``actual_collected_pct_of_gpr`` (rework item 4, extended by
      item 3): each line as a fraction of ``gpr`` (e.g. ``0.055`` = 5.5%; NOT
      pre-multiplied by 100, same convention as
      ``LeaseExpirationSummary.pct_of_portfolio_rent`` — the frontend formats it, same as
      that field). ``null`` when ``gpr`` is 0 (nothing to divide by, not "0%").
    """

    gpr: float
    loss_to_lease: float
    vacancy_loss: float
    collections_loss: float
    concessions: float = 0.0
    bad_debt: float = 0.0
    actual_collected: float
    residual: float
    loss_to_lease_pct_of_gpr: float | None = None
    vacancy_loss_pct_of_gpr: float | None = None
    collections_loss_pct_of_gpr: float | None = None
    concessions_pct_of_gpr: float | None = None
    bad_debt_pct_of_gpr: float | None = None
    actual_collected_pct_of_gpr: float | None = None


class UnitRentWaterfallRow(RentWaterfallTotals):
    """One unit's contribution to its property's rent waterfall (waterfall-followups item
    4, optional per-unit drill-down — see `RentWaterfall.units`). Same components as the
    property/portfolio totals, scoped to just this one unit; sums exactly to the parent
    `RentWaterfall`'s own totals block, the same "parts sum to the whole" guarantee
    `PortfolioRentWaterfall.properties` already has. Single-asset properties (no units)
    never populate this — see `RentWaterfall.units`."""

    unit_id: str
    unit_number: str
    label: str | None = None


class RentWaterfall(RentWaterfallTotals):
    """One property's rent waterfall for `period_from`/`period_to` (default: the latest
    month with actual data on file, same convention as the rent roll's variance window —
    see `RentRoll.period_from`/`period_to`). `period_from`/`period_to`/`unit_count` are
    null/0 only when the property has no summarized unit-month data at all yet.

    `units` (waterfall-followups item 4, optional): a per-unit breakdown of this same
    total, ranked by total rent leakage (loss-to-lease + vacancy + collections) — biggest
    under-collectors first, same convention as `PortfolioRentWaterfall.properties`. Empty
    for a single-asset (unit-less) property, which has no per-unit basis to decompose."""

    property_id: str
    property_name: str
    period_from: date | None = None
    period_to: date | None = None
    unit_count: int = 0
    units: list[UnitRentWaterfallRow] = []


class PropertyRentWaterfallRow(RentWaterfallTotals):
    """One property's contribution to the portfolio rent waterfall (see
    `PortfolioRentWaterfall.properties`) — same components, scoped to just that property's
    units, for ranking which assets carry the most vacancy loss / loss-to-lease/collections
    loss rather than only seeing the blended portfolio total."""

    property_id: str
    property_name: str
    unit_count: int


class PortfolioRentWaterfall(RentWaterfallTotals):
    """Portfolio-wide rent waterfall (optionally `tags`-scoped, OR semantics), plus a
    per-property breakdown (`properties`) whose components sum exactly to this totals
    block. `period_from`/`period_to`/`unit_count` are null/0 only when the scope has no
    summarized unit-month data at all yet."""

    period_from: date | None = None
    period_to: date | None = None
    unit_count: int = 0
    properties: list[PropertyRentWaterfallRow] = []


# ---- Account general settings (migration 0019): display currency ----
class GeneralSettingsIn(BaseModel):
    """Account-wide display preferences. Currency is a pure symbol/locale toggle — it never
    converts or changes any stored amount."""

    currency: str = Field(pattern="^(USD|GBP)$")


class GeneralSettingsOut(GeneralSettingsIn):
    pass


# ---- Compliance certificates (migration 0020): UK licensing / safety certs ----
class PropertyCertificateIn(BaseModel):
    """Create/update a compliance certificate on a property. ``expiry_date`` is required (the
    status is derived from it); everything else is optional reference data."""

    cert_type: str = Field(min_length=1, max_length=80)
    expiry_date: date
    issue_date: date | None = None
    reference: str | None = Field(default=None, max_length=120)
    provider: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=2000)


class PropertyCertificateOut(BaseModel):
    """A stored certificate plus its property identity and a DERIVED status. ``status`` and
    ``days_to_expiry`` are computed at read time from ``expiry_date`` versus today, never
    stored, so they are always current. ``status``: 'valid' | 'expiring' (within the alert
    window) | 'expired'."""

    id: str
    property_id: str
    property_name: str
    cert_type: str
    expiry_date: date
    issue_date: date | None = None
    reference: str | None = None
    provider: str | None = None
    notes: str | None = None
    status: str
    days_to_expiry: int

    class Config:
        from_attributes = True
