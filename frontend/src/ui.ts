// Small shared UI helpers so the views stay consistent without a CSS framework.
import type { CSSProperties } from "react";

export const fmtCurrency = (n: number) =>
  n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

export const fmtMonth = (iso: string) =>
  new Date(iso + "T00:00:00").toLocaleDateString("en-US", { year: "numeric", month: "short" });

export const card: CSSProperties = {
  border: "1px solid #ddd",
  borderRadius: 8,
  padding: "1rem 1.25rem",
  marginBottom: "1rem",
};

export const btn: CSSProperties = {
  padding: "6px 12px",
  borderRadius: 6,
  border: "1px solid #888",
  background: "#f6f6f6",
  cursor: "pointer",
};

export const btnPrimary: CSSProperties = {
  ...btn,
  border: "1px solid #2563eb",
  background: "#2563eb",
  color: "white",
};

export const input: CSSProperties = {
  padding: "6px 8px",
  borderRadius: 6,
  border: "1px solid #bbb",
};

export const STATUS_COLOR: Record<string, string> = {
  draft: "#9ca3af",
  posted: "#2563eb",
  locked: "#b91c1c",
};
