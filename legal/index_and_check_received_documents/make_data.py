"""Write the synthetic discovery volume TFL-VOL001 and the matter system's seeds in data/.

Every company, person, address and email domain below is fictional (the domains end in
.example). The volume, in data/TFL-VOL001/, has the kinds of file a small producing party
sends when there is no usable load file:

    scans.pdf          4 scanned pages, image only (no text layer): a memo, a two-page letter
                       and a proof-of-delivery form
    emails.pdf         6 emails printed to PDF with a text layer, one per page
    letter.pdf         a two-page scanned letter with no page numbers
    TFL0000010.docx    one native Word file, named by its Bates number

The matter system's seeds stand in for what the receiving firm already has on file:

    matter_productions.csv   the production log: each volume as its cover letter states it
    matter_natives.csv       the custodian of each native file the cover letter lists
    matter_counsel.csv       the lawyers on the matter and the party each acts for

Every page carries a Bates stamp and a custodian legend in its footer. Two stamping mistakes
are planted in emails.pdf: TFL0000006 is used twice and TFL0000008 is skipped. The PDFs carry
no document metadata (no producer, title or dates).

    uv run make_data.py
"""

from __future__ import annotations

import csv
import random
import textwrap
import zipfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"

DPI = 150
PAGE_SIZE = (1275, 1650)  # US Letter at 150 DPI
MARGIN = 120
FONT_SIZE = 24
LINE_GAP = 11

VOLUME = "TFL-VOL001"
VOLUME_DIR = DATA_DIR / VOLUME
MATTER_ID = "KSC-2025-014"
MATTER = "Kestrel Supply Co. v. Tollerby Freight Ltd. (fictional)"
PRODUCING_PARTY = "Tollerby Freight Ltd."

# The production log: the volume as its cover letter states it.
PRODUCTIONS = (
    {
        "volume": VOLUME,
        "matter_id": MATTER_ID,
        "matter": MATTER,
        "producing_party": PRODUCING_PARTY,
        "received_on": "2025-06-02",
        "bates_prefix": "TFL",
        "bates_digits": "7",
        "bates_begin": "TFL0000001",
        "bates_end": "TFL0000013",
    },
)
NATIVES = (
    {
        "volume": VOLUME,
        "file": "TFL0000010.docx",
        "custodian": "Lena Fairbrook",
        "source": "Operations share drive",
    },
)
# Counsel of record. Only the producing party's lawyers count for the privilege check; our own
# lawyers' names in the other side's files are not the other side's privileged material.
COUNSEL = (
    (
        PRODUCING_PARTY,
        "Owen Achterberg",
        "General Counsel, Tollerby Freight Ltd.",
        "O. Achterberg;oachterberg@tollerby-freight.example",
    ),
    (
        PRODUCING_PARTY,
        "Priya Raman",
        "Partner, Wenlow & Croft LLP (outside counsel)",
        "P. Raman;praman@wenlowcroft.example",
    ),
    (
        PRODUCING_PARTY,
        "Imogen Sallis",
        "Associate, Wenlow & Croft LLP (outside counsel)",
        "I. Sallis;isallis@wenlowcroft.example",
    ),
    (
        "Kestrel Supply Co.",
        "Nadia Okafor",
        "Partner, Brightwell Lane LLP (our firm)",
        "N. Okafor;nokafor@brightwell-lane.example",
    ),
)


@dataclass(frozen=True)
class Page:
    bates: str
    custodian: str
    lines: tuple[str, ...]  # "" is a blank line; "# " starts a bold heading line


# --- Scanned pages ----------------------------------------------------------------------------

SCANS = (
    Page(
        "TFL0000001",
        "Lena Fairbrook",
        (
            "# MEMORANDUM",
            "",
            "To: Dispatch Team",
            "From: Lena Fairbrook, Operations Manager",
            "Date: March 4, 2025",
            "Re: Revised loading schedule for the Kestrel account",
            "",
            "From Monday 10 March, Kestrel Supply Co. loads move to the 06:00 slot at Bay 4. "
            "Drivers must confirm pallet counts with the yard office before leaving the depot, "
            "and any short load is reported on the shift sheet the same day.",
            "",
            "The Tuesday and Thursday runs to Brayford Park stay as they are. Please update the "
            "dispatch board and tell the night shift before Friday.",
            "",
            "Page 1 of 1",
        ),
    ),
    Page(
        "TFL0000002",
        "Marcus Bell",
        (
            "# TOLLERBY FREIGHT LTD. - LEGAL DEPARTMENT",
            "PRIVILEGED & CONFIDENTIAL - ATTORNEY-CLIENT COMMUNICATION",
            "",
            "March 18, 2025",
            "",
            "Marcus Bell",
            "Chief Executive Officer, Tollerby Freight Ltd.",
            "",
            "Re: Kestrel Supply Co. - notice of termination",
            "",
            "Dear Marcus,",
            "",
            "You asked whether we can serve notice of termination on Kestrel Supply Co. now, "
            "while their claim for the missed February deliveries is still open. My advice is "
            "that we should not serve notice yet. Clause 21 lets either party terminate on "
            "ninety days' notice, but serving it during the dispute would let Kestrel argue "
            "that we are trying to avoid the indemnity in clause 14.",
            "",
            "Page 1 of 2",
        ),
    ),
    Page(
        "TFL0000003",
        "Marcus Bell",
        (
            "In my view the indemnity cap in clause 14 is likely to hold for late deliveries, "
            "but the position on lost stock is weaker. I recommend that we answer the claim "
            "first and revisit termination once the claim is settled or the cap is tested.",
            "",
            "Please do not forward this letter outside the executive team.",
            "",
            "Sincerely,",
            "",
            "Owen Achterberg",
            "General Counsel, Tollerby Freight Ltd.",
            "",
            "Page 2 of 2",
        ),
    ),
    Page(
        "TFL0000004",
        "Tom Okoye",
        (
            "# PROOF OF DELIVERY",
            "",
            "Delivery note no.: DN-44817",
            "Date: February 27, 2025",
            "Carrier: Tollerby Freight Ltd.",
            "Consignee: Kestrel Supply Co., Unit 9 Brayford Park",
            "Driver: Tom Okoye",
            "Pallets delivered: 22",
            "Received in good condition: Yes",
            "",
            "Received by: J. Morrow, Goods-In Supervisor",
            "",
            "Page 1 of 1",
        ),
    ),
)

# --- Emails printed to PDF (text layer) ---------------------------------------------------------

EMAILS = (
    Page(
        "TFL0000005",
        "Marcus Bell",
        (
            "From: Marcus Bell <mbell@tollerby-freight.example>",
            "Sent: Wednesday, March 12, 2025 4:41 PM",
            "To: Priya Raman <praman@wenlowcroft.example>",
            "Subject: Kestrel claim - your view on the indemnity cap",
            "",
            "Priya,",
            "",
            "Kestrel's solicitors say the cap in clause 14 does not apply to late deliveries. "
            "Before we answer them, can you tell me whether you think the cap holds, and what "
            "our exposure is if it does not? I need your advice before Friday's board call.",
            "",
            "Marcus",
        ),
    ),
    Page(
        "TFL0000006",
        "Lena Fairbrook",
        (
            "From: Lena Fairbrook <lfairbrook@tollerby-freight.example>",
            "Sent: Monday, March 17, 2025 9:05 AM",
            "To: Tom Okoye <tokoye@tollerby-freight.example>",
            "Cc: Priya Raman <praman@wenlowcroft.example>",
            "Subject: Week 12 pallet counts",
            "",
            "Tom,",
            "",
            "Week 12 counts for Kestrel: Monday 22, Tuesday 18, Wednesday 25. Bay 4 is closed "
            "on Thursday for resurfacing, so load from Bay 2 that day.",
            "",
            "Lena",
        ),
    ),
    Page(
        "TFL0000006",  # planted: the same number stamped on a second page
        "Tom Okoye",
        (
            "From: Tom Okoye <tokoye@tollerby-freight.example>",
            "Sent: Tuesday, March 18, 2025 2:12 PM",
            "To: Lena Fairbrook <lfairbrook@tollerby-freight.example>",
            "Subject: RE: Kestrel invoice details",
            "",
            "Lena,",
            "",
            'Accounts asked again: the legal entity name on the invoice should read "Kestrel '
            'Supply Co.", not "Kestrel Supplies". Their invoice address has not changed.',
            "",
            "Tom",
        ),
    ),
    Page(
        "TFL0000007",
        "Dana Whitfield",
        (
            "From: Dana Whitfield <dwhitfield@tollerby-freight.example>",
            "Sent: Thursday, March 20, 2025 11:30 AM",
            "To: Marcus Bell <mbell@tollerby-freight.example>",
            "Subject: Chronology of Kestrel delivery failures - prepared at the request of counsel",
            "",
            "Marcus,",
            "",
            "As counsel asked, here is my draft chronology of the missed Kestrel deliveries for "
            "use in the dispute. Counsel needs it to assess our defence to the claim, so please "
            "keep it within the litigation team and do not circulate it.",
            "",
            "Dana",
        ),
    ),
    Page(
        "TFL0000009",  # planted: TFL0000008 is skipped
        "Dana Whitfield",
        (
            "From: Dana Whitfield <dwhitfield@tollerby-freight.example>",
            "Sent: Monday, March 24, 2025 8:15 AM",
            "To: All Depot Staff <depot-staff@tollerby-freight.example>",
            "Subject: Lunch for Friday's driver safety training",
            "",
            "Hi all,",
            "",
            "Lunch for Friday's driver safety training will be sandwiches from the canteen at "
            "12:30 in the training room. Let me know about dietary needs by Wednesday.",
            "",
            "Dana",
        ),
    ),
    Page(
        "TFL0000013",  # planted: the privileged paragraph only reaches the second window
        "Ravi Mendes",
        (
            "From: Marcus Bell <mbell@tollerby-freight.example>",
            "Sent: Friday, April 4, 2025 5:02 PM",
            "To: Depot Managers <depot-managers@tollerby-freight.example>",
            "Subject: Q2 depot plan",
            "",
            "All,",
            "",
            "Thanks for the Q1 numbers. Here is the plan for the second quarter, so you can set "
            "your rotas now.",
            "",
            "Brayford Park moves to two loading slots a day from April 14, at 06:00 and 13:00. "
            "Bay 4 reopens on April 7 after the resurfacing, and Bay 2 goes back to returns "
            "only. The new pallet scanners arrive in the week of April 21; each depot gets two, "
            "and the yard office will book the training. Overtime stays capped at six hours a "
            "week per driver until the new rota settles. Please send me your holiday plans for "
            "May and June by April 18, and flag any week where you would be short of drivers "
            "or forklift cover. The quarterly safety walk is on May 9 at every depot, starting "
            "at 10:00, and I would like each of you to lead the one on your own site.",
            "",
            "One more thing, on Kestrel. Our General Counsel has advised that the February "
            "delivery logs may be needed as evidence in their claim against us. Do not delete "
            "or edit them, and send any question about the claim to him before you reply to "
            "anyone at Kestrel. His advice on our position is for the management team only, so "
            "please do not forward this paragraph.",
            "",
            "Marcus",
        ),
    ),
)

# --- A letter scanned without page numbers --------------------------------------------------------

LETTER = (
    Page(
        "TFL0000011",  # planted: no page numbers, so nothing says page 2 continues page 1
        "Marcus Bell",
        (
            "April 2, 2025",
            "",
            "Marcus Bell",
            "Chief Executive Officer, Tollerby Freight Ltd.",
            "",
            "Re: Kestrel Supply Co. - February delivery logs",
            "",
            "Dear Marcus,",
            "",
            "Thank you for sending the February delivery logs and the depot shift sheets. We "
            "have gone through them against the Kestrel delivery schedule.",
            "",
            "The logs show 14 scheduled runs to Brayford Park in February. Eleven left the "
            "depot on time. Three left late on the days Bay 4 was closed, and two of those "
            "arrived after the agreed slot. The shift sheets record the same three late "
            'departures, with the reason written as "Bay 4 closed".',
        ),
    ),
    Page(
        "TFL0000012",
        "Marcus Bell",
        (
            "Kestrel's claim lists five missed deliveries, not three. Before we respond, please "
            "send us the yard office records for February 10 to 14, so we can see whether the "
            "other two runs left the depot at all. We will then advise you on how to answer "
            "the claim.",
            "",
            "Kind regards,",
            "",
            "Imogen Sallis",
            "Associate, Wenlow & Croft LLP",
        ),
    ),
)


# --- The native Word file -----------------------------------------------------------------------

DOCX_NAME = "TFL0000010.docx"
DOCX_PARAGRAPHS = (
    "RATE SCHEDULE AMENDMENT NO. 2",
    "Between Tollerby Freight Ltd. and Kestrel Supply Co.",
    "Effective date: April 1, 2025",
    "1. The pallet rate for the Brayford Park route rises from GBP 41.00 to GBP 43.50 per pallet.",
    "2. The fuel surcharge is reviewed every quarter against the published diesel index.",
    "3. All other terms of the Haulage Agreement dated June 2, 2023 stay in force.",
    "Signed for Tollerby Freight Ltd.: Lena Fairbrook, Operations Manager",
    "Signed for Kestrel Supply Co.: Ruth Delacourt, Procurement Director",
)


def footer(page: Page) -> str:
    """The production legend; a page produced without one (custodian "") has none."""
    return f"CONFIDENTIAL - Custodian: {page.custodian}" if page.custodian else ""


# --- Scans ------------------------------------------------------------------------------------


def _scan(page: Image.Image, rng: random.Random) -> Image.Image:
    """Make a clean page look like a desk scan: skew, speckle, soft focus, grey paper."""
    skewed = page.rotate(rng.uniform(-0.5, 0.5), resample=Image.Resampling.BICUBIC, fillcolor=255)
    pixels = skewed.load()
    assert pixels is not None
    width, height = skewed.size
    for _ in range(2000):
        x, y = rng.randrange(width), rng.randrange(height)
        pixels[x, y] = rng.randint(90, 200)
    soft = skewed.filter(ImageFilter.GaussianBlur(0.5))
    return soft.point(lambda value: int(18 + value * 0.88))


def render_scan(page: Page, rng: random.Random) -> Image.Image:
    body = ImageFont.load_default(size=FONT_SIZE)
    bold = ImageFont.load_default(size=FONT_SIZE + 6)
    image = Image.new("L", PAGE_SIZE, 255)
    draw = ImageDraw.Draw(image)
    y = MARGIN
    width_chars = 80
    for line in page.lines:
        if not line:
            y += FONT_SIZE // 2 + LINE_GAP
            continue
        font = bold if line.startswith("# ") else body
        for part in textwrap.wrap(line.removeprefix("# "), width_chars):
            draw.text((MARGIN, y), part, font=font, fill=0)
            y += FONT_SIZE + LINE_GAP
    bottom = PAGE_SIZE[1] - 90
    draw.text((MARGIN, bottom), footer(page), font=body, fill=0)
    draw.text((PAGE_SIZE[0] - MARGIN - 170, bottom), page.bates, font=bold, fill=0)
    return _scan(image, rng)


def write_scans(path: Path, pages: tuple[Page, ...], seed: int) -> None:
    """An image-only PDF with no document info (no title or dates), so it rebuilds byte for
    byte."""
    rng = random.Random(seed)
    images = [render_scan(page, rng) for page in pages]
    images[0].save(
        path,
        format="PDF",
        save_all=True,
        append_images=images[1:],
        resolution=DPI,
        title="",
        creationDate="",
        modDate="",
    )


# --- A minimal PDF with a text layer (stdlib only) ------------------------------------------------


def _pdf_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _content_stream(page: Page) -> bytes:
    """Helvetica 11 pt on US Letter, 72 pt margins; the footer carries the stamp."""
    ops = ["BT", "/F1 11 Tf", "14 TL", "72 720 Td"]
    for line in page.lines:
        if not line:
            ops.append("T*")
            continue
        for part in textwrap.wrap(line, 90):
            ops.append(f"({_pdf_text(part)}) Tj T*")
    ops += ["ET", "BT", "/F1 9 Tf", f"72 40 Td ({_pdf_text(footer(page))}) Tj", "ET"]
    ops += ["BT", "/F2 11 Tf", f"460 40 Td ({page.bates}) Tj", "ET"]
    return "\n".join(ops).encode("latin-1")


def write_text_pdf(path: Path, pages: tuple[Page, ...]) -> None:
    font = b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>"
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # the page tree, filled in below
        font % b"Helvetica",
        font % b"Helvetica-Bold",
    ]
    kids: list[str] = []
    for page in pages:
        stream = _content_stream(page)
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        content_ref = len(objects)
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_ref} 0 R >>"
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


# --- A minimal .docx (stdlib only) ----------------------------------------------------------------

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" \
ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" \
Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" \
Target="word/document.xml"/>
</Relationships>"""


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_docx(path: Path, texts: tuple[str, ...] = DOCX_PARAGRAPHS) -> None:
    paragraphs = "".join(
        f"<w:p><w:r>{'<w:rPr><w:b/></w:rPr>' if index == 0 else ''}"
        f'<w:t xml:space="preserve">{_xml_escape(text)}</w:t></w:r></w:p>'
        for index, text in enumerate(texts)
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{paragraphs}</w:body></w:document>"
    )
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": _RELS,
        "word/document.xml": document,
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, text in parts.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, text.encode("utf-8"))


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    VOLUME_DIR.mkdir(parents=True, exist_ok=True)
    write_scans(VOLUME_DIR / "scans.pdf", SCANS, 20250304)
    write_scans(VOLUME_DIR / "letter.pdf", LETTER, 20250402)
    write_text_pdf(VOLUME_DIR / "emails.pdf", EMAILS)
    write_docx(VOLUME_DIR / DOCX_NAME)
    write_csv(DATA_DIR / "matter_productions.csv", list(PRODUCTIONS))
    write_csv(DATA_DIR / "matter_natives.csv", list(NATIVES))
    counsel = [
        {"matter_id": MATTER_ID, "party": party, "name": name, "role": role, "aliases": aliases}
        for party, name, role, aliases in COUNSEL
    ]
    write_csv(DATA_DIR / "matter_counsel.csv", counsel)
    for path in sorted([*DATA_DIR.glob("*.csv"), *VOLUME_DIR.iterdir()]):
        print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
