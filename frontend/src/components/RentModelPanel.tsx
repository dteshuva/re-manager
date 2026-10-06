import { useCallback, useEffect, useState } from "react";
import {
  createUnitLease,
  getUnitArrears,
  listUnitLeases,
  updateLease,
  type Lease,
  type LeaseInput,
  type UnitArrears,
} from "../api";
import { t } from "../terms";
import { currencySymbol, fmtCurrency, fmtDate, fmtMonth } from "../ui";

// The rent model for a single-let property, as a first-class panel on the property page —
// deliberately the same shape as InvestmentPanel: a title, one "Edit inputs" button, metric
// cards when there's something to show, and an inline form when there isn't or you're changing
// it. Those are the figures a landlord revisits; they deserve the same prominence as the
// acquisition inputs rather than being buried behind a per-row button in a wide table.
//
// Only for a property with exactly ONE unit. Rent is a property of a tenancy and a tenancy is a
// property of a unit, so on a multifamily building "the rent" is ambiguous and the per-row
// editor in the unit roster is the honest surface. A single-let house has one dwelling and one
// rent, so the panel can speak for the property.
//
// WHY THE SAVE LOADS FIRST: `PATCH /leases/{id}` is a FULL REPLACE — every field absent from the
// body is written as null. A panel that posted only its own inputs would wipe the tenant name,
// the deposit and the brought-forward arrears balance on every rent correction. So the stored
// tenancy is loaded, and the inputs are merged onto it.
const EMPTY = { start_date: "", contract_rent: "", last_rent_increase_date: "", rent_before_increase: "" };

export default function RentModelPanel({
  token,
  unitId,
  onSaved,
}: {
  token: string;
  unitId: string;
  // Called after a save so the host page can refetch — the rent roll, arrears ledger and
  // attention feed all read the same stored record, and none of them cache it.
  onSaved?: () => void;
}) {
  const [lease, setLease] = useState<Lease | null | undefined>(null);
  const [arrears, setArrears] = useState<UnitArrears | null>(null);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState(EMPTY);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const toForm = (l: Lease) => ({
    start_date: l.start_date ?? "",
    contract_rent: l.contract_rent?.toString() ?? "",
    last_rent_increase_date: l.last_rent_increase_date ?? "",
    rent_before_increase: l.rent_before_increase?.toString() ?? "",
  });

  const load = useCallback(() => {
    setError(null);
    listUnitLeases(token, unitId)
      .then((leases) => {
        // Same "current tenancy" rule the rest of the app uses: the one covering today, else the
        // most recently started. The list arrives most-recent-first.
        const today = new Date().toISOString().slice(0, 10);
        const current =
          leases.find(
            (l) =>
              (l.status === "active" || l.status === "notice") &&
              l.start_date <= today &&
              (l.end_date == null || l.end_date >= today),
          ) ?? leases[0];
        setLease(current ?? undefined);
      })
      .catch((e) => setError(e.message));
    // The accumulated balance belongs on this panel: the rent model is what it's measured
    // against, so showing one without the other invites reading the rent as if it were received.
    getUnitArrears(token, unitId).then(setArrears).catch(() => setArrears(null));
  }, [token, unitId]);

  useEffect(load, [load]);

  const save = () => {
    setError(null);
    if (!form.start_date) {
      setError("Start date is required — the rent schedule is anchored on it.");
      return;
    }
    if (form.contract_rent === "" || Number.isNaN(Number(form.contract_rent))) {
      setError("Rent is required.");
      return;
    }
    if (form.rent_before_increase !== "" && !form.last_rent_increase_date) {
      setError("A previous rent needs a last-increase date to attach it to.");
      return;
    }
    setSaving(true);
    const body: LeaseInput = {
      // Preserved, not re-stated: this panel doesn't own these, so it must never be what
      // changes them. See the note at the top of the file.
      tenant_name: lease?.tenant_name ?? null,
      status: lease?.status ?? "active",
      end_date: lease?.end_date ?? null,
      security_deposit: lease?.security_deposit ?? null,
      escalation_pct: lease?.escalation_pct ?? null,
      escalation_frequency_months: lease?.escalation_frequency_months ?? 12,
      pct_rent_rate: lease?.pct_rent_rate ?? null,
      pct_rent_breakpoint: lease?.pct_rent_breakpoint ?? null,
      concession_monthly: lease?.concession_monthly ?? null,
      opening_arrears: lease?.opening_arrears ?? null,
      // Carried through for the same reason as everything above it: this is a full-replace
      // PATCH, so a field omitted here is a field erased. Set on the Rent Roll tab.
      arrears_from_month: lease?.arrears_from_month ?? null,
      // ...and the four inputs this panel owns.
      start_date: form.start_date,
      contract_rent: Number(form.contract_rent),
      last_rent_increase_date: form.last_rent_increase_date || null,
      rent_before_increase:
        form.last_rent_increase_date && form.rent_before_increase !== ""
          ? Number(form.rent_before_increase)
          : null,
    };
    const req = lease ? updateLease(token, lease.id, body) : createUnitLease(token, unitId, body);
    req
      .then(() => {
        setEditing(false);
        load();
        onSaved?.();
      })
      .catch((e) => setError(e.message))
      .finally(() => setSaving(false));
  };

  const set = (k: keyof typeof EMPTY) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  const hasLease = !!lease;
  const monthsSince = (() => {
    const from = lease?.last_rent_increase_date ?? lease?.start_date;
    if (!from) return null;
    const d = new Date(from + "T00:00:00");
    const now = new Date();
    let m = (now.getFullYear() - d.getFullYear()) * 12 + (now.getMonth() - d.getMonth());
    if (now.getDate() < d.getDate()) m -= 1;
    return Math.max(m, 0);
  })();
  const balance = arrears?.current_balance ?? 0;

  return (
    <section style={{ marginTop: 8 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <h3 className="section-title" style={{ margin: 0 }}>Rent &amp; {t("lease")}</h3>
        <button
          className="btn"
          onClick={() => {
            setEditing((v) => !v);
            setForm(lease ? toForm(lease) : { ...EMPTY, start_date: new Date().toISOString().slice(0, 10) });
          }}
        >
          {editing ? "Cancel" : hasLease ? "Edit inputs" : `Add ${t("lease")}`}
        </button>
      </div>

      {error && <p className="alert-error">{error}</p>}

      {hasLease && !editing && (
        <div className="kpi-band" style={{ marginTop: 10 }}>
          <div className="kpi-card">
            <div className="kpi-card__label">Rent (agreed)</div>
            <div className="kpi-card__value">{fmtCurrency(lease.contract_rent)}</div>
            <div className="kpi-card__delta is-flat">
              {lease.end_date ? `fixed term to ${fmtDate(lease.end_date)}` : "periodic (rolling)"}
            </div>
          </div>
          <div className="kpi-card">
            <div className="kpi-card__label">Start date</div>
            <div className="kpi-card__value" style={{ fontSize: "1.1rem" }}>
              {fmtDate(lease.start_date)}
            </div>
            <div className="kpi-card__delta is-flat">
              {lease.tenant_name ? lease.tenant_name : "tenant not recorded"}
            </div>
          </div>
          <div className="kpi-card">
            <div className="kpi-card__label">Last increase</div>
            <div className="kpi-card__value" style={{ fontSize: "1.1rem" }}>
              {lease.last_rent_increase_date ? fmtDate(lease.last_rent_increase_date) : "never"}
            </div>
            <div className="kpi-card__delta is-flat">
              {monthsSince == null
                ? ""
                : lease.last_rent_increase_date
                  ? `${monthsSince} mo ago${
                      lease.rent_before_increase != null
                        ? `, was ${fmtCurrency(lease.rent_before_increase)}`
                        : ""
                    }`
                  : `${monthsSince} mo on this rent`}
            </div>
          </div>
          <div className="kpi-card">
            <div className="kpi-card__label">Arrears</div>
            <div className={`kpi-card__value${balance > 0 ? " value-negative" : ""}`}>
              {balance < 0 ? `${fmtCurrency(-balance)} cr` : fmtCurrency(balance)}
            </div>
            <div className="kpi-card__delta is-flat">
              {balance > 0 ? "owed to date" : balance < 0 ? "paid ahead" : "nothing owed"}
              {lease.arrears_from_month && ` · from ${fmtMonth(lease.arrears_from_month)}`}
            </div>
          </div>
        </div>
      )}

      {!hasLease && !editing && (
        <p className="hint">
          {`No ${t("lease")} on file yet — add the start date and the agreed rent to get the rent roll, `}
          expected-vs-actual rent and the arrears balance for this property.
        </p>
      )}

      {editing && (
        <div
          style={{
            background: "var(--surface)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius)",
            padding: "14px 16px",
            marginTop: 12,
          }}
        >
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12 }}>
            <Field label="Start date">
              <input className="input" type="date" value={form.start_date} onChange={set("start_date")} />
              <DateEcho iso={form.start_date} />
            </Field>
            <Field label={`Rent (${currencySymbol()}/mo)`}>
              <input
                className="input"
                type="number"
                min="0"
                step="1"
                value={form.contract_rent}
                onChange={set("contract_rent")}
                placeholder="0"
              />
            </Field>
            <Field label="Rent last increased">
              <input
                className="input"
                type="date"
                value={form.last_rent_increase_date}
                onChange={set("last_rent_increase_date")}
              />
              <DateEcho iso={form.last_rent_increase_date} />
            </Field>
            <Field label="Rent before increase">
              <input
                className="input"
                type="number"
                min="0"
                step="1"
                // Uninterpretable without a date to place it at — the server rejects the pairing.
                disabled={!form.last_rent_increase_date}
                value={form.rent_before_increase}
                onChange={set("rent_before_increase")}
                placeholder={form.last_rent_increase_date ? "previous rent" : "set a date first"}
              />
            </Field>
          </div>
          <p className="hint" style={{ marginTop: 10 }}>
            {form.last_rent_increase_date
              ? "The previous rent is what prices the months BEFORE the increase. Without it those " +
                "months are priced at today's rent, and a tenant who paid in full every month " +
                "shows up owing the difference."
              : "Set “rent last increased” if the rent has ever been reviewed — it drives the " +
                "“months since” signal that says a review is overdue."}
          </p>
          <div className="row" style={{ gap: 8, marginTop: 6 }}>
            <button className="btn btn-primary" onClick={save} disabled={saving}>
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
        </div>
      )}

      {hasLease && (
        <p className="hint" style={{ marginTop: 10, fontSize: 11.5 }}>
          {`These are the ${t("lease")}'s own terms — reference data that never feeds NOI or cash flow. `}
          Saves to the one record every screen reads, so the Rent Roll tab, the arrears ledger and
          the dashboard update together. Tenant name, deposit and opening arrears live on the Rent
          Roll tab.
        </p>
      )}
    </section>
  );
}

// A date input is rendered by the BROWSER in the browser's own locale, which no amount of app
// configuration can change — so someone on a US-configured browser sees mm/dd/yyyy boxes even on
// a British account. Echoing the chosen date back in the account's format removes the ambiguity
// at the only point it matters: before you save something a day or nine months out.
function DateEcho({ iso }: { iso: string }) {
  if (!iso) return null;
  return (
    <span style={{ fontSize: 11, color: "var(--ink-3)" }}>= {fmtDate(iso)}</span>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 5, fontSize: 12.5, color: "var(--ink-3)" }}>
      <span>{label}</span>
      {children}
    </label>
  );
}
