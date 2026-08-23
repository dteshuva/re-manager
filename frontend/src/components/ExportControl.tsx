import { useId, useState } from "react";
import type { ExportFormat } from "../api";

export interface ExportReportOption {
  value: string;
  label: string;
  onExport: (format: ExportFormat) => Promise<void>;
}

// A small "download the currently-viewed data" control: pick which report (when there's
// more than one), pick xlsx/csv, hit Download. The caller supplies `onExport` closures
// already bound to the active period range / tag filter / property, so this component
// never touches API params directly — it just triggers whichever export the caller wired
// up and surfaces a busy/error state.
export default function ExportControl({
  reports,
  idPrefix,
}: {
  reports: ExportReportOption[];
  idPrefix: string;
}) {
  const [reportIdx, setReportIdx] = useState(0);
  const [format, setFormat] = useState<ExportFormat>("xlsx");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reportId = useId();
  const formatId = useId();

  if (reports.length === 0) return null;
  const active = reports[Math.min(reportIdx, reports.length - 1)];

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      await active.onExport(format);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }} data-testid={`${idPrefix}-export`}>
      {reports.length > 1 && (
        <label htmlFor={reportId} className="row" style={{ gap: 6, fontSize: 12.5, fontWeight: 550 }}>
          Export
          <select
            id={reportId}
            className="select"
            value={reportIdx}
            onChange={(e) => setReportIdx(Number(e.target.value))}
          >
            {reports.map((r, i) => (
              <option key={r.value} value={i}>
                {r.label}
              </option>
            ))}
          </select>
        </label>
      )}
      <label htmlFor={formatId} className="row" style={{ gap: 6, fontSize: 12.5, fontWeight: 550 }}>
        {reports.length === 1 ? `Export ${active.label} as` : "as"}
        <select
          id={formatId}
          className="select"
          value={format}
          onChange={(e) => setFormat(e.target.value as ExportFormat)}
        >
          <option value="xlsx">Excel (.xlsx)</option>
          <option value="csv">CSV</option>
        </select>
      </label>
      <button type="button" className="btn" onClick={run} disabled={busy}>
        {busy ? "Exporting…" : "Download"}
      </button>
      {error && (
        <span className="alert-error" style={{ padding: "4px 8px", fontSize: 12.5 }}>
          {error}
        </span>
      )}
    </div>
  );
}
