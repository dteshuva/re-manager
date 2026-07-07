import { useEffect, useState } from "react";
import { getPortfolioInvestment, type InvestmentMetrics, type PortfolioInvestment } from "../api";
import { fmtCurrency, fmtMonth, fmtPct } from "../ui";

// Portfolio-level investment comparison: every property that has acquisition data, with its
// return metrics, plus value-weighted portfolio aggregates (Σ NOI / Σ price, Σ cash flow /
// Σ equity — never a mean of percentages). Editing inputs happens on the Property tab.
const pct = (v: number | null) => (v == null ? "—" : fmtPct(v));
const dscr = (v: number | null) => (v == null ? "—" : `${v.toFixed(2)}×`);

export default function Investments({ token }: { token: string }) {
  const [data, setData] = useState<PortfolioInvestment | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getPortfolioInvestment(token).then(setData).catch((e) => setError(e.message));
  }, [token]);

  if (error) return <p className="alert-error">{error}</p>;
  if (!data) return <p className="hint">Loading…</p>;

  if (data.properties.length === 0) {
    return (
      <p className="hint">
        No properties have acquisition data yet. Open the <strong>Property</strong> tab, pick a property, and add its
        purchase price, financing and date under “Investment returns.”
      </p>
    );
  }

  const aggregates = [
    { label: "Portfolio cap rate", value: pct(data.cap_rate), caption: `${data.cap_rate_property_count} properties · value-weighted` },
    { label: "Portfolio cash-on-cash", value: pct(data.cash_on_cash), caption: `${data.cash_on_cash_property_count} with 12+ mo · equity-weighted` },
    { label: "Portfolio DSCR", value: dscr(data.dscr), caption: `${data.dscr_property_count} with debt` },
    { label: "Total invested", value: fmtCurrency(data.total_equity_invested), caption: `equity · ${fmtCurrency(data.total_purchase_price)} price` },
  ];

  return (
    <section>
      <div className="kpi-grid">
        {aggregates.map((a) => (
          <div className="kpi-card" key={a.label}>
            <div className="kpi-card__label">{a.label}</div>
            <div className="kpi-card__value">{a.value}</div>
            <div className="kpi-card__delta is-flat">{a.caption}</div>
          </div>
        ))}
      </div>

      <h3 className="section-title">By property</h3>
      <p className="hint">
        Cap rate = NOI ÷ purchase price (yield on cost). Cash-on-cash and average need 12 / 24 months of posted data;
        “—” means not enough history yet. Edit inputs on the Property tab.
      </p>
      <div style={{ overflowX: "auto" }}>
        <table className="data-table">
          <thead>
            <tr>
              <th>Property</th>
              <th>Bought</th>
              <th>Price</th>
              <th>Equity</th>
              <th>Cap rate</th>
              <th>Cash-on-cash</th>
              <th>DSCR</th>
              <th>Avg CoC</th>
              <th>Data</th>
            </tr>
          </thead>
          <tbody>
            {data.properties.map((p) => (
              <Row key={p.property_id} p={p} />
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Row({ p }: { p: InvestmentMetrics }) {
  return (
    <tr>
      <td>{p.property_name}</td>
      <td>{p.purchase_date ? fmtMonth(p.purchase_date) : "—"}</td>
      <td>{fmtCurrency(p.purchase_price ?? 0)}</td>
      <td>{fmtCurrency(p.equity_invested ?? 0)}</td>
      <td>
        {pct(p.cap_rate)}
        {p.annualized && p.cap_rate != null && <span className="muted" style={{ fontSize: 11 }}> (ann.)</span>}
      </td>
      <td>{pct(p.cash_on_cash)}</td>
      <td>{dscr(p.dscr)}</td>
      <td>{pct(p.avg_cash_on_cash)}</td>
      <td className="muted">{p.months_available} mo</td>
    </tr>
  );
}
