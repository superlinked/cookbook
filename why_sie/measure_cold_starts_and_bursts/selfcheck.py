"""Offline checks of the recipe's own functions. No SIE call."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest import mock

from sie_sdk import ProvisioningError, SIEClient

from recipe import (
    Call,
    ColdResult,
    Refusal,
    call_until_answered,
    load_passages,
    load_rules,
    percentile,
    summarize_burst,
    verdicts,
)

HERE = Path(__file__).resolve().parent

RULES: dict[str, Any] = {
    "cold": {"found_warm_under_s": 5, "expect_answer_within_s": 300},
    "warm": {"expect_answer_within_s": 2},
    "burst": {"expect_errors": 0, "expect_p95_under_s": 3},
}
GOOD_BURST: dict[str, Any] = {"calls": 10, "errors": 0, "p95_s": 1.2}


class StubClient:
    """Answers `score` after refusing it with a 503 a given number of times."""

    def __init__(self, refusals: int, retry_after: float | None = 60.0) -> None:
        self.refusals = refusals
        self.retry_after = retry_after
        self.calls = 0

    def score(self, *args: Any, **kwargs: Any) -> list[float]:
        self.calls += 1
        if self.calls <= self.refusals:
            raise ProvisioningError("starting", retry_after=self.retry_after)
        return [0.5]


def run_stub(stub: StubClient, budget_s: float) -> tuple[ColdResult, list[float]]:
    slept: list[float] = []
    with mock.patch("recipe.time.sleep", side_effect=slept.append):
        result = call_until_answered(cast(SIEClient, stub), "m", "q", ["a", "b"], budget_s)
    return result, slept


def by_name(cold: ColdResult | None, warm: ColdResult | None) -> dict[str, dict[str, Any]]:
    return {v["name"]: v for v in verdicts(cold, warm, GOOD_BURST, RULES)}


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
    assert burst["calls_per_s"] == 1.0
    assert burst["p95_s"] == 0.4

    rules = load_rules(HERE / "rules.yaml")
    assert len(load_passages(HERE / "data" / "passages.json")) >= rules["cold"]["passages"]
    assert rules["client"]["timeout_s"] > 0

    answered, slept = run_stub(StubClient(2), 900.0)
    assert answered.ok
    assert len(answered.refusals) == 2
    assert all(r.retry_after_s == 60.0 for r in answered.refusals)
    assert slept == [60.0, 60.0]

    used_up, _ = run_stub(StubClient(5), 0.0)
    assert not used_up.ok
    assert used_up.error == "budget used up"

    _, slept = run_stub(StubClient(1, retry_after=0.0), 900.0)
    assert slept == [0.0]
    _, slept = run_stub(StubClient(1, retry_after=None), 900.0)
    assert slept == [60.0]

    cold = ColdResult("m", True, 85.0, [Refusal(20.0, 60.0), Refusal(80.0, 60.0)])
    warm = ColdResult("m", True, 0.4)
    names = by_name(cold, warm)
    assert names["cold"]["passed"] is True
    assert names["cold"]["refusals_with_retry_after"] == 2
    assert names["warm"]["passed"] is True
    assert names["burst"]["passed"] is True

    missing_header = ColdResult("m", True, 85.0, [Refusal(20.0, None), Refusal(80.0, 60.0)])
    verdict = by_name(missing_header, warm)["cold"]
    assert verdict["passed"] is False
    assert verdict["refusals_with_retry_after"] == 1

    no_answer = ColdResult("m", False, 900.0, [Refusal(900.0, 60.0)], "budget used up")
    assert by_name(no_answer, warm)["cold"]["passed"] is False
    too_slow = ColdResult("m", True, 301.0, [Refusal(20.0, 60.0)])
    assert by_name(too_slow, warm)["cold"]["passed"] is False

    assert by_name(cold, ColdResult("m", True, 2.5))["warm"]["passed"] is False
    assert by_name(cold, ColdResult("m", False, 0.1, error="HTTPError"))["warm"]["passed"] is False

    already_warm = ColdResult("m", True, 0.6)
    verdict = by_name(already_warm, warm)["cold"]
    assert verdict["passed"] is None
    assert verdict["reason"] == "model was already warm; no cold start measured"

    skipped = verdicts(None, None, GOOD_BURST, RULES)
    assert [v["name"] for v in skipped] == ["burst"]
    assert skipped[0]["passed"] is True

    slow_burst = {"calls": 10, "errors": 2, "p95_s": 5.0}
    assert not verdicts(None, None, slow_burst, RULES)[0]["passed"]
    print("selfcheck: ok")


if __name__ == "__main__":
    main()
