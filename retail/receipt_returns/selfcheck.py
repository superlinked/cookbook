"""Check the recipe's receipt parsing, refund rule, statuses and SIE error rule offline.

The checks use hand-written receipt text, the committed data/ and hand-written stand-in model
replies. Nothing here calls SIE or replays a recorded response. Run it with
`uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import csv
import functools
import io
import json
import shutil
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
MODEL = RULES["models"]["ocr"]

# Hand-written OCR text for two receipts, in the shape GLM-OCR returns: one receipt line per line.
SKILLET = """BRAMBLEWAY MARKET
18 Linden Parade, Port Ellery UT 40381
Date: 10/08/2026 17:42
Store #0412  Reg 2  Trans 08841
Member BW-****0431
----------------------------------------------
310442 CAST IRON SKILLET 10IN 34.99
205530 GLASS TUMBLER 2 @ 6.49 12.98
118207 LINEN TEA TOWELS 3PK 14.50
----------------------------------------------
SUBTOTAL 62.47
SALES TAX 7.25% 4.53
TOTAL 67.00
DEBIT ****2290 67.00
"""
# The near miss: the receipt adds up, but the tax line prints no rate.
NO_RATE = """BRAMBLEWAY MARKET
5 Saltmarsh Road, Eastbrook UT 40219
Date: 10/15/2026 13:08
Store #0418  Reg 3  Trans 05611
Member BW-****1288
----------------------------------------------
160214 WALNUT CUTTING BOARD 22.50
160530 BEESWAX FOOD WRAP 3PK 12.50
----------------------------------------------
SUBTOTAL 35.00
TAX 2.54
TOTAL 37.54
DEBIT ****2290 37.54
"""

SKILLET_RETURN = {
    "request": "RET-1001",
    "type": "return",
    "member": "BW-7712-0431",
    "requested_on": "2026-10-20",
    "receipt": {"file": "receipt-photos.pdf", "page": 1},
    "item": {"sku": "310442", "description": "cast iron skillet 10in"},
}
NO_RATE_RETURN = {
    "request": "RET-1007",
    "type": "return",
    "member": "BW-7712-1288",
    "requested_on": "2026-10-22",
    "receipt": {"file": "receipt-photos.pdf", "page": 10},
    "item": {"sku": "160214", "description": "walnut cutting board"},
}
TEXTS = {"RET-1001": SKILLET, "RET-1007": NO_RATE}

# A points-claim receipt: a quantity line, a gift card (no tax, no points) and a member coupon.
# Taxable and eligible spend: 49.99 + 24.00 - 10.00 = 63.99; tax 7.25% of that is 4.64.
BASKET = """BRAMBLEWAY MARKET
Date: 09/26/2026 11:15
Store #0418  Reg 1  Trans 05102
Member BW-****0588
402215 BLUETOOTH SPEAKER MINI 49.99
120334 SCENTED CANDLE CEDAR 2 @ 12.00 24.00
990001 GIFT CARD 50.00
MEMBER COUPON -10.00
SUBTOTAL 113.99
SALES TAX 7.25% 4.64
TOTAL 118.63
VISA ****7781 118.63
"""
BASKET_CLAIM = {
    "request": "PTS-9001",
    "type": "points_claim",
    "member": "BW-7712-0588",
    "requested_on": "2026-10-21",
    "receipt": {"file": "receipt-photos.pdf", "page": 2},
}
# A voided line for the returned SKU: the receipt adds up to 10.73, the widget was taken off.
VOIDED = """BRAMBLEWAY MARKET
Date: 10/08/2026 17:42
Store #0412  Reg 2  Trans 08900
Member BW-****0431
123456 WIDGET 20.00
123456 WIDGET VOID -20.00
118207 LINEN TEA TOWELS 3PK 10.00
SUBTOTAL 10.00
SALES TAX 7.25% 0.73
TOTAL 10.73
"""
# A negative line for another SKU: the skillet's refund (21.45) is more than the total (5.36).
OVER_TOTAL = """BRAMBLEWAY MARKET
Date: 10/08/2026 17:42
Store #0412  Reg 2  Trans 08901
Member BW-****0431
310442 CAST IRON SKILLET 20.00
205530 GLASS TUMBLER VOID -15.00
SUBTOTAL 5.00
SALES TAX 7.25% 0.36
TOTAL 5.36
"""


def returning(sku: str, on: str, **extra: Any) -> dict[str, Any]:
    """A return of one SKU on the given date, built from SKILLET_RETURN."""
    item = {"sku": sku, "description": "returned item"}
    return {**SKILLET_RETURN, "requested_on": on, "item": item, **extra}


def checked(raw: dict[str, Any], text: str) -> recipe.RequestResult:
    return recipe.process_request(raw, text, {}, RULES)


def check_parse() -> None:
    receipt = recipe.parse_receipt(recipe.Page("receipt-photos.pdf", 1), SKILLET, RULES)
    assert (receipt.chain, receipt.store, receipt.transaction) == (
        "BRAMBLEWAY MARKET",
        "0412",
        "0412-2-08841",
    )
    assert [i.sku for i in receipt.items] == ["310442", "205530", "118207"]
    assert str(receipt.items[1].unit) == "6.49"
    assert (str(receipt.tax), str(receipt.tax_rate), str(receipt.total)) == (
        "4.53",
        "7.25",
        "67.00",
    )
    receipt = recipe.parse_receipt(recipe.Page("receipt-photos.pdf", 10), NO_RATE, RULES)
    assert (str(receipt.tax), receipt.tax_rate, receipt.unparsed) == ("2.54", None, [])
    print("parse: store, transaction, quantity line, tax with and without a rate ok")


def check_statuses() -> None:
    result = checked(SKILLET_RETURN, SKILLET)
    assert result.status == "ready_for_staff", result.failed_names
    assert (str(result.refund), result.points) == ("37.53", -34)
    assert result.as_json()["status"] == "ready_for_staff"
    assert "decision" not in result.as_json()
    assert recipe.READY == "ready_for_staff"
    assert recipe.DESK == "service_desk"
    # A failed check: the same receipt, returned on day 34.
    late = checked({**SKILLET_RETURN, "requested_on": "2026-11-11"}, SKILLET)
    assert (late.status, late.failed_names) == ("service_desk", ["return_window"])
    with tempfile.TemporaryDirectory() as folder:
        recipe.write_outputs(Path(folder), [result, late])
        with (Path(folder) / "statuses.csv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    assert [(r["status"], r["refund"], r["points"]) for r in rows] == [
        ("ready_for_staff", "37.53", "-34"),
        ("service_desk", "", ""),
    ]
    print("statuses: ready_for_staff when every check passed, service_desk otherwise ok")


def check_tax_without_rate() -> None:
    result = checked(NO_RATE_RETURN, NO_RATE)
    assert result.refund is None
    assert result.status == "service_desk", "a refund that can't be worked out went to staff"
    assert result.failed_names == ["refund_worked_out"], result.failed_names
    assert (
        result.failed[0].detail == "Tax 2.54 read with no rate; the refund needs the printed rate"
    )
    passed = {c.check: c.detail for c in checked(SKILLET_RETURN, SKILLET).checks}
    assert passed["refund_worked_out"] == "37.53: 34.99 before tax, tax at 7.25%"
    assert "refund_worked_out" in RULES["checks"]
    print("tax without a rate: refund not worked out, request goes to the desk ok")


def check_voided_line() -> None:
    result = checked(returning("123456", "2026-10-20"), VOIDED)
    assert result.failed_names == ["returned_item"], result.failed_names
    assert result.failed[0].detail == (
        "SKU 123456 has a voided or negative line on the receipt (20.00, -20.00)"
    )
    assert (result.refund, result.points) == (None, None)
    # The receipt still adds up: the void is read as a line, not hidden.
    assert checked(returning("118207", "2026-10-20"), VOIDED).status == "ready_for_staff"
    # Another SKU's negative line: the refund would be more than the receipt total.
    result = checked(SKILLET_RETURN, OVER_TOTAL)
    assert result.failed_names == ["refund_worked_out"], result.failed_names
    assert result.failed[0].detail == "Refund 21.45 is more than the receipt total 5.36"
    print("voided line: returned_item fails; a refund over the receipt total fails ok")


def check_tax_rate() -> None:
    misread = SKILLET.replace("SALES TAX 7.25%", "SALES TAX 1.25%")
    result = checked(SKILLET_RETURN, misread)
    assert result.failed_names == ["receipt_math"], result.failed_names
    detail = next(c.detail for c in result.checks if c.check == "receipt_math")
    assert detail == "tax 4.53 is not 1.25% of taxable 62.47 (0.78)", detail
    # A gift card carries no tax: the basket's 4.64 is 7.25% of 63.99, not of 113.99.
    assert checked(BASKET_CLAIM, BASKET).status == "ready_for_staff"
    print("tax rate: a rate that doesn't explain the tax fails receipt_math ok")


def check_request_type() -> None:
    for kind in ["exchange", "Return"]:
        result = checked({**SKILLET_RETURN, "type": kind}, SKILLET)
        assert result.failed_names == ["request_type"], (kind, result.failed_names)
        assert result.failed[0].detail.startswith(f"unknown request type {kind!r}")
        assert (result.refund, result.points) == (None, None), kind
    print("request type: exchange and Return go to the desk, unchecked as claims ok")


def check_points_claim() -> None:
    result = checked(BASKET_CLAIM, BASKET)
    assert result.status == "ready_for_staff", result.failed_names
    # 63.99 eligible: gift card left out, coupon subtracted, rounded down.
    assert result.points == 63, result.points
    credited = {
        "0418-1-05102": {
            "transaction": "0418-1-05102",
            "member": "BW-7712-0301",
            "points": "63",
            "credited_on": "2026-10-01",
        }
    }
    twice = recipe.process_request(BASKET_CLAIM, BASKET, credited, RULES)
    assert twice.failed_names == ["not_credited"], twice.failed_names
    assert twice.failed[0].detail == (
        "Trans 0418-1-05102 already credited 63 points to BW-7712-0301 on 2026-10-01"
    )
    other = checked({**BASKET_CLAIM, "member": "BW-7712-0599"}, BASKET)
    assert other.failed_names == ["receipt_member"], other.failed_names
    day90 = checked({**BASKET_CLAIM, "requested_on": "2026-12-25"}, BASKET)
    assert day90.status == "ready_for_staff", day90.failed_names
    day91 = checked({**BASKET_CLAIM, "requested_on": "2026-12-26"}, BASKET)
    assert day91.failed_names == ["claim_window"], day91.failed_names
    print("points claim: 63 points, credit twice, wrong member and claim window ok")


def check_return_gates() -> None:
    # One candle of "2 @ 12.00", less its share of the 10.00 coupon: 12.00 - 1.62 = 10.38,
    # plus 7.25% tax 0.75 = 11.13; 10 points taken back.
    candle = checked(returning("120334", "2026-10-21"), BASKET)
    assert candle.status == "ready_for_staff", candle.failed_names
    assert (str(candle.refund), candle.points) == ("11.13", -10), (candle.refund, candle.points)
    # Electronics (prefix 4): day 15 is inside the 15-day window, day 16 is not.
    assert checked(returning("402215", "2026-10-11"), BASKET).status == "ready_for_staff"
    late = checked(returning("402215", "2026-10-12"), BASKET)
    assert late.failed_names == ["return_window"], late.failed_names
    assert late.failed[0].detail == "16 days after purchase; electronics window is 15 days"
    # A gift card is not returnable, and no refund is worked out for it.
    card = checked(returning("990001", "2026-10-01"), BASKET)
    assert card.failed_names == ["returned_item"], card.failed_names
    assert card.failed[0].detail == "990001 GIFT CARD is not returnable"
    assert (card.refund, card.points) == (None, None)
    assert "refund_worked_out" not in [c.check for c in card.checks]
    # A printed total that doesn't add up.
    off = checked(SKILLET_RETURN, SKILLET.replace("TOTAL 67.00", "TOTAL 87.00"))
    assert off.failed_names == ["receipt_math"], off.failed_names
    # The 30-day window includes its last day: bought 10/08, day 30 is 11/07.
    assert checked({**SKILLET_RETURN, "requested_on": "2026-11-07"}, SKILLET).status == (
        "ready_for_staff"
    )
    day31 = checked({**SKILLET_RETURN, "requested_on": "2026-11-08"}, SKILLET)
    assert day31.failed_names == ["return_window"], day31.failed_names
    print("return gates: coupon share, quantity line, windows, gift card, bad total ok")


def check_data() -> None:
    raws = recipe.load_requests(recipe.DATA_DIR / "requests.jsonl")
    assert [r["request"] for r in raws][-1] == "RET-1007"
    assert raws[-1] == NO_RATE_RETURN
    assert len(raws) == 10
    print("data: 10 requests, RET-1007 is the tax-without-rate return ok")


class StandIn:
    """Stands in for SIEClient: each call raises the next listed error, or, for None, returns
    the hand-written receipt text for the request. A dict is returned as the whole reply."""

    def __init__(self, *replies: Exception | dict[str, Any] | None) -> None:
        self.replies = list(replies)

    def extract(self, model: str, item: Any, **_: Any) -> dict[str, Any]:
        reply = self.replies.pop(0) if self.replies else None
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, dict):
            return reply
        text = TEXTS[str(item["id"]).removeprefix("ocr/")]
        return {"entities": [{"label": "markdown", "text": text}]}


TRANSIENT: list[SIEError] = [
    SIEConnectionError("connection refused"),
    ServerError("unavailable", status_code=503),
    RateLimitError("slow down"),
    InputTooLongError("too long"),
    ProvisioningError("no GPU yet"),
    ModelLoadingError("loading"),
    ModelLoadFailedError("try later", permanent=False),
    SIEError("an SIE error with no subclass"),
    recipe.MissingKeyError("no entities"),
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
    with tempfile.TemporaryDirectory() as folder:
        data = Path(folder)
        shutil.copy(recipe.DATA_DIR / "receipt-photos.pdf", data)
        shutil.copy(recipe.DATA_DIR / "credited-transactions.csv", data)
        lines = [json.dumps(raw) for raw in (SKILLET_RETURN, NO_RATE_RETURN)]
        (data / "requests.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

        def run(*replies: Exception | dict[str, Any] | None) -> list[recipe.RequestResult]:
            client = cast(SIEClient, StandIn(*replies))
            with contextlib.redirect_stdout(io.StringIO()):  # the run's "1 of 2 failed" line
                return recipe.run(client, RULES, data)

        # Both calls work: the stand-in text gives the planted outcomes.
        results = run()
        assert [r.status for r in results] == ["ready_for_staff", "service_desk"]
        # A transient error on one call of two: that request goes to the desk, the other doesn't.
        for error in TRANSIENT:
            results = run(None, error)
            assert [r.status for r in results] == ["ready_for_staff", "service_desk"], error
            assert results[1].failed[0].check == "readable", error
        long = "connection refused " + "x" * 300
        results = run(SIEConnectionError(long), None)
        assert [r.status for r in results] == ["service_desk", "service_desk"]
        reason = (
            "receipt-photos.pdf p1: the SIE call failed: SIEConnectionError: connection refused"
        )
        detail = results[0].failed[0].detail
        assert detail.startswith(reason), detail
        assert len(detail) == len(reason) - len("connection refused") + 200
        # A reply missing its entities list is a failed call; an empty list is "nothing read".
        results = run({"data": []}, None)
        assert results[0].failed[0].detail == (
            "receipt-photos.pdf p1: the SIE call failed: MissingKeyError: "
            f"the reply from {MODEL} has no 'entities' list"
        ), results[0].failed[0].detail
        results = run({"entities": []}, None)
        assert results[0].failed[0].detail == "receipt-photos.pdf p1: only 0 characters read"
        # Every call failed (a dead SIE_BASE_URL): the run stops, with the full message.
        message = stopped(lambda: run(SIEConnectionError(long), SIEConnectionError(long)))
        assert message.startswith(f"Every SIE call failed (2 of 2 to {MODEL})"), message
        assert long in message
        assert "Check SIE_BASE_URL and your network." in message, message
        assert "No outputs were written." in message, message
        for error in [ProvisioningError("no GPU yet"), ServerError("busy", status_code=503)]:
            message = stopped(functools.partial(run, error, error))
            assert "may still be starting or SIE is busy" in message, message
        message = stopped(lambda: run({"data": []}, {"data": []}))
        assert message.startswith("Every SIE call failed (2 of 2"), message
        # A refusal stops the run at once, with the full message, HTTP status and code.
        message = stopped(lambda: run(None, InsufficientCreditsError("no credits " + "y" * 300)))
        assert "InsufficientCreditsError (HTTP 402, code INSUFFICIENT_CREDITS)" in message, message
        assert "y" * 300 in message
        assert "SIE_API_KEY" in message
        message = stopped(lambda: run(ModelLoadFailedError("gated", permanent=True)))
        assert message.startswith(f"SIE can't load {MODEL}, ModelLoadFailedError"), message
        assert "Check the model name in rules.yaml" in message, message
        # A malformed reply has no HTTP status: the hint doesn't blame the key.
        message = stopped(lambda: run(RequestError("unexpected body")))
        assert "SIE_API_KEY" not in message, message
        assert "SIE_BASE_URL" in message, message
        # A bug in our own code is not hidden.
        try:
            run(TypeError("bad argument"))
        except TypeError:
            pass
        else:
            raise AssertionError("TypeError was swallowed")

    def bug() -> None:
        raise RuntimeError("a bug in recipe.py")

    def refusal() -> None:
        raise recipe.SIEStopped("SIE refused")

    caught = raised(lambda: recipe.stop_on_refusal(bug))
    assert type(caught) is RuntimeError, f"a RuntimeError became {type(caught).__name__}"
    caught = raised(lambda: recipe.stop_on_refusal(refusal))
    assert type(caught) is SystemExit, "SIEStopped did not stop the run cleanly"
    assert str(caught) == "Stopped: SIE refused"
    print("sie failure: transient to the desk; refusals and all-failed stop; bugs raise ok")


if __name__ == "__main__":
    check_parse()
    check_statuses()
    check_tax_without_rate()
    check_voided_line()
    check_tax_rate()
    check_request_type()
    check_points_claim()
    check_return_gates()
    check_data()
    check_sie_failure()
