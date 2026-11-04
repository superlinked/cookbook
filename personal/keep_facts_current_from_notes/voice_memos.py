"""The voice memo app: each recording's title, and its transcript where the app made one.

Stands in for the phone's voice memo library (the recording list with its titles, and the
transcript some phones keep for a recording). Here the records come from
`data/voice_memos.csv`, loaded into an in-memory SQLite database. A recording with an empty
transcript has none, and the recipe transcribes it with SIE.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

SEED = Path(__file__).resolve().parent / "data" / "voice_memos.csv"


@dataclass(frozen=True)
class Recording:
    file: str
    title: str
    transcript: str  # empty when the app has no transcript


class VoiceMemos:
    def __init__(self, seed: Path = SEED) -> None:
        """Load the seed; a missing seed means no titles and no transcripts."""
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE memos (file TEXT PRIMARY KEY, title TEXT, transcript TEXT)")
        if seed.exists():
            with seed.open(encoding="utf-8", newline="") as handle:
                rows = [(r["file"], r["title"], r["transcript"]) for r in csv.DictReader(handle)]
            self.db.executemany("INSERT INTO memos VALUES (?, ?, ?)", rows)

    def find_recording(self, file_name: str) -> Recording | None:
        """The app's record for this audio file, matched on the exact file name, or None."""
        row = self.db.execute(
            "SELECT file, title, transcript FROM memos WHERE file = ?", (file_name,)
        ).fetchone()
        return Recording(row[0], row[1], row[2].strip()) if row else None
