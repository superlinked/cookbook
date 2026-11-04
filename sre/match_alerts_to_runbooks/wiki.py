"""The team wiki where the runbooks live, as a few plain lookups.

Stands in for the wiki's REST API: fetching a page by the URL an alert's `runbook_url`
annotation points at, and listing every page in the runbook space (in Confluence, a content
search on that space). The seed is data/wiki_runbooks.csv, one runbook per row with its
Symptoms, Check, Mitigate and Escalate sections, loaded into an in-memory SQLite database so the
lookups read like the queries you'd send the real one.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

COLUMNS = ("runbook_id", "url", "title", "owner_team", "symptoms", "check", "mitigate", "escalate")


@dataclass(frozen=True)
class Runbook:
    runbook_id: str
    url: str
    title: str
    owner_team: str
    text: str  # the page as Markdown, headings included


def page_key(url: str) -> str:
    """The URL as the wiki matches it: no surrounding spaces, no `#anchor` or `?query` (a link
    to a section of a page is a link to that page), no trailing slash. Nothing else is forgiven,
    so a link to a page that moved or a near-identical page name finds nothing."""
    return url.strip().split("#", 1)[0].split("?", 1)[0].rstrip("/")


class Wiki:
    def __init__(self, seed: Path) -> None:
        self.db = sqlite3.connect(":memory:")
        names = ", ".join(f'"{name}"' for name in COLUMNS)
        self.db.execute(f"CREATE TABLE page ({names}, key TEXT UNIQUE, UNIQUE (runbook_id))")
        with seed.open(newline="", encoding="utf-8") as handle:
            rows = [
                (*(row[name] for name in COLUMNS), page_key(row["url"]))
                for row in csv.DictReader(handle)
            ]
        if not rows:
            raise ValueError(f"{seed} has no runbooks, so there is nothing to match alerts to")
        self.db.executemany(f"INSERT INTO page VALUES ({', '.join('?' * 9)})", rows)

    def _runbook(self, row: tuple[str, ...]) -> Runbook:
        runbook_id, url, title, team, symptoms, check, mitigate, escalate = row
        text = (
            f"## {runbook_id} {title}\n\n**Symptoms.** {symptoms}\n\n**Check.** {check}\n\n"
            f"**Mitigate.** {mitigate}\n\n**Escalate.** {escalate}"
        )
        return Runbook(runbook_id, url, title, team, text)

    def find_page(self, url: str) -> Runbook | None:
        """The runbook at exactly this URL, or None when the wiki has no such page."""
        names = ", ".join(f'"{name}"' for name in COLUMNS)
        row = self.db.execute(f"SELECT {names} FROM page WHERE key = ?", (page_key(url),))
        found = row.fetchone()
        return self._runbook(found) if found else None

    def runbooks(self) -> list[Runbook]:
        """Every runbook page in the space, in ID order."""
        names = ", ".join(f'"{name}"' for name in COLUMNS)
        rows = self.db.execute(f"SELECT {names} FROM page ORDER BY runbook_id").fetchall()
        return [self._runbook(row) for row in rows]
