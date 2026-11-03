"""Match steel deliveries to their PO lines and check the mill certificates and inspection reports.

Use case:  Goods receipt and receiving inspection of steel plate: is the PO line filled, and do
           the certificate and the inspection report check out against the order and the spec?
Who:       Receiving inspectors and supplier quality engineers at a fabricator, and the buyer
           who owns the purchase order.
Problem:   A delivery arrives with the supplier's delivery note, a mill certificate per heat
           and, where readings are still written on paper, a scanned receiving inspection
           form. Someone retypes the delivery lines, ticks them off against the PO, retypes the
           heat number, grade, strengths, impact energy and thickness readings, looks up the
           grade's limits, and decides by eye.
Input:     data/po_lines.csv (the PO lines as the ERP exports them: item, grade, size, ordered
           and already received quantity), data/receipts.jsonl (one delivery per line: when it
           arrived and which scanned page holds its delivery note and each delivered line's
           certificate and inspection report) and three scanned PDFs. The limits, the
           inspection plan, tolerances and thresholds are in rules.yaml.
SIE pipeline:
           extract  lightonai/LightOnOCR-3-4B  read each scanned page as Markdown (the delivery
                                               note's line table, the certificate's boxes and
                                               the measurement table come back as tables);
                                               called once per page
           (Python)                            read the delivery note's lines, match each to
                                               its PO line, and compare delivered plus already
                                               received with ordered
           extract  fastino/gliner2-large-v1   locate the certificate's heat number, grade,
                                               thickness, yield and tensile strength, impact
                                               energy and issue date, and the report's heat
                                               number, inspector and inspection date; called
                                               once per window of about 512 tokens
           (Python)                            take each labelled cell, read the measurement
                                               table against the inspection plan, check
                                               everything against the order and rules.yaml
Output:    po-lines.csv (one row per PO line on the deliveries, with its fill status),
           lots/<lot>.json per delivered lot (status, fields, measurements, every check with
           its status and detail), lots.csv and review-queue.csv (one row per failed or review
           check). Every status names what the checks found; the buyer and the quality engineer
           decide what happens next.
Why SIE:   Two models, no generative model makes a decision. The PO matching, quantities,
           limits, tolerances, guard bands, heat number match and qualified-inspector list are
           plain Python reading rules.yaml.
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

PASS, FAIL, REVIEW = "pass", "fail", "review"
# Lot status: what the document checks found. The quality engineer decides what happens next.
CHECKS_PASSED, NONCONFORMING = "checks_passed", "nonconforming"
# PO line fill status: what the quantities say. The buyer decides what happens next.
FILLED, PARTIALLY_FILLED, OVER_DELIVERED, NO_PO_LINE = (
    "filled",
    "partially_filled",
    "over_delivered",
    "no_po_line",
)
# When GLiNER2 finds nothing, SIE Cloud answers the output_schema call with HTTP 500 and this
# message. We record it as "no fields found"; a missing field can only send a check to review,
# never pass it.
EMPTY_RESULT_ERROR = "Inference completed without a valid conserved billing settlement"
LOT_FIELDS = ("lot", "delivery", "po_line", "po_line_matched", "fill_status", "status")
PO_FIELDS = (
    *("po_number", "line", "item", "ordered_qty", "previously_received"),
    *("delivered", "open_qty", "over_qty", "fill_status", "deliveries", "lots", "detail"),
)


@dataclass(frozen=True)
class PoLine:
    po_number: str
    line: int
    item: str
    grade: str
    thickness: Decimal
    width: Decimal
    length: Decimal
    ordered: int
    received: int  # on earlier deliveries


@dataclass
class Check:
    check: str
    status: str  # pass, fail or review
    detail: str


@dataclass
class LotResult:
    lot: str
    delivery: str
    received: date
    delivery_line: dict[str, str] = field(default_factory=dict)  # as read from the note
    po: PoLine | None = None
    fill_status: str = ""
    fields: dict[str, dict[str, str]] = field(default_factory=dict)  # kind -> field -> value
    measurements: list[dict[str, str]] = field(default_factory=list)
    table_problems: list[str] = field(default_factory=list)  # irregular measurement rows
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def claimed_po_line(self) -> str:
        """The PO line the delivery note names for this lot, matched or not; "" if not read."""
        line = self.delivery_line
        return f"{line['po_number']} line {line['po_line']}" if line else ""

    def with_status(self, status: str) -> list[Check]:
        return [check for check in self.checks if check.status == status]

    @property
    def flagged(self) -> list[Check]:
        return [check for check in self.checks if check.status != PASS]

    @property
    def status(self) -> str:
        if self.with_status(FAIL):
            return NONCONFORMING
        return REVIEW if self.with_status(REVIEW) else CHECKS_PASSED

    def add(self, check: str, status: str, detail: str) -> None:
        self.checks.append(Check(check, status, detail))

    def as_json(self) -> dict[str, Any]:
        return {
            "lot": self.lot,
            "delivery": self.delivery,
            "received": self.received.isoformat(),
            "delivery_line": self.delivery_line,
            "po_line": self.claimed_po_line,
            "po_line_matched": self.po is not None,
            "fill_status": self.fill_status,
            "status": self.status,
            "fail_checks": [check.check for check in self.with_status(FAIL)],
            "review_checks": [check.check for check in self.with_status(REVIEW)],
            "fields": self.fields,
            "measurements": self.measurements,
            "checks": [vars(check) for check in self.checks],
            "notes": self.notes,
        }


@dataclass
class PoLineFill:
    """One row of po-lines.csv: a PO line on this run's deliveries, or a delivery line that
    matched none."""

    po_number: str
    line: str
    item: str = ""
    ordered: int | None = None
    previously_received: int | None = None
    delivered: int = 0
    open_qty: int | None = None
    fill_status: str = ""
    deliveries: list[str] = field(default_factory=list)
    lots: list[str] = field(default_factory=list)
    detail: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)  # delivery note problems: fill goes to review

    def as_row(self) -> dict[str, Any]:
        return {
            "po_number": self.po_number,
            "line": self.line,
            "item": self.item,
            "ordered_qty": "" if self.ordered is None else self.ordered,
            "previously_received": (
                "" if self.previously_received is None else self.previously_received
            ),
            "delivered": self.delivered,
            "open_qty": "" if self.open_qty is None else max(self.open_qty, 0),
            "over_qty": "" if self.open_qty is None else max(-self.open_qty, 0),
            "fill_status": self.fill_status,
            "deliveries": ";".join(dict.fromkeys(self.deliveries)),
            "lots": ";".join(self.lots) or "-",
            "detail": "; ".join(self.detail),
        }


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def load_receipts(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def po_key(po_number: str, line: str | int) -> tuple[str, str]:
    """PO number and line compared without spaces or case: ("PO-48213", "2")."""
    return re.sub(r"\s+", "", po_number).upper(), str(line).strip()


def load_po_lines(path: Path) -> dict[tuple[str, str], PoLine]:
    with path.open(encoding="utf-8", newline="") as handle:
        lines = [
            PoLine(
                row["po_number"],
                int(row["line"]),
                row["item"],
                row["grade"],
                Decimal(row["thickness_mm"]),
                Decimal(row["width_mm"]),
                Decimal(row["length_mm"]),
                int(row["ordered_qty"]),
                int(row["received_qty"]),
            )
            for row in csv.DictReader(handle)
        ]
    return {po_key(line.po_number, line.line): line for line in lines}


def output_schema(rules: dict[str, Any], kind: str) -> dict[str, Any]:
    """The GLiNER2 output_schema for one document kind: one optional string per field."""
    properties = {
        name: {"type": "string", "description": spec["description"]}
        for name, spec in rules["documents"][kind].items()
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
    """One SIE call failed for a reason that may pass; the lot it was for goes to review."""


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
        reply without `key`, is recorded and raised as CallFailed, so the caller sends its lot
        to review and the run goes on. With `empty_result_ok` (the GLiNER2 calls), SIE Cloud's
        empty-result HTTP 500 is a reply with nothing in it, not a failure. An error that isn't
        an SIE error is a bug in this code and is raised as is.
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
        print(f"{n} of {total} SIE calls failed; their lots go to review. First: {short(first)}")


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
    those lots go to review."""
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
    """The error for a per-lot reason or the summary line, its message cut short."""
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


# --- Read pages (LightOnOCR) ------------------------------------------------------------------


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


def read_page(
    client: SIEClient, calls: Calls, pdf: Path, page: int, rules: dict[str, Any]
) -> str | None:
    """OCR one page of a scanned PDF (1-based page number): its Markdown, or None when the
    reply carried no markdown entity. Raises CallFailed on a transient SIE error."""
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
    return _markdown_entity(response)


# --- Flatten the OCR Markdown (plain Python) --------------------------------------------------


def flatten_markdown(text: str) -> str:
    """OCR Markdown to plain lines, one per table row, with cells joined by " | ".

    LightOnOCR writes a form box as a table cell holding the label, a <br> and the value, so
    "Heat number<br>A41873" becomes "Heat number | A41873": each label sits on its value's
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

    text = re.sub(r"<sup>(.*?)</sup>", r"\1", text)  # "N/mm<sup>2</sup>" reads "N/mm2"
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

    "Yield strength ReH | 372 MPa" with the span "372" gives "372 MPa", and "Impact energy KV
    at -20 °C | 64 J" with "64" gives "64 J": up to 20 characters of qualifier may sit between
    the label and its " | " or ":". Cells on one line are split at " | ". Labels match whole
    words, so "inspector" does not match "inspection".
    """
    span = _squash(value)
    for line in window.splitlines():
        for label in labels:
            match = _label_at(line, label)
            if match is None:
                continue
            rest = line[match.end() :]
            if re.match(r"^[^:|]{0,20}[:|]", rest):
                rest = re.sub(r"^[^:|]*?[:|]", "", rest, count=1)
            cell = rest.split(" | ")[0].strip(" :|")
            if span and span in _squash(cell):
                return cell
    return None


def table_rows(flat: str, columns: dict[str, list[str]]) -> tuple[list[dict[str, str]], list[str]]:
    """Rows of the table whose header row names every column in `columns`, and the rows that
    could not be read.

    Every line under the header with the header's number of cells is one row. A line with a
    different number of cells (two or more) is a row the OCR split or merged: it is returned
    as a problem, not read and not skipped silently. The first line with a single cell (the
    text after the table) ends it.
    """
    rows: list[dict[str, str]] = []
    irregular: list[str] = []
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
        if len(cells) == 1:
            header = None  # the table ended
            continue
        if len(cells) != width:
            irregular.append(f"row '{line}' has {len(cells)} cells, the table has {width}")
            continue
        rows.append({name: cells[i] for name, i in header.items()})
    return rows, irregular


# --- Extract fields (GLiNER2) -----------------------------------------------------------------


def extract_fields(
    client: SIEClient,
    calls: Calls,
    key: str,
    kind: str,
    text: str,
    rules: dict[str, Any],
    result: LotResult,
) -> dict[str, str]:
    """Find one document's fields with GLiNER2 and keep the labelled cell for each span.

    A span that is not in the window's text, or not on a line carrying the field's label, is
    dropped with a note, so its check goes to review instead of using a value nobody wrote. A
    field read with two different values (in two windows, or twice in one) goes to review as
    `conflicting_values` and is left out, so the check that needs it says it was not read. A
    window whose SIE call failed sends the lot to review as `readable`.
    """
    model, limits = rules["models"]["extract"], rules["thresholds"]
    specs = rules["documents"][kind]
    schema = output_schema(rules, kind)
    found: dict[str, str] = {}
    conflicts: dict[str, list[str]] = {}
    parts = windows(flatten_markdown(text), limits["window_words"], limits["window_overlap_words"])
    for index, window in enumerate(parts, start=1):
        item = Item(id=f"extract/{key}-w{index}", text=window)
        # One item per call: the empty-result HTTP 500 would fail a whole batch.
        try:
            work = functools.partial(
                client.extract, model, item, output_schema=schema, provision_timeout_s=WAIT_S
            )
            response = calls.call(model, work, "data", empty_result_ok=True)
        except CallFailed as failure:
            result.add("readable", REVIEW, f"{kind} window {index} was not read: {failure}")
            continue
        if response.get("empty_result_error"):
            result.notes.append(f"{kind} window {index}: no fields found (HTTP 500 empty result)")
        for name, value in (response.get("data") or {}).items():
            spec = specs.get(name)
            if spec is None:
                continue
            if not isinstance(value, str) or not re.search(r"[^\W_]", value):
                continue  # empty or a form blank ("______")
            # A cell never runs over a line break in the flattened text; GLiNER2 sometimes
            # carries a span into the next line's label ("S235JR\nStandard").
            value = value.strip().splitlines()[0].strip(" |")
            if _squash(value) not in _squash(window):
                result.notes.append(f"{kind}.{name}: '{value}' is not in the text; dropped")
                continue
            cell = labelled_cell(value, window, spec["label"])
            if cell is None:
                result.notes.append(f"{kind}.{name}: '{value}' is not on a labelled line; dropped")
                continue
            if name in found and _squash(found[name]) != _squash(cell):
                conflicts.setdefault(name, [found[name]]).append(cell)
            found.setdefault(name, cell)
    for name, values in conflicts.items():
        found.pop(name, None)
        shown = " / ".join(f"'{value}'" for value in dict.fromkeys(values))
        result.add("conflicting_values", REVIEW, f"{kind} {name} read as {shown}")
    return found


def impact_temperature(flat: str) -> int | None:
    """The Charpy test temperature on the impact energy line ("KV at -20 °C" -> -20)."""
    for line in flat.splitlines():
        if _label_at(line, "impact energy") is None:
            continue
        match = re.search(r"([+\-\N{MINUS SIGN}]?)\s*(\d+)\s*°?\s*C\b", line)
        if match:
            sign = -1 if match.group(1) in ("-", "\N{MINUS SIGN}") else 1
            return sign * int(match.group(2))
    return None


# --- Delivery note: lines, PO match and fill status (plain Python) ----------------------------


def parse_number(value: str) -> Decimal | None:
    """'12.00 mm' -> Decimal('12.00'); None when the text is not one number."""
    match = re.match(r"\s*(-?\d+(?:\.\d+)?)\s*(?:mm)?\s*$", value.casefold())
    if match is None:
        return None
    try:
        return Decimal(match.group(1))
    except InvalidOperation:
        return None


def parse_size(value: str) -> tuple[Decimal, Decimal, Decimal] | None:
    """'12x2000x6000' or '12 x 2000 x 6000 mm' -> thickness, width, length; None otherwise."""
    parts = re.split(
        r"\s*[x\N{MULTIPLICATION SIGN}*]\s*", value.casefold().replace("mm", "").strip()
    )
    numbers = [parse_number(part) for part in parts]
    if len(numbers) != 3 or any(number is None for number in numbers):
        return None
    thickness, width, length = cast(list[Decimal], numbers)
    return thickness, width, length


def parse_quantity(value: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+)\s*(?:pcs|plates?)?\s*", value.casefold())
    return int(match.group(1)) if match else None


def match_po_line(
    line: dict[str, str], po_lines: dict[tuple[str, str], PoLine]
) -> tuple[PoLine | None, str]:
    """The PO line a delivery note line names, if its item, grade and size match the order.

    Returns the PO line and "" on a match, or None and the reason it matched no PO line.
    """
    named = f"{line['po_number']} line {line['po_line']}"
    po = po_lines.get(po_key(line["po_number"], line["po_line"]))
    if po is None:
        return None, f"{named} is not on the order"
    wrong = []
    if line["item"].strip() != po.item:
        wrong.append(f"item {line['item']} (ordered {po.item})")
    if grade_key(line["grade"]) != grade_key(po.grade):
        wrong.append(f"grade {line['grade']} (ordered {po.grade})")
    if parse_size(line["size"]) != (po.thickness, po.width, po.length):
        ordered = f"{po.thickness}x{po.width}x{po.length}"
        wrong.append(f"size {line['size']} (ordered {ordered})")
    if wrong:
        return None, f"{named} on the delivery note says " + ", ".join(wrong)
    return po, ""


def note_total(flat: str, label: str) -> int | None:
    """The delivery note's own total ("Total plates: 20" -> 20); None if it isn't there."""
    match = re.search(rf"(?<![a-z]){re.escape(label)}\W*(\d+)", flat.casefold())
    return int(match.group(1)) if match else None


def delivery_problems(
    lines: list[dict[str, str]], irregular: list[str], total: int | None, expected: int
) -> list[str]:
    """Why the delivery note's line table can't be trusted as read; [] when it adds up.

    The rows read must match the number of delivery lines the scan batch has, every row must
    have the table's cells, and the line quantities must add up to the note's own total. A
    misread digit, a dropped row or a row read twice fails at least one of these.
    """
    problems = list(irregular)
    if len(lines) != expected:
        problems.append(f"{len(lines)} lines read, the scan batch has {expected}")
    quantities = [parse_quantity(line["quantity"]) for line in lines]
    if total is None:
        problems.append("no total read on the note")
    elif None not in quantities:
        added = sum(cast(list[int], quantities))
        if added != total:
            problems.append(f"line quantities add up to {added}, the note's total says {total}")
    return problems


def fill_status(ordered: int, received: int, allowance: int) -> tuple[str, int]:
    """A PO line's fill status once `received` plates are in (earlier deliveries plus this
    run's), and its open quantity (negative: plates over the order)."""
    open_qty = ordered - received
    if open_qty > 0:
        return PARTIALLY_FILLED, open_qty
    if -open_qty > allowance:
        return OVER_DELIVERED, open_qty
    return FILLED, open_qty


# --- Checks (plain Python) --------------------------------------------------------------------


def parse_date(value: str, formats: list[str]) -> date | None:
    cleaned = re.sub(r"\s+", " ", value).strip(" .,")
    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def parse_strength(value: str, rules: dict[str, Any]) -> Decimal | None:
    """'355 N/mm2' or '372 MPa' -> MPa; None when the number or the unit is not recognised."""
    compact = re.sub(r"\s+", "", value.casefold()).replace("²", "2")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(.*)", compact)
    if match is None:
        return None
    factor = rules["strength_units"].get(match.group(2))
    if factor is None:
        return None
    if factor == 1:
        return Decimal(match.group(1))
    return (Decimal(match.group(1)) * Decimal(str(factor))).quantize(Decimal("0.1"))


def parse_energy(value: str) -> Decimal | None:
    """'64 J' -> Decimal('64'); None when the text is not a number in joules."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*j\s*", value.casefold())
    return Decimal(match.group(1)) if match else None


def heat_key(value: str, rules: dict[str, Any]) -> str:
    separators = re.escape(rules["heat_number_separators"])
    return re.sub(f"[{separators}]", "", value).upper()


def grade_key(value: str) -> str:
    return re.sub(r"\s+", "", value).upper()


def _band(bands: list[dict[str, Any]], thickness: Decimal, top: str) -> dict[str, Any] | None:
    """Pick the first band with over_mm < thickness <= `top`, or None if no band covers it."""
    for band in bands:
        if thickness <= Decimal(str(band[top])) and thickness > Decimal(
            str(band.get("over_mm", 0))
        ):
            return band
    return None


def _unread(result: LotResult, check: str, values: dict[str, str | None]) -> bool:
    """Send `check` to review when a value it needs was not read. True if one was missing."""
    missing = [label for label, value in values.items() if not value]
    if missing:
        result.add(check, REVIEW, "Not read: " + ", ".join(missing))
    return bool(missing)


def check_heat(
    cert: dict[str, str],
    report: dict[str, str],
    delivered_heat: str | None,
    rules: dict[str, Any],
    result: LotResult,
) -> None:
    cert_heat, report_heat = cert.get("heat_number"), report.get("heat_number")
    if _unread(
        result,
        "heat_number",
        {
            "delivery note heat number": delivered_heat,
            "certificate heat number": cert_heat,
            "report heat number": report_heat,
        },
    ):
        return
    expected = heat_key(str(delivered_heat), rules)
    wrong = [
        f"{where} says '{value}'"
        for where, value in (("certificate", cert_heat), ("report", report_heat))
        if heat_key(str(value), rules) != expected
    ]
    if wrong:
        detail = f"Delivery note says {delivered_heat}; " + "; ".join(wrong)
        result.add("heat_number", REVIEW, detail)
    else:
        result.add(
            "heat_number", PASS, f"{delivered_heat} on the delivery note, certificate, report"
        )


def check_strengths(
    cert: dict[str, str], po: PoLine, rules: dict[str, Any], result: LotResult
) -> None:
    spec = rules["grades"].get(po.grade)
    if spec is None:
        for name in ("yield_strength", "tensile_strength", "impact_energy"):
            result.add(name, REVIEW, f"No limits in rules.yaml for ordered grade {po.grade}")
        return
    written = cert.get("yield_strength")
    yield_value: Decimal | None = None
    if not _unread(result, "yield_strength", {"yield strength": written}):
        value = yield_value = parse_strength(str(written), rules)
        band = _band(spec["yield_min_mpa"], po.thickness, "up_to_mm")
        if value is None:
            result.add("yield_strength", REVIEW, f"'{written}': number or unit not recognised")
        elif band is None:
            result.add("yield_strength", REVIEW, f"No yield band for {po.thickness} mm")
        else:
            minimum = Decimal(str(band["min"]))
            status = PASS if value >= minimum else FAIL
            limit = f"min {minimum} MPa for {po.grade} at {po.thickness} mm"
            result.add("yield_strength", status, f"ReH {value} MPa ('{written}'); {limit}")
    written = cert.get("tensile_strength")
    tensile_value: Decimal | None = None
    if not _unread(result, "tensile_strength", {"tensile strength": written}):
        value = tensile_value = parse_strength(str(written), rules)
        low, high = (Decimal(str(spec["tensile_mpa"][k])) for k in ("min", "max"))
        if value is None:
            result.add("tensile_strength", REVIEW, f"'{written}': number or unit not recognised")
        else:
            status = PASS if low <= value <= high else FAIL
            limit = f"{low} to {high} MPa for {po.grade}"
            result.add("tensile_strength", status, f"Rm {value} MPa ('{written}'); {limit}")
    if yield_value is not None and tensile_value is not None and yield_value > tensile_value:
        detail = f"ReH {yield_value} MPa is above Rm {tensile_value} MPa: a misread or a swap"
        result.add("strength_order", REVIEW, detail)
    check_impact(cert, po, spec["impact"], result)


def check_impact(cert: dict[str, str], po: PoLine, spec: dict[str, Any], result: LotResult) -> None:
    """Charpy energy at or above the grade minimum, tested at or below the grade's temperature."""
    written, temperature = cert.get("impact_energy"), cert.get("impact_test_temp_c")
    needed = {"impact energy": written, "impact test temperature": temperature}
    if _unread(result, "impact_energy", needed):
        return
    energy = parse_energy(str(written))
    minimum, required = Decimal(str(spec["min_j"])), int(spec["test_temp_c"])
    limit = f"min {minimum} J at {required} C or colder for {po.grade}"
    if energy is None:
        result.add("impact_energy", REVIEW, f"'{written}': not a value in joules")
    elif int(str(temperature)) > required:
        detail = f"KV {energy} J tested at {temperature} C; {limit}"
        result.add("impact_energy", REVIEW, detail)
    else:
        status = PASS if energy >= minimum else FAIL
        result.add("impact_energy", status, f"KV {energy} J at {temperature} C; {limit}")


def judge_measurement(
    measured: Decimal, nominal: Decimal, minus: Decimal, plus: Decimal, guard: Decimal
) -> tuple[str, str]:
    """Judge one reading as pass, fail or review, and return the limits used.

    A reading inside the limits but within `guard` of one goes to review, because the gauge
    cannot tell it apart from a reading just outside.
    """
    low, high = nominal - minus, nominal + plus
    limits = f"{low} to {high}"
    if measured < low or measured > high:
        return FAIL, limits
    if measured - low <= guard or high - measured <= guard:
        return REVIEW, limits
    return PASS, limits


def check_dimensions(
    rows: list[dict[str, str]], po: PoLine, rules: dict[str, Any], result: LotResult
) -> None:
    """Every reading on the inspection plan, there exactly once, inside tolerance.

    A planned reading that is missing, or written twice with different values, goes to review;
    so does a row that is not on the plan.
    """
    tolerances: dict[str, Any] = rules["dimensions"]
    nominals = {"thickness": po.thickness, "width": po.width, "length": po.length}
    plan: list[str] = rules["inspection_plan"]
    planned = {_squash(name) for name in plan}
    problems: dict[str, list[str]] = {FAIL: [], REVIEW: list(result.table_problems)}
    by_name: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        name = _squash(row["characteristic"])
        if name not in planned:
            problems[REVIEW].append(f"'{row['characteristic']}' is not on the inspection plan")
            continue
        by_name.setdefault(name, []).append(row)
    judged = 0
    for name in plan:
        found = by_name.get(_squash(name), [])
        values = list(dict.fromkeys(row["measured"].strip() for row in found))
        if not found:
            problems[REVIEW].append(f"{name} not read")
            continue
        if len(values) > 1:
            problems[REVIEW].append(f"{name} read twice: " + " / ".join(values))
            continue
        kind = name.split()[0].casefold()
        measured = parse_number(values[0])
        if kind not in tolerances or kind not in nominals or measured is None:
            problems[REVIEW].append(f"{name} = '{values[0]}' has no tolerance or is not a number")
            continue
        spec, nominal = tolerances[kind], nominals[kind]
        band = _band(spec["bands"], nominal, "up_to_mm") if "bands" in spec else spec
        if band is None:
            problems[REVIEW].append(f"No {kind} tolerance band for {nominal} mm")
            continue
        status, limits = judge_measurement(
            measured,
            nominal,
            Decimal(str(band["minus"])),
            Decimal(str(band["plus"])),
            Decimal(str(spec["guard_mm"])),
        )
        judged += 1
        if status == FAIL:
            problems[FAIL].append(f"{name} {measured} outside {limits}")
        elif status == REVIEW:
            guard = spec["guard_mm"]
            problems[REVIEW].append(f"{name} {measured} within {guard} mm of a limit ({limits})")
    if problems[FAIL]:
        result.add("dimensions", FAIL, "; ".join(problems[FAIL] + problems[REVIEW]))
    elif problems[REVIEW]:
        result.add("dimensions", REVIEW, "; ".join(problems[REVIEW]))
    else:
        result.add("dimensions", PASS, f"{judged} planned readings inside tolerance")


def check_lot(
    docs: dict[str, dict[str, str]],
    rows: list[dict[str, str]],
    rules: dict[str, Any],
    result: LotResult,
) -> None:
    """Run every document check for one lot. A fail makes it nonconforming; a review goes to
    quality. The checks that need the order run only when the lot's line matched a PO line."""
    cert, report = docs.get("certificate", {}), docs.get("inspection_report", {})
    po = result.po
    check_heat(cert, report, result.delivery_line.get("heat_number"), rules, result)
    if po is not None:
        grade = cert.get("grade")
        if not _unread(result, "grade", {"certificate grade": grade}):
            ok = grade_key(str(grade)) == grade_key(po.grade)
            result.add(
                "grade", PASS if ok else REVIEW, f"Certificate '{grade}', ordered {po.grade}"
            )
        thickness = cert.get("thickness")
        if not _unread(result, "thickness", {"certificate thickness": thickness}):
            ok = parse_number(str(thickness)) == po.thickness
            detail = f"Certificate '{thickness}', ordered {po.thickness} mm"
            result.add("thickness", PASS if ok else REVIEW, detail)
        check_strengths(cert, po, rules, result)
        check_dimensions(rows, po, rules, result)

    inspector = report.get("inspector")
    if not _unread(result, "inspector", {"inspector": inspector}):
        qualified = {grade_key(name) for name in rules["qualified_inspectors"]}
        ok = grade_key(str(inspector)) in qualified
        detail = f"'{inspector}' " + ("is qualified" if ok else "is not on the qualified list")
        result.add("inspector", PASS if ok else REVIEW, detail)

    issued, inspected = cert.get("issue_date"), report.get("inspection_date")
    if not _unread(result, "dates", {"certificate date": issued, "inspection date": inspected}):
        formats = rules["date_formats"]
        issued_on, inspected_on = (
            parse_date(str(issued), formats),
            parse_date(str(inspected), formats),
        )
        received = result.received.isoformat()
        if issued_on is None or inspected_on is None:
            result.add("dates", REVIEW, f"Not a date: '{issued}' / '{inspected}'")
        elif issued_on > result.received or inspected_on < result.received:
            detail = f"Certificate {issued_on}, received {received}, inspected {inspected_on}"
            result.add("dates", REVIEW, detail)
        else:
            detail = f"Certificate {issued_on} <= received {received} <= inspected {inspected_on}"
            result.add("dates", PASS, detail)


# --- Run --------------------------------------------------------------------------------------


def read_documents(
    client: SIEClient,
    calls: Calls,
    lot: dict[str, Any],
    rules: dict[str, Any],
    data_dir: Path,
    result: LotResult,
) -> None:
    """OCR the lot's certificate and report, extract their fields, read the measurements."""
    for kind in ("certificate", "inspection_report"):
        source = lot[kind]
        where = f"{kind} ({source['file']} p{source['page']})"
        try:
            text = read_page(client, calls, data_dir / source["file"], source["page"], rules)
        except CallFailed as failure:
            result.add("readable", REVIEW, f"{where} was not read: {failure}")
            result.fields[kind] = {}
            continue
        if text is None:
            result.add("readable", REVIEW, f"{where}: the OCR reply had no markdown text")
            result.fields[kind] = {}
            continue
        if len(text.strip()) < rules["thresholds"]["min_page_chars"]:
            result.add("readable", REVIEW, f"{where} could not be read")
        flat = flatten_markdown(text)
        fields = extract_fields(client, calls, f"{result.lot}/{kind}", kind, text, rules, result)
        if kind == "certificate":
            temperature = impact_temperature(flat)
            if temperature is not None and "impact_energy" in fields:
                fields["impact_test_temp_c"] = str(temperature)
        else:
            result.measurements, result.table_problems = table_rows(
                flat, rules["measurement_columns"]
            )
        result.fields[kind] = fields


def process_delivery(
    client: SIEClient,
    calls: Calls,
    receipt: dict[str, Any],
    po_lines: dict[tuple[str, str], PoLine],
    rules: dict[str, Any],
    data_dir: Path,
    fills: dict[tuple[str, str], PoLineFill],
) -> list[LotResult]:
    """Read one delivery: its note's lines, their PO lines, and each delivered lot's documents.

    Adds each delivered quantity to `fills`; the fill status is set once every delivery is in.
    """
    note, delivery = receipt["delivery_note"], receipt["delivery"]
    received = date.fromisoformat(receipt["received"])
    results = [LotResult(lot["lot"], delivery, received) for lot in receipt["lots"]]
    where = f"delivery note {delivery} ({note['file']} p{note['page']})"
    lines: list[dict[str, str]] = []
    problems: list[str] = []
    try:
        text = read_page(client, calls, data_dir / note["file"], note["page"], rules)
    except CallFailed as failure:
        unread = f"{where} was not read: {failure}"
    else:
        if text is None:
            unread = f"{where}: the OCR reply had no markdown text"
        else:
            flat = flatten_markdown(text)
            lines, irregular = table_rows(flat, rules["delivery_columns"])
            total = note_total(flat, rules["delivery_total_label"])
            problems = delivery_problems(lines, irregular, total, len(receipt["lots"]))
            unread = "; ".join([f"{where}: no delivery line read", *problems]) if not lines else ""
    if unread:
        row = fills.setdefault(("", delivery), PoLineFill("", "", fill_status=REVIEW))
        row.deliveries.append(delivery)
        row.detail.append(unread)
    by_number = {
        lot["delivery_line"]: result for lot, result in zip(receipt["lots"], results, strict=True)
    }
    for number, line in enumerate(lines, start=1):
        result = by_number.get(number)
        name = result.lot if result else None
        po, reason = match_po_line(line, po_lines)
        quantity = parse_quantity(line["quantity"])
        if po is None:
            key = (f"{delivery}/{number}", "")
            row = PoLineFill(line["po_number"], line["po_line"], line["item"])
            row.fill_status, row.detail = NO_PO_LINE, [reason]
            row.delivered = quantity or 0
            fills[key] = row
        else:
            key = po_key(po.po_number, po.line)
            row = fills.setdefault(
                key,
                PoLineFill(po.po_number, str(po.line), po.item, po.ordered, po.received),
            )
            if quantity is None:
                row.fill_status = REVIEW
                row.detail.append(f"{delivery} line {number}: quantity '{line['quantity']}'")
            else:
                row.delivered += quantity
        row.deliveries.append(delivery)
        row.held += [f"{delivery}: {problem}" for problem in problems]
        if name is None:
            row.detail.append(f"{delivery} line {number}: no certificate or report scanned")
            continue
        row.lots.append(name)
        assert result is not None
        result.delivery_line, result.po = line, po
        if po is None or problems:
            reasons = ([reason] if po is None else []) + problems
            result.add("po_line", REVIEW, f"{delivery}: " + "; ".join(reasons))
        else:
            detail = f"{delivery} line {number}: item, grade and size match {key[0]} line {key[1]}"
            result.add("po_line", PASS, detail)
    for number, result in by_number.items():
        if not result.delivery_line:
            reason = unread or f"{where}: line {number} not read"
            result.add("po_line", REVIEW, reason)
    for lot, result in zip(receipt["lots"], results, strict=True):
        read_documents(client, calls, lot, rules, data_dir, result)
    return results


def settle_fills(fills: dict[tuple[str, str], PoLineFill], allowance: int) -> None:
    """Set each matched PO line's fill status from everything delivered on this run.

    A PO line on a delivery note whose line table didn't add up goes to review with the
    reasons; the status the quantities as read would give is kept in its detail.
    """
    for row in fills.values():
        ordered, before = row.ordered, row.previously_received
        if not row.fill_status and ordered is not None and before is not None:
            row.fill_status, row.open_qty = fill_status(ordered, before + row.delivered, allowance)
            if row.fill_status == OVER_DELIVERED:
                row.detail.append(f"{plates(-row.open_qty)} over the order")
            elif row.fill_status == PARTIALLY_FILLED:
                row.detail.append(f"{plates(row.open_qty)} still open")
        if row.held:
            as_read = f"as read: {row.fill_status}"
            row.detail = [*dict.fromkeys(row.held), as_read, *row.detail]
            row.fill_status = REVIEW


def plates(count: int) -> str:
    return f"{count} plate" + ("" if count == 1 else "s")


def run(
    client: SIEClient, rules: dict[str, Any], data_dir: Path = DATA_DIR
) -> tuple[list[LotResult], list[PoLineFill], Calls]:
    receipts = load_receipts(data_dir / "receipts.jsonl")
    po_lines = load_po_lines(data_dir / "po_lines.csv")
    calls = Calls()
    fills: dict[tuple[str, str], PoLineFill] = {}
    results: list[LotResult] = []
    for receipt in receipts:
        results += process_delivery(client, calls, receipt, po_lines, rules, data_dir, fills)
    calls.check_all_failed()
    settle_fills(fills, int(rules["thresholds"]["over_delivery_allowance"]))
    by_lot = {lot: row.fill_status for row in fills.values() for lot in row.lots}
    for result in results:
        result.fill_status = by_lot.get(result.lot, REVIEW)
        check_lot(result.fields, result.measurements, rules, result)
    return results, list(fills.values()), calls


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


def write_outputs(output_dir: Path, results: list[LotResult], fills: list[PoLineFill]) -> None:
    (output_dir / "lots").mkdir(parents=True, exist_ok=True)
    for result in results:
        path = output_dir / "lots" / f"{result.lot}.json"
        text = json.dumps(result.as_json(), indent=2, ensure_ascii=False) + "\n"
        path.write_text(text, encoding="utf-8")
    _write_csv(output_dir / "po-lines.csv", PO_FIELDS, [row.as_row() for row in fills])
    lots = [
        {
            "lot": result.lot,
            "delivery": result.delivery,
            "po_line": result.claimed_po_line,
            "po_line_matched": "yes" if result.po else "no",
            "fill_status": result.fill_status,
            "status": result.status,
            "fail_checks": ";".join(check.check for check in result.with_status(FAIL)),
            "review_checks": ";".join(check.check for check in result.with_status(REVIEW)),
        }
        for result in results
    ]
    _write_csv(output_dir / "lots.csv", (*LOT_FIELDS, "fail_checks", "review_checks"), lots)
    queue = [
        {"lot": result.lot, "check": check.check, "status": check.status, "detail": check.detail}
        for result in results
        for check in result.flagged
    ]
    _write_csv(output_dir / "review-queue.csv", ("lot", "check", "status", "detail"), queue)


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    started = time.monotonic()
    with make_client() as client:
        results, fills, calls = stop_on_refusal(lambda: run(client, load_rules()))
    elapsed = time.monotonic() - started
    write_outputs(output, results, fills)
    for row in fills:
        named = f"{row.po_number} line {row.line}" if row.po_number else "(no PO line read)"
        print(f"{named:<20} {row.fill_status:<17} {'; '.join(row.detail) or '-'}")
    print()
    for result in results:
        reasons = "; ".join(f"{check.check}: {check.detail}" for check in result.flagged)
        print(f"{result.lot:<7} {result.fill_status:<17} {result.status:<14} {reasons or '-'}")
    per_model = ", ".join(f"{count} to {model}" for model, count in calls.count.items())
    print(f"\n{calls.total} SIE calls ({per_model}) in {elapsed:.0f} s")
    print("Statuses name what the checks found; the buyer and the quality engineer decide.")
    print(f"PO lines: {output / 'po-lines.csv'}\nreview queue: {output / 'review-queue.csv'}")


if __name__ == "__main__":
    main()
