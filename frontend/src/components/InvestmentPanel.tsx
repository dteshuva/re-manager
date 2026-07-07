import { useEffect, useState } from "react";
import {
  deleteInvestment,
  getInvestmentMetrics,
  putInvestment,
  type InvestmentMetrics,
} from "../api";
import { fmtCurrency, fmtMonth, fmtPct } from "../ui";

// Per-property investment returns: a KPI-style tile row (cap rate, cash-on-cash, DSCR,
// average cash-on-cash) computed from acquisition inputs + the P&L rollup, plus an editable
// inputs form. Metrics come back null when they can't be computed honestly (gated on data
// availability); we render "—" with a reason so the row stays stable.

// Loan is entered either as a dollar amount or as an LTV % of the purchase price; both resolve
// to a single stored loan_amount. We keep both raw fields so switching modes doesn't lose input.
type LoanMode = "amount" | "ltv";

interface FormState {
  purchase_price: string;
  closing_costs: string;
  loan_mode: LoanMode;
  loan_amount: string;
  loan_ltv: string;
  purchase_date: string;
}

const EMPTY: FormState = {
  purchase_price: "",
  closing_costs: "",
  loan_mode: "amount",
  loan_amount: "",
  loan_ltv: "",
  purchase_date: "",
};

function toForm(m: InvestmentMetrics): FormState {
  if (m.purchase_price == null) return EMPTY;
  const price = m.purchase_price;
  const loan = m.loan_amount ?? 0;
  return {
    purchase_price: String(price),
    closing_costs: String(m.closing_costs ?? 0),
    loan_mode: "amount",
    loan_amount: String(loan),
    loan_ltv: price > 0 ? String(+((loan / price) * 100).toFixed(2)) : "",
    purchase_date: m.purchase_date ?? "",
  };
}

export default function InvestmentPanel({ token, propertyId }: { token: string; propertyId: string }) {
  const [m, setM] = useState<InvestmentMetrics | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = () =>
    getInvestmentMetrics(token, propertyId)
      .then((res) => {
        setM(res);
        setForm(toForm(res));
        // No inputs yet → open the form so the user knows what to do.
        setEditing(res.purchase_price == null);
      })
      .catch((e) => setError(e.message));

  useEffect(() => {
    setError(null);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, propertyId]);

  const set = (k: keyof FormState) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  const price = parseFloat(form.purchase_price) || 0;
  const closing = parseFloat(form.closing_costs) || 0;
  // Resolve the loan from whichever mode is active.
  const loan =
    form.loan_mode === "ltv"
      ? price * ((parseFloat(form.loan_ltv) || 0) / 100)
      : parseFloat(form.loan_amount) || 0;
  const equityPreview = price - loan + closing;

  async function save() {
    setError(null);
    if (!form.purchase_price || price < 0) return setError("Enter a purchase price.");
    if (!form.purchase_date) return setError("Enter a purchase date.");
    setSaving(true);
    try {
      await putInvestment(token, propertyId, {
        purchase_price: price,
        closing_costs: closing,
        loan_amount: loan,
        purchase_date: form.purchase_date,
      });
      await load();
      setEditing(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!confirm("Remove acquisition data for this property? Return metrics will be cleared.")) return;
    setError(null);
    try {
      await deleteInvestment(token, propertyId);
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const hasInputs = m?.purchase_price != null;

  return (
    <section style={{ marginTop: 8 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <h3 className="section-title" style={{ margin: 0 }}>Investment returns</h3>
        <button className="btn" onClick={() => { setEditing((v) => !v); setForm(m ? toForm(m) : EMPTY); }}>
          {editing ? "Cancel" : hasInputs ? "Edit inputs" : "Add acquisition data"}
        </button>
      </div>

      {error && <p className="alert-error">{error}</p>}

      {m && hasInputs && <MetricCards m={m} />}
      {m && hasInputs && <DebtCheck m={m} />}
      {m && hasInputs && <ContextLine m={m} />}
      {m && !hasInputs && !editing && (
        <p className="hint">No acquisition data yet — add the purchase price, closing costs, financing and date to see cap rate, cash-on-cash and DSCR.</p>
      )}

      {editing && (
        <div style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: "var(--radius)", padding: "14px 16px", marginTop: 12 }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12 }}>
            <Field label="Purchase price">
              <input className="input" type="number" min="0" step="1000" value={form.purchase_price} onChange={set("purchase_price")} placeholder="0" />
            </Field>
            <Field label="Closing costs">
              <input className="input" type="number" min="0" step="500" value={form.closing_costs} onChange={set("closing_costs")} placeholder="0" />
            </Field>
            <label className="col" style={{ display: "flex", flexDirection: "column", gap: 5, fontSize: 12.5, color: "var(--ink-3)" }}>
              <span className="row" style={{ justifyContent: "space-between", alignItems: "center", gap: 6 }}>
                <span>Loan (0 = all cash)</span>
                <select
                  className="select"
                  value={form.loan_mode}
                  onChange={(e) => setForm((f) => ({ ...f, loan_mode: e.target.value as LoanMode }))}
                  style={{ fontSize: 11, padding: "2px 6px" }}
                >
                  <option value="amount">Amount</option>
                  <option value="ltv">LTV %</option>
                </select>
              </span>
              {form.loan_mode === "amount" ? (
                <input className="input" type="number" min="0" step="1000" value={form.loan_amount} onChange={set("loan_amount")} placeholder="0" />
              ) : (
                <input className="input" type="number" min="0" max="100" step="0.5" value={form.loan_ltv} onChange={set("loan_ltv")} placeholder="e.g. 75" />
              )}
              <span style={{ fontSize: 11, color: "var(--ink-3)" }}>
                {form.loan_mode === "ltv"
                  ? `= ${fmtCurrency(loan)} loan`
                  : price > 0
                    ? `= ${((loan / price) * 100).toFixed(1)}% LTV`
                    : "enter a purchase price to show LTV"}
              </span>
            </label>
            <Field label="Purchase date">
              <input className="input" type="date" value={form.purchase_date} onChange={set("purchase_date")} />
            </Field>
          </div>
          <p className="hint" style={{ marginTop: 10 }}>
            Equity invested = price − loan + closing = <strong>{fmtCurrency(equityPreview)}</strong>. This is the denominator for cash-on-cash.
          </p>
          <div className="row" style={{ gap: 8, marginTop: 6 }}>
            <button className="btn btn-primary" onClick={save} disabled={saving}>{saving ? "Saving…" : "Save"}</button>
            {hasInputs && <button className="btn" onClick={remove}>Remove</button>}
          </div>
        </div>
      )}

      {hasInputs && (
        <p className="hint" style={{ marginTop: 10, fontSize: 11.5 }}>
          Cap rate is NOI ÷ purchase price (yield on cost). Cash-on-cash & average use <em>initial</em> equity — out-of-pocket capex later isn't added to the basis. Metrics reflect posted months only.
        </p>
      )}
    </section>
  );
}

function MetricCards({ m }: { m: InvestmentMetrics }) {
  const cocReason =
    m.months_available < 12 ? `needs 12 mo (have ${m.months_available})` : "no equity";
  const cards = [
    {
      label: "Cap rate (on cost)",
      value: m.cap_rate == null ? "—" : fmtPct(m.cap_rate),
      caption: m.cap_rate == null ? "no data" : m.annualized ? `annualized · ${m.t12_months} mo` : "T12 NOI ÷ price",
    },
    {
      label: "Cash-on-cash",
      value: m.cash_on_cash == null ? "—" : fmtPct(m.cash_on_cash),
      caption: m.cash_on_cash == null ? cocReason : "T12 cash flow ÷ equity",
    },
    {
      label: "DSCR",
      value: m.dscr == null ? "—" : `${m.dscr.toFixed(2)}×`,
      caption: m.dscr == null ? "no debt service" : "NOI ÷ debt service",
    },
    {
      label: "Avg cash-on-cash",
      value: m.avg_cash_on_cash == null ? "—" : fmtPct(m.avg_cash_on_cash),
      caption: m.avg_cash_on_cash == null ? `needs 24 mo (have ${m.months_available})` : "annual, since inception",
    },
  ];
  return (
    <div className="kpi-grid" style={{ marginTop: 12 }}>
      {cards.map((c) => (
        <div className="kpi-card" key={c.label}>
          <div className="kpi-card__label">{c.label}</div>
          <div className="kpi-card__value">{c.value}</div>
          <div className="kpi-card__delta is-flat">{c.caption}</div>
        </div>
      ))}
    </div>
  );
}

// Flags when the entered loan and the debt service actually recorded in the P&L disagree —
// the mismatch that makes cash-on-cash / DSCR read unrealistically (a big loan with almost no
// recorded payment, or recorded payments with no loan entered). The "debt constant" (annual
// debt service ÷ loan) of a real mortgage runs ~5–9%; well outside that is suspicious.
const LOW_CONSTANT = 0.03;
const HIGH_CONSTANT = 0.2;

function debtWarning(m: InvestmentMetrics): string | null {
  const loan = m.loan_amount ?? 0;
  const annualDebt = m.t12_months ? ((m.t12_debt_service ?? 0) / m.t12_months) * 12 : 0;
  const pctOf = (frac: number) => `${(frac * 100).toFixed(1)}%`;

  if (loan > 0) {
    if (annualDebt <= 0) {
      return `You entered a ${fmtCurrency(loan)} loan, but no debt service is recorded in the P&L. Cash-on-cash and DSCR are treating this property as if it had no mortgage — record the monthly payment as a debt-service line item, or set the loan to 0 if it's all-cash.`;
    }
    const c = annualDebt / loan;
    if (c < LOW_CONSTANT) {
      return `Recorded debt service (~${fmtCurrency(annualDebt)}/yr) is only ${pctOf(c)} of the ${fmtCurrency(loan)} loan — far below a typical mortgage (~5–9%). DSCR and cash-on-cash likely look better than reality; check the debt-service line items or the loan amount.`;
    }
    if (c > HIGH_CONSTANT) {
      return `Recorded debt service (~${fmtCurrency(annualDebt)}/yr) is ${pctOf(c)} of the ${fmtCurrency(loan)} loan — unusually high for a mortgage. The loan amount may be understated, which would overstate equity and depress cash-on-cash.`;
    }
  } else if (annualDebt > 0) {
    return `The P&L records ~${fmtCurrency(annualDebt)}/yr of debt service, but you entered no loan (all-cash). Equity ignores that financing, so cash-on-cash may be understated — enter the loan amount if this property is levered.`;
  }
  return null;
}

function DebtCheck({ m }: { m: InvestmentMetrics }) {
  const msg = debtWarning(m);
  if (!msg) return null;
  return (
    <div
      style={{
        display: "flex",
        gap: 9,
        alignItems: "flex-start",
        background: "var(--warn-soft)",
        border: "1px solid var(--accent)",
        borderRadius: "var(--radius-sm)",
        padding: "10px 12px",
        margin: "2px 0 12px",
        fontSize: 12.5,
        color: "var(--ink-2)",
        lineHeight: 1.45,
      }}
    >
      <span aria-hidden style={{ color: "var(--accent)", fontWeight: 700 }}>⚠</span>
      <span><strong style={{ color: "var(--accent)" }}>Loan vs. debt service mismatch.</strong> {msg}</span>
    </div>
  );
}

function ContextLine({ m }: { m: InvestmentMetrics }) {
  return (
    <p className="hint" style={{ marginTop: -6 }}>
      {m.purchase_date && `Bought ${fmtMonth(m.purchase_date)} · `}
      Price {fmtCurrency(m.purchase_price ?? 0)} · Equity {fmtCurrency(m.equity_invested ?? 0)}
      {m.t12_noi != null && ` · T12 NOI ${fmtCurrency(m.t12_noi)}`}
      {` · ${m.months_available} mo of data`}
    </p>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="col" style={{ display: "flex", flexDirection: "column", gap: 5, fontSize: 12.5, color: "var(--ink-3)" }}>
      <span>{label}</span>
      {children}
    </label>
  );
}
