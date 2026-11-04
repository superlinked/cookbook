"""Write the synthetic contract packages and the register of information in data/.

Five ICT contracts between a fictional bank and five fictional suppliers. Each package is the
signed agreement with its schedules and, for one supplier, a later amendment:

- e-signed or Word-exported agreements get a text layer (a small stdlib PDF writer below);
- older or supplier-paper documents are image-only scans (Pillow, with skew, speckle and blur);
- one scanned page is blurred past reading, the way a bad fax copy comes out.

The register of information (register_contracts.csv) and the contract repository's document
list (register_documents.csv) are written next to them. Every party, person and address is
invented, and every page says so in its footer. The PDFs carry no document metadata.

    uv run --group data make_data.py
"""

from __future__ import annotations

import csv
import random
import textwrap
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"

CUSTOMER = "Eastmere Bank plc"
CUSTOMER_ADDRESS = "1 Quayside Row, Bristol BS1 6XE"
FOOTER = "Synthetic contract for the SIE Cookbook. Every party is fictional."

# Scans: A4 at 150 DPI.
DPI = 150
PAGE_SIZE = (1240, 1754)
MARGIN = 120
FONT_SIZE = 22
LINE_GAP = 10

SERVICES = (
    "2. SERVICES AND SUBCONTRACTING",
    "2.1 The Supplier shall provide the services described in Schedule 1 (the Services), being "
    "{services}. Schedule 1 is a clear and complete description of every function and ICT "
    "service provided under this Agreement.",
    "2.2 The Supplier may subcontract part of the Services only with the Customer's prior "
    "written consent. Any approved subcontractor is bound by the same obligations as the "
    "Supplier, and the Supplier remains fully responsible for the subcontractor's performance.",
)
SERVICE_LEVELS = (
    "3. SERVICE LEVELS",
    "3.1 The Supplier shall meet the service levels in Schedule 2, including monthly "
    "availability of 99.9%, a four-hour response time for priority 1 tickets and a recovery "
    "time objective of eight hours.",
    "3.2 Service levels are reviewed every twelve months. Any update or revision takes effect "
    "only when agreed in writing and recorded as a new version of Schedule 2.",
)
LOCATIONS = (
    "4. LOCATIONS OF SERVICE AND DATA",
    "4.1 The Services are provided from, and Customer Data is processed and stored in, "
    "{locations} only.",
    "4.2 The Supplier shall notify the Customer in writing at least ninety (90) days before any "
    "change to the countries or regions in which the Services are provided or Customer Data is "
    "processed or stored.",
)
DATA_SECURITY = (
    "5. DATA PROTECTION AND SECURITY",
    "5.1 The Supplier shall ensure the availability, authenticity, integrity and "
    "confidentiality of Customer Data, including personal data, in accordance with Applicable "
    "Data Protection Law and the security measures in Schedule 3.",
    "5.2 Customer Data is encrypted in transit and at rest. Access is limited to named "
    "personnel with a business need.",
)
INCIDENT_ASSISTANCE = (
    "6. ICT INCIDENT ASSISTANCE",
    "6.1 When an ICT incident related to the Services occurs, the Supplier shall provide the "
    "Customer with all reasonable assistance to contain, investigate and recover from it, at no "
    "additional cost to the Customer.",
)
INCIDENT_ASSISTANCE_WEAKENED = (
    "6. ICT INCIDENT ASSISTANCE",
    "6.1 When an ICT incident related to the Services occurs, the Supplier may, at its "
    "discretion, provide such assistance as it considers reasonable. Any assistance is charged "
    "at the Supplier's then-current time-and-materials rates.",
)
AUTHORITIES = (
    "7. COOPERATION WITH AUTHORITIES",
    "7.1 The Supplier shall cooperate fully with the competent authorities and resolution "
    "authorities of the Customer, including persons appointed by them, and shall give them "
    "access to information, premises and personnel relating to the Services.",
)
FEES = (
    "8. FEES AND PAYMENT",
    "8.1 The Customer shall pay the fees in Schedule 4 monthly in arrears within thirty (30) "
    "days of a valid invoice.",
    "8.2 Fees are fixed for the Initial Term. Late payments bear interest at two percent above "
    "the base rate.",
)
TERMINATION = (
    "9. TERM AND TERMINATION",
    "9.1 This Agreement runs for an Initial Term of three years and renews for successive "
    "one-year periods.",
    "9.2 The Customer may terminate this Agreement on thirty (30) days' written notice if the "
    "Supplier materially breaches applicable law or this Agreement, if circumstances arise that "
    "alter the performance of the Services, or if a competent authority can no longer "
    "effectively supervise the Customer because of this Agreement. The Customer may also "
    "terminate for convenience on six (6) months' written notice.",
)
EXIT = (
    "10. EXIT AND RETURN OF DATA",
    "10.1 On termination, expiry, insolvency, resolution or discontinuation of the Supplier's "
    "business, the Supplier shall give the Customer access to, recover and return all Customer "
    "Data, personal and non-personal, in an easily accessible, machine-readable format within "
    "thirty (30) days, and then delete its copies.",
)
LAW = (
    "11. GOVERNING LAW",
    "11.1 This Agreement is governed by the laws of England and Wales. The courts of London "
    "have exclusive jurisdiction.",
)
ALL_SECTIONS = (
    SERVICES,
    SERVICE_LEVELS,
    LOCATIONS,
    DATA_SECURITY,
    INCIDENT_ASSISTANCE,
    AUTHORITIES,
    FEES,
    TERMINATION,
    EXIT,
    LAW,
)

SCHEDULES = {
    1: (
        "Schedule 1: Services",
        "{services}, including monitoring, backup and restore, capacity management and a "
        "service desk open every day of the year.",
    ),
    2: (
        "Schedule 2: Service levels",
        "Monthly availability: 99.9%. Priority 1 response: four hours. Priority 2 response: one "
        "business day. Recovery time objective: eight hours. Recovery point objective: fifteen "
        "minutes. Measured monthly and reported by the tenth business day of the next month.",
    ),
    3: (
        "Schedule 3: Security measures",
        "Encryption in transit with TLS 1.2 or later and at rest with AES-256. Multi-factor "
        "authentication for every administrative account. An annual penetration test by an "
        "independent tester, with the summary shared with the Customer.",
    ),
    4: (
        "Schedule 4: Fees",
        "A fixed monthly fee of {fee}, invoiced monthly in arrears. Changes to the Services "
        "are priced by written change request.",
    ),
}


@dataclass(frozen=True)
class Agreement:
    contract_id: str
    filename: str
    supplier: str
    supplier_address: str
    services: str
    locations: str
    fee: str
    signed_on: str  # as printed
    signed_iso: str  # as the register holds it
    critical: bool
    signatories: tuple[str, str]
    sections: tuple[tuple[str, ...], ...]
    schedules: tuple[int, ...]  # the schedules attached to the agreement
    scanned: bool
    seed: int
    blurred_page: int | None = None  # a scanned page blurred past reading (1-based)


AGREEMENTS = (
    # Complete: every clause, plainly worded. E-signed, so it has a text layer.
    Agreement(
        contract_id="EB-ICT-0117",
        filename="nimbusline-cloud-hosting.pdf",
        supplier="Nimbusline Cloud Ltd",
        supplier_address="14 Fernhollow Yard, Leeds LS9 4QT",
        services="managed cloud hosting of the Customer's payments ledger and its backups",
        locations="Ireland and the Netherlands",
        fee="EUR 18,400",
        signed_on="12 March 2023",
        signed_iso="2023-03-12",
        critical=True,
        signatories=("Adaeze Marlow, Head of Procurement", "Pieter Vossenaar, Managing Director"),
        sections=ALL_SECTIONS,
        schedules=(1, 2, 3, 4),
        scanned=False,
        seed=11,
    ),
    # No locations clause and no incident-assistance clause. Supplier paper, scanned. Its
    # Schedule 4 (fees) was never filed in the repository.
    Agreement(
        contract_id="EB-ICT-0098",
        filename="brennick-data-services.pdf",
        supplier="Brennick Data Services GmbH",
        supplier_address="Lindqvistweg 8, 20457 Hamburg",
        services="operation of the Customer's customer-statement print and archive platform",
        locations="",
        fee="EUR 6,250",
        signed_on="2 June 2021",
        signed_iso="2021-06-02",
        critical=False,
        signatories=(
            "Adaeze Marlow, Head of Procurement",
            "Henrike Talbrecht, Geschaeftsfuehrerin",
        ),
        sections=tuple(s for s in ALL_SECTIONS if s not in (LOCATIONS, INCIDENT_ASSISTANCE)),
        schedules=(1, 2, 3),
        scanned=True,
        seed=23,
    ),
    # Incident assistance is discretionary and billed at then-current rates. Word export.
    Agreement(
        contract_id="EB-ICT-0142",
        filename="tallowmere-analytics.pdf",
        supplier="Tallowmere Analytics S.A.",
        supplier_address="27 rue des Vanniers, L-1430 Luxembourg",
        services="a hosted fraud-scoring service for card transactions",
        locations="Luxembourg and Belgium",
        fee="EUR 11,900",
        signed_on="19 August 2024",
        signed_iso="2024-08-19",
        critical=True,
        signatories=("Adaeze Marlow, Head of Procurement", "Lucien Ardevaux, Administrateur"),
        sections=tuple(
            INCIDENT_ASSISTANCE_WEAKENED if s is INCIDENT_ASSISTANCE else s for s in ALL_SECTIONS
        ),
        schedules=(1, 2, 3, 4),
        scanned=False,
        seed=37,
    ),
    # A compliant termination clause, later amended to add a termination fee (see AMENDMENT).
    Agreement(
        contract_id="EB-ICT-0076",
        filename="brindlecote-payments-gateway.pdf",
        supplier="Brindlecote Payments B.V.",
        supplier_address="Keizersgracht 911, 1017 DD Amsterdam",
        services="a card payments gateway for the Customer's online banking channel",
        locations="the Netherlands and Germany",
        fee="EUR 22,700",
        signed_on="5 October 2022",
        signed_iso="2022-10-05",
        critical=True,
        signatories=("Adaeze Marlow, Head of Procurement", "Maarten Olderveld, Director"),
        sections=ALL_SECTIONS,
        schedules=(1, 2, 3, 4),
        scanned=False,
        seed=41,
    ),
    # Every clause is in the agreement, but the schedules page came back from the fax blurred.
    Agreement(
        contract_id="EB-ICT-0051",
        filename="brackwater-records-archive.pdf",
        supplier="Brackwater Records Ltd",
        supplier_address="3 Tanner's Wharf, Hull HU1 2PL",
        services="offsite storage and retrieval of the Customer's archived loan files",
        locations="the United Kingdom",
        fee="GBP 3,100",
        signed_on="28 September 2020",
        signed_iso="2020-09-28",
        critical=False,
        signatories=("Adaeze Marlow, Head of Procurement", "Gwen Ashdown, Operations Director"),
        sections=ALL_SECTIONS,
        schedules=(1, 2, 3, 4),
        scanned=True,
        seed=53,
        blurred_page=3,
    ),
)

AMENDMENT_FILE = "brindlecote-amendment-1.pdf"
AMENDMENT_SIGNED = ("14 April 2025", "2025-04-14")


def termination_number(agreement: Agreement) -> int:
    """The section number termination ends up with after renumbering."""
    return 2 + agreement.sections.index(TERMINATION)


def agreement_paragraphs(agreement: Agreement) -> list[tuple[str, str]]:
    """(style, text) pairs in reading order. Sections are renumbered after a clause is dropped,
    so a gap in the numbering doesn't give the missing clause away."""
    paragraphs = [
        ("title", "ICT SERVICES AGREEMENT"),
        (
            "body",
            f"This Agreement is made on {agreement.signed_on} between {CUSTOMER}, "
            f"{CUSTOMER_ADDRESS} (the Customer), and {agreement.supplier}, "
            f"{agreement.supplier_address} (the Supplier).",
        ),
        ("heading", "1. DEFINITIONS"),
        (
            "body",
            "1.1 Customer Data means all data, including personal data, that the Supplier "
            "receives or creates for the Customer. ICT incident has the meaning given in "
            "Regulation (EU) 2022/2554.",
        ),
    ]
    for number, (heading, *clauses) in enumerate(agreement.sections, start=2):
        old = heading.split(".", 1)[0]
        paragraphs.append(("heading", heading.replace(f"{old}.", f"{number}.", 1)))
        for clause in clauses:
            text = clause.format(services=agreement.services, locations=agreement.locations)
            paragraphs.append(("body", text.replace(f"{old}.", f"{number}.", 1)))
    paragraphs.append(("body", "Signed for and on behalf of the parties:"))
    paragraphs.extend(("signature", name) for name in agreement.signatories)
    paragraphs.append(("pagebreak", ""))
    paragraphs.append(("title", "SCHEDULES"))
    for number in agreement.schedules:
        heading, text = SCHEDULES[number]
        paragraphs.append(("heading", heading))
        services = agreement.services[0].upper() + agreement.services[1:]
        paragraphs.append(("body", text.format(services=services, fee=agreement.fee)))
    return paragraphs


def amendment_paragraphs(agreement: Agreement) -> list[tuple[str, str]]:
    clause = f"{termination_number(agreement)}.2"
    return [
        ("title", "AMENDMENT No. 1 TO THE ICT SERVICES AGREEMENT"),
        (
            "body",
            f"This Amendment No. 1 is made on {AMENDMENT_SIGNED[0]} between {CUSTOMER} (the "
            f"Customer) and {agreement.supplier} (the Supplier), and amends the ICT Services "
            f"Agreement between them dated {agreement.signed_on} (the Agreement).",
        ),
        (
            "body",
            f"1. Clause {clause} of the Agreement is amended by adding at the end: Any "
            f"termination by the Customer under this clause {clause}, other than for the "
            "Supplier's material breach, is subject to a termination fee equal to the fees "
            "payable for the six (6) months following the date of termination.",
        ),
        (
            "body",
            "2. All other terms of the Agreement remain in full force and effect. Words defined "
            "in the Agreement have the same meaning in this Amendment.",
        ),
        ("body", "Signed for and on behalf of the parties:"),
        ("signature", agreement.signatories[0]),
        ("signature", agreement.signatories[1]),
    ]


# --- Scanned pages (Pillow) -------------------------------------------------------------------


def _wrap(text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if font.getlength(candidate) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _new_page() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    page = Image.new("L", PAGE_SIZE, 255)
    return page, ImageDraw.Draw(page)


def _scribble(draw: ImageDraw.ImageDraw, x: int, y: int, rng: random.Random) -> None:
    points = [(x + step * 14, y + rng.randint(-18, 18)) for step in range(16)]
    draw.line(points, fill=40, width=3, joint="curve")


def render_scan(
    paragraphs: list[tuple[str, str]], seed: int, blurred: int | None
) -> list[Image.Image]:
    rng = random.Random(seed)
    body = ImageFont.load_default(size=FONT_SIZE)
    fonts = {
        "title": ImageFont.load_default(size=FONT_SIZE + 12),
        "heading": ImageFont.load_default(size=FONT_SIZE + 2),
        "body": body,
        "signature": body,
    }
    text_width = PAGE_SIZE[0] - 2 * MARGIN
    pages: list[Image.Image] = []
    page, draw = _new_page()
    y = MARGIN
    for style, text in paragraphs:
        if style == "pagebreak":
            pages.append(page)
            page, draw = _new_page()
            y = MARGIN
            continue
        font = fonts[style]
        lines = _wrap(text, font, text_width)
        extra = {"signature": 90, "heading": 120}.get(style, 18)
        if y + len(lines) * (FONT_SIZE + LINE_GAP) + extra > PAGE_SIZE[1] - MARGIN:
            pages.append(page)
            page, draw = _new_page()
            y = MARGIN
        if style == "signature":
            _scribble(draw, MARGIN + 10, y + 30, rng)
            y += 60
            draw.line([(MARGIN, y), (MARGIN + 420, y)], fill=0, width=1)
            y += 8
        for line in lines:
            draw.text((MARGIN, y), line, font=font, fill=0)
            y += FONT_SIZE + LINE_GAP
        y += 18
    pages.append(page)
    for number, finished in enumerate(pages, start=1):
        draw = ImageDraw.Draw(finished)
        draw.text((MARGIN, PAGE_SIZE[1] - 100), FOOTER, font=body, fill=0)
        draw.text((MARGIN, PAGE_SIZE[1] - 70), f"Page {number} of {len(pages)}", font=body, fill=0)
    return [
        _blur(finished, rng) if number == blurred else _scan(finished, rng)
        for number, finished in enumerate(pages, start=1)
    ]


def _scan(page: Image.Image, rng: random.Random) -> Image.Image:
    """Make a clean page look like a desk scan: skew, speckle, soft focus, grey paper."""
    skewed = page.rotate(rng.uniform(-0.6, 0.6), resample=Image.Resampling.BICUBIC, fillcolor=255)
    pixels = skewed.load()
    assert pixels is not None
    width, height = skewed.size
    for _ in range(2500):
        x, y = rng.randrange(width), rng.randrange(height)
        pixels[x, y] = rng.randint(90, 200)
    soft = skewed.filter(ImageFilter.GaussianBlur(0.6))
    return soft.point(lambda value: int(18 + value * 0.88))


def _blur(page: Image.Image, rng: random.Random) -> Image.Image:
    """A fax copy gone wrong: the text smeared and faded past reading."""
    skewed = page.rotate(rng.uniform(-1.5, 1.5), resample=Image.Resampling.BICUBIC, fillcolor=255)
    smeared = skewed.filter(ImageFilter.GaussianBlur(9))
    return smeared.point(lambda value: int(150 + value * 0.4))


def write_scan(path: Path, pages: list[Image.Image]) -> None:
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


# --- Pages with a text layer (a minimal PDF writer, stdlib only) -----------------------------

TEXT_WRAP = 92  # characters per line, Helvetica 10 pt on A4
LINES_PER_PAGE = 58


def _pdf_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def text_pages(paragraphs: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    """Lay the paragraphs out as (font, line) per page; ("", "") is a blank line."""
    pages: list[list[tuple[str, str]]] = []
    page: list[tuple[str, str]] = []
    for style, text in paragraphs:
        if style == "pagebreak":
            pages.append(page)
            page = []
            continue
        font = "F2" if style in ("title", "heading") else "F1"
        lines = textwrap.wrap(text, TEXT_WRAP, break_on_hyphens=False)
        if style == "signature":
            lines = ["______________________________", *lines]
        if len(page) + len(lines) + 1 > LINES_PER_PAGE:
            pages.append(page)
            page = []
        page.extend((font, line) for line in lines)
        page.append(("", ""))
    pages.append(page)
    return pages


def _content_stream(lines: list[tuple[str, str]], number: int, total: int) -> bytes:
    ops = ["BT", "13 TL", "64 780 Td"]
    for font, line in lines:
        if font:
            ops.append(f"/{font} 10 Tf ({_pdf_text(line)}) Tj")
        ops.append("T*")
    ops.append("ET")
    ops += ["BT", "/F1 8 Tf", f"64 40 Td ({_pdf_text(FOOTER)}) Tj", "ET"]
    ops += ["BT", "/F1 8 Tf", f"460 40 Td (Page {number} of {total}) Tj", "ET"]
    return "\n".join(ops).encode("latin-1")


def write_text_pdf(path: Path, paragraphs: list[tuple[str, str]]) -> None:
    """A PDF with a real text layer and no document info dictionary."""
    font = b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>"
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # the page tree, filled in below
        font % b"Helvetica",
        font % b"Helvetica-Bold",
    ]
    pages = text_pages(paragraphs)
    kids: list[str] = []
    for number, lines in enumerate(pages, start=1):
        stream = _content_stream(lines, number, len(pages))
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        content = len(objects)
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content} 0 R >>"
            ).encode()
        )
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode()
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1)
    out += b"startxref\n%d\n%%%%EOF\n" % xref
    path.write_bytes(bytes(out))


# --- The register -----------------------------------------------------------------------------


def schedules_title(numbers: tuple[int, ...]) -> str:
    return f"Schedules {numbers[0]} to {numbers[-1]}"


def write_register() -> None:
    with (DATA_DIR / "register_contracts.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["contract_id", "supplier", "services", "critical_or_important"])
        for a in AGREEMENTS:
            writer.writerow([a.contract_id, a.supplier, a.services, "yes" if a.critical else "no"])
    with (DATA_DIR / "register_documents.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["contract_id", "document", "kind", "file", "signed_on"])
        for a in AGREEMENTS:
            title = f"ICT Services Agreement with {schedules_title(a.schedules)}"
            writer.writerow([a.contract_id, title, "agreement", a.filename, a.signed_iso])
            if a.filename == "brindlecote-payments-gateway.pdf":
                writer.writerow(
                    [
                        a.contract_id,
                        "Amendment No. 1",
                        "amendment",
                        AMENDMENT_FILE,
                        AMENDMENT_SIGNED[1],
                    ]
                )


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    for agreement in AGREEMENTS:
        path = DATA_DIR / agreement.filename
        paragraphs = agreement_paragraphs(agreement)
        if agreement.scanned:
            pages = render_scan(paragraphs, agreement.seed, agreement.blurred_page)
            write_scan(path, pages)
            count = len(pages)
        else:
            write_text_pdf(path, paragraphs)
            count = len(text_pages(paragraphs))
        print(f"wrote data/{agreement.filename} ({count} pages)")
        if agreement.filename == "brindlecote-payments-gateway.pdf":
            # Signed on paper two and a half years later, and scanned.
            amendment = render_scan(amendment_paragraphs(agreement), agreement.seed + 1, None)
            write_scan(DATA_DIR / AMENDMENT_FILE, amendment)
            print(f"wrote data/{AMENDMENT_FILE} ({len(amendment)} pages)")
    write_register()
    print("wrote data/register_contracts.csv, data/register_documents.csv")


if __name__ == "__main__":
    main()
