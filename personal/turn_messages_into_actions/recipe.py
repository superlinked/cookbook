"""Turn a week of messages into calendar holds, changes, payments, replies and to-dos.

Use case:  A household's message sweep: what does each email, text, voice memo and paper bill
           ask for, and is it already on the calendar or in the payee list?
Who:       One person doing their own household admin (or a personal assistant doing it for
           them), every few days, on a phone or a laptop.
Problem:   The messages say what to do in free text, relative dates and paper. The calendar,
           contacts and payee list already know a lot of it. Typing each item into the right
           app is slow, and an assistant that books or pays on its own picks the wrong Tuesday
           or pays the late fee. This one only proposes, with the words each item came from.
Input:     data/inbox.jsonl (the messages as the apps export them: sender, sent time with its
           offset, subject, body, and a WAV or photo with a transcript where the phone made
           one), the calendar (calendar_app.py over data/calendar.csv), the address book
           (contacts.py over data/contacts.csv) and the payee list (payees.py over
           data/payees.csv). Models, action types, fields and thresholds are in rules.yaml.
SIE pipeline (four models):
           extract  nvidia/parakeet-tdt-0.6b-v3  once per voice memo with no transcript
           extract  lightonai/LightOnOCR-3-4B    once per photo (a bill, a flyer)
           extract  fastino/GLiNER2.5-Decide     once per message: schedule, change, pay,
                                                 reply, todo or fyi, a probability for each
           extract  fastino/gliner2-large-v1     once per window of about 512 tokens of each
                                                 message that needs an action: what, who,
                                                 when, where, amount and due date
           (Python) split off quoted history, resolve dates against the sent time, check the
                    bill's lines add up, then reconcile in sent order with the calendar,
                    contacts and payee list
Output:    proposals.json, todo.md, events.ics and messages.json in runs/<timestamp>/. Each
           action has a status naming what the checks found (checks_passed, question,
           matches_existing, no_action) and the messages it came from. Nothing is sent, booked
           or paid: the person decides each item and does it in the calendar, to-do or banking
           app.
Why SIE:   Four small specialist models, each called only where it's needed; no generative
           model makes the decision. Dates, amounts, duplicates and the calendar and payee
           checks are plain Python you can read below.
Runs on:   SIE Serverless.

    uv run recipe.py
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import html
import io
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import yaml
from calendar_app import Calendar, Event
from contacts import Contacts
from dotenv import load_dotenv
from payees import Payees
from PIL import Image, ImageOps
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
INBOX_PATH = DATA_DIR / "inbox.jsonl"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
# How much of an SIE error message goes into a question or the summary line.
ERROR_CHARS = 200

QUESTION_ID = "action_type"
TRANSCRIBE_OPTIONS = {"timestamp_granularities": ["segment"]}
MAX_IMAGE_EDGE_PX = 2048  # LightOnOCR's recommended longest edge
# When GLiNER2 finds nothing, SIE Serverless answers the output_schema call with HTTP 500 and this
# message. We record it as "no fields found"; a missing field can only make a question, and a
# message with no fields at all is a question too.
EMPTY_RESULT_ERROR = "Inference completed without a valid conserved billing settlement"

# What the checks found, never a decision: the person approves or does every item themselves.
CHECKS_PASSED = "checks_passed"  # every field read, dates resolved, nothing clashes
QUESTION = "question"  # something missing, vague, unsure or contradictory: the question says what
MATCHES_EXISTING = "matches_existing"  # the calendar or payee list already has it
NO_ACTION = "no_action"  # a newsletter, a delivery notice, a receipt
STATUSES = (CHECKS_PASSED, QUESTION, MATCHES_EXISTING, NO_ACTION)


# --- Messages ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    id: str
    channel: str  # email | sms | voice_memo | photo
    sender: str
    subject: str
    sent: datetime
    body: str
    attachment: Path | None = None
    transcript: str = ""  # a memo's transcript, when the phone already made one

    def source(self, text: str) -> dict[str, Any]:
        """Where an item came from: the message and the sentence of it that says so."""
        return {
            "message": self.id,
            "channel": self.channel,
            "from": self.sender,
            "sent": self.sent.isoformat(),
            "text": text,
        }


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def load_inbox(path: Path = INBOX_PATH) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def parse_messages(
    records: list[dict[str, Any]], rules: dict[str, Any], attachments_dir: Path = DATA_DIR
) -> list[Message]:
    """Messages from the export's records, in sent-time order (a later message can only change
    an earlier one). A repeated id is an error in the export."""
    zone = ZoneInfo(rules["person"]["time_zone"])
    messages: list[Message] = []
    for number, record in enumerate(records, start=1):
        sent = datetime.fromisoformat(str(record["sent"]))
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=zone)
        attachment = record.get("attachment")
        path = None
        if attachment:
            path = Path(attachment)
            path = path if path.is_absolute() else attachments_dir / path
        messages.append(
            Message(
                id=str(record.get("id") or f"msg-{number:02d}"),
                channel=str(record.get("channel") or "email"),
                sender=str(record.get("from") or ""),
                subject=str(record.get("subject") or ""),
                sent=sent,
                body=str(record.get("body") or ""),
                attachment=path,
                transcript=str(record.get("transcript") or ""),
            )
        )
    ids = [message.id for message in messages]
    if repeated := sorted({i for i in ids if ids.count(i) > 1}):
        raise ValueError(f"message ids repeat in the inbox: {', '.join(repeated)}")
    return sorted(messages, key=lambda m: (m.sent.astimezone(UTC), m.id))


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
    """One SIE call failed for a reason that may pass; its message becomes a question."""


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
    ) -> Any:
        """Run one SIE call and return its reply, which must carry `key` (a list reply must
        carry it in every item).

        A refusal (see `is_fatal`) stops the run with SIEStopped. Any other SIE error, and a
        reply without `key`, is recorded and raised as CallFailed, so the caller turns its
        message into a question and the run goes on. With `empty_result_ok` (the GLiNER2
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
                return {"data": {}, "empty_result_error": str(error)}
            if is_fatal(error):
                raise SIEStopped(stop_message(error, model)) from error
            raise self.reject(model, error) from error
        replies = reply if isinstance(reply, list) else [reply]
        if not replies or any(not isinstance(r, dict) or key not in r for r in replies):
            raise self.reject(model, SIEError(f"{model} replied without '{key}'"))
        return reply

    def reject(self, model: str, error: SIEError) -> CallFailed:
        """Record a failed call (an SIE error, or a reply we can't use) as transient."""
        self.failures.append((model, error))
        return CallFailed(f"the SIE call failed: {short(error)}")

    def check_all_failed(self) -> None:
        """Stop the run when every call failed; print a line when only some did."""
        if not self.failures:
            return
        total, (_, first) = self.total, self.failures[0]
        if len(self.failures) >= total:
            models = ", ".join(sorted({name for name, _ in self.failures}))
            raise SIEStopped(
                f"Every SIE call failed ({total} of {total} to {models}), so the run stops: "
                f"{describe(first)}: {first}. {every_call_failed_advice(first)} "
                "No outputs were written."
            ) from first
        n = len(self.failures)
        print(
            f"{n} of {total} SIE calls failed; their messages became questions. "
            f"First: {short(first)}"
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
    those messages become questions."""
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
    """The error for a question or the summary line, its message cut short."""
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


# --- Read voice memos and photos ----------------------------------------------------------------


def transcribe(client: SIEClient, calls: Calls, message: Message, rules: dict[str, Any]) -> str:
    assert message.attachment is not None
    model = rules["models"]["transcribe"]
    path = message.attachment
    audio = {"data": path.read_bytes(), "format": path.suffix.lstrip(".").lower()}
    reply = calls.call(
        model,
        lambda: client.extract(
            model,
            Item(id=f"transcribe/{message.id}", audio=audio),
            options=TRANSCRIBE_OPTIONS,
            provision_timeout_s=WAIT_S,
        ),
        "data",
    )
    data = reply["data"] if isinstance(reply["data"], dict) else {}
    segments = data.get("segments")
    if isinstance(segments, list):
        parts = [str(s.get("text", "")).strip() for s in segments if isinstance(s, dict)]
        return " ".join(part for part in parts if part)
    return str(data.get("text") or "").strip()


def image_png(path: Path) -> bytes:
    """The photo upright (a phone's EXIF rotation applied), RGB, at most 2048 px on a side."""
    image = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    image.thumbnail((MAX_IMAGE_EDGE_PX, MAX_IMAGE_EDGE_PX))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def plain_text(markdown: str) -> str:
    """OCR Markdown as plain lines: HTML table cells become text on one line per row, entities
    are decoded ('&amp;' is '&') and bold marks are dropped."""
    text = re.sub(r"</t[dh]>", " ", markdown, flags=re.I)
    text = re.sub(r"</tr>|<br\s*/?>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).replace("**", "")
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def read_photo(client: SIEClient, calls: Calls, message: Message, rules: dict[str, Any]) -> str:
    assert message.attachment is not None
    model, png = rules["models"]["ocr"], image_png(message.attachment)
    reply = calls.call(
        model,
        lambda: client.extract(
            model, Item(id=f"ocr/{message.id}", images=[png]), provision_timeout_s=WAIT_S
        ),
        "entities",
    )
    for entity in reply["entities"] or []:
        if isinstance(entity, dict) and entity.get("label") == "markdown":
            return plain_text(str(entity.get("text", "")))
    return ""


@dataclass
class Read:
    """A message as the models see it: its new text, the quoted history kept apart, and why
    an attachment could not be read."""

    text: str  # From/Subject header and the new text
    quoted: str = ""
    problem: str | None = None


QUOTE_START = re.compile(
    r"^\s*(?:>|On .{3,200} wrote:\s*$|-{2,}\s*(?:Original Message|Forwarded message)\b)", re.I
)


def split_quoted(body: str) -> tuple[str, str]:
    """(new text, quoted history). The history starts at the first '>' line, an 'On ...
    wrote:' line or an 'Original Message' / 'Forwarded message' rule."""
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if QUOTE_START.match(line):
            return "\n".join(lines[:index]).strip(), "\n".join(lines[index:]).strip()
    return body.strip(), ""


def read_message(client: SIEClient, calls: Calls, message: Message, rules: dict[str, Any]) -> Read:
    """The message's text: the body, the phone's transcript, a new transcript or the OCR."""
    body, problem = message.body, None
    if message.attachment is not None:
        try:
            if message.transcript:
                body = message.transcript
            elif message.channel == "voice_memo":
                body = transcribe(client, calls, message, rules)
            else:
                body = read_photo(client, calls, message, rules)
        except CallFailed as failed:
            body, problem = "", f"I could not read {message.attachment.name} ({failed})."
        except OSError as error:  # missing, unreadable or not an image or WAV
            body, problem = "", f"I could not open {message.attachment.name} ({error})."
        if problem is None and len(body.strip()) < rules["thresholds"]["min_text_chars"]:
            problem = f"I could not read {message.attachment.name}. What is it?"
    # Only a typed message carries quoted history; a transcript or a bill is read whole.
    new, quoted = split_quoted(body) if message.attachment is None else (body.strip(), "")
    head = [f"From: {message.sender}"] + (
        [f"Subject: {message.subject}"] if message.subject else []
    )
    return Read("\n".join([*head, "", new]), quoted, problem)


def body_of(text: str) -> str:
    """The text after the From/Subject header."""
    return text.split("\n\n", 1)[1] if "\n\n" in text else text


# --- Action type: GLiNER2.5-Decide --------------------------------------------------------------


def word_windows(text: str, size: int, overlap: int = 0) -> list[str]:
    """Windows of at most `size` words, each repeating the last `overlap` words of the one before.
    Line breaks are kept."""
    tokens = re.findall(r"\S+\s*", text)
    if not tokens:
        return [""]
    step = max(1, size - overlap)
    parts: list[str] = []
    for start in range(0, len(tokens), step):
        parts.append("".join(tokens[start : start + size]).strip())
        if start + size >= len(tokens):
            break
    return parts


def type_schema(rules: dict[str, Any]) -> dict[str, Any]:
    return {
        QUESTION_ID: {
            "type": "choice",
            "instructions": rules["question"],
            "criteria": dict(rules["action_types"]),
        }
    }


@dataclass
class TypeReading:
    label: str = ""
    probabilities: dict[str, float] = field(default_factory=dict)
    error: str | None = None

    def ranked(self) -> list[tuple[str, float]]:
        return sorted(self.probabilities.items(), key=lambda kv: -kv[1])


def merge_probabilities(per_window: list[dict[str, float]]) -> dict[str, float]:
    """One distribution per message: each type's highest probability over the windows,
    renormalized. Averaging would let the rest of a long message drown out the one window that
    asks for something."""
    if not per_window:
        return {}
    peak = {label: max(window.get(label, 0.0) for window in per_window) for label in per_window[0]}
    total = sum(peak.values()) or 1.0
    return {label: value / total for label, value in peak.items()}


def read_type_probabilities(reply: Any, labels: list[str]) -> dict[str, float]:
    """The probability per action type in one window's reply, or an SIEError saying what was
    wrong with it: no probabilities, a type missing or extra, or a value outside 0 to 1."""
    answer = reply.get("data", {}).get(QUESTION_ID) if isinstance(reply, dict) else None
    probabilities = answer.get("probabilities") if isinstance(answer, dict) else None
    if not isinstance(probabilities, dict):
        raise SIEError(f"the reply has no `data.{QUESTION_ID}.probabilities`")
    if set(probabilities) != set(labels):
        raise SIEError(f"the reply's types {sorted(probabilities)} are not {sorted(labels)}")
    values: dict[str, float] = {}
    for label, value in probabilities.items():
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        if not ok or not 0.0 <= value <= 1.0:
            raise SIEError(f"the reply's probability for {label} is not from 0 to 1: {value!r}")
        values[str(label)] = float(value)
    return values


def classify(
    client: SIEClient, calls: Calls, message_id: str, text: str, rules: dict[str, Any]
) -> TypeReading:
    """One Decide call per message (its windows as the items): a probability per type."""
    model, labels = rules["models"]["action_type"], list(rules["action_types"])
    windows = word_windows(text, rules["thresholds"]["type_window_words"])
    schema = type_schema(rules)
    try:
        replies = calls.call(
            model,
            lambda: client.extract(
                model,
                [Item(id=f"{message_id}#{n}", text=w) for n, w in enumerate(windows, start=1)],
                output_schema=schema,
                options={"overflow_policy": "error"},  # windows are sized to fit
                provision_timeout_s=WAIT_S,
            ),
            "data",
        )
        if not isinstance(replies, list) or len(replies) != len(windows):
            raise calls.reject(model, SIEError(f"{model} did not answer every window"))
        try:
            per_window = [read_type_probabilities(reply, labels) for reply in replies]
        except SIEError as error:
            raise calls.reject(model, error) from error
    except CallFailed as failed:
        return TypeReading(error=str(failed))
    reading = TypeReading(probabilities=merge_probabilities(per_window))
    reading.label = reading.ranked()[0][0]
    return reading


def type_question(reading: TypeReading, rules: dict[str, Any]) -> str | None:
    """Why the action type cannot be trusted, or None."""
    if reading.error:
        return f"I could not tell what this message needs ({reading.error})."
    ranked = reading.ranked()
    top, score = ranked[0]
    second, runner = ranked[1] if len(ranked) > 1 else ("", 0.0)
    limits = rules["thresholds"]
    if score < limits["min_type_score"] or score - runner < limits["min_type_margin"]:
        return (
            f"I am not sure what this needs: {top} ({score:.2f}) or {second} ({runner:.2f})."
            " What should I do with it?"
        )
    return None


# --- Fields: gliner2-large-v1 ---------------------------------------------------------------------


def field_schema(rules: dict[str, Any]) -> dict[str, Any]:
    properties = {
        name: {"type": "string", "description": description}
        for name, description in rules["fields"].items()
    }
    return {"type": "object", "properties": properties}


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def in_text(value: str, text: str) -> bool:
    return bool(re.search(r"[^\W_]", value)) and _squash(value) in _squash(text)


@dataclass
class Fields:
    values: dict[str, str] = field(default_factory=dict)
    questions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def extract_fields(
    client: SIEClient, calls: Calls, message_id: str, text: str, rules: dict[str, Any]
) -> Fields:
    """The fields over windows of the text, each value checked to be in its window.

    A value the model returns that isn't in the window (ignoring case and spacing) is dropped
    with a note. Two different values of a `single_value_fields` field in two windows are a
    question, not "the first one wins". A failed call is a question.
    """
    model, limits = rules["models"]["fields"], rules["thresholds"]
    schema, found = field_schema(rules), Fields()
    seen: dict[str, list[str]] = {}
    parts = word_windows(text, limits["field_window_words"], limits["field_window_overlap_words"])
    for index, window in enumerate(parts, start=1):
        key = f"fields/{message_id}/w{index}"
        try:
            reply = calls.call(
                model,
                functools.partial(
                    client.extract,
                    model,
                    Item(id=key, text=window),
                    output_schema=schema,
                    options={"overflow_policy": "error"},  # windows are sized to fit
                    provision_timeout_s=WAIT_S,
                ),
                "data",
                empty_result_ok=True,
            )
        except CallFailed as failed:
            found.questions.append(f"I could not read the details of this message ({failed}).")
            continue
        if reply.get("empty_result_error"):
            found.notes.append(
                f"Window {index}: no fields found (SIE Serverless answered HTTP 500)"
            )
        data = reply["data"] if isinstance(reply["data"], dict) else {}
        for name, value in data.items():
            if name not in rules["fields"] or not isinstance(value, str):
                continue
            value = value.strip().strip(",.;")
            if not value:
                continue
            if not in_text(value, window):
                found.notes.append(f"Dropped {name} '{value}': not in the message")
                continue
            seen.setdefault(name, [])
            if all(_squash(value) != _squash(other) for other in seen[name]):
                seen[name].append(value)
    for name, values in seen.items():
        found.values[name] = values[0]
        if len(values) > 1 and name in rules["single_value_fields"]:
            listed = " or ".join(f"'{v}'" for v in values)
            found.questions.append(f"The message gives two {name.replace('_', ' ')}s: {listed}.")
    return found


# --- Dates: a small deterministic resolver ----------------------------------------------------

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}
_UNITS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth"]
ORDINAL_WORDS = {word: n for n, word in enumerate(_UNITS, start=1)} | {
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
    "thirteenth": 13,
    "fourteenth": 14,
    "fifteenth": 15,
    "sixteenth": 16,
    "seventeenth": 17,
    "eighteenth": 18,
    "nineteenth": 19,
    "twentieth": 20,
    "thirtieth": 30,
}
ORDINAL_WORDS |= {f"twenty-{word}": 20 + n for n, word in enumerate(_UNITS, start=1)}
ORDINAL_WORDS |= {f"twenty {word}": 20 + n for n, word in enumerate(_UNITS, start=1)}
ORDINAL_WORDS |= {"thirty-first": 31, "thirty first": 31}

_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))
_DAY_RE = r"\d{1,2}|" + "|".join(sorted(ORDINAL_WORDS, key=len, reverse=True))
_MONTH_DATE = re.compile(
    rf"\b({_MONTH_RE})\.?\s+({_DAY_RE})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", re.I
)
_NUMERIC_DATE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?(?![\d/])")
_AMPM_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s?m\b\.?", re.I)
# "2-4 PM", "from 1 to 3 PM": the am or pm after the end time belongs to the start too.
_MOVE_VERB = re.compile(
    r"\b(?:mov(?:e|es|ed|ing)|chang(?:es|ed|ing)|push(?:es|ed|ing)?|shift(?:s|ed|ing)? to"
    r"|resched\w*)\b",
    re.I,
)
_TIME_RANGE = re.compile(
    r"(?<![\d/:])(\d{1,2})(?::(\d{2}))?\s*(?:-|to|until)\s*(\d{1,2})(?::\d{2})?\s*([ap])\.?\s?m\b",
    re.I,
)
_BARE_TIME = re.compile(
    r"\bat\s+(\d{1,2})(?::(\d{2}))?\b(?!\s*(?:[ap]\.?\s?m\b|/|st|nd|rd|th))", re.I
)


@dataclass(frozen=True)
class When:
    """A resolved date and time, or why the text cannot be resolved."""

    day: date | None = None
    at: time | None = None
    problem: str | None = None


def _has_term(text: str, terms: list[str]) -> bool:
    return any(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.I) for term in terms)


def has_cancel(text: str, rules: dict[str, Any]) -> bool:
    """Whether the text says something is cancelled, not negated: "is NOT cancelled" or "is not
    going to be called off" doesn't count. A negation counts anywhere earlier in the same clause
    (back to the last comma, full stop or "but")."""
    low = re.sub(r"\s+", " ", text.replace("\u2019", "'")).casefold()
    for term in rules["cancel_terms"]:
        for match in re.finditer(rf"(?<!\w){re.escape(term)}(?!\w)", low):
            before = re.split(r"[,;.!?]|\bbut\b", low[: match.start()])[-1]
            if not any(
                re.search(rf"(?<![\w']){re.escape(n)}(?![\w'])", before)
                for n in rules["cancel_negations"]
            ):
                return True
    return False


def other_zone(text: str, rules: dict[str, Any]) -> str | None:
    """A time zone named in the text that isn't the person's own ('10 AM ET'), or None.
    Abbreviations match in capitals only, so 'et' or 'ct' in a word never counts."""
    for word in rules["other_zone_words"]:
        flags = 0 if word.isupper() else re.I
        if re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", text, flags):
            return str(word)
    return None


def _absolute_day(text: str, sent: date, past_days: int) -> tuple[date | None, str | None]:
    found = [
        (MONTHS[m.group(1).casefold()], m.group(2).casefold(), m.group(3))
        for m in _MONTH_DATE.finditer(text)
    ]
    if len({(month, token) for month, token, _ in found}) > 1:
        return None, f"'{text}' names more than one date"
    numeric = _NUMERIC_DATE.search(text)
    if found:
        month, token, raw = found[0]
        day_number = int(token) if token.isdigit() else ORDINAL_WORDS[token]
        year = int(raw) if raw else None
    elif numeric:
        month, day_number = int(numeric.group(1)), int(numeric.group(2))
        raw_year = numeric.group(3)
        year = (2000 + int(raw_year) if len(raw_year) == 2 else int(raw_year)) if raw_year else None
    else:
        return None, None
    try:
        day = date(year or sent.year, month, day_number)
        if year is None and sent - timedelta(days=past_days) <= day < sent:
            return None, f"'{text}' is before the message was sent: this year or next?"
        if year is None and day < sent:
            day = date(sent.year + 1, month, day_number)
    except ValueError:
        return None, f"'{text}' is not a valid date"
    for index, name in enumerate(WEEKDAYS):
        if re.search(rf"\b{name}\b", text, re.I) and day.weekday() != index:
            return None, (f"'{text}' says {name.title()}, but {day:%B} {day.day} is a {day:%A}")
    return day, None


def _relative_day(text: str, sent: date) -> tuple[date | None, str | None]:
    low = text.casefold()
    if re.search(r"\bday after tomorrow\b", low):
        return sent + timedelta(days=2), None
    if re.search(r"\btomorrow\b", low):
        return sent + timedelta(days=1), None
    if re.search(r"\b(today|tonight|this (morning|afternoon|evening))\b", low):
        return sent, None
    named = [name for name in WEEKDAYS if re.search(rf"\b{name}\b", low)]
    if len(named) > 1:
        return None, f"'{text}' names more than one day ({', '.join(n.title() for n in named)})"
    for index, name in enumerate(WEEKDAYS):
        nearest_delta = (index - sent.weekday()) % 7 or 7
        nearest = sent + timedelta(days=nearest_delta)
        if re.search(rf"\bnext\s+{name}\b", low):
            following_week = sent - timedelta(days=sent.weekday()) + timedelta(days=7 + index)
            if following_week != nearest:
                return None, (
                    f"'{text}' could mean {nearest:%a %b} {nearest.day} or "
                    f"{following_week:%a %b} {following_week.day}"
                )
            return nearest, None
        if re.search(rf"\b{name}\b", low):
            if sent.weekday() == index:
                return None, f"'{text}' was sent on a {name.title()}: today, or in a week?"
            return nearest, None
    return None, None


def _time_range(text: str) -> tuple[time | None, str | None]:
    """The start of a range such as "2-4 PM" (2 PM), or why it can't be read ("11-1 PM" could
    start at 11 AM or 11 PM). (None, None) when the text has no such range; "November 3 - 4 PM"
    is a day and a time, not a range, and "moves from 9 to 11 AM" is a question, as is "moves
    from 9 AM to 11 AM": two times next to a move could be the old one and the new one."""
    moved = _MOVE_VERB.search(text)
    times = {
        (m.group(1), m.group(2) or "00", m.group(3).casefold()) for m in _AMPM_TIME.finditer(text)
    }
    if moved and len(times) > 1:
        return None, f"'{text}' names two times next to a move; which one is the new time?"
    for match in _TIME_RANGE.finditer(text):
        if re.search(rf"\b(?:{_MONTH_RE})\.?\s*$", text[: match.start()], re.I):
            continue
        if re.search(r"\b(?:mov|chang|push|shift|resched)\w*", text[: match.start()], re.I):
            return None, f"'{match.group(0)}' could be a range or a move from one time to another"
        start, minute, end = int(match.group(1)), int(match.group(2) or 0), int(match.group(3))
        if not (1 <= start <= 12 and 1 <= end <= 12 and minute < 60):
            return None, f"'{match.group(0)}' is not a time"
        if start % 12 > end % 12:
            return None, f"'{match.group(0)}' could start in the morning or the evening"
        return time(start % 12 + (12 if match.group(4).casefold() == "p" else 0), minute), None
    return None, None


def _time_of_day(text: str, rules: dict[str, Any]) -> time | None:
    if match := _AMPM_TIME.search(text):
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        if not (1 <= hour <= 12 and minute < 60):
            return None
        hour = hour % 12 + (12 if match.group(3).casefold() == "p" else 0)
        return time(hour, minute)
    if re.search(r"\bnoon\b", text, re.I):
        return time(12, 0)
    if match := _BARE_TIME.search(text):
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        if not (1 <= hour <= 12 and minute < 60):
            return None
        if hour < rules["bare_hour_pm_below"]:
            hour += 12
        return time(hour % 24, minute)
    return None


def resolve(text: str, sent: datetime, rules: dict[str, Any]) -> When:
    """Resolve a date/time phrase against the local sent time of its message."""
    if zone := other_zone(text, rules):
        return When(problem=f"'{text}' is in another time zone ({zone})")
    day, problem = _absolute_day(text, sent.date(), rules["thresholds"]["yearless_past_days"])
    if problem:
        return When(problem=problem)
    if day is None and _has_term(text, rules["vague_terms"]):
        return When(problem=f"'{text}' is not a date")
    if day is None:
        day, problem = _relative_day(text, sent.date())
        if problem:
            return When(problem=problem)
    start, problem = _time_range(text)
    if problem:
        return When(problem=problem)
    return When(day=day, at=start or _time_of_day(text, rules))


ABBREVIATIONS = ("mr.", "ms.", "mrs.", "dr.", "st.", "jr.", "sr.", "no.", "a.m.", "p.m.")


def sentences(text: str) -> list[str]:
    """Sentences, line by line; "Ms. Abernathy" or "Dr. Okafor" does not end a sentence."""
    out: list[str] = []
    for line in text.splitlines():
        merged: list[str] = []
        for part in re.split(r"(?<=[.!?])\s+", line.strip()):
            if merged and merged[-1].casefold().endswith(ABBREVIATIONS):
                merged[-1] = f"{merged[-1]} {part}"
            elif part:
                merged.append(part)
        out += merged
    return out


def resolve_in_context(
    span: str, text: str, sent: datetime, rules: dict[str, Any], change: bool = False
) -> When:
    """Resolve the extracted span; a missing date or time is taken from the span's sentence
    ("practice this Saturday moves to 11 AM" when the model returned only "11 AM"). A time zone
    named anywhere in that sentence is a question, and so is a change whose sentence names two
    weekdays ("Monday's practice moves to Friday at 5 PM"), even when the span has both."""
    when = resolve(span, sent, rules)
    if when.problem:
        return when
    for sentence in sentences(body_of(text)):
        if _squash(span) not in _squash(sentence):
            continue
        if zone := other_zone(sentence, rules):
            return When(problem=f"'{sentence}' is in another time zone ({zone})")
        named = [name for name in WEEKDAYS if re.search(rf"\b{name}\b", sentence, re.I)]
        if change and len(named) > 1:
            days = ", ".join(name.title() for name in named)
            return When(problem=f"'{sentence}' names more than one day ({days})")
        if when.day and when.at:
            return when
        around = resolve(sentence, sent, rules)
        if around.problem and when.day is None:
            return around
        if not around.problem:
            return When(day=when.day or around.day, at=when.at or around.at)
    return when


def date_in_sentence(
    what: str, text: str, sent: datetime, rules: dict[str, Any]
) -> tuple[When, str] | None:
    """When the model returns no time, the date in the sentence that names `what`, if any."""
    for sentence in sentences(body_of(text)):
        if _squash(what) in _squash(sentence):
            when = resolve(sentence, sent, rules)
            if when.day is not None or when.problem:
                return when, sentence
    return None


# --- Amounts ----------------------------------------------------------------------------------

_AMOUNT = re.compile(r"(-)?\s?\$\s?(-)?(\d{1,3}(?:,\d{3})+|\d+)(\.\d{2})?")


def amounts(text: str) -> list[Decimal]:
    """Every dollar amount in the text, with its sign: '-$79.40' and '$-79.40' are negative."""
    found: list[Decimal] = []
    for match in _AMOUNT.finditer(text):
        try:
            value = Decimal(match.group(3).replace(",", "") + (match.group(4) or ""))
        except InvalidOperation:
            continue
        found.append(-value if match.group(1) or match.group(2) else value)
    return found


def parse_amount(text: str) -> Decimal | None:
    found = amounts(text)
    return found[0] if found else None


def bill_line(text: str, label: str) -> Decimal | None:
    """The first dollar amount on the line that starts with or holds the label, or within the
    next 120 characters (OCR may put a table's label and amount on different lines)."""
    match = re.search(re.escape(label), text, re.I)
    if not match:
        return None
    return parse_amount(text[match.end() : match.end() + 120])


def line_with(text: str, label: str) -> str:
    """The first line of the text that holds the label, with the next line when the amount is
    there instead (OCR often puts a table's label and amount on separate lines); or ''."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if label.casefold() in line.casefold():
            if parse_amount(line) is None and index + 1 < len(lines):
                return f"{line} {lines[index + 1]}"
            return line
    return ""


def check_bill(amount: Decimal, text: str, rules: dict[str, Any]) -> tuple[str | None, str]:
    """(question or None, note). The amount must be the one on the bill's "amount due" line,
    and the summary must add up when it is printed."""
    labels = rules["bill_lines"]
    due = bill_line(text, labels["amount_due"])
    if due is None:
        return f"I read ${amount}, but found no amount due line on the bill. Check the bill.", ""
    if due != amount:
        return f"The bill's amount due is ${due}, but I read ${amount}. Which is right?", ""
    parts = {name: bill_line(text, labels[name]) for name in labels if name != "amount_due"}
    if all(value is None for value in parts.values()):
        return None, f"Amount due line: ${due}. The bill prints no summary to add up."
    previous, paid, new = parts["previous_balance"], parts["payments"], parts["new_charges"]
    if previous is None or paid is None or new is None:
        missing = ", ".join(labels[name] for name, value in parts.items() if value is None)
        return f"The bill's summary is missing {missing}, so it can't be added up. Check it.", ""
    total = previous - abs(paid) + new
    if total != amount:
        return (
            f"The bill does not add up: ${previous} - ${abs(paid)} + ${new} = ${total}, "
            f"not ${amount}. Check the bill.",
            "",
        )
    return None, f"Checked: ${previous} - ${abs(paid)} + ${new} = ${amount}, the amount due."


# --- One message, one proposed action -------------------------------------------------------


@dataclass
class Proposal:
    type: str
    message: Message
    fields: dict[str, str]
    sources: list[dict[str, Any]]
    who: str  # the sender's contact id, or the sender's name when no contact matches exactly
    text: str = ""  # what the models read: header and new text
    type_score: float | None = None
    runner_up: str = ""
    start: datetime | None = None
    end: datetime | None = None
    due: date | None = None
    due_at: time | None = None
    amount: Decimal | None = None
    when: When | None = None  # for a change: the day (and time) it names
    cancel: bool = False  # for a change: it cancels rather than moves
    questions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    existing: dict[str, Any] | None = None  # the calendar event or payee it matches
    change: dict[str, Any] | None = None  # for a change: the event and its old and new time
    payee: str = ""  # for a bill: the payee it matched
    type_doubt: str | None = None  # the question asked when the action type is unsure
    person: str = ""  # whose inbox this is

    @property
    def status(self) -> str:
        if self.questions:
            return QUESTION
        return MATCHES_EXISTING if self.existing else CHECKS_PASSED

    @property
    def id(self) -> str:
        return f"act-{self.message.id}"

    @property
    def what(self) -> str:
        return self.fields.get("what") or self.message.subject or "message"

    @property
    def title(self) -> str:
        who = self.fields.get("who") or self.message.sender
        if self.type == "pay":
            amount = f"${self.amount}" if self.amount is not None else "the bill"
            # On a photographed bill "who" is often the person's own name (the customer).
            payee = self.payee or ("" if who in ("", "me", self.person) else who)
            return f"Pay {amount}" + (f" to {payee}" if payee else "")
        if self.type == "reply":
            return f"Reply to {self.message.sender} about {self.what}"
        if self.type == "schedule":
            place = f" at {self.fields['where']}" if self.fields.get("where") else ""
            return f"{self.what}{place}"
        if self.type == "change" and self.change:
            verb = "Cancel" if self.change["cancel"] else "Move"
            return f"{verb} '{self.change['title']}'"
        return self.what

    def as_json(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "status": self.status,
            "title": self.title,
            "fields": self.fields,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "due": _due_text(self.due, self.due_at),
            "amount": str(self.amount) if self.amount is not None else None,
            "questions": self.questions,
            "notes": self.notes,
            "type_score": round(self.type_score, 4) if self.type_score is not None else None,
            "sources": self.sources,
        }
        if self.existing:
            record["matches"] = self.existing
        if self.change:
            record["change"] = self.change
        return record


def _due_text(day: date | None, at: time | None) -> str | None:
    if day is None:
        return None
    return f"{day.isoformat()}T{at:%H:%M}" if at else day.isoformat()


def source_text(text: str, values: list[str]) -> str:
    """The sentence of the new text that holds the first of `values` found; else its first."""
    parts = sentences(body_of(text))
    for value in values:
        for sentence in parts:
            if value and _squash(value) in _squash(sentence):
                return sentence
    return parts[0] if parts else ""


def who_key(sender: str, contacts: Contacts) -> tuple[str, str]:
    """(who, note): the sender's contact id when the name matches a contact exactly, else the
    sender's name itself. A close name is not taken as the contact; it only adds a note."""
    match = contacts.find_contact(sender)
    if match.contact is not None:
        return match.contact.contact_id, ""
    note = ""
    if match.status in ("possible_match", "several"):
        names = " or ".join(contact.name for contact in match.contacts)
        note = f"'{sender}' is not in your contacts as written; maybe {names}."
    return f"sender:{_squash(sender)}", note


def _local(when: When, zone: ZoneInfo) -> datetime | None:
    return datetime.combine(when.day, when.at, tzinfo=zone) if when.day and when.at else None


def build_proposal(
    message: Message,
    read: Read,
    reading: TypeReading,
    found: Fields,
    contacts: Contacts,
    rules: dict[str, Any],
) -> Proposal:
    """One proposal from one message, checked on its own. Every rule it fails adds a question.
    The calendar and payee checks come later, in `reconcile`."""
    zone = ZoneInfo(rules["person"]["time_zone"])
    sent = message.sent.astimezone(zone)
    text, values = read.text, found.values
    kind = reading.label or "unknown"
    who, who_note = who_key(message.sender, contacts)
    ranked = reading.ranked()
    proposal = Proposal(
        type=kind,
        message=message,
        fields=values,
        sources=[
            message.source(
                source_text(
                    text,
                    [values.get(n, "") for n in SOURCE_FIELDS.get(kind, DEFAULT_SOURCE_FIELDS)],
                )
            )
        ],
        who=who,
        text=text,
        type_score=reading.probabilities.get(kind),
        runner_up=ranked[1][0] if len(ranked) > 1 else "",
        notes=[*found.notes, *([who_note] if who_note else [])],
        person=rules["person"]["name"],
    )
    if read.problem:
        proposal.questions.append(read.problem)
    if (question := type_question(reading, rules)) is not None:
        proposal.type_doubt = question
        proposal.questions.append(question)
    if reading.error:
        return proposal
    proposal.questions += found.questions
    if not values and not found.questions and kind != "change":
        # A change is checked by the event it matches, not by its fields.
        proposal.questions.append("I found no details in this message. What does it need?")
    if read.quoted:
        proposal.notes.append("Quoted history below the new text was not read for actions.")

    body = body_of(text)
    if kind in ("reply", "todo") and _has_term(body, rules["negation_terms"]):
        proposal.questions.append("The message says there may be nothing to do. Is there?")

    when: When | None = None
    if kind in ("schedule", "change") and "when" in values:
        when = resolve_in_context(values["when"], text, sent, rules, change=kind == "change")
    elif kind in ("schedule", "change") and (
        found_when := date_in_sentence(proposal.what, text, sent, rules)
        or (date_in_sentence("", text, sent, rules) if kind == "change" else None)
    ):
        when, sentence = found_when
        proposal.notes.append(f"No time from the model; read from the sentence: {sentence}")
        proposal.sources[0]["text"] = sentence
    for name in rules["required"].get(kind, []):
        if name not in values and not (name == "when" and when is not None):
            label = REQUIRED_LABELS.get(name, name.replace("_", " "))
            proposal.questions.append(f"What is the {label} for '{proposal.what}'?")

    if kind == "schedule" and when is not None:
        if when.problem:
            proposal.questions.append(
                f"When should I hold time for '{proposal.what}'? {when.problem}."
            )
        elif when.day is None:
            span = values.get("when", "the message")
            proposal.questions.append(f"'{span}' is not a date. When is '{proposal.what}'?")
        elif when.at is None:
            proposal.questions.append(
                f"What time is '{proposal.what}' on {when.day:%a %b} {when.day.day}?"
            )
        proposal.start = _local(when, zone)
        if proposal.start is not None:
            proposal.end = proposal.start + timedelta(minutes=rules["thresholds"]["hold_minutes"])
            if proposal.start < message.sent:
                proposal.questions.append(
                    f"'{proposal.what}' was already past when the message arrived. Is the date "
                    "right?"
                )
    if kind == "change":
        proposal.cancel = has_cancel(body, rules)
        proposal.when = when
        if when is not None and when.problem:
            proposal.questions.append(f"Which day does this change? {when.problem}.")
        elif not proposal.cancel and (when is None or when.at is None):
            proposal.questions.append(f"'{proposal.what}' changes, but to what time?")
    if kind in ("todo", "reply", "pay"):
        due_span = values.get("due_date") or (values.get("when") if kind == "todo" else None)
        if due_span:
            due = (
                resolve_in_context(due_span, text, sent, rules)
                if kind == "todo"
                else resolve(due_span, sent, rules)
            )
            if due.problem or due.day is None:
                reason = due.problem or f"'{due_span}' is not a date"
                proposal.questions.append(f"By when is '{proposal.what}' due? {reason}.")
            else:
                proposal.due, proposal.due_at = due.day, due.at
                if kind == "pay" and due.day < sent.date():
                    proposal.questions.append(
                        f"This bill was due on {due.day.isoformat()}, before it reached you. "
                        "Is it already paid?"
                    )
    if kind == "pay" and "amount" in values:
        span_amounts = amounts(values["amount"])
        if len(set(span_amounts)) > 1:
            listed = ", ".join(f"${a}" for a in span_amounts)
            proposal.questions.append(f"'{values['amount']}' holds more than one amount: {listed}.")
        elif not span_amounts:
            proposal.questions.append(f"'{values['amount']}' is not an amount. How much is due?")
        elif span_amounts[0] <= 0:
            proposal.questions.append(
                f"The bill shows ${span_amounts[0]}, a credit or nothing due. Is there anything "
                "to pay?"
            )
        else:
            proposal.amount = span_amounts[0]
            question, note = check_bill(proposal.amount, body, rules)
            if question:
                proposal.questions.append(question)
            if note:
                proposal.notes.append(note)
            due_line = line_with(body, rules["bill_lines"]["amount_due"])
            if due_line:
                proposal.sources[0]["text"] = due_line
    return proposal


# Which field's sentence a type's source shows first.
SOURCE_FIELDS = {
    "pay": ("amount", "due_date", "what"),
    "reply": ("due_date", "what", "when"),
    "todo": ("due_date", "when", "what"),
}
DEFAULT_SOURCE_FIELDS = ("when", "due_date", "what", "amount")
REQUIRED_LABELS = {"when": "date and time", "due_date": "due date"}


# --- Reconcile with the calendar, the payee list and earlier messages -----------------------


def words(text: str, rules: dict[str, Any]) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.casefold())) - set(rules["stop_words"])


def _organizer_matches(proposal: Proposal, event: Event, contacts: Contacts) -> bool:
    return bool(event.organizer) and who_key(event.organizer, contacts)[0] == proposal.who


def _hhmm(moment: datetime) -> str:
    return f"{moment:%a %b} {moment.day} {moment:%I:%M %p}".replace(" 0", " ")


def _event_json(event: Event) -> dict[str, Any]:
    return {
        "calendar": event.event_id,
        "title": event.title,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
    }


def topic_words(text: str, contacts: Contacts, rules: dict[str, Any]) -> set[str]:
    """The words that say what an event is: no stop words and no contact's name, so "Leo's
    teeth cleaning" and "Leo swim lesson" share nothing."""
    return words(text, rules) - contacts.name_words()


def same_as_event(
    proposal: Proposal, event: Event, contacts: Contacts, rules: dict[str, Any]
) -> bool:
    """A new hold repeats a calendar event: the same start, and the event's organizer is the
    sender or its title shares a word with what the message is about (a name doesn't count)."""
    if proposal.start is None or proposal.start != event.start:
        return False
    shared = topic_words(proposal.what, contacts, rules) & topic_words(event.title, contacts, rules)
    return _organizer_matches(proposal, event, contacts) or bool(shared)


def similar_events(
    proposal: Proposal, calendar: Calendar, contacts: Contacts, rules: dict[str, Any]
) -> list[Event]:
    """Calendar events on the hold's day, at another time, from the same organizer or sharing a
    word with it: a move the model read as a new event would otherwise double-book the day."""
    assert proposal.start is not None
    zone = ZoneInfo(rules["person"]["time_zone"])
    return [
        event
        for event in calendar.events_on(proposal.start.astimezone(zone).date(), zone)
        if event.start != proposal.start
        and (
            _organizer_matches(proposal, event, contacts)
            or words(proposal.what, rules) & words(event.title, rules)
        )
    ]


def same_hold(a: Proposal, b: Proposal, contacts: Contacts, rules: dict[str, Any]) -> bool:
    """Two messages are the same event only when the who, the date and the time agree, what
    they are about shares a word, and they name the same people ("Leo's cleaning" is not
    "your cleaning")."""
    if not (a.type == b.type == "schedule" and a.start is not None and a.start == b.start):
        return False
    if a.who != b.who:
        return False
    if not topic_words(a.what, contacts, rules) & topic_words(b.what, contacts, rules):
        return False
    return people(a, contacts) == people(b, contacts)


def people(proposal: Proposal, contacts: Contacts) -> set[str]:
    """The contacts a message's new text names, other than its sender."""
    return contacts.mentioned(body_of(proposal.text)) - {proposal.who}


def fold_into(kept: Proposal, later: Proposal, rules: dict[str, Any]) -> None:
    """A later message that repeats a kept item adds itself as a source. Its own doubt about
    the type is dropped only when the runner-up was a no-action type (a reminder read as half
    fyi); any other doubt or question stays."""
    kept.sources += later.sources
    kept.notes += [note for note in later.notes if note not in kept.notes]
    doubt_ok = later.type_doubt is not None and later.runner_up in rules["no_action_types"]
    if doubt_ok:
        kept.notes.append(
            f"{later.message.id}: unsure between {later.type} and {later.runner_up}; it repeats "
            "this item, so nothing new to do."
        )
    for question in later.questions:
        if (doubt_ok and question == later.type_doubt) or question in kept.questions:
            continue
        kept.questions.append(question)
    ids = " and ".join(source["message"] for source in kept.sources)
    kept.notes.append(f"Same {kept.type} in {ids}: one item.")


def clashes(start: datetime, end: datetime, calendar: Calendar, skip: str = "") -> list[Event]:
    return [e for e in calendar.events_between(start, end) if e.event_id != skip]


def change_targets(
    change: Proposal,
    calendar: Calendar,
    kept: list[Proposal],
    contacts: Contacts,
    rules: dict[str, Any],
) -> list[Event | Proposal]:
    """The events a change can be about: a calendar event, or a new hold from an earlier
    message, on the day it names (or in the search window when it names none), whose organizer
    is the sender or whose title shares a word with the change. A cancellation that names a
    time must match that time too."""
    zone = ZoneInfo(rules["person"]["time_zone"])
    when = change.when
    about = topic_words(change.fields.get("what") or body_of(change.text), contacts, rules)
    if when is not None and when.day is not None:
        events = calendar.events_on(when.day, zone)
    else:
        days = rules["thresholds"]["change_search_days"]
        events = calendar.events_between(change.message.sent, change.message.sent + timedelta(days))
    at = when.at if when is not None and change.cancel else None
    found: list[Event | Proposal] = [
        event
        for event in events
        if (
            _organizer_matches(change, event, contacts)
            or about & topic_words(event.title, contacts, rules)
        )
        and (at is None or event.start.astimezone(zone).time() == at)
    ]
    for hold in kept:
        if hold.type != "schedule" or hold.existing or hold.start is None:
            continue
        local = hold.start.astimezone(zone)
        on_day = when is None or when.day is None or local.date() == when.day
        if (
            on_day
            and (hold.who == change.who or about & topic_words(hold.what, contacts, rules))
            and (at is None or local.time() == at)
        ):
            found.append(hold)
    return found


def reconcile_change(
    change: Proposal,
    kept: list[Proposal],
    calendar: Calendar,
    contacts: Contacts,
    rules: dict[str, Any],
) -> None:
    """Point a change at the one event it changes, and work out the new time."""
    if change.questions:
        return
    targets = change_targets(change, calendar, kept, contacts, rules)
    if not targets:
        change.questions.append(
            f"I can't find what '{change.what}' changes on your calendar or in earlier messages. "
            "Which event is it?"
        )
        return
    if len(targets) > 1:
        names = "; ".join(
            f"{t.title} {_hhmm(t.start)}" if isinstance(t, Event) else f"{t.what} ({t.message.id})"
            for t in targets
        )
        change.questions.append(f"'{change.what}' could change more than one event: {names}.")
        return
    target = targets[0]
    known = not change.who.startswith("sender:") or (
        target.who == change.who
        if isinstance(target, Proposal)
        else _organizer_matches(change, target, contacts)
    )
    if not known:
        title = target.what if isinstance(target, Proposal) else target.title
        change.questions.append(
            f"'{change.message.sender}' is not in your contacts and didn't set up '{title}'. "
            "Is this change real?"
        )
        return
    zone = ZoneInfo(rules["person"]["time_zone"])
    if isinstance(target, Proposal):
        assert target.start is not None
        assert target.end is not None
        old_start, old_end, title, ref = target.start, target.end, target.what, target.message.id
        record: dict[str, Any] = {"message": ref}
    else:
        old_start, old_end, title, ref = target.start, target.end, target.title, target.event_id
        record = {"calendar": ref}
        # Earlier messages that matched this event are about it too: one item, every source.
        for hold in [p for p in kept if (p.existing or {}).get("calendar") == ref]:
            kept.remove(hold)
            change.sources = hold.sources + change.sources
    record |= {"title": title, "from": old_start.isoformat(), "cancel": change.cancel}
    if change.cancel:
        record["to"] = None
        change.change = record
        if isinstance(target, Proposal):
            kept.remove(target)
            change.sources = target.sources + change.sources
            change.notes.append(f"The hold from {ref} is withdrawn; nothing to add.")
        return
    assert change.when is not None
    assert change.when.at is not None
    day = change.when.day or old_start.astimezone(zone).date()
    new_start = datetime.combine(day, change.when.at, tzinfo=zone)
    new_end = new_start + (old_end - old_start)
    record["to"] = new_start.isoformat()
    change.change = record
    change.start, change.end = new_start, new_end
    if new_start == old_start:
        change.existing = record | {"note": "the time is unchanged"}
    if overlap := clashes(new_start, new_end, calendar, skip=ref):
        listed = "; ".join(f"{e.title} {_hhmm(e.start)}" for e in overlap)
        change.questions.append(f"The new time {_hhmm(new_start)} overlaps {listed}.")
    if isinstance(target, Proposal):
        kept.remove(target)
        change.sources = target.sources + change.sources


def reconcile_pay(proposal: Proposal, payees: Payees) -> None:
    """Check a bill against the payee list: one known payee, its account number on the bill,
    and what the online account says. A payee on autopay matches what's already set up."""
    text = f"From: {proposal.message.sender}\n{proposal.text}"
    found = payees.find_in_text(text)
    if not found:
        proposal.questions.append("No payee in your payee list matches this bill. A new payee?")
        return
    if len(found) > 1:
        names = " and ".join(payee.name for payee in found)
        proposal.questions.append(f"The bill names more than one payee: {names}. Which is it?")
        return
    payee = found[0]
    proposal.payee = payee.name
    if payee.account_number and not has_account(text, payee.account_number):
        proposal.questions.append(
            f"The bill doesn't show your {payee.name} account number {payee.account_number}. "
            "Is it yours?"
        )
    amount, due = proposal.amount, proposal.due
    if payee.balance is not None and amount is not None and payee.balance != amount:
        proposal.questions.append(
            f"Your {payee.name} online account says ${payee.balance}; the bill says ${amount}."
            " Which is right?"
        )
    if payee.due_date is not None and due is not None and payee.due_date != due:
        proposal.questions.append(
            f"Your {payee.name} online account says due {payee.due_date}; the bill says {due}."
            " Which is right?"
        )
    if payee.autopay:
        proposal.existing = {"payee": payee.name, "autopay": True}
    elif payee.balance is None:
        proposal.notes.append(f"{payee.name} is paper-billed: the bill is the only record.")


def has_account(text: str, account: str) -> bool:
    """The account number in the text, with or without its dashes and spaces."""
    chars = [re.escape(c) for c in account if c.isalnum()]
    pattern = r"(?<![0-9A-Za-z])" + r"[\s-]?".join(chars) + r"(?![0-9A-Za-z])"
    return bool(chars) and bool(re.search(pattern, text))


def reconcile(
    proposals: list[Proposal],
    calendar: Calendar,
    contacts: Contacts,
    payees: Payees,
    rules: dict[str, Any],
) -> list[Proposal]:
    """Check each proposal, in sent-time order, against the calendar, the payee list and the
    earlier messages. A hold the calendar already has, or a repeat of an earlier hold, adds its
    message as a source; a change edits or removes the one event it matches."""
    kept: list[Proposal] = []
    for proposal in proposals:
        if proposal.type == "schedule" and proposal.start is not None and not proposal.questions:
            assert proposal.end is not None
            events = calendar.events_between(proposal.start, proposal.end)
            same = [e for e in events if same_as_event(proposal, e, contacts, rules)]
            earlier = next((p for p in kept if same_hold(p, proposal, contacts, rules)), None)
            if same:
                ref = same[0].event_id
                first = next((p for p in kept if (p.existing or {}).get("calendar") == ref), None)
                if first is not None:
                    fold_into(first, proposal, rules)
                    continue
                proposal.existing = _event_json(same[0])
            elif earlier is not None:
                fold_into(earlier, proposal, rules)
                continue
            elif events:
                listed = "; ".join(f"{e.title} {_hhmm(e.start)}" for e in events)
                proposal.questions.append(f"It overlaps {listed} on your calendar.")
            elif similar := similar_events(proposal, calendar, contacts, rules):
                listed = "; ".join(f"{e.title} {_hhmm(e.start)}" for e in similar)
                proposal.questions.append(
                    f"Your calendar has {listed} the same day. Is this a new time for it, or "
                    "another event?"
                )
        elif proposal.type == "schedule" and proposal.start is not None:
            earlier = next((p for p in kept if same_hold(p, proposal, contacts, rules)), None)
            if earlier is not None and proposal.questions == [proposal.type_doubt]:
                fold_into(earlier, proposal, rules)
                continue
        if proposal.type == "change":
            reconcile_change(proposal, kept, calendar, contacts, rules)
        if proposal.type == "pay" and not proposal.questions:
            reconcile_pay(proposal, payees)
        kept.append(proposal)
    return kept


# --- Run ----------------------------------------------------------------------------------------


@dataclass
class Result:
    proposals: list[Proposal]
    no_action: list[dict[str, Any]]
    read: list[dict[str, Any]]
    calls: Calls


def run(
    client: SIEClient,
    rules: dict[str, Any],
    messages: list[Message],
    data_dir: Path = DATA_DIR,
) -> Result:
    """Read, classify, extract, check and reconcile every message. Nothing is written here, so
    a run that stops on an SIE refusal or on every call failing leaves no partial outputs."""
    if not messages:
        raise SystemExit("No messages in the inbox, so there is nothing to sweep.")
    calendar, contacts, payees = Calendar(data_dir), Contacts(data_dir), Payees(data_dir)
    calls = Calls()
    proposals: list[Proposal] = []
    no_action: list[dict[str, Any]] = []
    read_log: list[dict[str, Any]] = []
    for message in messages:
        read = read_message(client, calls, message, rules)
        reading = classify(client, calls, message.id, read.text, rules)
        read_log.append(
            {
                "message": message.id,
                "text": read.text,
                "quoted_history": read.quoted,
                "action_type": reading.label,
                "probabilities": {k: round(v, 4) for k, v in reading.ranked()},
                **({"error": reading.error} if reading.error else {}),
            }
        )
        unsure = type_question(reading, rules)
        if reading.label in rules["no_action_types"] and unsure is None and read.problem is None:
            no_action.append(
                {
                    **message.source(source_text(read.text, [])),
                    "type": reading.label,
                    "status": NO_ACTION,
                    "type_score": round(reading.probabilities[reading.label], 4),
                }
            )
            continue
        found = (
            Fields()
            if reading.error or read.problem
            else extract_fields(client, calls, message.id, read.text, rules)
        )
        proposals.append(build_proposal(message, read, reading, found, contacts, rules))
    calls.check_all_failed()
    return Result(
        reconcile(proposals, calendar, contacts, payees, rules), no_action, read_log, calls
    )


# --- Outputs --------------------------------------------------------------------------------------


def _uid(proposal: Proposal) -> str:
    seed = f"{proposal.id}|{proposal.start.isoformat() if proposal.start else ''}"
    return hashlib.sha256(seed.encode()).hexdigest()[:24] + "@turn-messages-into-actions"


def _ics_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def events_ics(result: Result, stamp: datetime) -> str:
    """New holds that passed every check, as TENTATIVE events. No network, deterministic UIDs;
    DTSTAMP is the newest message's sent time, so reruns are identical. Changes to existing
    events are in todo.md, for the person to make in the calendar."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//sie-cookbook//turn-messages-into-actions//EN",
        "CALSCALE:GREGORIAN",
    ]
    for proposal in result.proposals:
        if proposal.type != "schedule" or proposal.status != CHECKS_PASSED:
            continue
        assert proposal.start is not None
        assert proposal.end is not None
        sources = ", ".join(source["message"] for source in proposal.sources)
        said = proposal.sources[0]["text"]
        lines += [
            "BEGIN:VEVENT",
            f"UID:{_uid(proposal)}",
            f"DTSTAMP:{_ics_time(stamp)}",
            f"DTSTART:{_ics_time(proposal.start)}",
            f"DTEND:{_ics_time(proposal.end)}",
            f"SUMMARY:{_ics_text('Hold: ' + proposal.title)}",
            f"DESCRIPTION:{_ics_text(f'From {sources}: {said}')}",
            "STATUS:TENTATIVE",
            "TRANSP:OPAQUE",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "".join(_fold(line) for line in lines)


def _fold(line: str) -> str:
    """RFC 5545 line folding: at most 75 octets per line, continuations start with a space."""
    out: list[str] = []
    current = ""
    for char in line:
        limit = 75 if not out else 74
        if len((current + char).encode()) > limit:
            out.append(current)
            current = ""
        current += char
    out.append(current)
    return "\r\n ".join(out) + "\r\n"


def when_text(proposal: Proposal) -> str:
    if proposal.change and proposal.change.get("to"):
        old = datetime.fromisoformat(proposal.change["from"])
        return f"{_hhmm(old)} -> {_hhmm(datetime.fromisoformat(proposal.change['to']))}"
    if proposal.change:
        return _hhmm(datetime.fromisoformat(proposal.change["from"]))
    if proposal.start is not None:
        return _hhmm(proposal.start)
    if proposal.due is not None:
        at = f" {proposal.due_at:%I:%M %p}".replace(" 0", " ") if proposal.due_at else ""
        return f"by {proposal.due:%a %b} {proposal.due.day}{at}"
    return ""


def _one_line(text: str) -> str:
    """A model span or sentence on one Markdown line: line breaks and runs of spaces squashed."""
    return re.sub(r"\s+", " ", text).strip()


def todo_md(result: Result, person: str) -> str:
    sections = (
        ("schedule", "New calendar holds"),
        ("change", "Calendar changes"),
        ("pay", "Bills to pay"),
        ("reply", "Replies"),
        ("todo", "To-dos"),
    )
    out = [
        f"# Proposed actions for {person}",
        "",
        "Nothing here has been done. Questions need your answer first; the rest passed the",
        "checks and wait for your approval.",
        "",
        "## Questions for you",
        "",
    ]

    def cite(proposal: Proposal) -> str:
        return ", ".join(source["message"] for source in proposal.sources)

    questions = [p for p in result.proposals if p.status == QUESTION]
    for proposal in questions:
        for question in proposal.questions:
            out.append(f"- [ ] {_one_line(question)} ({proposal.type}; {cite(proposal)})")
        out += [f"  - {s['message']}: {_one_line(s['text'])}" for s in proposal.sources]
    if not questions:
        out.append("- none")
    for kind, heading in sections:
        items = [p for p in result.proposals if p.type == kind and p.status == CHECKS_PASSED]
        if not items:
            continue
        out += ["", f"## {heading}", ""]
        for proposal in items:
            when = when_text(proposal)
            title = _one_line(proposal.title)
            out.append(f"- [ ] {title}{', ' + when if when else ''} ({cite(proposal)})")
            out += [f"  - {s['message']}: {_one_line(s['text'])}" for s in proposal.sources]
    existing = [p for p in result.proposals if p.status == MATCHES_EXISTING]
    if existing:
        out += ["", "## Already on your calendar or payee list", ""]
        for proposal in existing:
            match = proposal.existing or {}
            where = (
                f"calendar {match['calendar']}"
                if "calendar" in match
                else f"{match.get('payee', '')} autopay"
            )
            out.append(f"- {_one_line(proposal.title)}: matches {where} ({cite(proposal)})")
    out += ["", "## No action", ""]
    out += [f"- {item['message']} {item['from']}: {item['type']}" for item in result.no_action]
    return "\n".join(out) + "\n"


def proposals_json(result: Result, person: str) -> dict[str, Any]:
    statuses = [p.status for p in result.proposals] + [NO_ACTION] * len(result.no_action)
    return {
        "person": person,
        "counts": {status: statuses.count(status) for status in STATUSES},
        "proposals": [proposal.as_json() for proposal in result.proposals],
        "no_action": result.no_action,
    }


def write_outputs(output_dir: Path, result: Result, rules: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    person = rules["person"]["name"]
    text = json.dumps(proposals_json(result, person), indent=2, ensure_ascii=False) + "\n"
    (output_dir / "proposals.json").write_text(text, encoding="utf-8")
    read = json.dumps(result.read, indent=2, ensure_ascii=False) + "\n"
    (output_dir / "messages.json").write_text(read, encoding="utf-8")
    (output_dir / "todo.md").write_text(todo_md(result, person), encoding="utf-8")
    with (output_dir / "events.ics").open("w", encoding="utf-8", newline="") as handle:
        handle.write(events_ics(result, newest(result)))


def newest(result: Result) -> datetime:
    times = [datetime.fromisoformat(s["sent"]) for p in result.proposals for s in p.sources]
    times += [datetime.fromisoformat(item["sent"]) for item in result.no_action]
    return max(times, default=datetime(2000, 1, 1, tzinfo=UTC))


# --- Entry point --------------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rules = load_rules()
    messages = parse_messages(load_inbox(), rules)
    with make_client() as client:
        result = stop_on_refusal(lambda: run(client, rules, messages))
    write_outputs(output, result, rules)
    for proposal in result.proposals:
        sources = ",".join(source["message"] for source in proposal.sources)
        detail = proposal.questions[0] if proposal.questions else when_text(proposal)
        print(f"{sources:<16} {proposal.type:<9} {proposal.status:<17} {proposal.title}  {detail}")
    for item in result.no_action:
        print(f"{item['message']:<16} {item['type']:<9} {NO_ACTION:<17} {item['from']}")
    counts = proposals_json(result, "")["counts"]
    print("\n" + ", ".join(f"{n} {status}" for status, n in counts.items()))
    calls = ", ".join(f"{n} {model}" for model, n in result.calls.count.items())
    print(f"SIE calls: {result.calls.total} ({calls})")
    print("Nothing was sent, booked or paid: you approve each item and do it in its app.")
    print(f"outputs: {output}")


if __name__ == "__main__":
    main()
