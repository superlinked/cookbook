"""Check a repair invoice against the open claim it was filed under before it's paid.

Use case:  Repair-invoice checking on an existing property or auto claim.
Who:       Claims processors and desk adjusters who handle the payment step on a claim.
Problem:   The shop's invoice lands on the claim as a PDF, scan or photo. Someone opens it next
           to the claim, checks it's this claim's invoice (claim number, insured, address or
           vehicle) from the shop on the claim, adds up the lines, checks the date and compares
           the total with what the adjuster authorized, then retypes the invoice number, date
           and total into the payment screen.
Input:     The claims system (claims_system.py over four CSV seeds in data/: open claims, the
           vendors approved on each, the authorized estimate and supplements, and the invoices
           waiting on each claim) and data/repair-invoices.pdf (the scanned invoices). The
           fields, checks and thresholds are in rules.yaml.
SIE pipeline:
           extract  lightonai/LightOnOCR-3-4B  read each scanned invoice page as Markdown (the
                                               line-item table comes back as a table); called
                                               once per page
           extract  fastino/gliner2-large-v1   locate the vendor, invoice number and date,
                                               bill-to, claim number, service address or
                                               vehicle, subtotal, tax and total; called twice
                                               per window of about 512 tokens (a schema call
                                               and an entity call)
           (Python)                            take each labelled cell, read the line items,
                                               check everything against the claim record
Output:    invoices/<document>.json per invoice (status, fields, line items, every check with
           its detail), invoices.csv, payment-queue.csv (checks_passed invoices with the
           invoice number, date and total ready to key in) and exceptions-queue.csv (one row
           per failed check). The claim handler decides what happens next.
Why SIE:   Two models, and no generative model makes the decision. The claim match, vendor
           match, arithmetic, dates and authorized amount are plain Python reading rules.yaml.
Runs on:   SIE Cloud, or your own SIE with SIE_BASE_URL.

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
from typing import Any, cast

import pypdfium2 as pdfium
import yaml
from claims_system import ClaimDocument, ClaimRecord, ClaimsSystem, claim_key
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
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
# A failed SIE call's message is cut to this many characters in reasons and the summary line.
ERROR_CHARS = 200
# A cell starting with one of these runs as a formula when a spreadsheet opens the CSV.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")

PASS, REVIEW = "pass", "review"
# Invoice status: what the checks found. The claim handler decides what happens next.
CHECKS_PASSED = "checks_passed"
# When GLiNER2 finds nothing, SIE Cloud answers the output_schema call with HTTP 500 and this
# message. We record it as "no fields found"; a missing field can only send a check to review,
# never pass it.
EMPTY_RESULT_ERROR = "Inference completed without a valid conserved billing settlement"
PAYMENT_FIELDS = (
    *("document", "claim_number", "named_insured", "vendor", "invoice_number"),
    *("invoice_date", "subtotal", "tax", "total", "authorized"),
)


@dataclass
class Check:
    check: str
    status: str  # pass or review
    detail: str


@dataclass
class InvoiceResult:
    document: str
    claim_number: str  # the claim the invoice was filed under
    received: date
    claim: ClaimRecord | None = None
    fields: dict[str, str] = field(default_factory=dict)
    line_items: list[dict[str, str]] = field(default_factory=list)
    table_problems: list[str] = field(default_factory=list)  # line-item rows that didn't read
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def flagged(self) -> list[Check]:
        return [check for check in self.checks if check.status != PASS]

    @property
    def status(self) -> str:
        return REVIEW if self.flagged or not self.checks else CHECKS_PASSED

    def add(self, check: str, status: str, detail: str) -> None:
        self.checks.append(Check(check, status, detail))

    def as_json(self) -> dict[str, Any]:
        claim = self.claim
        return {
            "document": self.document,
            "claim_number": self.claim_number,
            "received": self.received.isoformat(),
            "status": self.status,
            "review_checks": [check.check for check in self.flagged],
            "claim": None
            if claim is None
            else {
                "named_insured": claim.named_insured,
                "loss_date": claim.loss_date.isoformat(),
                "loss_location": claim.vin or claim.loss_address,
                "vendors": list(claim.vendors),
                "authorized": f"{claim.authorized:.2f}",
            },
            "fields": self.fields,
            "line_items": self.line_items,
            "checks": [vars(check) for check in self.checks],
            "notes": self.notes,
        }


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def output_schema(rules: dict[str, Any]) -> dict[str, Any]:
    """The GLiNER2 output_schema for the invoice: one optional string per field."""
    properties = {
        name: {"type": "string", "description": spec["description"]}
        for name, spec in rules["documents"]["invoice"].items()
    }
    return {"type": "object", "properties": properties}


def make_client() -> SIEClient:
    """Build an SIE client from the API key and SIE_BASE_URL in the environment or `.env`."""
    load_dotenv()
    api_key = os.environ.get("SIE_API_KEY", "").strip()
    base_url = os.environ.get("SIE_BASE_URL", "").strip()
    if not api_key and not base_url:
        raise RuntimeError("Set SIE_API_KEY, or SIE_BASE_URL for your own SIE, in .env")
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
    """One SIE call failed for a reason that may pass; the invoice it was for goes to review."""


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
        reply without `key`, is recorded and raised as CallFailed, so the caller sends its
        invoice to review and the run goes on. With `empty_result_ok` (the GLiNER2 calls), SIE
        Cloud's empty-result HTTP 500 is a reply with nothing in it, not a failure. An error
        that isn't an SIE error is a bug in this code and is raised as is.
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
            missing = SIEError(f"{model} replied without '{key}'")
            self.failures.append((model, missing))
            raise CallFailed(f"the SIE call failed: {short(missing)}")
        return cast(dict[str, Any], reply)

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
        print(
            f"{n} of {total} SIE calls failed; their invoices go to review. First: {short(first)}"
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


def is_fatal(error: SIEError) -> bool:
    """A refusal that fails every call the same way: a 4xx (bad key 401, forbidden 403, model
    not found 404, no credits or spend limit 402, inactive account) or a model the server
    marked as permanently unable to load. A RequestError with no HTTP status or a status below
    400 is a reply the SDK couldn't parse, and is fatal too. A rate limit, an over-long input, a
    timeout, a lost connection, a model still loading and a 5xx are transient or per-item:
    those invoices go to review."""
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
    """The error for a per-invoice reason or the summary line, its message cut short."""
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


# --- Read the invoice (LightOnOCR) ------------------------------------------------------------


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
    """OCR one page of a scanned PDF (1-based page number): its Markdown. Raises CallFailed
    on a transient SIE error, and on a reply with no markdown entity (a failed call too)."""
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
        missing = SIEError(f"{model} replied without a markdown entity")
        calls.failures.append((model, missing))
        raise CallFailed(f"the SIE call failed: {short(missing)}")
    return text


# --- Flatten the OCR Markdown (plain Python) --------------------------------------------------


def flatten_markdown(text: str) -> str:
    """OCR Markdown to plain lines, one per table row, with cells joined by " | ".

    LightOnOCR writes a box as a table cell holding the label, a <br> and the value, so
    "Total due<br>$3,597.64" becomes "Total due | $3,597.64": each label sits on its value's
    line. Markdown pipe tables get the same shape; headings, bold marks and bullets are dropped
    and HTML entities decoded.
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


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _label_at(line: str, label: str) -> re.Match[str] | None:
    return re.search(rf"(?<![a-z]){re.escape(label)}(?![a-z])", line.casefold())


def labelled_cell(value: str, window: str, labels: list[str]) -> str | None:
    """The whole cell after a label, on the line where the model's span sits; None if no label.

    "Total due | $3,597.64" with the span "3,597.64" gives "$3,597.64", and "Sales tax (6%) |
    203.64" with "203.64" gives "203.64": up to 20 characters of qualifier may sit between the
    label and its " | " or ":". Cells on one line are split at " | ". Labels match whole words,
    so "total" does not match "Subtotal".
    """
    span = _squash(value)
    for line in window.splitlines():
        for label in labels:
            match = _label_at(line, label)
            if match is None:
                continue
            rest = line[match.end() :]
            # "Claim No.: | value": the OCR put a label ending in a colon in a cell of its own.
            # Treat the colon and the cell break as one separator, or the cell comes back empty.
            rest = re.sub(r"^[^:|]{0,20}?:\s*(?=\|)", "", rest, count=1)
            if re.match(r"^[^:|]{0,20}[:|]", rest):
                rest = re.sub(r"^[^:|]*?[:|]", "", rest, count=1)
            cell = rest.split(" | ")[0].strip(" :|")
            if span and span in _squash(cell):
                return cell
    return None


def letterhead_line(value: str, window: str, not_labels: list[str]) -> str | None:
    """The whole line the span sits on, unless it carries one of `not_labels`.

    This is how the vendor is read from the letterhead and never from the bill-to line.
    """
    span = _squash(value)
    for line in window.splitlines():
        if span and span in _squash(line):
            if any(_label_at(line, label) for label in not_labels):
                continue
            return line.strip(" |")
    return None


# --- Extract fields (GLiNER2) -----------------------------------------------------------------


def extract_fields(
    client: SIEClient, calls: Calls, text: str, rules: dict[str, Any], result: InvoiceResult
) -> dict[str, str]:
    """Find the invoice's fields with GLiNER2 and keep the labelled cell for each span.

    Each window gets two calls to the same model: one with the `output_schema` (one value per
    field in `documents`), and one with the labels in `entity_labels`, which returns every
    span it finds (every street address on the page, the shop's included). Either way, a span
    that is not in the window's text, or not on a line carrying the field's label (or, for the
    letterhead, on a bill-to or claim line), is dropped with a note, so its check goes to review
    instead of using a value nobody wrote. A field read with two different values (in two
    windows, or twice in one) goes to review as `conflicting_values` and is left out, so the
    check that needs it says it was not read. A window whose SIE call failed sends the invoice
    to review as `readable`.
    """
    model, limits = rules["models"]["extract"], rules["thresholds"]
    specs: dict[str, Any] = {**rules["documents"]["invoice"], **rules["entity_fields"]}
    labels: dict[str, str] = rules["entity_labels"]
    requests: list[tuple[str, dict[str, Any], str]] = [
        ("fields", {"output_schema": output_schema(rules)}, "data"),
        ("entities", {"labels": list(labels)}, "entities"),
    ]
    found: dict[str, str] = {}
    conflicts: dict[str, list[str]] = {}
    parts = windows(flatten_markdown(text), limits["window_words"], limits["window_overlap_words"])
    for index, window in enumerate(parts, start=1):
        for kind, options, key in requests:
            item = Item(id=f"extract/{result.document}-w{index}-{kind}", text=window)
            # One item per call: the empty-result HTTP 500 would fail a whole batch.
            try:
                # Ask SIE to fail instead of cutting a window that runs over 512 tokens.
                work = functools.partial(
                    client.extract,
                    model,
                    item,
                    options={"overflow_policy": "error"},
                    provision_timeout_s=WAIT_S,
                    **options,
                )
                response = calls.call(model, work, key, empty_result_ok=True)
            except CallFailed as failure:
                detail = f"invoice window {index} ({kind}) was not read: {failure}"
                result.add("readable", REVIEW, detail)
                continue
            if response.get("empty_result_error"):
                result.notes.append(f"window {index} {kind}: none found (HTTP 500 empty result)")
            if kind == "fields":
                spans = list((response.get("data") or {}).items())
            else:
                spans = [
                    (labels.get(str(entity.get("label")), ""), entity.get("text"))
                    for entity in response.get("entities") or []
                ]
            for name, value in spans:
                spec = specs.get(name)
                if spec is None:
                    continue
                cell = span_cell(name, value, spec, window, result)
                if cell is None:
                    continue
                if name in found and _squash(found[name]) != _squash(cell):
                    conflicts.setdefault(name, [found[name]]).append(cell)
                found.setdefault(name, cell)
    for name, values in conflicts.items():
        found.pop(name, None)
        shown = " / ".join(f"'{value}'" for value in dict.fromkeys(values))
        result.add("conflicting_values", REVIEW, f"{name} read as {shown}")
    return found


def span_cell(
    name: str, value: Any, spec: dict[str, Any], window: str, result: InvoiceResult
) -> str | None:
    """The cell a model span points at, or None (with a note when a span was dropped)."""
    if not isinstance(value, str) or not re.search(r"[^\W_]", value):
        return None  # empty or a form blank ("______")
    # A cell never runs over a line break in the flattened text.
    value = value.strip().splitlines()[0].strip(" |")
    if _squash(value) not in _squash(window):
        result.notes.append(f"{name}: '{value}' is not in the text; dropped")
        return None
    if spec.get("letterhead"):
        cell = letterhead_line(value, window, spec.get("not_label", []))
    else:
        cell = labelled_cell(value, window, spec["label"])
    if cell is None:
        result.notes.append(f"{name}: '{value}' is not on a labelled line; dropped")
    return cell


# --- Line items (plain Python) ----------------------------------------------------------------


def parse_money(value: str) -> Decimal | None:
    """'$3,597.64', 'USD 3597.64' or '3,597.64USD' -> Decimal('3597.64'); '-12.00' keeps its
    sign. None when the text is not exactly one amount ("6%", "$10.00 $12.00", "1.346,20")."""
    cleaned = re.sub(r"\s+|\$|usd", "", value.casefold())
    match = re.fullmatch(r"(-?)(\d{1,3}(?:,\d{3})+|\d+)(\.\d{1,2})?", cleaned)
    if match is None:
        return None
    try:
        return Decimal(match.group(1) + match.group(2).replace(",", "") + (match.group(3) or ""))
    except InvalidOperation:
        return None


def parse_quantity(value: str) -> Decimal | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", value)
    return Decimal(match.group(1)) if match else None


def line_items(flat: str, rules: dict[str, Any]) -> tuple[list[dict[str, str]], list[str]]:
    """The invoice's line items, and the rows that could not be read.

    The table starts at a header row naming the Qty, Unit price and Amount columns. Every line
    under it with the header's number of cells is one item; a line with a different number of
    cells (two or more) is a row the OCR split or merged, returned as a problem, not dropped.
    The first line with a single cell (the totals or the text after the table) ends it.
    """
    columns: dict[str, list[str]] = rules["line_item_columns"]
    items: list[dict[str, str]] = []
    problems: list[str] = []
    header: dict[str, int] | None = None
    width = 0
    for line in flat.splitlines():
        cells = [cell.strip() for cell in line.split(" | ")]
        lowered = [cell.casefold() for cell in cells]
        positions = {
            name: next((i for i, cell in enumerate(lowered) if cell in names), -1)
            for name, names in columns.items()
        }
        if all(position >= 0 for position in positions.values()):
            header, width = positions, len(cells)
            continue
        if header is None:
            continue
        totals = any(_label_at(line, word) for word in ("subtotal", "total", "tax"))
        if len(cells) == width:
            values = {name: cells[i] for name, i in header.items()}
            numbers = [
                parse_quantity(values["qty"]),
                parse_money(values["unit_price"]),
                parse_money(values["amount"]),
            ]
            if None not in numbers:
                description = next((c for i, c in enumerate(cells) if i not in header.values()), "")
                items.append({"description": description, **values})
                continue
            if totals or not any(values.values()):
                header = None  # the totals block, inside the same table: the items ended
                continue
            problems.append(f"row '{line}' has a cell that is not a number")
            continue
        if len(cells) > 1 and not totals:
            problems.append(f"row '{line}' has {len(cells)} cells, the table has {width}")
            continue
        header = None  # the table ended
    return items, problems


# --- Checks (plain Python) --------------------------------------------------------------------


def parse_date(value: str, formats: list[str]) -> date | None:
    cleaned = re.sub(r"\s+", " ", value).strip(" .,")
    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def vendor_key(name: str, rules: dict[str, Any]) -> str:
    words = re.findall(r"[a-z0-9]+", name.casefold().replace("&", " and "))
    return " ".join(word for word in words if word not in rules["vendor_suffixes_ignored"])


def name_key(name: str) -> tuple[str, ...]:
    """A person's name as its words, sorted so the order doesn't matter: "Whitcombe, Dana" and
    "Dana Whitcombe" agree."""
    return tuple(sorted(re.findall(r"[a-z]+", name.casefold())))


def address_key(address: str, rules: dict[str, Any]) -> str:
    """An address without case or punctuation, with street words shortened ("Lane" -> "ln")."""
    short_forms: dict[str, str] = rules["address_abbreviations"]
    words = re.findall(r"[a-z0-9]+", address.casefold())
    return " ".join(short_forms.get(word, word) for word in words)


def vins_in(text: str) -> list[str]:
    """Every 17-character token of letters and digits in the text, upper case."""
    return re.findall(r"(?<![A-Z0-9])[A-Z0-9]{17}(?![A-Z0-9])", text.upper())


def _unread(result: InvoiceResult, check: str, values: dict[str, str | None]) -> bool:
    """Send `check` to review when a value it needs was not read. True if one was missing."""
    missing = [label for label, value in values.items() if not value]
    if missing:
        result.add(check, REVIEW, "Not read: " + ", ".join(missing))
    return bool(missing)


def claim_problems(
    invoice: dict[str, str], claim: ClaimRecord, claims: ClaimsSystem, rules: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Why this may not be this claim's invoice, and what did match.

    The claim number on the invoice must be exactly the claim's; the bill-to name must be the
    named insured (word order and punctuation aside); and the service address must be the loss
    address, or for an auto claim the vehicle line must carry the claim's VIN and no other. A
    value that was not read is a problem, never a match.
    """
    problems: list[str] = []
    matched: list[str] = []
    printed = invoice.get("claim_number")
    if not printed:
        problems.append("no claim number read on the invoice")
    elif claim_key(printed) == claim_key(claim.claim_number):
        matched.append(f"claim number {claim.claim_number}")
    else:
        other = claims.get_claim(printed)
        where = (
            f"which is another claim ({other.named_insured})"
            if other is not None
            else "which is not a claim in the claims system"
        )
        problems.append(f"invoice says claim {printed}, {where}; filed under {claim.claim_number}")

    bill_to = invoice.get("bill_to")
    if not bill_to:
        problems.append("no bill-to name read")
    elif name_key(bill_to) == name_key(claim.named_insured):
        matched.append(f"bill-to {claim.named_insured}")
    else:
        shared = set(name_key(bill_to)) & set(name_key(claim.named_insured))
        kind = "a possible match only" if shared else "not the insured"
        problems.append(f"billed to '{bill_to}', {kind}; insured is {claim.named_insured}")

    location = invoice.get("service_location")
    if claim.vin:
        if not location:
            problems.append(f"no vehicle read; the claim's VIN is {claim.vin}")
        elif vins_in(location) == [claim.vin]:
            matched.append(f"VIN {claim.vin}")
        else:
            problems.append(f"vehicle '{location}' is not the claim's VIN {claim.vin} alone")
    elif not location:
        problems.append("no service address read")
    elif address_key(location, rules) == address_key(claim.loss_address, rules):
        matched.append(f"address {claim.loss_address}")
    else:
        problems.append(f"service address '{location}'; loss address is {claim.loss_address}")
    return problems, matched


def check_invoice_math(
    invoice: dict[str, str], result: InvoiceResult, rules: dict[str, Any]
) -> None:
    """Each line is qty x unit price, the lines add up to the subtotal, subtotal + tax = total.

    A blank tax line is not zero: the tax must be read as an amount ("0.00" is one), or the
    arithmetic can't be checked. A line that shows only the rate ("(6%)") is a blank line.
    """
    items = result.line_items
    tax_cell = invoice.get("tax")
    if tax_cell and re.fullmatch(r"\(?\s*\d+(?:\.\d+)?\s*%\s*\)?", tax_cell.strip()):
        tax_cell = None  # the rate, with no amount next to it
    needed = {
        "line items": "yes" if items else None,
        "subtotal": invoice.get("subtotal"),
        "tax (a blank tax line is not zero)": tax_cell,
        "total": invoice.get("total"),
    }
    if _unread(result, "invoice_math", needed):
        return
    tolerance = Decimal(str(rules["thresholds"]["money_tolerance"]))
    problems: list[str] = list(result.table_problems)
    line_sum = Decimal("0")
    for item in items:
        qty, unit, amount = (
            parse_quantity(item["qty"]),
            parse_money(item["unit_price"]),
            parse_money(item["amount"]),
        )
        if qty is None or unit is None or amount is None:
            problems.append(f"line '{item['description']}' has an unreadable number")
            continue
        line_sum += amount
        if abs(qty * unit - amount) > tolerance:
            problems.append(f"'{item['description']}' {qty} x {unit} = {qty * unit}, not {amount}")
    subtotal, tax, total = (parse_money(invoice[name]) for name in ("subtotal", "tax", "total"))
    if subtotal is None or tax is None or total is None:
        shown = ", ".join(f"{name} '{invoice[name]}'" for name in ("subtotal", "tax", "total"))
        result.add("invoice_math", REVIEW, f"Not one amount each: {shown}")
        return
    if subtotal <= 0 or total <= 0 or tax < 0:
        problems.append(
            f"subtotal {subtotal}, tax {tax}, total {total}: a credit or a misread sign"
        )
    if abs(line_sum - subtotal) > tolerance:
        problems.append(f"lines add up to {line_sum}, subtotal says {subtotal}")
    if abs(subtotal + tax - total) > tolerance:
        problems.append(f"subtotal {subtotal} + tax {tax} = {subtotal + tax}, total says {total}")
    if problems:
        result.add("invoice_math", REVIEW, "; ".join(problems))
    else:
        detail = f"{len(items)} lines = {subtotal}; {subtotal} + tax {tax} = {total}"
        result.add("invoice_math", PASS, detail)


def check_invoice(
    invoice: dict[str, str],
    claim: ClaimRecord | None,
    claims: ClaimsSystem,
    rules: dict[str, Any],
    result: InvoiceResult,
) -> None:
    """Run every check for one invoice. Each one not passed is a row in the exceptions queue.

    The claim match runs first. When it fails, the other checks don't run: they would compare
    the invoice with a claim it may not belong to.
    """
    if claim is None or claim.status != "open":
        state = "not in the claims system" if claim is None else f"{claim.status}, not open"
        result.add("claim_match", REVIEW, f"Filed under {result.claim_number}, which is {state}")
        return
    problems, matched = claim_problems(invoice, claim, claims, rules)
    if problems:
        detail = "; ".join(problems) + ". Other checks not run: this may not be this claim's"
        result.add("claim_match", REVIEW, detail + " invoice")
        return
    result.add("claim_match", PASS, ", ".join(matched))

    vendor = invoice.get("vendor_name")
    if not _unread(result, "vendor", {"vendor": vendor}):
        approved = [
            v for v in claim.vendors if vendor_key(v, rules) == vendor_key(str(vendor), rules)
        ]
        on_claim = " / ".join(claim.vendors) or "none"
        if approved:
            result.add("vendor", PASS, f"'{vendor}' is {approved[0]}, approved on the claim")
        else:
            result.add("vendor", REVIEW, f"'{vendor}' is not a vendor on the claim ({on_claim})")

    number = invoice.get("invoice_number")
    if not _unread(result, "invoice_number", {"invoice number": number}):
        if re.fullmatch(r"\S+", str(number)):
            result.add("invoice_number", PASS, str(number))
        else:
            result.add("invoice_number", REVIEW, f"'{number}' is not one invoice number")

    check_invoice_math(invoice, result, rules)

    written = invoice.get("invoice_date")
    if not _unread(result, "invoice_date", {"invoice date": written}):
        issued = parse_date(str(written), rules["date_formats"])
        loss, received = claim.loss_date.isoformat(), result.received.isoformat()
        if issued is None:
            result.add("invoice_date", REVIEW, f"'{written}' is not a date")
        elif issued < claim.loss_date:
            result.add("invoice_date", REVIEW, f"Invoice {issued} is before the loss on {loss}")
        elif issued > result.received:
            result.add("invoice_date", REVIEW, f"Invoice {issued} is after it arrived ({received})")
        else:
            result.add(
                "invoice_date", PASS, f"Loss {loss} <= invoice {issued} <= received {received}"
            )

    total = invoice.get("total")
    if not _unread(result, "authorized_amount", {"total": total}):
        amount = parse_money(str(total))
        tolerance = Decimal(str(rules["thresholds"]["money_tolerance"]))
        parts = " + ".join(f"{a.kind} {a.amount}" for a in claim.authorizations) or "nothing"
        authorized = f"authorized {claim.authorized} ({parts})"
        if amount is None:
            result.add("authorized_amount", REVIEW, f"Total '{total}' is not one amount")
        elif amount - claim.authorized > tolerance:
            over = amount - claim.authorized
            result.add(
                "authorized_amount", REVIEW, f"Total {amount} is over {authorized} by {over}"
            )
        else:
            result.add("authorized_amount", PASS, f"Total {amount} within {authorized}")


# --- Run --------------------------------------------------------------------------------------


def process_invoice(
    client: SIEClient,
    calls: Calls,
    document: ClaimDocument,
    claims: ClaimsSystem,
    rules: dict[str, Any],
    data_dir: Path,
) -> InvoiceResult:
    result = InvoiceResult(document.document, document.claim_number, document.received)
    result.claim = claims.get_claim(document.claim_number)
    where = f"invoice ({document.file} p{document.page})"
    try:
        text = read_page(client, calls, data_dir / document.file, document.page, rules)
    except CallFailed as failure:
        result.add("readable", REVIEW, f"{where} was not read: {failure}")
        return result
    if len(text.strip()) < rules["thresholds"]["min_page_chars"]:
        result.add("readable", REVIEW, f"{where} could not be read")
        return result
    result.fields = extract_fields(client, calls, text, rules, result)
    result.line_items, result.table_problems = line_items(flatten_markdown(text), rules)
    check_invoice(result.fields, result.claim, claims, rules, result)
    return result


def run(
    client: SIEClient, rules: dict[str, Any], data_dir: Path = DATA_DIR
) -> tuple[list[InvoiceResult], Calls]:
    claims = ClaimsSystem(data_dir)
    calls = Calls()
    results = [
        process_invoice(client, calls, document, claims, rules, data_dir)
        for document in claims.waiting_invoices()
    ]
    calls.check_all_failed()
    return results, calls


def payment_row(result: InvoiceResult, rules: dict[str, Any]) -> dict[str, Any]:
    """The payment queue row: what the handler keys into the payment screen, as read."""
    fields, claim = result.fields, cast(ClaimRecord, result.claim)
    issued = parse_date(fields["invoice_date"], rules["date_formats"])
    amounts = {name: parse_money(fields[name]) for name in ("subtotal", "tax", "total")}
    return {
        "document": result.document,
        "claim_number": claim.claim_number,
        "named_insured": claim.named_insured,
        "vendor": fields["vendor_name"],
        "invoice_number": fields["invoice_number"],
        "invoice_date": issued.isoformat() if issued else "",
        **{name: f"{value:.2f}" if value is not None else "" for name, value in amounts.items()},
        "authorized": f"{claim.authorized:.2f}",
    }


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


def write_outputs(output_dir: Path, results: list[InvoiceResult], rules: dict[str, Any]) -> None:
    (output_dir / "invoices").mkdir(parents=True, exist_ok=True)
    for result in results:
        path = output_dir / "invoices" / f"{result.document}.json"
        text = json.dumps(result.as_json(), indent=2, ensure_ascii=False) + "\n"
        path.write_text(text, encoding="utf-8")
    summary = [
        {
            "document": result.document,
            "claim_number": result.claim_number,
            "status": result.status,
            "review_checks": ";".join(check.check for check in result.flagged),
        }
        for result in results
    ]
    _write_csv(
        output_dir / "invoices.csv",
        ("document", "claim_number", "status", "review_checks"),
        summary,
    )
    passed = [payment_row(r, rules) for r in results if r.status == CHECKS_PASSED]
    _write_csv(output_dir / "payment-queue.csv", PAYMENT_FIELDS, passed)
    queue = [
        {
            "document": result.document,
            "claim_number": result.claim_number,
            "check": check.check,
            "detail": check.detail,
        }
        for result in results
        for check in result.flagged
    ]
    _write_csv(
        output_dir / "exceptions-queue.csv", ("document", "claim_number", "check", "detail"), queue
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rules = load_rules()
    started = time.monotonic()
    with make_client() as client:
        results, calls = stop_on_refusal(lambda: run(client, rules))
    elapsed = time.monotonic() - started
    write_outputs(output, results, rules)
    for result in results:
        reasons = "; ".join(f"{check.check}: {check.detail}" for check in result.flagged)
        print(f"{result.document:<7} {result.status:<14} {reasons or '-'}")
    per_model = ", ".join(f"{count} to {model}" for model, count in calls.count.items())
    print(f"\n{calls.total} SIE calls ({per_model}) in {elapsed:.0f} s")
    print("Statuses name what the checks found; the claim handler decides what is paid.")
    print(f"payment queue:    {output / 'payment-queue.csv'}")
    print(f"exceptions queue: {output / 'exceptions-queue.csv'}")


if __name__ == "__main__":
    main()
