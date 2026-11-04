"""The loan system, as the recipe looks things up in it.

This stands in for the bank's loan origination or credit system: a borrower record lookup by
borrower ID (legal name, fiscal year end, reporting currency, and the fiscal year end the next
spread is due for), and last year's confirmed spread, read as the labels an analyst mapped to
template rows and confirmed. Here both come from two readable CSV seeds in `data/`, loaded into
an in-memory SQLite database:

- `loan_borrowers.csv`: one row per borrower.
- `loan_mappings.csv`: one row per confirmed mapping: borrower, statement, the section heading
  the label sat under ("" for none), the label, the template row and the period it was confirmed
  on.

Matching is exact, ignoring only case, spaces and punctuation: "Directors' loan account" is
"Directors loan account", but "Trade debtors" is not "Debtors", and "Bank loans" under "Creditors:
amounts falling due within one year" is not "Bank loans" under "... after more than one year". A
label that is close but not the same is a new label, and the recipe sends it to the reranker.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"


@dataclass(frozen=True)
class Borrower:
    borrower_id: str
    name: str
    fiscal_year_end: str  # MM-DD
    currency: str  # ISO 4217 code
    period_due: str  # YYYY-MM-DD, the fiscal year end this spread is for


@dataclass(frozen=True)
class Mapping:
    item_id: str
    confirmed_period: str  # the period of the spread it was confirmed on


def label_key(text: str) -> str:
    """A label or heading without case, spaces or punctuation: "Owner's equity:" is
    "ownersequity"."""
    return re.sub(r"[^0-9a-z]", "", text.casefold())


class LoanSystem:
    """Borrowers and last year's confirmed label mappings, from the CSV seeds."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self._db = sqlite3.connect(":memory:")
        self._db.executescript(
            """
            CREATE TABLE borrowers (
                borrower_id TEXT PRIMARY KEY, name TEXT NOT NULL, fiscal_year_end TEXT NOT NULL,
                currency TEXT NOT NULL, period_due TEXT NOT NULL);
            CREATE TABLE mappings (
                borrower_id TEXT NOT NULL, statement TEXT NOT NULL, heading TEXT NOT NULL,
                label TEXT NOT NULL, item_id TEXT NOT NULL, confirmed_period TEXT NOT NULL,
                PRIMARY KEY (borrower_id, statement, heading, label));
            """
        )
        for row in _rows(data_dir / "loan_borrowers.csv"):
            self._db.execute(
                "INSERT INTO borrowers VALUES (?, ?, ?, ?, ?)",
                tuple(row[name].strip() for name in Borrower.__dataclass_fields__),
            )
        for row in _rows(data_dir / "loan_mappings.csv"):
            key = (row["borrower_id"].strip(), row["statement"].strip())
            label = (label_key(row["heading"]), label_key(row["label"]))
            try:
                self._db.execute(
                    "INSERT INTO mappings VALUES (?, ?, ?, ?, ?, ?)",
                    (*key, *label, row["item_id"].strip(), row["confirmed_period"].strip()),
                )
            except sqlite3.IntegrityError:
                raise ValueError(
                    f"loan_mappings.csv maps {row['label']!r} under {row['heading']!r} twice "
                    f"for {key[0]}"
                ) from None

    def find_borrower(self, borrower_id: str) -> Borrower | None:
        """The borrower record for this ID, or None when the loan system has no such borrower."""
        row = self._db.execute(
            "SELECT borrower_id, name, fiscal_year_end, currency, period_due FROM borrowers "
            "WHERE borrower_id = ?",
            (borrower_id.strip(),),
        ).fetchone()
        return Borrower(*row) if row else None

    def confirmed_mapping(
        self, borrower_id: str, statement: str, heading: str, label: str
    ) -> Mapping | None:
        """The template row an analyst confirmed for this label under this heading on last
        year's spread, or None when the label is new."""
        row = self._db.execute(
            "SELECT item_id, confirmed_period FROM mappings WHERE borrower_id = ? AND "
            "statement = ? AND heading = ? AND label = ?",
            (borrower_id.strip(), statement, label_key(heading), label_key(label)),
        ).fetchone()
        return Mapping(*row) if row else None


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))
