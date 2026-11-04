"""The inventory system: stock per sellable variant (product, color, size).

Stands in for your inventory or availability API (a lookup of a product's variants and whether
each is in stock, as an order management or stock system returns it). The rows come from the
committed seed `data/inventory.csv`, loaded into an in-memory SQLite database. Each row is one
variant: its color and size as the PIM gave them, the sizing system the size belongs to
(women, men, toddler, apparel, ...) and whether it is in stock.

A product with no rows is not in the feed: its stock is unknown, which is not the same as out of
stock.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

SEED = Path(__file__).resolve().parent / "data" / "inventory.csv"
COLUMNS = ("product_id", "color", "size", "size_system", "in_stock")


@dataclass(frozen=True)
class Variant:
    product_id: str
    color: str
    size: str
    size_system: str
    in_stock: bool


class Inventory:
    def __init__(self, seed: Path = SEED) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE variants (product_id TEXT, color TEXT, size TEXT, size_system TEXT, "
            "in_stock INTEGER)"
        )
        with seed.open(encoding="utf-8", newline="") as handle:
            rows = []
            for row in csv.DictReader(handle):
                if row["in_stock"] not in ("yes", "no"):
                    raise ValueError(f"in_stock must be yes or no: {row}")
                rows.append(
                    (
                        row["product_id"],
                        row["color"],
                        row["size"],
                        row["size_system"],
                        int(row["in_stock"] == "yes"),
                    )
                )
        self.db.executemany("INSERT INTO variants VALUES (?, ?, ?, ?, ?)", rows)

    def find_variants(self, product_id: str) -> list[Variant]:
        """Every variant of the product the feed has; [] when the product isn't in the feed."""
        cursor = self.db.execute(
            f"SELECT {', '.join(COLUMNS)} FROM variants WHERE product_id = ? "
            "ORDER BY size_system, size, color",
            (product_id,),
        )
        return [
            Variant(pid, color, size, system, bool(stock))
            for pid, color, size, system, stock in cursor
        ]
