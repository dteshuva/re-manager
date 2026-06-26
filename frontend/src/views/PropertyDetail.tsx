import { Fragment, useEffect, useMemo, useState } from "react";
import {
  getPropertyMonthly,
  getPropertyUnitsMonthly,
  listProperties,
  type PeriodRange,
  type PnLMetrics,
  type Property,
  type PropertyMonthlyPnL,
  type UnitMonthlyPnL,
} from "../api";
import PeriodSelector from "../components/PeriodSelector";
import PnlTrendChart from "../components/PnlTrendChart";
import { fmtCurrency, fmtMonth, input } from "../ui";

// Property detail: the property's monthly P&L. For a multifamily property, click a month
// to see each unit's numbers for that month, plus the property-tier-only items (shared
// capex / debt service) that aren't allocated to units — so the month's property total can
// exceed the sum of its units, shown honestly.
export default function PropertyDetail({ token }: { token: string }) {
  const [properties, setProperties] = useState<Property[]>([]);
  const [propertyId, setPropertyId] = useState("");
  const [range, setRange] = useState<PeriodRange>({});
  const [allMonths, setAllMonths] = useState<string[]>([]);
  const [monthly, setMonthly] = useState<PropertyMonthlyPnL[]>([]);
  const [unitRows, setUnitRows] = useState<UnitMonthlyPnL[]>([]);
  const [openMonth, setOpenMonth] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const property = properties.find((p) => p.id === propertyId);
  const isMulti = property?.type === "multifamily";

  useEffect(() => {
    listProperties(token)
      .then((ps) => {
        setProperties(ps);
        setPropertyId((cur) => cur || (ps[0]?.id ?? ""));
      })
      .catch((e) => setError(e.message));
  }, [token]);

  useEffect(() => {
    if (!propertyId) return;
    getPropertyMonthly(token, propertyId)
      .then((r) => setAllMonths(r.map((x) => x.month)))
      .catch((e) => setError(e.message));
  }, [token, propertyId]);

  useEffect(() => {
    if (!propertyId) return;
    setOpenMonth(null);
    getPropertyMonthly(token, propertyId, range).then(setMonthly).catch((e) => setError(e.message));
    if (isMulti) {
      getPropertyUnitsMonthly(token, propertyId, range).then(setUnitRows).catch((e) => setError(e.message));
    } else {
      setUnitRows([]);
    }
  }, [token, propertyId, range.from, range.to, isMulti]);

  // unit rows grouped by month, sorted by unit number.
  const unitsByMonth = useMemo(() => {
    const m: Record<string, UnitMonthlyPnL[]> = {};
    for (const r of unitRows) (m[r.month] ??= []).push(r);
    for (const k of Object.keys(m)) m[k].sort((a, b) => a.unit_number.localeCompare(b.unit_number));
    return m;
  }, [unitRows]);

  return (
    <section>
      <h2>Property Detail</h2>
      {error && <p style={{ color: "crimson" }}>{error}</p>}

      <div style={{ marginBottom: 12 }}>
        <label>
          Property{" "}
          <select style={input} value={propertyId} onChange={(e) => setPropertyId(e.target.value)}>
            {properties.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} ({p.type})
              </option>
            ))}
          </select>
        </label>
      </div>

      <PeriodSelector key={propertyId} availableMonths={allMonths} onChange={setRange} />

      {monthly.length > 0 && (
        <>
          <h3>NOI &amp; Cash Flow trend</h3>
          <PnlTrendChart data={monthly} />
        </>
      )}

      <h3>Monthly P&amp;L</h3>
      {isMulti && (
        <p style={{ color: "#666", marginTop: -8 }}>
          Click a month to see each unit's rent &amp; expenses for that month.
        </p>
      )}
      <table cellPadding={6} style={{ borderCollapse: "collapse", width: "100%" }}>
        <thead>
          <tr style={{ textAlign: "right", borderBottom: "2px solid #333" }}>
            <th style={{ textAlign: "left" }}>Month</th>
            <th>Gross Rent</th>
            <th>Operating</th>
            <th>NOI</th>
            <th>Below-NOI</th>
            <th>Cash Flow</th>
          </tr>
        </thead>
        <tbody>
          {monthly.map((m) => {
            const isOpen = openMonth === m.month;
            return (
              <Fragment key={m.month}>
                <tr
                  onClick={() => isMulti && setOpenMonth(isOpen ? null : m.month)}
                  style={{
                    textAlign: "right",
                    borderBottom: "1px solid #ddd",
                    cursor: isMulti ? "pointer" : "default",
                    background: isOpen ? "#f3f6ff" : undefined,
                    fontWeight: isMulti ? 600 : undefined,
                  }}
                >
                  <td style={{ textAlign: "left" }}>
                    {isMulti && <span style={{ color: "#888" }}>{isOpen ? "▾ " : "▸ "}</span>}
                    {fmtMonth(m.month)}
                  </td>
                  <Cells m={m} />
                </tr>

                {isOpen && isMulti && (
                  <>
                    {(unitsByMonth[m.month] ?? []).map((u) => (
                      <tr key={m.month + u.unit_id} style={{ textAlign: "right", background: "#fafbff", borderBottom: "1px solid #eee" }}>
                        <td style={{ textAlign: "left", paddingLeft: 28, color: "#555" }}>
                          Unit {u.unit_number}
                          {u.label ? ` — ${u.label}` : ""}
                        </td>
                        <Cells m={u} />
                      </tr>
                    ))}
                    <tr style={{ textAlign: "right", background: "#fff7ed", fontStyle: "italic", borderBottom: "1px solid #ddd" }}>
                      <td style={{ textAlign: "left", paddingLeft: 28 }}>Property-tier only (not allocated to units)</td>
                      <Cells m={m.property_tier} />
                    </tr>
                  </>
                )}
              </Fragment>
            );
          })}
          {monthly.length === 0 && !error && (
            <tr>
              <td colSpan={6} style={{ color: "#888" }}>
                No data in this period.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
}

function Cells({ m }: { m: PnLMetrics }) {
  return (
    <>
      <td>{fmtCurrency(m.gross_rent)}</td>
      <td>{fmtCurrency(m.operating_expenses)}</td>
      <td>{fmtCurrency(m.noi)}</td>
      <td>{fmtCurrency(m.below_noi)}</td>
      <td style={{ color: m.cash_flow < 0 ? "crimson" : undefined }}>{fmtCurrency(m.cash_flow)}</td>
    </>
  );
}
