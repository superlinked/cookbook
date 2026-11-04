"""The calendar: the events Jordan entered, each with a title, start, end and place.

Stands in for a calendar app's event search (for example an events list call with a text query
and a time range). Here the events come from `data/calendar.csv`, loaded into an in-memory
SQLite database. The module is not called `calendar.py` because that name would hide Python's
own `calendar` module, which the HTTP libraries import.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SEED = Path(__file__).resolve().parent / "data" / "calendar.csv"


@dataclass(frozen=True)
class Event:
    event_id: str
    title: str
    start: datetime  # local time, as the calendar shows it
    end: datetime
    location: str

    def when(self) -> str:
        return self.start.strftime("%Y-%m-%d %H:%M")


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.casefold())


class Calendar:
    def __init__(self, seed: Path = SEED) -> None:
        """Load the seed; a missing seed is an empty calendar (your own files may have none)."""
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE events (event_id TEXT, title TEXT, start TEXT, end_at TEXT, "
            "location TEXT)"
        )
        if seed.exists():
            with seed.open(encoding="utf-8", newline="") as handle:
                rows = [
                    (r["event_id"], r["title"], r["start"], r["end"], r["location"])
                    for r in csv.DictReader(handle)
                ]
            self.db.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?)", rows)

    def find_events(self, query: str) -> list[Event]:
        """Every event whose title has all the words of `query` (whole words, any case).

        "eye exam" finds "Eye exam, Dr. Haldane"; it does not find "Eye drops pickup".
        """
        wanted = _words(query)
        rows = self.db.execute(
            "SELECT event_id, title, start, end_at, location FROM events ORDER BY start"
        ).fetchall()
        found = []
        for event_id, title, start, end, location in rows:
            if wanted and all(word in _words(title) for word in wanted):
                found.append(
                    Event(
                        event_id,
                        title,
                        datetime.fromisoformat(start),
                        datetime.fromisoformat(end),
                        location,
                    )
                )
        return found
