# Real Estate Portfolio Management

A web app for real estate operators to track the **actual** monthly financial performance of a
rental portfolio — rent, expenses, NOI, and cash flow — at three levels: **unit → property →
portfolio**. It replaces the spreadsheet sprawl most small landlords live in with a single
system that computes the P&L, flags what needs attention, and reports investment returns.

<!-- Live demo: https://... (coming soon) -->

---

## The problem

Owners of a handful of rental properties track performance in a tangle of spreadsheets: one per
property, inconsistent categories, NOI computed by hand, and no easy way to answer "which
property is bleeding cash this month, and why?" As the portfolio grows, that doesn't scale —
and the numbers quietly drift out of sync.

This app makes the monthly numbers **computed, consistent, and honest**: you record line items,
and everything else — NOI, cash flow, rollups, return metrics — is derived from them.

## Features

- **Three-level P&L.** Rent, operating expenses, NOI, below-the-line costs, and cash flow at
  unit, property, and portfolio level, with flexible period filters (YTD, trailing-12, custom).
- **A closing workflow.** Each property-month moves `draft → posted → locked`; locked months are
  protected from edits, and only an admin can reopen one — every reopen is audited.
- **Attention-first dashboard.** Instead of a wall of numbers, a ranked feed of what actually
  changed: NOI drops, expense spikes, vacancies, and missing data, biggest dollar impact first.
- **Data in three ways.** Manual entry, CSV/Excel bulk import, or **automatic PDF statement
  extraction that runs entirely on-device** — no data leaves the machine and no paid API is used.
- **Investment returns.** Enter a property's acquisition cost and get cap rate, cash-on-cash, and
  DSCR, plus value-weighted portfolio aggregates.
- **Instant reclassification.** Move a category between accounting buckets (e.g. capex →
  operating) and watch NOI and cash flow recompute live — no data migration.

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

- **Metrics that refuse to mislead.** Return metrics only appear when they can be computed
  honestly — cash-on-cash needs a full 12 months (a lumpy partial year is never annualized into a
  fake return), DSCR needs recorded debt service. Portfolio figures are value-weighted (Σ NOI ÷ Σ
  price), not a naïve average of percentages.

- **Fast at scale via a summary layer.** The dashboard and metrics read pre-aggregated monthly
  rollups (refreshed on post/lock), not raw line items at request time — so the attention feed
  stays quick across thousands of units. Line items remain the single source of truth.

## Tech stack

| Layer       | Choice                                                              |
| ----------- | ------------------------------------------------------------------ |
| Backend     | FastAPI + SQLAlchemy 2.0 + Alembic, JWT auth (PyJWT + bcrypt)      |
| Database    | Postgres (Supabase-compatible), P&L computed in SQL views          |
| Frontend    | React + Vite + TypeScript, Recharts for trends                    |
| PDF import  | pdfplumber + optional local Ollama LLM — no paid API              |

## Architecture

Line items are the source of truth; everything else is derived.

```
line_items ──► v_line_item_resolved ──► v_monthly_pnl ──► v_portfolio_monthly
(raw entries)   (effective class.)      (per unit/prop)    (portfolio rollup)
      │
      └──► property / unit / portfolio_month_summary   (pre-aggregated, refreshed on post/lock)
                     │
                     └──► attention feed  +  investment metrics
```

**Core tables:** `users`, `properties`, `units`, `categories` (a shared global list),
`monthly_records`, `line_items`, `period_status` (the workflow), `audit_log`. **Plus**
per-account `attention_settings` and per-property `property_investment` inputs. The interactive
API reference (FastAPI / Swagger) is served at `/docs`.


