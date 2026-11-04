"""The card system: look up a card's transactions and their dispute status.

Stands in for the card platform's API (the card processor's transaction search and its dispute
case lookup), which gives you every authorisation on a card with its time, merchant, amount and
whether it was declined, plus each dispute raised on a transaction: open, or withdrawn by the
customer, with the network's dispute reference. Here the rows come from
`data/card_transactions.csv`, loaded into an in-memory SQLite database. Every timestamp carries
its UTC offset, and each transaction ID appears once.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

SEED = Path(__file__).resolve().parent / "data" / "card_transactions.csv"
# A transaction's dispute: none (""), still open, or withdrawn by the customer.
DISPUTE_STATUSES = ("", "open", "withdrawn")


@dataclass(frozen=True)
class Transaction:
    transaction_id: str
    at: datetime
    merchant: str
    amount: Decimal
    currency: str
    status: str  # posted | declined
    dispute_status: str  # "" | open | withdrawn
    dispute_ref: str

    def describe(self) -> str:
        return f"{self.transaction_id} {self.merchant} {self.amount} {self.currency}"


def _when(value: str) -> datetime:
    """An ISO 8601 timestamp with its UTC offset ("Z" or "+00:00"); one without is refused."""
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(f"timestamp {value!r} has no UTC offset")
    return moment


class CardSystem:
    def __init__(self, seed: Path = SEED) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE transactions (card_id TEXT, transaction_id TEXT PRIMARY KEY, at TEXT,"
            " merchant TEXT, amount TEXT, currency TEXT, status TEXT, dispute_status TEXT,"
            " dispute_ref TEXT)"
        )
        with seed.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            if row["dispute_status"] not in DISPUTE_STATUSES:
                raise ValueError(f"{row['transaction_id']}: unknown dispute status")
            _when(row["at"])
        try:
            self.db.executemany(
                "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        r["card_id"],
                        r["transaction_id"],
                        r["at"],
                        r["merchant"],
                        r["amount"],
                        r["currency"],
                        r["status"],
                        r["dispute_status"],
                        r["dispute_ref"],
                    )
                    for r in rows
                ],
            )
        except sqlite3.IntegrityError as error:
            raise ValueError(f"transactions: {error}") from None

    def transactions(self, card_id: str, since: datetime, until: datetime) -> list[Transaction]:
        """Every transaction on the card from `since` to `until`, oldest first."""
        rows = self.db.execute(
            "SELECT transaction_id, at, merchant, amount, currency, status, dispute_status,"
            " dispute_ref FROM transactions WHERE card_id = ? ORDER BY at, transaction_id",
            (card_id,),
        ).fetchall()
        found = [
            Transaction(
                row[0],
                _when(row[1]),
                row[2],
                Decimal(row[3]),
                row[4],
                row[5],
                row[6],
                row[7],
            )
            for row in rows
        ]
        return [t for t in found if since <= t.at <= until]
