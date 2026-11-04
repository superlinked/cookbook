"""Draft the CMMS work-order completion form from the technician's notes.

Use case:  Work-order close-out: fill the completion form a technician skipped.
Who:       Maintenance technicians and their supervisors closing corrective and preventive
           work orders in the CMMS.
Problem:   The completion form asks for problem, cause and remedy codes, the component,
           actions, parts and quantities, labor, downtime and follow-up work. Technicians
           type a few lines of notes and skip most fields, so the history the reliability
           engineer, the storeroom and the planner rely on is full of blanks.
Input:     data/work_orders.csv (the CMMS fields: order id, booked asset, work type, reported
           and completed times in UTC, plus the technician's notes), data/parts_catalog.csv
           (the storeroom list) and rules.yaml (the code picklists and the field schema).
SIE pipeline:
           (Python)  split notes over 120 words into windows
           extract   fastino/GLiNER2.5-Decide  one choice question per picklist (problem,
                                               cause, remedy), a probability for every option
           extract   fastino/gliner2-large-v1  component, actions, parts used, labor,
                                               downtime and follow-up, as spans of the notes
           (Python)  check every field: codes on the picklist and clear enough, parts in the
                     catalog with a quantity, hours that parse and fit the order's times, no
                     other asset tag named in the notes
Output:    completion_forms.csv (one draft per order) and drafts.json (every field with its
           value, evidence, status and reason). A field is filled, blank or flagged; a form is
           checks_passed (nothing flagged) or review. A person confirms every draft before the
           CMMS record is saved; nothing is written to the CMMS.
Why SIE:   Two small encoder models; no generative model fills or decides anything. The
           picklists and schema are data you edit in rules.yaml, and there is
           nothing to host on SIE Serverless.
Run using: SIE Serverless, or SIE Cluster or SIE Local with SIE_BASE_URL.

    uv run recipe.py
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

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
ORDERS_PATH = ROOT / "data" / "work_orders.csv"
CATALOG_PATH = ROOT / "data" / "parts_catalog.csv"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
# How much of an SIE error message goes into a per-field reason or the summary line.
ERROR_CHARS = 200
# When GLiNER2 finds nothing at all, SIE Serverless can answer an output_schema call with HTTP 500
# and this message. We record it as "nothing found"; a missing field can only leave a field
# blank or flag a required one, never fill it.
EMPTY_RESULT_ERROR = "Inference completed without a valid conserved billing settlement"
# A cell starting with one of these runs as a formula in a spreadsheet.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")

FieldStatus = Literal["filled", "blank", "flagged"]
FormStatus = Literal["checks_passed", "review"]
FILLED: FieldStatus = "filled"
BLANK: FieldStatus = "blank"
FLAGGED: FieldStatus = "flagged"
CHECKS_PASSED: FormStatus = "checks_passed"
REVIEW: FormStatus = "review"
CODE_FIELDS = ("problem", "cause", "remedy")
# rules.yaml `fields` name -> form field it fills.
SPAN_FIELDS = {
    "component": "component",
    "actions": "actions",
    "parts_used": "parts_used",
    "labor": "labor_h",
    "downtime": "downtime_h",
    "follow_up": "follow_up",
}
FORM_FIELDS = (
    "wo_id",
    "asset_id",
    "work_type",
    "status",
    "problem",
    "cause",
    "remedy",
    "component",
    "actions",
    "parts_used",
    "labor_h",
    "downtime_h",
    "follow_up",
    "flagged_fields",
    "reasons",
)


@dataclass(frozen=True)
class WorkOrder:
    wo_id: str
    asset_id: str
    work_type: str
    reported_at: datetime
    completed_at: datetime
    notes: str

    @property
    def hours_open(self) -> float:
        return (self.completed_at - self.reported_at).total_seconds() / 3600


@dataclass
class Field:
    """One field of the completion form: what goes in the CMMS, the words it came from, and
    whether a person needs to look at it."""

    value: str = ""
    evidence: str = ""
    status: FieldStatus = BLANK
    reason: str = ""


@dataclass
class Form:
    wo_id: str
    asset_id: str
    work_type: str
    status: FormStatus
    fields: dict[str, Field]


@dataclass
class Calls:
    """Calls and failures per model, so a run where every call to a model failed stops."""

    made: dict[str, int] = field(default_factory=dict)
    failed: dict[str, list[SIEError]] = field(default_factory=dict)


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def load_orders(path: Path = ORDERS_PATH) -> list[WorkOrder]:
    with path.open(encoding="utf-8", newline="") as handle:
        return [
            WorkOrder(
                row["wo_id"].strip(),
                row["asset_id"].strip(),
                row["work_type"].strip(),
                _utc(row["reported_at"]),
                _utc(row["completed_at"]),
                row["notes"].strip(),
            )
            for row in csv.DictReader(handle)
        ]


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, str]:
    """Part number -> description, from the storeroom list."""
    with path.open(encoding="utf-8", newline="") as handle:
        return {
            row["part_no"].strip(): row["description"].strip() for row in csv.DictReader(handle)
        }


# --- Windows ----------------------------------------------------------------------------------


def windows(text: str, max_words: int) -> list[str]:
    """Split text into consecutive windows of at most ``max_words`` words.

    Both models read about 512 tokens. We split long notes here so that no model silently cuts
    one short, then merge the windows' answers back into one per order.
    """
    words = text.split()
    if not words:
        return [""]
    return [" ".join(words[i : i + max_words]) for i in range(0, len(words), max_words)]


def window_items(orders: list[WorkOrder], max_words: int) -> list[tuple[str, str]]:
    """(window id, text) for every window of every order; the id is ``<wo_id>#<n>``."""
    return [
        (f"{order.wo_id}#{number}", text)
        for order in orders
        for number, text in enumerate(windows(order.notes, max_words), start=1)
    ]


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


def code_schema(picklist: dict[str, Any]) -> dict[str, Any]:
    """One rules.yaml picklist as one GLiNER2.5-Decide choice question."""
    return {
        "answer": {
            "type": "choice",
            "instructions": picklist["question"],
            "criteria": {option["label"]: option["description"] for option in picklist["options"]},
        }
    }


def field_schema(rules: dict[str, Any]) -> dict[str, Any]:
    """The rules.yaml `fields` as one gliner2 output schema."""
    properties: dict[str, Any] = {}
    for name, spec in rules["fields"].items():
        prop: dict[str, Any] = (
            {"type": "array", "items": {"type": "string"}}
            if spec["type"] == "list"
            else {"type": "string"}
        )
        properties[name] = prop | {"description": spec["description"]}
    return {"type": "object", "properties": properties}


def call_sie(
    calls: Calls,
    model: str,
    work: Callable[[], Any],
    ids: list[str],
    has_answer: Callable[[dict[str, Any]], bool],
) -> dict[str, dict[str, Any]]:
    """Run one SIE call and return its result per window id.

    A call that fails for a reason that may pass (a lost connection, a timeout, a server
    error, a rate limit, an over-long input) marks every window in it with that error, so
    those fields are flagged and the run still writes its outputs. A refusal every call would
    repeat (see `is_fatal`) stops the run with SIEStopped. An error that isn't an SIE error is
    a bug in this code and is raised as is.

    A reply with the wrong number of results takes the transient path too. A reply where some
    result lacks its answer (``has_answer``) counts as a failed call toward the every-call-failed
    stop; the result itself is flagged later, when its fields are read.
    """
    calls.made[model] = calls.made.get(model, 0) + 1
    try:
        found = jsonable(work())
    except SIEError as error:
        if is_fatal(error):
            raise SIEStopped(stop_message(error, model)) from error
        calls.failed.setdefault(model, []).append(error)
        return {item_id: {"error": f"the SIE call failed: {short(error)}"} for item_id in ids}
    results = found if isinstance(found, list) else [found]
    if len(results) != len(ids):
        wrong = BadReply(f"the reply had {len(results)} results for {len(ids)} windows")
        calls.failed.setdefault(model, []).append(wrong)
        return {item_id: {"error": f"the SIE call failed: {short(wrong)}"} for item_id in ids}
    if not all(isinstance(r, dict) and has_answer(r) for r in results):
        calls.failed.setdefault(model, []).append(BadReply("a reply is missing its answer key"))
    return {
        item_id: result if isinstance(result, dict) else {}
        for item_id, result in zip(ids, results, strict=True)
    }


def check_calls(calls: Calls) -> None:
    """Stop the run if every call to one model failed; otherwise report the failures."""
    print("SIE calls: " + ", ".join(f"{made} to {model}" for model, made in calls.made.items()))
    for model, made in calls.made.items():
        failures = calls.failed.get(model, [])
        if failures and len(failures) == made:
            raise SIEStopped(
                f"Every SIE call failed ({made} of {made} to {model}), so the run stops: "
                f"{describe(failures[0])}: {failures[0]}. {every_call_failed_advice(failures[0])} "
                "No outputs were written."
            ) from failures[0]
        if failures:
            print(
                f"{len(failures)} of {made} SIE calls to {model} failed; their fields are "
                f"flagged. First: {short(failures[0])}"
            )


def ask_codes(
    client: SIEClient, items: list[tuple[str, str]], rules: dict[str, Any], calls: Calls
) -> dict[str, dict[str, dict[str, Any]]]:
    """Picklist name -> window id -> GLiNER2.5-Decide result (or error)."""
    model, size = rules["models"]["codes"], rules["thresholds"]["batch_size"]
    # Ask SIE to fail instead of truncating. The windows are sized to fit, so an overflow is a
    # per-batch failure we want to see, not a silently shortened note.
    options = {"overflow_policy": "error"}
    answers: dict[str, dict[str, dict[str, Any]]] = {}
    for name, picklist in rules["picklists"].items():
        schema = code_schema(picklist)
        answers[name] = {}
        for start in range(0, len(items), size):
            batch = items[start : start + size]

            def work(batch: list[tuple[str, str]] = batch, schema: dict[str, Any] = schema) -> Any:
                return client.extract(
                    model,
                    [Item(id=item_id, text=text) for item_id, text in batch],
                    output_schema=schema,
                    options=options,
                    provision_timeout_s=WAIT_S,
                )

            ids = [item_id for item_id, _ in batch]
            answers[name] |= call_sie(calls, model, work, ids, has_probabilities)
    return answers


def ask_fields(
    client: SIEClient, items: list[tuple[str, str]], rules: dict[str, Any], calls: Calls
) -> dict[str, dict[str, Any]]:
    """Window id -> gliner2 result (or error). One window per call: an empty-result HTTP 500
    would otherwise fail every window in the batch."""
    model, schema = rules["models"]["fields"], field_schema(rules)
    results: dict[str, dict[str, Any]] = {}
    for item_id, text in items:

        def work(item_id: str = item_id, text: str = text) -> Any:
            try:
                return client.extract(
                    model,
                    Item(id=item_id, text=text),
                    output_schema=schema,
                    provision_timeout_s=WAIT_S,
                )
            except ServerError as error:
                if EMPTY_RESULT_ERROR not in str(error):
                    raise
                return {"data": {}, "empty_result": True}

        results |= call_sie(calls, model, work, [item_id], has_data)
    return results


def has_probabilities(result: dict[str, Any]) -> bool:
    """A Decide result carries data.answer.probabilities."""
    answer = (
        (result.get("data") or {}).get("answer") if isinstance(result.get("data"), dict) else None
    )
    return isinstance(answer, dict) and isinstance(answer.get("probabilities"), dict)


def has_data(result: dict[str, Any]) -> bool:
    """A gliner2 schema result carries a data object (it may be empty: nothing found)."""
    return isinstance(result.get("data"), dict)


def every_call_failed_advice(error: SIEError) -> str:
    """What to check when every SIE call failed, judged by the first failure.

    A lost connection points at SIE_BASE_URL and the network. Any other transient error (a
    timeout, a model still loading, a 5xx, a rate limit) more likely means the model is still
    starting or SIE is busy, so the advice is to wait and run again.
    """
    if isinstance(error, SIEConnectionError):
        return "Check SIE_BASE_URL and your network."
    if isinstance(error, BadReply):
        return "Check the model names in rules.yaml and that SIE_BASE_URL points at an SIE server."
    return "The model may still be starting or SIE is busy; run again in a few minutes."


class BadReply(SIEError):  # type: ignore[misc]
    """A reply SIE sent that doesn't have the shape the recipe reads: the wrong number of
    results, or a result without its answer key."""


class SIEStopped(RuntimeError):
    """SIE refused the call for a reason a retry won't fix, or no call succeeded; the run
    stops without outputs."""


def is_fatal(error: SIEError) -> bool:
    """A refusal that fails every call the same way: a 4xx (bad key 401, forbidden 403, model
    not found 404, no credits or spend limit 402, inactive account) or a model the server
    marked as permanently unable to load. A RequestError with no HTTP status or a status below
    400 is a reply the SDK couldn't parse, and is fatal too. A rate limit, an over-long input, a
    timeout, a lost connection, a model still loading and a 5xx are transient or per-batch:
    those fields are flagged."""
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
    """The error for a per-field reason or the summary line, its message cut short."""
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


# --- Read the replies -------------------------------------------------------------------------


def code_probabilities(result: dict[str, Any], picklist: dict[str, Any]) -> dict[str, float]:
    """The option probabilities in one Decide reply. Raises ValueError for a failed call, a
    reply without ``data.answer.probabilities``, or a label that isn't on the picklist: each of
    those is a failed reply, never a code."""
    if error := result.get("error"):
        raise ValueError(str(error))
    data = result.get("data")
    answer = data.get("answer") if isinstance(data, dict) else None
    probs = answer.get("probabilities") if isinstance(answer, dict) else None
    if not isinstance(probs, dict) or not probs:
        raise ValueError("the reply has no data.answer.probabilities")
    labels = {option["label"] for option in picklist["options"]}
    if unknown := sorted(str(label) for label in probs if label not in labels):
        raise ValueError(f"the reply has labels not on the picklist: {', '.join(unknown)}")
    return {str(label): float(value) for label, value in probs.items()}


def merge_probabilities(per_window: list[dict[str, float]]) -> dict[str, float]:
    """One distribution per order: each option's highest probability over windows,
    renormalized. Taking the peak keeps the answer of the one window that describes the fault;
    an average would let the routine narrative in the other windows wash it out."""
    if len(per_window) == 1:
        return dict(per_window[0])
    labels = {label for window in per_window for label in window}
    peak = {label: max(window.get(label, 0.0) for window in per_window) for label in labels}
    total = sum(peak.values()) or 1.0
    return {label: value / total for label, value in peak.items()}


def code_field(
    name: str, per_window: list[dict[str, Any]], picklist: dict[str, Any], rules: dict[str, Any]
) -> Field:
    """Fill one code from its picklist, or leave it blank or flagged with the reason."""
    probs: list[dict[str, float]] = []
    for number, result in enumerate(per_window, start=1):
        try:
            probs.append(code_probabilities(result, picklist))
        except ValueError as problem:
            return Field(status=FLAGGED, reason=f"window {number}: {problem}")
    ranked = sorted(merge_probabilities(probs).items(), key=lambda pair: pair[1], reverse=True)
    (top, score), (second, second_score) = ranked[0], ranked[1] if len(ranked) > 1 else ("", 0.0)
    code = {option["label"]: option["code"] for option in picklist["options"]}[top]
    evidence = f"{top} {score:.3f}, next {second} {second_score:.3f}"
    limits = rules["thresholds"]
    if score < limits["min_code_score"]:
        reason = f"top option {top} scored {score:.3f} < {limits['min_code_score']:.2f}"
        return Field(evidence=evidence, status=FLAGGED, reason=reason)
    if score - second_score < limits["min_code_margin"]:
        reason = (
            f"{top} {score:.3f} vs {second} {second_score:.3f}: margin "
            f"{score - second_score:.3f} < {limits['min_code_margin']:.2f}"
        )
        return Field(evidence=evidence, status=FLAGGED, reason=reason)
    if code is None:
        return Field(evidence=evidence, status=BLANK, reason=f"the notes don't say ({top})")
    return Field(value=str(code), evidence=evidence, status=FILLED)


def _squash(text: str) -> str:
    return " ".join(text.casefold().split())


def field_values(
    per_window: list[tuple[str, dict[str, Any]]], rules: dict[str, Any]
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Merge the gliner2 replies of one order's windows: field -> spans found in the notes.

    Returns the spans and the problems per field. A failed call or a reply without its
    ``data`` key is a problem for every field (key ``*``); a span that isn't in the window's
    text is a problem for its own field, and is dropped, never used. The same span twice in one
    window counts once.
    """
    found: dict[str, list[str]] = {name: [] for name in rules["fields"]}
    problems: dict[str, list[str]] = {}
    for number, (text, result) in enumerate(per_window, start=1):
        seen: dict[str, set[str]] = {name: set() for name in found}
        if error := result.get("error"):
            problems.setdefault("*", []).append(f"window {number}: {error}")
            continue
        data = result.get("data")
        if not isinstance(data, dict):
            problems.setdefault("*", []).append(f"window {number}: the reply has no data")
            continue
        for name, raw in data.items():
            if name not in found:
                continue
            values = raw if isinstance(raw, list) else [raw]
            for value in values:
                if not isinstance(value, str) or not value.strip():
                    continue
                value = value.strip()
                if _squash(value) not in _squash(text):
                    problems.setdefault(name, []).append(f"'{value}' is not in the notes; dropped")
                elif value.casefold() not in seen[name]:
                    # The same words twice in one window are one value; in two windows they
                    # may be two entries (two labor lines), so both are kept.
                    seen[name].add(value.casefold())
                    found[name].append(value)
    return found, problems


# --- Checks (plain Python) --------------------------------------------------------------------

PART_NO_RE = re.compile(r"\b[A-Z0-9]+(?:-[A-Z0-9]+)+\b")
QTY_RE = re.compile(r"(?:^|\s)(\d+)\s*(?:x|off)\s", re.IGNORECASE)
HOURS_RE = re.compile(
    r"(?:(\d+)\s*x\s*)?(\d+(?:\.\d+)?)\s*(h|hr|hrs|hours?|min|mins|minutes?)\b", re.IGNORECASE
)
CLOCK_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*(?:to|-|until)\s*(\d{1,2}):(\d{2})\b")


def parse_part(
    span: str, notes: str, catalog: dict[str, str], asset_tag: re.Pattern[str]
) -> tuple[list[tuple[str, int]], list[str]]:
    """The catalog parts with quantities in one parts-used span, and what's wrong with it.

    A part number must be in the catalog exactly: a neighbouring number is a different part.
    A span that names more than one part number is flagged too, since its quantities are easy
    to pair with the wrong part; the parts that do match are still kept. With no part number,
    exactly one catalog description must appear in the span ("drive belts" -> drive belt). A
    quantity comes from the words before its part number in the span ("2 x bearing 6205-2RS"),
    or for the first part from the words just before the span in the notes.
    """
    # An asset tag in the span ("P-104 casing gasket") is not a part number, unless the
    # catalog has a part by that name: some part numbers look like tags (FG-100).
    numbers = [n for n in PART_NO_RE.findall(span) if n in catalog or not asset_tag.fullmatch(n)]
    problems: list[str] = []
    if not numbers:
        matches = [p for p, desc in catalog.items() if desc.casefold() in span.casefold()]
        if len(matches) != 1:
            found = "no catalog description" if not matches else f"{len(matches)} catalog parts"
            return [], [f"'{span}' matches {found}"]
        numbers, segments = matches, [span]
    else:
        segments, start = [], 0
        for number in numbers:
            at = span.find(number, start)
            segments.append(span[start:at])
            start = at + len(number)
        if unknown := [n for n in numbers if n not in catalog]:
            problems.append(f"{', '.join(unknown)} is not in the parts catalog")
        if len(numbers) > 1:
            problems.append(f"'{span}' names {len(numbers)} part numbers; check each quantity")
    parts: list[tuple[str, int]] = []
    for index, (number, segment) in enumerate(zip(numbers, segments, strict=True)):
        if number not in catalog:
            continue
        qty = QTY_RE.search(f" {segment} ")
        if qty is None and index == 0:
            at = notes.casefold().find(span.casefold())
            qty = QTY_RE.search(f" {notes[max(0, at - 8) : at]}") if at > 0 else None
        if qty is None or int(qty.group(1)) < 1:
            problems.append(f"no quantity for {number}")
        else:
            parts.append((number, int(qty.group(1))))
    return parts, problems


def parse_hours(span: str) -> tuple[float | None, str]:
    """Hours in a labor or downtime span: '3.5 h', '45 min', '2 x 3.5 h' (two people) or a
    clock range '03:10 to 05:40'. None and why when the span holds none, or more than one."""
    clocks = CLOCK_RE.findall(span)
    durations = HOURS_RE.findall(span)
    if len(clocks) + len(durations) != 1:
        return None, f"can't read one duration from '{span}'"
    if clocks:
        h1, m1, h2, m2 = (int(part) for part in clocks[0])
        minutes = (h2 * 60 + m2 - h1 * 60 - m1) % (24 * 60)
        return round(minutes / 60, 2), ""
    people, amount, unit = durations[0]
    hours = float(amount) / (60 if unit.casefold().startswith("min") else 1)
    return round(hours * int(people or 1), 2), ""


def with_crew(span: str, notes: str) -> str:
    """The labor span with the crew size the model left out: notes "Labor 2 x 3.5 h" and
    span "3.5 h" give "2 x 3.5 h"."""
    at = notes.find(span)
    if at <= 0 or re.match(r"\s*\d+\s*x", span):
        return span
    crew = re.search(r"(\d+)\s*x\s*$", notes[max(0, at - 8) : at])
    return f"{crew.group(1)} x {span}" if crew else span


def hours_field(spans: list[str], limit: float, what: str) -> Field:
    """A labor or downtime field: one duration, at most ``limit`` hours."""
    if not spans:
        return Field()
    if len(spans) > 1:
        return Field(evidence="; ".join(spans), status=FLAGGED, reason=f"{len(spans)} {what} spans")
    hours, problem = parse_hours(spans[0])
    if hours is None:
        return Field(evidence=spans[0], status=FLAGGED, reason=problem)
    if hours > limit:
        reason = f"{hours:g} h is more than the {limit:g} h this order allows"
        return Field(evidence=spans[0], status=FLAGGED, reason=reason)
    return Field(value=f"{hours:g}", evidence=spans[0], status=FILLED)


def text_field(spans: list[str]) -> Field:
    if not spans:
        return Field()
    return Field(value="; ".join(spans), evidence="; ".join(spans), status=FILLED)


def parts_field(
    spans: list[str], notes: str, catalog: dict[str, str], tag: re.Pattern[str]
) -> Field:
    if not spans:
        return Field()
    booked, problems = [], []
    for span in spans:
        parts, found = parse_part(span, notes, catalog, tag)
        booked += [f"{part} x{qty}" for part, qty in parts]
        problems += found
    status = FLAGGED if problems else FILLED
    return Field("; ".join(booked), "; ".join(spans), status, "; ".join(problems))


def asset_field(order: WorkOrder, tag: re.Pattern[str], catalog: dict[str, str]) -> Field:
    """The booked asset comes from the CMMS. Another tag named in the notes flags it. A
    catalog part number shaped like a tag (FG-100) is a part, not an asset."""
    others = sorted(
        {t for t in tag.findall(order.notes) if t != order.asset_id and t not in catalog}
    )
    if others:
        reason = f"the notes name {', '.join(others)} but the order is booked to {order.asset_id}"
        return Field(order.asset_id, "; ".join(others), FLAGGED, reason)
    return Field(order.asset_id, "CMMS", FILLED)


def flag(target: Field, reason: str) -> None:
    target.status = FLAGGED
    target.reason = "; ".join(filter(None, [target.reason, reason]))


def cue_in(notes: str, cues: list[str]) -> str | None:
    """The first cue phrase in the notes, matched on whole words, any case."""
    for cue in cues:
        if re.search(rf"\b{re.escape(cue.casefold())}\b", notes.casefold()):
            return cue
    return None


def check_cues(order: WorkOrder, fields: dict[str, Field], rules: dict[str, Any]) -> None:
    """Plain-word rules from rules.yaml that the models can't be trusted with.

    - Notes that say the problem wasn't found ("no leak found") don't get a fault code: the
      problem becomes the no-fault code (or blank, if the picklist has none) and is flagged.
    - A remedy that means a part went in (replace) with no part booked flags parts used.
    - Notes that mention follow-up work with no follow-up extracted flag the follow-up.
    """
    cues = rules["cues"]
    problem, no_fault = fields["problem"], cues["no_fault_code"]
    if problem.value != no_fault and (cue := cue_in(order.notes, cues["no_fault"])):
        was = problem.value or "no code"
        problem.value = no_fault or ""
        flag(problem, f"the notes say '{cue}'; set to {no_fault or 'blank'} instead of {was}")
    parts = fields["parts_used"]
    if fields["remedy"].value in cues["remedy_needs_part"] and not parts.value:
        flag(parts, f"remedy {fields['remedy'].value} says a part went in, but no part is booked")
    follow_up = fields["follow_up"]
    if not follow_up.value and (cue := cue_in(order.notes, cues["follow_up"])):
        flag(follow_up, f"the notes say '{cue}' but no follow-up work was found")


def draft_form(
    order: WorkOrder,
    codes: dict[str, list[dict[str, Any]]],
    replies: list[tuple[str, dict[str, Any]]],
    catalog: dict[str, str],
    rules: dict[str, Any],
) -> Form:
    """Build one draft completion form from the model replies and the checks."""
    tag = re.compile(rules["asset_tag_pattern"])
    fields = {"asset_id": asset_field(order, tag, catalog)}
    for name in CODE_FIELDS:
        fields[name] = code_field(name, codes[name], rules["picklists"][name], rules)
    spans, problems = field_values(replies, rules)
    limits = rules["thresholds"]
    fields["component"] = text_field(spans["component"][:1])
    if len(spans["component"]) > 1:
        fields["component"].evidence = "; ".join(spans["component"])
        fields["component"].status = FLAGGED
        fields["component"].reason = f"{len(spans['component'])} component spans; pick one"
    fields["actions"] = text_field(spans["actions"])
    fields["parts_used"] = parts_field(spans["parts_used"], order.notes, catalog, tag)
    fields["labor_h"] = hours_field(
        [with_crew(span, order.notes) for span in spans["labor"]],
        order.hours_open * limits["max_crew"],
        "labor",
    )
    fields["downtime_h"] = hours_field(spans["downtime"], order.hours_open, "downtime")
    fields["follow_up"] = text_field(spans["follow_up"])
    # A failed call or a bad reply flags every field the reply should have filled, since a
    # blank there is not "the notes don't say". A dropped span flags its own field.
    for name, form_name in SPAN_FIELDS.items():
        if found := problems.get("*", []) + problems.get(name, []):
            target = fields[form_name]
            target.status = FLAGGED
            target.reason = "; ".join(filter(None, [target.reason, *found]))
    check_cues(order, fields, rules)
    for name in rules["required"]:
        if fields[name].status == BLANK:
            fields[name].status = FLAGGED
            fields[name].reason = (fields[name].reason + "; " if fields[name].reason else "") + (
                "required on every closed order"
            )
    status = REVIEW if any(f.status == FLAGGED for f in fields.values()) else CHECKS_PASSED
    return Form(order.wo_id, order.asset_id, order.work_type, status, fields)


# --- Run --------------------------------------------------------------------------------------


def draft_forms(
    client: SIEClient, orders: list[WorkOrder], catalog: dict[str, str], rules: dict[str, Any]
) -> list[Form]:
    items = window_items(orders, rules["thresholds"]["max_words_per_window"])
    calls = Calls()
    codes = ask_codes(client, items, rules, calls)
    replies = ask_fields(client, items, rules, calls)
    check_calls(calls)
    forms = []
    for order in orders:
        ids = [(item_id, text) for item_id, text in items if item_id.split("#")[0] == order.wo_id]
        per_code = {name: [codes[name][item_id] for item_id, _ in ids] for name in CODE_FIELDS}
        per_window = [(text, replies[item_id]) for item_id, text in ids]
        forms.append(draft_form(order, per_code, per_window, catalog, rules))
    return forms


def csv_safe(value: Any) -> str:
    """A leading ' on a cell a spreadsheet would run as a formula; anything else as written."""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def form_row(form: Form) -> dict[str, str]:
    flagged = [name for name, f in form.fields.items() if f.status == FLAGGED]
    row = {"wo_id": form.wo_id, "asset_id": form.asset_id, "work_type": form.work_type}
    row |= {name: form.fields[name].value for name in FORM_FIELDS if name in form.fields}
    row |= {
        "status": form.status,
        "flagged_fields": "; ".join(flagged),
        "reasons": "; ".join(f"{name}: {form.fields[name].reason}" for name in flagged),
    }
    return row


def write_outputs(output_dir: Path, forms: list[Form]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "completion_forms.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FORM_FIELDS)
        writer.writeheader()
        for form in forms:
            writer.writerow({key: csv_safe(value) for key, value in form_row(form).items()})
    (output_dir / "drafts.json").write_text(
        json.dumps([asdict(form) for form in forms], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rules, orders, catalog = load_rules(), load_orders(), load_catalog()
    with make_client() as client:
        forms = stop_on_refusal(lambda: draft_forms(client, orders, catalog, rules))
    write_outputs(output, forms)
    for form in forms:
        row = form_row(form)
        print(
            f"{form.wo_id} {form.asset_id:<7} {form.status:<13} {row['problem'] or '-':<5}"
            f"{row['cause'] or '-':<6}{row['remedy'] or '-':<7}parts: {row['parts_used'] or '-'}"
        )
        for name, f in form.fields.items():
            if f.status == FLAGGED:
                print(f"        flagged {name}: {f.reason}")
    review = sum(form.status == REVIEW for form in forms)
    print(f"\n{len(forms)} work orders: {len(forms) - review} checks_passed, {review} to review")
    print("A person confirms every draft before the CMMS record is saved.")
    print(f"outputs: {output / 'completion_forms.csv'}, {output / 'drafts.json'}")


if __name__ == "__main__":
    main()
