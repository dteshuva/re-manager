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

import hashlib
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
    # The OTHER month this statement could reasonably post to, when the document gives two
    # answers — the period the agent says the statement covers, and the tenancy period the
    # rent is for. ``month`` holds the one the parser chose and explained; this holds the one
    # it did not, so a saved format can say "this sender's statements go to the other one"
    # without re-reading the PDF. None when the document only offers one answer.
    alt_month: date | None = None
    # The property as the statement PRINTED it, kept even after it has been matched to a
    # stored property. Teaching a format means recording "this sender's name for that
    # property", so the sender's name has to survive the matching.
    raw_property_name: str | None = None


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


def _extract_doc(content: bytes, max_pages: int):
    """Everything a parser below may need from the PDF, read once.

    ``text`` and ``tables`` are what the single-property parsers have always used. ``lines``
    additionally keeps each word's x position, which is the only way to tell which COLUMN a
    figure sits in on a tabular statement — see the sectioned-portfolio parser.
    """
    text_parts: list[str] = []
    tables: list[list[list]] = []
    lines: list["_DocLine"] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page_no, page in enumerate(pdf.pages[:max_pages]):
            text_parts.append(_clean(page.extract_text() or ""))
            for tbl in page.extract_tables() or []:
                tables.append(
                    [[_clean(c) if isinstance(c, str) else c for c in row] for row in tbl]
                )
            lines.extend(_page_lines(page, page_no))
    return "\n".join(text_parts), tables, lines


def _extract_text_and_tables(content: bytes, max_pages: int) -> tuple[str, list[list[list]]]:
    text, tables, _lines = _extract_doc(content, max_pages)
    return text, tables


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
    # Identifies the LAYOUT, so a saved set of corrections for this sender can be found
    # again next month. Computed from the document alone — never from the account's data —
    # so the same file fingerprints the same whoever uploads it. See :func:`fingerprint`.
    fingerprint: str = ""
    sample: str = ""


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


# --------------------------------------------- sectioned portfolio statements
# A THIRD shape, and again a different shape rather than a different layout. The rent roll
# above puts each property on ONE line with its rent at the end. An agent running on
# statement software sends the same portfolio as a sequence of TABLES instead: an address as
# a section heading, a row of column names beneath it, the figures, and a "Property Subtotal"
# closing each block, with "Statement Grand Totals" at the end:
#
#     Income/Expenses per Property
#     64 King Edward Street Gateshead NE8 3PR
#     Property / Unit    Income  Expenses  Mgt Fee  Mgt VAT  Other  Total Due
#                       £725.00     £0.00   £72.50    £0.00  £0.00    £652.50
#     Property Subtotal £725.00     £0.00   £72.50    £0.00  £0.00    £652.50
#
# Read by either parser above this is silently wrong in the expensive direction: the address
# line carries no amount, so the rent roll declines the file, and the single-property path
# then takes the FIRST address as the whole file's property and whatever total it can find as
# the only line item — three properties collapsed into one, every rent and fee lost.
#
# What makes this shape readable is geometry, not words. The figures are right-aligned under
# their headings, so "which column is this number in" is a question about x coordinates. Asked
# of a text line instead it has no good answer: splitting on whitespace cannot tell the one
# column "Mgt Fee" from the two columns "Mgt" and "Fee", and the row that carries the actual
# figures has no label at all, so pdfplumber's own table extraction drops it. This parser
# therefore works from WORD BOXES: group each line's words into columns by the gaps between
# them, then give every figure to the column whose edge it lines up with.
#
# Three things it must get right, each a way to post wrong money silently:
#
# * **Expenses are stored positive.** NOI is rent MINUS operating, so a management fee
#   imported as the -£192.50 the statement prints would raise NOI by the fee instead of
#   lowering it. The sign convention is read off the document and normalised.
# * **The statement's arithmetic is never a line item.** Subtotals, grand totals and a
#   "Total Due" column are the statement checking itself; they are used to RECONCILE what was
#   read and are never imported.
# * **One month for the whole statement.** A collection statement is one period's collection,
#   and tenancy periods inside it start on different days — posting each property by its own
#   rent period would split one statement across two months. The statement's own declared
#   period decides, and a rent period pointing elsewhere is reported, not silently followed.


@dataclass
class _Word:
    text: str
    x0: float
    x1: float


@dataclass
class _Column:
    name: str
    x0: float
    x1: float


@dataclass
class _DocLine:
    page: int
    top: float
    words: list[_Word] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass
class _Block:
    """One property's table under one heading."""

    name: str  # resolved property name (a stored one when it matched)
    raw: str  # the label exactly as the statement printed it
    known: bool
    heading: str
    columns: list[_Column] = field(default_factory=list)
    rows: list[tuple[str, dict[str, float]]] = field(default_factory=list)
    subtotal: dict[str, float] | None = None


_GRAND_TOTAL_RE = re.compile(
    r"^\s*(?:statement\s+)?grand\s*totals?\b|^\s*statement\s+totals?\b", re.IGNORECASE
)
_SUB_TOTAL_RE = re.compile(
    r"^\s*(?:property\s+|unit\s+)?sub\s*-?\s*totals?\b|^\s*totals?\b", re.IGNORECASE
)
# A column set carrying any of these is a DETAIL table (one row per receipt/charge) rather
# than a per-property summary of columns.
_DETAIL_COL_RE = re.compile(r"received|demanded|b\s*/\s*f|c\s*/\s*f", re.IGNORECASE)
_EXPENSE_HEADING_RE = re.compile(
    r"expense|expenditure|charge|cost|disbursement|outgoing", re.IGNORECASE
)
_INCOME_COL_RE = re.compile(
    r"^(?:total\s+)?(?:income|rent|receipts?|revenue|collected|demanded|received)\b",
    re.IGNORECASE,
)
# Columns that restate what the other columns already say. Never imported.
_ARITHMETIC_COL_RE = re.compile(
    r"^(?:total|due|balance|net|b\s*/\s*f|c\s*/\s*f|brought\s*forward|carried\s*forward|"
    r"opening|closing|property\s*/?\s*unit|unit|property|tenant|description|date|ref)\b",
    re.IGNORECASE,
)
_ABBREV_RE = re.compile(r"\b(?:mgt|mgmt|mngt|mgnt)\b", re.IGNORECASE)
_PUA_RE = re.compile(r"[-]")

# Two properties before the shape is believed, exactly as for a rent roll: one address above
# one table is an ordinary single-property statement and must keep its old path.
_MIN_SECTION_PROPERTIES = 2


def _clean(s: str | None) -> str:
    """Drop private-use glyphs. Statement software sets its bullets and icons from an icon
    font in the private-use area; they arrive as characters, and left alone one becomes a
    "category" with the statement's closing balance against it."""
    return _PUA_RE.sub(" ", s) if s else (s or "")


def _page_lines(page, page_no: int, tol: float = 3.0) -> list[_DocLine]:
    """A page's words grouped into visual lines, each line's words left-to-right."""
    placed: list[tuple[float, _Word]] = []
    for w in page.extract_words() or []:
        txt = _clean(str(w.get("text", ""))).strip()
        if not txt:
            continue
        try:
            placed.append((float(w["top"]), _Word(txt, float(w["x0"]), float(w["x1"]))))
        except (KeyError, TypeError, ValueError):
            continue
    placed.sort(key=lambda p: (p[0], p[1].x0))
    lines: list[_DocLine] = []
    for top, word in placed:
        if lines and abs(top - lines[-1].top) <= tol:
            lines[-1].words.append(word)
        else:
            lines.append(_DocLine(page=page_no, top=top, words=[word]))
    for ln in lines:
        ln.words.sort(key=lambda w: w.x0)
    return lines


def _column_groups(words: list[_Word]) -> list[_Column]:
    """Split a line's words into columns on the gaps between them.

    The threshold is derived from the line's own character width rather than fixed, so the
    same rule works for a 7pt table and an 11pt one: words a space apart ("Mgt" "Fee") are
    one column, words a tab apart are two.
    """
    if not words:
        return []
    widths = sorted((w.x1 - w.x0) / max(len(w.text), 1) for w in words)
    char_w = widths[len(widths) // 2]
    gap_limit = max(4.0, char_w * 2.5)
    groups: list[list[_Word]] = [[words[0]]]
    for prev, word in zip(words, words[1:]):
        if word.x0 - prev.x1 > gap_limit:
            groups.append([word])
        else:
            groups[-1].append(word)
    return [
        _Column(" ".join(w.text for w in g), g[0].x0, g[-1].x1) for g in groups
    ]


def _cell_value(word: _Word) -> float | None:
    """The figure in a cell, or None when the word isn't one.

    Stricter than :func:`_parse_amount`, which is given a cell already known to be a figure:
    here anything on the line may be offered, and a date (18/10/26) stripped of its
    punctuation would otherwise parse as 181026.
    """
    t = word.text.strip()
    if not t or t in {"-", "–", "—"}:
        return None
    if "/" in t or ":" in t:
        return None
    if _MONEY_RE.fullmatch(t) or re.fullmatch(r"\(?-?[£$€]?\s?-?\d[\d,]*(?:\.\d+)?\)?-?", t):
        return _parse_amount(t)
    return None


_SECTION_COLUMN_WORD_RE = re.compile(
    r"income|incomes|expense|expenses|expenditure|fee|fees|vat|other|others|total|totals|due|"
    r"tenant|tenants|unit|units|property|properties|rent|rents|charge|charges|amount|amounts|"
    r"net|gross|balance|arrears|paid|payment|payments|received|receipt|receipts|demanded|"
    r"demand|date|dates|description|period|opening|closing|brought|carried|forward|deposit|"
    r"deposits|commission|management|mgt|mgmt|mngt|b|c|f|ref|reference|invoice|supplier|"
    r"contractor|works|category|type|share|collected|owed|owing",
    re.IGNORECASE,
)

def _is_column_header(groups: list[_Column]) -> bool:
    """Whether this line names the columns of a table. Needs at least two columns and for
    most of them to be words a statement actually puts in a column heading — so an address
    or a sentence is never mistaken for one."""
    if len(groups) < 2:
        return False
    named = sum(
        1
        for g in groups
        if any(_SECTION_COLUMN_WORD_RE.fullmatch(w) for w in re.split(r"[\s/]+", g.name) if w)
    )
    return named >= 2 and named * 2 >= len(groups)




def _row_cells(
    words: list[_Word], columns: list[_Column]
) -> tuple[str, dict[str, float]]:
    """Split a data line into its row label and its figures by column.

    A figure belongs to the column whose edge it lines up with. Numbers in these tables are
    right-aligned, so the right edges agree to within a point; the left edges are compared
    too so a left-aligned column still matches.
    """
    if len(columns) < 2:
        return " ".join(w.text for w in words), {}
    first_value_x = columns[1].x0
    label_words: list[str] = []
    cells: dict[str, float] = {}
    for w in words:
        value = _cell_value(w)
        if value is None or w.x1 < first_value_x:
            # Anything that isn't a figure, and any figure still inside the label column,
            # belongs to the row's label ("Rent 19/09/26 - 18/10/26").
            label_words.append(w.text)
            continue
        best, best_d = None, None
        for col in columns[1:]:
            d = min(abs(w.x1 - col.x1), abs(w.x0 - col.x0))
            if best_d is None or d < best_d:
                best, best_d = col, d
        if best is not None and best_d is not None and best_d <= 30:
            key = best.name.strip().lower()
            cells[key] = round(cells.get(key, 0.0) + value, 2)
    return " ".join(label_words).strip(" .:\t-"), cells


def _tidy_label(name: str) -> str:
    """A column or row label as a category name: abbreviations expanded so it snaps to an
    existing category instead of coining "Mgt Fee" beside "Management Fee"."""
    out = _ABBREV_RE.sub("Management", name).strip(" .:/-")
    out = re.sub(r"\s+", " ", out)
    return out


def _parse_sectioned(
    lines: list[_DocLine], text: str, known_properties: list[str], known_categories: list[str]
) -> StatementFile | None:
    """Parse a sectioned portfolio statement, or return None if this isn't one."""
    blocks: list[_Block] = []
    file_totals: list[tuple[str, dict[str, float]]] = []
    pending_columns: list[_Column] = []
    heading = ""
    cur: _Block | None = None

    for ln in lines:
        words = ln.words
        if not words:
            continue
        groups = _column_groups(words)
        first = groups[0]
        # A figure BEYOND the first column is what makes a line a data row. The test cannot
        # be "does this line contain a number": a property is called "64 King Edward Street",
        # and its house number would make every address heading look like a row of figures.
        outer_figure = any(
            _cell_value(w) is not None and w.x0 >= first.x1 for w in words
        )

        if not outer_figure:
            if len(groups) <= 2:
                known = _match_known_property(first.name, known_properties)
                address, _note = _split_address(first.name)
                if known or address:
                    cur = _Block(
                        name=known or address or first.name,
                        raw=first.name.strip(),
                        known=known is not None,
                        heading=heading,
                        columns=list(pending_columns),
                    )
                    blocks.append(cur)
                    continue
            if _is_column_header(groups):
                # Kept even when no property block is open, so an agent that prints the
                # column names once above the whole section is read the same way.
                pending_columns = groups
                if cur is not None:
                    cur.columns = groups
                continue
            if len(groups) <= 2 and re.search(r"[A-Za-z]{2}", ln.text):
                heading = ln.text
                cur = None
            continue

        label, cells = _row_cells(words, cur.columns if cur and cur.columns else groups)
        if _GRAND_TOTAL_RE.match(label):
            if cells:
                file_totals.append((label, cells))
            cur = None
            continue
        if cur is None or not cur.columns or not cells:
            continue
        if _SUB_TOTAL_RE.match(label):
            cur.subtotal = cells
        else:
            cur.rows.append((label, cells))

    usable = [b for b in blocks if b.columns and (b.rows or b.subtotal)]
    if len({b.name.strip().lower() for b in usable}) < _MIN_SECTION_PROPERTIES:
        return None
    return _build_sectioned(usable, file_totals, text, known_categories)


def _sum_rows(rows: list[tuple[str, dict[str, float]]]) -> dict[str, float]:
    """Add a block's data rows column by column — the fallback when a block prints no
    subtotal of its own (a property with several units and no per-property line)."""
    out: dict[str, float] = {}
    for _label, cells in rows:
        for key, value in cells.items():
            out[key] = round(out.get(key, 0.0) + value, 2)
    return out


def _pick_col(columns: list[_Column], *patterns: str) -> str | None:
    """The first column matching the first pattern that matches anything — the preference
    order a caller writes out ("Received" before "Demanded")."""
    keys = [c.name.strip().lower() for c in columns]
    for pattern in patterns:
        for key in keys:
            if re.search(pattern, key, re.IGNORECASE):
                return key
    return None


def _detail_item(label: str, amount: float, demanded: float | None, dayfirst: bool) -> dict:
    """One row of a detail table: its own period read off the label, the label reduced to a
    category name ("Rent 19/09/26 - 18/10/26" → "Rent")."""
    month = start = end = None
    period = ""
    pm = _PERIOD_RE.search(label)
    if pm:
        start, end = _parse_dmy(pm.group(1), dayfirst), _parse_dmy(pm.group(2), dayfirst)
        period = pm.group(0)
        if start and end:
            month, end, _fixed = _period_month(start, end)
        label = (label[: pm.start()] + " " + label[pm.end():]).strip(" ,.-:")
    return {
        "label": _tidy_label(label) or "Rent",
        "raw": label.strip() or "Rent",
        "amount": amount,
        "demanded": demanded,
        "month": month,
        "period": period,
    }


def _build_sectioned(
    blocks: list[_Block],
    file_totals: list[tuple[str, dict[str, float]]],
    text: str,
    known_categories: list[str],
) -> StatementFile:
    """Turn the parsed blocks into one extraction per property, plus the file-level notes.

    Nothing is allocated here, unlike a rent roll: this shape states every property's own
    income and its own share of every charge, so the figures are taken as the statement gives
    them and checked back against its grand totals.
    """
    statement_month = _detect_month(text)
    dayfirst = _dayfirst(text)
    warnings: list[str] = []

    order: list[str] = []
    by_prop: dict[str, list[_Block]] = {}
    for b in blocks:
        key = b.name.strip().lower()
        if key not in by_prop:
            by_prop[key] = []
            order.append(key)
        by_prop[key].append(b)

    # ---- read each property's figures ------------------------------------------------
    parsed: list[dict] = []
    expense_values: list[float] = []
    for key in order:
        prop: dict = {
            "name": by_prop[key][0].name,
            "raw": by_prop[key][0].raw,
            "income_items": [],
            "expense_items": [],
            "summary_income": None,
            "summary_expense": None,
            "summary_other": [],
            "shortfall": [],
        }
        for b in by_prop[key]:
            cells = b.subtotal if b.subtotal is not None else _sum_rows(b.rows)
            cols = b.columns[1:]
            if any(_DETAIL_COL_RE.search(c.name) for c in cols):
                side = "expense" if _EXPENSE_HEADING_RE.search(b.heading) else "income"
                vcol = _pick_col(cols, r"received", r"\bpaid\b", r"amount", r"demanded", r"\bnet\b")
                dcol = _pick_col(cols, r"demanded", r"charged", r"\bdue\b")
                if vcol is None:
                    continue
                for label, rc in b.rows:
                    if _SUB_TOTAL_RE.match(label) or _GRAND_TOTAL_RE.match(label):
                        continue
                    amount = rc.get(vcol)
                    if amount is None:
                        continue
                    item = _detail_item(label, amount, rc.get(dcol) if dcol else None, dayfirst)
                    prop["expense_items" if side == "expense" else "income_items"].append(item)
                    if side == "expense":
                        expense_values.append(amount)
            else:
                for col in cols:
                    ckey = col.name.strip().lower()
                    amount = cells.get(ckey)
                    if amount is None or _ARITHMETIC_COL_RE.match(ckey):
                        continue
                    if _INCOME_COL_RE.match(ckey):
                        prop["summary_income"] = amount
                    elif re.match(r"^expenses?\b|^expenditure", ckey):
                        prop["summary_expense"] = amount
                        expense_values.append(amount)
                    else:
                        prop["summary_other"].append((col.name.strip(), amount))
                        expense_values.append(amount)
        parsed.append(prop)

    # ---- sign convention --------------------------------------------------------------
    # NOI is rent MINUS operating, so expenses are stored as positive magnitudes. A statement
    # that prints its charges as negatives ("Management Fee -£192.50") would, taken at face
    # value, RAISE NOI by the fee. Which convention this document uses is decided from the
    # document: if its charges are overwhelmingly negative they are all flipped, which leaves
    # a genuine credit negative — still the right sign for a refund.
    nonzero = [v for v in expense_values if abs(v) > 0.005]
    flip = bool(nonzero) and sum(1 for v in nonzero if v < 0) * 5 >= len(nonzero) * 4
    sign = -1.0 if flip else 1.0
    if flip:
        warnings.append(
            "This statement prints its charges as negative amounts; they were imported as "
            "positive expenses, which is how the P&L subtracts them."
        )

    rent_category = _snap_category("Rent", known_categories)
    extractions: list[RawExtraction] = []
    period_months: list[set[date]] = []
    rent_total = 0.0
    charge_totals: dict[str, float] = {}

    for prop in parsed:
        ex = RawExtraction(
            property_name=prop["name"],
            raw_property_name=prop["raw"],
            month=statement_month,
            backend="sectioned",
        )
        rows: list[dict] = []

        # Income: the itemised rows when the statement gives them (they carry the tenancy
        # period and the statement's own wording), otherwise the summary column.
        income_items = prop["income_items"]
        detail_income = round(sum(i["amount"] for i in income_items), 2)
        if income_items:
            for item in income_items:
                category = _snap_category(item["label"], known_categories)
                rows.append(
                    {
                        "unit": None,
                        "category": category,
                        "raw_category": item["raw"],
                        "amount": item["amount"],
                        "classification": _classify(category) or "rent",
                        "kind": "rent",
                        "note": f"period {item['period']}" if item["period"] else None,
                    }
                )
                if item["demanded"] is not None and abs(item["demanded"] - item["amount"]) > 0.005:
                    ex.warnings.append(
                        f"{_money(item['demanded'])} was demanded but {_money(item['amount'])} "
                        f"received — a shortfall of "
                        f"{_money(item['demanded'] - item['amount'])}. Only what was received "
                        "is imported."
                    )
            if prop["summary_income"] is not None and abs(prop["summary_income"] - detail_income) > 0.01:
                ex.warnings.append(
                    f"⚠ The itemised income totals {_money(detail_income)} but this property's "
                    f"summary row says {_money(prop['summary_income'])}. Check the rows below."
                )
        elif prop["summary_income"] is not None:
            rows.append(
                {
                    "unit": None,
                    "category": rent_category,
                    "raw_category": "Income",
                    "amount": prop["summary_income"],
                    "classification": "rent",
                    "kind": "rent",
                    "note": None,
                }
            )

        # Expenses: itemised when given, otherwise the summary's single Expenses column —
        # never both, or the itemised charges would be counted twice over their own total.
        expense_items = prop["expense_items"]
        if expense_items:
            for item in expense_items:
                category = _snap_category(item["label"], known_categories)
                rows.append(
                    {
                        "unit": None,
                        "category": category,
                        "raw_category": item["raw"],
                        "amount": round(item["amount"] * sign, 2),
                        "classification": _classify(category) or "operating",
                        "kind": "line",
                        "note": f"period {item['period']}" if item["period"] else None,
                    }
                )
            detail_expense = round(sum(i["amount"] for i in expense_items), 2)
            if prop["summary_expense"] is not None and abs(prop["summary_expense"] - detail_expense) > 0.01:
                ex.warnings.append(
                    f"⚠ The itemised expenses total {_money(detail_expense)} but this "
                    f"property's summary row says {_money(prop['summary_expense'])}."
                )
        elif prop["summary_expense"]:
            rows.append(
                {
                    "unit": None,
                    "category": _snap_category("Expenses", known_categories),
                    "raw_category": "Expenses",
                    "amount": round(prop["summary_expense"] * sign, 2),
                    "classification": "operating",
                    "kind": "line",
                    "note": None,
                }
            )

        # The remaining summary columns — management fee, its VAT, anything the agent calls
        # "Other" — are charges stated per property and appear nowhere else on the statement.
        for label, amount in prop["summary_other"]:
            if abs(amount) < 0.005:
                continue
            category = _snap_category(_tidy_label(label), known_categories)
            rows.append(
                {
                    "unit": None,
                    "category": category,
                    "raw_category": label,
                    "amount": round(amount * sign, 2),
                    "classification": _classify(category) or "operating",
                    "kind": "line",
                    "note": None,
                }
            )

        # A tenancy period pointing at another month is the one place this parser overrides
        # what a row says. Whether that is worth a word per property or one word for the file
        # is decided after every property is read — see below.
        months = {i["month"] for i in income_items if i["month"]}
        period_months.append(months)
        if statement_month is None:
            ex.month = next(iter(sorted(months)), None)
        elif len(months) == 1 and months != {statement_month}:
            ex.alt_month = next(iter(months))

        ex.rows = rows
        if not rows:
            ex.warnings.append("No figures were read for this property.")
        rent_total = round(rent_total + sum(r["amount"] for r in rows if r["kind"] == "rent"), 2)
        for r in rows:
            if r["kind"] != "rent":
                charge_totals[r["category"]] = round(
                    charge_totals.get(r["category"], 0.0) + r["amount"], 2
                )
        extractions.append(ex)

    # ---- the month, said once -----------------------------------------------------------
    # Tenancies in one portfolio start on different days, so their periods straddle the month
    # end in different directions. Posting each property by its own period would split ONE
    # collection statement across two months, so the statement's own declared period decides
    # for all of them. When the tenancy periods agree with each other and disagree with it,
    # that is one fact about the file rather than a repeated note against every property.
    stated = [m for m in period_months if m]
    if statement_month and stated:
        everywhere = set().union(*stated)
        if len(everywhere) == 1 and everywhere != {statement_month}:
            other = next(iter(everywhere))
            warnings.append(
                f"Every rent period here falls mostly in {other:%b %Y}, while the statement's "
                f"own period is {statement_month:%b %Y} — which is the month used, so the "
                f"whole statement posts together. Change the month on each property to post "
                f"to {other:%b %Y} instead."
            )
        else:
            for ex, months in zip(extractions, period_months):
                if months and months != {statement_month}:
                    named = ", ".join(sorted(m.strftime("%b %Y") for m in months))
                    ex.warnings.append(
                        f"Its rent period falls mostly in {named}, but it posts to "
                        f"{statement_month:%b %Y} with the rest of this statement. Change the "
                        "month above to post it separately."
                    )

    # ---- reconcile against the statement's own grand totals ----------------------------
    warnings.insert(0, _reconcile_sectioned(parsed, file_totals, rent_total, charge_totals, sign))
    if statement_month is None:
        warnings.append(
            "Could not read the statement's period — set the month for each property below."
        )
    return StatementFile(format="sectioned", extractions=extractions, warnings=warnings)


def _reconcile_sectioned(
    parsed: list[dict],
    file_totals: list[tuple[str, dict[str, float]]],
    rent_total: float,
    charge_totals: dict[str, float],
    sign: float,
) -> str:
    """One sentence saying whether what was read adds up to what the statement says it is.

    This is the check worth having: every figure here was read positionally out of a table,
    and the statement already prints the answer. Agreeing with it to the penny is strong
    evidence that no column was misread and no property was missed; disagreeing is worth
    seeing before anything is posted rather than at a year end.
    """
    stated_income = None
    for _label, cells in file_totals:
        for key, value in cells.items():
            if _INCOME_COL_RE.match(key) and not _ARITHMETIC_COL_RE.match(key):
                stated_income = value
                break
        if stated_income is not None:
            break

    charges = round(sum(charge_totals.values()), 2)
    head = (
        f"{len(parsed)} properties — rent {_money(rent_total)}, "
        f"charges {_money(charges)}."
    )
    if stated_income is None:
        return head
    if abs(stated_income - rent_total) < 0.01:
        return f"{head} Matches the statement's stated total income of {_money(stated_income)}."
    return (
        f"⚠ {head} The statement states total income of {_money(stated_income)}, a difference "
        f"of {_money(stated_income - rent_total)}. Check the rows below before uploading."
    )


# ----------------------------------------------------------------- layout fingerprint
# Two statements from one agent differ in every figure, every date and every property, and
# are otherwise the same document. A fingerprint has to survive all of that and still
# separate one agent from another, so it is taken from the parts that do NOT vary:
#
#   * the shape the parsers resolved the file to,
#   * the names of the columns, as a set (a portfolio that grew by a property must not
#     change the answer),
#   * the boilerplate — headings, labels, the covering note — with every number, date and
#     month name masked out.
#
# Addresses are excluded outright: they are the one piece of boilerplate that changes when
# the portfolio does. Nothing here reads the account's own data, so the fingerprint of a file
# is a property of the file.

_FP_MASKS = (
    re.compile(r"[£$€]\s?\(?-?\d[\d,]*(?:\.\d+)?\)?"),
    re.compile(r"\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}"),
    re.compile(r"\b(?:%s)[a-z]*\.?" % _MONTHS, re.IGNORECASE),
    re.compile(r"\d+"),
)
_FP_MAX_LINES = 60


def _fp_normalise(line: str) -> str:
    out = line
    for pattern in _FP_MASKS:
        out = pattern.sub("#", out)
    return re.sub(r"\s+", " ", out).strip().lower()


def fingerprint(lines: list["_DocLine"], shape: str) -> tuple[str, str]:
    """``(hash, sample)`` identifying this statement's LAYOUT.

    The sample is the masked text the hash was taken from, kept so a person can see why two
    files were treated as the same format — a hash alone is unarguable in the wrong way.
    """
    chrome: set[str] = set()
    columns: set[str] = set()
    for ln in lines:
        words = ln.words
        if not words:
            continue
        groups = _column_groups(words)
        if any(_cell_value(w) is not None and w.x0 >= groups[0].x1 for w in words):
            continue  # a row of figures: its labels vary with the portfolio
        if _is_column_header(groups):
            columns.update(_fp_normalise(g.name) for g in groups)
            continue
        address, _note = _split_address(groups[0].name)
        if address:
            continue
        norm = _fp_normalise(ln.text)
        if 3 <= len(norm) <= 90 and len(re.findall(r"[a-z]", norm)) >= 2:
            chrome.add(norm)

    body = sorted(chrome)[:_FP_MAX_LINES]
    sample = "\n".join([f"shape={shape}", "columns=" + " | ".join(sorted(columns)), *body])
    # The HASH is taken from the shape and the boilerplate only — not from the column names,
    # which are kept in the sample for a person to read. A column set grows the first time a
    # section that was empty has something in it (a month with a repair in it adds the expense
    # table's columns), and a fingerprint that changed for that reason would hand the operator
    # back a format they had already taught. The boilerplate does not move for that reason.
    digest = hashlib.sha256(("\n".join([f"shape={shape}", *body])).encode("utf-8"))
    return digest.hexdigest()[:32], sample


def sample_lines(sample: str) -> set[str]:
    """The boilerplate out of a stored sample, for comparing two layouts that did not hash
    alike. Drops the shape and column lines the sample carries for display."""
    return {
        ln
        for ln in sample.splitlines()[2:]
        if ln.strip()
    }


def layout_similarity(a: str, b: str) -> float:
    """How alike two layouts' boilerplate is, 0..1.

    An agent that adds a line to its covering note has not become a different agent, and a
    fingerprint is all-or-nothing about exactly that. This is the second question asked when
    the hashes differ — see the format lookup in the import router, which says out loud when
    a format was matched this way rather than exactly.
    """
    sa, sb = sample_lines(a), sample_lines(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


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


def _fingerprinted(result: StatementFile, lines: list["_DocLine"]) -> StatementFile:
    result.fingerprint, result.sample = fingerprint(lines, result.format)
    return result


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

    One PDF is not always one statement, and a portfolio arrives in more than one shape.
    A rent roll lists every property on its own line (``format="rent_roll"``); a sectioned
    statement gives each property its own table (``format="sectioned"``). Both come back with
    one extraction per property. Everything else is ``format="single"`` with exactly one.
    Each multi-property parser returns None unless its shape is unmistakable, so an ordinary
    statement takes the same path it always has. Never touches the database.
    """
    text, tables, lines = _extract_doc(content, max_pages)

    # Most specific shape first. Each returns None unless its shape is unmistakable, so an
    # ordinary single-property statement still reaches _extract_single untouched.
    sectioned = _parse_sectioned(lines, text, known_properties, known_categories)
    if sectioned is not None:
        return _fingerprinted(sectioned, lines)

    roll = _parse_rent_roll(text, known_properties, known_categories)
    if roll is not None:
        return _fingerprinted(roll, lines)

    ex = _extract_single(
        text,
        tables,
        known_properties=known_properties,
        known_categories=known_categories,
        use_ollama=use_ollama,
        ollama_url=ollama_url,
        ollama_model=ollama_model,
    )
    return _fingerprinted(StatementFile(format="single", extractions=[ex]), lines)


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
