"""Offline checks of the recipe's own functions. No SIE call."""

from __future__ import annotations

from pathlib import Path

from recipe import (
    Call,
    ColdResult,
    Refusal,
    load_passages,
    load_rules,
    percentile,
    summarize_burst,
    verdicts,
)

HERE = Path(__file__).resolve().parent


def main() -> None:
    assert percentile([], 0.5) is None
    assert percentile([3.0, 1.0, 2.0], 0.5) == 2.0
    assert percentile([float(i) for i in range(1, 101)], 0.95) == 95.0

    calls = [
        Call(0, 0.0, 0.2, True),
        Call(1, 0.0, 0.4, True),
        Call(2, 0.1, 1.9, False, "TimeoutError: x"),
    ]
    burst = summarize_burst(calls)
    assert burst["calls"] == 3
    assert burst["errors"] == 1
    assert burst["finished_in_s"] == 2.0
    assert burst["p95_s"] == 0.4

    rules = load_rules(HERE / "rules.yaml")
    assert len(load_passages(HERE / "data" / "passages.json")) >= rules["cold"]["passages"]

    cold = ColdResult("m", True, 85.0, [Refusal(20.0, 60.0), Refusal(80.0, 60.0)])
    warm = ColdResult("m", True, 0.4)
    good = {"calls": 10, "errors": 0, "p95_s": 1.2}
    names = {v["name"]: v for v in verdicts(cold, warm, good, rules)}
    assert names["cold"]["passed"]
    assert not names["cold"]["found_warm"]
    assert names["warm"]["passed"]
    assert names["burst"]["passed"]

    found_warm = ColdResult("m", True, 0.6)
    assert found_warm.found_warm(rules["cold"]["found_warm_under_s"])
    slow_burst = {"calls": 10, "errors": 2, "p95_s": 5.0}
    assert not {v["name"]: v for v in verdicts(cold, warm, slow_burst, rules)}["burst"]["passed"]
    print("selfcheck: ok")


if __name__ == "__main__":
    main()
