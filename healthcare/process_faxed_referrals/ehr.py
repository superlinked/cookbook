"""The EHR: does this patient already have a chart here?

Stands in for your EHR's patient search, as a FHIR Patient search would run it
(`GET [base]/Patient?family=...&given=...&birthdate=...`, plus the patient's Coverage for the
insurance plan and member ID). Here the charts come from `data/ehr_patients.csv`, one row per
chart, loaded into an in-memory SQLite database.

Matching is honest, and only an exact match lets the recipe use anything from the chart:

- `found`: exactly one chart has this name (the same words, in any order, ignoring case and
  punctuation) and this date of birth.
- `possible_match`: no exact match, and exactly one chart comes close: the same date of birth
  and a shared first or last name (a married surname, a nickname), or a name a typo away; the
  same name with a different date of birth; or the same member ID under another name or date
  of birth. Someone checks it before the chart is used.
- `several`: more than one chart fits at the same level. Someone picks, or registers a new
  patient.
- `none`: nothing fits; a new patient.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
# Two names this similar (0 to 1, difflib's ratio) count as close: a misread letter or two.
CLOSE_NAME_RATIO = 0.85


@dataclass(frozen=True)
class Chart:
    mrn: str
    given_name: str
    family_name: str
    birth_date: date
    phone: str
    insurance_plan: str
    member_id: str

    @property
    def name(self) -> str:
        return f"{self.given_name} {self.family_name}"


@dataclass(frozen=True)
class PatientMatch:
    status: str  # found | possible_match | several | none
    charts: tuple[Chart, ...] = ()
    why: str = ""  # what the close match rests on, for the reviewer
    same_name_elsewhere: tuple[Chart, ...] = ()  # other charts with this name, another DOB

    @property
    def chart(self) -> Chart | None:
        """The chart, only when the match is exact."""
        return self.charts[0] if self.status == "found" else None


class EHR:
    """The chart seed, with one plain patient search."""

    def __init__(self, data_dir: Path = DATA_DIR, close_name_ratio: float = CLOSE_NAME_RATIO):
        self.close_name_ratio = close_name_ratio
        self._db = sqlite3.connect(":memory:")
        self._db.execute(
            "CREATE TABLE charts (mrn TEXT PRIMARY KEY, given_name TEXT, family_name TEXT,"
            " birth_date TEXT, phone TEXT, insurance_plan TEXT, member_id TEXT, name_key TEXT)"
        )
        with (data_dir / "ehr_patients.csv").open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                date.fromisoformat(row["birth_date"])  # a bad date is an error in the seed
                self._db.execute(
                    "INSERT INTO charts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["mrn"],
                        row["given_name"].strip(),
                        row["family_name"].strip(),
                        row["birth_date"],
                        row["phone"].strip(),
                        row["insurance_plan"].strip(),
                        row["member_id"].strip(),
                        name_key(f"{row['given_name']} {row['family_name']}"),
                    ),
                )

    def _charts(self, where: str = "1", args: tuple[str, ...] = ()) -> list[Chart]:
        rows = self._db.execute(
            "SELECT mrn, given_name, family_name, birth_date, phone, insurance_plan, member_id"
            f" FROM charts WHERE {where} ORDER BY mrn",
            args,
        ).fetchall()
        return [Chart(r[0], r[1], r[2], date.fromisoformat(r[3]), r[4], r[5], r[6]) for r in rows]

    def find_patient(self, name: str, dob: date, member_id: str = "") -> PatientMatch:
        """The chart this patient belongs to: found, possible_match, several or none."""
        wanted = name_key(name)
        if not wanted:
            return PatientMatch("none")
        same_name = self._charts("name_key = ?", (wanted,))
        exact = [chart for chart in same_name if chart.birth_date == dob]
        others = tuple(chart for chart in same_name if chart.birth_date != dob)
        if len(exact) == 1:
            return PatientMatch("found", tuple(exact), same_name_elsewhere=others)
        if exact:
            why = f"{len(exact)} charts have this name and date of birth"
            return PatientMatch("several", tuple(exact), why)
        close: dict[str, tuple[Chart, str]] = {}
        for chart in self._charts("birth_date = ?", (dob.isoformat(),)):
            reason = self._close_name(wanted, name_key(chart.name))
            if reason:
                close[chart.mrn] = (chart, f"same date of birth, {reason}")
        for chart in others:
            close[chart.mrn] = (chart, f"same name, date of birth {chart.birth_date.isoformat()}")
        if member_id.strip():
            wanted_id = id_key(member_id)
            for chart in self._charts():
                if chart.member_id and id_key(chart.member_id) == wanted_id:
                    close.setdefault(chart.mrn, (chart, "same member ID, another name or DOB"))
        if not close:
            return PatientMatch("none")
        found = list(close.values())
        status = "possible_match" if len(found) == 1 else "several"
        why = "; ".join(f"{chart.mrn}: {reason}" for chart, reason in found)
        return PatientMatch(status, tuple(chart for chart, _ in found), why)

    def _close_name(self, a: str, b: str) -> str:
        """Why two different names may be one person, or "" when they don't look it."""
        shared = sorted(set(a.split()) & set(b.split()))
        if shared:
            return "shares the name " + " ".join(shared)
        if SequenceMatcher(None, a, b).ratio() >= self.close_name_ratio:
            return "a similar name"
        return ""


def name_key(name: str) -> str:
    """A name as its words, sorted, without case or punctuation: 'Lee,  Ann.' -> 'ann lee'.

    Two names are the same when they have the same words in any order. "Ann Lee" and "Joann
    Lee" are different names: words are compared whole, never as parts of a longer word.
    """
    return " ".join(sorted(re.findall(r"[^\W\d_]+(?:['-][^\W\d_]+)*", name.casefold())))


def id_key(value: str) -> str:
    """A member ID without case, spaces or dashes."""
    return re.sub(r"[^0-9a-z]", "", value.casefold())
