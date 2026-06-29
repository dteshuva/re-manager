import { Fragment, useState } from "react";
import type { PnLMetrics, PortfolioBreakdown, PropertyBreakdown } from "../api";
import { fmtCurrency } from "../ui";

const cells = (m: PnLMetrics) => [m.gross_rent, m.operating_expenses, m.noi, m.below_noi, m.cash_flow];

// Portfolio → property → unit breakdown for the selected period. Property rows are
// clickable (multifamily) to reveal each unit plus the property-tier-only items that are
// NOT allocated to units — so a property's total deliberately differs from the sum of its
// units, shown honestly rather than blended.
export default function BreakdownTable({ data }: { data: PortfolioBreakdown }) {
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const toggle = (id: string) => setOpen((o) => ({ ...o, [id]: !o[id] }));

  return (
    <table className="data-table">
      <thead>
        <tr>
          <th>Scope</th>
          <th>Gross Rent</th>
          <th>Operating</th>
          <th>NOI</th>
          <th>Below-NOI</th>
          <th>Cash Flow</th>
        </tr>
      </thead>
      <tbody>
        {data.properties.map((p) => (
          <PropertyRows key={p.property_id} p={p} open={!!open[p.property_id]} onToggle={() => toggle(p.property_id)} />
        ))}
        <tr className="row-total">
          <td>Portfolio total</td>
          {cells(data.total).map((v, i) => (
            <td key={i} className={i === 4 && v < 0 ? "value-negative" : undefined}>
              {fmtCurrency(v)}
            </td>
          ))}
        </tr>
      </tbody>
    </table>
  );
}

function PropertyRows({ p, open, onToggle }: { p: PropertyBreakdown; open: boolean; onToggle: () => void }) {
  const multi = p.type === "multifamily";
  const row = (label: string, m: PnLMetrics, rowClass: string) => (
    <tr className={rowClass}>
      <td>{label}</td>
      {cells(m).map((v, i) => (
        <td key={i} className={i === 4 && v < 0 ? "value-negative" : undefined}>
          {fmtCurrency(v)}
        </td>
      ))}
    </tr>
  );

  return (
    <Fragment>
      <tr
        onClick={() => multi && onToggle()}
        className={`row-strong${multi ? " is-clickable" : ""}${open ? " row-open" : ""}`}
      >
        <td>
          {multi && <span className="caret">{open ? "▾" : "▸"}</span>}
          {p.property_name}
          <span className="muted" style={{ fontWeight: 400 }}> ({p.type})</span>
        </td>
        {cells(p).map((v, i) => (
          <td key={i} className={i === 4 && v < 0 ? "value-negative" : undefined}>
            {fmtCurrency(v)}
          </td>
        ))}
      </tr>
      {open && multi && (
        <>
          {p.units.map((u) => row(`Unit ${u.unit_number}`, u, "row-sub"))}
          {row("Property-tier only (not allocated to units)", p.property_tier, "row-tier")}
        </>
      )}
    </Fragment>
  );
}
