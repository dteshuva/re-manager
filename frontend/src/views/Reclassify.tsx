import { useCallback, useEffect, useState } from "react";
import {
  CLASSIFICATIONS,
  getPortfolioMonthly,
  listCategories,
  updateCategory,
  type Category,
  type Classification,
  type MonthlyPnL,
  type PeriodRange,
} from "../api";
import PeriodSelector from "../components/PeriodSelector";
import { card, fmtCurrency } from "../ui";

type Totals = { operating_expenses: number; noi: number; below_noi: number; cash_flow: number };

const sum = (rows: MonthlyPnL[]): Totals =>
  rows.reduce(
    (a, r) => ({
      operating_expenses: a.operating_expenses + r.operating_expenses,
      noi: a.noi + r.noi,
      below_noi: a.below_noi + r.below_noi,
      cash_flow: a.cash_flow + r.cash_flow,
    }),
    { operating_expenses: 0, noi: 0, below_noi: 0, cash_flow: 0 },
  );

// The reclassification control. Each category's classification is the ONLY thing that
// drives the math: moving a category above or below the NOI line recomputes NOI and cash
// flow with no schema change or migration. Change a classification here and watch the
// portfolio totals recompute live (with the before→after delta).
export default function Reclassify({ token }: { token: string }) {
  const [cats, setCats] = useState<Category[]>([]);
  const [range, setRange] = useState<PeriodRange>({});
  const [allMonths, setAllMonths] = useState<string[]>([]);
  const [totals, setTotals] = useState<Totals | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reloadTotals = useCallback(
    async (r: PeriodRange): Promise<Totals> => {
      const rows = await getPortfolioMonthly(token, r);
      const t = sum(rows);
      setTotals(t);
      return t;
    },
    [token],
  );

  useEffect(() => {
    listCategories(token).then(setCats).catch((e) => setError(e.message));
    getPortfolioMonthly(token)
      .then((r) => setAllMonths(r.map((x) => x.month)))
      .catch((e) => setError(e.message));
  }, [token]);

  useEffect(() => {
    reloadTotals(range).catch((e) => setError(e.message));
  }, [range.from, range.to, reloadTotals]);

  async function reclassify(cat: Category, next: Classification) {
    setError(null);
    setFlash(null);
    const before = totals ?? (await reloadTotals(range));
    try {
      await updateCategory(token, cat.id, { default_classification: next });
      setCats((cs) => cs.map((c) => (c.id === cat.id ? { ...c, default_classification: next } : c)));
      const after = await reloadTotals(range);
      const dNoi = after.noi - before.noi;
      setFlash(
        `Reclassified “${cat.name}”: ${cat.default_classification} → ${next}. ` +
          `NOI ${fmtCurrency(before.noi)} → ${fmtCurrency(after.noi)} ` +
          `(${dNoi >= 0 ? "+" : ""}${fmtCurrency(dNoi)}) — recomputed, no migration.`,
      );
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <section>
      <h2>Reclassification</h2>
      <p style={{ color: "#666", marginTop: -8, maxWidth: 720 }}>
        A category's <strong>classification</strong> is the only thing that drives the math.
        Operating expenses sit <em>above</em> the NOI line; capex, debt service, and other
        below-line items sit <em>below</em> it. Move a category across the line and NOI /
        cash flow recompute instantly — no schema change, no data migration.
      </p>

      <PeriodSelector availableMonths={allMonths} onChange={setRange} />

      {totals && (
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 12 }}>
          <Stat label="Operating Expenses" value={totals.operating_expenses} />
          <Stat label="NOI" value={totals.noi} highlight />
          <Stat label="Below-NOI" value={totals.below_noi} />
          <Stat label="Cash Flow" value={totals.cash_flow} />
        </div>
      )}

      {flash && (
        <p style={{ ...card, background: "#ecfdf5", borderColor: "#a7f3d0", color: "#065f46" }}>{flash}</p>
      )}
      {error && <p style={{ color: "crimson" }}>{error}</p>}

      <table cellPadding={6} style={{ borderCollapse: "collapse", width: "100%", maxWidth: 640 }}>
        <thead>
          <tr style={{ textAlign: "left", borderBottom: "2px solid #333" }}>
            <th>Category</th>
            <th>Classification</th>
            <th>Line</th>
          </tr>
        </thead>
        <tbody>
          {cats.map((c) => {
            const above = c.default_classification === "operating" || c.default_classification === "rent";
            return (
              <tr key={c.id} style={{ borderBottom: "1px solid #eee", opacity: c.active ? 1 : 0.5 }}>
                <td>
                  {c.name}
                  {!c.active && <span style={{ color: "#888" }}> (inactive)</span>}
                </td>
                <td>
                  <select
                    value={c.default_classification}
                    onChange={(e) => reclassify(c, e.target.value as Classification)}
                    style={{ padding: "5px 8px", borderRadius: 6, border: "1px solid #bbb" }}
                  >
                    {CLASSIFICATIONS.map((x) => (
                      <option key={x} value={x}>
                        {x}
                      </option>
                    ))}
                  </select>
                </td>
                <td style={{ color: above ? "#2563eb" : "#b45309" }}>
                  {c.default_classification === "rent"
                    ? "income"
                    : above
                      ? "above NOI"
                      : "below NOI"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

function Stat({ label, value, highlight }: { label: string; value: number; highlight?: boolean }) {
  return (
    <div style={{ ...card, minWidth: 150, marginBottom: 0, borderColor: highlight ? "#2563eb" : "#ddd" }}>
      <div style={{ color: "#666", fontSize: 13 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 600, color: value < 0 ? "crimson" : "#111" }}>
        {fmtCurrency(value)}
      </div>
    </div>
  );
}
