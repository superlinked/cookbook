"""The asset inventory (CMDB): which product and version runs on which host, and who owns it.

Stands in for your asset inventory's API (a search of configuration items by product name, and
a fetch by host name). Here the assets come from the committed seed `data/asset_inventory.csv`,
loaded into an in-memory SQLite database: one row per host, with the product it runs, the
product's vendor and version, the other names people use for the product (`aliases`, separated
by `;`) and the owning team.

A product lookup is exact on purpose: the name must equal a product name or alias as a whole,
ignoring case, extra spaces and a leading "the". "Corvane VPN Concentrator" doesn't find
"Corvane VPN Gateway", and neither does "Corvane VPN". A lookup returns every host it finds, so
an alias two products share comes back as both products, never as a guess.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

SEED = Path(__file__).resolve().parent / "data" / "asset_inventory.csv"
COLUMNS = ("host", "product", "vendor", "version", "aliases", "owner_team")


@dataclass(frozen=True)
class Asset:
    host: str
    product: str
    vendor: str
    version: str
    aliases: tuple[str, ...]
    owner_team: str


def name_key(name: str) -> str:
    """A product name compared without case, extra spaces or a leading "the"."""
    words = name.casefold().split()
    if words[:1] == ["the"]:
        words = words[1:]
    return " ".join(words)


class AssetInventory:
    """The inventory, loaded from a CSV seed into SQLite."""

    def __init__(self, seed: Path = SEED) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE assets (" + ", ".join(f"{name} TEXT" for name in COLUMNS) + ")"
        )
        self.db.execute("CREATE TABLE names (key TEXT, surface TEXT, host TEXT)")
        with seed.open(encoding="utf-8", newline="") as handle:
            rows = [tuple(row[name].strip() for name in COLUMNS) for row in csv.DictReader(handle)]
        for row in rows:
            if not all(row[i] for i in (0, 1, 2, 5)):
                raise ValueError(f"host, product, vendor and owner_team are required: {row}")
        self.db.executemany(f"INSERT INTO assets VALUES ({', '.join('?' * len(COLUMNS))})", rows)
        names = [
            (name_key(surface), surface, row[0])
            for row in rows
            for surface in (row[1], *row[4].split(";"))
            if surface.strip()
        ]
        self.db.executemany("INSERT INTO names VALUES (?, ?, ?)", names)

    def _select(self, where: str, *args: str) -> list[Asset]:
        query = f"SELECT {', '.join(COLUMNS)} FROM assets {where} ORDER BY host"
        return [_asset(row) for row in self.db.execute(query, args)]

    def find_product(self, name: str) -> list[Asset]:
        """Every host whose product name or one of its aliases is exactly this name."""
        return self._select("WHERE host IN (SELECT host FROM names WHERE key = ?)", name_key(name))

    def find_host(self, host: str) -> list[Asset]:
        """The inventory's rows for this host; [] when the inventory doesn't know it."""
        return self._select("WHERE host = ?", host)

    def names(self) -> list[str]:
        """Every product name and alias as written in the inventory, longest first."""
        cursor = self.db.execute("SELECT DISTINCT surface FROM names")
        return sorted((surface.strip() for (surface,) in cursor), key=lambda s: (-len(s), s))

    def find_vendor(self, vendor: str) -> list[Asset]:
        """Every host running a product from this vendor (vendor name, any case)."""
        return self._select("WHERE lower(vendor) = lower(?)", vendor.strip())

    def vendors(self) -> list[str]:
        """Every vendor in the inventory."""
        return [v for (v,) in self.db.execute("SELECT DISTINCT vendor FROM assets ORDER BY 1")]


def _asset(row: tuple[str, ...]) -> Asset:
    host, product, vendor, version, aliases, owner = row
    names = tuple(a.strip() for a in aliases.split(";") if a.strip())
    return Asset(host, product, vendor, version, names, owner)
