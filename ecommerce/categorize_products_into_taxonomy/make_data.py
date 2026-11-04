"""Write the synthetic supplier sheets, the PIM seed and the trimmed Shopify taxonomy into data/.

The suppliers, brands, products and PIM records are fictional. The taxonomy is the public
Shopify Standard Product Taxonomy (MIT license), trimmed to the branches that cover these
products:

    uv run make_data.py
    uv run make_data.py --taxonomy path/to/categories.txt   # trim a local copy, no download

Deterministic: the same taxonomy release gives byte-identical files.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PRODUCTS_PATH = DATA_DIR / "supplier-products.csv"
DUPLICATE_SKU_PATH = DATA_DIR / "supplier-products-duplicate-sku.csv"
PIM_PATH = DATA_DIR / "pim-products.csv"
TAXONOMY_PATH = DATA_DIR / "shopify-categories-subset.txt"

TAXONOMY_RELEASE = "v2026-10"
TAXONOMY_URL = (
    "https://raw.githubusercontent.com/Shopify/product-taxonomy/"
    f"{TAXONOMY_RELEASE}/dist/en/categories.txt"
)
# Every category under these branches is kept, with its ancestors. Everything else is dropped,
# including Lawn & Garden, so the garden-hose row has no category to land in.
KEEP_BRANCHES = (
    "Apparel & Accessories > Clothing",
    "Apparel & Accessories > Clothing Accessories",
    "Apparel & Accessories > Shoes",
    "Electronics > Audio",
    "Electronics > Computers",
    "Home & Garden > Kitchen & Dining",
    "Food, Beverages & Tobacco > Beverages",
    "Food, Beverages & Tobacco > Food Items",
)

# (sku, supplier title, supplier description). Planted rows are marked.
PRODUCTS: list[tuple[str, str, str]] = [
    # Apparel: color, size and material are required.
    (
        "TT-A01",
        "Mens Crew Neck Tee - Heather Navy - M",
        "Short sleeve crew neck t-shirt, relaxed fit. Material: 100% organic cotton jersey. "
        "Color: heather navy. Size: M.",
    ),
    # Clear row: one obvious category, every required attribute in the text.
    (
        "TT-A02",
        "Womens High Rise Skinny Jean INDIGO 28",
        "High-rise skinny jeans with five pockets. Material: stretch denim (98% cotton, 2% "
        "elastane). Color: indigo. Size: 28.",
    ),
    (
        "TT-A03",
        "Unisex Pullover Hoodie Forest Green L",
        "Pullover hooded sweatshirt in forest green with a kangaroo pocket, cotton/polyester "
        "fleece, size L.",
    ),
    # Planted: no size anywhere in the row, so the required attribute is missing.
    (
        "TT-A04",
        "Merino Crew Sweater - Charcoal",
        "Fine-knit crew neck sweater with ribbed cuffs and hem. Material: 100% merino wool. "
        "Color: charcoal grey.",
    ),
    (
        "TT-A05",
        "Womens Waterproof Rain Jacket Yellow S",
        "Hooded waterproof rain jacket with taped seams, yellow nylon shell. Women's size S.",
    ),
    (
        "TT-A06",
        "High Waist Running Leggings BLK XS",
        "Full-length running leggings with side pocket. Material: polyester/spandex. Color: "
        "black. Size: XS.",
    ),
    (
        "TT-A07",
        "Linen Midi Dress Sage 10",
        "Sleeveless sage midi dress with a tie waist, cut from 100% linen. UK size 10.",
    ),
    # Already in the PIM (see PIM_PRODUCTS): it keeps its category and never reaches a model.
    (
        "TT-A08",
        "Merino Hiking Socks Grey M (2 pk)",
        "Cushioned crew hiking socks, two pairs. Material: merino wool blend. Color: grey. Size: "
        "M.",
    ),
    # Clear row.
    (
        "TT-A09",
        "Canvas Low Top Sneaker White US 9",
        "Lace-up low-top sneakers with rubber sole. Material: cotton canvas upper. Color: white. "
        "Size: US 9.",
    ),
    (
        "TT-A10",
        "Washed Cotton Baseball Cap Khaki",
        "Six-panel baseball cap in khaki washed cotton twill with an adjustable strap. One size "
        "fits most.",
    ),
    (
        "TT-A11",
        "Mens Oxford Button Down Shirt Lt Blue L",
        "Long sleeve oxford shirt with button-down collar. Material: 100% cotton oxford. Color: "
        "light blue. Size: L.",
    ),
    (
        "TT-A12",
        "Full Grain Leather Belt Brown 34",
        "Brown full-grain leather dress belt with a brushed nickel buckle, waist size 34.",
    ),
    # Electronics: brand and model number are required.
    # Clear row.
    (
        "TT-E01",
        "Aurivox AV-H700 ANC Over Ear Headphones Black",
        "Brand: Aurivox. Model: AV-H700. Wireless over-ear headphones with active noise "
        "cancelling, 30 hour battery, Bluetooth 5.3. Color: black.",
    ),
    (
        "TT-E02",
        "Aurivox True Wireless Earbuds AV-E210",
        "Brand: Aurivox. Model: AV-E210. In-ear true wireless earbuds with charging case. Color: "
        "white.",
    ),
    (
        "TT-E03",
        "Brenmoor BM-S5 Portable Bluetooth Speaker IPX7",
        "The Brenmoor BM-S5 is a waterproof portable Bluetooth speaker in slate blue with a 12 "
        "hour battery.",
    ),
    (
        "TT-E04",
        "Brenmoor USB Condenser Microphone BM-MIC2",
        "Brand: Brenmoor. Model: BM-MIC2. Cardioid USB condenser microphone for podcasting and "
        "streaming, with desk stand.",
    ),
    (
        "TT-E05",
        "Tavrin KX14 14in Laptop 16GB 512GB",
        "Tavrin Computing's KX14 is a 14-inch laptop with 16 GB RAM, a 512 GB SSD and a backlit "
        "keyboard, in silver.",
    ),
    (
        "TT-E06",
        "Tavrin KT10 10.1in Android Tablet",
        "Brand: Tavrin Computing. Model: KT10. 10.1-inch Android tablet computer, 64 GB, Wi-Fi.",
    ),
    (
        "TT-E07",
        "Aurivox AV-SB400 2.1 Soundbar w/ Subwoofer",
        "Brand: Aurivox. Model: AV-SB400. TV soundbar with wireless subwoofer, HDMI ARC.",
    ),
    (
        "TT-E08",
        "Brenmoor BM-TT1 Belt Drive Turntable",
        "Brand: Brenmoor. Model: BM-TT1. Belt-drive turntable for vinyl records, 33/45 rpm, "
        "built-in phono preamp.",
    ),
    (
        "TT-E09",
        "Aurivox AV-G50 Gaming Headset w/ Mic",
        "Brand: Aurivox. Model: AV-G50. Wired gaming headset with boom microphone, 3.5 mm jack.",
    ),
    (
        "TT-E10",
        "Tavrin KM5 Mini Desktop PC",
        "Brand: Tavrin Computing. Model: KM5. Mini desktop computer, 8 GB RAM, 256 GB SSD.",
    ),
    (
        "TT-E11",
        "Brenmoor BM-R3 DAB+ Portable Radio",
        "Brand: Brenmoor. Model: BM-R3. Portable DAB+ and FM radio with alarm clock.",
    ),
    (
        "TT-E12",
        "Brenmoor BM-CD9 Portable CD Player",
        "Brand: Brenmoor. Model: BM-CD9. Portable CD player with anti-skip and headphone jack.",
    ),
    # Planted: an accessory that names its host product. AV-H700 is the headphones' model
    # number (TT-E01), not the cushions'.
    (
        "TT-E13",
        "Replacement Ear Cushions for Aurivox AV-H700 Headphones",
        "Brand: Aurivox. Genuine replacement ear cushions for the Aurivox AV-H700 over-ear "
        "headphones, one pair. Memory foam with a protein leather cover. Color: black.",
    ),
    # Kitchen & Dining: material is required.
    (
        "TT-H01",
        "Velmora 8in Chef Knife Walnut Handle",
        "Velmora 8-inch chef's knife. Material: high-carbon stainless steel blade, walnut handle.",
    ),
    (
        "TT-H02",
        "Velmora 10in Pre-Seasoned Skillet",
        "10-inch pre-seasoned cast iron frying pan, oven safe.",
    ),
    (
        "TT-H03",
        "Acacia End Grain Cutting Board 16x12",
        "Reversible acacia wood cutting board with a juice groove.",
    ),
    (
        "TT-H04",
        "Nesting Mixing Bowl Set 3pc",
        "Set of three nesting mixing bowls, 1.5, 3 and 5 qt. Material: stainless steel.",
    ),
    # Clear row.
    (
        "TT-H05",
        "Velmora 1L French Press Coffee Maker",
        "8-cup French press coffee maker with stainless plunger. Material: borosilicate glass.",
    ),
    (
        "TT-H06",
        "1.7L Electric Kettle Brushed Steel",
        "Cordless 1.7 L electric kettle in brushed stainless steel with auto shut-off.",
    ),
    (
        "TT-H07",
        "Silicone Spatula Set 3pc Red",
        "Heat-resistant spatula set for baking and cooking. Material: food-grade silicone. Color:"
        " red.",
    ),
    (
        "TT-H08",
        "Stoneware Dinner Plate Set of 4 Oat",
        "Set of four 10.5-inch dinner plates in oat glazed stoneware.",
    ),
    (
        "TT-H09",
        "Lead Free Crystal Wine Glasses Set of 2",
        "Pair of red wine glasses, 20 oz. Material: lead-free crystal glass.",
    ),
    (
        "TT-H10",
        "Quilted Oven Mitts Pair Navy",
        "Pair of heat-resistant oven mitts in navy quilted cotton.",
    ),
    (
        "TT-H11",
        "Glass Meal Prep Containers w/ Lids 5pk",
        "Five food storage containers with snap-lock lids. Material: borosilicate glass.",
    ),
    # Planted: a vacuum bottle sits between Water Bottles and Thermoses.
    (
        "TT-H12",
        "750ml Vacuum Insulated Bottle Matte Black",
        "Double-wall vacuum insulated bottle, keeps drinks hot 12 hours or cold 24 hours, "
        "leak-proof screw cap. Material: stainless steel. Color: matte black.",
    ),
    (
        "TT-H13",
        "Ceramic Teapot with Infuser 900ml",
        "Teapot with removable stainless infuser. Material: ceramic. Color: white.",
    ),
    # Food & beverages: net quantity is required.
    # Clear row.
    (
        "TT-F01",
        "Brightmere Colombia Whole Bean Coffee 340g",
        "Single-origin medium roast whole bean coffee from Huila, Colombia. Net weight: 340 g.",
    ),
    (
        "TT-F02",
        "Brightmere Organic Sencha Green Tea 20ct",
        "Organic Japanese sencha green tea, 20 tea bags, 40 g in total.",
    ),
    (
        "TT-F03",
        "Extra Virgin Olive Oil Cold Pressed 500ml",
        "Cold-pressed extra virgin olive oil from Andalusia. Net volume: 500 ml.",
    ),
    (
        "TT-F04",
        "Bronze Cut Spaghetti Durum Wheat 500g",
        "Bronze-cut dried spaghetti made from durum wheat semolina, 500 g bag.",
    ),
    (
        "TT-F05",
        "Maple Pecan Granola 400g",
        "Oven-baked granola cereal with maple syrup and pecans. Net weight: 400 g. Flavor: maple "
        "pecan.",
    ),
    (
        "TT-F06",
        "70% Dark Chocolate Bar 100g",
        "Single-origin 70% cacao dark chocolate, one 100 g bar.",
    ),
    # Planted: a pack-times-size net volume, the way food suppliers write it.
    (
        "TT-F07",
        "Sparkling Mineral Water Lemon 12x330ml",
        "Lightly sparkling natural mineral water with lemon. Pack of 12 cans. Net volume: 12 x "
        "330 ml. Flavor: lemon.",
    ),
    (
        "TT-F08",
        "Habanero Hot Sauce 150ml",
        "Fermented habanero chili hot sauce. Net volume: 150 ml. Flavor: habanero mango.",
    ),
    (
        "TT-F09",
        "Raw Wildflower Honey 350g Jar",
        "Raw unfiltered wildflower honey in a glass jar. Net weight: 350 g.",
    ),
    (
        "TT-F10",
        "Thai Jasmine Rice 2kg",
        "Long grain Thai jasmine rice in a 2 kg bag.",
    ),
    (
        "TT-F11",
        "Barista Oat Milk 1L",
        "Oat milk drink for coffee, foams well. Net volume: 1 L.",
    ),
    (
        "TT-F12",
        "Crunchy Peanut Butter No Added Sugar 340g",
        "Crunchy peanut butter made from roasted peanuts and salt, 340 g jar.",
    ),
    # Planted: garden hose nozzle. Lawn & Garden is not in the trimmed taxonomy.
    (
        "TT-X01",
        "Brass Garden Hose Nozzle 7 Pattern",
        "Heavy-duty garden hose spray nozzle with seven spray patterns. Material: brass.",
    ),
]


# Planted: a second sheet where one SKU covers two variants (black and white). The run stops on
# it before any model call and names both rows.
DUPLICATE_SKU_PRODUCTS: list[tuple[str, str, str]] = [
    (
        "TT-A13",
        "Classic Crew Tee Black M",
        "Short sleeve crew neck t-shirt in black, 100% cotton jersey, size M.",
    ),
    (
        "TT-A13",
        "Classic Crew Tee White M",
        "Short sleeve crew neck t-shirt in white, 100% cotton jersey, size M.",
    ),
    (
        "TT-H14",
        "Enamel Pie Dish 24cm",
        "Round 24 cm pie dish, oven safe. Material: enamelled steel.",
    ),
]

# The store's PIM records the recipe looks supplier SKUs up in. TT-A08 is a carry-over product
# from this supplier. Another supplier also uses the code TT-A01, for a different product, so
# the lookup is by supplier and SKU together.
PIM_COLUMNS = (
    "product_id",
    "supplier",
    "supplier_sku",
    "title",
    "category_id",
    "category_path",
    "attributes",
)
PIM_PRODUCTS: list[tuple[str, ...]] = [
    (
        "P-10198",
        "Tollgate Trading",
        "TT-S02",
        "Cotton Crew Socks White L (3 pk)",
        "gid://shopify/TaxonomyCategory/aa-1-18-3",
        "Apparel & Accessories > Clothing > Socks > Crew Socks",
        "color=white; size=L; material=cotton blend",
    ),
    (
        "P-10231",
        "Tollgate Trading",
        "TT-A08",
        "Merino Hiking Socks Grey M (2 pk)",
        "gid://shopify/TaxonomyCategory/aa-1-18-3",
        "Apparel & Accessories > Clothing > Socks > Crew Socks",
        "color=grey; size=M; material=merino wool blend",
    ),
    (
        "P-31007",
        "Marrowby Goods",
        "TT-A01",
        "Enamel Camping Mug Blue 350ml",
        "gid://shopify/TaxonomyCategory/hg-11-10-5-5",
        "Home & Garden > Kitchen & Dining > Tableware > Drinkware > Mugs",
        "material=enamelled steel",
    ),
]


def write_csv(path: Path, header: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def write_products() -> None:
    header = ("sku", "supplier_title", "supplier_description")
    write_csv(PRODUCTS_PATH, header, list(PRODUCTS))
    write_csv(DUPLICATE_SKU_PATH, header, list(DUPLICATE_SKU_PRODUCTS))
    write_csv(PIM_PATH, PIM_COLUMNS, PIM_PRODUCTS)


def trim_taxonomy(full_text: str, branches: tuple[str, ...] = KEEP_BRANCHES) -> str:
    """Keep each category inside a branch, plus every ancestor of a kept category."""
    header: list[str] = []
    rows: list[tuple[str, str]] = []
    for line in full_text.splitlines():
        if line.startswith("#"):
            header.append(line)
        elif " : " in line:
            gid, path = (part.strip() for part in line.split(" : ", 1))
            rows.append((gid, path))
    kept_paths: set[str] = set()
    for _, path in rows:
        if any(path == branch or path.startswith(branch + " > ") for branch in branches):
            parts = path.split(" > ")
            kept_paths.update(" > ".join(parts[:depth]) for depth in range(1, len(parts) + 1))
    lines = [
        *header,
        f"# Trimmed for this recipe by make_data.py from Shopify/product-taxonomy "
        f"{TAXONOMY_RELEASE} (MIT license). Kept branches: {'; '.join(branches)}",
        "",
    ]
    lines += [f"{gid:<48} : {path}" for gid, path in rows if path in kept_paths]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--taxonomy", type=Path, help="local Shopify categories.txt to trim")
    args = parser.parse_args()
    if args.taxonomy:
        full = args.taxonomy.read_text(encoding="utf-8")
    else:
        with urlopen(TAXONOMY_URL, timeout=60) as response:
            full = response.read().decode("utf-8")
    write_products()
    TAXONOMY_PATH.write_text(trim_taxonomy(full), encoding="utf-8")
    written = (PRODUCTS_PATH, DUPLICATE_SKU_PATH, PIM_PATH, TAXONOMY_PATH)
    print(f"wrote {', '.join(path.name for path in written)} in {DATA_DIR}")


if __name__ == "__main__":
    main()
