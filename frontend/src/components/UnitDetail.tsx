import { useEffect, useState } from "react";
import { getUnitDetail, type UnitDetail as UnitDetailData } from "../api";
import PnlTrendChart from "./PnlTrendChart";
import { t } from "../terms";
import { fmtCurrency, fmtMonth, leaseStatusPillClass, statusPillClass } from "../ui";

// Level 3 — Unit detail, drilled into from the property's unit roster or attention feed.
// Shows the unit's month-by-month P&L (unit-specific items only) + a T12 trend, with an
// explicit reminder that property-level shared costs are not allocated here.
export default function UnitDetail({
  token,
  unitId,
  onClose,
}: {
  token: string;
  unitId: string;
  onClose: () => void;
}) {
  const [d, setD] = useState<UnitDetailData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setD(null);
    getUnitDetail(token, unitId).then(setD).catch((e) => setError(e.message));
  }, [token, unitId]);

  if (error) return <p className="alert-error">{error}</p>;
  if (!d) return null;

  const t12 = d.months.slice(-12);

  return (
    <div className="unit-detail">
      <div className="unit-detail__head">
        <div>
          <span className="unit-detail__title">
            Unit {d.unit_number}
            {d.label ? ` — ${d.label}` : ""}
          </span>
          <span className={statusPillClass(d.status)} style={{ marginLeft: 10 }}>
            {d.status}
          </span>
          {d.lease_status && (
            <span
              className={leaseStatusPillClass(d.lease_status)}
              style={{ marginLeft: 6 }}
              title={`Current ${t("lease")} state (today)`}
            >
              {`${t("lease")}: ${d.lease_status}`}
            </span>
          )}
          <span className="muted" style={{ marginLeft: 10, fontSize: 13 }}>{d.property_name}</span>
        </div>
        <button className="btn" onClick={onClose}>✕ Close</button>
      </div>

      <p className="hint">
        Property-level shared costs (capex, debt service) are recorded at the property tier and
        are <strong>not allocated</strong> to this unit — so unit cash flow is not a pro-rata
        share of property cash flow. The status pill above is this unit's most recent monthly
        {`record; the "${t("lease")}" pill is its current ${t("lease")} state as of today (see the Rent `}
        {`Roll tab for full ${t("lease")} detail) — the two are independent and can disagree.`}
        {d.lease_tenant_name && <> Current tenant on file: <strong>{d.lease_tenant_name}</strong>.</>}
      </p>

      {t12.length > 0 && <PnlTrendChart data={t12} height={220} />}

      <table className="data-table" style={{ marginTop: 12 }}>
        <thead>
          <tr>
            <th>Month</th>
            <th>Rent</th>
            <th>Operating</th>
            <th>Capex</th>
            <th>NOI</th>
            <th>Cash Flow</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {d.months.map((m) => (
            <tr key={m.month} className={m.status !== "occupied" ? "row-muted" : undefined}>
              <td>{fmtMonth(m.month)}</td>
              <td>{fmtCurrency(m.gross_rent)}</td>
              <td>{fmtCurrency(m.operating_expenses)}</td>
              <td>{fmtCurrency(m.capex)}</td>
              <td>{fmtCurrency(m.noi)}</td>
              <td className={m.cash_flow < 0 ? "value-negative" : undefined}>{fmtCurrency(m.cash_flow)}</td>
              <td>
                <span className={statusPillClass(m.status)}>{m.status}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
