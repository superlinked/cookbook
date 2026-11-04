"""The citator (KeyCite or Shepard's), as one plain lookup.

Stands in for a fresh citator run on each authority the draft cites: its status (good law,
superseded, overruled, repealed), what replaced it, and when it was checked. The seed is
data/citator.csv, one row per citation, loaded into an in-memory SQLite database. The recipe
takes this as given and never asks a model whether an authority is still good law.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CitatorResult:
    citation: str
    status: str  # good_law, superseded, overruled, repealed
    replaced_by: str  # the citation that replaced it, or ""
    checked_on: str  # YYYY-MM-DD


def spaced(citation: str) -> str:
    """The citation with single spaces (a non-breaking space counts as one)."""
    return " ".join(citation.replace(chr(0xA0), " ").split())


class Citator:
    def __init__(self, seed: Path) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE result (citation, status, replaced_by, checked_on, key TEXT UNIQUE)"
        )
        with seed.open(newline="", encoding="utf-8") as handle:
            rows = [
                (
                    row["citation"],
                    row["status"],
                    row["replaced_by"],
                    row["checked_on"],
                    spaced(row["citation"]),
                )
                for row in csv.DictReader(handle)
            ]
        self.db.executemany("INSERT INTO result VALUES (?, ?, ?, ?, ?)", rows)

    def status(self, citation: str) -> CitatorResult | None:
        """The citator's result for exactly this citation, parenthetical included (one version
        of a statute is not another), or None when it has none."""
        row = self.db.execute(
            "SELECT citation, status, replaced_by, checked_on FROM result WHERE key = ?",
            (spaced(citation),),
        ).fetchone()
        return CitatorResult(*row) if row else None
