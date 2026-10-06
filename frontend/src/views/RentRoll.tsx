import { Fragment, useEffect, useMemo, useState } from "react";
import {
  createUnitLease,
  getLeaseExpirations,
  getLeasePercentageRent,
  getMe,
  getPortfolioRentRoll,
  getPortfolioRentWaterfall,
  getPropertyRentRoll,
  getPropertyRentWaterfall,
  getTags,
  listProperties,
  updateLease,
  updateUnit,
  LEASE_STATUSES,
  type LeaseExpirations,
  type LeaseInput,
  type LeaseStatus,
  type PercentageRentCalc,
  type PeriodRange,
  type PortfolioRentWaterfall,
  type Property,
  type RentRoll as RentRollData,
  type RentRollRow,
  type RentWaterfall,
} from "../api";
import { toneClass } from "../components/KpiBand";
import PropertySearchSelect from "../components/PropertySearchSelect";
import { clickableProps } from "../hooks/clickable";
import { hasConcessions, t } from "../terms";
import { currencySymbol, fmtCurrency, fmtDate, fmtMonth, fmtMonthsToExpiry, fmtPct, isUK, leaseStatusPillClass } from "../ui";

type SortKey =
  | "property_name"
  | "unit_number"
  | "tenant_name"
  | "contract_rent"
  | "actual_rent"
  | "variance"
  | "arrears_balance"
  | "lease_end"
  | "months_to_expiry"
  | "status";

const COLUMNS: { key: SortKey; label: string }[] = [
  { key: "property_name", label: "Property" },
  { key: "unit_number", label: "Unit" },
  { key: "tenant_name", label: "Tenant" },
  { key: "contract_rent", label: "Contract rent" },
  { key: "actual_rent", label: "Actual rent" },
  { key: "variance", label: "Rent variance" },
  { key: "arrears_balance", label: "Arrears" },
  { key: "lease_end", label: "Lease end" },
  { key: "months_to_expiry", label: "Months to expiry" },
  { key: "status", label: "Status" },
];

// Text/date columns read oddly right-aligned (the `.data-table` default, meant for
// numeric columns) — these stay left-aligned in both the main roll and the expirations
// table. Numeric-ish columns (rent figures, months-to-expiry) keep the default.
const LEFT_ALIGN: Partial<Record<SortKey, true>> = {
  unit_number: true,
  tenant_name: true,
  lease_end: true,
};
const leftStyle = (on: boolean) => (on ? { textAlign: "left" as const } : undefined);

// null-safe comparator: nulls always sort last regardless of direction.
function compareRows(a: RentRollRow, b: RentRollRow, key: SortKey, dir: 1 | -1): number {
  const av = a[key];
  const bv = b[key];
  if (av == null && bv == null) return 0;
  if (av == null) return 1;
  if (bv == null) return -1;
  if (typeof av === "number" && typeof bv === "number") return (av - bv) * dir;
  return String(av).localeCompare(String(bv)) * dir;
}

const EXPIRY_HORIZONS = [
  { months: 1, label: "30 days" },
  { months: 3, label: "90 days" },
  { months: 6, label: "180 days" },
  { months: 12, label: "12 months" },
];

const emptyLeaseForm = (): LeaseInput => ({
  tenant_name: null,
  start_date: new Date().toISOString().slice(0, 10),
  end_date: null,
  contract_rent: 0,
  status: "active",
  security_deposit: null,
  escalation_pct: null,
  escalation_frequency_months: 12,
  pct_rent_rate: null,
  pct_rent_breakpoint: null,
  concession_monthly: null,
  last_rent_increase_date: null,
  rent_before_increase: null,
  opening_arrears: null,
  arrears_from_month: null,
});

// Rent roll: lease + actual-rent + rollover-risk view per unit, filterable by property and
// tag, with an occupancy summary and a lease-expiration ("rollover risk") section. Lease
// editing is admin-gated client-side (server enforces it too, same convention as
// Manage.tsx's BudgetSection/PropertyTags).
export default function RentRoll({ token }: { token: string }) {
  const [properties, setProperties] = useState<Property[]>([]);
  const [propertyId, setPropertyId] = useState("");
  const [allTags, setAllTags] = useState<string[]>([]);
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [data, setData] = useState<RentRollData | null>(null);
  const [waterfall, setWaterfall] = useState<RentWaterfall | PortfolioRentWaterfall | null>(null);
  const [expirations, setExpirations] = useState<LeaseExpirations | null>(null);
  const [horizonMonths, setHorizonMonths] = useState(3);
  // Expected-vs-actual rent-variance window ("YYYY-MM" <input type="month"> values, blank
  // = let the server default to the latest month with actual data on file). Only sent to
  // the API when at least one is set — see `varianceRange` below.
  const [periodFromInput, setPeriodFromInput] = useState("");
  const [periodToInput, setPeriodToInput] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: "property_name", dir: 1 });
  const [error, setError] = useState<string | null>(null);
  const [status, setStatusMsg] = useState<string | null>(null);
  const [editingUnitId, setEditingUnitId] = useState<string | null>(null);
  const [form, setForm] = useState<LeaseInput>(emptyLeaseForm());
  // Per-unit waterfall drill-down (item 4, optional) is collapsed by default — a property
  // can have 100+ units, and most visits to this view just want the property/portfolio
  // totals.
  const [showUnitBreakdown, setShowUnitBreakdown] = useState(false);
  // The rent roll drills down property → units: property rows are collapsed by default and
  // expand to reveal that property's unit rows (same pattern as the dashboard's
  // BreakdownTable), so the portfolio-wide roll isn't a thousand-row wall.
  const [openProperties, setOpenProperties] = useState<Record<string, boolean>>({});

  useEffect(() => {
    listProperties(token).then(setProperties).catch((e) => setError(e.message));
    getTags(token).then(setAllTags).catch(() => {/* tag filter is a nice-to-have */});
    getMe(token).then((m) => setIsAdmin(m.role === "admin")).catch(() => setIsAdmin(false));
  }, [token]);

  // `guard` is only set (and checked) when called from the effect below, so a slow,
  // superseded request can't overwrite fresher state if the user changes the
  // property/tags/horizon again before it resolves. The manual post-save call from
  // `saveLease` fires without a guard — it's a direct response to a completed user
  // action, not a race-prone dependency-driven fetch.
  // Undefined (not {}) when both inputs are blank, so getPropertyRentRoll/
  // getPortfolioRentRoll omit `from`/`to` entirely and the server applies its own default
  // (latest month with actual data on file) rather than us guessing one client-side.
  const varianceRange: PeriodRange | undefined =
    periodFromInput || periodToInput
      ? { from: periodFromInput ? `${periodFromInput}-01` : undefined, to: periodToInput ? `${periodToInput}-01` : undefined }
      : undefined;

  const reload = (guard?: { cancelled: boolean }) => {
    const req = propertyId
      ? getPropertyRentRoll(token, propertyId, varianceRange)
      : getPortfolioRentRoll(token, selectedTags, varianceRange);
    req.then((d) => !guard?.cancelled && setData(d)).catch((e) => !guard?.cancelled && setError(e.message));
    const wf = propertyId
      ? getPropertyRentWaterfall(token, propertyId, varianceRange)
      : getPortfolioRentWaterfall(token, selectedTags, varianceRange);
    wf.then((w) => !guard?.cancelled && setWaterfall(w)).catch((e) => !guard?.cancelled && setError(e.message));
    getLeaseExpirations(token, horizonMonths, propertyId ? undefined : selectedTags)
      .then((exp) => !guard?.cancelled && setExpirations(exp))
      .catch((e) => !guard?.cancelled && setError(e.message));
  };
  useEffect(() => {
    const guard = { cancelled: false };
    reload(guard);
    return () => {
      guard.cancelled = true;
    };
  }, [token, propertyId, selectedTags, horizonMonths, periodFromInput, periodToInput]);

  const toggleTag = (t: string) =>
    setSelectedTags((cur) => (cur.includes(t) ? cur.filter((x) => x !== t) : [...cur, t]));

  const sortedRows = useMemo(() => {
    if (!data) return [];
    return [...data.rows].sort((a, b) => compareRows(a, b, sort.key, sort.dir));
  }, [data, sort]);

  // Unit rows grouped under their property for the drill-down. Properties are listed
  // alphabetically; the active column sort applies to the units *within* each property
  // (so e.g. sorting by rent variance ranks the units inside whichever property you open).
  const propertyGroups = useMemo(() => {
    const byId = new Map<string, { property_id: string; property_name: string; rows: RentRollRow[] }>();
    for (const r of sortedRows) {
      let g = byId.get(r.property_id);
      if (!g) {
        g = { property_id: r.property_id, property_name: r.property_name, rows: [] };
        byId.set(r.property_id, g);
      }
      g.rows.push(r);
    }
    return [...byId.values()].sort((a, b) => a.property_name.localeCompare(b.property_name));
  }, [sortedRows]);

  // Property groups start COLLAPSED (owner's preference), except when there's only one group
  // to show — then the drill-down has nothing to hide and would just cost a click.
  //
  // The reason a collapsed roll used to be unusable is fixed separately and still holds: the
  // per-unit "Add lease"/"Edit" controls were reachable only inside an expanded group, so a
  // 15-property roll hid every one of them. "Expand all properties" below, plus the "+ Add
  // lease" control that now sits in the always-visible Tenant column, mean collapsing no longer
  // puts anything out of reach.
  const autoExpand = useMemo(() => propertyGroups.length === 1, [propertyGroups]);
  const allOpen = propertyGroups.every((g) => (openProperties[g.property_id] ?? autoExpand));

  const setAllOpen = (open: boolean) =>
    setOpenProperties(Object.fromEntries(propertyGroups.map((g) => [g.property_id, open])));

  // The expirations feed is portfolio-wide; when a specific property is selected, narrow it
  // client-side rather than adding a property-scoped endpoint just for this section.
  const expirationItems = useMemo(() => {
    if (!expirations) return [];
    return propertyId ? expirations.items.filter((i) => i.property_id === propertyId) : expirations.items;
  }, [expirations, propertyId]);

  const onSort = (key: SortKey) =>
    setSort((cur) => (cur.key === key ? { key, dir: cur.dir === 1 ? -1 : 1 } : { key, dir: 1 }));

  const startEdit = (row: RentRollRow) => {
    setEditingUnitId(row.unit_id);
    setStatusMsg(null);
    setForm({
      tenant_name: row.tenant_name,
      start_date: row.lease_start ?? new Date().toISOString().slice(0, 10),
      end_date: row.lease_end,
      contract_rent: row.contract_rent ?? 0,
      status: row.status === "vacant" && !row.lease_id ? "active" : row.status,
      security_deposit: row.security_deposit,
      escalation_pct: row.escalation_pct,
      escalation_frequency_months: row.escalation_frequency_months ?? 12,
      pct_rent_rate: row.pct_rent_rate,
      pct_rent_breakpoint: row.pct_rent_breakpoint,
      concession_monthly: row.concession_monthly,
      // Migration 0024/0025 fields. Carried through the same way concession_monthly is:
      // `updateLease` is a full-replace PATCH, so omitting one here would silently wipe a
      // recorded rent increase or opening balance on any unrelated edit.
      last_rent_increase_date: row.last_rent_increase_date,
      rent_before_increase: row.rent_before_increase,
      opening_arrears: row.opening_arrears,
      arrears_from_month: row.arrears_from_month,
    });
  };

  const saveLease = (row: RentRollRow) => {
    setError(null);
    // No tenant-name guard: the name is optional (migration 0026). A tenancy onboarded from
    // agent statements often has only dates and a rent, and demanding a name just produces a
    // column of placeholders that can never be told apart from the real thing.
    const body: LeaseInput = { ...form, end_date: form.end_date || null };
    const req = row.lease_id
      ? updateLease(token, row.lease_id, body)
      : createUnitLease(token, row.unit_id, body);
    req
      .then(() => {
        setStatusMsg(`Saved lease for unit ${row.unit_number}.`);
        setEditingUnitId(null);
        reload();
      })
      .catch((e) => setError(e.message));
  };

  const occ = data?.occupancy;

  return (
    <section>
      {error && <p className="alert-error">{error}</p>}
      {status && <p style={{ color: "var(--positive)", fontWeight: 500 }}>{status}</p>}

      <div className="row" style={{ gap: 16, flexWrap: "wrap", alignItems: "flex-end", marginBottom: 14 }}>
        <PropertySearchSelect
          properties={properties}
          value={propertyId}
          onChange={setPropertyId}
          placeholder="All properties (portfolio-wide)"
          wrapperStyle={{ maxWidth: 320 }}
        />
      </div>

      {allTags.length > 0 && (
        <div className="tag-filter" style={{ marginBottom: 14 }}>
          <span className="muted" style={{ fontSize: 12.5, fontWeight: 550 }}>
            Filter by tag{propertyId ? " (portfolio-wide only)" : ""}:
          </span>
          {allTags.map((t) => (
            <button
              key={t}
              type="button"
              className={`tag-chip${selectedTags.includes(t) ? " is-active" : ""}`}
              onClick={() => toggleTag(t)}
              disabled={!!propertyId}
              aria-pressed={selectedTags.includes(t)}
            >
              {t}
            </button>
          ))}
          {selectedTags.length > 0 && (
            <button type="button" className="btn btn-ghost" onClick={() => setSelectedTags([])}>
              Clear
            </button>
          )}
        </div>
      )}

      <div className="row" style={{ gap: 16, flexWrap: "wrap", alignItems: "flex-end", marginBottom: 6 }}>
        <label className="stack" style={{ maxWidth: 160 }}>
          Rent-variance from
          <input
            className="input"
            type="month"
            value={periodFromInput}
            onChange={(e) => setPeriodFromInput(e.target.value)}
          />
        </label>
        <label className="stack" style={{ maxWidth: 160 }}>
          Rent-variance to
          <input
            className="input"
            type="month"
            value={periodToInput}
            onChange={(e) => setPeriodToInput(e.target.value)}
          />
        </label>
        {(periodFromInput || periodToInput) && (
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => {
              setPeriodFromInput("");
              setPeriodToInput("");
            }}
          >
            Reset to latest month
          </button>
        )}
      </div>
      <p className="hint" style={{ marginBottom: 14 }}>
        Expected rent (below) is each unit's escalated lease rent summed over this window; actual is the
        same posted-rent source the roll already uses, summed the same way. Leave blank to default to the
        latest month with data on file. Reference data only — does not change NOI or cash flow anywhere
        else in the app.
      </p>

      {occ && (
        <>
          <div className="kpi-grid" style={{ marginBottom: 6 }}>
            <div className="kpi-card">
              <div className="kpi-card__label">Physical occupancy</div>
              <div className="kpi-card__value">
                {occ.physical_occupancy == null ? "—" : fmtPct(occ.physical_occupancy)}
              </div>
              <div className="kpi-card__delta is-flat">
                {occ.occupied_units}/{occ.total_units} units
              </div>
            </div>
            <div className="kpi-card">
              <div className="kpi-card__label">Economic occupancy</div>
              <div className="kpi-card__value">
                {occ.economic_occupancy == null ? "—" : fmtPct(occ.economic_occupancy)}
              </div>
              <div className="kpi-card__delta is-flat">actual rent ÷ gross potential rent (incl. vacant)</div>
            </div>
            <div className="kpi-card">
              <div className="kpi-card__label">Rent realization</div>
              <div className="kpi-card__value">
                {occ.rent_realization == null ? "—" : fmtPct(occ.rent_realization)}
              </div>
              <div className="kpi-card__delta is-flat">actual ÷ contract rent, occupied units only</div>
            </div>
            <div className="kpi-card">
              <div className="kpi-card__label">Units on the roll</div>
              <div className="kpi-card__value">{occ.total_units}</div>
              <div className="kpi-card__delta is-flat">
                {data?.property_id ? "this property" : "portfolio-wide"}
              </div>
            </div>
            <div className="kpi-card">
              <div className="kpi-card__label">{isUK() ? "Avg. void period" : "Avg. vacancy downtime"}</div>
              <div className="kpi-card__value">
                {occ.avg_vacant_days == null ? "—" : `${Math.round(occ.avg_vacant_days)}d`}
              </div>
              <div className="kpi-card__delta is-flat">
                {`days since prior ${t("lease")} ended, ${t("vacant")} units`}
              </div>
            </div>
          </div>
          <p className="hint" style={{ marginBottom: 20 }}>
            Occupancy is {t("lease")}-based (point-in-time, as of{" "}
            {data?.as_of ? fmtDate(data.as_of) : "today"}) — a different signal from the record/actuals-based
            “{t("vacancy")}”/“missing data” items on the attention feed, which reflect posted monthly records
            rather than current {t("lease")} state.
          </p>
        </>
      )}

      {data?.rent_variance && (
        <>
          <h3 className="section-title">Rent variance</h3>
          <p className="hint">
            Expected (escalated lease) rent vs. actual collected rent
            {data.period_from && data.period_to && (
              <>
                {" "}
                for{" "}
                {data.period_from === data.period_to
                  ? fmtMonth(data.period_from)
                  : `${fmtMonth(data.period_from)} – ${fmtMonth(data.period_to)}`}
              </>
            )}
            , across {data.rent_variance.unit_count} unit{data.rent_variance.unit_count === 1 ? "" : "s"} (excludes
            shell/synthetic units). Loss-to-lease/collections signal only — reference data, does not feed NOI or
            cash flow.
          </p>
          <div className="kpi-grid" style={{ marginBottom: 20 }}>
            <div className="kpi-card">
              <div className="kpi-card__label">Expected rent</div>
              <div className="kpi-card__value">{fmtCurrency(data.rent_variance.total_expected_rent)}</div>
              <div className="kpi-card__delta is-flat">escalated lease rent, period total</div>
            </div>
            <div className="kpi-card">
              <div className="kpi-card__label">Actual rent</div>
              <div className="kpi-card__value">{fmtCurrency(data.rent_variance.total_actual_rent)}</div>
              <div className="kpi-card__delta is-flat">collected, same period</div>
            </div>
            <div className="kpi-card">
              <div className="kpi-card__label">Variance</div>
              <div className="kpi-card__value">
                {data.rent_variance.variance > 0 ? "+" : ""}
                {fmtCurrency(data.rent_variance.variance)}
              </div>
              <div className={`kpi-card__delta ${toneClass(data.rent_variance.variance, false)}`}>
                <span className="kpi-card__arrow">
                  {data.rent_variance.variance > 0 ? "▲" : data.rent_variance.variance < 0 ? "▼" : "■"}
                </span>
                <span className="kpi-card__pct">
                  {data.rent_variance.variance_pct == null
                    ? "—"
                    : `${data.rent_variance.variance_pct > 0 ? "+" : ""}${data.rent_variance.variance_pct.toFixed(1)}%`}
                </span>
              </div>
              <div className="kpi-card__pct" style={{ marginTop: 2 }}>
                {data.rent_variance.variance >= 0
                  ? "over expected (ahead of lease terms)"
                  : "under expected (shortfall / loss-to-lease)"}
              </div>
            </div>
          </div>
        </>
      )}

      {waterfall && waterfall.unit_count > 0 && (() => {
        const wf = waterfall;
        const scale = wf.gpr > 0 ? wf.gpr : 1;
        // Each step's effect on the bridge from GPR down to actual collected. A loss `v`
        // reduces the bridge by `v`; a negative loss (e.g. gain-to-lease when in-place rent
        // is above market) adds back. `base` rows are the GPR start and actual-collected end.
        // `pct` is each line's %-of-GPR (raw fraction; null when gpr is 0) — IC reads a rent
        // bridge as "loss-to-lease X%, vacancy Y%, concessions Z%, bad debt W%" as much as in
        // raw dollars. Concessions/bad debt (waterfall-followups item 3) replace the old
        // single combined "Collections loss & concessions" line — they sum to exactly the
        // same collections_loss total, just split into a leasing-decision piece and a
        // delinquency piece.
        const steps: { label: string; value: number; kind: "base" | "loss"; pct: number | null }[] = [
          { label: "Gross potential rent", value: wf.gpr, kind: "base", pct: wf.gpr > 0 ? 1 : null },
          { label: "Loss to lease", value: wf.loss_to_lease, kind: "loss", pct: wf.loss_to_lease_pct_of_gpr },
          {
            label: `${t("Vacancy")} loss`, value: wf.vacancy_loss, kind: "loss",
            pct: wf.vacancy_loss_pct_of_gpr,
          },
          // A standing rent concession isn't a UK letting concept, so for a £ account the split
          // collapses and the whole collections shortfall reads as arrears — which is what it is
          // here. `concessions + bad_debt === collections_loss` by construction, so the bridge
          // still balances to the penny either way.
          ...(hasConcessions()
            ? [
                {
                  label: "Concessions (free/discounted rent)", value: wf.concessions,
                  kind: "loss" as const, pct: wf.concessions_pct_of_gpr,
                },
                {
                  label: "Bad debt (delinquency)", value: wf.bad_debt,
                  kind: "loss" as const, pct: wf.bad_debt_pct_of_gpr,
                },
              ]
            : [
                {
                  label: "Arrears (rent owed, not collected)", value: wf.collections_loss,
                  kind: "loss" as const, pct: wf.collections_loss_pct_of_gpr,
                },
              ]),
          {
            label: "Actual collected rent", value: wf.actual_collected, kind: "base",
            pct: wf.actual_collected_pct_of_gpr,
          },
        ];
        const props = "properties" in wf ? [...wf.properties] : [];
        // Rank assets by total rent leakage (loss-to-lease + vacancy + collections) so the
        // biggest under-collectors sort to the top. collections_loss = concessions + bad_debt
        // exactly, so this ranking is unaffected by the item-3 split.
        props.sort(
          (a, b) =>
            b.loss_to_lease + b.vacancy_loss + b.collections_loss -
            (a.loss_to_lease + a.vacancy_loss + a.collections_loss),
        );
        // Per-unit drill-down (item 4, optional): only present on a single property's own
        // waterfall (RentWaterfall.units), never on the portfolio's — already ranked by
        // total rent leakage server-side, biggest first.
        const unitRows = "units" in wf ? wf.units : [];
        return (
          <>
            <h3 className="section-title">Rent waterfall</h3>
            <p className="hint" style={{ marginTop: -6 }}>
              Gross potential rent (every unit at market) stepping down through loss-to-lease,
              vacancy, and collections loss to actual collected rent
              {wf.period_from && wf.period_to
                ? `, ${fmtMonth(wf.period_from)}–${fmtMonth(wf.period_to)}`
                : ""}
              . Reference data (market rent vs. lease vs. collected) — does not affect NOI.
            </p>
            <p className="hint" style={{ marginTop: -4, marginBottom: 14 }}>
              This bridge is <strong>market-basis</strong>: GPR here prices every unit at its own
              market/asking rent, not in-place contract rent — a different basis from the rent
              roll's "Economic occupancy" KPI above (which is contract-basis: actual rent ÷
              contract-rent-based GPR). The two won't match exactly by design. Vacancy loss is only
              one piece of the economic gap between GPR and actual collected — vacancy loss{" "}
              <em>and</em> collections loss (bad debt/concessions on occupied units) together make
              up the full shortfall; reading vacancy loss alone will understate it.
            </p>
            <div style={{ overflowX: "auto", marginBottom: 16 }}>
              <table className="data-table">
                <thead>
                  <tr>
                    <th scope="col" style={{ textAlign: "left" }}></th>
                    <th scope="col"></th>
                    <th scope="col">Amount</th>
                    <th scope="col">% of GPR</th>
                  </tr>
                </thead>
                <tbody>
                  {steps.map((s) => {
                    const gain = s.kind === "loss" && s.value < 0; // gain-to-lease etc.
                    const tone =
                      s.kind === "base" ? "" : gain ? "value-positive" : s.value > 0 ? "value-negative" : "";
                    return (
                      <tr key={s.label}>
                        <th scope="row" style={{ textAlign: "left", fontWeight: s.kind === "base" ? 700 : 500 }}>
                          {s.kind === "loss" ? "− " : s.kind === "base" && s.label.startsWith("Actual") ? "= " : ""}
                          {s.label}
                        </th>
                        <td style={{ width: "45%" }}>
                          <div
                            aria-hidden="true"
                            style={{
                              height: 14,
                              borderRadius: 3,
                              width: `${Math.min(100, (Math.abs(s.value) / scale) * 100)}%`,
                              background:
                                s.kind === "base"
                                  ? "var(--accent)"
                                  : gain
                                    ? "var(--positive)"
                                    : "var(--negative)",
                              opacity: s.kind === "base" ? 0.85 : 0.55,
                            }}
                          />
                        </td>
                        <td className={tone} style={{ fontWeight: s.kind === "base" ? 700 : 500 }}>
                          {s.kind === "loss" ? (gain ? "+" : s.value > 0 ? "−" : "") : ""}
                          {fmtCurrency(Math.abs(s.value))}
                        </td>
                        <td className={tone} style={{ fontWeight: s.kind === "base" ? 700 : 500 }}>
                          {s.pct == null ? (
                            "—"
                          ) : (
                            <>
                              {s.kind === "loss" ? (gain ? "+" : s.value > 0 ? "−" : "") : ""}
                              {fmtPct(Math.abs(s.pct))}
                              {s.label.startsWith("Actual") && (
                                <div className="muted" style={{ fontSize: 10.5, fontWeight: 400 }}>
                                  market-basis economic occupancy
                                </div>
                              )}
                            </>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {props.length > 0 && (
              <div className="card" style={{ marginBottom: 16 }}>
                <div className="section-title" style={{ fontSize: 14, marginBottom: 8 }}>
                  By property (ranked by total rent leakage)
                </div>
                <div style={{ overflowX: "auto" }}>
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th scope="col" style={{ textAlign: "left" }}>Property</th>
                        <th scope="col">GPR</th>
                        <th scope="col">Loss to lease</th>
                        <th scope="col">{`${t("Vacancy")} loss`}</th>
                        {hasConcessions() ? (
                          <>
                            <th scope="col">Concessions</th>
                            <th scope="col">Bad debt</th>
                          </>
                        ) : (
                          <th scope="col">Arrears</th>
                        )}
                        <th scope="col">Actual collected</th>
                      </tr>
                    </thead>
                    <tbody>
                      {props.map((p) => (
                        <tr key={p.property_id}>
                          <td style={{ textAlign: "left" }}>{p.property_name}</td>
                          <td>{fmtCurrency(p.gpr)}</td>
                          <td className={p.loss_to_lease > 0 ? "value-negative" : p.loss_to_lease < 0 ? "value-positive" : undefined}>
                            {fmtCurrency(p.loss_to_lease)}
                          </td>
                          <td className={p.vacancy_loss > 0 ? "value-negative" : undefined}>{fmtCurrency(p.vacancy_loss)}</td>
                          {hasConcessions() ? (
                            <>
                              <td className={p.concessions > 0 ? "value-negative" : undefined}>{fmtCurrency(p.concessions)}</td>
                              <td className={p.bad_debt > 0 ? "value-negative" : p.bad_debt < 0 ? "value-positive" : undefined}>
                                {fmtCurrency(p.bad_debt)}
                              </td>
                            </>
                          ) : (
                            <td className={p.collections_loss > 0 ? "value-negative" : p.collections_loss < 0 ? "value-positive" : undefined}>
                              {fmtCurrency(p.collections_loss)}
                            </td>
                          )}
                          <td style={{ fontWeight: 600 }}>{fmtCurrency(p.actual_collected)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {unitRows.length > 0 && (
              <div className="card" style={{ marginBottom: 16 }}>
                <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
                  <div className="section-title" style={{ fontSize: 14, margin: 0 }}>
                    Per-unit breakdown ({unitRows.length} unit{unitRows.length === 1 ? "" : "s"}, ranked
                    by total rent leakage)
                  </div>
                  <button
                    type="button"
                    className="btn btn-ghost"
                    onClick={() => setShowUnitBreakdown((v) => !v)}
                    aria-expanded={showUnitBreakdown}
                    aria-controls="rentroll-unit-waterfall-breakdown"
                    aria-label={showUnitBreakdown ? "Hide per-unit breakdown" : "Show per-unit breakdown"}
                  >
                    {showUnitBreakdown ? "Hide" : "Show"}
                  </button>
                </div>
                {showUnitBreakdown && (
                  <div id="rentroll-unit-waterfall-breakdown" style={{ overflowX: "auto", marginTop: 8 }}>
                    <table className="data-table">
                      <thead>
                        <tr>
                          <th scope="col" style={{ textAlign: "left" }}>Unit</th>
                          <th scope="col">GPR</th>
                          <th scope="col">Loss to lease</th>
                          <th scope="col">{`${t("Vacancy")} loss`}</th>
                          {hasConcessions() ? (
                            <>
                              <th scope="col">Concessions</th>
                              <th scope="col">Bad debt</th>
                            </>
                          ) : (
                            <th scope="col">Arrears</th>
                          )}
                          <th scope="col">Actual collected</th>
                        </tr>
                      </thead>
                      <tbody>
                        {unitRows.map((u) => (
                          <tr key={u.unit_id}>
                            <td style={{ textAlign: "left" }}>
                              {u.unit_number}
                              {u.label ? ` — ${u.label}` : ""}
                            </td>
                            <td>{fmtCurrency(u.gpr)}</td>
                            <td className={u.loss_to_lease > 0 ? "value-negative" : u.loss_to_lease < 0 ? "value-positive" : undefined}>
                              {fmtCurrency(u.loss_to_lease)}
                            </td>
                            <td className={u.vacancy_loss > 0 ? "value-negative" : undefined}>
                              {fmtCurrency(u.vacancy_loss)}
                            </td>
                            {hasConcessions() ? (
                              <>
                                <td className={u.concessions > 0 ? "value-negative" : undefined}>
                                  {fmtCurrency(u.concessions)}
                                </td>
                                <td className={u.bad_debt > 0 ? "value-negative" : u.bad_debt < 0 ? "value-positive" : undefined}>
                                  {fmtCurrency(u.bad_debt)}
                                </td>
                              </>
                            ) : (
                              <td className={u.collections_loss > 0 ? "value-negative" : u.collections_loss < 0 ? "value-positive" : undefined}>
                                {fmtCurrency(u.collections_loss)}
                              </td>
                            )}
                            <td style={{ fontWeight: 600 }}>{fmtCurrency(u.actual_collected)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )}
          </>
        );
      })()}

      <h3 className="section-title">{`${t("Leases")} ending soon`}</h3>
      <div className="stack" style={{ maxWidth: 220, marginBottom: 10 }}>
        <label htmlFor="rentroll-horizon">Rollover horizon</label>
        <select
          id="rentroll-horizon"
          className="select"
          value={horizonMonths}
          onChange={(e) => setHorizonMonths(Number(e.target.value))}
        >
          {EXPIRY_HORIZONS.map((h) => (
            <option key={h.months} value={h.months}>
              Next {h.label}
            </option>
          ))}
        </select>
      </div>
      <p className="hint">
        {`Active/notice ${t("leases")} whose fixed term ends within the selected horizon, soonest first. `}
        {isUK()
          ? "Periodic (rolling) tenancies have no end date, so they never appear here — for those, the " +
            "rent-review signal is “months since last increase” on the rent roll below."
          : "Month-to-month units (no fixed end date) never appear here."}
      </p>
      {expirations && (
        <p className="hint" style={{ fontWeight: 550 }}>
          {expirationItems.length} {expirationItems.length === 1 ? t("lease") : t("leases")}{" "}
          {isUK() ? "totalling" : "totaling"}{" "}
          {fmtCurrency(
            propertyId
              ? expirationItems.reduce((sum, i) => sum + i.contract_rent, 0)
              : expirations.total_contract_rent_expiring,
          )}{" "}
          in contract rent
          {!propertyId && expirations.pct_of_portfolio_rent != null && (
            <> — {fmtPct(expirations.pct_of_portfolio_rent)} of in-place portfolio rent</>
          )}
          .
        </p>
      )}
      {!expirations ? (
        <p className="hint">Loading…</p>
      ) : expirationItems.length === 0 ? (
        <p className="hint">No leases expiring in this window.</p>
      ) : (
        <div style={{ overflowX: "auto", marginBottom: 24 }}>
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Property</th>
                <th scope="col" style={leftStyle(true)}>Unit</th>
                <th scope="col" style={leftStyle(true)}>Tenant</th>
                <th scope="col" style={leftStyle(true)}>Lease end</th>
                <th scope="col">Months to expiry</th>
                <th scope="col">Contract rent</th>
              </tr>
            </thead>
            <tbody>
              {expirationItems.map((i) => (
                <tr key={i.unit_id}>
                  <td>{i.property_name}</td>
                  <td style={leftStyle(true)}>{i.unit_number}{i.label ? ` — ${i.label}` : ""}</td>
                  <td style={leftStyle(true)}>{i.tenant_name || "—"}</td>
                  <td style={leftStyle(true)}>{fmtDate(i.lease_end)}</td>
                  <td className={i.months_to_expiry <= 1 ? "value-negative" : undefined}>
                    {fmtMonthsToExpiry(i.months_to_expiry)}
                  </td>
                  <td>{fmtCurrency(i.contract_rent)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h3 className="section-title">Rent roll</h3>
      <p className="hint">
        {`One row per unit. Status is the ${t("lease")}'s own state — “${t("vacant")}” means no `}
        {`${t("lease")} is on file at all. A ${t("vacant")} row shows no tenant/months-to-expiry (there's no `}
        current tenancy to report) but still shows its contract rent, labelled “asking” — that potential rent
        is what feeds economic occupancy above. “Missing data” (shown under actual rent) flags an occupied
        {` unit with no recorded rent for the property's latest posted month, kept distinct from a real `}
        {`${t("vacancy")}.`}
        {isAdmin
          ? ` Use “Add ${t("lease")}” / “Edit” on a unit row to record a tenancy.`
          : ` Read-only (admin required to edit a ${t("lease")}).`}
      </p>
      {data && propertyGroups.length > 1 && (
        <div style={{ margin: "0 0 8px" }}>
          <button type="button" className="btn btn-ghost" onClick={() => setAllOpen(!allOpen)}>
            {allOpen ? "▾ Collapse all properties" : "▸ Expand all properties"}
          </button>
        </div>
      )}
      {!data ? (
        <p className="hint">Loading…</p>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table className="data-table">
            <thead>
              <tr>
                {COLUMNS.map((c) => (
                  <th
                    key={c.key}
                    scope="col"
                    style={leftStyle(!!LEFT_ALIGN[c.key])}
                    aria-sort={sort.key === c.key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}
                  >
                    <button
                      type="button"
                      className="btn btn-ghost"
                      style={{ padding: "2px 4px", fontWeight: 650, fontSize: "inherit" }}
                      onClick={() => onSort(c.key)}
                    >
                      {c.label}
                      {sort.key === c.key && (
                        <span aria-hidden="true">{sort.dir === 1 ? " ▲" : " ▼"}</span>
                      )}
                    </button>
                  </th>
                ))}
                <th scope="col">Details</th>
                {isAdmin && <th scope="col">Edit</th>}
              </tr>
            </thead>
            <tbody>
              {propertyGroups.map((g) => {
                // A single group (i.e. a property filter is applied) opens by default —
                // there's nothing to drill past.
                const isOpen = openProperties[g.property_id] ?? autoExpand;
                const sum = (pick: (r: RentRollRow) => number | null | undefined) =>
                  g.rows.reduce((t, r) => t + (pick(r) ?? 0), 0);
                const vacant = g.rows.filter((r) => r.status === "vacant").length;
                const varianceTotal = sum((r) => r.variance);
                const arrearsTotal = sum((r) => r.arrears_balance);
                return (
                  <Fragment key={g.property_id}>
                    <tr
                      {...clickableProps(() =>
                        setOpenProperties((o) => ({ ...o, [g.property_id]: !isOpen })),
                      )}
                      className={`row-strong is-clickable${isOpen ? " row-open" : ""}`}
                    >
                      <td>
                        <span className="caret" aria-hidden="true">{isOpen ? "▾" : "▸"}</span>
                        {g.property_name}
                      </td>
                      <td style={leftStyle(true)}>
                        {g.rows.length} unit{g.rows.length === 1 ? "" : "s"}
                      </td>
                      <td style={leftStyle(true)}>—</td>
                      <td>{fmtCurrency(sum((r) => r.contract_rent))}</td>
                      <td>{fmtCurrency(sum((r) => r.actual_rent))}</td>
                      <td className={varianceTotal === 0 ? undefined : varianceTotal > 0 ? "value-positive" : "value-negative"}>
                        {varianceTotal > 0 ? "+" : ""}
                        {fmtCurrency(varianceTotal)}
                      </td>
                      {/* Owed is "bad" and in credit is "good", so the tone is inverted
                          relative to the variance column beside it. */}
                      <td className={arrearsTotal === 0 ? undefined : arrearsTotal > 0 ? "value-negative" : "value-positive"}>
                        {fmtCurrency(arrearsTotal)}
                      </td>
                      <td style={leftStyle(true)}>—</td>
                      <td>—</td>
                      <td>{vacant === 0 ? "all occupied" : `${vacant} vacant`}</td>
                      <td>—</td>
                      {isAdmin && <td>—</td>}
                    </tr>
                    {isOpen &&
                      g.rows.map((row) => (
                        <RentRollRowView
                          key={row.unit_id}
                          row={row}
                          grouped
                          isAdmin={isAdmin}
                          isEditing={editingUnitId === row.unit_id}
                          form={form}
                          setForm={setForm}
                          onStartEdit={() => startEdit(row)}
                          onCancel={() => setEditingUnitId(null)}
                          onSave={() => saveLease(row)}
                          onMarketRentSaved={() => reload()}
                          token={token}
                          periodFrom={data?.period_from ?? null}
                          periodTo={data?.period_to ?? null}
                        />
                      ))}
                  </Fragment>
                );
              })}
              {sortedRows.length === 0 && (
                <tr className="row-empty">
                  <td colSpan={isAdmin ? 12 : 11}>No units found for this filter.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function RentRollRowView({
  row,
  grouped,
  isAdmin,
  isEditing,
  form,
  setForm,
  onStartEdit,
  onCancel,
  onSave,
  onMarketRentSaved,
  token,
  periodFrom,
  periodTo,
}: {
  row: RentRollRow;
  // Rendered underneath its property's row in the drill-down: the property name would just
  // repeat on every unit, so the first cell is left blank and the row is indented instead.
  grouped?: boolean;
  isAdmin: boolean;
  isEditing: boolean;
  form: LeaseInput;
  setForm: (f: LeaseInput) => void;
  onStartEdit: () => void;
  onCancel: () => void;
  onSave: () => void;
  onMarketRentSaved: () => void;
  token: string;
  periodFrom: string | null;
  periodTo: string | null;
}) {
  const [showDetail, setShowDetail] = useState(false);
  const detailRowId = `lease-detail-${row.unit_id}`;
  return (
    <>
      <tr
        className={
          [grouped ? "row-sub" : "", row.status === "vacant" ? "row-muted" : ""]
            .filter(Boolean)
            .join(" ") || undefined
        }
      >
        <td>{grouped ? "" : row.property_name}</td>
        <td style={leftStyle(true)}>{row.unit_number}{row.label ? ` — ${row.label}` : ""}</td>
        {/* The "Edit"/"Add lease" control lives in the last of twelve columns, which on any
            normal window is off the right edge behind a horizontal scroll — so a unit with no
            tenancy on file offers the action HERE, in the cell whose emptiness is the thing
            prompting the question. Same handler as the far-right button. */}
        <td style={leftStyle(true)}>
          {row.lease_id ? (
            row.status === "vacant" ? "—" : row.tenant_name || "—"
          ) : isAdmin ? (
            <button
              type="button"
              className="btn btn-ghost"
              style={{ padding: "2px 6px" }}
              onClick={onStartEdit}
              disabled={isEditing}
              title="Record the tenancy for this unit — tenant, start date and rent"
            >
              {`+ Add ${t("lease")}`}
            </button>
          ) : (
            <span className="muted">{`no ${t("lease")} on file`}</span>
          )}
        </td>
        <td>
          {row.contract_rent == null ? "—" : fmtCurrency(row.contract_rent)}
          {row.status === "vacant" && row.contract_rent != null && (
            <div className="muted" style={{ fontSize: 11 }}>asking</div>
          )}
        </td>
        <td>
          {row.actual_rent == null ? "—" : fmtCurrency(row.actual_rent)}
          {row.missing_data && (
            <div className="muted" style={{ fontSize: 11 }}>
              missing data{row.actual_month ? ` (last: ${fmtDate(row.actual_month)})` : ""}
            </div>
          )}
        </td>
        <td className={row.variance == null || row.variance === 0 ? undefined : row.variance > 0 ? "value-positive" : "value-negative"}>
          {row.variance == null || row.expected_rent == null || row.expected_rent === 0 ? (
            "—"
          ) : (
            <>
              {row.variance > 0 ? "+" : ""}
              {fmtCurrency(row.variance)}
              <div className="muted" style={{ fontSize: 11 }}>
                {row.variance > 0 ? "over" : row.variance < 0 ? "under" : "on"} expected
                {row.variance_pct != null && ` (${row.variance_pct > 0 ? "+" : ""}${row.variance_pct.toFixed(1)}%)`}
              </div>
            </>
          )}
        </td>
        {/* Arrears: the CUMULATIVE balance (everything this tenancy owes as at the window's
            end), with the window's own MOVEMENT underneath — the monthly figure and the
            accumulated one, which is what makes the row auditable. Negative = in credit. */}
        <td className={!row.arrears_balance ? undefined : row.arrears_balance > 0 ? "value-negative" : "value-positive"}>
          {row.arrears_balance == null ? (
            "—"
          ) : row.arrears_balance === 0 && !row.arrears_movement ? (
            <span className="muted">—</span>
          ) : (
            <>
              {row.arrears_balance < 0
                ? `${fmtCurrency(-row.arrears_balance)} cr`
                : fmtCurrency(row.arrears_balance)}
              <div className="muted" style={{ fontSize: 11 }}>
                {row.arrears_months_of_rent != null && row.arrears_balance > 0
                  ? `${row.arrears_months_of_rent.toFixed(1)} mo rent`
                  : row.arrears_balance < 0
                    ? "in credit"
                    : "cleared"}
                {!!row.arrears_movement &&
                  ` · ${row.arrears_movement > 0 ? "+" : ""}${fmtCurrency(row.arrears_movement)} this period`}
                {row.arrears_from_month && ` · from ${fmtMonth(row.arrears_from_month)}`}
              </div>
            </>
          )}
        </td>
        <td style={leftStyle(true)}>
          {row.status === "vacant"
            ? row.lease_end
              ? `${t("vacant")} since ${fmtDate(row.lease_end)}${row.vacant_days != null ? ` (${row.vacant_days}d)` : ""}`
              : t("vacant")
            : row.lease_end
              ? fmtDate(row.lease_end)
              : "periodic (rolling)"}
        </td>
        <td>{row.status === "vacant" ? "—" : fmtMonthsToExpiry(row.months_to_expiry)}</td>
        <td>
          <span className={leaseStatusPillClass(row.status)}>{row.status}</span>
          {row.holdover && (
            <span className="pill pill--holdover" style={{ marginLeft: 5 }}>
              holdover
            </span>
          )}
        </td>
        <td>
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => setShowDetail((v) => !v)}
            aria-expanded={showDetail}
            aria-controls={detailRowId}
            aria-label={`Lease details for unit ${row.unit_number}`}
          >
            {showDetail ? "Hide" : "Details"}
          </button>
        </td>
        {isAdmin && (
          <td>
            <button type="button" className="btn btn-ghost" onClick={onStartEdit} disabled={isEditing}>
              {row.lease_id ? "Edit" : `Add ${t("lease")}`}
            </button>
          </td>
        )}
      </tr>
      {showDetail && (
        <LeaseDetailRow
          row={row}
          colSpan={isAdmin ? 12 : 11}
          token={token}
          rowId={detailRowId}
          periodFrom={periodFrom}
          periodTo={periodTo}
          isAdmin={isAdmin}
          onMarketRentSaved={onMarketRentSaved}
        />
      )}
      {isEditing && (
        <tr>
          <td colSpan={isAdmin ? 12 : 11}>
            <form
              className="card"
              style={{ display: "flex", gap: 10, alignItems: "flex-end", flexWrap: "wrap", margin: 0 }}
              onSubmit={(e) => {
                e.preventDefault();
                onSave();
              }}
            >
              <label className="stack">
                Tenant name <span className="muted" style={{ fontWeight: 400 }}>(optional)</span>
                <input
                  className="input"
                  placeholder="leave blank if not recorded"
                  value={form.tenant_name ?? ""}
                  onChange={(e) => setForm({ ...form, tenant_name: e.target.value })}
                />
              </label>
              <label className="stack">
                Start date
                <input
                  className="input"
                  type="date"
                  value={form.start_date}
                  onChange={(e) => setForm({ ...form, start_date: e.target.value })}
                  required
                />
              </label>
              <label className="stack">
                End date
                <input
                  className="input"
                  type="date"
                  value={form.end_date ?? ""}
                  onChange={(e) => setForm({ ...form, end_date: e.target.value || null })}
                />
              </label>
              <label className="stack">
                Contract rent
                <input
                  className="input"
                  type="number"
                  step="0.01"
                  min="0"
                  value={form.contract_rent}
                  onChange={(e) => setForm({ ...form, contract_rent: Number(e.target.value) })}
                  required
                />
              </label>
              <label className="stack">
                Status
                <select
                  className="select"
                  value={form.status}
                  onChange={(e) => setForm({ ...form, status: e.target.value as LeaseStatus })}
                >
                  {LEASE_STATUSES.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </label>
              <p className="hint" style={{ margin: "0 0 -4px", flexBasis: "100%" }}>
                {isUK()
                  ? "Leave the end date blank for a periodic (rolling) tenancy — a fixed term that has " +
                    "run its course becomes one, and most ASTs are on one. Fill it in only while a " +
                    "fixed term is still running."
                  : "Lease type (fixed/month-to-month) follows the end date above automatically — clear " +
                    "it for a month-to-month lease."}
              </p>
              <label className="stack">
                {t("securityDeposit")}
                <input
                  className="input"
                  type="number"
                  step="0.01"
                  min="0"
                  value={form.security_deposit ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, security_deposit: e.target.value === "" ? null : Number(e.target.value) })
                  }
                />
              </label>
              {/* Not a UK letting concept — hidden rather than relabelled for a £ account (see
                  terms.ts). The stored value is preserved across saves either way, so hiding the
                  input can't erase one that a US account set. */}
              <label className="stack" style={{ display: hasConcessions() ? undefined : "none" }}>
                {`${t("Concession")} (${currencySymbol()}/mo)`}
                <input
                  className="input"
                  type="number"
                  step="0.01"
                  min="0"
                  value={form.concession_monthly ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, concession_monthly: e.target.value === "" ? null : Number(e.target.value) })
                  }
                />
              </label>
              <label className="stack">
                Escalation %/yr
                <input
                  className="input"
                  type="number"
                  step="0.1"
                  min="0"
                  max="100"
                  value={form.escalation_pct ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, escalation_pct: e.target.value === "" ? null : Number(e.target.value) })
                  }
                />
              </label>
              <label className="stack">
                Escalation cadence (mo)
                <input
                  className="input"
                  type="number"
                  step="1"
                  min="1"
                  value={form.escalation_frequency_months ?? 12}
                  onChange={(e) => setForm({ ...form, escalation_frequency_months: Number(e.target.value) })}
                />
              </label>
              {/* Periodic (rolling) tenancy rent history — migration 0024. On an England-style
                  tenancy the rent rises in discrete steps on a stated date rather than by a
                  contractual percentage, so these two fields ARE the rent schedule.
                  `rent_before_increase` matters because arrears is cumulative: without it,
                  pricing pre-increase months at today's higher rent invents historical debt
                  for a tenant who paid in full every month. */}
              <label className="stack">
                Rent last increased
                <input
                  className="input"
                  type="date"
                  value={form.last_rent_increase_date ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, last_rent_increase_date: e.target.value || null })
                  }
                />
              </label>
              <label className="stack">
                Rent before increase
                <input
                  className="input"
                  type="number"
                  step="1"
                  min="0"
                  // Meaningless without a date to attach it to (the server rejects that
                  // pairing), so the field stays disabled until one is set.
                  disabled={!form.last_rent_increase_date}
                  value={form.rent_before_increase ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, rent_before_increase: e.target.value === "" ? null : Number(e.target.value) })
                  }
                />
              </label>
              {/* The handover-month escape hatch (migration 0027). A purchase apportions the
                  rent in hand between seller and buyer, so the first month's collection is a
                  part period — indistinguishable from a tenant who underpaid unless the
                  measurement simply starts later. */}
              <label className="stack">
                Track arrears from
                <input
                  className="input"
                  type="month"
                  value={form.arrears_from_month ? form.arrears_from_month.slice(0, 7) : ""}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      arrears_from_month: e.target.value ? `${e.target.value}-01` : null,
                    })
                  }
                  title="Months before this are outside the arrears measurement — use it to exclude a handover month. Blank = measure from the start."
                />
              </label>
              <label className="stack">
                Opening arrears
                <input
                  className="input"
                  type="number"
                  step="1"
                  // Signed: a tenancy can begin in credit (rent paid up front), so no min.
                  value={form.opening_arrears ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, opening_arrears: e.target.value === "" ? null : Number(e.target.value) })
                  }
                  title="Arrears brought forward at the tenancy's start — debt predating this app's records. Negative = in credit."
                />
              </label>
              <label className="stack">
                Pct. rent rate (retail)
                <input
                  className="input"
                  type="number"
                  step="0.1"
                  min="0"
                  max="100"
                  value={form.pct_rent_rate ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, pct_rent_rate: e.target.value === "" ? null : Number(e.target.value) })
                  }
                />
              </label>
              <label className="stack">
                Pct. rent breakpoint
                <input
                  className="input"
                  type="number"
                  step="1"
                  min="0"
                  value={form.pct_rent_breakpoint ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, pct_rent_breakpoint: e.target.value === "" ? null : Number(e.target.value) })
                  }
                />
              </label>
              <button className="btn btn-primary" type="submit">
                Save
              </button>
              <button className="btn btn-ghost" type="button" onClick={onCancel}>
                Cancel
              </button>
            </form>
          </td>
        </tr>
      )}
    </>
  );
}

// Expandable per-lease detail row (deposit / escalation / lease type / percentage rent).
// Kept OUT of the main columns so the table stays uncluttered — this data is secondary to
// the core tenant/rent/expiry/status view most users need at a glance.
function LeaseDetailRow({
  row,
  colSpan,
  token,
  rowId,
  periodFrom,
  periodTo,
  isAdmin,
  onMarketRentSaved,
}: {
  row: RentRollRow;
  colSpan: number;
  token: string;
  rowId: string;
  periodFrom: string | null;
  periodTo: string | null;
  isAdmin: boolean;
  onMarketRentSaved: () => void;
}) {
  const [salesInput, setSalesInput] = useState("");
  const [calc, setCalc] = useState<PercentageRentCalc | null>(null);
  const [calcError, setCalcError] = useState<string | null>(null);
  // Market rent (migration 0015) is a UNIT-level field, independent of any lease — editable
  // here even for a vacant unit with no lease on file at all (waterfall-followups item 1).
  const [marketRentInput, setMarketRentInput] = useState(
    row.market_rent != null ? String(row.market_rent) : "",
  );
  const [savingMarketRent, setSavingMarketRent] = useState(false);
  const [marketRentError, setMarketRentError] = useState<string | null>(null);

  const hasPctRent = row.pct_rent_rate != null && row.pct_rent_breakpoint != null;

  const runCalc = () => {
    if (!row.lease_id) return;
    setCalcError(null);
    const sales = salesInput.trim() === "" ? undefined : Number(salesInput);
    getLeasePercentageRent(token, row.lease_id, sales)
      .then(setCalc)
      .catch((e) => setCalcError(e.message));
  };

  const saveMarketRent = () => {
    setMarketRentError(null);
    const trimmed = marketRentInput.trim();
    const value = trimmed === "" ? null : Number(trimmed);
    if (value != null && (Number.isNaN(value) || value < 0)) {
      setMarketRentError("Market rent must be a non-negative number.");
      return;
    }
    setSavingMarketRent(true);
    updateUnit(token, row.unit_id, { market_rent: value })
      .then(() => {
        setSavingMarketRent(false);
        onMarketRentSaved();
      })
      .catch((e) => {
        setSavingMarketRent(false);
        setMarketRentError(e.message);
      });
  };

  const marketRentSection = (
    <div className="stack">
      <span className="muted" style={{ fontSize: 12 }}>
        Market rent
      </span>
      {isAdmin ? (
        <div className="row" style={{ gap: 6, alignItems: "center" }}>
          <input
            className="input"
            type="number"
            step="0.01"
            min="0"
            style={{ width: 110 }}
            aria-label={`Market rent for unit ${row.unit_number}`}
            value={marketRentInput}
            onChange={(e) => setMarketRentInput(e.target.value)}
          />
          <button
            type="button"
            className="btn btn-ghost"
            onClick={saveMarketRent}
            disabled={savingMarketRent}
          >
            {savingMarketRent ? "Saving…" : "Save"}
          </button>
        </div>
      ) : (
        <strong>{row.market_rent == null ? "—" : fmtCurrency(row.market_rent)}</strong>
      )}
      {marketRentError && (
        <span className="alert-error" style={{ fontSize: 11 }}>
          {marketRentError}
        </span>
      )}
      <span className="hint" style={{ margin: 0, maxWidth: 220 }}>
        Asking/market rent — the rent waterfall's GPR reference figure for this unit,
        independent of its lease. Load a real comp/appraisal figure here.
      </span>
    </div>
  );

  if (!row.lease_id) {
    return (
      <tr id={rowId}>
        <td colSpan={colSpan}>
          <div className="card" style={{ display: "flex", gap: 24, flexWrap: "wrap", margin: 0 }}>
            {marketRentSection}
            <p className="hint" style={{ margin: 0 }}>
              No lease on file for this unit — nothing else to show.
            </p>
          </div>
        </td>
      </tr>
    );
  }

  const periodLabel =
    periodFrom && periodTo
      ? periodFrom === periodTo
        ? fmtMonth(periodFrom)
        : `${fmtMonth(periodFrom)} – ${fmtMonth(periodTo)}`
      : null;

  return (
    <tr id={rowId}>
      <td colSpan={colSpan}>
        <div className="card" style={{ display: "flex", gap: 24, flexWrap: "wrap", margin: 0 }}>
          {marketRentSection}
          <div className="stack">
            <span className="muted" style={{ fontSize: 12 }}>{t("securityDeposit")}</span>
            <strong>{row.security_deposit == null ? "—" : fmtCurrency(row.security_deposit)}</strong>
          </div>
          {hasConcessions() && (
            <div className="stack">
              <span className="muted" style={{ fontSize: 12 }}>{t("Concession")}</span>
              <strong>
                {row.concession_monthly == null ? "—" : `${fmtCurrency(row.concession_monthly)}/mo`}
              </strong>
            </div>
          )}
          <div className="stack">
            <span className="muted" style={{ fontSize: 12 }}>{`${t("Lease")} type`}</span>
            <strong>
              {row.lease_type === "mtm"
                ? isUK()
                  ? "Periodic (rolling)"
                  : "Month-to-month"
                : "Fixed term"}
            </strong>
          </div>
          {/* The rent HISTORY of a periodic tenancy: when the rent last moved, what it was
              before, and how long it has been. On a rolling UK tenancy this is the rent
              schedule — a rise is served by notice on a date, not accrued by a contractual
              percentage — and "months since" is the figure that says a review is overdue. */}
          <div className="stack">
            <span className="muted" style={{ fontSize: 12 }}>Rent last increased</span>
            <strong>
              {row.last_rent_increase_date ? fmtDate(row.last_rent_increase_date) : "never"}
            </strong>
            {row.months_since_last_increase != null && (
              <span className="hint" style={{ margin: 0 }}>
                {`${row.months_since_last_increase} mo ago`}
                {row.last_rent_increase_date ? "" : ` (since ${t("lease")} start)`}
                {row.rent_before_increase != null && `, was ${fmtCurrency(row.rent_before_increase)}`}
              </span>
            )}
          </div>
          {/* A contractual escalator is a US fixed-term device; only show it where one is
              actually recorded, so a UK tenancy's panel isn't cluttered with an empty field
              for a mechanism it doesn't use. */}
          {row.escalation_pct != null && (
            <div className="stack">
              <span className="muted" style={{ fontSize: 12 }}>{t("Escalation")}</span>
              <strong>
                {`${row.escalation_pct}% every ${row.escalation_frequency_months ?? 12} mo`}
              </strong>
              <span className="hint" style={{ margin: 0 }}>
                next: {row.next_escalation_date ? fmtDate(row.next_escalation_date) : "—"}
              </span>
            </div>
          )}
          <div className="stack">
            <span className="muted" style={{ fontSize: 12 }}>
              Rent variance{periodLabel ? ` (${periodLabel})` : ""}
            </span>
            {row.expected_rent == null ? (
              <strong>—</strong>
            ) : (
              <>
                <span style={{ fontSize: 12.5 }}>
                  Expected <strong>{fmtCurrency(row.expected_rent)}</strong> vs. actual{" "}
                  <strong>{fmtCurrency(row.period_actual_rent ?? 0)}</strong>
                </span>
                <span
                  className={row.variance == null || row.variance === 0 ? undefined : row.variance > 0 ? "value-positive" : "value-negative"}
                  style={{ fontWeight: 600 }}
                >
                  {row.variance != null && row.variance > 0 ? "+" : ""}
                  {row.variance == null ? "—" : fmtCurrency(row.variance)}
                  {row.variance_pct != null &&
                    ` (${row.variance_pct > 0 ? "+" : ""}${row.variance_pct.toFixed(1)}%)`}
                  {row.variance != null &&
                    ` ${row.variance > 0 ? "over" : row.variance < 0 ? "under" : "on"} expected`}
                </span>
              </>
            )}
          </div>
          {hasPctRent && (
            <div className="stack">
              <span className="muted" style={{ fontSize: 12 }}>Percentage rent (retail)</span>
              <strong>
                {row.pct_rent_rate}% over {fmtCurrency(row.pct_rent_breakpoint as number)}/yr sales
              </strong>
              <div className="row" style={{ gap: 8, alignItems: "flex-end", marginTop: 6 }}>
                <label className="stack">
                  <span style={{ fontSize: 12 }} id={`sales-label-${row.unit_id}`}>Annual sales</span>
                  <input
                    className="input"
                    type="number"
                    min="0"
                    step="1000"
                    aria-labelledby={`sales-label-${row.unit_id}`}
                    value={salesInput}
                    onChange={(e) => {
                      // Clear a stale result/error from a prior input as soon as the sales
                      // figure changes, so a leftover "Overage rent: …" line never lingers
                      // against a different, not-yet-computed input (fix #4).
                      setSalesInput(e.target.value);
                      setCalc(null);
                      setCalcError(null);
                    }}
                    style={{ width: 140 }}
                  />
                </label>
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={runCalc}
                  disabled={salesInput.trim() === ""}
                  title={salesInput.trim() === "" ? "Enter an annual sales figure first" : undefined}
                >
                  Compute overage
                </button>
              </div>
              {calcError && <p className="alert-error" style={{ margin: 0 }}>{calcError}</p>}
              {calc && calc.annual_sales != null && (
                <p className="hint" style={{ margin: 0, fontWeight: 550 }}>
                  Overage rent: {fmtCurrency(calc.overage_rent ?? 0)}/yr
                  {calc.overage_rent === 0 && " (sales at or below breakpoint — terms only)"}
                </p>
              )}
            </div>
          )}
        </div>
      </td>
    </tr>
  );
}
