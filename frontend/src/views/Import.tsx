import { useEffect, useState } from "react";
import {
  CLASSIFICATIONS,
  createCategory,
  createProperty,
  createUnit,
  extractStatementsBatch,
  getImportTemplate,
  getMissing,
  importFile,
  importRows,
  listCategories,
  listProperties,
  listUnits,
  type Category,
  type Classification,
  type ColumnMapping,
  type ImportReport,
  type ImportRowInput,
  type MissingScope,
  type Property,
  type StatementRow,
} from "../api";
import { btn, btnPrimary, card, input } from "../ui";

const CANONICAL: { key: keyof ColumnMapping; label: string; required?: boolean }[] = [
  { key: "property", label: "Property (name)", required: true },
  { key: "unit", label: "Unit (blank = property-tier)" },
  { key: "month", label: "Month", required: true },
  { key: "category", label: "Category", required: true },
  { key: "amount", label: "Amount", required: true },
  { key: "classification", label: "Classification (optional override)" },
];

const thisMonth = () => new Date().toISOString().slice(0, 7);

// Bulk import: upload a CSV/Excel file, map its columns, preview (dry-run) to catch bad
// rows, then commit. Idempotent — re-importing a month overwrites it. Plus a
// "what's missing" view so no property/unit-month slips through.
export default function ImportView({ token }: { token: string }) {
  const [file, setFile] = useState<File | null>(null);
  const [columns, setColumns] = useState<string[]>([]);
  const [mapping, setMapping] = useState<ColumnMapping>({
    property: "Property",
    unit: "Unit",
    month: "Month",
    category: "Category",
    amount: "Amount",
  });
  const [onError, setOnError] = useState<"abort" | "skip">("abort");
  const [report, setReport] = useState<ImportReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Sniff CSV header row client-side so mapping can use dropdowns + auto-match.
  async function onPick(f: File | null) {
    setFile(f);
    setReport(null);
    setError(null);
    setColumns([]);
    if (f && /\.(csv|txt)$/i.test(f.name)) {
      const head = (await f.text()).split(/\r?\n/)[0] ?? "";
      const cols = head.split(",").map((c) => c.trim()).filter(Boolean);
      setColumns(cols);
      // auto-match canonical fields to columns by case-insensitive contains
      setMapping((m) => {
        const next = { ...m };
        for (const { key } of CANONICAL) {
          const hit = cols.find((c) => c.toLowerCase() === key.toLowerCase()) ??
            cols.find((c) => c.toLowerCase().includes(key));
          if (hit) next[key] = hit;
        }
        return next;
      });
    }
  }

  async function run(dryRun: boolean) {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const clean: ColumnMapping = {};
      for (const k of Object.keys(mapping) as (keyof ColumnMapping)[]) {
        if (mapping[k]) clean[k] = mapping[k];
      }
      setReport(await importFile(token, file, clean, { dryRun, onError }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function downloadTemplate() {
    const csv = await getImportTemplate(token);
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = "import_template.csv";
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <section>
      <StatementBatchSection token={token} />

      <h3 className="section-title" style={{ marginTop: 28 }}>Spreadsheet import (CSV / Excel)</h3>
      <div style={card}>
        <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <input type="file" accept=".csv,.txt,.xlsx,.xls" onChange={(e) => onPick(e.target.files?.[0] ?? null)} />
          <button style={btn} onClick={downloadTemplate}>
            Download CSV template
          </button>
        </div>

        <h3 className="section-title" style={{ marginBottom: 4 }}>Column mapping</h3>
        <p style={{ color: "#666", marginTop: 0 }}>
          Map each field to a column in your file. {columns.length ? "Detected columns shown below." : "Type the column header names."}
        </p>
        <div style={{ display: "grid", gridTemplateColumns: "220px 1fr", gap: 8, maxWidth: 560 }}>
          {CANONICAL.map(({ key, label, required }) => (
            <div key={key} style={{ display: "contents" }}>
              <label style={{ alignSelf: "center" }}>
                {label}
                {required && <span style={{ color: "crimson" }}> *</span>}
              </label>
              {columns.length ? (
                <select
                  style={input}
                  value={mapping[key] ?? ""}
                  onChange={(e) => setMapping((m) => ({ ...m, [key]: e.target.value }))}
                >
                  <option value="">— none —</option>
                  {columns.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  style={input}
                  value={mapping[key] ?? ""}
                  placeholder="column header"
                  onChange={(e) => setMapping((m) => ({ ...m, [key]: e.target.value }))}
                />
              )}
            </div>
          ))}
        </div>

        <div style={{ marginTop: 12, display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <label>
            On bad rows:{" "}
            <select style={input} value={onError} onChange={(e) => setOnError(e.target.value as "abort" | "skip")}>
              <option value="abort">Abort (all-or-nothing)</option>
              <option value="skip">Skip bad rows, import the rest</option>
            </select>
          </label>
          <button style={btn} disabled={!file || busy} onClick={() => run(true)}>
            Preview (dry-run)
          </button>
          <button style={btnPrimary} disabled={!file || busy} onClick={() => run(false)}>
            Import
          </button>
        </div>
        {error && <p className="alert-error" style={{ marginTop: 12, marginBottom: 0 }}>{error}</p>}
      </div>

      {report && <ReportView report={report} />}

      <MissingView token={token} />
    </section>
  );
}

function ReportView({ report }: { report: ImportReport }) {
  const ok = report.committed;
  return (
    <div style={card}>
      <h3 className="section-title" style={{ marginTop: 0 }}>
        {report.dry_run ? "Preview" : ok ? "Imported ✓" : "Not imported"}
      </h3>
      <p style={{ color: report.dry_run ? "var(--ink-2)" : ok ? "var(--positive)" : "var(--negative)", fontWeight: 500 }}>
        {report.dry_run
          ? `Would apply ${report.applied_line_items} line item(s) across ${report.records_touched} record(s). Nothing written yet.`
          : ok
            ? `Applied ${report.applied_line_items} line item(s) across ${report.records_touched} record(s).`
            : `Aborted — fix the ${report.invalid_rows} flagged row(s) and re-import.`}
      </p>
      <p className="muted">
        valid: {report.valid_rows} · invalid: {report.invalid_rows} · mode: {report.on_error}
      </p>
      {report.errors.length > 0 && (
        <table className="data-table" style={{ textAlign: "left", maxWidth: 640 }}>
          <thead>
            <tr>
              <th style={{ textAlign: "left" }}>Row</th>
              <th style={{ textAlign: "left" }}>Field</th>
              <th style={{ textAlign: "left" }}>Problem</th>
            </tr>
          </thead>
          <tbody>
            {report.errors.map((e, i) => (
              <tr key={i}>
                <td>{e.row ?? "—"}</td>
                <td style={{ textAlign: "left" }}>{e.field ?? "—"}</td>
                <td style={{ textAlign: "left", color: "var(--negative)" }}>{e.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ------------------------- PDF statement import (free) -------------------------

function uniqueCI(values: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const v of values) {
    const k = v.trim().toLowerCase();
    if (v.trim() && !seen.has(k)) {
      seen.add(k);
      out.push(v.trim());
    }
  }
  return out;
}

// Batch PDF statement import: upload many statements at once (mixed months/properties),
// resolve shared new properties/categories ONCE, review each, then upload them all. Every
// statement flows through the same idempotent import core (`/import/rows`) the CSV path uses.
type BatchRow = StatementRow & { include: boolean; key: number };

type Stmt = {
  id: number;
  filename: string;
  error: string | null;
  backend: string;
  warnings: string[];
  detectedProperty: string | null;
  propertyId: string;
  month: string; // YYYY-MM
  rows: BatchRow[];
  include: boolean;
  expanded: boolean;
  result: ImportReport | null;
};

function StatementBatchSection({ token }: { token: string }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [properties, setProperties] = useState<Property[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [stmts, setStmts] = useState<Stmt[]>([]);
  const [newCatCls, setNewCatCls] = useState<Record<string, Classification>>({});
  const [newPropType, setNewPropType] = useState<Record<string, "single" | "multifamily">>({});
  // Decisions on NEW items, keyed by lowercased name. Absent = pending (not used). A decision
  // applies to that item across EVERY statement in the batch.
  const [catDecision, setCatDecision] = useState<Record<string, "approved" | "rejected">>({});
  const [propDecision, setPropDecision] = useState<Record<string, "approved" | "rejected">>({});
  const [onError, setOnError] = useState<"abort" | "skip">("abort");
  const [uploaded, setUploaded] = useState(false);

  const reloadLookups = async () => {
    const [p, c] = await Promise.all([listProperties(token), listCategories(token)]);
    setProperties(p);
    setCategories(c);
    return { p, c };
  };

  const syncProps = (arr: Stmt[], props: Property[]): Stmt[] => {
    const byName = new Map(props.map((p) => [p.name.trim().toLowerCase(), p]));
    return arr.map((s) =>
      s.propertyId || !s.detectedProperty
        ? s
        : { ...s, propertyId: byName.get(s.detectedProperty.trim().toLowerCase())?.id ?? "" },
    );
  };

  async function onExtract(files: File[]) {
    if (files.length === 0) return;
    setBusy(true);
    setError(null);
    setUploaded(false);
    setCatDecision({});
    setPropDecision({});
    try {
      const res = await extractStatementsBatch(token, files);
      const { p } = await reloadLookups();
      let id = 0;
      const next: Stmt[] = res.items.map((it) => {
        const pv = it.preview;
        return {
          id: id++,
          filename: it.filename,
          error: it.error,
          backend: pv?.backend ?? "",
          warnings: pv?.warnings ?? [],
          detectedProperty: pv?.detected_property ?? null,
          propertyId: pv?.property_id ?? "",
          month: pv?.detected_month ? pv.detected_month.slice(0, 7) : "",
          rows: (pv?.rows ?? []).map((r, i) => ({ ...r, include: true, key: i })),
          include: !it.error && (pv?.rows.length ?? 0) > 0,
          expanded: false,
          result: null,
        };
      });
      setStmts(syncProps(next, p));
      const seedCls: Record<string, Classification> = {};
      for (const u of res.unknown_categories) {
        if (u.suggested_classification) seedCls[u.name] = u.suggested_classification;
      }
      setNewCatCls(seedCls);
      const seedType: Record<string, "single" | "multifamily"> = {};
      for (const name of res.unknown_properties) seedType[name] = "single";
      setNewPropType(seedType);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const catByName = new Map(categories.map((c) => [c.name.trim().toLowerCase(), c]));
  const propByName = new Map(properties.map((p) => [p.name.trim().toLowerCase(), p]));
  const active = stmts.filter((s) => s.include && !s.error && !s.result);

  // A category is one of: "known" (already exists), "approved", "rejected", "pending".
  const catState = (name: string): "known" | "approved" | "rejected" | "pending" => {
    const k = name.trim().toLowerCase();
    if (catByName.has(k)) return "known";
    return catDecision[k] ?? "pending";
  };
  // Usable = it will actually be stored: an existing category, or a newly-approved one.
  const catUsable = (name: string) => {
    const st = catState(name);
    return st === "known" || st === "approved";
  };
  const propApproved = (s: Stmt) =>
    !!s.propertyId ||
    (!!s.detectedProperty && propDecision[s.detectedProperty.trim().toLowerCase()] === "approved");

  const tickedRows = (s: Stmt) => s.rows.filter((r) => r.include); // user-ticked
  const includedRowsOf = (s: Stmt) => tickedRows(s).filter((r) => catUsable(r.category)); // effective

  // New items across the batch (deduped), listed in the attention panel for approve/reject.
  const unknownCats = uniqueCI(
    active.flatMap((s) => tickedRows(s).map((r) => r.category)).filter((n) => catState(n) !== "known"),
  );
  const unknownProps = uniqueCI(
    active
      .filter((s) => !s.propertyId && s.detectedProperty && !propByName.has(s.detectedProperty.trim().toLowerCase()))
      .map((s) => s.detectedProperty as string),
  );
  const pendingCatCount = unknownCats.filter((n) => catState(n) === "pending").length;
  const pendingPropCount = unknownProps.filter((n) => !propDecision[n.trim().toLowerCase()]).length;

  const stmtStatus = (s: Stmt): { ok: boolean; label: string } => {
    if (s.error) return { ok: false, label: "can't read" };
    const pend = uniqueCI(tickedRows(s).map((r) => r.category)).filter((n) => catState(n) === "pending");
    if (pend.length) return { ok: false, label: `approve ${pend.length} categor${pend.length === 1 ? "y" : "ies"}` };
    if (!propApproved(s)) return { ok: false, label: s.detectedProperty ? "approve property" : "pick property" };
    if (!s.month) return { ok: false, label: "set month" };
    if (includedRowsOf(s).length === 0) return { ok: false, label: "no rows" };
    return { ok: true, label: "ready" };
  };
  const readyCount = active.filter((s) => stmtStatus(s).ok).length;

  const setStmt = (id: number, patch: Partial<Stmt>) =>
    setStmts((ss) => ss.map((s) => (s.id === id ? { ...s, ...patch } : s)));
  const setRow = (id: number, key: number, patch: Partial<BatchRow>) =>
    setStmts((ss) =>
      ss.map((s) =>
        s.id === id ? { ...s, rows: s.rows.map((r) => (r.key === key ? { ...r, ...patch } : r)) } : s,
      ),
    );
  // Toggle a decision; clicking the current choice again clears it back to pending.
  const decideCat = (name: string, d: "approved" | "rejected") =>
    setCatDecision((m) => {
      const k = name.trim().toLowerCase();
      const n = { ...m };
      if (n[k] === d) delete n[k];
      else n[k] = d;
      return n;
    });
  const decideProp = (name: string, d: "approved" | "rejected") =>
    setPropDecision((m) => {
      const k = name.trim().toLowerCase();
      const n = { ...m };
      if (n[k] === d) delete n[k];
      else n[k] = d;
      return n;
    });

  async function uploadAll() {
    setBusy(true);
    setError(null);
    try {
      const targets = stmts.filter((s) => s.include && !s.error && !s.result && stmtStatus(s).ok);
      if (targets.length === 0) return;

      // 1. Create approved-new properties once (existing ones already have propertyId).
      let props = await listProperties(token);
      let pByName = new Map(props.map((p) => [p.name.trim().toLowerCase(), p]));
      const propIds: Record<number, string> = {};
      for (const s of targets) {
        let pid = s.propertyId;
        if (!pid && s.detectedProperty) {
          const key = s.detectedProperty.trim().toLowerCase();
          let p = pByName.get(key);
          if (!p) {
            p = await createProperty(token, {
              name: s.detectedProperty,
              type: newPropType[s.detectedProperty] ?? "single",
              address: null,
            });
            props = [...props, p];
            pByName = new Map(props.map((x) => [x.name.trim().toLowerCase(), x]));
          }
          pid = p.id;
        }
        propIds[s.id] = pid;
      }

      // 2. Create approved-new categories once (includedRowsOf only holds usable categories,
      //    so this is exactly the approved, not-yet-existing set — rejected ones never appear).
      let cats = await listCategories(token);
      const haveCat = new Set(cats.map((c) => c.name.trim().toLowerCase()));
      const allNew = uniqueCI(
        targets.flatMap((s) => includedRowsOf(s).map((r) => r.category)).filter((n) => !haveCat.has(n.trim().toLowerCase())),
      );
      for (const name of allNew) {
        await createCategory(token, { name, default_classification: newCatCls[name] ?? "operating" });
      }
      cats = await listCategories(token);
      const catMap = new Map(cats.map((c) => [c.name.trim().toLowerCase(), c]));

      // 3. Per statement: create missing units (multifamily), then upload the effective rows.
      const results: Record<number, ImportReport> = {};
      for (const s of targets) {
        const pid = propIds[s.id];
        if (!pid) continue;
        const multi = props.find((p) => p.id === pid)?.type === "multifamily";
        const rows = includedRowsOf(s);
        if (multi) {
          const us = await listUnits(token, pid);
          const have = new Set(us.map((u) => u.unit_number.trim().toLowerCase()));
          const missing = uniqueCI(
            rows.map((r) => r.unit ?? "").filter((u) => u.trim() && !have.has(u.trim().toLowerCase())),
          );
          for (const u of missing) await createUnit(token, pid, { unit_number: u });
        }
        const payload: ImportRowInput[] = rows.map((r) => {
          const cat = catMap.get(r.category.trim().toLowerCase());
          return {
            property_id: pid,
            unit: multi && r.unit && r.unit.trim() ? r.unit.trim() : null,
            month: `${s.month}-01`,
            category_id: cat?.id,
            category: cat ? undefined : r.category.trim(),
            classification: r.classification ?? null,
            amount: r.amount,
            source_row: r.key + 1,
          };
        });
        results[s.id] = await importRows(token, payload, { dryRun: false, onError });
      }

      const fresh = await reloadLookups();
      setStmts((ss) =>
        syncProps(ss.map((s) => (results[s.id] ? { ...s, result: results[s.id] } : s)), fresh.p),
      );
      setUploaded(true);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const doneReports = stmts.filter((s) => s.result).map((s) => s.result as ImportReport);
  const totalApplied = doneReports.reduce((a, r) => a + r.applied_line_items, 0);
  const errorCount = stmts.filter((s) => s.error).length;

  const chip = (bg: string, fg: string, text: string) => (
    <span style={{ background: bg, color: fg, borderRadius: 4, padding: "1px 6px", fontSize: 12, fontWeight: 600 }}>
      {text}
    </span>
  );
  const catChip = (name: string) => {
    const st = catState(name);
    if (st === "approved") return chip("#e6f4ea", "var(--positive)", "approved");
    if (st === "rejected") return chip("#fde8e8", "var(--negative)", "rejected");
    if (st === "pending") return chip("#fdf6e3", "#8a6d3b", "pending");
    return null;
  };

  return (
    <div>
      <h3 className="section-title" style={{ marginTop: 0 }}>PDF statement import</h3>
      <p style={{ color: "#666", marginTop: 0 }}>
        Upload one or many statements (any property-manager format, mixed months and properties).
        They're parsed on your machine (free). New properties/categories are held for your
        approval — approve to use one everywhere, reject to skip it everywhere — then upload all.
      </p>
      <div style={card}>
        <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <input
            type="file"
            accept=".pdf,application/pdf"
            multiple
            onChange={(e) => onExtract(Array.from(e.target.files ?? []))}
          />
          {busy && <span className="muted">Working…</span>}
        </div>
        {error && <p className="alert-error" style={{ marginTop: 12, marginBottom: 0 }}>{error}</p>}
      </div>

      {stmts.length > 0 && (
        <>
          {/* Summary + upload */}
          <div style={{ ...card, display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
            <strong>{stmts.length} statement(s)</strong>
            <span className="muted">
              {readyCount} ready · {active.length - readyCount} need attention · {errorCount} unreadable
            </span>
            {pendingCatCount + pendingPropCount > 0 && (
              <span style={{ color: "#8a6d3b", fontWeight: 600 }}>
                {pendingCatCount + pendingPropCount} new item(s) awaiting approval below
              </span>
            )}
            <span style={{ flex: 1 }} />
            <label>
              On bad rows:{" "}
              <select style={input} value={onError} onChange={(e) => setOnError(e.target.value as "abort" | "skip")}>
                <option value="abort">Abort per statement</option>
                <option value="skip">Skip bad rows</option>
              </select>
            </label>
            <button style={btnPrimary} disabled={busy || readyCount === 0} onClick={uploadAll}>
              {busy ? "Uploading…" : `Approve & upload ${readyCount} ready`}
            </button>
          </div>

          {/* Attention panel: approve/reject the new items (applies across every statement) */}
          {(unknownProps.length > 0 || unknownCats.length > 0) && (
            <div style={{ ...card, background: "var(--surface-2, #fafafa)" }}>
              <strong>New items — approve to use, reject to skip (applies to every statement)</strong>
              {unknownProps.length > 0 && (
                <div style={{ marginTop: 8 }}>
                  <div className="muted">Properties</div>
                  {unknownProps.map((name) => {
                    const dec = propDecision[name.trim().toLowerCase()];
                    return (
                      <div key={name} style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 4 }}>
                        <span style={{ minWidth: 220 }}>{name}</span>
                        <select
                          style={input}
                          value={newPropType[name] ?? "single"}
                          onChange={(e) =>
                            setNewPropType((m) => ({ ...m, [name]: e.target.value as "single" | "multifamily" }))
                          }
                        >
                          <option value="single">single</option>
                          <option value="multifamily">multifamily</option>
                        </select>
                        <button style={dec === "approved" ? btnPrimary : btn} onClick={() => decideProp(name, "approved")}>Approve</button>
                        <button
                          style={dec === "rejected" ? { ...btn, color: "var(--negative)", borderColor: "var(--negative)" } : btn}
                          onClick={() => decideProp(name, "rejected")}
                        >
                          Reject
                        </button>
                        {!dec && chip("#fdf6e3", "#8a6d3b", "pending")}
                      </div>
                    );
                  })}
                </div>
              )}
              {unknownCats.length > 0 && (
                <div style={{ marginTop: 12 }}>
                  <div className="muted">Categories (set the classification, then approve or reject)</div>
                  {unknownCats.map((name) => {
                    const st = catState(name);
                    return (
                      <div key={name} style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 4 }}>
                        <span style={{ minWidth: 220 }}>{name}</span>
                        <select
                          style={input}
                          value={newCatCls[name] ?? "operating"}
                          onChange={(e) => setNewCatCls((m) => ({ ...m, [name]: e.target.value as Classification }))}
                        >
                          {CLASSIFICATIONS.map((x) => (
                            <option key={x} value={x}>{x}</option>
                          ))}
                        </select>
                        <button style={st === "approved" ? btnPrimary : btn} onClick={() => decideCat(name, "approved")}>Approve</button>
                        <button
                          style={st === "rejected" ? { ...btn, color: "var(--negative)", borderColor: "var(--negative)" } : btn}
                          onClick={() => decideCat(name, "rejected")}
                        >
                          Reject
                        </button>
                        {st === "pending" && chip("#fdf6e3", "#8a6d3b", "pending")}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}

          {/* Statement cards */}
          {stmts.map((s) => {
            const st = stmtStatus(s);
            return (
              <div key={s.id} style={card}>
                <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                  {!s.error && !s.result && (
                    <input
                      type="checkbox"
                      checked={s.include}
                      onChange={(e) => setStmt(s.id, { include: e.target.checked })}
                      title="Include in upload"
                    />
                  )}
                  <strong style={{ minWidth: 150 }}>{s.filename}</strong>
                  {s.result ? (
                    <span
                      style={{
                        color: s.result.committed ? "var(--positive)" : "var(--negative)",
                        fontWeight: 500,
                      }}
                    >
                      {s.result.committed
                        ? `Uploaded ${s.result.applied_line_items} item(s)`
                        : `Not uploaded — ${s.result.invalid_rows} bad row(s)`}
                    </span>
                  ) : s.error ? (
                    <span style={{ color: "var(--negative)" }}>⛔ {s.error}</span>
                  ) : (
                    <>
                      <select
                        style={input}
                        value={s.propertyId}
                        onChange={(e) => setStmt(s.id, { propertyId: e.target.value })}
                      >
                        <option value="">
                          {s.detectedProperty ? `add “${s.detectedProperty}”` : "— property —"}
                        </option>
                        {properties.map((p) => (
                          <option key={p.id} value={p.id}>
                            {p.name} ({p.type})
                          </option>
                        ))}
                      </select>
                      <input
                        style={{ ...input, width: 130 }}
                        type="month"
                        value={s.month}
                        onChange={(e) => setStmt(s.id, { month: e.target.value })}
                      />
                      <span className="muted">{includedRowsOf(s).length} row(s)</span>
                      <span style={{ color: st.ok ? "var(--positive)" : "var(--negative)", fontWeight: 500 }}>
                        {st.ok ? "✓ ready" : `⚠ ${st.label}`}
                      </span>
                      <button style={btn} onClick={() => setStmt(s.id, { expanded: !s.expanded })}>
                        {s.expanded ? "Hide rows" : "Rows"}
                      </button>
                    </>
                  )}
                </div>

                {!s.result &&
                  s.warnings.map((w, i) => (
                    <p key={i} className="muted" style={{ marginTop: 6, marginBottom: 0 }}>· {w}</p>
                  ))}

                {s.expanded && !s.error && !s.result && (
                  <table className="data-table" style={{ textAlign: "left", marginTop: 10 }}>
                    <thead>
                      <tr>
                        <th style={{ textAlign: "left" }}>Use</th>
                        <th style={{ textAlign: "left" }}>Unit</th>
                        <th style={{ textAlign: "left" }}>Category</th>
                        <th style={{ textAlign: "left" }}>Classification</th>
                        <th style={{ textAlign: "right" }}>Amount</th>
                      </tr>
                    </thead>
                    <tbody>
                      {s.rows.map((r) => {
                        const effective = r.include && catUsable(r.category);
                        return (
                          <tr key={r.key} style={{ opacity: effective ? 1 : 0.4 }}>
                            <td>
                              <input
                                type="checkbox"
                                checked={r.include}
                                onChange={(e) => setRow(s.id, r.key, { include: e.target.checked })}
                              />
                            </td>
                            <td style={{ textAlign: "left" }}>
                              <input
                                style={{ ...input, width: 64 }}
                                value={r.unit ?? ""}
                                onChange={(e) => setRow(s.id, r.key, { unit: e.target.value || null })}
                              />
                            </td>
                            <td style={{ textAlign: "left" }}>
                              <input
                                style={{ ...input, width: 210 }}
                                value={r.category}
                                onChange={(e) => setRow(s.id, r.key, { category: e.target.value })}
                              />{" "}
                              {r.include && catChip(r.category)}
                            </td>
                            <td style={{ textAlign: "left" }}>
                              <select
                                style={input}
                                value={r.classification ?? ""}
                                onChange={(e) =>
                                  setRow(s.id, r.key, {
                                    classification: (e.target.value || null) as Classification | null,
                                  })
                                }
                              >
                                <option value="">(category default)</option>
                                {CLASSIFICATIONS.map((x) => (
                                  <option key={x} value={x}>{x}</option>
                                ))}
                              </select>
                            </td>
                            <td style={{ textAlign: "right" }}>
                              <input
                                style={{ ...input, width: 100, textAlign: "right" }}
                                type="number"
                                step="0.01"
                                value={r.amount}
                                onChange={(e) => setRow(s.id, r.key, { amount: Number(e.target.value) })}
                              />
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                )}
              </div>
            );
          })}

          {uploaded && (
            <p style={{ ...card, color: "var(--positive)", fontWeight: 500 }}>
              Uploaded {doneReports.filter((r) => r.committed).length}/{doneReports.length} statement(s) ·{" "}
              {totalApplied} line item(s).
            </p>
          )}
        </>
      )}
    </div>
  );
}

function MissingView({ token }: { token: string }) {
  const [month, setMonth] = useState(thisMonth());
  const [missing, setMissing] = useState<MissingScope[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getMissing(token, `${month}-01`)
      .then(setMissing)
      .catch((e) => setError(e.message));
  }, [token, month]);

  return (
    <div style={card}>
      <h3 className="section-title" style={{ marginTop: 0 }}>What's missing</h3>
      <label>
        Month{" "}
        <input style={input} type="month" value={month} onChange={(e) => setMonth(e.target.value)} />
      </label>
      {error && <p className="alert-error" style={{ marginTop: 12 }}>{error}</p>}
      {missing && missing.length === 0 && (
        <p style={{ color: "var(--positive)", fontWeight: 500 }}>All property/unit-months have data for {month}. ✓</p>
      )}
      {missing && missing.length > 0 && (
        <ul>
          {missing.map((m) => (
            <li key={`${m.property_id}-${m.unit_id ?? "tier"}`}>
              {m.property_name}
              {m.unit_number ? ` — Unit ${m.unit_number}` : " — property-tier"}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
