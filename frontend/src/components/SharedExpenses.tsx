import { useEffect, useMemo, useState } from "react";
import {
  createSharedExpense,
  deleteSharedExpense,
  getSharedExpenses,
  listCategories,
  listProperties,
  postSharedExpense,
  previewSharedPost,
  previewSharedSplit,
  unpostSharedExpense,
  updateSharedExpense,
  type Category,
  type Property,
  type SharedAllocationMethod,
  type SharedExpense,
  type SharedExpenseInput,
  type SharedExpenseMember,
  type SharedExpensePostPlan,
  type SharedExpenseSplit,
  type SharedOnConflict,
} from "../api";
import { fmtCurrency, fmtMonth } from "../ui";

// Costs incurred for several properties at once — a blanket-loan payment, one insurance policy
// over multiple buildings, a single management retainer. The ledger is per-property, so such a
// bill otherwise has to be divided by hand and re-typed onto every property, every month.
//
// Here the arrangement is entered ONCE and then "posted" to a month or a range, which writes an
// ordinary property-tier line item onto each member. Nothing else in the app changes: the
// dashboards, NOI and exports read those line items exactly as they read hand-entered ones.
//
// Two things are deliberately server-side rather than computed here:
//   * the split — the leftover-cent handling that makes the shares add back to the exact bill
//     lives in one place, so the table you approve is the split that gets written; and
//   * the post plan — the dry run comes from the same code path as the real write, so what the
//     preview says will happen is what happens.

const METHODS: { value: SharedAllocationMethod; label: string; blurb: string }[] = [
  {
    value: "equal",
    label: "Split evenly",
    blurb:
      "Every property carries the same share. The straightforward reading of a shared bill, and the right one when the cost isn't obviously driven by size — a flat management retainer, or a loan you simply want spread evenly.",
  },
  {
    value: "price",
    label: "By purchase price",
    blurb:
      "Each property takes the share of the bill matching its share of the combined purchase price. This is how blanket debt is conventionally apportioned, so a larger asset carries more of the payment. Properties with no acquisition data on file weigh nothing here.",
  },
  {
    value: "units",
    label: "By unit count",
    blurb:
      "Pro-rata by rentable doors, for costs that scale with them — insurance and management usually do. A single-family house counts as one dwelling rather than zero.",
  },
  {
    value: "custom",
    label: "Enter each share",
    blurb:
      "You give each property its own figure — the insurer's per-building premium, or the lender's per-property allocation. Those are more accurate than any formula. The bill becomes the sum of what you enter.",
  },
];

const CONFLICT_OPTIONS: { value: SharedOnConflict; label: string; blurb: string }[] = [
  {
    value: "fail",
    label: "Stop and tell me",
    blurb: "Nothing is written if any of these months already has a figure in this category that wasn't posted from here.",
  },
  {
    value: "skip",
    label: "Leave those alone",
    blurb: "Post everywhere else and leave the property-months that already have a figure exactly as they are.",
  },
  {
    value: "replace",
    label: "Overwrite them",
    blurb: "Replace the existing figures with this arrangement's shares. Use this when the hand-entered numbers were the stopgap.",
  },
];

interface MemberDraft {
  property_id: string;
  custom_share: string; // custom allocation only
}

interface FormState {
  name: string;
  category_id: string;
  amount: string;
  allocation_method: SharedAllocationMethod;
  notes: string;
  members: MemberDraft[];
}

const EMPTY_FORM: FormState = {
  name: "",
  category_id: "",
  amount: "",
  allocation_method: "equal",
  notes: "",
  members: [],
};

const num = (s: string) => parseFloat(s) || 0;
const thisMonth = () => new Date().toISOString().slice(0, 7); // YYYY-MM
const firstOf = (yyyymm: string) => `${yyyymm}-01`;

function toForm(e: SharedExpense): FormState {
  return {
    name: e.name,
    category_id: e.category_id,
    amount: String(e.amount),
    allocation_method: e.allocation_method,
    notes: e.notes ?? "",
    members: e.members.map((m) => ({ property_id: m.property_id, custom_share: String(m.amount) })),
  };
}

// The request body, or null when the form isn't complete enough to split. Shared by the live
// preview and the save, so what you see previewed is what gets sent.
function toPayload(f: FormState): SharedExpenseInput | null {
  if (!f.name.trim() || !f.category_id || f.members.length < 2) return null;
  return {
    name: f.name.trim(),
    category_id: f.category_id,
    amount: num(f.amount),
    allocation_method: f.allocation_method,
    notes: f.notes.trim() || null,
    members: f.members.map((m) => ({
      property_id: m.property_id,
      custom_share: f.allocation_method === "custom" ? num(m.custom_share) : null,
    })),
  };
}

export default function SharedExpenses({ token, isAdmin }: { token: string; isAdmin: boolean }) {
  const [expenses, setExpenses] = useState<SharedExpense[] | null>(null);
  const [properties, setProperties] = useState<Property[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [error, setError] = useState<string | null>(null);
  // null = list view; "" = creating a new arrangement; an id = editing that one.
  const [editing, setEditing] = useState<string | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);

  const load = () =>
    getSharedExpenses(token)
      .then(setExpenses)
      .catch((e) => setError((e as Error).message));

  useEffect(() => {
    setError(null);
    load();
    listProperties(token).then(setProperties).catch(() => {
      /* the picker degrades to empty; the error surfaces on save */
    });
    listCategories(token, true).then(setCategories).catch(() => setCategories([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const startCreate = () => {
    setForm(EMPTY_FORM);
    setEditing("");
  };
  const cancel = () => {
    setEditing(null);
    setForm(EMPTY_FORM);
  };

  async function remove(e: SharedExpense) {
    const posted = e.posted_months.length;
    const msg =
      `Delete "${e.name}"?\n\n` +
      (posted
        ? `The ${posted} month(s) it has already posted STAY in the ledger — the money was really ` +
          `spent, so those line items simply stop being linked to this arrangement. You just lose ` +
          `the ability to post or un-post it as a group.`
        : `It hasn't posted anything yet, so nothing in the ledger changes.`);
    if (!confirm(msg)) return;
    setError(null);
    try {
      await deleteSharedExpense(token, e.id);
      await load();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  if (error && !expenses) return <p className="alert-error">{error}</p>;
  if (!expenses) return <p className="hint">Loading shared expenses…</p>;

  return (
    <section style={{ marginTop: 34 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <h2 className="section-title" style={{ margin: 0 }}>
          Shared expenses
        </h2>
        {editing === null && isAdmin && (
          <button className="btn btn-primary" onClick={startCreate}>
            New shared expense
          </button>
        )}
      </div>
      <p className="hint">
        A bill that covers several properties at once — the payment on a portfolio loan, an insurance policy written over
        multiple buildings, one management retainer. Set it up here once and post it to a month or a whole year, and each
        property gets its own share as an ordinary line item. The shares always add back to the exact bill, so your
        portfolio P&amp;L still matches the statement.
      </p>

      {error && <p className="alert-error">{error}</p>}

      {editing !== null ? (
        <SharedExpenseForm
          token={token}
          form={form}
          setForm={setForm}
          properties={properties}
          categories={categories}
          editingId={editing || null}
          onCancel={cancel}
          onSaved={async () => {
            cancel();
            await load();
          }}
        />
      ) : expenses.length === 0 ? (
        <p className="hint">
          None set up. If you're typing the same expense onto several properties every month — a blanket loan payment, a
          multi-property policy — set it up here instead and post it in one action.
        </p>
      ) : (
        expenses.map((e) => (
          <ExpenseCard
            key={e.id}
            token={token}
            e={e}
            isAdmin={isAdmin}
            onEdit={() => {
              setForm(toForm(e));
              setEditing(e.id);
            }}
            onDelete={() => remove(e)}
            onPosted={load}
          />
        ))
      )}
    </section>
  );
}

// ---- Read view -----------------------------------------------------------------------------
function ExpenseCard({
  token,
  e,
  isAdmin,
  onEdit,
  onDelete,
  onPosted,
}: {
  token: string;
  e: SharedExpense;
  isAdmin: boolean;
  onEdit: () => void;
  onDelete: () => void;
  onPosted: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [posting, setPosting] = useState(false);
  const method = METHODS.find((m) => m.value === e.allocation_method);

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
          <strong style={{ fontSize: 14.5 }}>{e.name}</strong>
          <div className="muted" style={{ fontSize: 12.5, marginTop: 3 }}>
            {fmtCurrency(e.amount)}/month · {e.property_count} properties · {e.category_name ?? "—"} ·{" "}
            {method?.label ?? e.allocation_method}
          </div>
        </div>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn" onClick={() => setOpen((v) => !v)}>
            {open ? "Hide split" : "Show split"}
          </button>
          {isAdmin && (
            <>
              <button className="btn btn-primary" onClick={() => setPosting((v) => !v)}>
                {posting ? "Close" : "Post to months…"}
              </button>
              <button className="btn" onClick={onEdit}>
                Edit
              </button>
              <button className="btn" onClick={onDelete}>
                Delete
              </button>
            </>
          )}
        </div>
      </div>

      <PostedMonths months={e.posted_months} />

      {e.notes && (
        <p className="hint" style={{ marginTop: 8 }}>
          {e.notes}
        </p>
      )}

      {open && (
        <div style={{ overflowX: "auto", marginTop: 12 }}>
          <SplitTable members={e.members} method={e.allocation_method} />
        </div>
      )}

      {posting && (
        <PostPanel
          token={token}
          e={e}
          onDone={() => {
            setPosting(false);
            onPosted();
          }}
        />
      )}
    </div>
  );
}

// Which months this arrangement is currently applied to. Contiguous runs are collapsed
// ("Jan 2026 – Dec 2026") because a year of monthly postings listed one by one is unreadable.
function PostedMonths({ months }: { months: string[] }) {
  if (months.length === 0)
    return (
      <p className="hint" style={{ marginTop: 8 }}>
        Not posted to any month yet — nothing from this arrangement is in the ledger.
      </p>
    );

  const runs: string[][] = [];
  for (const m of months) {
    const last = runs[runs.length - 1];
    if (last && isNextMonth(last[last.length - 1], m)) last.push(m);
    else runs.push([m]);
  }
  const label = runs
    .map((r) => (r.length === 1 ? fmtMonth(r[0]) : `${fmtMonth(r[0])} – ${fmtMonth(r[r.length - 1])}`))
    .join(", ");

  // Months posted ahead of the calendar are scheduled, not actual: they sit in the ledger and
  // start counting on their own as each one arrives. Saying so here is the point — otherwise
  // posting a year forward looks like it silently rewrote this year's numbers.
  const now = thisMonth();
  const ahead = months.filter((m) => m.slice(0, 7) > now).length;

  return (
    <p className="hint" style={{ marginTop: 8 }}>
      Posted to {months.length} month{months.length === 1 ? "" : "s"}: {label}
      {ahead > 0 && (
        <>
          {" — "}
          {ahead} of them scheduled ahead, which won't affect your current figures until each
          month arrives.
        </>
      )}
    </p>
  );
}

function isNextMonth(a: string, b: string): boolean {
  const [ay, am] = a.split("-").map(Number);
  const [by, bm] = b.split("-").map(Number);
  return by * 12 + bm === ay * 12 + am + 1;
}

function SplitTable({
  members,
  method,
}: {
  members: SharedExpenseMember[];
  method: SharedAllocationMethod;
}) {
  const total = members.reduce((s, m) => s + m.amount, 0);
  const basisLabel = method === "price" ? "Purchase price" : method === "units" ? "Units" : null;
  return (
    <table className="data-table">
      <thead>
        <tr>
          <th style={{ textAlign: "left" }}>Property</th>
          {basisLabel && <th>{basisLabel}</th>}
          {basisLabel && <th>Share</th>}
          <th>Per month</th>
        </tr>
      </thead>
      <tbody>
        {members.map((m) => (
          <tr key={m.property_id}>
            <td>{m.property_name}</td>
            {basisLabel && (
              <td className="muted">{method === "price" ? fmtCurrency(m.basis) : m.basis}</td>
            )}
            {basisLabel && <td className="muted">{(m.weight_share * 100).toFixed(1)}%</td>}
            <td>{fmtCurrency(m.amount)}</td>
          </tr>
        ))}
        <tr className="row-total">
          <td>Total</td>
          {basisLabel && <td />}
          {basisLabel && <td className="muted">100.0%</td>}
          <td>{fmtCurrency(total)}</td>
        </tr>
      </tbody>
    </table>
  );
}

// ---- Posting -------------------------------------------------------------------------------
function PostPanel({ token, e, onDone }: { token: string; e: SharedExpense; onDone: () => void }) {
  const [from, setFrom] = useState(thisMonth());
  const [to, setTo] = useState(thisMonth());
  const [onConflict, setOnConflict] = useState<SharedOnConflict>("fail");
  const [plan, setPlan] = useState<SharedExpensePostPlan | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Dry-run the post whenever the range or conflict policy changes. Debounced, and computed by
  // the server so the plan can't disagree with the write it is describing.
  useEffect(() => {
    if (!from || !to) return;
    let cancelled = false;
    const id = setTimeout(() => {
      previewSharedPost(token, e.id, {
        from_month: firstOf(from),
        to_month: firstOf(to),
        on_conflict: onConflict,
      })
        .then((p) => {
          if (cancelled) return;
          setPlan(p);
          setPlanError(null);
        })
        .catch((err) => {
          if (cancelled) return;
          setPlan(null);
          setPlanError((err as Error).message);
        });
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(id);
    };
  }, [token, e.id, from, to, onConflict]);

  const conflicts = plan?.rows.filter((r) => r.existing_source === "manual" || r.existing_source === "other_shared") ?? [];
  const locked = plan?.rows.filter((r) => r.locked) ?? [];
  const updates = plan?.rows.filter((r) => r.existing_source === "this") ?? [];

  async function doPost() {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const r = await postSharedExpense(token, e.id, {
        from_month: firstOf(from),
        to_month: firstOf(to),
        on_conflict: onConflict,
      });
      setResult(
        `Posted ${fmtCurrency(r.total_posted)} across ${r.line_items_written} line item(s) in ` +
          `${r.months.length} month(s)` +
          (r.skipped ? `, leaving ${r.skipped} property-month(s) untouched` : "") +
          ".",
      );
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function doUnpost() {
    if (
      !confirm(
        `Remove this arrangement's postings from ${fmtMonth(firstOf(from))} to ${fmtMonth(firstOf(to))}?\n\n` +
          `Only the line items posted from here are removed. Anything you entered by hand in the same ` +
          `category stays exactly where it is.`,
      )
    )
      return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const r = await unpostSharedExpense(token, e.id, firstOf(from), firstOf(to));
      setResult(`Removed ${r.line_items_removed} line item(s) totalling ${fmtCurrency(r.total_removed)}.`);
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      style={{
        marginTop: 14,
        paddingTop: 14,
        borderTop: "1px solid var(--border)",
      }}
    >
      <div className="row" style={{ gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
        <Field label="From month">
          <input className="input" type="month" value={from} onChange={(ev) => {
            setFrom(ev.target.value);
            if (ev.target.value > to) setTo(ev.target.value);
          }} />
        </Field>
        <Field label="To month">
          <input className="input" type="month" value={to} min={from} onChange={(ev) => setTo(ev.target.value)} />
        </Field>
      </div>

      <div style={{ marginTop: 12 }}>
        <span style={{ fontSize: 12.5, color: "var(--ink-3)" }}>
          If a month already has a {e.category_name ?? "matching"} figure that wasn't posted from here:
        </span>
        <div className="row" style={{ gap: 8, marginTop: 6, flexWrap: "wrap" }}>
          {CONFLICT_OPTIONS.map((o) => (
            <button
              key={o.value}
              type="button"
              className={`tag-chip${onConflict === o.value ? " is-active" : ""}`}
              aria-pressed={onConflict === o.value}
              onClick={() => setOnConflict(o.value)}
            >
              {o.label}
            </button>
          ))}
        </div>
        <p className="hint" style={{ marginTop: 6 }}>
          {CONFLICT_OPTIONS.find((o) => o.value === onConflict)!.blurb}
        </p>
      </div>

      {planError && <p className="alert-error">{planError}</p>}

      {plan && (
        <div style={{ marginTop: 10, fontSize: 12.5, lineHeight: 1.5, color: "var(--ink-2)" }}>
          <p style={{ margin: 0 }}>
            {plan.months.length} month{plan.months.length === 1 ? "" : "s"} × {e.property_count} properties ={" "}
            <strong>{plan.rows.length} line items</strong>, totalling{" "}
            <strong>{fmtCurrency(plan.total_amount)}</strong>.
            {updates.length > 0 && ` ${updates.length} of them already posted from here and will be updated in place.`}
          </p>

          {locked.length > 0 && (
            <Callout tone="error">
              <strong>{locked.length} property-month(s) are locked</strong> and can't be written to:{" "}
              {locked.slice(0, 6).map((r) => `${r.property_name} ${fmtMonth(r.month)}`).join(", ")}
              {locked.length > 6 ? `, and ${locked.length - 6} more` : ""}. An admin has to unlock them on the Data
              Entry screen first — a bulk action shouldn't be able to quietly edit a closed month.
            </Callout>
          )}

          {conflicts.length > 0 && (
            <Callout tone={onConflict === "fail" ? "warn" : "info"}>
              <strong>
                {conflicts.length} property-month(s) already have a {e.category_name ?? "matching"} figure
              </strong>{" "}
              that this arrangement didn't post:{" "}
              {conflicts
                .slice(0, 6)
                .map(
                  (r) =>
                    `${r.property_name} ${fmtMonth(r.month)} (${fmtCurrency(r.existing_amount ?? 0)}${
                      r.existing_source === "other_shared" ? ", from another shared expense" : ""
                    })`,
                )
                .join(", ")}
              {conflicts.length > 6 ? `, and ${conflicts.length - 6} more` : ""}.{" "}
              {onConflict === "fail"
                ? "Nothing will be written until you choose what to do with them."
                : onConflict === "skip"
                  ? "These will be left exactly as they are."
                  : "These will be overwritten with this arrangement's shares."}
            </Callout>
          )}
        </div>
      )}

      {error && <p className="alert-error">{error}</p>}
      {result && <p className="hint" style={{ color: "var(--ink-1)" }}>{result}</p>}

      <div className="row" style={{ gap: 8, marginTop: 12 }}>
        <button className="btn btn-primary" onClick={doPost} disabled={busy || !plan || plan.blocked}>
          {busy ? "Working…" : plan ? `Post ${fmtCurrency(plan.total_amount)}` : "Post"}
        </button>
        <button className="btn" onClick={doUnpost} disabled={busy}>
          Un-post this range
        </button>
      </div>
      <p className="hint" style={{ marginTop: 6 }}>
        Posting the same months again is safe: it updates what's already there rather than adding to it. That's how you
        correct a bill — change the amount above, then re-post the affected months.
      </p>
      {plan && plan.months.some((m) => m.slice(0, 7) > thisMonth()) && (
        <p className="hint">
          Months past {fmtMonth(firstOf(thisMonth()))} are <strong>scheduled</strong>: they go into the ledger now, but
          your dashboards, cap rate and cash-on-cash keep measuring months that have actually happened, so posting a year
          ahead won't drag today's figures negative. Each one starts counting once it arrives and you record the rent.
        </p>
      )}
    </div>
  );
}

function Callout({ tone, children }: { tone: "error" | "warn" | "info"; children: React.ReactNode }) {
  const border = tone === "error" ? "var(--negative)" : tone === "warn" ? "var(--accent)" : "var(--border)";
  const bg = tone === "error" ? "var(--negative-soft)" : tone === "warn" ? "var(--warn-soft)" : "transparent";
  return (
    <div
      style={{
        background: bg,
        border: `1px solid ${border}`,
        borderRadius: "var(--radius-sm)",
        padding: "10px 12px",
        marginTop: 10,
      }}
    >
      {children}
    </div>
  );
}

// ---- Entry form ----------------------------------------------------------------------------
function SharedExpenseForm({
  token,
  form,
  setForm,
  properties,
  categories,
  editingId,
  onCancel,
  onSaved,
}: {
  token: string;
  form: FormState;
  setForm: React.Dispatch<React.SetStateAction<FormState>>;
  properties: Property[];
  categories: Category[];
  editingId: string | null;
  onCancel: () => void;
  onSaved: () => void;
}) {
  const [query, setQuery] = useState("");
  const [split, setSplit] = useState<SharedExpenseSplit | null>(null);
  const [splitError, setSplitError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const selectedIds = useMemo(() => new Set(form.members.map((m) => m.property_id)), [form.members]);
  const payload = useMemo(() => toPayload(form), [form]);
  const custom = form.allocation_method === "custom";
  const method = METHODS.find((m) => m.value === form.allocation_method)!;

  // Ask the server for the split it WOULD produce, debounced so typing an amount doesn't fire a
  // request per keystroke. Server-side so the previewed cents are the posted cents.
  useEffect(() => {
    if (!payload) {
      setSplit(null);
      setSplitError(null);
      return;
    }
    let cancelled = false;
    const id = setTimeout(() => {
      previewSharedSplit(token, payload)
        .then((p) => {
          if (cancelled) return;
          setSplit(p);
          setSplitError(null);
        })
        .catch((err) => {
          if (cancelled) return;
          setSplit(null);
          setSplitError((err as Error).message);
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
          : [...f.members, { property_id: id, custom_share: "" }],
      };
    });

  const setMember = (id: string, value: string) =>
    setForm((f) => ({
      ...f,
      members: f.members.map((m) => (m.property_id === id ? { ...m, custom_share: value } : m)),
    }));

  const nameOf = (id: string) => properties.find((p) => p.id === id)?.name ?? id;
  const customTotal = form.members.reduce((s, m) => s + num(m.custom_share), 0);
  // A member the chosen basis weights at zero pays nothing while the others silently cover it.
  // Most often a property with no acquisition data under a price split — worth saying out loud,
  // because the split still sums to the bill and so looks correct at a glance.
  const zeroWeighted =
    split && form.allocation_method !== "custom"
      ? split.members.filter((m) => m.basis === 0).map((m) => m.property_name)
      : [];

  async function save() {
    setSaveError(null);
    if (!form.name.trim()) return setSaveError("Give it a name — e.g. the lender, or the policy.");
    if (!form.category_id) return setSaveError("Pick the category these line items should be filed under.");
    if (form.members.length < 2)
      return setSaveError(
        "Select at least two properties. A cost borne by one property is an ordinary line item — enter it on that property's month above.",
      );
    if (!custom && num(form.amount) <= 0) return setSaveError("Enter the monthly bill.");
    if (custom) {
      const blank = form.members.filter((m) => !m.custom_share.trim()).map((m) => nameOf(m.property_id));
      if (blank.length) return setSaveError(`Enter a share for: ${blank.join(", ")}.`);
    }
    if (!payload) return setSaveError("The form is incomplete.");

    setSaving(true);
    try {
      if (editingId) await updateSharedExpense(token, editingId, payload);
      else await createSharedExpense(token, payload);
      onSaved();
    } catch (err) {
      setSaveError((err as Error).message);
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
        <Field label="Name">
          <input
            className="input"
            value={form.name}
            onChange={(ev) => setForm((f) => ({ ...f, name: ev.target.value }))}
            placeholder="e.g. Blanket loan — monthly payment"
          />
        </Field>
        <Field label="Category">
          <select
            className="input"
            value={form.category_id}
            onChange={(ev) => setForm((f) => ({ ...f, category_id: ev.target.value }))}
          >
            <option value="">Select…</option>
            {categories.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Bill per month">
          <input
            className="input"
            type="number"
            min="0"
            step="50"
            value={custom ? "" : form.amount}
            disabled={custom}
            onChange={(ev) => setForm((f) => ({ ...f, amount: ev.target.value }))}
            placeholder={custom ? "sum of the shares below" : "0"}
          />
        </Field>
        <Field label="Note (optional)">
          <input
            className="input"
            value={form.notes}
            onChange={(ev) => setForm((f) => ({ ...f, notes: ev.target.value }))}
            placeholder="e.g. policy number, loan reference"
          />
        </Field>
      </div>

      <div style={{ marginTop: 14 }}>
        <span style={{ fontSize: 12.5, color: "var(--ink-3)" }}>How should the bill be split?</span>
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
        Properties this covers
      </h4>
      <input
        className="input"
        value={query}
        onChange={(ev) => setQuery(ev.target.value)}
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
          </label>
        ))}
      </div>

      {custom && form.members.length > 0 && (
        <div style={{ overflowX: "auto", marginTop: 12 }}>
          <table className="data-table">
            <thead>
              <tr>
                <th style={{ textAlign: "left" }}>Property</th>
                <th>Share per month</th>
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
                      step="50"
                      value={m.custom_share}
                      onChange={(ev) => setMember(m.property_id, ev.target.value)}
                      placeholder="0"
                      style={{ maxWidth: 140 }}
                    />
                  </td>
                </tr>
              ))}
              <tr className="row-total">
                <td>Bill per month</td>
                <td>{fmtCurrency(customTotal)}</td>
              </tr>
            </tbody>
          </table>
        </div>
      )}

      {splitError && <p className="alert-error">{splitError}</p>}

      {split && split.members.length > 0 && (
        <div style={{ marginTop: 14 }}>
          <h4 className="section-title" style={{ fontSize: 13, marginBottom: 6 }}>
            How this splits, every month
          </h4>
          <div style={{ overflowX: "auto" }}>
            <SplitTable members={split.members} method={form.allocation_method} />
          </div>
          <p className="hint" style={{ marginTop: 8 }}>
            The shares add back to exactly {fmtCurrency(split.total_amount)}, so posting this leaves your portfolio total
            equal to the real bill rather than a rounding of it. Each share lands as a property-level line item, so it
            stays out of your unit-by-unit figures — the same place capex and debt service already live.
          </p>
          {zeroWeighted.length > 0 && (
            <Callout tone="warn">
              <strong>
                {zeroWeighted.join(", ")} {zeroWeighted.length === 1 ? "carries" : "carry"} none of this bill.
              </strong>{" "}
              {form.allocation_method === "price"
                ? "Splitting by purchase price needs acquisition data, and there's none on file for them — so the rest of the properties are absorbing their share. Add their purchase price on the Investments screen, or split evenly instead."
                : "Their weighting works out to zero, so the rest of the properties are absorbing their share."}
            </Callout>
          )}
        </div>
      )}

      {saveError && <p className="alert-error">{saveError}</p>}

      <div className="row" style={{ gap: 8, marginTop: 12 }}>
        <button className="btn btn-primary" onClick={save} disabled={saving}>
          {saving ? "Saving…" : editingId ? "Save changes" : "Save"}
        </button>
        <button className="btn" onClick={onCancel} disabled={saving}>
          Cancel
        </button>
      </div>
      {editingId && (
        <p className="hint" style={{ marginTop: 8 }}>
          Saving changes the definition only — months you've already posted keep the figures they were posted with, so a
          renewal or a new member doesn't silently restate closed periods. Re-post a month to bring it in line.
        </p>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label
      className="col"
      style={{ display: "flex", flexDirection: "column", gap: 5, fontSize: 12.5, color: "var(--ink-3)" }}
    >
      <span>{label}</span>
      {children}
    </label>
  );
}
