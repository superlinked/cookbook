"""Fine-tune a GLiClass decision model on about 100 rows; compare it with zero-shot and few-shot.

    uv run recipe.py                 # both datasets: zero-shot, few-shot, fine-tuned
    uv run recipe.py --only private  # one dataset

Writes runs/<timestamp>/results.json, the fine-tuned weights under
runs/<timestamp>/<dataset>/model, and prints a table with one row per dataset. Training and
scoring run on this machine (CUDA, Apple MPS or CPU); serve.py scores the same rows through SIE.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import torch
import yaml
from gliclass import GLiClassModel, ZeroShotClassificationPipeline
from gliclass.data_processing import AugmentationConfig, DataCollatorWithPadding, GLiClassDataset
from gliclass.pipeline import format_examples_prompt
from gliclass.training import Trainer, TrainingArguments
from transformers import AutoTokenizer

HERE = Path(__file__).parent


def device() -> str:
    if torch.cuda.is_available():
        return "cuda:0"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def resolve(name: str, revision: str) -> tuple[str, dict[str, str]]:
    """A local copy under .models/ if there is one, else the Hub id pinned to `revision`."""
    local = HERE / ".models" / name
    if (local / "model.safetensors").exists():
        return str(local), {}
    return name, {"revision": revision}


def load_rows(path: Path, labels: list[str]) -> list[dict[str, Any]]:
    """Read a JSONL split and check each row against the labels in rules.yaml.

    Training reads each row's `all_labels` and scoring reads rules.yaml, so a label renamed
    in one place but not the other would quietly lower the accuracy. Stop instead.
    """
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for n, row in enumerate(rows, 1):
        if row["all_labels"] != labels:
            raise ValueError(f"{path}:{n}: all_labels differ from rules.yaml; rerun make_data.py")
        if len(row["true_labels"]) != 1 or row["true_labels"][0] not in labels:
            raise ValueError(f"{path}:{n}: needs exactly one true label from rules.yaml")
    return rows


def few_shot_examples(
    rows: list[dict[str, Any]], labels: list[str], per_label: int
) -> list[dict[str, Any]]:
    """The first `per_label` training rows of each label, as GLiClass few-shot examples."""
    examples = []
    for label in labels:
        mine = [r for r in rows if r["true_labels"][0] == label][:per_label]
        examples += [{"text": r["text"], "labels": [label]} for r in mine]
    return examples


def check_fits(
    tokenizer: Any,
    examples: list[dict[str, Any]],
    labels: list[str],
    rows: list[dict[str, Any]],
    window: int,
) -> None:
    """Stop if the examples leave too little room: the model would cut the row short silently."""
    used = len(tokenizer(format_examples_prompt(examples))["input_ids"])
    used += len(tokenizer(" ".join(labels))["input_ids"]) + 2 * len(labels)
    longest = max(len(tokenizer(r["text"])["input_ids"]) for r in rows)
    if used + longest > window:
        raise ValueError(
            f"few-shot examples ({used} tokens) and the longest row ({longest}) exceed the "
            f"{window}-token window; lower few_shot.per_label in rules.yaml"
        )


def top_pick(scores: list[dict[str, Any]], where: str) -> dict[str, Any]:
    """The highest-scoring label. No labels back means something is broken, not a miss."""
    if not scores:
        raise RuntimeError(f"{where}: the model returned no labels")
    best = max(scores, key=lambda s: float(s["score"]))
    return {"label": best["label"], "score": round(float(best["score"]), 4)}


def score(
    picks: list[dict[str, Any]], rows: list[dict[str, Any]], labels: list[str]
) -> dict[str, Any]:
    """Accuracy overall and per label, and every decision next to what was expected."""
    pairs = list(zip(picks, rows, strict=True))
    correct = sum(p["label"] == r["true_labels"][0] for p, r in pairs)
    per_label: dict[str, float | None] = {}
    for label in labels:
        mine = [p for p, r in pairs if r["true_labels"][0] == label]
        per_label[label] = (
            round(sum(p["label"] == label for p in mine) / len(mine), 3) if mine else None
        )
    return {
        "accuracy": round(correct / len(rows), 3),
        "correct": correct,
        "rows": len(rows),
        "per_label": per_label,
        "picks": [
            {"text": r["text"][:160], "expected": r["true_labels"][0], **p} for p, r in pairs
        ],
    }


def decide(
    model: Any,
    tokenizer: Any,
    rows: list[dict[str, Any]],
    labels: list[str],
    prompt: str,
    cfg: dict[str, Any],
    examples: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    pipe = ZeroShotClassificationPipeline(
        model,
        tokenizer,
        classification_type=cfg["classification_type"],
        device=device(),
        progress_bar=False,
    )
    start = time.perf_counter()
    outputs = pipe(
        [r["text"] for r in rows],
        labels,
        prompt=prompt,
        threshold=cfg["threshold"],
        batch_size=cfg["batch_size"],
        examples=examples,
    )
    seconds = time.perf_counter() - start
    picks = [top_pick(out, f"row {n}") for n, out in enumerate(outputs, 1)]
    return {"ms_per_row": round(1000 * seconds / len(rows), 1), **score(picks, rows, labels)}


def fine_tune(
    name: str,
    hub: dict[str, str],
    train_rows: list[dict[str, Any]],
    out_dir: Path,
    cfg: dict[str, Any],
    max_length: int,
) -> tuple[Any, Any]:
    random.seed(cfg["seed"])
    torch.manual_seed(cfg["seed"])
    model = GLiClassModel.from_pretrained(name, **hub)
    model.config.problem_type = "multi_label_classification"
    tokenizer = AutoTokenizer.from_pretrained(name, **hub)
    dataset = GLiClassDataset(
        train_rows,
        tokenizer,
        AugmentationConfig(enabled=False),
        max_length=max_length,
        problem_type="multi_label_classification",
        architecture_type=model.config.architecture_type,
        prompt_first=model.config.prompt_first,
    )
    args = TrainingArguments(
        output_dir=str(out_dir / "checkpoints"),
        learning_rate=cfg["learning_rate"],
        others_lr=cfg["learning_rate"],
        per_device_train_batch_size=cfg["batch_size"],
        num_train_epochs=cfg["epochs"],
        save_strategy="no",
        logging_steps=10,
        report_to="none",
        seed=cfg["seed"],
        use_cpu=device() == "cpu",
    )
    trainer = Trainer(
        model=model,
        args=args,
        train_dataset=dataset,
        processing_class=tokenizer,
        data_collator=DataCollatorWithPadding(device=device()),
    )
    trainer.train()
    model.save_pretrained(out_dir / "model")
    tokenizer.save_pretrained(out_dir / "model")
    return model, tokenizer


def run_dataset(
    key: str, spec: dict[str, Any], rules: dict[str, Any], run_dir: Path
) -> dict[str, Any]:
    labels = list(spec["labels"])
    train = load_rows(HERE / f"data/{key}/train.jsonl", labels)
    test = load_rows(HERE / f"data/{key}/test.jsonl", labels)
    name, hub = resolve(rules["model"], rules["model_revision"])
    cfg = rules["score"]
    print(
        f"\n== {spec['title']}: {len(train)} training rows, "
        f"{len(test)} test rows, {len(labels)} labels"
    )
    base = GLiClassModel.from_pretrained(name, **hub).to(device())
    tok = AutoTokenizer.from_pretrained(name, **hub)
    zero = decide(base, tok, test, labels, spec["prompt"], cfg)
    print(f"   zero-shot   {zero['accuracy']:.1%}  ({zero['correct']}/{zero['rows']})")
    fs = rules["few_shot"]
    fs_name, fs_hub = resolve(fs["model"], fs["model_revision"])
    fs_model = GLiClassModel.from_pretrained(fs_name, **fs_hub).to(device())
    fs_tok = AutoTokenizer.from_pretrained(fs_name, **fs_hub)
    examples = few_shot_examples(train, labels, fs["per_label"])
    check_fits(fs_tok, examples, labels, test, cfg["window"])
    instruct = decide(fs_model, fs_tok, test, labels, spec["prompt"], cfg)
    few = decide(fs_model, fs_tok, test, labels, spec["prompt"], cfg, examples)
    print(
        f"   instruct    {instruct['accuracy']:.1%}  ({instruct['correct']}/{instruct['rows']}), "
        f"{fs['model']} zero-shot"
    )
    print(
        f"   few-shot    {few['accuracy']:.1%}  ({few['correct']}/{few['rows']}), "
        f"the same model with {len(examples)} examples in the request"
    )
    start = time.perf_counter()
    tuned_model, tuned_tok = fine_tune(
        name, hub, train, run_dir / key, rules["train"], cfg["max_length"]
    )
    train_s = round(time.perf_counter() - start, 1)
    tuned = decide(tuned_model.to(device()), tuned_tok, test, labels, spec["prompt"], cfg)
    print(
        f"   fine-tuned  {tuned['accuracy']:.1%}  ({tuned['correct']}/{tuned['rows']}), "
        f"trained in {train_s} s"
    )
    return {
        "title": spec["title"],
        "train_rows": len(train),
        "test_rows": len(test),
        "zero_shot": zero,
        "instruct_zero_shot": {"model": fs["model"], **instruct},
        "few_shot": {"model": fs["model"], "examples": len(examples), **few},
        "fine_tuned": tuned,
        "train_seconds": train_s,
        "device": device(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--only", choices=["public", "private"])
    args = parser.parse_args()
    rules = yaml.safe_load((HERE / "rules.yaml").read_text())
    run_dir = HERE / "runs" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True)
    results = {"model": rules["model"], "train": rules["train"]}
    for key, spec in rules["datasets"].items():
        if args.only and key != args.only:
            continue
        results[key] = run_dataset(key, spec, rules, run_dir)
        (run_dir / "results.json").write_text(
            json.dumps(results, indent=2, ensure_ascii=False) + "\n"
        )
    print("\n| Dataset | Zero-shot | Few-shot | Fine-tuned on about 100 rows |")
    print("| --- | --- | --- | --- |")
    for key in rules["datasets"]:
        if key in results:
            r = results[key]
            print(
                f"| {r['title']} | {r['zero_shot']['accuracy']:.1%} "
                f"| {r['few_shot']['accuracy']:.1%} | {r['fine_tuned']['accuracy']:.1%} |"
            )
    print(f"\nwrote {run_dir / 'results.json'}")


if __name__ == "__main__":
    main()
