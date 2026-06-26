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
import { card, fmtCurrency, fmtMonth } from "../ui";

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
    <div style={{ ...card, height }}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={chartData} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#eee" />
          <XAxis dataKey="label" />
          <YAxis tickFormatter={(v) => money(v)} width={80} />
          <Tooltip formatter={(v) => money(v as number)} />
          <Legend />
          <Line type="monotone" dataKey="noi" name="NOI" stroke="#2563eb" strokeWidth={2} />
          <Line type="monotone" dataKey="cash_flow" name="Cash Flow" stroke="#16a34a" strokeWidth={2} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
