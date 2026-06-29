import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { card, CHART, fmtCurrency, fmtMonth } from "../ui";

// Shared NOI + Cash Flow trend chart, used by the portfolio dashboard and the
// property/unit detail views so they stay visually consistent.
export default function PnlTrendChart({
  data,
  height = 280,
}: {
  data: { month: string; noi: number; cash_flow: number }[];
  height?: number;
}) {
  if (data.length === 0) return null;
  const chartData = data.map((r) => ({ ...r, label: fmtMonth(r.month) }));
  const money = (v: number | string) => fmtCurrency(Number(v));
  return (
    <div style={{ ...card, height, padding: "18px 16px 8px" }}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={chartData} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={CHART.grid} vertical={false} />
          <XAxis dataKey="label" tick={{ fontSize: 12, fill: CHART.axis }} tickLine={false} axisLine={{ stroke: CHART.grid }} />
          <YAxis
            tickFormatter={(v) => money(v)}
            width={84}
            tick={{ fontSize: 12, fill: CHART.axis }}
            tickLine={false}
            axisLine={false}
          />
          <Tooltip
            formatter={(v) => money(v as number)}
            contentStyle={{ borderRadius: 10, border: "1px solid var(--border)", boxShadow: "var(--shadow-md)", fontSize: 13 }}
          />
          <Legend iconType="plainline" wrapperStyle={{ fontSize: 12 }} />
          <Line type="monotone" dataKey="noi" name="NOI" stroke={CHART.noi} strokeWidth={2.5} dot={false} />
          <Line type="monotone" dataKey="cash_flow" name="Cash Flow" stroke={CHART.cashFlow} strokeWidth={2.5} dot={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
