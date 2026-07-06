import { useEffect, useMemo, useState } from "react";
import type { PeriodRange } from "../api";

type Mode = "month" | "ytd" | "t12" | "range" | "all";

// Month-string helpers operating on "YYYY-MM".
const addMonths = (ym: string, delta: number): string => {
  const [y, m] = ym.split("-").map(Number);
  const d = new Date(y, m - 1 + delta, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
};
const toIso = (ym: string) => `${ym}-01`;
const currentYm = () => new Date().toISOString().slice(0, 7);

// Reusable period selector emitting a {from,to} range (inclusive month bounds). Every mode
// resolves to explicit bounds (incl. All = earliest→latest) so callers always have a concrete
// period and end-month. Modes: single Month, YTD (Jan→asOf), trailing-12, custom Range, All.
export default function PeriodSelector({
  availableMonths,
  onChange,
  defaultMode = "all",
}: {
  availableMonths: string[]; // ISO month strings (YYYY-MM-01), sorted ascending
  onChange: (range: PeriodRange) => void;
  defaultMode?: Mode;
}) {
  const latest = availableMonths.length
    ? availableMonths[availableMonths.length - 1].slice(0, 7)
    : currentYm();
  const earliest = availableMonths.length ? availableMonths[0].slice(0, 7) : currentYm();

  const [mode, setMode] = useState<Mode>(defaultMode);
  const [asOf, setAsOf] = useState(latest);
  const [from, setFrom] = useState(earliest);
  const [to, setTo] = useState(latest);

  // availableMonths loads async; once the real data range arrives, snap the pickers to it
  // (so e.g. Month defaults to the latest data month, not today's calendar month).
  useEffect(() => {
    setAsOf(latest);
    setFrom(earliest);
    setTo(latest);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [earliest, latest]);

  const range = useMemo<PeriodRange>(() => {
    switch (mode) {
      case "month":
        return { from: toIso(asOf), to: toIso(asOf) };
      case "range":
        return { from: toIso(from), to: toIso(to) };
      case "ytd":
        return { from: toIso(`${asOf.slice(0, 4)}-01`), to: toIso(asOf) };
      case "t12":
        return { from: toIso(addMonths(asOf, -11)), to: toIso(asOf) };
      case "all":
      default:
        return { from: toIso(earliest), to: toIso(latest) };
    }
  }, [mode, asOf, from, to, earliest, latest]);

  // A <input type="month"> reports every keystroke, so mid-typing a year yields partial
  // values like "0002-06" before "2024-06". Emitting those fires requests for nonsense
  // periods (and the backend 500s computing a prior period for year 2). Only propagate once
  // both bounds carry a full 4-digit year.
  const yearOk = (iso?: string) => !iso || Number(iso.slice(0, 4)) >= 1000;
  useEffect(() => {
    if (yearOk(range.from) && yearOk(range.to)) onChange(range);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range.from, range.to]);

  // Step the as-of month by ±1, clamped to the available data range (ym strings sort lexically).
  const stepAsOf = (delta: number) => {
    const next = addMonths(asOf, delta);
    if (next < earliest || next > latest) return;
    setAsOf(next);
  };
  const atStart = asOf <= earliest;
  const atEnd = asOf >= latest;

  // Shift the whole custom range window by ±1 month, keeping its width, clamped to the data range.
  const stepRange = (delta: number) => {
    const nextFrom = addMonths(from, delta);
    const nextTo = addMonths(to, delta);
    if (nextFrom < earliest || nextTo > latest) return;
    setFrom(nextFrom);
    setTo(nextTo);
  };
  const rangeAtStart = from <= earliest;
  const rangeAtEnd = to >= latest;

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
        {modeBtn("month", "Month")}
        {modeBtn("ytd", "YTD")}
        {modeBtn("t12", "T12")}
        {modeBtn("range", "Range")}
        {modeBtn("all", "All")}
      </div>

      {mode === "range" && (
        <span className="row" style={{ gap: 6 }}>
          <button
            className="btn btn-ghost"
            style={{ padding: "8px 10px" }}
            onClick={() => stepRange(-1)}
            disabled={rangeAtStart}
            aria-label="Shift range back one month"
            title="Shift range back one month"
          >
            ‹
          </button>
          <input className="input" type="month" value={from} onChange={(e) => setFrom(e.target.value)} />
          <span className="muted">→</span>
          <input className="input" type="month" value={to} onChange={(e) => setTo(e.target.value)} />
          <button
            className="btn btn-ghost"
            style={{ padding: "8px 10px" }}
            onClick={() => stepRange(1)}
            disabled={rangeAtEnd}
            aria-label="Shift range forward one month"
            title="Shift range forward one month"
          >
            ›
          </button>
        </span>
      )}

      {(mode === "month" || mode === "ytd" || mode === "t12") && (
        <label className="row" style={{ gap: 6 }}>
          {mode === "month" ? "month" : "as of"}
          <span className="row" style={{ gap: 4 }}>
            <button
              className="btn btn-ghost"
              style={{ padding: "8px 10px" }}
              onClick={() => stepAsOf(-1)}
              disabled={atStart}
              aria-label="Previous month"
              title="Previous month"
            >
              ‹
            </button>
            <input className="input" type="month" value={asOf} onChange={(e) => setAsOf(e.target.value)} />
            <button
              className="btn btn-ghost"
              style={{ padding: "8px 10px" }}
              onClick={() => stepAsOf(1)}
              disabled={atEnd}
              aria-label="Next month"
              title="Next month"
            >
              ›
            </button>
          </span>
        </label>
      )}
    </div>
  );
}
