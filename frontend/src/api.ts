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

function rangeQuery(range?: PeriodRange, tags?: string[]): string {
  const p = new URLSearchParams();
  if (range?.from) p.set("from", range.from);
  if (range?.to) p.set("to", range.to);
  for (const t of tags ?? []) p.append("tags", t);
  const q = p.toString();
  return q ? `?${q}` : "";
}

/** Register a NEW account (not a user inside an existing one) and sign straight in.
 *  The new account starts empty — its own properties, its own category list, its own
 *  thresholds — and shares nothing with any other account. */
export async function signup(
  email: string,
  password: string,
  accountName?: string,
): Promise<string> {
  const res = await fetch(`${API_URL}/auth/signup`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      email,
      password,
      account_name: accountName?.trim() || null,
    }),
  });
  if (!res.ok) {
    // 409 = email already registered; 422 = password too short / bad email.
    const detail = await res.json().catch(() => null);
    throw new Error(
      typeof detail?.detail === "string" ? detail.detail : "Could not create account",
    );
  }
  const data = await res.json();
  return data.access_token as string;
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
  tags?: string[],
): Promise<MonthlyPnL[]> {
  return getJson(token, `/portfolio/monthly${rangeQuery(range, tags)}`);
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
  tags?: string[],
): Promise<PortfolioBreakdown> {
  return getJson(token, `/portfolio/breakdown${rangeQuery(range, tags)}`);
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
  tags?: string[],
): Promise<PortfolioDashboard> {
  return getJson(token, `/portfolio/dashboard${rangeQuery(range, tags)}`);
}

// ---- Attention feed: ranked exceptions, computed from the rollups ----
export type AttentionType =
  | "noi_drop"
  | "expense_spike"
  | "vacancy"
  // Property-level "occupancy fell materially this month" (gated by
  // vacancy_min_occupancy_drop_pct). Absorbs the unit vacancies that caused it.
  | "occupancy_drop"
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
export function getAttentionFeed(
  token: string,
  range?: PeriodRange,
  tags?: string[],
): Promise<AttentionFeed> {
  return getJson(token, `/portfolio/attention${rangeQuery(range, tags)}`);
}

// ---- Portfolio-wide worst-units leaderboard: top-N units (across every property) by NOI
// drop in the period, ranked by $ magnitude. Complements the property-scoped attention feed
// (worst units within ONE property) and the portfolio attention feed (worst properties).
// A row can be a single unit's drop, or — when a tight cluster of same-property/same-month/
// same-magnitude drops collapses (see backend attention._cluster_and_rollup) — a rolled-up
// summary row for many units at once (rolled_up=true, unit_id/unit_number null, count set).
export interface WorstUnitItem {
  property_id: string;
  property_name: string;
  unit_id: string | null;
  unit_number: string | null;
  month: string;
  current: number | null;
  prior: number | null;
  change: number | null;
  pct_change: number | null;
  magnitude: number;
  label: string;
  rolled_up: boolean;
  count: number | null;
}

export interface WorstUnitsLeaderboard {
  period_from: string | null;
  period_to: string | null;
  items: WorstUnitItem[];
}

export function getWorstUnits(
  token: string,
  range?: PeriodRange,
  opts: { limit?: number; tags?: string[] } = {},
): Promise<WorstUnitsLeaderboard> {
  const q = rangeQuery(range, opts.tags);
  const sep = q ? "&" : "?";
  return getJson(
    token,
    `/portfolio/worst-units${q}${opts.limit != null ? `${sep}limit=${opts.limit}` : ""}`,
  );
}

// ---- Portfolio segmentation (tags): free-text tags on a property, plus an optional
// portfolio-wide filter (OR semantics) threaded through dashboard/breakdown/attention.
export const getTags = (t: string) => getJson<string[]>(t, "/tags");
export const getPropertyTags = (t: string, propertyId: string) =>
  getJson<string[]>(t, `/properties/${propertyId}/tags`);
export const addPropertyTag = (t: string, propertyId: string, tag: string) =>
  request<string[]>(t, "POST", `/properties/${propertyId}/tags`, { tag });
export const removePropertyTag = (t: string, propertyId: string, tag: string) =>
  request<string[]>(t, "DELETE", `/properties/${propertyId}/tags/${encodeURIComponent(tag)}`);

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
  // "vacant" = a record WAS posted (explicitly flagged, or $0 rent); "missing" = no record
  // posted for this unit-month at all — the two used to be indistinguishable.
  status: "occupied" | "vacant" | "missing";
  noi_change: number | null;
  // Migration 0012, additive: the unit's CURRENT lease state (today) — independent of
  // `status` above (a historical per-month fact); see the Rent Roll tab for full detail.
  lease_status?: LeaseStatus | null;
  lease_tenant_name?: string | null;
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
  status: "occupied" | "vacant" | "missing";
}

export interface UnitDetail {
  unit_id: string;
  unit_number: string;
  label: string | null;
  property_id: string;
  property_name: string;
  status: "occupied" | "vacant" | "missing";
  // Migration 0012, additive: the unit's CURRENT lease state (today) — independent of
  // `status`/`months` above (record-driven history); see the Rent Roll tab for full detail.
  lease_status?: LeaseStatus;
  lease_tenant_name?: string | null;
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
  /** The account whose data this session can see — the tenancy boundary. */
  account_id: string;
  account_name: string;
  /** Account display currency ('USD' | 'GBP'); drives the currency symbol app-wide. */
  account_currency: string;
}

export function getMe(token: string): Promise<Me> {
  return getJson(token, "/auth/me");
}

// ---- General settings: account display currency (migration 0019) ----
export interface GeneralSettings {
  currency: "USD" | "GBP";
}

export function getGeneralSettings(token: string): Promise<GeneralSettings> {
  return getJson(token, "/settings/general");
}

export function updateGeneralSettings(
  token: string,
  body: GeneralSettings,
): Promise<GeneralSettings> {
  return request(token, "PUT", "/settings/general", body);
}

// ---- Compliance: statutory certificates / licences (migration 0020) ----
export type CertificateStatus = "valid" | "expiring" | "expired";

export interface Certificate {
  id: string;
  property_id: string;
  property_name: string;
  cert_type: string;
  expiry_date: string;
  issue_date: string | null;
  reference: string | null;
  provider: string | null;
  notes: string | null;
  status: CertificateStatus;
  days_to_expiry: number;
}

export interface CertificateInput {
  cert_type: string;
  expiry_date: string;
  issue_date?: string | null;
  reference?: string | null;
  provider?: string | null;
  notes?: string | null;
}

export function getCertificateTypes(token: string): Promise<string[]> {
  return getJson(token, "/compliance/types");
}

export function getAllCertificates(token: string): Promise<Certificate[]> {
  return getJson(token, "/compliance/certificates");
}

export function getCertificateAlerts(token: string): Promise<Certificate[]> {
  return getJson(token, "/compliance/alerts");
}

export function getPropertyCertificates(
  token: string,
  propertyId: string,
): Promise<Certificate[]> {
  return getJson(token, `/properties/${propertyId}/certificates`);
}

export function addPropertyCertificate(
  token: string,
  propertyId: string,
  body: CertificateInput,
): Promise<Certificate> {
  return request(token, "POST", `/properties/${propertyId}/certificates`, body);
}

export function updateCertificate(
  token: string,
  certificateId: string,
  body: CertificateInput,
): Promise<Certificate> {
  return request(token, "PUT", `/certificates/${certificateId}`, body);
}

export function deleteCertificate(token: string, certificateId: string): Promise<void> {
  return request(token, "DELETE", `/certificates/${certificateId}`);
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
  // Migration 0015 — the unit's market/asking rent (rent waterfall's GPR reference
  // figure). Admin-editable via updateUnit. Reference data only, never feeds NOI.
  market_rent: number | null;
}

export interface Category {
  id: string;
  name: string;
  default_classification: Classification;
  active: boolean;
  // Only populated when listCategories(t, activeOnly, true) is called — how many line
  // items reference this category. 0 flags an unused merge/cleanup candidate.
  usage_count?: number | null;
}

export interface CategoryMergeResult {
  source_id: string;
  target_id: string;
  reassigned_count: number;
  deactivated: boolean;
}

export interface LineItem {
  id: string;
  monthly_record_id: string;
  category_id: string;
  category_name: string | null;
  classification: Classification | null; // per-line override
  amount: number;
  // Non-null ⇒ the line is stated as a RATE ("management: 8% of rent") and `amount` above is
  // that rate applied to the month's rent, maintained by the server whenever rent changes.
  rate_pct: number | null;
}

export interface MonthlyRecord {
  id: string;
  property_id: string;
  unit_id: string | null;
  month: string;
  notes: string | null;
  // Explicit "this unit is vacant this month" flag — distinct from having no record at all.
  // Meaningless for a property-tier record (unit_id null).
  is_vacant: boolean;
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
export const updateUnit = (
  t: string,
  id: string,
  body: { unit_number?: string; label?: string | null; market_rent?: number | null },
) => request<Unit>(t, "PATCH", `/units/${id}`, body);
export const deleteUnit = (t: string, id: string) => request<void>(t, "DELETE", `/units/${id}`);

// ---- Categories ----
export const listCategories = (t: string, activeOnly = false, includeUsage = false) => {
  const params = new URLSearchParams();
  if (activeOnly) params.set("active_only", "true");
  if (includeUsage) params.set("include_usage", "true");
  const qs = params.toString();
  return getJson<Category[]>(t, `/categories${qs ? `?${qs}` : ""}`);
};
export const createCategory = (t: string, body: { name: string; default_classification: Classification; active?: boolean }) =>
  request<Category>(t, "POST", "/categories", body);
export const updateCategory = (t: string, id: string, body: Partial<Pick<Category, "name" | "default_classification" | "active">>) =>
  request<Category>(t, "PATCH", `/categories/${id}`, body);
// Merge source category into target: reassigns every line item, deactivates the source,
// and refreshes affected month summaries server-side. Admin only (server-gated 403 for
// non-admins). A source/target with different classifications is server-rejected (409)
// unless `allowClassificationChange` is passed — the UI's own confirm dialog already warns
// about this before setting the flag, so the user flow is unchanged.
export const mergeCategory = (
  t: string,
  sourceId: string,
  targetId: string,
  allowClassificationChange = false,
) =>
  request<CategoryMergeResult>(t, "POST", `/categories/${sourceId}/merge`, {
    target_id: targetId,
    allow_classification_change: allowClassificationChange,
  });

// ---- Records + line items ----
export interface RecordUpsert {
  property_id: string;
  unit_id: string | null;
  month: string;
  notes?: string | null;
  is_vacant?: boolean;
  // Send `amount` for a fixed figure, or `rate_pct` for a rate-stated line whose amount the
  // server derives from rent — never both (the API rejects it).
  line_items: {
    category_id: string;
    classification?: Classification | null;
    amount: number;
    rate_pct?: number | null;
  }[];
}

// The rent a percentage line is charged on: the whole property's rent for the month when
// `unit_id` is null (the property-tier basis), else that unit's own rent.
export interface RentBasis {
  property_id: string;
  unit_id: string | null;
  month: string;
  rent: number;
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
export function getRentBasis(
  t: string,
  q: { property_id: string; month: string; unit_id?: string | null },
): Promise<RentBasis> {
  const p = new URLSearchParams({ property_id: q.property_id, month: q.month });
  if (q.unit_id) p.set("unit_id", q.unit_id);
  return getJson(t, `/rent-basis?${p.toString()}`);
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
  // "line" = read off the statement; "rent" = a property's rent on a rent roll; "allocated" =
  // this property's computed share of a charge the statement made once over the whole
  // portfolio. Allocated rows are marked in the review so a computed figure is never mistaken
  // for one the agent printed against this property.
  kind: "line" | "rent" | "allocated";
  note: string | null;
}

export interface StatementPreview {
  backend: string; // "ollama" | "heuristic" | "rent_roll"
  format: "single" | "rent_roll"; // the shape of the FILE this came from
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
  // Set when this item is ONE property split out of a multi-property rent roll: the PDF it
  // came from. Twelve items can share one source file.
  source_file: string | null;
}

// What one uploaded PDF turned out to be, and what the parser has to say about the FILE —
// whether the rents add back to the total it states, which portfolio charges were split
// across the properties, which figures named nobody. Said once per file rather than repeated
// on every property card it produced.
export interface StatementFileNote {
  filename: string;
  format: "single" | "rent_roll";
  statements: number;
  warnings: string[];
}

export interface StatementBatchPreview {
  items: StatementBatchItem[];
  files: StatementFileNote[];
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
  // Set when these figures came from a bulk purchase: closing costs and loan are then this
  // property's allocated share of a deal-wide total, not figures entered for it alone.
  acquisition_id: string | null;
  acquisition_name: string | null;
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
  // Non-null when the inputs came from a bulk purchase — closing costs and loan are then an
  // allocated share. Cap rate is unaffected (it uses only the real agreed price).
  acquisition_id: string | null;
  acquisition_name: string | null;
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

export interface MissingInvestmentProperty {
  property_id: string;
  property_name: string;
  type: string;
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
  // Adoption-gap nudge: how many properties have no acquisition data at all yet.
  total_property_count: number;
  missing_property_count: number;
  missing_properties: MissingInvestmentProperty[];
}

export const getInvestmentMetrics = (t: string, id: string) =>
  getJson<InvestmentMetrics>(t, `/properties/${id}/investment/metrics`);
export const putInvestment = (t: string, id: string, body: PropertyInvestmentInput) =>
  request<PropertyInvestmentOut>(t, "PUT", `/properties/${id}/investment`, body);
export const deleteInvestment = (t: string, id: string) =>
  request<void>(t, "DELETE", `/properties/${id}/investment`);
export const getPortfolioInvestment = (t: string) =>
  getJson<PortfolioInvestment>(t, "/investments");

// ---- Portfolio (bulk) acquisitions: one deal, costs allocated across several properties ----
// A bulk purchase has per-property prices but ONE closing-cost figure and ONE blanket loan.
// The server spreads those shared costs across the members (pro-rata by price, evenly, or by
// explicit shares) and writes the split onto each property's own investment row, so every
// return metric reads the same place it always did. Allocation is done server-side so the
// preview table and the saved deal can never round the leftover cents differently.
export type AllocationMethod = "price" | "equal" | "custom";

export interface AcquisitionMemberInput {
  property_id: string;
  purchase_price: number;
  // Only read under "custom" allocation; ignored (and may be omitted) otherwise.
  closing_costs?: number | null;
  loan_amount?: number | null;
}

export interface PortfolioAcquisitionInput {
  name: string;
  purchase_date: string; // YYYY-MM-DD — shared by every property in the deal
  total_closing_costs: number;
  total_loan_amount: number;
  allocation_method: AllocationMethod;
  notes?: string | null;
  members: AcquisitionMemberInput[]; // at least 2 — one property is an ordinary purchase
}

export interface AcquisitionMember {
  property_id: string;
  property_name: string;
  purchase_price: number;
  closing_costs: number; // allocated share
  loan_amount: number; // allocated share
  equity_invested: number;
  price_share: number; // fraction of the combined price (the pro-rata weight)
}

export interface PortfolioAcquisition {
  id: string;
  name: string;
  purchase_date: string;
  total_closing_costs: number;
  total_loan_amount: number;
  allocation_method: AllocationMethod;
  notes: string | null;
  created_at: string;
  updated_at: string;
  members: AcquisitionMember[];
  property_count: number;
  total_purchase_price: number;
  total_equity_invested: number;
  // What is actually stored on the member rows right now, and how far that has drifted from
  // the deal's stated totals. Non-zero drift means a member was hand-edited on its own
  // property page, so the parts no longer sum to the deal.
  allocated_closing_costs: number;
  allocated_loan_amount: number;
  closing_costs_drift: number;
  loan_amount_drift: number;
}

export interface AcquisitionPreview {
  members: AcquisitionMember[];
  total_purchase_price: number;
  total_closing_costs: number;
  total_loan_amount: number;
  total_equity_invested: number;
}

export const getAcquisitions = (t: string) =>
  getJson<PortfolioAcquisition[]>(t, "/acquisitions");
export const previewAcquisition = (t: string, body: PortfolioAcquisitionInput) =>
  request<AcquisitionPreview>(t, "POST", "/acquisitions/preview", body);
export const createAcquisition = (t: string, body: PortfolioAcquisitionInput) =>
  request<PortfolioAcquisition>(t, "POST", "/acquisitions", body);
export const updateAcquisition = (t: string, id: string, body: PortfolioAcquisitionInput) =>
  request<PortfolioAcquisition>(t, "PUT", `/acquisitions/${id}`, body);
// Deleting only UNGROUPS by default: members keep their allocated figures and their metrics.
// `purge` is the explicit opt-in to also clear their acquisition data.
export const deleteAcquisition = (t: string, id: string, purge = false) =>
  request<void>(t, "DELETE", `/acquisitions/${id}${purge ? "?purge=true" : ""}`);

// ---- Shared expenses: one bill covering several properties, split across them monthly ----
// A blanket-loan payment, a multi-property insurance policy, one management retainer. The
// arrangement is stored once (category, monthly amount, members, basis) and POSTED to a month
// or a range, which writes an ordinary property-tier line item onto each member. Nothing else
// in the app learns a new concept — the P&L, NOI and exports read line_items as they always
// did. The split is computed server-side so the preview and the saved postings can never
// round the leftover cents differently.
export type SharedAllocationMethod = "equal" | "price" | "units" | "custom";

// What to do when a member's property-month already holds a line in this category that this
// arrangement did not post. "fail" is the default: a bulk write must never silently destroy a
// hand-entered figure.
export type SharedOnConflict = "fail" | "replace" | "skip";

// Why a figure is already sitting in the target slot. Only "manual" and "other_shared" are
// conflicts; "this" is a previous post of the same arrangement and is updated in place.
export type SharedExistingSource = "none" | "this" | "manual" | "other_shared";

export interface SharedExpenseMemberInput {
  property_id: string;
  // Only read under "custom" allocation; ignored (and may be omitted) otherwise.
  custom_share?: number | null;
}

export interface SharedExpenseInput {
  name: string;
  category_id: string;
  classification?: string | null; // optional per-line override of the category default
  amount: number; // the bill for ONE month
  allocation_method: SharedAllocationMethod;
  notes?: string | null;
  members: SharedExpenseMemberInput[]; // at least 2 — one property is an ordinary line item
}

export interface SharedExpenseMember {
  property_id: string;
  property_name: string;
  amount: number; // this property's monthly share
  basis: number; // the raw weighting figure used (price, door count, or 1 for equal)
  weight_share: number; // that figure as a fraction of the total
}

export interface SharedExpense {
  id: string;
  name: string;
  category_id: string;
  category_name: string | null;
  classification: string | null;
  amount: number;
  allocation_method: SharedAllocationMethod;
  notes: string | null;
  created_at: string;
  updated_at: string;
  members: SharedExpenseMember[];
  property_count: number;
  allocated_amount: number; // sum of the shares — equals `amount` by construction
  posted_months: string[]; // months this arrangement has actually written line items into
}

export interface SharedExpenseSplit {
  members: SharedExpenseMember[];
  total_amount: number;
  allocation_method: SharedAllocationMethod;
}

export interface SharedExpensePostInput {
  from_month: string; // YYYY-MM-DD (first of month)
  to_month?: string | null; // defaults to from_month — a single month
  on_conflict?: SharedOnConflict;
}

export interface SharedExpensePostRow {
  month: string;
  property_id: string;
  property_name: string;
  amount: number;
  existing_amount: number | null;
  existing_source: SharedExistingSource;
  locked: boolean;
}

export interface SharedExpensePostPlan {
  months: string[];
  rows: SharedExpensePostRow[];
  amount_per_month: number;
  total_amount: number;
  conflict_count: number;
  locked_count: number;
  blocked: boolean; // the post as requested cannot go through as-is
}

export interface SharedExpensePostResult {
  months: string[];
  rows: SharedExpensePostRow[];
  line_items_written: number;
  records_created: number;
  skipped: number;
  total_posted: number;
}

export interface SharedExpenseUnpostResult {
  months: string[];
  line_items_removed: number;
  total_removed: number;
}

export const getSharedExpenses = (t: string) => getJson<SharedExpense[]>(t, "/shared-expenses");
export const previewSharedSplit = (t: string, body: SharedExpenseInput) =>
  request<SharedExpenseSplit>(t, "POST", "/shared-expenses/split", body);
export const createSharedExpense = (t: string, body: SharedExpenseInput) =>
  request<SharedExpense>(t, "POST", "/shared-expenses", body);
export const updateSharedExpense = (t: string, id: string, body: SharedExpenseInput) =>
  request<SharedExpense>(t, "PUT", `/shared-expenses/${id}`, body);
// Deleting only DETACHES by default: everything already posted stays in the ledger, since the
// money was really spent. `purge` is the explicit opt-in to remove those postings too.
export const deleteSharedExpense = (t: string, id: string, purge = false) =>
  request<void>(t, "DELETE", `/shared-expenses/${id}${purge ? "?purge=true" : ""}`);

// Dry run: exactly which property-months a post would write, what stands there now, and
// whether anything blocks it. Same server code path as the real post.
export const previewSharedPost = (t: string, id: string, body: SharedExpensePostInput) =>
  request<SharedExpensePostPlan>(t, "POST", `/shared-expenses/${id}/post/preview`, body);
export const postSharedExpense = (t: string, id: string, body: SharedExpensePostInput) =>
  request<SharedExpensePostResult>(t, "POST", `/shared-expenses/${id}/post`, body);
// Removes only the line items this arrangement posted; a hand-entered figure in the same
// category is left alone.
export const unpostSharedExpense = (t: string, id: string, from: string, to?: string | null) =>
  request<SharedExpenseUnpostResult>(
    t,
    "DELETE",
    `/shared-expenses/${id}/post?from_month=${from}${to ? `&to_month=${to}` : ""}`,
  );

// ---- Portfolio benchmarking: every property vs. the portfolio's simple-mean average on
// NOI/unit, opex ratio, physical + economic occupancy, cap rate, and cash-on-cash ----
export type BenchmarkMetricKey =
  | "noi_per_unit"
  | "opex_ratio"
  | "physical_occupancy"
  | "economic_occupancy"
  | "cap_rate"
  | "cash_on_cash";

export interface BenchmarkValue {
  value: number | null;
  delta_vs_mean: number | null;
  rank: number | null; // 1 = best-performing property for this metric
  percentile: number | null; // 0..100, 100 = best
}

export interface PropertyBenchmarkRow extends Record<BenchmarkMetricKey, BenchmarkValue> {
  property_id: string;
  property_name: string;
  type: "multifamily" | "single";
}

export interface BenchmarkMetricStats {
  mean: number | null;
  median: number | null;
  count: number; // properties with a computable value for this metric
  higher_is_better: boolean;
}

export interface PortfolioBenchmarks extends Record<BenchmarkMetricKey, BenchmarkMetricStats> {
  period_from: string | null;
  period_to: string | null;
  tags: string[] | null;
  property_count: number;
  properties: PropertyBenchmarkRow[];
}

export function getPortfolioBenchmarks(
  token: string,
  range?: PeriodRange,
  tags?: string[],
): Promise<PortfolioBenchmarks> {
  return getJson(token, `/portfolio/benchmarks${rangeQuery(range, tags)}`);
}

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

// ==================== Budget / variance (flat annual plan, actual-vs-plan) ===
// property_budget is a NEW parallel table (one flat annual figure per property/year); it
// never touches actuals. The monthly/pro-rated plan and every variance number are computed
// on read, same convention as investment metrics. Reads are open to any authed user; writing
// a budget is admin-only (mirrors the other portfolio-shaping mutations).
export interface PropertyBudgetInput {
  budgeted_gross_rent: number;
  budgeted_operating_expenses: number;
}

export interface PropertyBudgetOut extends PropertyBudgetInput {
  property_id: string;
  year: number;
  budgeted_noi: number; // budgeted_gross_rent - budgeted_operating_expenses
  updated_at: string;
}

// plan_*/variance_* are null only when the period has zero budget coverage;
// plan_coverage_months < total_months means the plan is partial, not that it's $0.
export interface VarianceMetrics {
  period_from: string | null;
  period_to: string | null;
  total_months: number;
  plan_coverage_months: number;
  actual_gross_rent: number;
  actual_operating_expenses: number;
  actual_noi: number;
  plan_gross_rent: number | null;
  plan_operating_expenses: number | null;
  plan_noi: number | null;
  variance_gross_rent: number | null;
  variance_operating_expenses: number | null;
  variance_noi: number | null;
  variance_noi_pct: number | null;
}

export interface PropertyVariance extends VarianceMetrics {
  property_id: string;
  property_name: string;
}

export interface PortfolioVariance extends VarianceMetrics {
  budgeted_property_count: number;
}

export const listPropertyBudgets = (t: string, propertyId: string) =>
  getJson<PropertyBudgetOut[]>(t, `/properties/${propertyId}/budgets`);
export const putPropertyBudget = (t: string, propertyId: string, year: number, body: PropertyBudgetInput) =>
  request<PropertyBudgetOut>(t, "PUT", `/properties/${propertyId}/budgets/${year}`, body);
export const deletePropertyBudget = (t: string, propertyId: string, year: number) =>
  request<void>(t, "DELETE", `/properties/${propertyId}/budgets/${year}`);
export const getPropertyVariance = (t: string, propertyId: string, range?: PeriodRange) =>
  getJson<PropertyVariance>(t, `/properties/${propertyId}/variance${rangeQuery(range)}`);
export const getPortfolioVariance = (t: string, range?: PeriodRange) =>
  getJson<PortfolioVariance>(t, `/portfolio/variance${rangeQuery(range)}`);

// ==================== Export (Excel/CSV) ======================================
// Every export route on the backend calls the SAME queries.py functions the JSON
// endpoints above already use (no recomputation), so the downloaded file always shows
// the same numbers as the currently-viewed screen. This just fetches the file as a
// blob (with the Bearer token — a plain <a href> download can't attach one) and
// triggers a browser download via a throwaway <a download> element.
export type ExportFormat = "xlsx" | "csv";

async function downloadExport(token: string, path: string, fallbackFilename: string): Promise<void> {
  const res = await fetch(`${API_URL}${path}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    if (res.status === 401) onUnauthorized?.();
    let detail = `${res.status}`;
    try {
      const data = await res.json();
      if (data?.detail) detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  const blob = await res.blob();
  // Prefer the filename the server put in Content-Disposition (it encodes the property
  // name / date range) over the generic fallback.
  const disposition = res.headers.get("Content-Disposition") ?? "";
  const match = /filename="?([^";]+)"?/.exec(disposition);
  const filename = match?.[1] ?? fallbackFilename;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function withFormat(q: string, format: ExportFormat): string {
  return q ? `${q}&format=${format}` : `?format=${format}`;
}

// Portfolio monthly P&L — mirrors the Dashboard's "Monthly detail" table, respecting the
// active period range and tag filter.
export function exportPortfolioMonthly(
  token: string,
  range: PeriodRange | undefined,
  tags: string[] | undefined,
  format: ExportFormat,
): Promise<void> {
  const path = `/export/portfolio/monthly${withFormat(rangeQuery(range, tags), format)}`;
  return downloadExport(token, path, `portfolio-monthly-pnl.${format}`);
}

// Portfolio actual-vs-plan variance for the active period.
export function exportPortfolioVariance(
  token: string,
  range: PeriodRange | undefined,
  format: ExportFormat,
): Promise<void> {
  const path = `/export/portfolio/variance${withFormat(rangeQuery(range), format)}`;
  return downloadExport(token, path, `portfolio-variance.${format}`);
}

// Property monthly P&L — mirrors PropertyDetail's "Monthly P&L" table (combined total +
// unit-rollup + property-tier columns), respecting the active period range.
export function exportPropertyMonthly(
  token: string,
  propertyId: string,
  range: PeriodRange | undefined,
  format: ExportFormat,
): Promise<void> {
  const path = `/export/properties/${propertyId}/monthly${withFormat(rangeQuery(range), format)}`;
  return downloadExport(token, path, `property-monthly-pnl.${format}`);
}

// Property actual-vs-plan variance for the active period.
export function exportPropertyVariance(
  token: string,
  propertyId: string,
  range: PeriodRange | undefined,
  format: ExportFormat,
): Promise<void> {
  const path = `/export/properties/${propertyId}/variance${withFormat(rangeQuery(range), format)}`;
  return downloadExport(token, path, `property-variance.${format}`);
}

// ==================== Rent roll / lease-level data (migration 0012) ==========
// `lease` is a NEW parallel table (a unit's tenancy history); contract_rent is reference
// data and never feeds NOI/cash-flow, which stays driven solely by monthly_records/line
// items. Reads are open to any authed user; writes (create/update/delete a lease) are
// admin-gated server-side, so the edit form below is disabled (not hidden) for non-admins
// — same convention as BudgetSection/PropertyTags in Manage.tsx.
export type LeaseStatus = "active" | "notice" | "expired" | "vacant";
export const LEASE_STATUSES: LeaseStatus[] = ["active", "notice", "expired", "vacant"];

export type LeaseType = "fixed" | "mtm";

export interface LeaseInput {
  tenant_name: string;
  start_date: string; // YYYY-MM-DD
  end_date: string | null; // null = month-to-month
  contract_rent: number;
  status: LeaseStatus;
  // v2 fields (migration 0013) — all reference/terms data, never fed into NOI/cash-flow.
  // `lease_type` is NOT here: it's server-derived from `end_date` (see LeaseOut) so an
  // edit can never leave it inconsistent with end_date.
  security_deposit?: number | null;
  escalation_pct?: number | null; // annual (or escalation_frequency_months-cadence) bump, %
  escalation_frequency_months?: number;
  // Percentage rent — retail leases only; leave both null for a residential lease.
  pct_rent_rate?: number | null; // overage rate, %
  pct_rent_breakpoint?: number | null; // annual sales breakpoint
  // Standing $/month rent concession (migration 0016) — the rent waterfall's `concessions`
  // bridge line is built from this. MUST be carried through on every save (this is a
  // full-replace PATCH) or an edit to any other field would silently null it out.
  concession_monthly?: number | null;
}

export interface Lease extends LeaseInput {
  id: string;
  unit_id: string;
  lease_type: LeaseType; // server-derived, read-only
  created_at: string;
  updated_at: string;
}

export const listUnitLeases = (t: string, unitId: string) =>
  getJson<Lease[]>(t, `/units/${unitId}/leases`);
export const createUnitLease = (t: string, unitId: string, body: LeaseInput) =>
  request<Lease>(t, "POST", `/units/${unitId}/leases`, body);
export const updateLease = (t: string, leaseId: string, body: LeaseInput) =>
  request<Lease>(t, "PATCH", `/leases/${leaseId}`, body);
export const deleteLease = (t: string, leaseId: string) =>
  request<void>(t, "DELETE", `/leases/${leaseId}`);

// Rent roll: one row per unit, its current lease (resolved server-side: the lease covering
// today, or the most recent on file), the latest actual recorded rent, and months-to-expiry.
// `status` is the lease's own status and is authoritative for occupancy here — 'vacant'
// means no lease on file at all. `missing_data` flags an occupied unit with no monthly
// record for the property's latest summarized month, kept distinct from a real vacancy.
// For a vacant row, `tenant_name` and `months_to_expiry` are null (no current tenancy/term
// to report) — `contract_rent` is still populated as the unit's asking/potential rent.
export interface RentRollRow {
  unit_id: string;
  unit_number: string;
  label: string | null;
  property_id: string;
  property_name: string;
  lease_id: string | null;
  tenant_name: string | null;
  lease_start: string | null;
  lease_end: string | null;
  contract_rent: number | null;
  status: LeaseStatus;
  actual_rent: number | null;
  actual_month: string | null;
  missing_data: boolean;
  months_to_expiry: number | null; // null = month-to-month
  // v2 (migration 0013) — all reference/terms data.
  security_deposit: number | null;
  escalation_pct: number | null;
  escalation_frequency_months: number | null;
  next_escalation_date: string | null; // next scheduled bump, derived
  lease_type: LeaseType | null; // null = no lease on file
  // True when this lease's term has lapsed but the tenant is still recorded as paying rent
  // and no newer lease has replaced it — distinct from a clean active lease and from a
  // true vacancy. Still counts "occupied" (status is unchanged, typically 'expired').
  holdover: boolean;
  // For a vacant row only: days since the prior lease's end_date ("downtime"). Null when
  // the unit has never had a lease on file.
  vacant_days: number | null;
  // Percentage-rent terms — retail leases only; both null for a residential lease.
  pct_rent_rate: number | null;
  pct_rent_breakpoint: number | null;
  // The unit's own market_rent (migration 0015) — the rent waterfall's GPR reference
  // figure, admin-editable via `updateUnit`. Independent of the lease/status above.
  market_rent: number | null;
  // Standing $/month concession (migration 0016) on the CURRENT lease, if any — null when
  // there's no lease on file, or the lease has no concession.
  concession_monthly: number | null;
  // Expected-vs-actual rent variance (reference data — never feeds NOI/cash-flow), summed
  // over the rent roll's `period_from`/`period_to` window. `expected_rent` is this unit's
  // escalated contract rent for every covered month a lease was in force (0 for a month
  // it was genuinely vacant; a holdover month keeps the lapsed lease's last in-effect
  // rent — see the backend docstring for the exact rule). `period_actual_rent` is the
  // same actual-gross-rent source the rent roll already uses, summed over the same
  // months. `variance` = actual − expected; `variance_pct` = variance ÷ expected (null
  // when expected is $0).
  expected_rent: number | null;
  period_actual_rent: number | null;
  variance: number | null;
  variance_pct: number | null;
}

export interface OccupancySummary {
  total_units: number;
  occupied_units: number;
  physical_occupancy: number | null; // fraction 0..1
  gross_potential_rent: number | null; // occupied contract rent + vacant potential rent
  // TRUE economic occupancy: actual collected rent / Gross Potential Rent (includes
  // vacant units' potential rent) — captures vacancy loss, so this is <= physical
  // occupancy whenever there's vacancy. See `rent_realization` for the old metric.
  economic_occupancy: number | null;
  // Actual / contract rent over OCCUPIED units only (excludes vacant units by
  // construction) — a collections-vs-contract signal, NOT an occupancy metric; can read
  // above physical occupancy / 100%.
  rent_realization: number | null;
  // Average downtime (days) across vacant units with a known prior end_date.
  avg_vacant_days: number | null;
}

// Property/portfolio-level expected-vs-actual rent-variance rollup, for `RentRoll`'s
// `period_from`/`period_to` window. Excludes shell/synthetic units, same as `occupancy`.
export interface RentVarianceRollup {
  total_expected_rent: number;
  total_actual_rent: number;
  variance: number; // total_actual_rent - total_expected_rent
  variance_pct: number | null; // null when total_expected_rent is $0
  unit_count: number;
}

export interface RentRoll {
  property_id: string | null; // null = portfolio-wide
  as_of: string;
  rows: RentRollRow[];
  occupancy: OccupancySummary;
  // Expected-vs-actual rent-variance window + rollup. Defaults (when no `range` is passed
  // to getPropertyRentRoll/getPortfolioRentRoll) to the latest month with any actual data
  // on file for the scope — both null only when the scope has no summarized data at all.
  period_from: string | null;
  period_to: string | null;
  rent_variance: RentVarianceRollup | null;
}

export function getPropertyRentRoll(
  token: string,
  propertyId: string,
  range?: PeriodRange,
): Promise<RentRoll> {
  return getJson(token, `/properties/${propertyId}/rent-roll${rangeQuery(range)}`);
}
export function getPortfolioRentRoll(
  token: string,
  tags?: string[],
  range?: PeriodRange,
): Promise<RentRoll> {
  return getJson(token, `/portfolio/rent-roll${rangeQuery(range, tags)}`);
}

// Rent waterfall (migration 0015: units.market_rent). Reference data — never feeds NOI.
// GPR steps down through the losses to actual collected; identity holds to the cent:
// gpr - loss_to_lease - vacancy_loss - collections_loss - actual_collected === residual (~0).
export interface RentWaterfallTotals {
  gpr: number;
  loss_to_lease: number; // market - in-place scheduled, over occupied months (negative = gain-to-lease)
  vacancy_loss: number; // full market rent over vacant months
  // scheduled - actual, over occupied months. Kept for backward compat — now always equals
  // concessions + bad_debt exactly (waterfall-followups item 3).
  collections_loss: number;
  // The split of collections_loss (item 3): concessions = modeled standing lease discounts
  // (lease.concession_monthly, migration 0016), capped per month at that month's own
  // shortfall; bad_debt = collections_loss - concessions (delinquency/the remainder; can be
  // negative in the same months collections_loss itself is negative — a collections gain).
  concessions: number;
  bad_debt: number;
  actual_collected: number;
  residual: number; // balance check, ~0 by construction
  // Each line as a fraction of gpr (0.055 = 5.5%; NOT pre-multiplied by 100 — same
  // convention as LeaseExpirationSummary.pct_of_portfolio_rent). null when gpr is 0.
  loss_to_lease_pct_of_gpr: number | null;
  vacancy_loss_pct_of_gpr: number | null;
  collections_loss_pct_of_gpr: number | null;
  concessions_pct_of_gpr: number | null;
  bad_debt_pct_of_gpr: number | null;
  actual_collected_pct_of_gpr: number | null;
}
// One unit's contribution to its property's waterfall (item 4, optional per-unit
// drill-down) — same components as the totals, scoped to one unit; sums exactly to the
// parent RentWaterfall's own totals.
export interface UnitRentWaterfallRow extends RentWaterfallTotals {
  unit_id: string;
  unit_number: string;
  label: string | null;
}
export interface RentWaterfall extends RentWaterfallTotals {
  property_id: string;
  property_name: string;
  period_from: string | null;
  period_to: string | null;
  unit_count: number;
  // Per-unit breakdown, ranked by total rent leakage (biggest first). Empty for a
  // single-asset (unit-less) property.
  units: UnitRentWaterfallRow[];
}
export interface PropertyRentWaterfallRow extends RentWaterfallTotals {
  property_id: string;
  property_name: string;
  unit_count: number;
}
export interface PortfolioRentWaterfall extends RentWaterfallTotals {
  period_from: string | null;
  period_to: string | null;
  unit_count: number;
  properties: PropertyRentWaterfallRow[];
}
export function getPropertyRentWaterfall(
  token: string,
  propertyId: string,
  range?: PeriodRange,
): Promise<RentWaterfall> {
  return getJson(token, `/properties/${propertyId}/rent-waterfall${rangeQuery(range)}`);
}
export function getPortfolioRentWaterfall(
  token: string,
  tags?: string[],
  range?: PeriodRange,
): Promise<PortfolioRentWaterfall> {
  return getJson(token, `/portfolio/rent-waterfall${rangeQuery(range, tags)}`);
}

// Lease-expiration / rollover-risk horizon.
export interface LeaseExpirationItem {
  unit_id: string;
  unit_number: string;
  label: string | null;
  property_id: string;
  property_name: string;
  tenant_name: string;
  lease_end: string;
  contract_rent: number;
  months_to_expiry: number;
}

export interface LeaseExpirations {
  within_months: number;
  as_of: string;
  items: LeaseExpirationItem[];
  total_contract_rent_expiring: number;
  pct_of_portfolio_rent: number | null;
}

export function getLeaseExpirations(
  token: string,
  withinMonths: number,
  tags?: string[],
): Promise<LeaseExpirations> {
  const p = new URLSearchParams({ within_months: String(withinMonths) });
  for (const t of tags ?? []) p.append("tags", t);
  return getJson(token, `/portfolio/lease-expirations?${p.toString()}`);
}

// Percentage rent (retail leases only, migration 0013): terms are stored on the lease;
// `annual_sales` is a caller-supplied, non-persisted input used only to compute overage
// on the fly (sales aren't tracked anywhere in this app).
export interface PercentageRentCalc {
  lease_id: string;
  has_percentage_rent_terms: boolean;
  pct_rent_rate: number | null;
  pct_rent_breakpoint: number | null;
  annual_sales: number | null;
  overage_rent: number | null;
}
export function getLeasePercentageRent(
  token: string,
  leaseId: string,
  annualSales?: number,
): Promise<PercentageRentCalc> {
  const q = annualSales != null ? `?annual_sales=${annualSales}` : "";
  return getJson(token, `/leases/${leaseId}/percentage-rent${q}`);
}
