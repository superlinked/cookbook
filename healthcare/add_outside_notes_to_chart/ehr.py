"""The practice's EHR, as the recipe looks things up in it.

Each lookup stands in for a read your EHR already offers, usually over its FHIR API:

- `patient(patient_id)`: a FHIR Patient read, plus the chart's allergy status (has the patient
  said "no known drug allergies", listed allergies, or has nobody asked yet).
- `outside_documents()`: a DocumentReference search for outside records filed in a chart and not
  yet abstracted (category "outside records"), with each note's text.
- `problems(patient_id)`: a Condition search (problem-list items, clinical status active).
- `medications(patient_id)`: a MedicationStatement search (what the patient said they take at
  registration or on the new-patient intake form).
- `allergies(patient_id)`: an AllergyIntolerance search, with the drug class the chart records.
- `vitals(patient_id)`: an Observation search (category vital-signs), newest first.

Here the chart is a set of readable CSV seeds in `data/` (`ehr_*.csv`) and the outside notes are
text files in `data/outside_notes/`, loaded into an in-memory SQLite database. Lookups are by the
chart's patient id; the outside notes were requested for a patient the practice had already
registered, so every document arrives filed in a known chart. Nothing here writes to the chart.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
# What the chart says about allergies before anyone reads the outside note.
ALLERGY_STATUSES = ("allergies_listed", "no_known_allergies", "not_recorded")

TABLES = {
    "patients": ("patient_id", "name", "dob", "registered_on", "allergy_status"),
    "problems": ("patient_id", "problem", "code", "noted_on"),
    "medications": ("patient_id", "drug", "dose", "route", "frequency", "source"),
    "allergies": ("patient_id", "substance", "reaction", "allergy_class"),
    "vitals": ("patient_id", "taken_on", "vital", "value"),
    "documents": (
        *("document_id", "patient_id", "received_on", "source_clinic"),
        *("visit_date", "category", "file"),
    ),
}


@dataclass(frozen=True)
class Patient:
    patient_id: str
    name: str
    dob: str  # ISO date
    allergy_status: str  # one of ALLERGY_STATUSES


@dataclass(frozen=True)
class OutsideDocument:
    document_id: str
    patient_id: str
    received_on: str
    source_clinic: str
    visit_date: str
    text: str


@dataclass(frozen=True)
class ChartProblem:
    problem: str
    code: str


@dataclass(frozen=True)
class ChartMedication:
    drug: str
    dose: str
    route: str
    frequency: str
    source: str


@dataclass(frozen=True)
class ChartAllergy:
    substance: str
    reaction: str
    allergy_class: str


@dataclass(frozen=True)
class ChartVital:
    taken_on: str
    vital: str
    value: str


class EHR:
    """The chart seeds, with plain read-only lookups."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self.notes_dir = data_dir / "outside_notes"
        self.db = sqlite3.connect(":memory:")
        for table, columns in TABLES.items():
            self.db.execute(f"CREATE TABLE {table} ({', '.join(f'{c} TEXT' for c in columns)})")
            rows = _rows(data_dir / f"ehr_{table}.csv", columns)
            marks = ", ".join("?" for _ in columns)
            self.db.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)
        self._validate()

    def _validate(self) -> None:
        for patient_id, status in self.db.execute(
            "SELECT patient_id, allergy_status FROM patients"
        ):
            if status not in ALLERGY_STATUSES:
                raise ValueError(f"{patient_id}: allergy_status must be one of {ALLERGY_STATUSES}")
            listed = bool(self.allergies(patient_id))
            if listed != (status == "allergies_listed"):
                raise ValueError(f"{patient_id}: allergy_status {status!r} but {listed=}")
        for document_id, patient_id, file in self.db.execute(
            "SELECT document_id, patient_id, file FROM documents"
        ):
            if self.patient(patient_id) is None:
                raise ValueError(f"{document_id}: no patient {patient_id!r} in the chart")
            if Path(file).name != file or not (self.notes_dir / file).is_file():
                raise ValueError(f"{document_id}: no note file {file!r} in {self.notes_dir}")

    def patient(self, patient_id: str) -> Patient | None:
        """The patient with exactly this chart id, or None."""
        row = self.db.execute(
            "SELECT patient_id, name, dob, allergy_status FROM patients WHERE patient_id = ?",
            (patient_id,),
        ).fetchone()
        return Patient(*row) if row else None

    def outside_documents(self) -> list[OutsideDocument]:
        """Every outside note filed in a chart and waiting to be abstracted, oldest first."""
        rows = self.db.execute(
            "SELECT document_id, patient_id, received_on, source_clinic, visit_date, file"
            " FROM documents WHERE category = 'outside records'"
            " ORDER BY received_on, document_id"
        ).fetchall()
        return [
            OutsideDocument(
                document_id=row[0],
                patient_id=row[1],
                received_on=row[2],
                source_clinic=row[3],
                visit_date=row[4],
                text=(self.notes_dir / row[5]).read_text(encoding="utf-8"),
            )
            for row in rows
        ]

    def problems(self, patient_id: str) -> list[ChartProblem]:
        """The active problem list."""
        rows = self.db.execute(
            "SELECT problem, code FROM problems WHERE patient_id = ? ORDER BY rowid", (patient_id,)
        )
        return [ChartProblem(*row) for row in rows]

    def medications(self, patient_id: str) -> list[ChartMedication]:
        """The medications the chart lists as taken now."""
        rows = self.db.execute(
            "SELECT drug, dose, route, frequency, source FROM medications"
            " WHERE patient_id = ? ORDER BY rowid",
            (patient_id,),
        )
        return [ChartMedication(*row) for row in rows]

    def allergies(self, patient_id: str) -> list[ChartAllergy]:
        """The allergies the chart lists, each with the drug class it records."""
        rows = self.db.execute(
            "SELECT substance, reaction, allergy_class FROM allergies"
            " WHERE patient_id = ? ORDER BY rowid",
            (patient_id,),
        )
        return [ChartAllergy(*row) for row in rows]

    def vitals(self, patient_id: str) -> list[ChartVital]:
        """Vital signs on the chart, newest first."""
        rows = self.db.execute(
            "SELECT taken_on, vital, value FROM vitals WHERE patient_id = ?"
            " ORDER BY taken_on DESC, rowid",
            (patient_id,),
        )
        return [ChartVital(*row) for row in rows]


def _rows(path: Path, columns: tuple[str, ...]) -> list[tuple[str, ...]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != columns:
            raise ValueError(f"{path.name}: columns must be {', '.join(columns)}")
        return [tuple(row[column].strip() for column in columns) for row in reader]
