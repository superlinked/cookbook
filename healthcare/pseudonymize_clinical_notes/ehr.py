"""The EHR: patients, their known identifiers and their notes, as lookups.

Each function stands in for a call the honest broker's tooling makes to the EHR's reporting
database or the clinical data warehouse: a patient fetch by ID (a FHIR Patient read), a patient
search by MRN (a FHIR Patient search on the MRN identifier) and the notes for a cohort by note type
and date (a FHIR DocumentReference search, or the note table in the warehouse). The seeds in
data/ehr_patients.csv, data/ehr_identifiers.csv and data/ehr_notes.csv are loaded into an
in-memory SQLite database, so the recipe reads the EHR the way it would read the live system and
never changes it.

Matching is exact on purpose: an MRN is compared by its digits ("0048-2913" is 00482913), a
patient ID as written. A lookup returns what it finds and nothing else; it never guesses.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
PATIENT_COLUMNS = ("patient_id", "name", "mrn", "dob", "street", "city", "state", "zip", "phone")
IDENTIFIER_COLUMNS = ("patient_id", "kind", "value")
NOTE_COLUMNS = ("note_id", "patient_id", "note_type", "note_date", "file")


@dataclass(frozen=True)
class Patient:
    """A patient as the EHR knows them: the demographics and every other identifier on file."""

    patient_id: str
    name: str
    mrn: str
    dob: date
    street: str
    city: str
    state: str
    zip: str
    phone: str
    aliases: tuple[str, ...] = ()
    other: tuple[tuple[str, str], ...] = ()  # (kind, value), e.g. a health plan member ID


@dataclass(frozen=True)
class Note:
    """One note in the export: its EHR IDs, type and date, and the note text."""

    note_id: str
    patient_id: str
    note_type: str
    note_date: date
    text: str


def mrn_key(value: str) -> str:
    """An MRN by its digits only, so "0048-2913" and "00482913" compare equal."""
    return re.sub(r"\D", "", value)


def _load(db: sqlite3.Connection, table: str, columns: tuple[str, ...], seed: Path) -> None:
    db.execute(f"CREATE TABLE {table} ({', '.join(f'{name} TEXT' for name in columns)})")
    with seed.open(encoding="utf-8", newline="") as handle:
        rows = [tuple(row[name] for name in columns) for row in csv.DictReader(handle)]
    db.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' for _ in columns)})", rows)


class Ehr:
    """The EHR's patients, identifiers and notes, loaded from the CSV seeds into SQLite."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self.data_dir = data_dir
        self.db = sqlite3.connect(":memory:")
        _load(self.db, "patients", PATIENT_COLUMNS, data_dir / "ehr_patients.csv")
        _load(self.db, "identifiers", IDENTIFIER_COLUMNS, data_dir / "ehr_identifiers.csv")
        _load(self.db, "notes", NOTE_COLUMNS, data_dir / "ehr_notes.csv")

    def get_patient(self, patient_id: str) -> Patient | None:
        """One patient by EHR patient ID, with every identifier on file; None if there's none."""
        row = self.db.execute(
            f"SELECT {', '.join(PATIENT_COLUMNS)} FROM patients WHERE patient_id = ?",
            (patient_id,),
        ).fetchone()
        if row is None:
            return None
        values = dict(zip(PATIENT_COLUMNS, row, strict=True))
        extra = self.db.execute(
            "SELECT kind, value FROM identifiers WHERE patient_id = ? ORDER BY rowid",
            (patient_id,),
        ).fetchall()
        return Patient(
            **{name: values[name] for name in PATIENT_COLUMNS if name != "dob"},
            dob=date.fromisoformat(values["dob"]),
            aliases=tuple(value for kind, value in extra if kind == "alias"),
            other=tuple((kind, value) for kind, value in extra if kind != "alias"),
        )

    def find_by_mrn(self, mrn: str) -> list[Patient]:
        """Every patient whose MRN has these digits; an empty list if none has."""
        key = mrn_key(mrn)
        if not key:
            return []
        ids = [
            patient_id
            for patient_id, value in self.db.execute("SELECT patient_id, mrn FROM patients")
            if mrn_key(value) == key
        ]
        return [patient for pid in sorted(ids) if (patient := self.get_patient(pid))]

    def notes_for(
        self, patient_ids: list[str], note_types: list[str], start: date, end: date
    ) -> list[Note]:
        """The cohort's notes of the given types dated from `start` to `end` (inclusive), by
        patient in cohort order, then date, then note ID."""
        if not patient_ids or not note_types:
            return []
        query = (
            f"SELECT {', '.join(NOTE_COLUMNS)} FROM notes "
            f"WHERE patient_id IN ({', '.join('?' for _ in patient_ids)}) "
            f"AND note_type IN ({', '.join('?' for _ in note_types)}) "
            "AND note_date BETWEEN ? AND ? ORDER BY note_date, note_id"
        )
        args = [*patient_ids, *note_types, start.isoformat(), end.isoformat()]
        found = [dict(zip(NOTE_COLUMNS, row, strict=True)) for row in self.db.execute(query, args)]
        found.sort(key=lambda row: patient_ids.index(row["patient_id"]))
        return [
            Note(
                row["note_id"],
                row["patient_id"],
                row["note_type"],
                date.fromisoformat(row["note_date"]),
                self._note_text(row["file"]),
            )
            for row in found
        ]

    def _note_text(self, file: str) -> str:
        """A note's text from the seed's notes folder; a path that leads out of it is refused."""
        root = self.data_dir.resolve()
        path = (root / file).resolve()
        if root not in path.parents:
            raise ValueError(f"note file {file!r} is outside {root}")
        return path.read_text(encoding="utf-8")
