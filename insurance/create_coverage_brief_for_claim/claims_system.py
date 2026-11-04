"""The claims system, as a few plain lookups.

Stands in for the claims system's API (in Guidewire ClaimCenter, a claim search by claim
number): the claim as intake recorded it, with the policy number it was opened against, the date
of loss, the date it was reported, the cause-of-loss code picked at first notice of loss and the
insured's own description of what happened. The seed is data/claims.csv, one claim per row,
loaded into an in-memory SQLite database so the lookups read like the queries you'd send the
real one.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

SEED = Path(__file__).resolve().parent / "data" / "claims.csv"
COLUMNS = (
    "claim_number",
    "policy_number",
    "date_of_loss",
    "reported_on",
    "cause_of_loss",
    "insured",
    "description",
)


@dataclass(frozen=True)
class Claim:
    claim_number: str
    policy_number: str
    date_of_loss: date
    reported_on: date
    cause_of_loss: str  # the coarse code picked at intake: WATER, WIND, FIRE, THEFT, OTHER
    insured: str
    description: str  # the insured's own words, as intake took them down


class ClaimsSystem:
    def __init__(self, seed: Path = SEED) -> None:
        self.db = sqlite3.connect(":memory:")
        names = ", ".join(COLUMNS)
        self.db.execute(f"CREATE TABLE claim ({names}, UNIQUE (claim_number))")
        with seed.open(newline="", encoding="utf-8") as handle:
            rows = [tuple(row[name].strip() for name in COLUMNS) for row in csv.DictReader(handle)]
        self.db.executemany(f"INSERT INTO claim VALUES ({', '.join('?' * len(COLUMNS))})", rows)

    @staticmethod
    def _claim(row: tuple[str, ...]) -> Claim:
        number, policy, loss, reported, cause, insured, description = row
        return Claim(
            number,
            policy,
            date.fromisoformat(loss),
            date.fromisoformat(reported),
            cause,
            insured,
            description,
        )

    def claim(self, claim_number: str) -> Claim | None:
        """The claim with exactly this number, or None when the claims system has no such claim.
        A number one digit off finds nothing; it is never matched to a neighbour."""
        found = self.db.execute(
            f"SELECT {', '.join(COLUMNS)} FROM claim WHERE claim_number = ?",
            (claim_number.strip(),),
        ).fetchone()
        return self._claim(found) if found else None

    def queue(self) -> list[Claim]:
        """Every claim waiting for a coverage review, in claim-number order."""
        rows = self.db.execute(f"SELECT {', '.join(COLUMNS)} FROM claim ORDER BY claim_number")
        return [self._claim(row) for row in rows.fetchall()]
