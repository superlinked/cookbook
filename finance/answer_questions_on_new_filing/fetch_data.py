"""Fetch the 10-K passages in data/passages.jsonl from SEC EDGAR.

Four public 10-K filings: Apple FY2025 and FY2024, Target FY2025 and FY2024. Their accession
numbers and document links are in data/edgar_filings.csv. Each passage is one paragraph of the
filing's HTML, copied word for word (only whitespace collapsed and a leading bullet removed),
picked by a phrase that must occur in exactly one paragraph. A passage can take in the bullet
paragraphs right after it, joined with a space, where a sentence ends in a bulleted list.

SEC EDGAR filings are US government records in the public domain. EDGAR asks every client to
identify itself with a User-Agent that names you and a contact email: set EDGAR_USER_AGENT
(for example "Jane Analyst jane@example.com") in the environment or in .env.
data/questions.yaml and data/edgar_filings.csv are written by hand.

    EDGAR_USER_AGENT="Your Name you@example.com" uv run fetch_data.py
"""

from __future__ import annotations

import csv
import json
import os
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
FILINGS_PATH = ROOT / "data" / "edgar_filings.csv"
PASSAGES_PATH = ROOT / "data" / "passages.jsonl"

# (passage id, accession number, 10-K section, a phrase found in exactly one paragraph of that
# filing, how many paragraphs after it to join on)
AAPL_FY2025 = "0000320193-25-000079"
AAPL_FY2024 = "0000320193-24-000123"
TGT_FY2025 = "0000027419-26-000016"
TGT_FY2024 = "0000027419-25-000018"
MDA = "Item 7. MD&A"
BUSINESS = "Item 1. Business"
RISKS = "Item 1A. Risk Factors"
PASSAGES: list[tuple[str, str, str, str, int]] = [
    ("P01", AAPL_FY2025, BUSINESS, "166,000 full-time equivalent employees", 0),
    ("P02", AAPL_FY2025, BUSINESS, "compensation should be competitive and equitable", 0),
    ("P03", AAPL_FY2025, MDA, "which totaled $132.4 billion", 0),
    ("P04", AAPL_FY2025, MDA, "outstanding fixed-rate notes with varying maturities", 0),
    ("P05", AAPL_FY2025, MDA, "fixed lease payment obligations of $16.8 billion", 0),
    ("P06", AAPL_FY2025, MDA, "manufacturing purchase obligations of $56.2 billion", 0),
    ("P07", AAPL_FY2025, MDA, "other purchase obligations of $14.8 billion", 0),
    ("P08", AAPL_FY2025, MDA, "quarterly cash dividend was $0.26 per share", 0),
    ("P09", AAPL_FY2025, MDA, "raised its quarterly dividend from $0.25 to $0.26", 0),
    ("P10", AAPL_FY2025, MDA, "lower than the statutory federal income tax rate", 0),
    ("P11", AAPL_FY2025, MDA, "$10.7 billion year-over-year decrease", 0),
    ("P12", AAPL_FY2025, BUSINESS, "Wearables includes smartwatches", 0),
    ("P13", AAPL_FY2025, MDA, "Wearables, Home and Accessories net sales decreased", 0),
    ("P14", AAPL_FY2025, MDA, "Services net sales increased during 2025", 0),
    ("P15", AAPL_FY2025, MDA, "Greater China net sales decreased", 0),
    ("P32", AAPL_FY2025, RISKS, "typically for periods up to 150 days", 0),
    ("P16", AAPL_FY2024, BUSINESS, "164,000 full-time equivalent employees", 0),
    ("P17", AAPL_FY2024, MDA, "quarterly cash dividend was $0.25 per share", 0),
    ("P31", AAPL_FY2024, MDA, "which totaled $140.8 billion", 0),
    ("P33", AAPL_FY2024, MDA, "manufacturing purchase obligations of $53.0 billion", 0),
    ("P34", AAPL_FY2024, MDA, "other purchase obligations of $12.0 billion", 0),
    ("P35", AAPL_FY2024, MDA, "outstanding fixed-rate notes with varying maturities", 0),
    ("P36", AAPL_FY2024, MDA, "fixed lease payment obligations of $15.6 billion", 0),
    ("P37", AAPL_FY2024, MDA, "higher than the statutory federal income tax rate", 0),
    ("P38", AAPL_FY2024, MDA, "effective tax rate for 2024 was higher compared to 2023", 0),
    ("P39", AAPL_FY2024, BUSINESS, "Wearables includes smartwatches", 0),
    ("P40", AAPL_FY2024, MDA, "Wearables, Home and Accessories net sales decreased", 0),
    ("P41", AAPL_FY2024, MDA, "Services net sales increased during 2024", 0),
    ("P42", AAPL_FY2024, MDA, "Greater China net sales decreased", 0),
    ("P43", AAPL_FY2024, RISKS, "typically for periods up to 150 days", 0),
    ("P18", TGT_FY2025, MDA, "Net Sales were $104.8 billion", 0),
    ("P19", TGT_FY2025, MDA, "Comparable sales decreased 2.6 percent", 0),
    ("P20", TGT_FY2025, BUSINESS, "A larger share of annual net sales", 0),
    ("P21", TGT_FY2025, BUSINESS, "we employed approximately 415,000", 0),
    ("P22", TGT_FY2025, MDA, "We expect capital expenditures in 2026", 0),
    ("P23", TGT_FY2025, MDA, "Capital expenditures in 2025 reflect", 0),
    ("P24", TGT_FY2025, MDA, "nearly 2,000-store network", 0),
    ("P25", TGT_FY2025, MDA, "We paid dividends totaling $2.1 billion", 0),
    ("P26", TGT_FY2025, MDA, "We enter into contractual obligations", 0),
    ("P27", TGT_FY2025, BUSINESS, "Our global sourcing operations operate", 0),
    ("P28", TGT_FY2025, MDA, "Our 2025 effective income tax rate was 22.3 percent", 0),
    ("P29", TGT_FY2025, MDA, "Income taxes: We pay income taxes", 0),
    ("P30", TGT_FY2025, MDA, "Year-end inventory was $12.3 billion", 0),
    ("P44", TGT_FY2025, MDA, "Inventory and cost of sales: The vast majority", 0),
    ("P45", TGT_FY2025, MDA, "Cash flows provided by operating activities were $6.6 billion", 0),
    ("P46", TGT_FY2024, BUSINESS, "we employed approximately 440,000", 0),
    ("P47", TGT_FY2024, MDA, "We paid dividends totaling $2.0 billion", 0),
    ("P48", TGT_FY2024, MDA, "Capital expenditures in 2024 reflect", 0),
    ("P49", TGT_FY2024, MDA, "We expect capital expenditures in 2025", 0),
    ("P50", TGT_FY2024, MDA, "We enter into contractual obligations", 0),
    ("P51", TGT_FY2024, BUSINESS, "Our global sourcing operations, which operate", 0),
    ("P52", TGT_FY2024, MDA, "Our 2024 effective income tax rate was 22.2 percent", 0),
    ("P53", TGT_FY2024, MDA, "Income taxes: We pay income taxes", 0),
    ("P54", TGT_FY2024, MDA, "Year-end inventory was $12.7 billion in 2024", 2),
    ("P55", TGT_FY2024, MDA, "Inventory and cost of sales: The vast majority", 0),
    ("P56", TGT_FY2024, MDA, "Cash flows provided by operating activities were $7.4 billion", 0),
]


class _Paragraphs(HTMLParser):
    """Split a filing into paragraphs: one per div, p or table row, whitespace collapsed."""

    BLOCKS = ("div", "p", "tr")
    HIDDEN = ("ix:header", "script", "style")

    def __init__(self) -> None:
        super().__init__()
        self.paragraphs: list[str] = []
        self._current: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.BLOCKS or tag == "br":
            self._flush()
        if tag in self.HIDDEN:
            self._hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.BLOCKS:
            self._flush()
        if tag in self.HIDDEN:
            self._hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self._current.append(data)

    def _flush(self) -> None:
        text = " ".join("".join(self._current).split())
        if text:
            self.paragraphs.append(text)
        self._current = []


def user_agent() -> str:
    """EDGAR's required User-Agent: your name and a contact email, from EDGAR_USER_AGENT."""
    load_dotenv()
    agent = os.environ.get("EDGAR_USER_AGENT", "").strip()
    if "@" not in agent:
        raise SystemExit(
            "Set EDGAR_USER_AGENT to your name and contact email, for example "
            '"Jane Analyst jane@example.com". SEC EDGAR asks every client to identify itself.'
        )
    return agent


def paragraphs(url: str, agent: str) -> list[str]:
    request = urllib.request.Request(url, headers={"User-Agent": agent})
    with urllib.request.urlopen(request, timeout=60) as response:
        html = response.read().decode("utf-8")
    parser = _Paragraphs()
    parser.feed(html)
    parser._flush()
    return parser.paragraphs


def passage_text(texts: list[str], passage_id: str, phrase: str, joined: int) -> str:
    """The one paragraph holding the phrase, plus `joined` bullet paragraphs after it."""
    found = [index for index, text in enumerate(texts) if phrase in text]
    if len(found) != 1:
        raise SystemExit(f"{passage_id}: {phrase!r} is in {len(found)} paragraphs")
    parts = texts[found[0] : found[0] + 1 + joined]
    for extra in parts[1:]:
        if not extra.startswith("•"):
            raise SystemExit(f"{passage_id}: the paragraph after it is not a bullet: {extra!r}")
    first = parts[0].lstrip("•").strip()
    return " ".join([first, *("• " + extra.lstrip("•").strip() for extra in parts[1:])])


def main() -> None:
    agent = user_agent()
    with FILINGS_PATH.open(encoding="utf-8", newline="") as handle:
        urls = {row["accession"]: row["url"] for row in csv.DictReader(handle)}
    texts: dict[str, list[str]] = {}
    for accession in dict.fromkeys(entry[1] for entry in PASSAGES):
        texts[accession] = paragraphs(urls[accession], agent)
        time.sleep(0.5)  # stay well under EDGAR's request rate limit
    rows: list[str] = []
    for passage_id, accession, section, phrase, joined in PASSAGES:
        entry = {
            "id": passage_id,
            "accession": accession,
            "section": section,
            "text": passage_text(texts[accession], passage_id, phrase, joined),
        }
        rows.append(json.dumps(entry, ensure_ascii=False))
    PASSAGES_PATH.parent.mkdir(parents=True, exist_ok=True)
    PASSAGES_PATH.write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"wrote {len(rows)} passages to {PASSAGES_PATH}")


if __name__ == "__main__":
    main()
