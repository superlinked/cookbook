"""The calendar, as the recipe looks events up in it.

This stands in for a time-range query on your calendar (Google Calendar's events.list with
timeMin and timeMax, or a CalDAV calendar-query REPORT). Here it is backed by
`data/calendar.csv`, the events in the next weeks as the calendar app would export them: the
ones you entered and the invitations you accepted. They are loaded into an in-memory SQLite
database with their times in UTC, so a query compares instants, not clock readings.

The lookups only return events; deciding whether a message repeats, moves or clashes with one is
the recipe's job.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DATA_DIR = Path(__file__).resolve().parent / "data"


@dataclass(frozen=True)
class Event:
    event_id: str
    title: str
    start: datetime
    end: datetime
    location: str
    organizer: str  # who sent the invitation; empty for an event you entered yourself


class Calendar:
    """The calendar seed, with plain time-range lookups."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self._db = sqlite3.connect(":memory:")
        self._db.execute(
            "CREATE TABLE events (event_id TEXT PRIMARY KEY, title TEXT, start_utc TEXT, "
            "end_utc TEXT, start TEXT, end TEXT, location TEXT, organizer TEXT)"
        )
        with (data_dir / "calendar.csv").open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                start, end = _instant(row["start"]), _instant(row["end"])
                if end <= start:
                    raise ValueError(f"calendar event {row['event_id']} ends before it starts")
                self._db.execute(
                    "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["event_id"],
                        row["title"].strip(),
                        _utc(start),
                        _utc(end),
                        row["start"],
                        row["end"],
                        row["location"].strip(),
                        row["organizer"].strip(),
                    ),
                )

    def events_between(self, start: datetime, end: datetime) -> list[Event]:
        """Every event that overlaps [start, end), earliest first."""
        rows = self._db.execute(
            "SELECT event_id, title, start, end, location, organizer FROM events "
            "WHERE start_utc < ? AND end_utc > ? ORDER BY start_utc, event_id",
            (_utc(end), _utc(start)),
        ).fetchall()
        return [
            Event(str(r[0]), str(r[1]), _instant(r[2]), _instant(r[3]), str(r[4]), str(r[5]))
            for r in rows
        ]

    def events_on(self, day: date, zone: ZoneInfo) -> list[Event]:
        """Every event that overlaps the local day `day` in `zone`."""
        start = datetime.combine(day, time(0, 0), tzinfo=zone)
        return self.events_between(start, start + timedelta(days=1))


def _instant(value: str) -> datetime:
    moment = datetime.fromisoformat(value.strip())
    if moment.tzinfo is None:
        raise ValueError(f"calendar time without a UTC offset: {value}")
    return moment


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
