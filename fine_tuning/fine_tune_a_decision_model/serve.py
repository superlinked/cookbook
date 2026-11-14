"""Score the test rows through SIE: the zero-shot model and the model recipe.py fine-tuned.

    uv run serve.py                    # both datasets
    uv run serve.py --only private     # one dataset
    uv run serve.py --zero-shot-only   # SIE Serverless: it serves only catalog models

Run recipe.py first, then start SIE Local or SIE Cluster with the configs in sie_models/
and the saved weights mounted (see the README). Set SIE_BASE_URL in .env. Writes
runs/<timestamp>-served/results.json and prints a table to compare with recipe.py's.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from sie_sdk import SIEClient
from sie_sdk.types import Item

HERE = Path(__file__).parent
BATCH = 16


def make_client(wait_s: float) -> SIEClient:
    """Build an SIE client from the API key and SIE_BASE_URL in the environment or `.env`."""
    load_dotenv()
    api_key = os.environ.get("SIE_API_KEY", "").strip()
    base_url = os.environ.get("SIE_BASE_URL", "").strip()
    if not api_key and not base_url:
        raise RuntimeError("Set SIE_API_KEY, or SIE_BASE_URL for SIE Cluster or SIE Local, in .env")
    base_url = base_url or "https://api.superlinked.com"
    return SIEClient(api_key=api_key or None, base_url=base_url, timeout_s=wait_s)


def load_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def top_label(result: dict[str, Any]) -> dict[str, Any]:
    """The highest-scoring label in one extract result."""
    found = result.get("classifications") or []
    if not found:
        return {"label": None, "score": 0.0}
    best = max(found, key=lambda c: c["score"])
    return {"label": best["label"], "score": round(float(best["score"]), 4)}


def accuracy(picks: list[dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    correct = sum(p["label"] == r["true_labels"][0] for p, r in zip(picks, rows, strict=True))
    return {"accuracy": round(correct / len(rows), 3), "correct": correct, "rows": len(rows)}


def decide(
    client: SIEClient, model: str, rows: list[dict[str, Any]], labels: list[str], prompt: str
) -> dict[str, Any]:
    """One SIE extract call per batch of rows; the top label is the decision."""
    picks: list[dict[str, Any]] = []
    start = time.perf_counter()
    for i in range(0, len(rows), BATCH):
        batch = rows[i : i + BATCH]
        results = client.extract(
            model, [Item(text=r["text"]) for r in batch], labels=labels, instruction=prompt
        )
        picks += [top_label(dict(r)) for r in results]
    seconds = time.perf_counter() - start
    return {
        **accuracy(picks, rows),
        "seconds": round(seconds, 1),
        "picks": [
            {"text": r["text"][:160], "expected": r["true_labels"][0], **p}
            for p, r in zip(picks, rows, strict=True)
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--only", choices=["public", "private"])
    parser.add_argument("--zero-shot-only", action="store_true")
    args = parser.parse_args()
    rules = yaml.safe_load((HERE / "rules.yaml").read_text())
    client = make_client(rules["wait_s"])
    run_dir = HERE / "runs" / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-served")
    run_dir.mkdir(parents=True)
    results: dict[str, Any] = {"model": rules["model"]}
    for key, spec in rules["datasets"].items():
        if args.only and key != args.only:
            continue
        labels = list(spec["labels"])
        test = load_rows(HERE / f"data/{key}/test.jsonl")
        print(f"\n== {spec['title']}: {len(test)} test rows")
        entry: dict[str, Any] = {"title": spec["title"]}
        models = {"zero_shot": rules["model"]}
        if not args.zero_shot_only:
            models["fine_tuned"] = spec["served_model"]
        for kind, model in models.items():
            entry[kind] = {"model": model, **decide(client, model, test, labels, spec["prompt"])}
            r = entry[kind]
            print(
                f"   {kind:<10}  {r['accuracy']:.1%}  ({r['correct']}/{r['rows']})  "
                f"{model}, {r['seconds']} s"
            )
        results[key] = entry
        (run_dir / "results.json").write_text(
            json.dumps(results, indent=2, ensure_ascii=False) + "\n"
        )
    print(f"\nwrote {run_dir / 'results.json'}")


if __name__ == "__main__":
    main()
