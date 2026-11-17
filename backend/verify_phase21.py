"""Verification of the totals check — does what was read add up to what the statement prints?

Run:  PYTHONPATH=.pydeps python3 verify_phase21.py

A parser that guesses a layout wrong does not fail; it produces plausible rows. The first
Lightning statement read as one property with its note lines as expenses, and nothing said so.
Statements print their own arithmetic, so that is the test, and its verdict is what holds a
file back for review (and, later, what calls in the AI fallback).

  1. A correctly read statement is "matched" — and every LABELLED total must agree.
  2. A misread one is "mismatch", even when its income happens to match (the Lightning case:
     only the expenditure total gave it away).
  3. A statement printing no totals is "unverified", not a false alarm.
  4. Negative-printed expenses are stored positive by the single-statement reader too (they
     used to RAISE NOI), and then match the printed totals by size.
  5. A rent-roll line that is only a period and an amount belongs to the property above it —
     it used to become a nameless "portfolio charge" spread over every property.
  6. The verdict reaches the API, per file.
"""
import os

from fastapi.testclient import TestClient

from app.main import app
from app.statements import extract_statements

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        failures.append(f"{label}{': ' + detail if detail else ''}")
        print(f"  FAIL  {label} {detail}")
    else:
        print(f"  ok    {label}")


def make_pdf(lines: list) -> bytes:
    """One page; a line is a string at the left margin or a list of ``(x, size, text)``."""

    def esc(s: str) -> str:
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    body, y = ["BT"], 770
    for line in lines:
        cells = [(40, 10, line)] if isinstance(line, str) else line
        for x, size, text in cells:
            body.append(f"/F1 {size} Tf 1 0 0 1 {x} {y} Tm ({esc(text)}) Tj")
        y -= 13 if cells[0][1] < 9 else 16
    body.append("ET")
    stream = "\n".join(body).encode("cp1252")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def read(lines: list):
    return extract_statements(make_pdf(lines), known_properties=[], known_categories=["Rent"], use_ollama=False)


def row(label, amount):
    return [(40, 10, label), (480, 10, amount)]


print("\n1. a correctly read statement matches")
good = read([
    "Statement of account  Period: October 2026",
    "90 Rawling Road, Gateshead",
    "Income",
    row("Rent for October", "600.00"),
    row("Total Income", "600.00"),
    "Expenditure",
    row("Management Fee", "60.00"),
    row("Gas Safety Certificate", "75.00"),
    row("Total Expenditure", "135.00"),
])
check("matched", good.check == "matched", f"{good.check} {good.check_notes}")
check("names both totals", any("income" in n for n in good.check_notes) and any("expenditure" in n for n in good.check_notes), str(good.check_notes))

print("\n2. a misread statement is flagged even when income matches")
# The reader takes the money-bearing note line as a second expense: income still adds up,
# expenditure doesn't.
misread = read([
    "Statement of account  Period: October 2026",
    "90 Rawling Road, Gateshead",
    "Income",
    row("Rent for October", "600.00"),
    row("Total Income", "600.00"),
    "Expenditure",
    row("Management Fee", "60.00"),
    row("Net value 50.00 VAT", "10.00"),
    row("Total Expenditure", "60.00"),
])
check("mismatch", misread.check == "mismatch", f"{misread.check} {misread.check_notes}")
check("says which total disagrees", any("expenditure: read" in n for n in misread.check_notes), str(misread.check_notes))
check("and that income was fine", any(n.startswith("income") and "✓" in n for n in misread.check_notes), str(misread.check_notes))

print("\n3. nothing printed to check against")
bare = read([
    "Statement  Period: October 2026",
    "90 Rawling Road, Gateshead",
    "Income",
    row("Rent for October", "600.00"),
    "Expenditure",
    row("Management Fee", "60.00"),
])
check("unverified, not a false alarm", bare.check == "unverified", f"{bare.check} {bare.check_notes}")

print("\n4. negative-printed expenses (US owner statement style)")
neg = read([
    "Owner Statement  Period: October 2026",
    "123 Any Street",
    "Income",
    row("Rent Income", "2,775.00"),
    row("Total Income", "2,775.00"),
    "Expense",
    row("Pool Services", "-110.00"),
    row("Management", "-166.50"),
    row("Gardening", "-90.00"),
    row("Total Expense", "-366.50"),
    row("Net Income", "2,408.50"),
])
amounts = {r["category"]: r["amount"] for e in neg.extractions for r in e.rows}
check("expenses stored positive", all(v > 0 for k, v in amounts.items() if k != "Rent Income"), str(amounts))
check("and the totals match (net included)", neg.check == "matched" and len(neg.check_notes) == 3, f"{neg.check} {neg.check_notes}")

print("\n5. a rent-roll continuation line")
roll = read([
    "125 Allendale Road 24/7/26 to 28/7/26 101.34",
    "29/6/26 to 28/8/26 697",
    "127 Allendale Road 24/7/26 to 25/8/26 200.53",
    "147 Allendale Road 24/7/26 to 9/8/26 269.98",
    "Total Collected 1268.85",
    "Mngt Fee 177.64",
    "Balance Due 1091.21",
])
by = {e.property_name: e for e in roll.extractions}
rent125 = next((r["amount"] for r in by["125 Allendale Road"].rows if r["kind"] == "rent"), None) if "125 Allendale Road" in by else None
check("rent roll", roll.format == "rent_roll", roll.format)
check("the 697 line is 125's rent (101.34 + 697)", rent125 == 798.34, str(rent125))
cats = {r["category"] for e in roll.extractions for r in e.rows}
check("no nameless portfolio charge", "" not in cats, str(cats))
check("collected and balance-due both match", roll.check == "matched" and len(roll.check_notes) == 2, f"{roll.check} {roll.check_notes}")

print("\n6. the verdict reaches the API")
client = TestClient(app)
tok = client.post("/auth/login", data={"username": "admin@example.com", "password": "admin12345"}).json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}
r = client.post(
    "/import/statement/extract-batch",
    headers=H,
    files=[
        ("files", ("misread.pdf", make_pdf([
            "Statement of account  Period: October 2026", "90 Rawling Road, Gateshead", "Income",
            row("Rent for October", "600.00"), row("Total Income", "600.00"), "Expenditure",
            row("Management Fee", "60.00"), row("Net value 50.00 VAT", "10.00"),
            row("Total Expenditure", "60.00"),
        ]), "application/pdf")),
    ],
)
note = r.json()["files"][0] if r.status_code == 200 else {}
check("file note carries check=mismatch", note.get("check") == "mismatch", str(note.get("check")))
check("...with its notes", bool(note.get("check_notes")), str(note.get("check_notes")))

# ---- the real files, when they're on this machine ------------------------------------------
REAL = "/mnt/c/Users/danie/Downloads/lightning_stmt_20261103125633_1344218522.pdf"
if os.path.exists(REAL):
    print("\nreal Lightning statement")
    rr = extract_statements(open(REAL, "rb").read(), known_properties=[], known_categories=["Rent"], use_ollama=False)
    check("ledger parse matches all three totals", rr.check == "matched" and len(rr.check_notes) == 3, f"{rr.check} {rr.check_notes}")

print()
if failures:
    print(f"FAILED ({len(failures)})")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("PASSED")
