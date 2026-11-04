"""Draft what an outside visit note adds to the chart, each item marked new, matching or different.

Use case:  Abstract outside visit notes for a new patient before the first visit.
Who:       Medical records staff and medical assistants who abstract outside records, the
           clinician who reconciles medications and allergies with the patient, and the coder.
Problem:   A new patient's notes from their old clinic arrive as plain text. Someone abstracts
           the problems, medications with dose, route and frequency, allergies and vitals by
           hand and compares them with the chart, retyping the other clinic's typos (HR 720,
           levothyroxine in mg) and missing a drug the other clinic stopped.
Input:     The practice's EHR (ehr.py over the data/ehr_*.csv seeds: the patient, the problem
           list, medications with doses, allergies with their class, recent vitals, and the
           outside notes filed in each chart), the practice's code-set subset in
           data/icd10cm-subset.tsv (CMS FY2027, public domain) and its rules in rules.yaml.
SIE pipeline:
           extract  fastino/gliner2-large-v1  problems, medications, allergies and follow-up,
                                              then vitals, from each window of about 512 tokens
           extract  fastino/gliner2-large-v1  dose, route and frequency of each active drug, on
                                              the sentence that gives its dose
           score    Qwen/Qwen3-Reranker-4B    rank the code-set subset for each problem
Output:    drafts/<note>.json and drafts/<note>.md (the draft filed with the outside note: each
           item new / matches_chart / differs_from_chart with the sentence it came from, code
           candidates, vitals with flags, what was left out and why), notes.csv and
           review-queue.csv split between the clinician and the coder.
Why SIE:   No generative model writes the draft. Two specialist models on SIE; every value is
           checked against the note in Python, and the chart comparison, dose units, vital
           ranges, allergy classes and code thresholds are plain rules in rules.yaml.
Run using: SIE Serverless, SIE Cluster or SIE Local (with SIE_BASE_URL).

    uv run recipe.py
"""

from __future__ import annotations

import argparse
import csv
import functools
import json
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

import yaml
from dotenv import load_dotenv
from ehr import EHR, ChartAllergy, ChartMedication, ChartProblem, ChartVital, OutsideDocument
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
# When GLiNER2 finds nothing, SIE Serverless answers the output_schema call with HTTP 500 and this
# message. For a list window that is a review reason (a note with no problems or medications
# read is not a clean draft); for a vitals window or a drug's details it means "nothing found",
# and the checks that need the value send the note to review.
EMPTY_RESULT_ERROR = "Inference completed without a valid conserved billing settlement"

# Note status: what the checks found. The clinician reconciles the draft with the patient at the
# first visit and the coder confirms codes; nothing is written into the chart.
CHECKS_PASSED, CLINICAL_REVIEW, CODER_REVIEW = "checks_passed", "clinical_review", "coder_review"
# Each abstracted item, compared with the chart.
NEW, MATCHES, DIFFERS = "new", "matches_chart", "differs_from_chart"
CLINICIAN, CODER = "clinician", "coder"
NOTE_FIELDS = (
    *("note", "patient_id", "source_clinic", "visit_date", "status"),
    *("new", "matches_chart", "differs_from_chart", "clinician_items", "coder_items"),
)
REVIEW_FIELDS = ("note", "patient_id", "status", "reviewer", "item", "reason")


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def load_codes(rules: dict[str, Any]) -> dict[str, str]:
    """The practice's code-set subset: code -> official description."""
    with (ROOT / rules["code_subset"]).open(encoding="utf-8") as handle:
        return {row["code"]: row["description"] for row in csv.DictReader(handle, delimiter="\t")}


@dataclass
class Draft:
    """What one outside note adds to the chart, for the clinician and the coder to review."""

    document: OutsideDocument
    problems: list[dict[str, Any]] = field(default_factory=list)
    medications: list[dict[str, Any]] = field(default_factory=list)
    allergies: dict[str, Any] = field(default_factory=dict)
    vitals: dict[str, dict[str, Any]] = field(default_factory=dict)
    follow_up: str | None = None
    left_out: list[dict[str, str]] = field(default_factory=list)
    chart_only: list[dict[str, str]] = field(default_factory=list)
    review: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        reviewers = {item["reviewer"] for item in self.review}
        if CLINICIAN in reviewers:
            return CLINICAL_REVIEW
        if CODER in reviewers:
            return CODER_REVIEW
        return CHECKS_PASSED

    def flag(self, reviewer: str, item: str, reason: str) -> None:
        entry = {"reviewer": reviewer, "item": item, "reason": reason}
        if entry not in self.review:
            self.review.append(entry)

    def items(self) -> list[dict[str, Any]]:
        return [*self.problems, *self.medications, *self.allergies.get("items", [])]

    def as_json(self) -> dict[str, Any]:
        doc = self.document
        return {
            "note": doc.document_id,
            "patient_id": doc.patient_id,
            "source_clinic": doc.source_clinic,
            "visit_date": doc.visit_date,
            "received_on": doc.received_on,
            "status": self.status,
            "problems": self.problems,
            "medications": self.medications,
            "allergies": self.allergies,
            "vitals": self.vitals,
            "follow_up": self.follow_up,
            "left_out": self.left_out,
            "on_chart_not_in_note": self.chart_only,
            "review_items": self.review,
            "notes": self.notes,
        }


# --- Text helpers -----------------------------------------------------------------------------


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def phrase_re(phrase: str) -> re.Pattern[str]:
    """A whole-word, case-insensitive pattern for a phrase in squashed text."""
    return re.compile(r"(?<!\w)" + re.escape(squash(phrase)) + r"(?!\w)")


def contains_phrase(text: str, phrase: str) -> bool:
    """Case-insensitive whole-word match, tolerant of line wraps."""
    return bool(phrase.strip()) and phrase_re(phrase).search(squash(text)) is not None


def terms_re(terms: list[str]) -> re.Pattern[str]:
    """Any of the terms as a whole word; the longest wins where two start at the same place."""
    ordered = sorted((re.escape(squash(term)) for term in terms), key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(ordered) + r")(?!\w)")


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


def note_lines(text: str) -> list[str]:
    """The note's lines, a wrapped line (starting lowercase) joined to the one before."""
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if lines and stripped[0].islower():
            lines[-1] += " " + stripped
        else:
            lines.append(stripped)
    return lines


def sentences(text: str) -> list[str]:
    """The note's lines split into sentences. A list number ('1. ') stays with its sentence."""
    parts: list[str] = []
    for line in note_lines(text):
        marker = re.match(r"\d+[.)]\s+", line)
        prefix = marker.group(0) if marker else ""
        split = [part.strip() for part in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line[len(prefix) :])]
        parts += [prefix + split[0], *split[1:]]
    return [part for part in parts if part]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")


# --- SIE calls --------------------------------------------------------------------------------


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


class SIEStopped(RuntimeError):
    """SIE refused the call for a reason a retry won't fix, or no call succeeded; the run
    stops without outputs."""


class CallFailed(Exception):
    """One SIE call failed for a reason that may pass; the note it was for goes to review."""


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
        reply without `key`, is recorded and raised as CallFailed, so the caller sends its note
        to review and the run goes on. With `empty_result_ok` (the GLiNER2 calls), SIE Serverless's
        empty-result HTTP 500 is a reply with nothing in it, marked `empty_result_error`, not a
        failure. An error that isn't an SIE error is a bug in this code and is raised as is.
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
        print(f"{n} of {total} SIE calls failed; their notes go to review. First: {short(first)}")


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
    those notes go to review."""
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


def stop_on_refusal[T](work: Callable[[], T]) -> T:
    """Run the work; turn SIEStopped into a clean exit. Any other error keeps its traceback."""
    try:
        return work()
    except SIEStopped as error:
        raise SystemExit(f"Stopped: {error}") from None


def object_schema(fields: dict[str, Any]) -> dict[str, Any]:
    """A GLiNER2 output_schema: string fields, or lists of strings, each with a description."""
    properties: dict[str, Any] = {}
    for name, spec in fields.items():
        if isinstance(spec, str):
            spec = {"type": "string", "description": spec}
        prop: dict[str, Any] = (
            {"type": "array", "items": {"type": "string"}}
            if spec["type"] == "list"
            else {"type": "string"}
        )
        properties[name] = prop | {"description": spec["description"]}
    return {"type": "object", "properties": properties}


def medication_schema(rules: dict[str, Any], drug: str) -> dict[str, Any]:
    fields = {name: text.format(drug=drug) for name, text in rules["medication_fields"].items()}
    return object_schema(fields)


def extract_fields(
    client: SIEClient,
    calls: Calls,
    key: str,
    rules: dict[str, Any],
    schema: dict[str, Any],
    text: str,
) -> tuple[dict[str, Any], bool]:
    """One GLiNER2 output_schema call: (fields found, whether SIE answered with its empty-result
    500). One item per call: that 500 would fail a whole batch. Raises CallFailed."""
    model = rules["models"]["extract"]
    work = functools.partial(
        client.extract,
        model,
        Item(id=key, text=text),
        output_schema=schema,
        options={"overflow_policy": "error"},  # a window too long to read whole fails
        provision_timeout_s=WAIT_S,
    )
    reply = calls.call(model, work, "data", empty_result_ok=True)
    data = reply.get("data")
    return (data if isinstance(data, dict) else {}), "empty_result_error" in reply


def rank_codes(
    client: SIEClient, calls: Calls, rules: dict[str, Any], codes: dict[str, str], query: str
) -> list[tuple[str, float]]:
    """Score every subset code against the problem's sentence, best first. Raises CallFailed."""
    model = rules["models"]["rerank"]
    items = [Item(id=code, text=description) for code, description in codes.items()]
    work = functools.partial(
        client.score,
        model,
        Item(text=query),
        items,
        instruction=rules["code_instruction"],
        provision_timeout_s=WAIT_S,
    )
    reply = calls.call(model, work, "scores")
    ranked = [
        (str(entry["item_id"]), float(entry["score"]))
        for entry in reply["scores"]
        if isinstance(entry, dict) and str(entry.get("item_id")) in codes
    ]
    if len(ranked) != len(codes):
        missing = SIEError(f"{model} scored {len(ranked)} of {len(codes)} codes")
        calls.failures.append((model, missing))
        raise CallFailed(f"the SIE call failed: {short(missing)}")
    return sorted(ranked, key=lambda pair: pair[1], reverse=True)


# --- Checks (plain Python) --------------------------------------------------------------------


def grounded(values: list[str], text: str) -> tuple[list[str], list[str]]:
    """Split model values into those found in the text and those that are not."""
    kept, dropped = [], []
    for value in values:
        cleaned = value.strip()
        if cleaned and re.search(r"[^\W_]", cleaned) and squash(cleaned) in squash(text):
            kept.append(cleaned)
        elif cleaned:
            dropped.append(cleaned)
    return kept, dropped


def dedupe(values: list[str]) -> list[str]:
    """Drop repeats and values contained in a longer one ("hypertension" in "essential ...")."""
    kept: list[str] = []
    for value in sorted(values, key=len, reverse=True):
        if not any(phrase_re(value).search(squash(other)) for other in kept):
            kept.append(value)
    order = {squash(value): index for index, value in reversed(list(enumerate(values)))}
    return sorted(kept, key=lambda value: order[squash(value)])


def strings(found: list[dict[str, Any]], name: str) -> list[str]:
    values: list[str] = []
    for data in found:
        value = data.get(name)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values += [item for item in value if isinstance(item, str)]
    return values


def mentions(name: str, note_sentences: list[str]) -> list[str]:
    return [sentence for sentence in note_sentences if contains_phrase(sentence, name)]


def medication_name(value: str) -> str:
    """The drug name in a span such as 'Lisinopril 10 mg PO once daily' -> 'Lisinopril'."""
    name = re.split(r"\s+(?=\d)", value.strip(), maxsplit=1)[0].strip(" ,;:-")
    return re.sub(r"^(?:start|stop|continue)\s+", "", name, flags=re.I)


def first_word(name: str) -> str:
    words = squash(name).split()
    return words[0] if words else ""


# Medications: active or stopped, and the dose written for each.


def med_action(name: str, sentence: str, rules: dict[str, Any]) -> str | None:
    """'stopped', 'active' or None (no cue): the cue nearest the drug's last mention in the
    sentence, in the same clause (clauses end at ';'). A cue before the drug wins a tie."""
    text = squash(sentence)
    found = list(phrase_re(name).finditer(text))
    if not found:
        return None
    at = found[-1]
    start = text.rfind(";", 0, at.start()) + 1
    end = text.find(";", at.end())
    end = len(text) if end < 0 else end
    stops = {squash(term) for term in rules["stop_terms"]}
    cues = terms_re([*rules["stop_terms"], *rules["start_terms"]])
    best: tuple[int, int, str] | None = None
    for cue in cues.finditer(text, start, end):
        if cue.end() <= at.start():
            rank = (at.start() - cue.end(), 0)
        elif cue.start() >= at.end():
            rank = (cue.start() - at.end(), 1)
        else:
            continue
        kind = "stopped" if cue.group(0) in stops else "active"
        if best is None or rank < best[:2]:
            best = (*rank, kind)
    return best[2] if best else None


def med_state(name: str, said: list[str], rules: dict[str, Any]) -> tuple[str, str]:
    """(active or discontinued, the sentence that decides it). The last mention with a cue
    decides; a drug only listed, with no cue anywhere, is active."""
    deciding = None
    for sentence in said:
        action = med_action(name, sentence, rules)
        if action is not None:
            deciding = (action, sentence)
    if deciding is None:
        return "active", said[-1]
    return ("discontinued" if deciding[0] == "stopped" else "active"), deciding[1]


def dose_re(rules: dict[str, Any]) -> re.Pattern[str]:
    """A number with a dose unit, glued or spaced ('50mg', '50 mg'); not '12 g/dL'."""
    units = "|".join(sorted(map(re.escape, rules["dose_units"]), key=len, reverse=True))
    return re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(" + units + r")(?![\w/])", re.I)


def drug_doses(
    drug: str, sentence: str, other_drugs: list[str], rules: dict[str, Any]
) -> list[str]:
    """Doses written after the drug in the sentence, before any other drug or a ';'."""
    text = squash(sentence)
    found = list(phrase_re(drug).finditer(text))
    if not found:
        return []
    doses: list[str] = []
    for match in dose_re(rules).finditer(text, found[0].end()):
        between = text[found[0].end() : match.start()]
        others = [d for d in other_drugs if squash(d) != squash(drug)]
        if ";" in between or any(phrase_re(other).search(between) for other in others):
            break
        doses.append(f"{match.group(1)} {match.group(2)}")
    return doses


def parse_dose(dose: str) -> tuple[float, str] | None:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([A-Za-z%]+)\.?", dose.strip())
    return (float(match.group(1)), match.group(2).casefold()) if match else None


def same_dose(a: str, b: str) -> bool:
    left, right = parse_dose(a), parse_dose(b)
    return left is not None and left == right


def check_dose(drug: str, dose: str, rules: dict[str, Any]) -> str | None:
    """None when the dose has a number and an allowed unit for this drug, else the reason."""
    parsed = parse_dose(dose)
    if parsed is None:
        return f"{drug} dose '{dose}' is not a number with a unit"
    unit = parsed[1]
    if unit not in [u.casefold() for u in rules["dose_units"]]:
        return f"{drug} dose '{dose}': unit is not one of {rules['dose_units']}"
    for key, units in rules["expected_units"].items():
        if contains_phrase(drug, key) and unit not in [u.casefold() for u in units]:
            return f"{drug} dose '{dose}' is in {unit}; {key} is dosed in {' or '.join(units)}"
    return None


def compare_medication(med: dict[str, Any], chart: list[ChartMedication]) -> tuple[str, str]:
    """(new / matches_chart / differs_from_chart, what the chart says). A match needs the same
    drug name and the same dose; a similar name (same first word) is a difference to check."""
    name = med["name"]
    for row in chart:
        if squash(row.drug) != squash(name):
            continue
        on_chart = f"the chart lists {row.drug} {row.dose} {row.frequency} ({row.source})"
        if med["status"] == "discontinued":
            return DIFFERS, f"{on_chart}; the note says it was stopped"
        if med.get("dose") and same_dose(med["dose"], row.dose):
            return MATCHES, on_chart
        if med.get("dose"):
            return DIFFERS, f"the note says {med['dose']}; {on_chart}"
        return DIFFERS, f"the note gives no dose; {on_chart}"
    for row in chart:
        if first_word(row.drug) == first_word(name):
            return DIFFERS, f"the chart lists '{row.drug} {row.dose}', a similar name: same drug?"
    return NEW, "not on the chart"


# Problems: whose problem it is, and whether the note asserts it.


def assertion(phrase: str, sentence: str, rules: dict[str, Any]) -> str:
    """How the sentence gives the problem: 'family', 'negated', 'uncertain' or 'affirmed'.

    Family history: a family cue earlier in the sentence (up to a ';'). Negated: a negation cue
    in the few words before it in the same clause, or 'ruled out' just after. Uncertain:
    'possible', 'suspected', 'rule out' and the like just before it.
    """
    text = squash(sentence)
    found = list(phrase_re(phrase).finditer(text))
    if not found:
        return "affirmed"
    at = found[-1]
    sentence_before = text[text.rfind(";", 0, at.start()) + 1 : at.start()]
    if terms_re(rules["family_terms"]).search(sentence_before):
        return "family"
    clause = re.split(r"[,;:]", sentence_before)[-1]
    clause = terms_re(rules["not_negating"]).sub(" ", clause)
    near = " ".join(clause.split()[-int(rules["cue_window_words"]) :])
    after = re.split(r"[,;:.]", text[at.end() :])[0]
    if terms_re(rules["negation_terms"]).search(near):
        return "negated"
    if re.match(r"\s*(?:" + terms_re(rules["ruled_out_after"]).pattern + ")", after):
        return "negated"
    if terms_re(rules["uncertain_terms"]).search(near):
        return "uncertain"
    return "affirmed"


def problem_state(problem: str, said: list[str], rules: dict[str, Any]) -> tuple[str, str]:
    """(state, sentence): the last mention that is not about a relative decides. A problem
    only ever mentioned for a relative is 'family'."""
    states = [(assertion(problem, sentence, rules), sentence) for sentence in said]
    own = [pair for pair in states if pair[0] != "family"]
    return own[-1] if own else states[-1]


def code_query(problem: str, sentence: str) -> str:
    """What the reranker ranks codes against: the problem phrase, then its sentence, so two
    problems in one sentence ("Asthma and allergic rhinitis") get different queries."""
    if squash(problem) == squash(sentence):
        return problem
    return f"{problem}: {sentence}"


def logodds(p: float) -> float:
    p = min(max(p, 1e-9), 1 - 1e-9)
    return math.log(p / (1 - p))


def suggest_code(
    ranked: list[tuple[str, float]], rules: dict[str, Any]
) -> tuple[str | None, float, str | None]:
    """(code or None, log-odds margin, reason for the coder or None)."""
    limits = rules["thresholds"]
    best, score = ranked[0]
    margin = logodds(score) - logodds(ranked[1][1]) if len(ranked) > 1 else math.inf
    if score < limits["min_code_score"]:
        reason = f"best code {best} scored {score:.2f}, below {limits['min_code_score']}"
        return None, margin, reason
    if margin < limits["min_code_margin_logodds"]:
        runner_up = ranked[1][0]
        reason = (
            f"{best} and {runner_up} are too close (log-odds margin {margin:.2f}, "
            f"below {limits['min_code_margin_logodds']})"
        )
        return None, margin, reason
    return best, margin, None


def compare_problem(problem: str, code: str | None, chart: list[ChartProblem]) -> tuple[str, str]:
    """A match needs the same problem name, or the same suggested code. A suggested code in the
    same ICD-10-CM category as a chart problem's different code is a difference to check."""
    for row in chart:
        if squash(row.problem) == squash(problem):
            return MATCHES, f"on the chart as '{row.problem}' ({row.code})"
    for row in chart:
        if code and row.code == code:
            return MATCHES, f"same code as the chart's '{row.problem}' ({row.code})"
    for row in chart:
        if code and row.code[:3] == code[:3]:
            return DIFFERS, f"the chart has '{row.problem}' ({row.code}); the note ranks {code}"
    return NEW, "not on the chart"


# Allergies.


def allergy_name_text(value: str) -> str:
    """The text without its bracketed reactions: 'Penicillin (hives, rash)' -> 'Penicillin'."""
    return re.sub(r"\s*\([^)]*\)", "", value)


def allergy_name(value: str) -> str:
    """'Penicillin (hives)' -> 'Penicillin'."""
    return allergy_name_text(value).strip(" .,;:")


def allergy_lines(text: str, rules: dict[str, Any]) -> list[tuple[str, str]]:
    """The note's allergy lines that have text after their label, with that text
    ('Allergies: Codeine' -> 'Codeine')."""
    labels = sorted(map(re.escape, rules["allergy_labels"]), key=len, reverse=True)
    pattern = re.compile(r"(?i)^(?:" + "|".join(labels) + r")\s*:\s*(\S.*)$")
    return [(line, m.group(1)) for line in note_lines(text) if (m := pattern.match(line))]


def unread_allergies(text: str, read: list[str], rules: dict[str, Any]) -> list[tuple[str, str]]:
    """Each item on an allergy line that names no allergy that was read and no 'no known
    allergies' term, with its line: 'Codeine' in 'Allergies: Penicillin (hives), Codeine
    (anaphylaxis).' when only Penicillin was read."""
    known = read + rules["no_known_allergy_terms"]
    split = "|".join(map(re.escape, rules["allergy_separators"]))
    unread = []
    for line, after in allergy_lines(text, rules):
        for item in re.split(split, allergy_name_text(after)):
            item = item.strip(" .")
            if item and not any(contains_phrase(item, name) for name in known):
                unread.append((item, line))
    return unread


def allergy_members(substance: str, chart_class: str, rules: dict[str, Any]) -> set[str]:
    """Every drug name the allergy rules out: its class (named, or the class of a member it
    names, or the class the chart records) and the substance itself."""
    members = {squash(substance)}
    for group, names in rules["allergy_classes"].items():
        if (
            group == chart_class
            or contains_phrase(substance, group)
            or any(contains_phrase(substance, name) for name in names)
        ):
            members |= {squash(name) for name in names}
    return members


def allergy_conflicts(
    allergies: list[tuple[str, str, str]], active_meds: list[tuple[str, str]], rules: dict[str, Any]
) -> list[str]:
    """Reasons for every active drug in the class of an allergy. `allergies` holds (substance,
    chart class or '', where it came from); `active_meds` holds (drug, where it came from)."""
    reasons = []
    for substance, chart_class, allergy_source in allergies:
        members = allergy_members(substance, chart_class, rules)
        for med, med_source in active_meds:
            if any(contains_phrase(med, member) for member in members):
                reasons.append(
                    f"{med} ({med_source}) conflicts with the '{substance}' allergy "
                    f"({allergy_source})"
                )
    return reasons


# Vitals.


def labelled_readings(name: str, text: str, rules: dict[str, Any]) -> list[str]:
    """Every reading the note writes right after one of the vital's labels ('HR 88')."""
    labels = "|".join(sorted(map(re.escape, rules["vital_labels"][name]), key=len, reverse=True))
    value = (
        r"\d{2,3}\s*/\s*\d{2,3}"
        if name == "blood_pressure"
        else r"\d+(?:\.\d+)?(?!\d|\.\d|\s*/\s*\d)"
    )
    pattern = (
        r"(?<![A-Za-z0-9])(?:" + labels + r")(?![A-Za-z])\s*(?:[:=]|of|is|was)?\s*(" + value + ")"
    )
    readings = [re.sub(r"\s+", "", m.group(1)) for m in re.finditer(pattern, text, re.I)]
    return list(dict.fromkeys(readings))


def _flag(value: float, normal: list[float] | None) -> str | None:
    if normal is None:
        return None
    if value < normal[0]:
        return "low"
    if value > normal[1]:
        return "high"
    return None


def check_vital(
    name: str, value: str, readings: list[str], rules: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """(recorded entry, review reason or None) for one vital the model read. The value counts
    only when the note writes that number after one of the vital's labels."""
    limits = rules["vitals"]
    label = name.replace("_", " ")
    if name == "blood_pressure":
        match = re.search(r"(\d{2,3})\s*/\s*(\d{2,3})", value)
        reading = f"{match.group(1)}/{match.group(2)}" if match else ""
        if match is None or reading not in readings:
            return (
                None,
                f"Model read blood pressure '{value}', which is not a BP reading in the note",
            )
        entry: dict[str, Any] = {"value": reading}
        for part, number in (("systolic", match.group(1)), ("diastolic", match.group(2))):
            low, high = limits[part]["plausible"]
            if not low <= float(number) <= high:
                return entry, f"{part.capitalize()} {number} is outside {low}-{high}: likely a typo"
            if flag := _flag(float(number), limits[part].get("normal")):
                entry[f"{part}_flag"] = flag
        return entry, None
    match = re.search(r"\d+(?:\.\d+)?", value)
    if match is None or not any(float(match.group(0)) == float(r) for r in readings):
        return None, f"Model read {label} '{value}', which is not a {label} reading in the note"
    number = float(match.group(0))
    unit = ""
    if name == "temperature":
        is_celsius = re.search(r"°?\s*C\b", value) is not None or number < 45
        unit = "C" if is_celsius else "F"
        number = number * 9 / 5 + 32 if is_celsius else number
    if name == "weight":
        unit = "lb" if re.search(r"\blbs?\b", value, re.I) else "kg"
        number = number * 0.45359237 if unit == "lb" else number
    entry = {"value": value.strip()}
    low, high = limits[name]["plausible"]
    if not low <= number <= high:
        shown = f"{match.group(0)} {unit}".strip()
        return entry, f"{label.capitalize()} {shown} is outside {low}-{high}: likely a typo"
    if flag := _flag(number, limits[name].get("normal")):
        entry["flag"] = flag
    return entry, None


# The note's header.


MONTHS = {
    name.casefold(): number
    for number in range(1, 13)
    for name in (date(2000, number, 1).strftime("%B"), date(2000, number, 1).strftime("%b"))
} | {"sept": 9}


def parse_dob(raw: str, before: date) -> date | None:
    """A date of birth at the start of `raw`: 4/22/1968, 4-22-1968, 1968-04-22, 4/22/68,
    'April 22, 1968', '22 Apr 1968'. A two-digit year takes the century that puts the date on
    or before `before` (the day the note arrived). None when no such date can be read."""
    text = raw.strip()
    parts: tuple[int, int, int] | None = None  # year, month, day
    if match := re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)", text):
        parts = int(match.group(1)), int(match.group(2)), int(match.group(3))
    elif match := re.match(r"(\d{1,2})([/-])(\d{1,2})\2(\d{4}|\d{2})(?![\d/-])", text):
        year = int(match.group(4))
        if len(match.group(4)) == 2:
            year += (
                2000 if (2000 + year, int(match.group(1))) <= (before.year, before.month) else 1900
            )
        parts = year, int(match.group(1)), int(match.group(3))
    elif match := re.match(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})(?!\d)", text):
        month = MONTHS.get(match.group(1).casefold())
        parts = (int(match.group(3)), month, int(match.group(2))) if month else None
    elif match := re.match(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})(?!\d)", text):
        month = MONTHS.get(match.group(2).casefold())
        parts = (int(match.group(3)), month, int(match.group(1))) if month else None
    if parts is None:
        return None
    try:
        born = date(*parts)
    except ValueError:
        return None
    return born if born <= before else None


def header_patient(
    text: str, rules: dict[str, Any], before: date | None = None
) -> tuple[str | None, date | None, str | None]:
    """(name, date of birth, the DOB text) the note's header prints after its labels. The date
    is None when the DOB text is missing or can't be read as a date on or before `before`."""
    names = "|".join(map(re.escape, sorted(rules["header_labels"]["name"], key=len, reverse=True)))
    dobs = "|".join(map(re.escape, sorted(rules["header_labels"]["dob"], key=len, reverse=True)))
    dob_label = r"(?<![A-Za-z])(?:" + dobs + r")(?![A-Za-z])\s*:?"
    name = re.search(
        r"(?im)^\s*(?:" + names + r")\s*:\s*(.+?)(?=\s{2,}|\s+" + dob_label + r"|\s*$)", text
    )
    dob = re.search(r"(?im)" + dob_label + r"\s*(.+?)\s*$", text)
    raw = re.split(r"\s{2,}", dob.group(1))[0] if dob else None
    born = parse_dob(raw, before or datetime.now(UTC).date()) if raw else None
    return (name.group(1).strip() if name else None), born, raw


# --- Build the draft --------------------------------------------------------------------------


def read_windows(
    client: SIEClient, calls: Calls, draft: Draft, rules: dict[str, Any]
) -> tuple[list[dict[str, Any]] | None, list[tuple[str, dict[str, Any]]] | None]:
    """Run GLiNER2 twice on each window: once for the lists, once for the vitals. None for the
    lists or the vitals when a call for them failed (the note goes to clinical review)."""
    doc, limits = draft.document, rules["thresholds"]
    parts = windows(doc.text, limits["window_words"], limits["window_overlap_words"])
    lists: list[dict[str, Any]] | None = []
    vitals: list[tuple[str, dict[str, Any]]] | None = []
    list_schema, vitals_schema = (
        object_schema(rules["record_fields"]),
        object_schema(rules["vital_fields"]),
    )
    for index, window in enumerate(parts, start=1):
        try:
            found, empty = extract_fields(
                client, calls, f"lists/{doc.document_id}/w{index}", rules, list_schema, window
            )
            if empty:
                draft.flag(
                    CLINICIAN,
                    "readable",
                    f"window {index}: the extraction model found no problem, medication or "
                    "allergy (SIE's empty-result HTTP 500); read the note by hand",
                )
            if lists is not None:
                lists.append(found)
        except CallFailed as failure:
            draft.flag(CLINICIAN, "readable", f"window {index} was not read: {failure}")
            lists = None
        try:
            found, empty = extract_fields(
                client, calls, f"vitals/{doc.document_id}/w{index}", rules, vitals_schema, window
            )
            if empty:
                draft.notes.append(f"window {index}: no vitals found (HTTP 500 empty result)")
            if vitals is not None:
                vitals.append((window, found))
        except CallFailed as failure:
            draft.flag(CLINICIAN, "vitals", f"vitals in window {index} were not read: {failure}")
            vitals = None
    return lists, vitals


def check_header(draft: Draft, ehr: EHR, rules: dict[str, Any]) -> None:
    patient = ehr.patient(draft.document.patient_id)
    if patient is None:
        draft.flag(CLINICIAN, "patient", f"no patient {draft.document.patient_id} in the chart")
        return
    received = date.fromisoformat(draft.document.received_on)
    name, born, raw = header_patient(draft.document.text, rules, received)
    if name is None and raw is None:
        draft.notes.append("The note's header gives no patient name or date of birth to check")
        return
    chart = f"the chart is '{patient.name}', born {patient.dob}"
    if name is not None and squash(name) != squash(patient.name):
        draft.flag(
            CLINICIAN,
            "patient",
            f"the note names '{name}'; {chart}: check the note is filed in the right chart",
        )
    if raw is not None and born is None:
        draft.flag(
            CLINICIAN,
            "patient",
            f"the note's date of birth '{raw}' could not be read as a date; {chart}: check it",
        )
    elif born is not None and born.isoformat() != patient.dob:
        draft.flag(
            CLINICIAN,
            "patient",
            f"the note gives a date of birth of {born.isoformat()}; {chart}: check the note is "
            "filed in the right chart",
        )
    if name is None or raw is None:
        missing = "patient name" if name is None else "date of birth"
        draft.notes.append(f"The note's header gives no {missing} to check")


def add_problems(
    client: SIEClient,
    calls: Calls,
    draft: Draft,
    lists: list[dict[str, Any]],
    rules: dict[str, Any],
    codes: dict[str, str],
    chart: list[ChartProblem],
) -> None:
    text = draft.document.text
    note_sentences = sentences(text)
    values, dropped = grounded(strings(lists, "problems"), text)
    for value in dropped:
        draft.notes.append(f"Dropped problem '{value}': not in the note")
    shown = int(rules["thresholds"]["code_candidates_shown"])
    assessment = [
        squash(line) for line in note_lines(text) if re.match(rules["assessment_item"], line)
    ]
    for problem in dedupe(values):
        said = mentions(problem, note_sentences) or [problem]
        state, sentence = problem_state(problem, said, rules)
        if state in ("family", "negated"):
            why = "family history" if state == "family" else "negated or ruled out"
            draft.left_out.append(
                {"kind": "problem", "item": problem, "reason": why, "sentence": sentence}
            )
            # A negation cue outside a numbered assessment line ("No complications from type 2
            # diabetes") may not negate the problem itself; the line check below covers those.
            if state == "negated" and not any(squash(sentence) in line for line in assessment):
                draft.flag(
                    CLINICIAN,
                    f"problem:{problem}",
                    f"read as negated and left out: '{sentence}': check it",
                )
            continue
        if state == "uncertain":
            draft.flag(CLINICIAN, f"problem:{problem}", f"given as uncertain: '{sentence}'")
        try:
            ranked = rank_codes(client, calls, rules, codes, code_query(problem, sentence))
        except CallFailed as failure:
            draft.flag(CLINICIAN, f"problem:{problem}", f"code candidates not ranked: {failure}")
            comparison, detail = compare_problem(problem, None, chart)
            draft.problems.append(
                {"problem": problem, "sentence": sentence, "comparison": comparison}
                | {"chart": detail, "code_status": "not_ranked", "code": None, "candidates": []}
            )
            continue
        code, margin, reason = suggest_code(ranked, rules)
        comparison, detail = compare_problem(problem, code, chart)
        draft.problems.append(
            {
                "problem": problem,
                "sentence": sentence,
                "comparison": comparison,
                "chart": detail,
                "code_status": "suggested" if code else "to_coder",
                "code": code,
                "description": codes[code] if code else None,
                "score": round(ranked[0][1], 4),
                "margin_logodds": round(margin, 2),
                "candidates": [
                    {"code": c, "description": codes[c], "score": round(s, 4)}
                    for c, s in ranked[:shown]
                ],
            }
        )
        if reason:
            draft.flag(CODER, f"problem:{problem}", f"No code suggested: {reason}")
        if comparison == DIFFERS:
            draft.flag(CLINICIAN, f"problem:{problem}", detail)
    # Every numbered assessment line should have given at least one problem that isn't negated.
    negated = {squash(o["item"]) for o in draft.left_out if o["reason"] == "negated or ruled out"}
    for line in note_lines(text):
        if not re.match(rules["assessment_item"], line):
            continue
        read = [value for value in dedupe(values) if contains_phrase(line, value)]
        if not any(contains_phrase(line, value) for value in values):
            draft.flag(CLINICIAN, "problems", f"no problem was read from '{line}'")
        elif read and all(squash(value) in negated for value in read):
            draft.flag(
                CLINICIAN,
                "problems",
                f"the only problem read from '{line}' was read as negated: check it",
            )
    if not values:
        draft.flag(CLINICIAN, "problems", "No problem was read from the note")


def add_medications(
    client: SIEClient,
    calls: Calls,
    draft: Draft,
    lists: list[dict[str, Any]],
    rules: dict[str, Any],
    chart: list[ChartMedication],
) -> None:
    text = draft.document.text
    note_sentences = sentences(text)
    skip = {squash(term) for term in rules["not_a_medication"]}
    names: list[str] = []
    for value in strings(lists, "medications"):
        name = medication_name(value)
        if not name or squash(name) in skip:
            continue
        if not contains_phrase(text, name):
            draft.notes.append(f"Dropped medication '{value}': not in the note")
            continue
        names.append(name)
    names = dedupe(names)
    for name in names:
        said = mentions(name, note_sentences)
        status, deciding = med_state(name, said, rules)
        med: dict[str, Any] = {"name": name, "status": status, "sentence": deciding}
        if status == "active":
            add_details(client, calls, draft, med, said, names, rules)
        med["comparison"], med["chart"] = compare_medication(med, chart)
        if med["comparison"] == DIFFERS:
            draft.flag(CLINICIAN, f"medication:{name}", med["chart"])
        draft.medications.append(med)
    # Every dose the note writes should belong to a medication that was read.
    for sentence in note_sentences:
        written = [f"{m.group(1)} {m.group(2)}" for m in dose_re(rules).finditer(squash(sentence))]
        claimed = {d for name in names for d in drug_doses(name, sentence, names, rules)}
        for dose in written:
            if dose not in claimed:
                draft.flag(
                    CLINICIAN,
                    "medications",
                    f"'{dose}' in '{sentence}' belongs to no medication that was read",
                )


def add_details(
    client: SIEClient,
    calls: Calls,
    draft: Draft,
    med: dict[str, Any],
    said: list[str],
    names: list[str],
    rules: dict[str, Any],
) -> None:
    """Dose, route and frequency of one active drug, from the last sentence that writes a dose
    for it (a dose changed in the plan beats the medication list)."""
    name = med["name"]
    written = {s: drug_doses(name, s, names, rules) for s in said}
    with_dose = [s for s in said if written[s] and med_action(name, s, rules) != "stopped"]
    sentence = with_dose[-1] if with_dose else med["sentence"]
    med["sentence"] = sentence
    doses = list(dict.fromkeys(d for s in with_dose for d in written[s]))
    if len(doses) > 1:
        draft.flag(
            CLINICIAN, f"medication:{name}", f"the note gives {name} as {' and '.join(doses)}"
        )
    key = f"med/{draft.document.document_id}/{slug(name)}"
    try:
        details, empty = extract_fields(
            client, calls, key, rules, medication_schema(rules, name), sentence
        )
    except CallFailed as failure:
        draft.flag(CLINICIAN, f"medication:{name}", f"{name} details were not read: {failure}")
        details, empty = {}, False
    if empty:
        draft.notes.append(f"{name}: no dose, route or frequency found (HTTP 500 empty result)")
    for detail in rules["medication_fields"]:
        raw = details.get(detail)
        kept, dropped = grounded([raw] if isinstance(raw, str) else [], sentence)
        med[detail] = kept[0] if kept else None
        for item in dropped:
            draft.notes.append(f"Dropped {name} {detail} '{item}': not in its sentence")
    if med["dose"] and not any(same_dose(med["dose"], d) for d in written[sentence]):
        draft.flag(
            CLINICIAN,
            f"medication:{name}",
            f"dose '{med['dose']}' is not written for {name} in '{sentence}'",
        )
        med["dose"] = None
    for detail in rules["required_medication_fields"]:
        if med.get(detail) is None:
            draft.flag(CLINICIAN, f"medication:{name}", f"{name}: {detail} not documented")
    if med["dose"] and (reason := check_dose(name, med["dose"], rules)):
        draft.flag(CLINICIAN, f"medication:{name}", reason)


def add_allergies(
    draft: Draft,
    lists: list[dict[str, Any]],
    rules: dict[str, Any],
    chart: list[ChartAllergy],
    chart_status: str,
) -> None:
    text = draft.document.text
    note_sentences = sentences(text)
    nkda_terms = rules["no_known_allergy_terms"]
    values, _ = grounded(strings(lists, "allergies"), text)
    names: list[str] = []
    read = dedupe([allergy_name(v) for v in values])
    for value in read:
        if not value or squash(value) in {squash(t) for t in nkda_terms}:
            continue
        said = mentions(value, note_sentences) or [value]
        if assertion(value, said[-1], rules) in ("negated", "family"):
            draft.left_out.append(
                {"kind": "allergy", "item": value, "reason": "negated", "sentence": said[-1]}
            )
            continue
        names.append(value)
    nkda = any(contains_phrase(text, term) for term in nkda_terms)
    in_note = "allergies_listed" if names else "no_known_allergies" if nkda else "not_documented"
    items = []
    for name in names:
        same = [row for row in chart if squash(row.substance) == squash(name)]
        if same:
            comparison, detail = MATCHES, f"on the chart: {same[0].substance} ({same[0].reaction})"
        elif chart_status == "not_recorded":
            comparison, detail = NEW, "the chart has no allergy status yet"
        else:
            on_chart = ", ".join(row.substance for row in chart) or "no known allergies"
            comparison, detail = DIFFERS, f"not on the chart, which says: {on_chart}"
            draft.flag(CLINICIAN, f"allergy:{name}", f"'{name}' is in the note; {detail}")
        items.append({"substance": name, "comparison": comparison, "chart": detail})
    if names and nkda:
        draft.flag(CLINICIAN, "allergies", "The note lists allergies and also says none known")
    if in_note != "not_documented":
        for row in chart:
            if not any(squash(row.substance) == squash(name) for name in names):
                reason = f"the chart lists a {row.substance} allergy; the note does not"
                draft.chart_only.append(
                    {"kind": "allergy", "item": row.substance, "detail": reason}
                )
                draft.flag(CLINICIAN, f"allergy:{row.substance}", reason)
    for item, line in unread_allergies(text, read, rules):
        draft.flag(CLINICIAN, "allergies", f"'{item}' in '{line}' was not read: read it by hand")
    if in_note == "not_documented" and chart_status == "not_recorded":
        draft.flag(CLINICIAN, "allergies", "Allergy status is not documented in the note or chart")
    draft.allergies = {"in_note": in_note, "on_chart": chart_status, "items": items}


def add_vitals(
    draft: Draft,
    found: list[tuple[str, dict[str, Any]]],
    rules: dict[str, Any],
    chart: list[ChartVital],
) -> None:
    text = draft.document.text
    for name in rules["vital_fields"]:
        readings = labelled_readings(name, text, rules)
        latest = next((row for row in chart if row.vital == name), None)
        context = (
            f"; the chart's last reading is {latest.value} on {latest.taken_on}" if latest else ""
        )
        entries: dict[str, Any] = {}
        reasons: list[str] = []
        for _window, data in found:
            value = data.get(name)
            if not isinstance(value, str) or not value.strip():
                continue
            entry, reason = check_vital(name, value, readings, rules)
            if reason:
                reasons.append(reason + context)
            if entry is not None:
                numbers = re.findall(r"\d+(?:\.\d+)?", entry["value"])
                entries.setdefault("/".join(numbers), entry)
        label = name.replace("_", " ")
        if len(readings) > 1:
            shown = ", ".join(readings)
            reasons.append(f"The note gives more than one {label} ({shown}); a person picks")
        if len(entries) > 1:
            values = ", ".join(f"'{entry['value']}'" for entry in entries.values())
            reasons.append(f"Windows disagree on {label}: {values}")
        if entries:
            entry = next(iter(entries.values()))
            if latest:
                entry["chart_latest"] = f"{latest.value} on {latest.taken_on}"
            draft.vitals[name] = entry
        elif readings and not reasons:
            reasons.append(f"The note gives {label} {readings[0]}, which the model did not read")
        for reason in dict.fromkeys(reasons):
            draft.flag(CLINICIAN, name, reason)


def check_allergy_conflicts(
    draft: Draft,
    rules: dict[str, Any],
    chart_meds: list[ChartMedication],
    chart: list[ChartAllergy],
) -> None:
    """Every active drug (in the note, or on the chart and not stopped in the note) against
    every allergy (in the note, or on the chart)."""
    stopped = {squash(m["name"]) for m in draft.medications if m["status"] == "discontinued"}
    active = [(m["name"], "in the note") for m in draft.medications if m["status"] == "active"]
    active += [
        (row.drug, "on the chart")
        for row in chart_meds
        if squash(row.drug) not in stopped
        and squash(row.drug) not in {squash(n) for n, _ in active}
    ]
    allergies = [(item["substance"], "", "in the note") for item in draft.allergies["items"]]
    allergies += [(row.substance, row.allergy_class, "on the chart") for row in chart]
    for reason in allergy_conflicts(allergies, active, rules):
        draft.flag(CLINICIAN, "allergies", reason)


def process_document(
    client: SIEClient,
    calls: Calls,
    doc: OutsideDocument,
    ehr: EHR,
    rules: dict[str, Any],
    codes: dict[str, str],
) -> Draft:
    draft = Draft(document=doc)
    patient = ehr.patient(doc.patient_id)
    check_header(draft, ehr, rules)
    lists, vitals = read_windows(client, calls, draft, rules)
    if vitals is not None:
        add_vitals(draft, vitals, rules, ehr.vitals(doc.patient_id))
    if lists is None or patient is None:
        return draft  # already flagged: nothing more can be drafted from this note
    chart_meds, chart_allergies = ehr.medications(doc.patient_id), ehr.allergies(doc.patient_id)
    add_problems(client, calls, draft, lists, rules, codes, ehr.problems(doc.patient_id))
    add_medications(client, calls, draft, lists, rules, chart_meds)
    add_allergies(draft, lists, rules, chart_allergies, patient.allergy_status)
    check_allergy_conflicts(draft, rules, chart_meds, chart_allergies)
    in_note = {squash(med["name"]) for med in draft.medications}
    for row in chart_meds:
        if squash(row.drug) not in in_note:
            detail = f"{row.drug} {row.dose} {row.frequency} ({row.source}); the note doesn't say"
            draft.chart_only.append({"kind": "medication", "item": row.drug, "detail": detail})
    follow_ups, _ = grounded(strings(lists, "follow_up"), doc.text)
    draft.follow_up = max(follow_ups, key=len) if follow_ups else None
    if draft.follow_up is None and rules["require_follow_up"]:
        draft.flag(CLINICIAN, "follow_up", "No follow-up plan documented")
    return draft


# --- Run --------------------------------------------------------------------------------------


def run(
    client: SIEClient, rules: dict[str, Any], data_dir: Path = DATA_DIR
) -> tuple[list[Draft], Calls]:
    ehr, codes, calls = EHR(data_dir), load_codes(rules), Calls()
    drafts = [
        process_document(client, calls, doc, ehr, rules, codes) for doc in ehr.outside_documents()
    ]
    calls.check_all_failed()
    return drafts, calls


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


def _cell(value: Any) -> str:
    return "" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def _table(header: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    return lines + ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]


def render_markdown(draft: Draft) -> str:
    """The draft as the clinician sees it next to the outside note at the first visit."""
    doc = draft.document
    out = [
        f"# Draft abstraction of {doc.document_id} (not reconciled)",
        "",
        f"Patient {doc.patient_id}. Outside note from {doc.source_clinic}, visit "
        f"{doc.visit_date}, received {doc.received_on}. Status: **{draft.status}**.",
        "",
        "Nothing here is in the chart yet. The clinician reconciles medications and allergies "
        "with the patient; the coder confirms every code.",
        "",
        "## Problems",
        "",
    ]
    out += _table(
        ["Problem", "Compared with chart", "Code candidate", "From the note"],
        [
            [
                p["problem"],
                f"{p['comparison']}: {p['chart']}",
                f"{p['code']} {p['description']}"
                if p["code"]
                else "to the coder: "
                + ", ".join(f"{c['code']} ({c['score']:.2f})" for c in p["candidates"]),
                p["sentence"],
            ]
            for p in draft.problems
        ],
    )
    out += ["", "## Medications", ""]
    out += _table(
        ["Drug", "Status", "Dose", "Route", "Frequency", "Compared with chart", "From the note"],
        [
            [
                m["name"],
                m["status"],
                m.get("dose"),
                m.get("route"),
                m.get("frequency"),
                f"{m['comparison']}: {m['chart']}",
                m["sentence"],
            ]
            for m in draft.medications
        ],
    )
    allergies = draft.allergies
    out += [
        "",
        "## Allergies",
        "",
        f"In the note: {allergies.get('in_note', 'not read')}. "
        f"On the chart: {allergies.get('on_chart', 'not read')}.",
        "",
    ]
    out += _table(
        ["Substance", "Compared with chart"],
        [[a["substance"], f"{a['comparison']}: {a['chart']}"] for a in allergies.get("items", [])],
    )
    out += ["", "## Vitals (from the outside visit)", ""]
    out += _table(
        ["Vital", "Value", "Flag", "Chart's last reading"],
        [
            [
                name.replace("_", " "),
                v["value"],
                ", ".join(str(v[k]) for k in v if k.endswith("flag")),
                v.get("chart_latest", ""),
            ]
            for name, v in draft.vitals.items()
        ],
    )
    out += ["", f"Follow-up planned by the outside clinic: {draft.follow_up or 'none read'}", ""]
    for title, reviewer in (("For the clinician", CLINICIAN), ("For the coder", CODER)):
        reasons = [
            f"- {r['item']}: {r['reason']}" for r in draft.review if r["reviewer"] == reviewer
        ]
        out += [f"## {title}", "", *(reasons or ["- nothing flagged"]), ""]
    if draft.chart_only:
        out += ["## On the chart, not in this note", ""]
        out += [f"- {c['kind']} {c['item']}: {c['detail']}" for c in draft.chart_only] + [""]
    if draft.left_out:
        out += ["## Left out of the draft", ""]
        out += [
            f"- {item['kind']} '{item['item']}': {item['reason']} ('{item['sentence']}')"
            for item in draft.left_out
        ] + [""]
    return "\n".join(out)


def write_outputs(output_dir: Path, drafts: list[Draft]) -> None:
    (output_dir / "drafts").mkdir(parents=True, exist_ok=True)
    for draft in drafts:
        stem = output_dir / "drafts" / draft.document.document_id
        text = json.dumps(draft.as_json(), indent=2, ensure_ascii=False) + "\n"
        stem.with_suffix(".json").write_text(text, encoding="utf-8")
        stem.with_suffix(".md").write_text(render_markdown(draft), encoding="utf-8")
    rows = []
    for draft in drafts:
        compared = [item["comparison"] for item in draft.items()]
        reviewers = [item["reviewer"] for item in draft.review]
        rows.append(
            {
                "note": draft.document.document_id,
                "patient_id": draft.document.patient_id,
                "source_clinic": draft.document.source_clinic,
                "visit_date": draft.document.visit_date,
                "status": draft.status,
                "new": compared.count(NEW),
                "matches_chart": compared.count(MATCHES),
                "differs_from_chart": compared.count(DIFFERS),
                "clinician_items": reviewers.count(CLINICIAN),
                "coder_items": reviewers.count(CODER),
            }
        )
    _write_csv(output_dir / "notes.csv", NOTE_FIELDS, rows)
    queue = [
        {
            "note": draft.document.document_id,
            "patient_id": draft.document.patient_id,
            "status": draft.status,
            **item,
        }
        for draft in drafts
        for item in draft.review
    ]
    _write_csv(output_dir / "review-queue.csv", REVIEW_FIELDS, queue)


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    started = time.monotonic()
    with make_client() as client:
        drafts, calls = stop_on_refusal(lambda: run(client, load_rules()))
    elapsed = time.monotonic() - started
    write_outputs(output, drafts)
    for draft in drafts:
        codes = ", ".join(p["code"] or f"? ({p['problem']})" for p in draft.problems)
        print(f"{draft.document.document_id:<8} {draft.status:<16} {codes}")
        for item in draft.items():
            name = item.get("problem") or item.get("name") or item.get("substance")
            print(f"{'':<8}   {item['comparison']:<19} {name}")
        for review in draft.review:
            print(f"{'':<8}   {review['reviewer']:<10} {review['reason']}")
    per_model = ", ".join(f"{count} to {model}" for model, count in calls.count.items())
    print(f"\n{calls.total} SIE calls ({per_model}) in {elapsed:.0f} s")
    print(
        "Statuses name what the checks found; the clinician reconciles each draft with the "
        "patient and the coder confirms codes."
    )
    print(f"drafts:       {output / 'drafts'}\nreview queue: {output / 'review-queue.csv'}")


if __name__ == "__main__":
    main()
