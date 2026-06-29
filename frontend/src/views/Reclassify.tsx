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
      <p className="hint" style={{ maxWidth: 720, marginTop: 0 }}>
        A category's <strong>classification</strong> is the only thing that drives the math.
        Operating expenses sit <em>above</em> the NOI line; capex, debt service, and other
        below-line items sit <em>below</em> it. Move a category across the line and NOI /
        cash flow recompute instantly — no schema change, no data migration.
      </p>

      <PeriodSelector availableMonths={allMonths} onChange={setRange} />

      {totals && (
        <div className="summary-grid">
          <Stat label="Operating Expenses" value={totals.operating_expenses} />
          <Stat label="NOI" value={totals.noi} highlight />
          <Stat label="Below-NOI" value={totals.below_noi} />
          <Stat label="Cash Flow" value={totals.cash_flow} />
        </div>
      )}

      {flash && (
        <p style={{ ...card, background: "var(--positive-soft)", borderColor: "#a7f3d0", color: "#065f46", fontWeight: 500 }}>{flash}</p>
      )}
      {error && <p className="alert-error">{error}</p>}

      <table className="data-table" style={{ textAlign: "left", maxWidth: 640 }}>
        <thead>
          <tr>
            <th style={{ textAlign: "left" }}>Category</th>
            <th style={{ textAlign: "left" }}>Classification</th>
            <th style={{ textAlign: "left" }}>Line</th>
          </tr>
        </thead>
        <tbody>
          {cats.map((c) => {
            const above = c.default_classification === "operating" || c.default_classification === "rent";
            const isIncome = c.default_classification === "rent";
            return (
              <tr key={c.id} style={{ opacity: c.active ? 1 : 0.5 }}>
                <td>
                  {c.name}
                  {!c.active && <span className="muted"> (inactive)</span>}
                </td>
                <td style={{ textAlign: "left" }}>
                  <select
                    className="select"
                    value={c.default_classification}
                    onChange={(e) => reclassify(c, e.target.value as Classification)}
                  >
                    {CLASSIFICATIONS.map((x) => (
                      <option key={x} value={x}>
                        {x}
                      </option>
                    ))}
                  </select>
                </td>
                <td style={{ textAlign: "left" }}>
                  <span className="pill" style={{ color: isIncome ? "var(--positive)" : above ? "var(--blue)" : "#b45309" }}>
                    {isIncome ? "income" : above ? "above NOI" : "below NOI"}
                  </span>
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
    <div className={`summary-card${highlight ? " is-accent" : ""}`}>
      <div className="summary-card__label">{label}</div>
      <div className={`summary-card__value${value < 0 ? " value-negative" : ""}`}>{fmtCurrency(value)}</div>
    </div>
  );
}
