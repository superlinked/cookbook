"""Check the recipe's parsing, masking, sums, quote checks, status rules and agent loop offline.

The checks use hand-written strings, the committed data/ and hand-written stand-in Claude replies.
Nothing here calls SIE or Claude. Run it with `uv run --frozen python selfcheck.py`; it exits
non-zero if a check fails.
"""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
from billing_system import BillingSystem, charge_for, daily_summary, months_between
from ingest import MANUAL, load_rules, split_sections
from sie_calls import Calls
from sie_sdk.client.errors import ServerError
from vector_index import VectorIndex

import recipe
from recipe import AgentResult, Complaint, Toolbox

RULES = load_rules()
SYSTEM = BillingSystem.load(recipe.DATA_DIR)
SECTIONS = split_sections(MANUAL.read_text(encoding="utf-8"))
INDEX = VectorIndex.build(np.eye(len(SECTIONS)), SECTIONS)
COMPLAINTS = {c.complaint_id: c for c in recipe.load_complaints(recipe.DATA_DIR / "complaints.csv")}
PLAIN = Complaint("C-1", "A-1002", "2026-10-08", "High bill", "Why is it high?", "")
REBILL = {
    "amount": 381.30,
    "recharge_periods": [
        {"period_start": f"2026-0{month}-01", "gallons": 5000} for month in (7, 8, 9)
    ],
}


class FakeSIE:
    """Stands in for SIEClient: finds one person name, or fails when told to."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def extract(self, model: str, item: Any, **_: Any) -> dict[str, Any]:
        if self.fail:
            raise ServerError("stand-in failure")
        if "images" in item:
            text = "SERIAL AV-204817 061000 GAL PHOTO DATE 2026-10-07"
            return {"entities": [{"label": "markdown", "text": text}]}
        start = item["text"].find("Noor Haddad")
        if start < 0:
            return {"entities": []}
        return {"entities": [{"start": start, "end": start + 11, "label": "person name"}]}


def box_for(complaint: Complaint = PLAIN, client: Any = None) -> Toolbox:
    return Toolbox(cast(Any, client or FakeSIE()), Calls(), INDEX, SYSTEM, RULES, complaint)


def draft(claim: str, source: str, quote: str, **extra: Any) -> dict[str, Any]:
    statement = {"claim": claim, "source": source, "quote": quote}
    question = {"question": "Why?", "answered": True, "statements": [statement]}
    return {
        "reply": claim,
        "likely_cause": "",
        "questions": [question],
        "questions_for_customer": [],
        "next_step_for_rep": "",
        "proposed_credit": None,
    } | extra


def check_manual_and_index() -> None:
    ids = {section["section_id"] for section in SECTIONS}
    assert "6.2-excess-use-adjustment" in ids
    assert "schedule-b-limits-and-fees" in ids
    assert INDEX.nearest(np.eye(len(SECTIONS))[3], 1)[0] == SECTIONS[3]
    with tempfile.TemporaryDirectory() as folder:
        INDEX.save(Path(folder), "abc")
        assert VectorIndex.load(Path(folder), "abc") is not None
        assert VectorIndex.load(Path(folder), "changed manual") is None
        saved = VectorIndex.load(Path(folder), "abc")
        assert saved is not None
        assert saved.nearest(np.eye(len(SECTIONS))[3], 1)[0] == SECTIONS[3]
        assert VectorIndex.load(Path(folder) / "missing", "abc") is None


def check_billing() -> None:
    # The rates are Schedule A in the manual: old rates to June, new ones from July.
    assert charge_for(5000, date(2026, 6, 1))["total"] == "34.00"
    assert charge_for(5000, date(2026, 7, 1))["total"] == "37.00"
    assert charge_for(55000, date(2026, 9, 1))["total"] == "411.30"
    assert charge_for(6000, date(2026, 7, 1))["volume_charge"] == "21.00"
    schedule = next(s["text"] for s in SECTIONS if s["section_id"].startswith("schedule-a"))
    for figure in ("$18.00", "$19.50", "$3.50", "$5.20", "$8.10", "1 July 2026"):
        assert figure in schedule
    # Every bill in the data agrees with the rates.
    for rows in SYSTEM.bills.values():
        for row in rows:
            start = date.fromisoformat(row["period_start"])
            assert charge_for(int(row["gallons"]), start)["total"] == row["amount"]
    assert months_between(date(2025, 8, 10), date(2026, 10, 8)) == 13
    assert months_between(date(2025, 8, 10), date(2026, 10, 10)) == 14


def check_readings() -> None:
    rows = [
        {"hour": "2026-09-01T02", "gallons": "5.0"},
        {"hour": "2026-09-01T03", "gallons": "3.0"},
        {"hour": "2026-09-01T07", "gallons": "90.0"},
    ]
    assert daily_summary(rows, [1, 2, 3, 4]) == [
        {"date": "2026-09-01", "gallons": "98", "lowest_overnight_gallons_per_hour": "3.0"}
    ]
    box = box_for()
    # The leak on A-1002 shows overnight from the 12th, while nobody is home.
    days = box.get_readings("2026-09-01", "2026-09-30")["days"]
    night = {day["date"]: float(day["lowest_overnight_gallons_per_hour"]) for day in days}
    assert night["2026-09-08"] == 0
    assert night["2026-09-13"] > 0
    assert sum(int(day["gallons"]) for day in days) in range(39990, 40011)
    manual = Complaint("C-2", "A-1001", "2026-10-08", "s", "b", "")
    assert "manually read" in box_for(manual).get_readings("2026-09-01", "2026-09-30")["note"]
    for bad in (("2026-01-01", "2026-09-30"), ("2026-09-30", "2026-09-01"), ("Sept", "2026-09-01")):
        try:
            box.get_readings(*bad)
        except recipe.ToolError:
            continue
        raise AssertionError(bad)


def check_figures_and_quotes() -> None:
    found = recipe.figures("Bill $411.30. 55,000 gal, 24 months, step 1. Ends 2026-09-12.", 2)
    assert found == {"411.30", "55000", "24", "2026", "09", "12"}
    assert recipe.figures("75.0 gallons and 40.50", 2) == {"75", "40.50"}
    box = box_for()
    box.outputs["get_account"] = ['{"gallons": "40000", "amount": "289.80"}']
    section = "6.2-excess-use-adjustment"
    good = draft("The bill was 289.80.", "get_account", "289.80")
    assert recipe.check_draft(good, box, "") == []
    # A quote may differ by line breaks and marks, not by words.
    words = "sudden failure of private plumbing"
    assert recipe.check_draft(draft("Yes.", section, f"a **{words}**"), box, "") == []
    made_up = draft("Yes.", section, "any leak at all qualifies")
    assert "word for word" in recipe.check_draft(made_up, box, "")[0]
    nowhere = draft("Yes.", "section-99", "anything")
    assert "doesn't exist" in recipe.check_draft(nowhere, box, "")[0]
    # A quote from a tool the agent never called fails too.
    uncalled = draft("Yes.", "calculate_bill", "289.80")
    assert "word for word" in recipe.check_draft(uncalled, box, "")[0]
    # A figure from nowhere fails, in a claim and in the reply; one the customer gave is fine.
    invented = draft("You owe 512.00.", "get_account", "289.80")
    assert len(recipe.check_draft(invented, box, "")) == 2
    theirs = draft("Your bill of 290 was 289.80.", "get_account", "289.80")
    assert recipe.check_draft(theirs, box, "How is this $290?") == []
    bare = draft("Yes.", "get_account", "289.80")
    bare["questions"][0]["statements"] = []
    assert "no source" in recipe.check_draft(bare, box, "")[0]


def check_action_claims() -> None:
    # Sentences Claude really wrote, and ones that are fine. The curly quote is on purpose.
    for sentence in (
        "I have passed your question on for review.",
        "I\u2019ve flagged your message so the right team can look at it.",
        "I've noted your request so it can be sent to the right people.",
        "We'll look into it.",
        "Your question has been forwarded.",
    ):
        found = recipe.action_claims(sentence, RULES["checks"]["action_claims"])
        assert found, sentence
    for sentence in (
        "I looked at your account, and your last six bills show the same use.",
        "We read your meter on 2026-09-01.",
        "Our rules don't say who handles damage claims.",
        "Please send the plumber's invoice.",
    ):
        assert recipe.action_claims(sentence, RULES["checks"]["action_claims"]) == [], sentence
    box = box_for()
    box.outputs["get_account"] = ['{"amount": "289.80"}']
    said = draft("I have passed this on.", "get_account", "289.80")
    assert "next_step_for_rep" in recipe.check_draft(said, box, "")[0]
    # The field for the rep is where that belongs, so it isn't checked.
    moved = draft("Billed 289.80.", "get_account", "289.80", next_step_for_rep="Passed it on.")
    assert recipe.check_draft(moved, box, "") == []


def check_credit() -> None:
    manual = Complaint("C-2", "A-1001", "2026-10-08", "s", "b", "x.jpg")
    box = box_for(manual)
    box.outputs["get_account"] = ['{"amount": "411.30"}']
    box.outputs["calculate_bill"] = ['{"total": "37.00"}']
    # Billed 40.50 + 40.50 + 411.30, recharged 37.00 x 3.
    right = draft("Billed 411.30.", "get_account", "411.30", proposed_credit=REBILL)
    assert recipe.check_draft(right, box, "") == []
    wrong = dict(REBILL, amount=500.0)
    assert "works out 381.3" in str(
        recipe.check_draft(
            draft("Billed 411.30.", "get_account", "411.30", proposed_credit=wrong), box, ""
        )
    )
    unchecked = {"amount": 50.0, "recharge_periods": []}
    assert "can't be checked" in str(
        recipe.check_draft(draft("x", "get_account", "411.30", proposed_credit=unchecked), box, "")
    )
    no_bill = {"amount": 50.0, "recharge_periods": [{"period_start": "2020-01-01", "gallons": 1}]}
    assert "no such bill" in str(
        recipe.check_draft(draft("x", "get_account", "411.30", proposed_credit=no_bill), box, "")
    )
    box.serial_matches = False
    assert "serial" in str(recipe.check_draft(right, box, ""))


def check_photo() -> None:
    box = box_for(COMPLAINTS["C-2001"])
    result = box.read_meter_photo()
    assert result["serial_matches_account"] is True
    assert box.serial_matches is True
    other = box_for(COMPLAINTS["C-2006"])
    assert other.read_meter_photo()["serial_matches_account"] is False
    try:
        box_for().read_meter_photo()
    except recipe.ToolError:
        return
    raise AssertionError("a complaint with no photo should fail")


def check_masking() -> None:
    text = "I'm Noor Haddad (noor@north.example, +1 555 0142) with 40,000 gallons on 2026-09-01."
    masked, counts = recipe.mask_personal_data(Calls(), cast(Any, FakeSIE()), "k", text, RULES)
    assert masked == (
        "I'm [person name] ([email address], [phone number]) with 40,000 gallons on 2026-09-01."
    )
    assert counts == {"person name": 1, "email address": 1, "phone number": 1}

    # The model can tag a meter reading as a phone number. It stays readable.
    class Reading(FakeSIE):
        def extract(self, model: str, item: Any, **_: Any) -> dict[str, Any]:
            return {"entities": [{"start": 0, "end": 6, "label": "phone number"}]}

    reading = "061000 GAL"
    assert (
        recipe.mask_personal_data(Calls(), cast(Any, Reading()), "k", reading, RULES)[0] == reading
    )
    assert recipe.replace_spans("abcdef", [(1, 3, "x"), (2, 5, "y")]) == ("a[x]f", {"x": 1})
    # A failed call means nothing goes to Claude.
    claude = Script([])
    row = recipe.answer_complaint(cast(Any, claude), box_for(client=FakeSIE(fail=True)))
    assert row["status"] == "no_draft"
    assert not claude.requests


def check_statuses() -> None:
    def result(*answered: bool, needed: tuple[bool, ...] = ()) -> AgentResult:
        questions = [{"answered": a} for a in answered]
        asks = [{"question": "Which?", "needed_before_anyone_can_act": n} for n in needed]
        return AgentResult(draft={"questions": questions, "questions_for_customer": asks})

    assert recipe.status_of(result(True, True), []) == "draft_ready"
    assert recipe.status_of(result(True, False), []) == "partly_answered"
    assert recipe.status_of(result(True, needed=(True, False)), []) == "partly_answered"
    # A question that's only useful to know doesn't move the status.
    assert recipe.status_of(result(True, needed=(False,)), []) == "draft_ready"
    assert recipe.status_of(result(False), []) == "not_in_policy"
    assert recipe.status_of(result(True), ["bad quote"]) == "check_failed"
    done = result(True)
    done.ran_out = True
    assert recipe.status_of(done, []) == "out_of_steps"
    assert recipe.status_of(AgentResult(stopped="declined"), []) == "no_draft"


def block(kind: str, **fields: Any) -> SimpleNamespace:
    return SimpleNamespace(type=kind, **fields)


def use(name: str, tool_input: dict[str, Any], use_id: str = "u1") -> SimpleNamespace:
    return block("tool_use", name=name, input=tool_input, id=use_id)


def reply(stop_reason: str, *content: SimpleNamespace) -> SimpleNamespace:
    usage = SimpleNamespace(
        input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0
    )
    return SimpleNamespace(stop_reason=stop_reason, content=list(content), usage=usage)


class Script:
    """Stands in for the Anthropic client: returns hand-written replies in order."""

    def __init__(self, replies: list[SimpleNamespace]) -> None:
        self.replies = replies
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.replies.pop(0)


def loop(replies: list[SimpleNamespace]) -> tuple[AgentResult, Script]:
    claude = Script(replies)
    return recipe.run_agent(cast(Any, claude), box_for(), "prompt"), claude


def check_markdown() -> None:
    final = draft("Billed 289.80.", "get_account", "289.80", next_step_for_rep="Find the owner.")
    final["questions_for_customer"] = [
        {"question": "Send the invoice.", "needed_before_anyone_can_act": True},
        {"question": "When were you away?", "needed_before_anyone_can_act": False},
    ]
    text = recipe.draft_markdown(PLAIN, "partly_answered", AgentResult(draft=final), [], {})
    assert "**Next step for the rep:** Find the owner." in text
    assert "## Still needed from the customer\n\n- Send the invoice." in text
    assert "## Worth asking the customer\n\n- When were you away?" in text


def check_loop() -> None:
    final = draft("Billed 289.80.", "get_account", "289.80")
    # A lookup, then a draft. The result goes back with the id of the call it answers.
    result, claude = loop(
        [
            reply("tool_use", use("get_account", {}, "a")),
            reply("tool_use", use("submit_draft", final, "b")),
        ]
    )
    assert result.draft == final
    assert result.tool_calls == 2
    assert not result.ran_out
    sent = claude.requests[1]["messages"][-1]["content"][0]
    assert sent["tool_use_id"] == "a"
    assert "289.80" in sent["content"]
    # A bad call comes back as an error Claude can read, not a crash.
    result, claude = loop(
        [
            reply("tool_use", use("get_readings", {"start_date": "soon", "end_date": "later"})),
            reply("tool_use", use("get_account", {})),
            reply("tool_use", use("submit_draft", final)),
        ]
    )
    assert claude.requests[1]["messages"][-1]["content"][0]["is_error"] is True
    # A draft with a problem goes back once, and the fixed one is accepted.
    bad = draft("Billed 512.00.", "get_account", "289.80")
    result, claude = loop(
        [
            reply("tool_use", use("get_account", {}, "a")),
            reply("tool_use", use("submit_draft", bad, "b")),
            reply("tool_use", use("submit_draft", final, "c")),
        ]
    )
    sent = claude.requests[2]["messages"][-1]["content"][0]
    assert sent["tool_use_id"] == "b"
    assert sent["is_error"] is True
    assert "512" in sent["content"]
    assert result.draft == final
    assert result.revisions == 1
    assert result.first_review
    assert recipe.status_of(result, result.problems) == "draft_ready"
    # A second bad draft isn't sent back again: it comes out check_failed.
    result, claude = loop(
        [
            reply("tool_use", use("get_account", {}, "a")),
            reply("tool_use", use("submit_draft", bad, "b")),
            reply("tool_use", use("submit_draft", bad, "c")),
        ]
    )
    assert result.draft == bad
    assert len(claude.requests) == 3
    assert recipe.status_of(result, result.problems) == "check_failed"
    # A refusal ends the complaint with no draft.
    result, _ = loop([reply("refusal")])
    assert result.draft is None
    assert "declined" in result.stopped
    # Text with no tool call gets one reminder, then the complaint ends.
    words = block("text", text="Done.")
    result, claude = loop([reply("end_turn", words), reply("end_turn", words)])
    assert "without calling" in result.stopped
    assert len(claude.requests) == 2
    # Past the limit the tool refuses, and a late draft still comes out as out of steps.
    limit = RULES["agent"]["max_lookups"]
    many = [use("get_account", {}, f"c{number}") for number in range(limit + 1)]
    result, claude = loop([reply("tool_use", *many), reply("tool_use", use("submit_draft", final))])
    assert result.lookups == limit
    assert result.tool_calls == limit + 1
    assert result.ran_out
    assert claude.requests[1]["messages"][-1]["content"][-1]["is_error"] is True
    assert recipe.status_of(result, []) == "out_of_steps"
    # A loop that never submits stops at the limit on requests.
    rounds = RULES["agent"]["max_rounds"]
    result, claude = loop(
        [reply("tool_use", use("get_account", {}, f"d{n}")) for n in range(rounds)]
    )
    assert result.draft is None
    assert result.ran_out
    assert len(claude.requests) == rounds


def check_data() -> None:
    assert len(COMPLAINTS) == 10
    for complaint in COMPLAINTS.values():
        assert complaint.account_id in SYSTEM.accounts
        if complaint.attachment:
            assert (recipe.DATA_DIR / complaint.attachment).is_file()
    assert recipe.csv_safe("=SUM(A1)") == "'=SUM(A1)"


if __name__ == "__main__":
    check_manual_and_index()
    check_billing()
    check_readings()
    check_figures_and_quotes()
    check_action_claims()
    check_credit()
    check_photo()
    check_masking()
    check_statuses()
    check_markdown()
    check_loop()
    check_data()
    print("selfcheck passed")
