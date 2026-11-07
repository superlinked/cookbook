"""A thin in-memory vector index, using vicinity's exact flat backend.

Vicinity compares a query with every vector, so results are exact, which suits a few hundred manual
sections. Only `search_policies` in recipe.py uses it, so a vector database can replace this file
without touching the agent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
from vicinity import Backend, Metric, Vicinity


class VectorIndex:
    """One vector per item, in the same order. Vicinity holds the vectors and each item's id."""

    def __init__(self, store: Vicinity[str], items: list[dict[str, str]]) -> None:
        self.store = store
        self.items = items
        self.by_id = {item["section_id"]: item for item in items}

    @classmethod
    def build(cls, vectors: Any, items: list[dict[str, str]]) -> VectorIndex:
        matrix = np.asarray(vectors, dtype=np.float32)
        ids = [item["section_id"] for item in items]
        store = Vicinity.from_vectors_and_items(
            matrix, ids, backend_type=Backend.BASIC, metric=Metric.COSINE
        )
        return cls(store, items)

    def nearest(self, query: Any, k: int) -> list[dict[str, str]]:
        """The k items most similar to the query, best first."""
        vector = np.asarray(query, dtype=np.float32)
        return [self.by_id[item_id] for item_id, _ in self.store.query(vector, k=k)[0]]

    def save(self, folder: Path, fingerprint: str) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self.store.save(folder / "vectors", overwrite=True)
        record = {"fingerprint": fingerprint, "items": self.items}
        (folder / "items.json").write_text(json.dumps(record, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, folder: Path, fingerprint: str) -> VectorIndex | None:
        """The saved index, or None when it's missing or was built from another manual or model."""
        try:
            record = json.loads((folder / "items.json").read_text(encoding="utf-8"))
            if record["fingerprint"] != fingerprint:
                return None
            return cls(Vicinity.load(folder / "vectors"), record["items"])
        except (OSError, ValueError, KeyError):
            return None
