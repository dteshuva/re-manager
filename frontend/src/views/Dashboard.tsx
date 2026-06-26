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
import { card, fmtCurrency, fmtMonth } from "../ui";

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
      <h2>Portfolio — Monthly P&amp;L</h2>
      {error && <p style={{ color: "crimson" }}>{error}</p>}

      <PeriodSelector availableMonths={allMonths} onChange={setRange} />

      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 16 }}>
        <SummaryCard label="Gross Rent" value={totals.gross_rent} />
        <SummaryCard label="Operating" value={totals.operating_expenses} />
        <SummaryCard label="NOI" value={totals.noi} />
        <SummaryCard label="Below-NOI" value={totals.below_noi} />
        <SummaryCard label="Cash Flow" value={totals.cash_flow} accent />
      </div>

      {breakdown && breakdown.properties.length > 0 && (
        <>
          <h3>Breakdown by property &amp; unit</h3>
          <p style={{ color: "#666", marginTop: -8 }}>
            Totals for the selected period. Click a multifamily property to drill into its
            units; <em>property-tier-only</em> items (shared capex, debt service) are shown
            separately and are not allocated to units.
          </p>
          <div style={{ marginBottom: 24 }}>
            <BreakdownTable data={breakdown} />
          </div>
        </>
      )}

      {chartData.length > 0 && (
        <>
          <h3>NOI &amp; Cash Flow trend</h3>
          <PnlTrendChart data={rows} />

          <h3>Rent vs Operating Expenses</h3>
          <div style={{ ...card, height: 260 }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chartData} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#eee" />
                <XAxis dataKey="label" />
                <YAxis tickFormatter={(v) => money(v)} width={80} />
                <Tooltip formatter={(v) => money(v as number)} />
                <Legend />
                <Bar dataKey="gross_rent" name="Gross Rent" fill="#2563eb" />
                <Bar dataKey="operating_expenses" name="Operating" fill="#f59e0b" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </>
      )}

      <h3>Monthly detail</h3>
      <table cellPadding={6} style={{ borderCollapse: "collapse", width: "100%" }}>
        <thead>
          <tr style={{ textAlign: "right", borderBottom: "2px solid #333" }}>
            <th style={{ textAlign: "left" }}>Month</th>
            <th>Gross Rent</th>
            <th>Operating</th>
            <th>NOI</th>
            <th>Below-NOI</th>
            <th>Cash Flow</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.month} style={{ textAlign: "right", borderBottom: "1px solid #ddd" }}>
              <td style={{ textAlign: "left" }}>{fmtMonth(r.month)}</td>
              <td>{fmtCurrency(r.gross_rent)}</td>
              <td>{fmtCurrency(r.operating_expenses)}</td>
              <td>{fmtCurrency(r.noi)}</td>
              <td>{fmtCurrency(r.below_noi)}</td>
              <td style={{ color: r.cash_flow < 0 ? "crimson" : undefined }}>{fmtCurrency(r.cash_flow)}</td>
            </tr>
          ))}
          {rows.length === 0 && !error && (
            <tr>
              <td colSpan={6} style={{ color: "#888" }}>
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
  return (
    <div style={{ ...card, minWidth: 140, marginBottom: 0 }}>
      <div style={{ color: "#666", fontSize: 13 }}>{label}</div>
      <div
        style={{
          fontSize: 22,
          fontWeight: 600,
          color: accent && value < 0 ? "crimson" : accent ? "#16a34a" : "#111",
        }}
      >
        {fmtCurrency(value)}
      </div>
    </div>
  );
}
