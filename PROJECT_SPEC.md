# Real Estate Portfolio Management — Project Spec

## Goal
A hosted web app to track **actual** monthly financial performance across a real estate
portfolio. View rent, expenses, NOI, and cash flow per month at three levels:
**unit → property → portfolio**. Multifamily properties drill down to individual units.

## Users
Me plus a few colleagues. Hosted, with authentication. Small team — not public.

## Out of scope for v1
- Underwriting / proforma projections (actual-vs-projected comparison). Schema should
  leave room to add an `underwriting` table later, but build none of it now.

---

## Financial definitions (IMPORTANT — get these exactly right)

Every cost is a **line item** with two attributes:
- `category` — free-ish label (e.g. "Property Tax", "Insurance", "Roof Replacement", "Mortgage")
- `classification` — one of: `operating`, `capex`, `debt_service`, `other_below_line`

Computations:
- **Gross Rent** = sum of rent for the scope/period
- **Operating Expenses** = sum of line items where `classification = operating`
- **NOI** = Gross Rent − Operating Expenses   *(capex is NOT in NOI by default)*
- **Below-NOI items** = capex + debt_service + other_below_line
- **Cash Flow** = NOI − (capex + debt_service + other_below_line)

### Reclassification toggle (required)
The user must be able to move a `category` (e.g. capex) above or below the NOI line
**without a schema change or data migration**. Implement via the `classification` field
and/or a display setting that controls which classifications count toward NOI. Changing it
recomputes NOI/cash flow from current classifications. Default: capex below NOI.

### Flexible / extensible categories (required)
- **No hardcoded expense fields.** A property's expenses are however many line items it
  has. Adding a new expense type (e.g. "HOA Fee") to one property and not another is just
  a new row — never a schema change.
- **Categories are data, not code.** Maintain a `categories` table (id, name,
  default_classification, active). Computations sum line items **by classification**, so a
  new operating category is picked up by NOI automatically and a new below-line category is
  picked up by cash flow automatically — zero code changes.
- **`classification` is the only thing that drives the math.** Any new item works as long
  as it has a classification.
- **Global categories with per-property opt-in** (recommended): one shared category list
  for consistent portfolio rollups (so "Insurance" means the same everywhere), but each
  property only uses the categories relevant to it; irrelevant ones simply never appear.
  Deleting/deactivating a category affects only future entry — historical line items keep
  their category and are never mutated.

---

## Hierarchy & rollup rules

- **Portfolio** → **Properties** → **Units** (units only for multifamily).
- A single-asset property has no units; its records attach directly to the property.
- Monthly records exist at unit level (multifamily) and/or property level.

### Rollup semantics
- **Rent** and **operating expenses**: additive. Units sum to property; properties sum to portfolio.
- **Property-level-only items** (e.g. shared-space capex, building debt service):
  recorded at the **property tier only**, NOT allocated down to units.
- Therefore: `property total ≠ sum of units` for capex / debt service / and thus NOI / cash flow.
  The UI must make this honest — show property-tier-only items as a distinct line, not silently
  blended into unit sums.
- Unit records carry only **unit-specific** rent, operating expenses, and capex.

---

## Data model (sketch — refine as needed)

- `properties` (id, name, type [multifamily | single | ...], address, created_at)
- `units` (id, property_id FK, unit_number, label, created_at)
- `categories` (id, name, default_classification ENUM, active BOOL) — global shared list
- `period_status` (id, property_id FK, month DATE, status ENUM [draft | posted | locked])
- `audit_log` (id, user_id, action, entity, entity_id, before, after, created_at) — used for
  unlock events and other sensitive changes; `locked` months are unlockable by an **admin only**,
  and every unlock is written here.
- `periods` — represent as a `month` DATE (first of month) on records; no separate table needed
- `monthly_records` (id, property_id FK, unit_id FK nullable, month DATE, notes)
  - unit_id NULL ⇒ a property-tier record (where shared/property-only items live)
- `line_items` (id, monthly_record_id FK, classification ENUM, category TEXT, amount NUMERIC)
  - rent can be a line item with classification `rent`, OR a dedicated `rent` column on
    `monthly_records` — pick one and be consistent. Recommend a `rent` line-item classification
    so everything flows through one table.
  - Suggested classification enum: `rent | operating | capex | debt_service | other_below_line`

NOI and cash flow are **computed in queries/views**, never stored, so they're always consistent.
Use Postgres aggregation (GROUP BY, or ROLLUP) for the tiered sums.

---

## Backend — endpoints

- `GET /portfolio/monthly?from=&to=` → portfolio P&L by month (rent, opex, NOI, below-line, cash flow)
- `GET /properties/:id/monthly?from=&to=` → property P&L by month, incl. property-tier-only items
- `GET /properties/:id/units/monthly?from=&to=` → per-unit monthly breakdown for a property
- `GET /units/:id/monthly?from=&to=` → single unit P&L by month
- CRUD for properties, units, monthly_records, line_items
- `POST /import` → CSV/Excel bulk import with column mapping + validation (see below)
- Auth on all endpoints

## Data entry
- **Manual**: forms for properties, units, and per-month line items.
- **Bulk import**: CSV/Excel upload with a column-mapping step and row validation
  (reject/flag bad rows, report errors, allow re-upload). Support importing many
  property/unit-months at once.

### Month-by-month management workflow (required — this is management software)
- Data is loaded one month at a time per property/unit. Each property-month has a
  **status**: `draft → posted → locked`. Locked months are protected from edits.
- **Idempotent import**: re-uploading the same month updates/replaces rather than
  duplicating. Key on `(property_id, unit_id, month, category)` so re-importing June
  cleanly overwrites June.
- **"What's missing" view**: for a given month, show which properties/units have no
  data posted yet, so nothing slips through.

### Import as the automation seam (future-proofing)
- The import endpoint must accept **structured, validated rows from any source**, not only
  hand-mapped CSV. Manual CSV mapping is just one producer of those rows.
- Future automation (upload a rent statement / rent roll → auto-update) becomes a parser
  that reads the document and emits the same validated rows into the same endpoint. The
  storage and computation path does not change — only the row-producer changes. Keep the
  import logic and parsing cleanly separated so a smarter parser can be dropped in later.

---

## Frontend — views

1. **Portfolio dashboard**: monthly table (rent / opex / NOI / below-line / cash flow) +
   trend charts for NOI and cash flow. Period selector (month range, YTD, T12).
2. **Property detail**: monthly P&L table; for multifamily, a unit drill-down
   (expandable rows or a units tab). Clearly separate property-tier-only items.
3. **Unit detail**: monthly P&L for one unit.
4. **Data entry / import** screens.
5. A clear **reclassification control** (e.g. manage categories, set each category's
   classification, see NOI recompute).

Period selector everywhere: month range, YTD, trailing-12 (T12).

---

## Stack (decided)
- **DB**: Postgres (Supabase = hosted Postgres + auth + row-level security; low infra for a small team)
- **Backend**: **FastAPI (Python)** — chosen over Node so the v2 rent-statement parsing
  (pandas + PDF/Excel extraction + eventual OCR/LLM parsing) lives in the ecosystem built for it.
  Pydantic gives typed validation + auto OpenAPI docs for the data-heavy endpoints.
- **Frontend**: React + Vite; AG Grid or TanStack Table for grids; Recharts for trends
- **Import parsing**: pandas (Python)
- **Categories**: global shared list (per-property opt-in)
- **Locked months**: unlockable by an admin only, with the change written to an audit log

## Build order (phases — review each before moving on)
1. Scaffold project + schema + migrations + auth
2. Aggregation endpoints (unit / property / portfolio monthly P&L) with the rollup rules above
3. Manual CRUD + entry forms
4. CSV/Excel import with mapping + validation
5. Portfolio dashboard (table + charts + period selector)
6. Property detail + unit drill-down
7. Unit detail + reclassification control

## Constraints
- NOI computed, never stored. Reclassification must not require migrations.
- Rollups must be honest about property-tier-only items (don't blend into unit sums).
- Keep room for a future `underwriting` table; build none of it in v1.
