"""The store's keyword search engine: pages of keyword hits for a query.

Stands in for the engine your search service already queries (an Elasticsearch, OpenSearch or
Solr `match` query with `from` and `size`, or your platform's search API). Here it is plain Okapi
BM25 over each product's title, brand and color from the PIM, the keyword ranking most
storefronts start with. It knows nothing about stock or the shopper's filters.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from pim import Product


@dataclass(frozen=True)
class Hit:
    rank: int  # 1-based position in the engine's full result list
    product_id: str
    score: float


def tokens(text: str) -> list[str]:
    """Lowercase words with a plural "s" dropped ("boots" finds "boot"), as most analyzers do."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words]


def index_text(product: Product) -> str:
    """What the engine searches: the fields a storefront search box usually indexes."""
    return f"{product.title} {product.brand} {product.color}"


class KeywordSearch:
    def __init__(self, products: list[Product], k1: float, b: float) -> None:
        self.k1 = k1
        self.b = b
        self.docs = {p.product_id: Counter(tokens(index_text(p))) for p in products}
        self.lengths = {pid: sum(counts.values()) for pid, counts in self.docs.items()}
        self.avg_length = sum(self.lengths.values()) / max(len(self.lengths), 1)
        frequency: Counter[str] = Counter()
        for counts in self.docs.values():
            frequency.update(counts.keys())
        n = len(self.docs)
        self.idf = {
            term: math.log(1 + (n - df + 0.5) / (df + 0.5)) for term, df in frequency.items()
        }

    def score(self, query: str, product_id: str) -> float:
        counts = self.docs[product_id]
        norm = self.k1 * (1 - self.b + self.b * self.lengths[product_id] / self.avg_length)
        total = 0.0
        for term in set(tokens(query)):
            tf = counts.get(term, 0)
            if tf:
                total += self.idf[term] * tf * (self.k1 + 1) / (tf + norm)
        return total

    def search(self, query: str, offset: int, limit: int) -> list[Hit]:
        """One page of hits: `limit` hits starting after the first `offset`. Ties go by product
        ID, so the order is stable. An empty page means the hits ran out."""
        scored = [(pid, self.score(query, pid)) for pid in self.docs]
        hits = sorted(((pid, s) for pid, s in scored if s > 0), key=lambda hit: (-hit[1], hit[0]))
        page = hits[offset : offset + limit]
        return [Hit(offset + i + 1, pid, s) for i, (pid, s) in enumerate(page)]
