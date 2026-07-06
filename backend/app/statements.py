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

import pdfplumber
from dateutil import parser as dateparser

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
    """Parse a PDF statement into raw rows. Tries the local LLM first when available, then
    always falls back to the pdfplumber heuristic. Never touches the database."""
    text, tables = _extract_text_and_tables(content, max_pages)

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
