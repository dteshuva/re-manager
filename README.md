# Real Estate Portfolio Management

Hosted web app to track **actual** monthly financial performance across a real estate
portfolio — rent, expenses, NOI, and cash flow at three levels: **unit → property →
portfolio**. See [PROJECT_SPEC.md](PROJECT_SPEC.md) for the full brief.

> **Status: all 7 build phases complete** — scaffold + schema + migrations + auth + computed
> P&L views + seed + aggregation endpoints + manual CRUD/entry forms with the
> draft→posted→locked workflow + CSV/Excel bulk import + portfolio dashboard + property
> detail with unit drill-down + unit detail + reclassification control. A frontend
> design/polish pass is planned next. Out of scope for v1: underwriting/proforma — the
> schema leaves room, nothing is built.

## Stack

| Layer    | Choice                                                              |
| -------- | ------------------------------------------------------------------ |
| Database | Postgres (Supabase-compatible)                                     |
| Backend  | FastAPI + SQLAlchemy 2.0 + Alembic + psycopg2; JWT (PyJWT) + bcrypt |
| Frontend | React + Vite + TypeScript + Recharts (trend charts)               |

---

## What Phase 1 delivers

1. **Scaffold** — [`backend/`](backend/) (FastAPI) and [`frontend/`](frontend/) (React + Vite).
2. **Schema + migration** — one Alembic migration ([`0001_initial.py`](backend/migrations/versions/0001_initial.py))
   creating every table, the two enums, integrity constraints, and the P&L views.
3. **Auth** — email/password login (JWT). Roles `admin` / `member`. **Only admins can
   unlock a locked month, and every unlock writes an `audit_log` row.**
4. **Computed P&L views** — `v_monthly_pnl` / `v_portfolio_monthly` compute gross rent,
   operating expenses, NOI, below-NOI total, and cash flow for any scope/month range.
   **NOI and cash flow are computed in SQL, never stored**, and are summed **by
   classification**, so new categories and reclassifications are picked up with zero code change.
5. **Seed** — 3 properties (one multifamily with 3 units, two single-asset), 9 global
   categories, 3 months of line items, and a locked month to demo admin unlock.

---

## Local setup

### Prerequisites
- Python 3.10+
- Node 18+ (for the frontend dev server)
- A Postgres database — either a **Supabase** project or a local Postgres.

### 1. Backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then edit DATABASE_URL + JWT_SECRET
```

**Point `DATABASE_URL` at your database.** For Supabase:
```
DATABASE_URL=postgresql+psycopg2://postgres:[PASSWORD]@db.[REF].supabase.co:5432/postgres
```

No local Postgres handy? A helper spins up a throwaway, user-owned cluster (no root/Docker):
```bash
./scripts/dev_db.sh start          # Postgres on localhost:5433, db 're_manager'
# set DATABASE_URL=postgresql+psycopg2://postgres@localhost:5433/re_manager
```

Run migrations and seed:
```bash
alembic upgrade head
python -m app.seed                 # creates admin (SEED_ADMIN_EMAIL / SEED_ADMIN_PASSWORD)
uvicorn app.main:app --reload      # API on http://localhost:8000  (docs at /docs)
```

### 2. Frontend

```bash
cd frontend
npm install
cp .env.example .env               # VITE_API_URL=http://localhost:8000
npm run dev                        # http://localhost:5173
```

Log in with the seeded admin (`admin@example.com` / `admin12345`) to see the portfolio
monthly P&L table rendered straight from the computed views.

---

## Schema overview

Eight tables and three views. Two Postgres enums:
`classification` = `rent | operating | capex | debt_service | other_below_line`;
`period_status_enum` = `draft | posted | locked`.

| Table             | Purpose                                                                                          |
| ----------------- | ------------------------------------------------------------------------------------------------ |
| `users`           | Auth. `role IN (admin, member)`; bcrypt `hashed_password`.                                        |
| `properties`      | `type IN (multifamily, single)`.                                                                  |
| `units`           | Belong to a property (multifamily only).                                                          |
| `categories`      | **Global shared list** — `name`, `default_classification`, `active`. Data, not code.             |
| `monthly_records` | One per (property, [unit], month). **`unit_id IS NULL` ⇒ property-tier record.**                  |
| `line_items`      | `category_id`, optional `classification` override, `amount`. Unique `(record, category)`.         |
| `period_status`   | Per property-month workflow: `draft → posted → locked`.                                           |
| `audit_log`       | `user_id, action, entity, entity_id, before, after` — written on every unlock.                    |

Views: `v_line_item_resolved` (effective classification per line) → `v_monthly_pnl`
(P&L per property/unit/month) → `v_portfolio_monthly` (portfolio rollup).

### Key design decisions

- **Classification is the only thing that drives the math.** Computations `GROUP BY`
  the *effective* classification, so adding a category never changes code or schema.

- **Reclassification with no migration.** The effective classification is
  `COALESCE(line_items.classification, categories.default_classification)`, resolved at
  **query time** in `v_line_item_resolved`. Changing a category's
  `default_classification` (a single `UPDATE`) instantly recomputes NOI/cash flow for all
  its line items — no data migration, history untouched. `line_items.classification` is
  an optional per-line *override* for one-off exceptions.

- **NOI / cash flow computed, never stored.** `v_monthly_pnl`:
  - `gross_rent` = Σ `rent`; `operating_expenses` = Σ `operating`
  - `noi` = gross_rent − operating_expenses *(capex is **not** in NOI)*
  - `below_noi` = Σ everything that is not rent/operating (capex + debt_service +
    other_below_line) — so any future classification automatically falls below the line
  - `cash_flow` = noi − below_noi

- **Honest rollups.** Rent and operating expenses are additive (unit → property →
  portfolio). Property-tier-only items (`unit_id NULL`: shared capex, debt service) live
  at the property tier and are **never allocated to units**, so `property total ≠ Σ units`
  for those. The `v_monthly_pnl` grain keeps unit and property-tier rows separate; callers
  filter `unit_id IS NULL` to show property-tier-only items as their own line.

- **Idempotent import seam (future-proofing).** Uniqueness on `monthly_records
  (property_id, unit_id, month)` + `line_items (monthly_record_id, category_id)` keys data
  on `(property, unit, month, category)`, so re-importing a month overwrites in place.

- **Room for underwriting.** v1 builds none of it (per spec); an `underwriting` table can
  be added later without touching the actuals model above.

---

## API (through Phase 2)

| Method | Path                                       | Auth  | Notes                                        |
| ------ | ------------------------------------------ | ----- | -------------------------------------------- |
| POST   | `/auth/login`                              | —     | OAuth2 password form → JWT                   |
| GET    | `/auth/me`                                 | user  | Current user                                 |
| POST   | `/auth/users`                              | admin | Provision a user                             |
| POST   | `/periods/{id}/unlock`                     | admin | Unlock locked month → `posted`; audits       |
| GET    | `/portfolio/monthly?from&to`               | user  | Portfolio P&L by month                       |
| GET    | `/portfolio/breakdown?from&to`             | user  | Period totals: portfolio → property → unit   |
| GET    | `/properties/{id}/monthly?from&to`         | user  | Property P&L by month; honest rollup split   |
| GET    | `/properties/{id}/units/monthly?from&to`   | user  | Per-unit monthly breakdown for a property    |
| GET    | `/units/{id}/monthly?from&to`              | user  | Single-unit P&L by month                     |

`from`/`to` are inclusive month bounds (`YYYY-MM-01`); both optional.

### CRUD + month workflow (Phase 3)

| Method | Path | Notes |
| ------ | ---- | ----- |
| GET/POST | `/properties`                       | List / create properties |
| GET/PATCH/DELETE | `/properties/{id}`        | Read / update / delete (cascades) |
| GET/POST | `/properties/{id}/units`            | List / create units (multifamily only) |
| GET/PATCH/DELETE | `/units/{id}`             | Read / update / delete a unit |
| GET/POST | `/categories`                       | List / create categories (`?active_only=true`) |
| PATCH    | `/categories/{id}`                  | Rename, toggle `active`, or **reclassify** |
| GET      | `/records?property_id&unit_id&month`| List monthly records |
| POST     | `/records`                          | **Idempotent upsert** of a property/unit month + its line items |
| GET/PATCH/DELETE | `/records/{id}`           | Read / update notes / delete a record |
| POST     | `/records/{id}/line-items`          | Add or update one line item (upsert by category) |
| PATCH/DELETE | `/line-items/{id}`              | Update / delete a line item |
| GET      | `/periods?property_id&month`        | List property-month statuses |
| PUT      | `/periods`                          | Set status `draft`→`posted`→`locked` (upsert) |
| POST     | `/periods/{id}/unlock`              | **Admin-only** reopen of a locked month (audited) |

**Month workflow.** Each property-month is `draft → posted → locked`. `POST /records` is
keyed on `(property_id, unit_id, month)`, so re-saving June overwrites it cleanly rather
than duplicating (the same idempotent path a future CSV/automation importer will reuse).
Writes to a **locked** property-month are rejected with `423 Locked`; only an admin can
reopen it via `/periods/{id}/unlock`, which writes to `audit_log`. Verify the whole flow
with `PYTHONPATH=.pydeps python3 verify_phase3.py`.

The frontend has six tabs: **Dashboard** (portfolio P&L + trend charts + breakdown),
**Property** (property detail with a Total / By-unit toggle), **Data Entry** (per
property/unit/month line-item form + workflow buttons), **Import** (bulk upload), **Manage**
(properties, units, categories), and **Reclassify** (reclassification control with live NOI
recompute).

### Portfolio dashboard (Phase 5)

The **Dashboard** tab adds, on top of the monthly P&L table:
- a reusable **period selector** — All / **YTD** / **T12** (trailing-12) / custom month
  **Range**, with an "as of" anchor that defaults to the latest month that has data;
- **summary cards** totalling rent, operating, NOI, below-NOI, and cash flow for the period;
- **trend charts** (Recharts): a line chart of **NOI** and **Cash Flow** over time, and a
  bar chart of **Gross Rent vs Operating Expenses**;
- a **Breakdown by property & unit** table (`GET /portfolio/breakdown`): period totals for
  every property, expandable to each unit plus the highlighted property-tier-only items,
  with a portfolio total row — so you see the same metrics at portfolio, property, and unit
  level in one place. The per-property total honestly reflects shared items, so it can
  differ from the sum of its units.

No backend changes were needed — the selector just narrows the `from`/`to` passed to
`GET /portfolio/monthly`, and all figures stay computed from the P&L views. The
`PeriodSelector` component is reused by the property/unit detail views in Phase 6–7.

### Property detail + unit drill-down (Phase 6)

The **Property** tab picks a property, applies the same period selector, shows the
NOI/Cash-Flow trend chart, and renders the property's monthly P&L. For a multifamily
property, **clicking a month** expands it to show each unit's rent & expenses for that
month, plus a highlighted **Property-tier only** row (shared capex / debt service, *not*
allocated to units). The month row itself is the property total, so the spec's rule is
visible: `property total ≠ sum of units` for property-tier-only items.

It's driven by `GET /properties/{id}/monthly` (the `units` / `property_tier` split) and
`GET /properties/{id}/units/monthly`. Single-asset properties just show the monthly table.

### Unit detail + reclassification control (Phase 7)

- **Unit** tab: pick a multifamily property + unit, apply the period selector, and see that
  unit's monthly P&L (rent / operating / NOI / capex / cash flow) plus the trend chart, via
  `GET /units/{id}/monthly`. Units carry only unit-specific items; shared property-tier
  items never appear here.
- **Reclassify** tab: the spec's required reclassification control. Each category's
  `classification` is the only thing that drives the math; changing it `PATCH`es the
  category and the portfolio totals (NOI, below-NOI, cash flow) **recompute live**, with a
  before→after NOI delta shown — no schema change, no migration. (E.g. moving "Roof
  Replacement" from capex to operating moves $8,000 above the NOI line and NOI drops by
  exactly that.)

This completes the 7-phase build order. Frontend styling is intentionally lightweight
(inline styles); a dedicated design/polish pass is the planned next step.

### Bulk import (Phase 4)

| Method | Path | Notes |
| ------ | ---- | ----- |
| GET  | `/import/template`            | Download a ready-to-edit CSV template |
| POST | `/import/file`               | Upload CSV/Excel + a column **mapping** (multipart) |
| POST | `/import/rows`               | Apply **structured rows** as JSON (the automation seam) |
| GET  | `/import/missing?month=`     | "What's missing": property/unit-months with no data |

`/import/file` and `/import/rows` share one **import core** (`app/importer.py`); the
CSV/Excel parser (`app/parsing.py`) is just one producer of rows. A future
rent-statement/PDF/LLM parser would emit the same `ImportRow` shape into the same core —
storage and computation never change, only the row-producer does.

- **Column mapping**: the request maps canonical fields (`property`, `unit`, `month`,
  `category`, `amount`, optional `classification` / `*_id`) to your file's column headers.
- **Validation + report**: every row is checked (known property/unit/category, parseable
  month/amount, not a locked month). The response is a report with per-row errors so you
  can fix and re-upload. `dry_run=true` previews without writing; `on_error=abort`
  (all-or-nothing) or `skip` (apply good rows, report the rest).
- **Idempotent**: keyed on `(property, unit, month, category)` — re-importing a month
  overwrites the affected line items in place and leaves other categories untouched.

Verify with `PYTHONPATH=.pydeps python3 verify_phase4.py` (CSV + Excel + JSON-seam,
dry-run vs commit, abort vs skip, locked-month rejection, what's-missing).

**Honest rollups (Phase 2 core).** `GET /properties/{id}/monthly` returns the property
total plus two distinct sub-blocks so property-tier-only items are never silently blended
into unit sums:

```jsonc
{
  "month": "2026-02-01",
  "gross_rent": 4575, "capex": 8000, "debt_service": 2600, "noi": 2525, "cash_flow": -8075,
  "units":         { "gross_rent": 4575, "capex": 0,    "debt_service": 0,    ... },  // additive across units
  "property_tier": { "gross_rent": 0,    "capex": 8000, "debt_service": 2600, ... }   // unit_id NULL, not allocated
}
```

`total == units + property_tier` per metric, but for capex / debt service (and therefore
NOI / cash flow) `property total ≠ sum of units` — by design. Verify with
`PYTHONPATH=.pydeps python3 verify_phase2.py`.

CRUD + entry forms arrive in Phase 3.

---

## Sample result — portfolio monthly NOI (from seed data)

```
  month  | gross_rent | operating_expenses |   noi   | below_noi | cash_flow
---------+------------+--------------------+---------+-----------+-----------
 2026-01 |   12100.00 |            4620.00 | 7480.00 |   8200.00 |   -720.00
 2026-02 |   12175.00 |            4620.00 | 7555.00 |  16200.00 |  -8645.00
 2026-03 |   12250.00 |            4620.00 | 7630.00 |   8200.00 |   -570.00
```

NOI grows steadily with rent; **February's cash flow dips because an $8,000 roof
replacement (capex) lands *below* the NOI line — NOI itself is unaffected**, exactly per
the financial model.
