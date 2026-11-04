"""The claims system: look up a claim, its file, its parties, reserves, payments and vendors.

Stands in for the claims system's claim API (e.g. a GET on the claim in Guidewire ClaimCenter's
Cloud API, with its contacts, documents and notes, reserve lines, payments and service requests),
which gives you the claim and policy numbers, the insureds, the date of loss, the adjuster on the
claim and the one taking it over, every note and email filed on it in date order, the reserves
by coverage, every payment issued and the vendors assigned. Here the rows come from the CSV
seeds in `data/`, loaded into an in-memory SQLite database; each note or email body is a text
file in `data/documents/`.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
BODIES = DATA / "documents"
# Who wrote a document: insurer staff, the insured, or a vendor.
ROLES = ("staff", "insured", "vendor")
TABLES = {
    "claims": (
        "claim_number",
        "policy_number",
        "line",
        "insureds",
        "date_of_loss",
        "reported_on",
        "adjuster_out",
        "adjuster_in",
        "handover_reason",
        "handover_on",
    ),
    "claim_documents": (
        "claim_number",
        "doc_id",
        "saved_at",
        "kind",
        "author",
        "author_role",
        "file",
    ),
    "parties": ("claim_number", "name", "role", "phone"),
    "reserves": ("claim_number", "coverage", "amount", "set_on"),
    "payments": ("claim_number", "payment_id", "payee", "coverage", "amount", "issued_on"),
    "vendors": ("claim_number", "vendor", "service", "contact", "job_ticket", "assigned_on"),
}


@dataclass(frozen=True)
class Claim:
    claim_number: str
    policy_number: str
    line: str
    insureds: list[str]
    date_of_loss: date
    reported_on: date
    adjuster_out: str
    adjuster_in: str
    handover_reason: str
    handover_on: date


@dataclass(frozen=True)
class Document:
    doc_id: str
    saved_at: datetime
    kind: str
    author: str
    author_role: str  # staff | insured | vendor
    text: str


@dataclass(frozen=True)
class Party:
    name: str
    role: str
    phone: str


@dataclass(frozen=True)
class Reserve:
    coverage: str  # A, B, C or D
    amount: Decimal
    set_on: date


@dataclass(frozen=True)
class Payment:
    payment_id: str
    payee: str
    coverage: str
    amount: Decimal
    issued_on: date

    def describe(self) -> str:
        return f"{self.payment_id} ({self.payee}, {self.amount} under Coverage {self.coverage})"


@dataclass(frozen=True)
class Vendor:
    vendor: str
    service: str
    contact: str
    job_ticket: str
    assigned_on: date


class ClaimsSystem:
    def __init__(self, data: Path = DATA, bodies: Path = BODIES) -> None:
        self.bodies = bodies
        self.db = sqlite3.connect(":memory:")
        for table, columns in TABLES.items():
            self.db.execute(f"CREATE TABLE {table} ({', '.join(f'{c} TEXT' for c in columns)})")
            with (data / f"{table}.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            if table == "claim_documents":
                for r in rows:
                    if r["author_role"] not in ROLES:
                        raise ValueError(f"{r['doc_id']}: author_role must be one of {ROLES}")
                    if Path(r["file"]).name != r["file"] or not (bodies / r["file"]).is_file():
                        raise ValueError(
                            f"{r['doc_id']}: no document file {r['file']!r} in {bodies}"
                        )
            marks = ", ".join("?" for _ in columns)
            self.db.executemany(
                f"INSERT INTO {table} VALUES ({marks})",
                [tuple(r[c] for c in columns) for r in rows],
            )

    def _rows(self, table: str, claim_number: str, order: str) -> list[dict[str, str]]:
        columns = TABLES[table]
        found = self.db.execute(
            f"SELECT {', '.join(columns)} FROM {table} WHERE claim_number = ? ORDER BY {order}",
            (claim_number,),
        ).fetchall()
        return [dict(zip(columns, row, strict=True)) for row in found]

    def find_claim(self, claim_number: str) -> Claim | None:
        """The claim with exactly this number, or None."""
        rows = self._rows("claims", claim_number, "claim_number")
        if not rows:
            return None
        r = rows[0]
        return Claim(
            r["claim_number"],
            r["policy_number"],
            r["line"],
            [name.strip() for name in r["insureds"].split(";") if name.strip()],
            date.fromisoformat(r["date_of_loss"]),
            date.fromisoformat(r["reported_on"]),
            r["adjuster_out"],
            r["adjuster_in"],
            r["handover_reason"],
            date.fromisoformat(r["handover_on"]),
        )

    def list_claims(self) -> list[str]:
        """Every claim number, in order."""
        return [r[0] for r in self.db.execute("SELECT claim_number FROM claims ORDER BY 1")]

    def claim_documents(self, claim_number: str) -> list[Document]:
        """Every note and email filed on the claim, oldest first."""
        return [
            Document(
                r["doc_id"],
                datetime.fromisoformat(r["saved_at"].replace("Z", "+00:00")),
                r["kind"],
                r["author"],
                r["author_role"],
                (self.bodies / r["file"]).read_text(encoding="utf-8"),
            )
            for r in self._rows("claim_documents", claim_number, "saved_at, doc_id")
        ]

    def parties(self, claim_number: str) -> list[Party]:
        """The people on the claim, with the phone the claims system holds for each."""
        return [
            Party(r["name"], r["role"], r["phone"])
            for r in self._rows("parties", claim_number, "name")
        ]

    def reserves(self, claim_number: str) -> list[Reserve]:
        """The open reserve on each coverage."""
        return [
            Reserve(r["coverage"], Decimal(r["amount"]), date.fromisoformat(r["set_on"]))
            for r in self._rows("reserves", claim_number, "coverage")
        ]

    def payments(self, claim_number: str) -> list[Payment]:
        """Every payment issued on the claim, oldest first."""
        return [
            Payment(
                r["payment_id"],
                r["payee"],
                r["coverage"],
                Decimal(r["amount"]),
                date.fromisoformat(r["issued_on"]),
            )
            for r in self._rows("payments", claim_number, "issued_on, payment_id")
        ]

    def vendor_assignments(self, claim_number: str) -> list[Vendor]:
        """The vendors assigned to the claim, with the job ticket each one invoices against."""
        return [
            Vendor(
                r["vendor"],
                r["service"],
                r["contact"],
                r["job_ticket"],
                date.fromisoformat(r["assigned_on"]),
            )
            for r in self._rows("vendors", claim_number, "assigned_on, vendor")
        ]
