"""Offline checks of the recipe's own functions. No server, no GPU, no SIE call."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from sie_sdk import SIEConnectionError

from recipe import (
    Targets,
    capacity_from,
    engine_recoveries,
    is_connect_error,
    percentile,
    summarize_kill,
    verdicts,
)

RULES: dict[str, Any] = {
    "expect": {
        "queue_lost_requests": 0,
        "engine_back_within_s": 120,
        "engine_fail_fast_ms": 10_000,
    }
}


def wrapped(cause: BaseException) -> SIEConnectionError:
    try:
        raise SIEConnectionError("Failed to connect") from cause
    except SIEConnectionError as exc:
        return exc


def check_targets() -> None:
    targets = Targets(["http://127.0.0.1:1", "http://127.0.0.1:2"], 10.0, 1.0)
    try:
        assert [targets.pick() for _ in range(4)] == [0, 1, 0, 1]
        targets.mark_down(0)
        assert [targets.pick() for _ in range(3)] == [1, 1, 1]
        targets.mark_down(1)
        assert {targets.pick() for _ in range(4)} == {0, 1}
        targets._down_until = [0.0, time.monotonic() + 10]
        assert targets.pick() == 0
    finally:
        asyncio.run(targets.close())
    no_skip = Targets(["http://127.0.0.1:1", "http://127.0.0.1:2"], 0.0, 1.0)
    try:
        no_skip.mark_down(0)
        assert [no_skip.pick() for _ in range(2)] == [0, 1]
    finally:
        asyncio.run(no_skip.close())


def main() -> None:
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
    assert is_connect_error(wrapped(ConnectionRefusedError("refused")))
    assert not is_connect_error(wrapped(TimeoutError("timed out")))
    assert not is_connect_error(ValueError("connection reset in the message only"))

    check_targets()

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
        {"t": 119.9, "ok": False, "ms": 400.0, "error": "ServerError: in flight"},
        {"t": 121.0, "ok": False, "ms": 3.0, "error": "ModelLoadFailedError: x"},
        {"t": 150.0, "ok": False, "ms": 2.0, "error": "ModelLoadFailedError: x"},
        {"t": 196.0, "ok": True, "ms": 900.0},
    ]
    back = engine_recoveries(engine_rows, [120.0])[0]
    assert back["answering_again_after_s"] == 76.0
    assert back["failed_until_then"] == 3
    assert back["in_flight_failed"] == 1
    assert back["first_error"] == "ServerError: in flight"

    never_rows: list[dict[str, Any]] = [
        {"t": 299.0, "ok": True, "ms": 40.0},
        {"t": 301.0, "ok": False, "ms": 5.0, "error": "ServerError: x"},
        {"t": 330.0, "ok": False, "ms": 6.0, "error": "ServerError: x"},
        {"t": 400.0, "ok": False, "ms": 7.0, "error": "ServerError: x"},
    ]
    never = engine_recoveries(never_rows, [300.0])[0]
    assert never["answering_again_after_s"] is None
    assert never["failed_until_then"] == 3
    assert never["longest_failure_ms"] == 7.0

    summary: dict[str, Any] = {
        "capacity": {
            "queue": {"steps": [], "capacity_per_gpu": 55},
            "round_robin": {"steps": [], "capacity_per_gpu": 60},
        },
        "kill": {
            "queue": {"failed": 0, "kills": [{"kill_s": 120.0}]},
            "round_robin": {"failed": 42, "kills": [{"kill_s": 120.0}]},
        },
        "engine": {"kills": [{"kill_s": 120.0, "killed": 1}], "recoveries": [back]},
    }
    by_name = {v["name"]: v for v in verdicts(summary, RULES)}
    assert not by_name["capacity"]["passed"]
    assert by_name["kill"]["passed"]
    assert by_name["engine"]["passed"]

    summary["engine"]["recoveries"] = [back, never]
    assert not {v["name"]: v for v in verdicts(summary, RULES)}["engine"]["passed"]

    summary["kill"]["queue"]["kills"].append({"error": "kill: worker-1 is still up"})
    kill_verdict = {v["name"]: v for v in verdicts(summary, RULES)}["kill"]
    assert not kill_verdict["passed"]
    assert "still up" in kill_verdict["reason"]

    empty = verdicts({"capacity": {}, "kill": {}}, RULES)
    assert [v["name"] for v in empty] == ["capacity", "kill"]
    assert all(not v["passed"] and v["reason"].startswith("not measured") for v in empty)

    broken: dict[str, Any] = {
        "setup_errors": {"queue": "RuntimeError: port 8080 is already in use"},
        "step_errors": {},
        "capacity": {"round_robin": {"steps": [], "capacity_per_gpu": 60}},
        "kill": {"round_robin": {"failed": 26, "kills": []}},
        "engine": {},
    }
    by_name = {v["name"]: v for v in verdicts(broken, RULES)}
    assert set(by_name) == {"capacity", "kill", "engine"}
    assert not any(v["passed"] for v in by_name.values())
    assert "port 8080" in by_name["kill"]["reason"]
    assert by_name["engine"]["reason"] == "not measured: engine: no result"
    print("selfcheck: ok")


if __name__ == "__main__":
    main()
