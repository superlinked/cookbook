"""Offline checks of the recipe's own functions. No server, no GPU, no SIE call."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from recipe import (
    Inputs,
    Runner,
    cell,
    compose_baseline,
    image_for,
    load_rules,
    load_texts,
    quantile,
    table,
    turns_per_s,
    verdicts,
)

HERE = Path(__file__).resolve().parent


def turn(
    t: float, ms: float, ok: bool = True, steps: tuple[str, ...] = ("guard",)
) -> dict[str, Any]:
    return {
        "t": t,
        "ok": ok,
        "turn_ms": ms,
        "calls": [{"step": s, "ok": ok, "ms": ms} for s in steps],
    }


def main() -> None:
    rules = load_rules(HERE / "rules.yaml")
    assert set(rules["steps"]) == set(rules["models"])
    assert len(load_texts(HERE / "data" / "messages.json")) >= 20
    assert len(load_texts(HERE / "data" / "passages.json")) >= rules["models"]["rerank"]["passages"]

    assert image_for(rules, ["embed", "rerank"]) == rules["models"]["embed"]["image"]
    assert image_for(rules, ["guard", "embed"]) is None

    inputs = Inputs(["m"], ["p"])
    four = Runner(rules, None, inputs, HERE, gpus=4)  # type: ignore[arg-type]
    passes = four.passes("one_per_model")
    assert len(passes) == 2
    assert passes[1] == ([["pii"]], ["pii"])
    assert "pii" not in passes[0][1]
    assert len(Runner(rules, None, inputs, HERE, gpus=5).passes("one_per_model")) == 1  # type: ignore[arg-type]

    assert quantile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.0
    assert quantile([float(i) for i in range(1, 101)], 0.95) == 95.0
    assert turns_per_s([turn(10, 1), turn(25, 1), turn(30, 1), turn(40, 1)], 20) == 0.15

    composed = compose_baseline([turn(30, 100)], [turn(30, 50, steps=("pii",))], seed=1)
    assert composed[0]["turn_ms"] == 150
    assert [c["step"] for c in composed[0]["calls"]] == ["guard", "pii"]
    failed = compose_baseline([turn(30, 100)], [turn(30, 50, ok=False)], seed=1)
    assert not failed[0]["ok"]

    c = cell([turn(5, 999), turn(30, 100), turn(31, 200), turn(32, 300, ok=False)], 20)
    assert c["turns"] == 3
    assert c["failed"] == 1
    assert c["p95_ms"] == 200

    cells = {
        "one_per_model": {50: {"p50_ms": 1, "p95_ms": 500.0, "turns": 1, "failed": 0}},
        "two_gpus": {50: {"p50_ms": 1, "p95_ms": 600.0, "turns": 1, "failed": 0}},
        "one_gpu": {50: {"p50_ms": 1, "p95_ms": 1000.0, "turns": 1, "failed": 0}},
    }
    rows = table(cells, {"one_per_model": 5, "two_gpus": 2, "one_gpu": 1}, 584)
    by = {v["setup"]: v for v in verdicts(rows, 1.25)}
    assert by["two_gpus"]["passed"]
    assert not by["one_gpu"]["passed"]
    print("selfcheck: ok")


if __name__ == "__main__":
    main()
