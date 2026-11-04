"""The matter's research folder in the document management system, as a few plain lookups.

Stands in for the DMS (iManage, NetDocuments and the like): a search of the matter's research
folder on its citation field, and fetching a saved document's text. The seed is
data/research_folder_index.csv, one row per saved authority (its citation as the associate filed
it, title, kind, jurisdiction, court, year and file name), loaded into an in-memory SQLite
database. The documents are the Markdown files under data/research_folder/: the full text the
associate saved from Westlaw or Lexis, split under `## ` headings into its parts (Facts,
Majority, Dissent, or a statute's Section and History).
"""

from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

COLUMNS = ("citation", "title", "kind", "jurisdiction", "court", "year", "file")


@dataclass(frozen=True)
class Authority:
    citation: str  # as filed, e.g. "214 Westm. 2d 377 (2019)"
    title: str
    kind: str  # case or statute
    jurisdiction: str
    court: str  # empty for a statute
    year: int
    file: str


@dataclass(frozen=True)
class Paragraph:
    number: int  # 1, 2, ... through the whole document
    part: str  # the heading's first word, lowercased: facts, majority, dissent, section, ...
    heading: str  # the heading as written, e.g. "Section (2)"
    text: str

    @property
    def pid(self) -> str:
        return f"P{self.number}"


def citation_key(citation: str) -> str:
    """The citation as the folder matches it: single spaces, no trailing parenthetical (year or
    "as amended 2022"). "214 Westm. 2d 377 (2019)" and "214  Westm. 2d 377" are the same key;
    "214 Westm. 2d 378" is not."""
    text = re.sub(r"\s+", " ", citation.replace(chr(0xA0), " ")).strip()
    return re.sub(r"\s*\([^()]*\)$", "", text).strip()


class ResearchFolder:
    def __init__(self, index: Path, folder: Path) -> None:
        self.folder = folder
        self.db = sqlite3.connect(":memory:")
        names = ", ".join(COLUMNS)
        self.db.execute(f"CREATE TABLE doc ({names}, key TEXT)")
        with index.open(newline="", encoding="utf-8") as handle:
            rows = [
                (*(row[name] for name in COLUMNS), citation_key(row["citation"]))
                for row in csv.DictReader(handle)
            ]
        if not rows:
            raise ValueError(f"{index} lists no documents, so there is nothing to check against")
        for row in rows:
            # A file name from the seed is opened under the folder; a path could leave it.
            if Path(row[6]).name != row[6] or row[6] in ("", ".", ".."):
                raise ValueError(f"{index}: file {row[6]!r} is not a plain file name")
        self.db.executemany(f"INSERT INTO doc VALUES ({', '.join('?' * 8)})", rows)

    def lookup(self, key: str) -> list[Authority]:
        """Every saved document filed under this citation key, oldest first. Two rows means two
        versions of one statute section were saved; none means the folder doesn't have it."""
        rows = self.db.execute(
            f"SELECT {', '.join(COLUMNS)} FROM doc WHERE key = ? ORDER BY year",
            (citation_key(key),),
        ).fetchall()
        return [
            Authority(citation, title, kind, juris, court, int(year), file)
            for citation, title, kind, juris, court, year, file in rows
        ]

    def open(self, authority: Authority) -> list[Paragraph]:
        """The document's paragraphs, each with the part it sits under. Lines before the first
        `## ` heading are the header (title, citation, court) and are not paragraphs."""
        text = (self.folder / authority.file).read_text(encoding="utf-8")
        paragraphs: list[Paragraph] = []
        heading = ""
        for block in re.split(r"\n\s*\n", text):
            block = block.strip()
            if block.startswith("## "):
                heading = block[3:].strip()
                continue
            if not heading or not block:
                continue
            part = heading.split()[0].lower()
            paragraphs.append(
                Paragraph(len(paragraphs) + 1, part, heading, " ".join(block.split()))
            )
        return paragraphs
