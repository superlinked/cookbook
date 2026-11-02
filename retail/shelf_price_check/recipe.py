"""Screen shelf price labels for regular-price discrepancies against the store's price file.

Use case:  Regular-label discrepancy screen: does each shelf label's regular retail price agree
           with the price file? A label that also carries a promotion goes to a person.
Who:       Store pricing associates and price-integrity teams.
Problem:   A shelf label that disagrees with the register is a customer complaint and, in many
           places, a compliance finding. Today someone walks the aisle with a printout and
           compares label after label by eye.
Input:     Shelf photos in data/ and the register's price file data/prices.csv
           (UPC, product name, register price). Rules and thresholds in rules.yaml.
SIE pipeline (two models):
           extract  IDEA-Research/grounding-dino-base  called once per photo: "price label" boxes
           extract  zai-org/GLM-OCR                    called once per kept box: the label text
           (Python crops and enlarges each box, parses UPC, name, retail price and any promotion
           tag or multi-buy offer, matches the UPC or name to prices.csv and compares the
           regular price.)
Output:    mismatches.csv (with the crop of each label), needs_review.csv (promotion labels,
           unreadable, unmatched or unchecked labels and photos, each with a reason),
           matches.csv, photos.csv (boxes returned, dropped by reason and read per photo), and
           labels.json with each kept box and its OCR text (dropped boxes are only counted, in
           photos.csv). A person checks the shelf and the register before any label or price is
           changed.
Why SIE:   Detection and OCR run on SIE Cloud; the comparison is plain Python you can audit line
           by line, and no generative model makes the decision. Nothing to host.
Runs on:   SIE Cloud.

    uv run recipe.py
"""

from __future__ import annotations

import argparse
import csv
import difflib
import io
import json
import os
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import yaml
from dotenv import load_dotenv
from PIL import Image
from sie_sdk import SIEClient
from sie_sdk.client.errors import (
    InputTooLongError,
    ModelLoadFailedError,
    RateLimitError,
    RequestError,
    SIEConnectionError,
    SIEError,
)
from sie_sdk.types import Item

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PRICES_PATH = DATA_DIR / "prices.csv"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
PHOTO_SUFFIXES = (".webp", ".jpg", ".jpeg", ".png")
# How much of an SIE error message goes into a per-label reason or the summary line.
ERROR_CHARS = 200

# Label statuses, in the order the run prints them. Each names what the checks found; a person
# decides what to do about it.
STATUSES = ("mismatch", "promotion", "unreadable", "unmatched", "not_checked", "match")
REVIEW_STATUSES = ("promotion", "unreadable", "unmatched", "not_checked")
# The columns of photos.csv, one row per photo. Its status is `checked` (labels were read),
# `no_labels` (the detector kept no label) or `not_checked` (the detect call failed); the last
# two also go to needs_review.csv. Each dropped box is counted under one reason.
PHOTO_FIELDS = (
    "photo",
    "status",
    "boxes_returned",
    "dropped_empty_box",
    "dropped_low_score",
    "dropped_too_large",
    "dropped_overlap",
    "labels_read",
    "reason",
)
REPORT_FIELDS = (
    "photo",
    "label",
    "status",
    "upc",
    "label_upc",
    "product",
    "label_price",
    "register_price",
    "crop",
    "reason",
)


@dataclass(frozen=True)
class PriceRow:
    upc: str
    product: str
    price: Decimal


@dataclass
class LabelReading:
    """What the parser found in one label's OCR text."""

    upcs: list[str] = field(default_factory=list)
    prices: list[Decimal] = field(default_factory=list)
    multi_buy: str = ""
    words: list[str] = field(default_factory=list)
    promotion: str = ""  # the promotion tag's text, when the label carries one


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def load_prices(path: Path = PRICES_PATH) -> list[PriceRow]:
    with path.open(encoding="utf-8", newline="") as handle:
        return [
            PriceRow(row["upc"].strip(), row["product"].strip(), Decimal(row["register_price"]))
            for row in csv.DictReader(handle)
        ]


def photos(data_dir: Path = DATA_DIR) -> list[Path]:
    """The shelf photos in `data_dir`; [] when the folder is missing or holds none."""
    if not data_dir.is_dir():
        return []
    return sorted(path for path in data_dir.iterdir() if path.suffix.lower() in PHOTO_SUFFIXES)


def make_client() -> SIEClient:
    """Build an SIE client from the API key and SIE_BASE_URL in the environment or `.env`."""
    load_dotenv()
    api_key = os.environ.get("SIE_API_KEY", "").strip()
    base_url = os.environ.get("SIE_BASE_URL", "").strip()
    if not api_key and not base_url:
        raise RuntimeError("Set SIE_API_KEY, or SIE_BASE_URL for your own SIE, in .env")
    base_url = base_url or "https://api.superlinked.com"
    return SIEClient(api_key=api_key or None, base_url=base_url, timeout_s=WAIT_S)


# --- SIE calls and their failures ------------------------------------------------------------


class SIEStopped(RuntimeError):
    """SIE refused the call for a reason a retry won't fix, or no call succeeded; the run
    stops without outputs."""


def is_fatal(error: SIEError) -> bool:
    """A refusal that fails every call the same way: a 4xx (bad key 401, forbidden 403, model
    not found 404, no credits or spend limit 402, inactive account) or a model the server
    marked as permanently unable to load. A RequestError with no HTTP status or a status below
    400 is a reply the SDK couldn't parse, and is fatal too. A rate limit, an over-long input, a
    timeout, a lost connection, a model still loading and a 5xx are transient or per-call:
    those photos and labels go to review."""
    if isinstance(error, ModelLoadFailedError):
        return bool(error.permanent)
    return isinstance(error, RequestError) and not isinstance(
        error, (RateLimitError, InputTooLongError)
    )


def describe(error: SIEError) -> str:
    """The error's class, with its HTTP status and SIE error code when the SDK has them."""
    details = [
        f"{label} {value}"
        for label, value in (
            ("HTTP", getattr(error, "status_code", None)),
            ("code", getattr(error, "code", None)),
        )
        if value is not None
    ]
    return f"{type(error).__name__}" + (f" ({', '.join(details)})" if details else "")


def short(error: SIEError) -> str:
    """The error for a per-label reason or the summary line, its message cut short."""
    return f"{describe(error)}: {str(error)[:ERROR_CHARS]}"


def stop_message(error: SIEError, model: str) -> str:
    """The message a fatal error stops the run with: SIE's full message, status and code, and
    what to check.

    A RequestError with no HTTP error status is a reply the SDK couldn't parse (a malformed or
    wrongly shaped body), not a refusal, so the key and model name are not the thing to check.
    """
    status = getattr(error, "status_code", None)
    if isinstance(error, ModelLoadFailedError):
        return (
            f"SIE can't load {model}, {describe(error)}: {error}. Check the model name in "
            "rules.yaml, then run again. No outputs were written."
        )
    if status is None or status < 400:
        return (
            f"SIE returned a reply the recipe couldn't read for {model}, {describe(error)}: "
            f"{error}. Check that SIE_BASE_URL points at an SIE server, then run again. No "
            "outputs were written."
        )
    return (
        f"SIE refused the call to {model}, {describe(error)}: {error}. Check SIE_API_KEY, the "
        "model name in rules.yaml and your SIE account, then run again. No outputs were written."
    )


def every_call_failed_advice(error: SIEError) -> str:
    """What to check when every SIE call failed, judged by the first failure.

    A lost connection points at SIE_BASE_URL and the network. Any other transient error (a
    timeout, a model still loading, a 5xx, a rate limit) more likely means the model is still
    starting or SIE is busy, so the advice is to wait and run again.
    """
    if isinstance(error, SIEConnectionError):
        return "Check SIE_BASE_URL and your network."
    return "The model may still be starting or SIE is busy; run again in a few minutes."


def stop_on_refusal[T](work: Callable[[], T]) -> T:
    """Run the work; turn SIEStopped into a clean exit. Any other error keeps its traceback."""
    try:
        return work()
    except SIEStopped as error:
        raise SystemExit(f"Stopped: {error}") from None


@dataclass
class CallLog:
    """Every SIE call the run made, and the ones that failed (with the model called)."""

    made: int = 0
    failures: list[tuple[str, SIEError]] = field(default_factory=list)

    def stop_if_all_failed(self) -> None:
        """No call succeeded, so nothing was checked: stop instead of writing empty reports."""
        if not self.failures or len(self.failures) < self.made:
            return
        first = self.failures[0][1]
        models = ", ".join(dict.fromkeys(model for model, _ in self.failures))
        raise SIEStopped(
            f"Every SIE call failed ({self.made} of {self.made} to {models}), so the run stops: "
            f"{describe(first)}: {first}. {every_call_failed_advice(first)} "
            "No outputs were written."
        ) from first


def call_sie(
    client: SIEClient, calls: CallLog, model: str, item: Item, key: str, **options: Any
) -> tuple[list[dict[str, Any]], str]:
    """One extract call; return the list under `key`, or [] and the reason the call failed.

    A call that fails for a reason that may pass (a lost connection, a timeout, a server error,
    a rate limit, a reply without `key`) is logged and its photo or label goes to review; the
    run continues. A refusal every call would repeat (see `is_fatal`) stops the run. An error
    that isn't an SIE error is a bug in this code and is raised as is.
    """
    calls.made += 1
    try:
        reply: Any = client.extract(model, item, provision_timeout_s=WAIT_S, **options)
        if not isinstance(reply, dict) or reply.get(key) is None:
            # A reply without `key` is a failed call, not an empty result: an empty list under
            # `key` is a real "nothing found". It takes the transient path below.
            error = reply.get("error") if isinstance(reply, dict) else None
            raise SIEError(
                f"the reply has no `{key}`" + (f" (item error: {error})" if error else "")
            )
    except SIEError as error:
        if is_fatal(error):
            raise SIEStopped(stop_message(error, model)) from error
        calls.failures.append((model, error))
        return [], f"the SIE call failed: {short(error)}"
    return cast(list[dict[str, Any]], reply[key]), ""


# --- Detect -----------------------------------------------------------------------------------


def _jpeg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def _iou(first: list[float], second: list[float]) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[0] + first[2], second[0] + second[2])
    bottom = min(first[1] + first[3], second[1] + second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    union = first[2] * first[3] + second[2] * second[3] - intersection
    return intersection / union if union > 0 else 0.0


def select_boxes(
    objects: list[dict[str, Any]], image_size: tuple[int, int], detection: dict[str, Any]
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Keep confident, label-sized boxes, one per label, in left-to-right order.

    Also count every box dropped and why (`empty_box` for zero width or height, `low_score`,
    `too_large`, `overlap`), so photos.csv shows what the detector returned and what the recipe
    never read.
    """
    image_area = image_size[0] * image_size[1]
    kept: list[dict[str, Any]] = []
    dropped: Counter[str] = Counter()
    for obj in sorted(objects, key=lambda item: float(item["score"]), reverse=True):
        box = [float(value) for value in obj["bbox"]]
        if box[2] <= 0 or box[3] <= 0:
            dropped["empty_box"] += 1
        elif float(obj["score"]) < detection["min_score"]:
            dropped["low_score"] += 1
        elif box[2] * box[3] > image_area * detection["max_box_area_fraction"]:
            dropped["too_large"] += 1
        elif any(
            _iou(box, [float(v) for v in other["bbox"]]) >= detection["dedupe_iou"]
            for other in kept
        ):
            dropped["overlap"] += 1
        else:
            kept.append(obj)
    kept.sort(key=lambda item: (float(item["bbox"][0]), float(item["bbox"][1])))
    return kept, dropped


def detect_labels(
    client: SIEClient, calls: CallLog, photo: Path, image: Image.Image, rules: dict[str, Any]
) -> tuple[list[dict[str, Any]], str]:
    """Every "price label" box the detector returns for one photo, or [] and why it failed."""
    detection = rules["detection"]
    options = {
        "box_threshold": detection["box_threshold"],
        "text_threshold": detection["text_threshold"],
    }
    item = Item(id=photo.stem, images=[_jpeg(image)])
    return call_sie(
        client,
        calls,
        rules["models"]["detect"],
        item,
        "objects",
        labels=detection["labels"],
        options=options,
    )


# --- Crop and read ----------------------------------------------------------------------------


def crop_box(bbox: list[float], image_size: tuple[int, int], pad: float) -> list[int]:
    x, y, width, height = (float(value) for value in bbox)
    return [
        max(0, round(x - width * pad)),
        max(0, round(y - height * pad)),
        min(image_size[0], round(x + width * (1 + pad))),
        min(image_size[1], round(y + height * (1 + pad))),
    ]


def make_crop(image: Image.Image, box: list[int], crop_rules: dict[str, Any]) -> Image.Image:
    crop = image.crop((box[0], box[1], box[2], box[3]))
    scale = min(
        float(crop_rules["upscale"]),
        crop_rules["max_edge_px"] / max(crop.width, crop.height),
    )
    size = (max(1, round(crop.width * scale)), max(1, round(crop.height * scale)))
    return crop.resize(size, Image.Resampling.LANCZOS)


def read_label(
    client: SIEClient, calls: CallLog, key: str, crop: Image.Image, rules: dict[str, Any]
) -> tuple[str, str]:
    """The OCR text of one label crop ("" when the model read nothing), and why the call
    failed ("" when it didn't)."""
    item = Item(id=key, images=[_jpeg(crop)])
    entities, failed = call_sie(client, calls, rules["models"]["ocr"], item, "entities")
    for entity in entities:
        if entity.get("label") == "markdown":
            return str(entity.get("text", "")), failed
    return "", failed


# --- Parse, match, compare (plain Python) -----------------------------------------------------

_UPC = re.compile(r"(?<!\d)(\d{3})[- ](\d{5})[- ](\d{5})(?!\d)")
_PRICE = re.compile(r"\$\s?(\d{1,4}\.\d{2})(?!\d)")
_MULTI_BUY = re.compile(r"\b\d+\s*/\s*\$\s?\d+(?:\.\d{2})?")
_WORD = re.compile(r"[a-z0-9]+")


def _has_marker(line: str, markers: list[str]) -> bool:
    upper = line.upper()
    return any(re.search(rf"\b{re.escape(marker)}\b", upper) for marker in markers)


def _starts_with_marker(line: str, markers: list[str]) -> bool:
    """The line starts with a whole marker word: "PER OZ" does, "PERFECT FOR..." doesn't."""
    upper = line.upper()
    return any(re.match(rf"{re.escape(marker)}\b", upper) for marker in markers)


def parse_label(text: str, pricing: dict[str, Any]) -> LabelReading:
    """Pull UPCs, the shelf label's retail price(s), any promotion tag and words out of the
    OCR text."""
    reading = LabelReading()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for match in _UPC.finditer(text):
        upc = "".join(match.groups())
        if upc not in reading.upcs:
            reading.upcs.append(upc)
    for index, line in enumerate(lines):
        if _has_marker(line, pricing["promo_markers"]):
            # Everything from here down is the promotion tag: its prices are not the regular
            # price. The label goes to review as `promotion` when its regular price agrees, and
            # to `mismatch` when it doesn't (see `decide`).
            reading.promotion = " / ".join(lines[index:])
            break
        next_line = lines[index + 1] if index + 1 < len(lines) else ""
        if _has_marker(line, pricing["unit_markers"]) or _starts_with_marker(
            next_line, pricing["unit_markers"]
        ):
            continue  # unit price
        if not reading.multi_buy and (multi := _MULTI_BUY.search(line)):
            reading.multi_buy = multi.group(0).replace(" ", "")
            continue
        for amount in _PRICE.findall(line):
            price = Decimal(amount)
            if price not in reading.prices:
                reading.prices.append(price)
    reading.words = _words(text)
    return reading


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("'", ""))


def upc_key(digits: str) -> str:
    """A label prints the UPC without its check digit; compare without leading zeros."""
    return digits.lstrip("0")


def name_score(product: str, label_words: list[str], similarity: float) -> float:
    """Share of the product name's words (numbers and units left out) found on the label."""
    wanted = [word for word in _words(product) if not word.isdigit() and len(word) > 2]
    if not wanted:
        return 0.0
    found = sum(
        1
        for word in wanted
        if any(
            difflib.SequenceMatcher(None, word, seen).ratio() >= similarity for seen in label_words
        )
    )
    return found / len(wanted)


def match_product(
    reading: LabelReading, prices: list[PriceRow], matching: dict[str, Any]
) -> tuple[PriceRow | None, str]:
    """Return the price-file row for this label, or None and the reason it is unmatched."""
    if len(reading.upcs) > 1:
        return None, f"More than one UPC in the crop ({', '.join(reading.upcs)})."
    scored = sorted(
        (
            (name_score(row.product, reading.words, matching["word_similarity"]), row)
            for row in prices
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if reading.upcs:
        label_upc = reading.upcs[0]
        for row in prices:
            if upc_key(label_upc) in (upc_key(row.upc), upc_key(row.upc[:-1])):
                return row, f"UPC {label_upc}"
        # Never fall back to the name here: a UPC that is not in the file may be a new product
        # or an OCR misread, and a person must decide which. The closest name is only a hint.
        hint = ""
        if scored and scored[0][0] >= matching["min_name_score"]:
            hint = f" Closest name in prices.csv: {scored[0][1].product} ({scored[0][0]:.2f})."
        return None, f"UPC {label_upc} is not in prices.csv.{hint}"
    if not scored or not reading.words:
        return None, "No text could be read from the label."
    best_score, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < matching["min_name_score"]:
        return None, f"No UPC read and no product name matched (best {best_score:.2f})."
    if best_score - runner_up < matching["min_name_margin"]:
        return None, (
            f"No UPC read and the name matches more than one product "
            f"({best_score:.2f} vs {runner_up:.2f})."
        )
    return best, f"name ({best_score:.2f})"


def decide(reading: LabelReading, prices: list[PriceRow], rules: dict[str, Any]) -> dict[str, Any]:
    """Status, product and reason for one label. Escalate instead of guessing."""
    if not reading.upcs and not reading.prices and not reading.multi_buy:
        # A shelf label always carries a UPC or a price; a crop with neither is unreadable or
        # not a label at all (the detector boxed a product), so its text is not matched.
        return {
            "upc": "",
            "label_upc": "",
            "product": "",
            "label_price": "",
            "register_price": "",
            "status": "unreadable",
            "reason": "No UPC and no price read; check the crop, it may not be a price label.",
        }
    row, how = match_product(reading, prices, rules["matching"])
    # `upc` is the price file's UPC (with its check digit), empty when no row matched;
    # `label_upc` is the digits read from the label (no check digit), whatever matched.
    decision: dict[str, Any] = {
        "upc": row.upc if row else "",
        "label_upc": ", ".join(reading.upcs),
        "product": row.product if row else "",
        "label_price": "",
        "register_price": str(row.price) if row else "",
    }
    if row is None:
        return {**decision, "status": "unmatched", "reason": how}
    if not reading.prices:
        reason = (
            f"Multi-buy price {reading.multi_buy}, not a single-unit price."
            if reading.multi_buy
            else "No retail price could be read from the label."
        )
        return {**decision, "status": "unreadable", "reason": f"Matched by {how}. {reason}"}
    if len(reading.prices) > 1:
        found = ", ".join(f"${price}" for price in reading.prices)
        return {
            **decision,
            "status": "unreadable",
            "reason": f"Matched by {how}. More than one retail price on the label ({found}).",
        }
    label_price = reading.prices[0]
    decision["label_price"] = str(label_price)
    difference = label_price - row.price
    # A promotion tag or a multi-buy offer next to the single regular price: the register may
    # charge the offer price, which this screen does not check.
    offers = [f"promotion tag: {reading.promotion}"] if reading.promotion else []
    if reading.multi_buy:
        offers.append(f"multi-buy offer: {reading.multi_buy}")
    promotion = " and a ".join(offers)
    if abs(difference) > Decimal(str(rules["pricing"]["tolerance"])):
        # The regular price disagrees whatever the promotion says: that is a finding on its own.
        return {
            **decision,
            "status": "mismatch",
            "reason": f"Matched by {how}. Label ${label_price}, register ${row.price} "
            f"({'+' if difference > 0 else '-'}${abs(difference)})."
            + (f" The label also carries a {promotion}." if promotion else ""),
        }
    if promotion:
        # The regular price agrees, but the shelf also advertises a promotion or multi-buy price
        # this screen does not check, and the register may charge either. Agreement does not
        # clear it.
        return {
            **decision,
            "status": "promotion",
            "reason": f"Matched by {how}. Regular price ${label_price} agrees with the register, "
            f"but the label also carries a {promotion}. Check the register's promotion price.",
        }
    return {**decision, "status": "match", "reason": f"Matched by {how}."}


# --- Run --------------------------------------------------------------------------------------


@dataclass
class PhotoResult:
    """One photo's detection counts, its label rows and their crops (written at the end)."""

    summary: dict[str, Any]
    rows: list[dict[str, Any]]
    crops: dict[str, Image.Image]


def check_photo(
    client: SIEClient,
    calls: CallLog,
    photo: Path,
    prices: list[PriceRow],
    rules: dict[str, Any],
) -> PhotoResult:
    """Detect, crop, read and decide every label on one photo."""
    image = Image.open(photo).convert("RGB")
    objects, failed = detect_labels(client, calls, photo, image, rules)
    kept, dropped = select_boxes(objects, image.size, rules["detection"])
    summary: dict[str, Any] = {
        "photo": photo.name,
        "status": "checked",
        "boxes_returned": len(objects),
        "dropped_empty_box": dropped["empty_box"],
        "dropped_low_score": dropped["low_score"],
        "dropped_too_large": dropped["too_large"],
        "dropped_overlap": dropped["overlap"],
        "labels_read": len(kept),
        "reason": "",
    }
    if failed:
        summary.update(status="not_checked", reason=f"Photo not checked: {failed}")
    elif not kept:
        summary.update(
            status="no_labels",
            reason="The detector kept no price label; check the photo, and photograph the "
            "shelf again if labels are on it.",
        )
    result = PhotoResult(summary, [], {})
    for number, obj in enumerate(kept, start=1):
        label_id = f"{photo.stem}-L{number:02d}"
        box = crop_box(obj["bbox"], image.size, rules["crop"]["pad_fraction"])
        crop = make_crop(image, box, rules["crop"])
        result.crops[label_id] = crop
        text, failed = read_label(client, calls, f"ocr/{label_id}", crop, rules)
        if failed:
            decision = {
                **dict.fromkeys(
                    ("upc", "label_upc", "product", "label_price", "register_price"), ""
                ),
                "status": "not_checked",
                "reason": f"Label not read: {failed}",
            }
        else:
            decision = decide(parse_label(text, rules["pricing"]), prices, rules)
        result.rows.append(
            {
                "photo": photo.name,
                "label": label_id,
                **decision,
                "crop": f"crops/{label_id}.jpg",
                "box_xywh": [round(float(value), 1) for value in obj["bbox"]],
                "score": round(float(obj["score"]), 4),
                "ocr_text": text,
            }
        )
    return result


def run(
    client: SIEClient,
    rules: dict[str, Any],
    output_dir: Path | None,
    data_dir: Path = DATA_DIR,
    prices_path: Path = PRICES_PATH,
) -> list[PhotoResult]:
    """Check every photo in `data_dir` against `prices_path`.

    Nothing is written until every photo is done, so a run that stops on an SIE refusal or on
    every call failing leaves no partial reports behind.
    """
    found = photos(data_dir)
    if not found:
        # An empty or misnamed data folder would otherwise write empty reports that look clean.
        raise SystemExit(
            f"No photos ({', '.join(PHOTO_SUFFIXES)}) in {data_dir}, so there is nothing to "
            "check. No outputs were written."
        )
    prices = load_prices(prices_path)
    calls = CallLog()
    results = [check_photo(client, calls, photo, prices, rules) for photo in found]
    calls.stop_if_all_failed()
    if calls.failures:
        print(
            f"{len(calls.failures)} of {calls.made} SIE calls failed; their photos or labels go "
            f"to review. First: {short(calls.failures[0][1])}"
        )
    if output_dir is not None:
        write_outputs(output_dir, results)
    return results


def _write_csv(
    path: Path, rows: list[dict[str, Any]], fields: tuple[str, ...] = REPORT_FIELDS
) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def photo_review_row(summary: dict[str, Any]) -> dict[str, Any]:
    """A needs_review.csv row for a photo the recipe could not check label by label."""
    return {"photo": summary["photo"], "status": summary["status"], "reason": summary["reason"]}


def write_outputs(output_dir: Path, results: list[PhotoResult]) -> None:
    rows = [row for result in results for row in result.rows]
    review = [
        photo_review_row(result.summary)
        for result in results
        if result.summary["status"] != "checked"
    ] + [row for row in rows if row["status"] in REVIEW_STATUSES]
    output_dir.mkdir(parents=True, exist_ok=True)
    crops_dir = output_dir / "crops"
    for result in results:
        for label_id, crop in result.crops.items():
            crops_dir.mkdir(exist_ok=True)
            crop.save(crops_dir / f"{label_id}.jpg", format="JPEG", quality=95)
    _write_csv(output_dir / "mismatches.csv", [r for r in rows if r["status"] == "mismatch"])
    _write_csv(output_dir / "needs_review.csv", review)
    _write_csv(output_dir / "matches.csv", [r for r in rows if r["status"] == "match"])
    _write_csv(output_dir / "photos.csv", [r.summary for r in results], PHOTO_FIELDS)
    (output_dir / "labels.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    with make_client() as client:
        results = stop_on_refusal(lambda: run(client, load_rules(), output))
    print("\nPHOTOS")
    for result in results:
        summary = result.summary
        print(
            f"  {summary['photo']:<34} {summary['boxes_returned']} boxes returned, "
            f"{summary['labels_read']} read (dropped: {summary['dropped_empty_box']} empty, "
            f"{summary['dropped_low_score']} low score, "
            f"{summary['dropped_too_large']} too large, {summary['dropped_overlap']} overlapping)"
            + (f"  {summary['status']}: {summary['reason']}" if summary["reason"] else "")
        )
    rows = [row for result in results for row in result.rows]
    for status in STATUSES:
        group = [row for row in rows if row["status"] == status]
        print(f"\n{status.upper()} ({len(group)})")
        for row in group:
            name = row["product"] or row["label_upc"] or "-"
            print(f"  {row['label']:<34} {name:<62} {row['reason']}")
    print(f"\nreports: {output}/mismatches.csv, needs_review.csv, matches.csv, photos.csv")
    print("A person checks the shelf and the register before any label or price is changed.")


if __name__ == "__main__":
    main()
