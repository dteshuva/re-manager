"""Verification of dated-ledger portfolio statements (Lightning Estates' layout).

Run:  PYTHONPATH=.pydeps python3 verify_phase19.py

The shape: an address heading per property, then an "Income" and an "Expenditure" list of
DATED entries (amount at the right edge) with small indented note lines under each, a bare
subtotal per section, then "Payment Made To You", "Total Income", "Total Expenditure", and
pages of invoice copies. Before this parser the file read as ONE property, the entry dates
became part of every category name, the note lines ("Net value £62.73, VAT @ 20% : £12.55")
became expenses, and the payout was imported as an operating cost.

Properties proved, each a way to post wrong money:

  1. Every entry reaches its own property; note lines never become amounts.
  2. The date is not part of the category; "Rent Received For Period …" is "Rent".
  3. Expenditure, printed negative, is stored positive (NOI subtracts it).
  4. The payout, the totals and everything after them (invoice copies) are never imported.
  5. Month: rent and the fee charged on it go to the month the tenancy period ENDS in (how
     this portfolio's history was posted; "mostly covers" double-books a mid-month tenancy).
     A period starting weeks AFTER the payment is a mislabel (the agent printed 125 Allendale's
     periods a month late; the owner confirms the rents were October's and November's), so it
     is moved back and reported. Entries with no period post to their own date.
  6. One property spanning two months becomes two statements.
  7. The read figures reconcile with the statement's own totals, and a mismatch is reported.
  8. The other shapes are untouched: a two-property sectioned file still reads as sectioned.

Parser-level and DB-free, so it runs without a server; the batch endpoint fans these out
exactly as it does a rent roll (one item per extraction).
"""
import os
from datetime import date

from app.statements import extract_statements

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        failures.append(f"{label}{': ' + detail if detail else ''}")
        print(f"  FAIL  {label} {detail}")
    else:
        print(f"  ok    {label}")


# ---- a PDF laid out like the real one ----------------------------------------------------
# The parser tells an entry from a note by x position (date at the margin, notes at the
# description column), so each piece is placed at its own x and size, on its own page.

MARGIN, DESC, AMT = 40, 108, 520


def make_pdf(pages: list[list[tuple[int, float, str]]]) -> bytes:
    """``pages`` is a list of pages; a page is a list of ``(x, size, text)`` lines."""

    def esc(s: str) -> str:
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    def line_ops(cells, y):
        return [f"/F1 {size} Tf 1 0 0 1 {x} {y} Tm ({esc(t)}) Tj" for x, size, t in cells]

    objs: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]  # pages filled below
    font_id = 3
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    kids = []
    for page in pages:
        body = ["BT"]
        y = 770
        for row in page:
            cells = row if isinstance(row, list) else [row]
            body += line_ops(cells, y)
            y -= 13 if cells[0][1] < 9 else 16
        body.append("ET")
        stream = "\n".join(body).encode("cp1252")
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        content_id = len(objs)
        objs.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>" % (font_id, content_id)
        )
        kids.append(len(objs))
    objs[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % k for k in kids),
        len(kids),
    )
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def heading(t):
    return (MARGIN, 10, t)


def entry(d, label, amount):
    return [(MARGIN, 10, d), (DESC, 10, label), (AMT, 10, amount)]


def note(t):
    return (DESC, 8, t)


def subtotal(amount):
    return (AMT, 10, amount)


HEADER = [(230, 12, "Lightning Estates"), (200, 10, "Office 605 Aidan House, Sunderland Road NE8 3HU")]

page1 = [
    *HEADER,
    heading("Account Statement"),
    heading("Statement Date 03/11/2026 12:25 Statement No."),
    heading("125 Allendale Road, Byker, Newcastle upon Tyne, Tyne and Wear, NE6 2SY"),
    heading("Income"),
    entry("29/09/2026", "Rent Received For Period 29.10.2026 - 28.11.2026 (Anne Tindale)", "£697.00"),
    entry("29/10/2026", "Rent Received For Period 29.11.2026 - 30.12.2026 (Anne Tindale)", "£697.00"),
    subtotal("£1,394.00"),
    heading("Expenditure"),
    entry("29/09/2026", "Management fee", "-£75.28"),
    note("9% of Rent Received"),
    note("Net value £62.73, VAT @ 20% : £12.55"),
    note("29.10.2026 to 28.11.2026"),
    entry("23/10/2026", "Maintenance & Repairs", "-£42.00"),
    note("Gas Services North East"),
    note("Invoice raised for work order, Ref No : WO.635.4121"),
    entry("29/10/2026", "Management fee", "-£75.28"),
    note("9% of Rent Received"),
    note("Net value £62.73, VAT @ 20% : £12.55"),
    note("29.11.2026 to 30.12.2026"),
    subtotal("-£192.56"),
    heading("24 Chatsworth Gardens, Byker, Newcastle upon Tyne, Tyne and Wear, NE6 2TP"),
    heading("Income"),
    (10, 8, "Lightning Estates 1"),  # page footer, as the page break falls mid-block
]
page2 = [
    *HEADER,  # repeated header across the page break: must not become a note or a property
    entry("16/10/2026", "Rent Received For Period 06.10.2026 - 05.11.2026 (Robert Allen)", "£700.00"),
    subtotal("£700.00"),
    heading("Expenditure"),
    entry("16/10/2026", "Management fee", "-£75.60"),
    note("9% of Rent Received"),
    note("Net value £63.00, VAT @ 20% : £12.60"),
    note("06.10.2026 to 05.11.2026"),
    entry("24/09/2026", "Selective licence fee (Part A)", "-£250.02"),
    note("Selective licence Part A initial fee paid to Newcastle City Council on 24.09.2026."),
    subtotal("-£325.62"),
    entry("03/11/2026", "Payment Made To You", "£1,575.82"),
    [(380, 10, "Total Income"), (AMT, 10, "£2,094.00")],
    [(380, 10, "Total Expenditure"), (AMT, 10, "-£518.18")],
    heading("Our Invoice"),
    [(MARGIN, 10, "03/11/2026"), (330, 10, "£335.59"), (420, 10, "£67.13"), (AMT, 10, "£402.72")],
]
page3 = [  # an invoice copy: dated, money-bearing, and NOT an entry
    *HEADER,
    heading("Invoice"),
    entry("29/09/2026", "Management fee (9%) 29.10.2026 to 28.11.2026", "£75.28"),
    heading("125 Allendale Road, Byker, Newcastle upon Tyne, Tyne and Wear, NE6 2SY"),
]

KNOWN_PROPS = ["125 Allendale Rd, Gateshead", "24 Chatsworth Gdns"]
KNOWN_CATS = ["Rent", "Property Management", "Maintenance & Repairs"]

r = extract_statements(
    make_pdf([page1, page2, page3]),
    known_properties=KNOWN_PROPS,
    known_categories=KNOWN_CATS,
    use_ollama=False,
)
by = {(e.property_name, e.month): e for e in r.extractions}


def rows(prop, month):
    e = by.get((prop, month))
    return {row["category"]: row for row in e.rows} if e else {}


print("shape")
check("read as a ledger", r.format == "ledger", r.format)
check(
    "addresses match stored names despite the district/town suffix",
    {e.property_name for e in r.extractions} == set(KNOWN_PROPS),
    str({e.property_name for e in r.extractions}),
)

print("months (property 5, 6)")
OCT, NOV = date(2026, 10, 1), date(2026, 11, 1)
SEP = date(2026, 9, 1)
check(
    "125: mislabelled periods moved back -> Oct and Nov",
    set(m for p, m in by if p.startswith("125")) == {OCT, NOV},
    str(sorted(m for p, m in by if p.startswith("125"))),
)
oct125 = rows("125 Allendale Rd, Gateshead", OCT)
nov125 = rows("125 Allendale Rd, Gateshead", NOV)
check("125 Oct: rent 697", oct125.get("Rent", {}).get("amount") == 697.0, str(oct125))
check("125 Nov: rent 697", nov125.get("Rent", {}).get("amount") == 697.0, str(nov125))
check(
    "the fee follows its rent's (corrected) month",
    oct125.get("Property Management", {}).get("amount") == 75.28
    and nov125.get("Property Management", {}).get("amount") == 75.28,
)
check("repair with no period posts to its own date (Oct)", oct125.get("Maintenance & Repairs", {}).get("amount") == 42.0)
check(
    "the correction is reported, on the card it affects",
    any("starts 1 month after the payment" in w for w in by[("125 Allendale Rd, Gateshead", OCT)].warnings),
)
check(
    "a normal period is not 'corrected'; 06/10-05/11 posts to the month it ends (Nov)",
    rows("24 Chatsworth Gdns", NOV).get("Rent", {}).get("amount") == 700.0
    and not any("after the payment" in w for e in r.extractions if e.property_name.startswith("24") for w in e.warnings),
)
check("licence fee dated 24/09 posts to Sep", rows("24 Chatsworth Gdns", SEP).get("Selective licence fee (Part A)", {}).get("amount") == 250.02)

print("figures (1-4)")
all_rows = [row for e in r.extractions for row in e.rows]
cats = {row["category"] for row in all_rows}
check("no date in any category", not any(c[:2].isdigit() for c in cats), str(cats))
check("no note line became a row", not any("net value" in c.lower() or "invoice" in c.lower() for c in cats), str(cats))
check("expenses are positive", all(row["amount"] > 0 for row in all_rows if row["kind"] == "line"))
check("the payout is not a row", not any("payment" in c.lower() for c in cats))
check(
    "nothing from the invoice pages was read (exactly two 75.28 fees for 125)",
    sum(1 for row in all_rows if row["amount"] == 75.28) == 2,
)
check(
    "raw label is stable month to month (no period, no tenant)",
    {row["raw_category"] for row in all_rows if row["category"] == "Rent"} == {"Rent Received"},
)

print("reconciliation (7)")
check("matches the statement's totals", "matches the statement" in r.warnings[0], r.warnings[0])

bad = make_pdf([[p if p != subtotal("£700.00") else p for p in page1], [
    *[x for x in page2 if not (isinstance(x, list) and x[0][2] == "Total Income")],
    [(380, 10, "Total Income"), (AMT, 10, "£2,194.00")],
]])
rb = extract_statements(bad, known_properties=KNOWN_PROPS, known_categories=KNOWN_CATS, use_ollama=False)
check("a total that doesn't add up is flagged", any(w.startswith("⚠") and "total income" in w for w in rb.warnings), str(rb.warnings))

print("other shapes untouched (8)")
COLS = (42, 250, 310, 370, 430, 487)
hdr = [(x, 9, t) for x, t in zip(COLS, ("Property / Unit", "Income", "Expenses", "Mgt Fee", "Mgt VAT", "Total Due"))]


def srow(*cells):
    return [(x, 9, t) for x, t in zip(COLS, cells)]


sectioned = make_pdf([[
    (42, 9, "64 King Edward Street Gateshead NE8 3PR"), hdr,
    srow("Flat 1", "650.00", "0.00", "-78.00", "-15.60", "556.40"),
    srow("Property Subtotal", "650.00", "0.00", "-78.00", "-15.60", "556.40"),
    (42, 9, "209 Windsor Terrace Gateshead NE8 1TU"), hdr,
    srow("Flat 1", "700.00", "-40.00", "-84.00", "-16.80", "559.20"),
    srow("Property Subtotal", "700.00", "-40.00", "-84.00", "-16.80", "559.20"),
]])
rs = extract_statements(sectioned, known_properties=[], known_categories=[], use_ollama=False)
check("a sectioned statement still reads as sectioned", rs.format == "sectioned", rs.format)

# ---- the real file, when it's on this machine ----------------------------------------------
REAL = "/mnt/c/Users/danie/Downloads/lightning_stmt_20261103125633_1344218522.pdf"
if os.path.exists(REAL):
    print("real Lightning statement")
    rr = extract_statements(
        open(REAL, "rb").read(),
        known_properties=["125 Allendale Rd", "127 Allendale Rd", "145 Allendale Rd", "147 Allendale Rd",
                          "151 Allendale Rd", "173 Allendale Rd", "22 Chatsworth Gardens", "24 Chatsworth Gardens"],
        known_categories=KNOWN_CATS,
        use_ollama=False,
    )
    check("ledger, 8 properties", rr.format == "ledger" and len({e.property_name for e in rr.extractions}) == 8)
    check(
        "income, expenditure and payout all reconcile",
        all(s in rr.warnings[0] for s in ("3,728.81 ✓", "3,119.26 ✓", "609.55 ✓")),
        rr.warnings[0],
    )
    m125 = sorted(e.month for e in rr.extractions if e.property_name == "125 Allendale Rd" and any(x["category"] == "Rent" for x in e.rows))
    check("125 Allendale rents -> Oct and Nov 2026", m125 == [OCT, NOV], str(m125))
    # Mid-month tenancies (127: 06/10-05/11, 147: 14/10-13/11) must NOT land in October,
    # which the previous agent's roll already filled.
    rent_months = {(e.property_name, e.month) for e in rr.extractions if any(x["category"] == "Rent" for x in e.rows)}
    check(
        "127 and 147 rents -> Nov, not the October the old agent already posted",
        ("127 Allendale Rd", NOV) in rent_months and ("147 Allendale Rd", NOV) in rent_months
        and ("127 Allendale Rd", OCT) not in rent_months and ("147 Allendale Rd", OCT) not in rent_months,
        str(sorted(rent_months)),
    )

print()
if failures:
    print(f"FAILED ({len(failures)})")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("PASSED")
