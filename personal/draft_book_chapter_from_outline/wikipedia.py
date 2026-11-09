"""Search Wikipedia and read articles, through its public API. No key needed.

Every article keeps its revision id, so a citation points at the exact text the draft quoted, even
after the article changes.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

MAX_REPLY_BYTES = 5_000_000  # far more than the plain text of the longest articles


class SearchFailed(Exception):
    """Wikipedia didn't answer, or answered with something unexpected."""


@dataclass(frozen=True)
class Article:
    title: str
    revision: int
    text: str

    @property
    def url(self) -> str:
        """The revision the draft read, not whatever the article says today."""
        return f"https://en.wikipedia.org/w/index.php?oldid={self.revision}"


class Wikipedia:
    def __init__(self, api: str, user_agent: str, timeout_s: float):
        self.api, self.user_agent, self.timeout_s = api, user_agent, timeout_s
        self.requests = 0

    def get(self, params: dict[str, Any]) -> dict[str, Any]:
        query = urllib.parse.urlencode({**params, "format": "json", "formatversion": "2"})
        request = urllib.request.Request(
            f"{self.api}?{query}", headers={"User-Agent": self.user_agent}
        )
        self.requests += 1
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                body = response.read(MAX_REPLY_BYTES + 1)
            if len(body) > MAX_REPLY_BYTES:
                raise SearchFailed(f"Wikipedia's reply was over {MAX_REPLY_BYTES} bytes")
            reply = json.loads(body)
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise SearchFailed(f"Wikipedia didn't answer: {error}") from error
        if not isinstance(reply, dict) or "error" in reply:
            raise SearchFailed(f"Wikipedia refused the request: {str(reply)[:200]}")
        return reply

    def search(self, query: str, limit: int) -> list[str]:
        """The titles of the best-matching articles."""
        reply = self.get({"action": "query", "list": "search", "srsearch": query, "srlimit": limit})
        hits = reply.get("query", {}).get("search", [])
        return [hit["title"] for hit in hits if isinstance(hit, dict) and "title" in hit]

    def article(self, title: str) -> Article | None:
        """An article's plain text and revision id, or None if there's no such article."""
        reply = self.get(
            {
                "action": "query",
                "prop": "extracts|revisions",
                "explaintext": 1,
                "rvprop": "ids",
                "redirects": 1,
                "titles": title,
            }
        )
        pages = reply.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing") or not pages[0].get("extract"):
            return None
        page = pages[0]
        revisions = page.get("revisions") or [{}]
        revision = revisions[0].get("revid")
        if not isinstance(revision, int) or revision <= 0:
            return None  # a citation has to point at a fixed revision
        return Article(page["title"], revision, page["extract"])
