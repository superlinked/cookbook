"""The document management system: each guidance document's section, title, population, status,
version and the document that replaced it.

Stands in for the metadata call your document management system or intranet already answers (a
`GET /documents/{id}` on SharePoint, PolicyStat, Q-Pulse or whatever the quality team keeps
controlled documents in). Here the records come from the committed seed
`data/document_system.csv`, loaded into an in-memory SQLite database so the lookups below read
like the queries you would send. Lookups are by exact document ID: an ID the system doesn't have
is not returned, and nothing is guessed from a similar ID.

Status and population are compared as the system stores them, after trimming spaces and case:
" Current" is "current", but "draft" is not "current".
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

SEED = Path(__file__).resolve().parent / "data" / "document_system.csv"
COLUMNS = ("doc_id", "section", "title", "population", "status", "version", "replaced_by")


@dataclass(frozen=True)
class Document:
    """One controlled document. `replaced_by` is "" when nothing replaced it."""

    doc_id: str
    section: str
    title: str
    population: str
    status: str
    version: str
    replaced_by: str

    def link(self) -> str:
        """Where the clinician opens the document. A placeholder scheme, not a live URL: put
        your document system's address here."""
        return f"dms://guidance/{self.doc_id}?version={self.version}"


@dataclass(frozen=True)
class Replacement:
    """Where following `replaced_by` from a retired document ends.

    `current` is the first current document on the chain, or None when the chain ends without
    one: no replacement (withdrawn), an ID the system doesn't have, or a loop. `chain` lists
    every ID followed, and `problem` says why there is no current document."""

    current: Document | None
    chain: tuple[str, ...]
    problem: str


def _clean(value: str) -> str:
    return " ".join(value.split()).lower()


class DocumentSystem:
    def __init__(self, seed: Path = SEED) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE documents (doc_id TEXT PRIMARY KEY, section TEXT, title TEXT, "
            "population TEXT, status TEXT, version TEXT, replaced_by TEXT)"
        )
        with seed.open(encoding="utf-8", newline="") as handle:
            rows = [
                (
                    row["doc_id"].strip(),
                    row["section"].strip(),
                    row["title"].strip(),
                    _clean(row["population"]),
                    _clean(row["status"]),
                    row["version"].strip(),
                    row["replaced_by"].strip(),
                )
                for row in csv.DictReader(handle)
            ]
        self.db.executemany("INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?)", rows)

    def get_documents(self, doc_ids: list[str]) -> dict[str, Document]:
        """The records for these IDs; an ID the system doesn't have is left out."""
        if not doc_ids:
            return {}
        marks = ", ".join("?" for _ in doc_ids)
        cursor = self.db.execute(
            f"SELECT {', '.join(COLUMNS)} FROM documents WHERE doc_id IN ({marks})", doc_ids
        )
        return {row[0]: Document(*row) for row in cursor}

    def find_replacement(self, doc_id: str, current_status: str = "current") -> Replacement:
        """Follow `replaced_by` from `doc_id` until a document with `current_status`."""
        chain: list[str] = []
        doc = self.get_documents([doc_id]).get(doc_id)
        while doc is not None and doc.replaced_by:
            next_id = doc.replaced_by
            if next_id in chain or next_id == doc_id:
                return Replacement(None, tuple(chain), f"the replacements loop back to {next_id}")
            chain.append(next_id)
            doc = self.get_documents([next_id]).get(next_id)
            if doc is None:
                return Replacement(
                    None, tuple(chain), f"replaced by {next_id}, which the document system lacks"
                )
            if doc.status == current_status:
                return Replacement(doc, tuple(chain), "")
        if not chain or doc is None:
            return Replacement(None, (), "no replacement")
        # The chain stopped at a document that isn't current (retired, draft, under review,
        # ...) and names no replacement: say which status it has, never assume "retired".
        return Replacement(
            None,
            tuple(chain),
            f"{chain[-1]} is {doc.status}, not {current_status}, with no replacement",
        )
