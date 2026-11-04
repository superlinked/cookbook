"""Turn a faxed referral packet into a draft referral, a callback list and a review queue.

Use case:  New-patient referrals that arrive by fax at a specialty practice.
Who:       The referral coordinator working the e-fax queue, and the clinician who decides how
           soon each referred patient is seen.
Problem:   A packet (cover sheet, referral order, clinic note) arrives as an image-only fax. The
           coordinator reads it, searches the EHR for the patient, enters the referral and
           chases what the practice's checklist says is missing; a clinician reads every
           referral to decide how soon the patient is seen.
Input:     Fax packets as image-only PDFs in data/, the EHR's charts (ehr.py, over
           data/ehr_patients.csv) and the provider directory (provider_directory.py, over
           data/provider_directory.csv). The intake checklist, labels and thresholds are in
           rules.yaml.
SIE pipeline:
           extract  lightonai/LightOnOCR-3-4B  read each fax page as Markdown; called once per
                                               page
           extract  fastino/gliner2-large-v1   point at the patient, insurance, referring
                                               provider, office numbers, reason, requested
                                               priority and order signer; called once per
                                               window of about 512 tokens
           (Python)                            check every value against its page, its label
                                               and its format, look the patient up in the EHR
                                               and the provider in the directory, read the
                                               requested priority from the wording, list what's
                                               missing against the checklist
Output:    drafts/<packet>.json per packet (status, every field with where it came from, the
           chart and directory match, the requested priority, missing and review items),
           callback-list.csv (each missing item with the referring office's phone and fax) and
           clinical-review-queue.csv (every packet, requested-urgent and time-bound first).
           Every status names what the checks found; the coordinator and the clinical reviewer
           decide what happens next.
Why SIE:   Two models, and no generative model makes the decision. The checks, the EHR and
           directory matching, the priority wording and the checklist are plain Python reading
           rules.yaml.
Models on: SIE Serverless, or your own SIE with SIE_BASE_URL.

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
from pathlib import Path
from typing import Any, cast

import pypdfium2 as pdfium
import yaml
from dotenv import load_dotenv
from ehr import EHR, PatientMatch
from provider_directory import ProviderDirectory, ProviderMatch
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

# Packet status: what the checks found. The coordinator and the clinical reviewer decide next.
CHECKS_PASSED, MISSING_INFO, REVIEW = "checks_passed", "missing_info", "review"
# When GLiNER2 finds nothing, SIE Serverless answers the output_schema call with HTTP 500 and this
# message. We record it as "no fields found": a field nobody read can only make a packet
# missing_info or review, never pass it, and every page after the cover must name the patient.
EMPTY_RESULT_ERROR = "Inference completed without a valid conserved billing settlement"
PHONE_FIELDS = ("patient_phone", "office_phone", "office_fax")
CALLBACK_FIELDS = (
    *("requested_priority", "packet", "patient", "referring_provider", "practice"),
    *("office_phone", "office_fax", "contact_from", "item"),
)
QUEUE_FIELDS = (
    *("order", "packet", "patient", "dob", "requested_priority", "as_written", "page"),
    *("priority_note", "intake_status", "referral_reason", "referring_provider"),
    "office_phone",
)


@dataclass(frozen=True)
class Candidate:
    """One value the extractor returned for one field in one window of one page."""

    field: str
    value: str
    page: int
    window: str


@dataclass
class Referral:
    """One packet: what was read, where each value came from, and what the checks found."""

    packet: str
    page_kinds: list[str] = field(default_factory=list)
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    chart: dict[str, Any] = field(default_factory=dict)
    provider: dict[str, Any] = field(default_factory=dict)
    priority: dict[str, Any] = field(
        default_factory=lambda: (
            {"kind": "not_stated", "as_written": "", "line": "", "page": None} | {"note": ""}
        )
    )
    missing: list[dict[str, str]] = field(default_factory=list)
    review: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    flagged: set[str] = field(default_factory=set)  # fields a review reason is about

    def flag(self, reason: str, *names: str) -> None:
        self.review.append(reason)
        self.flagged.update(names)

    @property
    def status(self) -> str:
        # Review wins: there's no point calling about a member ID for the wrong patient.
        if self.review:
            return REVIEW
        if self.missing:
            return MISSING_INFO
        return CHECKS_PASSED

    def value(self, name: str) -> str:
        return str(self.fields.get(name, {}).get("value", ""))

    def as_json(self) -> dict[str, Any]:
        return {
            "packet": self.packet,
            "status": self.status,
            "review_items": self.review,
            "missing_items": self.missing,
            "requested_priority": self.priority,
            "pages": [{"page": n, "document": kind} for n, kind in enumerate(self.page_kinds, 1)],
            "fields": self.fields,
            "chart_match": self.chart,
            "provider_match": self.provider,
            "notes": self.notes,
        }


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def output_schema(rules: dict[str, Any]) -> dict[str, Any]:
    """The GLiNER2 output_schema: one optional string per field."""
    properties = {
        name: {"type": "string", "description": spec["description"]}
        for name, spec in rules["fields"].items()
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
    """One SIE call failed for a reason that may pass; the packet it was for goes to review."""


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
        packet to review and the run goes on. With `empty_result_ok` (the GLiNER2 calls), SIE
        Serverless's empty-result HTTP 500 is a reply with nothing in it, not a failure. An error
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
        print(f"{n} of {total} SIE calls failed; their packets go to review. First: {short(first)}")


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
    those packets go to review."""
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
    """The error for a per-packet reason or the summary line, its message cut short."""
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
        if isinstance(entity, dict) and entity.get("label") == "markdown":
            return str(entity.get("text", ""))
    return None


def read_pages(
    client: SIEClient, calls: Calls, pdf: Path, rules: dict[str, Any], referral: Referral
) -> list[str]:
    """OCR every page of a fax packet. Fax pages never have a text layer to read instead.

    A page whose call failed, or whose reply carried no markdown, comes back as "" with a
    review reason, so the packet can't pass on the pages that were read.
    """
    model, limits = rules["models"]["ocr"], rules["thresholds"]
    document = pdfium.PdfDocument(pdf)
    pages: list[str] = []
    for index in range(len(document)):
        number = index + 1
        image = _page_png(document[index], limits["render_dpi"], limits["max_image_edge_px"])
        item = Item(id=f"ocr/{pdf.stem}/p{number}", images=[image])
        try:
            work = functools.partial(client.extract, model, item, provision_timeout_s=WAIT_S)
            text = _markdown_entity(calls.call(model, work, "entities"))
        except CallFailed as failure:
            referral.review.append(f"Page {number} was not read: {failure}")
            pages.append("")
            continue
        if text is None:
            referral.review.append(f"Page {number}: the OCR reply had no markdown text")
            text = ""
        elif len(text.strip()) < limits["min_page_chars"]:
            referral.review.append(f"Page {number} could not be read (too little text)")
        pages.append(flatten_markdown(text))
    return pages


def flatten_markdown(text: str) -> str:
    """OCR Markdown to plain lines: headings, bold marks and bullets dropped, a table row on one
    line with its cells joined by " | ", HTML entities decoded."""

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


def page_kind(text: str, rules: dict[str, Any]) -> str:
    """The page's document type (cover, order, note) from its first lines, or "other"."""
    head = [line.casefold() for line in text.splitlines()[: rules["thresholds"]["title_lines"]]]
    for kind, terms in rules["documents"].items():
        if any(term in line for line in head for term in terms):
            return str(kind)
    return "other"


# --- Find fields (GLiNER2) --------------------------------------------------------------------


def windows(text: str, size: int, overlap: int) -> list[str]:
    """Split text into windows of at most `size` words, each overlapping the last by `overlap`.

    Line breaks are kept, so the label check can still see which line a value sits on.
    """
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


def extract_candidates(
    client: SIEClient,
    calls: Calls,
    packet: str,
    pages: list[str],
    rules: dict[str, Any],
    referral: Referral,
) -> list[Candidate]:
    """Every value GLiNER2 points at, per window of every page. A window whose call failed
    sends the packet to review."""
    model, limits = rules["models"]["extract"], rules["thresholds"]
    schema = output_schema(rules)
    candidates: list[Candidate] = []
    for page_number, text in enumerate(pages, start=1):
        parts = windows(text, limits["window_words"], limits["window_overlap_words"])
        for index, window in enumerate(parts, start=1):
            item = Item(id=f"extract/{packet}/p{page_number}-w{index}", text=window)
            # One item per call: the empty-result HTTP 500 would fail a whole batch. A window
            # too long to read whole fails (InputTooLongError, the transient path: the packet
            # goes to review) instead of being cut short.
            work = functools.partial(
                client.extract,
                model,
                item,
                output_schema=schema,
                options={"overflow_policy": "error"},
                provision_timeout_s=WAIT_S,
            )
            try:
                response = calls.call(model, work, "data", empty_result_ok=True)
            except CallFailed as failure:
                referral.review.append(f"Page {page_number} window {index} was not read: {failure}")
                continue
            if response.get("empty_result_error"):
                referral.notes.append(
                    f"Page {page_number} window {index}: no fields found (HTTP 500 empty result)"
                )
            data = response.get("data")
            if not isinstance(data, dict):
                reason = f"Page {page_number} window {index} was not read: 'data' is not an object"
                referral.review.append(reason)
                continue
            for name, value in data.items():
                if name in rules["fields"] and isinstance(value, str) and value.strip():
                    candidates.append(Candidate(name, value.strip(), page_number, window))
    return candidates


# --- Check each value (plain Python) ----------------------------------------------------------


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _label_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9/]+", text.casefold())


def label_owner(label_text: str, rules: dict[str, Any]) -> str | None:
    """The field whose label the text ends with; the longest label wins, so "Patient phone"
    belongs to patient_phone and not to office_phone's "phone"."""
    words = _label_words(label_text)
    best: tuple[int, str] | None = None
    for name, spec in rules["fields"].items():
        for label in spec.get("labels", []):
            wanted = _label_words(label)
            if (
                wanted
                and words[-len(wanted) :] == wanted
                and (best is None or len(label) > best[0])
            ):
                best = (len(label), name)
    return best[1] if best else None


def on_labelled_line(name: str, value: str, window: str, rules: dict[str, Any]) -> bool:
    """True when the value sits right after one of its field's labels on some line.

    The label is the text before the last ":" (or the whole text since the last " | " cell
    break when there's no colon, as in "/s/ Maren Okafor"). A span may start inside its cell
    ("Achterberg" in "Name: Samuel Achterberg, MD"), but never after another label.
    """
    span = _squash(value)
    for line in window.splitlines():
        squashed = _squash(line)
        start = squashed.find(span)
        while start >= 0:
            cell = squashed[:start].rsplit(" | ", 1)[-1]
            label = cell.rsplit(":", 1)[0] if ":" in cell else cell
            label = label.rsplit(":", 1)[-1]
            if label_owner(label, rules) == name:
                return True
            start = squashed.find(span, start + 1)
    return False


def whole_value(name: str, value: str, window: str, rules: dict[str, Any]) -> tuple[bool, str]:
    """For a field marked `whole_value` in rules.yaml: does the span hold everything its label
    is followed by? Returns (True, "") when on some line the span starts right after its own
    label and is followed only by credentials ("MD"), then the end of the line or cell, or the
    next label ("DOB:", "Date:"). Otherwise (False, what follows the label), so "Arkwright"
    read from "Patient name: Delphine Arkwright" is not taken as the patient's whole name.
    """
    span = _squash(value)
    names = [label for spec in rules["fields"].values() for label in spec.get("labels", [])]
    every = sorted({_squash(label) for label in [*names, *rules["other_labels"]]}, key=len)
    next_label = re.compile(r"(?:^|\s)(?:" + "|".join(map(re.escape, every[::-1])) + r")\s*:")
    seen = ""
    for line in window.splitlines():
        squashed = _squash(line)
        start = squashed.find(span)
        while start >= 0:
            cell = squashed[:start].rsplit(" | ", 1)[-1]
            label = cell.rsplit(":", 1)[0] if ":" in cell else cell
            label = label.rsplit(":", 1)[-1]
            gap = cell.rsplit(":", 1)[1].strip() if ":" in cell else ""
            rest = squashed[start + len(span) :].split(" | ", 1)[0]
            cut = next_label.search(rest)
            rest = (rest[: cut.start()] if cut else rest).strip(" ,.;")
            if label_owner(label, rules) == name:
                if not person_words(gap, rules) and not person_words(rest, rules):
                    return True, ""
                seen = seen or " ".join(part for part in (gap, span, rest) if part)
            start = squashed.find(span, start + 1)
    return False, seen


def phone_digits(value: str) -> str:
    """A US phone or fax number as its 10 digits, "" when it isn't one."""
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def keep_candidates(
    candidates: list[Candidate], kinds: list[str], rules: dict[str, Any], referral: Referral
) -> list[Candidate]:
    """The values with evidence behind them; every dropped one gets a note saying why.

    A value counts when it has a letter or digit (not a form blank like "______"), is in the
    text of its window, sits after one of its field's labels, isn't one of the practice's own
    numbers, and, for the order's signer, is on the referral order page.
    """
    ours = {phone_digits(number) for number in rules["our_numbers"]}
    kept: list[Candidate] = []
    for c in candidates:
        why = ""
        if not re.search(r"[^\W_]", c.value):
            continue  # an empty form blank
        if _squash(c.value) not in _squash(c.window):
            why = "is not in the page text"
        elif rules["fields"][c.field].get("labels") and not on_labelled_line(
            c.field, c.value, c.window, rules
        ):
            why = "is not after a label for this field"
        elif c.field in PHONE_FIELDS and phone_digits(c.value) in ours:
            why = "is the practice's own number"
        elif c.field == "order_signed_by" and kinds[c.page - 1] != "order":
            why = f"signs the {kinds[c.page - 1]} page, not the referral order"
        elif rules["fields"][c.field].get("whole_value"):
            whole, seen = whole_value(c.field, c.value, c.window, rules)
            if not whole:
                referral.flag(
                    f"{c.field} '{c.value}' on page {c.page} is only part of what follows its "
                    f"label ('{seen}'); check the full name",
                    c.field,
                )
                continue
        if why:
            referral.notes.append(f"{c.field} '{c.value}' on page {c.page} {why}; not used")
            continue
        kept.append(c)
    return kept


def npi_is_valid(npi: str) -> bool:
    """NPI check digit: Luhn over the 9 leading digits prefixed with 80840."""
    if not re.fullmatch(r"\d{10}", npi):
        return False
    total = 0
    for position, char in enumerate(reversed("80840" + npi[:9])):
        digit = int(char)
        if position % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return (10 - total % 10) % 10 == int(npi[9])


def parse_date(value: str, formats: list[str]) -> date | None:
    """The date in the first format that fits. A two-digit year ("03/14/49") is read in the
    century that puts the date in the past (1949, not 2049): a date of birth is never ahead."""
    cleaned = re.sub(r"\s+", " ", value).strip(" .,")
    for fmt in formats:
        try:
            parsed = datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
        if "%y" in fmt and parsed > date.today():
            parsed = parsed.replace(year=parsed.year - 100)
        return parsed
    return None


def person_words(name: str, rules: dict[str, Any]) -> frozenset[str]:
    """A name's words without case, punctuation or credentials ("Dr." "MD")."""
    words = re.findall(r"[^\W\d_]+(?:-[^\W\d_]+)*", name.casefold())
    return frozenset(word for word in words if word not in rules["name_credentials"])


def comparison_key(name: str, value: str, rules: dict[str, Any]) -> str:
    """The form two values of a field are compared in, so formatting differences don't
    conflict: digits for NPIs and numbers, the date for a DOB, sorted words for names."""
    if name == "referring_npi":
        return re.sub(r"\D", "", value)
    if name in PHONE_FIELDS:
        return phone_digits(value) or _squash(value)
    if name == "patient_dob":
        parsed = parse_date(value, rules["date_formats"])
        return parsed.isoformat() if parsed else _squash(value)
    if name == "member_id":
        return re.sub(r"[^0-9a-z]", "", value.casefold())
    return " ".join(sorted(person_words(value, rules)))


def same_value(name: str, a: str, b: str) -> bool:
    """Two comparison keys of a field name one thing.

    Patient names must be the same words: "Ann Lee" is never "Joann Lee", and "Lee" alone is
    not "Ann Lee". A provider read as "Okafor" on one page and "Maren Okafor" on another is one
    provider (the words of the shorter are all in the longer).
    """
    if name in ("referring_provider", "order_signed_by"):
        words_a, words_b = set(a.split()), set(b.split())
        return bool(words_a and words_b) and (words_a <= words_b or words_b <= words_a)
    return a == b


def merge_field(
    name: str, found: list[Candidate], rules: dict[str, Any], referral: Referral
) -> Candidate | None:
    """One value for a field from every window's answer; two different values go to review."""
    if not found:
        return None
    if rules["fields"][name].get("on_conflict") == "first":
        return min(found, key=lambda c: c.page)
    groups: dict[str, list[Candidate]] = {}
    for candidate in found:
        groups.setdefault(comparison_key(name, candidate.value, rules), []).append(candidate)
    distinct: list[str] = []
    for key in sorted(groups, key=len, reverse=True):
        if not any(same_value(name, key, kept) for kept in distinct):
            distinct.append(key)
    # Each distinct value with the reads that name the same thing ("Okafor" with "Maren Okafor").
    clusters = {
        key: [c for other, reads in groups.items() if same_value(name, other, key) for c in reads]
        for key in distinct
    }
    # In page order; the value read first (the order comes before the note), in its longest
    # form, is the one recorded.
    distinct.sort(key=lambda key: min(c.page for c in clusters[key]))
    best = max(clusters[distinct[0]], key=lambda c: (len(c.value), -c.page))
    if len(distinct) > 1 and rules["fields"][name].get("on_conflict") == "review":
        firsts = [min(clusters[key], key=lambda c: c.page) for key in distinct]
        seen = "; ".join(f"'{c.value}' (p{c.page})" for c in firsts)
        referral.flag(f"Pages disagree on {name}: {seen}", name)
    return best


def check_value(name: str, value: str, rules: dict[str, Any], referral: Referral) -> str:
    """Check a merged value in plain Python and return the normalized value to record."""
    pattern = rules["formats"].get(name)
    if pattern and not re.fullmatch(pattern, value.strip(" .,;")):
        referral.flag(f"{name} '{value}' doesn't look like one value (format check)", name)
        return value.strip(" .,;")
    if name == "referring_npi":
        digits = re.sub(r"\D", "", value)
        if not npi_is_valid(digits):
            referral.flag(f"NPI '{value}' is not 10 digits with a valid check digit", name)
        return digits
    if name in PHONE_FIELDS:
        digits = phone_digits(value)
        if not digits:
            referral.flag(f"{name} '{value}' is not a 10-digit phone number", name)
            return value
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if name == "patient_dob":
        parsed = parse_date(value, rules["date_formats"])
        if parsed is None:
            referral.flag(f"Date of birth '{value}' is not a date", name)
            return value
        today = date.today()
        if parsed > today or today.year - parsed.year > rules["thresholds"]["max_age_years"]:
            referral.flag(f"Date of birth {parsed.isoformat()} is not plausible", name)
        return parsed.isoformat()
    return value.strip(" .,;")


def page_count(pages: list[str], kinds: list[str], rules: dict[str, Any]) -> int | None:
    """The page count the cover sheet prints ("Pages (including cover): 3"), if it has one."""
    for text, kind in zip(pages, kinds, strict=True):
        if kind != "cover":
            continue
        for line in text.splitlines():
            for label in sorted(rules["page_count_labels"], key=len, reverse=True):
                pattern = r"\W*".join(re.escape(word) for word in _label_words(label))
                match = re.search(rf"(?i)\b{pattern}\W*:\s*(\d+)\b", line)
                if match:
                    return int(match.group(1))
    return None


def check_patient_on_every_page(
    pages: list[str], kinds: list[str], kept: list[Candidate], referral: Referral
) -> None:
    """Every page after the cover must name the patient: a patient name read on that page, or
    the patient's full name in its text. A page that names nobody the recipe could read may be
    another patient's, so the packet goes to review."""
    name = referral.value("patient_name")
    words = set(re.findall(r"[^\W\d_]+", name.casefold()))
    for number, (text, kind) in enumerate(zip(pages, kinds, strict=True), start=1):
        if kind == "cover" or not text.strip():
            continue
        read_here = any(c.field == "patient_name" and c.page == number for c in kept)
        in_text = bool(words) and words <= set(re.findall(r"[^\W\d_]+", text.casefold()))
        if not read_here and not in_text:
            who = f"; check it is {name}'s" if name else ""
            reason = f"Page {number} ({kind}) names no patient the recipe read{who}"
            referral.flag(reason, "patient_name")


# --- Requested priority (plain Python) --------------------------------------------------------


def classify_priority(text: str, rules: dict[str, Any]) -> tuple[str, str]:
    """The kind of priority a line or span asks for, and the words that say so.

    urgent: an urgent term not right after a negation ("not urgent" and "non-urgent" don't
    count). time_bound: a time frame ("within 72 hours", "within 72h"). routine: a routine
    term. not_stated: none of these.
    """
    spec = rules["priority"]
    negation = "|".join(re.escape(word) for word in spec["negations"])
    for term in spec["urgent_terms"]:
        for match in re.finditer(rf"(?i)\b{re.escape(term)}\b", text):
            if not re.search(rf"(?i)\b(?:{negation})[\s-]+$", text[: match.start()]):
                return "urgent", match.group(0)
    frame = re.search(spec["time_frame"], text, re.I)
    if frame:
        return "time_bound", frame.group(0)
    for term in spec["routine_terms"]:
        routine = re.search(rf"(?i)\b{re.escape(term)}\b", text)
        if routine:
            return "routine", routine.group(0)
    return "not_stated", ""


def priority_rank(kind: str, rules: dict[str, Any]) -> int:
    return list(rules["priority"]["order"]).index(kind)


def requested_priority(
    pages: list[str], kinds: list[str], kept: list[Candidate], rules: dict[str, Any]
) -> dict[str, Any]:
    """The priority the referring clinician asked for, as written, with the line it came from.

    The wording on every page and the model's span are both read; the packet sorts by the more
    pressing of the two, and when they differ the note says why.
    """
    rank = functools.partial(priority_rank, rules=rules)
    hits: list[tuple[int, int, int, str, str]] = []  # rank, not order page, page, line, kind
    for number, (text, kind) in enumerate(zip(pages, kinds, strict=True), start=1):
        for line in text.splitlines():
            found, _ = classify_priority(line, rules)
            if found != "not_stated":
                hits.append((rank(found), kind != "order", number, line, found))
    hits.sort()
    wording = hits[0][4] if hits else "not_stated"
    # A span with no priority wording in it ("six months" from "Recheck in six months") says
    # nothing about priority, so it is not counted as the model's answer.
    read = [
        (classify_priority(c.value, rules)[0], c) for c in kept if c.field == "requested_priority"
    ]
    spans = sorted(
        ((rank(kind), c.page, i, c) for i, (kind, c) in enumerate(read) if kind != "not_stated"),
        key=lambda item: item[:3],
    )
    model = classify_priority(spans[0][3].value, rules)[0] if spans else None
    final = min(wording, model or wording, key=rank)
    result: dict[str, Any] = {"kind": final, "as_written": "", "line": "", "page": None, "note": ""}
    span = next((c for *_, c in spans if classify_priority(c.value, rules)[0] == final), None)
    hit = next((h for h in hits if h[4] == final), None)
    if span is not None:
        lines = [ln for ln in span.window.splitlines() if _squash(span.value) in _squash(ln)]
        line = lines[0].strip() if lines else span.value
        result.update(as_written=span.value, line=line, page=span.page)
    elif hit is not None:
        result.update(as_written=hit[3].strip(), line=hit[3].strip(), page=hit[2])
    if model != wording and rank(final) < rank("not_stated"):
        said = f"'{spans[0][3].value}' ({model})" if spans else "no priority"
        where = (
            f"the wording on page {hits[0][2]} says '{hits[0][3].strip()}' ({wording})"
            if hits
            else "no page has priority wording"
        )
        result["note"] = f"The model read {said}; {where}; sorted as {final}"
    return result


# --- Look up the patient and the provider -----------------------------------------------------


def chart_fields(match: PatientMatch) -> dict[str, Any]:
    charts = [
        {"mrn": c.mrn, "name": c.name, "birth_date": c.birth_date.isoformat()} for c in match.charts
    ]
    others = [
        {"mrn": c.mrn, "name": c.name, "birth_date": c.birth_date.isoformat()}
        for c in match.same_name_elsewhere
    ]
    return {"status": match.status, "charts": charts, "why": match.why, "same_name": others}


def match_patient(ehr: EHR, referral: Referral, rules: dict[str, Any]) -> None:
    """Search the EHR by name, DOB and member ID. Only an exact match fills gaps from the
    chart; a close or ambiguous match goes to review, nothing found means a new patient."""
    name, dob = referral.value("patient_name"), referral.value("patient_dob")
    born = parse_date(dob, ["%Y-%m-%d"]) if dob else None
    if not name or born is None or {"patient_name", "patient_dob"} & referral.flagged:
        why = "the patient's name or date of birth wasn't read and checked"
        referral.chart = {"status": "not_searched", "why": why}
        return
    match = ehr.find_patient(name, born, referral.value("member_id"))
    referral.chart = chart_fields(match)
    chart = match.chart
    if match.status in ("possible_match", "several"):
        listed = ", ".join(f"{c.mrn} {c.name} ({c.birth_date.isoformat()})" for c in match.charts)
        referral.review.append(
            f"EHR {match.status} for {name} ({dob}): {listed}; {match.why}. Check before "
            "using the chart or registering a new patient"
        )
        return
    if chart is None:
        referral.notes.append(f"No chart for {name} ({dob}): a new patient")
        return
    for others in match.same_name_elsewhere:
        referral.notes.append(
            f"Chart {others.mrn} has the same name with DOB {others.birth_date.isoformat()}; "
            f"matched {chart.mrn} on name and DOB"
        )
    on_file = {
        "patient_phone": chart.phone,
        "insurance_plan": chart.insurance_plan,
        "member_id": chart.member_id,
    }
    for key, value in on_file.items():
        if not value:
            continue
        read = referral.fields.get(key)
        if read is None:
            referral.fields[key] = {"value": value, "source": "ehr", "mrn": chart.mrn}
        elif comparison_key(key, str(read["value"]), rules) != comparison_key(key, value, rules):
            referral.notes.append(
                f"{key}: the fax says '{read['value']}', chart {chart.mrn} has '{value}'; the "
                "fax value is used, update the chart if it changed"
            )


def provider_fields(match: ProviderMatch) -> dict[str, Any]:
    providers = [
        {"npi": p.npi, "name": p.name, "practice": p.practice, "phone": p.phone, "fax": p.fax}
        for p in match.providers
    ]
    return {"status": match.status, "providers": providers, "why": match.why}


def match_provider(directory: ProviderDirectory, referral: Referral, rules: dict[str, Any]) -> None:
    """Look the referring provider up by NPI. Only an exact match gives the office's phone and
    fax from the directory; otherwise the numbers come from the fax, and a mismatch goes to
    review. A match on the name alone (no NPI read) counts only when the fax's practice or one
    of its numbers agrees with the directory's and none disagrees; otherwise it goes to review
    as a possible match and nothing is filled from it."""
    npi, name = referral.value("referring_npi"), referral.value("referring_provider")
    if "referring_npi" in referral.flagged:
        npi = ""
    if "referring_provider" in referral.flagged:
        name = ""
    if not npi and not name:
        referral.provider = {"status": "not_searched", "why": "no NPI or provider name read"}
        return
    match = directory.find_provider(npi, name)
    referral.provider = provider_fields(match)
    provider = match.provider
    if match.status in ("possible_match", "several"):
        referral.review.append(f"Provider directory {match.status}: {match.why}")
        return
    if provider is None:
        referral.notes.append(
            f"{name or 'The referring provider'} (NPI {npi or 'not read'}) is not in the "
            "provider directory: a new referring provider; the office numbers come from the fax"
        )
        return
    if not npi:
        agree: list[str] = []
        differ: list[str] = []
        for key, value in (
            ("practice", provider.practice),
            ("office_phone", provider.phone),
            ("office_fax", provider.fax),
        ):
            read = referral.fields.get(key)
            if read is None:
                continue
            same = comparison_key(key, str(read["value"]), rules) == comparison_key(
                key, value, rules
            )
            (agree if same else differ).append(f"{key} '{read['value']}' (directory '{value}')")
        if differ or not agree:
            confirm = (
                f"the fax disagrees: {'; '.join(differ)}"
                if differ
                else "no practice or office number on the fax confirms it"
            )
            why = f"no NPI read; '{name}' fits {provider.name} (NPI {provider.npi}), but {confirm}"
            referral.provider.update(status="possible_match", why=why)
            referral.review.append(f"Provider directory possible_match: {why}")
            return
    if "referring_provider" not in referral.fields:
        referral.fields["referring_provider"] = {
            "value": provider.name,
            "source": "directory",
            "npi": provider.npi,
        }
    on_file = {
        "referring_npi": provider.npi,
        "practice": provider.practice,
        "office_phone": provider.phone,
        "office_fax": provider.fax,
    }
    for key, value in on_file.items():
        read = referral.fields.get(key)
        if read is not None and comparison_key(key, str(read["value"]), rules) != comparison_key(
            key, value, rules
        ):
            referral.notes.append(
                f"{key}: the fax says '{read['value']}', the directory has '{value}'; the "
                "directory's is used"
            )
        if read is None or key in ("office_phone", "office_fax", "practice"):
            referral.fields[key] = {"value": value, "source": "directory", "npi": provider.npi}


# --- One packet -------------------------------------------------------------------------------


def build_referral(
    pages: list[str],
    candidates: list[Candidate],
    rules: dict[str, Any],
    ehr: EHR,
    directory: ProviderDirectory,
    referral: Referral,
) -> Referral:
    """Every check after the models: values, pages, lookups, priority and the checklist."""
    kinds = [page_kind(text, rules) for text in pages]
    referral.page_kinds = kinds
    if "order" not in kinds:
        referral.review.append(f"No referral order page found (pages read as {', '.join(kinds)})")
    printed = page_count(pages, kinds, rules)
    if printed is not None and printed != len(pages):
        referral.review.append(f"The cover sheet says {printed} pages, {len(pages)} arrived")
    kept = keep_candidates(candidates, kinds, rules, referral)
    for name in rules["fields"]:
        if name == "requested_priority":
            continue
        best = merge_field(name, [c for c in kept if c.field == name], rules, referral)
        if best is not None:
            value = check_value(name, best.value, rules, referral)
            referral.fields[name] = {"value": value, "source": "fax", "page": best.page}
    check_patient_on_every_page(pages, kinds, kept, referral)
    match_patient(ehr, referral, rules)
    match_provider(directory, referral, rules)
    referral.priority = requested_priority(pages, kinds, kept, rules)
    for name, item in rules["required"].items():
        if name not in referral.fields:
            referral.missing.append({"field": name, "item": item})
    return referral


def process_packet(
    client: SIEClient,
    calls: Calls,
    pdf: Path,
    rules: dict[str, Any],
    ehr: EHR,
    directory: ProviderDirectory,
) -> Referral:
    referral = Referral(packet=pdf.name)
    pages = read_pages(client, calls, pdf, rules, referral)
    candidates = extract_candidates(client, calls, pdf.stem, pages, rules, referral)
    return build_referral(pages, candidates, rules, ehr, directory, referral)


def run(
    client: SIEClient, rules: dict[str, Any], data_dir: Path = DATA_DIR
) -> tuple[list[Referral], Calls]:
    ehr = EHR(data_dir, float(rules["thresholds"]["close_name_ratio"]))
    directory = ProviderDirectory(data_dir, tuple(rules["name_credentials"]))
    calls = Calls()
    referrals = [
        process_packet(client, calls, pdf, rules, ehr, directory)
        for pdf in sorted(data_dir.glob("*.pdf"))
    ]
    calls.check_all_failed()
    return referrals, calls


# --- Outputs ----------------------------------------------------------------------------------


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


def by_priority(referrals: list[Referral], rules: dict[str, Any]) -> list[Referral]:
    """Requested-urgent first, then time-bound, not stated, routine; packet name within."""
    return sorted(referrals, key=lambda r: (priority_rank(r.priority["kind"], rules), r.packet))


def callback_rows(referrals: list[Referral], rules: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per missing item of each missing_info packet, with the office's numbers.

    A review packet has no callback rows: the reviewer settles who the patient is first.
    """
    rows: list[dict[str, Any]] = []
    for r in by_priority(referrals, rules):
        if r.status != MISSING_INFO:
            continue
        sources = {
            str(r.fields[k]["source"]) for k in ("office_phone", "office_fax") if k in r.fields
        }
        contact = "/".join(sorted(sources)) or "not found: look the office up before calling"
        for item in r.missing:
            rows.append(
                {
                    "requested_priority": r.priority["kind"],
                    "packet": r.packet,
                    "patient": r.value("patient_name"),
                    "referring_provider": r.value("referring_provider"),
                    "practice": r.value("practice"),
                    "office_phone": r.value("office_phone"),
                    "office_fax": r.value("office_fax"),
                    "contact_from": contact,
                    "item": item["item"],
                }
            )
    return rows


def queue_rows(referrals: list[Referral], rules: dict[str, Any]) -> list[dict[str, Any]]:
    """Every packet, for the clinical reviewer who decides how soon the patient is seen."""
    return [
        {
            "order": order,
            "packet": r.packet,
            "patient": r.value("patient_name"),
            "dob": r.value("patient_dob"),
            "requested_priority": r.priority["kind"],
            "as_written": r.priority["as_written"],
            "page": r.priority["page"] or "",
            "priority_note": r.priority["note"],
            "intake_status": r.status,
            "referral_reason": r.value("referral_reason"),
            "referring_provider": r.value("referring_provider"),
            "office_phone": r.value("office_phone"),
        }
        for order, r in enumerate(by_priority(referrals, rules), start=1)
    ]


def write_outputs(output_dir: Path, referrals: list[Referral], rules: dict[str, Any]) -> None:
    (output_dir / "drafts").mkdir(parents=True, exist_ok=True)
    for referral in referrals:
        path = output_dir / "drafts" / f"{Path(referral.packet).stem}.json"
        text = json.dumps(referral.as_json(), indent=2, ensure_ascii=False) + "\n"
        path.write_text(text, encoding="utf-8")
    _write_csv(output_dir / "callback-list.csv", CALLBACK_FIELDS, callback_rows(referrals, rules))
    _write_csv(output_dir / "clinical-review-queue.csv", QUEUE_FIELDS, queue_rows(referrals, rules))


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rules = load_rules()
    started = time.monotonic()
    with make_client() as client:
        referrals, calls = stop_on_refusal(lambda: run(client, rules))
    elapsed = time.monotonic() - started
    write_outputs(output, referrals, rules)
    for r in by_priority(referrals, rules):
        reasons = r.review or [item["item"] for item in r.missing]
        print(f"{r.packet:<46} {r.status:<14} {r.priority['kind']:<11}", end="  ")
        print("; ".join(reasons) or "-")
    per_model = ", ".join(f"{count} to {model}" for model, count in calls.count.items())
    print(f"\n{calls.total} SIE calls ({per_model}) in {elapsed:.0f} s")
    print("Statuses name what the checks found; the coordinator and the clinical reviewer decide.")
    print(f"drafts:          {output / 'drafts'}")
    print(f"callback list:   {output / 'callback-list.csv'}")
    print(f"clinical review: {output / 'clinical-review-queue.csv'}")


if __name__ == "__main__":
    main()
