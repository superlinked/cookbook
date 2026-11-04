"""The PIM (product information management system): the existing catalog, as lookups.

Each function stands in for a call you'd make to your PIM's product API: a product search by
GTIN or by manufacturer model number, a fetch by product ID, and the list of products you'd
compare a new record with (in a real PIM, a search filtered by category or brand). Here the
products come from data/pim_products.csv, loaded into an in-memory SQLite database, so the
recipe reads the catalog the way it would read a live system and never edits it.

Matching here is exact on purpose: a GTIN is compared as GTIN-14 (UPC-A 012345678905 and EAN-13
0012345678905 are one number), a model number without case, spaces or punctuation. A
placeholder (N/A, TBD, an all-zero GTIN) is no identifier at all, so it finds nothing. A lookup
returns every product it finds, so several products under one identifier reach the caller as a
list, never as a guess.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

SEED = Path(__file__).resolve().parent / "data" / "pim_products.csv"
COLUMNS = (
    *("record_id", "parent_id", "product_line", "brand", "title", "gtin", "model_number"),
    *("color", "size", "pack_count", "plug", "material", "x", "y", "w", "h"),
)


@dataclass(frozen=True)
class Product:
    record_id: str
    parent_id: str
    product_line: str
    brand: str
    title: str
    gtin: str
    model_number: str
    color: str
    size: str
    pack_count: str
    plug: str
    material: str
    box: tuple[int, int, int, int]  # x, y, width, height of the photo in data/photos.jpg


# Model numbers (after model_key) that systems write when they have none.
PLACEHOLDERS = {"NA", "NONE", "NULL", "TBD", "TBA", "UNKNOWN", "NOTAPPLICABLE"}


def gtin_key(value: str) -> str:
    """A GTIN as 14 digits, so UPC-A, EAN-13 and GTIN-14 forms of one number compare equal;
    "" when there are no digits or every digit is 0 (a placeholder)."""
    digits = re.sub(r"\D", "", value)
    return digits.zfill(14) if digits.strip("0") else ""


def model_key(value: str) -> str:
    """A model number without case, spaces or punctuation: "206mb-nv 12" is "206MBNV12"; ""
    for a placeholder (N/A, TBD, all zeros)."""
    key = re.sub(r"[^A-Z0-9]", "", value.upper())
    return "" if key in PLACEHOLDERS or not key.strip("0") else key


class Pim:
    """The existing catalog, loaded from a CSV seed into SQLite."""

    def __init__(self, seed: Path = SEED) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE products (" + ", ".join(f"{name} TEXT" for name in COLUMNS) + ", "
            "gtin_key TEXT, model_key TEXT)"
        )
        with seed.open(encoding="utf-8", newline="") as handle:
            rows = [
                (
                    *(row[name] for name in COLUMNS),
                    gtin_key(row["gtin"]),
                    model_key(row["model_number"]),
                )
                for row in csv.DictReader(handle)
            ]
        marks = ", ".join("?" * (len(COLUMNS) + 2))
        self.db.executemany(f"INSERT INTO products VALUES ({marks})", rows)

    def _select(self, where: str = "", *args: str) -> list[Product]:
        query = f"SELECT {', '.join(COLUMNS)} FROM products {where} ORDER BY record_id"
        return [_product(row) for row in self.db.execute(query, args)]

    def find_by_gtin(self, gtin: str) -> list[Product]:
        """Every product with this GTIN (a product search on the GTIN attribute)."""
        key = gtin_key(gtin)
        return self._select("WHERE gtin_key = ?", key) if key else []

    def find_by_model_number(self, model_number: str) -> list[Product]:
        """Every product with this manufacturer model number."""
        key = model_key(model_number)
        return self._select("WHERE model_key = ?", key) if key else []

    def get_product(self, record_id: str) -> Product | None:
        """One product by its PIM ID, or None when the PIM has no such product."""
        found = self._select("WHERE record_id = ?", record_id)
        return found[0] if found else None

    def candidates(self) -> list[Product]:
        """The products a new record is compared with. A real PIM would narrow this by
        category or brand first; this seed is small enough to compare with all of it."""
        return self._select()


def _product(row: tuple[str, ...]) -> Product:
    values = dict(zip(COLUMNS, row, strict=True))
    box = (int(values["x"]), int(values["y"]), int(values["w"]), int(values["h"]))
    return Product(**{name: values[name] for name in COLUMNS[:-4]}, box=box)
