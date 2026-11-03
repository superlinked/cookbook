"""Check the recipe's parsing, field checks and SIE error rule offline.

The checks use hand-written strings, the committed data/ and hand-written stand-in model
replies. Nothing here calls SIE or replays a recorded response. Run it with
`uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import csv
import io
import re
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sie_sdk import SIEClient
from sie_sdk.client.errors import (
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
)

import recipe

RULES = recipe.load_rules()
CATALOG = recipe.load_catalog()
TAG = re.compile(RULES["asset_tag_pattern"])
DECIDE, GLINER = RULES["models"]["codes"], RULES["models"]["fields"]


def order(notes: str, asset: str = "P-101", hours: float = 4.0) -> recipe.WorkOrder:
    start = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    end = datetime.fromtimestamp(start.timestamp() + hours * 3600, UTC)
    return recipe.WorkOrder("WO-1", asset, "corrective", start, end, notes)


def decide(probs: dict[str, float]) -> dict[str, Any]:
    """A stand-in GLiNER2.5-Decide reply."""
    return {"data": {"answer": {"type": "choice", "probabilities": probs}}}


def confident(picklist: str) -> dict[str, Any]:
    """A clear answer: the first option of the picklist at 0.9."""
    labels = [option["label"] for option in RULES["picklists"][picklist]["options"]]
    return decide(
        {label: 0.9 if i == 0 else 0.1 / (len(labels) - 1) for i, label in enumerate(labels)}
    )


class FakeSIE:
    """Stands in for SIEClient. ``decide_reply`` and ``fields_reply`` build each window's
    reply from its text; ``errors`` holds, per model, the error each call raises in turn
    (None: answer normally)."""

    def __init__(
        self,
        fields_reply: Callable[[str], dict[str, Any]],
        decide_reply: Callable[[str, str], dict[str, Any]] | None = None,
        errors: dict[str, list[Exception | None]] | None = None,
    ) -> None:
        self.fields_reply = fields_reply
        self.decide_reply = decide_reply or (lambda text, picklist: confident(picklist))
        self.errors = errors or {}
        self.calls: list[str] = []

    def extract(self, model: str, items: Any, **kwargs: Any) -> Any:
        self.calls.append(model)
        queue = self.errors.get(model, [])
        error = queue.pop(0) if queue else None
        if error is not None:
            raise error
        if model == GLINER:
            return {"id": items["id"], **self.fields_reply(items["text"])}
        question = kwargs["output_schema"]["answer"]["instructions"]
        name = next(n for n, p in RULES["picklists"].items() if p["question"] == question)
        return [{"id": item["id"], **self.decide_reply(item["text"], name)} for item in items]


def draft(client: FakeSIE, orders: list[recipe.WorkOrder]) -> list[recipe.Form]:
    with contextlib.redirect_stdout(io.StringIO()):
        return recipe.draft_forms(cast(SIEClient, client), orders, CATALOG, RULES)


def fields(**data: Any) -> Callable[[str], dict[str, Any]]:
    return lambda text: {"data": data}


GOOD = fields(component="seal", actions=["replaced seal"], parts_used=[], labor="2 h")


def check_data() -> None:
    orders = recipe.load_orders()
    assert len(orders) == 10
    assert all(o.completed_at > o.reported_at for o in orders)
    assert all(o.reported_at.tzinfo == UTC for o in orders)
    assert "44-0919" in CATALOG
    assert "44-0918" not in CATALOG
    long = next(o for o in orders if o.wo_id == "WO-26107")
    assert len(recipe.windows(long.notes, RULES["thresholds"]["max_words_per_window"])) == 2
    print("data: 10 orders in UTC, catalog without 44-0918, WO-26107 in two windows ok")


def check_statuses() -> None:
    assert (recipe.FILLED, recipe.BLANK, recipe.FLAGGED) == ("filled", "blank", "flagged")
    assert (recipe.CHECKS_PASSED, recipe.REVIEW) == ("checks_passed", "review")
    forms = draft(FakeSIE(GOOD), [order("P-101 seal leaking. Replaced seal. 2 h.")])
    assert forms[0].status == "checks_passed", forms[0]
    assert {f.status for f in forms[0].fields.values()} <= {"filled", "blank"}
    print("statuses: filled / blank / flagged, checks_passed / review ok")


def check_codes() -> None:
    picklist = RULES["picklists"]["problem"]
    field = recipe.code_field("problem", [confident("problem")], picklist, RULES)
    assert (field.status, field.value) == ("filled", "LEAK"), field
    # An unknown label is a failed reply, never a code.
    probs = {"leak": 0.1, "seal failure": 0.9}
    field = recipe.code_field("problem", [decide(probs)], picklist, RULES)
    assert (field.status, field.value) == ("flagged", ""), field
    assert "labels not on the picklist: seal failure" in field.reason, field.reason
    # A reply without its key is a failed reply too.
    replies: list[dict[str, Any]] = [{}, {"data": {}}, {"data": {"answer": {"choice": "leak"}}}]
    for reply in replies:
        field = recipe.code_field("problem", [reply], picklist, RULES)
        assert field.status == "flagged"
        assert "no data.answer.probabilities" in field.reason
    # Low score and a small margin are flagged; a confident "not stated" is blank.
    field = recipe.code_field(
        "problem", [decide({"leak": 0.45, "vibration": 0.40})], picklist, RULES
    )
    assert field.status == "flagged", field
    assert "0.450 < 0.50" in field.reason, field
    field = recipe.code_field(
        "problem", [decide({"leak": 0.55, "vibration": 0.45})], picklist, RULES
    )
    assert field.status == "flagged", field
    assert "margin 0.100" in field.reason, field
    field = recipe.code_field(
        "problem", [decide({"not stated": 0.9, "leak": 0.1})], picklist, RULES
    )
    assert (field.status, field.value) == ("blank", ""), field
    # Two windows: the peak of each option, renormalized.
    merged = recipe.merge_probabilities([{"a": 0.8, "b": 0.2}, {"a": 0.2, "b": 0.8}])
    assert merged == {"a": 0.5, "b": 0.5}, merged
    print("codes: unknown label and missing key flagged; low score, margin, not stated ok")


def check_parts() -> None:
    notes = "Replaced 2 x bearing 6205-2RS and 1 x lip seal LS-35. Fitted impeller 44-0918."
    assert recipe.parse_part("2 x bearing 6205-2RS", notes, CATALOG, TAG) == ("6205-2RS", 2, "")
    # The quantity the model left out of the span comes from the words just before it.
    assert recipe.parse_part("lip seal LS-35", notes, CATALOG, TAG) == ("LS-35", 1, "")
    # A neighbouring part number is a different part.
    part = recipe.parse_part("impeller 44-0918", notes, CATALOG, TAG)
    assert part == ("", None, "44-0918 is not in the parts catalog"), part
    # A part number shaped like an asset tag is still a part.
    assert recipe.parse_part("1 x FG-100", "", CATALOG, TAG) == ("FG-100", 1, "")
    # By description, with no quantity: matched, but flagged.
    part = recipe.parse_part("drive belts", "Replaced drive belts.", CATALOG, TAG)
    assert part == ("BLT-A52", None, "no quantity for BLT-A52"), part
    assert (
        recipe.parse_part("gasket", "", CATALOG, TAG)[2]
        == "'gasket' matches no catalog description"
    )
    field = recipe.parts_field(["2 x bearing 6205-2RS", "impeller 44-0918"], notes, CATALOG, TAG)
    assert (field.status, field.value) == ("flagged", "6205-2RS x2"), field
    print("parts: exact catalog match, 44-0918 near miss, tag-shaped FG-100, quantities ok")


def check_hours() -> None:
    assert recipe.parse_hours("3.5 h") == (3.5, "")
    assert recipe.parse_hours("45 min") == (0.75, "")
    assert recipe.parse_hours("2 x 3.5 h") == (7.0, "")
    assert recipe.parse_hours("03:10 to 05:40") == (2.5, "")
    assert recipe.parse_hours("23:30 to 00:15") == (0.75, "")  # past midnight
    assert recipe.parse_hours("a while")[0] is None
    assert recipe.parse_hours("2 h then 30 min")[0] is None
    assert recipe.with_crew("3.5 h", "Labor 2 x 3.5 h.") == "2 x 3.5 h"
    assert recipe.with_crew("3.5 h", "Labor 3.5 h.") == "3.5 h"
    field = recipe.hours_field(["5 h"], 4.0, "downtime")
    assert field.status == "flagged", field
    assert "more than the 4 h" in field.reason, field
    print("hours: h, min, crew, clock ranges, more than the order allows ok")


def check_asset() -> None:
    field = recipe.asset_field(order("P-104 casing gasket leaking.", "P-102"), TAG, CATALOG)
    assert field.status == "flagged", field
    assert field.reason == "the notes name P-104 but the order is booked to P-102"
    field = recipe.asset_field(order("Replaced 1 x FG-100 on P-103.", "P-103"), TAG, CATALOG)
    assert field.status == "filled", field
    print("asset: another tag flags the form; a tag-shaped part number doesn't ok")


def check_fields() -> None:
    notes = "P-101 seal leaking. Replaced seal."
    # A span that isn't in the notes is dropped and flags the form.
    forms = draft(
        FakeSIE(fields(component="pump casing", actions=["replaced seal"])), [order(notes)]
    )
    assert forms[0].fields["component"].status == "flagged", forms[0].fields
    assert "'pump casing' is not in the notes" in forms[0].fields["component"].reason
    assert forms[0].fields["actions"].status == "filled", forms[0].fields["actions"]
    # Nothing found (an empty answer) is a real blank; a missing required field is flagged.
    forms = draft(FakeSIE(fields()), [order(notes)])
    assert forms[0].fields["component"].status == "blank"
    assert forms[0].fields["actions"].status == "flagged"
    assert forms[0].fields["actions"].reason == "required on every closed order"
    # A gliner2 reply without its data key is a failed reply: the model fields are flagged.
    forms = draft(FakeSIE(lambda text: {"entities": []}), [order(notes)])
    assert forms[0].fields["component"].status == "flagged"
    assert "the reply has no data" in forms[0].fields["component"].reason
    # SIE Cloud's empty-result HTTP 500 counts as nothing found.
    empty = ServerError(recipe.EMPTY_RESULT_ERROR, status_code=500)
    forms = draft(FakeSIE(GOOD, errors={GLINER: [empty]}), [order(notes)])
    assert forms[0].fields["component"].status == "blank", forms[0].fields["component"]
    print("fields: ungrounded span, required blank, missing data key, empty-result 500 ok")


TRANSIENT: list[SIEError] = [
    SIEConnectionError("connection refused"),
    ServerError("unavailable", status_code=503),
    RateLimitError("slow down"),
    InputTooLongError("too long"),
    ProvisioningError("no GPU yet"),
    ModelLoadingError("loading"),
    ModelLoadFailedError("try later", permanent=False),
]
FATAL: list[SIEError] = [
    RequestError("bad key", status_code=401),
    InsufficientCreditsError("no credits"),
    ModelLoadFailedError("gated", permanent=True),
    RequestError("unexpected body"),
]


def stopped(call: Callable[[], object]) -> str:
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
        assert not recipe.is_fatal(error), type(error).__name__
    for error in FATAL:
        assert recipe.is_fatal(error), type(error).__name__
    two = [
        order("P-101 seal leaking. Replaced seal. 2 h."),
        order("P-101 seal leaking. Replaced seal. 2 h."),
    ]
    two[1] = recipe.WorkOrder("WO-2", *list(vars(two[1]).values())[1:])
    # A transient error on one gliner2 call of two: that order is flagged, the other passes.
    forms = draft(FakeSIE(GOOD, errors={GLINER: [SIEConnectionError("x" * 300)]}), two)
    assert [f.status for f in forms] == ["review", "checks_passed"], forms
    reason = forms[0].fields["component"].reason
    assert reason.startswith("window 1: the SIE call failed: SIEConnectionError: xxx"), reason
    # A transient error on one Decide call: that picklist is flagged on every order in it.
    forms = draft(FakeSIE(GOOD, errors={DECIDE: [None, ServerError("busy", status_code=503)]}), two)
    assert [f.fields["cause"].status for f in forms] == ["flagged", "flagged"]
    assert [f.fields["problem"].status for f in forms] == ["filled", "filled"]
    # Every call to one model failed: the run stops and says what to check.
    errors: dict[str, list[Exception | None]] = {GLINER: [SIEConnectionError("down")] * 2}
    message = stopped(lambda: draft(FakeSIE(GOOD, errors=errors), two))
    assert message.startswith(f"Every SIE call failed (2 of 2 to {GLINER})"), message
    assert "Check SIE_BASE_URL and your network." in message
    errors = {DECIDE: [ModelLoadingError("loading")] * 3}
    message = stopped(lambda: draft(FakeSIE(GOOD, errors=errors), two))
    assert f"(3 of 3 to {DECIDE})" in message
    assert "may still be starting" in message
    # A refusal stops at once with SIE's full message.
    errors = {DECIDE: [InsufficientCreditsError("no credits " + "y" * 300)]}
    message = stopped(lambda: draft(FakeSIE(GOOD, errors=errors), two))
    assert "InsufficientCreditsError (HTTP 402, code INSUFFICIENT_CREDITS)" in message, message
    assert "y" * 300 in message
    assert "No outputs were written." in message
    message = stopped(lambda: draft(FakeSIE(GOOD, errors={GLINER: [RequestError("odd")]}), two))
    assert "SIE_BASE_URL" in message, message
    assert "SIE_API_KEY" not in message, message
    # A bug in our own code is raised as is; main turns only SIEStopped into a clean exit.
    try:
        draft(FakeSIE(GOOD, errors={GLINER: [TypeError("bad")]}), two)
    except TypeError:
        pass
    else:
        raise AssertionError("TypeError was swallowed")

    def refusal() -> None:
        raise recipe.SIEStopped("SIE refused")

    def bug() -> None:
        raise RuntimeError("bug")

    caught = raised(lambda: recipe.stop_on_refusal(refusal))
    assert type(caught) is SystemExit, "SIEStopped did not stop the run cleanly"
    assert str(caught) == "Stopped: SIE refused"
    caught = raised(lambda: recipe.stop_on_refusal(bug))
    assert type(caught) is RuntimeError, f"a RuntimeError became {type(caught).__name__}"
    print("sie failure: transient flags fields; refusal and every-call-failed stop; bugs raise ok")


def check_csv() -> None:
    notes = "=HYPERLINK(1) P-101 seal leaking. Replaced seal."
    forms = draft(
        FakeSIE(fields(component="=HYPERLINK(1)", actions=["Replaced seal"])), [order(notes)]
    )
    with tempfile.TemporaryDirectory() as folder:
        recipe.write_outputs(Path(folder), forms)
        with (Path(folder) / "completion_forms.csv").open(encoding="utf-8") as handle:
            row = next(csv.DictReader(handle))
        assert row["component"] == "'=HYPERLINK(1)", row
        assert (Path(folder) / "drafts.json").exists()
    assert recipe.csv_safe("-3") == "'-3"
    assert recipe.csv_safe("WO-1") == "WO-1"
    print("csv: formula cells quoted ok")


if __name__ == "__main__":
    check_data()
    check_statuses()
    check_codes()
    check_parts()
    check_hours()
    check_asset()
    check_fields()
    check_sie_failure()
    check_csv()
    print("all checks passed")
