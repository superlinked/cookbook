"""The policy administration system, as a few plain lookups.

Stands in for the policy system's API (in ClaimCenter, the policy retrieval that brings back a
snapshot of the policy as it stood on the date of loss): the term in force that day, its
Declarations (named insured, risk address, limits, deductible), the policy form and the
endorsements attached to that term, with the wording of each from the form library. The seed is
data/policy_terms.csv, one policy term per row, loaded into an in-memory SQLite database; the
wording is one Markdown file per form or endorsement in data/forms/.

A term runs from `effective_from` up to, not including, `effective_to`, so a loss on a renewal
date belongs to the new term.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

SEED = Path(__file__).resolve().parent / "data" / "policy_terms.csv"
FORMS = Path(__file__).resolve().parent / "data" / "forms"
COLUMNS = (
    "policy_number",
    "term",
    "named_insured",
    "risk_address",
    "effective_from",
    "effective_to",
    "form",
    "endorsements",
    "dwelling_limit",
    "personal_property_limit",
    "deductible",
)
# What a lookup can find: one term in force on the date, none, or several overlapping terms.
FOUND, NOT_FOUND, SEVERAL = "found", "not_found", "several"


@dataclass(frozen=True)
class Packet:
    """The policy as it stood on the date of loss: Declarations and the wording in force."""

    policy_number: str
    term: str
    named_insured: str
    risk_address: str
    effective_from: date
    effective_to: date
    form: str
    endorsements: tuple[str, ...]
    dwelling_limit: int
    personal_property_limit: int
    deductible: int
    documents: tuple[tuple[str, str], ...]  # (form or endorsement ID, its Markdown wording)


@dataclass(frozen=True)
class Lookup:
    status: str  # found | not_found | several
    packet: Packet | None
    note: str  # why nothing was found, or which terms overlap


class PolicyAdmin:
    def __init__(self, seed: Path = SEED, forms: Path = FORMS) -> None:
        self.forms = forms
        self.db = sqlite3.connect(":memory:")
        self.db.execute(f"CREATE TABLE term ({', '.join(COLUMNS)})")
        with seed.open(newline="", encoding="utf-8") as handle:
            rows = [[row[name].strip() for name in COLUMNS] for row in csv.DictReader(handle)]
        for row in rows:
            # `packet_on` compares dates as text, so both are stored as YYYY-MM-DD.
            start, end = date.fromisoformat(row[4]), date.fromisoformat(row[5])
            if start >= end:
                raise ValueError(f"Policy {row[0]} term {row[1]} ends before it starts")
            row[4], row[5] = start.isoformat(), end.isoformat()
        self.db.executemany(f"INSERT INTO term VALUES ({', '.join('?' * len(COLUMNS))})", rows)

    def wording(self, document_id: str) -> str:
        """The Markdown wording of one form or endorsement from the form library."""
        path = self.forms / f"{document_id}.md"
        if path.name != f"{document_id}.md" or not path.is_file():
            raise ValueError(f"The form library has no document {document_id!r}")
        return path.read_text(encoding="utf-8")

    def _packet(self, row: tuple[str, ...]) -> Packet:
        values = dict(zip(COLUMNS, row, strict=True))
        endorsements = tuple(values["endorsements"].split())
        return Packet(
            policy_number=values["policy_number"],
            term=values["term"],
            named_insured=values["named_insured"],
            risk_address=values["risk_address"],
            effective_from=date.fromisoformat(values["effective_from"]),
            effective_to=date.fromisoformat(values["effective_to"]),
            form=values["form"],
            endorsements=endorsements,
            dwelling_limit=int(values["dwelling_limit"]),
            personal_property_limit=int(values["personal_property_limit"]),
            deductible=int(values["deductible"]),
            documents=tuple(
                (document, self.wording(document)) for document in (values["form"], *endorsements)
            ),
        )

    def packet_on(self, policy_number: str, loss_date: date) -> Lookup:
        """The term of exactly this policy number in force on `loss_date`, with its wording.

        One term: found. No term that day, or no such policy number: not_found, with which of the
        two it was. Two or more terms overlapping that day: several, and none is picked, since
        choosing the right version of a policy is a person's call.
        """
        number = policy_number.strip()
        rows = self.db.execute(
            f"SELECT {', '.join(COLUMNS)} FROM term WHERE policy_number = ?"
            " ORDER BY effective_from",
            (number,),
        ).fetchall()
        if not rows:
            return Lookup(NOT_FOUND, None, f"The policy system has no policy {number}.")
        day = loss_date.isoformat()
        in_force = [row for row in rows if row[4] <= day < row[5]]
        if not in_force:
            terms = ", ".join(f"{row[4]} to {row[5]}" for row in rows)
            return Lookup(
                NOT_FOUND,
                None,
                f"Policy {number} has no term in force on {day} (its terms: {terms}).",
            )
        if len(in_force) > 1:
            terms = ", ".join(row[1] for row in in_force)
            return Lookup(
                SEVERAL,
                None,
                f"Policy {number} has {len(in_force)} terms in force on {day}: {terms}.",
            )
        return Lookup(FOUND, self._packet(in_force[0]), "")
