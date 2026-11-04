"""The review platform: the discovery set's export and each document's extracted text.

Stands in for your review platform's export (a Relativity or Everlaw saved search of one
production volume, exported with its fields and extracted text). The seed in
data/review_platform.csv is loaded into an in-memory sqlite3 database, so the lookup is a plain
SQL query; the extracted text sits in data/text/<Bates number>.txt, as the export writes it. Only
what the review team already coded comes back: the Bates number, custodian, date, author,
recipients, document type, coding, confidentiality designation and privilege coding.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
SEED = DATA_DIR / "review_platform.csv"
TEXT_DIR = DATA_DIR / "text"
FIELDS = (
    "volume",
    "bates",
    "custodian",
    "date",
    "author",
    "recipients",
    "doc_type",
    "coding",
    "designation",
    "privilege_coding",
)
BATES = re.compile(r"[A-Z]{2,8}\d{6,10}")


@dataclass(frozen=True)
class Record:
    """One document's row in the export."""

    volume: str
    bates: str
    custodian: str
    date: str  # ISO 8601 in UTC, as the platform stores it
    author: str
    recipients: str  # "Name <email>; Name <email>", as the platform exports it
    doc_type: str
    coding: str
    designation: str  # CONFIDENTIAL, or empty
    privilege_coding: str


def production_set(volume: str, seed: Path = SEED) -> list[Record]:
    """Every document in the volume, in Bates order; an empty list if the volume has none. A
    Bates number that appears twice raises an error: the log would be ambiguous."""
    db = sqlite3.connect(":memory:")
    try:
        db.execute(f"CREATE TABLE export ({', '.join(f'{name} TEXT' for name in FIELDS)})")
        with seed.open(newline="", encoding="utf-8") as handle:
            rows = [tuple(row[name] or "" for name in FIELDS) for row in csv.DictReader(handle)]
        db.executemany(f"INSERT INTO export VALUES ({', '.join('?' for _ in FIELDS)})", rows)
        twice = db.execute(
            "SELECT bates FROM export WHERE volume = ? GROUP BY bates HAVING COUNT(*) > 1",
            (volume,),
        ).fetchall()
        if twice:
            raise ValueError(f"Bates numbers listed twice in the export: {twice}")
        found = db.execute(
            f"SELECT {', '.join(FIELDS)} FROM export WHERE volume = ? ORDER BY bates", (volume,)
        ).fetchall()
    finally:
        db.close()
    return [Record(*row) for row in found]


def extracted_text(bates: str, text_dir: Path = TEXT_DIR) -> str:
    """The document's extracted text. A Bates number in another shape, or one with no text
    file, raises an error, so a typo can't read a file outside the export."""
    if not BATES.fullmatch(bates):
        raise ValueError(f"{bates!r} is not a Bates number")
    path = text_dir / f"{bates}.txt"
    if not path.is_file():
        raise ValueError(f"no extracted text for {bates} in {text_dir}")
    return path.read_text(encoding="utf-8")
