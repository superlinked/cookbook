"""The client and matter system: the lawyers on a matter.

Stands in for your matter-management system's matter team lookup (the firm's lawyers staffed on
the matter and the client's in-house counsel). The seed in data/matter_system.csv is loaded into
an in-memory sqlite3 database. `other_names` holds the other ways the documents write a lawyer's
name (a surname on its own), separated by semicolons.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

SEED = Path(__file__).resolve().parent / "data" / "matter_system.csv"
FIELDS = ("matter", "name", "email", "role", "organization", "other_names")


@dataclass(frozen=True)
class Lawyer:
    name: str
    email: str
    role: str
    organization: str
    other_names: tuple[str, ...]


def lawyers_on_matter(matter: str, seed: Path = SEED) -> list[Lawyer]:
    """Every lawyer on the matter, by exact matter number; an empty list if none is."""
    db = sqlite3.connect(":memory:")
    try:
        db.execute(f"CREATE TABLE team ({', '.join(f'{name} TEXT' for name in FIELDS)})")
        with seed.open(newline="", encoding="utf-8") as handle:
            rows = [tuple(row[name] or "" for name in FIELDS) for row in csv.DictReader(handle)]
        db.executemany(f"INSERT INTO team VALUES ({', '.join('?' for _ in FIELDS)})", rows)
        found = db.execute(
            "SELECT name, email, role, organization, other_names FROM team "
            "WHERE matter = ? ORDER BY name",
            (matter,),
        ).fetchall()
    finally:
        db.close()
    return [
        Lawyer(
            name, email, role, organization, tuple(n.strip() for n in other.split(";") if n.strip())
        )
        for name, email, role, organization, other in found
    ]
