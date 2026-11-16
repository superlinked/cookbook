"""Offline checks of the recipe's own functions. No server, no GPU, no SIE call."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from recipe import (
    load_rules,
    log_events,
    models_for,
    quantile,
    ranking_at,
    summarize,
    verdicts,
    zipf_weights,
)

HERE = Path(__file__).resolve().parent


def result(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "requests": 100,
        "failed": 0,
        "unloads": 0,
        "waited_for_load": 0,
        "pinned_log_loads": 1,
        "pinned_requests": 40,
        "pinned_cold_starts": 0,
        "pinned_failed": 0,
    }
    return base | changes


def passed(setup: str, outcome: dict[str, Any]) -> dict[str, bool]:
    return {v["name"]: v["passed"] for v in verdicts({setup: outcome}, max_failed_share=0.05)}


def check_rules() -> None:
    rules = load_rules(HERE / "rules.yaml")
    fit, overflow = models_for(rules, "fit"), models_for(rules, "overflow")
    assert fit
    assert len(overflow) > len(fit)
    assert overflow[: len(fit)] == fit
    names = [m["model"] for m in overflow]
    assert len(set(names)) == len(names)
    assert rules["pinned"]["model"] not in names
    for model in overflow:
        assert model["kind"] in {"encode", "score", "extract"}
        if model["kind"] == "extract":
            assert model["labels"] in rules["labels"]


def check_log_events() -> None:
    log = "\n".join(
        [
            "03:41:02 INFO     sie_server.core.model_loader: "
            "Model 'BAAI/bge-m3' loaded successfully",
            "03:41:10 INFO     sie_server.core.registry: Unloading model 'c'",
            "03:41:11 INFO     something else",
        ]
    )
    events = log_events(log, dt.date(2026, 11, 16))
    assert [e[1:] for e in events] == [("load", "BAAI/bge-m3"), ("unload", "c")]
    assert events[0][0] == dt.datetime(2026, 11, 16, 3, 41, 2, tzinfo=dt.UTC).timestamp()

    midnight = "\n".join(
        [
            "23:59:58 INFO     sie_server.core.model_loader: Model 'a' loaded successfully",
            "23:59:59 INFO     something else",
            "00:00:01 INFO     sie_server.core.registry: Unloading model 'a'",
        ]
    )
    crossed = log_events(midnight, dt.date(2026, 11, 16))
    assert crossed[0][0] == dt.datetime(2026, 11, 16, 23, 59, 58, tzinfo=dt.UTC).timestamp()
    assert crossed[1][0] == dt.datetime(2026, 11, 17, 0, 0, 1, tzinfo=dt.UTC).timestamp()


def check_summary() -> None:
    day = dt.date(2026, 11, 16)
    log = "\n".join(
        [
            "00:00:01 INFO     sie_server.core.model_loader: Model 'pin' loaded successfully",
            "00:00:10 INFO     sie_server.core.model_loader: Model 'b' loaded successfully",
            "00:00:20 INFO     sie_server.core.registry: Unloading model 'c'",
        ]
    )
    events = log_events(log, day)
    base = dt.datetime.combine(day, dt.time(0, 0), tzinfo=dt.UTC).timestamp()
    records = [
        {"model": "pin", "pinned": True, "wall": base + 70, "ms": 30.0, "ok": True},
        {"model": "b", "pinned": False, "wall": base + 5, "ms": 6000.0, "ok": True},
        {"model": "a", "pinned": False, "wall": base + 30, "ms": 25.0, "ok": True},
        {"model": "a", "pinned": False, "wall": base + 40, "ms": 300000.0, "ok": False},
    ]
    summary = summarize(records, events, warmup_s=60, pinned_model="pin")
    assert summary.loads == 2
    assert summary.unloads == 1
    assert summary.waited_for_load == 1
    assert summary.failed == 1
    assert summary.pinned_log_loads == 1
    assert summary.pinned_requests == 1
    assert summary.pinned_cold_starts == 0
    assert summarize([], events, warmup_s=60, pinned_model="pin").requests == 0


def check_verdicts() -> None:
    assert passed("fit", result()) == {"fit": True, "fit: pinned": True}
    assert not passed("fit", result(unloads=1))["fit"]
    assert not passed("fit", result(failed=100))["fit"]
    assert not passed("fit", result(failed=6))["fit"]
    assert passed("fit", result(failed=5))["fit"]
    assert not passed("fit", result(pinned_requests=0))["fit: pinned"]
    assert not passed("fit", result(pinned_failed=1))["fit: pinned"]
    assert not passed("fit", result(pinned_cold_starts=1))["fit: pinned"]

    assert passed("overflow", result(unloads=30, waited_for_load=10))["overflow"]
    assert not passed("overflow", result(unloads=30, waited_for_load=0))["overflow"]
    assert not passed("overflow", result(unloads=0, waited_for_load=10))["overflow"]

    drifted = verdicts({"fit": result(pinned_log_loads=0)}, max_failed_share=0.05)
    assert len(drifted) == 2
    assert not any(v["passed"] for v in drifted)
    assert all(v["reason"] == "no load events parsed; check the log format" for v in drifted)

    crashed = verdicts({"overflow": {"error": "the server did not start"}}, max_failed_share=0.05)
    assert len(crashed) == 2
    assert not any(v["passed"] for v in crashed)
    assert all(v["reason"] == "not measured: the server did not start" for v in crashed)


def main() -> None:
    check_rules()

    weights = zipf_weights(3, 1.1)
    assert abs(sum(weights) - 1) < 1e-9
    assert weights[0] > weights[1] > weights[2]

    models = [{"model": "a"}, {"model": "b"}, {"model": "c"}]
    assert ranking_at(models, 0, 150)[0]["model"] == "a"
    assert ranking_at(models, 151, 150)[0]["model"] == "b"
    assert ranking_at(models, 3 * 150, 150)[0]["model"] == "a"

    assert quantile([], 0.5) is None
    assert quantile([42.0], 0.99) == 42.0
    hundred_and_one = [float(v) for v in range(1, 102)]
    assert quantile(hundred_and_one, 0.5) == 51.0
    assert quantile(hundred_and_one, 0.95) == 96.0

    check_log_events()
    check_summary()
    check_verdicts()
    print("selfcheck: ok")


if __name__ == "__main__":
    main()
