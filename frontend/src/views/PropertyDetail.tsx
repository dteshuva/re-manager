import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import {
  exportPropertyMonthly,
  exportPropertyVariance,
  getPropertyAttention,
  getPropertyDashboard,
  getPropertyMonthly,
  getPropertyUnitsMonthly,
  getPropertyVariance,
  getUnitRoster,
  listProperties,
  type AttentionFeed as AttentionFeedType,
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
import ExportControl from "../components/ExportControl";
import InvestmentPanel from "../components/InvestmentPanel";
import KpiBand from "../components/KpiBand";
import PeriodSelector from "../components/PeriodSelector";
import PnlTrendChart from "../components/PnlTrendChart";
import PropertySearchSelect from "../components/PropertySearchSelect";
import UnitDetail from "../components/UnitDetail";
import { clickableProps } from "../hooks/clickable";
import { fmtCurrency, fmtMonth, isYoyComparison, leaseStatusPillClass, statusPillClass } from "../ui";

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
  const [unitRows, setUnitRows] = useState<UnitMonthlyPnL[]>([]);
  const [openMonth, setOpenMonth] = useState<string | null>(null);
  const [selectedUnit, setSelectedUnit] = useState<string | null>(null);
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
    return () => {
      cancelled = true;
    };
  }, [token, propertyId, range.from, range.to]);

  // Unit roster (server-sorted/paginated) at the period's end month — multifamily only.
  useEffect(() => {
    if (!propertyId || !isMulti) {
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
  }, [token, propertyId, isMulti, range.to, sort, order, offset]);

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

      {propertyId && <InvestmentPanel token={token} propertyId={propertyId} />}

      <h3 className="section-title">Needs attention</h3>
      {feed && (
        <AttentionFeed
          feed={feed}
          onDrill={(it) => it.unit_id && openUnit(it.unit_id)}
        />
      )}

      {isMulti && roster && (
        <>
          <h3 className="section-title">Unit roster</h3>
          <p className="hint">
            {roster.total} units · showing {roster.rows.length} (page {Math.floor(offset / ROSTER_PAGE) + 1}
            {" "}of {Math.max(1, Math.ceil(roster.total / ROSTER_PAGE))}). Click a header to sort. "Lease" is the
            unit's current lease state (today) — a separate, live signal from the month's record-driven
            "Status" column; see the Rent Roll tab for full lease detail.
          </p>
          <table className="data-table">
            <thead>
              <tr>
                <th className="is-clickable" onClick={() => onSort("unit_number")}>Unit{sortArrow("unit_number")}</th>
                <th className="is-clickable" onClick={() => onSort("status")}>Status{sortArrow("status")}</th>
                <th>Lease</th>
                <th className="is-clickable" onClick={() => onSort("gross_rent")}>Rent{sortArrow("gross_rent")}</th>
                <th className="is-clickable" onClick={() => onSort("noi")}>NOI{sortArrow("noi")}</th>
                <th className="is-clickable" onClick={() => onSort("cash_flow")}>Cash Flow{sortArrow("cash_flow")}</th>
                <th className="is-clickable" onClick={() => onSort("noi_change")}>NOI Δ vs prior{sortArrow("noi_change")}</th>
              </tr>
            </thead>
            <tbody>
              {roster.rows.map((u) => (
                <tr key={u.unit_id} className="is-clickable" {...clickableProps(() => openUnit(u.unit_id))}>
                  <td>
                    Unit {u.unit_number}
                    {u.label ? ` — ${u.label}` : ""}
                  </td>
                  <td>
                    <span className={statusPillClass(u.status)}>{u.status}</span>
                  </td>
                  <td>
                    <span className={leaseStatusPillClass(u.lease_status ?? "vacant")}>{u.lease_status ?? "vacant"}</span>
                  </td>
                  <td>{fmtCurrency(u.gross_rent)}</td>
                  <td>{fmtCurrency(u.noi)}</td>
                  <td className={u.cash_flow < 0 ? "value-negative" : undefined}>{fmtCurrency(u.cash_flow)}</td>
                  <td className={u.noi_change == null ? "muted" : u.noi_change < 0 ? "value-negative" : "value-positive"}>
                    {u.noi_change == null ? "—" : `${u.noi_change > 0 ? "+" : ""}${fmtCurrency(u.noi_change)}`}
                  </td>
                </tr>
              ))}
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
            <th>Below-NOI</th>
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
              <td colSpan={6}>No data in this period.</td>
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

function Cells({ m }: { m: PnLMetrics }) {
  return (
    <>
      <td>{fmtCurrency(m.gross_rent)}</td>
      <td>{fmtCurrency(m.operating_expenses)}</td>
      <td>{fmtCurrency(m.noi)}</td>
      <td>{fmtCurrency(m.below_noi)}</td>
      <td className={m.cash_flow < 0 ? "value-negative" : undefined}>{fmtCurrency(m.cash_flow)}</td>
    </>
  );
}
