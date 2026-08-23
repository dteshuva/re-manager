import { useCallback, useEffect, useState } from "react";
import {
  CLASSIFICATIONS,
  getMe,
  getPortfolioMonthly,
  listCategories,
  mergeCategory,
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
  const [search, setSearch] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [mergeSource, setMergeSource] = useState("");
  const [mergeTarget, setMergeTarget] = useState("");

  const reloadTotals = useCallback(
    async (r: PeriodRange): Promise<Totals> => {
      const rows = await getPortfolioMonthly(token, r);
      const t = sum(rows);
      setTotals(t);
      return t;
    },
    [token],
  );

  const reloadCats = useCallback(
    () => listCategories(token, false, true).then(setCats).catch((e) => setError(e.message)),
    [token],
  );

  useEffect(() => {
    reloadCats();
    getPortfolioMonthly(token)
      .then((r) => setAllMonths(r.map((x) => x.month)))
      .catch((e) => setError(e.message));
    // Merge is admin-only server-side; resolve the role once so non-admins simply don't
    // see a control that would 403 (same pattern as AuditLogSection/BudgetSection).
    getMe(token)
      .then((m) => setIsAdmin(m.role === "admin"))
      .catch(() => setIsAdmin(false));
  }, [token, reloadCats]);

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

  async function doMerge() {
    setError(null);
    setFlash(null);
    if (!mergeSource || !mergeTarget || mergeSource === mergeTarget) return;
    const source = cats.find((c) => c.id === mergeSource);
    const target = cats.find((c) => c.id === mergeTarget);
    if (!source || !target) return;
    const count = source.usage_count ?? "an unknown number of";
    const classificationMismatch = source.default_classification !== target.default_classification;
    const reclassWarning = classificationMismatch
      ? ` Note: “${source.name}” is currently ${source.default_classification} and ` +
        `“${target.name}” is ${target.default_classification} — this moves those dollars ` +
        `across the NOI line, not just consolidating the breakdown.`
      : "";
    if (
      !confirm(
        `Merge “${source.name}” into “${target.name}”? ${count} line item(s) will move, and ` +
          `“${source.name}” will be deactivated.${reclassWarning}`,
      )
    )
      return;
    try {
      // The confirm dialog above already warned about a classification mismatch (if any) —
      // pass the explicit opt-in the server requires for that case so the UI flow stays a
      // single confirm, not a second round-trip after a 409.
      const result = await mergeCategory(token, mergeSource, mergeTarget, classificationMismatch);
      await reloadCats();
      await reloadTotals(range);
      setMergeSource("");
      setMergeTarget("");
      setFlash(
        `Merged “${source.name}” into “${target.name}” — ${result.reassigned_count} line item(s) ` +
          `reassigned, “${source.name}” deactivated.`,
      );
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const filteredCats = cats.filter((c) => c.name.toLowerCase().includes(search.trim().toLowerCase()));
  const zeroUsageCount = cats.filter((c) => c.usage_count === 0).length;

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

      {isAdmin && (
        <div className="card" style={{ maxWidth: 640, marginBottom: 16 }}>
          <div className="section-title" style={{ marginTop: 0 }}>Merge category</div>
          <p className="hint" style={{ marginTop: 0 }}>
            The PDF/statement importer can create a near-duplicate category per literal
            statement line. Merge folds a source category's line items into a target category
            and deactivates the source — the source's history is preserved (deactivated, not
            deleted), and the target's breakdown absorbs the dollar amounts.
            {zeroUsageCount > 0 && ` ${zeroUsageCount} categor${zeroUsageCount === 1 ? "y has" : "ies have"} zero usage — likely cleanup candidates.`}
          </p>
          <div style={{ display: "flex", gap: 8, alignItems: "flex-end", flexWrap: "wrap" }}>
            <label className="stack">
              Source (merged away)
              <select className="select" value={mergeSource} onChange={(e) => setMergeSource(e.target.value)}>
                <option value="">Select a category…</option>
                {cats.map((c) => (
                  <option key={c.id} value={c.id} disabled={c.id === mergeTarget}>
                    {c.name}
                    {!c.active ? " (inactive)" : ""}
                    {c.usage_count != null ? ` — ${c.usage_count} item(s)` : ""}
                  </option>
                ))}
              </select>
            </label>
            <label className="stack">
              Target (kept)
              <select className="select" value={mergeTarget} onChange={(e) => setMergeTarget(e.target.value)}>
                <option value="">Select a category…</option>
                {cats.map((c) => (
                  <option key={c.id} value={c.id} disabled={c.id === mergeSource}>
                    {c.name}
                    {!c.active ? " (inactive)" : ""}
                    {c.usage_count != null ? ` — ${c.usage_count} item(s)` : ""}
                  </option>
                ))}
              </select>
            </label>
            <button className="btn btn-primary" onClick={doMerge} disabled={!mergeSource || !mergeTarget}>
              Merge
            </button>
          </div>
        </div>
      )}

      <label className="stack" style={{ maxWidth: 320, marginBottom: 12 }}>
        Search categories
        <input
          className="input"
          type="search"
          placeholder="Filter by name…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </label>

      <table className="data-table" style={{ textAlign: "left", maxWidth: 720 }}>
        <thead>
          <tr>
            <th style={{ textAlign: "left" }}>Category</th>
            <th style={{ textAlign: "left" }}>Classification</th>
            <th style={{ textAlign: "left" }}>Line</th>
            <th style={{ textAlign: "left" }}>Usage</th>
          </tr>
        </thead>
        <tbody>
          {filteredCats.map((c) => {
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
                <td style={{ textAlign: "left" }}>
                  {c.usage_count === 0 ? (
                    <span className="muted">unused</span>
                  ) : c.usage_count != null ? (
                    c.usage_count
                  ) : (
                    "—"
                  )}
                </td>
              </tr>
            );
          })}
          {filteredCats.length === 0 && (
            <tr className="row-empty">
              <td colSpan={4}>No categories match “{search}”.</td>
            </tr>
          )}
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
