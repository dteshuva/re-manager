"""Verification of sectioned portfolio statements and saved statement formats.

Run:  PYTHONPATH=.pydeps python3 verify_phase16.py

Two things are proved here, and they are the two halves of the same problem: a statement
arrives in a layout nothing has seen before, and it must (a) be read correctly anyway and
(b) not have to be corrected by hand again next month.

**The shape.** Both multi-property parsers before this one read a statement as TEXT — a rent
roll puts each property on one line with its rent at the end, and reading the line is enough.
An agent running on statement software sends the same portfolio as a sequence of tables: an
address as a section heading, column names under it, the figures, a "Property Subtotal"
closing each block. Read as text that file is silently wrong in the worst direction — the
address carries no amount so the rent roll declines it, and the single-property path then
takes the FIRST address as the whole file's property and one total as the only line item.
Three properties collapse into one and every rent and fee is lost.

So the properties worth proving are the ones where being approximately right is being wrong:

  1. **Every figure reaches the right property and the right column.** Columns are matched
     geometrically, so "Mgt Fee" is one column rather than two and a row with no label at all
     is still read.
  2. **Expenses end up positive.** NOI is rent MINUS operating. A fee imported as the
     "-£192.50" a statement prints would RAISE NOI by the fee — an error that looks like
     good news and survives until a year end. Both sign conventions are tested.
  3. **The statement's own arithmetic is never imported.** Subtotals, grand totals and a
     "Total Due" column are the statement checking itself; they reconcile what was read and
     never become line items.
  4. **One month for the whole statement**, with the other candidate month reported rather
     than silently chosen.

**The memory.** A parser cannot know that this sender's "Mgt Fee" is this account's
"Management Fee", or that "64 King Edward Street" is the property stored as "64 King Edward
St, Gateshead". That is a human answer, so it is saved against a fingerprint of the layout
and applied to the next statement from the same sender:

  5. **A fingerprint survives a new month and separates two senders.** Different dates,
     different figures, a property bought since — same format. Different boilerplate —
     different format.
  6. **What was taught is what comes back**, including a classification the operator
     overrode and a decision to post to the tenancy month instead.
  7. **Re-teaching merges**, so a month that corrects one new charge doesn't drop everything
     learned before it.
  8. **A layout that has drifted is still recognised**, and says so rather than passing as an
     exact match.
  9. **Nothing is trusted on the way in.** An id that isn't this account's is refused.

Self-contained: creates its own properties and formats, writes no line items, and deletes
everything it wrote.
"""
from datetime import date

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


# ---- a PDF with real columns ---------------------------------------------------------------
# The parser under test reads COLUMNS, which is a question about x coordinates, so a fixture
# that writes plain text lines would not exercise it at all. Each cell is therefore placed at
# its own x with its own text matrix, exactly as statement software lays one out.

COLS = (42, 250, 310, 370, 430, 487, 540)  # label, then six figure columns


def make_pdf(rows: list, size: int = 9, top: int = 770, leading: int = 15) -> bytes:
    """``rows`` is a list of lines; a line is either a string (placed at the left margin) or
    a list of ``(x, text)`` cells."""

    def esc(s: str) -> str:
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    body = ["BT", f"/F1 {size} Tf"]
    y = top
    for row in rows:
        cells = [(COLS[0], row)] if isinstance(row, str) else row
        for x, txt in cells:
            body.append(f"1 0 0 1 {x} {y} Tm ({esc(str(txt))}) Tj")
        y -= leading
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


def cells(label: str, *values) -> list:
    return [(COLS[0], label)] + [(COLS[i + 1], v) for i, v in enumerate(values)]


def extract(name: str, rows: list) -> dict:
    r = client.post(
        "/import/statement/extract-batch",
        headers=H,
        files=[("files", (name, make_pdf(rows), "application/pdf"))],
    )
    assert r.status_code == 200, r.text[:300]
    return r.json()


# ---- the statement under test ---------------------------------------------------------------
# Modelled on a real managing agent's layout. Three properties, each with its own table; a
# management fee stated per property; an income detail section giving the tenancy periods;
# and the statement's own grand totals, which the parser must reconcile against and must not
# import. The rent periods run 19th-18th, so they fall mostly in OCTOBER while the statement
# declares a period that falls mostly in SEPTEMBER — the disagreement is the point.

HEADERS = ("Income", "Expenses", "Mgt Fee", "Mgt VAT", "Other", "Total Due")
DETAIL_HEADERS = ("B/F", "Demanded", "Received", "C/F")

STORED = {
    # stored spelling                   as the statement prints it
    "64 King Edward St, Gateshead": "64 King Edward Street Gateshead NE8 3PR",
    "125 Westbourne Ave, Gateshead": "125 Westbourne Avenue Gateshead NE8 4NQ",
    "209 Windsor Ave, Gateshead": "209 Windsor Avenue Gateshead NE8 4NY",
}
RENTS = {"64": 725.00, "125": 600.00, "209": 600.00}
FEES = {"64": 72.50, "125": 60.00, "209": 60.00}
PERIODS = {"64": "19/09/26 - 18/10/26", "125": "20/09/26 - 19/10/26", "209": "19/09/26 - 18/10/26"}


def money(x: float, negate: bool = False) -> str:
    return f"-£{abs(x):,.2f}" if negate and x else f"£{x:,.2f}"


def statement(
    *,
    sender: str = "SHELL MANAGEMENT",
    number: str = "#0010",
    stated_date: str = "1 Oct 2026",
    period: str = "14 Sep 2026 - 1 Oct 2026",
    rents: dict | None = None,
    fees: dict | None = None,
    negative_expenses: bool = False,
    stated_income: float | None = None,
    extra_note: str | None = None,
) -> list:
    """The statement, with every part a later test needs to vary exposed as an argument."""
    rents = rents or RENTS
    fees = fees or FEES
    rows: list = [
        "Landlord Statement",
        f"Statement Date {stated_date}",
        f"Statement Period {period}",
        f"Statement No. {number}",
        "Statement For",
        sender,
        "Note for you",
        "Please find enclosed your management statement.",
    ]
    if extra_note:
        rows.append(extra_note)

    rows.append("Income/Expenses per Property")
    for key, printed in zip(rents, STORED.values()):
        rows += [
            printed,
            cells("Property / Unit", *HEADERS),
            # The row that carries the figures has NO label of its own — the case pdfplumber's
            # own table extraction drops and a text parser cannot see.
            cells(
                "",
                money(rents[key]),
                money(0),
                money(fees[key], negative_expenses),
                money(0),
                money(0),
                money(rents[key] - fees[key]),
            ),
            cells(
                "Property Subtotal",
                money(rents[key]),
                money(0),
                money(fees[key], negative_expenses),
                money(0),
                money(0),
                money(rents[key] - fees[key]),
            ),
        ]
    total_rent = stated_income if stated_income is not None else sum(rents.values())
    rows.append(
        cells(
            "Statement Grand Totals",
            money(total_rent),
            money(0),
            money(sum(fees.values()), negative_expenses),
            money(0),
            money(0),
            money(sum(rents.values()) - sum(fees.values())),
        )
    )

    rows.append("Total Income")
    for key, printed in zip(rents, STORED.values()):
        rows += [
            printed,
            [(COLS[0], "Tenant")] + [(COLS[i + 2], h) for i, h in enumerate(DETAIL_HEADERS)],
            [(COLS[0], f"Rent {PERIODS[key]}")]
            + [
                (COLS[2], "-"),
                (COLS[3], money(rents[key])),
                (COLS[4], money(rents[key])),
                (COLS[5], "-"),
            ],
            [(COLS[0], "Property Subtotal")]
            + [
                (COLS[2], money(0)),
                (COLS[3], money(rents[key])),
                (COLS[4], money(rents[key])),
                (COLS[5], money(0)),
            ],
        ]
    return rows


# ---- fixtures --------------------------------------------------------------------------------
created: list[str] = []


def cleanup() -> None:
    for pid in created:
        client.delete(f"/properties/{pid}", headers=H)
    for fid in saved_formats:
        client.delete(f"/import/statement/formats/{fid}", headers=H)


saved_formats: list[str] = []


def preclean() -> None:
    """Leave no trace of an earlier run of this script.

    Several checks below assert that a layout is UNKNOWN, which a format left behind by a
    previous run would quietly turn into a pass of the wrong thing.
    """
    for fmt in client.get("/import/statement/formats", headers=H).json():
        if fmt["label"] in {"Shell Management", "Forged"}:
            client.delete(f"/import/statement/formats/{fmt['id']}", headers=H)
    for prop in client.get("/properties", headers=H).json():
        if prop["name"] in STORED:
            client.delete(f"/properties/{prop['id']}", headers=H)


preclean()


# ============================================================================================
# 1. The shape: three properties, read before any of them exists in the database
# ============================================================================================
print("\n1. A sectioned portfolio statement is recognised as one")
base = extract("shell-0010.pdf", statement())
note = base["files"][0]
check("the file is read as a sectioned portfolio statement", note["format"] == "sectioned", note["format"])
check("it produced one statement per property", note["statements"] == 3, str(note["statements"]))
check("a fingerprint was taken", len(note["fingerprint"]) >= 16)
check("no format is claimed for a layout never taught", note["format_label"] is None)
check(
    "an untaught layout says so",
    any("haven't taught" in w for w in note["warnings"]),
    "; ".join(note["warnings"])[:160],
)
check(
    "the rents read add back to the statement's own stated total",
    any("Matches the statement's stated total" in w for w in note["warnings"]),
    "; ".join(note["warnings"])[:200],
)

by_raw = {it["preview"]["raw_property"]: it["preview"] for it in base["items"]}
check("each property is offered under the address the statement printed", len(by_raw) == 3, str(list(by_raw)))
first = by_raw.get("64 King Edward Street Gateshead NE8 3PR")
check("the first property was found", first is not None)

if first:
    cats = {r["category"]: r for r in first["rows"]}
    check("it has exactly two rows — the rent and the fee", len(first["rows"]) == 2, str(list(cats)))
    rent = next((r for r in first["rows"] if r["kind"] == "rent"), None)
    fee = next((r for r in first["rows"] if r["kind"] != "rent"), None)
    check("the rent is the figure in the Income column", rent and rent["amount"] == 725.00, str(rent and rent["amount"]))
    check("the rent is classified as rent", rent and rent["classification"] == "rent")
    check("the fee is the figure in the Mgt Fee column", fee and fee["amount"] == 72.50, str(fee and fee["amount"]))
    check("the fee is an operating expense", fee and fee["classification"] == "operating")
    check(
        "the fee is POSITIVE, because NOI subtracts operating expenses",
        fee and fee["amount"] > 0,
        str(fee and fee["amount"]),
    )
    check(
        "the statement's own word for the fee is kept, for teaching",
        fee and fee["raw_category"] == "Mgt Fee",
        str(fee and fee["raw_category"]),
    )

every_row = [r for it in base["items"] for r in it["preview"]["rows"]]
check("nothing was imported twice", len(every_row) == 6, str(len(every_row)))
check(
    "no subtotal, grand total or Total Due column became a line item",
    not [r for r in every_row if "total" in r["category"].lower() or "due" in r["category"].lower()],
    str([r["category"] for r in every_row]),
)
check(
    "the £652.50 'Total Due' column is nowhere in the rows",
    not [r for r in every_row if abs(r["amount"] - 652.50) < 0.01],
)
check(
    "every property's rent was read",
    sorted(r["amount"] for r in every_row if r["kind"] == "rent") == [600.0, 600.0, 725.0],
    str(sorted(r["amount"] for r in every_row if r["kind"] == "rent")),
)

# ============================================================================================
# 2. The month: one for the whole statement, the other candidate reported
# ============================================================================================
print("\n2. The month is the statement's own period, and the disagreement is reported")
months = {it["preview"]["detected_month"] for it in base["items"]}
check("every property posts to the same month", months == {"2026-09-01"}, str(months))
check(
    "the tenancy periods pointing at another month are reported, once for the file",
    sum(1 for w in note["warnings"] if "Oct 2026" in w) == 1,
    "; ".join(note["warnings"])[:240],
)

# ============================================================================================
# 3. The other sign convention
# ============================================================================================
print("\n3. A statement that prints its charges as negatives")
neg = extract("shell-neg.pdf", statement(negative_expenses=True))
neg_fees = [r for it in neg["items"] for r in it["preview"]["rows"] if r["kind"] != "rent"]
check(
    "charges printed as -£72.50 are still imported as positive expenses",
    neg_fees and all(r["amount"] > 0 for r in neg_fees),
    str([r["amount"] for r in neg_fees]),
)
check(
    "and the flip is said out loud",
    any("negative amounts" in w for w in neg["files"][0]["warnings"]),
    "; ".join(neg["files"][0]["warnings"])[:160],
)

# ============================================================================================
# 4. Reconciliation catches a misread
# ============================================================================================
print("\n4. Figures that don't add up to the statement's own total are flagged")
bad = extract("shell-bad.pdf", statement(stated_income=2_000.00))
check(
    "a mismatch against the stated total is raised before anything is posted",
    any(w.startswith("⚠") and "2,000.00" in w for w in bad["files"][0]["warnings"]),
    "; ".join(bad["files"][0]["warnings"])[:240],
)


# ============================================================================================
# 5. The fingerprint: survives a new month, separates two senders
# ============================================================================================
print("\n5. A layout is recognised across months and not confused with another sender")
fp_base = note["fingerprint"]
next_month = extract(
    "shell-0011.pdf",
    statement(
        number="#0011",
        stated_date="1 Nov 2026",
        period="1 Oct 2026 - 1 Nov 2026",
        rents={"64": 725.00, "125": 625.00, "209": 600.00},
        fees={"64": 72.50, "125": 62.50, "209": 60.00},
    ),
)
check(
    "next month's statement — new dates, new figures, new statement number — fingerprints alike",
    next_month["files"][0]["fingerprint"] == fp_base,
    f"{next_month['files'][0]['fingerprint'][:12]} vs {fp_base[:12]}",
)

other_sender = extract(
    "other-agent.pdf",
    statement(sender="NORTHERN LETTINGS LLP")[0:6]
    + ["Owner Remittance Advice", "Collected on your behalf"]
    + statement()[8:],
)
check(
    "a different sender's boilerplate fingerprints differently",
    other_sender["files"][0]["fingerprint"] != fp_base,
)

# ============================================================================================
# 6. Teaching the format
# ============================================================================================
print("\n6. What the operator corrects once is applied next time")
for stored in STORED:
    r = client.post(
        "/properties", headers=H, json={"name": stored, "type": "single", "address": None}
    )
    assert r.status_code == 201, r.text[:200]
    created.append(r.json()["id"])
prop_id = dict(zip(STORED, created))

cats = {c["name"]: c["id"] for c in client.get("/categories", headers=H).json()}
# Deliberately NOT the category the parser would pick for "Mgt Fee" on its own (it folds it
# into "Property Management"), so a pass can only mean the saved alias did the work.
alias_target = "Repairs & Maintenance"
check("the test's target category exists", alias_target in cats, str(sorted(cats))[:160])

body = {
    "fingerprint": fp_base,
    "sample": note["sample"],
    "label": "Shell Management",
    "shape": "sectioned",
    "categories": [
        {"raw": "Mgt Fee", "target_id": cats[alias_target], "classification": "capex"},
    ],
    "properties": [
        {"raw": printed, "target_id": prop_id[stored]} for stored, printed in STORED.items()
    ],
    "month_rule": "rent_period",
}
r = client.post("/import/statement/formats", headers=H, json=body)
check("the format saves", r.status_code == 201, r.text[:200])
if r.status_code == 201:
    saved_formats.append(r.json()["id"])
    out = r.json()
    check(
        "it reads back as names, not as ids",
        out["category_aliases"].get("mgt fee") == alias_target,
        str(out["category_aliases"]),
    )
    check("it remembers all three properties", len(out["property_aliases"]) == 3, str(out["property_aliases"]))

taught = extract("shell-0012.pdf", statement(number="#0012"))
tnote = taught["files"][0]
check("the saved format is found", tnote["format_label"] == "Shell Management", str(tnote["format_label"]))
check("and matched exactly", tnote["format_match"] == "exact", str(tnote["format_match"]))
check(
    "the review screen says which format it used",
    any("Shell Management" in w for w in tnote["warnings"]),
    "; ".join(tnote["warnings"])[:160],
)

previews = [it["preview"] for it in taught["items"]]
check(
    "every property is now matched to the one stored under a different spelling",
    all(p["property_id"] and not p["property_unknown"] for p in previews),
    str([(p["detected_property"], p["property_unknown"]) for p in previews]),
)
check(
    "none is offered as a new property any more",
    taught["unknown_properties"] == [],
    str(taught["unknown_properties"]),
)
fees_now = [r for p in previews for r in p["rows"] if r["raw_category"] == "Mgt Fee"]
check("the fee rows are still found by the statement's own word", len(fees_now) == 3, str(len(fees_now)))
check(
    "the taught category replaced the one the parser would have guessed",
    all(r["category"] == alias_target for r in fees_now),
    str({r["category"] for r in fees_now}),
)
check(
    "the taught classification replaced the detected one",
    all(r["classification"] == "capex" for r in fees_now),
    str({r["classification"] for r in fees_now}),
)
check(
    "the taught month rule moved the statement to the tenancy month",
    {p["detected_month"] for p in previews} == {"2026-10-01"},
    str({p["detected_month"] for p in previews}),
)
check(
    "the rents are untouched by any of it",
    sorted(r["amount"] for p in previews for r in p["rows"] if r["kind"] == "rent") == [600.0, 600.0, 725.0],
)


# ============================================================================================
# 7. Re-teaching merges rather than replaces
# ============================================================================================
print("\n7. Correcting one new thing doesn't drop everything learned before it")
r = client.post(
    "/import/statement/formats",
    headers=H,
    json={
        "fingerprint": fp_base,
        "label": "Shell Management",
        "shape": "sectioned",
        "categories": [{"raw": "Ground Rent", "target_id": cats["Property Tax"]}],
        "properties": [],
        "month_rule": "rent_period",
    },
)
check("re-teaching succeeds", r.status_code == 201, r.text[:200])
if r.status_code == 201:
    merged = r.json()
    check("the new alias is there", merged["category_aliases"].get("ground rent") == "Property Tax")
    check(
        "and the one taught last month survived",
        merged["category_aliases"].get("mgt fee") == alias_target,
        str(merged["category_aliases"]),
    )
    check("as did the properties", len(merged["property_aliases"]) == 3, str(len(merged["property_aliases"])))
    check("it is listed as used", merged["times_used"] >= 1, str(merged["times_used"]))

# ============================================================================================
# 8. A layout that has drifted
# ============================================================================================
print("\n8. An agent that changes its covering note is still the same agent")
drifted = extract(
    "shell-0013.pdf",
    statement(number="#0013", extra_note="Our office will be closed over the bank holiday."),
)
dnote = drifted["files"][0]
check("the drifted layout fingerprints differently", dnote["fingerprint"] != fp_base)
check("but the saved format is still found", dnote["format_label"] == "Shell Management", str(dnote["format_label"]))
check("and is reported as a close match, not an exact one", dnote["format_match"] == "close", str(dnote["format_match"]))
check(
    "which the review screen says in words",
    any("close to, but not identical" in w for w in dnote["warnings"]),
    "; ".join(dnote["warnings"])[:200],
)
check(
    "the mappings still apply",
    all(it["preview"]["property_id"] for it in drifted["items"]),
)

# ============================================================================================
# 9. Nothing is trusted on the way in
# ============================================================================================
print("\n9. A format may only ever point at this account's own records")
r = client.post(
    "/import/statement/formats",
    headers=H,
    json={
        "fingerprint": "f" * 32,
        "label": "Forged",
        "shape": "sectioned",
        "categories": [{"raw": "Rent", "target_id": "00000000-0000-0000-0000-000000000001"}],
        "properties": [],
    },
)
check("a category id that isn't this account's is refused", r.status_code == 400, str(r.status_code))
r = client.post(
    "/import/statement/formats",
    headers=H,
    json={
        "fingerprint": "e" * 32,
        "label": "Forged",
        "shape": "sectioned",
        "categories": [],
        "properties": [{"raw": "somewhere", "target_id": "00000000-0000-0000-0000-000000000002"}],
    },
)
check("so is a property id that isn't", r.status_code == 400, str(r.status_code))
check(
    "and neither was stored",
    not [f for f in client.get("/import/statement/formats", headers=H).json() if f["label"] == "Forged"],
)

# ============================================================================================
# 10. The statements that already worked still take their old path
# ============================================================================================
print("\n10. One property per file is still one property per file")
single = extract(
    "ordinary.pdf",
    [
        "Owner Statement",
        "Property: Maple Court Apartments",
        "Statement Period 1 Mar 2026 - 31 Mar 2026",
        "Income",
        "Rent 4,200.00",
        "Total Income 4,200.00",
        "Expenses",
        "Repairs & Maintenance 310.00",
        "Utilities 120.00",
        "Total Expenses 430.00",
    ],
)
check("an ordinary statement is still read as a single", single["files"][0]["format"] == "single", single["files"][0]["format"])
check("and produces one statement", single["files"][0]["statements"] == 1, str(single["files"][0]["statements"]))
check(
    "with its line items intact",
    sorted(r["amount"] for r in single["items"][0]["preview"]["rows"]) == [120.0, 310.0, 4200.0],
    str(sorted(r["amount"] for r in single["items"][0]["preview"]["rows"])),
)

# ============================================================================================
# 11. Forgetting
# ============================================================================================
print("\n11. A format can be forgotten")
for fid in list(saved_formats):
    r = client.delete(f"/import/statement/formats/{fid}", headers=H)
    check("the format deletes", r.status_code == 204, str(r.status_code))
    saved_formats.remove(fid)
after = extract("shell-0014.pdf", statement(number="#0014"))
check(
    "the next statement is read as an untaught layout again",
    after["files"][0]["format_label"] is None,
    str(after["files"][0]["format_label"]),
)
check(
    "and its properties are matched by address alone, not by the forgotten alias",
    all(it["preview"]["property_id"] for it in after["items"]),
    "address matching still finds the stored properties",
)

# ---- done -------------------------------------------------------------------------------
cleanup()
print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("verify_phase16 PASSED")
