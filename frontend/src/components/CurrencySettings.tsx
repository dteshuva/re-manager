import { useEffect, useState } from "react";
import { getGeneralSettings, getMe, updateGeneralSettings } from "../api";
import { btnPrimary, card, setActiveCurrency } from "../ui";

// Account-wide display currency (backend migration 0019). A pure symbol/locale toggle — no
// stored amount is converted. Read by anyone; saved by admins only (the API enforces it).
// Because fmtCurrency reads a module-level value (not React state), a save reloads the page so
// every already-rendered figure re-formats with the new symbol.
const OPTIONS: { code: "USD" | "GBP"; label: string }[] = [
  { code: "USD", label: "US Dollar ($)" },
  { code: "GBP", label: "Pound Sterling (£)" },
];

export default function CurrencySettings({ token }: { token: string }) {
  const [currency, setCurrency] = useState<"USD" | "GBP">("USD");
  const [isAdmin, setIsAdmin] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getGeneralSettings(token)
      .then((s) => setCurrency(s.currency))
      .catch((e) => setError(e.message));
    getMe(token).then((m) => setIsAdmin(m.role === "admin")).catch(() => setIsAdmin(false));
  }, [token]);

  const save = () => {
    setStatus(null);
    setError(null);
    updateGeneralSettings(token, { currency })
      .then(() => {
        setActiveCurrency(currency);
        // Re-render every figure already on screen with the new symbol.
        window.location.reload();
      })
      .catch((e) => setError(e.message));
  };

  return (
    <div style={{ ...card, maxWidth: 640 }}>
      <p className="hint" style={{ marginTop: 0 }}>
        The currency used to display every figure across the app. This changes the{" "}
        <strong>symbol and formatting only</strong> — no stored amounts are converted.{" "}
        {isAdmin ? "" : "Read-only (admin required to edit)."}
      </p>
      <label className="settings-field" style={{ maxWidth: 320 }}>
        <span>Display currency</span>
        <select
          className="input"
          value={currency}
          disabled={!isAdmin}
          onChange={(e) => setCurrency(e.target.value as "USD" | "GBP")}
        >
          {OPTIONS.map((o) => (
            <option key={o.code} value={o.code}>
              {o.label}
            </option>
          ))}
        </select>
      </label>
      {isAdmin && (
        <button style={{ ...btnPrimary, marginTop: 14 }} onClick={save}>
          Save currency
        </button>
      )}
      {status && <p className="hint" style={{ color: "var(--positive)", marginBottom: 0 }}>{status}</p>}
      {error && <p className="alert-error" style={{ marginBottom: 0 }}>{error}</p>}
    </div>
  );
}
