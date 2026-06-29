// Small shared UI helpers so the views stay consistent.
// The visual design system lives in styles.css (tokens + component classes);
// these inline-style objects mirror it for views that haven't moved to classes yet.
import type { CSSProperties } from "react";

export const fmtCurrency = (n: number) =>
  n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

export const fmtMonth = (iso: string) =>
  new Date(iso + "T00:00:00").toLocaleDateString("en-US", { year: "numeric", month: "short" });

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
