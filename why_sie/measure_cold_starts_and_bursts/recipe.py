"""Measure what a request sees when its model is cold on SIE, and how a burst of calls comes back.

Three measurements, in order:
  1. cold:  one rerank call to a model that is often at zero, retried on each 503 until it answers
  2. warm:  the same call again, right after
  3. burst: many embedding calls from many callers at once

Writes runs/<timestamp>/summary.json and burst.jsonl, and prints the summary.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from sie_sdk import ProvisioningError, SIEClient
from sie_sdk.types import Item

HERE = Path(__file__).resolve().parent
WAIT_S = 600.0


def make_client() -> SIEClient:
    """Build an SIE client from the API key and SIE_BASE_URL in the environment or `.env`."""
    load_dotenv()
    api_key = os.environ.get("SIE_API_KEY", "").strip()
    base_url = os.environ.get("SIE_BASE_URL", "").strip()
    if not api_key and not base_url:
        raise RuntimeError("Set SIE_API_KEY, or SIE_BASE_URL for SIE Cluster or SIE Local, in .env")
    base_url = base_url or "https://api.superlinked.com"
    return SIEClient(api_key=api_key or None, base_url=base_url, timeout_s=WAIT_S)


def load_rules(path: Path) -> dict[str, Any]:
    rules: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return rules


def load_passages(path: Path) -> list[str]:
    passages: list[str] = json.loads(path.read_text(encoding="utf-8"))
    return passages


# --- Cold and warm calls -----------------------------------------------------------------------


@dataclass
class Refusal:
    """One 503 the gateway answered while the model was starting."""

    at_s: float
    retry_after_s: float | None


@dataclass
class ColdResult:
    model: str
    ok: bool
    seconds: float
    refusals: list[Refusal] = field(default_factory=list)
    error: str | None = None

    def found_warm(self, under_s: float) -> bool:
        return self.ok and not self.refusals and self.seconds < under_s


def call_until_answered(
    client: SIEClient, model: str, query: str, passages: list[str], budget_s: float
) -> ColdResult:
    """Score the passages against the query, waiting out each 503 for as long as it says to."""
    t0 = time.monotonic()
    refusals: list[Refusal] = []
    while True:
        try:
            client.score(
                model,
                Item(text=query),
                [Item(text=p) for p in passages],
                wait_for_capacity=False,
            )
            return ColdResult(model, True, round(time.monotonic() - t0, 2), refusals)
        except ProvisioningError as exc:
            elapsed = time.monotonic() - t0
            refusals.append(Refusal(round(elapsed, 2), exc.retry_after))
            if elapsed >= budget_s:
                return ColdResult(model, False, round(elapsed, 2), refusals, "budget used up")
            time.sleep(min(exc.retry_after or 60.0, max(budget_s - elapsed, 1.0)))
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            return ColdResult(model, False, round(time.monotonic() - t0, 2), refusals, error)


# --- The burst ---------------------------------------------------------------------------------


@dataclass
class Call:
    i: int
    sent_s: float
    seconds: float
    ok: bool
    error: str | None = None


def run_burst(
    client: SIEClient, model: str, passages: list[str], calls: int, callers: int
) -> list[Call]:
    """Send `calls` single-passage encode calls with `callers` in flight at once."""
    t0 = time.monotonic()

    def one(i: int) -> Call:
        sent = time.monotonic()
        try:
            client.encode(model, Item(text=passages[i % len(passages)]))
            return Call(i, round(sent - t0, 3), round(time.monotonic() - sent, 4), True)
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:120]}"
            return Call(i, round(sent - t0, 3), round(time.monotonic() - sent, 4), False, error)

    with ThreadPoolExecutor(max_workers=callers) as pool:
        return list(pool.map(one, range(calls)))


def percentile(values: list[float], q: float) -> float | None:
    """The value below which a share q of the values fall (nearest rank)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, round(q * len(ordered) + 0.5 - 1e-9))
    return ordered[min(rank, len(ordered)) - 1]


def summarize_burst(calls: list[Call]) -> dict[str, Any]:
    ok = [c.seconds for c in calls if c.ok]
    finished = max((c.sent_s + c.seconds for c in calls), default=0.0)
    return {
        "calls": len(calls),
        "errors": len(calls) - len(ok),
        "error_kinds": sorted({(c.error or "")[:60] for c in calls if not c.ok}),
        "finished_in_s": round(finished, 1),
        "calls_per_s": round(len(calls) / finished, 1) if finished else None,
        "p50_s": percentile(ok, 0.50),
        "p95_s": percentile(ok, 0.95),
    }


# --- Verdicts ----------------------------------------------------------------------------------


def verdicts(
    cold: ColdResult, warm: ColdResult, burst: dict[str, Any], rules: dict[str, Any]
) -> list[dict[str, Any]]:
    """Each measurement against the outcome rules.yaml expects."""
    cold_rules, warm_rules, burst_rules = rules["cold"], rules["warm"], rules["burst"]
    was_warm = cold.found_warm(cold_rules["found_warm_under_s"])
    cold_ok = cold.ok and cold.seconds <= cold_rules["expect_answer_within_s"]
    p95 = burst["p95_s"]
    burst_ok = (
        burst["errors"] <= burst_rules["expect_errors"]
        and p95 is not None
        and p95 < burst_rules["expect_p95_under_s"]
    )
    return [
        {
            "name": "cold",
            "expected": f"answered within {cold_rules['expect_answer_within_s']} s",
            "found_warm": was_warm,
            "passed": cold_ok,
        },
        {
            "name": "warm",
            "expected": f"answered within {warm_rules['expect_answer_within_s']} s",
            "passed": warm.ok and warm.seconds <= warm_rules["expect_answer_within_s"],
        },
        {
            "name": "burst",
            "expected": (
                f"{burst_rules['expect_errors']} errors, "
                f"p95 under {burst_rules['expect_p95_under_s']} s"
            ),
            "passed": burst_ok,
        },
    ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--rules", type=Path, default=HERE / "rules.yaml")
    parser.add_argument("--passages", type=Path, default=HERE / "data" / "passages.json")
    parser.add_argument("--calls", type=int, help="burst calls (default from rules.yaml)")
    parser.add_argument(
        "--callers", type=int, help="burst calls in flight at once (default from rules.yaml)"
    )
    parser.add_argument("--skip-cold", action="store_true", help="skip the cold and warm calls")
    parser.add_argument("--output", type=Path, help="run folder (default runs/<timestamp>)")
    args = parser.parse_args()

    rules = load_rules(args.rules)
    passages = load_passages(args.passages)
    out = args.output or HERE / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    client = make_client()
    summary: dict[str, Any] = {
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds")
    }
    try:
        cold_rules = rules["cold"]
        if args.skip_cold:
            cold = warm = ColdResult(cold_rules["model"], False, 0.0, error="skipped")
        else:
            some = passages[: cold_rules["passages"]]
            print(
                f"cold: calling {cold_rules['model']} until it answers "
                f"(up to {cold_rules['budget_s']} s)"
            )
            cold = call_until_answered(
                client, cold_rules["model"], cold_rules["query"], some, cold_rules["budget_s"]
            )
            print(f"cold: {cold.seconds} s, {len(cold.refusals)} 503s")
            warm = call_until_answered(
                client, cold_rules["model"], cold_rules["query"], some, cold_rules["budget_s"]
            )
            print(f"warm: {warm.seconds} s")
        burst_rules = rules["burst"]
        calls = args.calls or burst_rules["calls"]
        callers = args.callers or burst_rules["callers"]
        print(f"burst: {calls} calls to {burst_rules['model']}, {callers} at once")
        burst_calls = run_burst(client, burst_rules["model"], passages, calls, callers)
    finally:
        client.close()

    with (out / "burst.jsonl").open("w", encoding="utf-8") as fh:
        for call in burst_calls:
            fh.write(json.dumps(asdict(call)) + "\n")
    burst = summarize_burst(burst_calls)
    summary.update({"cold": asdict(cold), "warm": asdict(warm), "burst": burst})
    if not args.skip_cold:
        summary["verdicts"] = verdicts(cold, warm, burst, rules)
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
