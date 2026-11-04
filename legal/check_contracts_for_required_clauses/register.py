"""The register of information, as plain lookups: the ICT contracts with their suppliers, and
the documents that make up each contract package.

Stands in for two queries you'd send to the systems a bank already has: the contract's row in
the register of information DORA makes it keep (supplier, contract ID, the services, whether
they support a critical or important function), usually held in a vendor-risk or GRC tool, and
the contract repository's list of signed documents for that contract (the agreement with its
schedules, amendments, a DORA addendum). The seeds are data/register_contracts.csv and
data/register_documents.csv, loaded into an in-memory SQLite database so the lookups read like
the queries you'd send the real ones.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

CONTRACT_COLUMNS = ("contract_id", "supplier", "services", "critical_or_important")
DOCUMENT_COLUMNS = ("contract_id", "document", "kind", "file", "signed_on")
KINDS = ("agreement", "schedule", "amendment", "addendum")
LATER_KINDS = ("amendment", "addendum")  # documents that change an earlier one

# "Schedules 1 to 4", "Schedule 2", "Annex 3": the numbered parts a document title says it holds.
_PART_RE = re.compile(r"(?i)\b(schedule|annex|appendix)(?:es|s)?\s+(\d+)(?:\s+to\s+(\d+))?")


@dataclass(frozen=True)
class Contract:
    contract_id: str
    supplier: str
    services: str
    critical_or_important: bool


@dataclass(frozen=True)
class PackageDocument:
    contract_id: str
    document: str  # its title in the repository, e.g. "Amendment No. 1"
    kind: str  # agreement | schedule | amendment | addendum
    file: str
    signed_on: date

    @property
    def changes_earlier(self) -> bool:
        """An amendment or addendum: it changes the documents signed before it."""
        return self.kind in LATER_KINDS

    def parts(self) -> set[str]:
        """The numbered parts the title says the document holds: {"schedule 1", ...}."""
        held: set[str] = set()
        for match in _PART_RE.finditer(self.document):
            word, first, last = match.group(1).lower(), int(match.group(2)), match.group(3)
            held.update(f"{word} {n}" for n in range(first, int(last or first) + 1))
        return held


def _load(db: sqlite3.Connection, table: str, columns: tuple[str, ...], seed: Path) -> None:
    db.execute(f"CREATE TABLE {table} ({', '.join(columns)})")
    with seed.open(newline="", encoding="utf-8") as handle:
        rows = [tuple(row[name].strip() for name in columns) for row in csv.DictReader(handle)]
    marks = ", ".join("?" * len(columns))
    db.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)


class Register:
    def __init__(self, contracts: Path, documents: Path) -> None:
        self.db = sqlite3.connect(":memory:")
        _load(self.db, "contract", CONTRACT_COLUMNS, contracts)
        _load(self.db, "document", DOCUMENT_COLUMNS, documents)

    def contracts(self) -> list[Contract]:
        """Every ICT contract in the register, in register order."""
        rows = self.db.execute(f"SELECT {', '.join(CONTRACT_COLUMNS)} FROM contract ORDER BY rowid")
        return [_contract(row) for row in rows.fetchall()]

    def find_contract(self, contract_id: str) -> Contract | None:
        """The register row for this contract ID (exact match), or None."""
        row = self.db.execute(
            f"SELECT {', '.join(CONTRACT_COLUMNS)} FROM contract WHERE contract_id = ?",
            (contract_id,),
        ).fetchone()
        return _contract(row) if row else None

    def package(self, contract_id: str) -> list[PackageDocument]:
        """The signed documents the repository holds for this contract, oldest first.

        An unknown kind or an unreadable date raises ValueError: a package the recipe can't
        place in time can't tell an amendment from the agreement it changes.
        """
        rows = self.db.execute(
            f"SELECT {', '.join(DOCUMENT_COLUMNS)} FROM document WHERE contract_id = ? "
            "ORDER BY signed_on, rowid",
            (contract_id,),
        ).fetchall()
        documents = []
        for cid, title, kind, file, signed_on in rows:
            if kind not in KINDS:
                raise ValueError(f"{cid} {title}: unknown document kind {kind!r}")
            documents.append(PackageDocument(cid, title, kind, file, date.fromisoformat(signed_on)))
        return documents


def _contract(row: tuple[str, ...]) -> Contract:
    contract_id, supplier, services, critical = row
    flag = critical.strip().lower()
    if flag not in ("yes", "no"):
        raise ValueError(
            f"{contract_id}: critical_or_important must be yes or no, not {critical!r}"
        )
    return Contract(contract_id, supplier, services, flag == "yes")
