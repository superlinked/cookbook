"""Check each recorded first-notice-of-loss call against the claim that was saved from it.

Use case:  Call quality review of first-notice-of-loss (FNOL) intake calls.
Who:       The claims-operations reviewer, or the intake team lead, after a storm.
Problem:   The saved claim is what the adjuster triages from. Agents miss or mistype fields, and
           an injury or a house nobody can live in can be said on the call and lost in the
           claim. Listening to every call is too slow, so only a sample gets reviewed.
Input:     Call recordings (16 kHz mono WAV) in data/, the saved claims from the claims system
           (claims_system.py over data/claims.csv), the intake requirements claims operations
           owns (intake_requirements.yaml) and the recipe's reading rules (rules.yaml).
SIE pipeline:
           extract  nvidia/parakeet-tdt-0.6b-v3  once per call: timed transcript lines
           extract  fastino/gliner2-large-v1     once per transcript window (about 512
                                                 tokens): policy number, date of loss,
                                                 location, cause, injuries, urgent help
Output:    worklist.csv, one row per call with its check status, flags and callback questions,
           injury and urgent claims first; claims/<call>.json with every field's saved value,
           what the call said, the transcript line and time, and the timed transcript.
Why SIE:   Two specialist models on SIE: one for speech, one for structured extraction. No
           generative model makes a decision. Checking values, comparing them with the saved
           claim and the injury and urgency rules are plain Python.
Models on: SIE Serverless, or your own SIE with SIE_BASE_URL.

    uv run recipe.py
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from typing import Any, cast

import yaml
from claims_system import FOUND, ClaimsSystem, SavedClaim
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
REQUIREMENTS_PATH = ROOT / "intake_requirements.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
# A failed SIE call's message is cut to this many characters in reasons and the summary line.
ERROR_CHARS = 200
# Ask Parakeet for sentence segments with start and end times: they are the transcript lines.
TRANSCRIBE_OPTIONS = {"timestamp_granularities": ["segment"]}
# When GLiNER2 finds nothing in a window, SIE Serverless answers the output_schema call with
# HTTP 500 and this message. We record it as "no fields found" in that window, with a note. A
# field that is then missing can only become a callback question; it never passes a check.
EMPTY_RESULT_ERROR = "Inference completed without a valid conserved billing settlement"
# A cell starting with one of these runs as a formula in a spreadsheet.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")

# Field statuses: what comparing the saved claim with the call found.
IN_CLAIM, FILL_FROM_CALL, DIFFERS, MISSING = "in_claim", "fill_from_call", "differs", "missing"
# Check statuses, worst first. They name what the checks found; the reviewer decides.
NEEDS_REVIEW, CALLBACK_NEEDED, CHECKS_PASSED = "needs_review", "callback_needed", "checks_passed"
STATUS_ORDER = (NEEDS_REVIEW, CALLBACK_NEEDED, DIFFERS, FILL_FROM_CALL, CHECKS_PASSED)
INJURY, URGENT = "injury_reported", "urgent_assistance"
# A policy value that fails the pattern but has this many digits was still a number someone
# said: it sends the field to the reviewer instead of being dropped.
MIN_POLICY_DIGITS = 6
# The fields the recipe knows how to compare with the saved claim.
COMPARABLE = ("policy_number", "date_of_loss", "loss_location", "cause_of_loss", "injuries")
WORKLIST_FIELDS = (
    *("claim_number", "insured", "callback_phone", "recording", "flags", "check_status"),
    *("fill_from_call", "differs", "callback_questions", "review_reasons"),
)
MONTHS = {
    name: number
    for number, name in enumerate(
        [
            *("january", "february", "march", "april", "may", "june", "july", "august"),
            *("september", "october", "november", "december"),
        ],
        start=1,
    )
}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# --- Records ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    """One transcript line (a sentence segment from Parakeet)."""

    number: int  # 1-based
    start: float  # seconds into the call
    text: str

    def cite(self) -> dict[str, Any]:
        return {"line": self.number, "at": _clock(self.start), "text": self.text}


@dataclass(frozen=True)
class Candidate:
    """One value the extractor returned for one field in one window."""

    field: str
    value: str
    window: tuple[Line, ...]


@dataclass
class FieldCheck:
    """One required field: the saved value, what the call said, and what the comparison found."""

    name: str
    status: str  # in_claim | fill_from_call | differs | missing
    saved: str
    heard: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    question: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "saved": self.saved,
            "heard_on_call": self.heard,
            "evidence": self.evidence,
            "reason": self.reason,
            "callback_question": self.question,
        }


@dataclass
class Flag:
    """An urgent trigger the call raised: who needs to see this claim today, and why."""

    name: str  # injury_reported | urgent_assistance
    detail: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    ambiguous: bool = False

    def label(self) -> str:
        return f"{self.name} (ambiguous)" if self.ambiguous else self.name


@dataclass
class CallCheck:
    recording: str
    claim: SavedClaim | None = None
    claim_numbers: tuple[str, ...] = ()
    fields: dict[str, FieldCheck] = field(default_factory=dict)
    flags: list[Flag] = field(default_factory=list)
    review: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    transcript: list[Line] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.review:
            return NEEDS_REVIEW
        found = {check.status for check in self.fields.values()}
        if MISSING in found:
            return CALLBACK_NEEDED
        if DIFFERS in found:
            return DIFFERS
        if FILL_FROM_CALL in found:
            return FILL_FROM_CALL
        return CHECKS_PASSED

    def as_json(self) -> dict[str, Any]:
        claim = self.claim
        return {
            "recording": self.recording,
            "claim_number": claim.claim_number if claim else "",
            "insured": claim.insured if claim else "",
            "callback_phone": claim.callback_phone if claim else "",
            "check_status": self.status,
            "flags": [
                {
                    "flag": f.name,
                    "ambiguous": f.ambiguous,
                    "detail": f.detail,
                    "evidence": f.evidence,
                }
                for f in self.flags
            ],
            "fields": {name: check.as_json() for name, check in self.fields.items()},
            "review_reasons": self.review,
            "notes": self.notes,
            "transcript": [f"[{_clock(line.start)}] {line.text}" for line in self.transcript],
        }


def _clock(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 60:02d}:{whole % 60:02d}"


# --- Rules and requirements -------------------------------------------------------------------


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def load_requirements(rules: dict[str, Any], path: Path = REQUIREMENTS_PATH) -> dict[str, Any]:
    """Claims operations' intake requirements, checked against what the recipe can compare."""
    requirements = cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))
    for name in requirements["required_fields"]:
        if name not in COMPARABLE or name not in rules["fields"]:
            raise ValueError(
                f"{path.name} requires '{name}', which the recipe can't read or compare. "
                f"It knows: {', '.join(COMPARABLE)}"
            )
    for flag in (INJURY, URGENT):
        if not requirements["urgent_triggers"][flag]["phrases"]:
            raise ValueError(f"{path.name} lists no phrases for {flag}")
    return requirements


def output_schema(rules: dict[str, Any]) -> dict[str, Any]:
    """The GLiNER2 output_schema: one optional string per field."""
    properties = {
        name: {"type": "string", "description": spec["description"]}
        for name, spec in rules["fields"].items()
    }
    return {"type": "object", "properties": properties}


# --- SIE client and call failures -------------------------------------------------------------


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


class SIEStopped(RuntimeError):
    """SIE refused the call for a reason a retry won't fix, or no call succeeded; the run
    stops without outputs."""


class CallFailed(Exception):
    """One SIE call failed for a reason that may pass; the call it was for goes to review."""


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
        recording to review and the run goes on. With `empty_result_ok` (the GLiNER2 calls),
        SIE Serverless's empty-result HTTP 500 is a reply with nothing in it, not a failure. An
        error that isn't an SIE error is a bug in this code and is raised as is.
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
                return {key: {}, "empty_result_error": str(error)}
            if is_fatal(error):
                raise SIEStopped(stop_message(error, model)) from error
            self.failures.append((model, error))
            raise CallFailed(f"the SIE call failed: {short(error)}") from error
        if not isinstance(reply, dict) or not isinstance(reply.get(key), dict):
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
        print(f"{n} of {total} SIE calls failed; their calls go to review. First: {short(first)}")


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
    timeout, a lost connection, a model still loading and a 5xx are transient or per-call:
    those recordings go to review."""
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
    """The error for a per-call reason or the summary line, its message cut short."""
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


# --- 1. Transcribe ----------------------------------------------------------------------------


def transcribe(calls: Calls, client: SIEClient, wav: Path, rules: dict[str, Any]) -> list[Line]:
    """Parakeet's timed sentence segments as numbered lines. A reply without `data.segments`
    is a failed call; an empty list is a call with no speech in it."""
    model = rules["models"]["transcribe"]
    audio = {"data": wav.read_bytes(), "format": wav.suffix.lstrip(".").lower()}
    reply = calls.call(
        model,
        lambda: client.extract(
            model,
            Item(id=f"transcribe/{wav.stem}", audio=audio),
            options=TRANSCRIBE_OPTIONS,
            provision_timeout_s=WAIT_S,
        ),
        "data",
    )
    segments = reply["data"].get("segments")
    if not isinstance(segments, list):
        missing = SIEError(f"{model} replied without 'data.segments'")
        calls.failures.append((model, missing))
        raise CallFailed(f"the SIE call failed: {short(missing)}")
    return [
        Line(number, float(segment.get("start", 0.0)), str(segment["text"]).strip())
        for number, segment in enumerate(
            (s for s in segments if str(s.get("text", "")).strip()), start=1
        )
    ]


# --- 2. Window and extract --------------------------------------------------------------------


def windows(lines: list[Line], size: int, overlap: int) -> list[tuple[Line, ...]]:
    """Group whole lines into windows of at most `size` words; each window repeats the last
    `overlap` lines of the one before, so a question and its answer stay together. A single
    line longer than `size` is a window of its own."""
    parts: list[tuple[Line, ...]] = []
    start = 0
    while start < len(lines):
        end, words = start, 0
        while end < len(lines) and (end == start or words + len(lines[end].text.split()) <= size):
            words += len(lines[end].text.split())
            end += 1
        parts.append(tuple(lines[start:end]))
        if end >= len(lines):
            break
        start = max(start + 1, end - overlap)
    return parts


def extract_candidates(
    calls: Calls,
    client: SIEClient,
    stem: str,
    lines: list[Line],
    rules: dict[str, Any],
    check: CallCheck,
) -> list[Candidate]:
    """Every field value GLiNER2 returned, per window. A failed window sends the call to review:
    it may hold a correction, so it never counts as "nothing said"."""
    model, limits = rules["models"]["extract"], rules["thresholds"]
    schema = output_schema(rules)
    candidates: list[Candidate] = []
    parts = windows(lines, limits["window_words"], limits["window_overlap_lines"])
    for index, window in enumerate(parts, start=1):
        text = "\n".join(line.text for line in window)
        try:
            reply = calls.call(
                model,
                partial(
                    client.extract,
                    model,
                    Item(id=f"extract/{stem}/w{index}", text=text),
                    output_schema=schema,
                    options={"overflow_policy": "error"},  # a window too long fails, not cut
                    provision_timeout_s=WAIT_S,
                ),
                "data",
                empty_result_ok=True,
            )
        except CallFailed as failed:
            check.review.append(
                f"Transcript window {index} (lines {window[0].number}-{window[-1].number}) was "
                f"not read, {failed}; a value or correction may be in it, so listen to that part"
            )
            continue
        if reply.get("empty_result_error"):
            check.notes.append(
                f"Window {index}: no fields found (SIE Serverless answered HTTP 500 for the empty "
                "result)"
            )
        for name, value in reply["data"].items():
            if name in rules["fields"] and isinstance(value, str) and value.strip():
                candidates.append(Candidate(name, value.strip(), window))
    return candidates


# --- 3. Check the values (plain Python) -------------------------------------------------------


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _has_term(text: str, terms: list[str]) -> bool:
    lowered = text.casefold()
    return any(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", lowered) for term in terms)


def _strip_terms(text: str, terms: list[str]) -> str:
    lowered = text.casefold()
    for term in sorted(terms, key=len, reverse=True):
        lowered = re.sub(r"(?<!\w)" + re.escape(term) + r"(?!\w)", " ", lowered)
    return lowered


def locate(value: str, lines: tuple[Line, ...] | list[Line]) -> Line | None:
    """The transcript line a value was said on, or the first of two lines it spans."""
    wanted = _squash(value)
    for line in lines:
        if wanted in _squash(line.text):
            return line
    for first, second in itertools.pairwise(lines):
        if wanted in _squash(f"{first.text} {second.text}"):
            return first
    return None


def normalize_policy(value: str) -> str:
    return re.sub(r"[\s.\-]", "", value).upper()


def valid_policy(value: str, rules: dict[str, Any]) -> bool:
    return re.fullmatch(rules["policy_number_pattern"], normalize_policy(value)) is not None


def usable(candidate: Candidate, rules: dict[str, Any], check: CallCheck) -> bool:
    """A value counts only when it was said in its window and passes the field's check."""
    if (
        not re.search(r"[^\W_]", candidate.value)
        or locate(candidate.value, candidate.window) is None
    ):
        return False
    if candidate.field == "policy_number" and not valid_policy(candidate.value, rules):
        # A value with several digits is a number someone said; keep it, so it can't vanish
        # next to a different saved number. Anything else ("my policy") is dropped.
        if len(re.findall(r"\d", candidate.value)) >= MIN_POLICY_DIGITS:
            return True
        check.notes.append(f"Heard '{candidate.value}' as a policy number; not a valid one")
        return False
    if candidate.field == "date_of_loss" and not (
        re.search(r"\d", candidate.value) or _has_term(candidate.value, rules["date_words"])
    ):
        check.notes.append(f"Heard '{candidate.value}' as the date of loss; not a date")
        return False
    return True


def comparison_key(name: str, value: str) -> str:
    """The form two values of a field are grouped in, so formatting does not split them."""
    if name == "policy_number":
        return normalize_policy(value)
    return " ".join(re.findall(r"\w+", value.casefold()))


def heard_values(name: str, found: list[Candidate], rules: dict[str, Any]) -> list[Candidate]:
    """The distinct values the call gave for one field, in the order they were said. For an
    `on_conflict: first` field only the first value is kept."""
    values = distinct_values(name, found)
    return values[:1] if rules["fields"][name].get("on_conflict") == "first" else values


def distinct_values(name: str, found: list[Candidate]) -> list[Candidate]:
    """The distinct values the call gave for one field, in the order they were said. A value
    whose whole words sit inside a longer one ("Alder Creek Road" in "214 Alder Creek Road") is
    the same value; "14 Alder Creek Road" and "October 2" are not ("214 ...", "October 20")."""
    if not found:
        return []

    def heard_at(candidate: Candidate) -> int:
        line = locate(candidate.value, candidate.window)
        return line.number if line else 0

    groups: dict[str, list[Candidate]] = {}
    for candidate in sorted(found, key=heard_at):
        groups.setdefault(comparison_key(name, candidate.value), []).append(candidate)
    distinct: list[str] = []
    for key in sorted(groups, key=len, reverse=True):
        if not any(f" {key} " in f" {kept} " for kept in distinct):
            distinct.append(key)
    return sorted((groups[key][0] for key in distinct), key=heard_at)


# --- 4. Injuries and urgent help --------------------------------------------------------------


REPORTED, DENIED, AMBIGUOUS = "reported", "denied", "ambiguous"


def read_trigger(text: str, phrases: list[str], rules: dict[str, Any]) -> str | None:
    """How one sentence speaks to a trigger: reported, denied, ambiguous, or None.

    The sentence is cut into clauses. A clause with a trigger phrase reports it, unless the
    rest of the clause has a negation word ("nobody was hurt"), which denies it. A denial with
    a hedge ("not badly hurt") or partial word ("nobody else") implies someone was hurt, and
    a sentence that reports in one clause and denies in another is ambiguous. Questions are
    None."""
    if text.rstrip().endswith("?"):
        return None
    joiners = "|".join(re.escape(word) for word in rules["clause_words"])
    clauses = re.split(rf"[,;:.]|\b(?:{joiners})\b", text.casefold())
    found: set[str] = set()
    for clause in clauses:
        if not _has_term(clause, phrases):
            continue
        rest = _strip_terms(clause, phrases)
        if not _has_term(rest, rules["negation_words"]):
            found.add(REPORTED)
        elif _has_term(rest, rules["hedge_words"]) or _has_term(rest, rules["partial_words"]):
            found.add(AMBIGUOUS)
        else:
            found.add(DENIED)
    if not found:
        return None
    if AMBIGUOUS in found or found == {REPORTED, DENIED}:
        return AMBIGUOUS
    return found.pop()


def decide_trigger(
    name: str,
    lines: list[Line],
    model_values: list[Candidate],
    requirements: dict[str, Any],
    rules: dict[str, Any],
    check: CallCheck,
) -> str | None:
    """Raise the injury or urgent flag from the call's sentences and the model's span.

    Returns what the call said: REPORTED (the flag is up), DENIED, or None (not discussed).
    The flag goes up on any sentence that reports or is ambiguous, and says when it is
    ambiguous or when the call also denies it. When the model read a trigger that no sentence
    confirms, the flag still goes up, marked ambiguous: a missed injury costs more than a
    second look. Model spans on a sentence that only denies ("nobody was hurt") are dropped
    first; the first span left is the model's value.
    """
    phrases = requirements["urgent_triggers"][name]["phrases"]
    read = {line: read_trigger(line.text, phrases, rules) for line in lines}
    reported = [line for line, how in read.items() if how in (REPORTED, AMBIGUOUS)]
    denied = [line for line, how in read.items() if how == DENIED]
    model_value, model_line = None, None
    for value in model_values:
        line = locate(value.value, lines)
        if line in denied:
            check.notes.append(f"{name}: the model read '{value.value}' from a denial; dropped")
        elif model_value is None:
            model_value, model_line = value, line
    if reported:
        unclear = [line for line in reported if read[line] == AMBIGUOUS]
        ambiguous = bool(unclear or denied)
        if unclear:
            detail = f"Ambiguous: the sentence both reports and denies it ('{unclear[0].text}')"
        elif denied:
            detail = f"Ambiguous: the call says '{reported[0].text}' and also '{denied[0].text}'"
        else:
            detail = f"The caller said: '{reported[0].text}'"
        evidence = [line.cite() for line in sorted({*reported, *denied}, key=lambda x: x.number)]
        check.flags.append(Flag(name, detail, evidence, ambiguous))
        return REPORTED
    if model_value:
        detail = (
            f"Ambiguous: the model read '{model_value.value}' as {name}; no sentence confirms "
            "it, so listen to the call"
        )
        evidence = [model_line.cite()] if model_line else []
        check.flags.append(Flag(name, detail, evidence, ambiguous=True))
        return REPORTED
    return DENIED if denied else None


# --- 5. Compare each field with the saved claim -----------------------------------------------


def date_parts(text: str) -> dict[str, int]:
    """The year, month, day and weekday (0 = Monday) a spoken or written date names."""
    lowered = text.casefold()
    parts: dict[str, int] = {}
    if iso := re.search(r"\b(20\d\d)-(\d{1,2})-(\d{1,2})\b", lowered):
        return {"year": int(iso[1]), "month": int(iso[2]), "day": int(iso[3])}
    if slash := re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", lowered):
        parts.update(month=int(slash[1]), day=int(slash[2]))
    months = "|".join(MONTHS)
    ordinal = r"(\d{1,2})(?:st|nd|rd|th)?"
    # A span naming two days or two weekdays ("the 20th, or maybe the 21st") can't be compared.
    days = {int(m[1]) for m in re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)\b", lowered)}
    days |= {int(m[2]) for m in re.finditer(rf"\b({months})\s+(?:the\s+)?{ordinal}\b", lowered)}
    named = {w for w in WEEKDAYS if re.search(rf"\b{w}\b", lowered)}
    if len(days) > 1 or len(named) > 1:
        return {"conflict": 1}
    if after := re.search(rf"\b({months})\s+(?:the\s+)?{ordinal}\b", lowered):
        parts.update(month=MONTHS[after[1]], day=int(after[2]))
    elif before := re.search(rf"\b(?:the\s+)?{ordinal}\s+(?:of\s+)?({months})\b", lowered):
        parts.update(month=MONTHS[before[2]], day=int(before[1]))
    elif alone := re.search(rf"\b({months})\b", lowered):
        parts["month"] = MONTHS[alone[1]]
    if "day" not in parts and days:  # "Tuesday the 21st": a day with no month
        parts["day"] = days.pop()
    if year := re.search(r"\b(20\d\d)\b", lowered):
        parts["year"] = int(year[1])
    for number, weekday in enumerate(WEEKDAYS):
        if re.search(rf"\b{weekday}\b", lowered):
            parts["weekday"] = number
    return parts


def same_date(saved: str, heard: str) -> bool | None:
    """Whether a spoken date agrees with the saved one; None when it can't be compared (a
    relative date such as "yesterday", or nothing that reads as a date)."""
    try:
        when = date.fromisoformat(saved)
    except ValueError:
        return None
    parts = date_parts(heard)
    if not parts or "conflict" in parts:
        return None
    if "weekday" in parts and "day" not in parts:  # "Tuesday" could be any week
        return None
    known = {"year": when.year, "month": when.month, "day": when.day, "weekday": when.weekday()}
    return all(known[key] == value for key, value in parts.items())


def address_parts(text: str, rules: dict[str, Any]) -> dict[str, str]:
    """Street number, street (spaces dropped), unit and town of an address."""
    words: dict[str, str] = rules["address_words"]
    spaced = re.sub(r"#\s*", " # ", text.casefold())

    def norm(chunk: str) -> list[str]:
        return [words.get(token, token) for token in re.findall(r"[a-z0-9#]+", chunk)]

    parts: dict[str, str] = {}
    if unit := re.search(r"\bunit\s+(\w+)", " ".join(norm(spaced))):
        parts["unit"] = unit[1]
    chunks = [norm(chunk) for chunk in spaced.split(",")]
    for index, tokens in enumerate(chunks):
        if tokens and tokens[0].isdigit():
            street = list(itertools.takewhile(lambda token: token != "unit", tokens[1:]))
            parts.update(number=tokens[0], street="".join(street))
            towns = [c for c in chunks[index + 1 :] if c and c[0] != "unit"]
            if towns:
                parts["town"] = "".join(towns[-1])
            break
    return parts


def same_address(saved: str, heard: str, rules: dict[str, Any]) -> bool | None:
    """Whether the call's address agrees with the saved one: every part the call gives must
    match. None when the call's value has no street number and street."""
    said, kept = address_parts(heard, rules), address_parts(saved, rules)
    if not {"number", "street"} <= said.keys():
        return None
    return all(kept.get(part) == value for part, value in said.items())


def perils(text: str, rules: dict[str, Any]) -> set[str]:
    return {name for name, words in rules["perils"].items() if _has_term(text, words)}


def same_cause(saved: str, heard: str, rules: dict[str, Any]) -> bool | None:
    """Whether every peril the call names is in the saved cause; None when it names none."""
    said = perils(heard, rules)
    if not said:
        return None
    return said <= perils(saved, rules)


def same_value(name: str, saved: str, heard: str, rules: dict[str, Any]) -> bool | None:
    if name == "policy_number":
        return normalize_policy(saved) == normalize_policy(heard)
    if name == "date_of_loss":
        return same_date(saved, heard)
    if name == "loss_location":
        return same_address(saved, heard, rules)
    if name == "cause_of_loss":
        return same_cause(saved, heard, rules)
    raise ValueError(f"no comparison for {name}")


def compare_field(
    name: str, saved: str, heard: list[Candidate], question: str, rules: dict[str, Any]
) -> FieldCheck:
    """One field's status from the saved value and every value the call gave."""
    values = [normalize_policy(c.value) if name == "policy_number" else c.value for c in heard]
    evidence = [line.cite() for c in heard if (line := locate(c.value, c.window)) is not None]
    check = FieldCheck(name, MISSING, saved, values, evidence)
    if not saved and not heard:
        check.reason, check.question = "Blank in the claim and not said on the call", question
        return check
    if name == "policy_number" and (bad := [v for v in values if not valid_policy(v, rules)]):
        check.status = DIFFERS
        check.reason = f"Heard '{bad[0]}' as a policy number; it doesn't match the policy pattern"
        return check
    if len(heard) > 1:
        listed = "; ".join(f"'{value}'" for value in values)
        check.status, check.reason = DIFFERS, f"The call gives more than one value: {listed}"
        return check
    if not heard:
        check.status = IN_CLAIM
        check.reason = "In the claim; the call doesn't mention it"
        return check
    if not saved:
        check.status, check.reason = FILL_FROM_CALL, "Blank in the claim; said on the call"
        return check
    same = same_value(name, saved, values[0], rules)
    if same is None:
        check.status = DIFFERS
        check.reason = "The call's value can't be compared with the claim's; check both"
    elif same:
        check.status, check.reason = IN_CLAIM, "In the claim; the call agrees"
    else:
        check.status, check.reason = DIFFERS, "The claim and the call disagree"
    return check


def saved_injuries(value: str) -> str:
    """The saved injury field as Yes, No or "" (blank or unknown). "None reported", "No
    injuries" and "Not injured" are No; "Pending" is blank; any other text ("Husband cut arm")
    is Yes."""
    words = re.findall(r"[a-z']+", value.casefold())
    if not words or " ".join(words) in {"unknown", "not known", "unsure", "not sure", "pending"}:
        return ""
    return "No" if words[0] in {"no", "n", "none", "false", "nobody", "not"} else "Yes"


def compare_injuries(saved: str, heard: str | None, flag: Flag | None, question: str) -> FieldCheck:
    """The injury field against what the call said (REPORTED, DENIED or None)."""
    said = {REPORTED: "Yes", DENIED: "No"}.get(heard or "", "")
    evidence = flag.evidence if flag else []
    check = FieldCheck("injuries", MISSING, saved, [said] if said else [], evidence)
    kept = saved_injuries(saved)
    if not kept and not said:
        check.reason, check.question = "Blank in the claim and not discussed on the call", question
    elif not said:
        check.status, check.reason = IN_CLAIM, "In the claim; the call doesn't discuss injuries"
    elif not kept:
        check.status, check.reason = FILL_FROM_CALL, "Blank in the claim; said on the call"
    elif kept == said:
        check.status, check.reason = IN_CLAIM, "In the claim; the call agrees"
    else:
        check.status = DIFFERS
        check.reason = f"The claim says {kept}; the call says {said}"
    return check


# --- Run --------------------------------------------------------------------------------------


def build_check(
    lines: list[Line],
    candidates: list[Candidate],
    claim: SavedClaim,
    requirements: dict[str, Any],
    rules: dict[str, Any],
    check: CallCheck,
) -> CallCheck:
    """Every required field's status and the two flags, from the transcript, the model's
    values and the saved claim."""
    check.transcript = lines
    words = sum(len(line.text.split()) for line in lines)
    if words < rules["thresholds"]["min_transcript_words"]:
        check.review.append(f"The transcript has only {words} words; listen to the call")
    found = {
        name: [c for c in candidates if c.field == name and usable(c, rules, check)]
        for name in rules["fields"]
    }
    heard = {name: heard_values(name, found[name], rules) for name in rules["fields"]}
    said = {
        name: decide_trigger(
            name, lines, distinct_values(key, found[key]), requirements, rules, check
        )
        for name, key in ((INJURY, "injuries"), (URGENT, "urgent_help"))
    }
    injury_flag = next((flag for flag in check.flags if flag.name == INJURY), None)
    for name, spec in requirements["required_fields"].items():
        question = spec["callback_question"]
        if name == "injuries":
            check.fields[name] = compare_injuries(
                claim.value(name), said[INJURY], injury_flag, question
            )
        else:
            check.fields[name] = compare_field(
                name, claim.value(name), heard[name], question, rules
            )
    return check


def process_call(
    calls: Calls,
    client: SIEClient,
    wav: Path,
    claims: ClaimsSystem,
    requirements: dict[str, Any],
    rules: dict[str, Any],
) -> CallCheck:
    """Look up the saved claim, transcribe, extract and compare one recorded call."""
    lookup = claims.find_claim(wav.name)
    check = CallCheck(wav.name, lookup.claim, lookup.claim_numbers)
    if lookup.match != FOUND or lookup.claim is None:
        check.review.append(
            f"The claims system has {len(lookup.claim_numbers)} saved claims for this recording "
            f"({', '.join(lookup.claim_numbers) or 'none'}); find the right claim first"
        )
        return check
    try:
        lines = transcribe(calls, client, wav, rules)
    except CallFailed as failed:
        check.review.append(f"The call was not transcribed, {failed}; listen to the call")
        return check
    candidates = extract_candidates(calls, client, wav.stem, lines, rules, check)
    return build_check(lines, candidates, lookup.claim, requirements, rules, check)


def run(
    client: SIEClient, rules: dict[str, Any], requirements: dict[str, Any], wavs: list[Path]
) -> tuple[list[CallCheck], Calls]:
    calls = Calls()
    claims = ClaimsSystem()
    checks = [process_call(calls, client, wav, claims, requirements, rules) for wav in wavs]
    calls.check_all_failed()
    return checks, calls


def worklist_order(check: CallCheck) -> tuple[int, int, str]:
    """Injury and urgent claims first, then by check status (worst first)."""
    number = check.claim.claim_number if check.claim else check.recording
    return (0 if check.flags else 1, STATUS_ORDER.index(check.status), number)


def worklist_rows(checks: list[CallCheck]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for check in sorted(checks, key=worklist_order):
        claim, fields = check.claim, check.fields.values()

        def at(item: FieldCheck) -> str:
            return f" [{item.evidence[0]['at']}]" if item.evidence else ""

        rows.append(
            {
                "claim_number": claim.claim_number if claim else "",
                "insured": claim.insured if claim else "",
                "callback_phone": claim.callback_phone if claim else "",
                "recording": check.recording,
                "flags": " | ".join(flag.label() for flag in check.flags),
                "check_status": check.status,
                "fill_from_call": " | ".join(
                    f"{f.name}: '{f.heard[0]}'{at(f)}" for f in fields if f.status == FILL_FROM_CALL
                ),
                "differs": " | ".join(
                    f"{f.name}: claim '{f.saved}', call "
                    + " or ".join(f"'{value}'" for value in f.heard)
                    + at(f)
                    for f in fields
                    if f.status == DIFFERS
                ),
                "callback_questions": " | ".join(
                    f"{f.name}: {f.question}" for f in fields if f.status == MISSING
                ),
                "review_reasons": " | ".join(check.review),
            }
        )
    return rows


def csv_safe(value: Any) -> str:
    """A leading ' on a cell a spreadsheet would run as a formula; anything else as written."""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def write_outputs(output_dir: Path, checks: list[CallCheck]) -> None:
    (output_dir / "claims").mkdir(parents=True, exist_ok=True)
    for check in checks:
        path = output_dir / "claims" / f"{Path(check.recording).stem}.json"
        path.write_text(json.dumps(check.as_json(), indent=2) + "\n", encoding="utf-8")
    with (output_dir / "worklist.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=WORKLIST_FIELDS)
        writer.writeheader()
        writer.writerows(
            {key: csv_safe(value) for key, value in row.items()} for row in worklist_rows(checks)
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rules = load_rules()
    requirements = load_requirements(rules)
    wavs = sorted(DATA_DIR.glob("*.wav"))
    started = time.monotonic()
    with make_client() as client:
        checks, calls = stop_on_refusal(lambda: run(client, rules, requirements, wavs))
    elapsed = time.monotonic() - started
    write_outputs(output, checks)
    for row in worklist_rows(checks):
        print(f"{row['claim_number']:<14} {row['check_status']:<16} {row['flags'] or '-'}")
        for key in ("fill_from_call", "differs", "callback_questions", "review_reasons"):
            if row[key]:
                print(f"    {key}: {row[key]}")
    per_model = ", ".join(f"{count} to {model}" for model, count in calls.count.items())
    print(f"\n{calls.total} SIE calls ({per_model}) in {elapsed:.0f} s")
    print("Statuses name what the checks found; the reviewer fixes the claim, approves the")
    print("callbacks, or hands flagged claims to an adjuster.")
    print(f"worklist: {output / 'worklist.csv'}\nclaims:   {output / 'claims'}")


if __name__ == "__main__":
    main()
