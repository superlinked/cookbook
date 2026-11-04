"""The log platform: search log events by service and time range.

Stands in for your log platform's search API (a Loki, Elasticsearch or Datadog query for one
service between two timestamps). The seed in data/log_platform.csv is loaded into an in-memory
sqlite3 database, so the lookup is a plain SQL query. Each timestamp is also stored as a UTC key
in one fixed format (microseconds, "Z"), so the time range compares instants, not text: as text
".5Z" sorts after ".502Z", and "+02:00" and "Z" don't sort together. Each event comes back as the
line the platform shows, with the parsed `key=value` fields (`user_id=`, `client_ip=`, `host=`,
`request_id=`) written out as the platform stores them.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SEED = Path(__file__).resolve().parent / "data" / "log_platform.csv"


@dataclass(frozen=True)
class LogEvent:
    timestamp: str  # ISO 8601 in UTC, as the platform stores it
    level: str
    service: str
    message: str

    @property
    def line(self) -> str:
        """The event as one exported log line."""
        return f"{self.timestamp} {self.level:<5} {self.service} {self.message}"


def utc_key(moment: datetime) -> str:
    """A timezone-aware instant as fixed-width UTC text, which sorts in time order."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{moment.isoformat()} has no timezone")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _connect(seed: Path) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE events (at TEXT, timestamp TEXT, level TEXT, service TEXT, message TEXT)"
    )
    with seed.open(newline="", encoding="utf-8") as handle:
        rows = [
            (
                utc_key(datetime.fromisoformat(row["timestamp"])),
                row["timestamp"],
                row["level"],
                row["service"],
                row["message"],
            )
            for row in csv.DictReader(handle)
        ]
    db.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?)", rows)
    return db


def search(service: str, start: datetime, end: datetime, seed: Path = SEED) -> list[LogEvent]:
    """Every event the service logged from `start` up to `end` (timezone-aware), oldest
    first."""
    db = _connect(seed)
    try:
        found = db.execute(
            "SELECT timestamp, level, service, message FROM events "
            "WHERE service = ? AND at >= ? AND at < ? ORDER BY at",
            (service, utc_key(start), utc_key(end)),
        ).fetchall()
    finally:
        db.close()
    return [LogEvent(*row) for row in found]


def export(events: list[LogEvent]) -> str:
    """The events as the text file the engineer would download from the platform."""
    return "".join(event.line + "\n" for event in events)
