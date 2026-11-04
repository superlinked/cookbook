"""Build data/: the PIM seed, the variant stock feed and the sample queries.

    uv run --group data make_data.py                          # queries.json and inventory.csv
    uv run --group data make_data.py --esci esci-test-0.parquet   # pim.csv too, from ESCI

data/pim.csv (the PIM seed) is product text from the Amazon Shopping Queries dataset (ESCI,
https://github.com/amazon-science/esci-data, Apache-2.0): title, brand, color and the start of
the bullet points of every US product with an English title that ESCI lists for seven of its
shopper queries (QUERIES below, by ESCI query_id). Only the product text and the query text are
kept; ESCI's relevance labels are not. To rebuild pim.csv, download one file of the Hugging Face
parquet mirror of ESCI's test split (about 160 MB) at the mirror's parquet revision
45c948250c2116f1e535bac67b92501c695307a4, and pass it with --esci. Its sha256 is
01b0da5ffc4d8e414ae1d04e98c89bd06ebb35196796de1fd99032a775b04a8e.

    curl -L -o esci-test-0.parquet \\
      https://huggingface.co/datasets/tasksource/esci/resolve/45c948250c2116f1e535bac67b92501c695307a4/default/test/0000.parquet

Everything else is made up here. ESCI has no stock, sizes or facets, so:

- data/inventory.csv, the variant stock feed, has one row per sellable variant. Each product's
  listing is one variant: the size its title names (SIZES below) in the PIM color, in stock.
  VARIANTS below adds or replaces the variants the examples need, and leaves one product out
  of the feed.
- data/queries.json holds the filters each shopper picked. They follow the query's words
  ("black", "size 8"). Q-08 is written for this recipe; the other seven are ESCI's query text.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import json
import re
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PIM_COLUMNS = ("product_id", "title", "brand", "color", "listing_text")
FEED_COLUMNS = ("product_id", "color", "size", "size_system", "in_stock")
MAX_LISTING_CHARS = 300

# The sample queries and the filters each shopper picked. `esci_query_id` is where the text came
# from (None: written for this recipe). Every shopper also asked for in-stock items only.
QUERIES: list[dict[str, Any]] = [
    {
        "id": "Q-01",
        "esci_query_id": 112614,
        "query": "women s black tennis shoes size 8",
        "filters": {"color": ["black"], "size": "8", "size_system": "women"},
    },
    {
        "id": "Q-02",
        "esci_query_id": 52479,
        "query": "i want a warm jacket with a hood and that is waterproof and black or dark "
        "coloured like navy",
        "filters": {"color": ["black", "navy"], "size": None, "size_system": None},
    },
    {
        "id": "Q-03",
        "esci_query_id": 67457,
        "query": "mens black 100% cotton dress socks",
        "filters": {"color": ["black"], "size": None, "size_system": None},
    },
    {
        "id": "Q-04",
        "esci_query_id": 8142,
        "query": "all white boots for girls",
        "filters": {"color": ["white"], "size": None, "size_system": None},
    },
    {
        "id": "Q-05",
        "esci_query_id": 30283,
        "query": "cropped black shirt",
        "filters": {"color": ["black"], "size": None, "size_system": None},
    },
    {
        "id": "Q-06",
        "esci_query_id": 16215,
        "query": "biys sz 9 black tennis shoes",
        "filters": {"color": ["black"], "size": "9", "size_system": "toddler"},
    },
    {
        "id": "Q-07",
        "esci_query_id": 112641,
        "query": "women shoes flats black size 12w",
        "filters": {"color": ["black"], "size": "12W", "size_system": "women"},
    },
    {
        "id": "Q-08",
        "esci_query_id": None,
        "query": "black running shoes",
        "filters": {"color": ["black"], "size": "8.5", "size_system": "women"},
    },
]

# The one variant each listing is: the size its title names, in that title's sizing system.
# A title with no size gets the size noted beside it, which is made up.
SIZES: dict[str, tuple[str, str]] = {
    "B0002TOZ1O": ("L", "apparel"),
    "B0002TOZ28": ("L", "apparel"),
    "B0006BKBMU": ("10", "toddler"),
    "B000V6VUPC": ("9", "men"),
    "B001NGKPE6": ("9", "men"),
    "B001RD29FI": ("6", "women"),
    "B002CHXICQ": ("38.5", "EU"),
    "B002WJIO00": ("1", "little kid"),
    "B003GDJ1P8": ("3", "infant"),
    "B004F7UASG": ("10-13", "sock"),
    "B004QF0TFQ": ("6-12", "sock"),
    "B0052OR548": ("38", "EU"),
    "B005JA8SN2": ("M", "youth"),
    "B007Y3O962": ("6-12", "sock"),  # no size in the title
    "B00FSE8GL8": ("1", "little kid"),
    "B00FXOFX1E": ("7", "toddler"),
    "B00KLMEWCO": ("6-12", "sock"),
    "B00NMFM242": ("XL", "apparel"),
    "B00NMFOMGI": ("XL", "apparel"),
    "B00P1NBO9C": ("XL", "apparel"),
    "B00QSNKB20": ("XL", "apparel"),
    "B00Y5X08OQ": ("XL", "apparel"),
    "B0116PY4EA": ("5.5", "women"),
    "B013C6J8GK": ("6-12", "sock"),
    "B01555SCZE": ("6-12", "sock"),
    "B017768WEA": ("7", "girls"),
    "B018YJH5SI": ("9", "toddler"),
    "B0198WS0NY": ("12", "women"),  # no size in the title; replaced in VARIANTS
    "B01B77VO7C": ("XL", "apparel"),
    "B01ESD61JI": ("8.5", "women"),
    "B01HFFXLNA": ("XL", "apparel"),
    "B01I0B63QA": ("1", "little kid"),
    "B01I4P4MBK": ("9", "women"),
    "B01L7B4Q2I": ("XL", "apparel"),
    "B01M1EXBTD": ("9", "toddler"),
    "B01MRZH2RU": ("9", "toddler"),
    "B01MTF2ALO": ("L", "apparel"),
    "B06XRSQCLL": ("8", "women"),
    "B071LF8XSF": ("8", "women"),
    "B0735T525V": ("6-12", "sock"),
    "B073D253CN": ("6-12", "sock"),
    "B073WGSPVT": ("7.5", "women"),
    "B076CTJ526": ("2", "little kid"),
    "B076CTZPJV": ("L", "apparel"),
    "B076FTC6FQ": ("XL", "apparel"),
    "B077YQ9L44": ("M", "apparel"),
    "B077ZCDYWW": ("9.5", "men"),
    "B077ZGYDZW": ("6-12", "sock"),
    "B077ZGZZ3G": ("6-12", "sock"),
    "B077ZJXCTH": ("6-12", "sock"),
    "B078H8B41C": ("12W", "women"),
    "B078Z1NP65": ("10", "toddler"),
    "B07B71VBYH": ("10", "toddler"),
    "B07B9DWXQ9": ("10-13", "sock"),
    "B07D79PJS7": ("6.5", "big kid"),
    "B07FMZPZPQ": ("8.5", "women"),
    "B07FZWFLTL": ("13", "big kid"),
    "B07G2M41ZW": ("9", "men"),
    "B07G5J4TKH": ("13", "little kid"),
    "B07HXNDSMJ": ("M", "apparel"),
    "B07J9KJ7WG": ("M", "apparel"),
    "B07K35WGML": ("3", "little kid"),
    "B07K36ZVM6": ("6", "toddler"),
    "B07L1MST3W": ("12", "little kid"),
    "B07L5M3TKK": ("9", "toddler"),
    "B07L5Q2YVK": ("9", "toddler"),
    "B07L9T5PFL": ("9", "toddler"),
    "B07M9SFR3T": ("8", "women"),
    "B07MMLP2NX": ("M", "apparel"),
    "B07NST8SFH": ("12-16", "sock"),
    "B07P95HVXH": ("10", "women"),
    "B07PMWJ8X2": ("9", "women"),
    "B07S2Z9433": ("XL", "apparel"),
    "B07S387Q2X": ("8", "women"),
    "B07T7F8NHZ": ("8", "toddler"),
    "B08515JQCF": ("L", "apparel"),
    "B087GC7Q1R": ("XXL", "apparel"),
    "B08DMGTR74": ("L", "apparel"),  # no size in the title
    "B08FRPL9DV": ("L", "apparel"),
}

# Variants set by hand: product_id -> its rows (color, size, size_system, in stock), replacing the
# one listed variant; color None means the PIM color. An empty list leaves the product out of
# the feed entirely (stock unknown).
VARIANTS: dict[str, list[tuple[str | None, str, str, bool]]] = {
    # Q-01: black size 8 sold out, while white size 8 and black size 9 are in stock.
    "B071LF8XSF": [
        (None, "8", "women", False),
        ("White", "8", "women", True),
        (None, "9", "women", True),
    ],
    # Q-01: sold out in every size.
    "B07S387Q2X": [
        (None, "7", "women", False),
        (None, "8", "women", False),
        (None, "9", "women", False),
    ],
    # Q-07: the 12 Wide pump is sold out; the 11 Wide is in stock.
    "B078H8B41C": [(None, "12W", "women", False), (None, "11W", "women", True)],
    # Q-07: the flat is sold out in every size it comes in.
    "B0198WS0NY": [
        (None, "11", "women", False),
        (None, "12", "women", False),
        (None, "12W", "women", False),
    ],
    # Q-03: a sock the stock feed doesn't have.
    "B077ZJXCTH": [],
}


def trim(text: str | None, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " ..."


def english(row: dict[str, Any]) -> bool:
    return str(row["product_title"]).isascii()


ESCI_COLUMNS = [
    "query",
    "query_id",
    "product_id",
    "product_locale",
    "product_title",
    "product_brand",
    "product_color",
    "product_bullet_point",
]


def read_esci(parquet: Path, query_ids: list[int]) -> list[dict[str, Any]]:
    """US rows of the ESCI parquet file for these query_ids."""
    pq = importlib.import_module("pyarrow.parquet")
    pc = importlib.import_module("pyarrow.compute")
    pa = importlib.import_module("pyarrow")
    table = pq.read_table(parquet, columns=ESCI_COLUMNS)
    keep = pc.and_(
        pc.equal(table["product_locale"], "us"),
        pc.is_in(table["query_id"], value_set=pa.array(query_ids)),
    )
    return cast(list[dict[str, Any]], table.filter(keep).to_pylist())


def esci_products(rows: list[dict[str, Any]]) -> tuple[dict[int, str], list[dict[str, str]]]:
    """(query text by ESCI query_id, PIM rows): one English text per product."""
    # The mirror repeats a product once per locale's text; keep the first English one.
    products: dict[str, dict[str, str]] = {}
    query_text: dict[int, str] = {}
    for row in sorted(rows, key=lambda r: (r["query_id"], r["product_id"], not english(r))):
        if not english(row):
            continue
        query_text[row["query_id"]] = row["query"]
        products.setdefault(
            row["product_id"],
            {
                "product_id": row["product_id"],
                "title": row["product_title"],
                "brand": row["product_brand"] or "",
                "color": row["product_color"] or "",
                "listing_text": trim(row["product_bullet_point"], MAX_LISTING_CHARS),
            },
        )
    return query_text, [products[pid] for pid in sorted(products)]


def write_pim(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PIM_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_pim(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return {row["product_id"]: row for row in csv.DictReader(handle)}


def feed_rows(pim: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    """One row per variant: the listed one from SIZES, or the ones in VARIANTS."""
    missing = sorted(set(pim) - set(SIZES))
    if missing:
        raise SystemExit(f"no size in SIZES for {missing}")
    rows: list[dict[str, str]] = []
    for pid in sorted(pim):
        size, system = SIZES[pid]
        variants = VARIANTS.get(pid, [(None, size, system, True)])
        for color, v_size, v_system, in_stock in variants:
            rows.append(
                {
                    "product_id": pid,
                    "color": pim[pid]["color"] if color is None else color,
                    "size": v_size,
                    "size_system": v_system,
                    "in_stock": "yes" if in_stock else "no",
                }
            )
    return rows


def queries_json() -> str:
    queries = [{**query, "filters": {**query["filters"], "in_stock": True}} for query in QUERIES]
    return json.dumps({"queries": queries}, indent=2) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--esci", type=Path, help="an ESCI test-split parquet file (see above)")
    args = parser.parse_args()
    DATA_DIR.mkdir(exist_ok=True)
    if args.esci:
        ids = [q["esci_query_id"] for q in QUERIES if q["esci_query_id"] is not None]
        query_text, rows = esci_products(read_esci(args.esci, ids))
        for query in QUERIES:
            esci_id = query["esci_query_id"]
            if esci_id is not None and query_text.get(esci_id) != query["query"]:
                raise SystemExit(f"{query['id']}: ESCI has {query_text.get(esci_id)!r}")
        write_pim(DATA_DIR / "pim.csv", rows)
    pim = read_pim(DATA_DIR / "pim.csv")
    with (DATA_DIR / "inventory.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FEED_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(feed_rows(pim))
    (DATA_DIR / "queries.json").write_text(queries_json(), encoding="utf-8")
    print(f"{len(QUERIES)} queries, {len(pim)} products in the PIM, feed written")


if __name__ == "__main__":
    main()
