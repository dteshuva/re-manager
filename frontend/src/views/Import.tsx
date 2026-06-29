import { useEffect, useState } from "react";
import {
  getImportTemplate,
  getMissing,
  importFile,
  type ColumnMapping,
  type ImportReport,
  type MissingScope,
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
