"""The CLM (contract lifecycle management system), as the recipe looks NDA requests up in it.

This stands in for a call to your CLM's intake API (for example `GET /requests?status=in_review`
and `GET /requests/{id}` returning the intake form and the attached draft). Here it is backed by
a readable CSV seed, `data/clm_intake.csv`, loaded into an in-memory SQLite database. One row per
NDA request, as the requester filled in the intake form and legal triaged it:

- `request`: the CLM's request id.
- `received`: the date the request came in (UTC).
- `counterparty` and `our_entity`: the two parties' legal names.
- `requester` and `business_purpose`: who asked and why.
- `deal_type`: the kind of deal the NDA is for (supply, acquisition, joint development, ...).
- `paper`: `ours` or `theirs`, whose template the draft is on.
- `our_role`: `receive`, `disclose` or `both`, which way confidential information flows for us.
- `governing_law_preference`: the law the business asked for, used in the redline wording.
- `draft_file`: the counterparty's draft attached to the request, a .docx in `data/`.

A request is found by its exact id; an id the CLM doesn't hold comes back as None.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
SEED = "clm_intake.csv"
ROLES = ("receive", "disclose", "both")
PAPERS = ("ours", "theirs")
COLUMNS = (
    "request",
    "received",
    "counterparty",
    "our_entity",
    "requester",
    "business_purpose",
    "deal_type",
    "paper",
    "our_role",
    "governing_law_preference",
    "draft_file",
)


@dataclass(frozen=True)
class IntakeRecord:
    request: str
    received: date
    counterparty: str
    our_entity: str
    requester: str
    business_purpose: str
    deal_type: str
    paper: str  # ours or theirs
    our_role: str  # receive, disclose or both
    governing_law_preference: str  # "" when the requester left it blank
    draft_file: str


class CLM:
    """NDA requests from the CLM intake seed, with plain lookups."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self._db = sqlite3.connect(":memory:")
        self._db.execute(f"CREATE TABLE requests ({', '.join(COLUMNS)}, PRIMARY KEY (request))")
        with (data_dir / SEED).open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                values = tuple(row[column].strip() for column in COLUMNS)
                record = dict(zip(COLUMNS, values, strict=True))
                if record["our_role"] not in ROLES or record["paper"] not in PAPERS:
                    raise ValueError(f"{SEED}: request {record['request']} has a bad role/paper")
                placeholders = ", ".join("?" for _ in COLUMNS)
                self._db.execute(f"INSERT INTO requests VALUES ({placeholders})", values)

    def open_requests(self) -> list[str]:
        """Every NDA request waiting for legal review, in the order it came in."""
        query = "SELECT request FROM requests ORDER BY received, rowid"
        return [request for (request,) in self._db.execute(query)]

    def get_request(self, request: str) -> IntakeRecord | None:
        """The intake record for one request id, or None if the CLM holds no such request."""
        query = f"SELECT {', '.join(COLUMNS)} FROM requests WHERE request = ?"
        row = self._db.execute(query, (request,)).fetchone()
        if row is None:
            return None
        fields = dict(zip(COLUMNS, row, strict=True))
        fields["received"] = date.fromisoformat(fields["received"])
        return IntakeRecord(**fields)
