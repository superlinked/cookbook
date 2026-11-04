"""The vulnerability scanner: which CVEs it found on which hosts, and whether each is still open.

Stands in for your scanner's findings API (a search of findings by CVE ID, the way a scanner or
vulnerability-management tool returns them: one row per host and CVE, with a status). Here the
findings come from the committed seed `data/scanner_findings.csv`, loaded into an in-memory
SQLite database, so the recipe reads them the way it would read the live system and never
changes them.

A CVE with no rows means the scanner has no result for it on any host. That is not the same as
"patched": the scanner may not check for it yet. The caller decides what that means.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

SEED = Path(__file__).resolve().parent / "data" / "scanner_findings.csv"
OPEN, PATCHED = "open", "patched"
STATUSES = (OPEN, PATCHED)


@dataclass(frozen=True)
class Finding:
    host: str
    cve: str
    status: str  # open | patched


def cve_key(cve: str) -> str:
    """A CVE ID in upper case without spaces, so "cve-2099-41077" finds CVE-2099-41077."""
    return re.sub(r"\s", "", cve).upper()


class Scanner:
    """The scanner's findings, loaded from a CSV seed into SQLite."""

    def __init__(self, seed: Path = SEED) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE findings (host TEXT, cve TEXT, status TEXT)")
        rows = []
        with seed.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if row["status"] not in STATUSES:
                    raise ValueError(f"status must be one of {STATUSES} in {seed.name}: {row}")
                rows.append((row["host"].strip(), cve_key(row["cve"]), row["status"]))
        self.db.executemany("INSERT INTO findings VALUES (?, ?, ?)", rows)

    def find_cve(self, cve: str) -> list[Finding]:
        """Every host the scanner has a result for this CVE on, open or patched; [] for none."""
        cursor = self.db.execute(
            "SELECT host, cve, status FROM findings WHERE cve = ? ORDER BY host", (cve_key(cve),)
        )
        return [Finding(host, cve_id, status) for host, cve_id, status in cursor]
