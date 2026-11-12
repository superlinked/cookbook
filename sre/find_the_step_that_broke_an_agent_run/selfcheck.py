"""Offline checks of the recipe's own functions: no SIE call, no recorded reply.

uv run selfcheck.py
"""

from __future__ import annotations

import itertools
import json
from typing import Any

import dspy
import yaml
from sie_calls import Calls

import recipe

RULES = yaml.safe_load(recipe.RULES_PATH.read_text(encoding="utf-8"))
RUNS = {r.run_id: r for r in recipe.load_runs()}
EXPECTED = recipe.load_expected()
LONG = "long-swallowed-error"


def check_data() -> None:
    assert set(RUNS) == set(EXPECTED) == {"short-handoff", "medium-off-by-one", LONG}
    for run_id, run in RUNS.items():
        assert [e["step"] for e in run.events] == list(range(len(run.events))), run_id
        root = EXPECTED[run_id]["root_step"]
        assert run.events[root]["agent"] == EXPECTED[run_id]["responsible_agent"], run_id
        assert run.events[-2]["agent"] == "terminal", run_id
        assert "FAIL" in run.events[-2]["content"], run_id


def check_routing() -> None:
    ways = {run_id: recipe.route(run, RULES, rlm_only=False) for run_id, run in RUNS.items()}
    assert ways == {"short-handoff": "whole", "medium-off-by-one": "rlm", LONG: "rlm"}, ways
    assert all(recipe.route(run, RULES, rlm_only=True) == "rlm" for run in RUNS.values())


def check_pieces() -> None:
    run = RUNS[LONG]
    budget = int(RULES["rlm"]["piece_tokens"] * RULES["chars_per_token"])
    parts = recipe.pieces(run, budget)
    assert all(len(text) <= budget for _, _, text in parts)
    assert parts[0][0] == 0
    assert parts[-1][1] == run.events[-1]["step"]
    # In order; a long step may span two pieces.
    assert all(a[1] <= b[0] for a, b in itertools.pairwise(parts))
    # Nothing dropped: the pieces hold every character (a continued step repeats its header).
    whole = "".join(recipe.render(e) for e in run.events)
    assert sum(len(t) for _, _, t in parts) >= len(whole)


def check_helpers() -> None:
    run = RUNS[LONG]
    reader = recipe.Reader(client=None, calls=Calls(), usage=recipe.Usage(), rules=RULES)
    grep, show, steps_by, _scan, _ask = recipe.make_helpers(reader, run)
    hits = grep("except ValueError", 5)
    assert hits, "grep found nothing"
    assert hits[0][0] == EXPECTED[LONG]["root_step"]
    assert hits[0][1] == "coder"
    assert show(0, 0, 40).startswith("[step 0 user, characters 0-40")
    assert show(10**6) == "No step 1000000."
    reviews = steps_by("reviewer")
    assert reviews
    assert all(run.events[s]["agent"] == "reviewer" for s in reviews)


class CutReader(recipe.Reader):
    """Answers every call as a reply that ran into the output limit."""

    def chat(self, messages: Any, max_tokens: int, response_format: Any = None) -> dict[str, Any]:
        message = {"role": "assistant", "content": "The step that"}
        return {"choices": [{"index": 0, "finish_reason": "length", "message": message}]}


def check_cut_answer() -> None:
    reader = CutReader(client=None, calls=Calls(), usage=recipe.Usage(), rules=RULES)
    ask = recipe.make_helpers(reader, RUNS[LONG])[4]
    assert ask("Which step?", [0]).endswith("[unfinished: the answer reached the output limit]")


def check_answers() -> None:
    reply = (
        'Done.\n```json\n{"root_step": 11, "responsible_agent": "coordinator", "reason": "x"}\n```'
    )
    answer = recipe.parse_answer(reply)
    assert answer == {"root_step": 11, "responsible_agent": "coordinator", "reason": "x"}
    assert recipe.parse_answer('{"root_step": "eleven"}') is None
    assert recipe.parse_answer("no json") is None
    want = EXPECTED["short-handoff"]
    right = {"root_step": want["root_step"], "responsible_agent": "Coordinator", "reason": ""}
    wrong = {"root_step": want["root_step"] + 1, "responsible_agent": "coordinator", "reason": ""}
    assert recipe.verdict(right, want) == "Pass"
    assert recipe.verdict(wrong, want) == "Miss"
    assert recipe.verdict(None, want) == "Miss"


class StandInClient:
    """Answers chat calls with a hand-written reply, to check the engine's conversion both ways."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def chat_completions(self, model: str, messages: list[dict[str, Any]], **kw: Any) -> Any:
        self.sent.append({"model": model, "messages": messages, **kw})
        message = {"role": "assistant", "content": json.dumps({"answer": "42"})}
        return {
            "id": "stand-in",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        }


def check_engine() -> None:
    client = StandInClient()
    reader = recipe.Reader(client=client, calls=Calls(), usage=recipe.Usage(), rules=RULES)
    engine = recipe.SIEEngine(reader, 256, json_object=True)
    lm = dspy.LM("sie/stand-in", engine=engine, cache=False, temperature=1.0, max_tokens=256)
    with dspy.context(lm=lm, adapter=dspy.JSONAdapter()):
        out = dspy.Predict("question -> answer")(question="6 times 7?")
    assert out.answer == "42", out
    sent = client.sent[-1]
    assert sent["model"] == RULES["models"]["reader"]
    assert sent["max_completion_tokens"] == 256
    # The signature's own schema, so SIE constrains the answer to its fields.
    assert sent["response_format"]["type"] == "json_schema"
    assert "answer" in json.dumps(sent["response_format"])
    assert sent["messages"][-1]["role"] == "user"
    assert "6 times 7?" in sent["messages"][-1]["content"]
    assert reader.usage.calls == 1
    assert reader.usage.input_tokens == 7


CHECKS = (
    check_data,
    check_routing,
    check_pieces,
    check_helpers,
    check_cut_answer,
    check_answers,
    check_engine,
)


def main() -> None:
    for check in CHECKS:
        check()
        print(f"ok  {check.__name__}")


if __name__ == "__main__":
    main()
