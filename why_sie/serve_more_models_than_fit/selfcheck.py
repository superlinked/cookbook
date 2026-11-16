"""Offline checks of the recipe's own functions. No server, no GPU, no SIE call."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from recipe import load_rules, log_events, models_for, ranking_at, summarize, verdicts, zipf_weights

HERE = Path(__file__).resolve().parent


def main() -> None:
    rules = load_rules(HERE / "rules.yaml")
    assert 1 + len(models_for(rules, "fit")) == 10
    assert 1 + len(models_for(rules, "overflow")) == 16

    weights = zipf_weights(3, 1.1)
    assert abs(sum(weights) - 1) < 1e-9
    assert weights[0] > weights[1] > weights[2]

    models = [{"model": "a"}, {"model": "b"}, {"model": "c"}]
    assert ranking_at(models, 0, 150)[0]["model"] == "a"
    assert ranking_at(models, 151, 150)[0]["model"] == "b"
    assert ranking_at(models, 3 * 150, 150)[0]["model"] == "a"

    log = "\n".join(
        [
            "00:00:10 INFO  sie_server: Model 'b' loaded successfully in 8.1s",
            "00:00:20 INFO  sie_server: Unloading model 'c' (memory pressure)",
            "00:00:21 INFO  something else",
        ]
    )
    day = dt.date(2026, 11, 16)
    events = log_events(log, day)
    assert [e[1:] for e in events] == [("load", "b"), ("unload", "c")]

    base = dt.datetime.combine(day, dt.time(0, 0), tzinfo=dt.UTC).timestamp()
    records = [
        {"model": "pin", "pinned": True, "wall": base + 70, "ms": 30.0, "ok": True},
        {"model": "b", "pinned": False, "wall": base + 5, "ms": 6000.0, "ok": True},
        {"model": "a", "pinned": False, "wall": base + 30, "ms": 25.0, "ok": True},
        {
            "model": "a",
            "pinned": False,
            "wall": base + 40,
            "ms": 300000.0,
            "ok": False,
            "error": "x",
        },
    ]
    summary = summarize(records, events, warmup_s=60)
    assert summary.loads == 1
    assert summary.unloads == 1
    assert summary.waited_for_load == 1
    assert summary.failed == 1
    assert summary.pinned_requests == 1
    assert summary.pinned_cold_starts == 0

    result = {"unloads": 0, "waited_for_load": 0, "pinned_cold_starts": 0, "pinned_requests": 5}
    by_name = {v["name"]: v for v in verdicts({"fit": result})}
    assert by_name["fit"]["passed"]
    assert by_name["fit: pinned"]["passed"]
    print("selfcheck: ok")


if __name__ == "__main__":
    main()
