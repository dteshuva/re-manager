// Small shared UI helpers so the views stay consistent.
// The visual design system lives in styles.css (tokens + component classes);
// these inline-style objects mirror it for views that haven't moved to classes yet.
import type { CSSProperties } from "react";

// Account display currency. It's an account-wide setting (see backend migration 0019), so a
// module-level value that App sets once from `/auth/me` lets every fmtCurrency call site stay
// unchanged — no prop drilling / context through the ~13 views that format money. USD → $,
// GBP → £; the locale switches with it so grouping/placement read natively.
type CurrencyCode = "USD" | "GBP";
const CURRENCY_LOCALE: Record<CurrencyCode, string> = { USD: "en-US", GBP: "en-GB" };
const CURRENCY_SYMBOL: Record<CurrencyCode, string> = { USD: "$", GBP: "£" };
let activeCurrency: CurrencyCode = "USD";

export const setActiveCurrency = (code: string) => {
  activeCurrency = code === "GBP" ? "GBP" : "USD";
};

// The account's currency is also what tells us which country's conventions to use — for dates
// below, and for the UK/US vocabulary in `terms.ts`. There is no separate region setting and
// there doesn't need to be: an account keeping its books in £ is letting property in Britain.
export const activeLocale = () => CURRENCY_LOCALE[activeCurrency];
export const isUK = () => activeCurrency === "GBP";

// The bare symbol, for labels that name a unit rather than format a figure ("Discount (£/mo)").
// Anything that formats an actual amount should use `fmtCurrency`, not this.
export const currencySymbol = () => CURRENCY_SYMBOL[activeCurrency];

export const fmtCurrency = (n: number) =>
  n.toLocaleString(CURRENCY_LOCALE[activeCurrency], {
    style: "currency",
    currency: activeCurrency,
    maximumFractionDigits: 0,
  });

// Dates follow the account's locale for the same reason money does. This is NOT cosmetic:
// en-US renders 2026-09-08 as "9/8/2026" and en-GB as "08/09/2026" — day-first versus
// month-first, the same ten characters meaning two different days. A UK landlord reading a
// US-formatted tenancy start date or certificate expiry is being actively misinformed, and
// their own agent statements are day-first (see app/parsing.py), so the app has to match.
export const fmtMonth = (iso: string) =>
  new Date(iso + "T00:00:00").toLocaleDateString(activeLocale(), { year: "numeric", month: "short" });

// Full local date + time for timestamped rows (e.g. audit log entries).
export const fmtDateTime = (iso: string) =>
  new Date(iso).toLocaleString(activeLocale(), { dateStyle: "medium", timeStyle: "short" });

// A fraction (0.062) as a percent ("6.2%"). For rates like cap rate / cash-on-cash.
export const fmtPct = (frac: number, digits = 1) => `${(frac * 100).toFixed(digits)}%`;

// Unit-month status pill class: "occupied" | "vacant" (a record was posted, $0 rent) |
// "missing" (no record posted at all — distinct from a real vacancy, see migration 0011).
export const statusPillClass = (status: string) =>
  `pill pill--${status === "occupied" ? "occupied" : status === "missing" ? "missing" : "vacant"}`;

// Lease status pill class (rent roll): "active" (green) | "notice" (amber — tenant is
// vacating soon, reuses the "missing" pill's warn color) | "expired" (a lapsed, unrenewed
// contract) | "vacant" (no lease on file) — the latter two both reuse the "vacant" pill's
// negative color since both mean "this lease isn't currently in good standing."
export const leaseStatusPillClass = (status: string) =>
  `pill pill--${status === "active" ? "occupied" : status === "notice" ? "missing" : "vacant"}`;

// "25 Aug 2026" under en-GB, "Aug 25, 2026" under en-US — day-first where it should be, and
// never the ambiguous all-numeric form in either locale.
export const fmtDate = (iso: string) =>
  new Date(iso + "T00:00:00").toLocaleDateString(activeLocale(), {
    year: "numeric", month: "short", day: "numeric",
  });

// Compliance certificate status pill class: "valid" (green) | "expiring" (amber — within the
// alert window, renew now) | "expired" (red). Reuses the roster pill colors.
export const certStatusPillClass = (status: string) =>
  `pill pill--${status === "valid" ? "occupied" : status === "expiring" ? "missing" : "vacant"}`;

// Human label for a certificate's days-to-expiry: "expired 12d ago" / "expires in 30d" / "due today".
export const fmtDaysToExpiry = (days: number) => {
  if (days < 0) return `expired ${Math.abs(days)}d ago`;
  if (days === 0) return "due today";
  return `expires in ${days}d`;
};

// "N mo" for a positive/zero months-to-expiry, "expired" for a negative one (a lapsed,
// still-occupied holdover tenancy — e.g. status "expired" — rather than a bare confusing
// negative number), "—" when there's no term to report at all (a periodic/rolling tenancy
// has no end date, or the unit is empty so months_to_expiry is null).
export const fmtMonthsToExpiry = (months: number | null) => {
  if (months == null) return "—";
  if (months < 0) return "expired";
  return `${months} mo`;
};

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
  debtService: "#7c3aed",
  capex: "#b45309",
  grid: "#eef1f5",
  axis: "#8a95a8",
};
