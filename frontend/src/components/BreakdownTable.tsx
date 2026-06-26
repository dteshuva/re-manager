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
    <table cellPadding={6} style={{ borderCollapse: "collapse", width: "100%" }}>
      <thead>
        <tr style={{ textAlign: "right", borderBottom: "2px solid #333" }}>
          <th style={{ textAlign: "left" }}>Scope</th>
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
        <tr style={{ textAlign: "right", borderTop: "2px solid #333", fontWeight: 700 }}>
          <td style={{ textAlign: "left" }}>Portfolio total</td>
          {cells(data.total).map((v, i) => (
            <td key={i} style={{ color: i === 4 && v < 0 ? "crimson" : undefined }}>
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
  const row = (label: string, m: PnLMetrics, style?: React.CSSProperties, indent = 0) => (
    <tr style={{ textAlign: "right", ...style }}>
      <td style={{ textAlign: "left", paddingLeft: 8 + indent }}>{label}</td>
      {cells(m).map((v, i) => (
        <td key={i} style={{ color: i === 4 && v < 0 ? "crimson" : undefined }}>
          {fmtCurrency(v)}
        </td>
      ))}
    </tr>
  );

  return (
    <Fragment>
      <tr
        onClick={() => multi && onToggle()}
        style={{
          textAlign: "right",
          borderBottom: "1px solid #ddd",
          cursor: multi ? "pointer" : "default",
          fontWeight: 600,
          background: open ? "#f3f6ff" : undefined,
        }}
      >
        <td style={{ textAlign: "left" }}>
          {multi && <span style={{ color: "#888" }}>{open ? "▾ " : "▸ "}</span>}
          {p.property_name}
          <span style={{ color: "#888", fontWeight: 400 }}> ({p.type})</span>
        </td>
        {cells(p).map((v, i) => (
          <td key={i} style={{ color: i === 4 && v < 0 ? "crimson" : undefined }}>
            {fmtCurrency(v)}
          </td>
        ))}
      </tr>
      {open && multi && (
        <>
          {p.units.map((u) =>
            row(`Unit ${u.unit_number}`, u, { background: "#fafbff", borderBottom: "1px solid #eee" }, 28),
          )}
          {row(
            "Property-tier only (not allocated to units)",
            p.property_tier,
            { background: "#fff7ed", fontStyle: "italic", borderBottom: "1px solid #ddd" },
            28,
          )}
        </>
      )}
    </Fragment>
  );
}
