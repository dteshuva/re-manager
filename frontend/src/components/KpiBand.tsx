import { Line, LineChart, ResponsiveContainer, YAxis } from "recharts";
import type { OccupancyMetrics, TrendPoint } from "../api";
import { CHART, fmtCurrency } from "../ui";

// Works for both the portfolio and a single property — anything with current/prior
// occupancy metrics plus a T12 trend.
export interface KpiData {
  current: OccupancyMetrics | null;
  prior: OccupancyMetrics | null;
  trend: TrendPoint[];
}

// Portfolio KPI band: headline metrics for the anchor month, each with its change vs.
// the prior month and a trailing-12 sparkline. Everything here is rendered straight off
// the data returned by /portfolio/dashboard, which reads only portfolio_month_summary.

type MetricKey = "gross_rent" | "operating_expenses" | "noi" | "cash_flow";

const KPIS: { key: MetricKey; label: string; expenseLike: boolean; color: string }[] = [
  { key: "gross_rent", label: "Gross Rent", expenseLike: false, color: CHART.rent },
  { key: "operating_expenses", label: "Operating Expenses", expenseLike: true, color: CHART.opex },
  { key: "noi", label: "NOI", expenseLike: false, color: CHART.noi },
  { key: "cash_flow", label: "Cash Flow", expenseLike: false, color: CHART.cashFlow },
];

// Signed percent change vs prior; null when there's no prior or prior is 0.
function pctChange(cur: number, prior: number | undefined): number | null {
  if (prior === undefined || prior === 0) return null;
  return ((cur - prior) / Math.abs(prior)) * 100;
}

// "up good / down bad", except expense-like metrics where the sense is inverted.
function toneClass(change: number, expenseLike: boolean): string {
  if (change === 0) return "is-flat";
  const good = expenseLike ? change < 0 : change > 0;
  return good ? "is-good" : "is-bad";
}

function Sparkline({ data, dataKey, color }: { data: TrendPoint[]; dataKey: string; color: string }) {
  if (data.length < 2) return <div className="kpi-card__spark" />;
  return (
    <div className="kpi-card__spark">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 3, right: 1, bottom: 3, left: 1 }}>
          {/* domain padded so the line isn't clipped; axis hidden */}
          <YAxis hide domain={["dataMin", "dataMax"]} />
          <Line type="monotone" dataKey={dataKey} stroke={color} strokeWidth={1.75} dot={false} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function DeltaChip({ change, pct, expenseLike, fmt }: {
  change: number;
  pct: number | null;
  expenseLike: boolean;
  fmt: (n: number) => string;
}) {
  const tone = toneClass(change, expenseLike);
  const arrow = change > 0 ? "▲" : change < 0 ? "▼" : "■";
  const pctText = pct === null ? "—" : `${pct > 0 ? "+" : ""}${pct.toFixed(1)}%`;
  return (
    <div className={`kpi-card__delta ${tone}`}>
      <span className="kpi-card__arrow">{arrow}</span>
      <span>{change > 0 ? "+" : ""}{fmt(change)}</span>
      <span className="kpi-card__pct">{pctText}</span>
    </div>
  );
}

export default function KpiBand({ data }: { data: KpiData }) {
  const { current, prior, trend } = data;
  if (!current) {
    return <p className="hint">No summarized data yet — post a month to populate the dashboard.</p>;
  }
  const signedMoney = (n: number) => fmtCurrency(Math.abs(n));

  // Occupancy is a fraction; its change is in percentage points.
  const occCur = current.occupancy;
  const occPrior = prior?.occupancy ?? undefined;
  const occChangePp = occCur !== null && occPrior !== undefined ? (occCur - occPrior) * 100 : null;

  return (
    <div className="kpi-grid">
      {KPIS.map(({ key, label, expenseLike, color }) => {
        const cur = current[key];
        const change = prior ? cur - prior[key] : 0;
        const pct = pctChange(cur, prior?.[key]);
        return (
          <div className="kpi-card" key={key}>
            <div className="kpi-card__label">{label}</div>
            <div className="kpi-card__value">{fmtCurrency(cur)}</div>
            {prior ? (
              <DeltaChip change={change} pct={pct} expenseLike={expenseLike} fmt={signedMoney} />
            ) : (
              <div className="kpi-card__delta is-flat">no prior period</div>
            )}
            <Sparkline data={trend} dataKey={key} color={color} />
          </div>
        );
      })}

      <div className="kpi-card">
        <div className="kpi-card__label">Occupancy</div>
        <div className="kpi-card__value">
          {occCur === null ? "—" : `${(occCur * 100).toFixed(1)}%`}
        </div>
        {occChangePp === null ? (
          <div className="kpi-card__delta is-flat">no prior period</div>
        ) : (
          <div className={`kpi-card__delta ${toneClass(occChangePp, false)}`}>
            <span className="kpi-card__arrow">{occChangePp > 0 ? "▲" : occChangePp < 0 ? "▼" : "■"}</span>
            <span>{occChangePp > 0 ? "+" : ""}{occChangePp.toFixed(1)} pp</span>
            <span className="kpi-card__pct">
              {current.occupied_units}/{current.total_units} units
            </span>
          </div>
        )}
        <Sparkline data={trend.filter((t) => t.occupancy !== null)} dataKey="occupancy" color={CHART.noi} />
      </div>
    </div>
  );
}
