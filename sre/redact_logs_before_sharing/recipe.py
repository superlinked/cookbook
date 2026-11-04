"""Redact secrets and customer data from an incident's logs before they go to a vendor's support.

Use case:  Log redaction before the on-call engineer attaches an incident's logs and notes to a
           vendor's support case (here the card processor, cardvault).
Who:       The on-call engineer (SRE) who opens the case, and the security team that owns the
           list of what may leave the company.
Problem:   The vendor needs timestamps, hosts, error text, versions and the shape of each request.
           The same lines carry customer emails, client IPs, user IDs, session cookies, now and
           then a bearer token, an API key or a database password, and on a bad day a full card
           number. Grep misses some of it; stripping half the file leaves the vendor nothing to
           debug from.
Input:     The vendor case in data/case.yaml (incident, vendor, services, time range, notes
           file); the log lines from the log platform (log_platform.py, seeded from
           data/log_platform.csv); the on-call notes; the requester fields of the incident's
           tickets from the support desk (support_desk.py, seeded from data/support_desk.csv).
           Remove and keep lists, the company's and the vendor's names and every threshold in
           rules.yaml.
SIE pipeline (one model):
           extract  superlinked/pii-redact  called once per window of at most 150 words: names,
                                            companies, emails, IPs, IDs and URLs in the text
           (Python applies the field rules, the allowlist, one placeholder per value across the
           bundle and a final regex and Luhn sweep, and sets each file's status.)
Output:    runs/<timestamp>/: the cleaned files under ready_for_approval/, review/ or blocked/,
           report.csv (status and reasons per file), redactions.csv (what was taken out, by line,
           never the value) and private/ (the mapping back to real values and the exported
           originals; never attached).
Why SIE:   The PII model runs on SIE Serverless and finds what no log field labels: a customer named
           in the notes, a first name on its own. No generative model makes the decision; the
           rules are plain Python you can read below. Nothing to host.
Runs on:   SIE Serverless.

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
from pathlib import Path
from typing import Any, cast

import log_platform
import support_desk
import yaml
from dotenv import load_dotenv
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
CASE_PATH = DATA_DIR / "case.yaml"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
# How much of an SIE error message goes into a per-file reason or the summary line.
ERROR_CHARS = 200

# What the checks found, never a decision to send: the on-call engineer compares the original
# and the cleaned file and decides what is attached to the vendor case.
READY, REVIEW, BLOCKED = "ready_for_approval", "review", "blocked"
STATUSES = (READY, REVIEW, BLOCKED)
KEEP, MASK = "keep", "mask"
REPORT_FIELDS = ("document", "status", "redactions", "placeholders", "reasons")
REDACTION_FIELDS = ("document", "line", "what", "replaced_with", "found_by")
# A cell starting with one of these runs as a formula in a spreadsheet.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
PLACEHOLDER = re.compile(r"\[([A-Z][A-Z_]*_\d+)\]")
# On a tie in length, an identifier rule wins, then the model or a known customer, then a
# propagated name or another form of a known name, then a second spelling of a found value.
SOURCE_RANK = {
    "rule": 2,
    "model": 1,
    "known": 1,
    "propagated": 0,
    "name_form": 0,
    "second_spelling": -1,
}
FOUND_BY = {
    "rule": "identifier rule",
    "model": "PII model",
    "known": "support desk requester",
    "propagated": "name found in another file",
    "name_form": "another form of a known name",
    "second_spelling": "same value found elsewhere",
}


@dataclass(frozen=True)
class Span:
    """One stretch of a document to redact. Offsets are Python code points."""

    start: int
    end: int
    label: str
    text: str
    source: str  # "model", "known", "propagated", "name_form", "second_spelling" or "rule"
    score: float = 1.0  # the model's confidence; 1.0 for spans found in code
    whole: str = ""  # for a piece of a model span a rule cut through: the span's full text


@dataclass
class Entity:
    """One real-world thing behind a placeholder, with every way the bundle's files write it."""

    placeholder: str
    label: str
    values: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)
    keys: set[str] = field(default_factory=set)

    @property
    def tokens(self) -> set[str]:
        return {token for key in self.keys for token in key.split()}

    def add(self, value: str, document: str) -> None:
        if value not in self.values:
            self.values.append(value)
        if document not in self.documents:
            self.documents.append(document)


@dataclass
class Document:
    name: str
    text: str
    spans: list[Span] = field(default_factory=list)
    placeholders: list[str] = field(default_factory=list)  # one per span, same order
    replacements: list[str] = field(default_factory=list)  # what each span becomes in the text
    kept: list[tuple[int, int]] = field(default_factory=list)  # identifiers kept as written
    redacted: str = ""
    leaks: list[str] = field(default_factory=list)  # blocked: data still readable
    errors: list[str] = field(default_factory=list)  # blocked: not read, or rules can't decide
    reviews: list[str] = field(default_factory=list)  # review: a person should act on it

    @property
    def status(self) -> str:
        if self.leaks or self.errors:
            return BLOCKED
        if self.reviews:
            return REVIEW
        return READY


@dataclass(frozen=True)
class Case:
    """The vendor case the bundle is for, as the on-call engineer sets it up."""

    incident: str
    vendor: str
    services: tuple[str, ...]
    start: datetime  # in UTC
    end: datetime  # in UTC
    notes: str


def load_yaml(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return load_yaml(path)


def utc_time(value: object, what: str) -> datetime:
    """A window end as a UTC datetime. YAML gives a datetime for an unquoted timestamp and a
    string for a quoted one; both must carry a timezone ("Z" or "+02:00"), or the window could
    be off by hours without anyone noticing."""
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"window {what} {value!s} has no timezone; add Z or an offset")
    return moment.astimezone(UTC)


def load_case(path: Path = CASE_PATH) -> Case:
    raw = load_yaml(path)
    case = Case(
        incident=str(raw["incident"]),
        vendor=str(raw["vendor"]),
        services=tuple(raw["services"]),
        start=utc_time(raw["window"]["start"], "start"),
        end=utc_time(raw["window"]["end"], "end"),
        notes=str(raw["notes"]),
    )
    if case.end <= case.start:
        raise ValueError(f"window end {case.end} is not after its start {case.start}")
    return case


def build_bundle(case: Case, data_dir: Path = DATA_DIR) -> list[Document]:
    """The files the engineer would attach: one log export per service from the log platform,
    then the on-call notes. A service with no lines in the window, or a notes name that leads
    out of `data_dir`, raises an error before any SIE call."""
    documents: list[Document] = []
    for service in case.services:
        events = log_platform.search(service, case.start, case.end, data_dir / "log_platform.csv")
        if not events:
            raise ValueError(
                f"no {service} log lines from {case.start.isoformat()} to {case.end.isoformat()}"
            )
        documents.append(Document(f"{service}.log", log_platform.export(events)))
    notes = data_dir / case.notes
    if Path(case.notes).name != case.notes or not notes.is_file():
        raise ValueError(f"no notes file {case.notes!r} in {data_dir}")
    documents.append(Document(case.notes, notes.read_text(encoding="utf-8")))
    return documents


def known_customers(case: Case, data_dir: Path = DATA_DIR) -> list[tuple[str, str]]:
    """The requester name, email and organization of every ticket linked to the incident, as
    (value, label). The ticket itself is not part of the bundle."""
    known: list[tuple[str, str]] = []
    for ticket in support_desk.tickets_for_incident(case.incident, data_dir / "support_desk.csv"):
        for value, label in (
            (ticket.requester_name, "person"),
            (ticket.requester_email, "email address"),
            (ticket.organization, "organization"),
        ):
            if value.strip() and (value.strip(), label) not in known:
                known.append((value.strip(), label))
    return known


def flag_no_ticket(case: Case, known: list[tuple[str, str]], documents: list[Document]) -> None:
    """With no linked ticket (or one with no requester fields), the customer's name and email
    are not known before the model reads the files, so only the model can find them. Warn, and
    send every file to review so a person looks for the customer."""
    if known:
        return
    print(
        f"Warning: no support ticket linked to {case.incident} names a requester; only the PII "
        "model looks for the customer, so every file goes to review."
    )
    for document in documents:
        document.reviews.append(
            f"no support ticket linked to {case.incident} names a requester, so the customer's "
            "name and email were not known; a person checks for them"
        )


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def inside(start: int, end: int, ranges: Iterable[tuple[int, int]]) -> bool:
    return any(low <= start and end <= high for low, high in ranges)


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
    those files are blocked as not read."""
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
    """The error for a per-file reason or the summary line, its message cut short."""
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
    a rate limit, a reply without the key we asked for) is logged and its file is blocked as not
    read; the run continues. A refusal every call would repeat (see `is_fatal`) stops the run.
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


# --- 1. Detect: the PII model on windows ------------------------------------------------------


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


def detect_pii(
    client: SIEClient, calls: CallLog, document: Document, rules: dict[str, Any]
) -> list[Span]:
    """Send each window to the PII model and return its spans in document offsets. A window
    whose call failed blocks the file as not read; the other windows still count."""
    model, limits = rules["models"]["pii"], rules["thresholds"]
    labels = list(rules["labels"])
    stem = Path(document.name).stem
    windows = word_windows(document.text, limits["window_words"], limits["window_overlap_words"])
    spans: list[Span] = []
    for index, (offset, window) in enumerate(windows, start=1):
        item = Item(id=f"pii/{stem}/w{index}", text=window)

        def call(item: Item = item) -> list[dict[str, Any]]:
            reply: Any = jsonable(
                client.extract(
                    model,
                    item,
                    labels=labels,
                    # A window too long to read whole fails (InputTooLongError, the transient
                    # path: the file is blocked as not read) instead of being cut short.
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
            start, end = offset + entity["start"], offset + entity["end"]
            score = float(entity.get("score") or 0.0)
            text = document.text[start:end]
            spans.append(Span(start, end, entity["label"], text, "model", score))
    return spans


# --- 2. Known names: a name found once is redacted everywhere ---------------------------------


def find_terms(text: str, terms: Iterable[str], *, ignore_case: bool) -> list[tuple[int, int]]:
    """Every whole-word occurrence of any term, as (start, end)."""
    flags = re.IGNORECASE if ignore_case else 0
    found: list[tuple[int, int]] = []
    for term in terms:
        pattern = r"(?<!\w)" + re.escape(term) + r"(?!\w)"
        found.extend(match.span() for match in re.finditer(pattern, text, flags))
    return found


def known_mentions(text: str, known: list[tuple[str, str]]) -> list[Span]:
    """Every mention of a support-desk requester's name, email or organization, any case."""
    return [
        Span(start, end, label, text[start:end], "known")
        for value, label in known
        for start, end in find_terms(text, [value], ignore_case=True)
    ]


def propagate_names(text: str, names: dict[str, str], min_letters: int) -> list[Span]:
    """Redact every other mention of a name the model found in any file of the bundle."""
    spans: list[Span] = []
    for name, label in names.items():
        if sum(char.isalpha() for char in name) < min_letters:
            continue
        for start, end in find_terms(text, [name], ignore_case=False):
            spans.append(Span(start, end, label, text[start:end], "propagated"))
    return spans


def person_names(names: Iterable[str], rules: dict[str, Any]) -> list[str]:
    """The names in `names` that look like a person: two or more words, none of them a company
    word ("LLC", "Holdings") from `company_words`."""
    company = set(rules["company_words"])
    people: list[str] = []
    for name in names:
        words = re.findall(r"[^\W_]+(?:['-][^\W_]+)*", name)
        if len(words) >= 2 and not company & {word.casefold() for word in words}:
            people.append(name)
    return people


def name_form_spans(
    text: str, people: dict[str, str], rules: dict[str, Any], released: Iterable[str] = ()
) -> list[Span]:
    """Other ways a known person is written: "SURNAME, FIRST", the surname on its own and the
    first name on its own.

    `people` maps each known full name to its label. A lone surname or first name is matched as
    written, in capitals or capitalized, and skipped when it is on the allowlist, unless it is
    in `released` (a known customer's name wins over the allowlist). A name word that two known
    people share is still redacted; the placeholder step finds it fits both and sends the file
    to review."""
    min_letters = rules["thresholds"]["propagate_min_letters"]
    off = {normalize(term, "person", rules) for term in released}
    keep = {normalize(term, "person", rules) for term in allowlist_terms(rules)} - off
    spans: list[Span] = []
    for name, label in people.items():
        words = re.findall(r"[^\W_]+(?:['-][^\W_]+)*", name)
        first, surname = words[0], words[-1]
        reversed_name = r"(?<!\w)" + re.escape(surname) + r",\s*" + re.escape(first) + r"(?!\w)"
        for match in re.finditer(reversed_name, text, re.IGNORECASE):
            spans.append(Span(*match.span(), label, match.group(0), "name_form"))
        for word in sorted({first, surname}):
            if len(word) < min_letters or normalize(word, "person", rules) in keep:
                continue
            forms = sorted({word, word.upper(), word.capitalize()})
            for start, end in find_terms(text, forms, ignore_case=False):
                spans.append(Span(start, end, label, text[start:end], "name_form"))
    return spans


def phone_national(digits: str) -> str:
    """A phone number's digits without a country code: +1 (US) and +44 (UK, with or without the
    "(0)") come off, so "+44 7700 900152" and "07700 900152" compare equal."""
    if digits.startswith("440"):
        return digits[2:]
    if digits.startswith("44") and len(digits) == 12:
        return "0" + digits[2:]
    if digits.startswith("1") and len(digits) == 11:
        return digits[1:]
    return digits


def digits_pattern(digits: str, *, phone: bool) -> str:
    """A regex for these digits with any spacing, dots, dashes or brackets between them. For a
    phone it also allows the country-code forms ("+1 ", "+44 (0)"), and takes an opening
    bracket only when the area code closes it, so "(617-555-0142)" keeps both brackets."""
    separator = r"[ \t.()\-]{0,2}"
    prefix = ""
    if phone and digits.startswith("0"):
        prefix, digits = r"(?:\+\s?44\s?(?:\(0\)\s?)?|\((?=0\d{1,4}\))0|0)", digits[1:]
    elif phone and len(digits) == 10:
        prefix = r"(?:\+?1[ .\-]?)?(?:\((?=\d{3}\)))?"
    body = separator.join(re.escape(digit) for digit in digits)
    return r"(?<![\w+])(?<!\d[ .\-])" + prefix + body + r"(?!\d)(?![ .\-]\d)"


def spelling_key(span: Span, rules: dict[str, Any]) -> str | None:
    """How a found value is compared with its other mentions: digits for numbers (a phone
    without its country code), letters and digits for customer IDs, lower case for emails, the
    text as written for the `propagate_labels` (addresses). None for everything else: names have
    `propagate_names`, and other labels are left to the model, so one misread (a request path
    tagged as a URL) does not spread to every file. Values too short to search for safely are
    skipped."""
    if span.label in rules["digit_labels"]:
        digits = re.sub(r"\D", "", span.text)
        if len(digits) < rules["thresholds"]["second_spelling_min_digits"]:
            return None
        return phone_national(digits) if span.label in rules["phone_labels"] else digits
    if span.label in rules["alnum_labels"]:
        return re.sub(r"[^0-9A-Za-z]", "", span.text).upper()
    if span.label == "email address":
        return span.text.strip().casefold()
    if span.label not in rules["propagate_labels"]:
        return None
    if sum(char.isalnum() for char in span.text) < rules["thresholds"]["propagate_min_letters"]:
        return None
    return span.text


def second_spellings(text: str, values: dict[str, Span], rules: dict[str, Any]) -> list[Span]:
    """Every other mention of a value found anywhere in the bundle: the same phone or account
    number written with other separators or a country code, the same customer ID or email in
    other capitals or without its dashes, the same address in another file. `values` maps each
    value's comparison key (see `spelling_key`) to a span that found it."""
    spans: list[Span] = []
    for key, found in values.items():
        if found.label in rules["digit_labels"]:
            pattern = digits_pattern(key, phone=found.label in rules["phone_labels"])
            ranges = [match.span() for match in re.finditer(pattern, text)]
        elif found.label in rules["alnum_labels"]:
            pattern = r"(?<!\w)" + r"[ \-]?".join(re.escape(char) for char in key) + r"(?!\w)"
            ranges = [match.span() for match in re.finditer(pattern, text, re.IGNORECASE)]
        else:
            ranges = find_terms(text, [key], ignore_case=found.label == "email address")
        spans += [Span(s, e, found.label, text[s:e], "second_spelling") for s, e in ranges]
    return spans


def extend_to_patterns(text: str, spans: list[Span], rules: dict[str, Any]) -> list[Span]:
    """Stretch a model span (not a name) that stops inside a `residual_patterns` match to the end
    of that match, so a value cut one character short ("406-555-013" of "406-555-0131") is
    covered in full. The start moves back too, but only over digits and punctuation, never over
    a label word such as "Account"."""
    matches = [m.span() for p in rules["residual_patterns"].values() for m in re.finditer(p, text)]
    extended: list[Span] = []
    for span in spans:
        start, end = span.start, span.end
        if span.source == "model" and span.label not in rules["name_labels"]:
            for m_start, m_end in matches:
                if not (m_start < end and start < m_end):
                    continue
                end = max(end, m_end)
                if m_start < start and not any(c.isalpha() for c in text[m_start:start]):
                    start = m_start
        extended.append(Span(start, end, span.label, text[start:end], span.source, span.score))
    return extended


# --- 3. Log identifiers: keys, tokens, passwords, sessions, IDs, IPs by field, cards ----------


def ipv4_valid(value: str) -> bool:
    """Four dot-separated numbers of 0 to 255: true for 198.51.100.23, false for a build number
    such as 4.18.302.7."""
    parts = value.split(".")
    return len(parts) == 4 and all(p.isdigit() and int(p) <= 255 for p in parts)


def luhn_valid(digits: str) -> bool:
    """The card-number check digit: true for a real card number's shape, false for most other
    digit runs of the same length (order IDs, trace numbers)."""
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def card_valid(value: str) -> bool:
    return luhn_valid(re.sub(r"\D", "", value))


CHECKS: dict[str, Callable[[str], bool]] = {"luhn": card_valid, "ipv4": ipv4_valid}


def value_span(match: re.Match[str]) -> tuple[int, int]:
    """The part of a match to act on: its `value` group if the pattern has one, else all of it."""
    if "value" in match.re.groupindex and match.group("value") is not None:
        return match.span("value")
    return match.span()


@dataclass
class Identifiers:
    """What the identifier rules decided for one document."""

    spans: list[Span] = field(default_factory=list)  # to mask or replace
    kept: list[tuple[int, int]] = field(default_factory=list)  # `keep` rules: left as written
    kept_values: dict[str, set[str]] = field(default_factory=dict)  # keep rule -> its values
    errors: list[str] = field(default_factory=list)  # a rule can't decide: blocked
    reviews: list[str] = field(default_factory=list)  # redacted, but a person should act

    @property
    def ruled(self) -> list[tuple[int, int]]:
        """Every range a rule decided; model spans overlapping these keep only their rest."""
        return [*self.kept, *((span.start, span.end) for span in self.spans)]


def near_word(text: str, start: int, word: str, chars: int) -> bool:
    """Whether `word` appears, as a whole word, in the `chars` characters before `start`."""
    before = text[max(0, start - chars) : start]
    return re.search(r"(?i)(?<!\w)" + re.escape(word) + r"(?!\w)", before) is not None


def identifier_spans(
    text: str, rules: dict[str, Any], learned: dict[str, set[str]] | None = None
) -> Identifiers:
    """Apply the identifier rules in order. A match whose value lies wholly inside an
    allowlisted term, or overlaps a value an earlier rule matched, is skipped: the status page's
    own IP is kept, a request ID is kept before the customer-ID rule sees it, and a host IP
    before the IP rule. A value that only touches an allowlisted term ("felix@corvane.example")
    is still acted on.

    `learned` holds the values each `keep` rule kept anywhere in the bundle. A rule with
    `keep_values_from` keeps those values too; any other rule that would redact one of them
    blocks the file, since the same value is then both one of ours and a customer's."""
    learned = learned or {}
    found = Identifiers()
    allowed = allowlisted_ranges(text, rules)
    claimed: list[tuple[int, int]] = []
    every_learned = {value for values in learned.values() for value in values}
    for label, rule in rules["identifiers"].items():
        strict: list[tuple[int, int]] = []
        for match in re.finditer(rule["pattern"], text):
            start, end = value_span(match)
            if inside(start, end, allowed) or overlaps(start, end, claimed):
                continue
            value, line = text[start:end], line_of(text, start)
            check = rule.get("check")
            if check and not CHECKS[check](value):
                word = rule.get("unless_near")
                if not (word and near_word(text, start, word, int(rule["near_chars"]))):
                    continue
                found.reviews.append(
                    f"line {line}: {label} next to '{word}' fails the {check} check; "
                    "redacted, a person checks it"
                )
            action = rule["action"]
            if action != KEEP and value in learned.get(rule.get("keep_values_from", ""), set()):
                action = KEEP
            elif action != KEEP and value in every_learned:
                found.errors.append(
                    f"line {line}: a {label} that another file logs as one of our own; the "
                    "rules can't decide it"
                )
            if action != KEEP and rule.get("review"):
                found.reviews.append(f"line {line}: {rule['review']}")
            strict.append((start, end))
            if action == KEEP:
                found.kept.append((start, end))
                found.kept_values.setdefault(label, set()).add(value)
            else:
                found.spans.append(Span(start, end, label, value, "rule"))
        claimed += strict
        for match in re.finditer(rule.get("loose") or "(?!)", text):
            if not overlaps(match.start(), match.end(), [*allowed, *claimed]):
                reason = rule.get("loose_reason") or (
                    f"{label} in an unexpected format; the {rule['action']} rule can't apply"
                )
                found.errors.append(f"line {line_of(text, match.start())}: {reason}")
    return found


def learned_keeps(documents: list[Document], rules: dict[str, Any]) -> dict[str, set[str]]:
    """The values each `keep` rule kept in any file of the bundle (the host IPs, request IDs)."""
    learned: dict[str, set[str]] = {}
    for doc in documents:
        for label, values in identifier_spans(doc.text, rules).kept_values.items():
            learned.setdefault(label, set()).update(values)
    return learned


def mask(value: str, show_last: int) -> str:
    """Replace every digit but the last `show_last` with X: "4111 1111 1111 1111" ->
    "XXXX XXXX XXXX 1111"."""
    digits = sum(char.isdigit() for char in value)
    hide = max(0, digits - show_last)
    out: list[str] = []
    for char in value:
        if char.isdigit() and hide > 0:
            out.append("X")
            hide -= 1
        else:
            out.append(char)
    return "".join(out)


# --- 4. Allowlist: own company, staff, services, vendor and addresses -------------------------


def normalize(value: str, label: str, rules: dict[str, Any]) -> str:
    """The form two mentions are compared in: digits for numbers, letters and digits for IDs,
    lower case for emails, bare words for names and everything else."""
    if label in rules["digit_labels"]:
        digits = re.sub(r"\D", "", value)
        if digits:
            return digits
    if label in rules["alnum_labels"]:
        return re.sub(r"[^0-9A-Za-z]", "", value).upper()
    if label == "email address":
        return value.strip().casefold().replace("%40", "@")  # URL-encoded or not, one address
    value = re.sub("['\N{RIGHT SINGLE QUOTATION MARK}]s\\b", "", value)  # possessive
    words = re.findall(r"[^\W_]+", value.casefold())
    while words and words[0] in rules["honorifics"]:
        words = words[1:]
    return " ".join(words)


def allowlist_terms(rules: dict[str, Any]) -> list[str]:
    return [term for terms in rules["allowlist"].values() for term in terms]


def allowlisted_ranges(text: str, rules: dict[str, Any]) -> list[tuple[int, int]]:
    return find_terms(text, allowlist_terms(rules), ignore_case=True)


def apply_allowlist(
    text: str, spans: list[Span], rules: dict[str, Any], released: Iterable[str] = ()
) -> list[Span]:
    """Drop spans that are an allowlisted term, or are made only of allowlisted terms, title words
    ("Jr.") and punctuation ("Felix Oduya (Corvane Support)"). Terms in `released` do not count
    for this file (see `released_terms` and `known_releases`)."""
    off = {normalize(term, "person", rules) for term in released}
    terms = [term for term in allowlist_terms(rules) if normalize(term, "person", rules) not in off]
    ranges = find_terms(text, terms, ignore_case=True)
    keep_names = {normalize(term, "person", rules) for term in terms}
    suffixes = set(rules["allowlist_suffixes"])

    def only_allowlisted(span: Span) -> bool:
        chars = list(span.text)
        covered = False
        for start, end in ranges:
            for offset in range(max(start, span.start), min(end, span.end)):
                chars[offset - span.start] = " "
                covered = True
        rest = re.findall(r"[^\W_]+", "".join(chars).casefold())
        return covered and all(word in suffixes for word in rest)

    return [
        span
        for span in spans
        if normalize(span.text, "person", rules) not in keep_names and not only_allowlisted(span)
    ]


def released_terms(text: str, spans: list[Span], rules: dict[str, Any]) -> list[str]:
    """One-word allowlist terms that name someone else in this file. "Felix" is on the
    allowlist for Corvane's Felix Oduya; in a file that also names a customer "Felix Brandt",
    a bare "Felix" may be the customer, so it is redacted there like any name."""
    keep = {normalize(term, "person", rules) for term in allowlist_terms(rules)}
    short = {
        term
        for term in allowlist_terms(rules)
        if len(normalize(term, "person", rules).split()) == 1
    }
    names = [
        normalize(span.text, "person", rules)
        for span in spans
        if span.label in rules["name_labels"]
    ]
    return sorted(
        term
        for term in short
        if any(
            normalize(term, "person", rules) in name.split()
            and len(name.split()) > 1
            and name not in keep
            for name in names
        )
    )


def released_is_ambiguous(text: str, term: str, rules: dict[str, Any]) -> bool:
    """Whether the document also names the allowlisted person the short term was kept for
    ("Felix Oduya" next to "Felix Brandt"), so a bare "Felix" could be either."""
    word = normalize(term, "person", rules)
    longer = [
        t
        for t in allowlist_terms(rules)
        if word in normalize(t, "person", rules).split() and normalize(t, "person", rules) != word
    ]
    return bool(find_terms(text, longer, ignore_case=True))


def known_releases(known: list[tuple[str, str]], rules: dict[str, Any]) -> list[str]:
    """Allowlist terms that collide with a known customer from the support desk: the customer's
    full name or organization, or one word of the customer's name ("Felix" for a requester
    named Felix Brandt). The known customer wins, so these are redacted in every file."""
    names = [
        normalize(value, "person", rules) for value, label in known if label != "email address"
    ]
    words = {word for name in names for word in name.split()}
    return sorted(
        term
        for term in allowlist_terms(rules)
        if (key := normalize(term, "person", rules)) in names or key in words
    )


# --- 5. Rules cut through model spans; merge overlapping spans --------------------------------


def worth_redacting(piece: str, rules: dict[str, Any]) -> bool:
    """Whether what is left of a model span, once a rule took its part, still looks like data:
    two or more words, or one word with a digit and `leftover_value_min_chars` characters.
    A lone field name ("request_id=", "session_id=") or "Bearer" is left readable."""
    words = re.findall(r"[\w-]+", piece)
    if len(words) >= 2:
        return True
    minimum = rules["thresholds"]["leftover_value_min_chars"]
    return len(words) == 1 and len(words[0]) >= minimum and any(c.isdigit() for c in words[0])


def outside_ruled(
    text: str, span: Span, ruled: list[tuple[int, int]], rules: dict[str, Any]
) -> list[Span]:
    """The parts of a span that no identifier rule decided. The rule decides its own range (a
    request ID is kept, an email gets its own placeholder); the rest of the model's span is
    still redacted, so a URL tagged whole keeps its token redacted around a ruled email."""
    pieces = [(span.start, span.end)]
    for low, high in sorted(ruled):
        cut: list[tuple[int, int]] = []
        for start, end in pieces:
            if high <= start or end <= low:
                cut.append((start, end))
                continue
            if start < low:
                cut.append((start, low))
            if high < end:
                cut.append((high, end))
        pieces = cut
    if pieces == [(span.start, span.end)]:
        return [span]
    kept: list[Span] = []
    for start, end in pieces:
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start < end and worth_redacting(text[start:end], rules):
            kept.append(
                Span(start, end, span.label, text[start:end], span.source, span.score, span.text)
            )
    return kept


def merge_spans(text: str, spans: list[Span]) -> list[Span]:
    """Union overlapping spans into one, named after the longest of them (an address beats
    the postcode inside it; an email address beats the name inside it). On a tie an identifier
    rule wins, then the model or a known customer (higher score first), then a propagated
    name."""
    merged: list[Span] = []
    group: list[Span] = []

    def close() -> None:
        if not group:
            return
        start, end = group[0].start, max(span.end for span in group)
        lead = max(group, key=lambda s: (s.end - s.start, SOURCE_RANK[s.source], s.score))
        whole = lead.whole if len(group) == 1 else ""
        merged.append(Span(start, end, lead.label, text[start:end], lead.source, whole=whole))
        group.clear()

    for span in sorted(spans, key=lambda s: (s.start, -s.end)):
        if group and span.start >= max(s.end for s in group):
            close()
        group.append(span)
    close()
    return merged


# --- 6. Placeholders: one per value across the whole bundle -----------------------------------


class Placeholders:
    """Assign [PERSON_1], [EMAIL_2], [USER_ID_1] ... so the same value reads the same in every
    file."""

    def __init__(self, rules: dict[str, Any]) -> None:
        self.rules = rules
        self.entities: list[Entity] = []
        self.by_key: dict[str, Entity] = {}
        self.counters: dict[str, int] = {}
        self.ambiguous: dict[str, list[Entity]] = {}  # name key -> the names it could be

    def prefix(self, label: str) -> str:
        names = self.rules["placeholders"]
        return str(names.get(label, names["default"]))

    def new(self, label: str) -> Entity:
        prefix = self.prefix(label)
        self.counters[prefix] = self.counters.get(prefix, 0) + 1
        entity = Entity(f"[{prefix}_{self.counters[prefix]}]", label)
        self.entities.append(entity)
        return entity

    def is_name(self, label: str) -> bool:
        return label in self.rules["name_labels"]

    def key(self, span: Span) -> str:
        text = span.whole or span.text
        value = normalize(text, span.label, self.rules) or text.casefold()
        return value if self.is_name(span.label) else f"{self.prefix(span.label)}:{value}"

    def resolve_name(self, key: str, label: str) -> tuple[Entity, list[Entity]]:
        """Link a name to the one known name it is part of ("Delphine", or "MARCHETTI, DELPHINE"
        in another order). Returns the entity and, when the name fits several, those
        candidates."""
        if key in self.by_key:
            return self.by_key[key], self.ambiguous.get(key, [])
        words = set(key.split())
        candidates = [
            e for e in self.entities if self.is_name(e.label) and e.keys and words <= e.tokens
        ]
        entity = candidates[0] if len(candidates) == 1 else self.new(label)
        entity.keys.add(key)
        self.by_key[key] = entity
        if len(candidates) > 1:
            self.ambiguous[key] = candidates
        return entity, self.ambiguous.get(key, [])

    def assign(self, documents: list[Document]) -> None:
        """Fill each document's placeholders. Longer names go first, so "Delphine" can join
        "Delphine Marchetti"; a mention that fits several names is ambiguous and sent to
        review."""
        found = [(doc, i, span) for doc in documents for i, span in enumerate(doc.spans)]
        found.sort(
            key=lambda item: -len(self.key(item[2]).split()) if self.is_name(item[2].label) else 0
        )
        for document in documents:
            document.placeholders = [""] * len(document.spans)
        for document, index, span in found:
            key = self.key(span)
            if self.is_name(span.label):
                entity, others = self.resolve_name(key, span.label)
                if others:
                    choices = ", ".join(e.placeholder for e in others)
                    document.reviews.append(
                        f"line {line_of(document.text, span.start)}: a name that could be "
                        f"{choices}; redacted as {entity.placeholder}"
                    )
            else:
                entity = self.by_key.get(key) or self.new(span.label)
                entity.keys.add(key)
                self.by_key[key] = entity
            entity.add(span.text, document.name)
            document.placeholders[index] = entity.placeholder


def replacement(span: Span, placeholder: str, rules: dict[str, Any]) -> str:
    """What a span becomes in the text: a masked number for a `mask` identifier rule, its
    placeholder for everything else."""
    rule = rules["identifiers"].get(span.label)
    if span.source == "rule" and rule and rule["action"] == MASK:
        return mask(span.text, int(rule["show_last"]))
    return placeholder


# --- 7. Redact, 8. final sweep ----------------------------------------------------------------


def redact(text: str, spans: list[Span], replacements: list[str]) -> str:
    """Replace each span with its replacement, last span first so offsets stay valid."""
    for span, new in sorted(
        zip(spans, replacements, strict=True), reverse=True, key=lambda pair: pair[0].start
    ):
        text = text[: span.start] + new + text[span.end :]
    return text


def residual_sweep(
    redacted: str, rules: dict[str, Any], kept: Iterable[tuple[int, int]] = ()
) -> list[str]:
    """Look for data that must never go out readable, with regex and a Luhn check, after
    blanking every [placeholder]. Each hit blocks the file; the reason names the pattern and
    line, never the value. `kept` are ranges in the redacted text kept on purpose (identifier
    `keep` rules)."""
    masked = PLACEHOLDER.sub(lambda match: " " * len(match.group(0)), redacted)
    ranges = [*allowlisted_ranges(masked, rules), *kept]
    hits: list[tuple[int, str]] = []
    for name, pattern in rules["residual_patterns"].items():
        for match in re.finditer(pattern, masked):
            if not inside(match.start(), match.end(), ranges):
                hits.append((match.start(), name))
    for match in re.finditer(rules["card_number"]["pattern"], masked):
        if luhn_valid(re.sub(r"\D", "", match.group())) and not inside(
            match.start(), match.end(), ranges
        ):
            hits.append((match.start(), "card_number"))
    return [f"line {line_of(masked, start)}: {name} still readable" for start, name in sorted(hits)]


def kept_in_redacted(document: Document) -> list[tuple[int, int]]:
    """Move the kept identifier ranges from original to redacted offsets."""
    moved: list[tuple[int, int]] = []
    for start, end in document.kept:
        shift = sum(
            len(new) - (span.end - span.start)
            for span, new in zip(document.spans, document.replacements, strict=True)
            if span.end <= start
        )
        moved.append((start + shift, end + shift))
    return moved


# --- Run --------------------------------------------------------------------------------------


@dataclass
class Result:
    documents: list[Document]
    entities: list[Entity]
    calls: int


def run(
    client: SIEClient,
    rules: dict[str, Any],
    documents: list[Document],
    known: list[tuple[str, str]],
) -> Result:
    """Clean every file of the bundle. Stops with SIEStopped, before anything is written, on a
    refusal or when every SIE call failed."""
    calls = CallLog()
    released_known = known_releases(known, rules)
    found = {
        doc.name: apply_allowlist(
            doc.text, detect_pii(client, calls, doc, rules), rules, released_known
        )
        for doc in documents
    }
    calls.stop_if_all_failed()
    if calls.failures:
        print(
            f"{len(calls.failures)} of {calls.made} SIE calls failed; those files are blocked. "
            f"First: {short(calls.failures[0][1])}"
        )
    customers = {doc.name: known_mentions(doc.text, known) for doc in documents}
    names = {
        span.text: span.label
        for spans in [*found.values(), *customers.values()]
        for span in spans
        if span.label in rules["name_labels"]
    }
    names |= {value: label for value, label in known if label in rules["name_labels"]}
    people = {
        name: "person"
        for name in person_names([n for n, label in names.items() if label == "person"], rules)
    }
    min_letters = rules["thresholds"]["propagate_min_letters"]
    learned = learned_keeps(documents, rules)
    ruled: dict[str, Identifiers] = {}
    for doc in documents:
        identifiers = ruled[doc.name] = identifier_spans(doc.text, rules, learned)
        doc.kept = identifiers.kept
        doc.errors.extend(identifiers.errors)
        doc.reviews.extend(identifiers.reviews)
        for start, _ in find_terms(doc.text, released_known, ignore_case=True)[:1]:
            doc.reviews.append(
                f"line {line_of(doc.text, start)}: a name on the allowlist is also a known "
                "customer's; redacted, check who is meant"
            )
        spans = (
            found[doc.name] + customers[doc.name] + propagate_names(doc.text, names, min_letters)
        )
        spans += name_form_spans(doc.text, people, rules, released_known)
        doc.spans = spans
    values = {
        key: span
        for doc in documents
        for span in [*doc.spans, *ruled[doc.name].spans]
        if (key := spelling_key(span, rules)) is not None
    }
    for doc in documents:
        identifiers = ruled[doc.name]
        released = released_terms(doc.text, doc.spans, rules)
        spans = doc.spans + second_spellings(doc.text, values, rules)
        for term in released:
            spans += [
                Span(start, end, "person", doc.text[start:end], "name_form")
                for start, end in find_terms(doc.text, [term], ignore_case=True)
            ]
            if released_is_ambiguous(doc.text, term, rules):
                line = line_of(doc.text, find_terms(doc.text, [term], ignore_case=True)[0][0])
                doc.reviews.append(
                    f"line {line}: a short allowlisted name that is also part of another "
                    "person's name here; redacted, check who is meant"
                )
        spans = extend_to_patterns(doc.text, spans, rules)
        spans = apply_allowlist(doc.text, spans, rules, [*released, *released_known])
        # The identifier rules decide their own ranges, whatever the model tagged there: a
        # request ID and a host IP stay readable, a card is masked. The rest of a model span
        # that a rule cut through is still redacted.
        spans = [
            piece for s in spans for piece in outside_ruled(doc.text, s, identifiers.ruled, rules)
        ]
        doc.spans = merge_spans(doc.text, spans + identifiers.spans)
    placeholders = Placeholders(rules)
    placeholders.assign(documents)
    for doc in documents:
        doc.replacements = [
            replacement(span, placeholder, rules)
            for span, placeholder in zip(doc.spans, doc.placeholders, strict=True)
        ]
        doc.redacted = redact(doc.text, doc.spans, doc.replacements)
        doc.leaks = residual_sweep(doc.redacted, rules, kept_in_redacted(doc))
    entities = [entity for entity in placeholders.entities if entity.values]
    return Result(documents, entities, calls.made)


def mapping_json(result: Result) -> dict[str, Any]:
    """The mapping back to real values. It holds every original value and secret: keep it on
    the engineer's machine, apart from the cleaned files, and never attach it."""
    return {
        "entities": {
            entity.placeholder: {
                "label": entity.label,
                "values": entity.values,
                "documents": entity.documents,
            }
            for entity in result.entities
        },
        "documents": {
            doc.name: [
                {
                    "line": line_of(doc.text, s.start),
                    "start": s.start,
                    "end": s.end,
                    "text": s.text,
                    "label": s.label,
                    "source": s.source,
                    "replacement": r,
                }
                for s, r in zip(doc.spans, doc.replacements, strict=True)
            ]
            for doc in result.documents
        },
    }


def csv_safe(row: dict[str, Any]) -> dict[str, Any]:
    """Quote a cell that a spreadsheet would run as a formula ("=HYPERLINK(...)") with a '."""
    return {
        key: f"'{value}" if isinstance(value, str) and value.startswith(FORMULA_START) else value
        for key, value in row.items()
    }


def by_line(reasons: list[str]) -> list[str]:
    """Reasons in line order; one without a line number (a window not read) comes first."""
    found = [re.match(r"line (\d+):", reason) for reason in reasons]
    order = [int(match.group(1)) if match else 0 for match in found]
    return [reason for _, reason in sorted(zip(order, reasons, strict=True), key=lambda p: p[0])]


def report_rows(result: Result) -> list[dict[str, Any]]:
    """One row per file: its status, what replaced what, and every reason (blocking ones first),
    by line number, never the value."""
    return [
        {
            "document": doc.name,
            "status": doc.status,
            "redactions": len(doc.spans),
            "placeholders": " ".join(sorted(set(doc.placeholders))),
            "reasons": " | ".join([*by_line(doc.errors + doc.leaks), *by_line(doc.reviews)]),
        }
        for doc in result.documents
    ]


def redaction_rows(result: Result) -> list[dict[str, Any]]:
    """What was taken out of each file and why, by line: the label and what replaced it, never
    the value itself."""
    return [
        {
            "document": doc.name,
            "line": line_of(doc.text, span.start),
            "what": span.label,
            "replaced_with": new,
            "found_by": FOUND_BY[span.source],
        }
        for doc in result.documents
        for span, new in zip(doc.spans, doc.replacements, strict=True)
    ]


def write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(csv_safe(row) for row in rows)


def write_outputs(output_dir: Path, result: Result) -> None:
    """Each cleaned file under the folder named after its status; the reports; and private/,
    which holds the mapping and the originals and never goes to the vendor."""
    for folder in (*STATUSES, "private/original"):
        (output_dir / folder).mkdir(parents=True, exist_ok=True)
    for doc in result.documents:
        (output_dir / doc.status / doc.name).write_text(doc.redacted, encoding="utf-8")
        (output_dir / "private" / "original" / doc.name).write_text(doc.text, encoding="utf-8")
    (output_dir / "private" / "mapping.json").write_text(
        json.dumps(mapping_json(result), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    write_csv(output_dir / "report.csv", REPORT_FIELDS, report_rows(result))
    write_csv(output_dir / "redactions.csv", REDACTION_FIELDS, redaction_rows(result))


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rules, case = load_rules(), load_case()
    documents, known = build_bundle(case), known_customers(case)
    flag_no_ticket(case, known, documents)
    with make_client() as client:
        result = stop_on_refusal(lambda: run(client, rules, documents, known))
    write_outputs(output, result)
    for row in report_rows(result):
        print(f"\n{row['document']:<20} {row['status']:<18} {row['redactions']:>3} redactions")
        for reason in row["reasons"].split(" | ") if row["reasons"] else []:
            print(f"    {reason}")
    print(f"\n{result.calls} SIE calls. Nothing is sent to {case.vendor}: the on-call engineer")
    print("compares each original with its cleaned copy and attaches the ones they approve.")
    print(f"cleaned files: {output}/{{{','.join(STATUSES)}}}")
    print(f"report: {output / 'report.csv'}, {output / 'redactions.csv'}")
    print(f"mapping and originals (keep apart, never attach): {output / 'private'}")


if __name__ == "__main__":
    main()
