"""Offline checks of the recipe's own functions. No server, no GPU, no SIE call."""

from __future__ import annotations

from pathlib import Path

from recipe import best, break_even, build_mix, load_rules, load_texts

HERE = Path(__file__).resolve().parent


def main() -> None:
    rules = load_rules(HERE / "rules.yaml")
    cost = rules["cost"]

    result = break_even(52_600, cost)
    assert result["gpu_per_month_usd"] == 584.0
    assert 15.4e9 < result["tokens_that_buys_per_month"] < 15.6e9
    assert 5_850 < result["tokens_per_s_around_the_clock"] < 5_950
    assert 0.11 < result["busy_share_to_break_even"] < 0.115

    queries = ["q1", "q2", "q3"]
    passages = ["p1", "p2"]
    mix = build_mix(queries, passages, 0.7, 100, 1)
    assert len(mix) == 100
    assert sum(t.startswith("q") for t in mix) == 70
    assert build_mix(queries, passages, 0.7, 100, 1) == mix
    assert all(t.startswith("p") for t in build_mix(queries, passages, 0.0, 10, 1))

    cells = [{"in_flight": 4, "tokens_per_s": 10}, {"in_flight": 8, "tokens_per_s": 30}]
    assert best(cells)["in_flight"] == 8

    assert len(load_texts(HERE / "data" / "queries.json")) > 1000
    assert len(load_texts(HERE / "data" / "passages.json")) == 1000
    print("selfcheck: ok")


if __name__ == "__main__":
    main()
