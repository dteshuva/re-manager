# Insight Dashboard & Drill-Down — Design Spec

Companion to PROJECT_SPEC.md. Defines the top-down "land → see what's wrong → drill to the
unit" experience for operators with hundreds to thousands of units.

## Guiding principle
At scale, the product's job is **surfacing exceptions, not displaying everything**. Every
screen answers "what needs my attention this month?" Every flagged item links down to the
property or unit causing it. Three levels: Portfolio → Property → Unit.

Default time context everywhere: **current month vs. prior month**, with a selector for
month range / YTD / T12. "Change" always means vs. the comparison period unless stated.

---

## Level 1 — Portfolio dashboard (the landing page)

### Top band: headline KPIs (portfolio totals for the selected month)
- Gross Rent, Operating Expenses, NOI, Cash Flow — each with the value, the absolute change,
  and the % change vs. prior month. Color the change (up good / down bad, except expenses).
- Occupancy % (occupied units / total units) and its change.
- A small NOI and Cash Flow trend sparkline over T12.

### Attention feed (the heart of the page) — ranked list of exceptions
Each item is one sentence + the number + a link to the property/unit. Ranked by financial
magnitude (biggest $ impact first). Surface signals like:
- **NOI drop**: properties whose NOI fell most vs. prior month (absolute $ and %).
- **Expense spikes**: a property's operating expense (or a single category) materially above
  its own trailing-3-month average (e.g. > X% and > $Y, both thresholds configurable).
- **Vacancy / lost rent**: units that went vacant, or properties whose occupancy dropped.
- **Delinquency** (if/when rent-collected vs. rent-billed is tracked): units/properties with
  rent billed but not collected. (Flag as future if that data isn't modeled yet.)
- **Missing data**: properties/units with no posted records for the selected month
  (the "what's missing" check from PROJECT_SPEC).
- **Capex events**: properties with unusually large capex this month (context, not alarm).

Each feed item: short label, the metric, the delta, and a chevron that drills to Level 2.

### Portfolio composition (secondary, below the feed)
- A ranked table of all properties: name, units, occupancy, rent, NOI, cash flow, NOI change.
  Sortable, searchable, paginated/virtualized (could be thousands of rows — do NOT render all
  at once; virtualize or server-paginate). This is the "browse everything" fallback, not the
  primary path.

---

## Level 2 — Property detail (arrived at by drilling from a flag or the table)

Context header: property name, type, unit count, occupancy, and the same KPI band scoped to
this property (rent / opex / NOI / cash flow + changes).

- **Monthly P&L table** for the property: month-by-month rows with rent, operating expenses,
  NOI, below-NOI items (capex, debt service, other), cash flow. Honor PROJECT_SPEC rollup
  rules — show property-tier-only items (shared capex, debt service) as their own clearly
  labeled lines, not blended into unit sums.
- **Property-scoped attention feed**: the same exception logic, but for this property's units
  — which units dropped, which are vacant, which had expense spikes.
- **Unit roster** (for multifamily): a virtualized table of units — unit #, occupancy/status,
  rent, NOI, cash flow, change vs. prior month. Sortable; each row drills to Level 3.
- NOI / cash-flow trend chart for the property over T12.

---

## Level 3 — Unit detail

Context header: unit #, status, property it belongs to.
- Month-by-month P&L for the unit (rent, unit-specific operating expenses, unit-specific
  capex, NOI, cash flow). Note: unit scope excludes property-tier-only items by design.
- Trend chart over T12.
- Reminder line that property-level shared costs are not allocated here (per PROJECT_SPEC),
  so unit cash flow is not a pro-rata share of property cash flow.

---

## Cross-cutting requirements

### Performance (this is now load-bearing)
- Thousands of units × many months means naive per-request aggregation will be slow.
- Maintain **pre-aggregated monthly rollups** per property and per portfolio (a materialized
  view or a summary table refreshed when a month is posted/locked). Raw line items stay the
  source of truth; rollups make dashboards instant.
- All large tables are server-paginated or virtualized — never render thousands of rows.
- The attention feed is computed from the rollups, not from raw line items at request time.

### Configurable thresholds
- Expense-spike %, minimum $ materiality, NOI-drop threshold, etc. should be settings
  (per account), not hardcoded. Start with sane defaults; expose them later.

### Honesty about rollups
- Everywhere a property total is shown next to unit sums, respect the PROJECT_SPEC rule:
  property-tier-only items are not allocated to units, so property total ≠ Σ units for those.
  Never hide this — label property-tier-only lines explicitly.

### Out of scope for this phase
- Multi-tenancy / org isolation and auth-at-scale (important, but a separate phase).
- Delinquency requires billed-vs-collected rent; if not yet modeled, render the feed item as
  a stub/"coming soon" rather than faking it.

---

## Suggested build sub-steps (review each before moving on)
1. Rollup/materialized summary layer (property-month and portfolio-month) refreshed on post.
2. Portfolio dashboard: KPI band + trend sparklines from the rollups.
3. Attention feed engine: NOI drop, expense spike, vacancy, missing data (start with these
   four; they're computable from existing data). Ranked by $ impact, each linking to scope.
4. Property detail: scoped KPI band, monthly P&L, property-scoped feed, virtualized unit roster.
5. Unit detail: monthly P&L + trend.
6. Configurable thresholds as account settings.
