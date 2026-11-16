"""Offline checks of the recipe's own functions. No server, no GPU, no SIE call."""

from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from recipe import kept, load_rules, memory_lines, sglang_args, sie_args, verdicts, write_models_dir

HERE = Path(__file__).resolve().parent


def main() -> None:
    rules = load_rules(HERE / "rules.yaml")

    args = sglang_args(rules["embedder"], 0.55, embedding=True)
    assert "--is-embedding" in args
    assert args[args.index("--mem-fraction-static") + 1] == "0.55"
    assert "--mem-fraction-static" not in sglang_args(rules["guard"], None, embedding=False)
    assert sie_args(Path("/m"), ["a", "b"], 8080)[-1] == "a,b"

    with tempfile.TemporaryDirectory() as tmp:
        out = write_models_dir(rules, Path(tmp))
        for key in ("embedder", "guard"):
            name = rules[key]["model"].replace("/", "__") + ".yaml"
            config = yaml.safe_load((out / name).read_text(encoding="utf-8"))
            budget = config["profiles"]["default"]["kv_budget_tokens"]
            assert budget == rules[key]["kv_budget_tokens"]

    alone = {"embedder": {"tokens_per_s": 1000.0}, "guard": {"requests_per_s": 20.0}}
    both = {"embedder": {"tokens_per_s": 580.0}, "guard": {"requests_per_s": 11.4}}
    assert kept(alone, both, "embedder") == 0.58
    assert kept(alone, both, "guard") == 0.57

    summary = {
        "defaults": {"embedder_started": True, "guard_started_next_to_it": False},
        "sie": {"kept": {"embedder": 0.58, "guard": 0.57}},
        "by_hand": {"kept": {"embedder": 0.85, "guard": 0.81}},
    }
    by_name = {v["name"]: v for v in verdicts(summary, 0.8)}
    assert by_name["defaults"]["passed"]
    assert not by_name["sie"]["passed"]
    assert by_name["by_hand"]["passed"]

    log = "foo\nmax_total_num_tokens=8192, mem_fraction_static=0.41\nbar"
    assert len(memory_lines(log)) == 1
    print("selfcheck: ok")


if __name__ == "__main__":
    main()
