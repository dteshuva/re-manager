import { useEffect, useState } from "react";
import {
  addPropertyTag,
  CLASSIFICATIONS,
  createCategory,
  createProperty,
  createUnit,
  deleteProperty,
  deletePropertyBudget,
  deleteUnit,
  getAuditLog,
  getMe,
  getPropertyTags,
  listCategories,
  listProperties,
  listPropertyBudgets,
  listUnits,
  putPropertyBudget,
  removePropertyTag,
  updateCategory,
  type AuditLogEntry,
  type Category,
  type Classification,
  type Property,
  type PropertyBudgetOut,
  type Unit,
} from "../api";
import AttentionSettings from "../components/AttentionSettings";
import CurrencySettings from "../components/CurrencySettings";
import PropertySearchSelect from "../components/PropertySearchSelect";
import { btn, btnPrimary, card, fmtCurrency, fmtDateTime, input } from "../ui";

// Manage properties, their units, and the global category list. The category section
// doubles as the reclassification control (changing default_classification recomputes
// NOI/cash flow with no migration — a fuller UI lands in Phase 7).
export default function Manage({ token }: { token: string }) {
  const [properties, setProperties] = useState<Property[]>([]);
  const [isAdmin, setIsAdmin] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = () => listProperties(token).then(setProperties).catch((e) => setError(e.message));
  useEffect(() => {
    reload();
  }, [token]);

  useEffect(() => {
    let cancelled = false;
    getMe(token).then((m) => !cancelled && setIsAdmin(m.role === "admin")).catch(() => !cancelled && setIsAdmin(false));
    return () => {
      cancelled = true;
    };
  }, [token]);

  const wrap = (p: Promise<unknown>) => p.then(reload).catch((e) => setError(e.message));

  return (
    <section>
      {error && <p className="alert-error">{error}</p>}

      <h2 className="section-title">Properties</h2>
      <NewPropertyForm onCreate={(b) => wrap(createProperty(token, b))} />
      {properties.map((p) => (
        <PropertyCard
          key={p.id}
          token={token}
          property={p}
          isAdmin={isAdmin}
          onDelete={() => wrap(deleteProperty(token, p.id))}
          onError={setError}
        />
      ))}

      <CategorySection token={token} onError={setError} />

      <BudgetSection token={token} properties={properties} />

      <h2 className="section-title">Display currency</h2>
      <CurrencySettings token={token} />

      <h2 className="section-title">Attention thresholds</h2>
      <AttentionSettings token={token} />

      <AuditLogSection token={token} />
    </section>
  );
}

function AuditLogSection({ token }: { token: string }) {
  const [rows, setRows] = useState<AuditLogEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // GET /audit is admin-only (server-gated). Mirror the graceful-degradation pattern
  // AttentionSettings uses: resolve the current user first and only call the admin
  // endpoint when they're an admin, so a member never triggers a 403 error banner.
  const [isAdmin, setIsAdmin] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    getMe(token)
      .then((m) => !cancelled && setIsAdmin(m.role === "admin"))
      .catch(() => !cancelled && setIsAdmin(false));
    return () => {
      cancelled = true;
    };
  }, [token]);

  useEffect(() => {
    if (!isAdmin) return;
    let cancelled = false;
    setLoading(true);
    getAuditLog(token, { limit: 100 })
      .then((r) => !cancelled && setRows(r))
      .catch((e) => !cancelled && setError(e.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [token, isAdmin]);

  // Non-admins (or while the role is still resolving) see nothing — the audit log is an
  // admin-only surface, so there's no read-only equivalent to show.
  if (!isAdmin) return null;

  return (
    <div style={{ marginTop: 30 }}>
      <h2 className="section-title">Audit log</h2>
      <p className="hint">
        Who changed what, and when — currently captured on every admin unlock of a locked
        period. Most recent first.
      </p>
      {error && <p className="alert-error">{error}</p>}
      {loading ? (
        <p className="hint">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="hint">No audit events yet.</p>
      ) : (
        <table className="data-table" style={{ textAlign: "left" }}>
          <thead>
            <tr>
              <th style={{ textAlign: "left" }}>When</th>
              <th style={{ textAlign: "left" }}>Who</th>
              <th style={{ textAlign: "left" }}>Action</th>
              <th style={{ textAlign: "left" }}>Entity</th>
              <th style={{ textAlign: "left" }}>Before → After</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{fmtDateTime(r.created_at)}</td>
                <td>{r.user_email ?? "—"}</td>
                <td>{r.action}</td>
                <td>
                  {r.entity}
                  {r.entity_id ? ` (${r.entity_id.slice(0, 8)}…)` : ""}
                </td>
                <td>
                  {r.before ? JSON.stringify(r.before) : "—"} → {r.after ? JSON.stringify(r.after) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// Flat annual budget per property/year — the actual-vs-plan input surface. Reads (list) are
// open to any authed user; writing (PUT/DELETE) is admin-gated server-side, so the entry
// form here is disabled (not hidden — same pattern as AttentionSettings) for non-admins.
function BudgetSection({ token, properties }: { token: string; properties: Property[] }) {
  const [propertyId, setPropertyId] = useState("");
  const [budgets, setBudgets] = useState<PropertyBudgetOut[]>([]);
  const [year, setYear] = useState(String(new Date().getFullYear()));
  const [rent, setRent] = useState("");
  const [opex, setOpex] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getMe(token).then((m) => !cancelled && setIsAdmin(m.role === "admin")).catch(() => !cancelled && setIsAdmin(false));
    return () => {
      cancelled = true;
    };
  }, [token]);

  useEffect(() => {
    if (!propertyId && properties.length) setPropertyId(properties[0].id);
  }, [properties]);

  const reload = () => {
    if (!propertyId) return;
    listPropertyBudgets(token, propertyId).then(setBudgets).catch((e) => setError(e.message));
  };
  useEffect(reload, [token, propertyId]);

  const save = (e: React.FormEvent) => {
    e.preventDefault();
    setStatus(null);
    setError(null);
    const yr = Number(year);
    if (!propertyId || !yr || rent === "" || opex === "") return;
    putPropertyBudget(token, propertyId, yr, {
      budgeted_gross_rent: Number(rent),
      budgeted_operating_expenses: Number(opex),
    })
      .then(() => {
        setStatus(`Saved ${yr} budget.`);
        setRent("");
        setOpex("");
        reload();
      })
      .catch((e) => setError(e.message));
  };

  const edit = (b: PropertyBudgetOut) => {
    setYear(String(b.year));
    setRent(String(b.budgeted_gross_rent));
    setOpex(String(b.budgeted_operating_expenses));
  };

  const remove = (b: PropertyBudgetOut) => {
    if (!confirm(`Delete the ${b.year} budget for this property?`)) return;
    deletePropertyBudget(token, propertyId, b.year)
      .then(reload)
      .catch((e) => setError(e.message));
  };

  return (
    <div style={{ marginTop: 30 }}>
      <h2 className="section-title">Budget</h2>
      <p className="hint">
        A flat annual plan per property/year — budgeted NOI is rent minus opex, and the
        monthly plan (annual ÷ 12) is pro-rated across whatever period a variance view covers.
        This is separate from actuals; it never changes recorded NOI/cash flow.
        {isAdmin ? "" : " Read-only (admin required to edit)."}
      </p>

      <PropertySearchSelect
        properties={properties}
        value={propertyId}
        onChange={setPropertyId}
        wrapperStyle={{ maxWidth: 360, marginBottom: 12 }}
      />

      <table className="data-table" style={{ textAlign: "left", maxWidth: 640 }}>
        <thead>
          <tr>
            <th style={{ textAlign: "left" }}>Year</th>
            <th style={{ textAlign: "left" }}>Budgeted rent</th>
            <th style={{ textAlign: "left" }}>Budgeted opex</th>
            <th style={{ textAlign: "left" }}>Budgeted NOI</th>
            {isAdmin && <th></th>}
          </tr>
        </thead>
        <tbody>
          {budgets.map((b) => (
            <tr key={b.year}>
              <td>{b.year}</td>
              <td>{fmtCurrency(b.budgeted_gross_rent)}</td>
              <td>{fmtCurrency(b.budgeted_operating_expenses)}</td>
              <td>{fmtCurrency(b.budgeted_noi)}</td>
              {isAdmin && (
                <td style={{ display: "flex", gap: 6 }}>
                  <button className="btn btn-ghost" onClick={() => edit(b)}>
                    Edit
                  </button>
                  <button className="btn btn-ghost" onClick={() => remove(b)}>
                    ✕
                  </button>
                </td>
              )}
            </tr>
          ))}
          {budgets.length === 0 && (
            <tr className="row-empty">
              <td colSpan={isAdmin ? 5 : 4}>No budget set for this property yet.</td>
            </tr>
          )}
        </tbody>
      </table>

      {isAdmin && (
        <form
          className="card"
          style={{ display: "flex", gap: 8, alignItems: "flex-end", flexWrap: "wrap", marginTop: 12, maxWidth: 640 }}
          onSubmit={save}
        >
          <label className="stack">
            Year
            <input className="input" style={{ width: 90 }} type="number" value={year} onChange={(e) => setYear(e.target.value)} />
          </label>
          <label className="stack">
            Annual budgeted rent
            <input className="input" type="number" step="0.01" value={rent} onChange={(e) => setRent(e.target.value)} />
          </label>
          <label className="stack">
            Annual budgeted opex
            <input className="input" type="number" step="0.01" value={opex} onChange={(e) => setOpex(e.target.value)} />
          </label>
          <button className="btn btn-primary" type="submit">
            Save budget
          </button>
        </form>
      )}
      {status && <p style={{ color: "var(--positive)", fontWeight: 500 }}>{status}</p>}
      {error && <p className="alert-error">{error}</p>}
    </div>
  );
}

function NewPropertyForm({
  onCreate,
}: {
  onCreate: (b: { name: string; type: "multifamily" | "single"; address: string | null }) => void;
}) {
  const [name, setName] = useState("");
  const [type, setType] = useState<"multifamily" | "single">("single");
  const [address, setAddress] = useState("");
  return (
    <form
      style={{ ...card, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}
      onSubmit={(e) => {
        e.preventDefault();
        if (!name.trim()) return;
        onCreate({ name: name.trim(), type, address: address.trim() || null });
        setName("");
        setAddress("");
      }}
    >
      <input style={input} placeholder="Property name" value={name} onChange={(e) => setName(e.target.value)} />
      <select style={input} value={type} onChange={(e) => setType(e.target.value as "multifamily" | "single")}>
        <option value="single">single</option>
        <option value="multifamily">multifamily</option>
      </select>
      <input style={input} placeholder="Address (optional)" value={address} onChange={(e) => setAddress(e.target.value)} />
      <button style={btnPrimary} type="submit">
        Add property
      </button>
    </form>
  );
}

// Portfolio segmentation: assign/remove free-text tags (region, fund/entity, asset class,
// ...) on a property. Reads are open to any authed user; writes are admin-gated
// server-side, so the add/remove controls are hidden (not shown-then-403'd) for non-admins
// — same client-side-gating convention as BudgetSection/AuditLogSection.
function PropertyTags({
  token,
  propertyId,
  isAdmin,
  onError,
}: {
  token: string;
  propertyId: string;
  isAdmin: boolean;
  onError: (m: string) => void;
}) {
  const [tags, setTags] = useState<string[]>([]);
  const [newTag, setNewTag] = useState("");

  const reload = () => getPropertyTags(token, propertyId).then(setTags).catch((e) => onError(e.message));
  useEffect(() => {
    reload();
  }, [token, propertyId]);

  const add = (e: React.FormEvent) => {
    e.preventDefault();
    const tag = newTag.trim();
    if (!tag) return;
    addPropertyTag(token, propertyId, tag)
      .then((t) => {
        setTags(t);
        setNewTag("");
      })
      .catch((e) => onError(e.message));
  };

  const remove = (tag: string) => {
    removePropertyTag(token, propertyId, tag).then(setTags).catch((e) => onError(e.message));
  };

  return (
    <div className="tag-filter" style={{ marginTop: 8 }}>
      {tags.map((t) => (
        <span key={t} className="tag-chip">
          {t}
          {isAdmin && (
            <button
              type="button"
              className="tag-chip__remove"
              onClick={() => remove(t)}
              aria-label={`Remove tag ${t}`}
              title={`Remove tag ${t}`}
            >
              ×
            </button>
          )}
        </span>
      ))}
      {tags.length === 0 && <span className="muted" style={{ fontSize: 12.5 }}>No tags</span>}
      {isAdmin && (
        <form style={{ display: "inline-flex", gap: 6 }} onSubmit={add}>
          <input
            className="input"
            style={{ padding: "3px 9px", fontSize: 12.5, width: 130 }}
            placeholder="Add tag…"
            aria-label="Add tag"
            value={newTag}
            onChange={(e) => setNewTag(e.target.value)}
          />
          <button className="btn btn-ghost" style={{ padding: "3px 10px", fontSize: 12.5 }} type="submit">
            Add
          </button>
        </form>
      )}
    </div>
  );
}

function PropertyCard({
  token,
  property,
  isAdmin,
  onDelete,
  onError,
}: {
  token: string;
  property: Property;
  isAdmin: boolean;
  onDelete: () => void;
  onError: (m: string) => void;
}) {
  const [units, setUnits] = useState<Unit[]>([]);
  const [open, setOpen] = useState(false);
  const [unitNum, setUnitNum] = useState("");

  // A single-let property gets a units section too, capped at one (see the API's own rule in
  // app/routers/properties.py). It isn't bookkeeping pedantry: a tenancy hangs off a unit, so
  // without that one unit a house can never hold a rent schedule or an arrears balance — and
  // this screen was the only place to create one.
  const isSingle = property.type !== "multifamily";
  const atUnitCap = isSingle && units.length >= 1;

  const loadUnits = () => listUnits(token, property.id).then(setUnits).catch((e) => onError(e.message));
  useEffect(() => {
    if (open) loadUnits();
  }, [open]);

  return (
    <div style={card}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div>
          <strong>{property.name}</strong>{" "}
          <span style={{ color: "#888" }}>
            ({property.type}){property.address ? ` · ${property.address}` : ""}
          </span>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button style={btn} onClick={() => setOpen((o) => !o)}>
            {open ? "Hide units" : isSingle ? "Unit" : "Units"}
          </button>
          <button style={btn} onClick={() => confirm(`Delete ${property.name}? This removes its records.`) && onDelete()}>
            Delete
          </button>
        </div>
      </div>

      <PropertyTags token={token} propertyId={property.id} isAdmin={isAdmin} onError={onError} />

      {open && (
        <div style={{ marginTop: 10, paddingLeft: 12, borderLeft: "3px solid #eee" }}>
          {units.map((u) => (
            <div key={u.id} style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 4 }}>
              <span>
                Unit <strong>{u.unit_number}</strong>
                {u.label ? ` — ${u.label}` : ""}
              </span>
              <button
                style={btn}
                onClick={() =>
                  confirm(`Delete unit ${u.unit_number}?`) &&
                  deleteUnit(token, u.id).then(loadUnits).catch((e) => onError(e.message))
                }
              >
                ✕
              </button>
            </div>
          ))}
          {isSingle && (
            <p className="hint" style={{ margin: "4px 0" }}>
              {atUnitCap
                ? "A single-let property holds one unit — its own dwelling. That unit is where the " +
                  "tenancy, rent schedule and arrears live; edit them on the Property or Rent Roll tab."
                : "Add one unit (call it “1”) so this house can hold a tenancy — without it there's " +
                  "nowhere for the rent schedule or arrears balance to live."}
            </p>
          )}
          <form
            style={{ display: atUnitCap ? "none" : "flex", gap: 8, marginTop: 6 }}
            onSubmit={(e) => {
              e.preventDefault();
              if (!unitNum.trim()) return;
              createUnit(token, property.id, { unit_number: unitNum.trim() })
                .then(() => {
                  setUnitNum("");
                  loadUnits();
                })
                .catch((e) => onError(e.message));
            }}
          >
            <input
              style={input}
              placeholder={isSingle ? "Unit number (e.g. 1)" : "Unit number"}
              value={unitNum}
              onChange={(e) => setUnitNum(e.target.value)}
            />
            <button style={btn} type="submit">
              Add unit
            </button>
          </form>
        </div>
      )}
    </div>
  );
}

function CategorySection({ token, onError }: { token: string; onError: (m: string) => void }) {
  const [cats, setCats] = useState<Category[]>([]);
  const [name, setName] = useState("");
  const [cls, setCls] = useState<Classification>("operating");
  const [search, setSearch] = useState("");

  const reload = () => listCategories(token, false, true).then(setCats).catch((e) => onError(e.message));
  useEffect(() => {
    reload();
  }, [token]);

  const filteredCats = cats.filter((c) => c.name.toLowerCase().includes(search.trim().toLowerCase()));

  return (
    <div style={{ marginTop: 30 }}>
      <h2 className="section-title">Categories</h2>
      <p className="hint">
        Global, shared list. The <em>classification</em> is the only thing that drives the math —
        changing it recomputes NOI/cash flow with no migration. Merging near-duplicate
        categories (e.g. PDF-import junk) is available on the Reclassify screen.
      </p>
      <label className="stack" style={{ maxWidth: 320, marginBottom: 10 }}>
        Search categories
        <input
          className="input"
          type="search"
          placeholder="Filter by name…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </label>
      <table className="data-table" style={{ textAlign: "left" }}>
        <thead>
          <tr>
            <th style={{ textAlign: "left" }}>Name</th>
            <th style={{ textAlign: "left" }}>Classification</th>
            <th style={{ textAlign: "left" }}>Active</th>
            <th style={{ textAlign: "left" }}>Usage</th>
          </tr>
        </thead>
        <tbody>
          {filteredCats.map((c) => (
            <tr key={c.id}>
              <td>{c.name}</td>
              <td style={{ textAlign: "left" }}>
                <select
                  style={input}
                  value={c.default_classification}
                  onChange={(e) =>
                    updateCategory(token, c.id, { default_classification: e.target.value as Classification })
                      .then(reload)
                      .catch((e) => onError(e.message))
                  }
                >
                  {CLASSIFICATIONS.map((x) => (
                    <option key={x} value={x}>
                      {x}
                    </option>
                  ))}
                </select>
              </td>
              <td style={{ textAlign: "left" }}>
                <input
                  type="checkbox"
                  checked={c.active}
                  onChange={(e) =>
                    updateCategory(token, c.id, { active: e.target.checked })
                      .then(reload)
                      .catch((e) => onError(e.message))
                  }
                />
              </td>
              <td style={{ textAlign: "left" }}>
                {c.usage_count === 0 ? <span className="muted">unused</span> : (c.usage_count ?? "—")}
              </td>
            </tr>
          ))}
          {filteredCats.length === 0 && (
            <tr className="row-empty">
              <td colSpan={4}>No categories match “{search}”.</td>
            </tr>
          )}
        </tbody>
      </table>
      <form
        style={{ ...card, display: "flex", gap: 8, marginTop: 12 }}
        onSubmit={(e) => {
          e.preventDefault();
          if (!name.trim()) return;
          createCategory(token, { name: name.trim(), default_classification: cls })
            .then(() => {
              setName("");
              reload();
            })
            .catch((e) => onError(e.message));
        }}
      >
        <input style={input} placeholder="New category name" value={name} onChange={(e) => setName(e.target.value)} />
        <select style={input} value={cls} onChange={(e) => setCls(e.target.value as Classification)}>
          {CLASSIFICATIONS.map((x) => (
            <option key={x} value={x}>
              {x}
            </option>
          ))}
        </select>
        <button style={btnPrimary} type="submit">
          Add category
        </button>
      </form>
    </div>
  );
}
