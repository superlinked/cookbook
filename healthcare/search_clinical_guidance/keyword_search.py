"""The intranet's keyword search: guidance passages ranked by the words they share with a query.

Stands in for the search box clinicians use today (SharePoint search, an Elasticsearch `match`
query, or the document system's own search). Here it is plain Okapi BM25 over each passage's
title and text, the keyword ranking most intranets start with. It knows nothing about status or
population. The recipe uses it only when a rerank call fails, and to show what the search box
would have put first.
"""

from __future__ import annotations

import math
import re
from collections import Counter


def tokens(text: str) -> list[str]:
    """Lowercase words with a plural "s" dropped ("readings" finds "reading")."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words]


class KeywordSearch:
    def __init__(self, texts: dict[str, str], k1: float, b: float) -> None:
        """`texts` maps a passage ID to what the engine indexes for it."""
        self.k1 = k1
        self.b = b
        self.docs = {pid: Counter(tokens(text)) for pid, text in texts.items()}
        self.lengths = {pid: sum(counts.values()) for pid, counts in self.docs.items()}
        self.avg_length = sum(self.lengths.values()) / max(len(self.lengths), 1)
        frequency: Counter[str] = Counter()
        for counts in self.docs.values():
            frequency.update(counts.keys())
        n = len(self.docs)
        self.idf = {
            term: math.log(1 + (n - df + 0.5) / (df + 0.5)) for term, df in frequency.items()
        }

    def score(self, query: str, pid: str) -> float:
        counts = self.docs[pid]
        norm = self.k1 * (1 - self.b + self.b * self.lengths[pid] / self.avg_length)
        total = 0.0
        for term in set(tokens(query)):
            tf = counts.get(term, 0)
            if tf:
                total += self.idf[term] * tf * (self.k1 + 1) / (tf + norm)
        return total

    def search(self, query: str) -> list[tuple[str, float]]:
        """Every passage sharing a word with the query, best first. Ties go by ID."""
        scored = [(pid, self.score(query, pid)) for pid in self.docs]
        return sorted(((p, s) for p, s in scored if s > 0), key=lambda hit: (-hit[1], hit[0]))
