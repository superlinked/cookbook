"""Offline checks on the masking, the form parsing, the decision checks and the leak check.

No SIE or Claude call: the model replies below are hand-written stand-ins.

    uv run selfcheck.py
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from server import (
    RESUMES_DIR,
    Store,
    every_occurrence,
    load_applicants,
    load_rules,
    mask,
    model_spans,
    phone_pattern,
    replace_spans,
    require_token,
    resume_lines,
    sie_tool_error,
    tables_to_lines,
    valid_entity,
    word_windows,
)
from sie_calls import CallFailed, Calls, SIEStopped

from recipe import (
    Screening,
    all_leaks,
    check_decision,
    csv_safe,
    expected_decision,
    identity_leaks,
    masked_text,
    plain,
    report_markdown,
    status_of,
)

logging.getLogger("screening").setLevel(logging.ERROR)  # the stand-in failures log warnings
RULES = load_rules()
APPLICANTS = load_applicants()


def check_data() -> None:
    for key, row in APPLICANTS.items():
        assert (RESUMES_DIR / row["resume_file"]).is_file(), f"{key}: no resume file"
        assert Path(row["resume_file"]).name == row["resume_file"], f"{key}: resume_file is a path"
    assert set(RULES["role"]["must_have"]) == {
        "epa_608_universal",
        "commercial_3_years",
        "drivers_license",
    }


def check_replace_spans() -> None:
    text = "marcus.bell@example.com"
    # The name part and the email start together; the longer one names the span.
    masked, counts = replace_spans(text, [(0, 6, "person"), (0, len(text), "email")])
    assert masked == "[email]", masked
    assert counts == {"email": 1}, counts
    masked, _ = replace_spans("ab cd ef", [(3, 5, "x"), (0, 2, "y")])
    assert masked == "[y] [x] ef", masked


def check_record_masking() -> None:
    record = APPLICANTS["A-105"]
    text = "TOMAS HERRERA\nCall Tomas at 555.013.7764 or (555) 013-7764.\nHerrera Plumbing, 2015"
    masked, counts = mask(text, record, RULES, found=[])
    assert "tomas" not in masked.lower(), masked
    assert "herrera" not in masked.lower(), masked
    assert "7764" not in masked, masked
    assert counts["phone"] == 2, counts
    # The record's own patterns are case-blind and whole-word only.
    bell = APPLICANTS["A-101"]
    assert mask("Campbell Mechanical, Bellevue", bell, RULES, found=[])[0] == (
        "Campbell Mechanical, Bellevue"
    )
    # A phone number with no separators is caught only by the record's own pattern.
    masked, counts = mask("Cell 5550137764", record, RULES, found=[])
    assert "7764" not in masked, masked
    assert counts == {"phone": 1}, counts
    assert re.search(phone_pattern("(555) 014-2290"), "5550142290")
    assert not re.search(phone_pattern("(555) 014-2290"), "15550142290")
    # Masking with the record alone leaves none of the applicant's details in a text resume.
    for key, row in APPLICANTS.items():
        if row["resume_file"].endswith(".txt"):
            text = (RESUMES_DIR / row["resume_file"]).read_text(encoding="utf-8")
            masked, _ = mask(text, row, RULES, found=[])
            assert identity_leaks(masked, row, RULES) == [], key


def check_model_spans_spread() -> None:
    """A name the model found once is masked everywhere it appears."""
    record = APPLICANTS["A-104"]
    text = (
        "References: Hal Brenner, Facilities Manager, (555) 019-4400\n"
        "Worked for Hal Brenner for two years."
    )
    start = text.index("Hal Brenner")
    stand_in = [(start, start + len("Hal Brenner"), "person")]
    masked, counts = mask(text, record, RULES, found=stand_in)
    assert "Brenner" not in masked, masked
    assert counts["person"] == 2, counts
    assert counts["phone"] == 1, counts


def check_forms_and_lines() -> None:
    html = (
        "# FORM\n<table>\n  <tr>\n    <td>NAME</td>\n    <td>[person]</td>\n  </tr>\n"
        "  <tr>\n    <td>EPA 608 CERTIFICATION</td>\n"
        "    <td>Yes - Type I, Type II and Type III</td>\n"
        "  </tr>\n</table>"
    )
    lines = resume_lines(tables_to_lines(html))
    assert lines == [
        "# FORM",
        "NAME: [person]",
        "EPA 608 CERTIFICATION: Yes - Type I, Type II and Type III",
    ], lines
    wrapped = (
        "- Run maintenance routes: rooftop units, make-up air\n  units and two chillers.\n- Next"
    )
    assert resume_lines(wrapped) == [
        "- Run maintenance routes: rooftop units, make-up air units and two chillers.",
        "- Next",
    ]


MASKED = """[person]
CERTIFICATIONS
- EPA Section 608 Universal certification
- Ohio driver's license, clean record
Lead Service Technician, Crestline Mechanical 2021 - present
- Two 60-ton air-cooled chillers and **rooftop units**"""


def decision(**changes: object) -> dict[str, object]:
    base: dict[str, object] = {
        "applicant_id": "A-101",
        "decision": "advance",
        "must_haves": [
            {
                "requirement_id": "epa_608_universal",
                "verdict": "met",
                "quote": "EPA Section 608 Universal certification",
            },
            {
                "requirement_id": "commercial_3_years",
                "verdict": "met",
                "quote": "Two 60-ton air-cooled chillers and rooftop units",
            },
            {
                "requirement_id": "drivers_license",
                "verdict": "met",
                "quote": "Ohio driver's license, clean record",
            },
        ],
        "note": "Meets every must-have.",
    }
    return base | changes


def check_decisions() -> None:
    assert check_decision(decision(), "A-101", MASKED, RULES) == []
    assert plain("**Rooftop  units**") == "rooftop units"
    wrong_quote = decision(
        must_haves=[
            {
                "requirement_id": "epa_608_universal",
                "verdict": "met",
                "quote": "608 Universal, 2015",
            },
            *decision()["must_haves"][1:],  # type: ignore[index]
        ]
    )
    problems = check_decision(wrong_quote, "A-101", MASKED, RULES)
    assert any("isn't in the resume text" in p for p in problems), problems
    unclear = decision(
        must_haves=[
            *decision()["must_haves"][:2],  # type: ignore[index]
            {"requirement_id": "drivers_license", "verdict": "unclear", "quote": ""},
        ]
    )
    problems = check_decision(unclear, "A-101", MASKED, RULES)
    assert problems == ["With these verdicts the decision is needs_person, not advance."], problems
    missing = decision(must_haves=decision()["must_haves"][:1])  # type: ignore[index]
    assert any("missing" in p for p in check_decision(missing, "A-101", MASKED, RULES))
    assert check_decision(decision(), "A-101", "", RULES) == [
        "No resume was read, so no quote can be checked. Call read_resume first."
    ]
    must_haves: list[dict[str, str]] = decision()["must_haves"]  # type: ignore[assignment]
    first, rest = must_haves[0], must_haves[1:]
    no_quote = decision(must_haves=[{**first, "quote": " "}, *rest])
    assert check_decision(no_quote, "A-101", MASKED, RULES) == [
        "epa_608_universal is met but has no quote."
    ]
    assert check_decision(decision(applicant_id="A-102"), "A-101", MASKED, RULES) == [
        "The decision is for 'A-102', not A-101."
    ]
    twice = decision(must_haves=[{**first, "verdict": "not_met"}, first, *rest])
    assert check_decision(twice, "A-101", MASKED, RULES) == [
        "Give exactly one verdict per must-have; a must-have appears twice."
    ]
    not_met = decision(must_haves=[{**first, "verdict": "not_met"}, *rest])
    assert check_decision(not_met, "A-101", MASKED, RULES) == [
        "With these verdicts the decision is reject, not advance."
    ]
    assert plain("Ohio driver\u2019s license \u2013 clean") == "ohio driver's license - clean"
    assert expected_decision(["met", "unclear", "not_met"]) == "reject"
    assert expected_decision(["met", "unclear"]) == "needs_person"
    assert expected_decision(["met", "met"]) == "advance"


def check_leaks() -> None:
    record = APPLICANTS["A-101"]
    assert identity_leaks(MASKED, record, RULES) == []
    assert identity_leaks("Call MARCUS on 555 014 2290", record, RULES) == ["name", "phone"]
    assert identity_leaks("reach marcus.bell.hvac@example.com", record, RULES) == ["email", "name"]
    assert identity_leaks("MARCUS.BELL.HVAC@EXAMPLE.COM", record, RULES) == ["email", "name"]
    assert identity_leaks("Campbell Mechanical, Bellevue", record, RULES) == []
    assert identity_leaks("Márcus did rooftop units", record, RULES) == ["name"]
    assert identity_leaks("anything", {**record, "email": "", "phone": ""}, RULES) == []
    # Tool results arrive as JSON: a name at the start of a line, after an encoded newline, and
    # a decision note with an accent must still be found.
    result = json.dumps({"text": "[person]\nMarcus did rooftop units"})
    seen = Screening("A-101", events=[{"kind": "tool", "result": result}]).seen()
    assert identity_leaks(seen, record, RULES) == ["name"]
    note = Screening("A-101", decisions=[{"note": "Ask Marcus\u0301 about 5550142290"}]).seen()
    assert identity_leaks(note, record, RULES) == ["name", "phone"]
    # Another applicant's details are leaks too, named by their id.
    assert all_leaks("Priya led installs", "A-101", APPLICANTS, RULES) == ["A-103:name"]


class StandIn:
    """A stand-in SIE client: pii-redact finds the given names; `bad` adds a span it can't place."""

    def __init__(self, names: list[str], score: float = 0.9, bad: bool = False) -> None:
        self.names, self.score, self.bad = names, score, bad

    def extract(self, model: str, item: Any, **kwargs: Any) -> dict[str, Any]:
        text = item["text"]
        found = [
            {"start": at, "end": at + len(name), "label": "person", "score": self.score}
            for name in self.names
            if (at := text.find(name)) >= 0
        ]
        if self.bad:
            found.append({"start": 0, "end": 10**6, "label": "person"})
        return {"entities": found}


def check_windows_and_spans() -> None:
    assert word_windows("a b c d e f g", 3, 1) == [(0, "a b c"), (4, "c d e"), (8, "e f g")]
    assert word_windows("  ", 3, 1) == []
    text = "x  y\nz w v"
    assert all(text[at : at + len(w)] == w for at, w in word_windows(text, 2, 0))
    assert valid_entity("abc", {"start": 0, "end": 3, "label": "person"})
    assert valid_entity("abc", {"start": 0, "end": 3, "label": "person", "text": "abc"})
    assert not valid_entity("abc", {"start": 0, "end": 3, "label": "person", "text": "xyz"})
    assert not valid_entity("abc", {"start": 0, "end": 4, "label": "person"})
    assert not valid_entity("abc", {"start": 2, "end": 2, "label": "person"})
    assert not valid_entity("abc", {"start": 0, "end": 3})
    # A name in a later window lands at its place in the whole text.
    small = copy.deepcopy(RULES)
    small["masking"].update(window_words=4, window_overlap_words=1)
    text = "one two three four five six Hal Brenner seven"
    found = model_spans(Calls(), StandIn(["Hal Brenner"]), "k", text, small)
    assert {text[a:b] for a, b, _ in found} == {"Hal Brenner"}, found
    low = StandIn(["Hal Brenner"], score=0.1)
    assert model_spans(Calls(), low, "k", text, small) == []
    try:
        model_spans(Calls(), StandIn([], bad=True), "k", text, small)
        raise AssertionError("a span it can't place should fail")
    except CallFailed:
        pass
    # Short values aren't spread; a value with a trailing space still is.
    assert every_occurrence("Al Hal. Al.", [(0, 2, "person")], 3) == [(0, 2, "person")]
    text = "Ref: Hal Brenner \nHal Brenner, manager"
    masked, _ = replace_spans(text, every_occurrence(text, [(5, 17, "person")], 3))
    assert "Brenner" not in masked, masked


def check_store_boundary() -> None:
    """A failed mask returns an error with no resume text, and caches nothing."""
    store = Store(RULES, APPLICANTS, StandIn([], bad=True))
    message = ""
    try:
        store.resume("A-101")
    except ToolError as error:
        message = str(error)
    assert message, "a failed mask should be an error"
    assert "Marcus" not in message, message
    assert store.resumes == {}
    store = Store(RULES, APPLICANTS, StandIn([]))
    resume = store.resume(" A-101 ")
    assert identity_leaks(resume.masked, APPLICANTS["A-101"], RULES) == []
    assert store.resume("A-101") is resume
    # An SIE message can echo the request, so the tool error never carries it.
    cause = ValueError("input: 'Marcus Bell, HVAC'")
    for failure in (CallFailed("x"), SIEStopped("SIE refused the call, input: 'Marcus Bell'")):
        failure.__cause__ = cause
        message = str(sie_tool_error("read A-101's resume", failure))
        assert "Marcus" not in message, message
    stopped = SIEStopped("x")
    assert str(sie_tool_error("read A-101's resume", stopped)).startswith("SIE refused")


def check_token() -> None:
    async def hit(path: str, headers: list[tuple[bytes, bytes]], kind: str = "http") -> Any:
        sent: list[dict[str, Any]] = []
        reached: list[str] = []

        async def inner(scope: Any, receive: Any, send: Any) -> None:
            reached.append(path)

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Any) -> None:
            sent.append(message)

        scope = {"type": kind, "path": path, "headers": headers, "method": "GET"}
        await require_token(inner, "s3cret")(scope, receive, send)
        status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
        return bool(reached), status

    def run(path: str, headers: list[tuple[bytes, bytes]], kind: str = "http") -> Any:
        return asyncio.run(hit(path, headers, kind))

    assert run("/mcp", [(b"authorization", b"Bearer s3cret")]) == (True, None)
    assert run("/mcp", []) == (False, 401)
    assert run("/mcp", [(b"authorization", b"Bearer s3cre")]) == (False, 401)
    assert run("/mcp", [(b"authorization", b"s3cret")]) == (False, 401)
    assert run("/healthz", []) == (True, None)
    assert run("/healthz/", []) == (False, 401)
    assert run("/mcp", [], kind="websocket")[0] is False  # refused before the app
    assert run("/", [], kind="lifespan") == (True, None)


def check_outputs() -> None:
    for value in ["=HYPERLINK(1)", "+1", "-1", "@SUM(A1)", "\tx", "\rx"]:
        assert csv_safe(value) == "'" + value
    assert csv_safe("advance") == "advance"
    quoted = {"requirement_id": "a", "verdict": "met", "quote": "x | y\nz"}
    report = report_markdown(Screening("A-101", decisions=[decision(must_haves=[quoted])]), "x", [])
    assert "| a | met | x \\| y z |" in report, report
    assert status_of(Screening("x")) == "no_decision"
    assert status_of(Screening("x", out_of_turns=True)) == "out_of_turns"
    assert status_of(Screening("x", decisions=[decision()], problems=["p"])) == "check_failed"
    assert status_of(Screening("x", decisions=[decision(decision="reject")])) == "reject"
    assert masked_text('{"text": "[person]"}') == "[person]"
    assert masked_text("Error: x") == ""


def main() -> None:
    check_data()
    check_replace_spans()
    check_record_masking()
    check_model_spans_spread()
    check_forms_and_lines()
    check_decisions()
    check_leaks()
    check_windows_and_spans()
    check_store_boundary()
    check_token()
    check_outputs()
    print("selfcheck passed")


if __name__ == "__main__":
    main()
