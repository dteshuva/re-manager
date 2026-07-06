import { useEffect, useState } from "react";
import {
  getAttentionSettings,
  getMe,
  updateAttentionSettings,
  type AttentionThresholds,
} from "../api";
import { btnPrimary, card } from "../ui";

// Sub-step 6: edit the attention-feed thresholds (per account). Read by anyone; saved by
// admins only (the API enforces it). Changes take effect on the next dashboard feed load.
// The % is the primary, size-independent trigger; the $ floor is an optional materiality
// gate (0 = pure percentage).
const FIELDS: { key: keyof AttentionThresholds; label: string; unit: "$" | "%" | "pp" }[] = [
  { key: "noi_drop_min_pct", label: "NOI drop — min %", unit: "%" },
  { key: "noi_drop_min_abs", label: "NOI drop — $ gate (0 = off)", unit: "$" },
  { key: "expense_spike_min_pct", label: "Expense spike — min %", unit: "%" },
  { key: "expense_spike_min_abs", label: "Expense spike — $ gate (0 = off)", unit: "$" },
  { key: "vacancy_min_occupancy_drop_pct", label: "Vacancy — min occupancy drop", unit: "pp" },
  { key: "vacancy_high_absolute_pct", label: "High vacancy — flag at/above", unit: "%" },
  { key: "unit_noi_drop_min_pct", label: "Unit NOI drop — min %", unit: "%" },
  { key: "unit_noi_drop_min_abs", label: "Unit NOI drop — $ gate (0 = off)", unit: "$" },
  { key: "unit_expense_spike_min_pct", label: "Unit expense spike — min %", unit: "%" },
  { key: "unit_expense_spike_min_abs", label: "Unit expense spike — $ gate (0 = off)", unit: "$" },
];

export default function AttentionSettings({ token }: { token: string }) {
  const [values, setValues] = useState<AttentionThresholds | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getAttentionSettings(token)
      .then((s) => {
        const { updated_at: _omit, ...t } = s;
        setValues(t);
      })
      .catch((e) => setError(e.message));
    getMe(token).then((m) => setIsAdmin(m.role === "admin")).catch(() => setIsAdmin(false));
  }, [token]);

  if (!values) return null;

  const set = (k: keyof AttentionThresholds, v: string) =>
    setValues({ ...values, [k]: Number(v) });

  const save = () => {
    setStatus(null);
    setError(null);
    updateAttentionSettings(token, values)
      .then(() => setStatus("Saved. New thresholds apply on the next dashboard load."))
      .catch((e) => setError(e.message));
  };

  return (
    <div style={{ ...card, maxWidth: 640 }}>
      <p className="hint" style={{ marginTop: 0 }}>
        The <strong>%</strong> is the primary trigger — it scales with property size, so a big
        relative drop flags whether the property is small or large. The <strong>$ gate</strong> is
        an optional materiality floor (0 = pure percentage). Vacancy flags on an occupancy drop in
        percentage points. {isAdmin ? "" : "Read-only (admin required to edit)."}
      </p>
      <div className="settings-grid">
        {FIELDS.map((f) => (
          <label key={f.key} className="settings-field">
            <span>{f.label}</span>
            <span className="settings-input">
              {f.unit === "$" && <span className="settings-affix">$</span>}
              <input
                className="input"
                type="number"
                min={0}
                step={f.unit === "$" ? 100 : f.unit === "pp" ? 0.5 : 1}
                value={values[f.key]}
                disabled={!isAdmin}
                onChange={(e) => set(f.key, e.target.value)}
              />
              {f.unit === "%" && <span className="settings-affix">%</span>}
              {f.unit === "pp" && <span className="settings-affix">pp</span>}
            </span>
          </label>
        ))}
      </div>
      {isAdmin && (
        <button style={{ ...btnPrimary, marginTop: 14 }} onClick={save}>
          Save thresholds
        </button>
      )}
      {status && <p className="hint" style={{ color: "var(--positive)", marginBottom: 0 }}>{status}</p>}
      {error && <p className="alert-error" style={{ marginBottom: 0 }}>{error}</p>}
    </div>
  );
}
