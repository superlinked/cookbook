"""Check the recipe's escalation, field and routing rules offline.

The checks use hand-written strings, the committed data/ and hand-written stand-in model
replies. Nothing here calls SIE or replays a recorded response. Run it with
`uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import csv
import functools
import io
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

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
CATALOG = recipe.load_catalog()
EMAILS = recipe.load_emails()
TEAM_MODEL, FIELD_MODEL = RULES["models"]["team"], RULES["models"]["fields"]
GENERAL = RULES["general_queue"]


def email(text: str, email_id: str = "X") -> recipe.Email:
    return recipe.Email(email_id, "customer@mail.example", "", text)


def probabilities(top: str, score: float) -> dict[str, Any]:
    """A Decide reply giving ``top`` the score and the rest split evenly."""
    rest = (1.0 - score) / (len(CATALOG.teams) - 1)
    probs = {team: (score if team == top else rest) for team in CATALOG.teams}
    return {"data": {recipe.QUESTION_ID: {"label": top, "probabilities": probs}}}


class Stand:
    """Stands in for SIEClient. Each call to a model raises that model's next listed error,
    or, for None, answers every window: no spans from the field model, and the team model
    picking ``returns-refunds`` at 0.90. ``replies`` overrides one window's reply by id."""

    def __init__(
        self,
        errors: dict[str, list[Exception | None]] | None = None,
        replies: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.errors = {model: list(queue) for model, queue in (errors or {}).items()}
        self.replies = replies or {}
        self.calls: list[str] = []

    def __enter__(self) -> Stand:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def extract(self, model: str, items: list[Any], **_: Any) -> list[dict[str, Any]]:
        self.calls.append(model)
        queue = self.errors.get(model) or []
        error = queue.pop(0) if queue else None
        if error is not None:
            raise error
        default = (
            {"entities": []} if model == FIELD_MODEL else probabilities("returns-refunds", 0.9)
        )
        return [self.replies.get(f"{model}:{item['id']}", default) for item in items]


def route(
    emails: list[recipe.Email], client: Stand, rules: dict[str, Any] = RULES
) -> list[recipe.Route]:
    screened = recipe.screen(emails, rules)
    with contextlib.redirect_stdout(io.StringIO()):  # the run's "1 of 2 failed" line
        return recipe.route_emails(cast(SIEClient, client), emails, screened, CATALOG, rules)


def check_escalations() -> None:
    def names(text: str) -> list[str]:
        return [name for name, _, _ in recipe.escalations(email(text), RULES)]

    assert names("The heater CAUGHT   fire") == ["safety"]
    assert names("We sat by the fireplace") == []
    assert names("Sue at the store was helpful") == []
    assert names("I will sue you") == ["legal"]
    # Both flags are kept, safety first; legal goes in also_send_to.
    both = recipe.screen([email("He was injured and we called a lawyer.")], RULES)["X"]
    assert (both.team, both.method) == ("product-safety", "escalation")
    assert both.escalations == ["safety", "legal"]
    assert both.also_send_to == ["legal-escalations"]
    assert both.matched_phrases == ["injured", "lawyer"]
    assert both.reason == "safety escalation: 'injured'; legal escalation: 'lawyer'"
    assert both.as_row()["also_send_to"] == "legal-escalations"
    # Phrase-only escalation is a known limit: an injury in other words is not caught.
    assert names("a piece of glass cut my child's cheek") == []
    # The subject is read too: a phrase only in the subject line still escalates.
    subject_only = recipe.Email("Z", "z@mail.example", "Fire hazard", "My order is late")
    screened = recipe.screen([subject_only], RULES)["Z"]
    assert (screened.team, screened.escalations) == ("product-safety", ["safety"]), screened
    assert screened.matched_phrases == ["fire"], screened.matched_phrases
    print("escalations: whole words, subject read, 'Sue' a name, both flags kept ok")


def check_parse_emails() -> None:
    def refused(records: list[dict[str, Any]]) -> str:
        try:
            recipe.parse_emails(records)
        except ValueError as error:
            return str(error)
        raise AssertionError(f"{records} was accepted")

    ok = recipe.parse_emails([{"id": "A", "body": "hi"}, {"id": "B", "subject": "x"}])
    assert [m.id for m in ok] == ["A", "B"]
    reply = recipe.parse_emails(
        [{"id": "R", "from": "r@mail.example", "in_reply_to": "<x@shop.example>", "body": "hi"}]
    )[0]
    assert (reply.sender, reply.in_reply_to) == ("r@mail.example", "<x@shop.example>"), reply
    row = recipe.screen([reply], RULES)["R"].as_row()
    assert list(row) == list(recipe.ROUTE_FIELDS), list(row)
    assert (row["from"], row["in_reply_to"]) == ("r@mail.example", "<x@shop.example>"), row
    burn = {"id": "A", "body": "It burned me"}
    assert refused([burn, {"id": "A", "body": "Where is my order?"}]) == (
        "email id 'A' is used twice"
    )
    assert refused([{"id": "A#2", "body": "hi"}]) == "email id 'A#2' contains '#'"
    print("parse emails: from and in_reply_to kept; a repeated id or '#' in an id refused ok")


def check_fields() -> None:
    assert recipe.normalize_order_number("lk 208 331", RULES) == "LK-208331"
    assert recipe.normalize_order_number("#LK-204517", RULES) == "LK-204517"
    assert recipe.normalize_order_number("640", RULES) is None
    assert recipe.match_store("the Riverside store", CATALOG, RULES) == "ST-022"
    assert recipe.match_store("Cedar Fals", CATALOG, RULES) == "ST-047"
    assert recipe.match_store("Northgate store", CATALOG, RULES) is None
    text = "Order lk 208 331 from Riverside, or was it Maple Grove?"
    item = email(text)
    spans = [
        {"label": "order number", "text": "lk 208 331", "score": 0.9},
        {"label": "store location", "text": "Riverside", "score": 0.8},
        {"label": "store location", "text": "Maple Grove", "score": 0.7},
        {"label": "store location", "text": "Oakwood", "score": 0.7},
    ]
    found = recipe.Route("X")
    recipe.read_fields(found, item, spans, CATALOG, RULES)
    assert found.order_numbers == ["LK-208331"]
    assert (found.store_id, found.store_name) == ("ST-022", "Riverside Plaza")
    assert found.notes == [
        "store span 'Oakwood' is not in the email; dropped",
        "several stores named: ST-022, ST-014",
    ], found.notes
    # The model reads windows with whitespace collapsed, so its spans have single spaces; a
    # line break or a double space in the email must not drop them.
    broken = email("I returned it at the Riverside\nPlaza store.\nOrder LK 208  331, thanks")
    spans = [
        {"label": "order number", "text": "LK 208 331", "score": 0.9},
        {"label": "store location", "text": "Riverside Plaza", "score": 0.8},
    ]
    assert "LK 208 331" in recipe.windows(broken.text, 120)[0]
    found = recipe.Route("X")
    recipe.read_fields(found, broken, spans, CATALOG, RULES)
    assert found.order_numbers == ["LK-208331"], found.notes
    assert found.store_id == "ST-022", found.notes
    assert found.notes == [], found.notes
    print("fields: order pattern, store list, several stores, line breaks in spans ok")


def check_route_by_model() -> None:
    def decide(top: str, score: float, errors: list[str] | None = None) -> recipe.Route:
        found = recipe.Route("X")
        probs = probabilities(top, score)["data"][recipe.QUESTION_ID]["probabilities"]
        recipe.route_by_model(found, probs, errors or [], RULES)
        return found

    assert (decide("billing", 0.9).team, decide("billing", 0.9).method) == ("billing", "model")
    low = decide("billing", 0.4)
    assert (low.team, low.method, low.suggested_team) == (GENERAL, "general", "billing")
    assert low.reason.startswith("top team billing scored 0.40 < 0.50")
    close = recipe.Route("X")
    recipe.route_by_model(close, {"billing": 0.55, "loyalty": 0.45}, [], RULES)
    assert close.method == "general"
    assert "margin 0.10 < 0.20" in close.reason
    failed = decide("billing", 0.9, ["team model: timed out"])
    assert (failed.team, failed.reason) == (GENERAL, "team model: timed out")
    # Two windows that each pick a different team clearly: the merge shows the conflict, and
    # the email goes to the general queue.
    one = {**RULES, "thresholds": {**RULES["thresholds"], "max_words_per_window": 3}}
    text = "I was charged twice and my points are missing"
    assert len(recipe.windows(text, 3)) == 3
    replies = {f"{TEAM_MODEL}:A#{n}": probabilities("loyalty", 0.9) for n in (2, 3)}
    replies[f"{TEAM_MODEL}:A#1"] = probabilities("billing", 0.9)
    split = route([email(text, "A")], Stand(replies=replies), one)[0]
    assert (split.team, split.method) == (GENERAL, "general"), split.reason
    assert {split.suggested_team, split.second_team} == {"billing", "loyalty"}, split
    # Each peak is 0.90; renormalized over the teams they are 0.48 each, with no margin.
    assert split.margin == 0.0, split.margin
    assert split.reason.startswith("top team billing scored 0.48 < 0.50"), split.reason
    # The model's team must be a catalog team: an unknown one is an error, not a route.
    stray = probabilities("returns-refunds", 0.9)
    stray["data"][recipe.QUESTION_ID]["probabilities"]["warehouse"] = 0.05
    replies = {f"{TEAM_MODEL}:A#1": stray}
    pair = [email("Where is my refund?", "A"), email("My refund is late.", "B")]
    routes = route(pair, Stand(replies=replies))
    assert (routes[0].team, routes[0].method) == (GENERAL, "general"), routes[0]
    assert routes[0].suggested_team == "", routes[0]
    assert "teams not in the catalog: warehouse" in routes[0].reason, routes[0].reason
    assert routes[1].method == "model", routes[1].reason
    print("route by model: clear pick, low score, low margin, failed call, split, stray ok")


def check_examples() -> None:
    routes = {r.email_id: r for r in route(EMAILS, Stand())}
    escalated = {k: (r.team, r.escalations) for k, r in routes.items() if r.method == "escalation"}
    assert escalated == {
        "E-03": ("product-safety", ["safety"]),
        "E-04": ("legal-escalations", ["legal"]),
        "E-12": ("product-safety", ["safety", "legal"]),
    }, escalated
    assert routes["E-12"].also_send_to == ["legal-escalations"]
    # E-11 is the paraphrased injury: no phrase, so it goes to the team model like any other.
    assert routes["E-11"].method == "model"
    assert not routes["E-11"].escalations
    assert routes["E-05"].method == "model"  # "Sue" is a name
    # Every item in the data is an email: a .example sender, and E-04 is a reply in a thread.
    assert all(e.sender.endswith(".example") for e in EMAILS), [e.sender for e in EMAILS]
    assert [e.id for e in EMAILS if e.in_reply_to] == ["E-04"]
    print("examples: E-03, E-04 and E-12 escalate, E-12 keeps both; E-11 and E-05 do not ok")


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


def stopped(call: Callable[[], object]) -> str:
    """The SIEStopped message the call raises; fails when it raises nothing."""
    try:
        call()
    except recipe.SIEStopped as error:
        return str(error)
    raise AssertionError("the run did not stop")


def raised(call: Callable[[], object]) -> BaseException:
    """Whatever the call raises, SystemExit included; fails when it raises nothing."""
    try:
        call()
    except BaseException as error:  # SystemExit too: the check is on which one it was
        return error
    raise AssertionError("nothing was raised")


def check_sie_failure() -> None:
    for error in TRANSIENT:
        assert not recipe.is_fatal(error), f"{type(error).__name__} should be transient"
    for error in FATAL:
        assert recipe.is_fatal(error), f"{type(error).__name__} should stop the run"
    one = {**RULES, "thresholds": {**RULES["thresholds"], "batch_size": 1}}
    pair = [email("Where is my refund?", "A"), email("My refund is late.", "B")]
    burn = [email("The heater burned me.", "A"), email("My refund is late.", "B")]

    def run(emails: list[recipe.Email], **errors: list[Exception | None]) -> list[str]:
        client = Stand({TEAM_MODEL if k == "team" else FIELD_MODEL: v for k, v in errors.items()})
        return [r.method for r in route(emails, client, one)]

    # A transient error on one call of two: that email goes to the general queue.
    for error in TRANSIENT:
        assert run(pair, team=[error, None]) == ["general", "model"], type(error).__name__
        assert run(pair, fields=[error, None]) == ["general", "model"], type(error).__name__
    long = "connection refused " + "x" * 300
    routes = route(pair, Stand({TEAM_MODEL: [SIEConnectionError(long), None]}), one)
    reason = "team model: the SIE call failed: SIEConnectionError: connection refused"
    assert routes[0].reason.startswith(reason), routes[0].reason
    assert len(routes[0].reason) == len(reason) - len("connection refused") + 200
    assert routes[0].suggested_team == ""  # a failed call has no suggestion
    # An escalated email keeps its queue when its field call fails; the failure is a note.
    routes = route(burn, Stand({FIELD_MODEL: [SIEConnectionError("reset"), None]}), one)
    assert (routes[0].team, routes[0].method) == ("product-safety", "escalation")
    assert routes[0].notes == ["field model: the SIE call failed: SIEConnectionError: reset"]
    # A reply missing its key is a failed call, not "nothing found"; an empty list is fine.
    for model, key in ((FIELD_MODEL, "entities"), (TEAM_MODEL, "data")):
        client = Stand(replies={f"{model}:A#1": {"other": []}})
        routes = route(pair, client, one)
        assert [r.method for r in routes] == ["general", "model"], key
        assert f"the reply has no {key!r}" in routes[0].reason, routes[0].reason
    client = Stand(replies={f"{TEAM_MODEL}:A#1": {"data": {}}})
    assert "the reply has no 'care_team' probabilities" in route(pair, client, one)[0].reason
    # A per-window item error or missing key counts as a failed call: when it is every call
    # (here the first call raises and the second comes back with only an item error), the run
    # stops instead of sending every email to the general queue.
    for model, key in ((FIELD_MODEL, "entities"), (TEAM_MODEL, "data")):
        unusable: list[dict[str, Any]] = [
            {"error": {"code": "INFERENCE_ERROR", "message": "oom"}},
            {"other": []},
        ]
        for bad in unusable:
            client = Stand({model: [ServerError("down", status_code=503), None]})
            client.replies = {f"{model}:B#1": bad}
            message_ = stopped(functools.partial(route, pair, client, one))
            assert message_.startswith(f"Every SIE call failed (2 of 2 to {model})"), message_
        client = Stand(replies={f"{model}:A#1": bad, f"{model}:B#1": bad})
        message_ = stopped(functools.partial(route, pair, client, one))
        assert message_.startswith(f"Every SIE call failed (2 of 2 to {model})"), message_
        assert f"the reply has no {key!r}" in message_, message_
    # entities: [] is a real "nothing found": a model route that asks for the order number.
    empty = route(pair, Stand(), one)[0]
    assert empty.method == "model", empty.reason
    assert empty.notes == ["no order number found: ask the customer for it"], empty.notes
    # Every call failed (a dead SIE_BASE_URL): the run stops, with the full message.
    client = Stand({FIELD_MODEL: [SIEConnectionError(long), SIEConnectionError(long)]})
    message_ = stopped(lambda: route(pair, client, one))
    assert message_.startswith(f"Every SIE call failed (2 of 2 to {FIELD_MODEL})"), message_
    assert long in message_
    assert "Check SIE_BASE_URL and your network." in message_, message_
    assert message_.endswith(recipe.NOT_WRITTEN), message_
    for error in [ProvisioningError("no GPU yet"), ServerError("down", status_code=503)]:
        client = Stand({TEAM_MODEL: [error, error]})
        message_ = stopped(functools.partial(route, pair, client, one))
        assert message_.startswith(f"Every SIE call failed (2 of 2 to {TEAM_MODEL})"), message_
        assert "may still be starting or SIE is busy" in message_, message_
        assert "SIE_BASE_URL" not in message_, message_
    # A model SIE marked as never loading stops the run and points at the model name.
    client = Stand({TEAM_MODEL: [ModelLoadFailedError("gated", permanent=True)]})
    message_ = stopped(lambda: route(pair, client, one))
    assert message_.startswith(f"SIE can't load {TEAM_MODEL}, ModelLoadFailedError"), message_
    assert "SIE_API_KEY" not in message_, message_
    # A refusal stops the run at once, with the full message, HTTP status and code.
    client = Stand({FIELD_MODEL: [None, InsufficientCreditsError("no credits " + "y" * 300)]})
    message_ = stopped(lambda: route(pair, client, one))
    assert "InsufficientCreditsError (HTTP 402, code INSUFFICIENT_CREDITS)" in message_, message_
    assert "y" * 300 in message_
    assert "SIE_API_KEY" in message_
    # A malformed reply has no HTTP status: the hint doesn't blame the key.
    message_ = stopped(lambda: route(pair, Stand({FIELD_MODEL: [RequestError("odd body")]}), one))
    assert "SIE_API_KEY" not in message_, message_
    assert "SIE_BASE_URL" in message_, message_
    # A bug in our own code is not hidden, and only SIEStopped becomes a clean exit.
    try:
        route(pair, Stand({FIELD_MODEL: [TypeError("bad argument")]}), one)
    except TypeError:
        pass
    else:
        raise AssertionError("TypeError was swallowed")

    def bug() -> None:
        raise RuntimeError("a bug in recipe.py")

    def refusal() -> None:
        raise recipe.SIEStopped("SIE refused")

    caught = raised(lambda: recipe.stop_on_refusal(bug))
    assert type(caught) is RuntimeError
    assert str(caught) == "a bug in recipe.py"
    caught = raised(lambda: recipe.stop_on_refusal(refusal))
    assert type(caught) is SystemExit
    assert str(caught) == "Stopped: SIE refused"
    print("sie failure: transient and missing-key replies go to general; refusals stop ok")


def check_escalations_written_first() -> None:
    """main writes escalations.csv before any model call, so a stopped run still has them."""
    with tempfile.TemporaryDirectory() as folder:
        output = Path(folder) / "run"
        refused = InsufficientCreditsError("no credits")
        client = Stand({FIELD_MODEL: [refused]})
        original, argv = recipe.make_client, sys.argv
        recipe.make_client = lambda: cast(SIEClient, client)
        sys.argv = ["recipe.py", "--output", str(output)]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                caught = raised(recipe.main)
        finally:
            recipe.make_client, sys.argv = original, argv
        assert type(caught) is SystemExit, caught
        assert "No routes were written" in str(caught), caught
        assert client.calls == [FIELD_MODEL]
        assert not (output / "routes.csv").exists()
        with (output / "escalations.csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        got = [(r["email_id"], r["team"], r["escalations"], r["also_send_to"]) for r in rows]
        assert got == [
            ("E-03", "product-safety", "safety", ""),
            ("E-04", "legal-escalations", "legal", ""),
            ("E-12", "product-safety", "safety; legal", "legal-escalations"),
        ], got
        assert [r["from"] for r in rows] == [
            "m.ferreira@inbox.example",
            "pat.lindqvist@mail.example",
            "c.mbeki@mail.example",
        ], rows
    print("escalations first: a refused run still leaves escalations.csv, routes not written ok")


def check_config() -> None:
    assert [rule["queue"] for rule in RULES["escalations"]] == [
        "product-safety",
        "legal-escalations",
    ]
    assert GENERAL not in CATALOG.teams
    assert set(RULES["needs_order_number"]) <= set(CATALOG.teams)
    assert {recipe.ESCALATION, recipe.MODEL, recipe.GENERAL} == {"escalation", "model", "general"}
    print("config: queues, teams and methods ok")


if __name__ == "__main__":
    check_config()
    check_escalations()
    check_parse_emails()
    check_fields()
    check_route_by_model()
    check_examples()
    check_sie_failure()
    check_escalations_written_first()
    print("all checks passed")
