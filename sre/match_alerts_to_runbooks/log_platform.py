"""The log platform, as two plain queries: a service's lines in a time range, and the
configuration and flag changes in a time range.

Stands in for a query to your log platform (a Loki LogQL range query such as
`{env="production", service=~"payment|checkout"}`, or an Elasticsearch search with the same
filters). The seed is data/logs.csv, lines already split into time, environment, service, level
and message, loaded into an in-memory SQLite database so the lookups read like the queries you'd
send the real one.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

COLUMNS = ("time", "env", "service", "level", "message")


@dataclass(frozen=True)
class LogLine:
    time: str  # UTC, 2026-10-14T09:01:44Z
    env: str
    service: str
    level: str
    message: str

    def text(self) -> str:
        return f"{self.time} {self.service} [{self.level}] {self.message}"


def stamp(moment: datetime) -> str:
    """A time in the seed's format, so the range filters compare like for like."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class LogPlatform:
    def __init__(self, seed: Path) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute(f"CREATE TABLE line ({', '.join(COLUMNS)})")
        with seed.open(newline="", encoding="utf-8") as handle:
            rows = [tuple(row[name] for name in COLUMNS) for row in csv.DictReader(handle)]
        # Stored in one format, UTC with a Z, so the range filters compare like for like whatever
        # ISO form the seed used (an offset, fractional seconds). An unreadable time fails here.
        rows = [(stamp(datetime.fromisoformat(row[0])), *row[1:]) for row in rows]
        self.db.executemany("INSERT INTO line VALUES (?, ?, ?, ?, ?)", rows)

    def _query(
        self, env: str, services: list[str], start: datetime, end: datetime
    ) -> list[LogLine]:
        marks = ", ".join("?" * len(services))
        rows = self.db.execute(
            f"SELECT {', '.join(COLUMNS)} FROM line WHERE env = ? AND service IN ({marks}) "
            "AND time >= ? AND time <= ? ORDER BY time, service",
            (env, *services, stamp(start), stamp(end)),
        )
        return [LogLine(*row) for row in rows.fetchall()]

    def search(
        self, env: str, services: list[str], start: datetime, end: datetime
    ) -> list[LogLine]:
        """Lines from these services in this environment, from start to end inclusive."""
        return self._query(env, services, start, end)

    def changes(
        self, env: str, sources: list[str], start: datetime, end: datetime
    ) -> list[LogLine]:
        """Configuration, flag and deploy events in this environment from start to end. The
        sources are the services that write them (flagd, the deploy tool)."""
        return self._query(env, sources, start, end)
