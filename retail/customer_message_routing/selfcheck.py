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
MESSAGES = recipe.load_messages()
TEAM_MODEL, FIELD_MODEL = RULES["models"]["team"], RULES["models"]["fields"]
GENERAL = RULES["general_queue"]


def message(text: str, message_id: str = "X") -> recipe.Message:
    return recipe.Message(message_id, "chat", "", text)


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
    messages: list[recipe.Message], client: Stand, rules: dict[str, Any] = RULES
) -> list[recipe.Route]:
    screened = recipe.screen(messages, rules)
    with contextlib.redirect_stdout(io.StringIO()):  # the run's "1 of 2 failed" line
        return recipe.route_messages(cast(SIEClient, client), messages, screened, CATALOG, rules)


def check_escalations() -> None:
    def names(text: str) -> list[str]:
        return [name for name, _, _ in recipe.escalations(message(text), RULES)]

    assert names("The heater CAUGHT   fire") == ["safety"]
    assert names("We sat by the fireplace") == []
    assert names("Sue at the store was helpful") == []
    assert names("I will sue you") == ["legal"]
    # Both flags are kept, safety first; legal goes in also_send_to.
    both = recipe.screen([message("He was injured and we called a lawyer.")], RULES)["X"]
    assert (both.team, both.method) == ("product-safety", "escalation")
    assert both.escalations == ["safety", "legal"]
    assert both.also_send_to == ["legal-escalations"]
    assert both.matched_phrases == ["injured", "lawyer"]
    assert both.reason == "safety escalation: 'injured'; legal escalation: 'lawyer'"
    assert both.as_row()["also_send_to"] == "legal-escalations"
    # Phrase-only escalation is a known limit: an injury in other words is not caught.
    assert names("a piece of glass cut my child's cheek") == []
    print("escalations: whole words, 'Sue' a name, both flags kept, paraphrase not caught ok")


def check_fields() -> None:
    assert recipe.normalize_order_number("lk 208 331", RULES) == "LK-208331"
    assert recipe.normalize_order_number("#LK-204517", RULES) == "LK-204517"
    assert recipe.normalize_order_number("640", RULES) is None
    assert recipe.match_store("the Riverside store", CATALOG, RULES) == "ST-022"
    assert recipe.match_store("Cedar Fals", CATALOG, RULES) == "ST-047"
    assert recipe.match_store("Northgate store", CATALOG, RULES) is None
    text = "Order lk 208 331 from Riverside, or was it Maple Grove?"
    item = message(text)
    spans = [
        {"label": "order number", "text": "lk 208 331", "score": 0.9},
        {"label": "store location", "text": "Riverside", "score": 0.8},
        {"label": "store location", "text": "Maple Grove", "score": 0.7},
        {"label": "store location", "text": "Oakwood", "score": 0.7},
    ]
    found = recipe.Route("X", "chat")
    recipe.read_fields(found, item, spans, CATALOG, RULES)
    assert found.order_numbers == ["LK-208331"]
    assert (found.store_id, found.store_name) == ("ST-022", "Riverside Plaza")
    assert found.notes == [
        "store span 'Oakwood' is not in the message; dropped",
        "several stores named: ST-022, ST-014",
    ], found.notes
    print("fields: order pattern, store list, several stores noted ok")


def check_route_by_model() -> None:
    def decide(top: str, score: float, errors: list[str] | None = None) -> recipe.Route:
        found = recipe.Route("X", "chat")
        probs = probabilities(top, score)["data"][recipe.QUESTION_ID]["probabilities"]
        recipe.route_by_model(found, probs, errors or [], RULES)
        return found

    assert (decide("billing", 0.9).team, decide("billing", 0.9).method) == ("billing", "model")
    low = decide("billing", 0.4)
    assert (low.team, low.method, low.suggested_team) == (GENERAL, "general", "billing")
    assert low.reason.startswith("top team billing scored 0.40 < 0.50")
    close = recipe.Route("X", "chat")
    recipe.route_by_model(close, {"billing": 0.55, "loyalty": 0.45}, [], RULES)
    assert close.method == "general"
    assert "margin 0.10 < 0.20" in close.reason
    failed = decide("billing", 0.9, ["team model: timed out"])
    assert (failed.team, failed.reason) == (GENERAL, "team model: timed out")
    print("route by model: clear pick, low score, low margin, failed call ok")


def check_planted() -> None:
    routes = {r.message_id: r for r in route(MESSAGES, Stand())}
    escalated = {k: (r.team, r.escalations) for k, r in routes.items() if r.method == "escalation"}
    assert escalated == {
        "M-03": ("product-safety", ["safety"]),
        "M-04": ("legal-escalations", ["legal"]),
        "M-12": ("product-safety", ["safety", "legal"]),
    }, escalated
    assert routes["M-12"].also_send_to == ["legal-escalations"]
    # M-11 is the paraphrased injury: no phrase, so it goes to the team model like any other.
    assert routes["M-11"].method == "model"
    assert not routes["M-11"].escalations
    assert routes["M-05"].method == "model"  # "Sue" is a name
    print("planted: M-03, M-04 and M-12 escalate, M-12 keeps both; M-11 and M-05 do not ok")


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
    pair = [message("Where is my refund?", "A"), message("My refund is late.", "B")]
    burn = [message("The heater burned me.", "A"), message("My refund is late.", "B")]

    def run(messages: list[recipe.Message], **errors: list[Exception | None]) -> list[str]:
        client = Stand({TEAM_MODEL if k == "team" else FIELD_MODEL: v for k, v in errors.items()})
        return [r.method for r in route(messages, client, one)]

    # A transient error on one call of two: that message goes to the general queue.
    for error in TRANSIENT:
        assert run(pair, team=[error, None]) == ["general", "model"], type(error).__name__
        assert run(pair, fields=[error, None]) == ["general", "model"], type(error).__name__
    long = "connection refused " + "x" * 300
    routes = route(pair, Stand({TEAM_MODEL: [SIEConnectionError(long), None]}), one)
    reason = "team model: the SIE call failed: SIEConnectionError: connection refused"
    assert routes[0].reason.startswith(reason), routes[0].reason
    assert len(routes[0].reason) == len(reason) - len("connection refused") + 200
    assert routes[0].suggested_team == ""  # a failed call has no suggestion
    # An escalated message keeps its queue when its field call fails; the failure is a note.
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
        got = [(r["message_id"], r["team"], r["escalations"], r["also_send_to"]) for r in rows]
        assert got == [
            ("M-03", "product-safety", "safety", ""),
            ("M-04", "legal-escalations", "legal", ""),
            ("M-12", "product-safety", "safety; legal", "legal-escalations"),
        ], got
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
    check_fields()
    check_route_by_model()
    check_planted()
    check_sie_failure()
    check_escalations_written_first()
    print("all checks passed")
