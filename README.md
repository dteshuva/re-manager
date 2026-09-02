# Real Estate Portfolio Management

A multi-tenant web app for real estate operators to run a rental portfolio off one set of
numbers — **actual** monthly P&L, rent roll, budgets and statutory compliance — at three
levels: **unit → property → portfolio**. It replaces the spreadsheet sprawl most small
landlords live in with a single system that computes the P&L, flags what needs attention,
and reports investment returns.

<!-- Live demo: https://... (coming soon) -->

---

## The problem

Owners of a handful of rental properties track performance in a tangle of spreadsheets: one per
property, inconsistent categories, NOI computed by hand, and no easy way to answer "which
property is bleeding cash this month, and why?" As the portfolio grows, that doesn't scale —
and the numbers quietly drift out of sync.

This app makes the monthly numbers **computed, consistent, and honest**: you record line items,
and everything else — NOI, cash flow, rollups, variance, return metrics — is derived from them.

## Features

- **Three-level P&L.** Rent, operating expenses, NOI, below-the-line costs, and cash flow at
  unit, property, and portfolio level, with flexible period filters (YTD, trailing-12, custom).
- **A closing workflow.** Each property-month moves `draft → posted → locked`; locked months are
  protected from edits, and only an admin can reopen one — every reopen is audited.
- **Attention-first dashboard.** Instead of a wall of numbers, a ranked feed of what actually
  changed: NOI drops, expense spikes, vacancies, and missing data, biggest dollar impact first.
- **Data in three ways.** Manual entry, CSV/Excel bulk import, or **automatic PDF statement
  extraction that runs entirely on-device** — no data leaves the machine and no paid API is used.
  A single-property statement or a whole portfolio's rent roll both work: the rent roll comes back
  as one reviewable statement per property, with its portfolio-wide charges split across them.
- **Rent roll and leases.** Full tenancy history per unit — term, contract rent, escalations,
  security deposits, concessions, percentage rent for retail — plus lease-expiration horizons
  for rollover risk and a **rent waterfall** bridging market rent to cash actually collected.
- **Budget vs. actual.** A flat annual plan per property, with variance against actuals at
  property and portfolio level.
- **Compliance tracking.** Statutory certificates (EICR, Gas/CP12, EPC, HMO and selective
  licences, PAT, Legionella, fire risk) with expiry status and a dashboard alert window.
- **Investment returns.** Enter a property's acquisition cost and get cap rate, cash-on-cash, and
  DSCR, plus value-weighted portfolio aggregates.
- **Portfolio purchases.** Bought several properties on one contract with one blanket loan? Record
  the deal once and its shared closing costs and debt are split across the properties — pro-rata by
  price, evenly, or by the lender's per-property release prices. The shares always add back to the
  deal's totals to the cent, so portfolio-level returns match modelling it as a single entity.
- **Shared expenses.** A bill that covers several properties at once — a portfolio-loan payment, an
  insurance policy over multiple buildings, one management retainer — is entered once and posted to a
  month or a whole year, giving every property its own share as an ordinary line item. Split evenly,
  by purchase price, by unit count, or by figures you supply. A posted line remembers where it came
  from, so re-posting a corrected bill restates it in place, un-posting removes exactly what it
  wrote, and a figure someone typed by hand is reported as a conflict rather than overwritten.
- **Fees stated as a rate.** A management fee is "8% of rent", not a fixed figure. Enter the rate
  and the amount is derived from that month's rent and **kept there** — a renewal, a vacancy or a
  door re-let at a different price updates the fee by itself, even though rent is entered on a
  different record weeks apart. A fee on a unit is charged on that unit's rent; one at the property
  tier is charged on the property's whole rent for the month. It stays an ordinary line item, so
  the P&L, rollups and exports need to know nothing about it.
- **Instant reclassification.** Move a category between accounting buckets (e.g. capex →
  operating) and watch NOI and cash flow recompute live — no data migration.
- **Slice and export.** Tag properties and filter any report by tag, save the views you use, and
  export the P&L or variance to Excel/CSV as real numbers, not formatted strings.

## Engineering highlights

The parts I'm most happy with — where a design decision does real work:

- **The P&L is computed, never stored.** NOI and cash flow are derived in SQL views from the raw
  line items on every read. There's no denormalized total to fall out of sync, and corrections
  take effect the instant they're entered.

- **Classification is the only thing that drives the math.** Every computation groups by an
  *effective classification* resolved at query time (`COALESCE(line-item override, category
  default)`). So reclassifying a whole category — moving "roof replacement" from capex to
  operating — is a single `UPDATE` that instantly recomputes NOI across all history. No
  migration, no touched data.

- **A hard line between reference data and the math.** Leases, market rents, budgets and shell
  flags are *reference* data: they inform reporting but **never** feed NOI or cash flow, which
  stay driven solely by recorded line items. That invariant is what lets the rent roll and the
  waterfall be rich and opinionated without any risk of contaminating the financials.

- **Tenant isolation with one choke point.** Rather than trusting every query to remember its
  `account_id`, ownership is resolved through a single module of scoped resolvers, backed by
  composite foreign keys so the database rejects a cross-account reference the app might miss.
  Not-yours returns **404, never 403** — a 403 would confirm the id exists and leak the shape of
  another account's portfolio. Categories are a per-account copy rather than a shared list,
  precisely *because* reclassification recomputes financials: one tenant must never be able to
  move another's NOI.

- **Rollups that don't lie.** Rent and operating costs aggregate cleanly up the hierarchy, but
  shared property-level costs (debt service, a new roof) are *never* silently allocated across
  units. The app keeps property-tier items as their own line, so a property total honestly
  differs from the sum of its units — rather than fabricating a per-unit split.

- **One idempotent import core, three producers.** CSV/Excel parsing, structured JSON, and PDF
  extraction all emit the same row shape into a single validated, lock-aware, dry-runnable
  importer keyed on `(property, unit, month, category)`. Re-importing a month overwrites in
  place. Adding a new source means writing one row-producer — storage and computation never change.

- **On-device PDF extraction.** Property managers each send statements in their own layout, so the
  extractor is format-agnostic, not template-driven: pdfplumber pulls text and tables, rules find
  the month/property/line items, and an *optional* local Ollama LLM handles messy free-text
  layouts. Everything runs locally with zero per-use cost, and it degrades gracefully when the LLM
  isn't there.

- **One PDF is not always one statement.** A portfolio agent sends a *rent roll*: one page, a line
  per property (address, the rent period it covers, the rent collected), then a management fee and
  the odd contractor bill charged once over the whole portfolio. That file is split into one
  statement per property, so the rest of the system needs no new concept — it is the same batch of
  mixed-property statements the importer already handles. Three things it gets right, because each
  is a way to post wrong money silently: addresses are matched on normalised form, so a statement's
  "125 Allendale Road" finds a property stored as "125 Allendale Rd, Gateshead"; months are read
  day-first from the stated period and resolved to the month it mostly covers (a period ending in a
  mistyped year is repaired rather than believed); and a charge made once over twelve properties is
  split pro-rata by rent through the same largest-remainder allocator the bulk-purchase feature
  uses, so the shares add back to the charged figure to the penny and the statement's own
  arithmetic still reconciles after import. Totals, sub-totals and unlabelled arrears columns are
  recognised as the statement's arithmetic or reported as unattributed — never imported as money.

- **Metrics that refuse to mislead.** Return metrics only appear when they can be computed
  honestly — cash-on-cash needs a full 12 months (a lumpy partial year is never annualized into a
  fake return), DSCR needs recorded debt service. Portfolio figures are value-weighted (Σ NOI ÷ Σ
  price), not a naïve average of percentages. The rent waterfall reconciles exactly to the
  unit-tier P&L, and splits its collections gap into *concessions* (a leasing decision) and *bad
  debt* (delinquency) rather than lumping them together.

- **Status derived from the calendar, not a nightly job.** A certificate is expired, expiring or
  valid as a function of its expiry date and today — computed on read, so it can never be stale
  and there's no scheduled task to fail silently.

- **Fast at scale via a summary layer.** The dashboard and metrics read pre-aggregated monthly
  rollups (refreshed on post/lock), not raw line items at request time — so the attention feed
  stays quick across thousands of units. Line items remain the single source of truth.

- **Verification that drives the real API.** A phased suite asserts against a hand-computed demo
  fixture — including planted anomalies with known magnitudes — on a throwaway database built
  fresh from migrations. Isolation gets its own adversarial pass: it signs up two accounts, gives
  one a portfolio, then probes every listing, by-id and write endpoint as the other and asserts
  it sees nothing.

## Tech stack

| Layer       | Choice                                                              |
| ----------- | ------------------------------------------------------------------ |
| Backend     | FastAPI + SQLAlchemy 2.0 + Alembic, JWT auth (PyJWT + bcrypt)      |
| Database    | Postgres (Supabase-compatible), P&L computed in SQL views          |
| Frontend    | React + Vite + TypeScript, Recharts for trends                    |
| PDF import  | pdfplumber + optional local Ollama LLM — no paid API              |
| Export      | openpyxl (Excel) + stdlib csv                                      |

## Architecture

Line items are the source of truth; everything else is derived.

```
line_items ──► v_line_item_resolved ──► v_monthly_pnl ──► v_portfolio_monthly
(raw entries)   (effective class.)      (per unit/prop)    (portfolio rollup)
      │
      └──► property / unit / portfolio_month_summary   (pre-aggregated, refreshed on post/lock)
                     │
                     ├──► attention feed  +  investment metrics
                     └──► budget variance  +  rent waterfall
                                    ▲
      lease / market rent / budgets ─┘   (reference data — never feeds NOI or cash flow)
```

Every table hangs off an **account**; ownership is resolved through one scoped-lookup module and
enforced underneath by composite foreign keys.

**Core tables:** `accounts`, `users`, `properties`, `units`, `lease`, `categories` (per-account),
`monthly_records`, `line_items`, `period_status` (the workflow), `audit_log`. **Plus** the
reference/config tier: `property_investment`, `portfolio_acquisition` (bulk purchases, whose
allocated shares are written down onto `property_investment` so every metric reads one place),
`property_budget`, `property_tag`, `property_certificate`, and per-account `attention_settings`. The interactive API reference
(FastAPI / Swagger) is served at `/docs`.
