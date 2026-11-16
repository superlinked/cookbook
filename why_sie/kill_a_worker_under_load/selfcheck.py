"""Offline checks of the recipe's own functions. No server, no GPU, no SIE call."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from recipe import (
    capacity_from,
    engine_recoveries,
    is_connect_error,
    load_rules,
    percentile,
    summarize_kill,
    verdicts,
)

HERE = Path(__file__).resolve().parent


def main() -> None:
    rules = load_rules(HERE / "rules.yaml")
    assert percentile([], 0.5) is None
    assert percentile([float(i) for i in range(1, 101)], 0.99) == 99.0

    steps = [
        {"per_gpu": 40, "errors": 0, "p95_ms": 120.0},
        {"per_gpu": 50, "errors": 0, "p95_ms": 300.0},
        {"per_gpu": 55, "errors": 0, "p95_ms": 1900.0},
        {"per_gpu": 60, "errors": 0, "p95_ms": 200.0},
    ]
    assert capacity_from(steps, 500) == 50
    assert capacity_from([{"per_gpu": 40, "errors": 2, "p95_ms": 50.0}], 500) is None

    assert is_connect_error(ConnectionResetError("Connection reset by peer"))
    assert not is_connect_error(ValueError("bad input"))

    rows: list[dict[str, Any]] = [
        {"ok": True, "ms": 30.0},
        {"ok": True, "ms": 6000.0},
        {"ok": False, "ms": 2.0, "error": "ConnectionResetError: reset"},
    ]
    kill = summarize_kill(rows, 5000)
    assert kill["failed"] == 1
    assert kill["over_slow_ms"] == 1
    assert kill["errors"] == {"ConnectionResetError": 1}

    engine_rows: list[dict[str, Any]] = [
        {"t": 119.0, "ok": True, "ms": 50.0},
        {"t": 121.0, "ok": False, "ms": 3.0, "error": "ModelLoadFailedError: x"},
        {"t": 150.0, "ok": False, "ms": 2.0, "error": "ModelLoadFailedError: x"},
        {"t": 196.0, "ok": True, "ms": 900.0},
    ]
    back = engine_recoveries(engine_rows, [120.0])[0]
    assert back["answering_again_after_s"] == 76.0
    assert back["failed_until_then"] == 2

    summary = {
        "capacity": {
            "queue": {"capacity_per_gpu": 55},
            "round_robin": {"capacity_per_gpu": 60},
        },
        "kill": {"queue": {"failed": 0}, "round_robin": {"failed": 42}},
        "engine": {"recoveries": [back]},
    }
    by_name = {v["name"]: v for v in verdicts(summary, rules)}
    assert not by_name["capacity"]["passed"]
    assert by_name["kill"]["passed"]
    assert by_name["engine"]["passed"]
    print("selfcheck: ok")


if __name__ == "__main__":
    main()
