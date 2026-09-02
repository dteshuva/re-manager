import { useEffect, useMemo, useState } from "react";
import {
  createAcquisition,
  deleteAcquisition,
  getAcquisitions,
  listProperties,
  previewAcquisition,
  updateAcquisition,
  type AcquisitionMember,
  type AcquisitionPreview,
  type AllocationMethod,
  type PortfolioAcquisition,
  type PortfolioAcquisitionInput,
  type Property,
} from "../api";
import { fmtCurrency, fmtDate, fmtPct } from "../ui";

// Portfolio (bulk) purchases: one deal covering several properties, where each house has its
// own agreed price but the closing costs are a single settlement figure and the debt is one
// blanket loan. This is the entry surface for that — the server splits the shared costs across
// the members and writes the result onto each property's own investment row, so the return
// metrics elsewhere in the app need no knowledge that bulk deals exist.
//
// The allocation preview is computed SERVER-side (debounced) rather than here on purpose: the
// leftover-cent handling that makes the shares sum exactly to the totals lives in one place,
// so the table the user approves is byte-for-byte the split that gets saved.

const METHODS: { value: AllocationMethod; label: string; blurb: string }[] = [
  {
    value: "price",
    label: "Pro-rata by price",
    blurb:
      "Each property takes the share of the closing costs and loan that matches its share of the combined purchase price. This is how lenders and accountants apportion blanket debt, and it makes the portfolio-level returns come out exactly as if the deal were one entity.",
  },
  {
    value: "equal",
    label: "Split evenly",
    blurb:
      "Every property takes an identical share regardless of price. Use this when price is a poor proxy for how the costs were really incurred — otherwise pro-rata is the better default.",
  },
  {
    value: "custom",
    label: "Enter each share",
    blurb:
      "You give each property its own closing-cost and loan figure. Use this when the lender assigned per-property release prices — those are more accurate than any formula. The deal totals become the sum of what you enter.",
  },
];

interface MemberDraft {
  property_id: string;
  purchase_price: string;
  closing_costs: string; // custom allocation only
  loan_amount: string; // custom allocation only
}

interface FormState {
  name: string;
  purchase_date: string;
  total_closing_costs: string;
  total_loan_amount: string;
  allocation_method: AllocationMethod;
  notes: string;
  members: MemberDraft[];
}

const EMPTY_FORM: FormState = {
  name: "",
  purchase_date: "",
  total_closing_costs: "",
  total_loan_amount: "",
  allocation_method: "price",
  notes: "",
  members: [],
};

const num = (s: string) => parseFloat(s) || 0;

function toForm(d: PortfolioAcquisition): FormState {
  return {
    name: d.name,
    purchase_date: d.purchase_date,
    total_closing_costs: String(d.total_closing_costs),
    total_loan_amount: String(d.total_loan_amount),
    allocation_method: d.allocation_method,
    notes: d.notes ?? "",
    members: d.members.map((m) => ({
      property_id: m.property_id,
      purchase_price: String(m.purchase_price),
      closing_costs: String(m.closing_costs),
      loan_amount: String(m.loan_amount),
    })),
  };
}

// The request body, or null when the form isn't yet complete enough to price. Shared by the
// live preview and the save, so what you see previewed is what gets sent.
function toPayload(f: FormState): PortfolioAcquisitionInput | null {
  if (!f.name.trim() || !f.purchase_date || f.members.length < 2) return null;
  return {
    name: f.name.trim(),
    purchase_date: f.purchase_date,
    total_closing_costs: num(f.total_closing_costs),
    total_loan_amount: num(f.total_loan_amount),
    allocation_method: f.allocation_method,
    notes: f.notes.trim() || null,
    members: f.members.map((m) => ({
      property_id: m.property_id,
      purchase_price: num(m.purchase_price),
      closing_costs: f.allocation_method === "custom" ? num(m.closing_costs) : null,
      loan_amount: f.allocation_method === "custom" ? num(m.loan_amount) : null,
    })),
  };
}

export default function PortfolioAcquisitions({
  token,
  // Property ids that already have acquisition data — adding one to a deal REPLACES that data,
  // so the picker warns before it happens rather than after.
  existingInvestmentIds,
  onChanged,
}: {
  token: string;
  existingInvestmentIds: Set<string>;
  onChanged: () => void;
}) {
  const [deals, setDeals] = useState<PortfolioAcquisition[] | null>(null);
  const [properties, setProperties] = useState<Property[]>([]);
  const [error, setError] = useState<string | null>(null);
  // null = list view; "" = creating a new deal; an id = editing that deal.
  const [editing, setEditing] = useState<string | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);

  const load = () =>
    getAcquisitions(token)
      .then(setDeals)
      .catch((e) => setError(e.message));

  useEffect(() => {
    setError(null);
    load();
    listProperties(token).then(setProperties).catch(() => {
      /* the picker degrades to empty; the error surfaces on save */
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const startCreate = () => {
    setForm(EMPTY_FORM);
    setEditing("");
  };
  const startEdit = (d: PortfolioAcquisition) => {
    setForm(toForm(d));
    setEditing(d.id);
  };
  const cancel = () => {
    setEditing(null);
    setForm(EMPTY_FORM);
  };

  async function remove(d: PortfolioAcquisition) {
    const msg =
      `Delete the portfolio purchase "${d.name}"?\n\n` +
      `Its ${d.property_count} properties KEEP their allocated purchase price, closing costs and ` +
      `loan — they just stop being grouped as one deal, so no return metrics are lost.`;
    if (!confirm(msg)) return;
    setError(null);
    try {
      await deleteAcquisition(token, d.id);
      await load();
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  if (error && !deals) return <p className="alert-error">{error}</p>;
  if (!deals) return <p className="hint">Loading portfolio purchases…</p>;

  return (
    <section style={{ marginTop: 34 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <h3 className="section-title" style={{ margin: 0 }}>
          Portfolio purchases
        </h3>
        {editing === null && (
          <button className="btn btn-primary" onClick={startCreate}>
            Record a portfolio purchase
          </button>
        )}
      </div>
      <p className="hint">
        A bulk deal where several properties were bought together: each has its own agreed price, but the closing costs
        are one settlement figure and the debt is a single blanket loan. Record it here once and those shared costs are
        split across the properties — the per-property acquisition data is filled in for you, and every metric above
        picks it up automatically.
      </p>

      {error && <p className="alert-error">{error}</p>}

      {editing !== null ? (
        <AcquisitionForm
          token={token}
          form={form}
          setForm={setForm}
          properties={properties}
          existingInvestmentIds={existingInvestmentIds}
          editingId={editing || null}
          onCancel={cancel}
          onSaved={async () => {
            cancel();
            await load();
            onChanged();
          }}
        />
      ) : deals.length === 0 ? (
        <p className="hint">
          None recorded. If you bought a group of properties on one contract with one loan, record it here instead of
          hand-splitting the closing costs and loan across each property.
        </p>
      ) : (
        deals.map((d) => <DealCard key={d.id} d={d} onEdit={() => startEdit(d)} onDelete={() => remove(d)} />)
      )}
    </section>
  );
}

// ---- Read view -----------------------------------------------------------------------------
function DealCard({
  d,
  onEdit,
  onDelete,
}: {
  d: PortfolioAcquisition;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const [open, setOpen] = useState(false);
  const method = METHODS.find((m) => m.value === d.allocation_method);
  // Drift = the members no longer sum to the deal. Only a hand-edit on a property's own page
  // can cause it, and only the operator knows which side is right — so we report, never fix.
  const drift = Math.abs(d.closing_costs_drift) > 0.005 || Math.abs(d.loan_amount_drift) > 0.005;

  return (
    <div
      style={{
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        padding: "14px 16px",
        marginBottom: 12,
      }}
    >
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
        <div>
          <strong style={{ fontSize: 14.5 }}>{d.name}</strong>
          <div className="muted" style={{ fontSize: 12.5, marginTop: 3 }}>
            {fmtDate(d.purchase_date)} · {d.property_count} properties · {method?.label ?? d.allocation_method}
          </div>
        </div>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn" onClick={() => setOpen((v) => !v)}>
            {open ? "Hide split" : "Show split"}
          </button>
          <button className="btn" onClick={onEdit}>
            Edit
          </button>
          <button className="btn" onClick={onDelete}>
            Delete
          </button>
        </div>
      </div>

      <div className="row" style={{ gap: 22, marginTop: 10, flexWrap: "wrap", fontSize: 12.5 }}>
        <Stat label="Combined price" value={fmtCurrency(d.total_purchase_price)} />
        <Stat label="Closing costs" value={fmtCurrency(d.total_closing_costs)} />
        <Stat label="Portfolio loan" value={fmtCurrency(d.total_loan_amount)} />
        <Stat label="Equity invested" value={fmtCurrency(d.total_equity_invested)} />
      </div>

      {d.notes && (
        <p className="hint" style={{ marginTop: 8 }}>
          {d.notes}
        </p>
      )}

      {drift && (
        <div
          style={{
            background: "var(--warn-soft)",
            border: "1px solid var(--accent)",
            borderRadius: "var(--radius-sm)",
            padding: "10px 12px",
            marginTop: 10,
            fontSize: 12.5,
            lineHeight: 1.45,
            color: "var(--ink-2)",
          }}
        >
          <strong style={{ color: "var(--accent)" }}>The split no longer sums to this deal.</strong> The properties now
          carry {fmtCurrency(d.allocated_closing_costs)} of closing costs and {fmtCurrency(d.allocated_loan_amount)} of
          loan, against deal totals of {fmtCurrency(d.total_closing_costs)} and {fmtCurrency(d.total_loan_amount)} — a
          property was edited on its own page after the deal was recorded. Either update the totals here and re-allocate,
          or leave it if the per-property figures are the correct ones.
        </div>
      )}

      {open && (
        <div style={{ overflowX: "auto", marginTop: 12 }}>
          <MemberTable members={d.members} showShare={d.allocation_method === "price"} />
        </div>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="muted" style={{ fontSize: 11 }}>
        {label}
      </div>
      <div style={{ fontWeight: 600 }}>{value}</div>
    </div>
  );
}

function MemberTable({ members, showShare }: { members: AcquisitionMember[]; showShare: boolean }) {
  const total = (pick: (m: AcquisitionMember) => number) => members.reduce((s, m) => s + pick(m), 0);
  return (
    <table className="data-table">
      <thead>
        <tr>
          <th style={{ textAlign: "left" }}>Property</th>
          <th>Purchase price</th>
          {showShare && <th>Share</th>}
          <th>Closing costs</th>
          <th>Loan</th>
          <th>Equity</th>
        </tr>
      </thead>
      <tbody>
        {members.map((m) => (
          <tr key={m.property_id}>
            <td>{m.property_name}</td>
            <td>{fmtCurrency(m.purchase_price)}</td>
            {showShare && <td className="muted">{fmtPct(m.price_share)}</td>}
            <td>{fmtCurrency(m.closing_costs)}</td>
            <td>{fmtCurrency(m.loan_amount)}</td>
            <td>{fmtCurrency(m.equity_invested)}</td>
          </tr>
        ))}
        <tr className="row-total">
          <td>Total</td>
          <td>{fmtCurrency(total((m) => m.purchase_price))}</td>
          {showShare && <td className="muted">100.0%</td>}
          <td>{fmtCurrency(total((m) => m.closing_costs))}</td>
          <td>{fmtCurrency(total((m) => m.loan_amount))}</td>
          <td>{fmtCurrency(total((m) => m.equity_invested))}</td>
        </tr>
      </tbody>
    </table>
  );
}

// ---- Entry form ----------------------------------------------------------------------------
function AcquisitionForm({
  token,
  form,
  setForm,
  properties,
  existingInvestmentIds,
  editingId,
  onCancel,
  onSaved,
}: {
  token: string;
  form: FormState;
  setForm: React.Dispatch<React.SetStateAction<FormState>>;
  properties: Property[];
  existingInvestmentIds: Set<string>;
  editingId: string | null;
  onCancel: () => void;
  onSaved: () => void;
}) {
  const [query, setQuery] = useState("");
  const [preview, setPreview] = useState<AcquisitionPreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const selectedIds = useMemo(() => new Set(form.members.map((m) => m.property_id)), [form.members]);
  const payload = useMemo(() => toPayload(form), [form]);

  // Ask the server for the split it WOULD produce, debounced so typing a price doesn't fire a
  // request per keystroke. Server-side so the previewed cents are the saved cents.
  useEffect(() => {
    if (!payload) {
      setPreview(null);
      setPreviewError(null);
      return;
    }
    let cancelled = false;
    const id = setTimeout(() => {
      previewAcquisition(token, payload)
        .then((p) => {
          if (!cancelled) {
            setPreview(p);
            setPreviewError(null);
          }
        })
        .catch((e) => {
          if (!cancelled) {
            setPreview(null);
            setPreviewError((e as Error).message);
          }
        });
    }, 300);
    return () => {
      cancelled = true;
      clearTimeout(id);
    };
  }, [token, payload]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return properties;
    return properties.filter(
      (p) => p.name.toLowerCase().includes(q) || (p.address ?? "").toLowerCase().includes(q),
    );
  }, [properties, query]);

  const toggle = (id: string) =>
    setForm((f) => {
      const has = f.members.some((m) => m.property_id === id);
      return {
        ...f,
        members: has
          ? f.members.filter((m) => m.property_id !== id)
          : [...f.members, { property_id: id, purchase_price: "", closing_costs: "", loan_amount: "" }],
      };
    });

  const setMember = (id: string, key: keyof MemberDraft, value: string) =>
    setForm((f) => ({
      ...f,
      members: f.members.map((m) => (m.property_id === id ? { ...m, [key]: value } : m)),
    }));

  const nameOf = (id: string) => properties.find((p) => p.id === id)?.name ?? id;
  const custom = form.allocation_method === "custom";
  const method = METHODS.find((m) => m.value === form.allocation_method)!;

  // Properties joining the deal that already have acquisition data on file: saving replaces it.
  const willReplace = form.members
    .map((m) => m.property_id)
    .filter((id) => existingInvestmentIds.has(id))
    .map(nameOf);

  async function save() {
    setSaveError(null);
    if (!form.name.trim()) return setSaveError("Give the purchase a name — e.g. the seller or the street.");
    if (!form.purchase_date) return setSaveError("Enter the closing date.");
    if (form.members.length < 2)
      return setSaveError("Select at least two properties. A single-property purchase belongs on that property's own investment form.");
    const missingPrice = form.members.filter((m) => !m.purchase_price.trim()).map((m) => nameOf(m.property_id));
    if (missingPrice.length)
      return setSaveError(`Enter the agreed purchase price for: ${missingPrice.join(", ")}.`);
    if (!payload) return setSaveError("The form is incomplete.");

    setSaving(true);
    try {
      if (editingId) await updateAcquisition(token, editingId, payload);
      else await createAcquisition(token, payload);
      onSaved();
    } catch (e) {
      setSaveError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div
      style={{
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        padding: "16px 18px",
      }}
    >
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 12 }}>
        <Field label="Purchase name">
          <input
            className="input"
            value={form.name}
            onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
            placeholder="e.g. Elm Street 6-pack"
          />
        </Field>
        <Field label="Closing date (all properties)">
          <input
            className="input"
            type="date"
            value={form.purchase_date}
            onChange={(e) => setForm((f) => ({ ...f, purchase_date: e.target.value }))}
          />
        </Field>
        <Field label="Total closing costs">
          <input
            className="input"
            type="number"
            min="0"
            step="500"
            value={custom ? "" : form.total_closing_costs}
            disabled={custom}
            onChange={(e) => setForm((f) => ({ ...f, total_closing_costs: e.target.value }))}
            placeholder={custom ? "sum of the shares below" : "0"}
          />
        </Field>
        <Field label="Portfolio loan (0 = all cash)">
          <input
            className="input"
            type="number"
            min="0"
            step="1000"
            value={custom ? "" : form.total_loan_amount}
            disabled={custom}
            onChange={(e) => setForm((f) => ({ ...f, total_loan_amount: e.target.value }))}
            placeholder={custom ? "sum of the shares below" : "0"}
          />
        </Field>
      </div>

      <div style={{ marginTop: 14 }}>
        <span style={{ fontSize: 12.5, color: "var(--ink-3)" }}>How should the shared costs be split?</span>
        <div className="row" style={{ gap: 8, marginTop: 6, flexWrap: "wrap" }}>
          {METHODS.map((m) => (
            <button
              key={m.value}
              type="button"
              className={`tag-chip${form.allocation_method === m.value ? " is-active" : ""}`}
              aria-pressed={form.allocation_method === m.value}
              onClick={() => setForm((f) => ({ ...f, allocation_method: m.value }))}
            >
              {m.label}
            </button>
          ))}
        </div>
        <p className="hint" style={{ marginTop: 6 }}>
          {method.blurb}
        </p>
      </div>

      <h4 className="section-title" style={{ fontSize: 13, marginTop: 18, marginBottom: 6 }}>
        Properties in this purchase
      </h4>
      <input
        className="input"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Filter properties…"
        style={{ maxWidth: 280, marginBottom: 8 }}
      />
      <div
        style={{
          maxHeight: 190,
          overflowY: "auto",
          border: "1px solid var(--border)",
          borderRadius: "var(--radius-sm)",
          padding: "6px 10px",
        }}
      >
        {filtered.length === 0 && <p className="hint">No properties match that filter.</p>}
        {filtered.map((p) => (
          <label
            key={p.id}
            className="row"
            style={{ gap: 8, alignItems: "center", padding: "3px 0", fontSize: 13, cursor: "pointer" }}
          >
            <input type="checkbox" checked={selectedIds.has(p.id)} onChange={() => toggle(p.id)} />
            <span>{p.name}</span>
            {existingInvestmentIds.has(p.id) && (
              <span className="muted" style={{ fontSize: 11 }}>
                · has acquisition data
              </span>
            )}
          </label>
        ))}
      </div>

      {form.members.length > 0 && (
        <div style={{ overflowX: "auto", marginTop: 12 }}>
          <table className="data-table">
            <thead>
              <tr>
                <th style={{ textAlign: "left" }}>Property</th>
                <th>Agreed purchase price</th>
                {custom && <th>Closing costs</th>}
                {custom && <th>Loan</th>}
              </tr>
            </thead>
            <tbody>
              {form.members.map((m) => (
                <tr key={m.property_id}>
                  <td>{nameOf(m.property_id)}</td>
                  <td>
                    <input
                      className="input"
                      type="number"
                      min="0"
                      step="1000"
                      value={m.purchase_price}
                      onChange={(e) => setMember(m.property_id, "purchase_price", e.target.value)}
                      placeholder="0"
                      style={{ maxWidth: 140 }}
                    />
                  </td>
                  {custom && (
                    <td>
                      <input
                        className="input"
                        type="number"
                        min="0"
                        step="100"
                        value={m.closing_costs}
                        onChange={(e) => setMember(m.property_id, "closing_costs", e.target.value)}
                        placeholder="0"
                        style={{ maxWidth: 130 }}
                      />
                    </td>
                  )}
                  {custom && (
                    <td>
                      <input
                        className="input"
                        type="number"
                        min="0"
                        step="1000"
                        value={m.loan_amount}
                        onChange={(e) => setMember(m.property_id, "loan_amount", e.target.value)}
                        placeholder="0"
                        style={{ maxWidth: 140 }}
                      />
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {willReplace.length > 0 && (
        <p className="hint" style={{ marginTop: 10 }}>
          <strong>Note:</strong> {willReplace.join(", ")} already {willReplace.length === 1 ? "has" : "have"} acquisition
          data on file. Saving replaces {willReplace.length === 1 ? "it" : "them"} with this deal's figures.
        </p>
      )}

      {previewError && <p className="alert-error">{previewError}</p>}

      {preview && preview.members.length > 0 && (
        <div style={{ marginTop: 14 }}>
          <h4 className="section-title" style={{ fontSize: 13, marginBottom: 6 }}>
            How this splits
          </h4>
          <div style={{ overflowX: "auto" }}>
            <MemberTable members={preview.members} showShare={form.allocation_method === "price"} />
          </div>
          <p className="hint" style={{ marginTop: 8 }}>
            The shares add back to exactly {fmtCurrency(preview.total_closing_costs)} of closing costs and{" "}
            {fmtCurrency(preview.total_loan_amount)} of loan, so your portfolio-wide cap rate, cash-on-cash and DSCR come
            out the same as they would if you modelled the whole deal as one property. Per-property cap rate stays fully
            real — it only uses the agreed price. Per-property cash-on-cash and DSCR are share-based: each house carries
            its slice of the blanket loan.
          </p>
          {preview.total_equity_invested < 0 && (
            <p className="alert-error">
              The loan exceeds the combined price plus closing costs, so equity invested is negative. Cash-on-cash can't
              be read meaningfully from a negative basis — check the loan amount.
            </p>
          )}
          {preview.total_loan_amount > 0 && (
            <p className="hint">
              <strong>One thing to keep consistent:</strong> DSCR reads the debt service actually recorded in each
              property's P&amp;L, which this deal doesn't write — it only records the acquisition. Set the blanket loan's
              monthly payment up as a <strong>shared expense</strong> on the Data Entry screen and split it the same way,
              or one property will look over-levered and the rest debt-free.
            </p>
          )}
        </div>
      )}

      {saveError && <p className="alert-error">{saveError}</p>}

      <div className="row" style={{ gap: 8, marginTop: 12 }}>
        <button className="btn btn-primary" onClick={save} disabled={saving}>
          {saving ? "Saving…" : editingId ? "Save & re-allocate" : "Save purchase"}
        </button>
        <button className="btn" onClick={onCancel} disabled={saving}>
          Cancel
        </button>
      </div>
    </div>
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
