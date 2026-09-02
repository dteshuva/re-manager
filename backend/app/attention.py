"""Attention-feed engine (INSIGHT_DASHBOARD_SPEC sub-step 3).

The portfolio dashboard's job at scale is *surfacing exceptions*, not displaying
everything. This computes a ranked list of "what needs attention this month?", entirely
from the pre-aggregated rollups (``property_month_summary`` / ``property_category_month_summary``)
— never from raw line items at request time — so it stays fast across thousands of units.

Six detectors (the ones computable from existing data):
  * noi_drop      — property NOI fell vs prior month past both $ and % floors.
  * expense_spike — a single operating category materially above its own trailing-3-month
                    average (both $ and % floors).
  * vacancy       — a unit-month record WAS POSTED this month (explicit ``is_vacant`` or $0
                    rent), for a unit that had rent last recorded. Unit-grain, rolled up per
                    property (see ``_all_unit_vacancies``/``_rollup_by_property``) — NEVER
                    inferred from an occupied-unit-count drop, which can't tell a genuine
                    vacancy from an absent record (see ``missing_data`` below; migration 0011).
  * occupancy_drop— property-grain, CHANGE-based vacancy: occupancy fell by at least
                    ``vacancy_min_occupancy_drop_pct`` PERCENTAGE POINTS vs the property's
                    most recent earlier month. This is the materiality filter on vacancy —
                    one unit is 2.5pp in a 40-unit property but 0.1pp in a 1,000-unit one —
                    and the unit vacancies it explains are folded into it so a move-out is
                    ONE row (``_fold_vacancies_into_occupancy_drops``). Distinct from
                    ``high_vacancy``, which is a LEVEL check (very empty right now, even if
                    unchanged); the two are deduped.
  * missing_data  — either a whole property with a prior-month rollup but none for the
                    selected month (``_missing_data``), or, within a property that DID post
                    data, individual units with no record at all this month
                    (``_all_unit_missing_data``) — distinct from ``vacancy`` by construction.

Every item carries a ``magnitude`` ($) and the whole feed is ranked by it, biggest first.
Thresholds come from settings (per-account in sub-step 6); pass ``thresholds`` to override.
"""

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.queries import _tag_where, latest_actual_month

# Currency symbol used by _money() when building the human-readable feed labels. Set per
# request (per account) at each public entry point below; defaults to "$" so any call that
# doesn't set it — or an account still on USD — reads as before. A ContextVar keeps this
# correct under concurrent requests without threading a symbol through ~15 detector helpers.
_currency_symbol: ContextVar[str] = ContextVar("attention_currency_symbol", default="$")
CURRENCY_SYMBOLS = {"USD": "$", "GBP": "£"}


def _set_currency_symbol(db: Session, account_id: str) -> None:
    code = db.execute(
        text("SELECT currency FROM accounts WHERE id = :id"), {"id": account_id}
    ).scalar()
    _currency_symbol.set(CURRENCY_SYMBOLS.get(code, "$"))


@dataclass
class Thresholds:
    noi_drop_min_abs: float
    noi_drop_min_pct: float
    expense_spike_min_abs: float
    expense_spike_min_pct: float
    unit_noi_drop_min_abs: float
    unit_noi_drop_min_pct: float
    unit_expense_spike_min_abs: float
    unit_expense_spike_min_pct: float
    vacancy_min_occupancy_drop_pct: float
    vacancy_high_absolute_pct: float
    # Root-cause linking ratio (global engine tunable, not per-account). See config.
    reconcile_ratio: float = 0.8

    # Per-account columns in attention_settings (reconcile_ratio is config-sourced, not here).
    _FIELDS = (
        "noi_drop_min_abs", "noi_drop_min_pct",
        "expense_spike_min_abs", "expense_spike_min_pct",
        "unit_noi_drop_min_abs", "unit_noi_drop_min_pct",
        "unit_expense_spike_min_abs", "unit_expense_spike_min_pct",
        "vacancy_min_occupancy_drop_pct",
        "vacancy_high_absolute_pct",
    )

    @classmethod
    def from_settings(cls) -> "Thresholds":
        s = get_settings()
        return cls(
            **{f: getattr(s, f"attention_{f}") for f in cls._FIELDS},
            reconcile_ratio=s.attention_noi_expense_reconcile_ratio,
        )

    @classmethod
    def from_db(cls, db: Session, account_id: str) -> "Thresholds":
        """This account's thresholds from attention_settings, falling back to config defaults.

        The fallback covers an account provisioned before ``app.accounts.provision_account``
        existed (or one whose row was deleted); a normal signup always writes the row.
        """
        row = db.execute(
            text(
                "SELECT " + ", ".join(cls._FIELDS)
                + " FROM attention_settings WHERE account_id = :account_id"
            ),
            {"account_id": account_id},
        ).mappings().first()
        if row is None:
            return cls.from_settings()
        return cls(
            **{f: float(row[f]) for f in cls._FIELDS},
            reconcile_ratio=get_settings().attention_noi_expense_reconcile_ratio,
        )


# Roll-up tuning for the property-scoped unit-grain feed (see _cluster_and_rollup). A
# building-wide event (e.g. one rent cut applied to every unit) produces N near-identical
# per-unit items in the same month; collapsing those into one line is what keeps the
# property feed from flooding with duplicates while still surfacing genuine per-unit
# outliers individually.
ROLLUP_MIN_COUNT = 3  # roll up only when a cluster has MORE than this many members
ROLLUP_MAG_REL_TOL = 0.10  # per-unit magnitude must be within 10% of the cluster's running mean
ROLLUP_MAG_ABS_FLOOR = 10.0  # ...or within $10, whichever is looser (guards tiny denominators)
ROLLUP_PCT_ABS_TOL = 4.0  # and pct_change within 4 percentage points of the cluster's mean


def _add_months(d: date, n: int) -> date:
    total = (d.year * 12 + d.month - 1) + n
    return date(total // 12, total % 12 + 1, 1)


def _money(v: float) -> str:
    return f"{_currency_symbol.get()}{abs(v):,.0f}"


def _noi_drops(
    db: Session, account_id: str, month: date, t: Thresholds, tags: list[str] | None = None
) -> list[dict]:
    # Baseline = the property's most recent EARLIER month that has a rollup (not necessarily
    # month-1), so a gap in the data doesn't blind the comparison. See _prior_row().
    rows = db.execute(
        text(
            f"""
            SELECT cur.property_id::text AS pid, p.name AS name,
                   cur.noi AS cur_noi, prev.noi AS prev_noi,
                   cur.operating_expenses AS cur_opex, prev.operating_expenses AS prev_opex
            FROM property_month_summary cur
            JOIN LATERAL (
                SELECT noi, operating_expenses FROM property_month_summary p2
                WHERE p2.property_id = cur.property_id AND p2.month < cur.month
                ORDER BY p2.month DESC LIMIT 1
            ) prev ON TRUE
            JOIN properties p ON p.id = cur.property_id
            WHERE cur.month = :month
              AND cur.account_id = :account_id
              AND prev.noi > 0
              AND (prev.noi - cur.noi) >= :min_abs
              AND ((prev.noi - cur.noi) / prev.noi * 100.0) >= :min_pct
              AND {_tag_where("cur.property_id")}
            """
        ),
        {
            "month": month,
            "account_id": account_id,
            "min_abs": t.noi_drop_min_abs,
            "min_pct": t.noi_drop_min_pct,
            "tags": tags,
        },
    ).mappings().all()
    items = []
    for r in rows:
        change = float(r["cur_noi"]) - float(r["prev_noi"])
        pct = change / abs(float(r["prev_noi"])) * 100.0
        # Prior-month opex increase — the same baseline as the NOI decline, for root-cause linking.
        opex_increase = float(r["cur_opex"]) - float(r["prev_opex"])
        items.append(
            {
                "type": "noi_drop",
                "property_id": r["pid"],
                "property_name": r["name"],
                "category": None,
                "magnitude": abs(change),
                "current": float(r["cur_noi"]),
                "prior": float(r["prev_noi"]),
                "change": change,
                "pct_change": pct,
                "detail": {"opex_increase": opex_increase},
                "label": f"NOI fell {_money(change)} ({pct:.0f}%) vs prior month",
            }
        )
    return items


def _expense_spikes(
    db: Session,
    account_id: str,
    month: date,
    t: Thresholds,
    property_id: str | None = None,
    tags: list[str] | None = None,
) -> list[dict]:
    t3_start = _add_months(month, -3)
    scope = "AND cur.property_id = :pid" if property_id else ""
    t3_scope = "AND property_id = :pid" if property_id else ""
    rows = db.execute(
        text(
            f"""
            WITH t3 AS (
                SELECT property_id, category_id, AVG(amount) AS avg3
                FROM property_category_month_summary
                WHERE classification = 'operating'
                  AND account_id = :account_id
                  AND month >= :t3_start AND month < :month {t3_scope}
                GROUP BY property_id, category_id
            )
            SELECT cur.property_id::text AS pid, p.name AS name, c.name AS cat,
                   cur.amount AS cur_amt, t3.avg3 AS avg3
            FROM property_category_month_summary cur
            JOIN t3 ON t3.property_id = cur.property_id AND t3.category_id = cur.category_id
            JOIN properties p ON p.id = cur.property_id
            JOIN categories c ON c.id = cur.category_id
            WHERE cur.month = :month AND cur.classification = 'operating'
              AND cur.account_id = :account_id
              AND t3.avg3 > 0
              AND (cur.amount - t3.avg3) >= :min_abs
              AND ((cur.amount - t3.avg3) / t3.avg3 * 100.0) >= :min_pct {scope}
              AND {_tag_where("cur.property_id")}
            """
        ),
        {
            "month": month, "t3_start": t3_start, "pid": property_id,
            "account_id": account_id,
            "min_abs": t.expense_spike_min_abs, "min_pct": t.expense_spike_min_pct, "tags": tags,
        },
    ).mappings().all()
    items = []
    for r in rows:
        cur_amt, avg3 = float(r["cur_amt"]), float(r["avg3"])
        change = cur_amt - avg3
        pct = change / avg3 * 100.0
        items.append(
            {
                "type": "expense_spike",
                "property_id": r["pid"],
                "property_name": r["name"],
                "category": r["cat"],
                "magnitude": change,
                "current": cur_amt,
                "prior": avg3,  # trailing-3-month average
                "change": change,
                "pct_change": pct,
                "detail": {"baseline": "trailing_3_month_avg"},
                "label": f"{r['cat']} up {_money(change)} ({pct:.0f}%) vs 3-mo avg",
            }
        )
    return items


def _all_unit_vacancies(
    db: Session, account_id: str, month: date, tags: list[str] | None = None
) -> list[dict]:
    """Portfolio-wide version of the property-scoped ``_unit_vacancies`` detector: every unit,
    across every property, that has a GENUINELY posted vacant record this month (explicit
    ``is_vacant`` or $0 rent), for a unit that had rent > 0 in its most recent EARLIER recorded
    month. Distinct from :func:`_all_unit_missing_data` (no record posted at all) — same
    migration-0011 split the property-scoped feed already applies, extended portfolio-wide so
    the portfolio feed can't conflate "vacant" with "no record posted" either (an absent
    record used to masquerade as a vacancy here via an occupied-unit-count drop that didn't
    distinguish the two cases).
    """
    rows = db.execute(
        text(
            f"""
            SELECT u.property_id::text AS pid, p.name AS pname,
                   u.id::text AS uid, u.unit_number AS num, prev.prev_rent AS prev_rent
            FROM units u
            JOIN LATERAL (
                SELECT gross_rent AS prev_rent FROM unit_month_summary p2
                WHERE p2.unit_id = u.id AND p2.month < :month
                ORDER BY p2.month DESC LIMIT 1
            ) prev ON TRUE
            JOIN unit_month_summary cur ON cur.unit_id = u.id AND cur.month = :month
            JOIN properties p ON p.id = u.property_id
            WHERE prev.prev_rent > 0 AND (cur.is_vacant OR cur.gross_rent = 0)
              AND p.account_id = :account_id
              AND {_tag_where("u.property_id")}
            """
        ),
        {"month": month, "account_id": account_id, "tags": tags},
    ).mappings().all()
    out = []
    for r in rows:
        lost = float(r["prev_rent"])
        out.append({
            "type": "vacancy", "property_id": r["pid"], "property_name": r["pname"],
            "unit_id": r["uid"], "unit_number": r["num"], "category": None,
            "magnitude": lost, "current": 0.0, "prior": lost, "change": -lost,
            "pct_change": None, "detail": {"units_lost": 1},
            "label": f"Unit {r['num']} went vacant, lost rent {_money(lost)}",
        })
    return out


def _all_unit_missing_data(
    db: Session, account_id: str, month: date, tags: list[str] | None = None
) -> list[dict]:
    """Portfolio-wide version of the property-scoped ``_unit_missing_data`` detector: every
    unit, across every property, with NO ``monthly_records`` row at all this month, for a unit
    that had rent > 0 in its most recent EARLIER recorded month. Requires the PROPERTY to still
    have a rollup for this month (some other unit posted) — a property with zero records posted
    at all this month is a separate, coarser event already covered by :func:`_missing_data`
    below, so this detector doesn't double-fire the same blackout at both grains.
    """
    rows = db.execute(
        text(
            f"""
            SELECT u.property_id::text AS pid, p.name AS pname,
                   u.id::text AS uid, u.unit_number AS num, prev.prev_rent AS prev_rent
            FROM units u
            JOIN LATERAL (
                SELECT gross_rent AS prev_rent FROM unit_month_summary p2
                WHERE p2.unit_id = u.id AND p2.month < :month
                ORDER BY p2.month DESC LIMIT 1
            ) prev ON TRUE
            LEFT JOIN unit_month_summary cur ON cur.unit_id = u.id AND cur.month = :month
            JOIN properties p ON p.id = u.property_id
            WHERE prev.prev_rent > 0 AND cur.unit_id IS NULL
              AND p.account_id = :account_id
              AND EXISTS (
                  SELECT 1 FROM property_month_summary pms
                  WHERE pms.property_id = u.property_id AND pms.month = :month
              )
              AND {_tag_where("u.property_id")}
            """
        ),
        {"month": month, "account_id": account_id, "tags": tags},
    ).mappings().all()
    out = []
    for r in rows:
        at_risk = float(r["prev_rent"])
        out.append({
            "type": "missing_data", "property_id": r["pid"], "property_name": r["pname"],
            "unit_id": r["uid"], "unit_number": r["num"], "category": None,
            "magnitude": at_risk, "current": None, "prior": at_risk, "change": None,
            "pct_change": None, "detail": {"at_risk_basis": "prior_month_rent"},
            "label": f"Unit {r['num']} has no posted record this month",
        })
    return out


def _rollup_by_property(entries: list[dict], kind: str) -> list[dict]:
    """Group unit-level entries (already scoped to one month) by property, then run
    :func:`_cluster_and_rollup` within each property's group. Used to apply the exact same
    per-property clustering the property-scoped feed uses to a portfolio-wide, multi-property
    batch of unit-level items."""
    groups: dict[str, list[dict]] = {}
    for it in entries:
        groups.setdefault(it["property_id"], []).append(it)
    out: list[dict] = []
    for group in groups.values():
        out.extend(_cluster_and_rollup(group, kind))
    return out


def _occupancy_drops(
    db: Session, account_id: str, month: date, t: Thresholds, tags: list[str] | None = None
) -> list[dict]:
    """CHANGE-based, property-grain vacancy: flag a property whose occupancy fell by at
    least ``vacancy_min_occupancy_drop_pct`` percentage points versus its most recent
    EARLIER summarized month.

    This is the "did vacancy get materially worse here?" signal, and the only consumer of
    ``vacancy_min_occupancy_drop_pct``. It is deliberately measured in PERCENTAGE POINTS
    rather than units lost, because that is what makes the alert size-independent: one unit
    is a 2.5pp swing in a 40-unit property (worth a look) but 0.1pp in a 1,000-unit one
    (noise). The unit-grain ``_all_unit_vacancies`` detector has no threshold — it fires for
    every vacated unit regardless of scale — so without this there was no materiality filter
    on vacancy at all, and no property-level statement of how much occupancy moved.

    Distinct from :func:`_high_vacancies`, which is a LEVEL check (this property is very
    empty *right now*, even if it didn't get worse this month). The two are deduped in
    :func:`attention_feed` so one property-month never emits both.

    Baseline is the latest earlier month WITH a summary row rather than strictly month-1, so
    a gap in the data doesn't blind the comparison — same rule as :func:`_noi_drops`. Only
    months where BOTH sides have a real unit basis (``total_units > 0`` and a non-NULL
    occupancy) can be compared; a property with no units of its own — a single-asset
    property booking rent at the property tier — has no occupancy to speak of and is
    skipped rather than reported as 0%.

    ``magnitude`` is the estimated monthly rent now at stake (prior rent per occupied unit x
    units lost), keeping it on the same $ scale every other item is ranked by.
    """
    rows = db.execute(
        text(
            f"""
            SELECT cur.property_id::text AS pid, p.name AS name,
                   cur.occupancy AS cur_occ, prev.occupancy AS prev_occ,
                   cur.occupied_units AS cur_occupied, prev.occupied_units AS prev_occupied,
                   cur.total_units AS cur_total, prev.gross_rent AS prev_rent
            FROM property_month_summary cur
            JOIN LATERAL (
                SELECT occupancy, occupied_units, gross_rent
                FROM property_month_summary p2
                WHERE p2.property_id = cur.property_id
                  AND p2.month < cur.month
                  AND p2.total_units > 0
                  AND p2.occupancy IS NOT NULL
                ORDER BY p2.month DESC LIMIT 1
            ) prev ON TRUE
            JOIN properties p ON p.id = cur.property_id
            WHERE cur.month = :month
              AND cur.account_id = :account_id
              AND cur.total_units > 0
              AND cur.occupancy IS NOT NULL
              AND ((prev.occupancy - cur.occupancy) * 100.0) >= :min_pp
              AND {_tag_where("cur.property_id")}
            """
        ),
        {
            "month": month,
            "account_id": account_id,
            "min_pp": t.vacancy_min_occupancy_drop_pct,
            "tags": tags,
        },
    ).mappings().all()

    items = []
    for r in rows:
        cur_occ, prev_occ = float(r["cur_occ"]), float(r["prev_occ"])
        pp_drop = (prev_occ - cur_occ) * 100.0
        units_lost = int(r["prev_occupied"]) - int(r["cur_occupied"])
        prev_occupied = int(r["prev_occupied"])
        # Rent at stake ~ what each occupied unit was bringing in, times the units lost.
        rent_at_stake = (
            (float(r["prev_rent"]) / prev_occupied) * units_lost if prev_occupied > 0 and units_lost > 0 else 0.0
        )
        items.append(
            {
                "type": "occupancy_drop",
                "property_id": r["pid"],
                "property_name": r["name"],
                "category": None,
                "magnitude": rent_at_stake,
                "current": cur_occ * 100.0,
                "prior": prev_occ * 100.0,
                "change": -pp_drop,
                "pct_change": None,  # the change IS in percentage points; see `change`
                "detail": {
                    "occupancy_pp_drop": pp_drop,
                    "occupied_before": prev_occupied,
                    "occupied_after": int(r["cur_occupied"]),
                    "total_units": int(r["cur_total"]),
                    "units_lost": units_lost,
                },
                "label": (
                    f"Occupancy fell {pp_drop:.1f}pp "
                    f"({prev_occupied}/{int(r['cur_total'])} → {int(r['cur_occupied'])}/{int(r['cur_total'])} occupied)"
                ),
            }
        )
    return items


def _fold_vacancies_into_occupancy_drops(
    occ_drops: list[dict], unit_vacancies: list[dict]
) -> list[dict]:
    """Merge unit-grain vacancy items INTO the property-level occupancy drop that explains
    them, so one move-out event is one row rather than two.

    Same principle as :func:`_link_noi_to_expense`: when two detectors are describing the
    same underlying event at different grains, keep the one that carries the most context
    (here the property-level drop, which states how much occupancy actually moved) and fold
    the other's specifics into it — the vacated unit numbers, and the exact rent lost.

    Vacancies at properties with NO occupancy-drop item are returned untouched: either the
    drop was below the materiality floor, or the property has no occupancy basis to measure
    (single-asset). Nothing is silently dropped.
    """
    by_property: dict[str, dict] = {d["property_id"]: d for d in occ_drops}
    unfolded: list[dict] = []
    for vac in unit_vacancies:
        drop = by_property.get(vac["property_id"])
        if drop is None:
            unfolded.append(vac)
            continue
        folded = drop["detail"].setdefault("vacated_units", [])
        if vac.get("unit_number"):
            folded.append(vac["unit_number"])
        elif vac.get("detail", {}).get("units"):  # an already-rolled-up cluster
            folded.extend(vac["detail"]["units"])
        # Prefer the measured rent lost over the estimate from average rent/unit.
        drop["detail"]["rent_lost"] = drop["detail"].get("rent_lost", 0.0) + vac["magnitude"]

    for drop in occ_drops:
        units = drop["detail"].get("vacated_units")
        if units:
            shown = ", ".join(sorted(units, key=lambda u: (len(u), u))[:8])
            more = f" +{len(units) - 8} more" if len(units) > 8 else ""
            drop["label"] += f" — unit(s) {shown}{more} vacated"
            drop["magnitude"] = drop["detail"]["rent_lost"]
    return occ_drops + unfolded


def _high_vacancies(
    db: Session, account_id: str, month: date, t: Thresholds, tags: list[str] | None = None
) -> list[dict]:
    """Absolute (level, not change) vacancy: flag any property sitting at or above the
    configured vacancy rate this month, EVEN IF it didn't get worse — a property that is
    persistently very empty is a standing problem the change-based detector would miss.
    Magnitude is the estimated at-market rent lost to the empty units (ranking proxy).
    """
    rows = db.execute(
        text(
            f"""
            SELECT cur.property_id::text AS pid, p.name AS name,
                   cur.occupied_units AS cu, cur.total_units AS tu,
                   cur.occupancy AS occ, cur.gross_rent AS rent
            FROM property_month_summary cur
            JOIN properties p ON p.id = cur.property_id
            WHERE cur.month = :month
              AND cur.account_id = :account_id
              AND cur.total_units > 0
              AND cur.occupancy IS NOT NULL
              AND (1.0 - cur.occupancy) * 100.0 >= :min_vac_pct
              AND {_tag_where("cur.property_id")}
            """
        ),
        {
            "month": month,
            "account_id": account_id,
            "min_vac_pct": t.vacancy_high_absolute_pct,
            "tags": tags,
        },
    ).mappings().all()
    items = []
    for r in rows:
        occupied, total = int(r["cu"]), int(r["tu"])
        vacant = total - occupied
        rent = float(r["rent"])
        vac_pct = (1.0 - float(r["occ"])) * 100.0
        # At-market rent of the empty units ≈ current rent per occupied unit × vacant units.
        rent_at_stake = (rent / occupied) * vacant if occupied > 0 else 0.0
        items.append(
            {
                "type": "high_vacancy",
                "property_id": r["pid"],
                "property_name": r["name"],
                "category": None,
                "magnitude": rent_at_stake,
                "current": float(occupied),
                "prior": None,
                "change": None,
                "pct_change": None,
                "detail": {
                    "vacant_units": vacant, "total_units": total,
                    "occupancy": float(r["occ"]) * 100.0, "vacancy_rate": vac_pct,
                },
                "label": f"High vacancy: {vac_pct:.0f}% vacant ({occupied}/{total} occupied)",
            }
        )
    return items


def _missing_data(
    db: Session, account_id: str, month: date, prior: date, tags: list[str] | None = None
) -> list[dict]:
    rows = db.execute(
        text(
            f"""
            SELECT p.id::text AS pid, p.name AS name, prev.noi AS prev_noi
            FROM properties p
            JOIN property_month_summary prev
              ON prev.property_id = p.id AND prev.month = :prior
            LEFT JOIN property_month_summary cur
              ON cur.property_id = p.id AND cur.month = :month
            WHERE cur.property_id IS NULL
              AND p.account_id = :account_id
              AND {_tag_where("p.id")}
            """
        ),
        {"month": month, "prior": prior, "account_id": account_id, "tags": tags},
    ).mappings().all()
    items = []
    for r in rows:
        at_risk = float(r["prev_noi"])
        items.append(
            {
                "type": "missing_data",
                "property_id": r["pid"],
                "property_name": r["name"],
                "category": None,
                "magnitude": abs(at_risk),  # prior NOI = what's at stake (ranking proxy)
                "current": None,
                "prior": at_risk,
                "change": None,
                "pct_change": None,
                "detail": {"at_risk_basis": "prior_month_noi"},
                "label": "No posted records for this month",
            }
        )
    return items


def _distinct_months(db, table, where, params, date_from, date_to):
    return db.execute(
        text(f"SELECT DISTINCT month FROM {table} WHERE {where} AND month BETWEEN :f AND :t "
             "ORDER BY month"),
        {**params, "f": date_from, "t": date_to},
    ).scalars().all()


def attention_feed(
    db: Session,
    account_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
    *,
    thresholds: Thresholds | None = None,
    limit: int = 50,
    tags: list[str] | None = None,
) -> dict:
    """Ranked exception feed for the selected period, computed from the rollups.

    Runs the month-over-month detectors for EVERY month in the period (not just the last),
    so an anomaly in a middle month of a YTD/T12 range still surfaces — each item is tagged
    with the month it occurred in. No params → latest single month. ``tags`` optionally
    scopes every detector to properties carrying ANY of the given tags (OR semantics).
    """
    t = thresholds or Thresholds.from_db(db, account_id)
    _set_currency_symbol(db, account_id)
    if date_to is None:
        # Non-future: a month scheduled ahead (a shared expense posted forward) holds costs
        # with no matching income, so anchoring the feed there would report every property as
        # an NOI collapse the moment a recurring cost is scheduled. See `latest_actual_month`.
        date_to = latest_actual_month(
            db, "portfolio_month_summary", "account_id = :account_id",
            {"account_id": account_id},
        )
    if date_to is None:
        return {"period_from": None, "period_to": None, "items": [], "thresholds": vars(t)}
    if date_from is None:
        date_from = date_to

    items = []
    for month in _distinct_months(
        db,
        "portfolio_month_summary",
        "account_id = :account_id",
        {"account_id": account_id},
        date_from,
        date_to,
    ):
        prior = _add_months(month, -1)
        # Unit-grain, portfolio-wide: a genuinely-vacant unit (posted, is_vacant/$0 rent)
        # drives "vacancy"; an absent record drives "missing_data" — never the reverse (see
        # _all_unit_vacancies/_all_unit_missing_data docstrings). Rolled up per-property with
        # the same clustering the property-scoped feed uses, so a building-wide event collapses
        # into one item instead of flooding the feed with near-duplicates.
        vac = _rollup_by_property(
            _all_unit_vacancies(db, account_id, month, tags=tags), "vacancy"
        )
        # Property-level "vacancy got materially worse" (the only user of
        # vacancy_min_occupancy_drop_pct). Where it fires, the individual unit vacancies it
        # explains are folded into it, so a move-out is one row carrying the occupancy
        # movement AND the unit numbers — not a per-unit row plus a property row.
        occ_drops = _occupancy_drops(db, account_id, month, t, tags=tags)
        vac = _fold_vacancies_into_occupancy_drops(occ_drops, vac)
        # (unit NOI drops aren't part of the portfolio feed — see worst_units — so the
        # vacancy<->NOI link below is a no-op here; kept so both feeds order identically.)

        unit_missing = _rollup_by_property(
            _all_unit_missing_data(db, account_id, month, tags=tags), "missing_data"
        )
        # Absolute high-vacancy is a safety net: only surface it where the change-based
        # vacancy detectors didn't already flag the same property (avoid a double item).
        flagged = {v["property_id"] for v in vac}
        high_vac = [
            h
            for h in _high_vacancies(db, account_id, month, t, tags=tags)
            if h["property_id"] not in flagged
        ]
        batch = (
            _noi_drops(db, account_id, month, t, tags=tags)
            + _expense_spikes(db, account_id, month, t, tags=tags)
            + vac
            + high_vac
            + unit_missing
            + _missing_data(db, account_id, month, prior, tags=tags)
        )
        for it in batch:
            it["month"] = month
        items += batch
    items = _link_vacancy_to_noi_drop(items)
    items = _link_noi_to_expense(items, t.reconcile_ratio)
    items.sort(key=lambda it: it["magnitude"], reverse=True)
    return {
        "period_from": date_from,
        "period_to": date_to,
        "items": items[:limit],
        "thresholds": vars(t),
    }


def _link_vacancy_to_noi_drop(items: list[dict]) -> list[dict]:
    """Root-cause linking at UNIT grain: a unit that went vacant and that unit's NOI drop are
    the same event, so report it once.

    When a unit empties, its rent goes to $0 — which the unit NOI-drop detector independently
    (and correctly) sees as a ~100% NOI decline. Both statements are true, but showing both
    double-counts one move-out in a ranked feed, and the two magnitudes differ (lost RENT vs
    lost NOI, the gap being the unit's operating expenses), so they don't even read as the
    same event.

    The vacancy is kept as the cause; the NOI figure is folded into its ``detail`` and label,
    mirroring how :func:`_link_noi_to_expense` merges a property's NOI drop into the expense
    spike that explains it. No magnitude reconciliation test is needed here (unlike the
    expense case, where near-equality is the evidence they're the same event): a vacancy and
    an NOI drop on the SAME unit in the SAME month are the same event by construction.
    """
    vacant_units = {
        (it["property_id"], it.get("unit_id"), it.get("month"))
        for it in items
        if it["type"] == "vacancy" and it.get("unit_id")
    }
    if not vacant_units:
        return items

    kept: list[dict] = []
    noi_by_unit: dict[tuple, dict] = {}
    for it in items:
        key = (it["property_id"], it.get("unit_id"), it.get("month"))
        if it["type"] == "noi_drop" and it.get("unit_id") and key in vacant_units:
            noi_by_unit[key] = it
            continue  # merged: suppress the NOI-drop item
        kept.append(it)

    for it in kept:
        if it["type"] != "vacancy" or not it.get("unit_id"):
            continue
        noi = noi_by_unit.get((it["property_id"], it.get("unit_id"), it.get("month")))
        if noi is not None:
            it["detail"]["noi_lost"] = noi["magnitude"]
            it["label"] += f" (NOI -{_money(noi['magnitude'])})"
    return kept


def _link_noi_to_expense(items: list[dict], ratio: float) -> list[dict]:
    """Root-cause linking: a property-month's NOI drop and expense spike are the SAME event
    when the (prior-month) operating-expense increase accounts for most of the NOI decline —
    the magnitudes reconcile. In that case drop the NOI-drop item and note the effect on the
    spike (the cause). When the magnitudes don't line up (e.g. a rent-driven NOI drop), keep
    both, since the spike doesn't explain the drop.
    """
    spikes_by_pm: dict[tuple, list[dict]] = {}
    for it in items:
        if it["type"] == "expense_spike":
            spikes_by_pm.setdefault((it["property_id"], it["month"]), []).append(it)

    kept = []
    for it in items:
        if it["type"] == "noi_drop":
            group = spikes_by_pm.get((it["property_id"], it["month"]))
            opex_increase = it["detail"].get("opex_increase", 0.0)
            if group and it["magnitude"] > 0 and opex_increase >= it["magnitude"] * ratio:
                top = max(group, key=lambda s: s["magnitude"])  # attribute to the biggest spike
                top["detail"]["drove_noi_down"] = it["magnitude"]
                top["detail"]["noi_pct_change"] = it["pct_change"]
                top["label"] += f" — drove NOI down {_money(it['magnitude'])} ({it['pct_change']:.0f}%)"
                continue  # merged: suppress the NOI-drop item
        kept.append(it)
    return kept


# ============================ property-scoped (unit-grain) feed ==============

def _unit_noi_drops(
    db: Session, account_id: str, pid: str, name: str, month: date, t: Thresholds
):
    # Baseline = the unit's most recent EARLIER month with a row (gap-tolerant).
    rows = db.execute(
        text(
            """
            SELECT u.id::text AS uid, u.unit_number AS num,
                   cur.noi AS cur_noi, prev.noi AS prev_noi
            FROM unit_month_summary cur
            JOIN LATERAL (
                SELECT noi FROM unit_month_summary p2
                WHERE p2.unit_id = cur.unit_id AND p2.month < cur.month
                ORDER BY p2.month DESC LIMIT 1
            ) prev ON TRUE
            JOIN units u ON u.id = cur.unit_id
            WHERE cur.property_id = :pid AND cur.month = :month
              AND cur.account_id = :account_id
              AND prev.noi > 0
              AND (prev.noi - cur.noi) >= :min_abs
              AND ((prev.noi - cur.noi) / prev.noi * 100.0) >= :min_pct
            """
        ),
        {"pid": pid, "month": month, "account_id": account_id,
         "min_abs": t.unit_noi_drop_min_abs, "min_pct": t.unit_noi_drop_min_pct},
    ).mappings().all()
    out = []
    for r in rows:
        change = float(r["cur_noi"]) - float(r["prev_noi"])
        pct = change / abs(float(r["prev_noi"])) * 100.0
        out.append({
            "type": "noi_drop", "property_id": pid, "property_name": name,
            "unit_id": r["uid"], "unit_number": r["num"], "category": None,
            "magnitude": abs(change), "current": float(r["cur_noi"]), "prior": float(r["prev_noi"]),
            "change": change, "pct_change": pct, "detail": {},
            "label": f"Unit {r['num']} NOI fell {_money(change)} ({pct:.0f}%) vs prior month",
        })
    return out


def _all_unit_noi_drops(
    db: Session, account_id: str, month: date, tags: list[str] | None = None
) -> list[dict]:
    """Every unit, portfolio-wide, whose NOI dropped this month vs its most recent EARLIER
    recorded month — no threshold floor (this powers a ranked "worst N" leaderboard, not an
    alert feed, so a small drop simply ranks low rather than being filtered out)."""
    rows = db.execute(
        text(
            f"""
            SELECT cur.property_id::text AS pid, p.name AS pname,
                   u.id::text AS uid, u.unit_number AS num,
                   cur.noi AS cur_noi, prev.noi AS prev_noi
            FROM unit_month_summary cur
            JOIN LATERAL (
                SELECT noi FROM unit_month_summary p2
                WHERE p2.unit_id = cur.unit_id AND p2.month < cur.month
                ORDER BY p2.month DESC LIMIT 1
            ) prev ON TRUE
            JOIN units u ON u.id = cur.unit_id
            JOIN properties p ON p.id = cur.property_id
            WHERE cur.month = :month
              AND cur.account_id = :account_id
              AND prev.noi > 0
              AND cur.noi < prev.noi
              AND {_tag_where("cur.property_id")}
            """
        ),
        {"month": month, "account_id": account_id, "tags": tags},
    ).mappings().all()
    out = []
    for r in rows:
        change = float(r["cur_noi"]) - float(r["prev_noi"])
        pct = change / abs(float(r["prev_noi"])) * 100.0
        out.append(
            {
                "property_id": r["pid"],
                "property_name": r["pname"],
                "unit_id": r["uid"],
                "unit_number": r["num"],
                "month": month,
                "current": float(r["cur_noi"]),
                "prior": float(r["prev_noi"]),
                "change": change,
                "pct_change": pct,
                "magnitude": abs(change),
                "label": f"Unit {r['num']} ({r['pname']}) NOI fell {_money(change)} ({pct:.0f}%) vs prior month",
            }
        )
    return out


def worst_units(
    db: Session,
    account_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
    *,
    limit: int = 10,
    tags: list[str] | None = None,
) -> dict:
    """Portfolio-wide "worst units" leaderboard: the top-N units (across EVERY property, not
    just one) with the biggest NOI drop in the selected period, ranked by $ magnitude.

    Complements the existing attention feeds: portfolio attention surfaces the worst
    *properties*, and a property's own attention feed surfaces the worst *units within that
    one property* — but until now there was no way to ask "which units, across the whole
    portfolio, dropped the most?" in a single call. No params → latest single month. ``tags``
    optionally scopes to properties carrying ANY of the given tags (OR semantics).

    One building-wide event (e.g. a single rent cut applied identically across a property)
    otherwise floods the leaderboard with dozens of near-identical rows for that one property,
    crowding out every other property's worst units. Reuses the SAME per-property clustering
    the attention feed uses (``_cluster_and_rollup``/``_rollup_by_property``): a tight cluster
    of same-property/same-month/same-magnitude drops collapses into ONE row (``rolled_up:
    true``, ``count``), while genuine outliers stay individual rows.
    """
    _set_currency_symbol(db, account_id)
    if date_to is None:
        # Non-future: a month scheduled ahead (a shared expense posted forward) holds costs
        # with no matching income, so anchoring the feed there would report every property as
        # an NOI collapse the moment a recurring cost is scheduled. See `latest_actual_month`.
        date_to = latest_actual_month(
            db, "portfolio_month_summary", "account_id = :account_id",
            {"account_id": account_id},
        )
    if date_to is None:
        return {"period_from": None, "period_to": None, "items": []}
    if date_from is None:
        date_from = date_to

    items: list[dict] = []
    for month in _distinct_months(
        db,
        "portfolio_month_summary",
        "account_id = :account_id",
        {"account_id": account_id},
        date_from,
        date_to,
    ):
        rolled = _rollup_by_property(
            _all_unit_noi_drops(db, account_id, month, tags=tags), "noi_drop"
        )
        for it in rolled:
            it["month"] = month
        items += rolled
    items.sort(key=lambda it: it["magnitude"], reverse=True)
    return {"period_from": date_from, "period_to": date_to, "items": items[:limit]}


def _unit_vacancies(db: Session, account_id: str, pid: str, name: str, month: date):
    """Explicit/inferred vacancy: a unit-month RECORD WAS POSTED this month (flagged vacant,
    or $0 rent), for a unit that was occupied as of its most recent EARLIER recorded month.
    Distinct from :func:`_unit_missing_data` (no record posted at all) — migration 0011 split
    these because they used to be indistinguishable (both just "no row"). Using the latest
    prior row rather than strictly month-1 means a data gap doesn't hide the transition, and
    a unit already vacant last period (rent 0) won't re-flag.
    """
    rows = db.execute(
        text(
            """
            SELECT u.id::text AS uid, u.unit_number AS num, prev.prev_rent AS prev_rent
            FROM units u
            JOIN LATERAL (
                SELECT gross_rent AS prev_rent FROM unit_month_summary p2
                WHERE p2.unit_id = u.id AND p2.month < :month
                ORDER BY p2.month DESC LIMIT 1
            ) prev ON TRUE
            JOIN unit_month_summary cur ON cur.unit_id = u.id AND cur.month = :month
            WHERE u.property_id = :pid AND prev.prev_rent > 0
              AND cur.account_id = :account_id
              AND (cur.is_vacant OR cur.gross_rent = 0)
            """
        ),
        {"pid": pid, "month": month, "account_id": account_id},
    ).mappings().all()
    out = []
    for r in rows:
        lost = float(r["prev_rent"])
        out.append({
            "type": "vacancy", "property_id": pid, "property_name": name,
            "unit_id": r["uid"], "unit_number": r["num"], "category": None,
            "magnitude": lost, "current": 0.0, "prior": lost, "change": -lost,
            "pct_change": None, "detail": {"units_lost": 1},
            "label": f"Unit {r['num']} went vacant, lost rent {_money(lost)}",
        })
    return out


def _unit_missing_data(db: Session, account_id: str, pid: str, name: str, month: date):
    """A unit that had NO monthly_record posted at all this month, distinct from an explicit
    vacancy — for a unit that was occupied as of its most recent EARLIER recorded month (so a
    unit that's simply been vacant/unrecorded for a long stretch doesn't re-flag every month).
    """
    rows = db.execute(
        text(
            """
            SELECT u.id::text AS uid, u.unit_number AS num, prev.prev_rent AS prev_rent
            FROM units u
            JOIN properties p ON p.id = u.property_id
            JOIN LATERAL (
                SELECT gross_rent AS prev_rent FROM unit_month_summary p2
                WHERE p2.unit_id = u.id AND p2.month < :month
                ORDER BY p2.month DESC LIMIT 1
            ) prev ON TRUE
            LEFT JOIN unit_month_summary cur ON cur.unit_id = u.id AND cur.month = :month
            WHERE u.property_id = :pid AND prev.prev_rent > 0 AND cur.unit_id IS NULL
              AND p.account_id = :account_id
            """
        ),
        {"pid": pid, "month": month, "account_id": account_id},
    ).mappings().all()
    out = []
    for r in rows:
        at_risk = float(r["prev_rent"])
        out.append({
            "type": "missing_data", "property_id": pid, "property_name": name,
            "unit_id": r["uid"], "unit_number": r["num"], "category": None,
            "magnitude": at_risk, "current": None, "prior": at_risk, "change": None,
            "pct_change": None, "detail": {"at_risk_basis": "prior_month_rent"},
            "label": f"Unit {r['num']} has no posted record this month",
        })
    return out


def _unit_expense_spikes(
    db: Session, account_id: str, pid: str, name: str, month: date, t: Thresholds
):
    t3_start = _add_months(month, -3)
    rows = db.execute(
        text(
            """
            WITH t3 AS (
                SELECT unit_id, AVG(operating_expenses) AS avg3
                FROM unit_month_summary
                WHERE property_id = :pid AND account_id = :account_id
                  AND month >= :t3_start AND month < :month
                GROUP BY unit_id
            )
            SELECT u.id::text AS uid, u.unit_number AS num,
                   cur.operating_expenses AS cur_amt, t3.avg3 AS avg3
            FROM unit_month_summary cur
            JOIN t3 ON t3.unit_id = cur.unit_id
            JOIN units u ON u.id = cur.unit_id
            WHERE cur.property_id = :pid AND cur.month = :month
              AND cur.account_id = :account_id
              AND t3.avg3 > 0
              AND (cur.operating_expenses - t3.avg3) >= :min_abs
              AND ((cur.operating_expenses - t3.avg3) / t3.avg3 * 100.0) >= :min_pct
            """
        ),
        {"pid": pid, "month": month, "t3_start": t3_start, "account_id": account_id,
         "min_abs": t.unit_expense_spike_min_abs, "min_pct": t.unit_expense_spike_min_pct},
    ).mappings().all()
    out = []
    for r in rows:
        cur_amt, avg3 = float(r["cur_amt"]), float(r["avg3"])
        change = cur_amt - avg3
        pct = change / avg3 * 100.0
        out.append({
            "type": "expense_spike", "property_id": pid, "property_name": name,
            "unit_id": r["uid"], "unit_number": r["num"], "category": None,
            "magnitude": change, "current": cur_amt, "prior": avg3, "change": change,
            "pct_change": pct, "detail": {"baseline": "trailing_3_month_avg"},
            "label": f"Unit {r['num']} operating expenses up {_money(change)} ({pct:.0f}%) vs 3-mo avg",
        })
    return out


def _make_rollup_item(cluster: list[dict], kind: str) -> dict:
    """Collapse a tight cluster of unit-level items (same property/month/type) into one
    summary item. Uses the cluster TOTAL as the ranking ``magnitude`` (the real aggregate
    dollar impact on the property), and the per-unit AVERAGE in the label/detail (what
    the analyst actually sees repeated across units)."""
    n = len(cluster)
    first = cluster[0]
    total_mag = sum(it["magnitude"] for it in cluster)
    avg_mag = total_mag / n
    pct_vals = [it["pct_change"] for it in cluster if it.get("pct_change") is not None]
    avg_pct = sum(pct_vals) / len(pct_vals) if pct_vals else None
    unit_numbers = [it["unit_number"] for it in cluster if it.get("unit_number")]
    shown = sorted(unit_numbers, key=lambda u: (len(u), u))[:12]

    pct_txt = f" ({avg_pct:.0f}%)" if avg_pct is not None else ""
    if kind == "noi_drop":
        label = f"{n} units NOI fell ~{_money(avg_mag)}{pct_txt} vs prior month"
    elif kind == "vacancy":
        label = f"{n} units went vacant, lost ~{_money(avg_mag)} each (total {_money(total_mag)})"
    elif kind == "expense_spike":
        label = f"{n} units' operating expenses up ~{_money(avg_mag)}{pct_txt} vs 3-mo avg"
    elif kind == "missing_data":
        label = f"{n} units have no posted record this month (~{_money(avg_mag)} rent at risk each)"
    else:
        label = f"{n} units affected, ~{_money(avg_mag)} each"

    return {
        "type": kind,
        "property_id": first["property_id"],
        "property_name": first["property_name"],
        "unit_id": None,
        "unit_number": None,
        "category": first.get("category"),
        "magnitude": total_mag,
        "current": None,
        "prior": None,
        "change": None,
        "pct_change": avg_pct,
        "detail": {
            "rolled_up": True,
            "count": n,
            "avg_magnitude": avg_mag,
            "total_magnitude": total_mag,
            "unit_numbers": shown,
            "unit_numbers_more": max(len(unit_numbers) - len(shown), 0),
        },
        "label": label,
        "rolled_up": True,
        "count": n,
    }


def _cluster_and_rollup(entries: list[dict], kind: str) -> list[dict]:
    """Group unit-level entries (already scoped to one property+month+type) into runs of
    tightly-clustered magnitude/pct_change, and roll up any run bigger than
    ``ROLLUP_MIN_COUNT`` into a single summary item. Conservative by design: entries that
    don't fit a tight cluster (the true outliers) are always left as individual items,
    even alongside a rolled-up group in the same month.
    """
    if len(entries) <= ROLLUP_MIN_COUNT:
        return entries

    ordered = sorted(entries, key=lambda it: it["magnitude"])
    clusters: list[list[dict]] = []
    current: list[dict] = []
    for it in ordered:
        if not current:
            current = [it]
            continue
        ref_mag = sum(x["magnitude"] for x in current) / len(current)
        mag_ok = abs(it["magnitude"] - ref_mag) <= max(ROLLUP_MAG_REL_TOL * ref_mag, ROLLUP_MAG_ABS_FLOOR)
        pct_ok = True
        cur_pct_vals = [x["pct_change"] for x in current if x.get("pct_change") is not None]
        if it.get("pct_change") is not None and cur_pct_vals:
            ref_pct = sum(cur_pct_vals) / len(cur_pct_vals)
            pct_ok = abs(it["pct_change"] - ref_pct) <= ROLLUP_PCT_ABS_TOL
        if mag_ok and pct_ok:
            current.append(it)
        else:
            clusters.append(current)
            current = [it]
    if current:
        clusters.append(current)

    out: list[dict] = []
    for cluster in clusters:
        if len(cluster) > ROLLUP_MIN_COUNT:
            out.append(_make_rollup_item(cluster, kind))
        else:
            out.extend(cluster)
    return out


def property_attention(
    db: Session,
    account_id: str,
    property_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
    *,
    thresholds: Thresholds | None = None,
    limit: int = 100,
) -> dict | None:
    """Property-scoped feed over the selected period: the property's own category expense spikes
    (tier-inclusive) plus unit-grain signals (which units dropped, went explicitly vacant, had
    no record posted at all (missing data — a separate item type from vacancy, migration
    0011), or had an opex spike), for every month in the period. Returns None if the property
    doesn't exist in this account."""
    name = db.execute(
        text("SELECT name FROM properties WHERE id = :id AND account_id = :account_id"),
        {"id": property_id, "account_id": account_id},
    ).scalar()
    if name is None:
        return None

    t = thresholds or Thresholds.from_db(db, account_id)
    _set_currency_symbol(db, account_id)
    if date_to is None:
        date_to = latest_actual_month(
            db, "property_month_summary",
            "property_id = :id AND account_id = :account_id",
            {"id": property_id, "account_id": account_id},
        )
    if date_to is None:
        return {"period_from": None, "period_to": None, "items": [], "thresholds": vars(t)}
    if date_from is None:
        date_from = date_to

    items = []
    where = "account_id = :account_id AND property_id = :id"
    params = {"account_id": account_id, "id": property_id}
    for month in _distinct_months(db, "property_month_summary", where, params, date_from, date_to):
        # Unit-grain detectors roll up when a building-wide event (e.g. one rent cut
        # applied identically across a property) would otherwise emit one near-duplicate
        # item per unit; true per-unit outliers are left individual. See
        # _cluster_and_rollup. Property/tier-level category spikes have no per-unit
        # duplication problem, so they're never rolled up.
        # ORDER MATTERS. Linking happens at UNIT grain, before any roll-up or fold, because
        # both of those erase the unit_id the link keys on:
        #   1. suppress a unit's NOI drop when that same unit went vacant (same event),
        #   2. THEN roll near-identical per-unit items into one line,
        #   3. THEN fold the surviving unit vacancies into the property-level occupancy drop.
        raw_vac = _unit_vacancies(db, account_id, property_id, name, month)
        raw_noi = _unit_noi_drops(db, account_id, property_id, name, month, t)
        linked = _link_vacancy_to_noi_drop(raw_vac + raw_noi)
        unit_vac = _cluster_and_rollup([i for i in linked if i["type"] == "vacancy"], "vacancy")
        unit_noi = _cluster_and_rollup([i for i in linked if i["type"] == "noi_drop"], "noi_drop")

        # Same fold as the portfolio feed: a material property-level occupancy drop absorbs
        # the unit vacancies that caused it.
        occ_drops = [
            d
            for d in _occupancy_drops(db, account_id, month, t)
            if d["property_id"] == property_id
        ]
        unit_vac = _fold_vacancies_into_occupancy_drops(occ_drops, unit_vac)

        batch = (
            # property/tier category spikes
            _expense_spikes(db, account_id, month, t, property_id=property_id)
            + unit_noi
            + unit_vac
            + _cluster_and_rollup(
                _unit_missing_data(db, account_id, property_id, name, month), "missing_data"
            )
            + _cluster_and_rollup(
                _unit_expense_spikes(db, account_id, property_id, name, month, t), "expense_spike"
            )
        )
        for it in batch:
            it["month"] = month
        items += batch
    items.sort(key=lambda it: it["magnitude"], reverse=True)
    return {
        "period_from": date_from,
        "period_to": date_to,
        "items": items[:limit],
        "thresholds": vars(t),
    }
