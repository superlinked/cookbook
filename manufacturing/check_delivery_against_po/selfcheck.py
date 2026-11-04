"""Check the recipe's parsing, PO matching, fill status and checks offline.

The checks use hand-written strings, the committed data/ and hand-written stand-in model
replies. Nothing here calls SIE or replays a recorded response. Run it with
`uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import tempfile
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from sie_sdk import SIEClient
from sie_sdk.client.errors import (
    AccountInactiveError,
    InputTooLongError,
    InsufficientCreditsError,
    ModelLoadFailedError,
    ModelLoadingError,
    ProvisioningError,
    RateLimitError,
    RequestError,
    ServerError,
    SIEConnectionError,
    SIEError,
    SpendLimitError,
)

import recipe

RULES = recipe.load_rules()
DATA = recipe.DATA_DIR
PO_LINES = recipe.load_po_lines(DATA / "po_lines.csv")


# --- Hand-written OCR pages, in LightOnOCR's shape (form boxes as "label<br>value" cells) -----


def cells(*rows: list[str]) -> str:
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table>{body}</table>"


def delivery_note(*lines: list[str], total: int | None = -1) -> str:
    """A delivery note page; `total` -1 prints the sum of the numeric quantities, None no total
    line at all."""
    header = ["PO number", "Line", "Item", "Grade", "Size (mm)", "Heat number", "Qty"]
    if total == -1:
        total = sum(int(line[-1]) for line in lines if line[-1].isdigit())
    return (
        "# DELIVERY NOTE\n\n"
        + cells(["Delivery note<br>DN-1", "Dispatch date<br>2026-10-04"])
        + "\n\n"
        + cells(header, *lines)
        + ("" if total is None else f"\n\nTotal plates: {total}\n")
    )


def certificate(
    heat: str = "A41873",
    grade: str = "S355J2+N",
    yield_strength: str = "372 MPa",
    impact: str | None = "Impact energy KV at -20 °C<br>64 J",
) -> str:
    properties = ["Elongation A<br>24 %"] + ([impact] if impact else [])
    return "\n\n".join(
        [
            "# INSPECTION CERTIFICATE",
            cells(
                ["Certificate number<br>HSW-26-3137", "Date of issue<br>2026-09-28"],
                ["Steel grade<br>" + grade, "Heat number<br>" + heat],
                ["Thickness<br>12.00 mm", "Width<br>2000 mm", "Length<br>6000 mm"],
            ),
            "Mechanical properties (transverse)",
            cells(
                ["Yield strength ReH<br>" + yield_strength, "Tensile strength Rm<br>528 MPa"],
                properties,
            ),
        ]
    )


PLAN_ROWS = [
    ["Thickness T1", "12.00", "12.06"],
    ["Thickness T2", "12.00", "12.11"],
    ["Thickness T3", "12.00", "12.04"],
    ["Thickness T4", "12.00", "12.09"],
    ["Width", "2000", "2006"],
    ["Length", "6000", "6011"],
]


def report(rows: list[list[str]] | None = None, inspector: str = "R. Castellanos") -> str:
    table = [["Characteristic", "Nominal (mm)", "Measured (mm)", "Instrument", "Result"]]
    table += [[*row, "UT gauge", "OK"] for row in (PLAN_ROWS if rows is None else rows)]
    return "\n\n".join(
        [
            "# RECEIVING INSPECTION REPORT",
            cells(
                ["Report number<br>RI-2026-411", "Date of inspection<br>October 6, 2026"],
                ["Purchase order<br>PO-48213 line 1", "Heat number<br>A41873"],
                ["Material<br>Plate S355J2+N 12 x 2000 x 6000", "Inspector<br>" + inspector],
            ),
            cells(*table),
        ]
    )


# --- A stand-in for SIEClient -----------------------------------------------------------------


def stand_in_fields(text: str, kind: str) -> dict[str, str]:
    """What a well-behaved GLiNER2 would return: the value after each field's first label."""
    found: dict[str, str] = {}
    for name, spec in RULES["documents"][kind].items():
        for label in spec["label"]:
            match = re.search(rf"(?i)(?<![a-z]){re.escape(label)}[^|\n]*\| ([^|\n]+)", text)
            if match:
                found[name] = match.group(1).strip()
                break
    return found


class StandIn:
    """Stands in for SIEClient. OCR calls return the page Markdown keyed by the call's item id
    ("ocr/<pdf stem>/p<page>"); extract calls return `stand_in_fields` for the window, or the
    reply `fields` gives for the item id. `errors` maps an item id to the error its call
    raises (a list: one per call), `replies` to a raw reply that replaces the normal one."""

    def __init__(
        self,
        pages: dict[str, str],
        errors: dict[str, list[Exception]] | None = None,
        replies: dict[str, Any] | None = None,
        fields: Callable[[str, str], dict[str, str]] | None = None,
    ) -> None:
        self.pages, self.errors, self.replies = pages, errors or {}, replies or {}
        self.fields = fields
        self.ids: list[str] = []

    def extract(self, model: str, item: Any, **options: Any) -> Any:
        item_id, text = item["id"], item.get("text", "")
        self.ids.append(item_id)
        for prefix, errors in self.errors.items():
            if item_id.startswith(prefix) and errors:
                raise errors.pop(0)
        for prefix, reply in self.replies.items():
            if item_id.startswith(prefix):
                return reply
        if "output_schema" not in options:
            return {"entities": [{"label": "markdown", "text": self.pages[item_id]}]}
        kind = "certificate" if "/certificate" in item_id else "inspection_report"
        if self.fields is not None:
            return {"data": self.fields(item_id, text)}
        return {"data": stand_in_fields(text, kind)}


def one_delivery(
    folder: Path,
    note: str,
    lots: list[tuple[str, str]],
    po_rows: list[str] | None = None,
) -> dict[str, str]:
    """Write a one-delivery data folder (receipts.jsonl, po_lines.csv, three one-page-per-lot
    PDFs copied from data/) and return the stand-in OCR pages keyed by call id."""
    for name in ("delivery-notes.pdf", "mill-certificates.pdf", "inspection-reports.pdf"):
        (folder / name).write_bytes((DATA / name).read_bytes())
    rows = po_rows or [
        "PO-48213,1,41212,Harrowgate Steelworks Ltd,S355J2+N,12,2000,6000,6,0",
        "PO-48213,2,41212,Harrowgate Steelworks Ltd,S355J2+N,12,2000,6000,10,0",
    ]
    header = "po_number,line,item,supplier,grade,thickness_mm,width_mm,length_mm,"
    (folder / "po_lines.csv").write_text(
        header + "ordered_qty,received_qty\n" + "\n".join(rows) + "\n", encoding="utf-8"
    )
    receipt = {
        "delivery": "DN-1",
        "received": "2026-10-05",
        "delivery_note": {"file": "delivery-notes.pdf", "page": 1},
        "lots": [
            {
                "lot": f"lot-{index:02d}",
                "delivery_line": index,
                "certificate": {"file": "mill-certificates.pdf", "page": index},
                "inspection_report": {"file": "inspection-reports.pdf", "page": index},
            }
            for index in range(1, len(lots) + 1)
        ],
    }
    (folder / "receipts.jsonl").write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    pages = {"ocr/delivery-notes/p1": note}
    for index, (cert, form) in enumerate(lots, start=1):
        pages[f"ocr/mill-certificates/p{index}"] = cert
        pages[f"ocr/inspection-reports/p{index}"] = form
    return pages


LINE_1 = ["PO-48213", "1", "41212", "S355J2+N", "12x2000x6000", "A41873", "6"]


def run(
    lines: list[list[str]] | None = None,
    lots: list[tuple[str, str]] | None = None,
    po_rows: list[str] | None = None,
    rules: dict[str, Any] | None = None,
    note: str | None = None,
    **stand_in: Any,
) -> tuple[list[recipe.LotResult], list[recipe.PoLineFill], StandIn]:
    with tempfile.TemporaryDirectory() as name:
        folder = Path(name)
        lots = lots or [(certificate(), report())]
        note = note or delivery_note(*(lines or [LINE_1]))
        pages = one_delivery(folder, note, lots, po_rows)
        client = StandIn(pages, **stand_in)
        with contextlib.redirect_stdout(io.StringIO()):
            results, fills, _ = recipe.run(cast(SIEClient, client), rules or RULES, folder)
    return results, fills, client


def statuses(results: list[recipe.LotResult]) -> list[tuple[str, str, list[str]]]:
    return [(r.fill_status, r.status, [c.check for c in r.flagged]) for r in results]


# --- Checks -----------------------------------------------------------------------------------


def check_parsing() -> None:
    flat = recipe.flatten_markdown(certificate())
    assert "Steel grade | S355J2+N | Heat number | A41873" in flat.splitlines(), flat
    assert recipe.labelled_cell("372", flat, ["yield strength"]) == "372 MPa"
    # The impact label carries a qualifier ("KV at -20 °C") before its value.
    assert recipe.labelled_cell("64", flat, ["impact energy"]) == "64 J"
    assert recipe.impact_temperature(flat) == -20
    assert recipe.impact_temperature("Impact energy KV at \N{MINUS SIGN}20°C | 64 J") == -20
    assert recipe.impact_temperature("Impact energy KV at +20 °C | 64 J") == 20
    assert recipe.impact_temperature("Elongation A | 24 %") is None
    assert recipe.parse_size("12x2000x6000") == (Decimal(12), Decimal(2000), Decimal(6000))
    assert recipe.parse_size("12 x 2000 x 6000 mm") == (Decimal(12), Decimal(2000), Decimal(6000))
    assert recipe.parse_size("12x2000") is None
    assert recipe.parse_quantity("6") == 6
    assert recipe.parse_quantity("6 plates") == 6
    assert recipe.parse_quantity("six") is None
    assert recipe.parse_energy("64 J") == Decimal(64)
    assert recipe.parse_energy("64") is None
    flat = recipe.flatten_markdown(delivery_note(LINE_1))
    assert recipe.note_total(flat, RULES["delivery_total_label"]) == 6
    lines, irregular = recipe.table_rows(flat, RULES["delivery_columns"])
    assert irregular == []
    assert lines == [
        {
            "po_number": "PO-48213",
            "po_line": "1",
            "item": "41212",
            "grade": "S355J2+N",
            "size": "12x2000x6000",
            "heat_number": "A41873",
            "quantity": "6",
        }
    ], lines
    print("parsing: labelled cells, impact qualifier and temperature, sizes, delivery lines ok")


def check_po_matching() -> None:
    line = dict(zip(RULES["delivery_columns"], LINE_1, strict=True))
    po, reason = recipe.match_po_line(line, PO_LINES)
    assert po is not None
    assert (po.po_number, po.line) == ("PO-48213", 1)
    assert reason == ""
    # PO number written with a space and lower case still matches.
    po, _ = recipe.match_po_line({**line, "po_number": "po-48213 "}, PO_LINES)
    assert po is not None
    po, reason = recipe.match_po_line({**line, "po_line": "4", "grade": "S275JR"}, PO_LINES)
    assert po is None
    assert reason == (
        "PO-48213 line 4 on the delivery note says grade S275JR (ordered S355J2+N)"
    ), reason
    po, reason = recipe.match_po_line({**line, "po_line": "9"}, PO_LINES)
    assert (po, reason) == (None, "PO-48213 line 9 is not on the order")
    po, reason = recipe.match_po_line({**line, "size": "15x2000x6000"}, PO_LINES)
    assert po is None
    assert "size 15x2000x6000 (ordered 12x2000x6000)" in reason, reason
    po, reason = recipe.match_po_line({**line, "item": "41215"}, PO_LINES)
    assert po is None
    assert "item 41215 (ordered 41212)" in reason, reason
    print("po matching: match, wrong grade, unknown line, wrong size and item ok")


def check_fill_status() -> None:
    assert recipe.fill_status(6, 6, 0) == ("filled", 0)
    assert recipe.fill_status(10, 6, 0) == ("partially_filled", 4)
    assert recipe.fill_status(8, 10, 0) == ("over_delivered", -2)
    assert recipe.fill_status(8, 9, 1) == ("filled", -1)  # inside the allowance
    # Through the run: previously received counts, and so does a second line for the same PO
    # line on the same delivery.
    po = ["PO-48213,1,41212,Harrowgate Steelworks Ltd,S355J2+N,12,2000,6000,10,0"]
    two = [LINE_1, [*LINE_1[:5], "A41874", "2"]]
    results, fills, _ = run(two, [(certificate(), report())] * 2, po)
    (row,) = fills
    assert (row.fill_status, row.delivered, row.open_qty) == ("partially_filled", 8, 2), row
    assert row.lots == ["lot-01", "lot-02"]
    assert row.as_row()["detail"] == "2 plates still open"
    po = ["PO-48213,1,41212,Harrowgate Steelworks Ltd,S355J2+N,12,2000,6000,10,3"]
    results, fills, _ = run([[*LINE_1[:6], "8"]], po_rows=po)
    assert (fills[0].fill_status, fills[0].open_qty) == ("over_delivered", -1)
    assert fills[0].as_row()["detail"] == "1 plate over the order"
    assert (fills[0].as_row()["open_qty"], fills[0].as_row()["over_qty"]) == (0, 1)
    assert results[0].fill_status == "over_delivered"
    assert results[0].status == "checks_passed"  # the quantity is the buyer's question
    # A line naming the wrong grade matches no PO line and books nothing.
    wrong = [*LINE_1[:3], "S275JR", *LINE_1[4:]]
    results, fills, _ = run([wrong])
    assert [(r.fill_status, r.status) for r in results] == [("no_po_line", "review")]
    assert [c.check for c in results[0].flagged] == ["po_line"]
    assert [(f.fill_status, f.delivered) for f in fills] == [("no_po_line", 6)]
    # The JSON and lots.csv both show the PO line the note claimed, and that it didn't match.
    assert (results[0].as_json()["po_line"], results[0].as_json()["po_line_matched"]) == (
        "PO-48213 line 1",
        False,
    )
    with tempfile.TemporaryDirectory() as name:
        recipe.write_outputs(Path(name), results, fills)
        text = (Path(name) / "lots.csv").read_text(encoding="utf-8").splitlines()[1]
        assert text.startswith("lot-01,DN-1,PO-48213 line 1,no,no_po_line,review,"), text
    # A quantity that isn't a number: no fill status is guessed.
    results, fills, _ = run([[*LINE_1[:6], "six"]])
    assert fills[0].fill_status == "review"
    assert results[0].fill_status == "review"
    print("fill status: filled, partial, over, allowance, no PO line, unread quantity ok")


def check_delivery_note_table() -> None:
    """The line table must add up before any fill status is trusted."""
    po = ["PO-48213,1,41212,Harrowgate Steelworks Ltd,S355J2+N,12,2000,6000,10,0"]

    def held(note: str, lots: int = 1) -> tuple[list[str], list[str], str]:
        results, fills, _ = run(lots=[(certificate(), report())] * lots, po_rows=po, note=note)
        details = [c.detail for r in results for c in r.flagged if c.check == "po_line"]
        return [f.fill_status for f in fills], details, fills[0].as_row()["detail"]

    # A misread quantity: the note says 6 plates in total, the line was read as 8. Without
    # the check this PO line (10 ordered) would read partially_filled with 2 open.
    misread = delivery_note([*LINE_1[:6], "8"], total=6)
    status, details, detail = held(misread)
    assert status == ["review"], status
    assert details == ["DN-1: line quantities add up to 8, the note's total says 6"], details
    assert detail.endswith("as read: partially_filled; 2 plates still open"), detail
    # A row read twice: one more line than the scan batch has, and the quantities add to 12.
    status, details, _ = held(delivery_note(LINE_1, LINE_1, total=6))
    assert status == ["review"], status
    assert details[0] == (
        "DN-1: 2 lines read, the scan batch has 1; "
        "line quantities add up to 12, the note's total says 6"
    ), details
    # A dropped row: the batch has two lines, one was read, and the total says 8.
    status, details, _ = held(delivery_note(LINE_1, total=8), lots=2)
    assert status == ["review"], status
    assert "1 lines read, the scan batch has 2" in details[0], details
    # A row with a cell missing does not end the table quietly.
    broken = delivery_note(LINE_1[:6])
    status, details, _ = held(broken)
    assert status == ["review"], status
    assert "has 6 cells, the table has 7" in details[0], details
    # No total line on the note.
    status, details, _ = held(delivery_note(LINE_1, total=None))
    assert details == ["DN-1: no total read on the note"], details
    # A note that adds up passes.
    status, details, _ = held(delivery_note(LINE_1))
    assert (status, details) == (["partially_filled"], []), (status, details)
    print("delivery note: misread quantity, duplicated row, dropped row, bad row, no total ok")


def check_statuses() -> None:
    results, fills, client = run()
    assert statuses(results) == [("filled", "checks_passed", [])], statuses(results)
    assert results[0].as_json()["status"] == "checks_passed"
    assert "decision" not in results[0].as_json()
    # One OCR call per page, one extract call per window of the certificate and the report.
    assert [i for i in client.ids if i.startswith("ocr/")] == [
        "ocr/delivery-notes/p1",
        "ocr/mill-certificates/p1",
        "ocr/inspection-reports/p1",
    ]
    with tempfile.TemporaryDirectory() as name:
        recipe.write_outputs(Path(name), results, fills)
        lots = (Path(name) / "lots.csv").read_text(encoding="utf-8").splitlines()
        assert lots[1] == "lot-01,DN-1,PO-48213 line 1,yes,filled,checks_passed,,", lots
        assert (Path(name) / "po-lines.csv").read_text(encoding="utf-8").splitlines()[1] == (
            "PO-48213,1,41212,6,0,6,0,0,filled,DN-1,lot-01,"
        )
    print("statuses: checks_passed, filled; outputs carry status, not decision ok")


def check_inspection_plan() -> None:
    # lot-11's shape: the failing T3 row is missing; the rest pass.
    rows = [row for row in PLAN_ROWS if row[0] != "Thickness T3"]
    results, _, _ = run(lots=[(certificate(), report(rows))])
    (check,) = [c for c in results[0].checks if c.check == "dimensions"]
    assert (check.status, check.detail) == ("review", "Thickness T3 not read"), check
    # The same reading written twice with different values goes to review, not first wins.
    rows = [*PLAN_ROWS, ["Thickness T3", "12.00", "11.20"]]
    results, _, _ = run(lots=[(certificate(), report(rows))])
    (check,) = [c for c in results[0].checks if c.check == "dimensions"]
    assert check.status == "review"
    assert "Thickness T3 read twice: 12.04 / 11.20" in check.detail
    # Written twice with the same value is one reading.
    results, _, _ = run(lots=[(certificate(), report([*PLAN_ROWS, PLAN_ROWS[0]]))])
    assert results[0].status == "checks_passed", statuses(results)
    # A row with a cell missing is a review reason, not the end of the table.
    rows = [PLAN_ROWS[0], ["Thickness T2", "12.11"], *PLAN_ROWS[2:]]
    results, _, _ = run(lots=[(certificate(), report(rows))])
    (check,) = [c for c in results[0].checks if c.check == "dimensions"]
    assert check.status == "review", check
    assert "has 4 cells, the table has 5" in check.detail, check.detail
    assert "Thickness T2 not read" in check.detail, check.detail
    # A row that is not on the plan.
    rows = [*PLAN_ROWS, ["Flatness", "0", "3"]]
    results, _, _ = run(lots=[(certificate(), report(rows))])
    (check,) = [c for c in results[0].checks if c.check == "dimensions"]
    assert check.detail == "'Flatness' is not on the inspection plan", check
    # A failing reading still fails.
    rows = [*PLAN_ROWS[:2], ["Thickness T3", "12.00", "11.38"], *PLAN_ROWS[3:]]
    results, _, _ = run(lots=[(certificate(), report(rows))])
    assert statuses(results)[0][1:] == ("nonconforming", ["dimensions"])
    print("inspection plan: missing T3, conflicting repeat, same repeat, extra row, fail ok")


def check_spec_boundaries() -> None:
    """Strength limits, the yield band, the guard band, heat numbers and dates."""

    def checks(cert: str = "", form: str = "", po: list[str] | None = None) -> dict[str, Any]:
        lines = [LINE_1]
        if po:
            lines = [[*LINE_1[:4], po[1], LINE_1[5], LINE_1[6]]]
            po = [po[0]]
        results, _, _ = run(lines, [(cert or certificate(), form or report())], po)
        return {c.check: (c.status, c.detail) for c in results[0].checks}

    # Yield on the 355 minimum passes, 354 fails; 341 is lot-05's.
    assert checks(certificate(yield_strength="355 MPa"))["yield_strength"][0] == "pass"
    assert checks(certificate(yield_strength="354 MPa"))["yield_strength"][0] == "fail"
    # The band follows the ordered thickness: 348 passes at 20 mm (min 345), fails at 12.
    po20 = ["PO-48213,1,41212,Harrowgate Steelworks Ltd,S355J2+N,20,2000,6000,6,0"]
    rows20 = [
        [name, "20.00" if "Thick" in name else nom, "20.10" if "Thick" in name else got]
        for name, nom, got in PLAN_ROWS
    ]
    cert20 = certificate(yield_strength="348 MPa").replace("12.00 mm", "20.00 mm")
    got = checks(cert20, report(rows20), [po20[0], "20x2000x6000"])
    assert got["yield_strength"] == (
        "pass",
        "ReH 348 MPa ('348 MPa'); min 345 MPa for S355J2+N at 20 mm",
    ), got["yield_strength"]
    assert checks(certificate(yield_strength="348 MPa"))["yield_strength"][0] == "fail"
    # An OCR digit swap that puts yield above tensile ("841" for 418) goes to review.
    got = checks(certificate(yield_strength="841 MPa"))
    assert got["strength_order"][0] == "review", got
    assert got["yield_strength"][0] == "pass"  # above the minimum, so only the order catches it
    # The guard band: 11.53 passes (0.03 inside), 11.52 is review (0.02), 11.49 fails.
    for reading, status in [("11.53", "pass"), ("11.52", "review"), ("11.49", "fail")]:
        rows = [PLAN_ROWS[0], ["Thickness T2", "12.00", reading], *PLAN_ROWS[2:]]
        assert checks(form=report(rows))["dimensions"][0] == status, reading
    # A heat number that differs from the delivery note's goes to review; "A-41873" doesn't.
    got = checks(certificate(heat="A41837"))
    assert got["heat_number"] == (
        "review",
        "Delivery note says A41873; certificate says 'A41837'",
    ), got["heat_number"]
    assert checks(certificate(heat="A-41873"))["heat_number"][0] == "pass"
    # Dates out of order: a certificate issued after the delivery arrived.
    late = certificate().replace("2026-09-28", "2026-10-07")
    assert checks(late)["dates"] == (
        "review",
        "Certificate 2026-10-07, received 2026-10-05, inspected 2026-10-06",
    ), checks(late)["dates"]
    early = report().replace("October 6, 2026", "October 4, 2026")
    assert checks(form=early)["dates"][0] == "review"
    print("spec boundaries: yield on/below minimum, band, order, guard band, heat, dates ok")


def check_impact() -> None:
    results, _, _ = run(lots=[(certificate(impact=None), report())])
    (check,) = results[0].flagged
    assert (check.check, check.status) == ("impact_energy", "review"), check
    assert check.detail == "Not read: impact energy, impact test temperature", check.detail
    low = "Impact energy KV at -20 °C<br>21 J"
    results, _, _ = run(lots=[(certificate(impact=low), report())])
    assert statuses(results)[0][1:] == ("nonconforming", ["impact_energy"])
    warm = "Impact energy KV at +20 °C<br>64 J"
    results, _, _ = run(lots=[(certificate(impact=warm), report())])
    (check,) = results[0].flagged
    assert check.detail.startswith("KV 64 J tested at 20 C; min 27 J at -20 C"), check.detail
    (check,) = [c for c in run()[0][0].checks if c.check == "impact_energy"]
    assert check.status == "pass", check
    print("impact: missing, low, tested too warm, pass ok")


def check_conflicting_fields() -> None:
    def two_yields(item_id: str, text: str) -> dict[str, str]:
        found = stand_in_fields(text, "certificate" if "/certificate" in item_id else "x")
        if "/certificate" in item_id:
            found["yield_strength"] = "381" if item_id.endswith("-w2") else "372"
        return found

    def stand_in_x(text: str, kind: str) -> dict[str, str]:
        return stand_in_fields(text, kind if kind != "x" else "inspection_report")

    # Two windows: the first reads 372, the second 381 (both labelled cells in the text).
    cert = certificate(yield_strength="372 MPa") + "\n\n" + cells(["Yield strength<br>381 MPa"])
    rules = {**RULES, "thresholds": {**RULES["thresholds"], "window_words": 60}}
    results, _, _ = run(
        lots=[(cert, report())],
        rules=rules,
        fields=lambda item_id, text: (
            two_yields(item_id, text)
            if "/certificate" in item_id
            else stand_in_x(text, "inspection_report")
        ),
    )
    flagged = {c.check: c.detail for c in results[0].flagged}
    conflict = "certificate yield_strength read as '372 MPa' / '381 MPa'"
    assert flagged.get("conflicting_values") == conflict, flagged
    assert flagged["yield_strength"] == "Not read: yield strength", flagged
    print("conflicting fields: two values for one field go to review, not first wins ok")


TRANSIENT: list[SIEError] = [
    SIEConnectionError("connection refused"),
    ServerError("unavailable", status_code=503),
    RateLimitError("slow down"),
    InputTooLongError("too long"),
    ProvisioningError("no GPU yet"),
    ModelLoadingError("loading"),
    ModelLoadFailedError("try later", permanent=False),
    SIEError("an SIE error with no subclass"),
]
FATAL: list[SIEError] = [
    RequestError("bad key", status_code=401),
    RequestError("no such model", status_code=404),
    InsufficientCreditsError("no credits"),
    SpendLimitError("spend limit"),
    AccountInactiveError("inactive"),
    ModelLoadFailedError("gated", permanent=True),
]


def stopped(call: Callable[[], object]) -> str:
    """The SIEStopped message the call raises; fails when it raises nothing."""
    try:
        call()
    except recipe.SIEStopped as error:
        return str(error)
    raise AssertionError("the run did not stop")


def raised(call: Callable[[], object]) -> BaseException:
    """Whatever the call raises, SystemExit included; fails when it raises nothing."""
    try:
        call()
    except BaseException as error:  # SystemExit too: the check is on which one it was
        return error
    raise AssertionError("nothing was raised")


def check_sie_failure() -> None:
    for error in TRANSIENT:
        assert not recipe.is_fatal(error), f"{type(error).__name__} should be transient"
    for error in FATAL:
        assert recipe.is_fatal(error), f"{type(error).__name__} should stop the run"
    two = [(certificate(), report())] * 2
    lines = [LINE_1, [*LINE_1[:6], "2"]]
    # A transient error on one lot's certificate: that lot goes to review, the other passes.
    for error in TRANSIENT:
        results, _, _ = run(lines, two, errors={"ocr/mill-certificates/p1": [error]})
        assert [r.status for r in results] == ["review", "checks_passed"], type(error).__name__
        reason = results[0].flagged[0]
        assert reason.check == "readable", reason
        assert "was not read: the SIE call failed: " in reason.detail, reason
    # A transient error on the delivery note: its lots go to review, nothing is booked.
    results, fills, _ = run(errors={"ocr/delivery-notes": [ServerError("busy", status_code=503)]})
    assert statuses(results)[0][:2] == ("review", "review")
    assert [f.fill_status for f in fills] == ["review"]
    # A reply missing its key is a failed call on the same path; an empty list is not.
    results, _, _ = run(replies={"ocr/mill-certificates": {"something": []}})
    assert "replied without 'entities'" in results[0].flagged[0].detail, results[0].flagged
    results, _, _ = run(replies={"extract/lot-01/certificate": {"no_data": {}}})
    assert "replied without 'data'" in results[0].flagged[0].detail, results[0].flagged
    results, _, _ = run(replies={"extract/lot-01/certificate": {"data": {}}})
    assert "readable" not in [c.check for c in results[0].flagged], results[0].flagged
    # SIE Cloud's empty-result 500 reads as "no fields", not a failed call.
    empty = ServerError(recipe.EMPTY_RESULT_ERROR, status_code=500)
    results, _, _ = run(errors={"extract/lot-01/inspection_report": [empty]})
    assert "readable" not in [c.check for c in results[0].flagged]
    assert any("no fields found" in note for note in results[0].notes)
    # The empty-result 500 is only "nothing found" from the extraction model: from the OCR
    # model it's a failed call.
    results, _, _ = run(errors={"ocr/mill-certificates": [empty]})
    assert results[0].flagged[0].check == "readable", results[0].flagged
    assert "the SIE call failed: ServerError" in results[0].flagged[0].detail
    # An OCR reply with entities but no markdown entity says so.
    results, _, _ = run(replies={"ocr/mill-certificates": {"entities": []}})
    detail = results[0].flagged[0].detail
    assert detail == "certificate (mill-certificates.pdf p1): the OCR reply had no markdown text"
    # Every call failed (a dead SIE_BASE_URL): the run stops, with the full message.
    long = "connection refused " + "x" * 300
    dead = {"": [SIEConnectionError(long) for _ in range(20)]}
    message = stopped(lambda: run(errors=dead))
    assert message.startswith("Every SIE call failed (3 of 3 to lightonai/LightOnOCR-3-4B)"), (
        message
    )
    assert long in message
    assert "Check SIE_BASE_URL and your network." in message, message
    busy = {"": [ModelLoadingError("loading") for _ in range(20)]}
    message = stopped(lambda: run(errors=busy))
    assert "may still be starting or SIE is busy; run again in a few minutes." in message
    # A refusal stops the run at once, with the full message, HTTP status and code.
    refusal = {"ocr/mill-certificates": [InsufficientCreditsError("no credits " + "y" * 300)]}
    message = stopped(lambda: run(errors=refusal))
    assert "InsufficientCreditsError (HTTP 402, code INSUFFICIENT_CREDITS)" in message, message
    assert "y" * 300 in message
    assert "SIE_API_KEY" in message
    message = stopped(lambda: run(errors={"": [ModelLoadFailedError("gated", permanent=True)]}))
    assert message.startswith("SIE can't load lightonai/LightOnOCR-3-4B"), message
    message = stopped(lambda: run(errors={"": [RequestError("unexpected body")]}))
    assert "SIE_API_KEY" not in message, message
    assert "SIE_BASE_URL" in message, message
    # A bug in our own code is raised as is; main turns only SIEStopped into a clean exit.
    try:
        run(errors={"": [TypeError("bad argument")]})
    except TypeError:
        pass
    else:
        raise AssertionError("TypeError was swallowed")

    def bug() -> None:
        raise RuntimeError("a bug in recipe.py")

    def refused() -> None:
        raise recipe.SIEStopped("SIE refused")

    caught = raised(lambda: recipe.stop_on_refusal(bug))
    assert type(caught) is RuntimeError
    assert str(caught) == "a bug in recipe.py"
    caught = raised(lambda: recipe.stop_on_refusal(refused))
    assert type(caught) is SystemExit
    assert str(caught) == "Stopped: SIE refused"
    print("sie failure: transient to review; missing key fails; refusals and all-failed stop ok")


def check_csv_safe() -> None:
    assert recipe.csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert recipe.csv_safe("-2 plates") == "'-2 plates"
    assert recipe.csv_safe("PO-48213") == "PO-48213"
    assert recipe.csv_safe(-2) == "'-2"
    fill = recipe.PoLineFill("=cmd()", "1", fill_status="no_po_line")
    with tempfile.TemporaryDirectory() as name:
        recipe.write_outputs(Path(name), [], [fill])
        row = (Path(name) / "po-lines.csv").read_text(encoding="utf-8").splitlines()[1]
        assert row.startswith("'=cmd(),1,"), row
    print("csv_safe: formula-looking cells are quoted ok")


def check_committed_data() -> None:
    receipts = recipe.load_receipts(DATA / "receipts.jsonl")
    assert [r["delivery"] for r in receipts] == [
        "DN-26-0712",
        "DN-26-0731",
        "DN-26-0758",
        "DN-26-0790",
    ]
    assert sum(len(r["lots"]) for r in receipts) == 12 == len(PO_LINES)
    for name in ("delivery-notes.pdf", "mill-certificates.pdf", "inspection-reports.pdf"):
        raw = (DATA / name).read_bytes()
        for marker in (b"/Info", b"/Title", b"/Author", b"/CreationDate", b"Exif", b"xmpmeta"):
            assert marker not in raw, f"{name} carries {marker!r}"
    # The planted quantities: lot-02 partial, lot-03 over, the rest of the order filled.
    import make_data

    expected = {
        "lot-02": ("partially_filled", 4),
        "lot-03": ("over_delivered", -2),
    }
    for lot in make_data.LOTS:
        po = PO_LINES[recipe.po_key(lot.po_number, lot.po_line)]
        got = recipe.fill_status(po.ordered, po.received + lot.delivered, 0)
        assert got == expected.get(lot.lot, ("filled", 0)), (lot.lot, got)
    for name, spec in RULES["grades"].items():
        assert {"min_j", "test_temp_c"} <= set(spec["impact"]), name
    assert len(RULES["inspection_plan"]) == 6
    assert make_data.CUSTOMER.endswith("(fictional)"), make_data.CUSTOMER
    print("committed data: 4 deliveries, 12 lots, no PDF metadata, planted quantities ok")


if __name__ == "__main__":
    check_parsing()
    check_po_matching()
    check_fill_status()
    check_delivery_note_table()
    check_statuses()
    check_spec_boundaries()
    check_inspection_plan()
    check_impact()
    check_conflicting_fields()
    check_sie_failure()
    check_csv_safe()
    check_committed_data()
    print("all checks passed")
