import { useId, useMemo, useState } from "react";
import type { Property } from "../api";

// A plain <select> doesn't scale past ~100 properties (per the analyst report). This adds
// a client-side type-to-filter text input in front of the existing <select> — at current
// portfolio sizes a client-side filter is plenty fast, and it keeps the underlying control
// a real, fully keyboard-navigable <select> rather than a hand-rolled combobox widget.
export default function PropertySearchSelect({
  properties,
  value,
  onChange,
  label = "Property",
  wrapperClassName = "stack",
  wrapperStyle,
  placeholder,
}: {
  properties: Property[];
  value: string;
  onChange: (id: string) => void;
  label?: string;
  wrapperClassName?: string;
  wrapperStyle?: React.CSSProperties;
  // Leading "— select —" option; omit when a selection is always required/pre-filled.
  placeholder?: string;
}) {
  const [query, setQuery] = useState("");
  const searchId = useId();
  const selectId = useId();

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return properties;
    return properties.filter(
      (p) => p.name.toLowerCase().includes(q) || (p.address ?? "").toLowerCase().includes(q),
    );
  }, [properties, query]);

  // Never let the current selection silently disappear from the dropdown just because the
  // search text no longer matches it — keep it selectable (pinned to the top) regardless.
  const options = useMemo(() => {
    if (!value || filtered.some((p) => p.id === value)) return filtered;
    const selected = properties.find((p) => p.id === value);
    return selected ? [selected, ...filtered] : filtered;
  }, [filtered, properties, value]);

  return (
    <div className={wrapperClassName} style={wrapperStyle}>
      <label htmlFor={searchId}>{label}</label>
      <input
        id={searchId}
        type="text"
        className="input"
        placeholder="Search by name or address…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        aria-controls={selectId}
      />
      <select
        id={selectId}
        className="select"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label={`${label} (filtered by search above)`}
      >
        {placeholder && <option value="">{placeholder}</option>}
        {options.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name} {p.type ? `(${p.type})` : ""}
          </option>
        ))}
        {options.length === 0 && (
          <option value="" disabled>
            No matching properties
          </option>
        )}
      </select>
    </div>
  );
}
