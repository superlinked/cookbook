"""The bank's counterparty list, as the recipe looks things up in it.

This stands in for a query to the counterparty master in your loan system (a read-only SQL view
or the loan system's customer API): one row per borrower, with the names people actually write
and who owns the name inside the bank. Here it is backed by one readable CSV seed,
`data/counterparties.csv`, loaded into an in-memory SQLite database:

- `counterparty_id`, `legal_name`, `registration_number`: the borrower as the loan file has it.
- `aliases`: the other names trade press uses for it, `;`-separated ("Halden", "Northwind").
  Two borrowers may share an alias; that is a fact about the list, and the recipe sends such a
  mention to a person instead of picking one.
- `parent_ticker`: the listed parent's ticker as `EXCHANGE:SYMBOL`, empty for a private group.
- `relationship_manager`, `credit_officer`: who owns the name; alerts go to the credit officer.

Name lookups ignore case and surrounding spaces, nothing else: "Northwind Energy" is not
"Northwind Energy Services", and "Pellridge" is not "Pellridge Foods". A ticker lookup matches
the symbol and, when the article writes one, the exchange.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
SEED = "counterparties.csv"


@dataclass(frozen=True)
class Counterparty:
    id: str
    legal_name: str
    aliases: tuple[str, ...]
    registration_number: str
    parent_ticker: str  # "NYSE:NWEG", or "" for a private group
    relationship_manager: str
    credit_officer: str

    @property
    def names(self) -> tuple[str, ...]:
        """The legal name first, then every alias."""
        return (self.legal_name, *self.aliases)


class CounterpartyList:
    """The counterparty seed in an in-memory SQLite database, with plain lookups."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self._db = sqlite3.connect(":memory:")
        self._db.executescript(
            """
            CREATE TABLE counterparties (
                id TEXT PRIMARY KEY, legal_name TEXT NOT NULL, aliases TEXT NOT NULL,
                registration_number TEXT NOT NULL, parent_ticker TEXT NOT NULL,
                relationship_manager TEXT NOT NULL, credit_officer TEXT NOT NULL
            );
            CREATE TABLE names (
                name TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, PRIMARY KEY (name, id)
            );
            """
        )
        with (data_dir / SEED).open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                self._add(row)
        if not self.all():
            raise ValueError(f"{data_dir / SEED} lists no counterparties")

    def _add(self, row: dict[str, str]) -> None:
        cp_id, legal = row["counterparty_id"].strip(), row["legal_name"].strip()
        aliases = [a.strip() for a in row["aliases"].split(";") if a.strip()]
        ticker = row["parent_ticker"].strip().upper().replace(" ", "")
        if ticker and ":" not in ticker:
            raise ValueError(f"{cp_id}: parent_ticker {ticker!r} must be EXCHANGE:SYMBOL")
        if not row["credit_officer"].strip():
            raise ValueError(f"{cp_id} has no credit officer, so an alert would reach nobody")
        try:
            self._db.execute(
                "INSERT INTO counterparties VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    cp_id,
                    legal,
                    ";".join(aliases),
                    row["registration_number"].strip(),
                    ticker,
                    row["relationship_manager"].strip(),
                    row["credit_officer"].strip(),
                ),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"counterparty {cp_id} is listed twice") from None
        for name, kind in [(legal, "legal"), *((alias, "alias") for alias in aliases)]:
            self._db.execute(
                "INSERT OR IGNORE INTO names VALUES (?, ?, ?)", (_key(name), kind, cp_id)
            )

    def all(self) -> list[Counterparty]:
        """Every counterparty, in the seed's order."""
        rows = self._db.execute("SELECT * FROM counterparties ORDER BY rowid").fetchall()
        return [_counterparty(row) for row in rows]

    def get(self, counterparty_id: str) -> Counterparty:
        row = self._db.execute(
            "SELECT * FROM counterparties WHERE id = ?", (counterparty_id,)
        ).fetchone()
        if row is None:
            raise KeyError(counterparty_id)
        return _counterparty(row)

    def find_by_legal_name(self, name: str) -> list[Counterparty]:
        """Counterparties whose legal name is exactly `name` (ignoring case)."""
        return self._find(name, "legal")

    def find_by_alias(self, name: str) -> list[Counterparty]:
        """Counterparties that list `name` as an alias; two or more means the name is shared."""
        return self._find(name, "alias")

    def find_by_parent_ticker(self, symbol: str, exchange: str = "") -> list[Counterparty]:
        """Counterparties whose listed parent trades as `symbol` (on `exchange`, when given)."""
        symbol, exchange = symbol.strip().upper(), exchange.strip().upper()
        return [
            cp
            for cp in self.all()
            if cp.parent_ticker
            and cp.parent_ticker.split(":", 1)[1] == symbol
            and (not exchange or cp.parent_ticker.split(":", 1)[0] == exchange)
        ]

    def _find(self, name: str, kind: str) -> list[Counterparty]:
        rows = self._db.execute(
            "SELECT c.* FROM names n JOIN counterparties c ON c.id = n.id "
            "WHERE n.name = ? AND n.kind = ? ORDER BY c.rowid",
            (_key(name), kind),
        ).fetchall()
        return [_counterparty(row) for row in rows]


def _counterparty(row: tuple[str, ...]) -> Counterparty:
    cp_id, legal, aliases, number, ticker, manager, officer = row
    return Counterparty(
        cp_id, legal, tuple(a for a in aliases.split(";") if a), number, ticker, manager, officer
    )


def _key(name: str) -> str:
    return " ".join(name.strip().casefold().split())
