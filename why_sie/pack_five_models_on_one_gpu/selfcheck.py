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


GPUS = {"one_per_model": 5, "two_gpus": 2, "one_gpu": 1}
SETUPS = ["one_per_model", "two_gpus", "one_gpu"]


def row(p95: float, turns: int = 100, failed: int = 0) -> dict[str, Any]:
    return {"p50_ms": 1, "p95_ms": p95, "turns": turns, "failed": failed}


def by_setup(
    cells: dict[str, dict[int, dict[str, Any]]],
    bar: float,
    max_failed_share: float,
    skipped: dict[str, str],
) -> dict[str, dict[str, Any]]:
    rows = table(cells, GPUS, 584)
    return {v["setup"]: v for v in verdicts(rows, SETUPS, bar, max_failed_share, skipped)}


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
    four_failed = compose_baseline([turn(30, 100, ok=False)], [turn(30, 50, steps=("pii",))], 1)
    assert not four_failed[0]["ok"]
    assert compose_baseline([turn(30, 100)], [], seed=1) == []

    c = cell([turn(5, 999), turn(30, 100), turn(31, 200), turn(32, 300, ok=False)], 20)
    assert c["turns"] == 3
    assert c["failed"] == 1
    assert c["p95_ms"] == 200

    cells = {
        "one_per_model": {50: {"p50_ms": 1, "p95_ms": 500.0, "turns": 1, "failed": 0}},
        "two_gpus": {50: {"p50_ms": 1, "p95_ms": 600.0, "turns": 1, "failed": 0}},
        "one_gpu": {50: {"p50_ms": 1, "p95_ms": 1000.0, "turns": 1, "failed": 0}},
    }
    rows = table(cells, GPUS, 584)
    by = {v["setup"]: v for v in verdicts(rows, SETUPS, 1.25, 0.01, {})}
    assert by["one_per_model"]["passed"]
    assert by["two_gpus"]["passed"]
    assert not by["one_gpu"]["passed"]

    many_failed = {**cells, "one_gpu": {50: row(500.0, turns=100, failed=5)}}
    v = by_setup(many_failed, 1.25, 0.01, {})["one_gpu"]
    assert not v["passed"]
    assert v["failed"] == 5
    assert "5 of 100 turns failed" in v["reason"]

    no_base = {k: c for k, c in cells.items() if k != "one_per_model"}
    v = by_setup(no_base, 1.25, 0.01, {"one_per_model": "a server did not start"})["two_gpus"]
    assert not v["passed"]
    assert v["reason"].startswith("not measured:")
    no_base_50 = {**no_base, "one_per_model": {25: row(500.0)}}
    v = by_setup(no_base_50, 1.25, 0.01, {})["two_gpus"]
    assert v["reason"].startswith("not measured:")

    v = by_setup(no_base, 1.25, 0.01, {"one_gpu": "needs different SIE images"})["one_gpu"]
    assert not v["passed"]
    assert v["reason"] == "not measured: needs different SIE images"

    boundary = {"one_per_model": {50: row(1000.0)}, "two_gpus": {50: row(1254.0)}}
    rows = table(boundary, GPUS, 584)
    assert rows[1]["p95_vs_one_per_model"] == 1.25
    v = {x["setup"]: x for x in verdicts(rows, ["two_gpus"], 1.25, 0.01, {})}["two_gpus"]
    assert not v["passed"]
    print("selfcheck: ok")


if __name__ == "__main__":
    main()
