"""The PIM lookup: the product records the store's product information management system holds.

In production this module is a few calls to your PIM or commerce platform's product API (a
product search filtered by category and availability, and the list of category values the app
offers as department facets). Here it stands in for those calls: data/catalog.csv is the seed,
loaded into an in-memory SQLite database, one row per product with its ID, PIM category, title,
main color, availability and where its main photo is.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

CATALOG_PATH = Path(__file__).resolve().parent / "data" / "catalog.csv"

Box = tuple[int, int, int, int]  # x, y, width, height of a photo in its sprite sheet

COLUMNS = ("product_id", "category", "title", "color", "available", "x", "y", "w", "h")


@dataclass(frozen=True)
class Product:
    product_id: str
    category: str
    title: str
    color: str  # the product's main visible color, one word
    available: bool
    box: Box  # the main photo's tile in data/catalog-photos.jpg


class PIM:
    """Plain lookups on the product records. Department names match exactly, as facet values do."""

    def __init__(self, seed: Path = CATALOG_PATH) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE product (product_id TEXT PRIMARY KEY, category TEXT NOT NULL, "
            "title TEXT NOT NULL, color TEXT NOT NULL, available INTEGER NOT NULL, "
            "x INTEGER, y INTEGER, w INTEGER, h INTEGER)"
        )
        with seed.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            if row["available"] not in ("yes", "no"):
                raise ValueError(f"{row['product_id']}: available must be yes or no")
        self.db.executemany(
            "INSERT INTO product VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    *(row["product_id"], row["category"], row["title"], row["color"]),
                    row["available"] == "yes",
                    *(int(row[key]) for key in ("x", "y", "w", "h")),
                )
                for row in rows
            ],
        )

    def _products(self, where: str = "", params: tuple[object, ...] = ()) -> list[Product]:
        cursor = self.db.execute(
            f"SELECT {', '.join(COLUMNS)} FROM product {where} ORDER BY product_id", params
        )
        return [
            Product(
                product_id=pid,
                category=category,
                title=title,
                color=color,
                available=bool(available),
                box=(x, y, w, h),
            )
            for pid, category, title, color, available, x, y, w, h in cursor
        ]

    def all_products(self) -> list[Product]:
        """Every product record, available or not: the catalog index covers them all."""
        return self._products()

    def departments(self) -> set[str]:
        """The category values the catalog uses, which are the valid department facets."""
        return {
            category for (category,) in self.db.execute("SELECT DISTINCT category FROM product")
        }

    def has_department(self, department: str) -> bool:
        return department in self.departments()

    def sellable(self, department: str = "") -> list[Product]:
        """The available products, in the department when one is given."""
        if department:
            return self._products("WHERE available = 1 AND category = ?", (department,))
        return self._products("WHERE available = 1")

    def find_product(self, product_id: str) -> Product | None:
        found = self._products("WHERE product_id = ?", (product_id,))
        return found[0] if found else None
