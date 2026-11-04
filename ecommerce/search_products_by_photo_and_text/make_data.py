"""Build the PIM seed and the shopper searches in data/ from Amazon Berkeley Objects (ABO).

ABO (https://amazon-berkeley-objects.s3.amazonaws.com/index.html) is licensed CC BY 4.0,
credit Amazon.com. CATALOG holds 35 amazon.com ABO listings across 10 product types: the ABO
item ID, the ABO `product_type` (the PIM category, which the app offers as a department facet),
the listing's main photo and its English title (en dash written as a hyphen). Two columns are
not from ABO: `color`, the product's main visible color, picked by hand from ABO's color name
and the photo, and `available`, made up for this demo (ABO has no stock data).

QUERIES holds the shopper searches: words (written for this recipe), photos, and a photo with
words. The photos are other ABO photos: a worn shot and a room shot of catalog products, a color
variant the catalog does not carry, a product of a type the catalog does not sell, and a shot of
a rug whose catalog listing is marked unavailable. One photo is the room shot cropped to the
lamp, as a shopper would crop it; it is cut from ABO's full-size image so that the crop stays
sharp. A photo with words reuses another search's photo (`photo_of`).

All other photos come from ABO's "small" image set (at most 256 px). Each photo is fitted onto a
white 256x256 tile; the catalog photos go on one sprite sheet and the query photos on another,
and the CSV files hold each photo's tile box. The sprite sheets are written by Pillow from
decoded pixels, so they carry no EXIF or XMP metadata. Rerunning rebuilds every file byte for
byte (with the same Pillow major version).

    uv run make_data.py
"""

from __future__ import annotations

import csv
import io
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CATALOG_PATH = DATA_DIR / "catalog.csv"
CATALOG_PHOTOS_PATH = DATA_DIR / "catalog-photos.jpg"
QUERIES_PATH = DATA_DIR / "queries.csv"
QUERY_PHOTOS_PATH = DATA_DIR / "query-photos.jpg"
ABO_IMAGES = "https://amazon-berkeley-objects.s3.amazonaws.com/images/"
TILE = 256
COLUMNS = 7

# Two lines per product: "product_id|ABO item_id|ABO product_type|ABO image_id|small-image
# path|color|available", then the title, indented.
CATALOG = """
P01|B07B4L6C47|SOFA|91u1nJvgGhL|52/526b071c.jpg|beige|yes
    Amazon Brand - Stone & Beam Andover Modern Sofa Couch, 78"W, Linen
P02|B07HZ57QD3|SOFA|91S00XqPfqL|de/dea1781e.jpg|grey|yes
    Amazon Brand - Rivet Goodwin Modern Sofa, 88.6"W, Charcoal Grey
P03|B07BWMQLS9|SOFA|71AfsL90tjL|9d/9d973499.jpg|grey|yes
    Amazon Brand - Rivet Edgewest Modern Left-Facing U-Shape Sectional, Leather, 117"W, Charcoal
P04|B07X5XSRPT|SOFA|71zHcRnrAwL|09/09dd5663.jpg|brown|yes
    AmazonBasics Outdoor Patio Garden PE Rattan Textured Faux Wicker Modular Sofa - 4-Piece Set, Brown and Grey Rattan
P05|B07B4MMTXT|CHAIR|91YQ0SlflAL|a9/a9c5a07c.jpg|brown|yes
    Amazon Brand - Stone & Beam Jameson Farmhouse Leather Nailhead Recliner Chair, 36"W, Driftwood
P06|B075X2WN36|CHAIR|91Xl1truRSL|7c/7c6baa7b.jpg|green|yes
    Amazon Brand - Rivet Curved Tufted Velvet Accent Chair, Marina Mid-Century, 28.7"W, Hunter Green
P07|B07MBFF1M7|CHAIR|51TnKG4L00L|76/76d46992.jpg|grey|yes
    Amazon Brand - Ravenna Home Classic-Style Solid Pine Dining Chair, 40"H, Gray Finish, Set of 2
P08|B0853VN8BT|CHAIR|91jweMOux4L|e1/e1cbf822.jpg|navy|yes
    Amazon Brand - Stone & Beam Elisabet Upholstered Swivel Chair, 33.5"W, Midnight
P09|B0825DWB31|LAMP|51hgHleTRaL|f7/f7159926.jpg|white|yes
    Amazon Brand - Stone & Beam Contemporary Glass Table Lamp with Narrow-Necked Body, LED Bulb Included, 20.25"H, White
P10|B07DBCMTPC|LAMP|41QtgNNdyAL|35/354bee9d.jpg|brown|yes
    Ravenna Home Clear Ribbed Glass Cylinder Living Room Floor Lamp with LED Light Bulb - 64.25 Inches, Dark Bronze
P11|B07DBHB6B5|LAMP|41SHn0jzPOL|56/56162342.jpg|clear|yes
    Amazon Brand - Ravenna Home Modern Round Table Lamp With LED Light Bulb - 17.50 Inches, Chrome with Clear Glass
P12|B0825CP44X|LAMP|51rQA0rCqGL|09/096bad06.jpg|beige|yes
    Amazon Brand - Stone & Beam Contemporary 2-Tone Floral Textured Ceramic Table Lamp, LED Bulb Included, 17.75"H, Beige
P13|B07QD6YNB7|RUG|A1L0LwCPsLL|b3/b3cc41fa.jpg|cream|yes
    Amazon Brand - Rivet Handcrafted Wool-and-Cotton Rug with Undulating Stripes, 5' x 8', Cream and Taupe
P14|B075Z9SBPH|RUG|91mB4D4+AxL|97/97b7d355.jpg|blue|yes
    Amazon Brand - Stone & Beam Old World Area Rug, 1' 10" x 2' 10", Blue and Orange
P15|B07QHL4LQ2|RUG|A1-iOdeRXGL|16/1630845f.jpg|blue|no
    Ravenna Home Floral Medallion Tiled Rug, 4' x 6', Cerulean Blue with Ivory
P16|B07QC8BJFR|RUG|91mFII9hTXL|b9/b9be7993.jpg|grey|yes
    Amazon Brand - Ravenna Home Floral Medallion Tiled Rug, 2'3" x 7'3", Warm Grey with Ivory
P17|B07HMG2K51|HANDBAG|91-wTDkPv+L|02/02070037.jpg|grey|yes
    AmazonBasics Messenger Bag - Grey
P18|B06WD1T48N|HANDBAG|A1Uwe3x7EdL|98/9811d750.jpg|brown|yes
    Amazon Brand - The Fix Quinn Suede Fringe Wristlet Clutch, Havana Tan/Orange
P19|B06WRPKD88|HANDBAG|91zxIlJkmfL|f7/f7211441.jpg|red|yes
    Amazon Brand - The Fix Hampton Crossbody Leather Circle Bag, Bright Orange-Red
P20|B01J24H2K0|BACKPACK|81+ADtxHDkL|37/3794b141.jpg|black|yes
    AmazonBasics Carry-On Travel Backpack - Black
P21|B07QRZD489|BACKPACK|91p--tZQOsL|b6/b6d0aa6b.jpg|grey|yes
    AmazonBasics Urban Backpack for Laptops up to 15-Inches - Grey
P22|B07D7WCXBT|BACKPACK|91bCs6qWMuL|e4/e4e29084.jpg|grey|yes
    AmazonBasics Sport Laptop Backpack - Grey
P23|B07CWQMKCC|SUITCASE|91AXGmCOvmL|0b/0bd3e1ff.jpg|grey|yes
    AmazonBasics Premium Hardside Spinner Luggage with Built-In TSA Lock - 3-Piece Set (21", 26", 30"), Grey
P24|B0727Y5L53|SUITCASE|91Mb2i8VULL|79/795866a1.jpg|blue|yes
    AmazonBasics Hardside Spinner, Carry-On, Expandable Suitcase Luggage with Wheels, Blue - 3-Piece Set
P25|B07R7G6M7N|SUITCASE|91BNU4+ciCL|16/160fd8b4.jpg|red|yes
    AmazonBasics 28" ABS Hardside Spinner Luggage, Red
P26|B0896L76B3|DRINKING_CUP|61JIl2lXHBL|ae/aeedcece.jpg|clear|yes
    AmazonBasics Serene Coolers Glass Drinkware Set, 16-Ounce, Set of 6
P27|B08989KYPF|DRINKING_CUP|71ycJQJFldL|7b/7b3a0567.jpg|clear|yes
    AmazonBasics Tritan Stemless Wine Glasses - 14-Ounce, Set of 4
P28|B0896LKL83|DRINKING_CUP|61KxAqB14hL|8e/8ea9918c.jpg|clear|yes
    AmazonBasics Briercrest 16-Piece Old Fashioned and Coolers Glass Drinkware Set
P29|B07THKW5LF|BOOT|71RGYATqQyL|df/dfabc473.jpg|black|yes
    find. Arbor Chelsea Boots, Black), 10.5 UK
P30|B074JPC2PH|BOOT|81RgLQezkvL|b9/b9168d0e.jpg|black|yes
    Amazon Brand - The Fix Women's Hazel 4-Buckle Strap Ankle Boot, Black, 6.5 B US
P31|B06XH67XMQ|SHOES|81773IlJPuL|f4/f4f4ef81.jpg|grey|yes
    Amazon Brand - The Fix Women's Dolly Block Heel Buckle Gladiator Sandal
P32|B07YFHG6JT|SHOES|91V+yWmJbqL|bb/bb517433.jpg|navy|yes
    Concept 3 by Skechers Men's Traver Lace-up Mesh Sneaker, Navy Blue, 8 Medium US
P33|B01MSE5A2C|SHOES|61BMn9WOzqL|8c/8c2995f7.jpg|navy|yes
    Amazon Brand - 206 Collective Women's Madison Ballet Flat, Navy, 10.5 B US
P34|B07ML7J584|LAMP|41BOXA3Sb6L|2d/2d66313a.jpg|brown|yes
    Amazon Brand - Ravenna Home Faux Wood Table Lamp, Bulb Included, 18"H, Espresso Dark Brown
P35|B07MHMNJZT|LAMP|41QVbQ8Ol0L|1f/1f144afd.jpg|beige|yes
    Amazon Brand - Ravenna Home Faux Wood Table Lamp with LED Light Bulb, 18"H, Oak
"""  # noqa: E501


@dataclass(frozen=True)
class Row:
    product_id: str
    item_id: str
    category: str
    image_id: str
    path: str
    color: str
    available: str
    title: str


@dataclass(frozen=True)
class QuerySource:
    query_id: str
    text: str = ""  # the shopper's words; empty for a photo-only search
    department: str = ""  # the department facet the shopper picked; empty means none
    abo_item_id: str = ""  # photo searches: the ABO listing the photo comes from
    abo_image_id: str = ""
    path: str = ""  # ABO image path
    crop: tuple[float, float, float, float] | None = None  # shopper's crop, fractions of the photo
    photo_of: str = ""  # reuse this other search's photo
    note: str = ""

    @property
    def has_photo(self) -> bool:
        return bool(self.path or self.photo_of)

    @property
    def kind(self) -> str:
        if self.text and self.has_photo:
            return "photo_and_text"
        return "text" if self.text else "photo"


QUERIES = [
    QuerySource(
        "Q01",
        text="stemless wine glasses",
        department="DRINKING_CUP",
        note="three sets of clear glasses; only P27 is stemless",
    ),
    QuerySource(
        "Q02", text="grey laptop bag", note="no filter: a grey messenger bag and grey backpacks"
    ),
    QuerySource(
        "Q03",
        text="grey laptop bag",
        department="BACKPACK",
        note="the same words in the backpack department",
    ),
    QuerySource("Q04", text="cordless power drill", note="the catalog sells no tools"),
    QuerySource(
        "Q05",
        text="navy ballet flat",
        department="SANDAL",
        note="the app offers a department the catalog does not have",
    ),
    QuerySource(
        "Q06",
        abo_item_id="B07DBHB6B5",  # the room shot of the P11 lamp
        abo_image_id="91wtj8DCDDL",
        path="23/2372d9f5.jpg",
        note="room shot: a black armchair, with the P11 lamp small in the corner",
    ),
    QuerySource(
        "Q07",
        department="LAMP",
        abo_item_id="B07DBHB6B5",  # the room shot of the P11 lamp
        abo_image_id="91wtj8DCDDL",
        path="23/2372d9f5.jpg",
        crop=(0.547, 0.156, 0.918, 0.527),
        note="the same room shot, cropped by the shopper to the lamp",
    ),
    QuerySource(
        "Q08",
        department="HANDBAG",
        abo_item_id="B07HMG2K51",
        abo_image_id="91ghKd9nyML",
        path="a7/a7b83032.jpg",
        note="a man wearing the P17 messenger bag",
    ),
    QuerySource(
        "Q09",
        department="SHOES",
        abo_item_id="B01N9YD8AK",
        abo_image_id="61cV+UnTTvL",
        path="b0/b02e197d.jpg",
        note="the black Madison flat; the catalog has only the navy one (P33)",
    ),
    QuerySource(
        "Q10",
        abo_item_id="B07TK1DS8Z",
        abo_image_id="71hg33n5VaL",
        path="50/50e0267a.jpg",
        note="a claw hammer; the catalog sells no tools",
    ),
    QuerySource(
        "Q11",
        text="but the base should be dark wood",
        department="LAMP",
        photo_of="Q07",
        note="the cropped lamp from Q07, with words; P34 has a dark wood base, P35 a light one",
    ),
    QuerySource(
        "Q12",
        text="in brown leather",
        department="HANDBAG",
        photo_of="Q08",
        note="the worn messenger bag from Q08, with words; the catalog has only grey fabric",
    ),
    QuerySource(
        "Q13",
        abo_item_id="B07QHL4LQ2",  # another photo of the P15 rug, which the PIM marks unavailable
        abo_image_id="813JIvSr-JL",
        path="16/165f8844.jpg",
        note="the P15 rug, marked unavailable; P16 is the same pattern in grey",
    ),
]


def catalog() -> list[Row]:
    lines = [line.strip() for line in CATALOG.strip().splitlines()]
    rows = []
    for header, title in zip(lines[::2], lines[1::2], strict=True):
        product_id, item_id, category, image_id, path, color, available = header.split("|")
        rows.append(Row(product_id, item_id, category, image_id, path, color, available, title))
    return rows


def fetch(path: str, size: str = "small") -> Image.Image:
    with urllib.request.urlopen(f"{ABO_IMAGES}{size}/{path}", timeout=120) as response:
        return Image.open(io.BytesIO(response.read())).convert("RGB")


def query_photo(query: QuerySource) -> Image.Image:
    if query.crop is None:
        return fetch(query.path)
    image = fetch(query.path, size="original")
    left, top, right, bottom = query.crop
    width, height = image.size
    return image.crop(
        (round(left * width), round(top * height), round(right * width), round(bottom * height))
    )


def tile(image: Image.Image) -> Image.Image:
    """Fit the photo onto a white square tile without changing its aspect ratio."""
    image = image.copy()
    image.thumbnail((TILE, TILE), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (TILE, TILE), "white")
    canvas.paste(image, ((TILE - image.width) // 2, (TILE - image.height) // 2))
    return canvas


def sprite(images: list[Image.Image], target: Path) -> list[tuple[int, int]]:
    """Write the photos as one sprite sheet of tiles; return each photo's (x, y)."""
    rows = (len(images) + COLUMNS - 1) // COLUMNS
    sheet = Image.new("RGB", (COLUMNS * TILE, rows * TILE), "white")
    boxes = []
    for index, image in enumerate(images):
        x, y = (index % COLUMNS) * TILE, (index // COLUMNS) * TILE
        sheet.paste(tile(image), (x, y))
        boxes.append((x, y))
    sheet.save(target, format="JPEG", quality=90, optimize=True)
    return boxes


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    products = catalog()
    boxes = sprite([fetch(row.path) for row in products], CATALOG_PHOTOS_PATH)
    with CATALOG_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                *("product_id", "category", "title", "color", "available"),
                *("abo_item_id", "abo_image_id", "x", "y", "w", "h"),
            ]
        )
        for row, (x, y) in zip(products, boxes, strict=True):
            writer.writerow(
                [
                    *(row.product_id, row.category, row.title, row.color, row.available),
                    *(row.item_id, row.image_id, x, y, TILE, TILE),
                ]
            )

    own_photos = [query for query in QUERIES if query.path]
    photo_boxes = dict(
        zip(
            [query.query_id for query in own_photos],
            sprite([query_photo(query) for query in own_photos], QUERY_PHOTOS_PATH),
            strict=True,
        )
    )
    sources = {query.query_id: query for query in QUERIES}
    with QUERIES_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                *("query_id", "kind", "text", "department", "photo_of"),
                *("abo_item_id", "abo_image_id", "cropped", "x", "y", "w", "h", "note"),
            ]
        )
        for query in QUERIES:
            source = sources[query.photo_of] if query.photo_of else query
            box = photo_boxes.get(source.query_id)
            writer.writerow(
                [
                    *(query.query_id, query.kind, query.text, query.department, query.photo_of),
                    *(source.abo_item_id, source.abo_image_id, "yes" if source.crop else ""),
                    *(box if box else ("", "")),
                    *((TILE, TILE) if box else ("", "")),
                    query.note,
                ]
            )
    print(f"wrote {len(products)} products and {len(QUERIES)} searches to {DATA_DIR}")


if __name__ == "__main__":
    main()
