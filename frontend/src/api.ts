// Thin API client for the FastAPI backend.
// Phase 1: login. Phase 2: P&L aggregations. Phase 3: CRUD + month workflow.

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export type Classification =
  | "rent"
  | "operating"
  | "capex"
  | "debt_service"
  | "other_below_line";

export const CLASSIFICATIONS: Classification[] = [
  "rent",
  "operating",
  "capex",
  "debt_service",
  "other_below_line",
];

export type PeriodState = "draft" | "posted" | "locked";

export interface PnLMetrics {
  gross_rent: number;
  operating_expenses: number;
  noi: number;
  capex: number;
  debt_service: number;
  other_below_line: number;
  below_noi: number;
  cash_flow: number;
}

export interface MonthlyPnL extends PnLMetrics {
  month: string;
}

// Property P&L splits the rollup honestly: top-level metrics are the property total,
// `units` is the additive sum across units, `property_tier` is unit_id-NULL items
// (shared capex / debt service) that are NOT allocated to units.
export interface PropertyMonthlyPnL extends MonthlyPnL {
  units: PnLMetrics;
  property_tier: PnLMetrics;
}

export interface UnitMonthlyPnL extends MonthlyPnL {
  unit_id: string;
  unit_number: string;
  label: string | null;
}

export interface PeriodRange {
  from?: string; // inclusive month, YYYY-MM-01
  to?: string;
}

function rangeQuery(range?: PeriodRange): string {
  const p = new URLSearchParams();
  if (range?.from) p.set("from", range.from);
  if (range?.to) p.set("to", range.to);
  const q = p.toString();
  return q ? `?${q}` : "";
}

export async function login(email: string, password: string): Promise<string> {
  // OAuth2 password flow expects form-encoded `username`/`password`.
  const body = new URLSearchParams({ username: email, password });
  const res = await fetch(`${API_URL}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!res.ok) throw new Error("Login failed");
  const data = await res.json();
  return data.access_token as string;
}

// Central 401 hook: App.tsx registers a handler (force logout) once at startup so an
// expired/invalid token surfaces as "you were signed out," not a generic error banner
// repeated in every view that happens to fetch next. Views' own .catch(setError) still
// runs too (request() still throws), this just adds the app-wide side effect.
let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: (() => void) | null) {
  onUnauthorized = fn;
}

async function request<T>(
  token: string,
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    if (res.status === 401) onUnauthorized?.();
    // FastAPI puts the human-readable message in `detail`.
    let detail = `${res.status}`;
    try {
      const data = await res.json();
      if (data?.detail) detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

const getJson = <T>(token: string, path: string) => request<T>(token, "GET", path);

export function getPortfolioMonthly(
  token: string,
  range?: PeriodRange,
): Promise<MonthlyPnL[]> {
  return getJson(token, `/portfolio/monthly${rangeQuery(range)}`);
}

export function getPropertyMonthly(
  token: string,
  propertyId: string,
  range?: PeriodRange,
): Promise<PropertyMonthlyPnL[]> {
  return getJson(token, `/properties/${propertyId}/monthly${rangeQuery(range)}`);
}

export function getPropertyUnitsMonthly(
  token: string,
  propertyId: string,
  range?: PeriodRange,
): Promise<UnitMonthlyPnL[]> {
  return getJson(token, `/properties/${propertyId}/units/monthly${rangeQuery(range)}`);
}

export function getUnitMonthly(
  token: string,
  unitId: string,
  range?: PeriodRange,
): Promise<UnitMonthlyPnL[]> {
  return getJson(token, `/units/${unitId}/monthly${rangeQuery(range)}`);
}

// Period totals across the whole hierarchy: portfolio total + per-property (+ per-unit).
export interface UnitBreakdown extends PnLMetrics {
  unit_id: string;
  unit_number: string;
  label: string | null;
}
export interface PropertyBreakdown extends PnLMetrics {
  property_id: string;
  property_name: string;
  type: "multifamily" | "single";
  units: UnitBreakdown[];
  property_tier: PnLMetrics;
}
export interface PortfolioBreakdown {
  total: PnLMetrics;
  properties: PropertyBreakdown[];
}

export function getPortfolioBreakdown(
  token: string,
  range?: PeriodRange,
): Promise<PortfolioBreakdown> {
  return getJson(token, `/portfolio/breakdown${rangeQuery(range)}`);
}

// ---- Portfolio dashboard: KPI band + T12 sparklines, read strictly from the
//      pre-aggregated portfolio_month_summary (instant at scale) ----
export interface OccupancyMetrics extends PnLMetrics {
  occupancy: number | null; // fraction 0..1; null if no unit roster
  occupied_units: number;
  total_units: number;
}

export interface SummaryMetrics extends OccupancyMetrics {
  property_count: number;
}

export interface TrendPoint {
  month: string;
  gross_rent: number;
  operating_expenses: number;
  noi: number;
  cash_flow: number;
  occupancy: number | null;
}

export interface PortfolioDashboard {
  period_from: string | null;
  period_to: string | null;
  prior_from: string | null;
  prior_to: string | null;
  current: SummaryMetrics | null;
  prior: SummaryMetrics | null;
  trend: TrendPoint[];
}

// Period-aware: no range → latest single month (vs prior month); a range (month / YTD / T12 /
// custom) sums the period and compares to the preceding equal-length period.
export function getPortfolioDashboard(
  token: string,
  range?: PeriodRange,
): Promise<PortfolioDashboard> {
  return getJson(token, `/portfolio/dashboard${rangeQuery(range)}`);
}

// ---- Attention feed: ranked exceptions, computed from the rollups ----
export type AttentionType =
  | "noi_drop"
  | "expense_spike"
  | "vacancy"
  | "high_vacancy"
  | "missing_data";

export interface AttentionItem {
  type: AttentionType;
  property_id: string;
  property_name: string;
  unit_id: string | null;
  unit_number: string | null;
  category: string | null;
  month: string | null; // the month the exception occurred in (within the period)
  magnitude: number;
  current: number | null;
  prior: number | null;
  change: number | null;
  pct_change: number | null;
  detail: Record<string, unknown>;
  label: string;
  // Set when many near-identical unit-level items (same property/month/type) were
  // collapsed into this one summary line; count is how many units were rolled up.
  rolled_up: boolean;
  count: number | null;
}

export interface AttentionFeed {
  period_from: string | null;
  period_to: string | null;
  items: AttentionItem[];
  thresholds: Record<string, number>;
}

// Exceptions occurring anywhere in the period (each tagged with its month), not just the
// last month — so a mid-period anomaly in a YTD/T12 view still surfaces.
export function getAttentionFeed(token: string, range?: PeriodRange): Promise<AttentionFeed> {
  return getJson(token, `/portfolio/attention${rangeQuery(range)}`);
}

// ---- Property detail (sub-step 4): scoped KPI band + unit roster ----
export interface PropertyDashboard {
  property_id: string;
  property_name: string;
  type: "multifamily" | "single";
  period_from: string | null;
  period_to: string | null;
  prior_from: string | null;
  prior_to: string | null;
  current: OccupancyMetrics | null;
  prior: OccupancyMetrics | null;
  trend: TrendPoint[];
}

export interface UnitRosterRow extends PnLMetrics {
  unit_id: string;
  unit_number: string;
  label: string | null;
  status: "occupied" | "vacant";
  noi_change: number | null;
}

export interface UnitRoster {
  month: string | null;
  prior_month: string | null;
  total: number;
  rows: UnitRosterRow[];
}

export function getPropertyDashboard(
  token: string,
  propertyId: string,
  range?: PeriodRange,
): Promise<PropertyDashboard> {
  return getJson(token, `/properties/${propertyId}/dashboard${rangeQuery(range)}`);
}

export function getPropertyAttention(
  token: string,
  propertyId: string,
  range?: PeriodRange,
): Promise<AttentionFeed> {
  return getJson(token, `/properties/${propertyId}/attention${rangeQuery(range)}`);
}

export interface RosterQuery {
  month?: string;
  sort?: string;
  order?: "asc" | "desc";
  limit?: number;
  offset?: number;
}

export function getUnitRoster(
  token: string,
  propertyId: string,
  q: RosterQuery = {},
): Promise<UnitRoster> {
  const p = new URLSearchParams();
  if (q.month) p.set("month", q.month);
  if (q.sort) p.set("sort", q.sort);
  if (q.order) p.set("order", q.order);
  if (q.limit != null) p.set("limit", String(q.limit));
  if (q.offset != null) p.set("offset", String(q.offset));
  const qs = p.toString();
  return getJson(token, `/properties/${propertyId}/units/roster${qs ? `?${qs}` : ""}`);
}

// ---- Unit detail (sub-step 5, Level 3) ----
export interface UnitDetailMonth extends PnLMetrics {
  month: string;
  status: "occupied" | "vacant";
}

export interface UnitDetail {
  unit_id: string;
  unit_number: string;
  label: string | null;
  property_id: string;
  property_name: string;
  status: "occupied" | "vacant";
  months: UnitDetailMonth[];
}

export function getUnitDetail(token: string, unitId: string): Promise<UnitDetail> {
  return getJson(token, `/units/${unitId}/detail`);
}

// ---- Attention settings (sub-step 6): per-account configurable thresholds ----
export interface AttentionThresholds {
  noi_drop_min_abs: number;
  noi_drop_min_pct: number;
  expense_spike_min_abs: number;
  expense_spike_min_pct: number;
  unit_noi_drop_min_abs: number;
  unit_noi_drop_min_pct: number;
  unit_expense_spike_min_abs: number;
  unit_expense_spike_min_pct: number;
  vacancy_min_occupancy_drop_pct: number;
  vacancy_high_absolute_pct: number;
}

export interface AttentionSettings extends AttentionThresholds {
  updated_at: string;
}

export function getAttentionSettings(token: string): Promise<AttentionSettings> {
  return getJson(token, "/settings/attention");
}

export function updateAttentionSettings(
  token: string,
  body: AttentionThresholds,
): Promise<AttentionSettings> {
  return request(token, "PUT", "/settings/attention", body);
}

export interface Me {
  id: string;
  email: string;
  role: "admin" | "member";
  is_active: boolean;
}

export function getMe(token: string): Promise<Me> {
  return getJson(token, "/auth/me");
}

// ============================ Phase 3: CRUD + workflow ======================

export interface Property {
  id: string;
  name: string;
  type: "multifamily" | "single";
  address: string | null;
  created_at: string;
}

export interface Unit {
  id: string;
  property_id: string;
  unit_number: string;
  label: string | null;
}

export interface Category {
  id: string;
  name: string;
  default_classification: Classification;
  active: boolean;
}

export interface LineItem {
  id: string;
  monthly_record_id: string;
  category_id: string;
  category_name: string | null;
  classification: Classification | null; // per-line override
  amount: number;
}

export interface MonthlyRecord {
  id: string;
  property_id: string;
  unit_id: string | null;
  month: string;
  notes: string | null;
  line_items: LineItem[];
}

export interface PeriodStatus {
  id: string;
  property_id: string;
  month: string;
  status: PeriodState;
}

// ---- Properties ----
export const listProperties = (t: string) => getJson<Property[]>(t, "/properties");
export const createProperty = (t: string, body: Pick<Property, "name" | "type" | "address">) =>
  request<Property>(t, "POST", "/properties", body);
export const updateProperty = (t: string, id: string, body: Partial<Pick<Property, "name" | "type" | "address">>) =>
  request<Property>(t, "PATCH", `/properties/${id}`, body);
export const deleteProperty = (t: string, id: string) =>
  request<void>(t, "DELETE", `/properties/${id}`);

// ---- Units ----
export const listUnits = (t: string, propertyId: string) =>
  getJson<Unit[]>(t, `/properties/${propertyId}/units`);
export const createUnit = (t: string, propertyId: string, body: { unit_number: string; label?: string | null }) =>
  request<Unit>(t, "POST", `/properties/${propertyId}/units`, body);
export const updateUnit = (t: string, id: string, body: { unit_number?: string; label?: string | null }) =>
  request<Unit>(t, "PATCH", `/units/${id}`, body);
export const deleteUnit = (t: string, id: string) => request<void>(t, "DELETE", `/units/${id}`);

// ---- Categories ----
export const listCategories = (t: string, activeOnly = false) =>
  getJson<Category[]>(t, `/categories${activeOnly ? "?active_only=true" : ""}`);
export const createCategory = (t: string, body: { name: string; default_classification: Classification; active?: boolean }) =>
  request<Category>(t, "POST", "/categories", body);
export const updateCategory = (t: string, id: string, body: Partial<Pick<Category, "name" | "default_classification" | "active">>) =>
  request<Category>(t, "PATCH", `/categories/${id}`, body);

// ---- Records + line items ----
export interface RecordUpsert {
  property_id: string;
  unit_id: string | null;
  month: string;
  notes?: string | null;
  line_items: { category_id: string; classification?: Classification | null; amount: number }[];
}

export function listRecords(
  t: string,
  filters: { property_id?: string; unit_id?: string; month?: string },
): Promise<MonthlyRecord[]> {
  const p = new URLSearchParams();
  if (filters.property_id) p.set("property_id", filters.property_id);
  if (filters.unit_id) p.set("unit_id", filters.unit_id);
  if (filters.month) p.set("month", filters.month);
  const q = p.toString();
  return getJson(t, `/records${q ? `?${q}` : ""}`);
}
export const upsertRecord = (t: string, body: RecordUpsert) =>
  request<MonthlyRecord>(t, "POST", "/records", body);
export const deleteRecord = (t: string, id: string) => request<void>(t, "DELETE", `/records/${id}`);

// ---- Period workflow ----
export function listPeriods(
  t: string,
  filters: { property_id?: string; month?: string } = {},
): Promise<PeriodStatus[]> {
  const p = new URLSearchParams();
  if (filters.property_id) p.set("property_id", filters.property_id);
  if (filters.month) p.set("month", filters.month);
  const q = p.toString();
  return getJson(t, `/periods${q ? `?${q}` : ""}`);
}
export const setPeriodStatus = (t: string, body: { property_id: string; month: string; status: PeriodState }) =>
  request<PeriodStatus>(t, "PUT", "/periods", body);
export const unlockPeriod = (t: string, periodId: string) =>
  request<PeriodStatus>(t, "POST", `/periods/${periodId}/unlock`);

// ============================ Phase 4: bulk import ==========================

export interface ImportIssue {
  row: number | null;
  field: string | null;
  message: string;
}

export interface ImportReport {
  dry_run: boolean;
  on_error: "abort" | "skip";
  committed: boolean;
  total_rows: number;
  valid_rows: number;
  invalid_rows: number;
  applied_line_items: number;
  records_touched: number;
  errors: ImportIssue[];
}

export interface MissingScope {
  property_id: string;
  property_name: string;
  type: string;
  unit_id: string | null;
  unit_number: string | null;
}

// canonical import field -> source column header
export type ColumnMapping = Partial<
  Record<"property" | "property_id" | "unit" | "unit_id" | "month" | "category" | "category_id" | "classification" | "amount", string>
>;

export function getImportTemplate(token: string): Promise<string> {
  return fetch(`${API_URL}/import/template`, {
    headers: { Authorization: `Bearer ${token}` },
  }).then((r) => r.text());
}

export async function importFile(
  token: string,
  file: File,
  mapping: ColumnMapping,
  opts: { dryRun: boolean; onError: "abort" | "skip" },
): Promise<ImportReport> {
  const form = new FormData();
  form.append("file", file);
  form.append("mapping", JSON.stringify(mapping));
  form.append("dry_run", String(opts.dryRun));
  form.append("on_error", opts.onError);
  // No Content-Type header: the browser sets the multipart boundary itself.
  const res = await fetch(`${API_URL}/import/file`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body: form,
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    // 422 file-level errors carry a full report in `detail`.
    if (data?.detail && typeof data.detail === "object") return data.detail as ImportReport;
    throw new Error(data?.detail ? String(data.detail) : `Import failed (${res.status})`);
  }
  return data as ImportReport;
}

export function getMissing(token: string, month: string): Promise<MissingScope[]> {
  return getJson(token, `/import/missing?month=${month}`);
}

// ---- Structured rows import (the seam every parser targets) ----
export interface ImportRowInput {
  property?: string;
  property_id?: string;
  unit?: string | null;
  unit_id?: string;
  month: string; // YYYY-MM-01
  category?: string;
  category_id?: string;
  classification?: Classification | null;
  amount: number;
  source_row?: number;
}

export function importRows(
  token: string,
  rows: ImportRowInput[],
  opts: { dryRun: boolean; onError: "abort" | "skip" },
): Promise<ImportReport> {
  const q = new URLSearchParams({
    dry_run: String(opts.dryRun),
    on_error: opts.onError,
  });
  return request<ImportReport>(token, "POST", `/import/rows?${q}`, rows);
}

// ---- PDF statement extraction (free, on-machine) ----
export interface StatementRow {
  unit: string | null;
  category: string;
  category_id: string | null;
  unknown_category: boolean;
  classification: Classification | null;
  amount: number;
}

export interface StatementPreview {
  backend: string; // "ollama" | "heuristic"
  detected_property: string | null;
  property_id: string | null;
  property_unknown: boolean;
  detected_month: string | null; // YYYY-MM-01
  rows: StatementRow[];
  unknown_categories: string[];
  warnings: string[];
}

export async function extractStatement(token: string, file: File): Promise<StatementPreview> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${API_URL}/import/statement/extract`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body: form,
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error(data?.detail ? String(data.detail) : `Extraction failed (${res.status})`);
  }
  return data as StatementPreview;
}

export interface UnknownCategory {
  name: string;
  suggested_classification: Classification | null;
}

export interface StatementBatchItem {
  filename: string;
  preview: StatementPreview | null;
  error: string | null;
}

export interface StatementBatchPreview {
  items: StatementBatchItem[];
  unknown_properties: string[];
  unknown_categories: UnknownCategory[];
}

export async function extractStatementsBatch(
  token: string,
  files: File[],
): Promise<StatementBatchPreview> {
  const form = new FormData();
  for (const f of files) form.append("files", f);
  const res = await fetch(`${API_URL}/import/statement/extract-batch`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body: form,
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error(data?.detail ? String(data.detail) : `Extraction failed (${res.status})`);
  }
  return data as StatementBatchPreview;
}

// ==================== Investment insights (acquisition inputs + returns) =====
// Inputs are stored; every metric is computed on read from those inputs + the
// property_month_summary rollup, so they always track the current P&L.
export interface PropertyInvestmentInput {
  purchase_price: number;
  closing_costs: number;
  loan_amount: number; // 0 = all-cash
  purchase_date: string; // YYYY-MM-DD
}

export interface PropertyInvestmentOut extends PropertyInvestmentInput {
  property_id: string;
  equity_invested: number; // purchase_price - loan_amount + closing_costs
  updated_at: string;
}

// Metrics are null when they can't be computed honestly (see backend gates):
// cash_on_cash needs >=12 months of data; avg_cash_on_cash needs >=24; dscr needs
// recorded debt service. `annualized` marks a <12-month trailing window (cap rate only).
export interface InvestmentMetrics {
  property_id: string;
  property_name: string;
  type: "multifamily" | "single";
  purchase_price: number | null;
  closing_costs: number | null;
  loan_amount: number | null;
  purchase_date: string | null;
  equity_invested: number | null;
  months_available: number;
  t12_months: number;
  t12_noi: number | null;
  t12_cash_flow: number | null;
  t12_debt_service: number | null;
  annualized: boolean;
  cap_rate: number | null;
  cash_on_cash: number | null;
  dscr: number | null;
  avg_cash_on_cash: number | null;
}

export interface PortfolioInvestment {
  properties: InvestmentMetrics[];
  cap_rate: number | null; // Σ annualized-NOI / Σ price (value-weighted)
  cash_on_cash: number | null; // Σ T12 cash flow / Σ equity (>=12mo props only)
  dscr: number | null; // Σ T12 NOI / Σ T12 debt (props with debt)
  total_purchase_price: number;
  total_equity_invested: number;
  cap_rate_property_count: number;
  cash_on_cash_property_count: number;
  dscr_property_count: number;
}

export const getInvestmentMetrics = (t: string, id: string) =>
  getJson<InvestmentMetrics>(t, `/properties/${id}/investment/metrics`);
export const putInvestment = (t: string, id: string, body: PropertyInvestmentInput) =>
  request<PropertyInvestmentOut>(t, "PUT", `/properties/${id}/investment`, body);
export const deleteInvestment = (t: string, id: string) =>
  request<void>(t, "DELETE", `/properties/${id}/investment`);
export const getPortfolioInvestment = (t: string) =>
  getJson<PortfolioInvestment>(t, "/investments");

// ==================== Audit log (admin-only, read-only) ======================
// audit_log is written today by POST /periods/{id}/unlock; this just reads it back so
// "who reopened a locked month and why" is answerable without a DB console.
export interface AuditLogEntry {
  id: string;
  user_id: string | null;
  user_email: string | null;
  action: string;
  entity: string;
  entity_id: string | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  created_at: string;
}

export function getAuditLog(
  token: string,
  opts: { limit?: number; offset?: number; entity?: string } = {},
): Promise<AuditLogEntry[]> {
  const p = new URLSearchParams();
  if (opts.limit != null) p.set("limit", String(opts.limit));
  if (opts.offset != null) p.set("offset", String(opts.offset));
  if (opts.entity) p.set("entity", opts.entity);
  const q = p.toString();
  return getJson(token, `/audit${q ? `?${q}` : ""}`);
}
