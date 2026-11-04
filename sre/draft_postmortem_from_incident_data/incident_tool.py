"""The incident tool: look up an incident's record.

Stands in for the incident tool's API (e.g. a GET on the incident in PagerDuty or incident.io),
which gives you the incident's ID, title, Slack channel and the services it affected. Here the
records come from `data/incidents.csv`, loaded into an in-memory SQLite database.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

SEED = Path(__file__).resolve().parent / "data" / "incidents.csv"


@dataclass(frozen=True)
class Incident:
    incident_id: str
    title: str
    channel: str
    services: tuple[str, ...]


class IncidentTool:
    def __init__(self, seed: Path = SEED) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE incidents (incident TEXT, title TEXT, channel TEXT, services TEXT)"
        )
        with seed.open(encoding="utf-8", newline="") as handle:
            rows = [
                (row["incident"], row["title"], row["channel"], row["services"])
                for row in csv.DictReader(handle)
            ]
        self.db.executemany("INSERT INTO incidents VALUES (?, ?, ?, ?)", rows)

    @staticmethod
    def _incident(row: tuple[str, str, str, str]) -> Incident:
        services = tuple(name for name in row[3].split(";") if name)
        return Incident(row[0], row[1], row[2], services)

    def find_incident(self, incident_id: str) -> Incident | None:
        """The incident with exactly this ID, or None."""
        row = self.db.execute(
            "SELECT incident, title, channel, services FROM incidents WHERE incident = ?",
            (incident_id,),
        ).fetchone()
        return None if row is None else self._incident(row)

    def list_incidents(self) -> list[Incident]:
        """Every incident, in ID order."""
        rows = self.db.execute(
            "SELECT incident, title, channel, services FROM incidents ORDER BY incident"
        ).fetchall()
        return [self._incident(row) for row in rows]
