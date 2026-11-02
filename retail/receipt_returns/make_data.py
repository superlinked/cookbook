"""Write the synthetic receipt photos and service-desk requests in data/.

Everything is invented: the Brambleway Market chain, its stores, members, products, SKUs and
transaction numbers. The receipts are printed with Pillow's built-in font and "photographed":
tilted on a counter, unevenly lit, with sensor noise and JPEG compression.

    uv run --group data make_data.py

writes data/receipt-photos.pdf (one receipt photo per page), data/requests.jsonl (one return or
missing-points request per line, naming the receipt photo's page) and
data/credited-transactions.csv (transactions the loyalty system already credited). Rerunning
rebuilds the files byte for byte.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"

DPI = 150
FIXED_DATE = date(2026, 10, 27).timetuple()  # byte-identical PDFs across reruns
CENT = Decimal("0.01")
INK = (28, 28, 32)
CHAIN = "BRAMBLEWAY MARKET"
STORES = {
    "0412": "18 Linden Parade, Port Ellery UT 40381",
    "0418": "5 Saltmarsh Road, Eastbrook UT 40219",
}


@dataclass(frozen=True)
class Line:
    sku: str
    text: str
    amount: str  # line amount as printed, "34.99"
    taxable: bool = True  # gift cards are not taxed


@dataclass(frozen=True)
class Receipt:
    store: str
    when: str  # printed date and time, MM/DD/YYYY HH:MM
    register: int
    trans: str
    items: tuple[Line, ...]
    discounts: tuple[tuple[str, str], ...] = ()  # (label, "-10.00")
    tax_rate: str = "0.0725"
    print_rate: bool = True  # False prints "TAX" with the amount and no percentage
    total_offset: str = "0.00"  # plant a printed total that differs from the lines
    angle: float = -1.0  # degrees the photo is tilted
    unreadable: bool = False  # out of focus and underexposed

    @property
    def transaction(self) -> str:
        return f"{self.store}-{self.register}-{self.trans}"

    @property
    def subtotal(self) -> Decimal:
        items = sum((Decimal(line.amount) for line in self.items), Decimal(0))
        return items + sum((Decimal(amount) for _, amount in self.discounts), Decimal(0))

    @property
    def tax(self) -> Decimal:
        untaxed = sum((Decimal(line.amount) for line in self.items if not line.taxable), Decimal(0))
        return ((self.subtotal - untaxed) * Decimal(self.tax_rate)).quantize(CENT, ROUND_HALF_UP)

    @property
    def total(self) -> Decimal:
        return self.subtotal + self.tax + Decimal(self.total_offset)


@dataclass(frozen=True)
class Request:
    request: str
    kind: str  # "return" or "points_claim"
    member: str
    requested_on: date
    receipt: Receipt
    sku: str | None = None  # the item brought back, scanned from its tag
    description: str | None = None


REQUESTS = (
    Request(
        "RET-1001",
        "return",
        "BW-7712-0431",
        date(2026, 10, 20),
        Receipt(
            "0412",
            "10/08/2026 17:42",
            2,
            "08841",
            (
                Line("310442", "CAST IRON SKILLET 10IN", "34.99"),
                Line("205530", "GLASS TUMBLER 2 @ 6.49", "12.98"),
                Line("118207", "LINEN TEA TOWELS 3PK", "14.50"),
            ),
        ),
        sku="310442",
        description="cast iron skillet 10in",
    ),
    Request(
        "PTS-2001",
        "points_claim",
        "BW-7712-0588",
        date(2026, 10, 21),
        Receipt(
            "0418",
            "09/26/2026 11:15",
            1,
            "05102",
            (
                Line("402215", "BLUETOOTH SPEAKER MINI", "49.99"),
                Line("120334", "SCENTED CANDLE CEDAR 2 @ 12.00", "24.00"),
                Line("990001", "GIFT CARD", "50.00", taxable=False),
            ),
            discounts=(("MEMBER COUPON", "-10.00"),),
            angle=-2.5,
        ),
    ),
    Request(
        "RET-1002",
        "return",
        "BW-7712-0612",
        date(2026, 10, 21),
        Receipt(
            "0412",
            "09/21/2026 09:58",
            3,
            "07730",
            (
                Line("230118", "BAMBOO BATH MAT", "29.00"),
                Line("230905", "COTTON BATH TOWEL", "18.50"),
            ),
            angle=1.5,
        ),
        sku="230118",
        description="bamboo bath mat",
    ),
    Request(
        "RET-1003",
        "return",
        "BW-7712-0745",
        date(2026, 10, 20),
        Receipt(
            "0418",
            "09/16/2026 14:03",
            2,
            "04419",
            (
                Line("150772", "CERAMIC SERVING BOWL", "27.95"),
                Line("150790", "CERAMIC DINNER PLATE 4PK", "39.00"),
            ),
            angle=1.0,
        ),
        sku="150790",
        description="ceramic dinner plates, set of 4",
    ),
    Request(
        "RET-1004",
        "return",
        "BW-7712-0803",
        date(2026, 10, 20),
        Receipt(
            "0412",
            "09/30/2026 19:27",
            1,
            "08022",
            (
                Line("405118", "NOISE CANCEL HEADPHONES", "129.00"),
                Line("405932", "USB-C CHARGING CABLE", "9.99"),
            ),
            angle=-2.0,
        ),
        sku="405118",
        description="noise cancelling headphones",
    ),
    Request(
        "RET-1005",
        "return",
        "BW-7712-0917",
        date(2026, 10, 19),
        Receipt(
            "0418",
            "10/10/2026 12:36",
            2,
            "05377",
            (
                Line("310450", "CAST IRON SKILLET 12IN", "44.99"),
                Line("310611", "SILICONE SPATULA", "7.99"),
            ),
            angle=1.5,
        ),
        sku="310442",
        description="cast iron skillet 10in",
    ),
    Request(
        "PTS-2002",
        "points_claim",
        "BW-7712-1020",
        date(2026, 10, 18),
        Receipt(
            "0412",
            "10/05/2026 16:11",
            3,
            "07958",
            (
                Line("260114", "GARDEN HOSE 50FT", "32.99"),
                Line("260220", "HOSE NOZZLE 8-PATTERN", "11.49"),
            ),
            total_offset="20.00",
            angle=-1.5,
        ),
    ),
    Request(
        "RET-1006",
        "return",
        "BW-7712-1166",
        date(2026, 10, 22),
        Receipt(
            "0418",
            "10/14/2026 10:20",
            1,
            "05560",
            (Line("118220", "WOOL THROW BLANKET", "59.00"),),
            angle=2.0,
            unreadable=True,
        ),
        sku="118220",
        description="wool throw blanket",
    ),
    Request(
        "PTS-2003",
        "points_claim",
        "BW-7712-1204",
        date(2026, 10, 23),
        Receipt(
            "0412",
            "10/12/2026 08:47",
            2,
            "08517",
            (
                Line("140301", "COFFEE BEANS 1KG", "21.99"),
                Line("140355", "POUR-OVER FILTERS 100CT", "6.49"),
            ),
            angle=-1.0,
        ),
    ),
    Request(
        "RET-1007",
        "return",
        "BW-7712-1288",
        date(2026, 10, 22),
        Receipt(
            "0418",
            "10/15/2026 13:08",
            3,
            "05611",
            (
                Line("160214", "WALNUT CUTTING BOARD", "22.50"),
                Line("160530", "BEESWAX FOOD WRAP 3PK", "12.50"),
            ),
            print_rate=False,  # "TAX 2.54" with no rate: the refund can't be worked out
            angle=1.0,
        ),
        sku="160214",
        description="walnut cutting board",
    ),
)

# Transactions the loyalty system already credited; PTS-2003's receipt is one of them.
CREDITED = (
    ("0418-1-05044", "BW-7712-0301", "88", "2026-09-24"),
    ("0412-2-08517", "BW-7712-1204", "28", "2026-10-12"),
    ("0412-1-08310", "BW-7712-0431", "41", "2026-10-15"),
)


def print_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)  # type: ignore[return-value]


# --- Photos -----------------------------------------------------------------------------------


def _photograph(paper: Image.Image, angle: float, seed: int, *, unreadable: bool) -> Image.Image:
    """Lay the paper on a counter, tilt it, light it unevenly and add sensor noise."""
    rng = np.random.default_rng(seed)
    width, height = paper.width + 260, paper.height + 260
    y = np.linspace(0, 1, height)[:, None]
    x = np.linspace(0, 1, width)[None, :]
    grain = 5 * np.sin(x * 70 + 2 * np.sin(y * 9))
    base = np.stack(
        [96 + 26 * y + grain, 100 + 22 * y + grain, 108 + 18 * y + grain], axis=-1
    ) * np.ones((height, width, 1))
    counter = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")

    mask = Image.new("L", paper.size, 255)
    tilted = paper.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")
    tilted_mask = mask.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
    shadow = Image.new("RGB", tilted.size, (30, 30, 36))
    left = (width - tilted.width) // 2
    top = (height - tilted.height) // 2
    counter.paste(shadow, (left + 10, top + 14), tilted_mask.filter(ImageFilter.GaussianBlur(12)))
    counter.paste(tilted, (left, top), tilted_mask)

    pixels = np.asarray(counter).astype(np.float64)
    light = 1.0 - 0.16 * x - 0.12 * y  # brighter top left
    pixels *= light[..., None]
    if unreadable:
        pixels *= 0.45  # underexposed
    pixels += rng.normal(0, 4.0, pixels.shape)
    photo = Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8), "RGB")
    if unreadable:
        return photo.filter(ImageFilter.GaussianBlur(7))  # out of focus
    return photo.filter(ImageFilter.GaussianBlur(0.6))


def draw_receipt(request: Request, seed: int, address: str | None = None) -> Image.Image:
    """Print the receipt and photograph it. ``address`` prints a store that is not in STORES."""
    receipt = request.receipt
    width = 660
    rows: list[tuple[str, str, str]] = []  # (left text, right text, style)
    rows.append((CHAIN, "", "head"))
    rows.append((address or STORES[receipt.store], "", "small"))
    rows.append(("", "", "gap"))
    rows.append((f"Date: {receipt.when}", "", "body"))
    rows.append(
        (f"Store #{receipt.store}  Reg {receipt.register}  Trans {receipt.trans}", "", "body")
    )
    rows.append((f"Member {request.member[:3]}****{request.member[-4:]}", "", "small"))
    rows.append(("-" * 46, "", "small"))
    for item in receipt.items:
        rows.append((f"{item.sku} {item.text}", item.amount, "body"))
    for label, amount in receipt.discounts:
        rows.append((label, amount, "body"))
    rows.append(("-" * 46, "", "small"))
    rows.append(("SUBTOTAL", f"{receipt.subtotal:.2f}", "body"))
    rate = (Decimal(receipt.tax_rate) * 100).normalize()
    tax_label = f"SALES TAX {rate}%" if receipt.print_rate else "TAX"
    rows.append((tax_label, f"{receipt.tax:.2f}", "body"))
    rows.append(("TOTAL", f"{receipt.total:.2f}", "bold"))
    rows.append(("", "", "gap"))
    rows.append(("DEBIT ****2290", f"{receipt.total:.2f}", "small"))
    rows.append(("Returns within 30 days with receipt", "", "small"))
    rows.append(("Electronics returns within 15 days", "", "small"))

    fonts = {
        "head": print_font(32),
        "bold": print_font(30),
        "body": print_font(23),
        "small": print_font(19),
        "gap": print_font(19),
    }
    height = 60 + sum(int(fonts[style].size * 1.55) for _, _, style in rows) + 40
    paper = Image.new("RGB", (width, height), (247, 245, 238))
    draw = ImageDraw.Draw(paper)
    y = 50
    for left, right, style in rows:
        font = fonts[style]
        if style == "head" or (style == "small" and not right):
            text_width = draw.textlength(left, font=font)
            draw.text(((width - text_width) / 2, y), left, font=font, fill=INK)
        else:
            draw.text((32, y), left, font=font, fill=INK)
            if right:
                text_width = draw.textlength(right, font=font)
                draw.text((width - 32 - text_width, y), right, font=font, fill=INK)
        if style == "bold":
            draw.text((33, y), left, font=font, fill=INK)
            right_x = width - 31 - draw.textlength(right, font=font)
            draw.text((right_x, y), right, font=font, fill=INK)
        y += int(font.size * 1.55)
    return _photograph(paper, receipt.angle, seed, unreadable=receipt.unreadable)


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    photos: list[Image.Image] = []
    rows: list[dict[str, object]] = []
    for page, request in enumerate(REQUESTS, start=1):
        photos.append(draw_receipt(request, seed=page))
        row: dict[str, object] = {
            "request": request.request,
            "type": request.kind,
            "member": request.member,
            "requested_on": request.requested_on.isoformat(),
            "receipt": {"file": "receipt-photos.pdf", "page": page},
        }
        if request.sku is not None:
            row["item"] = {"sku": request.sku, "description": request.description}
        rows.append(row)
    photos[0].save(
        DATA_DIR / "receipt-photos.pdf",
        format="PDF",
        save_all=True,
        append_images=photos[1:],
        resolution=DPI,
        quality=85,
        creationDate=FIXED_DATE,
        modDate=FIXED_DATE,
    )
    lines = [json.dumps(row) for row in rows]
    (DATA_DIR / "requests.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with (DATA_DIR / "credited-transactions.csv").open("w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["transaction", "member", "points", "credited_on"])
        writer.writerows(CREDITED)
    print(f"wrote {len(rows)} requests and {len(photos)} receipt photos to {DATA_DIR}")


if __name__ == "__main__":
    main()
