"""Free, on-machine PDF statement extraction — the third producer for the import seam.

Property managers each send statements in their own layout, so this parser is format-agnostic
rather than template-driven. It emits the same structured rows the CSV/Excel parser does
(:mod:`app.parsing`) into the same import core (:mod:`app.importer`); nothing here writes to
the database.

Two backends, both running entirely on the local machine with **no per-use cost**:

* ``heuristic`` — :mod:`pdfplumber` pulls text + tables, then rules find the reporting month,
  the property, and the (category, amount) lines. Always available, zero setup.
* ``ollama`` — if a local `Ollama <https://ollama.com>`_ server is reachable, the extracted
  text is handed to a local LLM for messy / free-text layouts the rules miss. Purely optional;
  when Ollama is not running we silently fall back to the heuristic.

The endpoint layer resolves the raw property/category strings this module returns against the
DB (to flag which need creating first); keeping that out of here makes the extractor reusable
and trivial to unit-test.
"""

from __future__ import annotations

import io
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import pdfplumber
from dateutil import parser as dateparser

from app.allocation import allocate

_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
# Substrings that mark a table/line as a total rather than a real category.
_TOTAL_WORDS = {"total", "subtotal", "grand total", "balance", "net", "sum"}


@dataclass
class RawExtraction:
    """DB-free result of parsing one statement. ``rows`` hold raw category strings; the
    endpoint matches them to the global category list afterwards."""

    property_name: str | None = None
    month: date | None = None
    rows: list[dict] = field(default_factory=list)  # {unit, category, amount, classification}
    backend: str = "heuristic"
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers


def _parse_amount(raw: object) -> float | None:
    """Parse a money cell to a float. Handles $, thousands commas, and both
    parenthesised and trailing/leading-minus negatives. Returns None if not a number."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    if s.endswith("-"):
        neg, s = True, s[:-1]
    if s.startswith("-"):
        neg, s = True, s[1:]
    # Drop any currency symbol ($, £, €, …), thousands separators, and spaces.
    s = re.sub(r"[^0-9.]", "", s)
    if not re.fullmatch(r"\d+(\.\d+)?", s):
        return None
    v = float(s)
    return -v if neg else v


_MONTH_TOKEN = re.compile(
    r"(?:%s)[a-z]*\.?\s+\d{1,2},?\s+\d{4}"  # Month D, YYYY
    r"|(?:%s)[a-z]*\.?\s+\d{4}"  # Month YYYY
    r"|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"  # M/D/Y
    r"|\d{4}[/-]\d{1,2}"  # YYYY-MM
    r"|\d{1,2}[/-]\d{4}" % (_MONTHS, _MONTHS),  # MM/YYYY
    re.IGNORECASE,
)
_MONTH_HINT = re.compile(r"period|month|statement|as of|for the|ending|reporting", re.IGNORECASE)

# Day-bearing dates, so a statement PERIOD (start + end) can be resolved to the month it
# mostly covers rather than just grabbing the first month token we see.
_DATE_TOKEN = re.compile(
    r"(?:%s)[a-z]*\.?\s+\d{1,2},?\s+\d{4}"  # Month D, YYYY
    r"|\d{1,2}\s+(?:%s)[a-z]*\.?\s+\d{4}"  # D Month YYYY
    r"|\d{4}[/-]\d{1,2}[/-]\d{1,2}"  # YYYY-MM-DD
    r"|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}" % (_MONTHS, _MONTHS),  # M/D/Y
    re.IGNORECASE,
)


def _dominant_month(a: date, b: date) -> date:
    """The calendar month that owns the most days of the inclusive range [a, b]
    (ties broken toward the earlier month). This is what "which month does this
    statement period mainly cover" means for e.g. 12/16–01/15 → December."""
    if b < a:
        a, b = b, a
    counts: dict[tuple[int, int], int] = {}
    cur, guard = a, 0
    while cur <= b and guard < 400:
        counts[(cur.year, cur.month)] = counts.get((cur.year, cur.month), 0) + 1
        cur += timedelta(days=1)
        guard += 1
    (year, month), _ = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    return date(year, month, 1)


def _detect_period_month(text: str) -> date | None:
    """If the statement states a period (two dates on a line, optionally after
    period/from/through/statement), resolve it to the month it mostly covers."""
    fallback: date | None = None
    for line in text.splitlines():
        toks = _DATE_TOKEN.findall(line)
        if len(toks) < 2:
            continue
        parsed: list[date] = []
        for tok in toks[:2]:
            try:
                d = dateparser.parse(tok, default=date(2000, 1, 1))
            except (ValueError, OverflowError):
                continue
            parsed.append(date(d.year, d.month, d.day))
        if len(parsed) < 2:
            continue
        month = _dominant_month(parsed[0], parsed[1])
        if _MONTH_HINT.search(line):
            return month  # an explicitly labelled period wins outright
        fallback = fallback or month
    return fallback


def _detect_single_month(text: str) -> date | None:
    """Fallback when there is no explicit period: the first month token, preferring
    ones on a period/statement/month line. Floored to the first of the month."""
    hinted: list[str] = []
    plain: list[str] = []
    for line in text.splitlines():
        toks = _MONTH_TOKEN.findall(line)
        if not toks:
            continue
        (hinted if _MONTH_HINT.search(line) else plain).extend(toks)
    for tok in [*hinted, *plain]:
        try:
            d = dateparser.parse(tok, default=date(2000, 1, 1))
        except (ValueError, OverflowError):
            continue
        if d is not None:
            return date(d.year, d.month, 1)
    return None


def _detect_month(text: str) -> date | None:
    """Prefer a stated statement period (resolved to the month it mostly covers);
    otherwise fall back to the first month token in the document."""
    return _detect_period_month(text) or _detect_single_month(text)


def _detect_property(text: str, known_properties: list[str]) -> str | None:
    """Best-effort property name. A known property that literally appears in the text is the
    strongest signal; otherwise fall back to a ``Property:``-style labelled line."""
    low = text.lower()
    # Longest known name first, so "Maple Court Apartments" beats a stray "Maple".
    for name in sorted(known_properties, key=len, reverse=True):
        if name.strip() and name.strip().lower() in low:
            return name
    # Then the same address matching a rent roll uses, so a statement writing "125 Allendale
    # Road" still finds a property stored as "125 Allendale Rd, Gateshead". Only a KNOWN
    # property may be returned here: an address on the page that matches nothing is as likely
    # to be the managing agent's own letterhead as a property of yours.
    for line in text.splitlines():
        address, _ = _split_address(line.strip())
        if address:
            hit = _match_known_property(address, known_properties)
            if hit:
                return hit

    m = re.search(
        r"^\s*(?:property|building|community|association|for|re)\s*[:\-]\s*(.+)$",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if m:
        cand = m.group(1).strip()
        if cand and len(cand) <= 120:
            return cand
    return None


# --- classification: which financial bucket a line item belongs in --------------------
# Effective classification drives all the math (see the P&L views): rent → income,
# operating → the expense section (above NOI), and capex/debt_service/other → below the line.
# We tag each row from its label so rent lands in rent, maintenance/utilities/fees land in
# operating expenses, mortgage lands in debt service, etc. Checked in specificity order;
# the first hit wins, and no match returns None (fall back to the category's own default).
_RE_DEBT = re.compile(
    r"\b(mortgage|principal|amortiz|debt\s*service|note\s*payment|\bloan\b)", re.IGNORECASE
)
_RE_CAPEX = re.compile(
    r"\b(capital|capex|capital\s*expenditure|improvement|renovat|replacement|rehab)",
    re.IGNORECASE,
)
_RE_RENT = re.compile(
    r"\b(rent|rental|lease\b|tenant\s*charge|gross\s*rent|base\s*rent|market\s*rent)",
    re.IGNORECASE,
)
_RE_OTHER = re.compile(
    r"\b(reserve|distribution|owner\s*draw|owner\s*disburse|disbursement|contribution|"
    r"net\s*to\s*owner)",
    re.IGNORECASE,
)
_RE_OPERATING = re.compile(
    r"\b(repair|mainten|management|mgmt|admin|utilit|water|sewer|electric|gas|trash|garbage|"
    r"waste|insurance|tax|landscap|lawn|garden|pest|clean|hoa|dues|advertis|marketing|legal|"
    r"account|supplies|commission|payroll|wage|salary|inspection|pool|elevator|security|snow|"
    r"turnover|leasing|concession|plumb|hvac|roof|paint|appliance|contractor|vendor|"
    r"landlord|janitor|fee)",
    re.IGNORECASE,
)


def _classify(name: str) -> str | None:
    """Guess the classification for a category label. Returns one of the enum values, or
    None when nothing matches (so the category's own default_classification is used)."""
    n = name.strip()
    low = n.lower()
    if _RE_DEBT.search(n) and "income" not in low:
        return "debt_service"
    if _RE_CAPEX.search(n):
        return "capex"
    if _RE_RENT.search(n):
        return "rent"
    if _RE_OTHER.search(n):
        return "other_below_line"
    if _RE_OPERATING.search(n):
        return "operating"
    return None


def _is_total_row(desc: str) -> bool:
    d = desc.strip().lower()
    return d in _TOTAL_WORDS or d.startswith("total ") or d.startswith("subtotal")


# --- section-scoped parsing (the robust path for real statements) ----------------------
# Real owner/letting statements group line items under Income / Expense(iture) headings,
# with registers, invoice attachments, VAT tables, and running balances OUTSIDE those
# sections. Reading only within the sections drops the noise that wrecks a naive scan.
_SECTION_INCOME = {"income", "rental income", "revenue", "receipts", "rent"}
_SECTION_EXPENSE = {"expense", "expenses", "expenditure", "expenditures", "operating expenses"}
_YTD_RE = re.compile(r"year\s*to\s*date|y\.?t\.?d\.?|fiscal", re.IGNORECASE)

# A "money" token must look like money — decimals, a currency symbol, or a thousands comma —
# so bare integers like invoice numbers (34756) and property references (10887) are ignored.
_MONEY_RE = re.compile(
    r"[£$€]\s?\(?-?\d[\d,]*(?:\.\d{1,2})?\)?"  # currency-prefixed
    r"|\(?-?\d[\d,]*\.\d{2}\)?"  # has cents
    r"|-?\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?"  # comma-grouped thousands
)

# Labels that are summaries/totals/metadata rather than real line items.
_NOISE_RE = re.compile(
    r"^\s*(total|subtotal|grand\s*total|net\s*(income|other|total)|cash\s*flow|"
    r"(beginning|ending|opening|closing|actual\s*ending)\s*(cash|balance)|balance|"
    r"invoice\s*(total|number|no)|property\s*reference|payment\s*(details|amount)|"
    r"vat\s*(summary|rate|registration)|company\s*registration|exempt|required\s*reserves|"
    r"prepaid\s*rent|selected\s*period|account\s*name|beginning\s*cash)",
    re.IGNORECASE,
)


def _is_noise(label: str) -> bool:
    return bool(_NOISE_RE.match(label.strip())) or _is_total_row(label)


def _line_item(line: str, *, prefer_first: bool, section: str | None) -> dict | None:
    """Extract (label, amount) from one line. The label is the text before the first money
    token; the amount is the last money token, or the first when the doc has a YTD column
    (so we take the period value, not year-to-date). Classification defers to _classify,
    with the section as a fallback so an unrecognised expense line still lands in operating."""
    ms = list(_MONEY_RE.finditer(line))
    if not ms:
        return None
    label = line[: ms[0].start()].strip(" .:\t-")
    # Drop a volatile "for period <dates> from <name>" tail so per-period lines collapse to a
    # stable category (e.g. "Rent for period 27/05… from Megan" → "Rent"); this lets the two
    # rent lines in a month merge, and keeps the category the same month to month.
    label = re.split(r"\s+for\s+(?:the\s+)?period\b", label, maxsplit=1, flags=re.IGNORECASE)[0].strip(" .:\t-")
    # Drop a trailing invoice/reference number (e.g. "Letting Fee 35114" → "Letting Fee").
    label = re.sub(r"\s+\d{3,}$", "", label).strip(" .:\t-")
    if not label or _is_noise(label):
        return None
    order = ms if not (prefer_first and len(ms) >= 2) else [ms[0]]
    amount = None
    for m in (order if order is not ms else reversed(ms)):
        amount = _parse_amount(m.group(0))
        if amount is not None:
            break
    if amount is None:
        return None
    cls = _classify(label)
    if cls is None and section == "expense":
        cls = "operating"
    return {"unit": None, "category": label, "amount": amount, "classification": cls}


def _rows_from_sections(text: str) -> list[dict]:
    """Read line items only inside Income / Expense(iture) sections, stopping each at its
    Total line. Ignores everything outside — registers, invoice attachments, VAT tables."""
    prefer_first = bool(_YTD_RE.search(text))
    rows: list[dict] = []
    section: str | None = None
    for raw in text.splitlines():
        low = raw.strip().lower()
        if low in _SECTION_INCOME:
            section = "income"
            continue
        if low in _SECTION_EXPENSE:
            section = "expense"
            continue
        if section is None:
            continue
        if low.startswith("total ") or low.startswith("subtotal"):
            section = None  # end of this section's line items
            continue
        item = _line_item(raw, prefer_first=prefer_first, section=section)
        if item:
            rows.append(item)
    return rows


def _rows_from_tables(tables: list[list[list]]) -> list[dict]:
    """Pull (category, amount, unit?) from extracted tables. For each table we pick the
    column that most often parses as money as the amount column, the first mostly-text
    column as the description, and an optional unit column by header name."""
    out: list[dict] = []
    for table in tables:
        rows = [[(c or "").strip() for c in row] for row in table if row]
        if len(rows) < 2:
            continue
        ncol = max(len(r) for r in rows)
        header = [c.lower() for c in rows[0]] + [""] * (ncol - len(rows[0]))

        # Amount column = the one with the most numeric cells (ties: rightmost).
        amount_col, best = None, 0
        for c in range(ncol):
            hits = sum(1 for r in rows[1:] if c < len(r) and _parse_amount(r[c]) is not None)
            if hits >= best and hits > 0:
                amount_col, best = c, hits
        if amount_col is None:
            continue

        unit_col = next(
            (c for c, h in enumerate(header) if re.search(r"unit|apt|suite|#|space", h)),
            None,
        )
        # Description column = first mostly-text column that isn't amount/unit.
        desc_col = None
        for c in range(ncol):
            if c in (amount_col, unit_col):
                continue
            texty = sum(
                1
                for r in rows[1:]
                if c < len(r) and r[c] and _parse_amount(r[c]) is None
            )
            if texty:
                desc_col = c
                break
        if desc_col is None:
            continue

        for r in rows[1:]:
            desc = r[desc_col].strip() if desc_col < len(r) else ""
            amount = _parse_amount(r[amount_col]) if amount_col < len(r) else None
            if not desc or amount is None or _is_total_row(desc):
                continue
            unit = (
                r[unit_col].strip()
                if unit_col is not None and unit_col < len(r) and r[unit_col].strip()
                else None
            )
            out.append({"unit": unit, "category": desc, "amount": amount, "classification": None})
    return out


def _rows_from_text(text: str) -> list[dict]:
    """Last-resort fallback for statements with neither Income/Expense sections nor clean
    tables: any line that carries a money token, e.g. ``Repairs & Maintenance …… 1,234.56``.
    Uses the same money-token + YTD-aware logic so it doesn't grab invoice numbers or the
    year-to-date column."""
    prefer_first = bool(_YTD_RE.search(text))
    out: list[dict] = []
    for line in text.splitlines():
        item = _line_item(line, prefer_first=prefer_first, section=None)
        if item:
            out.append(item)
    return out


def _extract_text_and_tables(content: bytes, max_pages: int) -> tuple[str, list[list[list]]]:
    text_parts: list[str] = []
    tables: list[list[list]] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages[:max_pages]:
            text_parts.append(page.extract_text() or "")
            for tbl in page.extract_tables() or []:
                tables.append(tbl)
    return "\n".join(text_parts), tables


# ------------------------------------------------------- multi-property rent rolls
# A second SHAPE of statement, not just a second layout. Everything above assumes one
# statement = one property: a header names the building and the line items below it are all
# that building's. A managing agent for a portfolio instead sends ONE page listing every
# property on its own line — address, the rent period it covers, the rent collected — and
# then charges the whole portfolio once at the bottom (management fee, a shared contractor
# bill) before arriving at the balance paid to the owner:
#
#     125 Allendale Road   29/8/26 to 28/9/26   697
#     127 Allendale Road   24/8/26 to 23/9/26   600
#     ...
#     Total Collected                          6710
#     Mngt Fee                                  939.4
#     D&D GAS                                    80
#     Balance Due                              5690.6
#
# Read by the single-property parsers this is worse than useless: the bare integers aren't
# even money-shaped (that regex deliberately ignores integers so invoice numbers don't become
# amounts), and every row belongs to a DIFFERENT property, so one detected_property would be
# wrong for eleven of twelve rows.
#
# So a rent roll is parsed into MANY extractions — one per property, each a perfectly
# ordinary single-property statement — and the rest of the system needs no new concept: the
# batch endpoint already resolves a list of mixed-property statements, dedupes their unknown
# properties, and applies each through the same idempotent import core.
#
# Three things this parser must get right, because each is a way to silently post wrong money:
#
# * **Whose row is it.** A line is only a rent row when its label resolves to a property —
#   either one that already exists (matched on a normalised address, so "125 Allendale Road"
#   finds "125 Allendale Rd, Gateshead") or something unmistakably address-shaped. Anything
#   else is a charge, a total, or noise, and totals are never imported as line items.
# * **Which month.** Every row states its own period in UK day-first dates, and those periods
#   straddle month ends, so the month is the one the period mostly covers — with the whole
#   statement posting to ONE month (the one most rows land in), since that is what a
#   collection statement is. Rows whose own period says otherwise are reported, not silently
#   moved.
# * **The portfolio charges.** A fee charged once over twelve properties has nowhere to live
#   in a per-property ledger, so it is split pro-rata by the rent each property contributed,
#   through :mod:`app.allocation` — the shares therefore sum back to the charged figure to
#   the penny, and the statement's own arithmetic still reconciles after the import.

_STREET = (
    r"(?:road|rd|street|st|avenue|ave|av|drive|dr|lane|ln|close|way|terrace|terr|ter|"
    r"place|court|crescent|cres|gardens|gdns|gdn|grove|view|park|square|sq|row|hill|"
    r"walk|rise|mews|villas|green|bank|parade|heights|hts|boulevard|blvd|circle|cir|"
    r"trail|highway|hwy|house|cottage|villa|buildings)"
)
# A UK-style address at the START of a label: number (optionally 12A or 12-14), up to four
# words, then a street type. The rest of the label is kept as a note — a rent roll often
# carries one ("awaiting Benefit", "vacant", a tenant name) where the period would be.
_ADDRESS_RE = re.compile(
    rf"^\s*((?:flat\s+\w+,?\s+)?\d+[a-z]?(?:\s*[-/]\s*\d+[a-z]?)?\s+(?:[A-Za-z'&.]+\s+){{0,4}}{_STREET})\b\.?",
    re.IGNORECASE,
)

# Street-type synonyms folded to one spelling, so a statement's "125 Allendale Road" matches
# a stored "125 Allendale Rd" (and vice versa) without anyone having to retype either.
_STREET_FOLD = {
    "road": "rd", "street": "st", "avenue": "ave", "av": "ave", "drive": "dr",
    "lane": "ln", "terrace": "ter", "terr": "ter", "gardens": "gdns", "gdn": "gdns",
    "garden": "gdns", "crescent": "cres", "square": "sq", "boulevard": "blvd",
    "circle": "cir", "highway": "hwy", "heights": "hts", "saint": "st",
}

# The trailing money column. Anchored at end-of-line and refusing a digit/slash/dot/dash
# immediately before it, so the "26" of a period ending 8/9/26 and the "69" of a sort code
# 20-83-69 can never be read as the amount. Bare integers ARE allowed here (unlike _MONEY_RE)
# because in this shape the column is positional: whatever ends the line is the figure.
_TRAIL_AMOUNT_RE = re.compile(r"(?<![\w/.\-])(\(?-?[£$€]?\s?-?\d[\d,]*(?:\.\d+)?\)?-?)\s*$")

_DMY_RE = r"\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}"
# "29/8/26 to 28/9/26", "29/8/26 - 28/9/26", "29/8/26–28/9/26".
_PERIOD_RE = re.compile(
    rf"({_DMY_RE})\s*(?:to|until|thru|through|[-–—])\s*({_DMY_RE})", re.IGNORECASE
)
_ANY_DMY_RE = re.compile(_DMY_RE)

# Labels that are the statement's own arithmetic rather than a chargeable item.
_ROLL_TOTAL_RE = re.compile(
    r"^\s*(total|totals|sub\s*-?\s*total|subtotal|grand\s*total|balance|bal\b|amount\s*due|"
    r"net\b|carried\s*forward|brought\s*forward|arrears|paid|payment)",
    re.IGNORECASE,
)
_RE_MGMT_FEE = re.compile(r"\b(mgmt|mngt|mgt|manage\w*)\b", re.IGNORECASE)

# A rent roll must show at least this many properties before we believe the shape. One
# address on a line is an ordinary statement's header; twelve of them is a rent roll.
_MIN_ROLL_PROPERTIES = 2
_MAX_PERIOD_DAYS = 62  # a longer "period" is a typo, not a tenancy month


@dataclass
class _RollRow:
    """One property's line on a rent roll, before it becomes an extraction."""

    name: str
    known: bool  # matched an existing property (vs. an address we read off the page)
    rent: float
    month: date | None  # from this row's own stated period
    start: date | None  # the period as resolved (an impossible end date repaired)
    end: date | None
    note: str  # anything the label carried besides the address ("awaiting Benefit")
    period: str  # the period text as printed, for the warning that quotes it


@dataclass
class StatementFile:
    """One uploaded PDF, resolved into the statements it actually contains.

    ``format`` is ``"single"`` for the one-property statements this module has always
    handled (``extractions`` then holds exactly one), and ``"rent_roll"`` for a
    multi-property roll, where ``extractions`` holds one per property. ``warnings`` are
    FILE-level notes — reconciliation against the statement's own totals, charges that were
    allocated, amounts that belonged to nobody — as opposed to the per-property warnings that
    live on each extraction.
    """

    format: str
    extractions: list[RawExtraction]
    warnings: list[str] = field(default_factory=list)


def _money(x: float) -> str:
    return f"{x:,.2f}"


def _norm_addr(s: str) -> list[str]:
    """An address as comparable tokens: case-folded, depunctuated, street types folded to a
    single spelling. "125 Allendale Road, Gateshead" -> ['125', 'allendale', 'rd', 'gateshead']."""
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    return [_STREET_FOLD.get(t, t) for t in s.split() if t]


def _match_known_property(label: str, known: list[str]) -> str | None:
    """The stored property this label names, or None.

    Matching is by token PREFIX in either direction, so a statement that says "125 Allendale
    Road" matches a property stored as "125 Allendale Rd, Gateshead" (the stored name is
    longer) and a statement line carrying a trailing note still matches (the label is
    longer). At least two tokens must agree, so a bare house number matches nothing. Ties go
    to the longest agreement, then the shortest stored name.
    """
    lt = _norm_addr(label)
    if len(lt) < 2:
        return None
    best: tuple[int, int, str] | None = None
    for name in known:
        kt = _norm_addr(name)
        n = min(len(lt), len(kt))
        if n < 2 or lt[:n] != kt[:n]:
            continue
        cand = (n, -len(kt), name)
        if best is None or cand > best:
            best = cand
    return best[2] if best else None


def _split_address(label: str) -> tuple[str | None, str]:
    """Split a label into its leading address and whatever follows (kept as a note)."""
    m = _ADDRESS_RE.match(label)
    if not m:
        return None, ""
    return re.sub(r"\s+", " ", m.group(1)).strip(" ,.-"), label[m.end():].strip(" ,.-:")


def _dayfirst(text: str) -> bool:
    """Whether this document writes dates day-first (29/8/26) or month-first (8/29/26).

    Decided from the document itself: any date whose first number exceeds 12 can only be
    day-first, and vice versa. Only when every date is ambiguous does the currency symbol
    break the tie — which is exactly the case a UK statement of 9/8/26 needs, and where
    guessing wrong would post a month of rent to the wrong month.
    """
    day_ev = month_ev = 0
    for tok in _ANY_DMY_RE.findall(text):
        parts = re.split(r"[/.\-]", tok)
        if len(parts) < 2:
            continue
        a, b = int(parts[0]), int(parts[1])
        if a > 12 >= b:
            day_ev += 1
        elif b > 12 >= a:
            month_ev += 1
    if day_ev or month_ev:
        return day_ev > month_ev
    return "£" in text


def _parse_dmy(tok: str, dayfirst: bool) -> date | None:
    try:
        d = dateparser.parse(tok, dayfirst=dayfirst, default=date(2000, 1, 1))
    except (ValueError, OverflowError, TypeError):
        return None
    return date(d.year, d.month, d.day)


def _overlaps_month(start: date, end: date, month: date) -> bool:
    """Whether an inclusive date range touches the calendar month beginning at ``month``."""
    nxt = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    return start < nxt and end >= month


def _period_month(start: date, end: date) -> tuple[date, date, bool]:
    """The month a tenancy period belongs to, and whether the period had to be repaired.

    A rent period straddles the month end (29/8 to 28/9), so the month it belongs to is the
    one it mostly covers — September, here. A mistyped year ("20/8/26 to 19/9/27", a slip
    that costs a keystroke and a year) would otherwise drag the answer months away, so an
    implausibly long period has its end year snapped back to the start's before the question
    is asked; if that doesn't produce a sane span we fall back to the start's own month
    rather than inventing one.
    """
    days = (end - start).days
    if days < 0 or days > _MAX_PERIOD_DAYS:
        for year in (start.year, start.year + 1):
            try:
                cand = end.replace(year=year)
            except ValueError:  # 29 Feb -> non-leap year
                continue
            if 0 <= (cand - start).days <= _MAX_PERIOD_DAYS:
                return _dominant_month(start, cand), cand, True
        return date(start.year, start.month, 1), start, True
    return _dominant_month(start, end), end, False


def _trailing_amount(line: str) -> tuple[str, float] | None:
    """Split a rent-roll line into (label, amount) on its trailing money column."""
    m = _TRAIL_AMOUNT_RE.search(line.rstrip())
    if not m:
        return None
    amount = _parse_amount(m.group(1))
    if amount is None:
        return None
    return line[: m.start()].strip(" .:\t-"), amount


def _snap_category(label: str, known_categories: list[str]) -> str:
    """Prefer an existing category name over coining a near-duplicate.

    An exact (case-insensitive) name wins. Failing that, a management fee — the charge
    essentially every rent roll ends with, and the one most likely to arrive under a new
    spelling every month ("Mngt Fee", "Mgmt Fee", "Management Charge") — is folded into the
    account's existing management category if it has one. Anything else keeps the
    statement's own wording and is offered to the user as a new category to approve.
    """
    by_lower = {c.strip().lower(): c for c in known_categories}
    hit = by_lower.get(label.strip().lower())
    if hit:
        return hit
    if _RE_MGMT_FEE.search(label) and re.search(r"fee|charge|commission", label, re.IGNORECASE):
        for c in known_categories:
            if _RE_MGMT_FEE.search(c):
                return c
    return label.strip()


def _parse_rent_roll(
    text: str, known_properties: list[str], known_categories: list[str]
) -> StatementFile | None:
    """Parse a multi-property rent roll, or return None if this isn't one.

    Returning None (rather than a half-parsed result) is what keeps the ordinary
    single-property statements working exactly as before: unless at least
    ``_MIN_ROLL_PROPERTIES`` distinct properties are found on their own money-bearing lines,
    the caller falls through to the section/table/text parsers untouched.
    """
    dayfirst = _dayfirst(text)

    rows: list[_RollRow] = []
    charges: list[tuple[str, float]] = []
    totals: list[tuple[str, float]] = []
    orphans: list[tuple[str, float]] = []  # (heading, amount) — an amount naming nobody
    heading = ""
    repaired = 0

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        hit = _trailing_amount(line)
        if hit is None:
            if re.search(r"[A-Za-z]", line):
                heading = line  # e.g. "ARREARS", used to explain any orphan below it
            continue
        label, amount = hit
        if not label:
            orphans.append((heading, amount))
            continue

        # Strip the stated period off the label before asking who the line belongs to, so
        # "125 Allendale Road 29/8/26 to 28/9/26" is looked up as an address.
        month: date | None = None
        start = end = None
        period = ""
        pm = _PERIOD_RE.search(label)
        if pm:
            start, end = _parse_dmy(pm.group(1), dayfirst), _parse_dmy(pm.group(2), dayfirst)
            period = pm.group(0)
            if start and end:
                month, end, fixed = _period_month(start, end)
                repaired += fixed
            label = (label[: pm.start()] + " " + label[pm.end():]).strip(" ,.-:")

        known = _match_known_property(label, known_properties)
        address, note = _split_address(label)
        if known or address:
            rows.append(
                _RollRow(
                    name=known or address or label,
                    known=known is not None,
                    rent=amount,
                    month=month,
                    start=start,
                    end=end,
                    note=note,
                    period=period,
                )
            )
        elif _ROLL_TOTAL_RE.match(label):
            totals.append((label, amount))
        else:
            charges.append((label, amount))

    if len({r.name.strip().lower() for r in rows}) < _MIN_ROLL_PROPERTIES:
        return None

    return _build_rent_roll(rows, charges, totals, orphans, repaired, known_categories)


def _build_rent_roll(
    rows: list[_RollRow],
    charges: list[tuple[str, float]],
    totals: list[tuple[str, float]],
    orphans: list[tuple[str, float]],
    repaired: int,
    known_categories: list[str],
) -> StatementFile:
    """Turn the parsed lines into one extraction per property, plus the file-level notes.

    Two judgement calls are made here and both are REPORTED rather than hidden, because each
    is a place where a reasonable operator might want the other answer:

    * **One month for the whole statement.** A collection statement is a month's collection,
      so every row posts to the month most rows land in. A row whose own period says
      otherwise (rent collected this month for last month's period) is named in a warning
      with the month it would have chosen, so it can be moved with one edit.
    * **Portfolio charges are split pro-rata by rent.** A fee charged once over the whole
      portfolio has no per-property figure on the statement, and rent is the basis a
      management fee is actually charged on. The split comes from :func:`app.allocation.allocate`,
      so the shares sum back to the charged amount exactly and the statement's own arithmetic
      still holds after import. The rows are ordinary rows: unticking them drops the charge.
    """
    warnings: list[str] = []

    # The statement's month: the one the most rows' periods land in (ties -> earliest).
    counts: dict[date, int] = {}
    for r in rows:
        if r.month:
            counts[r.month] = counts.get(r.month, 0) + 1
    month = min(counts, key=lambda m: (-counts[m], m)) if counts else None
    if month is None:
        warnings.append("No rent periods could be read — set the month for each property below.")

    rent_total = round(sum(r.rent for r in rows), 2)

    # Reconcile against the statement's own "Total Collected" when it states one. A mismatch
    # means a row was misread or missed, which is worth saying out loud before anything is
    # posted rather than discovering it in the P&L.
    stated = next(
        (
            amt
            for label, amt in totals
            if re.search(r"collect|rent|^totals?$", label.strip(), re.IGNORECASE)
        ),
        None,
    )
    if stated is not None:
        if abs(stated - rent_total) < 0.01:
            warnings.append(
                f"{len(rows)} properties, rent {_money(rent_total)} — matches the statement's "
                f"stated total."
            )
        else:
            warnings.append(
                f"⚠ The {len(rows)} rents read total {_money(rent_total)}, but the statement "
                f"states {_money(stated)} (a difference of {_money(stated - rent_total)}). "
                f"Check the rows below before uploading."
            )
    else:
        warnings.append(f"{len(rows)} properties, rent {_money(rent_total)}.")

    if repaired:
        warnings.append(
            f"{repaired} rent period{'s' if repaired > 1 else ''} had an end date more than "
            f"{_MAX_PERIOD_DAYS} days after the start (usually a mistyped year); "
            f"{'they were' if repaired > 1 else 'it was'} read as ending in the same tenancy month."
        )

    # Portfolio-wide charges, split across the properties by the rent each contributed.
    weights = [Decimal(str(r.rent)) for r in rows]
    charge_rows: list[list[dict]] = [[] for _ in rows]
    charged: list[str] = []
    for label, amount in charges:
        sign = -1 if amount < 0 else 1
        shares = allocate(Decimal(str(abs(amount))), weights)
        category = _snap_category(label, known_categories)
        classification = _classify(label) or "operating"
        for i, share in enumerate(shares):
            value = float(share) * sign
            if value == 0:
                continue  # a property that collected nothing carries none of the charge
            charge_rows[i].append(
                {
                    "unit": None,
                    "category": category,
                    "amount": value,
                    "classification": classification,
                    "kind": "allocated",
                    "note": f"{_money(amount)} charged over the portfolio, split by rent",
                }
            )
        charged.append(f"{category} {_money(amount)}")
    if charged:
        warnings.append(
            f"Portfolio charge(s) {', '.join(charged)} were split across the "
            f"{len(rows)} properties pro-rata by rent (the shares add back to the charged "
            "figure exactly). Untick those rows to leave them out."
        )

    # An orphan is an amount printed with no label of its own. Most are genuinely
    # unattributable (an arrears column, a stray figure) and must be reported so nobody
    # assumes they were imported — but a rent roll also prints its running arithmetic on a
    # bare line, with the words on the NEXT line ("5770.6" above "Sub Total"). Those aren't
    # missing data, so any orphan that equals a figure the statement has already computed —
    # the rent total, or the rent total after each charge — is recognised and passed over.
    running = rent_total
    computed = {round(rent_total, 2), *(round(a, 2) for _, a in totals)}
    for _, amount in charges:
        running = round(running - amount, 2)
        computed.add(running)
    unexplained = [(h, a) for h, a in orphans if round(a, 2) not in computed]
    if unexplained:
        by_heading: dict[str, list[str]] = {}
        for head, amt in unexplained:
            by_heading.setdefault(head or "no heading", []).append(_money(amt))
        for head, amts in by_heading.items():
            warnings.append(
                f"Not imported — {', '.join(amts)} under “{head}” name no property."
            )

    rent_category = _snap_category("Rent", known_categories)
    extractions: list[RawExtraction] = []
    for i, r in enumerate(rows):
        ex = RawExtraction(property_name=r.name, month=r.month or month, backend="rent_roll")
        ex.rows = [
            {
                "unit": None,
                "category": rent_category,
                "amount": r.rent,
                "classification": "rent",
                "kind": "rent",
                "note": r.note or None,
            },
            *charge_rows[i],
        ]
        if r.note:
            ex.warnings.append(f"The statement notes “{r.note}” against this property.")
        if month and r.month and r.month != month:
            # Posted with the rest of the statement, but say so — this is the one place the
            # parser overrides what the row itself said.
            ex.month = month
            # Most rent periods straddle a month end, so "this row's dominant month differs"
            # is usually just which side of the boundary it fell on — not worth a warning on
            # eleven of twelve rows. What IS worth saying is a period that never touches the
            # statement's month at all: that is last month's rent turning up in this month's
            # collection, and it may belong in the earlier month instead.
            if r.start and r.end and not _overlaps_month(r.start, r.end, month):
                ex.warnings.append(
                    f"Its period ({r.period}) is entirely within {r.month:%b %Y}, but it will "
                    f"post to {month:%b %Y} with the rest of this statement. Change the month "
                    "above to post it separately."
                )
        elif r.month is None and month:
            ex.warnings.append(
                f"No period stated; using the statement's month ({month:%b %Y})."
            )
        extractions.append(ex)

    return StatementFile(format="rent_roll", extractions=extractions, warnings=warnings)


# ------------------------------------------------------------------------- ollama


def _ollama_reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/tags", timeout=1.5) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _extract_with_ollama(
    text: str, known_categories: list[str], *, url: str, model: str
) -> RawExtraction | None:
    """Ask a local Ollama model to structure the statement text. Returns None on any
    failure so the caller falls back to the heuristic."""
    cats = ", ".join(known_categories) if known_categories else "(none defined yet)"
    prompt = (
        "You extract line items from a rental property's monthly statement.\n"
        "Return ONLY JSON of the form:\n"
        '{"property": string|null, "month": "YYYY-MM"|null, '
        '"rows": [{"unit": string|null, "category": string, "amount": number}]}\n'
        "Rules: amount is a plain number (expenses/costs may be negative if shown that way); "
        "unit is the apartment/unit number or null for a whole-property item; "
        "skip total/subtotal lines. Prefer these existing category names when they match: "
        f"{cats}.\n\nSTATEMENT:\n{text[:12000]}"
    )
    body = json.dumps(
        {"model": model, "prompt": prompt, "stream": False, "format": "json"}
    ).encode()
    req = urllib.request.Request(
        f"{url.rstrip('/')}/api/generate", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            payload = json.loads(r.read().decode())
        data = json.loads(payload.get("response", "{}"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None

    ex = RawExtraction(backend="ollama")
    prop = data.get("property")
    ex.property_name = prop.strip() if isinstance(prop, str) and prop.strip() else None
    month = data.get("month")
    if isinstance(month, str) and month.strip():
        try:
            d = dateparser.parse(month, default=date(2000, 1, 1))
            ex.month = date(d.year, d.month, 1)
        except (ValueError, OverflowError, TypeError):
            pass
    for row in data.get("rows") or []:
        if not isinstance(row, dict):
            continue
        cat = row.get("category")
        amt = _parse_amount(row.get("amount"))
        if not isinstance(cat, str) or not cat.strip() or amt is None:
            continue
        unit = row.get("unit")
        ex.rows.append(
            {
                "unit": str(unit).strip() if unit not in (None, "") else None,
                "category": cat.strip(),
                "amount": amt,
                "classification": None,
            }
        )
    if not ex.rows:
        return None  # nothing usable — let the heuristic try
    return ex


# --------------------------------------------------------------------------- entry


def _extract_single(
    text: str,
    tables: list[list[list]],
    *,
    known_properties: list[str],
    known_categories: list[str],
    use_ollama: bool = True,
    ollama_url: str = "http://localhost:11434",
    ollama_model: str = "llama3.2",
) -> RawExtraction:
    """Parse ONE property's statement out of already-extracted text/tables. Tries the local
    LLM first when available, then always falls back to the pdfplumber heuristic."""
    ex: RawExtraction | None = None
    if use_ollama and _ollama_reachable(ollama_url):
        ex = _extract_with_ollama(text, known_categories, url=ollama_url, model=ollama_model)
        if ex is None:
            # Ollama was up but gave nothing usable; note it and fall through.
            pass

    if ex is None:
        ex = RawExtraction(backend="heuristic")
        # Section-scoped first (robust on real statements), then clean tables, then any
        # money-bearing line as a last resort.
        ex.rows = _rows_from_sections(text) or _rows_from_tables(tables) or _rows_from_text(text)

    # Fill property from the text if the chosen backend didn't supply it.
    if not ex.property_name:
        ex.property_name = _detect_property(text, known_properties)

    # Month: prefer the period-aware detector (start/end → month it mostly covers). It reads
    # the full text, so it's more precise than a backend's single guess; keep the backend's
    # value only as a fallback.
    ex.month = _detect_month(text) or ex.month

    # Classify rows the backend/section parser didn't already tag, from the label: rent lands
    # in rent, maintenance/utilities/fees in operating expenses, mortgage in debt service, etc.
    # Left as None ⇒ the category's own default_classification is used.
    for row in ex.rows:
        if row.get("classification") is None:
            row["classification"] = _classify(row.get("category", ""))

    # Sum line items that share a (unit, category) within the statement. The data model stores
    # ONE line item per category per month, so multiple same-category lines (e.g. two
    # "Management Fee" charges, or split rent) must combine — this also reconciles with the
    # statement's own category totals and avoids a duplicate-key error at import time.
    merged: dict[tuple, dict] = {}
    order: list[tuple] = []
    for row in ex.rows:
        key = (row.get("unit"), row["category"].strip().lower())
        if key in merged:
            merged[key]["amount"] = round(merged[key]["amount"] + row["amount"], 2)
        else:
            merged[key] = row
            order.append(key)
    ex.rows = [merged[k] for k in order]

    if not ex.rows:
        ex.warnings.append(
            "No line items could be read from this PDF. If it is a scanned image, "
            "the text isn't machine-readable; try the CSV/Excel import instead."
        )
    if ex.month is None:
        ex.warnings.append("Could not detect the statement month — pick it manually below.")
    if not ex.property_name:
        ex.warnings.append("Could not detect the property — choose or add it below.")
    return ex


def extract_statements(
    content: bytes,
    *,
    known_properties: list[str],
    known_categories: list[str],
    max_pages: int = 20,
    use_ollama: bool = True,
    ollama_url: str = "http://localhost:11434",
    ollama_model: str = "llama3.2",
) -> StatementFile:
    """Parse a PDF into the statement(s) it contains. **The entry point to use.**

    One PDF is not always one statement. A portfolio agent's rent roll lists every property
    on its own line, so it comes back as ``format="rent_roll"`` with one extraction per
    property; everything else comes back as ``format="single"`` with exactly one. The rent
    roll is tried first and returns None unless the shape is unmistakable, so an ordinary
    statement takes the same path it always has. Never touches the database.
    """
    text, tables = _extract_text_and_tables(content, max_pages)

    roll = _parse_rent_roll(text, known_properties, known_categories)
    if roll is not None:
        return roll

    ex = _extract_single(
        text,
        tables,
        known_properties=known_properties,
        known_categories=known_categories,
        use_ollama=use_ollama,
        ollama_url=ollama_url,
        ollama_model=ollama_model,
    )
    return StatementFile(format="single", extractions=[ex])


def extract_statement(
    content: bytes,
    *,
    known_properties: list[str],
    known_categories: list[str],
    max_pages: int = 20,
    use_ollama: bool = True,
    ollama_url: str = "http://localhost:11434",
    ollama_model: str = "llama3.2",
) -> RawExtraction:
    """One statement from a PDF — the single-property view of :func:`extract_statements`.

    Kept for callers that can only handle one property. If the file turns out to be a
    multi-property rent roll this returns its FIRST property with the file's notes attached,
    which is lossy by construction — use :func:`extract_statements` to get them all.
    """
    result = extract_statements(
        content,
        known_properties=known_properties,
        known_categories=known_categories,
        max_pages=max_pages,
        use_ollama=use_ollama,
        ollama_url=ollama_url,
        ollama_model=ollama_model,
    )
    ex = result.extractions[0]
    ex.warnings = [*result.warnings, *ex.warnings]
    if len(result.extractions) > 1:
        ex.warnings.insert(
            0,
            f"This file covers {len(result.extractions)} properties; only the first is shown "
            "here. Use the batch upload to import them all.",
        )
    return ex
