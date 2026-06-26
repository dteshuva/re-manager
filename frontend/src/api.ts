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
