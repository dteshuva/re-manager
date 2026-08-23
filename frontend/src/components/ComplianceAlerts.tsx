import { useEffect, useState } from "react";
import { getCertificateAlerts, type Certificate } from "../api";
import { card, certStatusPillClass, fmtDate, fmtDaysToExpiry } from "../ui";

// Dashboard banner: the compliance certificates that have expired or are about to (backend
// /compliance/alerts). Renders nothing when everything is current, so it only ever draws
// attention when there's a real problem. Full management lives on the Compliance tab.
export default function ComplianceAlerts({ token }: { token: string }) {
  const [alerts, setAlerts] = useState<Certificate[] | null>(null);

  useEffect(() => {
    getCertificateAlerts(token)
      .then(setAlerts)
      .catch(() => setAlerts([]));
  }, [token]);

  if (!alerts || alerts.length === 0) return null;

  const expired = alerts.filter((a) => a.status === "expired").length;
  const expiring = alerts.length - expired;

  return (
    <div style={{ ...card, borderColor: "var(--negative)" }}>
      <h3 style={{ marginTop: 0, marginBottom: 4 }}>
        ⚠ Compliance: {expired > 0 && <strong>{expired} expired</strong>}
        {expired > 0 && expiring > 0 && ", "}
        {expiring > 0 && <span>{expiring} expiring soon</span>}
      </h3>
      <p className="hint" style={{ marginTop: 0 }}>
        Statutory certificates needing attention. Manage them on the Compliance tab.
      </p>
      <table className="data-table" style={{ textAlign: "left" }}>
        <thead>
          <tr>
            <th>Status</th>
            <th>Property</th>
            <th>Certificate</th>
            <th>Expiry</th>
          </tr>
        </thead>
        <tbody>
          {alerts.slice(0, 8).map((a) => (
            <tr key={a.id}>
              <td>
                <span className={certStatusPillClass(a.status)}>{a.status}</span>
              </td>
              <td>{a.property_name}</td>
              <td>{a.cert_type}</td>
              <td>
                {fmtDate(a.expiry_date)}
                <span className="hint" style={{ display: "block", fontSize: 12 }}>
                  {fmtDaysToExpiry(a.days_to_expiry)}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {alerts.length > 8 && (
        <p className="hint" style={{ marginBottom: 0 }}>+{alerts.length - 8} more on the Compliance tab.</p>
      )}
    </div>
  );
}
