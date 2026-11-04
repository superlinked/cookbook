"""The support desk: the tickets linked to an incident, with their requester fields.

Stands in for your support desk's API (for example a ticket search by linked incident in
Zendesk or Jira Service Management). The seed in data/support_desk.csv is loaded into an
in-memory sqlite3 database. Only the structured fields come back (requester name and email,
organization), never the ticket's conversation: the ticket isn't part of what goes to the
vendor, its fields only tell the recipe whose name and email to look for.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

SEED = Path(__file__).resolve().parent / "data" / "support_desk.csv"
FIELDS = ("ticket_id", "incident_id", "requester_name", "requester_email", "organization")


@dataclass(frozen=True)
class Ticket:
    ticket_id: str
    incident_id: str
    requester_name: str
    requester_email: str
    organization: str


def tickets_for_incident(incident_id: str, seed: Path = SEED) -> list[Ticket]:
    """Every ticket linked to the incident, by exact incident ID; an empty list if none is."""
    db = sqlite3.connect(":memory:")
    try:
        db.execute(f"CREATE TABLE tickets ({', '.join(f'{name} TEXT' for name in FIELDS)})")
        with seed.open(newline="", encoding="utf-8") as handle:
            rows = [tuple(row[name] for name in FIELDS) for row in csv.DictReader(handle)]
        db.executemany(f"INSERT INTO tickets VALUES ({', '.join('?' for _ in FIELDS)})", rows)
        found = db.execute(
            f"SELECT {', '.join(FIELDS)} FROM tickets WHERE incident_id = ? ORDER BY ticket_id",
            (incident_id,),
        ).fetchall()
    finally:
        db.close()
    return [Ticket(*row) for row in found]
