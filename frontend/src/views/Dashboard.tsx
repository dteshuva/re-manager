import { useEffect, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  exportPortfolioMonthly,
  exportPortfolioVariance,
  getAttentionFeed,
  getPortfolioBreakdown,
  getPortfolioDashboard,
  getPortfolioMonthly,
  getPortfolioVariance,
  getTags,
  getWorstUnits,
  type AttentionFeed as AttentionFeedType,
  type ExportFormat,
  type MonthlyPnL,
  type PeriodRange,
  type PortfolioBreakdown,
  type PortfolioDashboard,
  type PortfolioVariance,
  type WorstUnitsLeaderboard,
} from "../api";
import AttentionFeed from "../components/AttentionFeed";
import BreakdownTable from "../components/BreakdownTable";
import ComplianceAlerts from "../components/ComplianceAlerts";
import ExportControl from "../components/ExportControl";
import KpiBand from "../components/KpiBand";
import PeriodSelector from "../components/PeriodSelector";
import PnlTrendChart from "../components/PnlTrendChart";
import SavedViews from "../components/SavedViews";
import { card, CHART, fmtCurrency, fmtMonth, isYoyComparison } from "../ui";

// Portfolio dashboard: period selector + summary cards + monthly table + trend charts
// for NOI and cash flow. All figures come straight from the computed P&L views; the
// period selector just narrows the from/to passed to the API.
export default function Dashboard({ token }: { token: string }) {
  const [allMonths, setAllMonths] = useState<string[]>([]);
  const [rows, setRows] = useState<MonthlyPnL[]>([]);
  const [breakdown, setBreakdown] = useState<PortfolioBreakdown | null>(null);
  const [range, setRange] = useState<PeriodRange>({});
  const [dashboard, setDashboard] = useState<PortfolioDashboard | null>(null);
  const [variance, setVariance] = useState<PortfolioVariance | null>(null);
  const [feed, setFeed] = useState<AttentionFeedType | null>(null);
  const [worstUnits, setWorstUnits] = useState<WorstUnitsLeaderboard | null>(null);
  const [allTags, setAllTags] = useState<string[]>([]);
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  const toggleTag = (t: string) =>
    setSelectedTags((cur) => (cur.includes(t) ? cur.filter((x) => x !== t) : [...cur, t]));

  // One initial load (no range) to discover which months have data, plus the full set of
  // tags in use (for the filter chips) — neither depends on the selected period/tags.
  useEffect(() => {
    let cancelled = false;
    getPortfolioMonthly(token)
      .then((r) => {
        if (cancelled) return;
        setAllMonths(r.map((x) => x.month));
        setError(null);
      })
      .catch((e) => !cancelled && setError(e.message));
    getTags(token)
      .then((t) => !cancelled && setAllTags(t))
      .catch(() => {
        /* tag filter is a nice-to-have; don't block the dashboard on it */
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  // One period selector (+ optional tag filter) drives everything: KPI band + sparklines
  // (period vs prior period), the attention feed and worst-units leaderboard (anchored at
  // the period's last month), and the detail sections below. A fresh load clears any stale
  // error: the requests run in parallel, so a single transient network blip must not leave
  // the banner stuck once the reload succeeds. `cancelled` guards against a slow, superseded
  // request overwriting fresher state if the user changes the period/tags again before this
  // one resolves.
  useEffect(() => {
    let cancelled = false;
    Promise.all([
      getPortfolioDashboard(token, range, selectedTags).then((d) => !cancelled && setDashboard(d)),
      getPortfolioVariance(token, range).then((v) => !cancelled && setVariance(v)),
      getAttentionFeed(token, range, selectedTags).then((f) => !cancelled && setFeed(f)),
      getPortfolioMonthly(token, range, selectedTags).then((r) => !cancelled && setRows(r)),
      getPortfolioBreakdown(token, range, selectedTags).then((b) => !cancelled && setBreakdown(b)),
      getWorstUnits(token, range, { limit: 10, tags: selectedTags }).then(
        (w) => !cancelled && setWorstUnits(w),
      ),
    ])
      .then(() => !cancelled && setError(null))
      .catch((e) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [token, range.from, range.to, selectedTags]);

  const chartData = useMemo(
    () => rows.map((r) => ({ ...r, label: fmtMonth(r.month) })),
    [rows],
  );

  const money = (v: number | string) => fmtCurrency(Number(v));

  // Export always downloads the CURRENTLY-VIEWED period/tag scope — these closures just
  // bind the active range/tags into the shared export API functions (which call the same
  // backend query functions the dashboard itself reads from).
  const exportReports = useMemo(
    () => [
      {
        value: "monthly",
        label: "Monthly P&L",
        onExport: (format: ExportFormat) => exportPortfolioMonthly(token, range, selectedTags, format),
      },
      {
        value: "variance",
        label: "Variance vs budget",
        onExport: (format: ExportFormat) => exportPortfolioVariance(token, range, format),
      },
    ],
    [token, range, selectedTags],
  );

  return (
    <section>
      {error && <p className="alert-error">{error}</p>}

      <div className="row" style={{ justifyContent: "space-between", alignItems: "center", marginBottom: 14, flexWrap: "wrap", gap: 10 }}>
        <h3 className="section-title" style={{ margin: 0 }}>
          Portfolio · {periodLabel(dashboard)}
          {dashboard?.prior_from && (
            <span className="muted" style={{ fontWeight: 500, fontSize: 13 }}>
              {" "}vs {periodLabel({ period_from: dashboard.prior_from, period_to: dashboard.prior_to })}
              {isYoyComparison(dashboard.period_from, dashboard.period_to, dashboard.prior_from, dashboard.prior_to) && (
                <strong style={{ color: "var(--accent)", fontWeight: 650 }}> · year-over-year</strong>
              )}
            </span>
          )}
        </h3>
        <PeriodSelector availableMonths={allMonths} onChange={setRange} defaultMode="month" />
      </div>

      <div className="row" style={{ justifyContent: "space-between", alignItems: "center", marginBottom: 14, flexWrap: "wrap", gap: 10 }}>
        <SavedViews
          current={{ range, tags: selectedTags }}
          onApply={(v) => {
            setRange(v.range);
            setSelectedTags(v.tags);
          }}
        />
        <ExportControl reports={exportReports} idPrefix="dashboard" />
      </div>

      {allTags.length > 0 && (
        <div className="tag-filter" style={{ marginBottom: 14 }}>
          <span className="muted" style={{ fontSize: 12.5, fontWeight: 550 }}>
            Filter by tag:
          </span>
          {allTags.map((t) => (
            <button
              key={t}
              type="button"
              className={`tag-chip${selectedTags.includes(t) ? " is-active" : ""}`}
              onClick={() => toggleTag(t)}
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

      {dashboard && <KpiBand data={dashboard} variance={variance} />}

      <ComplianceAlerts token={token} />

      <h3 className="section-title">Needs attention</h3>
      <p className="hint">
        Exceptions occurring anywhere in {feed ? periodLabel(feed) : "the selected period"}, ranked by
        dollar impact — biggest first. Each is tagged with its month and links to the property causing it.
        This feed is record/actuals-based (posted monthly records) — a separate, sometimes-disagreeing
        signal from the lease-based occupancy shown on the Rent Roll tab.
      </p>
      {feed && <AttentionFeed feed={feed} />}

      <h3 className="section-title">Worst units, portfolio-wide</h3>
      <p className="hint">
        The units with the biggest NOI drop this period, across every property — ranked by $
        impact, not just within one property. A building-wide event (e.g. one rent cut applied
        across a whole property) collapses into a single rolled-up row rather than flooding the
        table with near-identical units.
      </p>
      {worstUnits ? (
        <table className="data-table">
          <thead>
            <tr>
              <th>Unit</th>
              <th style={{ textAlign: "left" }}>Property</th>
              <th style={{ textAlign: "left" }}>Month</th>
              <th>NOI</th>
              <th>Prior NOI</th>
              <th>Change</th>
            </tr>
          </thead>
          <tbody>
            {worstUnits.items.map((u) => (
              <tr key={`${u.property_id}-${u.unit_id ?? "rollup"}-${u.month}`}>
                <td>{u.rolled_up ? `${u.count} units` : `Unit ${u.unit_number}`}</td>
                <td style={{ textAlign: "left" }}>{u.property_name}</td>
                <td style={{ textAlign: "left" }}>{fmtMonth(u.month)}</td>
                <td>{u.current != null ? fmtCurrency(u.current) : "—"}</td>
                <td>{u.prior != null ? fmtCurrency(u.prior) : "—"}</td>
                <td className="value-negative">
                  {u.change != null && u.pct_change != null
                    ? `${fmtCurrency(u.change)} (${u.pct_change.toFixed(0)}%)`
                    : `${fmtCurrency(-u.magnitude)} total` +
                      (u.pct_change != null ? ` (~${u.pct_change.toFixed(0)}%)` : "")}
                </td>
              </tr>
            ))}
            {worstUnits.items.length === 0 && (
              <tr className="row-empty">
                <td colSpan={6}>No unit NOI drops in this period.</td>
              </tr>
            )}
          </tbody>
        </table>
      ) : (
        <p className="hint">Loading…</p>
      )}

      {breakdown && breakdown.properties.length > 0 && (
        <>
          <h3 className="section-title">Breakdown by property &amp; unit</h3>
          <p className="hint">
            Totals for the selected period. Click a multifamily property to drill into its
            units; <em>property-tier-only</em> items (shared capex, debt service) are shown
            separately and are not allocated to units.
          </p>
          <BreakdownTable data={breakdown} />
        </>
      )}

      {chartData.length > 0 && (
        <>
          <h3 className="section-title">NOI &amp; Cash Flow trend</h3>
          <PnlTrendChart data={rows} />

          <h3 className="section-title">Rent vs Operating Expenses</h3>
          <div
            style={{ ...card, height: 280, padding: "18px 16px 8px" }}
            role="img"
            aria-label={`Rent and operating expenses by month, ${chartData.length} month${chartData.length === 1 ? "" : "s"}. See the monthly detail table below for exact figures.`}
          >
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chartData} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
                <CartesianGrid strokeDasharray="3 3" stroke={CHART.grid} vertical={false} />
                <XAxis dataKey="label" tick={{ fontSize: 12, fill: CHART.axis }} tickLine={false} axisLine={{ stroke: CHART.grid }} />
                <YAxis tickFormatter={(v) => money(v)} width={84} tick={{ fontSize: 12, fill: CHART.axis }} tickLine={false} axisLine={false} />
                <Tooltip
                  cursor={{ fill: "rgba(16,24,40,0.04)" }}
                  formatter={(v) => money(v as number)}
                  contentStyle={{ borderRadius: 10, border: "1px solid var(--border)", boxShadow: "var(--shadow-md)", fontSize: 13 }}
                />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Bar dataKey="gross_rent" name="Gross Rent" fill={CHART.rent} radius={[4, 4, 0, 0]} />
                <Bar dataKey="operating_expenses" name="Operating" fill={CHART.opex} radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </>
      )}

      <h3 className="section-title">Monthly detail</h3>
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
          {rows.map((r) => (
            <tr key={r.month}>
              <td>{fmtMonth(r.month)}</td>
              <td>{fmtCurrency(r.gross_rent)}</td>
              <td>{fmtCurrency(r.operating_expenses)}</td>
              <td>{fmtCurrency(r.noi)}</td>
              <td>{fmtCurrency(r.below_noi)}</td>
              <td className={r.cash_flow < 0 ? "value-negative" : undefined}>{fmtCurrency(r.cash_flow)}</td>
            </tr>
          ))}
          {rows.length === 0 && !error && (
            <tr className="row-empty">
              <td colSpan={6}>
                No data in this period — widen the range or add records under <strong>Data Entry</strong>.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
}

// "Dec 2025" for a single month, "Jan 2025 – Dec 2025" for a span.
function periodLabel(d: { period_from: string | null; period_to: string | null } | null): string {
  if (!d?.period_from || !d?.period_to) return "—";
  return d.period_from === d.period_to
    ? fmtMonth(d.period_to)
    : `${fmtMonth(d.period_from)} – ${fmtMonth(d.period_to)}`;
}
