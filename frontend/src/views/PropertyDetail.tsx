import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import {
  exportPropertyMonthly,
  exportPropertyVariance,
  getPropertyAttention,
  getPropertyCategories,
  getPropertyDashboard,
  getPropertyMonthly,
  getPropertyUnitsMonthly,
  getPropertyVariance,
  getUnitRoster,
  listProperties,
  type AttentionFeed as AttentionFeedType,
  type CategoryAmount,
  type ExportFormat,
  type PeriodRange,
  type PnLMetrics,
  type Property,
  type PropertyDashboard,
  type PropertyMonthlyPnL,
  type PropertyVariance,
  type UnitMonthlyPnL,
  type UnitRoster,
} from "../api";
import AttentionFeed from "../components/AttentionFeed";
import CategoryComposition from "../components/CategoryComposition";
import ExportControl from "../components/ExportControl";
import InvestmentPanel from "../components/InvestmentPanel";
import RentModelPanel from "../components/RentModelPanel";
import KpiBand from "../components/KpiBand";
import PeriodSelector from "../components/PeriodSelector";
import PnlTrendChart from "../components/PnlTrendChart";
import PropertySearchSelect from "../components/PropertySearchSelect";
import TenancyEditor from "../components/TenancyEditor";
import UnitDetail from "../components/UnitDetail";
import { clickableProps } from "../hooks/clickable";
import { t } from "../terms";
import { fmtCurrency, fmtDate, fmtMonth, isYoyComparison, leaseStatusPillClass, statusPillClass } from "../ui";

const ROSTER_PAGE = 25;

// Level 2 — Property detail. Top-down: a scoped KPI band + property-scoped attention feed
// + a server-paginated/sortable unit roster (all from the rollups), then the month-by-month
// P&L with the honest unit-vs-property-tier split.
export default function PropertyDetail({
  token,
  initialPropertyId,
  onConsumeInitial,
}: {
  token: string;
  // Set by a cross-tab nudge (e.g. Investments' "missing acquisition data" prompt) to land
  // here pre-selected to a specific property instead of the default first-in-list.
  initialPropertyId?: string | null;
  onConsumeInitial?: () => void;
}) {
  const [properties, setProperties] = useState<Property[]>([]);
  const [propertyId, setPropertyId] = useState("");
  const [allMonths, setAllMonths] = useState<string[]>([]);
  const [dashboard, setDashboard] = useState<PropertyDashboard | null>(null);
  const [variance, setVariance] = useState<PropertyVariance | null>(null);
  const [feed, setFeed] = useState<AttentionFeedType | null>(null);
  const [roster, setRoster] = useState<UnitRoster | null>(null);
  const [sort, setSort] = useState("unit_number");
  const [order, setOrder] = useState<"asc" | "desc">("asc");
  const [offset, setOffset] = useState(0);
  // One period selector drives the whole page (KPI band over the period; feed + roster at
  // its end month; the monthly P&L over the range).
  const [range, setRange] = useState<PeriodRange>({});
  const [monthly, setMonthly] = useState<PropertyMonthlyPnL[]>([]);
  // What the headline figures are made of, category by category, over the selected period.
  const [categories, setCategories] = useState<CategoryAmount[]>([]);
  const [unitRows, setUnitRows] = useState<UnitMonthlyPnL[]>([]);
  const [openMonth, setOpenMonth] = useState<string | null>(null);
  const [selectedUnit, setSelectedUnit] = useState<string | null>(null);
  // Which roster row has its tenancy editor open (unit id), and a bump to force the roster to
  // refetch after a save so the row shows the figure that was just stored rather than a stale
  // client copy — the server is the only source of truth for these.
  const [editingUnit, setEditingUnit] = useState<string | null>(null);
  const [rosterReload, setRosterReload] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const unitPanelRef = useRef<HTMLDivElement>(null);

  const property = properties.find((p) => p.id === propertyId);
  const isMulti = property?.type === "multifamily";

  const openUnit = (unitId: string) => {
    setSelectedUnit(unitId);
    requestAnimationFrame(() => unitPanelRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
  };

  useEffect(() => {
    let cancelled = false;
    listProperties(token)
      .then((ps) => {
        if (cancelled) return;
        setProperties(ps);
        setPropertyId((cur) => cur || initialPropertyId || (ps[0]?.id ?? ""));
        if (initialPropertyId) onConsumeInitial?.();
      })
      .catch((e) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  // A nudge (Investments' "missing acquisition data" prompt) can set initialPropertyId
  // after properties are already loaded — jump to it and consume the signal once.
  useEffect(() => {
    if (initialPropertyId) {
      setPropertyId(initialPropertyId);
      onConsumeInitial?.();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialPropertyId]);

  // Discover the property's months; reset per-property view state.
  useEffect(() => {
    if (!propertyId) return;
    let cancelled = false;
    setOffset(0);
    setSelectedUnit(null);
    getPropertyMonthly(token, propertyId)
      .then((r) => !cancelled && setAllMonths(r.map((x) => x.month)))
      .catch((e) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [token, propertyId]);

  // Reset the roster page when the anchor month changes.
  useEffect(() => setOffset(0), [range.to]);

  // Scoped KPI band (over the period) + property-scoped feed (at the period's end month).
  // `cancelled` guards against a slow, superseded request overwriting fresher state if the
  // user switches property/period again before this one resolves.
  useEffect(() => {
    if (!propertyId) return;
    let cancelled = false;
    getPropertyDashboard(token, propertyId, range).then((d) => !cancelled && setDashboard(d)).catch((e) => !cancelled && setError(e.message));
    getPropertyVariance(token, propertyId, range).then((v) => !cancelled && setVariance(v)).catch((e) => !cancelled && setError(e.message));
    getPropertyAttention(token, propertyId, range).then((f) => !cancelled && setFeed(f)).catch((e) => !cancelled && setError(e.message));
    getPropertyCategories(token, propertyId, range).then((c) => !cancelled && setCategories(c.rows)).catch((e) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [token, propertyId, range.from, range.to]);

  // Unit roster (server-sorted/paginated) at the period's end month.
  //
  // Deliberately NOT multifamily-only any more. A single-let house has exactly one unit, and
  // that unit is where its tenancy, its rent schedule and its arrears live — so gating the
  // roster on `type === "multifamily"` left a portfolio of houses with no way to see or edit
  // any of it from this page. The roster degrades perfectly well to one row.
  useEffect(() => {
    if (!propertyId) {
      setRoster(null);
      return;
    }
    let cancelled = false;
    getUnitRoster(token, propertyId, { month: range.to, sort, order, limit: ROSTER_PAGE, offset })
      .then((r) => !cancelled && setRoster(r))
      .catch((e) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [token, propertyId, range.to, sort, order, offset, rosterReload]);

  // Lower detail: month-by-month P&L over the selected range.
  useEffect(() => {
    if (!propertyId) return;
    let cancelled = false;
    setOpenMonth(null);
    getPropertyMonthly(token, propertyId, range).then((r) => !cancelled && setMonthly(r)).catch((e) => !cancelled && setError(e.message));
    if (isMulti) {
      getPropertyUnitsMonthly(token, propertyId, range).then((r) => !cancelled && setUnitRows(r)).catch((e) => !cancelled && setError(e.message));
    } else {
      setUnitRows([]);
    }
    return () => {
      cancelled = true;
    };
  }, [token, propertyId, range.from, range.to, isMulti]);

  const unitsByMonth = useMemo(() => {
    const m: Record<string, UnitMonthlyPnL[]> = {};
    for (const r of unitRows) (m[r.month] ??= []).push(r);
    for (const k of Object.keys(m)) m[k].sort((a, b) => a.unit_number.localeCompare(b.unit_number));
    return m;
  }, [unitRows]);

  // Export always downloads the CURRENTLY-VIEWED property + period range.
  const exportReports = useMemo(
    () =>
      propertyId
        ? [
            {
              value: "monthly",
              label: "Monthly P&L",
              onExport: (format: ExportFormat) => exportPropertyMonthly(token, propertyId, range, format),
            },
            {
              value: "variance",
              label: "Variance vs budget",
              onExport: (format: ExportFormat) => exportPropertyVariance(token, propertyId, range, format),
            },
          ]
        : [],
    [token, propertyId, range],
  );

  const onSort = (col: string) => {
    setOffset(0);
    if (col === sort) setOrder((o) => (o === "asc" ? "desc" : "asc"));
    else {
      setSort(col);
      setOrder(col === "unit_number" ? "asc" : "desc");
    }
  };
  const sortArrow = (col: string) => (sort === col ? (order === "asc" ? " ▲" : " ▼") : "");

  return (
    <section>
      {error && <p className="alert-error">{error}</p>}

      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-end", marginBottom: 12, flexWrap: "wrap", gap: 10 }}>
        <PropertySearchSelect
          properties={properties}
          value={propertyId}
          onChange={setPropertyId}
          wrapperStyle={{ minWidth: 220 }}
        />
        <PeriodSelector key={propertyId} availableMonths={allMonths} onChange={setRange} defaultMode="month" />
      </div>

      <div className="row" style={{ justifyContent: "flex-end", marginBottom: 12, flexWrap: "wrap", gap: 10 }}>
        <ExportControl reports={exportReports} idPrefix="property-detail" />
      </div>

      {dashboard && (
        <p className="hint" style={{ marginTop: -4 }}>
          {dashboard.property_name} · {dashboard.type}
          {dashboard.current ? ` · ${dashboard.current.total_units || 0} units` : ""}
          {dashboard.period_from ? ` · ${periodLabel(dashboard)}` : ""}
          {dashboard.prior_from ? ` vs ${periodLabel({ period_from: dashboard.prior_from, period_to: dashboard.prior_to })}` : ""}
          {dashboard.prior_from &&
            isYoyComparison(dashboard.period_from, dashboard.period_to, dashboard.prior_from, dashboard.prior_to) && (
              <strong style={{ color: "var(--accent)", fontWeight: 650 }}> · year-over-year</strong>
            )}
        </p>
      )}

      {dashboard && <KpiBand data={dashboard} variance={variance} />}

      {/* Rent model, given the same billing as the acquisition inputs beside it. Only for a
          property with ONE unit: rent belongs to a tenancy and a tenancy to a unit, so on a
          multifamily building "the rent" is ambiguous and the per-row editor in the unit roster
          below is the honest surface. */}
      {roster?.total === 1 && roster.rows[0] && (
        <RentModelPanel
          token={token}
          unitId={roster.rows[0].unit_id}
          onSaved={() => setRosterReload((n) => n + 1)}
        />
      )}

      {propertyId && <InvestmentPanel token={token} propertyId={propertyId} />}

      <h3 className="section-title">Needs attention</h3>
      {feed && (
        <AttentionFeed
          feed={feed}
          onDrill={(it) => it.unit_id && openUnit(it.unit_id)}
        />
      )}

      {roster && (
        <>
          <h3 className="section-title">
            {roster.total === 1 ? `Tenancy & rent` : "Unit roster"}
          </h3>
          <p className="hint">
            {roster.total} units · showing {roster.rows.length} (page {Math.floor(offset / ROSTER_PAGE) + 1}
            {" "}of {Math.max(1, Math.ceil(roster.total / ROSTER_PAGE))}). Click a header to sort. "{t("Lease")}" is the
            {`unit's current ${t("lease")} state (today) — a separate, live signal from the month's record-driven `}
            {`"Status" column. `}
            {`“Rent (agreed)” is what the ${t("lease")} says is owed; “Rent collected” is what came `}
            in that month — the gap between them is what arrears measures. Start date, agreed rent
            and last increase are editable here with “Edit rent”, and save to the same record the
            {` Rent Roll tab reads, so a change shows in both. The Rent Roll has the rest of the `}
            {`${t("lease")} (tenant, deposit, opening arrears).`}
          </p>
          <table className="data-table">
            <thead>
              <tr>
                <th className="is-clickable" onClick={() => onSort("unit_number")}>Unit{sortArrow("unit_number")}</th>
                <th className="is-clickable" onClick={() => onSort("status")}>Status{sortArrow("status")}</th>
                <th>{t("Lease")}</th>
                {/* The tenancy's own schedule, next to the month's actuals. "Rent" below is
                    what was COLLECTED that month; "Rent (agreed)" here is what the tenancy
                    says is owed — the two are different questions and the gap between them is
                    what arrears measures. */}
                <th>Start date</th>
                <th>Rent (agreed)</th>
                <th>Last increase</th>
                <th className="is-clickable" onClick={() => onSort("gross_rent")}>Rent collected{sortArrow("gross_rent")}</th>
                <th className="is-clickable" onClick={() => onSort("noi")}>NOI{sortArrow("noi")}</th>
                <th className="is-clickable" onClick={() => onSort("cash_flow")}>Cash Flow{sortArrow("cash_flow")}</th>
                <th className="is-clickable" onClick={() => onSort("noi_change")}>NOI Δ vs prior{sortArrow("noi_change")}</th>
                <th>Edit</th>
              </tr>
            </thead>
            <tbody>
              {roster.rows.flatMap((u) => [
                <tr key={u.unit_id} className="is-clickable" {...clickableProps(() => openUnit(u.unit_id))}>
                  <td>
                    Unit {u.unit_number}
                    {u.label ? ` — ${u.label}` : ""}
                  </td>
                  <td>
                    <span className={statusPillClass(u.status)}>{u.status}</span>
                  </td>
                  <td>
                    <span className={leaseStatusPillClass(u.lease_status ?? "vacant")}>
                      {u.lease_status === "vacant" || u.lease_status == null ? t("vacant") : u.lease_status}
                    </span>
                  </td>
                  <td>{u.lease_start ? fmtDate(u.lease_start) : "—"}</td>
                  <td>
                    {u.lease_contract_rent == null ? "—" : fmtCurrency(u.lease_contract_rent)}
                    {u.lease_id && u.lease_end == null && (
                      <div className="muted" style={{ fontSize: 11 }}>periodic</div>
                    )}
                  </td>
                  <td>
                    {u.lease_id == null ? (
                      "—"
                    ) : (
                      <>
                        {u.last_rent_increase_date ? fmtDate(u.last_rent_increase_date) : "never"}
                        {u.months_since_last_increase != null && (
                          <div className="muted" style={{ fontSize: 11 }}>
                            {`${u.months_since_last_increase} mo ago`}
                          </div>
                        )}
                      </>
                    )}
                  </td>
                  <td>{fmtCurrency(u.gross_rent)}</td>
                  <td>{fmtCurrency(u.noi)}</td>
                  <td className={u.cash_flow < 0 ? "value-negative" : undefined}>{fmtCurrency(u.cash_flow)}</td>
                  <td className={u.noi_change == null ? "muted" : u.noi_change < 0 ? "value-negative" : "value-positive"}>
                    {u.noi_change == null ? "—" : `${u.noi_change > 0 ? "+" : ""}${fmtCurrency(u.noi_change)}`}
                  </td>
                  <td>
                    <button
                      type="button"
                      className="btn btn-ghost"
                      style={{ padding: "2px 6px" }}
                      // The row opens the unit drill-down on click, so this must not bubble.
                      onClick={(e) => {
                        e.stopPropagation();
                        setEditingUnit(editingUnit === u.unit_id ? null : u.unit_id);
                      }}
                      aria-expanded={editingUnit === u.unit_id}
                    >
                      {u.lease_id ? "Edit rent" : `Add ${t("lease")}`}
                    </button>
                  </td>
                </tr>,
                editingUnit === u.unit_id && (
                  <tr key={`${u.unit_id}-edit`}>
                    <td colSpan={10}>
                      <TenancyEditor
                        token={token}
                        unitId={u.unit_id}
                        unitLabel={`unit ${u.unit_number}${u.label ? ` — ${u.label}` : ""}`}
                        onSaved={() => {
                          setEditingUnit(null);
                          // Refetch rather than patching local state: every other screen reads
                          // the same stored tenancy, and showing a client-side guess here is how
                          // two views start disagreeing.
                          setRosterReload((n) => n + 1);
                        }}
                        onCancel={() => setEditingUnit(null)}
                      />
                    </td>
                  </tr>
                ),
              ])}
            </tbody>
          </table>
          <div className="row" style={{ gap: 8, marginTop: 10 }}>
            <button className="btn" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - ROSTER_PAGE))}>
              ‹ Prev
            </button>
            <button
              className="btn"
              disabled={offset + ROSTER_PAGE >= roster.total}
              onClick={() => setOffset(offset + ROSTER_PAGE)}
            >
              Next ›
            </button>
          </div>
        </>
      )}

      {selectedUnit && (
        <div ref={unitPanelRef} style={{ marginTop: 18 }}>
          <UnitDetail token={token} unitId={selectedUnit} onClose={() => setSelectedUnit(null)} />
        </div>
      )}

      <h3 className="section-title">Detail</h3>

      {monthly.length > 0 && (
        <>
          <h4 className="section-title" style={{ fontSize: 13 }}>NOI &amp; Cash Flow trend</h4>
          <PnlTrendChart data={monthly} />
        </>
      )}

      <h4 className="section-title" style={{ fontSize: 13 }}>What the expenses are made of</h4>
      <CategoryComposition rows={categories} />

      <h4 className="section-title" style={{ fontSize: 13 }}>Monthly P&amp;L</h4>
      {isMulti && (
        <p className="hint">Click a month to see each unit's rent &amp; expenses for that month.</p>
      )}
      <table className="data-table">
        <thead>
          <tr>
            <th>Month</th>
            <th>Gross Rent</th>
            <th>Operating</th>
            <th>NOI</th>
            <th>Debt Service</th>
            <th>Capex</th>
            <th>Other</th>
            <th>Cash Flow</th>
          </tr>
        </thead>
        <tbody>
          {monthly.map((m) => {
            const isOpen = openMonth === m.month;
            return (
              <Fragment key={m.month}>
                <tr
                  {...clickableProps(isMulti ? () => setOpenMonth(isOpen ? null : m.month) : undefined)}
                  className={`${isMulti ? "row-strong is-clickable" : ""}${isOpen ? " row-open" : ""}`}
                >
                  <td>
                    {isMulti && <span className="caret" aria-hidden="true">{isOpen ? "▾" : "▸"}</span>}
                    {fmtMonth(m.month)}
                  </td>
                  <Cells m={m} />
                </tr>

                {isOpen && isMulti && (
                  <>
                    {(unitsByMonth[m.month] ?? []).map((u) => (
                      <tr key={m.month + u.unit_id} className="row-sub">
                        <td>
                          Unit {u.unit_number}
                          {u.label ? ` — ${u.label}` : ""}
                        </td>
                        <Cells m={u} />
                      </tr>
                    ))}
                    <tr className="row-tier">
                      <td>Property-tier only (not allocated to units)</td>
                      <Cells m={m.property_tier} />
                    </tr>
                  </>
                )}
              </Fragment>
            );
          })}
          {monthly.length === 0 && !error && (
            <tr className="row-empty">
              <td colSpan={8}>No data in this period.</td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
}

// "Dec 2025" for a single month, "Jan 2025 – Dec 2025" for a span.
function periodLabel(d: { period_from: string | null; period_to: string | null }): string {
  if (!d.period_from || !d.period_to) return "—";
  return d.period_from === d.period_to
    ? fmtMonth(d.period_to)
    : `${fmtMonth(d.period_from)} – ${fmtMonth(d.period_to)}`;
}

// Below-NOI used to be one column. It mixes a mortgage payment, a roof and everything else,
// which are three different decisions — so it is three columns, and the row still adds up:
// NOI − debt service − capex − other = cash flow.
function Cells({ m }: { m: PnLMetrics }) {
  return (
    <>
      <td>{fmtCurrency(m.gross_rent)}</td>
      <td>{fmtCurrency(m.operating_expenses)}</td>
      <td>{fmtCurrency(m.noi)}</td>
      <td>{fmtCurrency(m.debt_service)}</td>
      <td>{fmtCurrency(m.capex)}</td>
      <td>{fmtCurrency(m.other_below_line)}</td>
      <td className={m.cash_flow < 0 ? "value-negative" : undefined}>{fmtCurrency(m.cash_flow)}</td>
    </>
  );
}
