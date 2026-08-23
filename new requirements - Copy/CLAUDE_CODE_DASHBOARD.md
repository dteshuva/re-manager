# Insight Dashboard — Kickoff prompt for Claude Code

Add INSIGHT_DASHBOARD_SPEC.md to the repo alongside PROJECT_SPEC.md, then paste the message
below. Do the sub-steps in order; review each before continuing.

---

I'm evolving this app from a personal tracker into a product for operators managing hundreds
to thousands of units. The new design is in INSIGHT_DASHBOARD_SPEC.md (companion to
PROJECT_SPEC.md). Read both. The core experience is top-down: land on a portfolio dashboard
that surfaces what needs attention, then drill into a property, then into a unit.

**Work through the sub-steps in INSIGHT_DASHBOARD_SPEC.md in order. Pause after each so I can
review.** Keep the existing financial model, classifications, and rollup rules intact.

Start with sub-step 1 only:

**Sub-step 1 — Rollup/summary layer.**
Build pre-aggregated monthly summaries so dashboards are fast at scale (thousands of units,
many months). For each property-month and for each portfolio-month, precompute: gross rent,
operating expenses, NOI, capex, debt service, other below-line, cash flow, occupancy, and
unit count. Use a materialized view or a summary table refreshed when a month is posted/locked.
Raw line items remain the source of truth — summaries are derived, and must reconcile exactly
with the line-item math (NOI = rent − operating; cash flow = NOI − below-line). Respect the
rollup-honesty rule: property-tier-only items (unit_id NULL) stay at the property tier and are
not allocated to units.

Before building, first generate enough seed data to make scale real — e.g. ~20 properties,
~1,000 units total, 12 months of line items — so we can actually measure query performance.

When done: show me the summary schema, how/when it refreshes, a sample portfolio-month result,
and a quick before/after on dashboard-style query speed (raw aggregation vs. summary). Then wait.
