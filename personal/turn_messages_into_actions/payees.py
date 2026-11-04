"""The payee list in the banking app, as the recipe looks bills up in it.

This stands in for the bill-pay payee list your bank's app keeps (its payee or biller search).
Here it is backed by `data/payees.csv`, loaded into an in-memory SQLite database. Each payee has
a name and the other names it prints on a bill, the account number on file, whether it is on
autopay, and, for a payee with an online account, the balance and due date the bank last
fetched. A payee that only sends paper has nothing there but its name and account number.

`find_in_text` looks for every payee whose full name or alias appears in a bill, as whole words,
ignoring case and spacing. A shorter name inside a longer one that also appears ("Bayview Water"
inside "Bayview Water & Power") counts once, as the longer payee. The recipe treats more than
one payee, or none, as a question.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"


@dataclass(frozen=True)
class Payee:
    payee_id: str
    name: str
    account_number: str
    autopay: bool
    balance: Decimal | None  # from the online account; None for a paper-only payee
    due_date: date | None


class Payees:
    """The payee seed, with plain lookups."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self._db = sqlite3.connect(":memory:")
        self._db.executescript(
            """
            CREATE TABLE payees (payee_id TEXT PRIMARY KEY, name TEXT, account_number TEXT,
                                 autopay INTEGER, balance TEXT, due_date TEXT);
            CREATE TABLE names (key TEXT PRIMARY KEY, payee_id TEXT NOT NULL);
            """
        )
        for row in _rows(data_dir / "payees.csv"):
            autopay = row["autopay"].strip().casefold()
            if autopay not in ("yes", "no"):
                raise ValueError(f"payee {row['payee_id']}: autopay must be yes or no")
            self._db.execute(
                "INSERT INTO payees VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row["payee_id"],
                    row["name"].strip(),
                    row["account_number"].strip(),
                    autopay == "yes",
                    row["balance"].strip(),
                    row["due_date"].strip(),
                ),
            )
            for name in filter(None, (key(n) for n in [row["name"], *row["aliases"].split(";")])):
                self._db.execute("INSERT INTO names VALUES (?, ?)", (name, row["payee_id"]))

    def _payee(self, payee_id: str) -> Payee:
        row = self._db.execute(
            "SELECT payee_id, name, account_number, autopay, balance, due_date FROM payees "
            "WHERE payee_id = ?",
            (payee_id,),
        ).fetchone()
        return Payee(
            payee_id=str(row[0]),
            name=str(row[1]),
            account_number=str(row[2]),
            autopay=bool(row[3]),
            balance=Decimal(row[4]) if row[4] else None,
            due_date=date.fromisoformat(row[5]) if row[5] else None,
        )

    def find_in_text(self, text: str) -> list[Payee]:
        """Every payee whose name or alias appears in `text` as whole words."""
        haystack = f" {key(text)} "
        hits = [
            (str(name), str(payee_id))
            for name, payee_id in self._db.execute("SELECT key, payee_id FROM names")
            if f" {name} " in haystack
        ]
        # A name that is part of a longer name also found is the longer payee's, not its own.
        kept = {
            payee_id
            for name, payee_id in hits
            if not any(name != other and f" {name} " in f" {other} " for other, _ in hits)
        }
        return [self._payee(payee_id) for payee_id in sorted(kept)]


def key(text: str) -> str:
    """Text compared without case, punctuation or extra spaces; '&' is kept as a word."""
    return " ".join(re.findall(r"[^\W_]+(?:'[^\W_]+)*|&", text.casefold()))


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))
