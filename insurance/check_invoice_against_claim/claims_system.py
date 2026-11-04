"""The claims system, as the recipe looks claims and their documents up in it.

This stands in for calls to your claims system's API (for example Guidewire ClaimCenter's
Claim API: `GET /claims/{claimId}` with its exposures, vendors and reserves, and the claim's
documents list). Here it is backed by four readable CSV seeds in `data/`, loaded into an
in-memory SQLite database:

- `claims.csv`: one open claim per row: claim number, status, policy number, line of business,
  named insured, date of loss, the loss address, and for auto the vehicle and its VIN.
- `claim_vendors.csv`: the vendors approved on each claim (the shop or contractor assigned).
- `authorizations.csv`: what the adjuster has authorized for the repair: the approved estimate
  and any approved supplements, each with its amount and the date it was approved.
- `claim_documents.csv`: the documents waiting on each claim: which claim the invoice was filed
  under, when it arrived, and which page of which scanned PDF holds it.

A claim is found by its exact claim number (after dropping spaces, without case). A number the
system doesn't hold comes back as None, never as the nearest claim.
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
class Authorization:
    kind: str  # estimate or supplement
    amount: Decimal
    approved_on: date


@dataclass(frozen=True)
class ClaimRecord:
    claim_number: str
    status: str
    policy_number: str
    line: str
    named_insured: str
    loss_date: date
    loss_address: str
    vehicle: str  # "" for a property claim
    vin: str  # "" for a property claim
    vendors: tuple[str, ...]
    authorizations: tuple[Authorization, ...]

    @property
    def authorized(self) -> Decimal:
        """What may be paid for the repair now: the estimate plus every approved supplement."""
        return sum((a.amount for a in self.authorizations), Decimal("0"))


@dataclass(frozen=True)
class ClaimDocument:
    document: str  # the claims system's document id
    claim_number: str  # the claim it was filed under
    kind: str
    received: date
    file: str
    page: int


def claim_key(number: str) -> str:
    """Claim numbers compare without spaces or case: "clm-26 104817" -> "CLM-26104817"."""
    return re.sub(r"\s+", "", number).upper()


class ClaimsSystem:
    """Open claims, their vendors, authorized amounts and waiting documents, with plain lookups."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self._db = sqlite3.connect(":memory:")
        self._db.executescript(
            """
            CREATE TABLE claims (
                key TEXT PRIMARY KEY, claim_number TEXT, status TEXT, policy_number TEXT,
                line TEXT, named_insured TEXT, loss_date TEXT, loss_address TEXT,
                vehicle TEXT, vin TEXT
            );
            CREATE TABLE vendors (key TEXT, vendor TEXT);
            CREATE TABLE authorizations (key TEXT, kind TEXT, amount TEXT, approved_on TEXT);
            CREATE TABLE documents (
                document TEXT PRIMARY KEY, claim_number TEXT, kind TEXT, received TEXT,
                file TEXT, page INTEGER
            );
            """
        )
        for row in _rows(data_dir / "claims.csv"):
            self._db.execute(
                "INSERT INTO claims VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    claim_key(row["claim_number"]),
                    *(row["claim_number"], row["status"], row["policy_number"], row["line"]),
                    *(row["named_insured"], row["loss_date"], row["loss_address"]),
                    *(row["vehicle"], row["vin"]),
                ),
            )
        for row in _rows(data_dir / "claim_vendors.csv"):
            self._db.execute(
                "INSERT INTO vendors VALUES (?, ?)", (claim_key(row["claim_number"]), row["vendor"])
            )
        for row in _rows(data_dir / "authorizations.csv"):
            self._db.execute(
                "INSERT INTO authorizations VALUES (?, ?, ?, ?)",
                (claim_key(row["claim_number"]), row["kind"], row["amount"], row["approved_on"]),
            )
        for row in _rows(data_dir / "claim_documents.csv"):
            # The document ID names the run's invoices/<document>.json, so it is a bare name too.
            if Path(row["document"]).name != row["document"] or row["document"] in ("", ".."):
                raise ValueError(f"document ID {row['document']!r} is not a plain name")
            # A file name only, inside data/: a path in the seed could point anywhere.
            if Path(row["file"]).name != row["file"] or not (data_dir / row["file"]).is_file():
                raise ValueError(f"{row['document']}: no document file {row['file']!r} in data/")
            self._db.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?)",
                (
                    *(row["document"], row["claim_number"], row["kind"], row["received"]),
                    *(row["file"], int(row["page"])),
                ),
            )

    def waiting_invoices(self) -> list[ClaimDocument]:
        """Every repair invoice waiting on a claim, in the seed's order."""
        return [
            ClaimDocument(document, claim, kind, date.fromisoformat(received), file, page)
            for document, claim, kind, received, file, page in self._db.execute(
                "SELECT document, claim_number, kind, received, file, page FROM documents "
                "WHERE kind = 'repair_invoice' ORDER BY rowid"
            )
        ]

    def get_claim(self, claim_number: str) -> ClaimRecord | None:
        """The claim with exactly this number, or None if the system holds no such claim."""
        key = claim_key(claim_number)
        row = self._db.execute(
            "SELECT claim_number, status, policy_number, line, named_insured, loss_date, "
            "loss_address, vehicle, vin FROM claims WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        vendors = tuple(
            vendor
            for (vendor,) in self._db.execute(
                "SELECT vendor FROM vendors WHERE key = ? ORDER BY rowid", (key,)
            )
        )
        authorizations = tuple(
            Authorization(kind, Decimal(amount), date.fromisoformat(approved))
            for kind, amount, approved in self._db.execute(
                "SELECT kind, amount, approved_on FROM authorizations WHERE key = ? ORDER BY rowid",
                (key,),
            )
        )
        number, status, policy, line, insured, loss, address, vehicle, vin = row
        return ClaimRecord(
            *(number, status, policy, line, insured, date.fromisoformat(loss), address),
            *(vehicle, vin, vendors, authorizations),
        )


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))
