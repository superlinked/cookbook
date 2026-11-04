"""Match each item on a contents inventory to a receipt line and list the proof still needed.

Use case:  Substantiate a home contents claim: for each item the policyholder lists, find the
           receipt line that proves it, and write the requests for whatever proof is missing.
Who:       Contents claims handlers at a home insurer.
Problem:   The policyholder uploads phone photos of their receipts and an inventory of what was
           lost, often handwritten. The handler reads every receipt, finds the line for each
           item, checks the price, quantity and date, adds up each receipt, and writes to the
           policyholder for the proof that's missing.
Input:     The claims system (claims_system.py over the CSV seeds in data/): each claim's
           policy, policyholder, date of loss, contents amount claimed, which uploaded page is a
           receipt and which the inventory, and the inventory rows when it came through the
           portal. The photos are in data/receipt-photos.pdf and data/inventories.pdf. Matching
           rules and thresholds are in rules.yaml; the substantiation guideline (proof
           threshold, other proof accepted, request wording) is in proof_guidelines.yaml.
SIE pipeline:
           extract  lightonai/LightOnOCR-3-4B  read each photo, printed or handwritten, as
                                               Markdown; called once per photo
           extract  fastino/gliner2-large-v1   receipt: merchant, date, items and total;
                                               inventory: items and the total claimed; called
                                               once per window of about 512 tokens
           (Python)                            prices, quantities, tax and discount lines,
                                               receipt arithmetic, matching each inventory
                                               item to a receipt line, the written total
                                               against the claims system and its items, the
                                               request list
Output:    reconciliation.csv (one row per inventory item with the receipt line it matched and
           its status, plus the receipt lines nobody claimed), claims.csv (one row per claim with
           its check status and reasons), requests/<claim>.md for each claim in review, and
           claims/<claim>.json with everything read. Every status names what the checks found;
           the handler decides what proof is enough and what each item is worth.
Why SIE:   Two models, and no generative model makes a decision. Every amount, sum, date, match
           and request is plain Python reading rules.yaml and proof_guidelines.yaml.
Run using: SIE Serverless, SIE Cluster or SIE Local (with SIE_BASE_URL).

    uv run recipe.py
"""

from __future__ import annotations

import argparse
import csv
import functools
import html
import io
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NoReturn, cast

import pypdfium2 as pdfium
import yaml
from claims_system import Attachment, Claim, ClaimsSystem
from dotenv import load_dotenv
from sie_sdk import SIEClient
from sie_sdk.client.errors import (
    InputTooLongError,
    ModelLoadFailedError,
    RateLimitError,
    RequestError,
    ServerError,
    SIEConnectionError,
    SIEError,
)
from sie_sdk.types import Item

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
RULES_PATH = ROOT / "rules.yaml"
GUIDELINE_PATH = ROOT / "proof_guidelines.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
# A failed SIE call's message is cut to this many characters in reasons and the summary line.
ERROR_CHARS = 200
# A cell starting with one of these runs as a formula when a spreadsheet opens the CSV.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")

# Match status per inventory item: what the receipts show. The handler decides what it proves.
MATCHED, PRICE_DIFFERS, AMBIGUOUS = "matched", "price_differs", "ambiguous"
AFTER_LOSS, NO_RECEIPT, NOT_READ = "after_loss", "no_receipt", "not_read"
NOT_CLAIMED = "not_claimed"  # a receipt line no inventory item matched; for information
# Check status per claim: whether the paperwork lines up. Nothing is paid or denied here.
CHECKS_PASSED, REVIEW = "checks_passed", "review"
# When GLiNER2 finds nothing, SIE Serverless answers the output_schema call with HTTP 500 and this
# message. We record it as "no fields found"; a missing field can only send a claim to review,
# never pass it.
EMPTY_RESULT_ERROR = "Inference completed without a valid conserved billing settlement"
MONEY = re.compile(r"(?<![\d.])-?\$?\s?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}(?![\d])")
# "2 @ 14.99" on a receipt line: two at 14.99 each.
EACH = re.compile(r"(?<![\d.])(\d{1,3})\s*@\s*\$?(\d+\.\d{2})(?![\d])")
# "(2)" after an item on a handwritten list: two of them, and the price is for both.
COUNT = re.compile(r"\(\s*(?:qty\s*)?(\d{1,3})\s*\)", re.I)
ORDINALS = ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth")
CLAIM_FIELDS = (
    *("claim", "policyholder", "date_of_loss", "contents_claimed", "inventory_source"),
    *("status", "items", "matched", "requests", "reasons"),
)
TABLE_FIELDS = (
    *("claim", "inventory_line", "inventory_item", "inventory_qty", "inventory_amount"),
    *("receipt", "receipt_page", "receipt_line", "receipt_item", "receipt_qty"),
    *("receipt_unit_price", "purchase_date", "status", "proof_requested", "detail"),
)


@dataclass(frozen=True)
class Page:
    file: str
    page: int


@dataclass
class ReceiptLine:
    receipt: int  # the receipt's number on the claim, 1-based
    line: int  # the item's line on that receipt, 1-based
    text: str  # the OCR line it was read from
    description: str  # the line without its prices and quantity
    quantity: int
    unit_price: Decimal
    amount: Decimal  # the line's price: quantity times unit price

    def as_json(self) -> dict[str, Any]:
        return {
            "receipt": self.receipt,
            "line": self.line,
            "text": self.text,
            "description": self.description,
            "quantity": self.quantity,
            "unit_price": str(self.unit_price),
            "amount": str(self.amount),
        }


@dataclass
class Receipt:
    number: int
    attachment: str
    page: Page
    text: str = ""
    unread: str = ""  # why the photo couldn't be read; "" when it was
    sie_failed: bool = False  # unread because of SIE, not the photo: nothing to ask for
    merchant: str | None = None
    purchase_date: str | None = None  # as printed
    bought: date | None = None
    total: Decimal | None = None
    lines: list[ReceiptLine] = field(default_factory=list)
    unpriced: list[str] = field(default_factory=list)
    discounts: list[Decimal] = field(default_factory=list)
    tax: list[Decimal] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # reasons for review, receipt by receipt

    @property
    def name(self) -> str:
        return f"receipt {self.number} ({self.page.file} p{self.page.page})"

    def as_json(self) -> dict[str, Any]:
        return {
            "receipt": self.number,
            "attachment": self.attachment,
            "page": vars(self.page),
            "unread": self.unread or None,
            "merchant": self.merchant,
            "purchase_date": self.purchase_date,
            "total": _str(self.total),
            "lines": [line.as_json() for line in self.lines],
            "unpriced_items": self.unpriced,
            "discounts": [str(d) for d in self.discounts],
            "tax": [str(t) for t in self.tax],
            "problems": self.problems,
        }


@dataclass
class InventoryItem:
    line: int
    description: str
    quantity: int
    amount: Decimal  # what the inventory claims for the line: quantity times the unit price
    text: str  # the OCR line, or the portal row

    def label(self) -> str:
        count = f"{self.quantity} x " if self.quantity > 1 else ""
        return f"{count}{self.description} ({self.amount:.2f})"


@dataclass
class Row:
    """One row of the reconciliation table: an inventory item, or a receipt line nobody
    claimed, with what the receipts show."""

    status: str
    detail: str
    item: InventoryItem | None = None
    receipt_line: ReceiptLine | None = None
    proof_requested: str = ""  # for no_receipt: "yes", or why not


@dataclass
class ClaimResult:
    claim: Claim
    receipts: list[Receipt] = field(default_factory=list)
    inventory_page: Page | None = None
    inventory_read: bool = False
    inventory_unread: str = ""  # why the inventory photo couldn't be read; "" when it was
    inventory_sie_failed: bool = False  # unread because of SIE: nothing to ask for
    inventory_total: Decimal | None = None
    items: list[InventoryItem] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return REVIEW if self.reasons else CHECKS_PASSED

    def receipt(self, number: int) -> Receipt:
        return self.receipts[number - 1]

    def as_json(self) -> dict[str, Any]:
        claim = self.claim
        return {
            "claim": claim.claim_number,
            "policy": claim.policy_number,
            "policyholder": claim.policyholder,
            "date_of_loss": claim.date_of_loss.isoformat(),
            "contents_claimed": str(claim.contents_claimed),
            "inventory_source": claim.inventory_source,
            "status": self.status,
            "reasons": self.reasons,
            "requests": self.requests,
            "inventory": {
                "page": vars(self.inventory_page) if self.inventory_page else None,
                "total_written": _str(self.inventory_total),
                "items": [
                    {
                        "line": i.line,
                        "description": i.description,
                        "quantity": i.quantity,
                        "amount": str(i.amount),
                        "text": i.text,
                    }
                    for i in self.items
                ],
            },
            "receipts": [receipt.as_json() for receipt in self.receipts],
            "reconciliation": [table_row(self, row) for row in self.rows],
            "notes": self.notes,
        }


def _str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def load_yaml(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return load_yaml(path)


def load_guideline(path: Path = GUIDELINE_PATH) -> dict[str, Any]:
    return load_yaml(path)


def output_schema(rules: dict[str, Any], kind: str) -> dict[str, Any]:
    """Build the GLiNER2 output_schema for one document kind from rules.yaml."""
    properties: dict[str, Any] = {}
    for name, spec in rules["documents"][kind].items():
        if spec.get("list"):
            prop: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
        else:
            prop = {"type": "string"}
        properties[name] = prop | {"description": spec["description"]}
    return {"type": "object", "properties": properties}


def make_client() -> SIEClient:
    """Build an SIE client from the API key and SIE_BASE_URL in the environment or `.env`."""
    load_dotenv()
    api_key = os.environ.get("SIE_API_KEY", "").strip()
    base_url = os.environ.get("SIE_BASE_URL", "").strip()
    if not api_key and not base_url:
        raise RuntimeError("Set SIE_API_KEY, or SIE_BASE_URL for SIE Cluster or SIE Local, in .env")
    base_url = base_url or "https://api.superlinked.com"
    return SIEClient(api_key=api_key or None, base_url=base_url, timeout_s=WAIT_S)


def jsonable(value: Any) -> Any:
    """Turn SDK results (numpy arrays, nested dicts and lists) into plain JSON values."""
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, dict):
        return {str(key): jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(child) for child in value]
    return value


# --- SIE call failures ------------------------------------------------------------------------


class SIEStopped(RuntimeError):
    """SIE refused the call for a reason a retry won't fix, or no call succeeded; the run
    stops without outputs."""


class CallFailed(Exception):
    """One SIE call failed for a reason that may pass; the claim it was for goes to review."""


@dataclass
class Calls:
    """Every SIE call the run made, and the transient failures among them."""

    count: dict[str, int] = field(default_factory=dict)  # model -> calls
    failures: list[tuple[str, SIEError]] = field(default_factory=list)  # (model, error)

    @property
    def total(self) -> int:
        return sum(self.count.values())

    def call(
        self, model: str, work: Callable[[], Any], key: str, empty_result_ok: bool = False
    ) -> dict[str, Any]:
        """Run one SIE call and return its reply, which must carry `key`.

        A refusal (see `is_fatal`) stops the run with SIEStopped. Any other SIE error, and a
        reply without `key`, is recorded and raised as CallFailed, so the caller marks what it
        was reading as not read and the run goes on. With `empty_result_ok` (the GLiNER2
        calls), SIE Serverless's empty-result HTTP 500 is a reply with nothing in it, not a failure.
        An error that isn't an SIE error is a bug in this code and is raised as is.
        """
        self.count[model] = self.count.get(model, 0) + 1
        try:
            reply = jsonable(work())
        except SIEError as error:
            if (
                empty_result_ok
                and isinstance(error, ServerError)
                and EMPTY_RESULT_ERROR in str(error)
            ):
                return {key: {} if key == "data" else [], "empty_result_error": str(error)}
            if is_fatal(error):
                raise SIEStopped(stop_message(error, model)) from error
            self.failures.append((model, error))
            raise CallFailed(f"the SIE call failed: {short(error)}") from error
        if not isinstance(reply, dict) or key not in reply:
            self.fail(model, f"{model} replied without '{key}'")
        return cast(dict[str, Any], reply)

    def fail(self, model: str, message: str) -> NoReturn:
        """Record a reply that holds nothing usable as a failed call and raise CallFailed."""
        missing = SIEError(message)
        self.failures.append((model, missing))
        raise CallFailed(f"the SIE call failed: {short(missing)}")

    def check_all_failed(self) -> None:
        """Stop the run when every call failed; print a line when only some did."""
        if not self.failures:
            return
        total, (_, first) = self.total, self.failures[0]
        if len(self.failures) == total:
            models = ", ".join(sorted({name for name, _ in self.failures}))
            raise SIEStopped(
                f"Every SIE call failed ({total} of {total} to {models}), so the run stops: "
                f"{describe(first)}: {first}. {every_call_failed_advice(first)} "
                "No outputs were written."
            ) from first
        n = len(self.failures)
        print(f"{n} of {total} SIE calls failed; their claims go to review. First: {short(first)}")


def every_call_failed_advice(error: SIEError) -> str:
    """What to check when every SIE call failed, judged by the first failure.

    A lost connection points at SIE_BASE_URL and the network. Any other transient error (a
    timeout, a model still loading, a 5xx, a rate limit) more likely means the model is still
    starting or SIE is busy, so the advice is to wait and run again.
    """
    if isinstance(error, SIEConnectionError):
        return "Check SIE_BASE_URL and your network."
    return "The model may still be starting or SIE is busy; run again in a few minutes."


def is_fatal(error: SIEError) -> bool:
    """A refusal that fails every call the same way: a 4xx (bad key 401, forbidden 403, model
    not found 404, no credits or spend limit 402, inactive account) or a model the server
    marked as permanently unable to load. A RequestError with no HTTP status or a status below
    400 is a reply the SDK couldn't parse, and is fatal too. A rate limit, an over-long input, a
    timeout, a lost connection, a model still loading and a 5xx are transient or per-item:
    those claims go to review."""
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
    """The error for a per-claim reason or the summary line, its message cut short."""
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


def stop_on_refusal[T](work: Callable[[], T]) -> T:
    """Run the work; turn SIEStopped into a clean exit. Any other error keeps its traceback."""
    try:
        return work()
    except SIEStopped as error:
        raise SystemExit(f"Stopped: {error}") from None


# --- Read photos (LightOnOCR) -----------------------------------------------------------------


def _page_png(page: pdfium.PdfPage, dpi: int, max_edge: int) -> bytes:
    image = page.render(scale=dpi / 72).to_pil().convert("RGB")
    image.thumbnail((max_edge, max_edge))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _markdown_entity(response: dict[str, Any]) -> str | None:
    """The page's Markdown; None when the reply's entities hold no markdown entity."""
    for entity in response.get("entities") or []:
        if entity.get("label") == "markdown":
            return str(entity.get("text", ""))
    return None


def read_page(client: SIEClient, calls: Calls, pdf: Path, page: int, rules: dict[str, Any]) -> str:
    """OCR one photo page of a PDF (1-based page number): its Markdown. Raises CallFailed on a
    transient SIE error or a reply with no markdown entity, which counts as a failed call."""
    limits, model = rules["thresholds"], rules["models"]["ocr"]
    document = pdfium.PdfDocument(pdf)
    image = _page_png(document[page - 1], limits["render_dpi"], limits["max_image_edge_px"])
    response = calls.call(
        model,
        lambda: client.extract(
            model, Item(id=f"ocr/{pdf.stem}/p{page}", images=[image]), provision_timeout_s=WAIT_S
        ),
        "entities",
    )
    text = _markdown_entity(response)
    if text is None:
        calls.fail(model, f"{model} replied without a markdown entity")
    return text


def flatten_markdown(text: str) -> str:
    """OCR Markdown to plain lines, one per table row, with cells joined by " | ".

    An item and its price then share a line, which is what the price checks read. Headings,
    bold marks and bullets are dropped and HTML entities decoded.
    """

    def table(match: re.Match[str]) -> str:
        lines = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", match.group(0), re.S):
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
            parts = [re.sub(r"<[^>]+>", " ", re.sub(r"<br\s*/?>", " | ", cell)) for cell in cells]
            lines.append(" | ".join(re.sub(r"\s+", " ", part).strip() for part in parts))
        return "\n" + "\n".join(lines) + "\n"

    text = re.sub(r"<table[^>]*>.*?</table>", table, text, flags=re.S)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"\*\*|^\s*(?:#+|[-*])\s+", "", text, flags=re.M)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"[|:\-\s]*", stripped):
            continue  # blank, a rule ("---") or a pipe table's separator row
        if stripped.startswith("|"):
            stripped = " | ".join(cell.strip() for cell in stripped.strip("|").split("|"))
        lines.append(stripped)
    return html.unescape("\n".join(lines))


# --- Money, quantities and lines (plain Python) -----------------------------------------------


def amounts(line: str) -> list[Decimal]:
    """Every money amount on a line, in order: "-30.00", "$1,249.97", "29.98"."""
    found: list[Decimal] = []
    for match in MONEY.finditer(line):
        try:
            found.append(Decimal(re.sub(r"[$,\s]", "", match.group())))
        except InvalidOperation:
            continue
    return found


def has_label(line: str, labels: list[str]) -> bool:
    """Whether a label is on the line as a whole word, plural allowed: "cash" is on "CASH
    TENDERED" but not on "CASHMERE SWEATER"."""
    lowered = line.casefold()
    return any(
        re.search(r"(?<![a-z])" + re.escape(label) + r"s?(?![a-z])", lowered) for label in labels
    )


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def describe_line(line: str) -> str:
    """An item line without its prices, quantity notation and table pipes: "MIXING BOWL SET
    2 @ 14.99 | 29.98" is "MIXING BOWL SET"."""
    text = EACH.sub(" ", line)
    text = COUNT.sub(" ", text)
    text = MONEY.sub(" ", text)
    text = re.sub(r"[|]", " ", text)
    return re.sub(r"\s+", " ", text).strip(" -:*.")


def line_price(line: str) -> tuple[int, Decimal, Decimal] | str:
    """(quantity, unit price, line price) from an item line, or why it can't be priced.

    "2 @ 14.99 | 29.98" is two at 14.99; the line price must be two times 14.99, and a line
    printing only "2 @ 14.99" costs 29.98. "(2) 29.98" on a handwritten list is two for 29.98.
    Anything else is one at the last amount on the line.
    """
    found = amounts(EACH.sub(" ", line))
    each = EACH.search(line)
    if each:
        quantity, unit = int(each.group(1)), Decimal(each.group(2))
        if quantity < 1:
            return f"quantity {quantity} on '{line}'"
        price = found[-1] if found else unit * quantity
        if price != unit * quantity:
            return f"'{line}' says {quantity} @ {unit} but prices the line at {price}"
        return quantity, unit, price
    if not found:
        return f"no price on '{line}'"
    price = found[-1]
    count = COUNT.search(line)
    quantity = int(count.group(1)) if count else 1
    if quantity < 1:
        return f"quantity {quantity} on '{line}'"
    return quantity, price / quantity, price


def priced_line(span: str, lines: list[str], rules: dict[str, Any]) -> tuple[int, Decimal] | None:
    """Find the product line holding `span` and the last amount on it.

    If the OCR put the price on a line of its own, the amount comes from the next line. Returns
    None if the span is on no product line or has no price.
    """
    labels = rules["labels"]["not_item"]
    needle = _squash(span)
    for index, line in enumerate(lines):
        if needle not in _squash(line) or has_label(line, labels):
            continue
        on_line = amounts(line)
        if on_line:
            return index, on_line[-1]
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if following and re.fullmatch(r"[\s$\d.,|-]+", following) and amounts(following):
            return index, amounts(following)[-1]
        return None
    return None


def item_line_indexes(
    spans: list[str], lines: list[str], rules: dict[str, Any]
) -> tuple[list[int], list[str]]:
    """The OCR lines the model's item spans sit on, in page order, and the spans with no price.

    Each line gives at most one item. GLiNER2 once returned "Price\\nAREA RUG 5X8 SLATE" live, a
    span running from a table header into the row below, so a span that crosses a line break
    is read line by line, and every part on a priced product line counts: two items in one span
    become two lines, never one. A span on a summary line ("INSTANT COUPON", "TOTAL") is not an
    item at all.
    """
    found: set[int] = set()
    unpriced: list[str] = []
    summary = [line for line in lines if has_label(line, rules["labels"]["not_item"])]
    for span in spans:
        parts = [part.strip() for part in span.splitlines() if part.strip()]
        hits = [priced_line(part, lines, rules) for part in parts]
        indexes = [hit[0] for hit in hits if hit is not None]
        if indexes:
            found.update(indexes)
        elif not any(_squash(span) in _squash(line) for line in summary):
            unpriced.append(span)
    return sorted(found), unpriced


def parse_date(value: str, formats: list[str]) -> date | None:
    candidates = re.findall(r"\d{1,4}[/.-]\d{1,2}[/.-]\d{2,4}", value) or [value]
    for candidate in candidates:
        cleaned = re.sub(r"\s+", " ", candidate).strip(" .,")
        for fmt in formats:
            try:
                return datetime.strptime(cleaned, fmt).date()
            except ValueError:
                continue
    return None


def labelled_amount(value: str, lines: list[str], labels: list[str]) -> Decimal | None:
    """Return the amount in `value` only if it sits on a line carrying one of `labels`."""
    wanted = amounts(value)
    if not wanted:
        return None
    for line in lines:
        if has_label(line, labels) and wanted[-1] in amounts(line):
            return wanted[-1]
    return None


# --- Extract fields (GLiNER2) -----------------------------------------------------------------


def windows(text: str, size: int, overlap: int) -> list[str]:
    """Split text into windows of at most `size` words, each overlapping the last by `overlap`."""
    tokens = re.findall(r"\S+\s*", text)
    if not tokens:
        return []
    step = max(1, size - overlap)
    parts: list[str] = []
    for start in range(0, len(tokens), step):
        parts.append("".join(tokens[start : start + size]).strip())
        if start + size >= len(tokens):
            break
    return parts


def extract_spans(
    client: SIEClient,
    calls: Calls,
    key: str,
    kind: str,
    text: str,
    rules: dict[str, Any],
    notes: list[str],
) -> dict[str, list[str]]:
    """Run GLiNER2 over each window and collect its spans per field, in the order found.

    A span counts only if it appears in the window's text; anything else is dropped with a
    note. Raises CallFailed when a window's call fails, so the whole page counts as not read.
    """
    model, limits = rules["models"]["extract"], rules["thresholds"]
    schema = output_schema(rules, kind)
    found: dict[str, list[str]] = {name: [] for name in rules["documents"][kind]}
    for index, window in enumerate(
        windows(text, limits["window_words"], limits["window_overlap_words"]), start=1
    ):
        item = Item(id=f"extract/{key}-w{index}", text=window)
        # One item per call: the empty-result HTTP 500 would fail a whole batch.
        # A window too long to read whole fails instead of being cut short.
        work = functools.partial(
            client.extract,
            model,
            item,
            output_schema=schema,
            options={"overflow_policy": "error"},
            provision_timeout_s=WAIT_S,
        )
        response = calls.call(model, work, "data", empty_result_ok=True)
        if response.get("empty_result_error"):
            notes.append(f"{key} window {index}: no fields found (HTTP 500 empty result)")
        for name, raw in (response.get("data") or {}).items():
            if name not in found:
                continue
            for value in raw if isinstance(raw, list) else [raw]:
                if not isinstance(value, str) or not re.search(r"[^\W_]", value):
                    continue  # empty or a form blank ("______")
                value = value.strip()
                if _squash(value) not in _squash(window):
                    notes.append(f"{key}.{name}: '{value}' is not in the text; dropped")
                elif value not in found[name]:
                    found[name].append(value)
    return found


def one_amount(
    values: list[str], lines: list[str], labels: list[str], what: str, notes: list[str]
) -> tuple[Decimal | None, str | None]:
    """The single labelled amount the spans agree on, or None with a problem when they read two
    different amounts. A span that is not on a labelled line is dropped with a note."""
    read: list[Decimal] = []
    for value in values:
        amount = labelled_amount(value, lines, labels)
        if amount is None:
            notes.append(f"{what}: '{value}' is not on a {'/'.join(labels)} line; dropped")
        elif amount not in read:
            read.append(amount)
    if len(read) > 1:
        return None, f"{what} read as " + " / ".join(str(a) for a in read)
    return (read[0] if read else None), None


# --- Read a receipt and an inventory ----------------------------------------------------------


def ocr_text(
    client: SIEClient,
    calls: Calls,
    page: Page,
    data_dir: Path,
    rules: dict[str, Any],
) -> tuple[str, str, bool]:
    """(Markdown, why it isn't readable or "", whether SIE was the reason) for one photo."""
    try:
        text = read_page(client, calls, data_dir / page.file, page.page, rules)
    except CallFailed as failure:
        return "", f"not read: {failure}", True
    chars = len(flatten_markdown(text))
    if chars < rules["thresholds"]["min_page_chars"]:
        return text, f"only {chars} characters of text came back; the photo is unreadable", False
    return text, "", False


def read_receipt(
    client: SIEClient,
    calls: Calls,
    number: int,
    attachment: Attachment,
    data_dir: Path,
    rules: dict[str, Any],
    result: ClaimResult,
) -> Receipt:
    """OCR one receipt photo, find its fields, and read its item lines, discounts and tax."""
    receipt = Receipt(number, attachment.attachment, Page(attachment.file, attachment.page))
    receipt.text, receipt.unread, receipt.sie_failed = ocr_text(
        client, calls, receipt.page, data_dir, rules
    )
    if receipt.unread:
        return receipt
    key = f"{result.claim.claim_number}/receipt-{number}"
    lines = flatten_markdown(receipt.text).splitlines()
    try:
        spans = extract_spans(client, calls, key, "receipt", "\n".join(lines), rules, result.notes)
    except CallFailed as failure:
        receipt.unread, receipt.sie_failed = f"fields not read: {failure}", True
        return receipt
    labels = rules["labels"]
    receipt.merchant = next(iter(spans["merchant"]), None)
    what = f"receipt {number} total"
    receipt.total, conflict = one_amount(spans["total"], lines, labels["total"], what, result.notes)
    if conflict:
        receipt.problems.append(conflict)
    dates = {
        parsed: value
        for value in spans["purchase_date"]
        if (parsed := parse_date(value, rules["date_formats"])) is not None
    }
    if len(dates) > 1:
        shown = " / ".join(dates.values())
        receipt.problems.append(f"receipt {number} purchase date read as {shown}")
    elif dates:
        receipt.bought, receipt.purchase_date = next(iter(dates.items()))
    elif spans["purchase_date"]:
        receipt.problems.append(
            f"receipt {number}: '{spans['purchase_date'][0]}' is not a date I can read"
        )
    else:
        receipt.problems.append(f"receipt {number}: purchase date not read")
    indexes, receipt.unpriced = item_line_indexes(spans["items"], lines, rules)
    for index in indexes:
        priced = line_price(lines[index])
        if isinstance(priced, str):
            receipt.unpriced.append(priced)
            continue
        quantity, unit, price = priced
        receipt.lines.append(
            ReceiptLine(
                number,
                len(receipt.lines) + 1,
                lines[index],
                describe_line(lines[index]),
                quantity,
                unit,
                price,
            )
        )
    # Discounts and tax sit on labelled summary lines, so Python reads them without a model.
    for line in lines:
        if has_label(line, labels["discount"]) and amounts(line):
            receipt.discounts.append(-abs(amounts(line)[-1]))
        elif has_label(line, labels["tax"]) and amounts(line):
            receipt.tax.append(amounts(line)[-1])
    receipt.problems += receipt_math(receipt, rules)
    return receipt


def receipt_math(receipt: Receipt, rules: dict[str, Any]) -> list[str]:
    """Problems with a receipt's arithmetic: its item lines, discounts and tax must add up to
    its printed total. A missed or misread line shows up here."""
    name = f"receipt {receipt.number}"
    if receipt.total is None:
        return [f"{name}: total not read, so it can't be added up"]
    if receipt.unpriced:
        return [f"{name}: no price read for " + ", ".join(receipt.unpriced)]
    if not receipt.lines:
        return [f"{name}: no priced item read"]
    items = sum((line.amount for line in receipt.lines), Decimal(0))
    discounts, tax = sum(receipt.discounts, Decimal(0)), sum(receipt.tax, Decimal(0))
    added = items + discounts + tax
    if abs(added - receipt.total) <= Decimal(rules["thresholds"]["money_tolerance"]):
        return []
    return [
        f"{name} doesn't add up: {len(receipt.lines)} items {items}, discounts {discounts}, "
        f"tax {tax} make {added}; printed total {receipt.total}"
    ]


def read_inventory(
    client: SIEClient,
    calls: Calls,
    page: Page,
    data_dir: Path,
    rules: dict[str, Any],
    result: ClaimResult,
) -> None:
    """OCR a handwritten inventory and read its items and written total into `result`.

    Every line with a price on it must be an item or the total: a priced line the model didn't
    point at goes to review, so a dropped item can't let the claim pass on the items that were
    read. A photo that can't be read sets `inventory_unread`.
    """
    result.inventory_page = page
    text, result.inventory_unread, result.inventory_sie_failed = ocr_text(
        client, calls, page, data_dir, rules
    )
    if result.inventory_unread:
        return
    lines = flatten_markdown(text).splitlines()
    key = f"{result.claim.claim_number}/inventory"
    try:
        spans = extract_spans(
            client, calls, key, "inventory", "\n".join(lines), rules, result.notes
        )
    except CallFailed as failure:
        result.inventory_unread = f"fields not read: {failure}"
        result.inventory_sie_failed = True
        return
    labels = rules["labels"]
    total, conflict = one_amount(
        spans["total_claimed"], lines, labels["total"], "inventory total", result.notes
    )
    result.inventory_total = total
    if conflict:
        result.reasons.append(conflict)
    indexes, unpriced = item_line_indexes(spans["items"], lines, rules)
    for span in unpriced:
        result.reasons.append(f"inventory: no price read for '{span}'")
    for index in indexes:
        priced = line_price(lines[index])
        if isinstance(priced, str):
            result.reasons.append(f"inventory: {priced}")
            continue
        quantity, _, price = priced
        description = describe_line(lines[index])
        result.items.append(
            InventoryItem(len(result.items) + 1, description, quantity, price, lines[index])
        )
    for index, line in enumerate(lines):
        if index not in indexes and amounts(line) and not has_label(line, labels["not_item"]):
            result.reasons.append(f"inventory: '{line}' has a price but no item was read from it")
    result.inventory_read = True


# --- Match items to receipt lines (plain Python) ----------------------------------------------


def item_words(text: str, rules: dict[str, Any]) -> set[str]:
    """The words of a description, lower case and singular, digits split from letters."""
    spec = rules["item_match"]
    words: set[str] = set()
    for token in re.findall(r"[a-z]+|\d+", text.casefold()):
        word = spec["same_words"].get(token, token)
        if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
            word = word[:-1]
        if word in spec["ignore_words"] or (len(word) == 1 and word.isalpha()):
            continue
        words.add(word)
    return words


def words_agree(claimed: str, printed: str, rules: dict[str, Any]) -> tuple[bool, int]:
    """Whether the inventory's description and a receipt line name the same thing, and how many
    words they share.

    Every number on the inventory's side must be on the receipt line ("14in" is not "15in"),
    and enough of its words must be there (see `item_match` in rules.yaml). A one-word
    description only agrees with a one-word line: "Laptop" is not "LAPTOP SLEEVE 14IN".
    """
    spec = rules["item_match"]
    mine, theirs = item_words(claimed, rules), item_words(printed, rules)
    shared = len(mine & theirs)
    if not mine:
        return False, 0
    numbers_ok = {w for w in mine if w.isdigit()} <= theirs
    enough = shared >= min(spec["min_shared_words"], len(theirs))
    share_ok = shared / len(mine) >= float(spec["min_word_share"])
    return numbers_ok and enough and share_ok, shared


def _where(line: ReceiptLine) -> str:
    return f"receipt {line.receipt} line {line.line} '{line.description}'"


def match_item(
    item: InventoryItem,
    lines: list[ReceiptLine],
    left: dict[int, int],
    result: ClaimResult,
    rules: dict[str, Any],
) -> Row:
    """Find the receipt line that supports one inventory item and say what it shows.

    `left` is how many units of each line (by position in `lines`) earlier items haven't used.
    Same words and the same price for enough units is the evidence a match needs; the receipt
    date then picks `matched` or `after_loss`. Same price with words that only partly agree is
    `ambiguous`, never a match, and so are two different lines that both fit.
    """
    loss = result.claim.date_of_loss
    same_words, same_price, partial = [], [], []
    for position, line in enumerate(lines):
        agree, shared = words_agree(item.description, line.description, rules)
        priced = line.unit_price * item.quantity == item.amount
        if agree:
            same_words.append(position)
        if agree and priced:
            same_price.append(position)
        elif priced and shared and left[position] > 0:
            partial.append(position)
    unread = [r for r in result.receipts if r.unread]
    usable = [p for p in same_price if left[p] >= item.quantity]
    if usable:
        dated = [p for p in usable if result.receipt(lines[p].receipt).bought is not None]
        if not dated:
            return Row(
                NOT_READ,
                f"on {_where(lines[usable[0]])}, but that receipt's purchase date wasn't read",
                item,
                lines[usable[0]],
            )
        before = [p for p in dated if cast(date, result.receipt(lines[p].receipt).bought) <= loss]
        chosen = before or dated
        kinds = {_squash(lines[p].description) for p in chosen}
        if len(kinds) > 1:
            fits = "; ".join(_where(lines[p]) for p in chosen)
            return Row(AMBIGUOUS, f"two different receipt lines fit: {fits}", item)
        position = chosen[0]
        left[position] -= item.quantity
        line = lines[position]
        receipt = result.receipt(line.receipt)
        bought = cast(date, receipt.bought).isoformat()
        if before:
            return Row(MATCHED, f"{_where(line)}, bought {bought}", item, line)
        return Row(
            AFTER_LOSS,
            f"{_where(line)}, bought {bought}, after the loss on {loss.isoformat()}; "
            "maybe the replacement purchase",
            item,
            line,
        )
    if same_price:
        line = lines[same_price[0]]
        return Row(
            AMBIGUOUS,
            f"{_where(line)} has {left[same_price[0]]} of {line.quantity} left for this item, "
            f"which claims {item.quantity}",
            item,
            line,
        )
    if partial:
        line = lines[partial[0]]
        return Row(
            AMBIGUOUS,
            f"same price as {_where(line)}, but the words don't agree",
            item,
            line,
        )
    if same_words:
        line = lines[same_words[0]]
        detail = (
            f"{_where(line)} at {line.unit_price} each; the inventory says "
            f"{item.amount / item.quantity:.2f} each"
        )
        if unread:
            names = ", ".join(r.name for r in unread)
            return Row(NOT_READ, f"{detail}; it may also be on {names}, not read", item, line)
        return Row(PRICE_DIFFERS, detail, item, line)
    if unread:
        names = ", ".join(r.name for r in unread)
        return Row(NOT_READ, f"on no receipt that was read; {names} wasn't read", item)
    return Row(NO_RECEIPT, "on no receipt line", item)


def reconcile(result: ClaimResult, rules: dict[str, Any], guideline: dict[str, Any]) -> None:
    """Match every inventory item to a receipt line, then list the lines nobody claimed."""
    lines = [line for receipt in result.receipts for line in receipt.lines]
    left = {position: line.quantity for position, line in enumerate(lines)}
    threshold = Decimal(guideline["proof_threshold"])
    for item in result.items:
        row = match_item(item, lines, left, result, rules)
        if row.status == NO_RECEIPT:
            row.proof_requested = (
                "yes" if item.amount > threshold else f"no: at or below {threshold}"
            )
        result.rows.append(row)
    referenced = {id(row.receipt_line) for row in result.rows if row.receipt_line is not None}
    for line in lines:
        if id(line) not in referenced:
            result.rows.append(Row(NOT_CLAIMED, "on the receipt, not on the inventory", None, line))


# --- Claim status and the request list --------------------------------------------------------


def items_against_total(
    items: list[InventoryItem], written: Decimal, rules: dict[str, Any]
) -> list[str]:
    """A problem when the inventory's items don't add up to its written total.

    The policyholder writes the total with sales tax, so the total must lie between the items'
    sum and that sum plus `max_sales_tax_rate`. A priced line no item was read from shows up
    here even when nothing else flagged it.
    """
    listed = sum((item.amount for item in items), Decimal(0))
    rate = Decimal(rules["thresholds"]["max_sales_tax_rate"])
    most = (listed * (1 + rate)).quantize(Decimal("0.01"))
    if listed <= written <= most:
        return []
    return [
        f"inventory items add up to {listed}; the written total {written} is not that plus "
        f"sales tax of at most {float(rate) * 100:g}%"
    ]


def settle(result: ClaimResult, guideline: dict[str, Any], rules: dict[str, Any]) -> None:
    """Collect the reasons for review and, for a claim in review, the requests to send."""
    claim = result.claim
    reasons: list[str] = []
    for receipt in result.receipts:
        if receipt.unread:
            reasons.append(f"{receipt.name} {receipt.unread}")
        reasons += receipt.problems
    if claim.inventory_source == "handwritten" and result.inventory_read:
        written = result.inventory_total
        if written is None:
            reasons.append("inventory total not read")
        elif written != claim.contents_claimed:
            reasons.append(
                f"inventory total {written} doesn't equal the {claim.contents_claimed} "
                "in the claims system"
            )
        if written is not None and result.items:
            reasons += items_against_total(result.items, written, rules)
    if result.inventory_read and not result.items:
        reasons.append("no priced item read on the inventory")
    for row in result.rows:
        if row.item is None or row.status == MATCHED:
            continue
        if row.status == NO_RECEIPT and row.proof_requested != "yes":
            continue
        reasons.append(f"{row.item.label()}: {row.status}, {row.detail}")
    result.reasons += reasons
    if result.status == REVIEW:
        result.requests = requests(result, guideline)


def requests(result: ClaimResult, guideline: dict[str, Any]) -> list[str]:
    """What to ask the policyholder for, in the guideline's words.

    Other proof for each item with no receipt above the threshold, a new photo of each photo
    that couldn't be read (never when SIE was the reason: that's ours to rerun), and a price
    confirmation for each price that differs. `ambiguous` and `after_loss` stay with the
    handler, who knows whether to ask.
    """
    wording = guideline["requests"]
    other = guideline["other_proof_accepted"]
    other_proof = ", ".join(other[:-1]) + ", or " + other[-1] if len(other) > 1 else other[0]
    asks: list[str] = []
    if result.inventory_unread and not result.inventory_sie_failed:
        asks.append(wording["inventory_not_read"].strip())
    for receipt in result.receipts:
        if not receipt.unread or receipt.sie_failed:
            continue
        covers = [
            row.item.label()
            for row in result.rows
            if row.item is not None and row.status == NOT_READ and receipt.name in row.detail
        ]
        photo = f"the {_ordinal(receipt.number)} receipt photo"
        tail = f" It may show: {'; '.join(covers)}." if covers else ""
        asks.append(wording["not_read"].format(photo=photo, covers=tail).strip())
    for row in result.rows:
        item = row.item
        if item is None:
            continue
        if row.status == NO_RECEIPT and row.proof_requested == "yes":
            asks.append(
                wording["no_receipt"]
                .format(item=item.description, amount=f"{item.amount:.2f}", other_proof=other_proof)
                .strip()
            )
        elif row.status == PRICE_DIFFERS and row.receipt_line is not None:
            line = row.receipt_line
            receipt = result.receipt(line.receipt)
            asks.append(
                wording["price_differs"]
                .format(
                    item=item.description,
                    claimed=f"{item.amount:.2f}",
                    merchant=receipt.merchant or f"receipt {receipt.number}",
                    bought=receipt.bought.isoformat() if receipt.bought else "(date not read)",
                    receipt_price=f"{line.unit_price * item.quantity:.2f}",
                )
                .strip()
            )
    return asks


def _ordinal(number: int) -> str:
    return ORDINALS[number - 1] if number <= len(ORDINALS) else f"number {number}"


# --- Run --------------------------------------------------------------------------------------


def process_claim(
    client: SIEClient,
    calls: Calls,
    system: ClaimsSystem,
    claim_number: str,
    rules: dict[str, Any],
    guideline: dict[str, Any],
    data_dir: Path,
) -> ClaimResult:
    claim = system.get_claim(claim_number)
    if claim is None:
        raise KeyError(f"{claim_number} is not in the claims system")
    result = ClaimResult(claim)
    attachments = system.attachments(claim_number)
    receipts = [a for a in attachments if a.kind == "receipt"]
    for number, attachment in enumerate(receipts, start=1):
        result.receipts.append(
            read_receipt(client, calls, number, attachment, data_dir, rules, result)
        )
    if claim.inventory_source == "portal":
        result.inventory_read = True
        for row in system.inventory_rows(claim_number):
            if row.quantity < 1:
                result.reasons.append(
                    f"inventory: portal row {row.line} has quantity {row.quantity}"
                )
                continue
            amount = row.unit_price * row.quantity
            item = InventoryItem(
                row.line, row.description, row.quantity, amount, f"portal row {row.line}"
            )
            result.items.append(item)
        result.notes.append("inventory from the portal: no written total to compare")
    else:
        inventory = next((a for a in attachments if a.kind == "inventory"), None)
        if inventory is None:
            result.reasons.append("no inventory attached to the claim")
        else:
            page = Page(inventory.file, inventory.page)
            read_inventory(client, calls, page, data_dir, rules, result)
            if result.inventory_unread:
                where = f"inventory ({page.file} p{page.page})"
                result.reasons.append(f"{where} {result.inventory_unread}")
    if result.inventory_read:
        reconcile(result, rules, guideline)
    settle(result, guideline, rules)
    return result


def run(
    client: SIEClient,
    rules: dict[str, Any],
    guideline: dict[str, Any],
    data_dir: Path = DATA_DIR,
) -> tuple[list[ClaimResult], Calls]:
    system = ClaimsSystem(data_dir)
    calls = Calls()
    results = [
        process_claim(client, calls, system, number, rules, guideline, data_dir)
        for number in system.open_contents_claims()
    ]
    calls.check_all_failed()
    return results, calls


def csv_safe(value: Any) -> str:
    """A leading ' on a cell a spreadsheet would run as a formula; anything else as written."""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_safe(value) for key, value in row.items()})


def table_row(result: ClaimResult, row: Row) -> dict[str, Any]:
    """One reconciliation-table row: the inventory side, the receipt side and the status."""
    item, line = row.item, row.receipt_line
    receipt = result.receipt(line.receipt) if line else None
    return {
        "claim": result.claim.claim_number,
        "inventory_line": item.line if item else "",
        "inventory_item": item.description if item else "",
        "inventory_qty": item.quantity if item else "",
        "inventory_amount": f"{item.amount:.2f}" if item else "",
        "receipt": line.receipt if line else "",
        "receipt_page": f"{receipt.page.file} p{receipt.page.page}" if receipt else "",
        "receipt_line": line.line if line else "",
        "receipt_item": line.description if line else "",
        "receipt_qty": line.quantity if line else "",
        "receipt_unit_price": f"{line.unit_price:.2f}" if line else "",
        "purchase_date": receipt.bought.isoformat() if receipt and receipt.bought else "",
        "status": row.status,
        "proof_requested": row.proof_requested,
        "detail": row.detail,
    }


def request_page(result: ClaimResult, guideline: dict[str, Any]) -> str:
    """The request list for one claim in review, as Markdown the handler can check and send."""
    claim = result.claim
    lines = [
        f"# {claim.claim_number}: proof to ask for",
        "",
        f"Policy {claim.policy_number}, {claim.policyholder}, loss on "
        f"{claim.date_of_loss.isoformat()}. Check status: {result.status}.",
        "",
        "## Why this claim is in review",
        "",
        *(f"- {reason}" for reason in result.reasons),
        "",
        "## To ask the policyholder for",
        "",
    ]
    if result.requests:
        lines += [f"{n}. {text}" for n, text in enumerate(result.requests, start=1)]
    else:
        lines.append(guideline["requests"]["nothing_to_ask"].strip())
    below = [
        row.item.label()
        for row in result.rows
        if row.item is not None and row.status == NO_RECEIPT and row.proof_requested != "yes"
    ]
    if below:
        threshold = guideline["proof_threshold"]
        lines += ["", f"Not asked for (no receipt, at or below {threshold}): " + "; ".join(below)]
    return "\n".join(lines) + "\n"


def write_outputs(output_dir: Path, results: list[ClaimResult], guideline: dict[str, Any]) -> None:
    (output_dir / "claims").mkdir(parents=True, exist_ok=True)
    (output_dir / "requests").mkdir(parents=True, exist_ok=True)
    for result in results:
        number = result.claim.claim_number
        text = json.dumps(result.as_json(), indent=2, ensure_ascii=False) + "\n"
        (output_dir / "claims" / f"{number}.json").write_text(text, encoding="utf-8")
        if result.status == REVIEW:
            page = request_page(result, guideline)
            (output_dir / "requests" / f"{number}.md").write_text(page, encoding="utf-8")
    table = [table_row(result, row) for result in results for row in result.rows]
    _write_csv(output_dir / "reconciliation.csv", TABLE_FIELDS, table)
    claims = [
        {
            "claim": r.claim.claim_number,
            "policyholder": r.claim.policyholder,
            "date_of_loss": r.claim.date_of_loss.isoformat(),
            "contents_claimed": str(r.claim.contents_claimed),
            "inventory_source": r.claim.inventory_source,
            "status": r.status,
            "items": len(r.items),
            "matched": sum(1 for row in r.rows if row.item and row.status == MATCHED),
            "requests": len(r.requests),
            "reasons": " | ".join(r.reasons),
        }
        for r in results
    ]
    _write_csv(output_dir / "claims.csv", CLAIM_FIELDS, claims)


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    guideline = load_guideline()
    started = time.monotonic()
    with make_client() as client:
        results, calls = stop_on_refusal(lambda: run(client, load_rules(), guideline))
    elapsed = time.monotonic() - started
    write_outputs(output, results, guideline)
    for result in results:
        counts: dict[str, int] = {}
        for row in result.rows:
            counts[row.status] = counts.get(row.status, 0) + 1
        summary = ", ".join(f"{n} {status}" for status, n in counts.items()) or "no items"
        number = result.claim.claim_number
        print(f"{number:<12} {result.status:<14} {summary}; {len(result.requests)} requests")
        for reason in result.reasons:
            print(f"{'':<12} - {reason}")
    per_model = ", ".join(f"{count} to {model}" for model, count in calls.count.items())
    print(f"\n{calls.total} SIE calls ({per_model}) in {elapsed:.0f} s")
    print("Statuses name what the checks found; the handler decides what proof is enough.")
    print(f"table:    {output / 'reconciliation.csv'}\nrequests: {output / 'requests'}")


if __name__ == "__main__":
    main()
