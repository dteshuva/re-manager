"""Attention-feed engine (INSIGHT_DASHBOARD_SPEC sub-step 3).

The portfolio dashboard's job at scale is *surfacing exceptions*, not displaying
everything. This computes a ranked list of "what needs attention this month?", entirely
from the pre-aggregated rollups (``property_month_summary`` / ``property_category_month_summary``)
— never from raw line items at request time — so it stays fast across thousands of units.

Four detectors (the ones computable from existing data):
  * noi_drop      — property NOI fell vs prior month past both $ and % floors.
  * expense_spike — a single operating category materially above its own trailing-3-month
                    average (both $ and % floors).
  * vacancy       — a property's occupied-unit count dropped vs prior month (lost rent).
  * missing_data  — a property that had a prior-month rollup but none for the selected month.

Every item carries a ``magnitude`` ($) and the whole feed is ranked by it, biggest first.
Thresholds come from settings (per-account in sub-step 6); pass ``thresholds`` to override.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings


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
    def from_db(cls, db: Session) -> "Thresholds":
        """Per-account thresholds from attention_settings, falling back to config defaults."""
        row = db.execute(
            text("SELECT " + ", ".join(cls._FIELDS) + " FROM attention_settings WHERE id = 1")
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
    return f"${abs(v):,.0f}"


def _noi_drops(db: Session, month: date, t: Thresholds) -> list[dict]:
    # Baseline = the property's most recent EARLIER month that has a rollup (not necessarily
    # month-1), so a gap in the data doesn't blind the comparison. See _prior_row().
    rows = db.execute(
        text(
            """
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
              AND prev.noi > 0
              AND (prev.noi - cur.noi) >= :min_abs
              AND ((prev.noi - cur.noi) / prev.noi * 100.0) >= :min_pct
            """
        ),
        {"month": month, "min_abs": t.noi_drop_min_abs, "min_pct": t.noi_drop_min_pct},
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
    db: Session, month: date, t: Thresholds, property_id: str | None = None
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
              AND t3.avg3 > 0
              AND (cur.amount - t3.avg3) >= :min_abs
              AND ((cur.amount - t3.avg3) / t3.avg3 * 100.0) >= :min_pct {scope}
            """
        ),
        {
            "month": month, "t3_start": t3_start, "pid": property_id,
            "min_abs": t.expense_spike_min_abs, "min_pct": t.expense_spike_min_pct,
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


def _vacancies(db: Session, month: date, t: Thresholds) -> list[dict]:
    # Size-independent: flag when occupancy falls by >= the configured percentage points
    # (so one lost unit flags a small property but not a 1,000-unit one). Baseline is the
    # most recent EARLIER month that has data, so a gap doesn't hide the drop.
    rows = db.execute(
        text(
            """
            SELECT cur.property_id::text AS pid, p.name AS name,
                   cur.occupied_units AS cu, prev.occupied_units AS pu,
                   cur.total_units AS tu, cur.occupancy AS cocc, prev.occupancy AS pocc,
                   (prev.gross_rent - cur.gross_rent) AS rent_delta
            FROM property_month_summary cur
            JOIN LATERAL (
                SELECT occupied_units, occupancy, gross_rent FROM property_month_summary p2
                WHERE p2.property_id = cur.property_id AND p2.month < cur.month
                ORDER BY p2.month DESC LIMIT 1
            ) prev ON TRUE
            JOIN properties p ON p.id = cur.property_id
            WHERE cur.month = :month
              AND cur.total_units > 0
              AND prev.occupancy IS NOT NULL AND cur.occupancy IS NOT NULL
              AND (prev.occupancy - cur.occupancy) * 100.0 >= :min_pp
            """
        ),
        {"month": month, "min_pp": t.vacancy_min_occupancy_drop_pct},
    ).mappings().all()
    items = []
    for r in rows:
        lost = int(r["pu"]) - int(r["cu"])
        rent_delta = float(r["rent_delta"])
        cocc = float(r["cocc"]) * 100 if r["cocc"] is not None else None
        pocc = float(r["pocc"]) * 100 if r["pocc"] is not None else None
        occ_txt = f" ({pocc:.0f}%→{cocc:.0f}%)" if cocc is not None and pocc is not None else ""
        items.append(
            {
                "type": "vacancy",
                "property_id": r["pid"],
                "property_name": r["name"],
                "category": None,
                "magnitude": max(rent_delta, 0.0),
                "current": float(r["cu"]),
                "prior": float(r["pu"]),
                "change": float(-lost),
                "pct_change": None,
                "detail": {
                    "units_lost": lost, "total_units": int(r["tu"]),
                    "occupancy": cocc, "prior_occupancy": pocc,
                },
                "label": f"{lost} unit{'s' if lost != 1 else ''} went vacant{occ_txt}, "
                         f"lost rent {_money(rent_delta)}",
            }
        )
    return items


def _high_vacancies(db: Session, month: date, t: Thresholds) -> list[dict]:
    """Absolute (level, not change) vacancy: flag any property sitting at or above the
    configured vacancy rate this month, EVEN IF it didn't get worse — a property that is
    persistently very empty is a standing problem the change-based detector would miss.
    Magnitude is the estimated at-market rent lost to the empty units (ranking proxy).
    """
    rows = db.execute(
        text(
            """
            SELECT cur.property_id::text AS pid, p.name AS name,
                   cur.occupied_units AS cu, cur.total_units AS tu,
                   cur.occupancy AS occ, cur.gross_rent AS rent
            FROM property_month_summary cur
            JOIN properties p ON p.id = cur.property_id
            WHERE cur.month = :month
              AND cur.total_units > 0
              AND cur.occupancy IS NOT NULL
              AND (1.0 - cur.occupancy) * 100.0 >= :min_vac_pct
            """
        ),
        {"month": month, "min_vac_pct": t.vacancy_high_absolute_pct},
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


def _missing_data(db: Session, month: date, prior: date) -> list[dict]:
    rows = db.execute(
        text(
            """
            SELECT p.id::text AS pid, p.name AS name, prev.noi AS prev_noi
            FROM properties p
            JOIN property_month_summary prev
              ON prev.property_id = p.id AND prev.month = :prior
            LEFT JOIN property_month_summary cur
              ON cur.property_id = p.id AND cur.month = :month
            WHERE cur.property_id IS NULL
            """
        ),
        {"month": month, "prior": prior},
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
    date_from: date | None = None,
    date_to: date | None = None,
    *,
    thresholds: Thresholds | None = None,
    limit: int = 50,
) -> dict:
    """Ranked exception feed for the selected period, computed from the rollups.

    Runs the month-over-month detectors for EVERY month in the period (not just the last),
    so an anomaly in a middle month of a YTD/T12 range still surfaces — each item is tagged
    with the month it occurred in. No params → latest single month.
    """
    t = thresholds or Thresholds.from_db(db)
    if date_to is None:
        date_to = db.execute(text("SELECT max(month) FROM portfolio_month_summary")).scalar()
    if date_to is None:
        return {"period_from": None, "period_to": None, "items": [], "thresholds": vars(t)}
    if date_from is None:
        date_from = date_to

    items = []
    for month in _distinct_months(db, "portfolio_month_summary", "TRUE", {}, date_from, date_to):
        prior = _add_months(month, -1)
        vac = _vacancies(db, month, t)
        # Absolute high-vacancy is a safety net: only surface it where the change-based
        # vacancy detector didn't already flag the same property (avoid a double item).
        flagged = {v["property_id"] for v in vac}
        high_vac = [h for h in _high_vacancies(db, month, t) if h["property_id"] not in flagged]
        batch = (
            _noi_drops(db, month, t)
            + _expense_spikes(db, month, t)
            + vac
            + high_vac
            + _missing_data(db, month, prior)
        )
        for it in batch:
            it["month"] = month
        items += batch
    items = _link_noi_to_expense(items, t.reconcile_ratio)
    items.sort(key=lambda it: it["magnitude"], reverse=True)
    return {
        "period_from": date_from,
        "period_to": date_to,
        "items": items[:limit],
        "thresholds": vars(t),
    }


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

def _unit_noi_drops(db: Session, pid: str, name: str, month: date, t: Thresholds):
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
              AND prev.noi > 0
              AND (prev.noi - cur.noi) >= :min_abs
              AND ((prev.noi - cur.noi) / prev.noi * 100.0) >= :min_pct
            """
        ),
        {"pid": pid, "month": month,
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


def _unit_vacancies(db: Session, pid: str, name: str, month: date):
    # Occupied as of its most recent EARLIER month with a row (rent > 0), now gone (no row)
    # or zero rent. Using the latest prior row rather than strictly month-1 means a data gap
    # doesn't hide the transition, and a unit already vacant last period (rent 0) won't re-flag.
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
            LEFT JOIN unit_month_summary cur ON cur.unit_id = u.id AND cur.month = :month
            WHERE u.property_id = :pid AND prev.prev_rent > 0
              AND (cur.unit_id IS NULL OR cur.gross_rent = 0)
            """
        ),
        {"pid": pid, "month": month},
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


def _unit_expense_spikes(db: Session, pid: str, name: str, month: date, t: Thresholds):
    t3_start = _add_months(month, -3)
    rows = db.execute(
        text(
            """
            WITH t3 AS (
                SELECT unit_id, AVG(operating_expenses) AS avg3
                FROM unit_month_summary
                WHERE property_id = :pid AND month >= :t3_start AND month < :month
                GROUP BY unit_id
            )
            SELECT u.id::text AS uid, u.unit_number AS num,
                   cur.operating_expenses AS cur_amt, t3.avg3 AS avg3
            FROM unit_month_summary cur
            JOIN t3 ON t3.unit_id = cur.unit_id
            JOIN units u ON u.id = cur.unit_id
            WHERE cur.property_id = :pid AND cur.month = :month
              AND t3.avg3 > 0
              AND (cur.operating_expenses - t3.avg3) >= :min_abs
              AND ((cur.operating_expenses - t3.avg3) / t3.avg3 * 100.0) >= :min_pct
            """
        ),
        {"pid": pid, "month": month, "t3_start": t3_start,
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
    property_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
    *,
    thresholds: Thresholds | None = None,
    limit: int = 100,
) -> dict | None:
    """Property-scoped feed over the selected period: the property's own category expense spikes
    (tier-inclusive) plus unit-grain signals (which units dropped, went vacant, or had an opex
    spike), for every month in the period. Returns None if the property doesn't exist."""
    name = db.execute(
        text("SELECT name FROM properties WHERE id = :id"), {"id": property_id}
    ).scalar()
    if name is None:
        return None

    t = thresholds or Thresholds.from_db(db)
    if date_to is None:
        date_to = db.execute(
            text("SELECT max(month) FROM property_month_summary WHERE property_id = :id"),
            {"id": property_id},
        ).scalar()
    if date_to is None:
        return {"period_from": None, "period_to": None, "items": [], "thresholds": vars(t)}
    if date_from is None:
        date_from = date_to

    items = []
    where, params = "property_id = :id", {"id": property_id}
    for month in _distinct_months(db, "property_month_summary", where, params, date_from, date_to):
        # Unit-grain detectors roll up when a building-wide event (e.g. one rent cut
        # applied identically across a property) would otherwise emit one near-duplicate
        # item per unit; true per-unit outliers are left individual. See
        # _cluster_and_rollup. Property/tier-level category spikes have no per-unit
        # duplication problem, so they're never rolled up.
        batch = (
            _expense_spikes(db, month, t, property_id=property_id)  # property/tier category spikes
            + _cluster_and_rollup(_unit_noi_drops(db, property_id, name, month, t), "noi_drop")
            + _cluster_and_rollup(_unit_vacancies(db, property_id, name, month), "vacancy")
            + _cluster_and_rollup(_unit_expense_spikes(db, property_id, name, month, t), "expense_spike")
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
