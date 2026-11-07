"""Check the recipe's grouping, watcher, tools, masking, note checks and agent loop offline.

The checks use hand-written strings, the committed data/ and hand-written stand-in model replies.
Nothing here calls SIE or Claude. Run it with `uv run --frozen python selfcheck.py`; it exits
non-zero if a check fails.
"""

from __future__ import annotations

import csv
import json
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
from log_patterns import pattern_of
from sie_calls import CallFailed, Calls, credits_of
from sie_sdk.client.errors import ServerError

import recipe
from recipe import AgentResult, LogLine, Odd, Runbooks, Toolbox, Watcher

RULES = recipe.load_rules()
LOGS = recipe.load_logs(recipe.DATA_DIR / "logs.csv.gz")
KNOWN = recipe.load_known(recipe.DATA_DIR / "known_patterns.csv")
DEPLOYS = recipe.load_deploys(recipe.DATA_DIR / "deploys.csv")
SECTIONS = recipe.split_sections((recipe.DATA_DIR / "runbooks.md").read_text(encoding="utf-8"))
DAY = datetime(2026, 11, 5, tzinfo=UTC)


def at(hour: int, minute: int = 0) -> datetime:
    return DAY + timedelta(hours=hour, minutes=minute)


class FakeSIE:
    """Stands in for SIEClient: masks email addresses, embeds and scores, or fails when told to."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def extract(self, model: str, item: Any, **_: Any) -> dict[str, Any]:
        if self.fail:
            raise ServerError("stand-in failure")
        return {
            "entities": [
                {"start": m.start(), "end": m.end(), "label": "person name"}
                for m in re.finditer(r"Noor Haddad", item["text"])
            ]
        }

    def encode(self, model: str, items: list[Any], **_: Any) -> list[dict[str, Any]]:
        return [{"dense": np.eye(len(SECTIONS))[0]} for _ in items]

    def score(self, model: str, query: Any, items: list[Any], **_: Any) -> dict[str, Any]:
        # The last candidate scores highest, so a result in input order would be wrong.
        return {"scores": [{"item_id": it["id"], "score": n / 100} for n, it in enumerate(items)]}


def runbooks() -> Runbooks:
    return Runbooks(SECTIONS, recipe.unit_rows(np.eye(len(SECTIONS))))


def box_for(now: datetime = DAY + timedelta(hours=10, minutes=45), client: Any = None) -> Toolbox:
    return Toolbox(
        cast(Any, client or FakeSIE()), Calls(), RULES, LOGS, DEPLOYS, runbooks(), now, "case-x"
    )


class StandInSimilar:
    """Scores like the ones the embedding model gave on this data: the reworded access log lines
    are the same event as their old wording, a new line for another region is the same as the first
    one seen, and every other new line is new."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, patterns: list[str]) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        for pattern in patterns:
            stem = re.sub(r"region \S+", "region", pattern)
            earlier = [p for p in self.seen if re.sub(r"region \S+", "region", p) == stem]
            if "returned <n> after" in pattern:
                out.append((pattern.replace("returned <n> after", "<n> in"), 0.89))
            elif earlier:
                out.append((earlier[0], 0.97))
            else:
                out.append(("payments WARN card declined for order <n>: expired card", 0.7))
        self.seen += patterns
        return out


def stand_in_similar(patterns: list[str]) -> list[tuple[str, float]]:
    return StandInSimilar()(patterns)


def check_patterns_and_data() -> None:
    assert pattern_of("auth", "WARN", "token refresh failed for user ana.ruiz7@example.com") == (
        "auth WARN token refresh failed for user <email>"
    )
    assert pattern_of("worker", "INFO", "job 0a1b2c3d finished in 120 ms") == (
        "worker INFO job <id> finished in <n> ms"
    )
    assert pattern_of("frontend", "WARN", "429 rate limited client 10.0.3.25") == (
        "frontend WARN <n> rate limited client <ip>"
    )
    assert len(LOGS) > 80000
    times = [line.time for line in LOGS]
    assert times == sorted(times)
    assert all(line.time.date() == DAY.date() for line in LOGS)
    # Every line from before today's changes groups into a pattern known from last week.
    early = {line.pattern for line in LOGS if line.time < at(9)}
    assert early <= set(KNOWN), early - set(KNOWN)
    assert {row["version"] for row in DEPLOYS} >= {"v2.14.0", "c-311"}
    ids = {section["section_id"] for section in SECTIONS}
    assert {"when-to-page", "auth-token-refresh-failures"} <= ids


def check_watcher_day() -> None:
    """The whole committed day, with stand-in similarity scores: three cases and no others."""
    watcher = Watcher(KNOWN, RULES)
    similar = StandInSimilar()
    raised: dict[str, list[Odd]] = {}
    same: set[str] = set()
    for start, lines in recipe.windows_of(LOGS, RULES["watch"]["window_minutes"]):
        window = watcher.step(start, lines, similar)
        same.update(window.same_as_known)
        if window.odd:
            raised[start.strftime("%H:%M")] = window.odd
    assert list(raised) == ["10:30", "13:00", "16:15"], list(raised)
    assert {item.reason for item in raised["10:30"]} == {"new"}
    assert len(raised["10:30"]) == 2
    assert all("warmup" in item.pattern for item in raised["13:00"])
    (spike,) = raised["16:15"]
    assert spike.reason == "rate"
    assert spike.pattern == "auth WARN token refresh failed for user <email>: upstream timeout"
    assert spike.usual is not None
    assert spike.count >= 30 * spike.usual
    # The reworded access log lines are the same event, and nothing about checkout's slow climb
    # raises a case: that's the limit the README names.
    # The two reworded access log lines, plus any warmup line for a region not in the first case.
    warmups = len(raised["13:00"])
    assert sum("returned <n> after" in p for p in same) == 2
    assert sum("warmup" in p for p in same) == 3 - warmups
    assert not any("checkout INFO" in item.pattern for odd in raised.values() for item in odd)


def check_watcher_rules() -> None:
    known = {"svc INFO ok <n>": 2.0}
    watcher = Watcher(known, RULES)

    def lines(count: int, message: str = "ok 1", minute: int = 0) -> list[LogLine]:
        return [LogLine(at(1, minute), "svc", "INFO", message) for _ in range(count)]

    # 19 lines is under the minimum, however far above usual.
    assert not watcher.step(at(1), lines(19), stand_in_similar).odd
    assert watcher.step(at(2), lines(20), stand_in_similar).odd[0].reason == "rate"
    # Quiet for two hours after raising a case, then raised again.
    assert not watcher.step(at(3), lines(40), stand_in_similar).odd
    assert watcher.step(at(4), lines(40), stand_in_similar).odd
    # A reworded line that means the same event counts toward the known pattern's rate.
    watcher = Watcher(known, RULES)

    def same(patterns: list[str]) -> list[tuple[str, float]]:
        return [("svc INFO ok <n>", 0.95) for _ in patterns]

    window = watcher.step(at(5), lines(12) + lines(12, "fine 1"), same)
    assert window.same_as_known == ["svc INFO fine <n>"]
    assert window.odd[0].count == 24
    # A really new line is raised once, and isn't compared again.
    watcher = Watcher(known, RULES)
    calls: list[list[str]] = []

    def new(patterns: list[str]) -> list[tuple[str, float]]:
        calls.append(patterns)
        return [("svc INFO ok <n>", 0.2) for _ in patterns]

    assert watcher.step(at(6), lines(1, "boom 1"), new).odd[0].reason == "new"
    assert not watcher.step(at(9), lines(1, "boom 2"), new).odd
    assert calls == [["svc INFO boom <n>"]]


def check_case_text() -> None:
    new = Odd("p ERROR x <n>", "new", 40, None, "p WARN y", 0.70, [])
    rate = Odd("a WARN z", "rate", 93, 1.79, "", 1.0, [])
    text = recipe.case_text("case-1", at(10, 30), 15, [new, rate], "line one")
    assert "raised at 2026-11-05T10:45 for the window 10:30 to 10:45" in text
    assert "similarity of 0.70. 40 lines" in text
    assert "averaged 1.79 per 15 minutes: about 52 times" in text
    assert recipe.case_prompt("x </case> y").count("</case>") == 1


def check_tools() -> None:
    box = box_for(at(16, 30))
    found = json.loads(
        box.run(
            "search_logs",
            {
                "text": "refresh failed",
                "service": "auth",
                "start_time": "16:00",
                "end_time": "23:00",
            },
        )
    )
    # The range stops at the time the case was raised, and email addresses never come back.
    assert found["to"] == "16:30"
    assert found["matching_lines"] > 50
    assert found["examples"]
    assert not any("@" in line for line in found["examples"])
    assert box.masked_counts.get("email address", 0) == len(found["examples"])
    assert len(found["examples"]) == RULES["logs"]["max_examples"]
    deploys = json.loads(box.run("get_deploys", {"start_time": "", "end_time": ""}))
    # Empty times mean the two hours before the case: 14:30 to 16:30.
    assert [row["version"] for row in deploys["deploys"]] == ["v4.0.1", "c-311"]
    for bad in ({"start_time": "17:00", "end_time": ""}, {"start_time": "25:00", "end_time": ""}):
        try:
            box.run("get_deploys", bad)
        except recipe.ToolError:
            continue
        raise AssertionError(bad)
    results = json.loads(box.run("search_runbooks", {"question": "auth"}))["results"]
    assert len(results) == RULES["search"]["return_top_k"]
    assert [r["score"] for r in results] == sorted((r["score"] for r in results), reverse=True)
    # A failed SIE call is an error Claude can read, with no detail from the server.
    failing = box_for(at(16, 30), FakeSIE(fail=True))
    message = ""
    try:
        failing.run(
            "search_logs", {"text": "refresh", "service": "", "start_time": "", "end_time": ""}
        )
    except recipe.ToolError as error:
        message = str(error)
    assert message, "a failed mask should fail the search"
    assert "stand-in" not in message
    assert failing.failed_calls == 1


def check_masking() -> None:
    text = "Noor Haddad (noor@north.example) at 2026-11-05T10:42:00"
    masked, counts = recipe.mask_personal_data(Calls(), cast(Any, FakeSIE()), "k", text, RULES)
    assert masked == "[person name] ([email address]) at 2026-11-05T10:42:00"
    assert counts == {"person name": 1, "email address": 1}

    class NoOffsets(FakeSIE):
        def extract(self, model: str, item: Any, **_: Any) -> dict[str, Any]:
            return {"entities": [{"label": "person name", "text": "Noor"}]}

    try:
        recipe.mask_personal_data(Calls(), cast(Any, NoOffsets()), "k", "I am Noor", RULES)
    except CallFailed:
        pass
    else:
        raise AssertionError("a span with no offsets should fail")


def note(claim: str, source: str, quote: str, **extra: Any) -> dict[str, Any]:
    return {
        "summary": claim,
        "likely_cause": "",
        "page_oncall": True,
        "next_step_for_oncall": "",
        "statements": [{"claim": claim, "source": source, "quote": quote}],
    } | extra


def check_note_checks() -> None:
    box = box_for()
    box.case = "Case case-x. 40 lines in this window."
    box.outputs["get_deploys"] = [
        json.dumps({"deploys": [{"version": "v2.14.0", "time": "10:40"}]})
    ]
    assert recipe.check_note(note("40 lines.", "case", "40 lines in this window"), box) == []
    assert recipe.check_note(note("Deployed at 10:40.", "get_deploys", "v2.14.0"), box) == []
    quoted = note("Page.", "when-to-page", "customers can't pay")
    assert recipe.check_note(quoted, box) == []
    made_up = note("x", "get_deploys", "v9.9.9")
    assert "word for word" in recipe.check_note(made_up, box)[0]
    assert "doesn't exist" in recipe.check_note(note("x", "slack", "y"), box)[0]
    assert "77" in str(recipe.check_note(note("77 errors.", "case", "40 lines"), box))
    claimed = note("I have rolled back payments.", "case", "40 lines")
    assert "next_step_for_oncall" in str(recipe.check_note(claimed, box))
    # The next step is where a recommendation belongs, so it isn't checked for claims.
    moved = note("40 lines.", "case", "40 lines", next_step_for_oncall="Roll back v2.14.0.")
    assert recipe.check_note(moved, box) == []


def check_statuses() -> None:
    def result(page: bool) -> AgentResult:
        return AgentResult(note=note("x", "case", "x", page_oncall=page))

    assert recipe.status_of(result(True), []) == "page_recommended"
    assert recipe.status_of(result(False), []) == "no_page_recommended"
    assert recipe.status_of(result(True), ["bad quote"]) == "check_failed"
    done = result(True)
    done.ran_out = True
    assert recipe.status_of(done, []) == "out_of_steps"
    assert recipe.status_of(AgentResult(stopped="declined"), []) == "no_note"


def block(kind: str, **fields: Any) -> SimpleNamespace:
    return SimpleNamespace(type=kind, **fields)


def use(name: str, tool_input: dict[str, Any], use_id: str = "u1") -> SimpleNamespace:
    return block("tool_use", name=name, input=tool_input, id=use_id)


def reply(stop_reason: str, *content: SimpleNamespace) -> SimpleNamespace:
    usage = SimpleNamespace(
        input_tokens=10, output_tokens=5, cache_read_input_tokens=3, cache_creation_input_tokens=2
    )
    return SimpleNamespace(stop_reason=stop_reason, content=list(content), usage=usage)


class Script:
    """Stands in for the Anthropic client: returns hand-written replies in order."""

    def __init__(self, replies: list[SimpleNamespace]) -> None:
        self.replies = replies
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))
        self.messages = SimpleNamespace(count_tokens=self.count_tokens)

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.replies.pop(0)

    def count_tokens(self, **kwargs: Any) -> SimpleNamespace:
        lines = kwargs["messages"][0]["content"].count("\n") + 1
        return SimpleNamespace(input_tokens=30 * lines)


def check_loop() -> None:
    deploy = {"start_time": "10:00", "end_time": ""}
    good = note("Deployed.", "get_deploys", "v2.14.0")
    bad = note("Deployed 99 times.", "get_deploys", "v2.14.0")
    box = box_for()
    claude = Script(
        [
            reply("tool_use", use("get_deploys", deploy, "a")),
            reply("tool_use", use("submit_note", bad, "b")),
            reply("tool_use", use("submit_note", good, "c")),
        ]
    )
    result = recipe.run_agent(cast(Any, claude), box, "prompt")
    sent = claude.requests[2]["messages"][-1]["content"][0]
    assert sent["tool_use_id"] == "b"
    assert sent["is_error"] is True
    assert "99" in sent["content"]
    assert result.note == good
    assert result.revisions == 1
    assert (result.input_tokens, result.cache_read_tokens, result.cache_write_tokens) == (30, 9, 6)
    # Text with no tool call gets one reminder, then the case ends.
    words = block("text", text="Done.")
    result = recipe.run_agent(
        cast(Any, Script([reply("end_turn", words), reply("end_turn", words)])), box_for(), "p"
    )
    assert "without calling" in result.stopped
    # Past the limit the tool refuses, and a late note still comes out as out of steps.
    limit = RULES["agent"]["max_lookups"]
    many = [use("get_deploys", deploy, f"c{n}") for n in range(limit + 1)]
    claude = Script([reply("tool_use", *many), reply("tool_use", use("submit_note", good))])
    result = recipe.run_agent(cast(Any, claude), box_for(), "p")
    assert result.lookups == limit
    assert result.ran_out
    assert recipe.status_of(result, []) == "out_of_steps"


def check_whole_case() -> None:
    lines = [LogLine(at(16, 20), "auth", "WARN", "token refresh failed for user Noor Haddad")]
    odd = [
        Odd("auth WARN token refresh failed for user Noor Haddad", "rate", 93, 1.79, "", 1.0, lines)
    ]
    window = recipe.WindowResult(at(16, 15), 900, 20, [], [], odd)
    good = note("93 lines.", "case", "93 lines in this window", page_oncall=False)
    claude = Script([reply("tool_use", use("submit_note", good))])
    box = box_for(at(16, 30))
    row = recipe.investigate(cast(Any, claude), box, window, 15)
    sent = str(claude.requests[0]["messages"])
    assert "Noor Haddad" not in sent
    assert "user [person name]" in sent
    assert row["status"] == "no_page_recommended"
    assert float(row["cost_usd"]) > 0
    # When masking fails, nothing goes to Claude.
    claude = Script([])
    failed = recipe.investigate(
        cast(Any, claude), box_for(at(16, 30), FakeSIE(fail=True)), window, 15
    )
    assert failed["status"] == "no_note"
    assert not claude.requests
    with tempfile.TemporaryDirectory() as folder:
        windows = [
            {
                "window": "16:15",
                "lines": 900,
                "patterns": 20,
                "new_patterns": 0,
                "same_as_known": 0,
                "odd": "rate",
                "case_id": "case-x",
            }
        ]
        recipe.write_outputs(Path(folder), windows, [row], "summary")
        with (Path(folder) / "cases.csv").open(encoding="utf-8", newline="") as file:
            assert next(csv.DictReader(file))["status"] == "no_page_recommended"
        assert (Path(folder) / "notes" / "case-x.md").read_text(encoding="utf-8") == row["markdown"]


def check_credits() -> None:
    charged = {"credits_debited": 1, "credits_debited_decimal": "1.500000"}
    # A batch reply repeats the request's charge on every item, so it counts once.
    assert credits_of([{"dense": [1.0], "request": charged}, {"request": charged}]) == 1.5
    assert credits_of({"scores": [], "request": {"credits_debited": 2}}) == 2.0
    assert credits_of({"entities": []}) is None
    calls = Calls()
    calls.call("m", lambda: {"ok": 1, "request": charged}, lambda r: "ok" in r, "ok")
    calls.call("m", lambda: {"ok": 1}, lambda r: "ok" in r, "ok")
    assert calls.credits == {"m": 1.5}
    assert calls.unreported == 1
    text = recipe.summary_markdown(RULES, 10, [], [], calls, 1.0)
    assert "SIE credits charged: 1.5 (m: 1.5), and 1 calls whose reply didn't say" in text


def check_cost() -> None:
    row = {
        "input_tokens": 1_000_000,
        "cache_write_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
    }
    assert recipe.claude_cost(row, RULES["prices"]["claude-opus-5-5"]) == 4.0
    per_line = recipe.tokens_per_line(cast(Any, Script([])), LOGS, RULES)
    assert per_line == 30.0
    text = recipe.summary_markdown(RULES, 1000, [], [], Calls(), per_line)
    assert "1000 lines x 30.0 = 30,000 input tokens" in text
    assert "with claude-opus-5-5 at $4.00 per million: $0.12" in text


if __name__ == "__main__":
    check_patterns_and_data()
    check_watcher_day()
    check_watcher_rules()
    check_case_text()
    check_tools()
    check_masking()
    check_note_checks()
    check_statuses()
    check_loop()
    check_whole_case()
    check_credits()
    check_cost()
    print("selfcheck passed")
