"""Seed sample data at realistic scale, with a few HAND-COMPUTED anomalies planted as a
ground-truth answer key for the attention feed (INSIGHT_DASHBOARD_SPEC sub-step 3).

Run after migrations:  PYTHONPATH=.pydeps python3 -m app.seed

Scale: 15 multifamily (40-100 units) + 2 single-asset, 24 months (Jan 2024 - Dec 2025),
~1,000 units. Operating expenses (Repairs & Maintenance, Utilities) carry a small,
deterministic seasonal wiggle (±12%) so charts aren't ruler-flat — kept far below every
attention threshold so it trips nothing. Rent is flat (contractual) so vacancy / NOI math
stays exactly hand-computable.

Planted anomalies, all in **June 2025** (prior month May 2025), each sized to trip exactly
one detector and nothing else (see ANSWER KEY printed at the end):
  1. NOI cliff      — Oak Ridge Residences: June rent/unit 1650 -> 800 (concession).
                      Rent -52,700 => NOI drop ~ -52,235 (-58%). No expense/occupancy change.
  2. Expense spike  — Cedar Commons: +$30,000 property-tier "Repairs & Maintenance" in June.
                      Category 6,000 -> 36,000 vs ~6,568 T3M avg (+~448%). NOI dip ~29,437
                      stays below the NOI-drop $ floor, so it shows ONLY as an expense spike.
  3. Vacancy        — Maple Court Apartments unit 105 vacant from June (explicit
                      is_vacant record, $0 rent — NOT an omitted record, see migration 0011).
                      Surfaces as ONE `occupancy_drop` item: occupancy 45/45 -> 44/45 is a
                      2.2pp fall, clearing the 2.0pp `vacancy_min_occupancy_drop_pct` floor,
                      and the unit-grain vacancy (and unit 105's consequent NOI drop) fold
                      into it so the move-out is a single row.
                      Occupancy 45/45 -> 44/45; lost rent $1,500. NOI dip tiny (below floor).
  4. Missing data   — Ash Grove Apartments: no June 2025 records at all.

Re-running wipes domain data and reseeds, then rebuilds the summary layer.
"""

from __future__ import annotations

import itertools
import math
import uuid
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import delete, select

from app.accounts import DEFAULT_CATEGORIES, default_thresholds
from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    Account,
    AttentionSettings,
    AuditLog,
    Category,
    Lease,
    LineItem,
    MonthlyRecord,
    PeriodStatus,
    Property,
    Unit,
    User,
)
from app.security import hash_password
from app.summaries import rebuild_all

# 24 months: Jan 2024 .. Dec 2025
MONTHS = [date(2024 + i // 12, i % 12 + 1, 1) for i in range(24)]
ANCHOR_IDX = MONTHS.index(date(2025, 6, 1))  # planted-anomaly month (= 17)

# --- planted-anomaly knobs (the answer key) ---------------------------------
NOI_CLIFF_PROP = "Oak Ridge Residences"
NOI_CLIFF_JUNE_RENT = Decimal("800")  # normal 1650
SPIKE_PROP = "Cedar Commons"
SPIKE_CATEGORY = "Repairs & Maintenance"
SPIKE_AMOUNT = Decimal("30000")  # extra property-tier repair in June
VACANCY_PROP = "Maple Court Apartments"
VACANCY_UNIT = "105"
MISSING_PROP = "Ash Grove Apartments"

# --- lease-coherence fix (Option 2 — "don't fake it") -----------------------
# Root cause this section fixes: lease terms used to be seeded independently of the actual
# collected rent (a +/-3% wiggle off a REFERENCE rent, then a fabricated flat 3%/yr
# escalation on top) — by late in the fixed 2024-2025 actuals window that escalation had
# compounded past this seed's intentionally FLAT actual rent, so the rent waterfall's
# `bad_debt` line (contract/escalated-schedule minus actual) was almost entirely a seed-data
# artifact: phantom delinquency for tenants who were paying exactly what they always had.
# Every real (non-shell) unit's lease is now made coherent with its own actual collected
# rent instead (see `_coherent_contract_rent`): no fabricated escalation anywhere
# (`escalation_pct` stays NULL — the column/feature is untouched, an analyst can still enter
# a real one), and `contract_rent` tracks the unit's own actual rent, so a normally-paying
# tenant's scheduled rent equals what they actually pay and contributes zero to
# `loss_to_lease`'s scheduled-vs-actual leg or to `bad_debt`.
#
# A SMALL, EXPLICIT set of units is the deliberate exception: their `contract_rent` is
# seeded ABOVE their own actual rent by a real, stated percentage, so the waterfall's
# `bad_debt` line stays non-zero but is now a genuine, explainable shortfall for exactly
# these units — nothing else. Disjoint by construction from the concession-bearing leases
# (`_lease_concession`, a different, non-delinquent kind of shortfall) and from the Maple
# Court 105 vacancy (`VACANCY_PROP`/`VACANCY_UNIT`, a third, separate leakage cause) — each
# rent-waterfall leakage line traces back to exactly one root cause.
_DELINQUENT_UNITS: dict[tuple[str, str], Decimal] = {
    ("Maple Court Apartments", "101"): Decimal("0.08"),
    ("Oak Ridge Residences", "130"): Decimal("0.10"),
    ("Cedar Commons", "140"): Decimal("0.07"),
    ("Willow Creek Towers", "150"): Decimal("0.12"),
    ("Spruce Hill Apartments", "120"): Decimal("0.06"),
}

# name, address, unit count, flat contractual monthly rent per unit. Module-level (not
# just a main()-local) so scripts/seed_leases.py can use the same base-rent reference for
# any unit whose recent actual rent can't be read back (defensive fallback only — the live
# DB normally has real unit_month_summary rows to read instead).
MULTIFAMILY_CONFIGS = [
    ("Maple Court Apartments", "120 Maple Ct", 45, Decimal("1500")),
    ("Oak Ridge Residences", "505 Oak Ridge Dr", 62, Decimal("1650")),
    ("Pine Valley Tower", "777 Pine Valley Ln", 58, Decimal("1550")),
    ("Cedar Commons", "200 Cedar Ave", 75, Decimal("1700")),
    ("Elm Park Gardens", "333 Elm Park Rd", 48, Decimal("1450")),
    ("Spruce Hill Apartments", "899 Spruce Hill Way", 82, Decimal("1800")),
    ("Birch Lane Residential", "456 Birch Ln", 50, Decimal("1550")),
    ("Willow Creek Towers", "1010 Willow Creek Blvd", 95, Decimal("1900")),
    ("Ash Grove Apartments", "222 Ash Grove St", 40, Decimal("1400")),
    ("Hickory Heights", "678 Hickory Heights Ave", 70, Decimal("1750")),
    ("Sycamore Square Lofts", "999 Sycamore Sq", 55, Decimal("1600")),
    ("Magnolia Park Residences", "444 Magnolia Park Dr", 85, Decimal("1850")),
    ("Walnut Hill Towers", "567 Walnut Hill St", 72, Decimal("1775")),
    ("Chestnut Ridge Apartments", "191 Chestnut Ridge Rd", 65, Decimal("1700")),
    ("Dogwood Plaza Apartments", "812 Dogwood Plaza Way", 100, Decimal("1950")),
]

# --- lease seeding (rent roll / lease-level data) ---------------------------
# Shared by this module's fresh-install path (main(), called by the WIPING
# `python -m app.seed`) and scripts/seed_leases.py (an idempotent, non-wiping seeder
# for the current live DB — see that script for why the two paths exist). Kept here,
# not there, so both stay in lockstep with exactly one implementation.
#
# `i` is a stable running index across every unit that gets a lease (position in a
# deterministic property-name/unit-number ordering); it drives the tenant name, a small
# +/-3% contract-rent wiggle, and which "bucket" of lease-expiry staggering a unit lands
# in, so re-deriving the same unit always yields the same plan. `force_status` overrides
# the bucket for the small, deliberately sparse set of vacant/notice/expired units (see
# `_forced_status`) — everything else is a plain 'active' lease with a staggered
# `end_date` (or `None` for a month-to-month unit) so the expiration/rollover-risk
# endpoints have real near-term and far-term data to rank.
TENANT_FIRST = [
    "James", "Maria", "Robert", "Linda", "Michael", "Patricia", "David", "Jennifer",
    "William", "Elizabeth", "Carlos", "Sofia", "Daniel", "Ashley", "Kevin", "Nicole",
    "Brian", "Megan", "Steven", "Rachel", "Anthony", "Laura", "Jason", "Stephanie",
    "Eric", "Amanda", "Thomas", "Kimberly", "Mark", "Christina",
]
TENANT_LAST = [
    "Smith", "Johnson", "Williams", "Brown", "Garcia", "Miller", "Davis", "Rodriguez",
    "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas",
    "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson", "White",
    "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker",
]


def _tenant_name(i: int) -> str:
    return f"{TENANT_FIRST[i % len(TENANT_FIRST)]} {TENANT_LAST[(i * 7 + 3) % len(TENANT_LAST)]}"


def _round5(x: Decimal) -> Decimal:
    """Round to the nearest $5 so a derived rent figure reads like a real lease/market
    number rather than an exact-cents copy of another figure."""
    return (x / 5).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * 5


def _coherent_contract_rent(
    actual_ref: Decimal,
    property_name: str | None,
    unit_number: str | None,
    concession_monthly: Decimal | None,
) -> Decimal:
    """The lease's `contract_rent`, made coherent with the unit's own actual collected rent
    (lease-coherence fix, `_DELINQUENT_UNITS`'s docstring above). Replaces the old
    independent +/-3%-wiggle-off-a-reference-rent formula, which is what let a fabricated
    escalation drift the schedule away from the flat actuals in the first place. Priority,
    highest first:

      1. One of `_DELINQUENT_UNITS` -> a deliberate, stated markup above `actual_ref` — the
         ONLY units allowed to show real `bad_debt`.
      2. A concession-bearing lease (`concession_monthly` already decided by
         `_lease_concession`, keyed off `actual_ref`) -> `actual_ref + concession_monthly`,
         so the shortfall this creates is exactly the concession, not stray bad debt.
      3. Otherwise -> exactly `actual_ref`: scheduled rent equals actual rent, so this unit
         contributes zero to `loss_to_lease`'s scheduled-vs-market leg and zero `bad_debt`.
    """
    markup = _DELINQUENT_UNITS.get((property_name, unit_number))
    if markup is not None:
        return _round5(actual_ref * (Decimal("1") + markup))
    if concession_monthly is not None:
        return actual_ref + concession_monthly
    return actual_ref


def _forced_status(property_name: str, unit_number: str, i: int) -> str | None:
    """A small, deliberately sparse set of non-'active' leases (a "handful", not a
    portfolio-wide mix) — plus one explicit tie-in to the existing planted vacancy story:
    Maple Court unit 105 (vacant: explicit is_vacant/$0-rent records since June 2025 — see VACANCY_PROP)
    also carries a lease record whose last tenant moved out and was never replaced."""
    if property_name == VACANCY_PROP and unit_number == VACANCY_UNIT:
        return "vacant"
    if i % 190 == 55:
        return "vacant"
    if i % 140 == 30:
        return "notice"
    if i % 260 == 100:
        return "expired"
    return None


# --- lease-timeline coverage (rent-waterfall rework item 1 — root cause) ----------------
#
# `_lease_plan`'s dates used to be entirely `date.today()`-relative (`end_date - 365 days`,
# or `today - (180+i%400) days`), which drifts away from the FIXED 2024-01..2025-12 actuals
# window every single day this demo environment keeps running. By the time this rework
# landed, "today" had drifted to mid-2026 and only ~292/1002 real units still had a lease in
# force as of Dec 2025 (the actuals window's last month) — the rent-variance/rent-waterfall
# features were computing over a mostly lease-less window. `_lease_coverage_start` anchors
# every unit's earliest lease just BEFORE the fixed window instead (Oct-Dec 2023, varied per
# unit so escalation anniversaries don't all land on the same calendar month) so there's
# continuous lease-in-force coverage across the whole window, with an escalation anniversary
# actually landing inside it (Oct-Dec 2024, ~12 months in). `end_date` stays today-relative
# (unchanged) — this only backdates `start_date`, preserving the existing lease-expiration/
# rollover-risk spread. Deliberately kept CLOSE to MONTHS[0] (2024-01-01) rather than further
# back: pushing the anchor years earlier would compound more 3%/yr escalation cycles by Dec
# 2025 against this seed's intentionally FLAT `actual` rent series (see this module's top
# docstring — "Rent is flat... so vacancy/NOI math stays exactly hand-computable"), inflating
# `collections_loss` (scheduled - actual) into an unrealistically large, uniform portfolio-
# wide "shortfall" that's really just a seed-data artifact, not a demonstrable story.
LEASE_COVERAGE_START_BASE = date(2023, 10, 1)
LEASE_COVERAGE_START_SPREAD_DAYS = 90  # Oct 1 - Dec 29, 2023 — always before MONTHS[0]

# The last real actual month on file for Maple Court unit 105 before the planted vacancy
# anomaly begins (`ANCHOR_IDX` = June 2025) — the intentional in-window vacancy's lease
# should end here, not at a today-relative date that drifts past the actuals window.
VACANCY_LEASE_END = MONTHS[ANCHOR_IDX - 1]


def _lease_coverage_start(i: int) -> date:
    """Deterministic pre-window lease-history anchor — see the block comment above."""
    return LEASE_COVERAGE_START_BASE + timedelta(days=(i * 53) % LEASE_COVERAGE_START_SPREAD_DAYS)


# A deterministic, sparse set of tenancies that begin carrying arrears from before this
# dataset starts (migration 0025's `lease.opening_arrears`). ~1% of leases, by index, so the
# accumulated-balance column and the attention feed's arrears detector have a cause that is
# NOT a rent shortfall inside the window — proving the two inputs are independent. Set to one
# month's contract rent: a clean, hand-checkable figure.
#
# Deliberately NOT seeded as negative (a tenancy starting in credit): the column is signed and
# the engine handles it, but planting one would put a credit into the portfolio's net exposure
# for no demonstrative gain beyond what one write-off adjustment shows better.
def _opening_arrears(i: int, contract_rent: Decimal) -> Decimal | None:
    return contract_rent if i % 97 == 13 else None


# The last rent increase on a PERIODIC (rolling) tenancy — migration 0024. Only leases with
# NO end_date qualify, because that is what a periodic tenancy IS: an England-style AST that
# has run past its fixed term and now rolls month to month, its rent rising in discrete steps
# on a stated date rather than by a contractual percentage. ~5% of this dataset (`bucket == 6`
# in `_lease_plan`).
#
# `rent_before_increase` is deliberately left NULL, for exactly the reason `escalation_pct` is
# (see `_DELINQUENT_UNITS`'s docstring): this seed's actual rent series is intentionally FLAT,
# so planting a prior rent BELOW the current one would price the pre-increase months below
# what was actually collected and manufacture a portfolio-wide credit balance — a seed-data
# artifact, not a story. Recording the DATE is honest on its own ("the rent was last reviewed
# then"), it drives `months_since_last_increase` (the overdue-review signal this feature
# exists for), and an analyst entering a real prior figure on any lease gets exact pre-increase
# pricing immediately. The feature is proven against hand-built data in verify_phase15.py
# instead, where the actual series can be made to step with the rent.
def _last_rent_increase(i: int, start_date: date, end_date: date | None) -> date | None:
    if end_date is not None:
        return None
    # First anniversary of the tenancy: Oct-Dec 2024 given `_lease_coverage_start`'s Oct-Dec
    # 2023 anchor — inside the fixed 2024-2025 actuals window, so the date is one a reader can
    # see against real months rather than a figure floating outside the data.
    return date(start_date.year + 1, start_date.month, start_date.day)


# v2 fields (migration 0013): security deposit, escalation, MTM/fixed lease_type. All
# reference/terms data — none feed NOI/cash-flow. Shared by every branch below via
# `_v2_terms` so the "fixed-term leases get a modest bump, MTM leases don't" and "deposit
# is ~1-1.5x rent regardless of status" rules can't drift between branches.
#
# Migrations 0024/0025 add two more here for the same reason — one definition, every branch.
def _v2_terms(
    i: int, contract_rent: Decimal, end_date: date | None,
    concession_monthly: Decimal | None = None,
    start_date: date | None = None,
) -> dict:
    # Deterministic 1.0x-1.5x deposit (in 0.1 steps), rounded to the nearest dollar — a
    # deposit is a historical fact of the tenancy, so this applies regardless of status.
    deposit_factor = Decimal("1") + Decimal(i % 6) / Decimal("10")
    security_deposit = (contract_rent * deposit_factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    lease_type = "mtm" if end_date is None else "fixed"
    # Lease-coherence fix: NO fabricated escalation, on fixed-term OR month-to-month leases.
    # This used to be a flat 3%/yr bump on every fixed-term lease, seeded independently of
    # this seed's intentionally FLAT actual rent — by late in the fixed 2024-2025 actuals
    # window that schedule had compounded well past actual, manufacturing phantom `bad_debt`
    # in the rent waterfall for tenants who were paying exactly what they always had. The
    # column/feature stays: an analyst can still enter a real escalation on any lease; this
    # seed just stops planting a fake one that contradicts the data. See `_DELINQUENT_UNITS`
    # above for where this dataset's REAL, deliberate rent-waterfall shortfalls live instead.
    escalation_pct = None
    return {
        "security_deposit": security_deposit,
        "escalation_pct": escalation_pct,
        "escalation_frequency_months": 12,
        "lease_type": lease_type,
        "pct_rent_rate": None,
        "pct_rent_breakpoint": None,
        # Migration 0016 (waterfall-followups item 3): a standing concession on a handful
        # of leases — see `_lease_concession`'s docstring. None for the overwhelming
        # majority. Passed in by the caller (keyed off `actual_ref`, not `contract_rent` —
        # see `_coherent_contract_rent`) rather than recomputed here, so the SAME value is
        # used both to decide `contract_rent` and to store on the lease.
        "concession_monthly": concession_monthly,
        # Migration 0024: when the rent last went up on a rolling tenancy (fixed-term leases
        # get None). `start_date` is optional only so the retail lease below — which builds its
        # own terms dict — isn't forced to thread it through for a fixed-term lease that can
        # never qualify anyway.
        "last_rent_increase_date": (
            _last_rent_increase(i, start_date, end_date) if start_date is not None else None
        ),
        "rent_before_increase": None,  # never fabricated — see `_last_rent_increase`
        # Migration 0025: brought-forward arrears on a sparse set of tenancies.
        "opening_arrears": _opening_arrears(i, contract_rent),
    }


def _lease_plan(
    i: int, base_rent: Decimal, actual_rent: Decimal | None, *, force_status: str | None = None,
    property_name: str | None = None, unit_number: str | None = None,
) -> dict:
    """Build one lease row's fields (tenant/start/end/contract_rent/status/v2 terms) for
    unit index `i`. `actual_rent` (the unit's most recent recorded gross rent, if known) is
    preferred over `base_rent` as the contract-rent reference — reference data should
    track what a unit actually rents for, not a property-wide average.

    `contract_rent` is made coherent with that actual reference by `_coherent_contract_rent`
    (lease-coherence fix — see `_DELINQUENT_UNITS`'s docstring above): equal to it for the
    overwhelming majority of units (zero phantom shortfall), `actual + concession` for the
    handful of concession-bearing leases, or a deliberate stated markup above it for the
    small, explicit `_DELINQUENT_UNITS` set (genuine, explainable `bad_debt`).

    `start_date` is ALWAYS `_lease_coverage_start(i)` (pre-window anchor, see that
    function's docstring) regardless of branch, so every unit has continuous lease-in-force
    coverage across the whole 2024-2025 actuals window — `end_date` is what varies by
    branch/status. `property_name`/`unit_number` are consulted for two things: the Maple
    Court unit 105 planted-vacancy special case (see VACANCY_LEASE_END), and membership in
    `_DELINQUENT_UNITS`."""
    today = date.today()
    ref_rent = actual_rent if actual_rent is not None else base_rent
    concession_monthly = _lease_concession(i, ref_rent)
    contract_rent = _coherent_contract_rent(ref_rent, property_name, unit_number, concession_monthly)
    tenant = _tenant_name(i)
    start_date = _lease_coverage_start(i)

    if force_status == "vacant":
        if property_name == VACANCY_PROP and unit_number == VACANCY_UNIT:
            # The intentional in-window vacancy: lease ends the month before the planted
            # anomaly begins (no today-relative drift), and is never replaced.
            end_date = VACANCY_LEASE_END
        else:
            end_date = today - timedelta(days=45 + (i % 30))
        return {
            "tenant_name": tenant, "start_date": start_date,
            "end_date": end_date, "contract_rent": contract_rent, "status": "vacant",
            **_v2_terms(i, contract_rent, end_date, concession_monthly, start_date),
        }
    if force_status == "expired":
        end_date = today - timedelta(days=5 + (i % 20))
        return {
            "tenant_name": tenant, "start_date": start_date,
            "end_date": end_date, "contract_rent": contract_rent, "status": "expired",
            **_v2_terms(i, contract_rent, end_date, concession_monthly, start_date),
        }
    if force_status == "notice":
        end_date = today + timedelta(days=15 + (i % 30))
        return {
            "tenant_name": tenant, "start_date": start_date,
            "end_date": end_date, "contract_rent": contract_rent, "status": "notice",
            **_v2_terms(i, contract_rent, end_date, concession_monthly, start_date),
        }

    # Plain 'active' lease: staggered end_date so rollover-risk queries have a realistic
    # spread — roughly 5% within 30 days, 10% within 31-90, 15% within 91-180, 5%
    # month-to-month (no fixed term), the rest (~65%) renewed further out.
    bucket = i % 20
    if bucket == 0:
        # NB: the day offset must NOT reuse `i % 20` here — that's exactly the modulus
        # that put us in this branch (bucket == i % 20 == 0 for every qualifying i), so
        # `i % 20` is always 0 and every such unit collapsed onto the identical end_date
        # (seed-clustering bug, analyst report fix #7). `i % 19` is co-prime with 20, so
        # it varies independently of the bucket selector and actually staggers this group.
        end_date = today + timedelta(days=10 + (i % 19))
    elif bucket in (1, 2):
        end_date = today + timedelta(days=35 + (i % 55))
    elif bucket in (3, 4, 5):
        end_date = today + timedelta(days=95 + (i % 85))
    elif bucket == 6:
        end_date = None
    else:
        end_date = today + timedelta(days=200 + (i % 450))

    return {
        "tenant_name": tenant, "start_date": start_date,
        "end_date": end_date, "contract_rent": contract_rent, "status": "active",
        **_v2_terms(i, contract_rent, end_date, concession_monthly, start_date),
    }


def _market_rent(i: int, in_place_rent: Decimal) -> Decimal:
    """Deterministic 3%-9% premium over a unit's in-place rent (rounded to the nearest $5),
    varied by unit index — the rent waterfall's GPR line (migration 0015). A modest,
    realistic markup so loss-to-lease is demonstrable but not uniform across the portfolio.
    Bumped from the original 2%-8% range by the lease-coherence fix: now that
    `contract_rent` no longer carries a fabricated escalation (see `_DELINQUENT_UNITS`'s
    docstring above), `loss_to_lease` needs its own, honest premium over in-place rent to
    stay positive and material rather than collapsing toward zero. Shared with
    `scripts/backfill_unit_market_rent.py` so the live DB and a fresh install can't drift
    apart."""
    factor = Decimal("1.03") + Decimal(i % 7) / Decimal("100")
    wiggled = in_place_rent * factor
    return (wiggled / 5).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * 5


def _lease_concession(i: int, actual_ref: Decimal) -> Decimal | None:
    """Deterministic "handful" of leases with a standing monthly concession (waterfall-
    followups item 3 — migration 0016's ``lease.concession_monthly``, which the rent
    waterfall splits out of ``collections_loss`` into its own ``concessions`` line). Every
    40th lease by index (``i % 40 == 7``, ~2.5% of the portfolio) gets roughly a 3% rent
    concession, rounded to the nearest $5 — modest, but large enough to read clearly as a
    non-zero `concessions` bridge line rather than noise. Every other lease (the
    overwhelming majority) gets ``None`` — no concession, same "reference data, mostly
    absent" convention as `security_deposit`/`market_rent` before it.

    `actual_ref` (the unit's own actual collected rent, lease-coherence fix): this used to
    be keyed off `contract_rent`, but `contract_rent` for a concession-bearing lease is now
    ITSELF derived from this concession (`actual_ref + concession_monthly` — see
    `_coherent_contract_rent`), so the concession has to be computed from the
    actual-rent reference first to avoid a circular definition. Shared with
    `scripts/backfill_lease_concessions.py` so the live DB and a fresh install use the same
    rule (see that script's docstring for why a move-in "free months" model was rejected in
    favor of a standing $/month concession)."""
    if i % 40 != 7:
        return None
    raw = actual_ref * Decimal("0.03")
    return (raw / 5).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * 5


def _retail_lease_plan(monthly_rent: Decimal) -> dict:
    """Cedar Plaza Retail's single lease — the one lease in this dataset carrying
    percentage-rent terms (migration 0013). Cedar Plaza Retail is a "single" (unit-less)
    property; percentage rent is a per-*lease* concept, so this plants ONE shell unit
    ("RETAIL") under it purely to hang the lease off (see
    ``scripts/backfill_lease_v2_fields.py`` for the live-DB equivalent and its docstring for
    why). That shell unit has no monthly_records of its own — Cedar's actuals stay entirely
    property-tier (unit_id IS NULL), unaffected by this — so it will show up in the rent
    roll as an occupied unit with a "missing data" actual-rent gap, which is expected and
    harmless (contract rent / percentage-rent terms are reference data regardless).

    Terms: a 5-year fixed term (2 years in, 3 to go), a retail-scale deposit (1.5x monthly
    rent), a 3%/yr escalation, and standard percentage-rent terms — 6% overage rent on
    annual sales above a $900,000 breakpoint (roughly annual base rent / rate, a common
    retail-lease convention)."""
    today = date.today()
    start_date = today - timedelta(days=365 * 2)
    end_date = today + timedelta(days=365 * 3)
    return {
        "tenant_name": "Cedar Plaza Retail Tenant LLC",
        "start_date": start_date,
        "end_date": end_date,
        "contract_rent": monthly_rent,
        "status": "active",
        "security_deposit": (monthly_rent * Decimal("1.5")).quantize(Decimal("1"), rounding=ROUND_HALF_UP),
        "escalation_pct": Decimal("3.00"),
        "escalation_frequency_months": 12,
        "lease_type": "fixed",
        "pct_rent_rate": Decimal("6.00"),
        "pct_rent_breakpoint": Decimal("900000.00"),
        # No standing concession on Cedar's percentage-rent lease (bypasses `_v2_terms`,
        # migration 0016) — this shell-unit lease is excluded from the waterfall entirely
        # anyway (is_shell), so a concession here would never be visible/used.
        "concession_monthly": None,
    }

# Categories seasonality applies to (small, bounded, below all thresholds).
SEASONAL_CATS = {"Repairs & Maintenance", "Utilities"}

# Shared with signup (app/accounts.py) so a seeded demo account and a freshly registered
# one classify identically — the seed's planted anomalies depend on these exact names.
CATEGORIES = DEFAULT_CATEGORIES

CAT: dict[str, Category] = {}
# The account every seeded row belongs to. Set once in main(); the seed builds ONE
# account's portfolio, and re-running rebuilds that same account rather than piling up
# new tenants.
ACCOUNT_ID: str = ""


def _season(idx: int) -> Decimal:
    """Deterministic ±12% seasonal factor by calendar month (peak Mar, trough Sep)."""
    m = idx % 12 + 1
    return Decimal(str(round(1 + 0.12 * math.sin(2 * math.pi * m / 12), 6)))


def _q(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"))


def _wipe(db, account_id: str) -> None:
    # property_*_summary cascade from properties; portfolio_month_summary is rebuilt later.
    # SCOPED to the seed's own account (migration 0017): re-running the seed must not wipe
    # any other account that has signed up on this deployment.
    for model in (AuditLog, LineItem, MonthlyRecord, Category):
        db.execute(delete(model).where(model.account_id == account_id))
    # PeriodStatus/Unit have no account_id of their own — they hang off a property.
    prop_ids = select(Property.id).where(Property.account_id == account_id)
    db.execute(delete(PeriodStatus).where(PeriodStatus.property_id.in_(prop_ids)))
    db.execute(delete(Unit).where(Unit.property_id.in_(prop_ids)))
    db.execute(delete(Property).where(Property.account_id == account_id))
    db.commit()


def _upsert_account(db) -> Account:
    """The account the demo portfolio belongs to — reused across re-seeds (found via the
    seed admin's existing user row) so property ids stay stable for the verify_phase*
    scripts that hard-code them."""
    settings = get_settings()
    existing_admin = db.scalar(select(User).where(User.email == settings.seed_admin_email))
    if existing_admin is not None:
        account = db.get(Account, existing_admin.account_id)
        print(f"  reusing account: {account.name}")
        return account
    account = Account(name="Demo Portfolio")
    db.add(account)
    db.flush()
    db.add(default_thresholds(account.id))
    print("  created account: Demo Portfolio")
    return account


def _upsert_admin(db, account_id: str) -> User:
    settings = get_settings()
    admin = db.scalar(select(User).where(User.email == settings.seed_admin_email))
    if admin is None:
        admin = User(
            account_id=account_id,
            email=settings.seed_admin_email,
            hashed_password=hash_password(settings.seed_admin_password),
            role="admin",
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)
        print(f"  created admin user: {settings.seed_admin_email}")
    else:
        print(f"  admin user already exists: {settings.seed_admin_email}")
    return admin


def _add_record(db, *, property_id, unit_id, month, items, notes=None, is_vacant=False) -> None:
    """Create a monthly_record and its line items. ``items`` = {category_name: amount}.
    Ids generated client-side to avoid a flush per record.

    ``is_vacant`` (migration 0011) marks a POSTED record for a unit known to be empty —
    the honest way to express a real vacancy, as opposed to simply omitting the record,
    which now means "no data on file" instead."""
    record = MonthlyRecord(
        id=str(uuid.uuid4()), account_id=ACCOUNT_ID, property_id=property_id,
        unit_id=unit_id, month=month, notes=notes, is_vacant=is_vacant,
    )
    db.add(record)
    for name, amount in items.items():
        db.add(
            LineItem(
                id=str(uuid.uuid4()),
                account_id=ACCOUNT_ID,
                monthly_record_id=record.id,
                category_id=CAT[name].id,
                amount=Decimal(str(amount)),
            )
        )


def _create_multifamily_property(db, name, address, num_units, base_rent, lease_counter):
    prop = Property(account_id=ACCOUNT_ID, name=name, type="multifamily", address=address)
    db.add(prop)
    db.flush()
    units = []
    for i in range(num_units):
        unit_num = str(100 + i)
        u = Unit(property_id=prop.id, unit_number=unit_num, label=f"Unit {unit_num}")
        db.add(u)
        units.append(u)
    db.flush()

    # One lease per unit (rent roll / lease-level data). `actual_rent=None` here: this is
    # the fresh-install path, run before the summary layer exists, so there's no recorded
    # gross rent to read back yet — `base_rent` (this property's flat contractual rent,
    # which IS what's actually charged except at the two planted rent-affecting anomalies)
    # is the right reference. See scripts/seed_leases.py for the live-DB path, which reads
    # each unit's actual latest recorded rent instead.
    for u in units:
        lease_i = next(lease_counter)
        forced = _forced_status(name, u.unit_number, lease_i)
        lease_fields = _lease_plan(
            lease_i, base_rent, None, force_status=forced,
            property_name=name, unit_number=u.unit_number,
        )
        db.add(Lease(unit_id=u.id, **lease_fields))
        # market_rent (migration 0015): premium over this unit's in-place contract_rent —
        # see `_market_rent`'s docstring. Real (non-shell) units only; Cedar's shell unit
        # (below, a different code path) is deliberately left NULL — excluded from the
        # waterfall regardless.
        u.market_rent = _market_rent(lease_i, lease_fields["contract_rent"])

    for idx, month in enumerate(MONTHS):
        # Anomaly 4: this property simply has no records for the anchor month.
        if name == MISSING_PROP and idx == ANCHOR_IDX:
            continue

        f = _season(idx)
        for u in units:
            # Anomaly 3: unit goes vacant from the anchor month on.
            #
            # Posted as an EXPLICIT vacancy (a record with is_vacant=true and $0 rent),
            # not by omitting the record. Migration 0011 split those two cases apart: an
            # absent record now means "no data posted" (missing_data) and only an explicit
            # flag means "we know this unit is empty" (vacancy). This seed used to plant
            # the vacancy by omission, which after 0011 made the app report it as
            # missing_data — contradicting this file's own ANSWER KEY, and leaving the
            # vacancy detector with nothing to fire on. Property-level missing data is
            # still exercised, separately, by MISSING_PROP below.
            if name == VACANCY_PROP and u.unit_number == VACANCY_UNIT and idx >= ANCHOR_IDX:
                _add_record(
                    db,
                    property_id=prop.id,
                    unit_id=u.id,
                    month=month,
                    items={"Rent": Decimal("0")},
                    is_vacant=True,
                )
                continue
            # Anomaly 1: rent concession at the anchor month only.
            rent = (
                NOI_CLIFF_JUNE_RENT
                if (name == NOI_CLIFF_PROP and idx == ANCHOR_IDX)
                else base_rent
            )
            _add_record(
                db,
                property_id=prop.id,
                unit_id=u.id,
                month=month,
                items={
                    "Rent": rent,
                    "Repairs & Maintenance": _q(Decimal("80") * f),
                    "Utilities": _q(Decimal("45") * f),
                },
            )

        # property-tier-only items (flat; not seasonal)
        tier_items = {
            "Property Tax": Decimal(num_units) * Decimal("35"),
            "Insurance": Decimal(num_units) * Decimal("12"),
            "Property Management": Decimal(num_units) * Decimal("15"),
            "Mortgage": Decimal(num_units) * Decimal("55"),
        }
        if idx % 12 == 2:  # one-time capex each March (below NOI; trips no detector)
            tier_items["Roof Replacement"] = Decimal(num_units) * Decimal("200")
        # Anomaly 2: a single operating category spikes at the property tier in June.
        if name == SPIKE_PROP and idx == ANCHOR_IDX:
            tier_items[SPIKE_CATEGORY] = tier_items.get(SPIKE_CATEGORY, Decimal("0")) + SPIKE_AMOUNT
        _add_record(
            db,
            property_id=prop.id,
            unit_id=None,
            month=month,
            items=tier_items,
            notes="Property-tier shared items",
        )
    db.flush()
    return prop


def main() -> None:
    db = SessionLocal()
    try:
        print("Seeding sample data...")
        global ACCOUNT_ID
        account = _upsert_account(db)
        ACCOUNT_ID = account.id
        _wipe(db, ACCOUNT_ID)
        _upsert_admin(db, ACCOUNT_ID)

        for name, classification in CATEGORIES.items():
            cat = Category(
                account_id=ACCOUNT_ID, name=name, default_classification=classification
            )
            db.add(cat)
            CAT[name] = cat
        db.flush()

        multifamily_configs = MULTIFAMILY_CONFIGS
        lease_counter = itertools.count()
        properties = []
        for name, address, num_units, base_rent in multifamily_configs:
            properties.append(
                _create_multifamily_property(db, name, address, num_units, base_rent, lease_counter)
            )

        # --- single-asset properties (no units; no planted anomalies) ---
        birch = Property(
            account_id=ACCOUNT_ID, name="Birch Street House", type="single",
            address="45 Birch St",
        )
        db.add(birch)
        db.flush()
        for idx, month in enumerate(MONTHS):
            f = _season(idx)
            items = {
                "Rent": Decimal("2400"),
                "Property Tax": Decimal("350"),
                "Insurance": Decimal("110"),
                "Repairs & Maintenance": _q(Decimal("90") * f),
                "Mortgage": Decimal("1500"),
            }
            if idx % 12 == 6:  # capex in July
                items["Roof Replacement"] = Decimal("3000")
            _add_record(db, property_id=birch.id, unit_id=None, month=month, items=items)

        cedar = Property(
            account_id=ACCOUNT_ID, name="Cedar Plaza Retail", type="single",
            address="900 Cedar Ave",
        )
        db.add(cedar)
        db.flush()
        # Shell unit purely to hang the percentage-rent lease off (see _retail_lease_plan's
        # docstring) — Cedar's actual financials below stay entirely property-tier.
        # `is_shell=True` (migration 0014) excludes it from the rent roll's occupancy/GPR/
        # unit-count aggregates in app/queries.py, so it can't corrupt them.
        cedar_unit = Unit(property_id=cedar.id, unit_number="RETAIL", label="Retail Space", is_shell=True)
        db.add(cedar_unit)
        db.flush()
        db.add(Lease(unit_id=cedar_unit.id, **_retail_lease_plan(Decimal("5200"))))
        for idx, month in enumerate(MONTHS):
            items = {
                "Rent": Decimal("5200"),
                "Property Tax": Decimal("1100"),
                "Insurance": Decimal("400"),
                "Property Management": Decimal("520"),
                "Mortgage": Decimal("3100"),
                "Owner Distribution": Decimal("1000"),
            }
            if idx % 12 == 10:  # capex in November
                items["Roof Replacement"] = Decimal("5000")
            _add_record(db, property_id=cedar.id, unit_id=None, month=month, items=items)

        # --- period_status: post every (property, month) that has records ---
        for prop in properties:
            for idx, month in enumerate(MONTHS):
                if prop.name == MISSING_PROP and idx == ANCHOR_IDX:
                    continue
                db.add(PeriodStatus(property_id=prop.id, month=month, status="posted"))
        for prop in (birch, cedar):
            for month in MONTHS:
                db.add(PeriodStatus(property_id=prop.id, month=month, status="posted"))

        db.commit()

        print("Rebuilding summary layer from seeded line items...")
        rebuild_all(db)

        total_units = sum(c[2] for c in multifamily_configs)
        print(f"Done. 15 multifamily ({total_units} units) + 2 single-asset, "
              f"{len(MONTHS)} months (Jan 2024 - Dec 2025).")
        print("\n  ANSWER KEY — attention feed at 2025-06 (relative thresholds + linking):")
        print(f"    NOI drop      : {NOI_CLIFF_PROP} (~-$52,235, -58%; rent-driven)")
        print(f"    Expense spike : {SPIKE_PROP} / {SPIKE_CATEGORY} (~+$29,432, +448%) —")
        print(f"                    its ~-26% NOI drop reconciles (~1:1) and MERGES into this item")
        print(f"    Vacancy       : {VACANCY_PROP} unit {VACANCY_UNIT} — one 'occupancy_drop' item")
        print(f"                    (occupancy 45/45 -> 44/45 = -2.2pp, -$1,500 rent; unit")
        print(f"                     vacancy + its NOI drop folded in)")
        print(f"    Missing data  : {MISSING_PROP} (no June 2025 records)")
        print("    ...exactly 4 items, nothing else.")
        lease_count = db.execute(select(Lease.id)).all()
        print(f"\n  Leases seeded : {len(lease_count)} (1/unit; staggered end_dates + a handful of "
              f"vacant/notice/expired; {VACANCY_PROP} unit {VACANCY_UNIT} lease is 'vacant').")
    finally:
        db.close()


if __name__ == "__main__":
    main()
