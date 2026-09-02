"""Verification of multi-property rent rolls — one statement, twelve properties.

Run:  PYTHONPATH=.pydeps python3 verify_phase14.py

Every statement parser before this one assumed one PDF = one property: a header names the
building and every line below it is that building's money. A portfolio agent doesn't send
that. It sends ONE page with a line per property — address, the rent period it covers, the
rent collected — and then charges the whole portfolio once at the bottom before arriving at
the balance paid to the owner. Read with the old assumption the file is silently wrong in the
worst possible way: one property's name on the statement, twelve properties' money behind it.

So the properties worth proving are the ones where being approximately right is being wrong:

  1. **Every row reaches the right property.** A statement writes addresses the way a person
     does ("125 Allendale Road"), the database may hold them another way ("125 Allendale Rd,
     Gateshead"), and an address that matches nothing must be offered as new rather than
     guessed at or dropped.
  2. **The month is the month.** UK statements date day-first, and rent periods straddle the
     month end, so the month has to be read from the period rather than from the first number
     that parses. A statement posted a month out is invisible until a year-end.
  3. **A charge made once over the portfolio is split, and the split adds back.** The shares
     must sum to the charged figure to the penny — otherwise the statement's own arithmetic
     stops reconciling the moment it is imported.
  4. **Nothing that isn't money owed to a property gets imported as if it were.** Totals,
     sub-totals, balances and an unlabelled arrears column are the statement's arithmetic and
     its notes; each is either recognised or reported, and none becomes a line item.
  5. **Ordinary single-property statements are untouched.** The rent-roll path is only taken
     when the shape is unmistakable.

Self-contained: creates its own properties, writes to months after the seed's range, and
deletes everything it wrote.
"""
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
tok = client.post(
    "/auth/login", data={"username": "admin@example.com", "password": "admin12345"}
).json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        failures.append(f"{label}{': ' + detail if detail else ''}")
        print(f"  FAIL  {label} {detail}")
    else:
        print(f"  ok    {label}")


def cents(x) -> int:
    return int((Decimal(str(x)) * 100).to_integral_value())


def make_pdf(lines: list[str]) -> bytes:
    """A one-page PDF containing ``lines`` as ordinary text, written by hand.

    The statements under test have to be exactly known — down to which figure sits on which
    line — so this builds them rather than depending on a sample file or on a PDF-authoring
    library the project doesn't ship.
    """
    def esc(s: str) -> str:
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    body = ["BT", "/F1 11 Tf", "40 750 Td", "14 TL"]
    for i, line in enumerate(lines):
        if i:
            body.append("T*")
        body.append(f"({esc(line)}) Tj")
    body.append("ET")
    stream = "\n".join(body).encode("latin-1")

    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1,
        xref,
    )
    return bytes(out)


def extract(name: str, lines: list[str]) -> dict:
    r = client.post(
        "/import/statement/extract-batch",
        headers=H,
        files=[(  "files", (name, make_pdf(lines), "application/pdf"))],
    )
    assert r.status_code == 200, r.text[:300]
    return r.json()


# ---- fixtures ------------------------------------------------------------------------------
# Three of the statement's properties exist already, each stored with a DIFFERENT but
# equivalent spelling of its street, so matching has to fold "Road"/"Rd" and "Gdns"/"Gardens"
# rather than compare strings. The other two are new and must be offered for approval.
STORED = {
    "125 Allendale Rd, Gateshead": "125 Allendale Road",
    "9 Sandhoe Gardens": "9 Sandhoe Gdns",
    "24 Chatsworth Gdns": "24 Chatsworth Gdns",
    "64 King Edward Street": "64 King Edward St",
}
created: list[str] = []
for stored_name in STORED:
    r = client.post(
        "/properties", headers=H, json={"name": stored_name, "type": "single", "address": None}
    )
    assert r.status_code == 201, r.text[:200]
    created.append(r.json()["id"])
PROP_ID = dict(zip(STORED, created))


def cleanup() -> None:
    for pid in created:
        client.delete(f"/properties/{pid}", headers=H)


# The statement under test. Rents 697 + 500 + 700 + 725 + 600 + 0 = 3222, a 14% management
# fee of 451.08, and an 80.00 contractor bill — the sub-total 2770.92 and balance 2690.92 are
# the statement's own arithmetic, printed the way real ones print it (the sub-total's figure
# on its own line, ABOVE the words). "209 Windsor Ave" and "145 Allendale Road" are not in the
# database. "64 King Edward St" ends in the wrong YEAR — a real slip on a real statement.
ROLL = [
    "125 Allendale Road 29/8/26 to 28/9/26 697",
    "9 Sandhoe Gdns 9/8/26 to 8/9/26 500",
    "24 Chatsworth Gdns 24/7/26 to 23/8/26 700",
    "64 King Edward St 20/8/26 to 19/9/27 725",
    "209 Windsor Ave 17/8/26 to 16/9/26 600",
    "145 Allendale Road awaiting Benefit 0",
    "Total Collected 3222",
    "Mngt Fee 451.08",
    "2770.92",
    "Sub Total",
    "D&D GAS 80",
    "Balance Due 2690.92",
    "ARREARS",
    "1200",
]
MONTH = date(2026, 9, 1)

try:
    print("== 1. One file, one statement per property ==")
    res = extract("SEPT.pdf", ROLL)
    items = res["items"]
    note = next((f for f in res["files"] if f["filename"] == "SEPT.pdf"), None)
    check("the file is recognised as a rent roll", bool(note) and note["format"] == "rent_roll",
          str(note))
    check("it produces one statement per property (6)", len(items) == 6, str(len(items)))
    check("every statement is tagged with the PDF it was split from",
          all(i["source_file"] == "SEPT.pdf" for i in items),
          str([i["source_file"] for i in items]))
    check("each statement is labelled with its own property",
          all(i["preview"]["detected_property"] in i["filename"] for i in items),
          str([i["filename"] for i in items]))
    by_prop = {i["preview"]["detected_property"]: i["preview"] for i in items}

    print("\n== 2. Addresses resolve to the properties that exist, however they're spelt ==")
    for stored_name, on_statement in STORED.items():
        pv = by_prop.get(stored_name)
        check(f"“{on_statement}” matches the stored “{stored_name}”",
              pv is not None and pv["property_id"] == PROP_ID[stored_name],
              str(list(by_prop)))
    check("the two unknown addresses are offered as new, not guessed at",
          sorted(res["unknown_properties"]) == ["145 Allendale Road", "209 Windsor Ave"],
          str(res["unknown_properties"]))
    check("...and are flagged unknown on their own statements",
          all(by_prop[n]["property_unknown"] for n in ("209 Windsor Ave", "145 Allendale Road")))

    print("\n== 3. The month comes from the period, read day-first ==")
    check("every property posts to the statement's month, Sep 2026",
          {pv["detected_month"] for pv in by_prop.values()} == {"2026-09-01"},
          str({p: pv["detected_month"] for p, pv in by_prop.items()}))
    # 9/8/26 is only September if the dates are read month-first. They are UK dates: the
    # document's own 29/8/26 proves day-first, and that has to carry to the ambiguous ones.
    check("the ambiguous 9/8/26 was read as 9 August, not 8 September",
          not any("Sep" in w and "9 Sandhoe" in w for w in note["warnings"]))
    check("the period ending in the wrong YEAR is repaired, not believed",
          any("mistyped year" in w for w in note["warnings"])
          and not any("post it separately" in w for w in by_prop["64 King Edward Street"]["warnings"]),
          str(note["warnings"]))
    check("the one row whose period never touches Sep is flagged, and only that one",
          [p for p, pv in by_prop.items() if any("post it separately" in w for w in pv["warnings"])]
          == ["24 Chatsworth Gdns"])

    print("\n== 4. The portfolio charges are split, and the split adds back exactly ==")
    def shares(category: str) -> list[float]:
        return [
            r["amount"]
            for pv in by_prop.values()
            for r in pv["rows"]
            if r["category"] == category and r["kind"] == "allocated"
        ]

    fee = shares("Property Management")
    check("the management fee is folded into the existing category, not a new one",
          len(fee) > 0 and not any(r["category"] == "Mngt Fee" for pv in by_prop.values() for r in pv["rows"]),
          str([r["category"] for pv in by_prop.values() for r in pv["rows"]]))
    check("the fee's shares sum to the charged 451.08 to the penny",
          cents(sum(fee)) == cents(451.08), str(sum(fee)))
    check("the gas bill's shares sum to the charged 80.00 to the penny",
          cents(sum(shares("D&D GAS"))) == cents(80.00), str(sum(shares("D&D GAS"))))
    check("shares are pro-rata by rent (697/3222 of 451.08 = 97.58)",
          cents(next(r["amount"] for r in by_prop["125 Allendale Rd, Gateshead"]["rows"]
                     if r["kind"] == "allocated" and r["category"] == "Property Management"))
          == cents(97.58))
    check("a property that collected nothing carries none of the charge",
          [r["kind"] for r in by_prop["145 Allendale Road"]["rows"]] == ["rent"],
          str(by_prop["145 Allendale Road"]["rows"]))
    check("the rent rows are rent, and marked as read off the statement",
          all(any(r["kind"] == "rent" and r["classification"] == "rent" for r in pv["rows"])
              for pv in by_prop.values()))
    check("the statement's note against a property is kept, not dropped",
          any("awaiting Benefit" in (r["note"] or "") for r in by_prop["145 Allendale Road"]["rows"]),
          str(by_prop["145 Allendale Road"]["rows"]))

    print("\n== 5. The statement's arithmetic is checked, never imported ==")
    every_category = {r["category"] for pv in by_prop.values() for r in pv["rows"]}
    check("no total, sub-total or balance became a line item",
          not (every_category & {"Total Collected", "Sub Total", "Balance Due", "ARREARS"}),
          str(sorted(every_category)))
    check("the rents read are reconciled against the stated total",
          any("matches the statement" in w for w in note["warnings"]), str(note["warnings"]))
    check("the sub-total printed on its own line is recognised, not reported as lost money",
          not any("2,770.92" in w for w in note["warnings"]), str(note["warnings"]))
    check("the unlabelled arrears figure IS reported as not imported",
          any("1,200.00" in w and "ARREARS" in w for w in note["warnings"]), str(note["warnings"]))

    print("\n== 6. A wrong total is refused quietly agreeing with itself ==")
    bad = extract("BAD.pdf", [*ROLL[:6], "Total Collected 4000"])
    bad_note = bad["files"][0]
    check("a stated total that disagrees with the rows is called out",
          any("⚠" in w and "4,000.00" in w for w in bad_note["warnings"]), str(bad_note["warnings"]))

    print("\n== 7. The rows apply through the ordinary import core ==")
    pid = PROP_ID["125 Allendale Rd, Gateshead"]
    rows = by_prop["125 Allendale Rd, Gateshead"]["rows"]
    payload = [
        {
            "property_id": pid,
            "month": "2026-09-01",
            "category_id": r["category_id"],
            "classification": r["classification"],
            "amount": r["amount"],
        }
        for r in rows
        if r["category_id"]
    ]
    r = client.post("/import/rows", headers=H, json=payload)
    check("the reviewed rows import", r.status_code in (200, 201) and r.json()["committed"],
          r.text[:200])
    r = client.get("/records", headers=H, params={"property_id": pid, "month": str(MONTH)})
    stored_rows = r.json()[0]["line_items"] if r.status_code == 200 and r.json() else []
    got = {li["category_id"]: li["amount"] for li in stored_rows}
    check("the rent landed on the property, in September",
          cents(got.get(rows[0]["category_id"], 0)) == cents(697.00), str(got))
    check("so did its share of the management fee",
          any(cents(v) == cents(97.58) for v in got.values()), str(got))
    r = client.post("/import/rows", headers=H, json=payload)
    r = client.get("/records", headers=H, params={"property_id": pid, "month": str(MONTH)})
    again = {li["category_id"]: li["amount"] for li in r.json()[0]["line_items"]}
    check("re-importing the same statement overwrites rather than doubles", again == got,
          f"{got} -> {again}")

    print("\n== 8. An ordinary single-property statement is untouched by any of this ==")
    single = extract("March.pdf", [
        "Statement of account",
        "Date: 09/03/2026",
        "Income",
        "125 Allendale Road",
        "Rent for period 27/02/2026 - 26/03/2026 from Megan ONeill 512.00 - 512.00",
        "Total Income 512.00",
        "Expenditure",
        "Management Fee at 10% 35308 51.20 0.00 51.20",
        "Total Expenditure 51.20",
    ])
    check("it is read as a single statement", single["files"][0]["format"] == "single",
          str(single["files"][0]))
    check("...producing exactly one, untagged", len(single["items"]) == 1
          and single["items"][0]["source_file"] is None)
    sp = single["items"][0]["preview"]
    check("...for the property it names, in its own month",
          sp["property_id"] == PROP_ID["125 Allendale Rd, Gateshead"] and sp["detected_month"] == "2026-03-01",
          f"{sp['detected_property']} / {sp['detected_month']}")
    check("...with its rent and fee, and no totals",
          sorted((r["category"], r["amount"]) for r in sp["rows"])
          == [("Management Fee at 10%", 51.2), ("Rent", 512.0)], str(sp["rows"]))

finally:
    cleanup()

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("verify_phase14: ALL CHECKS PASSED")
