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
  getPortfolioBreakdown,
  getPortfolioMonthly,
  type MonthlyPnL,
  type PeriodRange,
  type PortfolioBreakdown,
} from "../api";
import BreakdownTable from "../components/BreakdownTable";
import PeriodSelector from "../components/PeriodSelector";
import PnlTrendChart from "../components/PnlTrendChart";
import { card, CHART, fmtCurrency, fmtMonth } from "../ui";

// Portfolio dashboard: period selector + summary cards + monthly table + trend charts
// for NOI and cash flow. All figures come straight from the computed P&L views; the
// period selector just narrows the from/to passed to the API.
export default function Dashboard({ token }: { token: string }) {
  const [allMonths, setAllMonths] = useState<string[]>([]);
  const [rows, setRows] = useState<MonthlyPnL[]>([]);
  const [breakdown, setBreakdown] = useState<PortfolioBreakdown | null>(null);
  const [range, setRange] = useState<PeriodRange>({});
  const [error, setError] = useState<string | null>(null);

  // One initial load (no range) to discover which months have data.
  useEffect(() => {
    getPortfolioMonthly(token)
      .then((r) => setAllMonths(r.map((x) => x.month)))
      .catch((e) => setError(e.message));
  }, [token]);

  // Reload whenever the selected range changes.
  useEffect(() => {
    getPortfolioMonthly(token, range).then(setRows).catch((e) => setError(e.message));
    getPortfolioBreakdown(token, range).then(setBreakdown).catch((e) => setError(e.message));
  }, [token, range.from, range.to]);

  const chartData = useMemo(
    () => rows.map((r) => ({ ...r, label: fmtMonth(r.month) })),
    [rows],
  );

  const totals = useMemo(
    () =>
      rows.reduce(
        (a, r) => ({
          gross_rent: a.gross_rent + r.gross_rent,
          operating_expenses: a.operating_expenses + r.operating_expenses,
          noi: a.noi + r.noi,
          below_noi: a.below_noi + r.below_noi,
          cash_flow: a.cash_flow + r.cash_flow,
        }),
        { gross_rent: 0, operating_expenses: 0, noi: 0, below_noi: 0, cash_flow: 0 },
      ),
    [rows],
  );

  const money = (v: number | string) => fmtCurrency(Number(v));

  return (
    <section>
      {error && <p className="alert-error">{error}</p>}

      <PeriodSelector availableMonths={allMonths} onChange={setRange} />

      <div className="summary-grid">
        <SummaryCard label="Gross Rent" value={totals.gross_rent} />
        <SummaryCard label="Operating" value={totals.operating_expenses} />
        <SummaryCard label="NOI" value={totals.noi} />
        <SummaryCard label="Below-NOI" value={totals.below_noi} />
        <SummaryCard label="Cash Flow" value={totals.cash_flow} accent />
      </div>

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
          <div style={{ ...card, height: 280, padding: "18px 16px 8px" }}>
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

function SummaryCard({ label, value, accent }: { label: string; value: number; accent?: boolean }) {
  const valueClass = accent ? (value < 0 ? "value-negative" : "value-positive") : undefined;
  return (
    <div className={`summary-card${accent ? " is-accent" : ""}`}>
      <div className="summary-card__label">{label}</div>
      <div className={`summary-card__value${valueClass ? " " + valueClass : ""}`}>{fmtCurrency(value)}</div>
    </div>
  );
}
