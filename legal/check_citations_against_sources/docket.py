"""The court's docket, as one plain lookup.

Stands in for the docketing system (or the court's case search) the firm already uses: given a
case number, which court the case is in, that court's jurisdiction, the judge and the next
deadline. The seed is data/docket.csv, loaded into an in-memory SQLite database.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

COLUMNS = ("case_number", "caption", "court", "jurisdiction", "judge", "next_deadline")


@dataclass(frozen=True)
class Case:
    case_number: str
    caption: str
    court: str
    jurisdiction: str
    judge: str
    next_deadline: str


class Docket:
    def __init__(self, seed: Path) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute(f"CREATE TABLE cases ({', '.join(COLUMNS)}, UNIQUE (case_number))")
        with seed.open(newline="", encoding="utf-8") as handle:
            rows = [tuple(row[name] for name in COLUMNS) for row in csv.DictReader(handle)]
        self.db.executemany(f"INSERT INTO cases VALUES ({', '.join('?' * 6)})", rows)

    def find_case(self, case_number: str) -> Case | None:
        """The case with exactly this number (spaces trimmed), or None."""
        row = self.db.execute(
            f"SELECT {', '.join(COLUMNS)} FROM cases WHERE case_number = ?",
            (case_number.strip(),),
        ).fetchone()
        return Case(*row) if row else None
