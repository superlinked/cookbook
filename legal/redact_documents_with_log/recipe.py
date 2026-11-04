"""Redact a client's documents to the agreed ESI protocol and write the redaction log.

Use case:  The producing side of discovery: redacting a client's documents coded "produce with
           redactions" before they go to the other side.
Who:       The litigation paralegal or litigation-support specialist working for the associate
           who runs the discovery response.
Problem:   The protocol lists what may come out (SSNs, dates of birth, account numbers down to
           the last four, non-parties' home addresses); everything else must stay readable. The
           values sit anywhere in the text, one account is written two ways, an insurer's claim
           reference looks like an SSN, and one paragraph in a long chain asks the firm's
           lawyer for advice. Every redaction needs its line in the log.
Input:     The volume in data/production.yaml; the export of that volume from the review
           platform (review_platform.py, seeded from data/review_platform.csv, with the
           extracted text in data/text/); the lawyers on the matter from the client and matter
           system (matter_system.py, seeded from data/matter_system.csv); the protocol's
           redaction list, privilege phrases and every threshold in rules.yaml.
SIE pipeline (one model):
           extract  superlinked/pii-redact  called once per window of at most 300 words: SSNs,
                                            dates of birth, account numbers and addresses in
                                            the text
           (Python keeps only the kinds on the protocol's list that have their label nearby,
           adds what the label-and-shape rules find, spreads each number to its other
           spellings, sweeps the redacted text, flags possible privilege and sets each
           document's status.)
Output:    runs/<timestamp>/: redacted/<Bates>.txt, redaction_log.csv (Bates, line, kind,
           basis, never the value), spans.json (what to redact where, for the review platform),
           privilege_flags.csv (for the associate) and report.csv (status and reasons).
Why SIE:   The PII model runs on SIE Cloud and finds the values wherever they sit in the prose.
           No generative model makes the decision; the rules are plain Python you can read
           below. Nothing to host.
Runs on:   SIE Cloud.

    uv run recipe.py
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, cast

import matter_system
import review_platform
import yaml
from dotenv import load_dotenv
from matter_system import Lawyer
from review_platform import Record
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
PRODUCTION_PATH = DATA_DIR / "production.yaml"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
# How much of an SIE error message goes into a per-document reason or the summary line.
ERROR_CHARS = 200

# What the checks found, never a decision to produce: the associate decides every privilege flag
# and signs off the set.
CHECKED, PRIVILEGE, REVIEW = "redactions_checked", "privilege_flag", "needs_review"
STATUSES = (CHECKED, PRIVILEGE, REVIEW)
REPORT_FIELDS = (
    "bates",
    "custodian",
    "status",
    "designation",
    "redactions",
    "privilege_flags",
    "reasons",
)
LOG_FIELDS = ("bates", "line", "redaction", "basis")
FLAG_FIELDS = (
    "bates",
    "lines",
    "reasons",
    "author",
    "recipients",
    "date",
    "proposed_basis",
    "associate_decision",
    "paragraph",
)
FOUND_BY = {"model": "PII model", "rule": "label and shape rule", "spread": "same number elsewhere"}
# A cell starting with one of these runs as a formula in a spreadsheet.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
REDACTED = re.compile(r"\[REDACTED: [^\]\n]*\]")
HEADER_LINE = re.compile(r"(?i)^(from|to|cc|bcc|date|sent|subject):")
SEPARATOR = re.compile(r"(?im)^(?:-{3,}.*|_{5,}|>?\s*on .+ wrote:)$")


@dataclass(frozen=True)
class Span:
    """One stretch of a document to redact. Offsets are Python code points."""

    start: int
    end: int
    kind: str  # a key under `protocol` in rules.yaml
    source: str  # "model", "rule" or "spread"; several joined by "+" after a merge


@dataclass(frozen=True)
class Flag:
    """A paragraph that may be privileged, for the associate to decide."""

    start: int
    end: int
    first_line: int
    last_line: int
    reasons: tuple[str, ...]
    author: str
    recipients: str
    date: str


@dataclass
class Document:
    record: Record
    text: str
    spans: list[Span] = field(default_factory=list)  # confirmed redactions, merged
    held: list[tuple[int, int]] = field(default_factory=list)  # left readable for a person
    replacements: list[str] = field(default_factory=list)  # one per span, same order
    redacted: str = ""
    flags: list[Flag] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # a window was not read
    leaks: list[str] = field(default_factory=list)  # the sweep found a listed kind
    reviews: list[str] = field(default_factory=list)  # a person should look

    @property
    def bates(self) -> str:
        return self.record.bates

    @property
    def status(self) -> str:
        if self.errors or self.leaks or self.reviews:
            return REVIEW
        if self.flags:
            return PRIVILEGE
        return CHECKED


def load_yaml(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    """rules.yaml, with the shared {date} pattern filled into each kind's shape and rule."""
    rules = load_yaml(path)
    for kind in rules["protocol"].values():
        for key in ("shape", "rule"):
            if kind.get(key):
                kind[key] = kind[key].replace("{date}", rules["date"])
    return rules


def load_production(path: Path = PRODUCTION_PATH) -> dict[str, Any]:
    return load_yaml(path)


def build_set(
    production: dict[str, Any], rules: dict[str, Any], data_dir: Path = DATA_DIR
) -> list[Document]:
    """The documents of the volume coded for redaction, with their extracted text, in Bates
    order. An empty volume, or a text file the export doesn't list, raises an error before any
    SIE call: the set on disk and the export must agree."""
    records = review_platform.production_set(production["volume"], data_dir / "review_platform.csv")
    if not records:
        raise ValueError(f"the export has no documents in volume {production['volume']}")
    text_dir = data_dir / "text"
    listed = {record.bates for record in records}
    stray = sorted(path.stem for path in text_dir.glob("*.txt") if path.stem not in listed)
    if stray:
        raise ValueError(f"extracted text with no row in the export: {', '.join(stray)}")
    return [
        Document(record, review_platform.extracted_text(record.bates, text_dir))
        for record in records
        if record.coding == rules["redact_coding"]
    ]


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def overlaps(start: int, end: int, ranges: Iterable[tuple[int, int]]) -> bool:
    return any(start < high and low < end for low, high in ranges)


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
    those documents go to needs_review as not read."""
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
    """The error for a per-document reason or the summary line, its message cut short."""
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
        """No call succeeded, so nothing was read: stop instead of writing empty reports."""
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
    a rate limit, a reply without the key we asked for) is logged and its document goes to
    needs_review as not read; the run continues. A refusal every call would repeat (see
    `is_fatal`) stops the run. An error that isn't an SIE error is a bug in this code and is
    raised as is.
    """
    calls.made += 1
    try:
        return work(), ""
    except SIEError as error:
        if is_fatal(error):
            raise SIEStopped(stop_message(error, model)) from error
        calls.failures.append((model, error))
        return None, f"the SIE call failed: {short(error)}"


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


# --- 1. Find: the PII model on windows ---------------------------------------------------------


def word_windows(text: str, size: int, overlap: int) -> list[tuple[int, str]]:
    """Split text into windows of at most `size` words; each is (offset in text, window text)."""
    words = list(re.finditer(r"\S+", text))
    if not words:
        return []
    step = max(1, size - overlap)
    parts: list[tuple[int, str]] = []
    for first in range(0, len(words), step):
        last = words[min(first + size, len(words)) - 1]
        start = words[first].start()
        parts.append((start, text[start : last.end()]))
        if first + size >= len(words):
            break
    return parts


def valid_entity(window: str, entity: dict[str, Any]) -> bool:
    """A span counts only if its offsets are in range and match the text it claims."""
    start, end = entity.get("start"), entity.get("end")
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(window):
        return False
    return bool(entity.get("label")) and entity.get("text") == window[start:end]


def detect_pii(
    client: SIEClient, calls: CallLog, document: Document, rules: dict[str, Any]
) -> list[tuple[int, int, str]]:
    """Send each window to the PII model; return its spans as (start, end, label) in document
    offsets. A window whose call failed sends the document to review as not read; the other
    windows still count."""
    model, limits = rules["models"]["pii"], rules["thresholds"]
    labels = list(rules["labels"])
    windows = word_windows(document.text, limits["window_words"], limits["window_overlap_words"])
    found: list[tuple[int, int, str]] = []
    for index, (offset, window) in enumerate(windows, start=1):
        item = Item(id=f"pii/{document.bates}/w{index}", text=window)

        def call(item: Item = item) -> list[dict[str, Any]]:
            reply: Any = jsonable(
                client.extract(
                    model,
                    item,
                    labels=labels,
                    # A window too long to read whole fails (InputTooLongError, the transient
                    # path: the document goes to review as not read) instead of being cut short.
                    options={"overflow_policy": "error"},
                    provision_timeout_s=WAIT_S,
                )
            )
            if not isinstance(reply, dict) or reply.get("error") or reply.get("entities") is None:
                # A reply without `entities` is a failed call, not an empty result: an empty list
                # is a real "nothing found". It takes the transient path in `guarded`.
                error = reply.get("error") if isinstance(reply, dict) else None
                raise SIEError(
                    "the reply has no `entities`" + (f" (item error: {error})" if error else "")
                )
            return cast(list[dict[str, Any]], reply["entities"])

        entities, failed = guarded(calls, model, call)
        if entities is None:
            document.errors.append(f"window {index} was not read: {failed}")
            continue
        for entity in entities:
            if not isinstance(entity, dict) or not valid_entity(window, entity):
                document.errors.append(f"window {index}: the model returned an invalid span")
                continue
            found.append((offset + entity["start"], offset + entity["end"], entity["label"]))
    return found


# --- 2. Keep only the protocol's kinds, with their label nearby -------------------------------


def kind_of(label: str, rules: dict[str, Any]) -> str | None:
    """The protocol kind a model label means, or None: not on the redaction list."""
    for kind, spec in rules["protocol"].items():
        if label in spec["model_labels"]:
            return str(kind)
    return None


def labelled(text: str, start: int, kind: str, rules: dict[str, Any]) -> bool:
    """Whether one of the kind's label words sits on the same line, at most `label_near_chars`
    before the value, with no other number in between. "Account 4471900321, claim
    404-18-2210": the account label belongs to the account, not to the claim reference."""
    line_start = text.rfind("\n", 0, start) + 1
    before = text[max(line_start, start - rules["thresholds"]["label_near_chars"]) : start]
    found = list(re.finditer(rules["protocol"][kind]["label"], before))
    return bool(found) and not re.search(r"\d", before[found[-1].end() :])


def model_spans(
    document: Document, found: list[tuple[int, int, str]], rules: dict[str, Any]
) -> tuple[list[Span], list[tuple[int, int, str]]]:
    """The model's spans of a protocol kind, cut to the values of the kind's shape inside them
    (two account numbers in one span become two). Returns the ones with their label nearby, and
    the ones without, as (start, end, kind), for a person. A span of a kind with a shape but no
    value of that shape inside it is one the rules can't place: it goes to a person too, unless
    it has no digit at all (the model tagging the label "SSN" itself), which holds no value."""
    confirmed: list[Span] = []
    unsure: list[tuple[int, int, str]] = []
    text = document.text
    for start, end, label in found:
        kind = kind_of(label, rules)
        if kind is None:
            continue
        shape = rules["protocol"][kind].get("shape")
        pieces = (
            [(start + m.start(), start + m.end()) for m in re.finditer(shape, text[start:end])]
            if shape
            else [(start, end)]
        )
        if shape and not pieces and re.search(r"\d", text[start:end]):
            unsure.append((start, end, kind))
        for low, high in pieces:
            if labelled(text, low, kind, rules):
                confirmed.append(Span(low, high, kind, "model"))
            else:
                unsure.append((low, high, kind))
    return confirmed, unsure


def rule_spans(text: str, rules: dict[str, Any]) -> list[Span]:
    """What each kind's `rule` finds by its label and shape, without the model."""
    return [
        Span(match.start("value"), match.end("value"), kind, "rule")
        for kind, spec in rules["protocol"].items()
        for match in re.finditer(spec["rule"], text)
    ]


# --- 3. Spread: the same number in every spelling across the set -------------------------------


def digits_pattern(digits: str) -> str:
    """A regex for these digits with any spaces (non-breaking too), dots or dashes between
    them, not inside a longer number."""
    body = r"[ \xa0.\-]?".join(re.escape(digit) for digit in digits)
    return r"(?<![\d])(?<!\d[ \xa0.\-])" + body + r"(?!\d)(?![ \xa0.\-]\d)"


def spread_values(documents: list[Document], rules: dict[str, Any]) -> dict[str, str]:
    """The digits of every confirmed value of a `spread` kind, mapped to its kind."""
    values: dict[str, str] = {}
    for document in documents:
        for span in document.spans:
            if not rules["protocol"][span.kind].get("spread"):
                continue
            digits = re.sub(r"\D", "", document.text[span.start : span.end])
            if len(digits) >= rules["thresholds"]["spread_min_digits"]:
                values.setdefault(digits, span.kind)
    return values


def spread_spans(text: str, values: dict[str, str]) -> list[Span]:
    return [
        Span(*match.span(), kind, "spread")
        for digits, kind in values.items()
        for match in re.finditer(digits_pattern(digits), text)
    ]


def merge_spans(spans: list[Span], rules: dict[str, Any]) -> list[Span]:
    """Merge overlapping spans into one, of the kind of the longest (on a tie, the kind listed
    first in the protocol), and remember every way it was found."""
    order = list(rules["protocol"])
    merged: list[Span] = []
    group: list[Span] = []

    def close() -> None:
        if not group:
            return
        start, end = group[0].start, max(span.end for span in group)
        lead = max(group, key=lambda s: (s.end - s.start, -order.index(s.kind)))
        sources = sorted({part for s in group for part in s.source.split("+")})
        merged.append(Span(start, end, lead.kind, "+".join(sources)))
        group.clear()

    for span in sorted(spans, key=lambda s: (s.start, -s.end)):
        if group and span.start >= max(s.end for s in group):
            close()
        group.append(span)
    close()
    return merged


# --- 4. Redact, sweep -------------------------------------------------------------------------


def replacement(text: str, span: Span, rules: dict[str, Any]) -> str:
    """What the redacted text shows: the kind's label, and for `last_four` the last digits."""
    spec = rules["protocol"][span.kind]
    if spec["how"] != "last_four":
        return str(spec["replace_with"])
    digits = re.sub(r"\D", "", text[span.start : span.end])
    if len(digits) < spec["show_last_min_digits"]:
        return str(spec["replace_short"])
    return str(spec["replace_with"]).replace("{last}", digits[-spec["show_last"] :])


def redact(text: str, spans: list[Span], replacements: list[str]) -> str:
    """Replace each span with its replacement, last span first so offsets stay valid."""
    for span, new in sorted(
        zip(spans, replacements, strict=True), reverse=True, key=lambda pair: pair[0].start
    ):
        text = text[: span.start] + new + text[span.end :]
    return text


def blanked(text: str, spans: list[Span]) -> str:
    """The text with every redacted span turned into spaces (newlines kept), so the sweep reads
    what goes out at the original offsets and line numbers."""
    chars = list(text)
    for span in spans:
        for offset in range(span.start, span.end):
            if chars[offset] != "\n":
                chars[offset] = " "
    return "".join(chars)


def sweep(
    text: str, spans: list[Span], held: list[tuple[int, int]], rules: dict[str, Any]
) -> list[str]:
    """Every kind's rule and the `sweep` shapes, run on the text as it goes out. A hit is a listed
    kind still readable: the reason gives the line, never the value. Values already left for a
    person (`held`) are reported once, where they were held."""
    out = blanked(text, spans)
    patterns = {
        f"a labelled {spec['log'].lower()}": spec["rule"] for spec in rules["protocol"].values()
    }
    patterns.update(rules["sweep"])
    hits: list[str] = []
    for name, pattern in patterns.items():
        for match in re.finditer(pattern, out):
            start, end = match.span("value") if "value" in match.re.groupindex else match.span()
            if overlaps(start, end, held):
                continue
            hit = f"line {line_of(text, start)}: {name} is still readable"
            if hit not in hits:
                hits.append(hit)
    return hits


# --- 5. Possible privilege --------------------------------------------------------------------


@dataclass(frozen=True)
class Paragraph:
    start: int
    end: int
    text: str


def paragraphs(text: str) -> list[Paragraph]:
    """Blocks of text between blank lines (a line with only spaces or a \\r counts as blank, so
    Windows line endings split the same way). Offsets stay those of the extracted text."""
    return [
        Paragraph(match.start(), match.start() + len(block), block)
        for match in re.finditer(r"\S(?:.|\n(?![ \t\r]*\n))*", text)
        for block in [match.group(0).rstrip()]
    ]


def is_header(paragraph: Paragraph) -> bool:
    """An email header block: two or more lines, every one a From, To, Cc, Date, Sent or
    Subject field (after any "Original Message" line), and a From or To among them. One line
    starting "To:" in a body ("To: Priya - can I hold the payment?") is a paragraph."""
    lines = [line.strip() for line in paragraph.text.splitlines()]
    lines = [line for line in lines if not SEPARATOR.fullmatch(line)]
    fields = [HEADER_LINE.match(line) for line in lines]
    return (
        len(lines) >= 2
        and all(fields)
        and any(f.group(1).casefold() in ("from", "to") for f in fields if f)
    )


def header_field(paragraph: Paragraph, name: str) -> str:
    for line in paragraph.text.splitlines():
        match = HEADER_LINE.match(line.strip())
        if match and match.group(1).casefold() == name:
            return line.strip()[match.end() :].strip()
    return ""


def utc_date(value: str) -> str:
    """A date as ISO 8601 UTC ("2026-07-06T15:30:00Z"), from the export's ISO form or an email
    header's ("Mon, 6 Jul 2026 11:30:00 -0400"). One without a time zone, or one that doesn't
    parse, is kept as written for the associate to read."""
    try:
        when = datetime.fromisoformat(value.strip())
    except ValueError:
        try:
            when = parsedate_to_datetime(value.strip())
        except (TypeError, ValueError, IndexError):
            return value
    if when.tzinfo is None:
        return value
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_terms(text: str, terms: Iterable[str], *, ignore_case: bool) -> list[tuple[int, int]]:
    """Every whole-word occurrence of any term, as (start, end)."""
    flags = re.IGNORECASE if ignore_case else 0
    found: list[tuple[int, int]] = []
    for term in terms:
        if term:
            pattern = r"(?<![\w.@-])" + re.escape(term) + r"(?![\w@-])"
            found.extend(match.span() for match in re.finditer(pattern, text, flags))
    return found


def firm_names(lawyers: list[Lawyer]) -> list[str]:
    """Each lawyer's firm as written in the matter system, and without its LLP or LLC."""
    names: list[str] = []
    for lawyer in lawyers:
        short_name = re.sub(r",?\s+(?:LLP|LLC|PC|P\.C\.|PLLC)$", "", lawyer.organization)
        for name in (lawyer.organization, short_name):
            if name and name not in names:
                names.append(name)
    return names


def lawyers_named(text: str, lawyers: list[Lawyer]) -> list[str]:
    """The lawyers (and firms) the text names: full name or email in any case, another listed
    form of the name as written. "Priya Okonkwo" is someone else; "Hollis Street Storage" is
    not the firm "Hollis & Crane"."""
    named = [
        lawyer.name
        for lawyer in lawyers
        if find_terms(text, [lawyer.name, lawyer.email], ignore_case=True)
        or find_terms(text, lawyer.other_names, ignore_case=False)
    ]
    named += [firm for firm in firm_names(lawyers) if find_terms(text, [firm], ignore_case=True)]
    return list(dict.fromkeys(named))


def privilege_flags(document: Document, lawyers: list[Lawyer], rules: dict[str, Any]) -> list[Flag]:
    """Flag each paragraph that names a lawyer on the matter, sits in a message to or from one,
    or uses a privilege phrase. A header block starts a new message in a chain; the first
    message's people come from the export (author and recipients) as well as its header.
    Headers and separators are never flagged themselves."""
    text, record = document.text, document.record
    flags: list[Flag] = []
    people = f"{record.author}; {record.recipients}"
    author, recipients, date = record.author, record.recipients, record.date
    first_header = True
    for paragraph in paragraphs(text):
        if is_header(paragraph):
            if not first_header:
                people = paragraph.text
                author = header_field(paragraph, "from")
                recipients = "; ".join(
                    v for v in (header_field(paragraph, n) for n in ("to", "cc")) if v
                )
                date = header_field(paragraph, "date") or header_field(paragraph, "sent")
            else:
                people = f"{people}\n{paragraph.text}"
            first_header = False
            continue
        if SEPARATOR.fullmatch(paragraph.text.strip()):
            continue
        first_header = False
        reasons = [f"in a message to or from {name}" for name in lawyers_named(people, lawyers)]
        reasons += [f"names {name}" for name in lawyers_named(paragraph.text, lawyers)]
        for pattern in rules["privilege_phrases"]:
            for match in re.finditer(pattern, paragraph.text):
                reasons.append(f'says "{match.group(0)}"')
        if reasons:
            flags.append(
                Flag(
                    paragraph.start,
                    paragraph.end,
                    line_of(text, paragraph.start),
                    line_of(text, paragraph.end),
                    tuple(dict.fromkeys(reasons)),
                    author,
                    recipients,
                    utc_date(date),
                )
            )
    return flags


# --- Run --------------------------------------------------------------------------------------


@dataclass
class Result:
    documents: list[Document]
    calls: int


def flag_no_lawyers(lawyers: list[Lawyer], documents: list[Document]) -> None:
    """With no lawyer on the matter in the matter system, only the phrases can flag privilege,
    so every document goes to a person."""
    if lawyers:
        return
    print(
        "Warning: the matter system lists no lawyers on this matter; every document goes to review."
    )
    for document in documents:
        document.reviews.append(
            "the matter system lists no lawyers on this matter, so privilege was checked by "
            "phrases only; a person checks"
        )


def run(
    client: SIEClient, rules: dict[str, Any], documents: list[Document], lawyers: list[Lawyer]
) -> Result:
    """Find, keep, spread, redact, sweep and flag, in the order of the flow. The spread waits
    for every document's own redactions, so a number found in the last document still reaches
    the first."""
    calls = CallLog()
    unsure: dict[str, list[tuple[int, int, str]]] = {}
    for doc in documents:
        found = detect_pii(client, calls, doc, rules)
        confirmed, unsure[doc.bates] = model_spans(doc, found, rules)
        doc.spans = merge_spans(confirmed + rule_spans(doc.text, rules), rules)
    calls.stop_if_all_failed()
    values = spread_values(documents, rules)
    for doc in documents:
        covered = [(s.start, s.end) for s in doc.spans]
        elsewhere = [
            s for s in spread_spans(doc.text, values) if not overlaps(s.start, s.end, covered)
        ]
        doc.spans = merge_spans(doc.spans + elsewhere, rules)
        for start, end, kind in unsure[doc.bates]:
            if overlaps(start, end, [(s.start, s.end) for s in doc.spans]):
                continue  # confirmed another way: a label rule or the same number elsewhere
            doc.held.append((start, end))
            doc.reviews.append(
                f"line {line_of(doc.text, start)}: the PII model found what may be a "
                f"{rules['protocol'][kind]['log'].lower()} with no label saying so; left "
                "readable, a person decides"
            )
        doc.replacements = [replacement(doc.text, span, rules) for span in doc.spans]
        doc.redacted = redact(doc.text, doc.spans, doc.replacements)
        doc.leaks = sweep(doc.text, doc.spans, doc.held, rules)
        doc.flags = privilege_flags(doc, lawyers, rules)
        if doc.record.privilege_coding == rules["partly_privileged_coding"] and not doc.flags:
            doc.reviews.append(
                f"the reviewer coded it {doc.record.privilege_coding.lower()}, but no paragraph "
                "was flagged; a person finds the privileged part"
            )
    return Result(documents, calls.made)


def csv_safe(row: dict[str, Any]) -> dict[str, Any]:
    """Quote a cell that a spreadsheet would run as a formula ("=HYPERLINK(...)") with a '."""
    return {
        key: f"'{value}" if isinstance(value, str) and value.startswith(FORMULA_START) else value
        for key, value in row.items()
    }


def report_rows(result: Result) -> list[dict[str, Any]]:
    """One row per document: its status and every reason, by line, never a value."""
    return [
        {
            "bates": doc.bates,
            "custodian": doc.record.custodian,
            "status": doc.status,
            "designation": doc.record.designation,
            "redactions": len(doc.spans),
            "privilege_flags": len(doc.flags),
            "reasons": " | ".join([*doc.errors, *doc.leaks, *doc.reviews]),
        }
        for doc in result.documents
    ]


def log_rows(result: Result, rules: dict[str, Any]) -> list[dict[str, Any]]:
    """The redaction log the other side receives: Bates number, line, kind and basis, never the
    value. Privilege redactions join it only once the associate confirms them."""
    return [
        {
            "bates": doc.bates,
            "line": line_of(doc.text, span.start),
            "redaction": rules["protocol"][span.kind]["log"],
            "basis": rules["protocol"][span.kind]["basis"],
        }
        for doc in result.documents
        for span in doc.spans
    ]


def flag_rows(result: Result, rules: dict[str, Any]) -> list[dict[str, Any]]:
    """The privilege flags for the associate: where, why, the log fields a confirmed privilege
    redaction would carry, and the paragraph as it reads after the PII redactions."""
    rows: list[dict[str, Any]] = []
    for doc in result.documents:
        for flag in doc.flags:
            pairs = [
                (s, r)
                for s, r in zip(doc.spans, doc.replacements, strict=True)
                if s.start < flag.end and flag.start < s.end
            ]
            # A span that runs past the paragraph is cut to it, so no value shows in the excerpt.
            cut = [
                Span(
                    max(s.start, flag.start) - flag.start,
                    min(s.end, flag.end) - flag.start,
                    s.kind,
                    s.source,
                )
                for s, _ in pairs
            ]
            rows.append(
                {
                    "bates": doc.bates,
                    "lines": f"{flag.first_line}-{flag.last_line}",
                    "reasons": "; ".join(flag.reasons),
                    "author": flag.author,
                    "recipients": flag.recipients,
                    "date": flag.date,
                    "proposed_basis": rules["privilege_basis"],
                    "associate_decision": "",
                    "paragraph": redact(
                        doc.text[flag.start : flag.end], cut, [r for _, r in pairs]
                    ),
                }
            )
    return rows


def spans_json(result: Result, production: dict[str, Any]) -> dict[str, Any]:
    """What litigation support needs to apply the redactions in the review platform: each
    document's designation, its redactions as character offsets into the extracted text, and
    the flagged paragraphs (applied only once the associate confirms them). No values."""
    return {
        "matter": production["matter"],
        "volume": production["volume"],
        "offsets": "Python code points into the extracted text in data/text/<bates>.txt",
        "documents": [
            {
                "bates": doc.bates,
                "status": doc.status,
                "designation": doc.record.designation,
                "redactions": [
                    {
                        "start": span.start,
                        "end": span.end,
                        "line": line_of(doc.text, span.start),
                        "kind": span.kind,
                        "replace_with": new,
                        "found_by": [FOUND_BY[part] for part in span.source.split("+")],
                    }
                    for span, new in zip(doc.spans, doc.replacements, strict=True)
                ],
                "privilege_flags": [
                    {
                        "start": flag.start,
                        "end": flag.end,
                        "lines": [flag.first_line, flag.last_line],
                    }
                    for flag in doc.flags
                ],
            }
            for doc in result.documents
        ],
    }


def write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(csv_safe(row) for row in rows)


def write_outputs(
    output_dir: Path, result: Result, rules: dict[str, Any], production: dict[str, Any]
) -> None:
    (output_dir / "redacted").mkdir(parents=True, exist_ok=True)
    for doc in result.documents:
        (output_dir / "redacted" / f"{doc.bates}.txt").write_text(doc.redacted, encoding="utf-8")
    write_csv(output_dir / "report.csv", REPORT_FIELDS, report_rows(result))
    write_csv(output_dir / "redaction_log.csv", LOG_FIELDS, log_rows(result, rules))
    write_csv(output_dir / "privilege_flags.csv", FLAG_FIELDS, flag_rows(result, rules))
    (output_dir / "spans.json").write_text(
        json.dumps(spans_json(result, production), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rules, production = load_rules(), load_production()
    documents = build_set(production, rules)
    lawyers = matter_system.lawyers_on_matter(production["matter"], DATA_DIR / "matter_system.csv")
    flag_no_lawyers(lawyers, documents)
    with make_client() as client:
        result = stop_on_refusal(lambda: run(client, rules, documents, lawyers))
    write_outputs(output, result, rules, production)
    for row in report_rows(result):
        print(
            f"\n{row['bates']}  {row['status']:<18} {row['redactions']:>2} redactions  "
            f"{row['privilege_flags']} privilege flags  {row['designation']}"
        )
        for reason in row["reasons"].split(" | ") if row["reasons"] else []:
            print(f"    {reason}")
    print(f"\n{result.calls} SIE calls. Nothing goes to the other side from here: the associate")
    print("decides each privilege flag and signs off the set; litigation support applies the")
    print("redactions in the review platform from spans.json.")
    print(f"redacted text: {output / 'redacted'}")
    print(f"redaction log: {output / 'redaction_log.csv'}")
    print(f"privilege flags: {output / 'privilege_flags.csv'}")
    print(f"report: {output / 'report.csv'}, spans: {output / 'spans.json'}")


if __name__ == "__main__":
    main()
