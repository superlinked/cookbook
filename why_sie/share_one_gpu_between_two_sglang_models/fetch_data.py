"""Rebuild data/passages.json from NFCorpus (BeIR/nfcorpus, CC BY-SA 4.0) at a fixed revision."""

from __future__ import annotations

import json
import tempfile
import urllib.request
from pathlib import Path

import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
REVISION = "b5026a0e96e8a7ac4f95f482a596389289d46269"
URL = (
    "https://huggingface.co/datasets/BeIR/nfcorpus/resolve/"
    f"{REVISION}/corpus/corpus-00000-of-00001.parquet"
)
COUNT = 500


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "corpus.parquet"
        urllib.request.urlretrieve(URL, path)
        table = pq.read_table(path, columns=["title", "text"]).slice(0, COUNT).to_pylist()
    passages = [f"{row['title']}\n{row['text']}".strip() for row in table]
    out = HERE / "data" / "passages.json"
    out.write_text(json.dumps(passages, indent=0) + "\n", encoding="utf-8")
    print(f"wrote {len(passages)} passages to {out}")


if __name__ == "__main__":
    main()
