"""Check a returned item's photos against the sale and the store policy, for the returns desk.

Use case:  Return item check: is the item on the counter the one the receipt says was sold, what
           condition is it in, and what does the store policy say about that?
Who:       Store associates at a returns desk, and the managers they call over.
Problem:   The receipt scan proves a sale, not that this box is that sale. Swapped items, a
           cheaper model in the right brand's box and opened or broken goods all come back across
           the same counter, and the associate has a few seconds to notice.
Input:     data/returns.jsonl (one return per line: the sale line the POS pulled from the receipt
           barcode, the return date and the photo file names) and the photos in data/photos/.
           Rules, thresholds and the store policy in rules.yaml.
SIE pipeline (two models):
           extract           zai-org/GLM-OCR       called once per photo: the packaging text
           chat_completions  Qwen/Qwen3.8-27B-FP8  called once per return, with all its photos:
                                                   fixed condition questions, answered in JSON
           (Python reads barcode numbers, model numbers and name words out of the text and
           compares them with the sale line, checks the condition answers against a strict
           schema, and applies the store policy.)
Output:    guidance.json, guidance.csv and guidance.md in runs/<timestamp>/: per return, the
           guidance (accept, inspect or refer) with every reason, the product check, the
           condition answers and the packaging text. It is guidance for the associate, not a
           decision: a person decides every return, and a manager decides every `refer`.
Why SIE:   Both models run on SIE Cloud. The model answers questions; plain Python you can read
           line by line turns the answers and the policy into guidance, and no generative model
           makes the decision. Nothing to host.
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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

import yaml
from dotenv import load_dotenv
from PIL import Image, ImageOps
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
RETURNS_PATH = DATA_DIR / "returns.jsonl"
PHOTOS_DIR = DATA_DIR / "photos"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
# How much of an SIE error message goes into a per-return reason or the summary line.
ERROR_CHARS = 200

# The guidance levels, least to most severe. Each is advice to the associate, never a refund
# decision: `accept` means no check objected, `inspect` means look at the item before deciding,
# `refer` means call a manager.
GUIDANCE = ("accept", "inspect", "refer")
# What the product check can find. `not_checked` means an OCR call failed.
PRODUCT_STATUSES = ("match", "mismatch", "cant_confirm", "not_checked")

# The fixed question set. The model sees only the photos, never the sale line, so its answers
# can't lean toward the product the receipt names.
CONDITION_ANSWERS: dict[str, tuple[str, ...]] = {
    "packaging": ("sealed", "closed", "opened", "none", "unclear"),
    "packaging_damage": ("none", "minor", "major", "not_visible", "unclear"),
    "product_damage": ("none", "minor", "major", "not_visible", "unclear"),
    "contents": ("complete", "incomplete", "not_visible", "unclear"),
}
CONDITION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [*CONDITION_ANSWERS, "evidence"],
    "properties": {
        **{
            key: {"type": "string", "enum": list(values)}
            for key, values in CONDITION_ANSWERS.items()
        },
        "evidence": {"type": "string"},
    },
}
PROMPT = """\
These photos were taken at a store's returns desk. They show one item a customer is bringing \
back, with whatever packaging came with it. Look only at what the photos show, and answer each \
question with one of its allowed values. Don't guess: if a photo doesn't show something clearly, \
say "unclear" or "not_visible".

- packaging: "sealed" if the retail packaging is closed and a factory seal or shrink wrap is \
visibly intact; "closed" if it is closed with no sign it was opened, but no seal is visible; \
"opened" if it has been opened (flaps open, seal or wrap cut or torn, contents out); "none" if no \
retail packaging is in the photos; "unclear" otherwise. A plain shipping carton counts as \
packaging.
- packaging_damage: damage to the packaging itself. "none"; "minor" for scuffs, creases or worn \
corners; "major" for crushing, tears, holes, dents or water damage; "not_visible" if there is no \
packaging; "unclear".
- product_damage: damage to the product itself. "none"; "minor" for light scuffs or scratches; \
"major" for cracks, breaks, dents or missing parts; "not_visible" if the product can't be seen \
(for example it is still inside closed packaging); "unclear".
- contents: only for opened packaging. "complete" if the contents look complete and back in the \
packaging; "incomplete" if something looks missing or has been taken out; "not_visible" if the \
packaging is not opened or the contents can't be seen; "unclear".
- evidence: one or two short sentences saying what in the photos supports these answers."""

CSV_FIELDS = (
    "return_id",
    "transaction_id",
    "product",
    "guidance",
    "product_check",
    "packaging",
    "packaging_damage",
    "product_damage",
    "contents",
    "days_since_sale",
    "reasons",
)


@dataclass(frozen=True)
class Sale:
    """The sale line the POS pulled up from the receipt barcode."""

    transaction_id: str
    date: date
    sku: str
    upc: str
    product: str
    brand: str
    model: str
    price: str
    category: str


@dataclass(frozen=True)
class Return:
    return_id: str
    returned_on: date
    photos: tuple[str, ...]
    sale: Sale


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def load_returns(path: Path = RETURNS_PATH, photos_dir: Path = PHOTOS_DIR) -> list[Return]:
    """Every return in `path`. A photo name that leads out of `photos_dir` or names a missing
    file raises an error before any SIE call."""
    returns: list[Return] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        sale = dict(row["sale"])
        photos = tuple(row["photos"])
        for name in photos:
            if Path(name).name != name or not (photos_dir / name).is_file():
                raise ValueError(f"{row['return_id']}: no photo {name!r} in {photos_dir}")
        if not photos:
            raise ValueError(f"{row['return_id']}: no photos listed")
        returns.append(
            Return(
                return_id=row["return_id"],
                returned_on=date.fromisoformat(row["returned_on"]),
                photos=photos,
                sale=Sale(**{**sale, "date": date.fromisoformat(sale["date"])}),
            )
        )
    return returns


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
    those returns go to review."""
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
    """The error for a per-return reason or the summary line, its message cut short."""
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


def guarded[T](calls: CallLog, model: str, work: Callable[[], T]) -> tuple[T | None, str]:
    """Run one SIE call; return its result, or None and the reason it failed.

    A call that fails for a reason that may pass (a lost connection, a timeout, a server error,
    a rate limit, a reply without the key we asked for) is logged and its return goes to
    review; the run continues. A refusal every call would repeat (see `is_fatal`) stops the run.
    An error that isn't an SIE error is a bug in this code and is raised as is.
    """
    calls.made += 1
    try:
        return work(), ""
    except SIEError as error:
        if is_fatal(error):
            raise SIEStopped(stop_message(error, model)) from error
        calls.failures.append((model, error))
        return None, f"the SIE call failed: {short(error)}"


def _jpeg(photo: Path, max_edge: int) -> bytes:
    """The photo upright (EXIF rotation applied), shrunk to `max_edge`, as JPEG bytes."""
    with Image.open(photo) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


# --- Read the packaging (OCR) -----------------------------------------------------------------


def read_photo(
    client: SIEClient, calls: CallLog, key: str, photo: Path, rules: dict[str, Any]
) -> tuple[str, str]:
    """The packaging text GLM-OCR reads in one photo ("" when it read nothing), and why the
    call failed ("" when it didn't)."""
    model = rules["models"]["ocr"]
    item = Item(id=key, images=[_jpeg(photo, rules["photos"]["ocr_max_edge_px"])])

    def call() -> list[dict[str, Any]]:
        reply: Any = client.extract(model, item, provision_timeout_s=WAIT_S)
        if not isinstance(reply, dict) or reply.get("entities") is None:
            # A reply without `entities` is a failed call, not an empty result: an empty list is
            # a real "nothing read". It takes the transient path in `guarded`.
            error = reply.get("error") if isinstance(reply, dict) else None
            raise SIEError(
                "the reply has no `entities`" + (f" (item error: {error})" if error else "")
            )
        return cast(list[dict[str, Any]], reply["entities"])

    entities, failed = guarded(calls, model, call)
    for entity in entities or []:
        if entity.get("label") == "markdown":
            return clean_text(str(entity.get("text", ""))), failed
    return "", failed


def clean_text(text: str) -> str:
    """Drop the Markdown code fences GLM-OCR sometimes wraps around its text (an empty photo
    comes back as a fenced block with nothing in it)."""
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("```"))


# --- Same product? (plain Python) -------------------------------------------------------------

_DIGIT_RUN = re.compile(r"(?<![0-9A-Za-z])\d[\d -]*\d(?![0-9A-Za-z])")
_TOKEN = re.compile(r"[A-Z0-9]+(?:-[A-Z0-9]+)*")
_WORD = re.compile(r"[a-z]+")


def gtin_valid(digits: str) -> bool:
    """A UPC-A, EAN-13 or GTIN-14 number whose last digit is its check digit."""
    if len(digits) not in (12, 13, 14) or not digits.isdigit():
        return False
    body = [int(digit) for digit in digits[:-1]]
    total = sum(digit * (3 if index % 2 == 0 else 1) for index, digit in enumerate(body[::-1]))
    return (10 - total % 10) % 10 == int(digits[-1])


def gtin_key(digits: str) -> str:
    """UPC-A 087547003503 and EAN-13 0087547003503 are one number: compare them as GTIN-14."""
    return digits.zfill(14)


def barcodes(text: str) -> list[str]:
    """Barcode numbers printed in the text, line by line. The digits under a barcode are often
    printed in groups ("0 87547 00350 3"), so spaces and hyphens inside a run are dropped. Only
    numbers with a valid check digit count, which keeps dates and phone numbers out."""
    found: list[str] = []
    for line in text.splitlines():
        for match in _DIGIT_RUN.finditer(line):
            digits = re.sub(r"\D", "", match.group(0))
            if gtin_valid(digits) and digits not in found:
                found.append(digits)
    return found


def compact(value: str) -> str:
    """A model number without case, spaces or punctuation: "GT-I9100" is "GTI9100"."""
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def model_tokens(text: str) -> list[str]:
    """Every word-like token in the text, upper case, hyphens dropped ("2-SLICE" is "2SLICE")."""
    return [token.replace("-", "") for token in _TOKEN.findall(text.upper())]


def looks_like_model(token: str) -> bool:
    return len(token) >= 4 and any(c.isalpha() for c in token) and any(c.isdigit() for c in token)


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def name_words(sale: Sale) -> list[str]:
    """The product name's words that say what it is: letters only, three or more, without the
    brand ("Black+Decker 2-Slice Toaster" gives slice, toaster)."""
    brand = set(words(sale.brand))
    found: list[str] = []
    for word in words(sale.product):
        if len(word) >= 3 and word not in brand and word not in found:
            found.append(word)
    return found


def check_product(sale: Sale, text: str, rules: dict[str, Any]) -> dict[str, Any]:
    """Compare the packaging text with the sale line. Return the status (match, mismatch or
    cant_confirm), the reason and what was read.

    Barcode first, then model number, then brand and name words: the most specific evidence
    decides, and a later step never overrules an earlier one.
    """
    settings = rules["product_check"]
    codes = barcodes(text)
    tokens = model_tokens(text)
    text_words = set(words(text))
    brand_words = words(sale.brand)
    brand_read = bool(brand_words) and all(word in text_words for word in brand_words)
    wanted = name_words(sale)
    name_read = [word for word in wanted if word in text_words]
    share = len(name_read) / len(wanted) if wanted else 0.0
    found: dict[str, Any] = {
        "barcodes_read": codes,
        "brand_read": brand_read,
        "name_words_read": name_read,
    }

    def result(status: str, reason: str) -> dict[str, Any]:
        return {"status": status, "reason": reason, **found}

    if sale.upc:
        if any(gtin_key(code) == gtin_key(sale.upc) for code in codes):
            return result("match", f"UPC {sale.upc} on the packaging matches the sale.")
        if codes:
            return result(
                "mismatch",
                f"The packaging shows UPC {', '.join(codes)}; the sale was UPC {sale.upc}.",
            )
    model = compact(sale.model)
    if model:
        if model in tokens:
            return result("match", f"Model {sale.model} on the packaging matches the sale.")
        near = [
            token
            for token in dict.fromkeys(tokens)
            if looks_like_model(token)
            and difflib.SequenceMatcher(None, token, model).ratio()
            >= settings["near_model_similarity"]
        ]
        if near:
            return result(
                "mismatch",
                f"The packaging shows model {', '.join(near)}, close to but not the sale's "
                f"{sale.model}.",
            )
    listed = ", ".join(name_read)
    if share >= settings["min_name_share"] and brand_read:
        return result(
            "match",
            f"Brand {sale.brand} and name words ({listed}) on the packaging match the sale; "
            "no UPC or model number of the sale was read.",
        )
    if share >= settings["min_name_share"]:
        return result(
            "mismatch",
            f"The packaging names the product ({listed}) but not the sale's brand "
            f"({sale.brand}), model or UPC.",
        )
    if brand_read:
        return result(
            "cant_confirm",
            f"Brand {sale.brand} read, but not the sale's UPC, model or product name.",
        )
    return result(
        "cant_confirm",
        "Nothing on the packaging identifies the product: none of the sale's UPC, model, brand "
        "or product name was read.",
    )


# --- Condition (vision model) -----------------------------------------------------------------


def ask_condition(
    client: SIEClient, calls: CallLog, ret: Return, rules: dict[str, Any], photos_dir: Path
) -> tuple[dict[str, Any] | None, str]:
    """Ask the vision model the fixed condition questions about all of a return's photos.

    Return the chat reply's choice (finish reason and content), or None and why the call
    failed. A reply with no message in it is a failed call on the transient path.
    """
    model = rules["models"]["condition"]
    max_edge = rules["photos"]["condition_max_edge_px"]
    images = [
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/jpeg;base64,"
                + base64.b64encode(_jpeg(photos_dir / name, max_edge)).decode("ascii")
            },
        }
        for name in ret.photos
    ]

    def call() -> dict[str, Any]:
        response: Any = client.chat_completions(
            model,
            [{"role": "user", "content": [*images, {"type": "text", "text": PROMPT}]}],
            max_completion_tokens=rules["condition"]["max_completion_tokens"],
            temperature=0.0,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "condition", "schema": CONDITION_SCHEMA, "strict": True},
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


def parse_condition(choice: dict[str, Any]) -> tuple[dict[str, str] | None, str]:
    """The model's answers, checked strictly against the question set, or None and what was
    wrong with the reply. Nothing is repaired or guessed: a reply that breaks the schema is a
    review reason, not an answer."""
    if choice.get("finish_reason") not in (None, "stop"):
        return None, f"the reply was cut off ({choice.get('finish_reason')})"
    try:
        answers = json.loads(choice.get("content") or "")
    except json.JSONDecodeError:
        return None, "the reply is not JSON"
    if not isinstance(answers, dict):
        return None, "the reply is not a JSON object"
    expected = {*CONDITION_ANSWERS, "evidence"}
    if set(answers) != expected:
        missing = sorted(expected - set(answers))
        extra = sorted(set(answers) - expected)
        return None, f"the reply's keys are wrong (missing {missing}, extra {extra})"
    for key, allowed in CONDITION_ANSWERS.items():
        if answers[key] not in allowed:
            return None, f"{key} is {answers[key]!r}, not one of {', '.join(allowed)}"
    if not isinstance(answers["evidence"], str) or not answers["evidence"].strip():
        return None, "the reply gives no evidence"
    return {key: str(value).strip() for key, value in answers.items()}, ""


# --- Guidance (plain Python) ------------------------------------------------------------------


def reason(level: str, check: str, text: str) -> dict[str, str]:
    return {"level": level, "check": check, "reason": text}


def condition_reasons(
    answers: dict[str, str], category: dict[str, Any] | None, rules: dict[str, Any]
) -> list[dict[str, str]]:
    """One reason per answer that rules.yaml gives a meaning to; opened packaging follows the
    category's policy."""
    reasons: list[dict[str, str]] = []
    findings = rules["condition"]["findings"]
    for key in CONDITION_ANSWERS:
        listed = findings.get(key, {}).get(answers[key])
        if listed:
            reasons.append(reason(listed[0], "condition", listed[1]))
    if answers["packaging"] == "opened" and category is not None:
        level = category["opened"]
        text = (
            "Opened; this category can be returned opened."
            if level == "accept"
            else "Opened; check it before taking it back."
        )
        reasons.append(reason(level, "condition", text))
    if not reasons:
        seen = ", ".join(f"{key.replace('_', ' ')} {answers[key]}" for key in CONDITION_ANSWERS)
        reasons.append(reason("accept", "condition", f"No condition finding ({seen})."))
    return reasons


def policy_reasons(ret: Return, rules: dict[str, Any]) -> list[dict[str, str]]:
    """The return window for the sale's category."""
    policy = rules["policy"]
    category = policy["categories"].get(ret.sale.category)
    if category is None:
        return [
            reason(
                policy["unknown_category"],
                "policy",
                f"No policy for category {ret.sale.category!r} in rules.yaml.",
            )
        ]
    days = (ret.returned_on - ret.sale.date).days
    window = category["window_days"]
    if days < 0:
        return [reason("inspect", "policy", "The sale is dated after the return; check it.")]
    if days > window:
        return [
            reason(
                policy["past_window"],
                "policy",
                f"{days} days since the sale, past the {window}-day window for "
                f"{ret.sale.category}.",
            )
        ]
    return [reason("accept", "policy", f"{days} days since the sale, within {window} days.")]


def most_severe(reasons: list[dict[str, str]], rules: dict[str, Any]) -> str:
    order = rules["guidance"]["severity"]
    return max((r["level"] for r in reasons), key=order.index, default="accept")


def guide(
    ret: Return,
    texts: dict[str, str],
    ocr_failed: str,
    choice: dict[str, Any] | None,
    condition_failed: str,
    rules: dict[str, Any],
) -> dict[str, Any]:
    """Turn the OCR text, the condition reply and the policy into guidance with reasons."""
    category = rules["policy"]["categories"].get(ret.sale.category)
    levels = rules["guidance"]
    if ocr_failed:
        product = {"status": "not_checked", "reason": f"Packaging not read: {ocr_failed}"}
    else:
        product = check_product(ret.sale, "\n".join(texts.values()), rules)
    reasons = [reason(levels["product"][product["status"]], "product", product["reason"])]
    answers: dict[str, str] | None = None
    if choice is None:
        problem = f"Condition not checked: {condition_failed}"
    else:
        answers, problem = parse_condition(choice)
        problem = f"Condition answers not usable: {problem}" if problem else ""
    if answers is None:
        reasons.append(reason(levels["condition_not_checked"], "condition", problem))
    else:
        reasons += condition_reasons(answers, category, rules)
    reasons += policy_reasons(ret, rules)
    return {
        "return_id": ret.return_id,
        "transaction_id": ret.sale.transaction_id,
        "product": ret.sale.product,
        "guidance": most_severe(reasons, rules),
        "reasons": reasons,
        "product_check": product,
        "condition": answers,
        "days_since_sale": (ret.returned_on - ret.sale.date).days,
        "photos": [{"photo": name, "packaging_text": texts.get(name, "")} for name in ret.photos],
    }


# --- Run --------------------------------------------------------------------------------------


def check_return(
    client: SIEClient, calls: CallLog, ret: Return, rules: dict[str, Any], photos_dir: Path
) -> dict[str, Any]:
    """Read every photo, ask the condition questions once, and work out the guidance."""
    texts: dict[str, str] = {}
    failed: list[str] = []
    for name in ret.photos:
        text, why = read_photo(client, calls, f"{ret.return_id}/{name}", photos_dir / name, rules)
        texts[name] = text
        if why:
            failed.append(f"{name}: {why}")
    choice, condition_failed = ask_condition(client, calls, ret, rules, photos_dir)
    return guide(ret, texts, "; ".join(failed), choice, condition_failed, rules)


def run(
    client: SIEClient,
    rules: dict[str, Any],
    output_dir: Path | None,
    returns_path: Path = RETURNS_PATH,
    photos_dir: Path = PHOTOS_DIR,
) -> list[dict[str, Any]]:
    """Check every return in `returns_path`.

    Nothing is written until every return is done, so a run that stops on an SIE refusal or on
    every call failing leaves no partial reports behind.
    """
    returns = load_returns(returns_path, photos_dir)
    if not returns:
        raise SystemExit(f"No returns in {returns_path}, so there is nothing to check.")
    calls = CallLog()
    results = [check_return(client, calls, ret, rules, photos_dir) for ret in returns]
    calls.stop_if_all_failed()
    if calls.failures:
        print(
            f"{len(calls.failures)} of {calls.made} SIE calls failed; their returns go to "
            f"review. First: {short(calls.failures[0][1])}"
        )
    if output_dir is not None:
        write_outputs(output_dir, results)
    return results


def csv_row(result: dict[str, Any]) -> dict[str, Any]:
    answers = result["condition"] or {}
    return {
        "return_id": result["return_id"],
        "transaction_id": result["transaction_id"],
        "product": result["product"],
        "guidance": result["guidance"],
        "product_check": result["product_check"]["status"],
        **{key: answers.get(key, "") for key in CONDITION_ANSWERS},
        "days_since_sale": result["days_since_sale"],
        "reasons": " | ".join(f"{r['level']}: {r['reason']}" for r in result["reasons"]),
    }


def markdown(results: list[dict[str, Any]]) -> str:
    """One card per return for the associate: the guidance, why, and what the models saw."""
    lines = [
        "# Return item check",
        "",
        "Guidance for the associate, not a decision. A person decides every return, and a "
        "manager decides every `refer`.",
    ]
    for result in results:
        answers = result["condition"]
        lines += [
            "",
            f"## {result['return_id']}: {result['guidance']}",
            "",
            f"- Sale: {result['transaction_id']}, {result['product']}, "
            f"{result['days_since_sale']} days ago",
            *(f"- {r['level']} ({r['check']}): {r['reason']}" for r in result["reasons"]),
        ]
        if answers:
            seen = ", ".join(f"{key} {answers[key]}" for key in CONDITION_ANSWERS)
            lines.append(f"- Condition answers: {seen}. Evidence: {answers['evidence']}")
        for photo in result["photos"]:
            text = " / ".join(line for line in photo["packaging_text"].splitlines() if line)
            lines.append(f"- Text read on {photo['photo']}: {text[:300] or '(none)'}")
    return "\n".join(lines) + "\n"


def write_outputs(output_dir: Path, results: list[dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "guidance.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    with (output_dir / "guidance.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(csv_row(result) for result in results)
    (output_dir / "guidance.md").write_text(markdown(results), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rules = load_rules()
    with make_client() as client:
        results = stop_on_refusal(lambda: run(client, rules, output))
    for result in results:
        print(f"\n{result['return_id']}  {result['guidance']:<7}  {result['product']}")
        for item in result["reasons"]:
            print(f"    {item['level']:<7} {item['check']:<9} {item['reason']}")
    counts = {level: sum(r["guidance"] == level for r in results) for level in GUIDANCE}
    print(f"\n{len(results)} returns: " + ", ".join(f"{n} {level}" for level, n in counts.items()))
    print("Guidance only: the associate decides every return, and a manager every refer.")
    print(f"outputs: {output / 'guidance.json'}, {output / 'guidance.csv'}, guidance.md")


if __name__ == "__main__":
    main()
