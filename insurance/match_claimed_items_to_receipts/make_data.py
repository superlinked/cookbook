"""Write the synthetic contents claims in data/: the claims-system seed, receipt photos and
handwritten inventories.

Everything is invented: every policyholder, store, address, product and claim number. The
receipts are printed with Pillow's built-in font and "photographed": tilted onto a table,
unevenly lit, with sensor noise and JPEG compression. The handwritten inventories are written
in Caveat, a handwriting font under the SIL Open Font License 1.1, on lined paper in blue ink.
One claim's inventory came through the portal's form instead, so it is rows, not a photo.

    uv run --group data make_data.py

writes the claims system's seed (claims.csv: policy, policyholder, date of loss, contents
amount claimed and where the inventory came from; attachments.csv: which uploaded page is a
receipt and which the inventory; portal_inventory.csv: the portal's inventory rows), and the
photos in receipt-photos.pdf and inventories.pdf. The PDFs carry no document metadata (no
title, author or dates). The policyholder writes the total with sales tax: each item's price
plus the claim's local rate, the same amount the claims system holds. The first run downloads
the font from the google/fonts repository at a pinned commit and checks its SHA-256; it is
cached outside the repo. Rerunning rebuilds the files byte for byte.
"""

from __future__ import annotations

import csv
import hashlib
import random
import urllib.request
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"

DPI = 150
CENT = Decimal("0.01")

# Caveat (SIL OFL 1.1), https://github.com/googlefonts/caveat, from google/fonts at a pinned commit.
FONT_URL = (
    "https://raw.githubusercontent.com/google/fonts/5571d84c0d8c70ec1af4f64072d8c5cf1e4e9643/"
    "ofl/caveat/Caveat%5Bwght%5D.ttf"
)
FONT_SHA256 = "0bdb6b660482d31531b3945849fba5916b3ef8695da7024a9e6b9ee3c4157988"
FONT_CACHE = Path.home() / ".cache" / "sie-cloud-research" / "Caveat-wght.ttf"

INK = (28, 28, 32)
PEN = (24, 48, 140)


@dataclass(frozen=True)
class Line:
    text: str
    amount: str  # printed amount, "549.00" or "-30.00"


@dataclass(frozen=True)
class Receipt:
    merchant: str
    address: str
    when: str  # printed date and time, MM/DD/YYYY HH:MM
    items: tuple[Line, ...]
    discounts: tuple[Line, ...] = ()
    tax_rate: str = "0.08"
    total_offset: str = "0.00"  # print a total that differs from the lines
    angle: float = -1.0  # degrees the photo is tilted
    unreadable: bool = False  # out of focus and underexposed

    @property
    def subtotal(self) -> Decimal:
        return sum((Decimal(line.amount) for line in self.items + self.discounts), Decimal(0))

    @property
    def tax(self) -> Decimal:
        return (self.subtotal * Decimal(self.tax_rate)).quantize(CENT, ROUND_HALF_UP)

    @property
    def total(self) -> Decimal:
        return self.subtotal + self.tax + Decimal(self.total_offset)


@dataclass(frozen=True)
class Owned:
    """One inventory item as the policyholder lists it: `text` is written as is on a
    handwritten list ("Mixing bowls (2)"), `amount` is what they paid for all `quantity`."""

    text: str
    amount: str
    quantity: int = 1


@dataclass(frozen=True)
class Claim:
    claim: str
    policy: str
    policyholder: str
    date_of_loss: date
    cause: str
    receipts: tuple[Receipt, ...]
    inventory: tuple[Owned, ...]  # as the policyholder lists them
    source: str = "handwritten"  # or "portal": the inventory is form rows, not a photo
    tax_rate: str = "0.08"  # the local rate the policyholder adds to the total
    total_style: str = "$"  # "$" writes "$1,234.56"; "" writes "1234.56"

    @property
    def claimed(self) -> Decimal:
        """The inventory's total with sales tax: the amount in the claims system too."""
        items = sum((Decimal(item.amount) for item in self.inventory), Decimal(0))
        return (items * (1 + Decimal(self.tax_rate))).quantize(CENT, ROUND_HALF_UP)


CLAIMS = (
    Claim(
        "CLM-26-1001",
        "HO-4471902",
        "Marta Lindqvist",
        date(2026, 10, 14),
        "a pipe burst in the basement and flooded the family room",
        (
            Receipt(
                "LARKSPUR ELECTRONICS",
                "220 Mercer Ave, Port Ellery UT 40381",
                "08/22/2026 15:41",
                (
                    Line("NIMBUS 55IN 4K TV", "549.00"),
                    Line("TILT WALL MOUNT KIT", "39.99"),
                    Line("HDMI CABLE 2M", "12.99"),
                ),
            ),
            Receipt(
                "FERNHOLLOW HOME & GARDEN",
                "14 Orchard Row, Eastbrook UT 40219",
                "06/03/2026 11:07",
                (Line("AREA RUG 5X8 SLATE", "189.00"), Line("FLOOR LAMP BRASS", "74.50")),
                angle=1.5,
            ),
        ),
        (
            Owned("Nimbus 55in TV", "549.00"),
            Owned("TV wall mount", "39.99"),
            Owned("Area rug 5x8", "189.00"),
            Owned("Brass floor lamp", "74.50"),
        ),
    ),
    Claim(
        "CLM-26-1002",
        "HO-5208816",
        "Desmond Achterberg",
        date(2026, 10, 2),
        "a pan fire in the kitchen; smoke and water damage",
        (
            Receipt(
                "BRIGHTCANAL APPLIANCE",
                "9 Quayside Terrace, Port Ellery UT 40388",
                "09/30/2026 18:22",
                (
                    Line("STAND MIXER 5QT RED", "329.99"),
                    Line("MIXING BOWL SET  2 @ 14.99", "29.98"),
                    Line("CHEF KNIFE 8IN", "54.00"),
                ),
                discounts=(Line("INSTANT COUPON", "-30.00"),),
                tax_rate="0.0725",
                angle=-2.5,
            ),
        ),
        (
            Owned("Red stand mixer 5 qt", "329.99"),
            Owned("Mixing bowls (2)", "29.98", 2),
            Owned("8 inch chef knife", "54.00"),
        ),
        tax_rate="0.0725",
        total_style="",
    ),
    Claim(
        "CLM-26-1003",
        "HO-3390457",
        "Priya Ramaswamy",
        date(2026, 9, 28),
        "a break-in at the house; the garage was emptied",
        (
            Receipt(
                "TIDEWATER OUTFITTERS",
                "77 Harbour Road, Port Ellery UT 40381",
                "07/12/2026 10:15",
                (
                    Line("TRAIL BIKE 27.5 HARDTAIL", "389.00"),
                    Line("BIKE LOCK U-BAR", "23.97"),
                ),
                total_offset="30.00",
                angle=1.0,
            ),
        ),
        (Owned("Trail bike 27.5", "389.00"), Owned("U-bar bike lock", "23.97")),
    ),
    Claim(
        "CLM-26-1004",
        "HO-6612083",
        "Tomasz Wierzbicki",
        date(2026, 10, 9),
        "a storm blew in the living room window",
        (
            Receipt(
                "COPPERLEAF BOOKS & MUSIC",
                "3 Cinderford Close, Westmarch UT 40566",
                "05/18/2026 13:50",
                (Line("ACOUSTIC GUITAR DREADNOUGHT", "279.00"), Line("GUITAR STAND", "19.99")),
                angle=-2.0,
            ),
        ),
        (Owned("Acoustic guitar", "479.00"), Owned("Guitar stand", "19.99")),
    ),
    Claim(
        "CLM-26-1005",
        "HO-2045718",
        "Aiyana Whitford",
        date(2026, 10, 11),
        "a break-in through the back door while we were away",
        (
            Receipt(
                "LARKSPUR ELECTRONICS",
                "220 Mercer Ave, Port Ellery UT 40381",
                "09/05/2026 17:03",
                (Line("ORBIT 14IN LAPTOP", "899.00"), Line("LAPTOP SLEEVE 14IN", "24.99")),
                angle=1.5,
            ),
        ),
        (
            Owned("Orbit 14in laptop", "899.00"),
            Owned("Laptop sleeve", "24.99"),
            Owned("Aurora wireless headphones", "279.00"),
            Owned("Doormat", "24.00"),
        ),
    ),
    Claim(
        "CLM-26-1006",
        "HO-7731264",
        "Gideon Farrow",
        date(2026, 10, 18),
        "water from the upstairs flat came through the ceiling",
        (
            Receipt(
                "FERNHOLLOW HOME & GARDEN",
                "14 Orchard Row, Eastbrook UT 40219",
                "04/26/2026 09:12",
                (Line("BOOKCASE OAK 5-SHELF", "159.00"), Line("ARMCHAIR LINEN", "349.00")),
                angle=-1.0,
            ),
            Receipt(
                "BRIGHTCANAL APPLIANCE",
                "9 Quayside Terrace, Port Ellery UT 40388",
                "08/14/2026 16:30",
                (Line("DEHUMIDIFIER 35PT", "219.99"),),
                tax_rate="0.0725",
                angle=2.0,
                unreadable=True,
            ),
        ),
        (
            Owned("Oak bookcase", "159.00"),
            Owned("Linen armchair", "349.00"),
            Owned("Dehumidifier 35 pint", "219.99"),
        ),
    ),
    Claim(
        "CLM-26-1007",
        "HO-8156390",
        "Lena Okonkwo-Hart",
        date(2026, 10, 14),
        "my bag was stolen from the car outside my house",
        (
            Receipt(
                "TIDEWATER OUTFITTERS",
                "77 Harbour Road, Port Ellery UT 40381",
                "10/20/2026 12:44",
                (Line("HIKING BOOTS WOMENS 8", "164.00"), Line("RAIN SHELL JACKET", "129.00")),
                angle=-1.5,
            ),
        ),
        (Owned("Hiking boots", "164.00"), Owned("Rain jacket", "129.00")),
    ),
    Claim(
        "CLM-26-1008",
        "HO-9047731",
        "Corin Abernathy",
        date(2026, 10, 21),
        "a kitchen fire spread smoke through the ground floor",
        (
            Receipt(
                "FERNHOLLOW HOME & GARDEN",
                "14 Orchard Row, Eastbrook UT 40219",
                "07/30/2026 14:26",
                (
                    Line("BAR STOOL WALNUT  2 @ 64.00", "128.00"),
                    Line("TV WALL MOUNT", "39.99"),
                ),
                angle=1.0,
            ),
        ),
        (Owned("Walnut bar stool", "128.00", 2), Owned("TV stand", "39.99")),
        source="portal",
    ),
)


# --- Fonts ------------------------------------------------------------------------------------


def handwriting_font(size: int) -> ImageFont.FreeTypeFont:
    """Caveat at `size`, downloaded once at the pinned commit and checked against its SHA-256."""
    if not FONT_CACHE.exists():
        FONT_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(FONT_URL, timeout=60) as response:
            FONT_CACHE.write_bytes(response.read())
    digest = hashlib.sha256(FONT_CACHE.read_bytes()).hexdigest()
    if digest != FONT_SHA256:
        raise RuntimeError(f"{FONT_CACHE} has SHA-256 {digest}, expected {FONT_SHA256}")
    return ImageFont.truetype(str(FONT_CACHE), size)


def print_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)  # type: ignore[return-value]


def money(value: Decimal, sign: str = "$") -> str:
    return f"{sign}{value:,.2f}" if sign else f"{value:.2f}"


# --- Photos -----------------------------------------------------------------------------------


def _photograph(paper: Image.Image, angle: float, seed: int, *, unreadable: bool) -> Image.Image:
    """Lay the paper on a table, tilt it, light it unevenly and add sensor noise."""
    rng = np.random.default_rng(seed)
    width, height = paper.width + 260, paper.height + 260
    # A wood-ish table: a warm gradient with faint grain.
    y = np.linspace(0, 1, height)[:, None]
    x = np.linspace(0, 1, width)[None, :]
    grain = 6 * np.sin(x * 90 + 3 * np.sin(y * 7))
    base = np.stack(
        [120 + 30 * y + grain, 88 + 22 * y + grain, 60 + 14 * y + grain * 0.6], axis=-1
    ) * np.ones((height, width, 1))
    table = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")

    mask = Image.new("L", paper.size, 255)
    tilted = paper.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")
    tilted_mask = mask.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
    shadow = Image.new("RGB", tilted.size, (40, 30, 20))
    left = (width - tilted.width) // 2
    top = (height - tilted.height) // 2
    table.paste(shadow, (left + 10, top + 14), tilted_mask.filter(ImageFilter.GaussianBlur(12)))
    table.paste(tilted, (left, top), tilted_mask)

    pixels = np.asarray(table).astype(np.float64)
    light = 1.0 - 0.18 * x - 0.10 * y  # brighter top left, like a lamp to one side
    pixels *= light[..., None]
    if unreadable:
        pixels *= 0.45  # underexposed
    pixels += rng.normal(0, 4.0, pixels.shape)
    photo = Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8), "RGB")
    if unreadable:
        return photo.filter(ImageFilter.GaussianBlur(7))  # out of focus
    return photo.filter(ImageFilter.GaussianBlur(0.6))


def draw_receipt(receipt: Receipt, seed: int) -> Image.Image:
    width = 640
    lines: list[tuple[str, str, str]] = []  # (left text, right text, style)
    lines.append((receipt.merchant, "", "head"))
    lines.append((receipt.address, "", "small"))
    lines.append(("", "", "gap"))
    lines.append((f"Date: {receipt.when}", "", "body"))
    lines.append((f"Store #{seed:03d}  Reg 2  Trans {4100 + seed * 37}", "", "small"))
    lines.append(("-" * 44, "", "small"))
    for item in receipt.items:
        lines.append((item.text, item.amount, "body"))
    for discount in receipt.discounts:
        lines.append((discount.text, discount.amount, "body"))
    lines.append(("-" * 44, "", "small"))
    lines.append(("SUBTOTAL", f"{receipt.subtotal:.2f}", "body"))
    rate = (Decimal(receipt.tax_rate) * 100).normalize()
    lines.append((f"SALES TAX {rate}%", f"{receipt.tax:.2f}", "body"))
    lines.append(("TOTAL", f"{receipt.total:.2f}", "bold"))
    lines.append(("", "", "gap"))
    lines.append(("VISA ****4417", f"{receipt.total:.2f}", "small"))
    lines.append(("THANK YOU FOR SHOPPING WITH US", "", "small"))
    lines.append(("Returns within 30 days with receipt", "", "small"))

    fonts = {
        "head": print_font(30),
        "bold": print_font(30),
        "body": print_font(24),
        "small": print_font(19),
        "gap": print_font(19),
    }
    height = 60 + sum(int(fonts[style].size * 1.55) for _, _, style in lines) + 40
    paper = Image.new("RGB", (width, height), (247, 245, 238))
    draw = ImageDraw.Draw(paper)
    y = 50
    for left, right, style in lines:
        font = fonts[style]
        if style in ("head",) or (style == "small" and not right):
            text_width = draw.textlength(left, font=font)
            draw.text(((width - text_width) / 2, y), left, font=font, fill=INK)
        else:
            draw.text((36, y), left, font=font, fill=INK)
            if right:
                text_width = draw.textlength(right, font=font)
                draw.text((width - 36 - text_width, y), right, font=font, fill=INK)
        if style == "bold":
            draw.text((37, y), left, font=font, fill=INK)
            right_x = width - 35 - draw.textlength(right, font=font)
            draw.text((right_x, y), right, font=font, fill=INK)
        y += int(font.size * 1.55)
    return _photograph(paper, receipt.angle, seed, unreadable=receipt.unreadable)


def draw_inventory(claim: Claim, seed: int, height: int = 1450) -> Image.Image:
    rng = random.Random(seed)
    width = 1100
    paper = Image.new("RGB", (width, height), (252, 251, 246))
    draw = ImageDraw.Draw(paper)
    spacing = 62
    for row in range(3, height // spacing):
        draw.line([(0, row * spacing), (width, row * spacing)], fill=(170, 195, 225), width=2)
    draw.line([(110, 0), (110, height)], fill=(225, 150, 150), width=2)

    font = handwriting_font(46)
    sign = claim.total_style
    total = money(claim.claimed, sign)
    rows = [
        f"Claim {claim.claim}   Policy {claim.policy}",
        f"Contents inventory - {claim.policyholder}",
        f"On {claim.date_of_loss.strftime('%B')} {claim.date_of_loss.day}, {claim.cause}.",
        "Items lost or damaged:",
        *(f"- {item.text}   {money(Decimal(item.amount), sign)}" for item in claim.inventory),
        f"Total claimed (with sales tax): {total}",
        "Receipts attached. I confirm this is true.",
        f"Signed: {claim.policyholder}",
    ]
    y = 3 * spacing - 52
    for text in rows:
        # A cause sentence longer than the page wraps onto the next ruled line.
        words, current, wrapped = text.split(), "", []
        for word in words:
            candidate = f"{current} {word}".strip()
            if draw.textlength(candidate, font=font) > width - 180:
                wrapped.append(current)
                current = word
            else:
                current = candidate
        wrapped.append(current)
        for part in wrapped:
            x = 130 + rng.randint(-6, 10)
            for word in part.split(" "):
                dy = rng.randint(-3, 3)
                draw.text((x, y + dy), word, font=font, fill=PEN)
                x += int(draw.textlength(word + " ", font=font)) + rng.randint(-1, 3)
            y += spacing
    return _photograph(paper, rng.uniform(-2.0, 2.0), seed + 1000, unreadable=False)


def _save_pdf(path: Path, pages: list[Image.Image]) -> None:
    """An image-only PDF with no document info (no title, author or dates), so it rebuilds byte
    for byte."""
    pages[0].save(
        path,
        format="PDF",
        save_all=True,
        append_images=pages[1:],
        resolution=DPI,
        quality=85,
        title="",
        creationDate="",
        modDate="",
    )


def _write_csv(path: Path, header: list[str], rows: list[list[object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    receipts: list[Image.Image] = []
    inventories: list[Image.Image] = []
    claims: list[list[object]] = []
    attachments: list[list[object]] = []
    portal: list[list[object]] = []
    for number, claim in enumerate(CLAIMS, start=1):
        claims.append(
            [
                *(claim.claim, claim.policy, claim.policyholder),
                *(claim.date_of_loss.isoformat(), f"{claim.claimed:.2f}", claim.source),
            ]
        )
        for index, receipt in enumerate(claim.receipts, start=1):
            receipts.append(draw_receipt(receipt, seed=len(receipts) + 1))
            row = [claim.claim, f"receipt-{index}", "receipt", "receipt-photos.pdf"]
            attachments.append([*row, len(receipts)])
        if claim.source == "portal":
            for line, item in enumerate(claim.inventory, start=1):
                unit = (Decimal(item.amount) / item.quantity).quantize(CENT)
                portal.append([claim.claim, line, item.text, item.quantity, f"{unit:.2f}"])
        else:
            inventories.append(draw_inventory(claim, seed=number))
            row = [claim.claim, "inventory", "inventory", "inventories.pdf"]
            attachments.append([*row, len(inventories)])
    _save_pdf(DATA_DIR / "receipt-photos.pdf", receipts)
    _save_pdf(DATA_DIR / "inventories.pdf", inventories)
    _write_csv(
        DATA_DIR / "claims.csv",
        [
            *("claim_number", "policy_number", "policyholder", "date_of_loss"),
            *("contents_claimed", "inventory_source"),
        ],
        claims,
    )
    _write_csv(
        DATA_DIR / "attachments.csv",
        ["claim_number", "attachment", "kind", "file", "page"],
        attachments,
    )
    _write_csv(
        DATA_DIR / "portal_inventory.csv",
        ["claim_number", "line", "description", "quantity", "unit_price"],
        portal,
    )
    print(f"wrote {len(claims)} claims, {len(receipts)} receipt photos to {DATA_DIR}")


if __name__ == "__main__":
    main()
