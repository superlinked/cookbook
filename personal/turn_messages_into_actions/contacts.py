"""The address book, as the recipe looks people up in it.

This stands in for a contacts search on your phone or mail account (a CardDAV query, or the
People API's searchContacts). Here it is backed by `data/contacts.csv`, one row per contact with
the other names they show up under, loaded into an in-memory SQLite database.

Matching is honest:

- `found`: the name is a contact's name or one of its aliases, ignoring case and spacing.
- `possible_match`: every word of the name is in exactly one contact's name ("Dana" for "Coach
  Dana Ramos"). The recipe does not treat it as that contact; it only notes it.
- `several`: the words fit more than one contact ("Ellery" fits Sam and Leo).
- `none`: no contact fits.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"


@dataclass(frozen=True)
class Contact:
    contact_id: str
    name: str
    relationship: str


@dataclass(frozen=True)
class ContactMatch:
    status: str  # found | possible_match | several | none
    contacts: tuple[Contact, ...] = ()

    @property
    def contact(self) -> Contact | None:
        """The contact, only when the match is exact."""
        return self.contacts[0] if self.status == "found" else None


class Contacts:
    """The contacts seed, with plain lookups."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self._db = sqlite3.connect(":memory:")
        self._db.executescript(
            """
            CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, name TEXT, relationship TEXT);
            CREATE TABLE names (key TEXT PRIMARY KEY, contact_id TEXT NOT NULL);
            """
        )
        for row in _rows(data_dir / "contacts.csv"):
            self._db.execute(
                "INSERT INTO contacts VALUES (?, ?, ?)",
                (row["contact_id"], row["name"].strip(), row["relationship"].strip()),
            )
            names = [row["name"], *row["aliases"].split(";")]
            for name in filter(None, (key(name) for name in names)):
                # A name two contacts share is an error in the seed, not something to guess at.
                self._db.execute("INSERT INTO names VALUES (?, ?)", (name, row["contact_id"]))

    def _contact(self, contact_id: str) -> Contact:
        row = self._db.execute(
            "SELECT contact_id, name, relationship FROM contacts WHERE contact_id = ?",
            (contact_id,),
        ).fetchone()
        return Contact(str(row[0]), str(row[1]), str(row[2]))

    def name_words(self) -> set[str]:
        """Every word of every contact's name and aliases ("leo", "ellery", "brightsmile")."""
        names = self._db.execute("SELECT key FROM names")
        return {word for (name,) in names for word in _words(str(name))}

    def mentioned(self, text: str) -> set[str]:
        """The ids of the contacts whose name or alias is in the text as whole words ("Leo's
        cleaning" names Leo)."""
        padded = f" {' '.join(_words(text))} "
        return {
            str(contact_id)
            for name, contact_id in self._db.execute("SELECT key, contact_id FROM names")
            if f" {' '.join(_words(str(name)))} " in padded
        }

    def find_contact(self, name: str) -> ContactMatch:
        """The contact a sender name belongs to: found, possible_match, several or none."""
        wanted = key(name)
        if not wanted:
            return ContactMatch("none")
        row = self._db.execute("SELECT contact_id FROM names WHERE key = ?", (wanted,)).fetchone()
        if row:
            return ContactMatch("found", (self._contact(str(row[0])),))
        words = set(wanted.split())
        fits = sorted(
            {
                str(contact_id)
                for name_key, contact_id in self._db.execute("SELECT key, contact_id FROM names")
                if words <= set(str(name_key).split())
            }
        )
        if not fits:
            return ContactMatch("none")
        status = "possible_match" if len(fits) == 1 else "several"
        return ContactMatch(status, tuple(self._contact(contact_id) for contact_id in fits))


def key(name: str) -> str:
    """A name compared without case, punctuation or extra spaces: 'Coach  Dana Ramos.'."""
    return " ".join(re.findall(r"[^\W_]+(?:['&][^\W_]+)*|&", name.casefold()))


def _words(text: str) -> list[str]:
    """Letters and digits only, the way the recipe compares event names: "Leo's" is "leo s"."""
    return re.findall(r"[a-z0-9]+", text.casefold())


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))
