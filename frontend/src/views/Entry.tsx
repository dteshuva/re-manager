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
import { btn, btnPrimary, card, input, STATUS_COLOR } from "../ui";

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
      <h2>Data Entry</h2>

      <div style={{ ...card, display: "flex", gap: 12, flexWrap: "wrap", alignItems: "center" }}>
        <label>
          Property{" "}
          <select style={input} value={propertyId} onChange={(e) => setPropertyId(e.target.value)}>
            <option value="">— select —</option>
            {properties.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Scope{" "}
          <select
            style={input}
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
        <label>
          Month{" "}
          <input style={input} type="month" value={monthStr} onChange={(e) => setMonthStr(e.target.value)} />
        </label>
        {period && (
          <span style={{ marginLeft: "auto" }}>
            Status:{" "}
            <strong style={{ color: STATUS_COLOR[period.status] }}>{period.status}</strong>
          </span>
        )}
      </div>

      {!propertyId ? (
        <p style={{ color: "#888" }}>Select a property to begin.</p>
      ) : (
        <>
          {locked && (
            <p style={{ color: "#b91c1c" }}>
              🔒 This month is locked. Edits are disabled — an admin must unlock it.
            </p>
          )}

          <table cellPadding={6} style={{ borderCollapse: "collapse", width: "100%", maxWidth: 640 }}>
            <thead>
              <tr style={{ textAlign: "left", borderBottom: "2px solid #333" }}>
                <th style={{ width: "60%" }}>Category</th>
                <th>Amount</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={i}>
                  <td>
                    <select
                      style={{ ...input, width: "100%" }}
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
                  <td>
                    <input
                      style={{ ...input, width: 120 }}
                      type="number"
                      step="0.01"
                      value={row.amount}
                      disabled={locked}
                      onChange={(e) => setRow(i, { amount: e.target.value })}
                    />
                  </td>
                  <td>
                    <button style={btn} disabled={locked} onClick={() => removeRow(i)}>
                      ✕
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div style={{ marginTop: 8 }}>
            <button style={btn} disabled={locked} onClick={addRow}>
              + Add line
            </button>
          </div>

          <div style={{ marginTop: 12 }}>
            <textarea
              style={{ ...input, width: "100%", maxWidth: 640, minHeight: 50 }}
              placeholder="Notes (optional)"
              value={notes}
              disabled={locked}
              onChange={(e) => setNotes(e.target.value)}
            />
          </div>

          <div style={{ marginTop: 12, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <button style={btnPrimary} disabled={locked} onClick={save}>
              Save month
            </button>
            {recordId && (
              <button
                style={btn}
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
            <span style={{ borderLeft: "1px solid #ccc", paddingLeft: 8 }}>Workflow:</span>
            <button style={btn} disabled={locked} onClick={() => changeStatus("draft")}>
              Mark draft
            </button>
            <button style={btn} disabled={locked} onClick={() => changeStatus("posted")}>
              Post
            </button>
            <button style={btn} disabled={locked} onClick={() => changeStatus("locked")}>
              Lock
            </button>
            {locked && (
              <button style={btn} onClick={unlock}>
                Unlock (admin)
              </button>
            )}
          </div>

          {msg && <p style={{ color: "green" }}>{msg}</p>}
          {error && <p style={{ color: "crimson" }}>{error}</p>}
        </>
      )}
    </section>
  );
}
