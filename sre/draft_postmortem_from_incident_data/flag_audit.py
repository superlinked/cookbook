"""The feature-flag system's audit log: flag flips and runtime config changes per service.

Stands in for the flag system's audit-log API (e.g. a list of changes to a project's flags
between two times). Here the log comes from `data/flag_audit.csv`, loaded into an in-memory
SQLite database.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SEED = Path(__file__).resolve().parent / "data" / "flag_audit.csv"
COLUMNS = ("at", "kind", "service", "name", "change", "actor", "note")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True)
class FlagChange:
    at: datetime
    kind: str  # flag | config
    service: str
    name: str  # the flag or config key
    change: str  # e.g. "on, rollout 100%" or "16 -> 2"
    actor: str
    note: str

    def describe(self) -> str:
        what = f"{self.kind} {self.service} {self.name} {self.change} by {self.actor}"
        return what + (f": {self.note}" if self.note else "")


class FlagAudit:
    def __init__(self, seed: Path = SEED) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE changes (at TEXT, kind TEXT, service TEXT, name TEXT, change TEXT, "
            "actor TEXT, note TEXT)"
        )
        with seed.open(encoding="utf-8", newline="") as handle:
            rows = [
                (parse_time(r["at"]).isoformat(), *(r[k] for k in COLUMNS[1:]))
                for r in csv.DictReader(handle)
            ]
        self.db.executemany("INSERT INTO changes VALUES (?, ?, ?, ?, ?, ?, ?)", rows)

    def changes(
        self, services: list[str] | tuple[str, ...], since: datetime, until: datetime
    ) -> list[FlagChange]:
        """Every flag flip and config change for these services between `since` and `until`."""
        marks = ", ".join("?" for _ in services)
        rows = self.db.execute(
            "SELECT at, kind, service, name, change, actor, note FROM changes "
            f"WHERE service IN ({marks}) AND at >= ? AND at <= ? ORDER BY at",
            (*services, since.astimezone(UTC).isoformat(), until.astimezone(UTC).isoformat()),
        ).fetchall()
        return [FlagChange(parse_time(at), *rest) for at, *rest in rows]
