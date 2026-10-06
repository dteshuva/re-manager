import { useEffect, useState } from "react";
import {
  createUnitLease,
  listUnitLeases,
  updateLease,
  type Lease,
  type LeaseInput,
} from "../api";
import { t } from "../terms";
import { currencySymbol, fmtDate } from "../ui";

// The three figures an operator actually changes — start date, last rent increase, rent —
// editable wherever a unit appears, not only on the rent roll.
//
// WHY THIS IS A SHARED COMPONENT AND NOT A SECOND FORM
// ----------------------------------------------------
// `PATCH /leases/{id}` is a FULL REPLACE: every field absent from the body is written as null.
// A compact three-field form that posted only its own three fields would therefore silently
// destroy the tenant's name, the deposit, the previous rent and the
// brought-forward arrears balance — every time someone corrected a rent on the property page.
// That is exactly the hazard the API's own docs warn about, and the reason this component
// LOADS the whole tenancy first and merges the edits into it before saving. One implementation,
// used by every screen that offers these fields, so no caller can get that merge wrong.
//
// The save goes to the same endpoint the rent roll uses and the server is the only source of
// truth, so an edit made here shows up on the rent roll, in the arrears ledger, in the
// attention feed and in the unit roster — nothing is cached client-side per view.
export default function TenancyEditor({
  token,
  unitId,
  unitLabel,
  onSaved,
  onCancel,
}: {
  token: string;
  unitId: string;
  unitLabel?: string;
  // Called after a successful save so the host view can refetch. The figures below are
  // reference data the P&L never reads, but the rent roll, arrears and this unit's own roster
  // row all do.
  onSaved: () => void;
  onCancel?: () => void;
}) {
  // The full tenancy as stored — the base every save is merged onto. `null` while loading;
  // `undefined` once we know there is no tenancy on file yet (a create, not an edit).
  const [existing, setExisting] = useState<Lease | null | undefined>(null);
  const [startDate, setStartDate] = useState("");
  const [lastIncrease, setLastIncrease] = useState("");
  const [rentBefore, setRentBefore] = useState("");
  const [rent, setRent] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setExisting(null);
    setError(null);
    listUnitLeases(token, unitId)
      .then((leases) => {
        if (!live) return;
        // Same "current tenancy" rule the rest of the app uses: the one covering today, else
        // the most recently started. The list arrives most-recent-first.
        const today = new Date().toISOString().slice(0, 10);
        const current =
          leases.find(
            (l) =>
              (l.status === "active" || l.status === "notice") &&
              l.start_date <= today &&
              (l.end_date == null || l.end_date >= today),
          ) ?? leases[0];
        setExisting(current ?? undefined);
        setStartDate(current?.start_date ?? today);
        setLastIncrease(current?.last_rent_increase_date ?? "");
        setRentBefore(current?.rent_before_increase?.toString() ?? "");
        setRent(current?.contract_rent?.toString() ?? "");
      })
      .catch((e) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [token, unitId]);

  const save = () => {
    setError(null);
    if (!startDate) {
      setError("Start date is required — the rent schedule is anchored on it.");
      return;
    }
    if (rent === "" || Number.isNaN(Number(rent))) {
      setError("Rent is required.");
      return;
    }
    if (rentBefore !== "" && !lastIncrease) {
      setError("A previous rent needs a last-increase date to attach it to.");
      return;
    }
    setBusy(true);

    // The merge that makes this safe: start from every field the stored tenancy has, then
    // overwrite only the four this editor owns.
    const body: LeaseInput = {
      tenant_name: existing?.tenant_name ?? null,
      status: existing?.status ?? "active",
      // Blank = a periodic (rolling) tenancy. Preserved as-is: this editor doesn't offer the
      // field, so it must never be the thing that changes it.
      end_date: existing?.end_date ?? null,
      security_deposit: existing?.security_deposit ?? null,
      escalation_pct: existing?.escalation_pct ?? null,
      escalation_frequency_months: existing?.escalation_frequency_months ?? 12,
      pct_rent_rate: existing?.pct_rent_rate ?? null,
      pct_rent_breakpoint: existing?.pct_rent_breakpoint ?? null,
      // Carried through untouched: it isn't offered anywhere on a UK account (not a British
      // letting concept — see terms.ts), but a stored value must survive a save regardless.
      concession_monthly: existing?.concession_monthly ?? null,
      opening_arrears: existing?.opening_arrears ?? null,
      // Carried through for the same reason as everything above it: this is a full-replace
      // PATCH, so a field omitted here is a field erased. Set on the Rent Roll tab.
      arrears_from_month: existing?.arrears_from_month ?? null,
      // ...and the four this editor owns.
      start_date: startDate,
      contract_rent: Number(rent),
      last_rent_increase_date: lastIncrease || null,
      rent_before_increase: lastIncrease && rentBefore !== "" ? Number(rentBefore) : null,
    };

    const req = existing
      ? updateLease(token, existing.id, body)
      : createUnitLease(token, unitId, body);
    req
      .then(() => onSaved())
      .catch((e) => setError(e.message))
      .finally(() => setBusy(false));
  };

  if (existing === null && !error) return <p className="hint">Loading…</p>;

  return (
    <div className="card" style={{ margin: 0 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
        <strong>
          {existing ? `${t("Lease")} — ` : `New ${t("lease")} — `}
          {unitLabel ?? "unit"}
        </strong>
        {existing?.tenant_name && <span className="muted">{existing.tenant_name}</span>}
      </div>

      <div className="row" style={{ gap: 10, flexWrap: "wrap", alignItems: "flex-end", marginTop: 10 }}>
        <label className="stack">
          Start date
          <input
            className="input"
            type="date"
            value={startDate}
            onChange={(e) => setStartDate(e.target.value)}
          />
          {/* A date input renders in the BROWSER's locale, not the account's — so echo what was
              chosen in the account's format. "09/08" means two different days in the two. */}
          {startDate && (
            <span style={{ fontSize: 11, color: "var(--ink-3)" }}>= {fmtDate(startDate)}</span>
          )}
        </label>
        <label className="stack">
          {`Rent (${currencySymbol()}/mo)`}
          <input
            className="input"
            type="number"
            step="0.01"
            min="0"
            value={rent}
            onChange={(e) => setRent(e.target.value)}
          />
        </label>
        <label className="stack">
          Rent last increased
          <input
            className="input"
            type="date"
            value={lastIncrease}
            onChange={(e) => setLastIncrease(e.target.value)}
          />
          {lastIncrease && (
            <span style={{ fontSize: 11, color: "var(--ink-3)" }}>= {fmtDate(lastIncrease)}</span>
          )}
        </label>
        <label className="stack">
          Rent before increase
          <input
            className="input"
            type="number"
            step="0.01"
            min="0"
            // Meaningless without a date to place it at — the server rejects that pairing.
            disabled={!lastIncrease}
            value={rentBefore}
            onChange={(e) => setRentBefore(e.target.value)}
          />
        </label>
        <button className="btn btn-primary" type="button" disabled={busy} onClick={save}>
          {busy ? "Saving…" : "Save"}
        </button>
        {onCancel && (
          <button className="btn btn-ghost" type="button" onClick={onCancel}>
            Cancel
          </button>
        )}
      </div>

      {error && <p className="hint" style={{ color: "var(--danger, #c0392b)" }}>{error}</p>}
      <p className="hint" style={{ marginBottom: 0 }}>
        {existing
          ? "Saves to the one tenancy record every screen reads, so the rent roll, the arrears " +
            "ledger and the dashboard all update together. Fields this editor doesn't show " +
            "(tenant, deposit, opening arrears) are preserved — edit those on the " +
            "Rent Roll tab."
          : "No tenancy on file for this unit yet — saving creates one. Leave it rolling (no end " +
            "date); add the tenant and deposit on the Rent Roll tab if you want them recorded."}
        {" "}
        {lastIncrease
          ? "The previous rent is what prices the months before the increase — without it, " +
            "earlier months are priced at today's rent and a tenant who always paid in full " +
            "appears to be in arrears."
          : "Set “rent last increased” if the rent has ever been reviewed; it drives the " +
            "“months since” overdue-review signal."}
        {existing?.start_date && existing.start_date !== startDate && (
          <>
            {" "}Currently starts {fmtDate(existing.start_date)}.
          </>
        )}
      </p>
    </div>
  );
}
