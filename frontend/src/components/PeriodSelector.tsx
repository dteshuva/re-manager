import { useEffect, useMemo, useState } from "react";
import type { PeriodRange } from "../api";

type Mode = "all" | "range" | "ytd" | "t12";

// Month-string helpers operating on "YYYY-MM".
const addMonths = (ym: string, delta: number): string => {
  const [y, m] = ym.split("-").map(Number);
  const d = new Date(y, m - 1 + delta, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
};
const toIso = (ym: string) => `${ym}-01`;
const currentYm = () => new Date().toISOString().slice(0, 7);

// Reusable period selector emitting a {from,to} range (inclusive month bounds).
// Modes: All, custom Range, YTD (Jan→asOf), and trailing-12 (asOf-11→asOf).
export default function PeriodSelector({
  availableMonths,
  onChange,
}: {
  availableMonths: string[]; // ISO month strings (YYYY-MM-01), sorted ascending
  onChange: (range: PeriodRange) => void;
}) {
  // Default the "as of" anchor to the latest month that actually has data.
  const latest = availableMonths.length
    ? availableMonths[availableMonths.length - 1].slice(0, 7)
    : currentYm();
  const earliest = availableMonths.length ? availableMonths[0].slice(0, 7) : currentYm();

  const [mode, setMode] = useState<Mode>("all");
  const [asOf, setAsOf] = useState(latest);
  const [from, setFrom] = useState(earliest);
  const [to, setTo] = useState(latest);

  const range = useMemo<PeriodRange>(() => {
    switch (mode) {
      case "range":
        return { from: toIso(from), to: toIso(to) };
      case "ytd":
        return { from: toIso(`${asOf.slice(0, 4)}-01`), to: toIso(asOf) };
      case "t12":
        return { from: toIso(addMonths(asOf, -11)), to: toIso(asOf) };
      case "all":
      default:
        return {};
    }
  }, [mode, asOf, from, to]);

  useEffect(() => {
    onChange(range);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range.from, range.to]);

  const modeBtn = (m: Mode, label: string) => (
    <button
      key={m}
      onClick={() => setMode(m)}
      className={`segmented__btn${mode === m ? " is-active" : ""}`}
    >
      {label}
    </button>
  );

  return (
    <div className="row" style={{ marginBottom: 18 }}>
      <div className="segmented">
        {modeBtn("all", "All")}
        {modeBtn("ytd", "YTD")}
        {modeBtn("t12", "T12")}
        {modeBtn("range", "Range")}
      </div>

      {mode === "range" && (
        <span className="row" style={{ gap: 6 }}>
          <input className="input" type="month" value={from} onChange={(e) => setFrom(e.target.value)} />
          <span className="muted">→</span>
          <input className="input" type="month" value={to} onChange={(e) => setTo(e.target.value)} />
        </span>
      )}

      {(mode === "ytd" || mode === "t12") && (
        <label className="row" style={{ gap: 6 }}>
          as of
          <input className="input" type="month" value={asOf} onChange={(e) => setAsOf(e.target.value)} />
        </label>
      )}
    </div>
  );
}
