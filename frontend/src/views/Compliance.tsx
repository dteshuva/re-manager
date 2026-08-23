import { useEffect, useMemo, useState } from "react";
import {
  addPropertyCertificate,
  deleteCertificate,
  getAllCertificates,
  getCertificateTypes,
  getMe,
  listProperties,
  updateCertificate,
  type Certificate,
  type CertificateInput,
  type Property,
} from "../api";
import PropertySearchSelect from "../components/PropertySearchSelect";
import { btn, btnPrimary, card, certStatusPillClass, fmtDate, fmtDaysToExpiry } from "../ui";

// Compliance tab: track statutory licensing / safety certificates (EICR, Gas Safety/CP12,
// EPC, HMO licence…) per property and flag any that have expired or are about to. Reads are
// open to any member; the add/edit/delete forms are admin-gated (the API enforces it too).
const EMPTY_FORM: CertificateInput = {
  cert_type: "",
  expiry_date: "",
  issue_date: "",
  reference: "",
  provider: "",
  notes: "",
};

export default function Compliance({ token }: { token: string }) {
  const [certs, setCerts] = useState<Certificate[]>([]);
  const [properties, setProperties] = useState<Property[]>([]);
  const [types, setTypes] = useState<string[]>([]);
  const [isAdmin, setIsAdmin] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // Add/edit form state. `editingId` null ⇒ the form creates a new cert; otherwise it edits.
  const [propertyId, setPropertyId] = useState("");
  const [form, setForm] = useState<CertificateInput>(EMPTY_FORM);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const reload = () =>
    getAllCertificates(token)
      .then(setCerts)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));

  useEffect(() => {
    listProperties(token).then(setProperties).catch((e) => setError(e.message));
    getCertificateTypes(token).then(setTypes).catch(() => setTypes([]));
    getMe(token).then((m) => setIsAdmin(m.role === "admin")).catch(() => setIsAdmin(false));
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const counts = useMemo(() => {
    const c = { expired: 0, expiring: 0, valid: 0 };
    for (const cert of certs) c[cert.status] += 1;
    return c;
  }, [certs]);

  const resetForm = () => {
    setEditingId(null);
    setPropertyId("");
    setForm(EMPTY_FORM);
  };

  const startEdit = (cert: Certificate) => {
    setEditingId(cert.id);
    setPropertyId(cert.property_id);
    setForm({
      cert_type: cert.cert_type,
      expiry_date: cert.expiry_date,
      issue_date: cert.issue_date ?? "",
      reference: cert.reference ?? "",
      provider: cert.provider ?? "",
      notes: cert.notes ?? "",
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const submit = () => {
    setError(null);
    if (!editingId && !propertyId) {
      setError("Select a property for the certificate.");
      return;
    }
    if (!form.cert_type.trim() || !form.expiry_date) {
      setError("Certificate type and expiry date are required.");
      return;
    }
    // Normalize empty strings back to null so the backend stores NULL, not "".
    const payload: CertificateInput = {
      cert_type: form.cert_type.trim(),
      expiry_date: form.expiry_date,
      issue_date: form.issue_date || null,
      reference: form.reference?.trim() || null,
      provider: form.provider?.trim() || null,
      notes: form.notes?.trim() || null,
    };
    setSaving(true);
    const op = editingId
      ? updateCertificate(token, editingId, payload)
      : addPropertyCertificate(token, propertyId, payload);
    op.then(() => {
      resetForm();
      return reload();
    })
      .catch((e) => setError(e.message))
      .finally(() => setSaving(false));
  };

  const remove = (cert: Certificate) => {
    if (!window.confirm(`Delete the ${cert.cert_type} certificate for ${cert.property_name}?`)) return;
    setError(null);
    deleteCertificate(token, cert.id)
      .then(() => reload())
      .catch((e) => setError(e.message));
  };

  const set = (k: keyof CertificateInput, v: string) => setForm({ ...form, [k]: v });

  return (
    <section>
      {error && <p className="alert-error">{error}</p>}

      {/* Status summary */}
      <div className="kpi-row" style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 18 }}>
        <SummaryCard label="Expired" value={counts.expired} tone="negative" />
        <SummaryCard label="Expiring soon" value={counts.expiring} tone="warn" />
        <SummaryCard label="Valid" value={counts.valid} tone="positive" />
      </div>

      {/* Add / edit form (admin only) */}
      {isAdmin && (
        <div style={{ ...card }}>
          <h3 style={{ marginTop: 0 }}>{editingId ? "Edit certificate" : "Add certificate"}</h3>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
            {!editingId && (
              <PropertySearchSelect
                properties={properties}
                value={propertyId}
                onChange={setPropertyId}
                placeholder="— select property —"
                wrapperStyle={{ minWidth: 220 }}
              />
            )}
            <label className="stack">
              <span>Certificate type</span>
              <input
                className="input"
                list="cert-type-presets"
                value={form.cert_type}
                onChange={(e) => set("cert_type", e.target.value)}
                placeholder="EICR, Gas Safety (CP12)…"
              />
              <datalist id="cert-type-presets">
                {types.map((t) => (
                  <option key={t} value={t} />
                ))}
              </datalist>
            </label>
            <label className="stack">
              <span>Expiry date *</span>
              <input className="input" type="date" value={form.expiry_date} onChange={(e) => set("expiry_date", e.target.value)} />
            </label>
            <label className="stack">
              <span>Issue date</span>
              <input className="input" type="date" value={form.issue_date ?? ""} onChange={(e) => set("issue_date", e.target.value)} />
            </label>
            <label className="stack">
              <span>Reference</span>
              <input className="input" value={form.reference ?? ""} onChange={(e) => set("reference", e.target.value)} placeholder="Cert. no." />
            </label>
            <label className="stack">
              <span>Provider</span>
              <input className="input" value={form.provider ?? ""} onChange={(e) => set("provider", e.target.value)} placeholder="Contractor" />
            </label>
            <label className="stack" style={{ minWidth: 200, flex: 1 }}>
              <span>Notes</span>
              <input className="input" value={form.notes ?? ""} onChange={(e) => set("notes", e.target.value)} />
            </label>
          </div>
          <div style={{ marginTop: 14, display: "flex", gap: 8 }}>
            <button style={btnPrimary} onClick={submit} disabled={saving}>
              {saving ? "Saving…" : editingId ? "Save changes" : "Add certificate"}
            </button>
            {editingId && (
              <button style={btn} onClick={resetForm} disabled={saving}>
                Cancel
              </button>
            )}
          </div>
        </div>
      )}

      {/* All certificates */}
      <div style={{ ...card }}>
        <h3 style={{ marginTop: 0 }}>All certificates</h3>
        {loading ? (
          <p className="hint">Loading…</p>
        ) : certs.length === 0 ? (
          <p className="hint" style={{ marginBottom: 0 }}>
            No certificates recorded yet.{isAdmin ? " Add one above." : ""}
          </p>
        ) : (
          <table className="data-table" style={{ textAlign: "left" }}>
            <thead>
              <tr>
                <th>Status</th>
                <th>Property</th>
                <th>Certificate</th>
                <th>Expiry</th>
                <th>Reference</th>
                <th>Provider</th>
                {isAdmin && <th></th>}
              </tr>
            </thead>
            <tbody>
              {certs.map((cert) => (
                <tr key={cert.id}>
                  <td>
                    <span className={certStatusPillClass(cert.status)}>{cert.status}</span>
                  </td>
                  <td>{cert.property_name}</td>
                  <td>{cert.cert_type}</td>
                  <td>
                    {fmtDate(cert.expiry_date)}
                    <span className="hint" style={{ display: "block", fontSize: 12 }}>
                      {fmtDaysToExpiry(cert.days_to_expiry)}
                    </span>
                  </td>
                  <td>{cert.reference ?? "—"}</td>
                  <td>{cert.provider ?? "—"}</td>
                  {isAdmin && (
                    <td style={{ whiteSpace: "nowrap" }}>
                      <button style={{ ...btn, padding: "4px 10px" }} onClick={() => startEdit(cert)}>
                        Edit
                      </button>{" "}
                      <button style={{ ...btn, padding: "4px 10px" }} onClick={() => remove(cert)}>
                        Delete
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}

function SummaryCard({ label, value, tone }: { label: string; value: number; tone: "negative" | "warn" | "positive" }) {
  const color = tone === "negative" ? "var(--negative)" : tone === "warn" ? "#9a3412" : "var(--positive)";
  return (
    <div style={{ ...card, marginBottom: 0, minWidth: 140, flex: "0 0 auto" }}>
      <div className="hint" style={{ marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 28, fontWeight: 650, color }}>{value}</div>
    </div>
  );
}
