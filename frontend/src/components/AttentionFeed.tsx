import type { AttentionFeed as Feed, AttentionItem, AttentionType } from "../api";
import { clickableProps } from "../hooks/clickable";
import { t } from "../terms";
import { fmtCurrency, fmtMonth, isUK } from "../ui";

// The heart of the portfolio dashboard: a ranked list of exceptions for the selected
// month, each one sentence + the number + a drill affordance. Data comes from
// /portfolio/attention (computed from the rollups), already ranked by $ magnitude.

// A function rather than a constant because the vocabulary follows the account's currency
// (see terms.ts), which App sets from /auth/me after this module is first evaluated.
const typeMeta = (): Record<AttentionType, { label: string; cls: string }> => ({
  noi_drop: { label: "NOI Drop", cls: "feed-chip--noi" },
  expense_spike: { label: "Expense Spike", cls: "feed-chip--spike" },
  vacancy: { label: t("Vacancy"), cls: "feed-chip--vacancy" },
  occupancy_drop: { label: "Occupancy Drop", cls: "feed-chip--vacancy" },
  high_vacancy: { label: isUK() ? "High Voids" : "High Vacancy", cls: "feed-chip--vacancy" },
  missing_data: { label: "Missing Data", cls: "feed-chip--missing" },
  arrears: { label: "Arrears", cls: "feed-chip--spike" },
});

export default function AttentionFeed({
  feed,
  onDrill,
}: {
  feed: Feed;
  // Drill to the property (Level 2 lands in sub-step 4); optional for now.
  onDrill?: (item: AttentionItem) => void;
}) {
  if (feed.items.length === 0) {
    return (
      <p className="hint" style={{ margin: "0 0 18px" }}>
        {`✓ Nothing needs attention in this period — no NOI drops, expense spikes, ${t("vacancy")}s, `}
        arrears, or missing data.
      </p>
    );
  }
  // Show each item's month when the selected period spans more than one month.
  const multiMonth = feed.period_from !== feed.period_to;
  const meta_ = typeMeta();
  return (
    <ul className="feed">
      {feed.items.map((it, i) => {
        // An item type this build doesn't know (a newer backend) must not blank the whole feed.
        const meta = meta_[it.type] ?? { label: it.type, cls: "feed-chip--missing" };
        return (
          <li
            key={`${it.type}-${it.property_id}-${it.unit_id ?? ""}-${it.month ?? i}`}
            className={`feed-item${onDrill ? " is-clickable" : ""}`}
            {...clickableProps(onDrill ? () => onDrill(it) : undefined)}
          >
            <span className={`feed-chip ${meta.cls}`}>{meta.label}</span>
            <div className="feed-item__body">
              <div className="feed-item__title">
                {/* Always name the property, even for a unit-scoped item. A portfolio of
                    single-let houses numbers every door "1", so "Unit 1" alone produced a
                    column of identical rows with no way to tell which house was in trouble —
                    and even in a multifamily portfolio the building is the thing you act on. */}
                {it.unit_number ? `${it.property_name} · unit ${it.unit_number}` : it.property_name}
                {it.rolled_up && it.count != null && (
                  <span
                    className="feed-chip feed-chip--missing"
                    style={{ marginLeft: 6 }}
                    title={
                      it.type === "arrears"
                        ? `${it.count} further units in arrears at this property, below the ones named individually above`
                        : `${it.count} units rolled up — magnitude and % were tightly clustered`
                    }
                  >
                    {it.count} units
                  </span>
                )}
                {multiMonth && it.month && <span className="feed-item__month">{fmtMonth(it.month)}</span>}
              </div>
              <div className="feed-item__label">{it.label}</div>
            </div>
            <div className="feed-item__mag">
              <span className="muted" style={{ fontSize: 11 }}>
                {it.type === "missing_data" ? "at risk" : it.type === "arrears" ? "owed" : "impact"}
              </span>
              <strong>{fmtCurrency(it.magnitude)}</strong>
            </div>
            {onDrill && <span className="feed-item__chevron" aria-hidden="true">›</span>}
          </li>
        );
      })}
    </ul>
  );
}
