"""Write the synthetic claims-system seeds and the scanned repair invoices in data/.

Everything is invented. The insurer ("Larkspur Mutual"), the insureds, the repair shops, the
addresses, the vehicles and their VINs, and every number are made up, and every invoice says so
in its footer. Every VIN carries a Q, a letter real VINs never use.

    uv run make_data.py

writes four CSV seeds for claims_system.py (claims.csv, claim_vendors.csv,
authorizations.csv and claim_documents.csv) and one image-only PDF, repair-invoices.pdf, as a
scanner or a print-to-PDF of a scan would produce it, with no document metadata. Rerunning
rebuilds every file byte for byte.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"

DPI = 150
RECEIVED = date(2026, 10, 20)  # every invoice landed on its claim that day
INVOICE_PDF = "repair-invoices.pdf"
MONTHS = [
    *("January", "February", "March", "April", "May", "June"),
    *("July", "August", "September", "October", "November", "December"),
]
FOOTER = "Fictional sample generated for a software demo. Not a real invoice, shop or claim."
INK = (25, 25, 30)
GREY = (95, 95, 110)
D = Decimal


@dataclass(frozen=True)
class LineItem:
    description: str
    qty: int
    unit_price: Decimal

    @property
    def amount(self) -> Decimal:
        return self.qty * self.unit_price


@dataclass(frozen=True)
class Invoice:
    vendor: str
    vendor_address: str
    number: str
    issued: date
    bill_to: str
    claim_number: str | None  # as printed; None leaves the line off the invoice
    location: str  # the service address, or the vehicle and its VIN
    items: tuple[LineItem, ...]
    tax_rate: Decimal  # percent
    printed_subtotal: Decimal | None = None  # an error we added; None prints the true subtotal
    printed_total: Decimal | None = None  # an error we added; None prints subtotal + tax
    tax_blank: bool = False  # a gap we added: the tax line has a rate and no amount

    @property
    def subtotal(self) -> Decimal:
        return self.printed_subtotal or sum((i.amount for i in self.items), D("0"))

    @property
    def tax(self) -> Decimal:
        return (self.subtotal * self.tax_rate / 100).quantize(D("0.01"))

    @property
    def total(self) -> Decimal:
        return self.printed_total or self.subtotal + self.tax


@dataclass(frozen=True)
class Claim:
    document: str  # the invoice's document id in the claims system; the case
    what_it_tests: str
    claim_number: str
    policy_number: str
    line: str
    insured: str
    loss_date: date
    loss_address: str  # where the loss happened; for auto, where the accident was
    vehicle: str  # auto only
    vin: str  # auto only
    vendors: tuple[str, ...]  # the vendors approved on the claim
    # (kind, amount, approved on); None as the amount means "the invoice total"
    authorizations: tuple[tuple[str, Decimal | None, date], ...]
    invoice: Invoice


def _vehicle(vehicle: str, vin: str) -> str:
    return f"{vehicle}, VIN {vin}"


CLAIMS = [
    Claim(
        "clm-01",
        "matches its claim in every field",
        "CLM-26-104817",
        "HO-2026-004817",
        "Homeowners",
        "Dana Whitcombe",
        date(2026, 10, 4),
        "41 Fernhollow Lane, Ashby Vale, CO 80911",
        "",
        "",
        ("Brightwater Roofing & Gutters Ltd",),
        (("estimate", D("3650.00"), date(2026, 10, 8)),),
        Invoice(
            "Brightwater Roofing & Gutters Ltd",
            "220 Millrace Road, Ashby Vale, CO 80910",
            "BRG-10442",
            date(2026, 10, 12),
            "Dana Whitcombe",
            "CLM-26-104817",
            "41 Fernhollow Lane, Ashby Vale, CO 80911",
            (
                LineItem("Remove storm-damaged shingles", 1, D("640.00")),
                LineItem("Architectural shingles (bundle)", 24, D("42.50")),
                LineItem("Synthetic underlayment (roll)", 3, D("68.00")),
                LineItem("Roofing labor (hours)", 18, D("85.00")),
            ),
            D("6"),
        ),
    ),
    Claim(
        "clm-02",
        "near miss: vendor written 'and ... Inc.' vs '&'; total equals the authorized amount",
        "CLM-26-211532",
        "PA-2026-011532",
        "Personal auto",
        "Marcus Ellery-Thorne",
        date(2026, 9, 1),
        "Quarry Road and Hale Street, Brookmere, CO 80422",
        "2021 Mazda CX-5",
        "JM3QKFBC1M0412387",
        ("Kestrel Auto Body & Paint",),
        (("estimate", None, date(2026, 9, 5)),),
        Invoice(
            "Kestrel Auto Body and Paint, Inc.",
            "9 Foundry Street, Brookmere, CO 80421",
            "KAB-2026-0917",
            date(2026, 9, 18),
            "Marcus Ellery-Thorne",
            "CLM-26-211532",
            _vehicle("2021 Mazda CX-5", "JM3QKFBC1M0412387"),
            (
                LineItem("Rear bumper cover (OEM)", 1, D("612.00")),
                LineItem("Tail lamp assembly, left", 1, D("348.00")),
                LineItem("Body labor (hours)", 9, D("96.00")),
                LineItem("Paint and materials", 1, D("455.00")),
            ),
            D("7"),
        ),
    ),
    Claim(
        "clm-03",
        "invoice dated before the date of loss",
        "CLM-26-307201",
        "HO-2026-007201",
        "Homeowners",
        "Priya Vantongeren",
        date(2026, 8, 14),
        "7 Larch Court, Pell Harbor, CO 80634",
        "",
        "",
        ("Northgate Plumbing Co.",),
        (("estimate", D("1200.00"), date(2026, 8, 17)),),
        Invoice(
            "Northgate Plumbing Co.",
            "1180 Tanner Avenue, Pell Harbor, CO 80632",
            "NP-58201",
            date(2026, 8, 9),
            "Priya Vantongeren",
            "CLM-26-307201",
            "7 Larch Court, Pell Harbor, CO 80634",
            (
                LineItem("Emergency call-out", 1, D("180.00")),
                LineItem("Replace supply hoses and valve", 1, D("146.00")),
                LineItem("Subfloor drying (days)", 3, D("140.00")),
                LineItem("Vinyl plank flooring (sq ft)", 60, D("7.00")),
            ),
            D("0"),
        ),
    ),
    Claim(
        "clm-04",
        "claim number on the invoice is missing a digit",
        "CLM-26-104392",
        "HO-2026-004392",
        "Homeowners",
        "Tomasz Brightman",
        date(2026, 9, 27),
        "88 Orchard Rise, Ashby Vale, CO 80911",
        "",
        "",
        ("Greenfield Fence & Deck",),
        (("estimate", D("1150.00"), date(2026, 10, 1)),),
        Invoice(
            "Greenfield Fence & Deck",
            "45 Cooper Lane, Ashby Vale, CO 80912",
            "GFD-3307",
            date(2026, 10, 6),
            "Tomasz Brightman",
            "CLM-26-10432",
            "88 Orchard Rise, Ashby Vale, CO 80911",
            (
                LineItem("Cedar fence panel", 2, D("230.00")),
                LineItem("Fence post, set in concrete", 1, D("160.00")),
                LineItem("Install labor (hours)", 5, D("80.00")),
                LineItem("Debris haul-away", 1, D("60.00")),
            ),
            D("6"),
        ),
    ),
    Claim(
        "clm-05",
        "subtotal 2,410.00; the lines add up to 2,140.00",
        "CLM-25-419844",
        "HO-2025-019844",
        "Homeowners",
        "Ingrid Solvang",
        date(2026, 9, 14),
        "3 Willowmere Drive, Pell Harbor, CO 80633",
        "",
        "",
        ("Clearview Skylight Repair",),
        (("estimate", D("2600.00"), date(2026, 9, 18)),),
        Invoice(
            "Clearview Skylight Repair",
            "602 Glassworks Row, Pell Harbor, CO 80631",
            "CSR-7716",
            date(2026, 9, 29),
            "Ingrid Solvang",
            "CLM-25-419844",
            "3 Willowmere Drive, Pell Harbor, CO 80633",
            (
                LineItem("Skylight unit, 2 x 4 ft", 2, D("590.00")),
                LineItem("Flashing kit", 2, D("95.00")),
                LineItem("Install labor (hours)", 6, D("90.00")),
                LineItem("Ceiling stain repair and paint", 1, D("230.00")),
            ),
            D("6"),
            printed_subtotal=D("2410.00"),
        ),
    ),
    Claim(
        "clm-06",
        "invoice from a different glass shop than the one on the claim",
        "CLM-26-502265",
        "PA-2026-002265",
        "Personal auto",
        "Rafael Oduya",
        date(2026, 10, 2),
        "Interstate 25 northbound near exit 140, CO",
        "2019 Honda Accord",
        "1HGQV1F34KA052265",
        ("Pinecrest Glass Co.",),
        (("estimate", D("600.00"), date(2026, 10, 3)),),
        Invoice(
            "Halverson Glass & Glazing LLC",
            "17 Station Approach, Brookmere, CO 80423",
            "HGG-40981",
            date(2026, 10, 7),
            "Rafael Oduya",
            "CLM-26-502265",
            _vehicle("2019 Honda Accord", "1HGQV1F34KA052265"),
            (
                LineItem("Windshield, acoustic laminated", 1, D("389.00")),
                LineItem("Urethane and moulding kit", 1, D("46.00")),
                LineItem("Install labor (hours)", 1, D("110.00")),
            ),
            D("7"),
        ),
    ),
    Claim(
        "clm-07",
        "invoice total 6,800.00; authorized 6,262.48",
        "CLM-26-612077",
        "HO-2026-012077",
        "Homeowners",
        "Celia Marchbank",
        date(2026, 10, 9),
        "112 Gorse Hill Road, Brookmere, CO 80422",
        "",
        "",
        ("Ridgeway Restoration Services",),
        (("estimate", D("6262.48"), date(2026, 10, 11)),),
        Invoice(
            "Ridgeway Restoration Services",
            "300 Kiln Street, Brookmere, CO 80421",
            "RRS-12054",
            date(2026, 10, 16),
            "Celia Marchbank",
            "CLM-26-612077",
            "112 Gorse Hill Road, Brookmere, CO 80422",
            (
                LineItem("Smoke and soot cleaning (rooms)", 2, D("640.00")),
                LineItem("Range hood, stainless", 1, D("720.00")),
                LineItem("Wall cabinet, painted maple", 2, D("890.00")),
                LineItem("Carpentry labor (hours)", 16, D("95.00")),
                LineItem("Drywall and paint", 1, D("1115.09")),
            ),
            D("6"),
        ),
    ),
    Claim(
        "clm-08",
        "tax line blank; the total includes tax",
        "CLM-26-709315",
        "HO-2026-009315",
        "Homeowners",
        "Hana Lindqvist",
        date(2026, 10, 5),
        "56 Saltmarsh Lane, Pell Harbor, CO 80634",
        "",
        "",
        ("Northgate Plumbing Co.",),
        (("estimate", D("1400.00"), date(2026, 10, 7)),),
        Invoice(
            "Northgate Plumbing Co.",
            "1180 Tanner Avenue, Pell Harbor, CO 80632",
            "NP-58377",
            date(2026, 10, 11),
            "Hana Lindqvist",
            "CLM-26-709315",
            "56 Saltmarsh Lane, Pell Harbor, CO 80634",
            (
                LineItem("Sump pump, 1/2 HP", 1, D("410.00")),
                LineItem("Pump install labor (hours)", 3, D("120.00")),
                LineItem("Water extraction (hours)", 4, D("125.00")),
            ),
            D("6"),
            tax_blank=True,
        ),
    ),
    Claim(
        "clm-09",
        "near miss: right shop, total equals the estimate, billed to another customer at "
        "another address, no claim number",
        "CLM-26-118206",
        "HO-2026-018206",
        "Homeowners",
        "Tobias Fenwick",
        date(2026, 10, 1),
        "19 Copperbeech Way, Ashby Vale, CO 80911",
        "",
        "",
        ("Brightwater Roofing & Gutters Ltd",),
        (("estimate", None, date(2026, 10, 6)),),
        Invoice(
            "Brightwater Roofing & Gutters Ltd",
            "220 Millrace Road, Ashby Vale, CO 80910",
            "BRG-10457",
            date(2026, 10, 14),
            "Margit Allanby",
            None,
            "7 Thornfield Close, Ashby Vale, CO 80912",
            (
                LineItem("Gutter run, seamless aluminum (ft)", 64, D("14.50")),
                LineItem("Downspout and elbows", 2, D("85.00")),
                LineItem("Fascia board repair", 1, D("310.00")),
                LineItem("Install labor (hours)", 6, D("85.00")),
            ),
            D("6"),
        ),
    ),
    Claim(
        "clm-10",
        "near miss: total 1,180.00 over the first estimate, with an approved supplement for it",
        "CLM-26-224417",
        "PA-2026-024417",
        "Personal auto",
        "Lena Okafor-Reyes",
        date(2026, 9, 22),
        "Elm Street at Prospect Avenue, Pell Harbor, CO 80633",
        "2020 Toyota RAV4",
        "2T3QWRFV5LW024417",
        ("Marlow Collision Center",),
        (
            ("estimate", None, date(2026, 9, 25)),
            ("supplement", D("1180.00"), date(2026, 10, 8)),
        ),
        Invoice(
            "Marlow Collision Center",
            "58 Depot Road, Pell Harbor, CO 80632",
            "MCC-88213",
            date(2026, 10, 15),
            "Lena Okafor-Reyes",
            "CLM-26-224417",
            _vehicle("2020 Toyota RAV4", "2T3QWRFV5LW024417"),
            (
                LineItem("Front bumper cover (OEM)", 1, D("684.00")),
                LineItem("Headlamp assembly, right", 1, D("512.00")),
                LineItem("Radiator support (found on teardown)", 1, D("640.00")),
                LineItem("Body labor (hours)", 14, D("96.00")),
                LineItem("Paint and materials", 1, D("520.00")),
            ),
            D("7"),
        ),
    ),
]


# --- Drawing ----------------------------------------------------------------------------------


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)  # type: ignore[return-value]


def _long_date(day: date) -> str:
    return f"{MONTHS[day.month - 1]} {day.day}, {day.year}"


def _money(amount: Decimal) -> str:
    return f"{amount:,.2f}"


def draw_invoice(claim: Claim) -> Image.Image:
    invoice = claim.invoice
    page = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(page)
    draw.text((80, 70), invoice.vendor, font=_font(42), fill=(20, 80, 70))
    draw.text((80, 130), invoice.vendor_address, font=_font(24), fill=GREY)
    draw.text((900, 70), "INVOICE", font=_font(46), fill=INK)
    header = [
        f"Invoice number: {invoice.number}",
        f"Invoice date: {_long_date(invoice.issued)}",
        f"Bill to: {invoice.bill_to}",
    ]
    if invoice.claim_number is not None:
        header.append(f"Claim number: {invoice.claim_number}")
    where = "Vehicle" if claim.vin else "Service address"
    header.append(f"{where}: {invoice.location}")
    y = 220
    for text in header:
        draw.text((80, y), text, font=_font(26), fill=INK)
        y += 46
    y += 30
    columns = [(80, "Description"), (700, "Qty"), (820, "Unit price"), (1020, "Amount")]
    draw.rectangle((80, y, 1160, y + 50), fill=(225, 232, 230))
    for x, title in columns:
        draw.text((x + 10, y + 12), title, font=_font(24), fill=INK)
    y += 60
    for item in invoice.items:
        cells = [item.description, str(item.qty), _money(item.unit_price), _money(item.amount)]
        for (x, _), cell in zip(columns, cells, strict=True):
            draw.text((x + 10, y + 8), cell, font=_font(24), fill=INK)
        y += 46
        draw.line((80, y, 1160, y), fill=(200, 200, 200), width=1)
    y += 30
    totals = [
        ("Subtotal", _money(invoice.subtotal)),
        (f"Sales tax ({invoice.tax_rate}%)", "" if invoice.tax_blank else _money(invoice.tax)),
        ("Total due", "$" + _money(invoice.total)),
    ]
    for label, amount in totals:
        draw.text((760, y), label, font=_font(26), fill=INK)
        width = draw.textlength(amount, font=_font(26))
        draw.text((1150 - width, y), amount, font=_font(26), fill=INK)
        y += 50
    draw.text(
        (80, y + 60),
        "Payment due within 30 days. Thank you for your business.",
        font=_font(22),
        fill=INK,
    )
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


# --- The claims system's seeds ----------------------------------------------------------------


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def write_seeds(data_dir: Path) -> None:
    _write_csv(
        data_dir / "claims.csv",
        [
            *("claim_number", "status", "policy_number", "line", "named_insured", "loss_date"),
            *("loss_address", "vehicle", "vin"),
        ],
        [
            [
                *(claim.claim_number, "open", claim.policy_number, claim.line, claim.insured),
                *(claim.loss_date.isoformat(), claim.loss_address, claim.vehicle, claim.vin),
            ]
            for claim in CLAIMS
        ],
    )
    _write_csv(
        data_dir / "claim_vendors.csv",
        ["claim_number", "vendor"],
        [[claim.claim_number, vendor] for claim in CLAIMS for vendor in claim.vendors],
    )
    rows = []
    for claim in CLAIMS:
        supplements = sum((a or D("0") for _, a, _ in claim.authorizations), D("0"))
        for kind, amount, approved in claim.authorizations:
            # An estimate given as None is the invoice total less any supplements: the shop
            # billed exactly what was authorized.
            value = amount if amount is not None else claim.invoice.total - supplements
            rows.append([claim.claim_number, kind, f"{value:.2f}", approved.isoformat()])
    _write_csv(
        data_dir / "authorizations.csv", ["claim_number", "kind", "amount", "approved_on"], rows
    )
    _write_csv(
        data_dir / "claim_documents.csv",
        ["document", "claim_number", "kind", "received", "file", "page"],
        [
            [
                *(claim.document, claim.claim_number, "repair_invoice", RECEIVED.isoformat()),
                *(INVOICE_PDF, str(page)),
            ]
            for page, claim in enumerate(CLAIMS, start=1)
        ],
    )


def main() -> None:
    argparse.ArgumentParser(
        description="Write the synthetic claims-system seeds and repair invoices."
    ).parse_args()
    DATA_DIR.mkdir(exist_ok=True)
    write_seeds(DATA_DIR)
    _save_pdf(DATA_DIR / INVOICE_PDF, [draw_invoice(claim) for claim in CLAIMS])
    print(f"wrote {len(CLAIMS)} claims and their invoices to {DATA_DIR}")


if __name__ == "__main__":
    main()
