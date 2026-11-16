"""Offline checks of the recipe's own functions. No server, no GPU, no SIE call."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from recipe import (
    best,
    break_even,
    build_mix,
    format_share,
    input_tokens,
    load_rules,
    load_texts,
    summarize_cell,
    verdicts,
)

HERE = Path(__file__).resolve().parent

COST = {
    "gpu_price_per_hour": 1.0,
    "hours_per_month": 100,
    "per_call_price_per_million_tokens": 0.1,
}
EXPECT = {
    "short_mix": "short",
    "long_mix": "long",
    "short_below_share_of_long": 0.5,
    "long_cells_within": 0.1,
    "break_even_busy_share": [0.1, 0.2],
}


def cell(in_flight: int, tokens_per_s: int, excluded: bool = False) -> dict[str, Any]:
    return {"in_flight": in_flight, "tokens_per_s": tokens_per_s, "excluded": excluded}


def mix(cells: list[dict[str, Any]], busy_share: float | None = None) -> dict[str, Any]:
    top = best(cells)
    return {
        "best_tokens_per_s": top["tokens_per_s"] if top else None,
        "cells": cells,
        "break_even": {"busy_share_to_break_even": busy_share},
    }


def best_in_flight(cells: list[dict[str, Any]]) -> int | None:
    top = best(cells)
    return int(top["in_flight"]) if top else None


def main() -> None:
    rules = load_rules(HERE / "rules.yaml")
    assert rules["cost"]["break_even_mix"] in rules["load"]["mixes"]

    result = break_even(10_000, COST)
    assert result["gpu_per_month_usd"] == 100.0
    assert result["tokens_that_buys_per_month"] == 1_000_000_000
    assert result["tokens_per_s_around_the_clock"] == 2778
    assert result["busy_share_to_break_even"] == 0.2778

    assert break_even(0, COST)["busy_share_to_break_even"] is None
    assert format_share(None) == "n/a (no tokens measured)"
    assert format_share(0.1144) == "11.4% busy"

    queries = ["q1", "q2", "q3"]
    passages = ["p1", "p2"]
    texts = build_mix(queries, passages, 0.7, 100, 1)
    assert len(texts) == 100
    assert sum(t.startswith("q") for t in texts) == 70
    assert build_mix(queries, passages, 0.7, 100, 1) == texts
    assert all(t.startswith("p") for t in build_mix(queries, passages, 0.0, 10, 1))

    assert input_tokens([{"request": {"usage": {"input_tokens": 120}}}]) == 120
    assert input_tokens([{"dense": [0.1]}]) is None
    assert input_tokens([{"request": {"usage": {}}}]) is None

    records: list[dict[str, Any]] = [
        {"sent_s": 1.0, "error": True},
        {"sent_s": 2.0, "error": False, "tokens": 999, "latency_s": 0.5},
        {"sent_s": 11.0, "error": False, "tokens": 300, "latency_s": 0.1},
        {"sent_s": 12.0, "error": False, "tokens": 200, "latency_s": 0.3},
        {"sent_s": 13.0, "error": False, "tokens": None, "latency_s": 0.2},
    ]
    stats = summarize_cell(4, records, [80.0, 90.0], seconds=20, warmup_s=10, max_bad_share=0.5)
    assert stats["requests"] == 3
    assert stats["errors"] == 0
    assert stats["missing_usage"] == 1
    assert stats["tokens_per_s"] == 50
    assert stats["p50_ms"] == 200
    assert stats["gpu_busy_pct"] == 85
    assert not stats["excluded"]
    strict = summarize_cell(4, records, [], seconds=20, warmup_s=10, max_bad_share=0.1)
    assert strict["excluded"]
    assert strict["gpu_busy_pct"] is None
    late_error = [*records, {"sent_s": 14.0, "error": True}]
    assert summarize_cell(4, late_error, [], 20, 10, 1.0)["errors"] == 1
    assert summarize_cell(4, [], [], 20, 10, 0.01)["excluded"]

    assert best_in_flight([cell(4, 10), cell(8, 30)]) == 8
    assert best_in_flight([cell(4, 10), cell(8, 30, excluded=True)]) == 4
    assert best_in_flight([cell(4, 10, excluded=True)]) is None

    passing = {
        "short": mix([cell(4, 10_000), cell(8, 18_000)]),
        "long": mix([cell(4, 52_000), cell(8, 54_000)]),
        "mixed": mix([cell(4, 50_000)], busy_share=0.15),
    }
    assert verdicts(passing, EXPECT, "mixed") == {"short": "Pass", "long": "Pass", "mixed": "Pass"}
    failing = {
        "short": mix([cell(4, 30_000)]),
        "long": mix([cell(4, 30_000), cell(8, 54_000)]),
        "mixed": mix([cell(4, 0)], busy_share=None),
    }
    assert verdicts(failing, EXPECT, "mixed") == {"short": "Miss", "long": "Miss", "mixed": "Miss"}
    excluded_long = {**passing, "long": mix([cell(4, 52_000, excluded=True), cell(8, 54_000)])}
    assert verdicts(excluded_long, EXPECT, "mixed")["long"] == "Miss"

    assert len(load_texts(HERE / "data" / "queries.json")) > 1000
    assert len(load_texts(HERE / "data" / "passages.json")) == 1000
    print("selfcheck: ok")


if __name__ == "__main__":
    main()
