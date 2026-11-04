"""The claims system, as the recipe looks a saved claim up in it.

This stands in for a call to your real claims system, such as Guidewire ClaimCenter's Claim API
(`GET /claim/v1/claims/{claimId}`, or a claim search on the call's recording ID that your
telephony integration stores on the claim). Here it is backed by one readable CSV seed,
`data/claims.csv`, loaded into an in-memory SQLite database: one row per saved claim, with the
recording of the intake call, the claim number, the policy the agent found, the insured, the
callback phone, and the date of loss, location, cause and injury fields as the agent saved
them. A blank cell is a field the agent left blank.

Matching is exact on the recording's file name, ignoring case and surrounding spaces. One row is
`found`; no row is `not_found`; two or more rows are `several`, and the recipe sends the call to
a person instead of picking one.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
COLUMNS = (
    *("recording", "claim_number", "policy_number", "insured", "callback_phone"),
    *("date_of_loss", "loss_location", "cause_of_loss", "injuries"),
)
FOUND, NOT_FOUND, SEVERAL = "found", "not_found", "several"


@dataclass(frozen=True)
class SavedClaim:
    recording: str
    claim_number: str
    policy_number: str
    insured: str
    callback_phone: str
    date_of_loss: str  # YYYY-MM-DD, or "" when left blank
    loss_location: str
    cause_of_loss: str
    injuries: str  # "Yes", "No", or "" when left blank

    def value(self, field: str) -> str:
        """The saved value of one intake field, "" when the agent left it blank."""
        return str(getattr(self, field)).strip()


@dataclass(frozen=True)
class Lookup:
    match: str  # found | not_found | several
    claim: SavedClaim | None  # set only when match is found
    claim_numbers: tuple[str, ...]  # every claim the recording points at


class ClaimsSystem:
    """Saved claims from the seed, with one plain lookup."""

    def __init__(self, path: Path = DATA_DIR / "claims.csv") -> None:
        self._db = sqlite3.connect(":memory:")
        self._db.execute(f"CREATE TABLE claims ({', '.join(f'{c} TEXT' for c in COLUMNS)})")
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            missing = set(COLUMNS) - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"{path} has no column {sorted(missing)}")
            for row in reader:
                values = [(row[column] or "").strip() for column in COLUMNS]
                self._db.execute(
                    f"INSERT INTO claims VALUES ({', '.join('?' * len(COLUMNS))})", values
                )

    def find_claim(self, recording: str) -> Lookup:
        """The saved claim whose intake call is this recording."""
        rows = self._db.execute(
            f"SELECT {', '.join(COLUMNS)} FROM claims WHERE lower(recording) = ? ORDER BY rowid",
            (recording.strip().casefold(),),
        ).fetchall()
        numbers = tuple(row[1] for row in rows)
        if len(rows) == 1:
            return Lookup(FOUND, SavedClaim(*rows[0]), numbers)
        return Lookup(SEVERAL if rows else NOT_FOUND, None, numbers)
