"""The onboarding system, as the recipe looks cases up in it.

This stands in for a call to your onboarding or client lifecycle system's case API (for example
`GET /cases/{id}` returning the application form and the list of uploaded documents). Here it is
backed by two readable CSV seeds in `data/`, loaded into an in-memory SQLite database:

- `onboarding_cases.csv`: one application per case, as the customer filled in the form: the case
  date, then one column per form field. A field the customer left blank is empty.
- `onboarding_documents.csv`: one row per uploaded document and purpose: the document id, what
  the customer gave it for (`identity` or `address`), the type they said it is, and which page of
  which scanned PDF holds it. One document given for both purposes has two rows.

A case is found by its exact id. A case with no documents comes back with an empty list; a case
id the system doesn't hold comes back as None.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
# Columns of onboarding_cases.csv that are about the case, not form fields.
CASE_COLUMNS = ("case", "received")


@dataclass(frozen=True)
class SubmittedDocument:
    document: str  # the onboarding system's document id
    purpose: str  # identity or address
    declared_type: str  # what the customer said it is: passport, utility_bill, ...
    file: str
    page: int


@dataclass(frozen=True)
class Application:
    case: str
    received: date
    form: dict[str, str]  # form field -> value as typed, "" when left blank
    documents: tuple[SubmittedDocument, ...]


class OnboardingSystem:
    """Applications and their uploaded documents from the onboarding seeds, with plain lookups."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self._db = sqlite3.connect(":memory:")
        cases = _rows(data_dir / "onboarding_cases.csv")
        self.form_fields = tuple(
            name for name in (cases[0].keys() if cases else ()) if name not in CASE_COLUMNS
        )
        self._db.executescript(
            """
            CREATE TABLE cases (name TEXT PRIMARY KEY, received TEXT NOT NULL);
            CREATE TABLE form (name TEXT, field TEXT, value TEXT, PRIMARY KEY (name, field));
            CREATE TABLE documents (
                name TEXT, document TEXT, purpose TEXT, declared_type TEXT, file TEXT,
                page INTEGER, PRIMARY KEY (name, document, purpose)
            );
            """
        )
        for row in cases:
            self._db.execute("INSERT INTO cases VALUES (?, ?)", (row["case"], row["received"]))
            for field in self.form_fields:
                self._db.execute(
                    "INSERT INTO form VALUES (?, ?, ?)", (row["case"], field, row[field].strip())
                )
        for row in _rows(data_dir / "onboarding_documents.csv"):
            values = (row["case"], row["document"], row["purpose"], row["declared_type"])
            self._db.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?)",
                (*values, row["file"], int(row["page"])),
            )

    def open_cases(self) -> list[str]:
        """Every case id waiting for a KYC check, in the seed's order."""
        return [name for (name,) in self._db.execute("SELECT name FROM cases ORDER BY rowid")]

    def get_case(self, case: str) -> Application | None:
        """The application and its uploaded documents, or None if there is no such case."""
        row = self._db.execute("SELECT received FROM cases WHERE name = ?", (case,)).fetchone()
        if row is None:
            return None
        form = dict(
            self._db.execute("SELECT field, value FROM form WHERE name = ? ORDER BY rowid", (case,))
        )
        documents = tuple(
            SubmittedDocument(document, purpose, declared_type, file, page)
            for document, purpose, declared_type, file, page in self._db.execute(
                "SELECT document, purpose, declared_type, file, page FROM documents "
                "WHERE name = ? ORDER BY rowid",
                (case,),
            )
        )
        return Application(case, date.fromisoformat(row[0]), form, documents)


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))
