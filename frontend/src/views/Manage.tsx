import { useEffect, useState } from "react";
import {
  CLASSIFICATIONS,
  createCategory,
  createProperty,
  createUnit,
  deleteProperty,
  deleteUnit,
  listCategories,
  listProperties,
  listUnits,
  updateCategory,
  type Category,
  type Classification,
  type Property,
  type Unit,
} from "../api";
import AttentionSettings from "../components/AttentionSettings";
import { btn, btnPrimary, card, input } from "../ui";

// Manage properties, their units, and the global category list. The category section
// doubles as the reclassification control (changing default_classification recomputes
// NOI/cash flow with no migration — a fuller UI lands in Phase 7).
export default function Manage({ token }: { token: string }) {
  const [properties, setProperties] = useState<Property[]>([]);
  const [error, setError] = useState<string | null>(null);

  const reload = () => listProperties(token).then(setProperties).catch((e) => setError(e.message));
  useEffect(() => {
    reload();
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
          onDelete={() => wrap(deleteProperty(token, p.id))}
          onError={setError}
        />
      ))}

      <CategorySection token={token} onError={setError} />

      <h2 className="section-title">Attention thresholds</h2>
      <AttentionSettings token={token} />
    </section>
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

function PropertyCard({
  token,
  property,
  onDelete,
  onError,
}: {
  token: string;
  property: Property;
  onDelete: () => void;
  onError: (m: string) => void;
}) {
  const [units, setUnits] = useState<Unit[]>([]);
  const [open, setOpen] = useState(false);
  const [unitNum, setUnitNum] = useState("");

  const loadUnits = () => listUnits(token, property.id).then(setUnits).catch((e) => onError(e.message));
  useEffect(() => {
    if (open && property.type === "multifamily") loadUnits();
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
          {property.type === "multifamily" && (
            <button style={btn} onClick={() => setOpen((o) => !o)}>
              {open ? "Hide units" : "Units"}
            </button>
          )}
          <button style={btn} onClick={() => confirm(`Delete ${property.name}? This removes its records.`) && onDelete()}>
            Delete
          </button>
        </div>
      </div>

      {open && property.type === "multifamily" && (
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
          <form
            style={{ display: "flex", gap: 8, marginTop: 6 }}
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
            <input style={input} placeholder="Unit number" value={unitNum} onChange={(e) => setUnitNum(e.target.value)} />
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

  const reload = () => listCategories(token).then(setCats).catch((e) => onError(e.message));
  useEffect(() => {
    reload();
  }, [token]);

  return (
    <div style={{ marginTop: 30 }}>
      <h2 className="section-title">Categories</h2>
      <p className="hint">
        Global, shared list. The <em>classification</em> is the only thing that drives the math —
        changing it recomputes NOI/cash flow with no migration.
      </p>
      <table className="data-table" style={{ textAlign: "left" }}>
        <thead>
          <tr>
            <th style={{ textAlign: "left" }}>Name</th>
            <th style={{ textAlign: "left" }}>Classification</th>
            <th style={{ textAlign: "left" }}>Active</th>
          </tr>
        </thead>
        <tbody>
          {cats.map((c) => (
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
            </tr>
          ))}
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
