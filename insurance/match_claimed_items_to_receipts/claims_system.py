"""The claims system: the open contents claims, as lookups.

Each function stands in for a call you'd make to your claims system's API: the contents claims
waiting in a handler's queue, a claim fetched by its number (policy, policyholder, date of loss,
contents amount claimed), the documents attached to it (which uploaded page is a receipt photo
and which is the inventory), and the inventory rows when the policyholder filled in the portal's
inventory form instead of writing the list by hand. Here they come from three CSV seeds in
data/ loaded into an in-memory SQLite database, so the recipe reads the claim the way it would
read a live system and never edits it.

A claim is looked up by its exact claim number. A number the system doesn't have returns None,
never a guess at a neighbouring claim.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
TABLES = {
    "claims": (
        "claims.csv",
        (
            *("claim_number", "policy_number", "policyholder", "date_of_loss"),
            *("contents_claimed", "inventory_source"),
        ),
    ),
    "attachments": (
        "attachments.csv",
        ("claim_number", "attachment", "kind", "file", "page"),
    ),
    "portal_inventory": (
        "portal_inventory.csv",
        ("claim_number", "line", "description", "quantity", "unit_price"),
    ),
}


@dataclass(frozen=True)
class Claim:
    claim_number: str
    policy_number: str
    policyholder: str
    date_of_loss: date
    contents_claimed: Decimal
    inventory_source: str  # "handwritten" (a photo of a written list) or "portal" (form rows)


@dataclass(frozen=True)
class Attachment:
    attachment: str  # the claim system's name for the upload, e.g. "receipt-2"
    kind: str  # "receipt" or "inventory"
    file: str
    page: int  # 1-based page in `file`


@dataclass(frozen=True)
class InventoryRow:
    line: int
    description: str
    quantity: int
    unit_price: Decimal


class ClaimsSystem:
    """The claims system, loaded from the CSV seeds in `data_dir` into SQLite."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self.db = sqlite3.connect(":memory:")
        for table, (file, columns) in TABLES.items():
            self.db.execute(f"CREATE TABLE {table} ({', '.join(f'{c} TEXT' for c in columns)})")
            with (data_dir / file).open(encoding="utf-8", newline="") as handle:
                records = list(csv.DictReader(handle))
            if table == "attachments":
                for record in records:
                    # Only a file name in data_dir: no path that reads outside it.
                    name = record["file"]
                    if Path(name).name != name or not (data_dir / name).is_file():
                        raise ValueError(
                            f"{record['claim_number']} {record['attachment']}: no document "
                            f"file {name!r} in {data_dir}"
                        )
            rows = [tuple(record[c] for c in columns) for record in records]
            marks = ", ".join("?" * len(columns))
            self.db.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)

    def open_contents_claims(self) -> list[str]:
        """The claim numbers in the contents handler's queue, oldest number first."""
        query = "SELECT claim_number FROM claims ORDER BY claim_number"
        return [row[0] for row in self.db.execute(query)]

    def get_claim(self, claim_number: str) -> Claim | None:
        """One claim by its exact number, or None when the system has no such claim."""
        row = self.db.execute(
            "SELECT claim_number, policy_number, policyholder, date_of_loss, contents_claimed, "
            "inventory_source FROM claims WHERE claim_number = ?",
            (claim_number.strip(),),
        ).fetchone()
        if row is None:
            return None
        number, policy, holder, loss, claimed, source = row
        return Claim(number, policy, holder, date.fromisoformat(loss), Decimal(claimed), source)

    def attachments(self, claim_number: str) -> list[Attachment]:
        """The uploaded pages on a claim, in the order the claim system lists them."""
        rows = self.db.execute(
            "SELECT attachment, kind, file, page FROM attachments WHERE claim_number = ? "
            "ORDER BY rowid",
            (claim_number,),
        )
        return [Attachment(name, kind, file, int(page)) for name, kind, file, page in rows]

    def inventory_rows(self, claim_number: str) -> list[InventoryRow]:
        """The inventory as the portal's form stored it; empty for a handwritten inventory."""
        rows = self.db.execute(
            "SELECT line, description, quantity, unit_price FROM portal_inventory "
            "WHERE claim_number = ? ORDER BY CAST(line AS INTEGER)",
            (claim_number,),
        )
        return [
            InventoryRow(int(line), text, int(qty), Decimal(price))
            for line, text, qty, price in rows
        ]
