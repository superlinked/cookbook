"""Score the test rows through SIE: zero-shot, few-shot, and the model recipe.py fine-tuned.

    uv run serve.py                    # both datasets
    uv run serve.py --only private     # one dataset
    uv run serve.py --catalog-only     # SIE Serverless: zero-shot and few-shot, no fine-tuned model

Run recipe.py first, then start SIE Local or SIE Cluster with the configs in sie_models/
and the saved weights mounted (see the README). Set SIE_BASE_URL in .env. Writes
runs/<timestamp>-served/results.json in the same shape as recipe.py's, and prints one line
per model.
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

from recipe import few_shot_examples, load_rows, score, top_pick

HERE = Path(__file__).parent


def make_client(wait_s: float) -> SIEClient:
    """Build an SIE client from the API key and SIE_BASE_URL in the environment or `.env`."""
    load_dotenv()
    api_key = os.environ.get("SIE_API_KEY", "").strip()
    base_url = os.environ.get("SIE_BASE_URL", "").strip()
    if not api_key and not base_url:
        raise RuntimeError("Set SIE_API_KEY, or SIE_BASE_URL for SIE Cluster or SIE Local, in .env")
    base_url = base_url or "https://api.superlinked.com"
    return SIEClient(api_key=api_key or None, base_url=base_url, timeout_s=wait_s)


def decide(
    client: SIEClient,
    model: str,
    rows: list[dict[str, Any]],
    labels: list[str],
    prompt: str,
    cfg: dict[str, Any],
    examples: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One SIE extract call per batch of rows; the top label is the decision."""
    options: dict[str, Any] = {"classification_type": cfg["classification_type"]}
    if examples:
        options["examples"] = examples
    picks: list[dict[str, Any]] = []
    start = time.perf_counter()
    size = cfg["batch_size"]
    for i in range(0, len(rows), size):
        batch = rows[i : i + size]
        # Each result is an sie_sdk ExtractResult; GLiClass fills `classifications`
        # with {"label", "score"} for every label.
        results = client.extract(
            model,
            [Item(text=r["text"]) for r in batch],
            labels=labels,
            instruction=prompt,
            options=options,
        )
        picks += [
            top_pick(list(r.get("classifications") or []), f"{model}, row {i + n}")
            for n, r in enumerate(results, 1)
        ]
    seconds = time.perf_counter() - start
    return {"seconds": round(seconds, 1), **score(picks, rows, labels)}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--only", choices=["public", "private"])
    parser.add_argument("--catalog-only", action="store_true")
    args = parser.parse_args()
    rules = yaml.safe_load((HERE / "rules.yaml").read_text())
    client = make_client(rules["wait_s"])
    run_dir = HERE / "runs" / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-served")
    run_dir.mkdir(parents=True)
    results: dict[str, Any] = {"model": rules["model"], "score": rules["score"]}
    for key, spec in rules["datasets"].items():
        if args.only and key != args.only:
            continue
        labels = list(spec["labels"])
        train = load_rows(HERE / f"data/{key}/train.jsonl", labels)
        test = load_rows(HERE / f"data/{key}/test.jsonl", labels)
        print(f"\n== {spec['title']}: {len(test)} test rows")
        results[key] = {"title": spec["title"]}
        fs = rules["few_shot"]
        examples = few_shot_examples(train, labels, fs["per_label"])
        runs: dict[str, tuple[str, list[dict[str, Any]] | None]] = {
            "zero_shot": (rules["model"], None),
            "instruct_zero_shot": (fs["model"], None),
            "few_shot": (fs["model"], examples),
        }
        if not args.catalog_only:
            runs["fine_tuned"] = (spec["served_model"], None)
        for kind, (model, shots) in runs.items():
            r = decide(client, model, test, labels, spec["prompt"], rules["score"], shots)
            results[key][kind] = {"model": model, **r}
            print(
                f"   {kind:<18}  {r['accuracy']:.1%}  ({r['correct']}/{r['rows']})  "
                f"{model}, {r['seconds']} s"
            )
            (run_dir / "results.json").write_text(
                json.dumps(results, indent=2, ensure_ascii=False) + "\n"
            )
    print(f"\nwrote {run_dir / 'results.json'}")


if __name__ == "__main__":
    main()
