"""Rebuild data/texts.json: 70% NFCorpus queries, 30% passages (BeIR/nfcorpus, CC BY-SA 4.0)."""

from __future__ import annotations

import json
import random
import tempfile
import urllib.request
from pathlib import Path

import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
REVISION = "b5026a0e96e8a7ac4f95f482a596389289d46269"
BASE = f"https://huggingface.co/datasets/BeIR/nfcorpus/resolve/{REVISION}"
SIZE = 2000
SEED = 20261111


def read(path: str, columns: list[str]) -> list[dict[str, str]]:
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "part.parquet"
        urllib.request.urlretrieve(f"{BASE}/{path}", local)
        rows: list[dict[str, str]] = pq.read_table(local, columns=columns).to_pylist()
        return rows


def main() -> None:
    queries = [r["text"].strip() for r in read("queries/queries-00000-of-00001.parquet", ["text"])]
    corpus = read("corpus/corpus-00000-of-00001.parquet", ["title", "text"])
    passages = [f"{r['title']}\n{r['text']}".strip() for r in corpus]
    rng = random.Random(SEED)
    n_query = round(SIZE * 0.7)
    texts = [rng.choice(queries) for _ in range(n_query)]
    texts += [rng.choice(passages) for _ in range(SIZE - n_query)]
    rng.shuffle(texts)
    out = HERE / "data" / "texts.json"
    out.write_text(json.dumps(texts, indent=0) + "\n", encoding="utf-8")
    print(f"wrote {len(texts)} texts to {out}")


if __name__ == "__main__":
    main()
