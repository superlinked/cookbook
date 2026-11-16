"""Offline checks of the recipe's own functions. No server, no GPU, no SIE call."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import httpx
import yaml

from recipe import (
    MissingUsage,
    kept,
    load_rules,
    memory_lines,
    sglang_args,
    sglang_calls,
    sie_args,
    sie_calls,
    verdicts,
    write_models_dir,
)

HERE = Path(__file__).resolve().parent
BAR = 0.8
MAX_ERROR_SHARE = 0.05
MODELS = {
    "embedder": {"model": "e", "port": 1},
    "guard": {"model": "g", "port": 2, "max_tokens": 4},
}


def sglang_reply(usage: dict[str, Any] | None) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = {"object": "list", "data": []}
        if usage is not None:
            body["usage"] = usage
        return httpx.Response(200, content=json.dumps(body).encode())

    return httpx.MockTransport(handle)


async def call_sglang(usage: dict[str, Any] | None, name: str) -> int:
    async with httpx.AsyncClient(transport=sglang_reply(usage)) as http:
        result: int = await sglang_calls(http, MODELS)[name]("some text")
        return result


class StubSIE:
    def __init__(self, with_usage: bool) -> None:
        self.with_usage = with_usage

    async def encode(self, model: str, item: Any) -> dict[str, Any]:
        return {"request": {"usage": {"input_tokens": 9}}} if self.with_usage else {"request": {}}

    async def chat_completions(self, model: str, messages: Any, **kwargs: Any) -> dict[str, Any]:
        return {"usage": {"prompt_tokens": 12}} if self.with_usage else {"choices": []}


def raises_missing_usage(coro: Any) -> bool:
    try:
        asyncio.run(coro)
    except MissingUsage:
        return True
    return False


def stats(errors: float = 0.0) -> dict[str, Any]:
    return {"error_share": errors}


def main() -> None:
    rules = load_rules(HERE / "rules.yaml")

    args = sglang_args(rules["embedder"], 0.55, embedding=True)
    assert "--is-embedding" in args
    assert args[args.index("--mem-fraction-static") + 1] == "0.55"
    assert "--mem-fraction-static" not in sglang_args(rules["guard"], None, embedding=False)
    assert sie_args(Path("/m"), ["a", "b"], 8080)[-1] == "a,b"

    with tempfile.TemporaryDirectory() as tmp:
        out = write_models_dir(rules, Path(tmp))
        for key in ("embedder", "guard"):
            name = rules[key]["model"].replace("/", "__") + ".yaml"
            config = yaml.safe_load((out / name).read_text(encoding="utf-8"))
            budget = config["profiles"]["default"]["kv_budget_tokens"]
            assert budget == rules[key]["kv_budget_tokens"]

    assert asyncio.run(call_sglang({"prompt_tokens": 17, "total_tokens": 17}, "embedder")) == 17
    assert asyncio.run(call_sglang({"prompt_tokens": 40, "completion_tokens": 3}, "guard")) == 40
    assert raises_missing_usage(call_sglang(None, "embedder"))
    assert raises_missing_usage(call_sglang({"total_tokens": 17}, "guard"))

    good, bad = sie_calls(StubSIE(True), MODELS), sie_calls(StubSIE(False), MODELS)
    assert asyncio.run(good["embedder"]("x")) == 9
    assert asyncio.run(good["guard"]("x")) == 12
    assert raises_missing_usage(bad["embedder"]("x"))
    assert raises_missing_usage(bad["guard"]("x"))

    alone = {"embedder": {"tokens_per_s": 1000.0}, "guard": {"requests_per_s": 20.0}}
    both = {"embedder": {"tokens_per_s": 580.0}, "guard": {"requests_per_s": 11.4}}
    assert kept(alone, both, "embedder") == 0.58
    assert kept(alone, both, "guard") == 0.57
    assert kept({"embedder": {"tokens_per_s": 0.0}}, both, "embedder") is None

    clean = {"embedder": stats(), "guard": stats()}
    phases = {
        "embedder_alone": {"embedder": stats()},
        "guard_alone": {"guard": stats()},
        "together": clean,
    }
    summary = {
        "defaults": {
            "embedder_started": True,
            "guard_started_next_to_it": False,
            "guard_ran_out_of_memory": True,
        },
        "sie": {**phases, "kept": {"embedder": 0.58, "guard": 0.57}},
        "by_hand": {**phases, "kept": {"embedder": 0.85, "guard": 0.81}},
    }
    by_name = {v["name"]: v for v in verdicts(summary, BAR, MAX_ERROR_SHARE)}
    assert by_name["defaults"]["passed"]
    assert not by_name["sie"]["passed"]
    assert by_name["by_hand"]["passed"]

    def one(setup: str, result: dict[str, Any]) -> dict[str, Any]:
        verdict: dict[str, Any] = verdicts({setup: result}, BAR, MAX_ERROR_SHARE)[0]
        return verdict

    for defaults in (
        {"embedder_started": False, "guard_started_next_to_it": False},
        {"embedder_started": True, "guard_started_next_to_it": False},
        {"embedder_started": True, "guard_started_next_to_it": True},
        {"error": "RuntimeError: boom"},
    ):
        verdict = one("defaults", {"guard_ran_out_of_memory": False, **defaults})
        assert not verdict["passed"]
        assert verdict["reason"]

    errored = one("sie", {"error": "guard did not start alone"})
    assert not errored["passed"]
    assert not errored["started"]
    assert errored["kept"] == "not measured"
    assert errored["reason"].startswith("not measured")

    no_alone = one("by_hand", {**phases, "kept": {"embedder": None, "guard": 0.9}})
    assert not no_alone["passed"]
    assert no_alone["reason"] == "not measured: no throughput alone for embedder"

    noisy = {**phases, "guard_alone": {"guard": stats(0.2)}}
    failing = one("by_hand", {**noisy, "kept": {"embedder": 0.9, "guard": 2.0}})
    assert not failing["passed"]
    assert "guard_alone.guard" in failing["reason"]

    log = "foo\nmax_total_num_tokens=8192, mem_fraction_static=0.41\nbar"
    assert len(memory_lines(log)) == 1
    print("selfcheck: ok")


if __name__ == "__main__":
    main()
