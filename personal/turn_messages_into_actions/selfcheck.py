"""Check the recipe's lookups, date and bill rules, reconciliation and SIE error rule offline.

The checks use the committed data/, hand-written messages and hand-written stand-in model
replies. Nothing here calls SIE or replays a recorded response.
Run it with `uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import copy
import io
import tempfile
from collections.abc import Callable
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from calendar_app import Calendar
from contacts import Contacts
from payees import Payees
from sie_sdk import SIEClient
from sie_sdk.client.errors import (
    AccountInactiveError,
    InputTooLongError,
    InsufficientCreditsError,
    ModelLoadFailedError,
    ModelLoadingError,
    ProvisioningError,
    RateLimitError,
    RequestError,
    ServerError,
    SIEConnectionError,
    SIEError,
    SpendLimitError,
)

import recipe

RULES = recipe.load_rules()
MODELS = RULES["models"]
LABELS = list(RULES["action_types"])
ZONE = ZoneInfo(RULES["person"]["time_zone"])
CALENDAR, CONTACTS, PAYEES = Calendar(), Contacts(), Payees()
SENT = datetime(2026, 10, 27, 21, 15, tzinfo=ZONE)  # a Tuesday evening


def probs(top: str, score: float = 0.8, second: str = "", runner: float = 0.0) -> dict[str, float]:
    """A probability for every action type: the top one, an optional runner-up, the rest even."""
    given = {top: score} | ({second: runner} if second else {})
    rest = [label for label in LABELS if label not in given]
    share = (1.0 - sum(given.values())) / len(rest)
    return {label: given.get(label, share) for label in LABELS}


def decide_reply(p: dict[str, float]) -> dict[str, Any]:
    return {
        "data": {recipe.QUESTION_ID: {"choice": max(p, key=lambda k: p[k]), "probabilities": p}}
    }


EMPTY = ServerError(recipe.EMPTY_RESULT_ERROR, status_code=500)

BILL_TEXT = """# Bayview Water & Power
PO Box 4410, San Francisco, CA 94120
Account number 4417-2290-31
Previous balance $79.40
Payment received Oct 2 - thank you -$79.40
Total new charges $84.17
Amount due $84.17
Due date November 12, 2026
Please pay by the due date to avoid a $5.00 late fee."""


class StandIn:
    """Stands in for SIEClient. `script[message_id]` holds what each model answers for that
    message: "type" (probabilities, a raw reply or an exception), "fields" (a dict, a raw reply
    with "raw", or an exception), "transcript" and "ocr" (text or an exception). A call nobody
    planned for is a check failure."""

    def __init__(self, script: dict[str, dict[str, Any]]) -> None:
        self.script = copy.deepcopy(script)
        self.calls: list[tuple[str, str]] = []
        self.options: list[tuple[str, dict[str, Any]]] = []  # (model, options) per call
        self.windows: list[int] = []  # items per Decide call

    def _answer(self, message_id: str, what: str) -> Any:
        assert what in self.script.get(message_id, {}), f"no {what} planned for {message_id}"
        answer = self.script[message_id].pop(what)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def extract(self, model: str, item: Any, **kwargs: Any) -> Any:
        self.options.append((model, kwargs.get("options") or {}))
        if model == MODELS["action_type"]:
            message_id = item[0]["id"].rsplit("#", 1)[0]
            self.calls.append((model, message_id))
            self.windows.append(len(item))
            answer = self._answer(message_id, "type")
            if isinstance(answer, dict) and set(answer) == set(LABELS):
                return [decide_reply(answer) for _ in item]
            return answer
        _kind, message_id = item["id"].split("/")[:2]
        self.calls.append((model, message_id))
        if model == MODELS["fields"]:
            answer = self._answer(message_id, "fields")
            return answer.get("raw", {"data": answer})
        if model == MODELS["transcribe"]:
            text = self._answer(message_id, "transcript")
            return {"data": {"segments": [{"text": text}]}}
        assert model == MODELS["ocr"], model
        return {"entities": [{"label": "markdown", "text": self._answer(message_id, "ocr")}]}

    def unused(self) -> list[str]:
        return [f"{m}:{k}" for m, plan in self.script.items() for k in plan]


def msg(message_id: str, body: str, sender: str = "Maya Lindqvist", **extra: Any) -> dict[str, Any]:
    return {
        "id": message_id,
        "sent": extra.pop("sent", SENT.isoformat()),
        "channel": extra.pop("channel", "email"),
        "from": sender,
        "subject": extra.pop("subject", ""),
        "body": body,
        **extra,
    }


def sweep(
    records: list[dict[str, Any]], script: dict[str, dict[str, Any]], rules: dict[str, Any] = RULES
) -> tuple[recipe.Result, StandIn]:
    client = StandIn(script)
    messages = recipe.parse_messages(records, rules)
    with contextlib.redirect_stdout(io.StringIO()):  # the run's "1 of 2 failed" line
        result = recipe.run(cast(SIEClient, client), rules, messages)
    assert not client.unused(), f"planned replies left unused: {client.unused()}"
    return result, client


def by_message(result: recipe.Result) -> dict[str, recipe.Proposal]:
    return {s["message"]: p for p in result.proposals for s in p.sources}


def one(body: str, kind: str, fields: dict[str, Any] | BaseException, **extra: Any) -> Any:
    """The proposal (or no_action item) for one hand-written message."""
    script = {"m": {"type": probs(kind), "fields": fields}}
    if kind in RULES["no_action_types"]:
        del script["m"]["fields"]
    result, _ = sweep([msg("m", body, **extra)], script)
    return result.proposals[0] if result.proposals else result.no_action[0]


def stopped(call: Callable[[], object]) -> str:
    try:
        call()
    except recipe.SIEStopped as error:
        return str(error)
    raise AssertionError("the run did not stop")


def raised(call: Callable[[], object]) -> BaseException:
    try:
        call()
    except BaseException as error:  # SystemExit too: the check is on which one it was
        return error
    raise AssertionError("nothing was raised")


# --- The planted inbox, end to end with stand-in replies -------------------------------------

PLANTED: dict[str, dict[str, Any]] = {
    "msg-01": {"type": probs("schedule"), "fields": {"what": "parent-teacher conference"}},
    "msg-02": {
        "type": probs("schedule"),
        "fields": {"what": "Practice", "when": "this Saturday at 9 AM", "where": "Rossi Field"},
    },
    "msg-03": {
        "type": probs("schedule"),
        "fields": {"what": "teeth cleaning", "when": "Tuesday, November 3 at 10:30 AM"},
    },
    "msg-14": {
        "type": probs("schedule"),
        "fields": {"what": "Leo's teeth cleaning", "when": "Tuesday, November 3 at 11:15 AM"},
    },
    "msg-04": {"type": probs("reply"), "fields": {"what": "the plumber", "due_date": "Friday"}},
    "msg-05": {"type": probs("fyi")},
    "msg-06": {
        "transcript": "Note to self. Renew the car registration online before Friday.",
        "type": probs("todo"),
        "fields": {"what": "Renew the car registration", "when": "before Friday"},
    },
    "msg-07": {"type": probs("schedule"), "fields": {"what": "coffee", "when": "next week"}},
    "msg-08": {
        "ocr": BILL_TEXT,
        "type": probs("pay"),
        "fields": {"amount": "$84.17", "due_date": "November 12, 2026"},
    },
    # Decide reads "Book a vet appointment" as a booking; with no time it is a question.
    "msg-09": {
        "type": probs("schedule", 0.85),
        "fields": {"what": "Book a vet appointment", "due_date": "November 20th"},
    },
    # The reminder: unsure between schedule and fyi, and it repeats msg-03.
    "msg-10": {
        "type": probs("schedule", 0.45, "fyi", 0.40),
        "fields": {"what": "cleaning", "when": "next Tuesday at 10:30am"},
    },
    "msg-11": {"type": probs("todo"), "fields": {"what": "pick up Leo", "when": "tomorrow at 3"}},
    "msg-12": {"type": probs("fyi")},
    "msg-13": {"type": probs("change"), "fields": {"what": "practice", "when": "11 AM"}},
    "msg-15": {"type": probs("change"), "fields": EMPTY},
}


def check_planted() -> None:
    result, client = sweep(recipe.load_inbox(), PLANTED)
    found = by_message(result)
    status = {m: p.status for m, p in found.items()}
    assert status["msg-01"] == recipe.MATCHES_EXISTING
    assert found["msg-01"].existing is not None
    assert found["msg-01"].existing["calendar"] == "cal-04"
    # The dentist: one hold with both sources; Leo's (same sender, same day) is its own hold.
    assert found["msg-03"] is found["msg-10"], "the reminder repeats msg-03"
    assert status["msg-03"] == recipe.CHECKS_PASSED, found["msg-03"].questions
    assert found["msg-14"] is not found["msg-03"], "Leo's cleaning must not merge with Jordan's"
    assert status["msg-14"] == recipe.CHECKS_PASSED
    assert found["msg-14"].start == datetime(2026, 11, 3, 11, 15, tzinfo=ZONE)
    # Practice: the change points at Oct 31 (cal-01), not the Nov 7 practice (cal-05).
    assert found["msg-02"] is found["msg-13"]
    change = found["msg-13"]
    assert change.status == recipe.CHECKS_PASSED, change.questions
    assert change.change is not None
    assert change.change["calendar"] == "cal-01"
    assert change.change["to"] == "2026-10-31T11:00:00-07:00"
    assert [s["message"] for s in change.sources] == ["msg-02", "msg-13"]
    # The cancellation: removes the shift; nothing is held at the quoted 10 AM.
    cancel = found["msg-15"]
    assert cancel.status == recipe.CHECKS_PASSED, cancel.questions
    assert cancel.change is not None
    assert cancel.change["cancel"] is True
    assert cancel.change["calendar"] == "cal-06"
    assert "10 AM" not in cancel.sources[0]["text"], "the source is never the quoted original"
    assert found["msg-04"].due == date(2026, 10, 30)
    assert found["msg-06"].due == date(2026, 10, 30)
    assert status["msg-09"] == recipe.QUESTION
    assert found["msg-09"].questions == [
        "What is the date and time for 'Book a vet appointment'?"
    ], found["msg-09"].questions
    assert found["msg-09"].start is None, "no time is invented"
    assert status["msg-07"] == recipe.QUESTION
    bill = found["msg-08"]
    assert bill.status == recipe.CHECKS_PASSED, bill.questions
    assert bill.amount == Decimal("84.17")
    assert bill.payee == "Bayview Water & Power"
    assert found["msg-11"].due == date(2026, 10, 30)
    assert found["msg-11"].due_at == time(15, 0)
    assert {i["message"] for i in result.no_action} == {"msg-05", "msg-12"}
    # Only the two new holds go to events.ics.
    ics = recipe.events_ics(result, recipe.newest(result))
    assert ics.count("BEGIN:VEVENT") == 2, ics
    # msg-09 has the phone's transcript, so only msg-06 is sent to speech to text.
    assert [m for model, m in client.calls if model == MODELS["transcribe"]] == ["msg-06"]
    # Sent-time order, not file order: the reversed export gives the same result.
    again, _ = sweep(list(reversed(recipe.load_inbox())), PLANTED)
    assert [p.as_json() for p in again.proposals] == [p.as_json() for p in result.proposals]
    print("planted: every case as the README says, in any file order ok")


# --- Lookups ------------------------------------------------------------------------------------


def check_lookups() -> None:
    assert CONTACTS.find_contact("brightsmile  dental").status == "found"
    assert CONTACTS.find_contact("Dana").status == "possible_match"
    assert CONTACTS.find_contact("Dana").contact is None, "a close name is not the contact"
    assert CONTACTS.find_contact("Ellery").status == "several"
    assert CONTACTS.find_contact("Brightsmile Dental Billing").status == "none"
    # Only an exact contact counts as who sent it; a close name stays the sender's own name.
    assert recipe.who_key("Coach Dana Ramos", CONTACTS)[0] == "c-03"
    who, note = recipe.who_key("Dana", CONTACTS)
    assert who == "sender:dana"
    assert "maybe Coach Dana Ramos" in note
    # A payee whose name sits inside a longer payee's name counts once, as the longer one.
    seed = Path(tempfile.mkdtemp())
    (seed / "payees.csv").write_text(
        "payee_id,name,aliases,account_number,autopay,balance,due_date\n"
        "p-01,Bayview Water & Power,,,no,,\n"
        "p-09,Bayview Water,,,no,,\n",
        encoding="utf-8",
    )
    assert [p.payee_id for p in Payees(seed).find_in_text(BILL_TEXT)] == ["p-01"]
    assert [p.payee_id for p in Payees(seed).find_in_text("Bayview Water bill")] == ["p-09"]
    # Payees: whole names only; the near-miss Bayview payee never matches the water bill.
    assert [p.payee_id for p in PAYEES.find_in_text(BILL_TEXT)] == ["p-01"]
    assert PAYEES.find_in_text("Bayview services: $40 due") == []
    assert [p.payee_id for p in PAYEES.find_in_text("BAYVIEW WATER AND POWER")] == ["p-01"]
    two = PAYEES.find_in_text("Bayview Water & Power and Bayview Waste Collection")
    assert [p.payee_id for p in two] == ["p-01", "p-02"]
    assert recipe.has_account("Acct 4417 2290 31", "4417-2290-31")
    assert not recipe.has_account("Acct 4417-2290-311", "4417-2290-31")
    # Calendar: instants, across the DST change on Nov 1.
    oct31 = [e.event_id for e in CALENDAR.events_on(date(2026, 10, 31), ZONE)]
    assert oct31 == ["cal-01"]
    nov7 = [e.event_id for e in CALENDAR.events_on(date(2026, 11, 7), ZONE)]
    assert nov7 == ["cal-05", "cal-06"]
    print("lookups: contacts, payees and calendar match honestly ok")


# --- Reading: quoted history, attachments --------------------------------------------------------


def check_reading() -> None:
    body = "Sorry, it's off.\n\nOn Mon, Oct 26 Sam wrote:\n> See you Saturday at 10 AM"
    new, quoted = recipe.split_quoted(body)
    assert new == "Sorry, it's off."
    assert quoted.startswith("On Mon")
    assert recipe.split_quoted("Hi\n> quoted\nmore") == ("Hi", "> quoted\nmore")
    assert recipe.split_quoted("-----Original Message-----\nold")[0] == ""
    # A cancellation whose only date and time are in the quoted original: no hold, ever.
    body = "This is cancelled, sorry!\n\nOn Mon, Oct 26 Sam wrote:\n> Lunch Thursday at 1 PM"
    item = one(body, "schedule", {"what": "Lunch"}, sender="Sam Ellery")
    assert item.status == recipe.QUESTION
    assert item.start is None
    # An unreadable photo is a question even when the type reads as a confident fyi.
    photo = "bill-1028-bayview-water-power.png"
    script = {"m": {"ocr": "   ", "type": probs("fyi")}}
    result, _ = sweep([msg("m", "", channel="photo", attachment=photo, sender="me")], script)
    assert not result.no_action
    assert result.proposals[0].status == recipe.QUESTION
    assert "could not read" in result.proposals[0].questions[0]
    # A missing attachment file is a question too, not a crash.
    script = {"m": {"type": probs("todo")}}
    result, _ = sweep([msg("m", "", channel="photo", attachment="gone.png", sender="me")], script)
    assert "could not open gone.png" in result.proposals[0].questions[0]
    assert recipe.plain_text("<td>Amount due</td><td>$84.17</td></tr>") == "Amount due $84.17"
    assert recipe.plain_text("Bayview Water &amp; Power") == "Bayview Water & Power"
    caught = raised(lambda: recipe.parse_messages([msg("a", "x"), msg("a", "y")], RULES))
    assert isinstance(caught, ValueError)
    assert "repeat" in str(caught)
    print("reading: quoted history kept out, unreadable attachments asked about ok")


# --- Dates, amounts, the bill --------------------------------------------------------------------


def check_dates() -> None:
    def when(text: str, sent: datetime = SENT) -> recipe.When:
        return recipe.resolve(text, sent, RULES)

    assert when("Friday") == recipe.When(date(2026, 10, 30))
    assert when("tomorrow at 3") == recipe.When(date(2026, 10, 28), time(15, 0))
    assert when("Tuesday, November 3 at 10:30am").at == time(10, 30)
    assert when("at 3pm today").at == time(15, 0)
    assert when("sometime next week").problem
    assert when("November 3 or November 5").problem, "two dates in one span"
    assert when("November 3 at 10 AM ET").problem, "another time zone"
    assert when("November 3 at 10 AM PT").problem is None
    assert when("Tuesday").problem, "sent on a Tuesday: today or in a week?"
    monday = datetime(2026, 10, 26, 9, 0, tzinfo=ZONE)
    assert when("next Wednesday", monday).problem
    assert when("Monday, November 4").problem, "Nov 4 2026 is a Wednesday"
    print("dates: relative, glued, vague, two-date and other-zone spans ok")


def check_bill() -> None:
    def bill(amount: str, text: str = BILL_TEXT) -> tuple[str | None, str]:
        return recipe.check_bill(Decimal(amount), text, RULES)

    assert bill("84.17")[0] is None
    assert "Checked" in bill("84.17")[1]
    assert bill("5.00")[0], "the late fee is not the amount due"
    no_line = bill("84.17", BILL_TEXT.replace("Amount due $84.17\n", ""))[0]
    assert no_line is not None
    assert "found no amount due line" in no_line
    partial = BILL_TEXT.replace("Total new charges $84.17\n", "")
    assert bill("84.17", partial)[0], "a summary with a line missing"
    wrong = BILL_TEXT.replace("Previous balance $79.40", "Previous balance $70.40")
    assert bill("84.17", wrong)[0], "a summary that doesn't add up"
    assert recipe.amounts("-$79.40 and $-12.00, then $1,084.17") == [
        Decimal("-79.40"),
        Decimal("-12.00"),
        Decimal("1084.17"),
    ]
    # A credit stays negative and is a question, never a payment.
    credit = BILL_TEXT.replace("Amount due $84.17", "Amount due -$12.00")
    item = one(credit, "pay", {"amount": "-$12.00", "due_date": "November 12, 2026"}, sender="me")
    assert item.status == recipe.QUESTION
    assert item.amount is None
    # One amount span holding two amounts is flagged, not cut down to the first.
    two = {"amount": "$84.17 and $5.00", "due_date": "November 12, 2026"}
    two_text = BILL_TEXT + "\nTotal: $84.17 and $5.00"
    assert one(two_text, "pay", two, sender="me").status == recipe.QUESTION
    print("bill: amount due line, summary, late fee, credits and two-amount spans ok")


# --- Reconciliation ------------------------------------------------------------------------------


def check_reconcile() -> None:
    # A bill from a payee on autopay matches what's set up.
    autopay = "Fogline Mobile. Account 8812-4470. Amount due $64.00. Due date November 15, 2026."
    item = one(autopay, "pay", {"amount": "$64.00", "due_date": "November 15, 2026"})
    assert item.status == recipe.MATCHES_EXISTING, item.questions
    # The online account disagrees with the bill: a question.
    online = "Harbor Mutual. Account HM-220-1185. Amount due $120.00. Due date November 20, 2026."
    item = one(online, "pay", {"amount": "$120.00", "due_date": "November 20, 2026"})
    assert item.status == recipe.QUESTION
    assert "online account says $112.50" in item.questions[0]
    # The right payee, but another account number: a question.
    other = BILL_TEXT.replace("4417-2290-31", "4417-2290-39")
    item = one(other, "pay", {"amount": "$84.17", "due_date": "November 12, 2026"}, sender="me")
    assert item.status == recipe.QUESTION
    # An unknown payee is a question.
    unknown = "Amount due $30.00. Due date November 12, 2026."
    item = one(unknown, "pay", {"amount": "$30.00", "due_date": "November 12, 2026"})
    assert item.status == recipe.QUESTION
    # A hold that overlaps a calendar event is a question.
    body = "Can we meet Tuesday, November 3 at 2:30 PM?"
    item = one(body, "schedule", {"what": "meet", "when": "Tuesday, November 3 at 2:30 PM"})
    assert item.status == recipe.QUESTION
    assert "Design review" in item.questions[-1]
    # A move the model read as a new event: a question, not a second practice that day.
    body = "Practice this Saturday at 11 AM, same field."
    item = one(body, "schedule", {"what": "Practice", "when": "this Saturday at 11 AM"})
    assert item.status == recipe.QUESTION
    assert "Falcons soccer practice" in item.questions[0]
    # A date already past when the message came: a question.
    lunch = {"what": "Lunch", "when": "October 20, 2026 at noon"}
    item = one("Lunch October 20, 2026 at noon", "schedule", lunch)
    assert item.status == recipe.QUESTION
    assert "already past" in item.questions[0]
    # The same date with no year is a question too, never a hold a year from now.
    item = one("Lunch October 20 at noon", "schedule", {"what": "Lunch", "when": "October 20"})
    assert item.status == recipe.QUESTION
    assert item.start is None
    # A change that matches nothing is a question; so is one that fits two events.
    item = one("Yoga moves to 7 PM on Friday.", "change", {"what": "Yoga", "when": "7 PM"})
    assert item.status == recipe.QUESTION
    body = "Saturday's practice and book fair shift are cancelled."
    script = {"m": {"type": probs("change"), "fields": {"when": "Saturday"}}}
    nov1 = datetime(2026, 11, 1, 9, 0, tzinfo=ZONE).isoformat()
    result, _ = sweep([msg("m", body, "Hillcrest PTA", sent=nov1)], script)
    assert result.proposals[0].status == recipe.QUESTION
    assert "more than one event" in result.proposals[0].questions[0]
    # A reminder whose doubt is with a real action (reply) keeps the doubt.
    records = [
        msg("a", "Your cleaning is Tuesday, November 3 at 10:30 AM.", "Brightsmile Dental"),
        msg("b", "Cleaning next Tuesday at 10:30am. Can you confirm?", "Brightsmile Dental"),
    ]
    script = {
        "a": {
            "type": probs("schedule"),
            "fields": {"what": "cleaning", "when": "Tuesday, November 3 at 10:30 AM"},
        },
        "b": {
            "type": probs("schedule", 0.45, "reply", 0.40),
            "fields": {"what": "Cleaning", "when": "next Tuesday at 10:30am"},
        },
    }
    result, _ = sweep(records, script)
    assert len(result.proposals) == 1
    assert result.proposals[0].status == recipe.QUESTION, "a reply-shaped doubt stays"
    # Same day, same time, different sender: two holds, not one.
    records[1] = msg("b", "Cleaning on Tuesday, November 3 at 10:30 AM.", "Theo Marsh")
    script["b"] = {
        "type": probs("schedule"),
        "fields": {"what": "Cleaning", "when": "Tuesday, November 3 at 10:30 AM"},
    }
    result, _ = sweep(records, script)
    assert len(result.proposals) == 2
    # Negation: "no need to reply" is a question, not a reply to do.
    item = one("No need to reply, just letting you know.", "reply", {"what": "letting you know"})
    assert item.status == recipe.QUESTION
    # A type split between two actions is a question.
    script = {"m": {"type": probs("todo", 0.40, "reply", 0.35), "fields": {"what": "x"}}}
    result, _ = sweep([msg("m", "Could you pick up x and tell me?")], script)
    assert result.proposals[0].status == recipe.QUESTION
    # Two windows giving two dates for one field: a question, not "the first one wins".
    long_body = " ".join(["filler"] * 200)
    script = {
        "m": {
            "type": probs("reply"),
            "fields": {"what": "x", "due_date": "Friday"},
        }
    }
    windows = recipe.word_windows(
        f"From: Maya Lindqvist\n\nReply by Friday. {long_body} Reply by Monday.",
        RULES["thresholds"]["field_window_words"],
        RULES["thresholds"]["field_window_overlap_words"],
    )
    assert len(windows) == 2
    client = StandIn({})
    fields_iter = iter([{"data": {"due_date": "Friday"}}, {"data": {"due_date": "Monday"}}])
    client.extract = lambda model, item, **_: next(fields_iter)  # type: ignore[method-assign]
    found = recipe.extract_fields(
        cast(SIEClient, client),
        recipe.Calls(),
        "m",
        f"From: Maya Lindqvist\n\nReply by Friday. {long_body} Reply by Monday.",
        RULES,
    )
    assert any("two due dates" in q for q in found.questions), found.questions
    print("reconcile: payees, clashes, changes, reminders, negation and splits ok")


# --- Review fixes: confident statuses that must not come out -------------------------------------


def check_review_fixes() -> None:
    def when(text: str, sent: datetime = SENT) -> recipe.When:
        return recipe.resolve(text, sent, RULES)

    # A time range starts at its first number, with the end's am or pm; "11-1 PM" is a question.
    assert when("Tuesday, November 3 from 1 to 3 PM").at == time(13, 0)
    assert when("Tuesday, November 3, 2-4 PM").at == time(14, 0)
    assert when("Tuesday, November 3, 2:30-4 pm").at == time(14, 30)
    assert when("Tuesday, November 3, 11-1 PM").problem
    assert when("November 3 - 4 PM").at == time(16, 0), "a day and a time, not a range"
    assert when("practice this Saturday moves from 9 to 11 AM").problem, "a move, not a range"
    assert when("practice this Saturday moves from 9 AM to 11 AM").problem, "old and new time"
    assert when("Practice from 9 AM to 11 AM this Saturday.").at == time(9, 0), "a range, no move"
    body = "Practice moves from 9 AM to 11 AM this Saturday."
    item = one(
        body, "change", {"what": "Practice", "when": "this Saturday"}, sender="Coach Dana Ramos"
    )
    assert item.status == recipe.QUESTION, (item.status, item.questions)
    # Two weekdays in one sentence: which day is a question, not the first in week order.
    assert when("Monday's practice moves to Friday at 5 PM").problem
    body = "Monday's practice moves to Friday at 5 PM."
    item = one(body, "change", {"what": "practice", "when": "5 PM"}, sender="Coach Dana Ramos")
    assert item.status == recipe.QUESTION, item.questions
    assert item.change is None
    # A negated cancel word does not cancel.
    assert not recipe.has_cancel("Saturday's practice is NOT cancelled.", RULES)
    assert not recipe.has_cancel("Practice won\u2019t be called off.", RULES)
    assert not recipe.has_cancel("Saturday's practice is not going to be cancelled.", RULES)
    assert not recipe.has_cancel("Practice isn't going to be called off.", RULES)
    assert recipe.has_cancel("Not this week, but practice is cancelled.", RULES)
    assert recipe.has_cancel("Practice is no longer on.", RULES)
    item = one(
        "Saturday's practice is not cancelled.",
        "change",
        {"what": "practice"},
        sender="Coach Dana Ramos",
    )
    assert item.status == recipe.QUESTION, item.questions
    assert not item.cancel
    # The fields call refuses an over-long window instead of cutting it (InputTooLongError is a
    # question, see check_sie_failure).
    _, client = sweep([msg("m", "Reply by Friday?")], {"m": {"type": probs("reply"), "fields": {}}})
    assert (MODELS["fields"], {"overflow_policy": "error"}) in client.options, client.options
    # A change from someone not in the contacts, who didn't set up the event: a question.
    item = one("Saturday's practice is cancelled.", "change", {}, sender="Pat Unknown")
    assert item.status == recipe.QUESTION, item.questions
    assert "not in your contacts" in item.questions[0], item.questions
    # A same-start event sharing only a name is not the same event (Leo's cleaning at 5 PM is
    # not "Leo swim lesson"): it overlaps, a question.
    body = "Leo's teeth cleaning is Wednesday, November 4 at 5 PM."
    fields = {"what": "Leo's teeth cleaning", "when": "Wednesday, November 4 at 5 PM"}
    item = one(body, "schedule", fields, sender="Brightsmile Dental")
    assert item.status == recipe.QUESTION, item.status
    assert "overlaps Leo swim lesson" in item.questions[0], item.questions
    # One sender, one start, but another person or another thing: two holds, not one.
    at = "Tuesday, November 3 at 9 AM"
    for second, what in (("Leo's teeth cleaning", "teeth cleaning"), ("Flu shot", "flu shot")):
        records = [
            msg("a", f"Your teeth cleaning is {at}.", "Brightsmile Dental"),
            msg("b", f"{second} is {at}.", "Brightsmile Dental"),
        ]
        script = {
            "a": {"type": probs("schedule"), "fields": {"what": "teeth cleaning", "when": at}},
            "b": {"type": probs("schedule"), "fields": {"what": what, "when": at}},
        }
        result, _ = sweep(records, script)
        assert len(result.proposals) == 2, (second, [p.sources for p in result.proposals])
    # A carriage return can't break an events.ics line; a model span can't break todo.md.
    assert "\r" not in recipe._ics_text("a\r\nb\rc")
    result, _ = sweep(
        [msg("m", f"Your teeth cleaning is {at}.", "Brightsmile Dental")],
        {"m": {"type": probs("schedule"), "fields": {"what": "teeth  \n cleaning", "when": at}}},
    )
    todo = recipe.todo_md(result, "Jordan")
    assert "- [ ] teeth cleaning, Tue Nov 3 9:00 AM (m)" in todo, todo
    print("review fixes: ranges, two days, negated cancel, strangers, names and spans ok")


def check_coverage() -> None:
    """Rules the planted inbox doesn't reach, one hand-written case each."""
    # Doubt from the score alone, and from the margin alone.
    for p in (probs("todo", 0.45, "reply", 0.10), probs("todo", 0.55, "reply", 0.45)):
        result, _ = sweep([msg("m", "Pick up x?")], {"m": {"type": p, "fields": {"what": "x"}}})
        assert result.proposals[0].status == recipe.QUESTION, p
    assert (
        recipe.type_question(recipe.TypeReading("todo", probs("todo", 0.55, "reply", 0.38)), RULES)
        is None
    )
    # A bill needs its due date.
    item = one(BILL_TEXT, "pay", {"amount": "$84.17"}, sender="me")
    assert "What is the due date" in " ".join(item.questions), item.questions
    # A due date already past when the bill came, two payees, the online account's due date.
    late = {"amount": "$84.17", "due_date": "October 20, 2026"}
    item = one(BILL_TEXT.replace("November 12", "October 20"), "pay", late, sender="me")
    assert "before it reached you" in " ".join(item.questions), item.questions
    two = BILL_TEXT + "\nBayview Waste Collection account BWC-55120"
    item = one(two, "pay", {"amount": "$84.17", "due_date": "November 12, 2026"}, sender="me")
    assert "more than one payee" in item.questions[0], item.questions
    harbor = "Harbor Mutual. Account HM-220-1185. Amount due $112.50. Due date November 21, 2026."
    item = one(harbor, "pay", {"amount": "$112.50", "due_date": "November 21, 2026"})
    assert "online account says due 2026-11-20" in item.questions[0], item.questions
    # A bill with only its amount due line: no summary to add up, and that's said.
    question, note = recipe.check_bill(Decimal("84.17"), "Amount due $84.17", RULES)
    assert question is None
    assert "prints no summary" in note
    # Two windows giving two times or two amounts: a question.
    long_body = " ".join(["filler"] * 200)
    for name, first, second in (
        ("when", "Friday at 9 AM", "Monday at 9 AM"),
        ("amount", "$5", "$6"),
    ):
        replies = iter([{"data": {name: first}}, {"data": {name: second}}])
        client = StandIn({})
        client.extract = lambda model, item, r=replies, **_: next(r)  # type: ignore[method-assign]
        text = f"From: x\n\n{first}. {long_body} {second}."
        found = recipe.extract_fields(cast(SIEClient, client), recipe.Calls(), "m", text, RULES)
        assert any(f"two {name}s" in q for q in found.questions), (name, found.questions)
    # A change to a time that overlaps another event; a change to the time it already has.
    nov1 = datetime(2026, 11, 1, 9, 0, tzinfo=ZONE).isoformat()
    body = "Practice on Saturday, November 7 moves to 10:30 AM."
    fields = {"what": "Practice", "when": "10:30 AM"}
    item = one(body, "change", fields, sender="Coach Dana Ramos", sent=nov1)
    assert "overlaps Book fair volunteer shift" in " ".join(item.questions), item.questions
    body = "Practice this Saturday moves to 9 AM."
    item = one(body, "change", {"what": "Practice", "when": "9 AM"}, sender="Coach Dana Ramos")
    assert item.status == recipe.MATCHES_EXISTING, item.questions
    # A cancellation that names a time cancels only the event at that time.
    for at, expected in (("2 PM", recipe.QUESTION), ("10 AM", recipe.CHECKS_PASSED)):
        body = f"The {at} book fair shift on Saturday is cancelled."
        fields = {"what": "book fair shift", "when": at}
        item = one(body, "change", fields, sender="Hillcrest PTA", sent=nov1)
        assert item.status == expected, (at, item.questions)
    # A to-do that says there's nothing to do.
    item = one("No need to pick up the dry cleaning.", "todo", {"what": "pick up the dry cleaning"})
    assert item.status == recipe.QUESTION
    # A change that names no day is looked for `change_search_days` ahead.
    body = "The parent-teacher conference moves to 5 PM."
    script = {"m": {"type": probs("change"), "fields": {"what": "conference", "when": "5 PM"}}}
    records = [msg("m", body, "Hillcrest Elementary Office")]
    assert sweep(records, script)[0].proposals[0].status == recipe.CHECKS_PASSED
    short = copy.deepcopy(RULES)
    short["thresholds"]["change_search_days"] = 5
    assert sweep(records, script, short)[0].proposals[0].status == recipe.QUESTION
    # Stop words don't count when comparing names.
    assert recipe.words("your teeth cleaning appointment", RULES) == {"teeth", "cleaning"}
    # A long message goes to Decide in windows of `type_window_words`, in one call.
    narrow = copy.deepcopy(RULES)
    narrow["thresholds"]["type_window_words"] = 10
    text = "Please reply. " + " ".join(["word"] * 20)  # 25 words with the From line
    script = {"m": {"type": probs("reply"), "fields": {"what": "x"}}}
    _, client = sweep([msg("m", text)], script, narrow)
    assert client.windows == [3], client.windows
    print("coverage: doubts, bill rules, two values, change rules, search window and windows ok")


# --- The SIE error rule --------------------------------------------------------------------------

TRANSIENT: list[SIEError] = [
    SIEConnectionError("connection refused"),
    ServerError("unavailable", status_code=503),
    RateLimitError("slow down"),
    InputTooLongError("too long"),
    ProvisioningError("no GPU yet"),
    ModelLoadingError("loading"),
    ModelLoadFailedError("try later", permanent=False),
    SIEError("an SIE error with no subclass"),
]
FATAL: list[SIEError] = [
    RequestError("bad key", status_code=401),
    RequestError("no such model", status_code=404),
    InsufficientCreditsError("no credits"),
    SpendLimitError("spend limit"),
    AccountInactiveError("inactive"),
    ModelLoadFailedError("gated", permanent=True),
]


def check_sie_failure() -> None:
    for error in TRANSIENT:
        assert not recipe.is_fatal(error), f"{type(error).__name__} should be transient"
    for error in FATAL:
        assert recipe.is_fatal(error), f"{type(error).__name__} should stop the run"
    ok: dict[str, Any] = {
        "type": probs("reply"),
        "fields": {"what": "the plumber", "due_date": "Friday"},
    }
    records = [msg("a", "Reply by Friday?"), msg("b", "Reply by Friday please?")]
    # A transient failure on one message: it becomes a question; the other is fine.
    for error in TRANSIENT:
        result, _ = sweep(records, {"a": {"type": error}, "b": copy.deepcopy(ok)})
        found = by_message(result)
        assert found["a"].status == recipe.QUESTION
        assert "the SIE call failed" in found["a"].questions[0], found["a"].questions
        assert found["b"].status == recipe.CHECKS_PASSED
    # A failed field call is a question, even for a reply that needs no field.
    result, _ = sweep(records, {"a": {"type": probs("reply"), "fields": TRANSIENT[0]}, "b": ok})
    assert by_message(result)["a"].status == recipe.QUESTION
    assert "the SIE call failed" in by_message(result)["a"].questions[0]
    # A reply missing its key is a failed call: no probabilities, or no `data`.
    script = {"a": {"type": [{"data": {}}]}, "b": copy.deepcopy(ok)}
    assert by_message(sweep(records, script)[0])["a"].status == recipe.QUESTION
    # ... or probabilities that leave a type out, or one outside 0 to 1.
    missing = {k: v for k, v in probs("reply").items() if k != "pay"}
    too_big = probs("reply") | {"reply": 1.5}
    for bad in (missing, too_big):
        script = {"a": {"type": [decide_reply(bad)]}, "b": copy.deepcopy(ok)}
        result, _ = sweep(records, script)
        assert by_message(result)["a"].status == recipe.QUESTION
        assert len(result.calls.failures) == 1
    script = {"a": {"type": probs("reply"), "fields": {"raw": {"result": {}}}}, "b": ok}
    assert by_message(sweep(records, script)[0])["a"].status == recipe.QUESTION
    # An empty list of fields is a real "nothing found" (a question for a reply), and SIE
    # Cloud's empty-result 500 is the same, not a failed call.
    empties: list[Any] = [{}, EMPTY]
    for empty in empties:
        result, _ = sweep(records[:1], {"a": {"type": probs("reply"), "fields": empty}})
        item = result.proposals[0]
        assert item.questions == ["I found no details in this message. What does it need?"]
        assert result.calls.failures == []

    def stops(script: dict[str, dict[str, Any]]) -> str:
        return stopped(lambda: sweep(records, script))

    # Every call failed: the run stops with advice, and main writes nothing.
    message = stops(
        {"a": {"type": SIEConnectionError("down")}, "b": {"type": SIEConnectionError("down")}}
    )
    assert message.startswith("Every SIE call failed (2 of 2"), message
    assert "Check SIE_BASE_URL and your network." in message
    assert "No outputs were written." in message
    busy = ServerError("unavailable", status_code=503)
    message = stops({"a": {"type": busy}, "b": {"type": busy}})
    assert "may still be starting" in message
    # A refusal stops the run at once with SIE's full message.
    long = "no credits " + "y" * 300
    message = stops({"a": {"type": InsufficientCreditsError(long)}})
    assert message.startswith(f"SIE refused the call to {MODELS['action_type']}, ")
    assert "y" * 300 in message
    message = stops({"a": {"type": ModelLoadFailedError("gated", permanent=True)}})
    assert message.startswith(f"SIE can't load {MODELS['action_type']}")
    message = stops({"a": {"type": RequestError("unexpected body")}})
    assert "SIE_BASE_URL" in message
    assert "SIE_API_KEY" not in message
    # A bug in our own code is not hidden, and main turns only SIEStopped into a clean exit.
    caught = raised(lambda: sweep(records, {"a": {"type": TypeError("bad argument")}}))
    assert type(caught) is TypeError, type(caught)

    def refusal() -> None:
        raise recipe.SIEStopped("SIE refused")

    caught = raised(lambda: recipe.stop_on_refusal(refusal))
    assert type(caught) is SystemExit
    assert str(caught) == "Stopped: SIE refused"
    print("sie failure: transient errors give questions; refusals and all-failed stop ok")


# --- Outputs -------------------------------------------------------------------------------------


def check_outputs() -> None:
    assert recipe.STATUSES == ("checks_passed", "question", "matches_existing", "no_action")
    for old in ("proposed", "approved", "ready"):
        assert old not in recipe.STATUSES
    result, _ = sweep(recipe.load_inbox(), PLANTED)
    folder = Path(tempfile.mkdtemp())
    recipe.write_outputs(folder, result, RULES)
    todo = (folder / "todo.md").read_text(encoding="utf-8")
    assert todo.index("## Questions for you") < todo.index("## New calendar holds")
    assert "quote" not in todo.casefold()
    saved = (folder / "proposals.json").read_text(encoding="utf-8")
    assert '"quote"' not in saved
    assert '"checks_passed": 8' in saved, saved[:300]
    assert '"question": 2' in saved, saved[:300]
    print("outputs: four finding statuses, questions first, no 'quote' ok")


if __name__ == "__main__":
    check_planted()
    check_lookups()
    check_reading()
    check_dates()
    check_bill()
    check_reconcile()
    check_review_fixes()
    check_coverage()
    check_sie_failure()
    check_outputs()
