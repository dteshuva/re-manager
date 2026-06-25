# Real Estate Portfolio Management

Hosted web app to track **actual** monthly financial performance across a real estate
portfolio — rent, expenses, NOI, and cash flow at three levels: **unit → property →
portfolio**. See [PROJECT_SPEC.md](PROJECT_SPEC.md) for the full brief.

> **Status: Phase 1 complete** (scaffold + schema + migrations + auth + computed P&L
> views + seed). Phases 2–7 (aggregation endpoints, CRUD/entry, import, dashboards,
> drill-down, reclassification UI) are not built yet.

## Stack

| Layer    | Choice                                                              |
| -------- | ------------------------------------------------------------------ |
| Database | Postgres (Supabase-compatible)                                     |
| Backend  | FastAPI + SQLAlchemy 2.0 + Alembic + psycopg2; JWT (PyJWT) + bcrypt |
| Frontend | React + Vite + TypeScript                                          |

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

## API (Phase 1 surface)

| Method | Path                              | Auth   | Notes                                  |
| ------ | --------------------------------- | ------ | -------------------------------------- |
| POST   | `/auth/login`                     | —      | OAuth2 password form → JWT             |
| GET    | `/auth/me`                        | user   | Current user                           |
| POST   | `/auth/users`                     | admin  | Provision a user                       |
| POST   | `/periods/{id}/unlock`            | admin  | Unlock locked month → `posted`; audits |
| GET    | `/portfolio/monthly?date_from&date_to`        | user | Portfolio P&L by month   |
| GET    | `/properties/{id}/monthly?date_from&date_to`  | user | Property P&L by month    |

Full aggregation endpoints (per-unit drill-down, etc.) and CRUD arrive in Phase 2.

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
