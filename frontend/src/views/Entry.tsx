import { useEffect, useMemo, useState } from "react";
import {
  deleteRecord,
  getMe,
  getRentBasis,
  listCategories,
  listPeriods,
  listProperties,
  listRecords,
  listUnits,
  setPeriodStatus,
  unlockPeriod,
  upsertRecord,
  type Category,
  type Classification,
  type LineItem,
  type MonthlyRecord,
  type PeriodState,
  type PeriodStatus,
  type Property,
  type Unit,
} from "../api";
import { t } from "../terms";
import { currencySymbol, fmtCurrency } from "../ui";
import PropertySearchSelect from "../components/PropertySearchSelect";
import SharedExpenses from "../components/SharedExpenses";

// `id` is a stable client-side key (backend line item id when loaded, a fresh uuid for a
// row added in this session) — never the array index, so deleting/reordering rows can't
// misattribute focus/state to the wrong DOM node.
//
// `basis` is how the row is STATED: a figure the operator types, or a rate the server turns
// into a figure ("management: 8% of rent"). `amount` and `rate_pct` are both kept as the
// user typed them so toggling between the two doesn't discard what's in the other box.
//
// `classification` is the line's own override (null = the category's default). It isn't
// edited here, but it is carried through a save so re-saving a month doesn't silently drop an
// override an import set — and on a single-let it decides which lines are rent.
type Row = {
  id: string;
  category_id: string;
  basis: "amount" | "percent";
  amount: string;
  rate_pct: string;
  classification: Classification | null;
};

const newRow = (): Row => ({
  id: crypto.randomUUID(),
  category_id: "",
  basis: "amount",
  amount: "",
  rate_pct: "",
  classification: null,
});

const rowFrom = (li: LineItem): Row => ({
  id: li.id,
  category_id: li.category_id,
  basis: li.rate_pct != null ? "percent" : "amount",
  amount: String(li.amount),
  rate_pct: li.rate_pct != null ? String(li.rate_pct) : "",
  classification: li.classification ?? null,
});

const thisMonth = () => new Date().toISOString().slice(0, 7); // YYYY-MM

// One-month-at-a-time entry for a property/unit. Pick scope + month, edit line items,
// save (idempotent upsert), and drive the draft → posted → locked workflow. Locked
// months are read-only here; only an admin can reopen them.
export default function Entry({ token }: { token: string }) {
  const [properties, setProperties] = useState<Property[]>([]);
  const [units, setUnits] = useState<Unit[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);

  const [propertyId, setPropertyId] = useState("");
  const [scope, setScope] = useState(""); // "" = property-tier, else unit id
  const [monthStr, setMonthStr] = useState(thisMonth());

  const [rows, setRows] = useState<Row[]>([]);
  const [notes, setNotes] = useState("");
  const [isVacant, setIsVacant] = useState(false);
  const [recordId, setRecordId] = useState<string | null>(null);
  // Single-let only: the house's unit-tier record, where its rent lives (see `soleUnit`).
  const [unitRecordId, setUnitRecordId] = useState<string | null>(null);
  const [unitNotes, setUnitNotes] = useState<string | null>(null);
  const [period, setPeriod] = useState<PeriodStatus | null>(null);

  // Rent this scope's percentage lines are charged on, straight from the server so the
  // preview matches what will actually be stored — for a property-tier record that is the
  // whole property's rent, units the operator isn't looking at included. null = not loaded.
  const [basisRent, setBasisRent] = useState<number | null>(null);

  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The "Unlock (admin)" action is server-gated (403 for non-admins), but a member should
  // never see a button that only fails after a round-trip — resolve the current user's role
  // the same way AttentionSettings/AuditLogSection do and hide the button client-side too.
  const [isAdmin, setIsAdmin] = useState(false);

  const month = `${monthStr}-01`;
  const property = properties.find((p) => p.id === propertyId);
  const locked = period?.status === "locked";

  // A single-let house is ONE ledger to the person typing it, but its rent is filed on its one
  // lettable unit (that's where the tenancy, rent roll and arrears read it) and everything
  // else on the property. So for a single-let this screen shows both records as one list and,
  // on save, files rent to the unit and the rest to the property — the same rule the importer
  // applies to a statement. Without this the house's rent was invisible here, and re-typing
  // it put a second copy at the property tier.
  const lettable = units.filter((u) => !u.is_shell);
  const soleUnit = property && property.type !== "multifamily" && lettable.length === 1 ? lettable[0] : null;

  useEffect(() => {
    let cancelled = false;
    listProperties(token).then((r) => !cancelled && setProperties(r)).catch((e) => !cancelled && setError(e.message));
    listCategories(token, true).then((r) => !cancelled && setCategories(r)).catch((e) => !cancelled && setError(e.message));
    getMe(token).then((m) => !cancelled && setIsAdmin(m.role === "admin")).catch(() => !cancelled && setIsAdmin(false));
    return () => {
      cancelled = true;
    };
  }, [token]);

  // Units for the selected property: a multifamily's to pick a scope from, a single-let's to
  // find the one unit its rent is filed on.
  useEffect(() => {
    let cancelled = false;
    setScope("");
    setUnits([]);
    if (property) {
      listUnits(token, property.id).then((r) => !cancelled && setUnits(r)).catch((e) => !cancelled && setError(e.message));
    }
    return () => {
      cancelled = true;
    };
  }, [propertyId]);

  const applyRecords = (recs: MonthlyRecord[]) => {
    if (soleUnit) {
      const propRec = recs.find((r) => r.unit_id === null) ?? null;
      const unitRec = recs.find((r) => r.unit_id === soleUnit.id) ?? null;
      setRecordId(propRec?.id ?? null);
      setUnitRecordId(unitRec?.id ?? null);
      setNotes(propRec?.notes ?? "");
      setUnitNotes(unitRec?.notes ?? null);
      setIsVacant(unitRec?.is_vacant ?? false);
      // Rent first, as a statement reads.
      const items = [...(unitRec?.line_items ?? []), ...(propRec?.line_items ?? [])];
      setRows(items.length ? items.map(rowFrom) : [newRow()]);
      return;
    }
    const unitId = scope || null;
    const rec = recs.find((r) => r.unit_id === unitId) ?? null;
    setRecordId(rec?.id ?? null);
    setUnitRecordId(null);
    setNotes(rec?.notes ?? "");
    setIsVacant(rec?.is_vacant ?? false);
    setRows(rec && rec.line_items.length ? rec.line_items.map(rowFrom) : [newRow()]);
  };

  // Load the record + period status whenever scope/month changes. `cancelled` guards
  // against a slow, superseded request overwriting fresher state if the user switches
  // property/scope/month again before this one resolves.
  useEffect(() => {
    if (!propertyId) return;
    let cancelled = false;
    setMsg(null);
    setError(null);
    listRecords(token, { property_id: propertyId, month })
      .then((recs) => !cancelled && applyRecords(recs))
      .catch((e) => !cancelled && setError(e.message));
    listPeriods(token, { property_id: propertyId, month })
      .then((ps) => !cancelled && setPeriod(ps[0] ?? null))
      .catch((e) => !cancelled && setError(e.message));
    setBasisRent(null);
    getRentBasis(token, { property_id: propertyId, month, unit_id: scope || null })
      .then((b) => !cancelled && setBasisRent(b.rent))
      .catch(() => !cancelled && setBasisRent(null));
    return () => {
      cancelled = true;
    };
  }, [propertyId, scope, monthStr, soleUnit?.id]);

  const addRow = () => setRows((r) => [...r, newRow()]);
  const setRow = (id: string, patch: Partial<Row>) =>
    setRows((r) => r.map((row) => (row.id === id ? { ...row, ...patch } : row)));
  const removeRow = (id: string) => setRows((r) => r.filter((row) => row.id !== id));

  const catName = useMemo(() => Object.fromEntries(categories.map((c) => [c.id, c.name])), [categories]);
  const catClass = useMemo(
    () => Object.fromEntries(categories.map((c) => [c.id, c.default_classification])),
    [categories],
  );

  async function save() {
    setMsg(null);
    setError(null);
    // A rate-stated row sends the rate and no amount — the server derives the figure from
    // rent, and sending both is rejected precisely because they could disagree.
    const filled = rows.filter((r) => r.category_id && (r.basis === "percent" ? r.rate_pct !== "" : r.amount !== ""));
    const toItem = (r: Row) => ({
      category_id: r.category_id,
      ...(r.classification ? { classification: r.classification } : {}),
      ...(r.basis === "percent" ? { amount: 0, rate_pct: Number(r.rate_pct) } : { amount: Number(r.amount) }),
    });
    const line_items = filled.map(toItem);
    const dupes = line_items.length !== new Set(line_items.map((l) => l.category_id)).size;
    if (dupes) {
      setError(
        soleUnit
          ? "Each category can appear only once per month — remove the duplicate line."
          : "Each category can appear only once per record.",
      );
      return;
    }
    if (soleUnit) {
      // Rent (by the line's effective classification) to the unit, the rest to the property.
      const isRent = (r: Row) => (r.classification ?? catClass[r.category_id]) === "rent";
      const rentItems = filled.filter(isRent).map(toItem);
      const otherItems = filled.filter((r) => !isRent(r)).map(toItem);
      try {
        // Unit first: a "% of rent" fee on the property is derived from the rent just saved.
        if (rentItems.length || unitRecordId || isVacant) {
          await upsertRecord(token, {
            property_id: propertyId,
            unit_id: soleUnit.id,
            month,
            notes: unitNotes,
            is_vacant: isVacant,
            line_items: rentItems,
          });
        }
        if (otherItems.length || recordId || notes) {
          await upsertRecord(token, {
            property_id: propertyId,
            unit_id: null,
            month,
            notes: notes || null,
            is_vacant: false,
            line_items: otherItems,
          });
        }
        applyRecords(await listRecords(token, { property_id: propertyId, month }));
        getRentBasis(token, { property_id: propertyId, month, unit_id: null })
          .then((b) => setBasisRent(b.rent))
          .catch(() => setBasisRent(null));
        setMsg(`Saved ${line_items.length} line item(s).`);
      } catch (e) {
        setError((e as Error).message);
      }
      return;
    }
    try {
      const rec = await upsertRecord(token, {
        property_id: propertyId,
        unit_id: scope || null,
        month,
        notes: notes || null,
        is_vacant: scope ? isVacant : false,
        line_items,
      });
      setRecordId(rec.id);
      // Re-seed from the response: percentage rows come back with the amount the server
      // derived, and saved rent may have moved the basis every percentage row previews against.
      setRows(rec.line_items.length ? rec.line_items.map(rowFrom) : [newRow()]);
      getRentBasis(token, { property_id: propertyId, month, unit_id: scope || null })
        .then((b) => setBasisRent(b.rent))
        .catch(() => setBasisRent(null));
      setMsg(`Saved ${line_items.length} line item(s).`);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function changeStatus(status: PeriodState) {
    setMsg(null);
    setError(null);
    try {
      setPeriod(await setPeriodStatus(token, { property_id: propertyId, month, status }));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function unlock() {
    if (!period) return;
    try {
      setPeriod(await unlockPeriod(token, period.id));
      setMsg("Unlocked (admin). Month is now posted and editable.");
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <section>
      <div className="card" style={{ display: "flex", gap: 16, flexWrap: "wrap", alignItems: "center", padding: "14px 18px", marginBottom: 18 }}>
        <PropertySearchSelect
          properties={properties}
          value={propertyId}
          onChange={setPropertyId}
          placeholder="— select —"
        />
        <label className="stack">
          Scope
          <select
            className="select"
            value={scope}
            onChange={(e) => setScope(e.target.value)}
            disabled={property?.type !== "multifamily"}
          >
            <option value="">{soleUnit ? `Whole house (rent filed to unit ${soleUnit.unit_number})` : "Property-tier (shared)"}</option>
            {units.map((u) => (
              <option key={u.id} value={u.id}>
                Unit {u.unit_number}
              </option>
            ))}
          </select>
        </label>
        <label className="stack">
          Month
          <input className="input" type="month" value={monthStr} onChange={(e) => setMonthStr(e.target.value)} />
        </label>
        {period && (
          <span style={{ marginLeft: "auto" }}>
            <span className={`badge badge-${period.status}`}>{period.status}</span>
          </span>
        )}
      </div>

      {!propertyId ? (
        <p className="muted">Select a property to begin.</p>
      ) : (
        <>
          {locked && (
            <p className="alert-error" style={{ background: "var(--warn-soft)", borderColor: "#f3d6c5", color: "#9a3412" }}>
              🔒 This month is locked. Edits are disabled — an admin must unlock it.
            </p>
          )}

          {(scope || soleUnit) && (
            <label className="row" style={{ gap: 8, marginBottom: 12, alignItems: "center" }}>
              <input
                type="checkbox"
                checked={isVacant}
                disabled={locked}
                onChange={(e) => setIsVacant(e.target.checked)}
              />
              {soleUnit ? `Mark this house as ${t("vacant")} this month` : `Mark this unit as ${t("vacant")} this month`}
              <span className="muted" style={{ fontSize: 12 }}>
                {`(explicit — distinct from simply not posting a record; ${currencySymbol()}0 rent either way)`}
              </span>
            </label>
          )}

          <div className="card" style={{ padding: 0, maxWidth: 640, overflow: "hidden" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr>
                  <th style={{ width: "44%", textAlign: "left", padding: "11px 14px", fontSize: 11, fontWeight: 600, letterSpacing: "0.04em", textTransform: "uppercase", color: "var(--ink-3)", background: "var(--surface-2)", borderBottom: "1px solid var(--border)" }}>Category</th>
                  <th style={{ textAlign: "left", padding: "11px 14px", fontSize: 11, fontWeight: 600, letterSpacing: "0.04em", textTransform: "uppercase", color: "var(--ink-3)", background: "var(--surface-2)", borderBottom: "1px solid var(--border)" }}>Amount</th>
                  <th style={{ background: "var(--surface-2)", borderBottom: "1px solid var(--border)" }}></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id}>
                    <td style={{ padding: "8px 14px", borderBottom: "1px solid var(--border)" }}>
                      <select
                        className="select"
                        style={{ width: "100%" }}
                        value={row.category_id}
                        disabled={locked}
                        onChange={(e) => setRow(row.id, { category_id: e.target.value })}
                      >
                        <option value="">— category —</option>
                        {categories.map((c) => (
                          <option key={c.id} value={c.id}>
                            {c.name} ({c.default_classification})
                          </option>
                        ))}
                        {/* keep an inactive category visible if it's already on this row */}
                        {row.category_id && !catName[row.category_id] && (
                          <option value={row.category_id}>(inactive category)</option>
                        )}
                      </select>
                    </td>
                    <td style={{ padding: "8px 14px", borderBottom: "1px solid var(--border)" }}>
                      <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                        {/* Some costs are agreed as a rate, not a figure — a management fee is
                            "8% of rent", not "$336". Stating the rate lets the server keep the
                            figure in step with rent instead of the operator retyping it. */}
                        <select
                          className="select"
                          style={{ width: 104 }}
                          value={row.basis}
                          disabled={locked}
                          onChange={(e) => setRow(row.id, { basis: e.target.value as Row["basis"] })}
                        >
                          <option value="amount">Amount</option>
                          <option value="percent">% of rent</option>
                        </select>
                        {row.basis === "percent" ? (
                          <input
                            className="input"
                            style={{ width: 84 }}
                            type="number"
                            step="0.01"
                            min="0"
                            max="100"
                            placeholder="8"
                            value={row.rate_pct}
                            disabled={locked}
                            onChange={(e) => setRow(row.id, { rate_pct: e.target.value })}
                          />
                        ) : (
                          <input
                            className="input"
                            style={{ width: 120 }}
                            type="number"
                            step="0.01"
                            value={row.amount}
                            disabled={locked}
                            onChange={(e) => setRow(row.id, { amount: e.target.value })}
                          />
                        )}
                      </div>
                      {row.basis === "percent" && (
                        <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                          {basisRent === null
                            ? "rent basis unavailable"
                            : row.rate_pct === ""
                              ? `of ${fmtCurrency(basisRent)} rent`
                              : `= ${fmtCurrency((Number(row.rate_pct) / 100) * basisRent)} of ${fmtCurrency(basisRent)} rent`}
                        </div>
                      )}
                    </td>
                    <td style={{ padding: "8px 14px", borderBottom: "1px solid var(--border)" }}>
                      <button className="btn btn-ghost" disabled={locked} onClick={() => removeRow(row.id)}>
                        ✕
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div style={{ marginTop: 10, display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
            <button className="btn" disabled={locked} onClick={addRow}>
              + Add line
            </button>
            <span className="muted" style={{ fontSize: 12 }}>
              A “% of rent” line (a management fee, typically) is recomputed from{" "}
              {scope ? "this unit’s rent" : "the property’s whole rent for the month"} whenever that
              rent changes — so it never goes stale.
            </span>
          </div>

          <div style={{ marginTop: 12 }}>
            <textarea
              className="input"
              style={{ width: "100%", maxWidth: 640, minHeight: 54 }}
              placeholder="Notes (optional)"
              value={notes}
              disabled={locked}
              onChange={(e) => setNotes(e.target.value)}
            />
          </div>

          <div style={{ marginTop: 14, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <button className="btn btn-primary" disabled={locked} onClick={save}>
              Save month
            </button>
            {(recordId || unitRecordId) && (
              <button
                className="btn"
                disabled={locked}
                onClick={() =>
                  confirm(soleUnit ? "Delete this month's lines for the house?" : "Delete this record and its line items?") &&
                  Promise.all([recordId, unitRecordId].filter((x): x is string => !!x).map((id) => deleteRecord(token, id)))
                    .then(() => {
                      setRecordId(null);
                      setUnitRecordId(null);
                      setRows([newRow()]);
                      setNotes("");
                      setMsg("Record deleted.");
                    })
                    .catch((e) => setError(e.message))
                }
              >
                Delete record
              </button>
            )}
            <span className="muted" style={{ borderLeft: "1px solid var(--border-strong)", paddingLeft: 10 }}>Workflow:</span>
            <button className="btn" disabled={locked} onClick={() => changeStatus("draft")}>
              Mark draft
            </button>
            <button className="btn" disabled={locked} onClick={() => changeStatus("posted")}>
              Post
            </button>
            <button className="btn" disabled={locked} onClick={() => changeStatus("locked")}>
              Lock
            </button>
            {locked && isAdmin && (
              <button className="btn" onClick={unlock}>
                Unlock (admin)
              </button>
            )}
          </div>

          {msg && <p style={{ color: "var(--positive)", fontWeight: 500 }}>{msg}</p>}
          {error && <p className="alert-error" style={{ marginTop: 12 }}>{error}</p>}
        </>
      )}

      {/* Costs that belong to several properties at once (a portfolio loan payment, a blanket
          policy) have no single property to be typed against, so they live below the per-property
          form rather than inside it: define the split once, then post it to a month or a year and
          each member gets its own share as an ordinary property-tier line item. */}
      <SharedExpenses token={token} isAdmin={isAdmin} />
    </section>
  );
}
