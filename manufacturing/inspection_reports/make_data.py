"""Write the synthetic receiving files in data/: delivery notes, certificates, reports, PO lines.

Everything is invented. The steel mill ("Harrowgate Steelworks"), the fabricator ("Calder
Fabrication"), the inspectors, the heat numbers and every test value are made up, and every page
says so in its footer. The certificate follows the usual layout of an EN 10204 type 3.1
inspection certificate for structural plate; it is not a real mill's certificate.

    uv run make_data.py

writes data/po_lines.csv (the PO lines as the ERP exports them: item, ordered grade and size,
ordered quantity and quantity already received), data/receipts.jsonl (one delivery per line:
when it arrived and which page holds its delivery note and each delivered line's certificate
and inspection report) and three image-only PDFs, delivery-notes.pdf, mill-certificates.pdf and
inspection-reports.pdf, as a scanner would produce them. The PDFs carry no document info (no
title, author or dates). Rerunning rebuilds every file byte for byte.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"

DPI = 150
MILL = "Harrowgate Steelworks Ltd"
CUSTOMER = "Calder Fabrication Ltd"
INK = (25, 25, 30)
GREY = (95, 95, 110)
MONTHS = [
    *("January", "February", "March", "April", "May", "June"),
    *("July", "August", "September", "October", "November", "December"),
]
FOOTER = "Fictional sample generated for a software demo. Not a real certificate, mill or report."
CHEMISTRY = ("C", "Mn", "Si", "P", "S", "CEV")


@dataclass(frozen=True)
class Lot:
    """One delivered line: one heat, its certificate and its receiving inspection form."""

    lot: str
    planted: str
    po_number: str
    po_line: int
    item: str  # the fabricator's item number on the PO line
    grade: str  # ordered grade
    thickness: str  # ordered nominal thickness, mm
    heat: str  # heat number on the delivery note
    ordered: int  # plates ordered on the PO line
    previously_received: int  # plates received on earlier deliveries
    delivered: int  # plates on this delivery
    yield_strength: str  # as written on the certificate, unit included
    tensile_strength: str
    thickness_readings: tuple[str, str, str, str]  # T1..T4 on the inspection report, mm
    width: str = "2006"
    length: str = "6011"
    note_grade: str | None = None  # None: the delivery note shows the ordered grade
    cert_heat: str | None = None  # None: the certificate shows the delivery note heat
    report_heat: str | None = None  # None: the report shows the certificate heat
    cert_grade: str | None = None  # None: the certificate shows the delivered grade
    impact: str | None = "64 J"  # None leaves the impact energy box off the certificate
    dropped_rows: tuple[str, ...] = ()  # measurement rows left off the report's table
    inspector: str = "R. Castellanos"  # "" leaves the box blank
    # More variations; the defaults draw the demo pages.
    mill: str = MILL
    size: tuple[str, str] = ("2000", "6000")  # ordered width and length, mm
    cert_thickness: str | None = None  # None: the certificate shows the ordered thickness
    cert_date_style: str = "iso"
    report_date_style: str = "long"
    issue_label: str = "Date of issue"
    cert_heat_label: str = "Heat number"
    report_heat_label: str = "Heat number"
    inspection_label: str = "Date of inspection"


@dataclass(frozen=True)
class Delivery:
    """One truck: the supplier's delivery note and the lots on it, in delivery note order."""

    note: str
    received: date
    lots: tuple[Lot, ...]


def _s355(lot: str, line: int, heat: str, planted: str, **more: Any) -> Lot:
    """An S355J2+N 12 mm lot on PO-48213 with consistent values unless `more` says otherwise."""
    values: dict[str, Any] = {
        "po_number": "PO-48213",
        "po_line": line,
        "item": "41212",
        "grade": "S355J2+N",
        "thickness": "12",
        "heat": heat,
        "ordered": 6,
        "previously_received": 0,
        "delivered": 6,
        "yield_strength": "372 MPa",
        "tensile_strength": "528 MPa",
        "thickness_readings": ("12.06", "12.11", "12.04", "12.09"),
    }
    values.update(more)
    return Lot(lot=lot, planted=planted, **values)


DELIVERIES = [
    Delivery(
        "DN-26-0712",
        date(2026, 10, 5),
        (
            _s355("lot-01", 1, "A41873", "full delivery: 6 of 6 plates; consistent documents"),
            _s355(
                "lot-02",
                2,
                "A41902",
                "partial delivery: 6 of 10 plates. Near miss: yield exactly at the 355 minimum, "
                "written in N/mm2; tensile 1 above the minimum; thinnest reading 0.03 mm inside "
                "tolerance; report heat written 'A-41902'",
                ordered=10,
                yield_strength="355 N/mm2",
                tensile_strength="471 N/mm2",
                thickness_readings=("11.58", "11.53", "11.61", "11.57"),
                report_heat="A-41902",
            ),
            _s355(
                "lot-03",
                3,
                "A41955",
                "over-delivery: 8 ordered, 6 received before, 4 on this delivery",
                ordered=8,
                previously_received=6,
                delivered=4,
                yield_strength="381 MPa",
                tensile_strength="541 MPa",
                thickness_readings=("12.08", "12.03", "12.10", "12.05"),
            ),
            _s355(
                "lot-04",
                4,
                "B17654",
                "delivery note line names PO-48213 line 4 (ordered S355J2+N) but says S275JR",
                item="41212",
                ordered=4,
                delivered=4,
                note_grade="S275JR",
                yield_strength="296 MPa",
                tensile_strength="447 MPa",
                thickness_readings=("12.05", "12.02", "12.07", "12.04"),
            ),
        ),
    ),
    Delivery(
        "DN-26-0731",
        date(2026, 10, 8),
        (
            _s355(
                "lot-05",
                1,
                "A42015",
                "yield strength below the grade minimum",
                po_number="PO-48230",
                ordered=5,
                delivered=5,
                yield_strength="341 MPa",
                tensile_strength="498 MPa",
                thickness_readings=("12.02", "12.05", "11.98", "12.03"),
            ),
            Lot(
                "lot-06",
                "heat number on the certificate and report is not the one on the delivery note "
                "(digits swapped)",
                "PO-48230",
                2,
                "41010",
                "S275JR",
                "10",
                "B17730",
                5,
                0,
                5,
                "301 MPa",
                "452 MPa",
                ("10.04", "10.07", "10.02", "10.05"),
                cert_heat="B17703",
            ),
        ),
    ),
    Delivery(
        "DN-26-0758",
        date(2026, 10, 12),
        (
            _s355(
                "lot-07",
                1,
                "A42240",
                "one thickness reading below tolerance; the report still marks it OK",
                po_number="PO-48244",
                item="41215",
                thickness="15",
                yield_strength="366 MPa",
                tensile_strength="519 MPa",
                thickness_readings=("15.04", "14.97", "14.38", "15.02"),
            ),
            _s355(
                "lot-08",
                2,
                "A42251",
                "one thickness reading 0.01 mm inside the lower limit, within the gauge's "
                "uncertainty",
                po_number="PO-48244",
                yield_strength="379 MPa",
                tensile_strength="534 MPa",
                thickness_readings=("11.62", "11.51", "11.66", "11.70"),
            ),
        ),
    ),
    Delivery(
        "DN-26-0790",
        date(2026, 10, 14),
        (
            Lot(
                "lot-09",
                "inspector box left blank on the inspection report",
                "PO-48251",
                1,
                "40808",
                "S235JR",
                "8",
                "C08816",
                6,
                0,
                6,
                "281 MPa",
                "423 MPa",
                ("8.05", "8.09", "8.03", "8.06"),
                inspector="",
            ),
            _s355(
                "lot-10",
                2,
                "A42307",
                "near miss: 20 mm plate, yield 348 passes the 345 minimum for 16-40 mm; grade "
                "written 'S355 J2+N'",
                po_number="PO-48251",
                item="41220",
                thickness="20",
                yield_strength="348 MPa",
                tensile_strength="506 MPa",
                thickness_readings=("20.12", "20.06", "20.15", "20.09"),
                width="2009",
                length="6014",
                cert_grade="S355 J2+N",
            ),
            _s355(
                "lot-11",
                3,
                "A42318",
                "near miss: lot-07's failing T3 reading, with the T3 row left off the report",
                po_number="PO-48251",
                item="41215",
                thickness="15",
                yield_strength="368 MPa",
                tensile_strength="522 MPa",
                thickness_readings=("15.05", "14.98", "14.38", "15.03"),
                dropped_rows=("Thickness T3",),
            ),
            _s355(
                "lot-12",
                4,
                "A42326",
                "near miss: yield and tensile inside the limits; no impact energy on the "
                "certificate",
                po_number="PO-48251",
                yield_strength="375 MPa",
                tensile_strength="531 MPa",
                thickness_readings=("12.07", "12.04", "12.09", "12.06"),
                impact=None,
            ),
        ),
    ),
]
LOTS = [lot for delivery in DELIVERIES for lot in delivery.lots]
RECEIVED = {lot.lot: delivery.received for delivery in DELIVERIES for lot in delivery.lots}


# --- Drawing ----------------------------------------------------------------------------------


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)  # type: ignore[return-value]


def _long_date(day: date) -> str:
    return f"{MONTHS[day.month - 1]} {day.day}, {day.year}"


def format_date(day: date, style: str) -> str:
    """A date as a certificate or report writes it; every style is in rules.yaml date_formats."""
    month = MONTHS[day.month - 1]
    return {
        "iso": day.isoformat(),
        "long": _long_date(day),
        "abbr": f"{month[:3]} {day.day}, {day.year}",
        "day_first": f"{day.day} {month} {day.year}",
        "dotted": f"{day.day:02d}.{day.month:02d}.{day.year}",
        "us": f"{day.month:02d}/{day.day:02d}/{day.year}",
    }[style]


def _box(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], label: str, value: str) -> None:
    """One form box: the label small at the top left, the typed value below it."""
    x0, y0, _, _ = box
    draw.rectangle(box, outline=INK, width=2)
    draw.text((x0 + 12, y0 + 8), label, font=_font(20), fill=GREY)
    draw.text((x0 + 12, y0 + 40), value, font=_font(28), fill=INK)


def _rows(draw: ImageDraw.ImageDraw, y: int, rows: list[list[tuple[str, str]]]) -> int:
    """Rows of equal-width boxes across the page; returns the y below the last row."""
    left, right, height = 80, 1160, 96
    for row in rows:
        width = (right - left) // len(row)
        for index, (label, value) in enumerate(row):
            x0 = left + index * width
            _box(draw, (x0, y, x0 + width, y + height), label, value)
        y += height
    return y


def _table(draw: ImageDraw.ImageDraw, y: int, columns: list[int], rows: list[list[str]]) -> int:
    """A ruled table: the first row is the header. Returns the y below it."""
    draw.rectangle((80, y, 1160, y + 48), fill=(225, 230, 236))
    for index, row in enumerate(rows):
        for x, cell in zip(columns, row, strict=True):
            draw.text((x + 10, y + 11), cell, font=_font(24), fill=INK)
        y += 48 if index == 0 else 46
        draw.line((80, y, 1160, y), fill=(200, 200, 200), width=1)
    return y


def _chemistry(lot: Lot) -> list[str]:
    seed = sum(ord(c) for c in lot.lot)
    carbon = {"S355J2+N": 0.16, "S275JR": 0.15, "S235JR": 0.12}[lot.grade] + (seed % 3) / 100
    manganese = {"S355J2+N": 1.42, "S275JR": 1.08, "S235JR": 0.71}[lot.grade] + (seed % 5) / 100
    cev = carbon + manganese / 6 + 0.04
    return [
        f"{carbon:.2f}",
        f"{manganese:.2f}",
        "0.21",
        f"0.0{11 + seed % 6}",
        f"0.00{4 + seed % 4}",
        f"{cev:.2f}",
    ]


def certificate_number(lot: Lot) -> str:
    return f"HSW-26-{3100 + int(lot.lot[-2:]) * 37}"


def draw_certificate(lot: Lot) -> Image.Image:
    page = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(page)
    issued = RECEIVED[lot.lot] - timedelta(days=7)
    width, length = lot.size
    draw.text((80, 70), lot.mill.upper(), font=_font(34), fill=(110, 40, 30))
    draw.text((80, 118), "INSPECTION CERTIFICATE", font=_font(44), fill=INK)
    draw.text((80, 176), "EN 10204 type 3.1", font=_font(24), fill=GREY)
    y = _rows(
        draw,
        230,
        [
            [
                ("Certificate number", certificate_number(lot)),
                (lot.issue_label, format_date(issued, lot.cert_date_style)),
            ],
            [("Customer", CUSTOMER), ("Purchase order", f"{lot.po_number} line {lot.po_line}")],
            [
                ("Product", "Hot-rolled plate"),
                ("Steel grade", lot.cert_grade or lot.note_grade or lot.grade),
            ],
            [("Standard", "EN 10025-2"), (lot.cert_heat_label, lot.cert_heat or lot.heat)],
            [
                ("Thickness", lot.cert_thickness or f"{lot.thickness}.00 mm"),
                ("Width", f"{width} mm"),
                ("Length", f"{length} mm"),
            ],
        ],
    )
    draw.text((80, y + 30), "Mechanical properties (transverse)", font=_font(26), fill=INK)
    impact = "KV at -20 °C" if "J2" in (lot.note_grade or lot.grade) else "KV at +20 °C"
    properties = [("Elongation A", "24 %")]
    if lot.impact is not None:
        properties.append((f"Impact energy {impact}", lot.impact))
    y = _rows(
        draw,
        y + 76,
        [
            [
                ("Yield strength ReH", lot.yield_strength),
                ("Tensile strength Rm", lot.tensile_strength),
            ],
            properties,
        ],
    )
    draw.text((80, y + 30), "Chemical composition (ladle analysis, %)", font=_font(26), fill=INK)
    columns = [80 + i * 180 for i in range(len(CHEMISTRY))]
    y = _table(draw, y + 76, columns, [list(CHEMISTRY), _chemistry(lot)])
    draw.text(
        (80, y + 40),
        "We certify that the products described above comply with the order and the standard.",
        font=_font(22),
        fill=INK,
    )
    draw.text((80, y + 90), "Inspection representative: P. Hallworth", font=_font(24), fill=INK)
    draw.text((80, 1660), FOOTER, font=_font(20), fill=GREY)
    return page


def report_number(lot: Lot) -> str:
    return f"RI-2026-{410 + int(lot.lot[-2:])}"


def draw_report(lot: Lot) -> Image.Image:
    page = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(page)
    inspected = RECEIVED[lot.lot] + timedelta(days=1)
    width, length = lot.size
    draw.text((80, 70), CUSTOMER.upper(), font=_font(34), fill=(30, 60, 110))
    draw.text((80, 118), "RECEIVING INSPECTION REPORT", font=_font(44), fill=INK)
    draw.text((80, 176), "Dimensional check of incoming plate", font=_font(24), fill=GREY)
    heat = lot.report_heat or lot.cert_heat or lot.heat
    y = _rows(
        draw,
        230,
        [
            [
                ("Report number", report_number(lot)),
                (lot.inspection_label, format_date(inspected, lot.report_date_style)),
            ],
            [
                ("Purchase order", f"{lot.po_number} line {lot.po_line}"),
                (lot.report_heat_label, heat),
            ],
            [
                (
                    "Material",
                    f"Plate {lot.note_grade or lot.grade} {lot.thickness} x {width} x {length}",
                ),
                ("Inspector", lot.inspector),
            ],
        ],
    )
    nominal = f"{lot.thickness}.00"
    rows = [["Characteristic", "Nominal (mm)", "Measured (mm)", "Instrument", "Result"]]
    for index, reading in enumerate(lot.thickness_readings, start=1):
        rows.append([f"Thickness T{index}", nominal, reading, "UT gauge", "OK"])
    rows.append(["Width", width, lot.width, "Tape", "OK"])
    rows.append(["Length", length, lot.length, "Tape", "OK"])
    rows = [row for row in rows if row[0] not in lot.dropped_rows]
    y = _table(draw, y + 50, [80, 380, 590, 800, 1000], rows)
    draw.text(
        (80, y + 40),
        "Thickness measured 50 mm in from each corner. Width and length at mid-plate.",
        font=_font(22),
        fill=INK,
    )
    draw.text((80, y + 100), f"Inspector signature: {lot.inspector}", font=_font(24), fill=INK)
    draw.text((80, 1660), FOOTER, font=_font(20), fill=GREY)
    return page


def draw_delivery_note(delivery: Delivery) -> Image.Image:
    """The supplier's delivery note: a few header boxes and one table row per delivered line."""
    page = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(page)
    dispatched = delivery.received - timedelta(days=1)
    draw.text((80, 70), MILL.upper(), font=_font(34), fill=(110, 40, 30))
    draw.text((80, 118), "DELIVERY NOTE", font=_font(44), fill=INK)
    draw.text((80, 176), "Packing list for the goods below", font=_font(24), fill=GREY)
    y = _rows(
        draw,
        230,
        [
            [("Delivery note", delivery.note), ("Dispatch date", dispatched.isoformat())],
            [("Customer", CUSTOMER), ("Delivery address", "Goods-in, Unit 4, Calder Works")],
        ],
    )
    rows = [["PO number", "Line", "Item", "Grade", "Size (mm)", "Heat number", "Qty"]]
    for lot in delivery.lots:
        width, length = lot.size
        rows.append(
            [
                *(lot.po_number, str(lot.po_line), lot.item, lot.note_grade or lot.grade),
                *(f"{lot.thickness}x{width}x{length}", lot.heat, str(lot.delivered)),
            ]
        )
    y = _table(draw, y + 60, [80, 250, 340, 460, 620, 840, 1030], rows)
    plates = sum(lot.delivered for lot in delivery.lots)
    draw.text((80, y + 40), f"Total plates: {plates}", font=_font(24), fill=INK)
    draw.text((80, y + 100), "Qty in plates. Mill certificates enclosed.", font=_font(22), fill=INK)
    draw.text((80, 1660), FOOTER, font=_font(20), fill=GREY)
    return page


def _save_pdf(path: Path, pages: list[Image.Image]) -> None:
    """An image-only PDF with no document info (no title, author or dates), so it rebuilds byte
    for byte."""
    pages[0].save(
        path,
        format="PDF",
        save_all=True,
        append_images=pages[1:],
        resolution=DPI,
        title="",
        creationDate="",
        modDate="",
    )


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with (DATA_DIR / "po_lines.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            [
                *("po_number", "line", "item", "supplier", "grade"),
                *("thickness_mm", "width_mm", "length_mm", "ordered_qty", "received_qty"),
            ]
        )
        for lot in LOTS:
            writer.writerow(
                [
                    *(lot.po_number, lot.po_line, lot.item, MILL, lot.grade),
                    *(lot.thickness, *lot.size, lot.ordered, lot.previously_received),
                ]
            )
    _save_pdf(DATA_DIR / "delivery-notes.pdf", [draw_delivery_note(d) for d in DELIVERIES])
    _save_pdf(DATA_DIR / "mill-certificates.pdf", [draw_certificate(lot) for lot in LOTS])
    _save_pdf(DATA_DIR / "inspection-reports.pdf", [draw_report(lot) for lot in LOTS])
    pages = {lot.lot: page for page, lot in enumerate(LOTS, start=1)}
    lines = [
        json.dumps(
            {
                "delivery": delivery.note,
                "received": delivery.received.isoformat(),
                "delivery_note": {"file": "delivery-notes.pdf", "page": number},
                "lots": [
                    {
                        "lot": lot.lot,
                        "delivery_line": line,
                        "certificate": {"file": "mill-certificates.pdf", "page": pages[lot.lot]},
                        "inspection_report": {
                            "file": "inspection-reports.pdf",
                            "page": pages[lot.lot],
                        },
                    }
                    for line, lot in enumerate(delivery.lots, start=1)
                ],
            }
        )
        for number, delivery in enumerate(DELIVERIES, start=1)
    ]
    (DATA_DIR / "receipts.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {len(DELIVERIES)} deliveries and {len(LOTS)} lots to {DATA_DIR}")


if __name__ == "__main__":
    main()
