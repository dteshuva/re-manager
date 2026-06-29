import { useEffect, useMemo, useState } from "react";
import {
  deleteRecord,
  listCategories,
  listPeriods,
  listProperties,
  listRecords,
  listUnits,
  setPeriodStatus,
  unlockPeriod,
  upsertRecord,
  type Category,
  type PeriodState,
  type PeriodStatus,
  type Property,
  type Unit,
} from "../api";

type Row = { category_id: string; amount: string };

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
  const [recordId, setRecordId] = useState<string | null>(null);
  const [period, setPeriod] = useState<PeriodStatus | null>(null);

  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const month = `${monthStr}-01`;
  const property = properties.find((p) => p.id === propertyId);
  const locked = period?.status === "locked";

  useEffect(() => {
    listProperties(token).then(setProperties).catch((e) => setError(e.message));
    listCategories(token, true).then(setCategories).catch((e) => setError(e.message));
  }, [token]);

  // Units for the selected property (multifamily only).
  useEffect(() => {
    setScope("");
    if (property?.type === "multifamily") {
      listUnits(token, property.id).then(setUnits).catch((e) => setError(e.message));
    } else {
      setUnits([]);
    }
  }, [propertyId]);

  // Load the record + period status whenever scope/month changes.
  useEffect(() => {
    if (!propertyId) return;
    setMsg(null);
    setError(null);
    const unitId = scope || null;
    listRecords(token, { property_id: propertyId, month })
      .then((recs) => {
        const rec = recs.find((r) => r.unit_id === unitId) ?? null;
        setRecordId(rec?.id ?? null);
        setNotes(rec?.notes ?? "");
        setRows(
          rec && rec.line_items.length
            ? rec.line_items.map((li) => ({ category_id: li.category_id, amount: String(li.amount) }))
            : [{ category_id: "", amount: "" }],
        );
      })
      .catch((e) => setError(e.message));
    listPeriods(token, { property_id: propertyId, month })
      .then((ps) => setPeriod(ps[0] ?? null))
      .catch((e) => setError(e.message));
  }, [propertyId, scope, monthStr]);

  const addRow = () => setRows((r) => [...r, { category_id: "", amount: "" }]);
  const setRow = (i: number, patch: Partial<Row>) =>
    setRows((r) => r.map((row, j) => (j === i ? { ...row, ...patch } : row)));
  const removeRow = (i: number) => setRows((r) => r.filter((_, j) => j !== i));

  const catName = useMemo(() => Object.fromEntries(categories.map((c) => [c.id, c.name])), [categories]);

  async function save() {
    setMsg(null);
    setError(null);
    const line_items = rows
      .filter((r) => r.category_id && r.amount !== "")
      .map((r) => ({ category_id: r.category_id, amount: Number(r.amount) }));
    const dupes = line_items.length !== new Set(line_items.map((l) => l.category_id)).size;
    if (dupes) {
      setError("Each category can appear only once per record.");
      return;
    }
    try {
      const rec = await upsertRecord(token, {
        property_id: propertyId,
        unit_id: scope || null,
        month,
        notes: notes || null,
        line_items,
      });
      setRecordId(rec.id);
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
        <label className="stack">
          Property
          <select className="select" value={propertyId} onChange={(e) => setPropertyId(e.target.value)}>
            <option value="">— select —</option>
            {properties.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <label className="stack">
          Scope
          <select
            className="select"
            value={scope}
            onChange={(e) => setScope(e.target.value)}
            disabled={property?.type !== "multifamily"}
          >
            <option value="">Property-tier (shared)</option>
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

          <div className="card" style={{ padding: 0, maxWidth: 640, overflow: "hidden" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr>
                  <th style={{ width: "58%", textAlign: "left", padding: "11px 14px", fontSize: 11, fontWeight: 600, letterSpacing: "0.04em", textTransform: "uppercase", color: "var(--ink-3)", background: "var(--surface-2)", borderBottom: "1px solid var(--border)" }}>Category</th>
                  <th style={{ textAlign: "left", padding: "11px 14px", fontSize: 11, fontWeight: 600, letterSpacing: "0.04em", textTransform: "uppercase", color: "var(--ink-3)", background: "var(--surface-2)", borderBottom: "1px solid var(--border)" }}>Amount</th>
                  <th style={{ background: "var(--surface-2)", borderBottom: "1px solid var(--border)" }}></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row, i) => (
                  <tr key={i}>
                    <td style={{ padding: "8px 14px", borderBottom: "1px solid var(--border)" }}>
                      <select
                        className="select"
                        style={{ width: "100%" }}
                        value={row.category_id}
                        disabled={locked}
                        onChange={(e) => setRow(i, { category_id: e.target.value })}
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
                      <input
                        className="input"
                        style={{ width: 120 }}
                        type="number"
                        step="0.01"
                        value={row.amount}
                        disabled={locked}
                        onChange={(e) => setRow(i, { amount: e.target.value })}
                      />
                    </td>
                    <td style={{ padding: "8px 14px", borderBottom: "1px solid var(--border)" }}>
                      <button className="btn btn-ghost" disabled={locked} onClick={() => removeRow(i)}>
                        ✕
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div style={{ marginTop: 10 }}>
            <button className="btn" disabled={locked} onClick={addRow}>
              + Add line
            </button>
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
            {recordId && (
              <button
                className="btn"
                disabled={locked}
                onClick={() =>
                  confirm("Delete this record and its line items?") &&
                  deleteRecord(token, recordId)
                    .then(() => {
                      setRecordId(null);
                      setRows([{ category_id: "", amount: "" }]);
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
            {locked && (
              <button className="btn" onClick={unlock}>
                Unlock (admin)
              </button>
            )}
          </div>

          {msg && <p style={{ color: "var(--positive)", fontWeight: 500 }}>{msg}</p>}
          {error && <p className="alert-error" style={{ marginTop: 12 }}>{error}</p>}
        </>
      )}
    </section>
  );
}
