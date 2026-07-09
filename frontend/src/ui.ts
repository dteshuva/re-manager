// Small shared UI helpers so the views stay consistent.
// The visual design system lives in styles.css (tokens + component classes);
// these inline-style objects mirror it for views that haven't moved to classes yet.
import type { CSSProperties } from "react";

export const fmtCurrency = (n: number) =>
  n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

export const fmtMonth = (iso: string) =>
  new Date(iso + "T00:00:00").toLocaleDateString("en-US", { year: "numeric", month: "short" });

// Full local date + time for timestamped rows (e.g. audit log entries).
export const fmtDateTime = (iso: string) =>
  new Date(iso).toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short" });

// A fraction (0.062) as a percent ("6.2%"). For rates like cap rate / cash-on-cash.
export const fmtPct = (frac: number, digits = 1) => `${(frac * 100).toFixed(digits)}%`;

// The dashboard endpoints auto-compute "prior period" as the immediately preceding period
// of equal length — which lands exactly one calendar year back whenever the selected span
// is exactly 12 months (a full-year Range, a T12 window, or any 12-month custom span).
// That's a genuine year-over-year comparison; this just detects it so the UI can label it
// as such instead of a generic "vs prior period" (the capability already existed
// server-side, it just wasn't surfaced).
export function isYoyComparison(
  from: string | null | undefined,
  to: string | null | undefined,
  priorFrom: string | null | undefined,
  priorTo: string | null | undefined,
): boolean {
  if (!from || !to || !priorFrom || !priorTo) return false;
  const monthsBetween = (a: string, b: string) => {
    const [ay, am] = a.slice(0, 7).split("-").map(Number);
    const [by, bm] = b.slice(0, 7).split("-").map(Number);
    return (by - ay) * 12 + (bm - am);
  };
  return monthsBetween(priorFrom, from) === 12 && monthsBetween(priorTo, to) === 12;
}

export const card: CSSProperties = {
  background: "var(--surface)",
  border: "1px solid var(--border)",
  borderRadius: "var(--radius)",
  boxShadow: "var(--shadow-sm)",
  padding: "16px 18px",
  marginBottom: "18px",
};

export const btn: CSSProperties = {
  padding: "8px 14px",
  borderRadius: "var(--radius-sm)",
  border: "1px solid var(--border-strong)",
  background: "var(--surface)",
  color: "var(--ink)",
  fontWeight: 550,
  cursor: "pointer",
};

export const btnPrimary: CSSProperties = {
  ...btn,
  border: "1px solid var(--accent)",
  background: "var(--accent)",
  color: "#fff",
};

export const input: CSSProperties = {
  padding: "8px 11px",
  borderRadius: "var(--radius-sm)",
  border: "1px solid var(--border-strong)",
  background: "var(--surface)",
  color: "var(--ink)",
};

export const STATUS_COLOR: Record<string, string> = {
  draft: "#64748b",
  posted: "#2563eb",
  locked: "#d92d20",
};

// Chart palette (kept consistent across all Recharts views).
export const CHART = {
  noi: "#2563eb",
  cashFlow: "#15803d",
  rent: "#2563eb",
  opex: "#f0631e",
  grid: "#eef1f5",
  axis: "#8a95a8",
};
