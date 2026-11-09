"""Search PubMed and read abstracts, through NCBI's public E-utilities API. No key needed.

Each abstract keeps its PMID, journal and year, so a citation points at one paper.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

MAX_REPLY_BYTES = 5_000_000  # far more than a few abstracts
MIN_GAP_S = 0.4  # NCBI allows three requests a second without a key


class SearchFailed(Exception):
    """PubMed didn't answer, or answered with something unexpected."""


@dataclass(frozen=True)
class Paper:
    pmid: str
    title: str
    journal: str
    year: str
    paragraphs: tuple[tuple[str, str], ...]  # (label, text); labels like BACKGROUND, or ""

    @property
    def url(self) -> str:
        return f"https://pubmed.ncbi.nlm.nih.gov/{self.pmid}/"


class PubMed:
    def __init__(self, api: str, user_agent: str, timeout_s: float, tool: str, email: str = ""):
        self.api, self.user_agent, self.timeout_s = api.rstrip("/"), user_agent, timeout_s
        # NCBI asks every client to send a tool name, and an email it can write to if needed.
        self.identity = {"tool": tool, **({"email": email} if email else {})}
        self.requests = 0
        self.last = 0.0

    def get(self, endpoint: str, params: dict[str, Any]) -> bytes:
        wait = MIN_GAP_S - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        query = urllib.parse.urlencode({**params, **self.identity})
        request = urllib.request.Request(
            f"{self.api}/{endpoint}?{query}", headers={"User-Agent": self.user_agent}
        )
        self.requests += 1
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                body: bytes = response.read(MAX_REPLY_BYTES + 1)
        except (OSError, http.client.HTTPException) as error:
            raise SearchFailed(f"PubMed didn't answer: {error}") from error
        finally:
            self.last = time.monotonic()
        if len(body) > MAX_REPLY_BYTES:
            raise SearchFailed(f"PubMed's reply was over {MAX_REPLY_BYTES} bytes")
        return body

    def search(self, query: str, limit: int) -> list[str]:
        """The PMIDs of the most relevant papers."""
        body = self.get(
            "esearch.fcgi",
            {
                "db": "pubmed",
                "term": query,
                "retmax": limit,
                "retmode": "json",
                "sort": "relevance",
            },
        )
        try:
            ids = json.loads(body)["esearchresult"]["idlist"]
        except (ValueError, KeyError, TypeError) as error:
            raise SearchFailed(f"PubMed's search reply wasn't readable: {error}") from error
        return [str(pmid) for pmid in ids if str(pmid).isdigit()]

    def papers(self, pmids: list[str]) -> list[Paper]:
        """Title, journal, year and abstract for each PMID. Papers with no abstract are left out."""
        if not pmids:
            return []
        body = self.get("efetch.fcgi", {"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"})
        try:
            root = ET.fromstring(body)
        except ET.ParseError as error:
            raise SearchFailed(f"PubMed's abstracts weren't readable: {error}") from error
        papers = []
        for article in root.iter("PubmedArticle"):
            paragraphs = tuple(
                (part.get("Label", ""), " ".join("".join(part.itertext()).split()))
                for part in article.findall(".//Article/Abstract/AbstractText")  # not translations
            )
            pmid = article.findtext(".//PMID") or ""
            if not pmid or not any(text for _, text in paragraphs):
                continue
            title = article.find(".//ArticleTitle")
            year = article.findtext(".//PubDate/Year") or ""
            papers.append(
                Paper(
                    pmid=pmid,
                    title=" ".join("".join(title.itertext()).split()) if title is not None else "",
                    journal=article.findtext(".//Journal/ISOAbbreviation") or "",
                    year=year or (article.findtext(".//PubDate/MedlineDate") or "")[:4],
                    paragraphs=paragraphs,
                )
            )
        return papers
