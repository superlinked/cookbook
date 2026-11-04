"""The provider directory: who is this referring clinician, and how do we reach their office?

Stands in for your practice's referring-provider directory (the EHR's referring-provider
master file, kept in step with the NPPES NPI Registry), looked up by NPI the way an NPI
Registry search (`GET /api/?number=...`) would be. Here the providers come from
`data/provider_directory.csv`, one row per provider, loaded into an in-memory SQLite database.

Matching is honest, and only an exact match lets the recipe use the directory's phone and fax:

- `found`: the NPI is in the directory and the name on the fax is that provider's name
  (credentials such as MD or FNP dropped, the same words in any order; a surname alone, like
  "Dr. Okafor", counts). With no NPI on the fax, exactly one provider with this name.
- `possible_match`: the NPI belongs to a provider with another name, or the name is in the
  directory under another NPI. Someone checks it before the directory's numbers are used.
- `several`: with no NPI, more than one provider has this name.
- `none`: neither the NPI nor the name is in the directory: a new referring provider.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
# Titles and credentials dropped before two provider names are compared.
CREDENTIALS = ("dr", "md", "do", "np", "fnp", "pa", "pa-c", "aprn", "rn", "mbbs", "phd")


@dataclass(frozen=True)
class Provider:
    npi: str
    name: str
    practice: str
    phone: str
    fax: str


@dataclass(frozen=True)
class ProviderMatch:
    status: str  # found | possible_match | several | none
    providers: tuple[Provider, ...] = ()
    why: str = ""

    @property
    def provider(self) -> Provider | None:
        """The provider, only when the match is exact."""
        return self.providers[0] if self.status == "found" else None


class ProviderDirectory:
    """The provider seed, with one plain lookup."""

    def __init__(self, data_dir: Path = DATA_DIR, credentials: tuple[str, ...] = CREDENTIALS):
        self.credentials = credentials
        self._db = sqlite3.connect(":memory:")
        self._db.execute(
            "CREATE TABLE providers (npi TEXT PRIMARY KEY, name TEXT, practice TEXT, phone TEXT,"
            " fax TEXT)"
        )
        with (data_dir / "provider_directory.csv").open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                self._db.execute(
                    "INSERT INTO providers VALUES (?, ?, ?, ?, ?)",
                    tuple(row[key].strip() for key in ("npi", "name", "practice", "phone", "fax")),
                )

    def _providers(self, where: str = "1", args: tuple[str, ...] = ()) -> list[Provider]:
        rows = self._db.execute(
            f"SELECT npi, name, practice, phone, fax FROM providers WHERE {where} ORDER BY npi",
            args,
        ).fetchall()
        return [Provider(*map(str, row)) for row in rows]

    def same_name(self, a: str, b: str) -> bool:
        """Two provider names are one when the words of the shorter are all in the longer
        ("Okafor" and "Maren Okafor, MD"), credentials dropped."""
        words_a, words_b = self.words(a), self.words(b)
        if not words_a or not words_b:
            return False
        return words_a <= words_b or words_b <= words_a

    def words(self, name: str) -> set[str]:
        found = re.findall(r"[^\W\d_]+(?:-[^\W\d_]+)*", name.casefold())
        return {word for word in found if word not in self.credentials}

    def find_provider(self, npi: str, name: str) -> ProviderMatch:
        """The referring provider: found, possible_match, several or none."""
        npi = re.sub(r"\D", "", npi)
        by_name = [p for p in self._providers() if name.strip() and self.same_name(name, p.name)]
        if npi:
            by_npi = self._providers("npi = ?", (npi,))
            if by_npi:
                provider = by_npi[0]
                if not name.strip() or self.same_name(name, provider.name):
                    return ProviderMatch("found", (provider,))
                why = f"NPI {npi} is {provider.name} in the directory, the fax names {name}"
                return ProviderMatch("possible_match", (provider,), why)
            if by_name:
                listed = ", ".join(f"{p.name} (NPI {p.npi})" for p in by_name)
                why = f"NPI {npi} is not in the directory, but {listed} is"
                return ProviderMatch("possible_match", tuple(by_name), why)
            return ProviderMatch("none")
        if len(by_name) == 1:
            return ProviderMatch("found", tuple(by_name))
        if by_name:
            listed = ", ".join(f"{p.name} (NPI {p.npi})" for p in by_name)
            return ProviderMatch("several", tuple(by_name), f"several providers fit: {listed}")
        return ProviderMatch("none")
