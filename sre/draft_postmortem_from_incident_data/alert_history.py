"""The alerting system's history: firing and resolved events for a set of services.

Stands in for a query on wherever your alert history lives (an Alertmanager webhook log, a
PagerDuty log-entries call, an incident tool's alert timeline). Each event is shaped like an
Alertmanager notification: a `firing` event carries the alert's `startsAt`, and a `resolved`
event carries both `startsAt` and `endsAt`. Here they come from `data/alert_events.csv`, loaded
into an in-memory SQLite database.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SEED = Path(__file__).resolve().parent / "data" / "alert_events.csv"
COLUMNS = ("status", "fingerprint", "alertname", "service", "severity", "starts_at", "ends_at")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True)
class AlertEvent:
    status: str  # firing | resolved
    fingerprint: str
    name: str
    service: str
    severity: str
    starts_at: datetime
    ends_at: datetime | None  # None on a firing event
    summary: str

    @property
    def at(self) -> datetime:
        """When the event happened: startsAt for a firing event, endsAt for a resolve."""
        return self.ends_at if self.ends_at is not None else self.starts_at


class AlertHistory:
    def __init__(self, seed: Path = SEED) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE events (status TEXT, fingerprint TEXT, alertname TEXT, service TEXT, "
            "severity TEXT, starts_at TEXT, ends_at TEXT, summary TEXT, at TEXT)"
        )
        rows = []
        with seed.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                firing = row["status"] == "firing"
                at = parse_time(row["starts_at"] if firing else row["ends_at"]).isoformat()
                rows.append((*(row[name] for name in COLUMNS), row["summary"], at))
        self.db.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

    def events(
        self, services: list[str] | tuple[str, ...], since: datetime, until: datetime
    ) -> list[AlertEvent]:
        """Firing and resolved events for these services that happened between `since` and
        `until`, oldest first. An alert that fired before `since` and resolved inside the range
        comes back as its resolve alone."""
        marks = ", ".join("?" for _ in services)
        rows = self.db.execute(
            f"SELECT {', '.join(COLUMNS)}, summary FROM events WHERE service IN ({marks}) "
            "AND at >= ? AND at <= ? ORDER BY at, fingerprint, status",
            (*services, since.astimezone(UTC).isoformat(), until.astimezone(UTC).isoformat()),
        ).fetchall()
        return [
            AlertEvent(
                status,
                fingerprint,
                name,
                service,
                severity,
                parse_time(starts),
                None if status == "firing" else parse_time(ends),
                summary,
            )
            for status, fingerprint, name, service, severity, starts, ends, summary in rows
        ]
