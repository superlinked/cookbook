"""Check the recipe's parser, matching, promotion rule, detection counts and SIE error rule offline.

The checks use hand-written strings, the committed data/prices.csv, small images drawn here
and hand-written stand-in model replies. Nothing here calls SIE or replays a recorded response.
Run it with `uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import csv
import functools
import io
import tempfile
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from PIL import Image
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
PRICES = recipe.load_prices()
DETECT = RULES["models"]["detect"]
OCR = RULES["models"]["ocr"]

# A hand-written label in the layout of the Sprouts labels in data/: regular price first, then
# the promotion tag that hangs under it.
MAPLE = """UNIT PRICE
59.0¢
PER OZ
RETAIL PRICE
$6.49
11 OZ
BOB'S RED MILL
MAPLE SEA SALT GRANOLA
000-39978-00173
HEALTHY SAVINGS
$5.99
SAVE 50¢
VALID THRU 10/27/26"""
# The same layout without a promotion tag.
MUESLI = """27.5¢
PER OZ
$10.99
40 OZ
BOB'S RED MILL
MUESLI
000-39978-01103"""


def decide(text: str) -> dict[str, Any]:
    return recipe.decide(recipe.parse_label(text, RULES["pricing"]), PRICES, RULES)


def check_parse() -> None:
    reading = recipe.parse_label(MAPLE, RULES["pricing"])
    assert reading.upcs == ["0003997800173"], reading.upcs
    assert reading.prices == [Decimal("6.49")], reading.prices  # not 5.99, not 59.0¢
    assert reading.promotion.startswith("HEALTHY SAVINGS / $5.99 / SAVE 50¢"), reading.promotion
    assert recipe.parse_label(MUESLI, RULES["pricing"]).promotion == ""
    multi = recipe.parse_label("RED STAR\n2/$3\n000-17929-00331", RULES["pricing"])
    assert (multi.prices, multi.multi_buy) == ([], "2/$3")
    print("parse: UPC, regular price, promotion tag, unit and multi-buy prices ok")


def check_promotion() -> None:
    # Near miss: the regular price agrees with the register, but the shelf also advertises a
    # promotion the screen doesn't check. It goes to review, never into matches.csv.
    decision = decide(MAPLE)
    assert decision["status"] == "promotion", decision
    assert decision["product"] == "Bob's Red Mill Maple Sea Salt Granola 11 oz"
    assert "Regular price $6.49 agrees with the register" in decision["reason"], decision
    assert "HEALTHY SAVINGS / $5.99" in decision["reason"], decision
    assert "promotion" in recipe.REVIEW_STATUSES
    # A regular price that disagrees is a mismatch whatever the promotion says, and says so.
    wrong = decide(MAPLE.replace("$6.49", "$6.99"))
    assert wrong["status"] == "mismatch", wrong
    assert "Label $6.99, register $6.49 (+$0.50)." in wrong["reason"], wrong
    assert "also carries a promotion tag: HEALTHY SAVINGS" in wrong["reason"], wrong
    # No promotion tag: a regular price that agrees is a match.
    plain = decide(MUESLI)
    assert (plain["status"], plain["reason"]) == ("match", "Matched by UPC 0003997801103."), plain
    assert recipe.STATUSES == (
        "mismatch",
        "promotion",
        "unreadable",
        "unmatched",
        "not_checked",
        "match",
    )
    print("promotion: agreeing regular price with a promo tag goes to review; mismatch kept ok")


def check_matching() -> None:
    unknown = decide("SPROUTS\nNUTRITIONAL YEAST\nRETAIL PRICE\n$6.49\n006-46670-51819")
    assert unknown["status"] == "unmatched", unknown
    assert unknown["reason"] == "UPC 0064667051819 is not in prices.csv.", unknown
    by_name = decide("BOB'S RED MILL\nCRANBERRY ALMOND GRANOLA\n$6.99")
    assert (by_name["status"], by_name["reason"]) == ("match", "Matched by name (1.00)."), by_name
    # "Muesli" is in both muesli names: no UPC, so the name can't decide between them.
    both = decide("BOB'S RED MILL\nGLUTEN FREE MUESLI\n$8.49")
    assert both["status"] == "unmatched", both
    assert "matches more than one product (1.00 vs 1.00)" in both["reason"], both
    two = decide("BOB'S RED MILL\nMUESLI\n$10.99\n$9.99\n000-39978-01103")
    assert two["status"] == "unreadable", two
    assert "More than one retail price" in two["reason"], two
    bag = decide("Bob's Red Mill\nPOTATO STARCH\nNET WT 22 OZ")
    assert bag["status"] == "unreadable", bag
    print("matching: UPC, name fallback, unknown UPC, two prices, no price ok")


def box(x: float, y: float, w: float, h: float, score: float) -> dict[str, Any]:
    return {"label": "price label", "bbox": [x, y, w, h], "score": score}


def check_select_boxes() -> None:
    objects = [
        box(10, 10, 20, 10, 0.50),  # kept
        box(11, 10, 20, 10, 0.40),  # overlaps the first
        box(60, 10, 20, 10, 0.27),  # under min_score
        box(0, 0, 90, 90, 0.60),  # most of the photo: a product, not a label
        box(60, 50, 20, 10, 0.35),  # kept
    ]
    kept, dropped = recipe.select_boxes(objects, (100, 100), RULES["detection"])
    assert [o["score"] for o in kept] == [0.50, 0.35]
    assert dropped == {"low_score": 1, "too_large": 1, "overlap": 1}, dropped
    print("select_boxes: kept boxes and dropped counts by reason ok")


class StandIn:
    """Stands in for SIEClient. Each call takes the next listed reply: an exception is raised,
    anything else is returned as the reply."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.models: list[str] = []

    def extract(self, model: str, item: Any, **_: Any) -> Any:
        self.models.append(model)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def objects(*boxes: dict[str, Any]) -> dict[str, Any]:
    return {"objects": list(boxes)}


def ocr(text: str) -> dict[str, Any]:
    return {"entities": [{"label": "markdown", "text": text}]}


ONE_LABEL = objects(box(10, 10, 20, 10, 0.5))


def run(
    *replies: Any, n_photos: int = 2, folder: Path | None = None
) -> tuple[list[recipe.PhotoResult], Path, StandIn]:
    """Run the recipe on `n_photos` small blank photos with stand-in replies; the outputs go
    to `folder`/out."""
    folder = folder or Path(tempfile.mkdtemp())
    data = folder / "data"
    data.mkdir()
    for number in range(1, n_photos + 1):
        Image.new("RGB", (100, 100), "white").save(data / f"shelf-{number}.png")
    client = StandIn(*replies)
    output = folder / "out"
    with contextlib.redirect_stdout(io.StringIO()):  # the run's "1 of 3 failed" line
        results = recipe.run(cast(SIEClient, client), RULES, output, data, recipe.PRICES_PATH)
    assert not client.replies, f"{len(client.replies)} replies left unused"
    return results, output, client


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def check_photos_report() -> None:
    # Photo 1: one label kept, one dropped as low score. Photo 2: the detector found nothing
    # (an empty list is a real "nothing found"), which still reaches a person.
    results, output, client = run(
        objects(box(10, 10, 20, 10, 0.5), box(60, 60, 20, 10, 0.26)),
        ocr(MUESLI),
        objects(),
    )
    assert client.models == [DETECT, OCR, DETECT]
    photos = read_csv(output / "photos.csv")
    assert [(p["photo"], p["status"], p["boxes_returned"], p["labels_read"]) for p in photos] == [
        ("shelf-1.png", "checked", "2", "1"),
        ("shelf-2.png", "no_labels", "0", "0"),
    ], photos
    assert photos[0]["dropped_low_score"] == "1"
    review = read_csv(output / "needs_review.csv")
    assert [(r["photo"], r["label"], r["status"]) for r in review] == [
        ("shelf-2.png", "", "no_labels")
    ], review
    assert [r["label"] for r in read_csv(output / "matches.csv")] == ["shelf-1-L01"]
    assert (output / "crops" / "shelf-1-L01.jpg").is_file()
    assert len(results) == 2
    print("photos.csv: boxes returned, dropped and read per photo; empty photo to review ok")


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

    # A transient detect failure on one photo: that photo goes to review, the other is checked.
    for error in TRANSIENT:
        _, output, _ = run(error, ONE_LABEL, ocr(MUESLI))
        review = read_csv(output / "needs_review.csv")
        assert [(r["photo"], r["status"]) for r in review] == [("shelf-1.png", "not_checked")]
        assert review[0]["reason"].startswith("Photo not checked: the SIE call failed: ")
        assert len(read_csv(output / "matches.csv")) == 1, type(error).__name__
    # A transient OCR failure: that label goes to review with the error, cut short.
    long = "connection refused " + "x" * 300
    _, output, _ = run(ONE_LABEL, SIEConnectionError(long), ONE_LABEL, ocr(MUESLI))
    review = read_csv(output / "needs_review.csv")
    assert [(r["label"], r["status"]) for r in review] == [("shelf-1-L01", "not_checked")]
    reason = "Label not read: the SIE call failed: SIEConnectionError: connection refused"
    assert review[0]["reason"].startswith(reason), review[0]["reason"]
    assert len(review[0]["reason"]) == len(reason) - len("connection refused") + 200
    assert len(read_csv(output / "photos.csv")) == 2
    # A reply missing its key is a failed call on the same path, not "nothing found".
    _, output, _ = run({"error": {"code": "x"}}, ONE_LABEL, ocr(MUESLI))
    review = read_csv(output / "needs_review.csv")
    assert review[0]["status"] == "not_checked", review
    assert "SIEError: the reply has no `objects` (item error: {'code': 'x'})" in review[0]["reason"]
    _, output, _ = run(ONE_LABEL, {"data": {}}, ONE_LABEL, ocr(MUESLI))
    review = read_csv(output / "needs_review.csv")
    assert [(r["label"], r["status"]) for r in review] == [("shelf-1-L01", "not_checked")]
    assert "the reply has no `entities`" in review[0]["reason"], review
    # OCR that read nothing (an empty entity list) is a real result: the label is unreadable.
    _, output, _ = run(ONE_LABEL, {"entities": []}, n_photos=1)
    assert read_csv(output / "needs_review.csv")[0]["status"] == "unreadable"

    def stops(*replies: Any) -> str:
        """The stop message; also checks the run wrote nothing, not even a crop."""
        folder = Path(tempfile.mkdtemp())
        message = stopped(functools.partial(run, *replies, folder=folder))
        assert not (folder / "out").exists(), "a stopped run wrote outputs"
        return message

    # Every call failed (a dead SIE_BASE_URL): the run stops, writes nothing, full message.
    message = stops(SIEConnectionError(long), SIEConnectionError(long))
    assert message.startswith(f"Every SIE call failed (2 of 2 to {DETECT})"), message
    assert long in message
    assert "Check SIE_BASE_URL and your network." in message, message
    assert "No outputs were written." in message
    # Any other transient error on every call more likely means a cold model or a busy SIE.
    for error in [ProvisioningError("no GPU yet"), ServerError("unavailable", status_code=503)]:
        message = stops(error, error)
        assert "may still be starting or SIE is busy; run again in a few minutes." in message
        assert "SIE_BASE_URL" not in message, message
    # A refusal on a later call stops the run at once: no outputs, not even the crops already
    # cut for the first photo.
    message = stops(ONE_LABEL, InsufficientCreditsError("no credits " + "y" * 300))
    assert message.startswith(f"SIE refused the call to {OCR}, "), message
    assert "InsufficientCreditsError (HTTP 402, code INSUFFICIENT_CREDITS)" in message, message
    assert "y" * 300 in message
    assert "SIE_API_KEY" in message
    # A model SIE marked as never loading points at the model name.
    message = stops(ModelLoadFailedError("gated", permanent=True))
    assert message.startswith(f"SIE can't load {DETECT}, ModelLoadFailedError"), message
    assert "SIE_API_KEY" not in message, message
    # A malformed reply has no HTTP status: the hint doesn't blame the key.
    message = stops(RequestError("unexpected body"))
    assert "SIE_API_KEY" not in message, message
    assert "SIE_BASE_URL" in message, message
    # A bug in our own code is not hidden, and main turns only SIEStopped into a clean exit.
    caught = raised(lambda: run(TypeError("bad argument")))
    assert type(caught) is TypeError, f"TypeError became {type(caught).__name__}"

    def bug() -> None:
        raise RuntimeError("a bug in recipe.py")

    def refusal() -> None:
        raise recipe.SIEStopped("SIE refused")

    caught = raised(lambda: recipe.stop_on_refusal(bug))
    assert type(caught) is RuntimeError, f"a RuntimeError became {type(caught).__name__}"
    caught = raised(lambda: recipe.stop_on_refusal(refusal))
    assert type(caught) is SystemExit, "SIEStopped did not stop the run cleanly"
    assert str(caught) == "Stopped: SIE refused"
    print("sie failure: transient errors go to review; refusals and all-failed stop; bugs raise ok")


if __name__ == "__main__":
    check_parse()
    check_promotion()
    check_matching()
    check_select_boxes()
    check_photos_report()
    check_sie_failure()
