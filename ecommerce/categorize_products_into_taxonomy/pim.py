"""The store's PIM (product information management system), as the recipe looks into it.

A stand-in for the PIM's product search filtered on the supplier and the supplier's SKU (in
Akeneo, a product search on the supplier and supplier-SKU attributes; in a store admin, a
product search on the vendor and the variant SKU). The records come from the committed seed
data/pim-products.csv, loaded into an in-memory SQLite database.

Matching is exact: the same supplier and the same SKU, with case ignored in the SKU the same way
the recipe's duplicate-SKU check ignores it ("tt-a08" is TT-A08). Two suppliers can use the same
code for different products, so the SKU alone never finds a record.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

PIM_PATH = Path(__file__).resolve().parent / "data" / "pim-products.csv"
COLUMNS = (
    "product_id",
    "supplier",
    "supplier_sku",
    "title",
    "category_id",
    "category_path",
    "attributes",
)


@dataclass(frozen=True)
class PimProduct:
    """A product the store already sells, with the category and attributes it has."""

    product_id: str
    supplier: str
    supplier_sku: str
    title: str
    category_id: str
    category_path: str
    attributes: str


def sku_key(sku: str) -> str:
    """The SKU as the lookup compares it: spacing around it and case ignored."""
    return sku.strip().casefold()


class Pim:
    """Read-only lookups into the PIM seed."""

    def __init__(self, path: Path = PIM_PATH) -> None:
        self.db = sqlite3.connect(":memory:")
        columns = ", ".join(f"{c} TEXT" for c in COLUMNS)
        self.db.execute(f"CREATE TABLE products ({columns}, sku_key TEXT)")
        with path.open(encoding="utf-8", newline="") as handle:
            rows = [
                (*(row[c] for c in COLUMNS), sku_key(row["supplier_sku"]))
                for row in csv.DictReader(handle)
            ]
        marks = ", ".join("?" * (len(COLUMNS) + 1))
        self.db.executemany(f"INSERT INTO products VALUES ({marks})", rows)

    def find_by_supplier_sku(self, supplier: str, sku: str) -> list[PimProduct]:
        """Every PIM product listed under this supplier's SKU (normally none or one)."""
        cursor = self.db.execute(
            f"SELECT {', '.join(COLUMNS)} FROM products WHERE supplier = ? AND sku_key = ? "
            "ORDER BY product_id",
            (supplier.strip(), sku_key(sku)),
        )
        return [PimProduct(*row) for row in cursor.fetchall()]
