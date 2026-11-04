"""Pseudonymize clinical notes for an approved data request and log every replacement.

Use case:  An approved research or quality-improvement data request for clinical notes, filled
           by the honest broker: every identifier replaced, dates shifted, a log of what was
           replaced, and the key kept apart.
Who:       The analyst in the health system's research data team (the honest broker) who fills
           the request after the IRB or the privacy office approved it.
Problem:   The structured fields come out of the warehouse easily; the notes carry names, MRNs,
           dates, addresses and phone numbers of the patient and the people around them, written
           anywhere in the narrative. Deleting every date ruins the study; leaving one name in
           releases a patient.
Input:     The approved request in data/request.yaml (approval reference, cohort, note types,
           date window, release rules); the EHR (ehr.py, seeded from data/ehr_*.csv and
           data/notes/): each cohort patient's known identifiers and their notes by type. The
           allowlist, patterns, sweep and thresholds are in rules.yaml.
SIE pipeline (one model):
           extract  superlinked/pii-redact  called once per window of at most 300 words: names,
                                            employers, addresses, phones, emails, plan IDs in
                                            the narrative
           (Python adds regex and the EHR's known identifiers, checks each note against its
           patient, carries each identifier to every note, shifts dates by an offset drawn for
           this request, applies the release rules and sweeps the result.)
Output:    runs/<timestamp>/: extract/ (one file per note and manifest.csv), held/ (notes that
           escalated), replacement-log.csv (line, category and placeholder, never the value),
           note-status.csv (status and reasons per note) and private/key.json (placeholders,
           study IDs and date offsets; it never travels with the extract). Every status names
           what the checks found; the honest broker signs off.
Why SIE:   The PII model finds what no EHR field holds: the daughter's name, the son-in-law's
           employer, a granddaughter's email. No generative model makes the decision; the rules
           are plain Python you can read below.
Run using: SIE Serverless, or SIE Cluster or SIE Local with SIE_BASE_URL.

    uv run recipe.py
    uv run recipe.py --request data/second_request.yaml
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import ehr
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
REQUEST_PATH = DATA_DIR / "request.yaml"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
# How much of an SIE error message goes into the summary line.
ERROR_CHARS = 200

# What the checks found, never a decision to release: the honest broker checks every note
# against its original and signs the extract off (or the privacy officer does).
CHECKS_PASSED, REVIEW, ESCALATE = "checks_passed", "review", "escalate"
# What a release may keep, per the approved request.
RELEASE_OPTIONS = {
    "dates": ("shift", "remove"),
    "zip": ("three_digits", "remove"),
    "ages_over_89": ("group_90_plus", "remove"),
}
PATIENT_LABEL = "patient"
DATE_LABEL = "date"
BIRTH_LABEL = "date of birth"
ZIP_LABEL = "postal code"
AGE_LABEL = "age over 89"
MRN_LABEL = "medical record number"
MANIFEST_FIELDS = (
    *("request_id", "approval_reference", "study_id", "note_file", "note_type", "note_date"),
)
STATUS_FIELDS = ("note_file", "study_id", "note_type", "status", "replacements", "reasons")
LOG_FIELDS = ("note_file", "status", "line", "category", "placeholder", "found_by")
# A cell starting with one of these runs as a formula when a spreadsheet opens the CSV.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
PLACEHOLDER = re.compile(r"\[([A-Z][A-Z_]*_\d+)\]")
SHIFTED_DATE = re.compile(r"\[DATE (\d{4}-\d{2}-\d{2})\]")
# On spans of equal length, the more specific source names the merged span.
SOURCE_RANK = {
    "pattern": 3,
    "ehr": 2,
    "model": 1,
    "propagated": 0,
    "name_form": 0,
    "second_spelling": -1,
}
FOUND_BY = {
    "pattern": "regex pattern",
    "ehr": "EHR known identifier",
    "model": "PII model",
    "propagated": "name found in another note",
    "name_form": "another form of a known name",
    "second_spelling": "same value found elsewhere",
}


@dataclass(frozen=True)
class Span:
    """One stretch of a note to replace. Offsets are Python code points."""

    start: int
    end: int
    label: str
    text: str
    source: str  # a key of SOURCE_RANK
    whole: str = ""  # for a piece of a model span a pattern cut through: the span's full text


@dataclass
class Entity:
    """One real-world thing behind a placeholder, with every way the notes write it."""

    placeholder: str
    label: str
    values: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    keys: set[str] = field(default_factory=set)

    @property
    def tokens(self) -> set[str]:
        return {token for key in self.keys for token in key.split()}

    def add(self, value: str, note: str) -> None:
        if value not in self.values:
            self.values.append(value)
        if note not in self.notes:
            self.notes.append(note)


@dataclass(frozen=True)
class Request:
    """An approved data request, as the request system hands it to the honest broker."""

    request_id: str
    approval_reference: str
    approved_by: str
    route: str
    cohort: tuple[str, ...]
    note_types: tuple[str, ...]
    start: date
    end: date
    release: dict[str, str]


@dataclass
class Work:
    """One note on its way through the recipe."""

    note: ehr.Note
    patient: ehr.Patient
    file: str  # the note's name in the extract: study ID, sequence, note type
    offset_days: int | None = None  # None: the dates can't be shifted for this note
    spans: list[Span] = field(default_factory=list)
    placeholders: list[str] = field(default_factory=list)  # one per span, same order
    redacted: str = ""
    ours: list[tuple[int, int]] = field(default_factory=list)  # placeholders we wrote
    leaks: list[str] = field(default_factory=list)  # escalate: something may be readable
    errors: list[str] = field(default_factory=list)  # escalate: not read, misfiled, a date
    reviews: list[str] = field(default_factory=list)  # review: a person picks

    @property
    def text(self) -> str:
        return self.note.text

    @property
    def status(self) -> str:
        if self.leaks or self.errors:
            return ESCALATE
        if self.reviews:
            return REVIEW
        return CHECKS_PASSED


def load_yaml(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return load_yaml(path)


def as_date(value: object) -> date:
    """YAML gives a date for an unquoted 2026-09-01 and a string for a quoted one."""
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def load_request(path: Path = REQUEST_PATH) -> Request:
    """The approved request. A release rule the recipe doesn't know, or an empty cohort or
    window, stops here, before any SIE call."""
    raw = load_yaml(path)
    release = {key: str(value) for key, value in (raw.get("release") or {}).items()}
    for key, allowed in RELEASE_OPTIONS.items():
        if release.get(key) not in allowed:
            raise ValueError(f"release.{key} is {release.get(key)!r}; use one of {allowed}")
    unknown = sorted(set(release) - set(RELEASE_OPTIONS))
    if unknown:
        raise ValueError(f"release rules the recipe doesn't know: {unknown}")
    window = raw["date_window"]
    request = Request(
        request_id=str(raw["request_id"]),
        approval_reference=str(raw["approval"]["reference"]),
        approved_by=str(raw["approval"]["approved_by"]),
        route=str(raw["approval"].get("route", "")),
        cohort=tuple(str(pid) for pid in raw["cohort"]),
        note_types=tuple(str(kind) for kind in raw["note_types"]),
        start=as_date(window["start"]),
        end=as_date(window["end"]),
        release=release,
    )
    if not request.cohort or not request.note_types:
        raise ValueError("the request names no cohort or no note types")
    if request.end < request.start:
        raise ValueError(f"date window ends {request.end} before it starts {request.start}")
    return request


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
    those notes escalate as not read."""
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
    """The error for a per-note reason or the summary line, its message cut short."""
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
        """No call succeeded, so nothing was read: stop instead of writing empty outputs."""
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
    a rate limit, a reply without the key we asked for) is logged and its note escalates as not
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
        # The reason lands in note-status.csv, so it names the error class, never SIE's text.
        return None, f"the SIE call failed: {describe(error)}; the run summary has the message"


# --- 1. Find identifiers in the narrative: the PII model on windows ---------------------------


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


def detect_pii(client: SIEClient, calls: CallLog, work: Work, rules: dict[str, Any]) -> list[Span]:
    """Send each window to the PII model and return its spans in note offsets. A window whose
    call failed escalates the note as not read; the other windows still count."""
    model, limits = rules["models"]["pii"], rules["thresholds"]
    labels = list(rules["labels"])
    windows = word_windows(work.text, limits["window_words"], limits["window_overlap_words"])
    spans: list[Span] = []
    for index, (offset, window) in enumerate(windows, start=1):
        item = Item(id=f"pii/{work.note.note_id}/w{index}", text=window)

        def call(item: Item = item) -> list[dict[str, Any]]:
            reply: Any = jsonable(
                client.extract(
                    model,
                    item,
                    labels=labels,
                    # A window too long to read whole fails (InputTooLongError, the transient
                    # path: the note escalates as not read) instead of being cut short.
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
            work.errors.append(f"window {index} was not read: {failed}")
            continue
        for entity in entities:
            if not isinstance(entity, dict) or not valid_entity(window, entity):
                work.errors.append(f"window {index}: the model returned an invalid span")
                continue
            start, end = offset + entity["start"], offset + entity["end"]
            spans.append(Span(start, end, entity["label"], work.text[start:end], "model"))
    return spans


# --- 2. Regex: MRN, date of birth, phone, ZIP, plan IDs, ages over 89 and dates ---------------


def date_regex(rules: dict[str, Any]) -> str:
    return "|".join(f"(?:{fmt['regex']})" for fmt in rules["date_formats"])


def parse_date(value: str, rules: dict[str, Any]) -> date | None:
    """A full date in one of the formats in rules.yaml, or None."""
    for fmt in rules["date_formats"]:
        if re.fullmatch(fmt["regex"], value):
            try:
                return datetime.strptime(value, fmt["format"]).date()
            except ValueError:
                return None
    return None


def value_span(match: re.Match[str]) -> tuple[int, int]:
    """The part of a match to replace: its first `value*` group that matched, else all of it."""
    for name, value in match.groupdict().items():
        if name.startswith("value") and value is not None:
            return match.span(name)
    return match.span()


def clinical_ranges(text: str, rules: dict[str, Any]) -> list[tuple[int, int]]:
    """Blood pressures and dose fractions ("BP 98/64", "1/2 tablet"): kept as written, and never
    read as a date by the date rules or the sweep."""
    return [m.span() for p in rules["clinical_numbers"].values() for m in re.finditer(p, text)]


def pattern_matches(text: str, rules: dict[str, Any]) -> list[tuple[str, re.Match[str]]]:
    """Every regex match as (label, match): the patterns, then full dates labelled `date`, then
    dates the rules can't shift (no year, a two-digit year, "3 Oct 2026"), also as `date`. A
    clinical number is never a date."""
    dates = date_regex(rules)
    clinical = clinical_ranges(text, rules)
    found = [
        (pattern["label"], match)
        for pattern in rules["patterns"].values()
        for match in re.finditer(pattern["regex"].replace("{date}", dates), text)
    ]
    full = [(DATE_LABEL, match) for match in re.finditer(dates, text)]
    odd = [
        (DATE_LABEL, match)
        for regex in rules["unshiftable_dates"]
        for match in re.finditer(regex, text)
        if not overlaps(*match.span(), [m.span() for _, m in full])
        and not overlaps(*match.span(), clinical)
    ]
    return found + full + odd


def pattern_spans(text: str, rules: dict[str, Any], births: Iterable[date] = ()) -> list[Span]:
    """Identifiers the rules find with regex. A date that is a date of birth (labelled "DOB", or
    equal to a date of birth the EHR has) is removed as a date of birth, never shifted."""
    known = set(births)
    spans: list[Span] = []
    for label, match in pattern_matches(text, rules):
        start, end = value_span(match)
        if label == DATE_LABEL and parse_date(text[start:end], rules) in known:
            label = BIRTH_LABEL
        spans.append(Span(start, end, label, text[start:end], "pattern"))
    labelled = [s for s in spans if s.label == BIRTH_LABEL]
    return [
        span
        for span in spans
        if span.label != DATE_LABEL
        or not overlaps(span.start, span.end, [(b.start, b.end) for b in labelled])
    ]


def ruled_ranges(text: str, rules: dict[str, Any]) -> list[tuple[int, int]]:
    """The whole stretch each regex match decides, label included ("MRN: 00482913")."""
    return [match.span() for _, match in pattern_matches(text, rules)]


# --- 3. The EHR: the note's patient, their known identifiers --------------------------------


def find_terms(text: str, terms: Iterable[str], *, ignore_case: bool) -> list[tuple[int, int]]:
    """Every whole-word occurrence of any term, as (start, end)."""
    flags = re.IGNORECASE if ignore_case else 0
    found: list[tuple[int, int]] = []
    for term in terms:
        if not term.strip():
            continue
        pattern = r"(?<!\w)" + re.escape(term) + r"(?!\w)"
        found.extend(match.span() for match in re.finditer(pattern, text, flags))
    return found


def patient_names(patient: ehr.Patient) -> list[str]:
    return [patient.name, *patient.aliases]


def known_spans(text: str, patient: ehr.Patient, label: str, rules: dict[str, Any]) -> list[Span]:
    """Every identifier the EHR holds for the patient, wherever the note writes it: the name and
    aliases (any case), the MRN and phone in any spelling, the street, city and ZIP, and the other
    identifiers on file (a plan member ID). The date of birth is matched in `pattern_spans`."""
    found: list[tuple[int, int, str]] = [
        (start, end, label)
        for start, end in find_terms(text, patient_names(patient), ignore_case=True)
    ]
    for value, value_label, phone in (
        (patient.mrn, MRN_LABEL, False),
        (patient.phone, "phone number", True),
    ):
        digits = re.sub(r"\D", "", value)
        if digits:
            pattern = digits_pattern(phone_national(digits) if phone else digits, phone=phone)
            found += [(*m.span(), value_label) for m in re.finditer(pattern, text)]
    for value, value_label in (
        (patient.street, "street address"),
        (patient.city, "city"),
        (patient.zip, ZIP_LABEL),
    ):
        found += [(s, e, value_label) for s, e in find_terms(text, [value], ignore_case=True)]
    labels = rules["ehr_identifier_labels"]
    for kind, value in patient.other:
        kind_label = str(labels.get(kind, labels["default"]))
        found += [(s, e, kind_label) for s, e in find_terms(text, [value], ignore_case=True)]
    return [Span(start, end, lab, text[start:end], "ehr") for start, end, lab in found]


def name_fits(value: str, patient: ehr.Patient, rules: dict[str, Any]) -> bool:
    """Whether the name in a patient field is this patient: the part before the first comma, or
    before the second ("VANCE, TEODORA, MRN ..."), has the same words as the EHR name or an alias,
    in any order. A part of the name ("Vance", "T. Vance") doesn't count, since it could be
    someone else."""
    parts = value.split(",")
    tries = [parts[0], " ".join(parts[:2])]
    names = [set(normalize(name, "person", rules).split()) for name in patient_names(patient)]
    return any(set(normalize(part, "person", rules).split()) in names for part in tries)


def check_identity(
    work: Work, spans: list[Span], records: ehr.Ehr, rules: dict[str, Any]
) -> tuple[bool, list[ehr.Patient]]:
    """Check the note's text against the patient the export files it under.

    An MRN in the text that isn't the patient's, or a patient field ("Patient:", "Re:") naming
    someone else, means the note may be misfiled: its dates would move by the wrong patient's
    offset and the wrong patient's text would go out. The note escalates and its dates are
    removed, not shifted. Returns whether the note may be misfiled, and the other EHR patients
    its MRNs belong to, so their known identifiers are replaced too."""
    text, own = work.text, ehr.mrn_key(work.patient.mrn)
    problems: list[str] = []
    others: dict[str, ehr.Patient] = {}
    seen: set[tuple[int, int]] = set()
    for span in sorted(spans, key=lambda s: s.start):
        if span.label != MRN_LABEL or (span.start, span.end) in seen:
            continue
        seen.add((span.start, span.end))
        if ehr.mrn_key(span.text) == own:
            continue
        found = [
            p for p in records.find_by_mrn(span.text) if p.patient_id != work.patient.patient_id
        ]
        whose = "another EHR patient's" if found else "one the EHR doesn't have"
        problems.append(
            f"line {line_of(text, span.start)}: an MRN that isn't this patient's ({whose})"
        )
        others.update({p.patient_id: p for p in found})
    for match in re.finditer(rules["patient_name_fields"], text):
        if not name_fits(match.group("value"), work.patient, rules):
            problems.append(
                f"line {line_of(text, match.start())}: a patient field names someone other than "
                "the EHR patient"
            )
    if problems:
        work.errors += [
            *problems,
            "possibly misfiled under this patient: dates were removed, not shifted; held out",
        ]
    return bool(problems), list(others.values())


# --- 4. Carry each identifier to every note and spelling --------------------------------------


def propagate_names(text: str, names: dict[str, str], min_letters: int) -> list[Span]:
    """Replace every other mention of a name the model found in any note."""
    spans: list[Span] = []
    for name, label in names.items():
        if sum(char.isalpha() for char in name) < min_letters:
            continue
        for start, end in find_terms(text, [name], ignore_case=False):
            spans.append(Span(start, end, label, text[start:end], "propagated"))
    return spans


def person_names(names: Iterable[str], rules: dict[str, Any]) -> list[str]:
    """The names in `names` that look like a person: two or more words, none of them a company
    word ("LLC", "Laboratories") from `company_words`."""
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
    in `released` (a cohort patient's name wins over the allowlist). A name word that two known
    people share is still replaced; the placeholder step finds it fits both and sends the note
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
    without its country code), lower case for emails, the text as written for the
    `propagate_labels` (addresses). None for everything else: names have `propagate_names`, and
    other labels are left to the model, so one misread doesn't spread to every note. Values too
    short to search for safely are skipped."""
    if span.label in rules["digit_labels"]:
        digits = re.sub(r"\D", "", span.text)
        if len(digits) < rules["thresholds"]["second_spelling_min_digits"]:
            return None
        return phone_national(digits) if span.label in rules["phone_labels"] else digits
    if span.label == "email address":
        return span.text.strip().casefold()
    if span.label not in rules["propagate_labels"]:
        return None
    if sum(char.isalnum() for char in span.text) < rules["thresholds"]["propagate_min_letters"]:
        return None
    return span.text


def second_spellings(text: str, values: dict[str, Span], rules: dict[str, Any]) -> list[Span]:
    """Every other mention of a value found in any note: the same phone or MRN written with
    other separators or a country code, the same email in other capitals, the same address in
    another note. `values` maps each value's comparison key (see `spelling_key`) to a span that
    found it."""
    spans: list[Span] = []
    for key, found in values.items():
        if found.label in rules["digit_labels"]:
            pattern = digits_pattern(key, phone=found.label in rules["phone_labels"])
            ranges = [match.span() for match in re.finditer(pattern, text)]
        else:
            ranges = find_terms(text, [key], ignore_case=found.label == "email address")
        spans += [Span(s, e, found.label, text[s:e], "second_spelling") for s, e in ranges]
    return spans


def extend_to_patterns(text: str, spans: list[Span], rules: dict[str, Any]) -> list[Span]:
    """Stretch a model span (not a name) that stops inside a `residual_patterns` match to the end
    of that match, so a value cut one character short ("617-555-014" of "617-555-0142") is
    covered in full. The start moves back too, but only over digits and punctuation, never over
    a label word such as "Phone"."""
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
        extended.append(Span(start, end, span.label, text[start:end], span.source))
    return extended


# --- 5. Allowlist: the facility, its clinicians, disease eponyms ------------------------------


def normalize(value: str, label: str, rules: dict[str, Any]) -> str:
    """The form two mentions are compared in: digits for numbers, bare words for names."""
    if label in rules["digit_labels"]:
        digits = re.sub(r"\D", "", value)
        if digits:
            return digits
    if label == "email address":
        return value.strip().casefold()
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
    """Drop spans that are an allowlisted term, or are made only of allowlisted terms, credentials
    ("MD") and punctuation ("Selwyn Achebe, MD"). Terms in `released` don't count here (see
    `released_terms` and `known_releases`)."""
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
    """One-word allowlist terms that name someone else in this note. "Achebe" is on the allowlist
    for Dr. Selwyn Achebe; in a note that also names a relative "Ngozi Achebe", a bare "Achebe"
    may be the relative, so it is replaced there like any name."""
    keep = {normalize(term, "person", rules) for term in allowlist_terms(rules)}
    short_terms = {
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
        for term in short_terms
        if any(
            normalize(term, "person", rules) in name.split()
            and len(name.split()) > 1
            and name not in keep
            for name in names
        )
    )


def released_is_ambiguous(text: str, term: str, rules: dict[str, Any]) -> bool:
    """Whether the note also names the allowlisted person the short term was kept for ("Selwyn
    Achebe" next to "Ngozi Achebe"), so a bare "Achebe" could be either."""
    word = normalize(term, "person", rules)
    longer = [
        t
        for t in allowlist_terms(rules)
        if word in normalize(t, "person", rules).split() and normalize(t, "person", rules) != word
    ]
    return bool(find_terms(text, longer, ignore_case=True))


def known_releases(patients: Iterable[ehr.Patient], rules: dict[str, Any]) -> list[str]:
    """Allowlist terms that collide with a cohort patient: the patient's full name, or one word
    of it ("Achebe" for a patient named Ngozi Achebe). The patient wins, so these are replaced in
    every note."""
    names = [normalize(name, "person", rules) for p in patients for name in patient_names(p)]
    words = {word for name in names for word in name.split()}
    return sorted(
        term
        for term in allowlist_terms(rules)
        if (key := normalize(term, "person", rules)) in names or key in words
    )


# --- 6. Patterns decide their own stretch; merge overlapping spans ---------------------------


def worth_replacing(piece: str, rules: dict[str, Any]) -> bool:
    """Whether what is left of a span, once a pattern took its part, still looks like data: two
    or more words, or one word with a digit and `leftover_value_min_chars` characters. A lone
    label ("MRN", "DOB") is left readable."""
    words = re.findall(r"[\w-]+", piece)
    if len(words) >= 2:
        return True
    minimum = rules["thresholds"]["leftover_value_min_chars"]
    return len(words) == 1 and len(words[0]) >= minimum and any(c.isdigit() for c in words[0])


def outside_ruled(
    text: str, span: Span, ruled: list[tuple[int, int]], rules: dict[str, Any]
) -> list[Span]:
    """The parts of a span that no regex pattern decided. The pattern decides its own stretch
    (the date is shifted, "MRN:" stays readable); the rest of the model's span is still
    replaced, so "Ines Vance (phone 508-555-0164)" tagged as one span keeps the name replaced."""
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
        while start < end and text[start] in " ,;-()\t\n":
            start += 1
        while end > start and text[end - 1] in " ,;-()\t\n":
            end -= 1
        if start < end and worth_replacing(text[start:end], rules):
            kept.append(Span(start, end, span.label, text[start:end], span.source, span.text))
    return kept


def merge_spans(text: str, spans: list[Span]) -> list[Span]:
    """Union overlapping spans into one, named after the longest of them (an address beats the
    city inside it). On a tie a regex pattern wins, then the EHR, then the model."""
    merged: list[Span] = []
    group: list[Span] = []

    def close() -> None:
        if not group:
            return
        start, end = group[0].start, max(span.end for span in group)
        lead = max(group, key=lambda s: (s.end - s.start, SOURCE_RANK[s.source]))
        whole = lead.whole if len(group) == 1 else ""
        merged.append(Span(start, end, lead.label, text[start:end], lead.source, whole))
        group.clear()

    for span in sorted(spans, key=lambda s: (s.start, -s.end)):
        if group and span.start >= max(s.end for s in group):
            close()
        group.append(span)
    close()
    return merged


# --- 7. Draw this request's pseudonyms; replace and generalize --------------------------------


@dataclass(frozen=True)
class Draw:
    """What this request gets and no other: a study ID and a date offset per patient, and the
    order the patients are numbered in ([PATIENT_1], [PATIENT_2]). All of it is random, none of
    it derived from an identifier, and it is kept only in the private key."""

    study_ids: dict[str, str]
    offsets: dict[str, int]
    order: tuple[str, ...]


def draw_pseudonyms(cohort: Iterable[str], rules: dict[str, Any], rng: random.Random) -> Draw:
    """Draw study IDs, date offsets and the patient numbering for one request."""
    shift, ids = rules["date_shift"], rules["study_id"]
    low, high = int(shift["min_days"]), int(shift["max_days"])
    patients = list(dict.fromkeys(cohort))
    study_ids: dict[str, str] = {}
    for patient_id in patients:
        while True:
            study_id = ids["prefix"] + "".join(
                rng.choice(ids["alphabet"]) for _ in range(int(ids["length"]))
            )
            if study_id not in study_ids.values():
                break
        study_ids[patient_id] = study_id
    offsets = {pid: rng.randint(low, high) * rng.choice((-1, 1)) for pid in patients}
    order = list(patients)
    rng.shuffle(order)
    return Draw(study_ids, offsets, tuple(order))


def generalize(
    span: Span, offset_days: int | None, release: dict[str, str], rules: dict[str, Any]
) -> str | None:
    """What the request's release rules keep of a value: a shifted date, the first three ZIP
    digits (000 for a restricted area), or "90+" for an age over 89. None means the span gets an
    ordinary placeholder instead. Safe Harbor keeps no element of a date but the year, so a note
    with shifted dates is not a Safe Harbor release; the request says what this release keeps."""
    if span.label == ZIP_LABEL and release["zip"] == "three_digits":
        digits = re.sub(r"\D", "", span.text)
        if len(digits) in (5, 9):
            prefix = digits[:3]
            return f"[ZIP {'000' if prefix in rules['zip_restricted_prefixes'] else prefix}]"
    if span.label == AGE_LABEL and release["ages_over_89"] == "group_90_plus":
        return "[AGE 90+]"
    if span.label == DATE_LABEL and release["dates"] == "shift" and offset_days is not None:
        parsed = parse_date(span.text, rules)
        if parsed is not None:
            return f"[DATE {(parsed + timedelta(days=offset_days)).isoformat()}]"
    return None


class Placeholders:
    """Assign [PATIENT_1], [PERSON_2], [MRN_1] ... so the same entity reads the same in every
    note of the extract."""

    def __init__(self, rules: dict[str, Any]) -> None:
        self.rules = rules
        self.entities: list[Entity] = []
        self.by_key: dict[str, Entity] = {}
        self.counters: dict[str, int] = {}
        self.ambiguous: dict[str, list[Entity]] = {}  # name key -> the names it could be
        self.patients: dict[str, Entity] = {}  # EHR patient ID -> entity

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

    def add_patients(self, patients: list[ehr.Patient]) -> None:
        """Number the cohort's patients in the order drawn for this request."""
        for patient in patients:
            entity = self.new(PATIENT_LABEL)
            self.patients[patient.patient_id] = entity
            for name in patient_names(patient):
                key = normalize(name, PATIENT_LABEL, self.rules)
                entity.keys.add(key)
                self.by_key[key] = entity

    def resolve_name(self, key: str, label: str) -> tuple[Entity, list[Entity]]:
        """Link a name to the one known name it is part of ("Mr. Quill", or "QUILL,
        BARTHOLOMEW" in another order). Returns the entity and, when the name fits several,
        those candidates."""
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

    def assign(self, works: list[Work], release: dict[str, str]) -> None:
        """Fill each note's placeholders. Longer names go first, so "Mr. Quill" can join
        "Bartholomew Quill"; a mention that fits several names is sent to review with the
        candidates."""
        found = [(work, i, span) for work in works for i, span in enumerate(work.spans)]
        found.sort(
            key=lambda item: -len(self.key(item[2]).split()) if self.is_name(item[2].label) else 0
        )
        for work in works:
            work.placeholders = [""] * len(work.spans)
        for work, index, span in found:
            kept = generalize(span, work.offset_days, release, self.rules)
            if kept is not None:
                work.placeholders[index] = kept
                continue
            key = self.key(span)
            if self.is_name(span.label):
                entity, others = self.resolve_name(key, span.label)
                if others:
                    choices = ", ".join(e.placeholder for e in others)
                    work.reviews.append(
                        f"line {line_of(work.text, span.start)}: a name that could be "
                        f"{choices}; replaced as {entity.placeholder}"
                    )
            else:
                entity = self.by_key.get(key) or self.new(span.label)
                entity.keys.add(key)
                self.by_key[key] = entity
            entity.add(span.text, work.file)
            work.placeholders[index] = entity.placeholder


# --- 8. Replace, then sweep -------------------------------------------------------------------


def redact(
    text: str, spans: list[Span], placeholders: list[str]
) -> tuple[str, list[tuple[int, int]]]:
    """Replace each span with its placeholder. Returns the new text and where each placeholder
    this recipe wrote sits in it, so the sweep can skip exactly those and nothing else."""
    parts: list[str] = []
    ours: list[tuple[int, int]] = []
    cursor = length = 0
    for span, placeholder in sorted(
        zip(spans, placeholders, strict=True), key=lambda pair: pair[0].start
    ):
        gap = text[cursor : span.start]
        parts += [gap, placeholder]
        length += len(gap)
        ours.append((length, length + len(placeholder)))
        length += len(placeholder)
        cursor = span.end
    parts.append(text[cursor:])
    return "".join(parts), ours


def residual_sweep(
    redacted: str, rules: dict[str, Any], ours: Iterable[tuple[int, int]]
) -> list[str]:
    """Regex for data that must never stay readable, run with only the placeholders this recipe
    wrote blanked out. A bracket that was already in the note ("[Emergency contact: ...]") is
    read like any other text. Allowlisted terms and clinical numbers are not hits. Each hit is a
    reason to escalate; it gives the pattern and the line, never the value."""
    chars = list(redacted)
    for start, end in ours:
        chars[start:end] = " " * (end - start)
    masked = "".join(chars)
    skip = [*allowlisted_ranges(masked, rules), *clinical_ranges(masked, rules)]
    hits: list[tuple[int, str]] = []
    for name, pattern in rules["residual_patterns"].items():
        for match in re.finditer(pattern, masked):
            if not inside(match.start(), match.end(), skip):
                hits.append((match.start(), name))
    return [f"line {line_of(masked, start)}: {name} still readable" for start, name in sorted(hits)]


# --- Run --------------------------------------------------------------------------------------


@dataclass
class Result:
    request: Request
    draw: Draw
    works: list[Work]
    entities: list[Entity]
    patients: dict[str, Entity]
    calls: int


def build_works(
    request: Request, records: ehr.Ehr, draw: Draw
) -> tuple[list[Work], dict[str, ehr.Patient]]:
    """The request's notes from the EHR, each with its patient and its name in the extract. A
    cohort patient the EHR doesn't have, or a request with no notes, stops here, before any SIE
    call."""
    cohort: dict[str, ehr.Patient] = {}
    for patient_id in request.cohort:
        patient = records.get_patient(patient_id)
        if patient is None:
            raise ValueError(f"cohort patient {patient_id} is not in the EHR")
        cohort[patient_id] = patient
    notes = records.notes_for(
        list(request.cohort), list(request.note_types), request.start, request.end
    )
    if not notes:
        raise ValueError(f"the EHR has no notes for request {request.request_id}")
    works: list[Work] = []
    counts: dict[str, int] = {}
    for note in notes:
        counts[note.patient_id] = counts.get(note.patient_id, 0) + 1
        study = draw.study_ids[note.patient_id]
        name = f"{study}-{counts[note.patient_id]:02d}_{note.note_type}.txt"
        works.append(Work(note, cohort[note.patient_id], name))
    return works, cohort


def run(
    client: SIEClient,
    rules: dict[str, Any],
    request: Request,
    records: ehr.Ehr,
    rng: random.Random,
) -> Result:
    """Pseudonymize every note of the request. Stops with SIEStopped, before anything is
    written, on a refusal or when every SIE call failed."""
    draw = draw_pseudonyms(request.cohort, rules, rng)
    works, cohort = build_works(request, records, draw)
    release = request.release
    released_known = known_releases(cohort.values(), rules)
    calls = CallLog()
    found = {
        work.file: apply_allowlist(
            work.text, detect_pii(client, calls, work, rules), rules, released_known
        )
        for work in works
    }
    calls.stop_if_all_failed()
    if calls.failures:
        print(
            f"{len(calls.failures)} of {calls.made} SIE calls failed; those notes escalate. "
            f"First: {short(calls.failures[0][1])}"
        )
    births = {p.dob for p in cohort.values()}
    patterns = {w.file: pattern_spans(w.text, rules, births) for w in works}
    known: dict[str, list[Span]] = {}
    others: dict[str, ehr.Patient] = {}
    for work in works:
        spans = [
            s for p in cohort.values() for s in known_spans(work.text, p, PATIENT_LABEL, rules)
        ]
        misfiled, theirs = check_identity(work, patterns[work.file] + spans, records, rules)
        if not misfiled:
            work.offset_days = draw.offsets[work.patient.patient_id]
        for patient in theirs:
            others[patient.patient_id] = patient
            spans += known_spans(work.text, patient, "person", rules)
        if theirs:
            dobs = births | {p.dob for p in theirs}
            patterns[work.file] = pattern_spans(work.text, rules, dobs)
        known[work.file] = spans
    names = {
        span.text: span.label
        for spans in found.values()
        for span in spans
        if span.label in rules["name_labels"]
    }
    people = {
        name: PATIENT_LABEL
        for patient in cohort.values()
        for name in person_names(patient_names(patient), rules)
    }
    people |= {
        name: "person" for p in others.values() for name in person_names(patient_names(p), rules)
    }
    for name in person_names([n for n, label in names.items() if label == "person"], rules):
        people.setdefault(name, "person")
    min_letters = rules["thresholds"]["propagate_min_letters"]
    for work in works:
        for start, _ in find_terms(work.text, released_known, ignore_case=True)[:1]:
            work.reviews.append(
                f"line {line_of(work.text, start)}: an allowlisted name is also a cohort "
                "patient's; replaced, check who is meant"
            )
        work.spans = (
            found[work.file]
            + known[work.file]
            + propagate_names(work.text, names, min_letters)
            + name_form_spans(work.text, people, rules, released_known)
        )
    values = {
        key: span
        for work in works
        for span in [*work.spans, *patterns[work.file]]
        if (key := spelling_key(span, rules)) is not None
    }
    for work in works:
        text = work.text
        released = released_terms(text, work.spans, rules)
        spans = work.spans + second_spellings(text, values, rules)
        for term in released:
            spans += [
                Span(start, end, "person", text[start:end], "name_form")
                for start, end in find_terms(text, [term], ignore_case=True)
            ]
            if released_is_ambiguous(text, term, rules):
                line = line_of(text, find_terms(text, [term], ignore_case=True)[0][0])
                work.reviews.append(
                    f"line {line}: a short allowlisted name that is also part of another "
                    "person's name here; replaced, check who is meant"
                )
        spans = extend_to_patterns(text, spans, rules)
        spans = apply_allowlist(text, spans, rules, [*released, *released_known])
        # A regex pattern decides its own stretch, whatever the model or the EHR matched there:
        # the date is shifted, the ZIP keeps three digits, "MRN:" stays. The rest of a span a
        # pattern cut through is still replaced.
        ruled = ruled_ranges(text, rules)
        spans = [piece for s in spans for piece in outside_ruled(text, s, ruled, rules)]
        work.spans = merge_spans(text, spans + patterns[work.file])
    placeholders = Placeholders(rules)
    placeholders.add_patients([cohort[pid] for pid in draw.order])
    placeholders.assign(works, release)
    for work in works:
        if work.offset_days is not None and release["dates"] == "shift":
            work.errors += [
                f"line {line_of(work.text, span.start)}: a date the rules can't shift (no year, "
                "or a format not in date_formats) was removed; check it"
                for span, placeholder in zip(work.spans, work.placeholders, strict=True)
                if span.label == DATE_LABEL and not SHIFTED_DATE.fullmatch(placeholder)
            ]
        work.redacted, work.ours = redact(work.text, work.spans, work.placeholders)
        work.leaks = residual_sweep(work.redacted, rules, work.ours)
    entities = [entity for entity in placeholders.entities if entity.values]
    return Result(request, draw, works, entities, placeholders.patients, calls.made)


# --- Outputs ----------------------------------------------------------------------------------


def shifted_note_date(work: Work) -> str:
    if work.offset_days is None:
        return ""
    return (work.note.note_date + timedelta(days=work.offset_days)).isoformat()


def key_json(result: Result) -> dict[str, Any]:
    """The key back to real people. It holds every original value, the study IDs and the date
    offsets: it stays with the honest broker, apart from the extract, and never goes with it."""
    request = result.request
    return {
        "request_id": request.request_id,
        "approval_reference": request.approval_reference,
        "patients": {
            patient_id: {
                "study_id": result.draw.study_ids[patient_id],
                "placeholder": result.patients[patient_id].placeholder,
                "offset_days": result.draw.offsets[patient_id],
            }
            for patient_id in request.cohort
        },
        "notes": {
            work.file: {
                "ehr_note_id": work.note.note_id,
                "ehr_patient_id": work.patient.patient_id,
                "status": work.status,
                "offset_days": work.offset_days,
                "replacements": [
                    {
                        "line": line_of(work.text, s.start),
                        "start": s.start,
                        "end": s.end,
                        "text": s.text,
                        "label": s.label,
                        "source": s.source,
                        "placeholder": p,
                    }
                    for s, p in zip(work.spans, work.placeholders, strict=True)
                ],
            }
            for work in result.works
        },
        "placeholders": {
            entity.placeholder: {
                "label": entity.label,
                "values": entity.values,
                "notes": entity.notes,
            }
            for entity in result.entities
        },
    }


def by_line(reasons: list[str]) -> list[str]:
    """Reasons in line order; one without a line number (a window not read) comes first."""
    found = [re.match(r"line (\d+):", reason) for reason in reasons]
    order = [int(match.group(1)) if match else 0 for match in found]
    return [reason for _, reason in sorted(zip(order, reasons, strict=True), key=lambda p: p[0])]


def manifest_rows(result: Result) -> list[dict[str, Any]]:
    """One row per note in the extract: the study ID, the note type and the shifted note date.
    Escalated notes are held out, so they aren't listed."""
    return [
        {
            "request_id": result.request.request_id,
            "approval_reference": result.request.approval_reference,
            "study_id": result.draw.study_ids[work.patient.patient_id],
            "note_file": work.file,
            "note_type": work.note.note_type,
            "note_date": shifted_note_date(work),
        }
        for work in result.works
        if work.status != ESCALATE
    ]


def status_rows(result: Result) -> list[dict[str, Any]]:
    """One row per note: its status and every reason (escalating ones first), by line number,
    never the value."""
    return [
        {
            "note_file": work.file,
            "study_id": result.draw.study_ids[work.patient.patient_id],
            "note_type": work.note.note_type,
            "status": work.status,
            "replacements": len(work.spans),
            "reasons": " | ".join([*by_line(work.errors + work.leaks), *by_line(work.reviews)]),
        }
        for work in result.works
    ]


def log_rows(result: Result) -> list[dict[str, Any]]:
    """Every replacement, by note and line: the category and the placeholder, never the value."""
    return [
        {
            "note_file": work.file,
            "status": work.status,
            "line": line_of(work.text, span.start),
            "category": span.label,
            "placeholder": placeholder,
            "found_by": FOUND_BY[span.source],
        }
        for work in result.works
        for span, placeholder in zip(work.spans, work.placeholders, strict=True)
    ]


def csv_safe(value: Any) -> str:
    """A leading ' on a cell a spreadsheet would run as a formula; anything else as written."""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_safe(value) for key, value in row.items()})


def write_outputs(output_dir: Path, result: Result) -> None:
    """The extract (notes that didn't escalate, and the manifest), the held notes, the log and
    the note statuses, and private/ with the key, which never goes with the extract."""
    for folder in ("extract/notes", "held", "private"):
        (output_dir / folder).mkdir(parents=True, exist_ok=True)
    for work in result.works:
        folder = "held" if work.status == ESCALATE else "extract/notes"
        (output_dir / folder / work.file).write_text(work.redacted, encoding="utf-8")
    write_csv(output_dir / "extract" / "manifest.csv", MANIFEST_FIELDS, manifest_rows(result))
    write_csv(output_dir / "replacement-log.csv", LOG_FIELDS, log_rows(result))
    write_csv(output_dir / "note-status.csv", STATUS_FIELDS, status_rows(result))
    (output_dir / "private" / "key.json").write_text(
        json.dumps(key_json(result), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--request", type=Path, default=REQUEST_PATH, help="the approved request")
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rules, request, records = load_rules(), load_request(args.request), ehr.Ehr()
    started = time.monotonic()
    with make_client() as client:
        result = stop_on_refusal(
            lambda: run(client, rules, request, records, random.SystemRandom())
        )
    elapsed = time.monotonic() - started
    write_outputs(output, result)
    print(f"Request {request.request_id} ({request.approval_reference}):")
    for row in status_rows(result):
        print(f"\n{row['note_file']:<40} {row['status']:<14} {row['replacements']:>3} replaced")
        for reason in row["reasons"].split(" | ") if row["reasons"] else []:
            print(f"    {reason}")
    print(f"\n{result.calls} SIE calls in {elapsed:.0f} s. Nothing is released: the honest broker")
    print("checks each note against its original and signs the extract off.")
    print(f"extract: {output / 'extract'}\nheld: {output / 'held'}")
    print(f"log: {output / 'replacement-log.csv'}, {output / 'note-status.csv'}")
    print(f"key (keep apart, never send): {output / 'private'}")


if __name__ == "__main__":
    main()
