import { useEffect, useId, useState } from "react";
import type { PeriodRange } from "../api";

// Frontend-only, localStorage-backed (no backend table — this is pure UI convenience
// state, not portfolio data). Namespaced key so it doesn't collide with anything else
// re-manager might someday put in localStorage.
const STORAGE_KEY = "re-manager:saved-views";

export interface SavedViewState {
  range: PeriodRange;
  tags: string[];
}

interface SavedView extends SavedViewState {
  name: string;
  savedAt: string;
}

function isPeriodRange(v: unknown): v is PeriodRange {
  if (typeof v !== "object" || v === null) return false;
  const r = v as Record<string, unknown>;
  return (r.from === undefined || typeof r.from === "string")
    && (r.to === undefined || typeof r.to === "string");
}

function loadViews(): SavedView[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (v): v is SavedView =>
        !!v
        && typeof v === "object"
        && typeof (v as SavedView).name === "string"
        && typeof (v as SavedView).savedAt === "string"
        && Array.isArray((v as SavedView).tags)
        && isPeriodRange((v as SavedView).range),
    );
  } catch {
    // Corrupt/foreign JSON under our key — treat as no saved views rather than crash.
    return [];
  }
}

function persistViews(views: SavedView[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(views));
  } catch {
    // localStorage unavailable (private browsing quota, disabled storage, ...) — saving
    // silently becomes a no-op for this session rather than throwing into the UI.
  }
}

// Save the current period range + tag filter under a name; list, apply, and delete saved
// views. Applying restores the exact range/tags via `onApply`. Everything here is
// synchronous localStorage I/O, so there's no loading state to manage.
export default function SavedViews({
  current,
  onApply,
}: {
  current: SavedViewState;
  onApply: (view: SavedViewState) => void;
}) {
  const [views, setViews] = useState<SavedView[]>([]);
  const [name, setName] = useState("");
  const [selected, setSelected] = useState("");
  const selectId = useId();
  const nameId = useId();

  useEffect(() => {
    setViews(loadViews());
  }, []);

  const save = () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    const next = [
      ...views.filter((v) => v.name !== trimmed),
      { name: trimmed, range: current.range, tags: current.tags, savedAt: new Date().toISOString() },
    ].sort((a, b) => a.name.localeCompare(b.name));
    setViews(next);
    persistViews(next);
    setName("");
    setSelected(trimmed);
  };

  const apply = (viewName: string) => {
    setSelected(viewName);
    const v = views.find((x) => x.name === viewName);
    if (v) onApply({ range: v.range, tags: v.tags });
  };

  const remove = () => {
    if (!selected) return;
    const next = views.filter((v) => v.name !== selected);
    setViews(next);
    persistViews(next);
    setSelected("");
  };

  return (
    <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      <label htmlFor={selectId} className="row" style={{ gap: 6, fontSize: 12.5, fontWeight: 550 }}>
        Saved views
        <select
          id={selectId}
          className="select"
          value={selected}
          onChange={(e) => (e.target.value ? apply(e.target.value) : setSelected(""))}
        >
          <option value="">— none —</option>
          {views.map((v) => (
            <option key={v.name} value={v.name}>
              {v.name}
            </option>
          ))}
        </select>
      </label>
      {selected && (
        <button
          type="button"
          className="btn btn-ghost"
          onClick={remove}
          aria-label={`Delete saved view "${selected}"`}
        >
          Delete
        </button>
      )}
      <label htmlFor={nameId} className="row" style={{ gap: 6, fontSize: 12.5, fontWeight: 550 }}>
        Save current as
        <input
          id={nameId}
          type="text"
          className="input"
          placeholder="e.g. Q2 2025, Northeast"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              save();
            }
          }}
          style={{ maxWidth: 190 }}
        />
      </label>
      <button type="button" className="btn" onClick={save} disabled={!name.trim()}>
        Save
      </button>
    </div>
  );
}
