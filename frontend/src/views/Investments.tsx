import { useEffect, useMemo, useState } from "react";
import {
  getPortfolioBenchmarks,
  getPortfolioInvestment,
  getTags,
  type BenchmarkMetricKey,
  type InvestmentMetrics,
  type PortfolioBenchmarks,
  type PortfolioInvestment,
  type PropertyBenchmarkRow,
} from "../api";
import { toneClass } from "../components/KpiBand";
import { fmtCurrency, fmtMonth, fmtPct } from "../ui";

// Portfolio-level investment comparison: every property that has acquisition data, with its
// return metrics, plus value-weighted portfolio aggregates (Σ NOI / Σ price, Σ cash flow /
// Σ equity — never a mean of percentages). Editing inputs happens on the Property tab.
const pct = (v: number | null) => (v == null ? "—" : fmtPct(v));
const dscr = (v: number | null) => (v == null ? "—" : `${v.toFixed(2)}×`);

// ---- Benchmarks: every property vs. the portfolio's SIMPLE-MEAN average (each property
// counts once — unlike the value-weighted aggregates above) on a fixed metric set. ----
type SortKey = BenchmarkMetricKey | "property_name";

const BENCHMARK_METRICS: {
  key: BenchmarkMetricKey;
  label: string;
  fmt: (v: number) => string;
  expenseLike: boolean; // true = lower is better (tone convention from KpiBand.toneClass)
}[] = [
  { key: "noi_per_unit", label: "NOI / unit", fmt: fmtCurrency, expenseLike: false },
  { key: "opex_ratio", label: "Opex ratio", fmt: (v) => fmtPct(v), expenseLike: true },
  { key: "physical_occupancy", label: "Physical occ.", fmt: (v) => fmtPct(v), expenseLike: false },
  { key: "economic_occupancy", label: "Economic occ.", fmt: (v) => fmtPct(v), expenseLike: false },
  { key: "cap_rate", label: "Cap rate", fmt: (v) => fmtPct(v), expenseLike: false },
  { key: "cash_on_cash", label: "Cash-on-cash", fmt: (v) => fmtPct(v), expenseLike: false },
];

function fmtDelta(v: number, key: BenchmarkMetricKey): string {
  const sign = v > 0 ? "+" : ""; // fmtCurrency already renders negatives with a leading "-"
  if (key === "noi_per_unit") return `${sign}${fmtCurrency(v)}`;
  return `${sign}${(v * 100).toFixed(1)}pp`;
}

function deltaArrow(tone: string): string {
  return tone === "is-good" ? "▲" : tone === "is-bad" ? "▼" : "■";
}

function sortValue(row: PropertyBenchmarkRow, key: SortKey): number | string | null {
  return key === "property_name" ? row.property_name : row[key].value;
}

// Null-safe comparator (nulls always sort last, regardless of direction) — same convention
// as the rent roll's sortable table.
function compareBenchRows(a: PropertyBenchmarkRow, b: PropertyBenchmarkRow, key: SortKey, dir: 1 | -1): number {
  const av = sortValue(a, key);
  const bv = sortValue(b, key);
  if (av == null && bv == null) return 0;
  if (av == null) return 1;
  if (bv == null) return -1;
  if (typeof av === "number" && typeof bv === "number") return (av - bv) * dir;
  return String(av).localeCompare(String(bv)) * dir;
}

function BenchmarksSection({ token }: { token: string }) {
  const [data, setData] = useState<PortfolioBenchmarks | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [allTags, setAllTags] = useState<string[]>([]);
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: "property_name", dir: 1 });

  useEffect(() => {
    getTags(token).then(setAllTags).catch(() => {
      /* tag filter is a nice-to-have */
    });
  }, [token]);

  useEffect(() => {
    setData(null);
    getPortfolioBenchmarks(token, undefined, selectedTags).then(setData).catch((e) => setError(e.message));
  }, [token, selectedTags]);

  const sortedRows = useMemo(() => {
    if (!data) return [];
    return [...data.properties].sort((a, b) => compareBenchRows(a, b, sort.key, sort.dir));
  }, [data, sort]);

  const onSort = (key: SortKey) =>
    setSort((cur) => (cur.key === key ? { key, dir: cur.dir === 1 ? -1 : 1 } : { key, dir: 1 }));

  const toggleTag = (t: string) =>
    setSelectedTags((cur) => (cur.includes(t) ? cur.filter((x) => x !== t) : [...cur, t]));

  return (
    <section style={{ marginTop: 34 }}>
      <h3 className="section-title">Benchmarks</h3>
      <p className="hint">
        Every property vs. the portfolio's <strong>simple average</strong> (each property counts once — unlike the
        value-weighted aggregates above). Median is shown alongside the mean since these ratios can be skewed by one
        outlier property. NOI/unit and opex ratio are for the selected period; physical occupancy is that period's
        unit-month-weighted average. Economic occupancy is the rent roll's current lease snapshot (not period-scoped —
        occupancy from lease status is a point-in-time read). Cap rate and cash-on-cash are the same trailing-12,
        acquisition-based figures as the table above. “—” means not enough data to compute honestly; that property is
        excluded from the average, not counted as zero.
      </p>

      {allTags.length > 0 && (
        <div className="tag-filter" style={{ marginBottom: 14 }}>
          <span className="muted" style={{ fontSize: 12.5, fontWeight: 550 }}>
            Filter by tag:
          </span>
          {allTags.map((t) => (
            <button
              key={t}
              type="button"
              className={`tag-chip${selectedTags.includes(t) ? " is-active" : ""}`}
              onClick={() => toggleTag(t)}
              aria-pressed={selectedTags.includes(t)}
            >
              {t}
            </button>
          ))}
          {selectedTags.length > 0 && (
            <button type="button" className="btn btn-ghost" onClick={() => setSelectedTags([])}>
              Clear
            </button>
          )}
        </div>
      )}

      {error && <p className="alert-error">{error}</p>}
      {!error && !data && <p className="hint">Loading…</p>}
      {data && data.properties.length === 0 && (
        <p className="hint">No properties match this filter, or none have summarized data for the period yet.</p>
      )}

      {data && data.properties.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col" style={{ textAlign: "left" }} aria-sort={sort.key === "property_name" ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
                  <button type="button" className="btn btn-ghost" style={{ padding: "2px 4px", fontWeight: 650, fontSize: "inherit" }} onClick={() => onSort("property_name")}>
                    Property
                    {sort.key === "property_name" && <span aria-hidden="true">{sort.dir === 1 ? " ▲" : " ▼"}</span>}
                  </button>
                </th>
                {BENCHMARK_METRICS.map((m) => (
                  <th key={m.key} scope="col" aria-sort={sort.key === m.key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
                    <button type="button" className="btn btn-ghost" style={{ padding: "2px 4px", fontWeight: 650, fontSize: "inherit" }} onClick={() => onSort(m.key)}>
                      {m.label}
                      {sort.key === m.key && <span aria-hidden="true">{sort.dir === 1 ? " ▲" : " ▼"}</span>}
                    </button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              <tr className="row-total">
                <td>Portfolio average ({data.property_count} properties)</td>
                {BENCHMARK_METRICS.map((m) => {
                  const stat = data[m.key];
                  return (
                    <td key={m.key}>
                      {stat.mean != null ? m.fmt(stat.mean) : "—"}
                      <div className="muted" style={{ fontSize: 11 }}>
                        median {stat.median != null ? m.fmt(stat.median) : "—"} · n={stat.count}
                      </div>
                    </td>
                  );
                })}
              </tr>
              {sortedRows.map((row) => (
                <tr key={row.property_id}>
                  <td>{row.property_name}</td>
                  {BENCHMARK_METRICS.map((m) => {
                    const bv = row[m.key];
                    if (bv.value == null) {
                      return (
                        <td key={m.key} className="muted">
                          —
                        </td>
                      );
                    }
                    const tone = bv.delta_vs_mean == null ? "is-flat" : toneClass(bv.delta_vs_mean, m.expenseLike);
                    return (
                      <td key={m.key}>
                        {m.fmt(bv.value)}
                        <div className={`kpi-card__delta ${tone}`} style={{ fontSize: 11, marginTop: 2, justifyContent: "flex-end" }}>
                          <span aria-hidden="true">{deltaArrow(tone)}</span>
                          <span>{bv.delta_vs_mean != null ? fmtDelta(bv.delta_vs_mean, m.key) : "—"}</span>
                          {bv.rank != null && <span className="muted">#{bv.rank}/{data[m.key].count}</span>}
                        </div>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export default function Investments({
  token,
  onGoToProperty,
}: {
  token: string;
  // Optional: jump straight to a property on the Property tab (used by the "missing
  // acquisition data" nudge below). Falls back to just naming the tab if not wired.
  onGoToProperty?: (propertyId: string) => void;
}) {
  const [data, setData] = useState<PortfolioInvestment | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getPortfolioInvestment(token).then(setData).catch((e) => setError(e.message));
  }, [token]);

  if (error) return <p className="alert-error">{error}</p>;
  if (!data) return <p className="hint">Loading…</p>;

  const nudge = data.missing_property_count > 0 && (
    <p className="hint" style={{ background: "var(--warn-soft)", padding: "10px 14px", borderRadius: "var(--radius-sm)" }}>
      <strong>
        {data.missing_property_count} of {data.total_property_count} properties
      </strong>{" "}
      {data.missing_property_count === 1 ? "is" : "are"} missing acquisition data — cap rate, cash-on-cash and DSCR
      can't be computed until purchase price, financing and date are entered. {" "}
      {data.missing_properties.slice(0, 6).map((p, i) => (
        <span key={p.property_id}>
          {i > 0 && ", "}
          {onGoToProperty ? (
            <button
              type="button"
              className="btn btn-ghost"
              style={{ padding: "1px 8px", fontSize: 12.5 }}
              onClick={() => onGoToProperty(p.property_id)}
            >
              {p.property_name}
            </button>
          ) : (
            p.property_name
          )}
        </span>
      ))}
      {data.missing_properties.length > 6 && ` +${data.missing_properties.length - 6} more`}
    </p>
  );

  if (data.properties.length === 0) {
    return (
      <>
        {nudge}
        <p className="hint">
          No properties have acquisition data yet. Open the <strong>Property</strong> tab, pick a property, and add its
          purchase price, financing and date under “Investment returns.”
        </p>
        <BenchmarksSection token={token} />
      </>
    );
  }

  const aggregates = [
    { label: "Portfolio cap rate", value: pct(data.cap_rate), caption: `${data.cap_rate_property_count} properties · value-weighted` },
    { label: "Portfolio cash-on-cash", value: pct(data.cash_on_cash), caption: `${data.cash_on_cash_property_count} with 12+ mo · equity-weighted` },
    { label: "Portfolio DSCR", value: dscr(data.dscr), caption: `${data.dscr_property_count} with debt` },
    { label: "Total invested", value: fmtCurrency(data.total_equity_invested), caption: `equity · ${fmtCurrency(data.total_purchase_price)} price` },
  ];

  return (
    <section>
      {nudge}
      <div className="kpi-grid">
        {aggregates.map((a) => (
          <div className="kpi-card" key={a.label}>
            <div className="kpi-card__label">{a.label}</div>
            <div className="kpi-card__value">{a.value}</div>
            <div className="kpi-card__delta is-flat">{a.caption}</div>
          </div>
        ))}
      </div>

      <h3 className="section-title">By property</h3>
      <p className="hint">
        Cap rate = NOI ÷ purchase price (yield on cost). Cash-on-cash and average need 12 / 24 months of posted data;
        “—” means not enough history yet. Edit inputs on the Property tab.
      </p>
      <div style={{ overflowX: "auto" }}>
        <table className="data-table">
          <thead>
            <tr>
              <th>Property</th>
              <th>Bought</th>
              <th>Price</th>
              <th>Equity</th>
              <th>Cap rate</th>
              <th>Cash-on-cash</th>
              <th>DSCR</th>
              <th>Avg CoC</th>
              <th>Data</th>
            </tr>
          </thead>
          <tbody>
            {data.properties.map((p) => (
              <Row key={p.property_id} p={p} />
            ))}
          </tbody>
        </table>
      </div>

      <BenchmarksSection token={token} />
    </section>
  );
}

function Row({ p }: { p: InvestmentMetrics }) {
  return (
    <tr>
      <td>{p.property_name}</td>
      <td>{p.purchase_date ? fmtMonth(p.purchase_date) : "—"}</td>
      <td>{fmtCurrency(p.purchase_price ?? 0)}</td>
      <td>{fmtCurrency(p.equity_invested ?? 0)}</td>
      <td>
        {pct(p.cap_rate)}
        {p.annualized && p.cap_rate != null && <span className="muted" style={{ fontSize: 11 }}> (ann.)</span>}
      </td>
      <td>{pct(p.cash_on_cash)}</td>
      <td>{dscr(p.dscr)}</td>
      <td>{pct(p.avg_cash_on_cash)}</td>
      <td className="muted">{p.months_available} mo</td>
    </tr>
  );
}
