import { useState } from "react";
import type { CategoryAmount, Classification } from "../api";
import { CHART, fmtCurrency } from "../ui";

// What the headline figures are actually MADE OF.
//
// A single "Operating Expenses: £412" tells an owner that money left the building and nothing
// about whether it was one boiler or twelve small bills — which is the whole difference between
// a month worth investigating and a month to ignore. The same was true of the old single
// "Below-NOI" column, which mixes a mortgage with a roof: two numbers that mean completely
// different things to whoever has to act on them.
//
// So each expense section lists its categories largest first, with each one's share of its own
// section. Shares are of the SECTION, not of the property — "41% of your operating spend" is a
// sentence an owner can act on; "6% of everything" is not.

const GROUPS: { key: Classification; label: string; hint: string; color: string }[] = [
  {
    key: "operating",
    label: "Operating expenses",
    hint: "Running the property. Subtracted from rent to reach NOI.",
    color: CHART.opex,
  },
  {
    key: "debt_service",
    label: "Debt service",
    hint: "Mortgage and loan payments. Below NOI.",
    color: CHART.debtService,
  },
  {
    key: "capex",
    label: "Capital expenditure",
    hint: "Improvements with a life beyond this year. Below NOI.",
    color: CHART.capex,
  },
  {
    key: "other_below_line",
    label: "Other below NOI",
    hint: "Owner distributions and anything else outside operations.",
    color: CHART.axis,
  },
];

export default function CategoryComposition({ rows }: { rows: CategoryAmount[] }) {
  // Collapsed by default for everything except operating expenses: the question people arrive
  // with is almost always "what is the opex", and three more open tables would bury it.
  const [open, setOpen] = useState<Record<string, boolean>>({ operating: true });

  const present = GROUPS.map((g) => ({
    ...g,
    rows: rows
      .filter((r) => r.classification === g.key && r.amount !== 0)
      .sort((a, b) => Math.abs(b.amount) - Math.abs(a.amount)),
  })).filter((g) => g.rows.length > 0);

  if (present.length === 0) {
    return <p className="hint">No expenses posted in this period.</p>;
  }

  return (
    <div style={{ display: "grid", gap: 12 }}>
      {present.map((g) => {
        const total = g.rows.reduce((a, r) => a + r.amount, 0);
        const largest = Math.max(...g.rows.map((r) => Math.abs(r.amount)), 1);
        const isOpen = open[g.key] ?? false;
        return (
          <div key={g.key}>
            <button
              type="button"
              onClick={() => setOpen((o) => ({ ...o, [g.key]: !isOpen }))}
              aria-expanded={isOpen}
              style={{
                display: "flex",
                alignItems: "baseline",
                gap: 8,
                width: "100%",
                background: "none",
                border: "none",
                padding: "4px 0",
                cursor: "pointer",
                textAlign: "left",
                font: "inherit",
                color: "inherit",
              }}
            >
              <span className="caret" aria-hidden="true">{isOpen ? "▾" : "▸"}</span>
              <strong>{g.label}</strong>
              <span style={{ marginLeft: "auto", fontWeight: 600 }}>{fmtCurrency(total)}</span>
              <span className="muted" style={{ fontSize: 12 }}>
                {g.rows.length} {g.rows.length === 1 ? "category" : "categories"}
              </span>
            </button>
            {isOpen && (
              <>
                <p className="hint" style={{ margin: "2px 0 6px" }}>{g.hint}</p>
                <table className="data-table" style={{ textAlign: "left" }}>
                  <tbody>
                    {g.rows.map((r) => {
                      const share = total === 0 ? 0 : (r.amount / total) * 100;
                      return (
                        <tr key={r.category_id}>
                          <td style={{ textAlign: "left", width: "40%" }}>{r.category}</td>
                          <td style={{ width: "35%" }}>
                            {/* Width is relative to the section's LARGEST line, not to its
                                total, so the smaller items stay visible instead of collapsing
                                into slivers against one dominant charge. */}
                            <div
                              aria-hidden="true"
                              style={{
                                height: 8,
                                borderRadius: 4,
                                background: g.color,
                                opacity: 0.85,
                                width: `${Math.max((Math.abs(r.amount) / largest) * 100, 2)}%`,
                              }}
                            />
                          </td>
                          <td style={{ textAlign: "right" }}>{fmtCurrency(r.amount)}</td>
                          <td style={{ textAlign: "right", width: 70 }} className="muted">
                            {share.toFixed(0)}%
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </>
            )}
          </div>
        );
      })}
    </div>
  );
}
