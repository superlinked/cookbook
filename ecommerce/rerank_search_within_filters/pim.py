"""The PIM (product information management system): product records by product ID.

Stands in for your PIM's product API (a `GET /products?ids=...` on Akeneo, Salsify or your
commerce platform's catalog). Here the records come from the committed seed `data/pim.csv`,
loaded into an in-memory SQLite database so the lookups below read like the queries you would
send. Lookups are by exact product ID only: a product ID the PIM doesn't have is not returned,
and nothing is guessed from a similar ID.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

SEED = Path(__file__).resolve().parent / "data" / "pim.csv"
COLUMNS = ("product_id", "title", "brand", "color", "listing_text")


@dataclass(frozen=True)
class Product:
    """One product record. `color` is "" when the PIM has no color for the product."""

    product_id: str
    title: str
    brand: str
    color: str
    listing_text: str


class PIM:
    def __init__(self, seed: Path = SEED) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE products (product_id TEXT PRIMARY KEY, title TEXT, brand TEXT, "
            "color TEXT, listing_text TEXT)"
        )
        with seed.open(encoding="utf-8", newline="") as handle:
            rows = [tuple(row[column] for column in COLUMNS) for row in csv.DictReader(handle)]
        self.db.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?)", rows)

    def get_products(self, product_ids: list[str]) -> dict[str, Product]:
        """The records for these IDs; an ID the PIM doesn't have is left out."""
        if not product_ids:
            return {}
        marks = ", ".join("?" for _ in product_ids)
        cursor = self.db.execute(
            f"SELECT {', '.join(COLUMNS)} FROM products WHERE product_id IN ({marks})",
            product_ids,
        )
        return {row[0]: Product(*row) for row in cursor}

    def all_products(self) -> list[Product]:
        """Every record, in product ID order: what a search index is built from."""
        cursor = self.db.execute(f"SELECT {', '.join(COLUMNS)} FROM products ORDER BY product_id")
        return [Product(*row) for row in cursor]
