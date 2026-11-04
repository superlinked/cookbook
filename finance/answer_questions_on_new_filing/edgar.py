"""SEC EDGAR lookups: which 10-K filings a company has, and the passages of each.

This stands in for two real calls. The filing list is EDGAR's submissions API
(https://data.sec.gov/submissions/CIK##########.json: form, accession number, report date,
filing date and primary document per filing), seeded here from data/edgar_filings.csv. The
passages stand in for fetching a filing's primary document from the EDGAR archive and splitting
it into paragraphs, which fetch_data.py did once and saved to data/passages.jsonl.

Both seeds load into an in-memory sqlite3 database, so the lookups below are plain queries. A
filing comes in labelled by the company that filed it, with its fiscal year and period end, so
nothing here guesses which company or year a passage is about.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILINGS_PATH = ROOT / "data" / "edgar_filings.csv"
PASSAGES_PATH = ROOT / "data" / "passages.jsonl"
ANNUAL_REPORT = "10-K"


@dataclass(frozen=True)
class Filing:
    cik: int
    ticker: str
    company: str
    form: str
    fiscal_year: int
    period_end: str
    filed: str
    accession: str
    url: str

    @property
    def label(self) -> str:
        return f"{self.ticker} FY{self.fiscal_year}"


@dataclass(frozen=True)
class Passage:
    passage_id: str
    accession: str
    section: str
    text: str


class Edgar:
    """The filing index and the filings' passages, loaded once per run."""

    def __init__(self, filings_path: Path = FILINGS_PATH, passages_path: Path = PASSAGES_PATH):
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE filings (cik INTEGER, ticker TEXT, company TEXT, form TEXT, "
            "fiscal_year INTEGER, period_end TEXT, filed TEXT, accession TEXT PRIMARY KEY, "
            "url TEXT)"
        )
        self.db.execute(
            "CREATE TABLE passages (id TEXT PRIMARY KEY, accession TEXT, section TEXT, "
            "text TEXT, position INTEGER)"
        )
        with filings_path.open(encoding="utf-8", newline="") as handle:
            self.db.executemany(
                "INSERT INTO filings VALUES (:cik, :ticker, :company, :form, :fiscal_year, "
                ":period_end, :filed, :accession, :url)",
                list(csv.DictReader(handle)),
            )
        lines = passages_path.read_text(encoding="utf-8").splitlines()
        rows = [json.loads(line) | {"position": n} for n, line in enumerate(lines) if line.strip()]
        self.db.executemany(
            "INSERT INTO passages VALUES (:id, :accession, :section, :text, :position)", rows
        )
        orphans = self.db.execute(
            "SELECT id FROM passages WHERE accession NOT IN (SELECT accession FROM filings)"
        ).fetchall()
        if orphans:
            raise ValueError(f"Passages from no filing in {filings_path.name}: {orphans}")

    def _filings(self, where: str, *args: object) -> list[Filing]:
        cursor = self.db.execute(
            "SELECT cik, ticker, company, form, fiscal_year, period_end, filed, accession, url "
            f"FROM filings WHERE {where} ORDER BY fiscal_year, filed",
            args,
        )
        return [Filing(*row) for row in cursor.fetchall()]

    def find_10k(self, ticker: str, fiscal_year: int) -> Filing | None:
        """The company's 10-K for that fiscal year; None if it has none.

        Two 10-Ks for one year is not something to pick between quietly, so it raises.
        """
        found = self._filings(
            "ticker = ? AND fiscal_year = ? AND form = ?", ticker, fiscal_year, ANNUAL_REPORT
        )
        if len(found) > 1:
            accessions = ", ".join(filing.accession for filing in found)
            raise LookupError(f"{ticker} has {len(found)} 10-Ks for {fiscal_year}: {accessions}")
        return found[0] if found else None

    def latest_10k(self, ticker: str) -> Filing | None:
        """The company's newest 10-K: the filing the analyst is briefing on.

        Two 10-Ks for the newest year raise, as in `find_10k`.
        """
        found = self._filings("ticker = ? AND form = ?", ticker, ANNUAL_REPORT)
        return self.find_10k(ticker, found[-1].fiscal_year) if found else None

    def period_ends(self, ticker: str) -> dict[str, int]:
        """Every 10-K period end on record for the company, mapped to its fiscal year.

        Target's fiscal 2025 ends on January 31, 2026, so "as of January 31, 2026" in its filing
        is fiscal 2025, not 2026.
        """
        return {
            filing.period_end: filing.fiscal_year
            for filing in self._filings("ticker = ? AND form = ?", ticker, ANNUAL_REPORT)
        }

    def passages(self, filing: Filing) -> list[Passage]:
        """Every passage on file for the filing, in the order of data/passages.jsonl."""
        cursor = self.db.execute(
            "SELECT id, accession, section, text FROM passages WHERE accession = ? "
            "ORDER BY position",
            (filing.accession,),
        )
        return [Passage(*row) for row in cursor.fetchall()]
