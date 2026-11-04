"""Build the PIM seed, the supplier feed and the photo sheet from Amazon Berkeley Objects (ABO).

ABO (https://amazon-berkeley-objects.s3.amazonaws.com/index.html) is licensed CC BY 4.0,
credit Amazon.com. Each entry in SOURCE is one ABO product listing: its English title, ABO item
ID and one of that listing's catalog photos, and the side it plays here: E for a product already
in the PIM, I for a record in the incoming supplier feed. Titles are copied from ABO's listing
metadata; the sofa titles' en dash is written as a hyphen.

The PIM's material field is copied from ABO too (see ABO_FABRIC_TYPE below). Everything else is
synthetic, written for this recipe: the PIM's parent IDs, product lines, GTINs (from the GS1
restricted-circulation range starting with 20, so none of them is a real product's barcode),
model numbers, colors, sizes, pack counts and plug types, and the supplier's brand, color,
material and model number fields. Most supplier rows leave those fields blank,
the way a feed with no barcodes does, so the recipe reads color, size and pack count from the
title.

How the photos were picked: where two listings show the very same main photo of one product,
one of them uses another photo from its own ABO listing. L35 re-lists the L34 sofa with the
listing's second photo. L40 pairs the amazon.com/go tea title with a photo from the amazon.com
listing of the same item (the go listing's own photos show a different tea). L43 is left as ABO
has it: its title is a Lightning cable and its photo an HDMI cable. Two records are planted on
top of ABO: L44 copies L12's title and photo with another model number, and L45 is a white
6-foot cable with no pack count, shown with the L06 2-pack photo.

The photos come from ABO's "small" image set (at most 256 px). Each is fitted onto a white
256x256 tile and all tiles are written as ONE sprite sheet; each CSV row holds its tile box.
PIL writes the sheet without EXIF or XMP metadata.

    uv run make_data.py
"""

from __future__ import annotations

import csv
import io
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PIM_PATH = DATA_DIR / "pim_products.csv"
FEED_PATH = DATA_DIR / "supplier_feed.csv"
SPRITE_PATH = DATA_DIR / "photos.jpg"
ABO_SMALL_IMAGES = "https://amazon-berkeley-objects.s3.amazonaws.com/images/small/"
TILE = 256
COLUMNS = 7

# Two lines per record: "record_id|side|ABO item_id|ABO image_id|ABO small-image path", then the
# title, indented. Side E is a product already in the PIM, I a record in the supplier feed.
SOURCE = """
L01|I|B07L3K7BNW|81P9xoCI5bL|51/5111539d.jpg
    AmazonBasics Lightning to USB A Cable, 6-Foot
L02|E|B07DDPQ1HZ|71i-lXR63DL|7f/7f72cfb8.jpg
    AmazonBasics MFi-Certified Lightning to USB A Cable for Apple iPhone and iPad - 6 Feet (1.8 Meters) - Black
L03|I|B07DDK15FN|71qsSjYPE8L|ad/ade7bfaf.jpg
    AmazonBasics MFi-Certified Lightning to USB A Cable for Apple iPhone and iPad - 10 Feet (3 Meters) - Black
L04|E|B07DC4PZC4|613Wvm+VqKL|e5/e5798b38.jpg
    AmazonBasics MFi-Certified Lightning to USB A Cable for Apple iPhone and iPad - 6 Feet (1.8 Meters) - White
L05|I|B07DDN6RKK|71JrBN7b3KL|d5/d50c8170.jpg
    AmazonBasics Lightning to USB A Cable - MFi Certified iPhone Charger - Blue, 6-Foot
L06|E|B017YEATOG|613jL3B7XZL|aa/aae19e17.jpg
    AmazonBasics Lightning to USB A Cable - MFi Certified iPhone Charger - White, 6-Foot, 2-Pack
L07|E|B07TK1DS8Z|71hg33n5VaL|50/50e0267a.jpg
    AmazonBasics Hickory Handle Claw Hammer - Forged Steel Construction, 20 oz.
L08|I|B07TK1DS8Z|71T95M2x6VL|7e/7ef88b01.jpg
    AmazonBasics - Carpenter's Hammer with Hickory Handle, DS-HH20
L09|E|B07TGH9TFK|71vjuCg3SwL|b3/b3ff4ee2.jpg
    AmazonBasics Hickory Wood Handle Claw Hammer - 16 oz.
L10|I|B07TK1XYWL|71HiARFawRL|c2/c2f9cd7e.jpg
    AmazonBasics Fiberglass Handle Claw Hammer - 16 oz.
L11|E|B07PB375Q4|61kgt2hs4TL|da/da3c73c2.jpg
    AmazonBasics 65W One-Port USB-C 3.0 Wall Charger for Laptops, Tablets and Phones - White
L12|E|B07P6P2L22|71zDSr+hlkL|47/4736bed1.jpg
    AmazonBasics 65W One-Port USB-C 3.0 Wall Charger for Laptops, Tablets and Phones - Black
L13|I|B081VV4T3F|51iWTLrmOWL|c7/c7f08520.jpg
    AmazonBasics One-Port USB-C Wall Charger for Phones
L14|E|B081VV4T3F|51Tpd2MqZxL|9c/9cb06f22.jpg
    AmazonBasics One-Port USB-C Wall Charger for  Phones
L15|E|B01IJ5A0PC|61CgHKyRyqL|1b/1b52bfda.jpg
    AmazonBasics Multi-Angle Portable Stand for Tablets, E-readers and Phones - Silver
L16|I|B01IJ5A2UA|61wKeo-Ri0L|36/368b1b11.jpg
    AmazonBasics Multi-Angle Portable Stand for Tablets, E-readers and Phones - Black
L17|I|B07GL9G6WK|81gYXPnEFpL|4b/4b67a34a.jpg
    365 EVERYDAY VALUE Organic Roasted Unsalted Cashews, 10 OZ
L18|E|B074V3VNTN|61iEoKWReNL|67/6706467c.jpg
    365 Everyday Value, Organic Cashews - Roasted & Unsalted, 12 oz
L19|I|B074VBM1WN|61sAkscUxkL|df/dfec78f2.jpg
    365 Everyday Value, Organic Cashews - Roasted & Salted, 12 oz
L20|E|B075N4RWX3|81VXor4fWSL|9d/9da6ab53.jpg
    365 Everyday Value, White Chocolate Mini Baking Chips, 12 oz
L21|I|B074H737KJ|51mlPVleHKL|70/70e07f48.jpg
    365 Everyday Value, White Chocolate Mini Baking Chips, 12 oz
L22|E|B075MXHG98|81vdFuyBDkL|b7/b7d35bbd.jpg
    365 Everyday Value, Semi-Sweet Chocolate Mini Baking Chips, 12 oz
L23|E|B01MTEIBYL|81cKm0WAVPL|41/41b2f8d4.jpg
    The Fix Amazon Brand Women's Bond Studded Block Heel Dress Sandal,Matte Silver Mirror,10 B US
L24|I|B01MTEIBYL|81aC6WpW-NL|70/701f4c8f.jpg
    Amazon Brand - The Fix Women's Bond Studded Block Heel Dress Sandal,Matte Silver Mirror,10 B US
L25|E|B01MTEIBXL|81kWB9aqrgL|3c/3cd96149.jpg
    Amazon Brand - The Fix Women's Bond Studded Block Heel Dress Sandal,Black,8.5 B US
L26|I|B01N4TR5K7|816dwgwMIsL|75/75997d50.jpg
    The Fix Amazon Brand Women's Bond Studded Block Heel Dress Sandal,Mushroom Python Print,9.5 B US
L27|E|B01MTFTFME|71-tWgJAwQL|05/0506b6d1.jpg
    206 Collective Amazon Brand Women's Madison Ballet Flat, Gold Metallic, 8.5 B US
L28|I|B01MTFTFME|719vMScU2rL|5c/5c4e450b.jpg
    Amazon Brand - 206 Collective Women's Madison Ballet Flat, Gold Metallic, 8.5 B US
L29|E|B01N9YD8AK|61cV+UnTTvL|b0/b02e197d.jpg
    206 Collective Amazon Brand Women's Madison Ballet Flat, Black, 7 B US
L30|I|B01N4UXIK1|61DNNT-JxoL|1e/1eac352f.jpg
    Amazon Brand - 206 Collective Women's Madison Ballet Flat, Black, 7 B US
L31|I|B01N4UXIK1|71JmKAt7BpL|f9/f9ded333.jpg
    206 Collective Women's Madison Ballet Flat, Black Suede, 7 B US
L32|E|B01MSE5FSH|61BMn9WOzqL|8c/8c2995f7.jpg
    Amazon Brand - 206 Collective Women's Madison Ballet Flat, Navy, 9 B US
L33|I|B01MYEY4FM|61BMn9WOzqL|8c/8c2995f7.jpg
    Amazon Brand - 206 Collective Women's Madison Ballet Flat, Navy, 10 B US
L34|E|B072PZ4LVN|91Bet9BSaOL|4e/4ed5e182.jpg
    Amazon Brand - Rivet Revolve Modern Upholstered Sofa Couch, 80"W, Grey Weave
L35|I|B072PZ4LVN|A10rNmdZ1qL|42/42c81c25.jpg
    Amazon Brand - Rivet Revolve Modern Upholstered Sofa Couch, 80"W, Grey Weave
L36|E|B07CB54MLT|81-34e3KjWL|46/46f78b1a.jpg
    Amazon Brand - Rivet Revolve Modern Upholstered Sofa Couch, 80"W, Storm Grey
L37|I|B071ZLY1BR|91q4Mb3lGiL|11/1149da30.jpg
    Amazon Brand - Rivet Revolve Modern Upholstered Sofa Couch, 80"W, Denim Blue
L38|E|B071FMSY5S|91SGN5erCeL|6f/6fcbe9a8.jpg
    Amazon Brand - Rivet Revolve Modern Upholstered Loveseat Sofa, 56"W, Denim Blue
L39|E|B06WCZSGXS|81+MZBnsCOL|57/5714e347.jpg
    Wickedly Prime Organic Chamomile Tea, Full-Leaf Bags, 15 Count
L40|I|B06WCZSGXS|31h0tYoF54L|c7/c76a4dce.jpg
    Wickedly Prime Herbal Tea, Organic, Cool Calm & Chamomile, 15-ct
L41|E|B01N6XQOI8|61BMn9WOzqL|8c/8c2995f7.jpg
    206 Collective Women's Madison Ballet Flat, Navy, 12 B US
L42|I|B01N6XQOI8|61BMn9WOzqL|8c/8c2995f7.jpg
    206 Collective Amazon Brand Women's Madison Ballet Flat, Navy, 12 B US
L43|I|B07DDPQ1HZ|61pBvlYVPxL|f6/f69bbb35.jpg
    AmazonBasics Lightning to USB A Cable - MFi Certified iPhone Charger - Black, 6-Foot
L44|I|B07P6P2L22|71zDSr+hlkL|47/4736bed1.jpg
    AmazonBasics 65W One-Port USB-C 3.0 Wall Charger for Laptops, Tablets and Phones - Black
L45|I|B017YEATOG|613jL3B7XZL|aa/aae19e17.jpg
    AmazonBasics Lightning to USB A Cable - MFi Certified iPhone Charger - White, 6-Foot
"""  # noqa: E501

# The PIM's material per ABO item_id: the English value of the listing's `fabric_type` field,
# copied from ABO's listing metadata (listings/metadata/listings_*.json.gz) for every PIM shoe.
# ABO has no material for the cables, chargers, stand, hammers or food, and the sofas' `material`
# is a sentence about the whole sofa ("Polyester fabric, sturdy hardwood frame and solid beech
# wood legs"), so they are left blank. B01MTEIBYL's Chinese, Spanish and German entries say
# synthetic; its English entries say Leather, which is what the PIM gets.
ABO_FABRIC_TYPE = {
    "B01MTEIBYL": "Leather",  # L23, Bond sandal, Matte Silver Mirror
    "B01MTEIBXL": "Leather",  # L25, Bond sandal, Black
    "B01MTFTFME": "Leather",  # L27, Madison flat, Gold Metallic
    "B01N9YD8AK": "Leather",  # L29, Madison flat, Black
    "B01MSE5FSH": "Suede",  # L32, Madison flat, Navy
    "B01N6XQOI8": "Suede",  # L41, Madison flat, Navy
}

# Synthetic PIM fields, one product per line:
# record_id|parent_id|product_line|brand|color|size|pack_count|plug|model_number
# A blank field means the PIM doesn't state it; it never means "1" or "none".
PIM = """
L02|PL-LTG-CABLE|Lightning to USB A Cable|AmazonBasics|Black|6 ft|||
L04|PL-LTG-CABLE|Lightning to USB A Cable|AmazonBasics|White|6 ft|||
L06|PL-LTG-CABLE|Lightning to USB A Cable|AmazonBasics|White|6 ft|2||
L07|PL-HICKORY-HAMMER|Hickory Handle Claw Hammer|AmazonBasics||20 oz|||
L09|PL-HICKORY-HAMMER|Hickory Handle Claw Hammer|AmazonBasics||16 oz|||
L11|PL-65W-CHARGER|65W One-Port USB-C Wall Charger|AmazonBasics|White||||CHG65-W1
L12|PL-65W-CHARGER|65W One-Port USB-C Wall Charger|AmazonBasics|Black||||CHG65-B1
L14|PL-PHONE-CHARGER|One-Port USB-C Wall Charger for Phones|AmazonBasics|White|||UK|CHG1P-G
L15|PL-TABLET-STAND|Multi-Angle Portable Stand|AmazonBasics|Silver||||
L18|PL-CASHEWS-UNSALTED|Organic Roasted Unsalted Cashews|365 Everyday Value||12 oz|||
L20|PL-WHITE-CHIPS|White Chocolate Mini Baking Chips|365 Everyday Value||12 oz|||
L22|PL-SEMISWEET-CHIPS|Semi-Sweet Chocolate Mini Baking Chips|365 Everyday Value||12 oz|||
L23|PL-BOND-SANDAL|Bond Studded Block Heel Dress Sandal|The Fix|Matte Silver Mirror|10 B US|||
L25|PL-BOND-SANDAL|Bond Studded Block Heel Dress Sandal|The Fix|Black|8.5 B US|||
L27|PL-MADISON-FLAT|Madison Ballet Flat|206 Collective|Gold Metallic|8.5 B US|||
L29|PL-MADISON-FLAT|Madison Ballet Flat|206 Collective|Black|7 B US|||
L32|PL-MADISON-FLAT|Madison Ballet Flat|206 Collective|Navy|9 B US|||
L34|PL-REVOLVE-SOFA|Revolve Modern Upholstered|Rivet|Grey Weave|80"W|||
L36|PL-REVOLVE-SOFA|Revolve Modern Upholstered|Rivet|Storm Grey|80"W|||
L38|PL-REVOLVE-SOFA|Revolve Modern Upholstered|Rivet|Denim Blue|56"W|||
L39|PL-CHAMOMILE-TEA|Chamomile Tea|Wickedly Prime||15 count|||
L41|PL-MADISON-FLAT|Madison Ballet Flat|206 Collective|Navy|12 B US|||206MB-NV-12
"""
PIM_COLUMNS = (
    *("parent_id", "product_line", "brand", "color", "size", "pack_count", "plug"),
    "model_number",
)

# Synthetic supplier feed fields: brand for every row; color and model number only where the
# supplier filled them in. Everything else is blank, as in a feed with no barcodes.
FEED_BRANDS = {
    **dict.fromkeys(
        ("L01", "L03", "L05", "L08", "L10", "L13", "L16", "L43", "L44", "L45"), "AmazonBasics"
    ),
    **dict.fromkeys(("L17", "L19", "L21"), "365 Everyday Value"),
    **dict.fromkeys(("L24", "L26"), "The Fix"),
    **dict.fromkeys(("L28", "L30", "L31", "L33", "L42"), "206 Collective"),
    **dict.fromkeys(("L35", "L37"), "Rivet"),
    "L40": "Wickedly Prime",
}
FEED_COLORS = {"L31": "Black"}
FEED_MATERIALS = {"L31": "Suede"}
FEED_MODELS = {"L42": "206MB-NV-12", "L44": "CHG65-B2"}

PIM_FIELDS = (
    *("record_id", "parent_id", "product_line", "brand", "title", "gtin", "model_number"),
    *("color", "size", "pack_count", "plug", "material", "x", "y", "w", "h"),
)
FEED_FIELDS = (
    *("record_id", "brand", "title", "gtin", "model_number", "color", "size", "pack_count"),
    *("plug", "material", "x", "y", "w", "h"),
)


def records() -> list[tuple[str, str, str, str, str]]:
    """(record_id, side, ABO item_id, title, ABO small-image path) per record."""
    lines = [line.strip() for line in SOURCE.strip().splitlines()]
    rows = []
    for header, title in zip(lines[::2], lines[1::2], strict=True):
        record_id, side, item_id, _image_id, path = header.split("|")
        rows.append((record_id, side, item_id, title, path))
    return rows


def pim_fields() -> dict[str, dict[str, str]]:
    """The synthetic PIM fields per record_id, from the PIM table above."""
    found = {}
    for line in PIM.strip().splitlines():
        record_id, *values = line.split("|")
        found[record_id] = dict(zip(PIM_COLUMNS, values, strict=True))
    return found


def gtin(serial: int) -> str:
    """A GTIN-13 in the GS1 restricted-circulation range (prefix 20) with its check digit."""
    body = f"20{serial:010d}"
    total = sum(int(digit) * (3 if index % 2 else 1) for index, digit in enumerate(body))
    return body + str((10 - total % 10) % 10)


def fetch(path: str) -> Image.Image:
    with urllib.request.urlopen(ABO_SMALL_IMAGES + path, timeout=60) as response:
        return Image.open(io.BytesIO(response.read())).convert("RGB")


def tile(image: Image.Image) -> Image.Image:
    """Fit the photo onto a white square tile without changing its aspect ratio."""
    image = image.copy()
    image.thumbnail((TILE, TILE), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (TILE, TILE), "white")
    canvas.paste(image, ((TILE - image.width) // 2, (TILE - image.height) // 2))
    return canvas


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rows = records()
    sprite = Image.new("RGB", (COLUMNS * TILE, -(-len(rows) // COLUMNS) * TILE), "white")
    cache: dict[str, Image.Image] = {}
    pim_rows: list[dict[str, str | int]] = []
    feed_rows: list[dict[str, str | int]] = []
    for index, (record_id, side, item_id, title, path) in enumerate(rows):
        if path not in cache:
            cache[path] = tile(fetch(path))
        x, y = (index % COLUMNS) * TILE, (index // COLUMNS) * TILE
        sprite.paste(cache[path], (x, y))
        box = {"x": x, "y": y, "w": TILE, "h": TILE}
        if side == "E":
            fields = pim_fields()[record_id]
            material = ABO_FABRIC_TYPE.get(item_id, "")
            pim_rows.append(
                {
                    "record_id": record_id,
                    "title": title,
                    "gtin": gtin(index + 1),
                    **fields,
                    "material": material,
                    **box,
                }
            )
        else:
            feed_rows.append(
                {
                    "record_id": record_id,
                    "brand": FEED_BRANDS[record_id],
                    "title": title,
                    "gtin": "",
                    "model_number": FEED_MODELS.get(record_id, ""),
                    "color": FEED_COLORS.get(record_id, ""),
                    "size": "",
                    "pack_count": "",
                    "plug": "",
                    "material": FEED_MATERIALS.get(record_id, ""),
                    **box,
                }
            )
    for out_path, columns, table in (
        (PIM_PATH, PIM_FIELDS, pim_rows),
        (FEED_PATH, FEED_FIELDS, feed_rows),
    ):
        with out_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(table)
    sprite.save(SPRITE_PATH, format="JPEG", quality=90, optimize=True)
    print(f"wrote {PIM_PATH} ({len(pim_rows)} products), {FEED_PATH} ({len(feed_rows)} records)")
    print(f"wrote {SPRITE_PATH} ({len(rows)} photos)")


if __name__ == "__main__":
    main()
