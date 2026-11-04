"""Check each shelf tag in one shelf photo: is its product there, the right one, at the right price?

Use case:  Shelf check from one photo: for each shelf tag, is the product above it in stock,
           is it the product the tag names, and does the tag's regular price match the catalog?
Who:       Store associates and brand reps walking an aisle (merchandisers, field reps).
Problem:   An empty slot, a neighbor's product pushed into the wrong slot or a stale tag each
           cost a sale or a complaint. Today someone walks the aisle with a handheld scanner,
           or by eye, tag by tag.
Input:     Shelf photos in data/ and the catalog data/catalog.csv (UPC, brand, product name,
           regular price). Rules, prompts and thresholds in rules.yaml.
SIE pipeline (three models):
           extract           IDEA-Research/grounding-dino-base  called once per photo: tag and
                                                                package boxes
           extract           zai-org/GLM-OCR                    called once per kept tag: the
                                                                tag text
           chat_completions  Qwen/Qwen3.8-27B-FP8               called once per tag found in
                                                                the catalog with at least two
                                                                candidates, with a picture of
                                                                its slot (every tag blanked
                                                                out): which listed product is
                                                                there, in JSON
           (Python groups the tags into shelf edges and slots, parses each tag's UPC, regular
           price and any promotion, looks the UPC up in catalog.csv, lists the candidate
           products, checks the vision model's answer against a strict schema, and turns it,
           the package boxes and the price comparison into a status.)
Output:    tags.csv (every tag with its status: in_place, out_of_stock, wrong_product,
           price_mismatch or review, and the reason), findings.csv (the tags that aren't
           in_place, plus photos that couldn't be checked), photos.csv (boxes found and dropped
           per photo), tags.json (boxes, slots, candidates, answers and OCR text), crops/ and
           annotated/ photos. A person walks to the shelf and fixes it; the recipe doesn't
           decide how.
Why SIE:   All three models run on SIE Cloud. The vision model only picks from a list and says
           what it saw; plain Python you can audit line by line turns that, the boxes and the
           price comparison into a status, and no generative model makes the decision.
Runs on:   SIE Cloud.

    uv run recipe.py
"""

from __future__ import annotations

import argparse
import base64
import csv
import difflib
import io
import json
import os
import re
import statistics
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import yaml
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageOps
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
CATALOG_PATH = DATA_DIR / "catalog.csv"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
PHOTO_SUFFIXES = (".webp", ".jpg", ".jpeg", ".png")
# A failed SIE call's message is cut to this many characters in reasons and the summary line.
ERROR_CHARS = 200
# A CSV cell starting with one of these is a formula in a spreadsheet; it gets a leading '.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")

# Tag statuses, in the order the run prints them. Each names what the checks found; a person
# at the shelf decides what to do about it (restock, move a product, reprint a tag).
IN_PLACE, OUT_OF_STOCK, WRONG_PRODUCT, PRICE_MISMATCH, REVIEW = (
    "in_place",
    "out_of_stock",
    "wrong_product",
    "price_mismatch",
    "review",
)
STATUSES = (OUT_OF_STOCK, WRONG_PRODUCT, PRICE_MISMATCH, REVIEW, IN_PLACE)
# What the slot check found, before it's combined with the price check.
FACING_IN_PLACE, FACING_EMPTY, FACING_WRONG, FACING_UNREADABLE = (
    "in_place",
    "out_of_stock",
    "wrong_product",
    "unreadable",
)
# The vision model's answers besides a candidate letter.
NONE, CANT_TELL = "none", "cant_tell"
# The columns of photos.csv, one row per photo. Its status is `checked`, `no_tags` (the
# detector kept no tag) or `not_checked` (the detect call failed); the last two also go to
# findings.csv. Each dropped box is counted under one reason.
PHOTO_FIELDS = (
    "photo",
    "status",
    "boxes_returned",
    "tags_kept",
    "packages_kept",
    "packages_in_slots",
    "dropped_low_score",
    "dropped_too_large",
    "dropped_overlap",
    "dropped_contained",
    "dropped_on_tag",
    "reason",
)
TAG_FIELDS = (
    "photo",
    "tag",
    "status",
    "findings",
    "upc",
    "tag_upc",
    "brand",
    "product",
    "tag_price",
    "catalog_price",
    "promotion",
    "shown",
    "evidence",
    "tag_crop",
    "slot_crop",
    "reason",
)


@dataclass(frozen=True)
class CatalogRow:
    upc: str
    brand: str
    product: str
    price: Decimal


@dataclass
class TagReading:
    """What the parser found in one tag's OCR text."""

    upcs: list[str] = field(default_factory=list)
    prices: list[Decimal] = field(default_factory=list)
    multi_buy: str = ""
    promotion: str = ""  # the promotion's text, when the tag carries one
    words: list[str] = field(default_factory=list)


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def load_catalog(path: Path = CATALOG_PATH) -> list[CatalogRow]:
    with path.open(encoding="utf-8", newline="") as handle:
        return [
            CatalogRow(
                row["upc"].strip(),
                row["brand"].strip(),
                row["product"].strip(),
                Decimal(row["regular_price"]),
            )
            for row in csv.DictReader(handle)
        ]


def open_photo(path: Path) -> Image.Image:
    """The photo upright (its EXIF orientation applied, as a phone camera records it), in RGB."""
    with Image.open(path) as opened:
        return ImageOps.exif_transpose(opened).convert("RGB")


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
    those photos and tags go to review."""
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
    """The error for a per-tag reason or the summary line, its message cut short."""
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
    a rate limit, a reply without `key`) is logged and its photo or tag goes to review; the run
    continues. A refusal every call would repeat (see `is_fatal`) stops the run. An error that
    isn't an SIE error is a bug in this code and is raised as is.
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


def guarded[T](calls: CallLog, model: str, work: Callable[[], T]) -> tuple[T | None, str]:
    """Run one SIE call that isn't a plain extract; return its result, or None and the reason
    it failed, with the same rule as `call_sie`."""
    calls.made += 1
    try:
        return work(), ""
    except SIEError as error:
        if is_fatal(error):
            raise SIEStopped(stop_message(error, model)) from error
        calls.failures.append((model, error))
        return None, f"the SIE call failed: {short(error)}"


# --- Detect: tags and packages -----------------------------------------------------------------


def _jpeg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def _box(obj: dict[str, Any]) -> list[float]:
    return [float(value) for value in obj["bbox"]]


def _intersection(first: list[float], second: list[float]) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[0] + first[2], second[0] + second[2])
    bottom = min(first[1] + first[3], second[1] + second[3])
    return max(0.0, right - left) * max(0.0, bottom - top)


def _iou(first: list[float], second: list[float]) -> float:
    intersection = _intersection(first, second)
    union = first[2] * first[3] + second[2] * second[3] - intersection
    return intersection / union if union > 0 else 0.0


def _inside(box: list[float], other: list[float]) -> float:
    """The share of `box`'s area that lies inside `other`."""
    area = box[2] * box[3]
    return _intersection(box, other) / area if area > 0 else 0.0


def detect(
    client: SIEClient, calls: CallLog, photo: Path, image: Image.Image, rules: dict[str, Any]
) -> tuple[list[dict[str, Any]], str]:
    """Every tag and package box the detector returns for one photo, or [] and why it failed."""
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


def select_boxes(
    objects: list[dict[str, Any]], image_size: tuple[int, int], detection: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Counter[str]]:
    """Split the boxes into kept tags and kept packages, and count every box dropped and why.

    Tags: a score of at least `tag_min_score`, one box per tag. Packages: a score of at least
    `product_min_score`, not most of the photo, one box per package, not a picture printed
    inside a bigger package box, and not a tag the detector also called a package. A box with
    no width or height counts as `low_score`.
    """
    image_area = image_size[0] * image_size[1]
    tags: list[dict[str, Any]] = []
    packages: list[dict[str, Any]] = []
    dropped: Counter[str] = Counter()
    ranked = sorted(objects, key=lambda item: float(item["score"]), reverse=True)
    for obj in ranked:
        if obj.get("label") != detection["tag_label"]:
            continue
        box, score = _box(obj), float(obj["score"])
        if box[2] <= 0 or box[3] <= 0 or score < detection["tag_min_score"]:
            dropped["low_score"] += 1
        elif any(_iou(box, _box(other)) >= detection["dedupe_iou"] for other in tags):
            dropped["overlap"] += 1
        else:
            tags.append(obj)
    for obj in ranked:
        if obj.get("label") == detection["tag_label"]:
            continue
        box, score = _box(obj), float(obj["score"])
        if box[2] <= 0 or box[3] <= 0 or score < detection["product_min_score"]:
            dropped["low_score"] += 1
        elif box[2] * box[3] > image_area * detection["max_product_area"]:
            dropped["too_large"] += 1
        elif any(_iou(box, _box(other)) >= detection["dedupe_iou"] for other in packages):
            dropped["overlap"] += 1
        elif any(_inside(box, _box(other)) >= detection["contained"] for other in packages):
            dropped["contained"] += 1
        elif any(_inside(box, _box(tag)) >= detection["on_tag"] for tag in tags):
            dropped["on_tag"] += 1
        else:
            packages.append(obj)
    return tags, packages, dropped


# --- Shelf edges and slots (plain Python) -----------------------------------------------------


@dataclass
class Slot:
    """One tag, the part of the shelf it prices and the package boxes standing in it."""

    tag: dict[str, Any]
    row: int
    left: float
    right: float
    top: float
    bottom: float
    packages: list[dict[str, Any]] = field(default_factory=list)


def shelf_rows(tags: list[dict[str, Any]], image_height: int, row_gap: float) -> list[list[Any]]:
    """Group the tags into shelf edges, top to bottom, each sorted left to right.

    A tag whose top is within `row_gap` of the photo height of the row's first tag hangs on the
    same shelf edge.
    """
    rows: list[list[dict[str, Any]]] = []
    for tag in sorted(tags, key=lambda item: _box(item)[1]):
        if rows and _box(tag)[1] - _box(rows[-1][0])[1] <= row_gap * image_height:
            rows[-1].append(tag)
        else:
            rows.append([tag])
    return [sorted(row, key=lambda item: _box(item)[0]) for row in rows]


def assign_slots(
    tags: list[dict[str, Any]],
    packages: list[dict[str, Any]],
    image_size: tuple[int, int],
    shelf: dict[str, Any],
) -> tuple[list[Slot], int]:
    """Give each tag its slot and the package boxes standing in it; return the slots in
    reading order and how many packages stand in at least one slot.

    The edge of a row is the median top of its tags. A slot runs from its tag's left edge to
    the next tag's left edge (the last one to the photo's right edge), and from the edge above
    (the top of the photo for the first row) down to its own edge, each plus `band_slack` of
    the photo height. A package stands on the first edge, top to bottom, whose edge plus the
    slack is at or below its bottom. It counts as present in a slot of that edge when its
    horizontal centre lies in the slot, or when it covers at least `min_slot_cover` of the
    slot's width: a share of the slot, not of the box, so a wide neighbour reaching a little way
    into an empty slot doesn't fill it. A package below the last edge belongs to a tag out of
    frame.
    """
    width, height = image_size
    slack = shelf["band_slack"] * height
    rows = shelf_rows(tags, height, shelf["row_gap"])
    edges = [statistics.median(_box(tag)[1] for tag in row) for row in rows]
    slots: list[Slot] = []
    for number, row in enumerate(rows):
        top = edges[number - 1] + slack if number else 0.0
        bottom = min(float(height), edges[number] + slack)
        for index, tag in enumerate(row):
            right = _box(row[index + 1])[0] if index + 1 < len(row) else float(width)
            slots.append(Slot(tag, number, _box(tag)[0], right, top, bottom))
    placed = 0
    for package in packages:
        box = _box(package)
        bottom = box[1] + box[3]
        row_number = next((n for n, edge in enumerate(edges) if bottom <= edge + slack), None)
        if row_number is None or box[2] <= 0:
            continue
        centre = box[0] + box[2] / 2
        inside = False
        for slot in slots:
            if slot.row != row_number:
                continue
            overlap = max(0.0, min(box[0] + box[2], slot.right) - max(box[0], slot.left))
            width_of_slot = slot.right - slot.left
            covers = width_of_slot > 0 and overlap >= shelf["min_slot_cover"] * width_of_slot
            if slot.left <= centre < slot.right or covers:
                slot.packages.append(package)
                inside = True
        placed += inside
    return slots, placed


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


def read_text(
    client: SIEClient, calls: CallLog, key: str, crop: Image.Image, rules: dict[str, Any]
) -> tuple[str, str]:
    """The OCR text of one crop ("" when the model read nothing), and why the call failed
    ("" when it didn't)."""
    item = Item(id=key, images=[_jpeg(crop)])
    entities, failed = call_sie(client, calls, rules["models"]["ocr"], item, "entities")
    for entity in entities:
        if entity.get("label") == "markdown":
            return str(entity.get("text", "")), failed
    return "", failed


# --- Parse the tag (plain Python) -------------------------------------------------------------

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


def parse_tag(text: str, tag_rules: dict[str, Any]) -> TagReading:
    """Pull the UPCs, the regular retail price(s) and any promotion out of a tag's OCR text."""
    reading = TagReading()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for match in _UPC.finditer(text):
        upc = "".join(match.groups())
        if upc not in reading.upcs:
            reading.upcs.append(upc)
    for index, line in enumerate(lines):
        if _has_marker(line, tag_rules["promo_markers"]):
            # Everything from here down is the promotion: its prices are not the regular price.
            reading.promotion = " / ".join(lines[index:])
            break
        next_line = lines[index + 1] if index + 1 < len(lines) else ""
        if _has_marker(line, tag_rules["unit_markers"]) or _starts_with_marker(
            next_line, tag_rules["unit_markers"]
        ):
            continue  # unit price
        if not reading.multi_buy and (multi := _MULTI_BUY.search(line)):
            reading.multi_buy = multi.group(0).replace(" ", "")
            continue
        for amount in _PRICE.findall(line):
            price = Decimal(amount)
            if price not in reading.prices:
                reading.prices.append(price)
    reading.words = words(text)
    return reading


def upc_key(digits: str) -> str:
    """A tag prints the UPC without its check digit; compare without leading zeros."""
    return digits.lstrip("0")


def name_score(row: CatalogRow, tag_words: list[str], similarity: float) -> float:
    """Share of the catalog brand and name words (numbers and short words left out) found on
    the tag."""
    wanted = _name_words(f"{row.brand} {row.product}")
    if not wanted:
        return 0.0
    return sum(_found(word, tag_words, similarity) for word in wanted) / len(wanted)


def find_in_catalog(
    reading: TagReading, catalog: list[CatalogRow], tag_rules: dict[str, Any], similarity: float
) -> tuple[CatalogRow | None, str]:
    """The catalog row for this tag and how it was found, or None and why.

    The UPC decides. Only when the tag shows no UPC at all does the product name on the tag
    decide, and only when one catalog name clearly beats the rest and no longer catalog name
    holds all its words plus words that are also on the tag ("Muesli" on an "Old Country Style
    Muesli" tag).
    """
    if len(reading.upcs) > 1:
        return None, f"More than one UPC in the tag crop ({', '.join(reading.upcs)})."
    scored = sorted(
        ((name_score(row, reading.words, similarity), row) for row in catalog),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if reading.upcs:
        tag_upc = reading.upcs[0]
        for row in catalog:
            if upc_key(tag_upc) in (upc_key(row.upc), upc_key(row.upc[:-1])):
                return row, f"Matched by UPC {tag_upc}."
        # Never fall back to the name here: a UPC that is not in the catalog may be a new
        # product or an OCR misread, and a person must decide which. The closest name is a hint.
        hint = ""
        if scored and scored[0][0] >= tag_rules["min_name_score"]:
            best_score, best = scored[0]
            hint = f" Closest name in catalog.csv: {best.brand} {best.product} ({best_score:.2f})."
        return None, f"UPC {tag_upc} is not in catalog.csv.{hint}"
    if not scored or not reading.words:
        return None, "No text could be read from the tag."
    best_score, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < tag_rules["min_name_score"]:
        return None, f"No UPC read and no catalog name matched the tag (best {best_score:.2f})."
    if best_score - runner_up < tag_rules["min_name_margin"]:
        return None, (
            f"No UPC read and the tag's name matches more than one catalog product "
            f"({best_score:.2f} vs {runner_up:.2f})."
        )
    longer = wider_name(best, reading.words, catalog, similarity)
    if longer is not None:
        return None, (
            f"No UPC read and the tag's name fits {display_name(best)} ({best_score:.2f}), but "
            f"also the longer {display_name(longer)}, whose extra words are on the tag."
        )
    return best, f"No UPC read; matched by the name on the tag ({best_score:.2f})."


def wider_name(
    best: CatalogRow, tag_words: list[str], catalog: list[CatalogRow], similarity: float
) -> CatalogRow | None:
    """A catalog row whose name holds every word of `best`'s name plus at least one more word
    that is on the tag, or None. A short name always scores 1.00 on a longer name's tag, so the
    short name winning says nothing when the tag also shows the longer name's extra words."""
    wanted = set(_name_words(display_name(best)))
    for row in catalog:
        theirs = set(_name_words(display_name(row)))
        if wanted < theirs and any(_found(word, tag_words, similarity) for word in theirs - wanted):
            return row
    return None


# --- Words, for the name printed on the tag (plain Python) ----------------------------------


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("'", "").replace(chr(0x2019), ""))


def _name_words(text: str) -> list[str]:
    """The words of a brand or product name that can identify it: no numbers or short words."""
    return [word for word in dict.fromkeys(words(text)) if len(word) > 2 and not word.isdigit()]


def _found(word: str, seen: list[str], similarity: float) -> bool:
    return any(difflib.SequenceMatcher(None, word, other).ratio() >= similarity for other in seen)


# --- Right product? (vision model) -----------------------------------------------------------


def display_name(row: CatalogRow) -> str:
    return f"{row.brand} {row.product}"


def candidates(
    row: CatalogRow, others: list[CatalogRow], catalog: list[CatalogRow], minimum: int
) -> list[tuple[str, str]]:
    """The products the vision model picks from: the tag's own product and the product of every
    other tag found in the same photo, one per distinct name, labelled A, B, C...

    They're in alphabetical order, so where the tag's own product lands depends only on its
    name, never on which tag is being asked about. Two catalog rows with the same name are one
    option: the photo can't tell them apart, and neither can the model. When that leaves only
    the tag's own product (a list of one gives the answer away), the list is padded with other
    catalog products, the same brand first, up to `minimum` names. A catalog with one product
    name gives a list of one; the caller sends that tag to review.
    """
    names = {display_name(other) for other in [row, *others]}
    if len(names) == 1:
        same_brand = [other for other in catalog if other.brand.casefold() == row.brand.casefold()]
        rest = [other for other in catalog if other.brand.casefold() != row.brand.casefold()]
        for other in same_brand + rest:
            if len(names) >= minimum:
                break
            names.add(display_name(other))
    ordered = sorted(names, key=str.casefold)
    return [(chr(ord("A") + index), name) for index, name in enumerate(ordered)]


def blank_tags(image: Image.Image, tags: list[dict[str, Any]]) -> Image.Image:
    """A copy of the photo with every kept tag painted gray, so the model never sees a tag's
    name or price."""
    blanked = image.copy()
    draw = ImageDraw.Draw(blanked)
    for tag in tags:
        x, y, w, h = _box(tag)
        draw.rectangle((x, y, x + w, y + h), fill=(128, 128, 128))
    return blanked


def slot_region(slot: Slot, image_size: tuple[int, int], pad: float) -> list[int]:
    """The slot's part of the shelf, `pad` of its width wider on each side."""
    extra = (slot.right - slot.left) * pad
    return [
        max(0, round(slot.left - extra)),
        max(0, round(slot.top)),
        min(image_size[0], round(slot.right + extra)),
        min(image_size[1], round(slot.bottom)),
    ]


def ask_slot(
    client: SIEClient,
    calls: CallLog,
    crop: Image.Image,
    options: list[tuple[str, str]],
    rules: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """Ask the vision model which listed product stands in the slot.

    Return the chat reply's choice (finish reason and content), or None and why the call
    failed. A reply with no message in it is a failed call on the transient path.
    """
    model = rules["models"]["vision"]
    vision = rules["vision"]
    listing = "\n".join(f"{letter}: {name}" for letter, name in options)
    prompt = vision["prompt"].format(candidates=listing)
    allowed = [letter for letter, _ in options] + [NONE, CANT_TELL]
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["shown", "evidence"],
        "properties": {
            "shown": {"type": "string", "enum": allowed},
            "evidence": {"type": "string"},
        },
    }
    small = crop.copy()
    small.thumbnail((vision["max_edge_px"], vision["max_edge_px"]), Image.Resampling.LANCZOS)
    image = {
        "type": "image_url",
        "image_url": {
            "url": "data:image/jpeg;base64," + base64.b64encode(_jpeg(small)).decode("ascii")
        },
    }

    def call() -> dict[str, Any]:
        response: Any = client.chat_completions(
            model,
            [{"role": "user", "content": [image, {"type": "text", "text": prompt}]}],
            max_completion_tokens=vision["max_completion_tokens"],
            temperature=0.0,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "slot", "schema": schema, "strict": True},
            },
            provision_timeout_s=WAIT_S,
        )
        try:
            choice = response["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise SIEError("the reply has no `choices[0].message.content`") from None
        return {"finish_reason": choice.get("finish_reason"), "content": content}

    return guarded(calls, model, call)


def parse_answer(choice: dict[str, Any], allowed: list[str]) -> tuple[dict[str, str] | None, str]:
    """The model's answer, checked strictly, or None and what was wrong with the reply. Nothing
    is repaired or guessed: a reply that breaks the schema is a review reason, not an answer."""
    if choice.get("finish_reason") not in (None, "stop"):
        return None, f"the reply was cut off ({choice.get('finish_reason')})"
    try:
        answer = json.loads(choice.get("content") or "")
    except json.JSONDecodeError:
        return None, "the reply is not JSON"
    if not isinstance(answer, dict):
        return None, "the reply is not a JSON object"
    if set(answer) != {"shown", "evidence"}:
        return None, f"the reply's keys are {sorted(answer)}, not ['evidence', 'shown']"
    if answer["shown"] not in allowed:
        return None, f"shown is {answer['shown']!r}, not one of {', '.join(allowed)}"
    if not isinstance(answer["evidence"], str) or not answer["evidence"].strip():
        return None, "the reply gives no evidence"
    return {"shown": str(answer["shown"]), "evidence": answer["evidence"].strip()}, ""


def check_facing(
    answer: dict[str, str] | None,
    problem: str,
    options: list[tuple[str, str]],
    row: CatalogRow,
    boxes: int,
) -> tuple[str, str]:
    """What the slot holds, from the model's answer and the package boxes in the slot.

    Both shelf findings need the model and the detector to agree. The tag's own product, with
    a package box in the slot: in_place. Another listed product, with a box: wrong_product.
    "none" with no box: out_of_stock. A product named with no box (the model may be looking at
    a neighbor's package at the edge of the picture), "none" with a box, "cant_tell", or no
    usable answer: unreadable, so a person looks. A false in_place on an empty slot is the one
    mistake an inventory check can't afford.
    """
    if answer is None:
        return FACING_UNREADABLE, problem
    names = dict(options)
    own = next(letter for letter, name in options if name == display_name(row))
    shown, evidence = answer["shown"], answer["evidence"]
    if not evidence.endswith((".", "!", "?")):
        evidence += "."
    if shown == NONE:
        if boxes == 0:
            return FACING_EMPTY, f"The slot is empty and no package box stands in it: {evidence}"
        return FACING_UNREADABLE, (
            f"The vision model sees an empty slot, but the detector found {boxes} package "
            f"box(es) in it: {evidence}"
        )
    if shown == CANT_TELL:
        return FACING_UNREADABLE, f"Can't confirm the product from the photo: {evidence}"
    if boxes == 0:
        return FACING_UNREADABLE, (
            f"The vision model named {names[shown]} but no product box is in the slot; check "
            f"the slot. It saw: {evidence}"
        )
    if shown == own:
        return FACING_IN_PLACE, f"The slot shows {names[own]}: {evidence}"
    return FACING_WRONG, f"Looks like {names[shown]}: {evidence}"


# --- Right price? and the tag's status (plain Python) -----------------------------------------


def check_price(reading: TagReading, row: CatalogRow, tolerance: float) -> tuple[str, str]:
    """`agrees`, `differs` or `unreadable`, and the reason."""
    if not reading.prices:
        if reading.multi_buy:
            return "unreadable", f"Only a multi-buy price {reading.multi_buy}, no single price."
        return "unreadable", "No regular price read on the tag."
    if len(reading.prices) > 1:
        found = ", ".join(f"${price}" for price in reading.prices)
        return "unreadable", f"More than one regular price on the tag ({found})."
    price = reading.prices[0]
    difference = price - row.price
    if abs(difference) > Decimal(str(tolerance)):
        sign = "+" if difference > 0 else "-"
        return "differs", f"Tag ${price}, catalog ${row.price} ({sign}${abs(difference)})."
    return "agrees", f"Tag price ${price} agrees with the catalog."


def tag_status(facing: str, price: str) -> tuple[str, list[str]]:
    """Combine the slot check and the price check into one status and every finding.

    The shelf findings come first because they're what a person sees at the shelf; a price that
    differs is still listed in the findings.
    """
    findings = [facing] if facing in (FACING_EMPTY, FACING_WRONG) else []
    if price == "differs":
        findings.append(PRICE_MISMATCH)
    if findings:
        return findings[0], findings
    if facing == FACING_UNREADABLE or price == "unreadable":
        return REVIEW, [REVIEW]
    return IN_PLACE, []


def lookup(
    tag_text: str, catalog: list[CatalogRow], rules: dict[str, Any]
) -> tuple[TagReading, CatalogRow | None, str]:
    """The tag's parsed text, its catalog row (or None) and how it was found (or why not)."""
    reading = parse_tag(tag_text, rules["tag"])
    row, how = find_in_catalog(reading, catalog, rules["tag"], rules["tag"]["name_similarity"])
    return reading, row, how


def decide(
    reading: TagReading,
    row: CatalogRow | None,
    how: str,
    facing: tuple[str, str],
    rules: dict[str, Any],
) -> dict[str, Any]:
    """Status, catalog row and reason for one tag, from its catalog lookup, the slot check and
    the price check."""
    decision: dict[str, Any] = {
        "upc": row.upc if row else "",
        "tag_upc": ", ".join(reading.upcs),
        "brand": row.brand if row else "",
        "product": row.product if row else "",
        "tag_price": ", ".join(str(price) for price in reading.prices),
        "catalog_price": str(row.price) if row else "",
        "promotion": reading.promotion or reading.multi_buy,
    }
    if row is None:
        return {**decision, "status": REVIEW, "findings": REVIEW, "reason": how}
    price, price_reason = check_price(reading, row, rules["pricing"]["tolerance"])
    status, findings = tag_status(facing[0], price)
    promotion = (
        f" The tag also carries a promotion: {decision['promotion']}."
        if decision["promotion"]
        else ""
    )
    return {
        **decision,
        "status": status,
        "findings": "; ".join(findings),
        "reason": f"{how} {facing[1]} {price_reason}{promotion}",
    }


# --- Run --------------------------------------------------------------------------------------


@dataclass
class PhotoResult:
    """One photo's counts, its tag rows, its crops and boxes (written at the end)."""

    summary: dict[str, Any]
    rows: list[dict[str, Any]]
    crops: dict[str, Image.Image]
    image: Image.Image | None = None


def _blank_decision(reason: str) -> dict[str, Any]:
    blank = ("upc", "tag_upc", "brand", "product", "tag_price", "catalog_price", "promotion")
    return {**dict.fromkeys(blank, ""), "status": REVIEW, "findings": REVIEW, "reason": reason}


def check_photo(
    client: SIEClient,
    calls: CallLog,
    photo: Path,
    catalog: list[CatalogRow],
    rules: dict[str, Any],
) -> PhotoResult:
    """Detect, slot, read and decide every tag on one photo.

    Every tag is read first, because each slot question lists the products of all the photo's
    tags found in the catalog.
    """
    image = open_photo(photo)
    objects, failed = detect(client, calls, photo, image, rules)
    tags, packages, dropped = select_boxes(objects, image.size, rules["detection"])
    slots, placed = assign_slots(tags, packages, image.size, rules["shelf"])
    summary: dict[str, Any] = {
        "photo": photo.name,
        "status": "checked",
        "boxes_returned": len(objects),
        "tags_kept": len(tags),
        "packages_kept": len(packages),
        "packages_in_slots": placed,
        **{f"dropped_{reason}": dropped[reason] for reason in ("low_score", "too_large")},
        **{f"dropped_{reason}": dropped[reason] for reason in ("overlap", "contained", "on_tag")},
        "reason": "",
    }
    if failed:
        summary.update(status="not_checked", reason=f"Photo not checked: {failed}")
    elif not tags:
        summary.update(
            status="no_tags",
            reason="The detector kept no shelf tag; check the photo, and photograph the shelf "
            "again if tags are on it.",
        )
    result = PhotoResult(summary, [], {}, image)
    crop_rules = rules["crop"]
    read: list[tuple[str, str, str, TagReading, CatalogRow | None, str]] = []
    for number, slot in enumerate(slots, start=1):
        tag_id = f"{photo.stem}-T{number:02d}"
        box = crop_box(slot.tag["bbox"], image.size, crop_rules["pad_fraction"])
        tag_crop = make_crop(image, box, crop_rules)
        result.crops[tag_id] = tag_crop
        tag_text, tag_failed = read_text(client, calls, f"ocr/{tag_id}", tag_crop, rules)
        reading, row, how = lookup(tag_text, catalog, rules)
        read.append((tag_id, tag_text, tag_failed, reading, row, how))
    found = [row for *_, row, _ in read if row is not None]
    blanked = blank_tags(image, tags) if slots else image
    for slot, (tag_id, tag_text, tag_failed, reading, row, how) in zip(slots, read, strict=True):
        region = slot_region(slot, image.size, rules["slot_crop"]["pad_fraction"])
        options: list[tuple[str, str]] = []
        answer: dict[str, str] | None = None
        content = ""
        if tag_failed:
            decision = _blank_decision(f"Tag not read: {tag_failed}")
        elif row is None:
            decision = decide(reading, row, how, (FACING_UNREADABLE, ""), rules)
        else:
            others = [other for other in found if other is not row]
            options = candidates(row, others, catalog, rules["vision"]["min_candidates"])
            if len(options) < 2:
                # A list of one tells the model the answer; a person looks instead.
                options = []
                facing = (
                    FACING_UNREADABLE,
                    "Only one candidate; can't ask without giving the answer away.",
                )
            else:
                crop = blanked.crop((region[0], region[1], region[2], region[3]))
                result.crops[f"{tag_id}-slot"] = crop
                choice, call_failed = ask_slot(client, calls, crop, options, rules)
                if choice is None:
                    problem = f"Slot not checked: {call_failed}"
                else:
                    content = str(choice.get("content") or "")
                    allowed = [letter for letter, _ in options] + [NONE, CANT_TELL]
                    answer, why = parse_answer(choice, allowed)
                    problem = f"The vision model's reply couldn't be used: {why}."
                facing = check_facing(answer, problem, options, row, len(slot.packages))
            decision = decide(reading, row, how, facing, rules)
        result.rows.append(
            {
                "photo": photo.name,
                "tag": tag_id,
                **decision,
                "shown": answer["shown"] if answer else "",
                "evidence": answer["evidence"] if answer else "",
                "tag_crop": f"crops/{tag_id}.jpg",
                "slot_crop": f"crops/{tag_id}-slot.jpg" if options else "",
                "shelf_edge": slot.row + 1,
                "slot_xyxy": region,
                "box_xywh": [round(v, 1) for v in _box(slot.tag)],
                "score": round(float(slot.tag["score"]), 4),
                "tag_text": tag_text,
                "candidates": dict(options),
                "vision_reply": content,
                "packages_in_slot": [[round(v, 1) for v in _box(p)] for p in slot.packages],
            }
        )
    return result


def run(
    client: SIEClient,
    rules: dict[str, Any],
    output_dir: Path | None,
    data_dir: Path = DATA_DIR,
    catalog_path: Path = CATALOG_PATH,
) -> tuple[list[PhotoResult], CallLog]:
    """Check every photo in `data_dir` against `catalog_path`.

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
    catalog = load_catalog(catalog_path)
    calls = CallLog()
    results = [check_photo(client, calls, photo, catalog, rules) for photo in found]
    calls.stop_if_all_failed()
    if calls.failures:
        print(
            f"{len(calls.failures)} of {calls.made} SIE calls failed; their photos or tags go "
            f"to review. First: {short(calls.failures[0][1])}"
        )
    if output_dir is not None:
        write_outputs(output_dir, results)
    return results, calls


def csv_safe(value: Any) -> str:
    """A leading ' on a cell a spreadsheet would run as a formula; anything else as written."""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: tuple[str, ...]) -> None:
    """Every cell through `csv_safe`: the vision model's evidence and the tag's promotion text
    come from the photo, and a cell like `=HYPERLINK(...)` must not run in a spreadsheet."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({key: csv_safe(value) for key, value in row.items()} for row in rows)


COLORS = {
    IN_PLACE: (0, 160, 70),
    OUT_OF_STOCK: (220, 30, 30),
    WRONG_PRODUCT: (230, 120, 0),
    PRICE_MISMATCH: (150, 40, 200),
    REVIEW: (40, 90, 220),
}


def annotate(result: PhotoResult) -> Image.Image | None:
    """The photo with each tag boxed in its status color and labelled with its number and
    status, the slot region the vision model saw outlined in the same color and the package
    boxes in it in gray; shrunk to 1600 pixels wide."""
    if result.image is None:
        return None
    image = result.image.copy()
    draw = ImageDraw.Draw(image)
    width = max(4, image.width // 400)
    for row in result.rows:
        color = COLORS[row["status"]]
        x, y, w, h = row["box_xywh"]
        draw.rectangle((x, y, x + w, y + h), outline=color, width=width * 2)
        if row["slot_crop"]:
            draw.rectangle(tuple(row["slot_xyxy"]), outline=color, width=width)
        for px, py, pw, ph in row["packages_in_slot"]:
            draw.rectangle((px, py, px + pw, py + ph), outline=(128, 128, 128), width=width)
        label = f"{row['tag'][-3:]} {row['status']}"
        # The label sits on a filled band at the top of the tag, so it reads on any background.
        size = image.width // 50
        left, top, right, bottom = draw.textbbox((x, y), label, font_size=size)
        draw.rectangle((left - 6, top - 6, right + 6, bottom + 6), fill=color)
        draw.text((x, y), label, fill=(255, 255, 255), font_size=size)
    scale = 1600 / image.width
    return image.resize((1600, round(image.height * scale)), Image.Resampling.LANCZOS)


def photo_finding_row(summary: dict[str, Any]) -> dict[str, Any]:
    """A findings.csv row for a photo the recipe could not check tag by tag."""
    return {"photo": summary["photo"], "status": summary["status"], "reason": summary["reason"]}


def write_outputs(output_dir: Path, results: list[PhotoResult]) -> None:
    rows = [row for result in results for row in result.rows]
    findings = [
        photo_finding_row(result.summary)
        for result in results
        if result.summary["status"] != "checked"
    ] + [row for row in rows if row["status"] != IN_PLACE]
    output_dir.mkdir(parents=True, exist_ok=True)
    crops_dir = output_dir / "crops"
    annotated_dir = output_dir / "annotated"
    for result in results:
        for crop_id, crop in result.crops.items():
            crops_dir.mkdir(exist_ok=True)
            crop.save(crops_dir / f"{crop_id}.jpg", format="JPEG", quality=90)
        picture = annotate(result) if result.rows else None
        if picture is not None:
            annotated_dir.mkdir(exist_ok=True)
            name = Path(result.summary["photo"]).stem + ".jpg"
            picture.save(annotated_dir / name, format="JPEG", quality=85)
    _write_csv(output_dir / "tags.csv", rows, TAG_FIELDS)
    _write_csv(output_dir / "findings.csv", findings, TAG_FIELDS)
    _write_csv(output_dir / "photos.csv", [r.summary for r in results], PHOTO_FIELDS)
    (output_dir / "tags.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    started = datetime.now(UTC)
    with make_client() as client:
        results, calls = stop_on_refusal(lambda: run(client, load_rules(), output))
    seconds = (datetime.now(UTC) - started).total_seconds()
    print("\nPHOTOS")
    for result in results:
        summary = result.summary
        print(
            f"  {summary['photo']:<30} {summary['tags_kept']} tags, "
            f"{summary['packages_kept']} packages ({summary['packages_in_slots']} in a slot)"
            + (f"  {summary['status']}: {summary['reason']}" if summary["reason"] else "")
        )
    rows = [row for result in results for row in result.rows]
    for status in STATUSES:
        group = [row for row in rows if row["status"] == status]
        print(f"\n{status.upper()} ({len(group)})")
        for row in group:
            name = f"{row['brand']} {row['product']}".strip() or row["tag_upc"] or "-"
            print(f"  {row['tag']:<30} {name[:50]:<50} {row['reason']}")
    print(f"\n{calls.made} SIE calls in {seconds:.0f} s")
    print(f"reports: {output}/tags.csv, findings.csv, photos.csv, tags.json, annotated/")
    print("A person checks the shelf and fixes it: restock, move the product or reprint the tag.")


if __name__ == "__main__":
    main()
