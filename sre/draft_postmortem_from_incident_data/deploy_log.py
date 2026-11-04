"""The deploy pipeline's history: deploys, rollbacks and certificate renewals per service.

Stands in for the deploy pipeline's API (e.g. a list of deployments for a service between two
times). Here the history comes from `data/deploys.csv`, loaded into an in-memory SQLite
database.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SEED = Path(__file__).resolve().parent / "data" / "deploys.csv"
COLUMNS = ("at", "kind", "service", "version", "actor", "note")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True)
class Deploy:
    at: datetime
    kind: str  # deploy | rollback | cert_renewal
    service: str
    version: str  # empty for a certificate renewal
    actor: str
    note: str

    def describe(self) -> str:
        what = " ".join(part for part in (self.kind, self.service, self.version) if part)
        return f"{what} by {self.actor}" + (f": {self.note}" if self.note else "")


class DeployLog:
    def __init__(self, seed: Path = SEED) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE deploys (at TEXT, kind TEXT, service TEXT, version TEXT, actor TEXT, "
            "note TEXT)"
        )
        with seed.open(encoding="utf-8", newline="") as handle:
            rows = [
                (parse_time(r["at"]).isoformat(), *(r[k] for k in COLUMNS[1:]))
                for r in csv.DictReader(handle)
            ]
        self.db.executemany("INSERT INTO deploys VALUES (?, ?, ?, ?, ?, ?)", rows)

    def deploys(
        self, services: list[str] | tuple[str, ...], since: datetime, until: datetime
    ) -> list[Deploy]:
        """Every deploy, rollback and renewal for these services between `since` and `until`."""
        marks = ", ".join("?" for _ in services)
        rows = self.db.execute(
            "SELECT at, kind, service, version, actor, note FROM deploys "
            f"WHERE service IN ({marks}) AND at >= ? AND at <= ? ORDER BY at",
            (*services, since.astimezone(UTC).isoformat(), until.astimezone(UTC).isoformat()),
        ).fetchall()
        return [Deploy(parse_time(at), *rest) for at, *rest in rows]
