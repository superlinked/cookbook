"""Offline checks: the data and the SIE configs agree with rules.yaml, and scoring counts right.

    uv run selfcheck.py

No SIE call and no model: it reads the committed data/, sie_models/ and rules.yaml, and
scores hand-written stand-in replies.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import torch
import yaml

from recipe import few_shot_examples, load_rows, score, segment_ids, top_pick

HERE = Path(__file__).parent


def check_data_and_configs() -> None:
    rules = yaml.safe_load((HERE / "rules.yaml").read_text())
    for key, spec in rules["datasets"].items():
        labels = list(spec["labels"])
        for split in ("train", "test"):
            rows = load_rows(HERE / f"data/{key}/{split}.jsonl", labels)
            assert rows, f"data/{key}/{split}.jsonl is empty"
        config = yaml.safe_load((HERE / f"sie_models/{key}.yaml").read_text())
        assert config["sie_id"] == spec["served_model"], key
        assert config["weights_path"] == f"/weights/{key}", key
        assert "hf_id" not in config, key
        assert "extract" in config["tasks"], key
        loadtime = config["profiles"]["default"]["adapter_options"]["loadtime"]
        assert loadtime["classification_type"] == rules["score"]["classification_type"], key


def check_load_rows_refuses_drift() -> None:
    labels = ["a", "b"]
    bad = [
        {"text": "x", "all_labels": ["a", "c"], "true_labels": ["a"]},  # renamed label
        {"text": "x", "all_labels": labels, "true_labels": ["a", "b"]},  # two answers
        {"text": "x", "all_labels": labels, "true_labels": ["c"]},  # unknown answer
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rows.jsonl"
        for row in bad:
            path.write_text(json.dumps(row) + "\n")
            try:
                load_rows(path, labels)
            except ValueError:
                continue
            raise AssertionError(f"load_rows accepted {row}")


def check_top_pick_and_score() -> None:
    # The higher score listed second still wins.
    pick = top_pick([{"label": "a", "score": 0.2}, {"label": "b", "score": 0.7}], "t")
    assert pick == {"label": "b", "score": 0.7}
    try:
        top_pick([], "t")
    except RuntimeError:
        pass
    else:
        raise AssertionError("top_pick accepted an empty reply")
    rows = [
        {"text": "1", "true_labels": ["a"]},
        {"text": "2", "true_labels": ["a"]},
        {"text": "3", "true_labels": ["b"]},
    ]
    picks = [
        {"label": "a", "score": 0.9},
        {"label": "b", "score": 0.6},
        {"label": "b", "score": 0.8},
    ]
    result = score(picks, rows, ["a", "b", "c"])
    assert (result["correct"], result["accuracy"]) == (2, 0.667), result
    assert result["per_label"] == {"a": 0.5, "b": 1.0, "c": None}, result["per_label"]


def check_few_shot_examples() -> None:
    rows = [
        {"text": "a1", "true_labels": ["a"]},
        {"text": "b1", "true_labels": ["b"]},
        {"text": "a2", "true_labels": ["a"]},
    ]
    got = few_shot_examples(rows, ["a", "b", "c"], 1)
    assert got == [{"text": "a1", "labels": ["a"]}, {"text": "b1", "labels": ["b"]}], got
    assert len(few_shot_examples(rows, ["a", "b"], 2)) == 3


def check_segment_ids() -> None:
    """Labels 0, the row from the text token on 1, examples from the first example token on 2."""
    model = SimpleNamespace(config=SimpleNamespace(text_token_index=8, example_token_index=9))
    ids = torch.tensor([[1, 5, 5, 8, 4, 4, 9, 4, 9, 4], [1, 5, 8, 4, 4, 4, 0, 0, 0, 0]])
    got = segment_ids(model, ids).tolist()
    assert got == [[0, 0, 0, 1, 1, 1, 2, 2, 2, 2], [0, 0, 1, 1, 1, 1, 1, 1, 1, 1]], got


def main() -> None:
    check_data_and_configs()
    check_load_rows_refuses_drift()
    check_top_pick_and_score()
    check_few_shot_examples()
    check_segment_ids()
    print("selfcheck passed")


if __name__ == "__main__":
    main()
