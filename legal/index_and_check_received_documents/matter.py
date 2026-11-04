"""The firm's matter system, as the recipe looks things up in it.

This stands in for the matter-management or litigation-support tracker a receiving firm already
keeps: the production log, where a paralegal records each volume received from the other side
as its cover letter states it (producing party, Bates prefix, the range it claims, and who each
native file came from), and the counsel-of-record list for the matter, built from the pleadings,
the correspondence and the other side's privilege log. In a real firm this is a query against
that system's API or database. Here it is backed by three readable CSV seeds in `data/`,
loaded into an in-memory SQLite database:

- `matter_productions.csv`: one row per volume received, with the cover letter's fields.
- `matter_natives.csv`: one row per native file the cover letter lists, with its custodian.
- `matter_counsel.csv`: one row per lawyer on the matter, the party they act for, and the
  initials and email addresses they show up under (`;`-separated).

Matching is exact, ignoring case and surrounding spaces: volume `TFL-VOL001` is not
`TFL-VOL01`, and a native file is found only under the exact name the cover letter gives. The
claimed Bates range is read into numbers once, when the volume is looked up; a label that isn't
the volume's prefix and digits raises ValueError.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"


@dataclass(frozen=True)
class Volume:
    """One volume received, as its cover letter states it."""

    volume: str
    matter_id: str
    matter: str
    producing_party: str
    received_on: str
    bates_prefix: str
    bates_digits: int
    bates_begin: str
    bates_end: str
    bates_first: int  # the claimed range as numbers, read once from bates_begin and bates_end
    bates_last: int


@dataclass(frozen=True)
class Native:
    file: str
    custodian: str
    source: str


@dataclass(frozen=True)
class Lawyer:
    name: str
    role: str
    party: str
    aliases: tuple[str, ...]


class MatterSystem:
    """The production log and counsel list from the matter seeds, with plain lookups."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self._db = sqlite3.connect(":memory:")
        self._db.executescript(
            """
            CREATE TABLE productions (
                volume TEXT PRIMARY KEY, matter_id TEXT NOT NULL, matter TEXT NOT NULL,
                producing_party TEXT NOT NULL, received_on TEXT NOT NULL,
                bates_prefix TEXT NOT NULL, bates_digits INTEGER NOT NULL,
                bates_begin TEXT NOT NULL, bates_end TEXT NOT NULL);
            CREATE TABLE natives (
                volume TEXT NOT NULL, file TEXT NOT NULL, custodian TEXT NOT NULL,
                source TEXT NOT NULL, PRIMARY KEY (volume, file));
            CREATE TABLE counsel (
                matter_id TEXT NOT NULL, party TEXT NOT NULL, name TEXT NOT NULL,
                role TEXT NOT NULL, aliases TEXT NOT NULL);
            """
        )
        for row in _rows(data_dir / "matter_productions.csv"):
            self._db.execute(
                "INSERT INTO productions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _key(row["volume"]),
                    *(row[name].strip() for name in ("matter_id", "matter", "producing_party")),
                    row["received_on"].strip(),
                    row["bates_prefix"].strip(),
                    int(row["bates_digits"]),
                    row["bates_begin"].strip(),
                    row["bates_end"].strip(),
                ),
            )
        for row in _rows(data_dir / "matter_natives.csv"):
            self._db.execute(
                "INSERT INTO natives VALUES (?, ?, ?, ?)",
                (
                    _key(row["volume"]),
                    _key(row["file"]),
                    row["custodian"].strip(),
                    row["source"].strip(),
                ),
            )
        for row in _rows(data_dir / "matter_counsel.csv"):
            self._db.execute(
                "INSERT INTO counsel VALUES (?, ?, ?, ?, ?)",
                tuple(
                    row[name].strip() for name in ("matter_id", "party", "name", "role", "aliases")
                ),
            )

    def find_volume(self, volume: str) -> Volume | None:
        """The production log's row for a volume, or None if it was never logged."""
        row = self._db.execute(
            "SELECT * FROM productions WHERE volume = ?", (_key(volume),)
        ).fetchone()
        if row is None:
            return None
        name, matter_id, matter, party, received, prefix, digits, begin, end = row
        name = name.upper()
        return Volume(
            name,
            matter_id,
            matter,
            party,
            received,
            prefix,
            digits,
            begin,
            end,
            bates_first=bates_value(begin, prefix, digits, name),
            bates_last=bates_value(end, prefix, digits, name),
        )

    def find_native(self, volume: str, file: str) -> Native | None:
        """The cover letter's entry for a native file in a volume, or None if it isn't listed."""
        row = self._db.execute(
            "SELECT file, custodian, source FROM natives WHERE volume = ? AND file = ?",
            (_key(volume), _key(file)),
        ).fetchone()
        return Native(file, row[1], row[2]) if row else None

    def counsel_for(self, matter_id: str, party: str) -> list[Lawyer]:
        """Every lawyer on the matter who acts for `party`, in the seed's order."""
        rows = self._db.execute(
            "SELECT name, role, party, aliases FROM counsel "
            "WHERE matter_id = ? AND lower(party) = lower(?) ORDER BY rowid",
            (matter_id.strip(), party.strip()),
        )
        return [
            Lawyer(name, role, who, tuple(a.strip() for a in aliases.split(";") if a.strip()))
            for name, role, who, aliases in rows
        ]


def bates_value(stamp: str, prefix: str, digits: int, volume: str) -> int:
    """The number in a Bates label the cover letter gives, such as TFL0000001 or TFL-0000001.

    The label must be the volume's prefix and exactly `digits` digits, with an optional space or
    dash between them. Anything else raises ValueError: a range that can't be read is not
    checked against.
    """
    match = re.fullmatch(rf"{re.escape(prefix)}[\s-]?(\d{{{digits}}})", stamp.strip(), re.I)
    if match is None:
        raise ValueError(
            f"The production log gives {stamp!r} as a Bates label for volume {volume}, which is "
            f"not the prefix {prefix} and {digits} digits. Fix data/matter_productions.csv."
        )
    return int(match.group(1))


def _key(value: str) -> str:
    return value.strip().casefold()


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))
