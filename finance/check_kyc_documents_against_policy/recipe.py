"""Check a KYC case's form and scanned documents against the CDD policy, requirement by requirement.

Use case:  KYC document check: does each onboarding case meet the bank's customer due diligence
           policy, and if not, what do we ask the customer for?
Who:       KYC analysts and onboarding officers at a smaller bank, broker or credit union that
           takes documents by upload, post or branch scan, and KYC remediation teams.
Problem:   The application form and the policy are structured; the ID and the proof of address
           are scans. Someone reads each scan, checks the MRZ, the expiry, the name, the date of
           birth, the address and the document's age against the form, and works out what's
           missing.
Input:     The onboarding system (onboarding.py over data/onboarding_cases.csv and
           data/onboarding_documents.csv: the form, and each uploaded document with the type
           the customer gave it), two scanned PDFs, the policy in policy.yaml and how the
           documents are read in rules.yaml.
SIE pipeline (two models):
           extract  lightonai/LightOnOCR-3-4B  read each scanned page as Markdown, MRZ
                                               included; called once per page
           extract  fastino/gliner2-large-v1   locate the ID's name, date of birth, expiry and
                                               number, and the address document's issuer,
                                               holder, address and date; called once per
                                               window of about 512 tokens
           (Python checks the form and the document types before any call, parses the MRZ and
           its check digits, keeps only labelled values, looks the issuer up, and checks each
           requirement against the form and the policy.)
Output:    cases/<case>.json per case (status, the checklist, what to ask the customer for, the
           fields read), checklist.csv (one row per requirement), cases.csv and requests.md (the
           plain list of what to ask each customer for) in runs/<timestamp>/. The status names
           what the checks found; the KYC analyst decides what happens next.
Why SIE:   Two models, and no generative model makes a decision. The policy, the MRZ, the
           dates and every comparison are plain Python reading policy.yaml and rules.yaml.
Run using: SIE Serverless, or SIE Cluster or SIE Local with SIE_BASE_URL.

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
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pypdfium2 as pdfium
import yaml
from dotenv import load_dotenv
from onboarding import Application, OnboardingSystem, SubmittedDocument
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
POLICY_PATH = ROOT / "policy.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
# A failed SIE call's message is cut to this many characters in reasons and the summary line.
ERROR_CHARS = 200
# A cell starting with one of these runs as a formula when a spreadsheet opens the CSV.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")

# Requirement status: what the check found.
MET, NOT_MET, MISSING = "met", "not_met", "missing"
# Case status: every requirement met, or not. The KYC analyst decides what happens next.
CHECKS_PASSED, REVIEW = "checks_passed", "review"
# When GLiNER2 finds nothing, SIE Serverless answers the output_schema call with HTTP 500 and this
# message. We record it as "no fields found"; a missing field can only leave a requirement
# not met, never meet it.
EMPTY_RESULT_ERROR = "Inference completed without a valid conserved billing settlement"
MONTHS = [
    *("January", "February", "March", "April", "May", "June"),
    *("July", "August", "September", "October", "November", "December"),
]


# --- MRZ (ICAO Doc 9303) ----------------------------------------------------------------------

MRZ_WEIGHTS = (7, 3, 1)


def check_digit(text: str) -> str:
    """ICAO 9303 check digit: digits as-is, A-Z as 10-35, '<' as 0, weighted 7-3-1, mod 10."""
    total = 0
    for index, char in enumerate(text):
        if char.isdigit():
            value = int(char)
        elif "A" <= char <= "Z":
            value = ord(char) - 55
        else:
            value = 0
        total += value * MRZ_WEIGHTS[index % 3]
    return str(total % 10)


@dataclass(frozen=True)
class Mrz:
    format: str  # TD1 (ID card, 3 x 30) or TD3 (passport, 2 x 44)
    document_number: str
    date_of_birth: str  # YYMMDD
    date_of_expiry: str  # YYMMDD
    surname: str
    given_names: str
    checks: dict[str, bool]

    @property
    def valid(self) -> bool:
        return all(self.checks.values())


def _clean_mrz_line(line: str) -> str:
    # OCR sometimes writes "<" as an entity, escaped, or as a guillemet.
    for written, meant in (("&lt;", "<"), ("\\<", "<"), (chr(0xAB), "<<"), (chr(0x2039), "<")):
        line = line.replace(written, meant)
    return re.sub(r"[\s`|]", "", line).upper()


def _fit(line: str, length: int) -> str:
    """Repair an OCR'd MRZ line whose filler run came out one or two '<' short or long."""
    if len(line) == length:
        return line
    runs = list(re.finditer(r"<{2,}", line))
    if not runs:
        return line
    run = max(runs, key=lambda match: len(match.group()))
    size = max(1, len(run.group()) + length - len(line))
    return line[: run.start()] + "<" * size + line[run.end() :]


def _mrz_name(text: str) -> tuple[str, str]:
    surname, _, given = text.partition("<<")
    return surname.replace("<", " ").strip(), given.replace("<", " ").strip()


def parse_mrz(text: str) -> Mrz | None:
    """Find and parse a TD1 or TD3 MRZ in OCR text, or None when there is none."""
    lines = [_clean_mrz_line(line) for line in text.splitlines()]
    lines = [line for line in lines if re.fullmatch(r"[A-Z0-9<]{26,60}", line) and "<" in line]
    for index, line in enumerate(lines):
        if line.startswith("P") and len(line) >= 40 and index + 1 < len(lines):
            l1, l2 = _fit(line, 44), _fit(lines[index + 1], 44)
            if len(l1) != 44 or len(l2) != 44:
                continue
            surname, given = _mrz_name(l1[5:44])
            checks = {
                "document_number": check_digit(l2[0:9]) == l2[9],
                "date_of_birth": check_digit(l2[13:19]) == l2[19],
                "date_of_expiry": check_digit(l2[21:27]) == l2[27],
                "composite": check_digit(l2[0:10] + l2[13:20] + l2[21:43]) == l2[43],
            }
            return Mrz("TD3", l2[0:9].strip("<"), l2[13:19], l2[21:27], surname, given, checks)
        if line.startswith(("I", "A", "C")) and len(line) <= 34 and index + 2 < len(lines):
            l1, l2, l3 = (_fit(part, 30) for part in lines[index : index + 3])
            if {len(l1), len(l2), len(l3)} != {30}:
                continue
            surname, given = _mrz_name(l3)
            checks = {
                "document_number": check_digit(l1[5:14]) == l1[14],
                "date_of_birth": check_digit(l2[0:6]) == l2[6],
                "date_of_expiry": check_digit(l2[8:14]) == l2[14],
                "composite": check_digit(l1[5:30] + l2[0:7] + l2[8:15] + l2[18:29]) == l2[29],
            }
            return Mrz("TD1", l1[5:14].strip("<"), l2[0:6], l2[8:14], surname, given, checks)
    return None


def mrz_date(yymmdd: str, *, birth: bool, reference: date) -> date | None:
    """An MRZ date; a birth date takes the century that puts it on or before the reference."""
    try:
        day = datetime.strptime("20" + yymmdd, "%Y%m%d").date()
    except ValueError:
        return None
    if birth and day > reference:
        day = day.replace(year=day.year - 100)
    return day


# --- Cases and requirements -------------------------------------------------------------------


@dataclass
class Requirement:
    requirement: str
    document: str  # the document id it was checked on; "" for the form and the case
    status: str  # met, not_met or missing
    detail: str
    ask: str = ""  # what to ask the customer for; "" when it's for the analyst to look at


@dataclass
class DocumentRead:
    """What the recipe read from one uploaded document."""

    doc: SubmittedDocument
    fields: dict[str, str] = field(default_factory=dict)
    conflicts: dict[str, list[str]] = field(default_factory=dict)  # field -> values read
    mrz: Mrz | None = None
    issuer_type: str = ""  # the type the issuer read from an address document issues
    issuer_problem: str = ""  # why the issuer gives no type
    problem: str = ""  # why it couldn't be read; "" when it was
    notes: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        mrz = None if self.mrz is None else {**vars(self.mrz), "valid": self.mrz.valid}
        return {
            "purpose": self.doc.purpose,
            "declared_type": self.doc.declared_type,
            "where": f"{self.doc.file} p{self.doc.page}",
            "fields": self.fields,
            "conflicting_values": self.conflicts,
            "mrz": mrz,
            "issuer_type": self.issuer_type,
            "problem": self.problem,
            "notes": self.notes,
        }


@dataclass
class CaseResult:
    case: str
    received: date
    requirements: list[Requirement] = field(default_factory=list)
    documents: dict[str, DocumentRead] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def unmet(self) -> list[Requirement]:
        return [req for req in self.requirements if req.status != MET]

    @property
    def status(self) -> str:
        return REVIEW if self.unmet else CHECKS_PASSED

    @property
    def ask_customer(self) -> list[str]:
        return list(dict.fromkeys(req.ask for req in self.unmet if req.ask))

    @property
    def for_analyst(self) -> list[Requirement]:
        return [req for req in self.unmet if not req.ask]

    def add(self, requirement: str, document: str, status: str, detail: str, ask: str = "") -> None:
        self.requirements.append(Requirement(requirement, document, status, detail, ask))

    def as_json(self) -> dict[str, Any]:
        return {
            "case": self.case,
            "received": self.received.isoformat(),
            "status": self.status,
            "ask_customer": self.ask_customer,
            "requirements": [vars(req) for req in self.requirements],
            "documents": {name: read.as_json() for name, read in self.documents.items()},
            "notes": self.notes,
        }


def load_yaml(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return load_yaml(path)


def load_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    """The policy, with every requirement it names checked against the ones this code knows."""
    policy = load_yaml(path)
    for purpose, known in (("identity", IDENTITY_CHECKS), ("address", ADDRESS_CHECKS)):
        unknown = set(policy[purpose]["requirements"]) - set(known)
        if unknown:
            raise ValueError(f"{path.name}: no check for {purpose} requirement(s) {unknown}")
    return policy


def output_schema(rules: dict[str, Any], purpose: str) -> dict[str, Any]:
    """The GLiNER2 output_schema for one document purpose: one optional string per field."""
    properties = {
        name: {"type": "string", "description": spec["description"]}
        for name, spec in rules["documents"][purpose].items()
    }
    return {"type": "object", "properties": properties}


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


# --- SIE call failures ------------------------------------------------------------------------


class SIEStopped(RuntimeError):
    """SIE refused the call for a reason a retry won't fix, or no call succeeded; the run
    stops without outputs."""


class CallFailed(Exception):
    """One SIE call failed for a reason that may pass; the document it was for isn't read."""


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
        reply without `key`, is recorded and raised as CallFailed, so the caller leaves that
        document's requirements not met and the run goes on. With `empty_result_ok` (the GLiNER2
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
        print(f"{n} of {total} SIE calls failed; their cases go to review. First: {short(first)}")


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
    those cases go to review."""
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
    """The error for a per-case reason or the summary line, its message cut short."""
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


# --- Extract fields (GLiNER2) -----------------------------------------------------------------


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


def flatten_markdown(text: str) -> str:
    """Turn OCR Markdown into plain "Label | value" lines, so each label sits on its value's line.

    LightOnOCR writes forms as HTML tables. A row of labels (with lower-case letters) followed
    by a row of values (no lower-case letters), the way a passport prints a label above its
    value, becomes one "Label | value" line per pair. A "label<br>value" cell becomes
    "label | value". Tags, headings, bold marks and bullets are dropped and HTML entities
    decoded.
    """

    def table(match: re.Match[str]) -> str:
        rows = [
            [
                re.sub(r"<[^>]+>", " ", re.sub(r"<br\s*/?>", " | ", cell)).strip()
                for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
            ]
            for row in re.findall(r"<tr[^>]*>(.*?)</tr>", match.group(0), re.S)
        ]
        lines: list[str] = []
        index = 0
        while index < len(rows):
            row = rows[index]
            following = rows[index + 1] if index + 1 < len(rows) else []
            if (
                len(following) == len(row) > 0
                and all(re.search(r"[a-z]", cell) for cell in row)
                and not any(re.search(r"[a-z]", cell) for cell in following)
            ):
                lines += [f"{label} | {value}" for label, value in zip(row, following, strict=True)]
                index += 2
                continue
            lines.append(" | ".join(row))
            index += 1
        return "\n" + "\n".join(lines) + "\n"

    text = re.sub(r"<table[^>]*>.*?</table>", table, text, flags=re.S)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"\*\*|^\s*(?:#+|[-*])\s+", "", text, flags=re.M)
    return html.unescape(re.sub(r"\n\s*\n+", "\n", text).strip())


def _label_at(line: str, label: str) -> re.Match[str] | None:
    return re.search(rf"(?<![a-z]){re.escape(label)}(?![a-z])", line.casefold())


def labelled_cell(value: str, window: str, labels: list[str]) -> str | None:
    """The whole cell after a label, on the line where the model's span sits; None if no label.

    "Service address: Flat 2, 114 Harbour Rd, Port Ellery" with the span "114 Harbour Rd"
    gives "Flat 2, 114 Harbour Rd, Port Ellery". Cells on one line are split at " | ". Labels
    match whole words, so "expiry" does not match "expiryless".
    """
    span = _squash(value)
    for line in window.splitlines():
        for label in labels:
            match = _label_at(line, label)
            if match is None:
                continue
            rest = line[match.end() :]
            if re.match(r"^[^:|]{0,12}[:|]", rest):
                rest = re.sub(r"^[^:|]*?[:|]", "", rest, count=1)
            cell = rest.split(" | ")[0].strip(" :|.")
            if span and span in _squash(cell):
                return cell
    return None


def whole_cell(value: str, window: str) -> str | None:
    """The whole cell (or line) the span sits in, for a value with no label, such as the
    issuer's name heading a bill. None when the span spreads over two cells."""
    span = _squash(value)
    for line in window.splitlines():
        for cell in line.split(" | "):
            if span and span in _squash(cell):
                return cell.strip(" :|.")
    return None


def extract_fields(
    client: SIEClient,
    calls: Calls,
    key: str,
    purpose: str,
    text: str,
    rules: dict[str, Any],
    read: DocumentRead,
) -> None:
    """Find one document's fields with GLiNER2 and keep the checked cell for each span.

    A span that is not in the window's text, or not on a line carrying the field's label, is
    dropped with a note, so the requirement that needs it can't be met by a value nobody wrote.
    A field read with two different values (in two windows, or twice in one) is left out and
    kept in `read.conflicts`. A window whose SIE call failed leaves the document unread.
    """
    model, limits = rules["models"]["extract"], rules["thresholds"]
    specs = rules["documents"][purpose]
    schema = output_schema(rules, purpose)
    found: dict[str, str] = {}
    parts = windows(flatten_markdown(text), limits["window_words"], limits["window_overlap_words"])
    for index, window in enumerate(parts, start=1):
        item = Item(id=f"extract/{key}-w{index}", text=window)
        # One item per call: the empty-result HTTP 500 would fail a whole batch.
        try:
            work = functools.partial(
                client.extract,
                model,
                item,
                output_schema=schema,
                options={"overflow_policy": "error"},  # a window too long to read whole fails
                provision_timeout_s=WAIT_S,
            )
            response = calls.call(model, work, "data", empty_result_ok=True)
        except CallFailed as failure:
            read.problem = f"window {index} was not read: {failure}"
            continue
        if response.get("empty_result_error"):
            read.notes.append(f"window {index}: no fields found (HTTP 500 empty result)")
        data = response.get("data")
        for name, value in (data if isinstance(data, dict) else {}).items():
            spec = specs.get(name)
            if spec is None:
                continue
            if not isinstance(value, str) or not re.search(r"[^\W_]", value):
                continue  # empty or a form blank ("______")
            # A cell never runs over a line break in the flattened text.
            value = value.strip().splitlines()[0].strip(" |")
            if _squash(value) not in _squash(window):
                read.notes.append(f"{name}: '{value}' is not in the text; dropped")
                continue
            cell = (
                whole_cell(value, window)
                if spec.get("cell")
                else labelled_cell(value, window, spec.get("label", []))
            )
            if cell is None:
                read.notes.append(f"{name}: '{value}' is not on a labelled line; dropped")
                continue
            target = spec.get("field", name)
            if target in found and _squash(found[target]) != _squash(cell):
                read.conflicts.setdefault(target, [found[target]]).append(cell)
            found.setdefault(target, cell)
    for name in read.conflicts:
        found.pop(name, None)
        read.conflicts[name] = list(dict.fromkeys(read.conflicts[name]))
    read.fields.update(found)


# --- Comparisons (plain Python) ---------------------------------------------------------------


def _words(text: str) -> list[str]:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.findall(r"[a-z0-9]+", plain.casefold())


def parse_date(value: str, formats: list[str]) -> date | None:
    cleaned = re.sub(r"\s+", " ", value).strip(" .,")
    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def long_date(day: date) -> str:
    return f"{day.day} {MONTHS[day.month - 1]} {day.year}"


def name_matches(surname: str, given_names: str, other: str, rules: dict[str, Any]) -> bool:
    """`other` names the same person: every surname word, the first given name, nothing extra.

    A middle name may be left out and a single letter matches a given name with that initial.
    """
    surnames, givens = _words(surname), _words(given_names)
    words = [word for word in _words(other) if word not in rules["name_titles"]]
    if not surnames or not givens or not set(surnames) <= set(words) or givens[0] not in words:
        return False
    for word in words:
        if word in surnames or word in givens:
            continue
        if len(word) == 1 and any(given.startswith(word) for given in givens[1:]):
            continue
        return False
    return True


@dataclass(frozen=True)
class Address:
    house_number: str
    flat: str  # "" when there is none
    street: str
    postcode: str


def _postcode(text: str, rules: dict[str, Any]) -> list[str]:
    found = re.findall(rules["address"]["postcode_pattern"], text, flags=re.I)
    return list(dict.fromkeys(re.sub(r"\s+", "", code).upper() for code in found))


def parse_address(text: str, rules: dict[str, Any]) -> tuple[Address | None, str]:
    """Take a written address apart into house number, flat, street and postcode.

    Returns the address and "", or None and why it couldn't be taken apart: no postcode or two,
    no house number before a street type, or two house numbers, flats or streets (an address
    span that holds two addresses).
    """
    spec = rules["address"]
    codes = _postcode(text, rules)
    if len(codes) != 1:
        return None, "no postcode" if not codes else "two postcodes: " + ", ".join(codes)
    rest = re.sub(spec["postcode_pattern"], " ", text, flags=re.I)
    flat_words = set(spec["flat_words"])
    tokens: list[str] = []
    for word in _words(rest):
        glued = re.fullmatch(r"([a-z]+)(\d+[a-z]?)", word)
        if glued and glued.group(1) in flat_words:  # "flat2", "apt3"
            tokens += [glued.group(1), glued.group(2)]
        else:
            tokens.append(spec["abbreviations"].get(word, word))
    flats: list[str] = []
    remaining: list[str] = []
    index = 0
    while index < len(tokens):
        if tokens[index] in flat_words and index + 1 < len(tokens):
            flats.append(tokens[index + 1])
            index += 2
            continue
        remaining.append(tokens[index])
        index += 1
    streets: list[tuple[str, str]] = []
    for start, token in enumerate(remaining):
        if not re.fullmatch(r"\d+[a-z]?", token):
            continue
        for end in range(start + 1, min(len(remaining), start + 1 + spec["max_street_words"])):
            if re.match(r"\d", remaining[end]):
                break
            if remaining[end] in spec["street_types"]:
                streets.append((token, " ".join(remaining[start + 1 : end + 1])))
                break
    if len(flats) > 1:
        return None, "two flat numbers: " + ", ".join(flats)
    if not streets:
        return None, "no house number and street"
    if len(set(streets)) > 1:
        return None, "two street addresses: " + ", ".join(f"{n} {s}" for n, s in streets)
    house, street = streets[0]
    return Address(house, flats[0] if flats else "", street, codes[0]), ""


def form_address(form: dict[str, str], rules: dict[str, Any]) -> Address:
    """The application's address in the same shape as a parsed one."""
    abbreviations = rules["address"]["abbreviations"]
    street = " ".join(abbreviations.get(word, word) for word in _words(form.get("street", "")))
    postcode = _postcode(form.get("postcode", ""), rules)
    return Address(
        " ".join(_words(form.get("house_number", ""))),
        " ".join(_words(form.get("flat", ""))),
        street,
        postcode[0] if postcode else "",
    )


def address_differences(document: Address, application: Address) -> list[str]:
    differences = []
    for part in ("house_number", "flat", "street", "postcode"):
        got, want = getattr(document, part), getattr(application, part)
        if got != want:
            label = part.replace("_", " ")
            differences.append(f"{label} '{got or '(none)'}', application '{want or '(none)'}'")
    return differences


def months_before(day: date, months: int) -> date:
    month = day.month - months
    year = day.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    for last in (31, 30, 29, 28):
        try:
            return date(year, month, min(day.day, last))
        except ValueError:
            continue
    raise AssertionError("unreachable")


def issuer_type(issuer: str, text: str, rules: dict[str, Any]) -> tuple[str | None, str]:
    """The document type the issuer issues, from `issuers` in rules.yaml, and the reason when
    there isn't one: the issuer isn't listed, or the page also names a listed issuer of
    another type."""
    listed = {" ".join(_words(name)): kind for name, kind in rules["issuers"].items()}
    key = " ".join(_words(issuer))
    kind = listed.get(key)
    if kind is None:
        return None, f"issuer '{issuer}' is not on the issuer list; a person confirms the type"
    page = f" {' '.join(_words(text))} "
    others = sorted(name for name, other in listed.items() if other != kind and f" {name} " in page)
    if others:
        return None, f"the page names issuers of two types: '{issuer}' and '{others[0]}'"
    return kind, ""


# --- Requirements (plain Python) --------------------------------------------------------------


@dataclass(frozen=True)
class Context:
    """What every requirement check may look at."""

    form: dict[str, str]
    received: date
    policy: dict[str, Any]
    rules: dict[str, Any]

    @property
    def oldest(self) -> date:
        return months_before(self.received, int(self.policy["address"]["max_age_months"]))

    def ask(self, requirement: str) -> str:
        text = str(self.policy["asks"].get(requirement, ""))
        return text.format(oldest=long_date(self.oldest)).strip()


Verdict = tuple[str, str]  # (status, detail)


def _value(read: DocumentRead, name: str, label: str) -> tuple[str | None, str]:
    """A field's value, or None and why it isn't there (not read, or read two ways)."""
    if name in read.conflicts:
        shown = " / ".join(f"'{value}'" for value in read.conflicts[name])
        return None, f"{label} read two ways: {shown}"
    if name not in read.fields:
        return None, f"{label} not read"
    return read.fields[name], ""


def check_id_mrz(read: DocumentRead, ctx: Context) -> Verdict:
    mrz = read.mrz
    # An MRZ on the page is checked whatever type the customer said the document was.
    if mrz is None and read.doc.declared_type not in ctx.policy["identity"]["mrz_types"]:
        return MET, f"a {read.doc.declared_type} carries no MRZ to check"
    if mrz is None:
        return NOT_MET, "no MRZ found on the ID"
    if not mrz.valid:
        bad = ", ".join(name for name, ok in mrz.checks.items() if not ok)
        return NOT_MET, f"MRZ check digit fails for: {bad}"
    return MET, f"{mrz.format} MRZ, every check digit holds"


def check_id_name(read: DocumentRead, ctx: Context) -> Verdict:
    surname, given = ctx.form.get("surname", ""), ctx.form.get("given_names", "")
    if not surname or not given:
        return NOT_MET, "no name on the form to compare with"
    printed_surname, why = _value(read, "surname", "surname")
    printed_given, why_given = _value(read, "given_names", "given names")
    if printed_surname is None or printed_given is None:
        return NOT_MET, why or why_given
    application = f"{given} {surname}"
    problems = []
    if _words(printed_surname) != _words(surname) or _words(printed_given) != _words(given):
        problems.append(f"ID '{printed_given} {printed_surname}', application '{application}'")
    mrz = read.mrz
    if mrz is not None:
        # TD1 names can be cut at 30 characters; a cut MRZ name must still be a prefix.
        machine = " ".join(_words(mrz.given_names))
        if _words(mrz.surname) != _words(surname) or not (
            machine and " ".join(_words(given)).startswith(machine)
        ):
            problems.append(f"MRZ '{mrz.given_names} {mrz.surname}', application '{application}'")
    if problems:
        return NOT_MET, "; ".join(problems)
    return MET, f"'{printed_given} {printed_surname}'" + (" (MRZ agrees)" if mrz else "")


def check_id_date_of_birth(read: DocumentRead, ctx: Context) -> Verdict:
    formats = ctx.rules["date_formats"]
    on_form = parse_date(ctx.form.get("date_of_birth", ""), formats)
    if on_form is None:
        return NOT_MET, f"date of birth on the form '{ctx.form.get('date_of_birth', '')}'"
    printed, why = _value(read, "date_of_birth", "date of birth")
    if printed is None:
        return NOT_MET, why
    on_id = parse_date(printed, formats)
    if on_id is None:
        return NOT_MET, f"date of birth on the ID '{printed}' is not a date"
    problems = []
    if on_id != on_form:
        swapped = (on_id.day, on_id.month) == (on_form.month, on_form.day)
        hint = " (day and month swapped)" if swapped else ""
        problems.append(f"ID {on_id.isoformat()}, application {on_form.isoformat()}{hint}")
    mrz = read.mrz
    note = ""
    if mrz is not None and mrz.checks.get("date_of_birth"):
        machine = mrz_date(mrz.date_of_birth, birth=True, reference=ctx.received)
        if machine != on_form:
            problems.append(f"MRZ {machine}, application {on_form.isoformat()}")
        note = " (MRZ agrees)"
    elif mrz is not None:
        note = " (the MRZ birth date fails its check digit, so only the printed date counts)"
    if problems:
        return NOT_MET, "; ".join(problems)
    return MET, on_id.isoformat() + note


def check_id_number(read: DocumentRead, ctx: Context) -> Verdict:
    printed, why = _value(read, "document_number", "document number")
    if printed is None:
        return NOT_MET, why
    number = re.sub(r"[^A-Z0-9]", "", printed.upper())
    mrz = read.mrz
    if mrz is not None and mrz.checks.get("document_number"):
        if number != mrz.document_number:
            return NOT_MET, f"printed '{printed}', MRZ '{mrz.document_number}'"
        return MET, f"{number} (MRZ agrees)"
    return MET, number


def check_id_valid(read: DocumentRead, ctx: Context) -> Verdict:
    printed, why = _value(read, "date_of_expiry", "date of expiry")
    on_id = parse_date(printed, ctx.rules["date_formats"]) if printed else None
    mrz = read.mrz
    machine = None
    if mrz is not None and mrz.checks.get("date_of_expiry"):
        machine = mrz_date(mrz.date_of_expiry, birth=False, reference=ctx.received)
    if on_id is not None and machine is not None and on_id != machine:
        return NOT_MET, f"expiry printed {on_id}, MRZ {machine}"
    expiry = machine or on_id
    if expiry is None:
        return NOT_MET, why or f"date of expiry '{printed}' is not a date"
    limit = ctx.received + timedelta(days=int(ctx.policy["identity"]["min_validity_days"]))
    if expiry < limit:
        return NOT_MET, f"the ID expired on {expiry.isoformat()}"
    return MET, f"valid until {expiry.isoformat()}"


def check_address_type(read: DocumentRead, ctx: Context) -> Verdict:
    issuer, why = _value(read, "issuer", "issuer")
    if issuer is None:
        return NOT_MET, why
    kind = read.issuer_type
    if not kind:
        return NOT_MET, read.issuer_problem or f"no type for issuer '{issuer}'"
    accepted = ctx.policy["address"]["accepted"]
    told = f"uploaded as {read.doc.declared_type}; '{issuer}' issues {kind}"
    if kind not in accepted:
        return NOT_MET, f"{told}, which the policy doesn't accept as proof of address"
    return MET, told if kind != read.doc.declared_type else f"'{issuer}' issues {kind}"


def check_address_holder(read: DocumentRead, ctx: Context) -> Verdict:
    surname, given = ctx.form.get("surname", ""), ctx.form.get("given_names", "")
    holder, why = _value(read, "account_holder", "account holder")
    if holder is None:
        return NOT_MET, why
    if not name_matches(surname, given, holder, ctx.rules):
        return NOT_MET, f"in the name of '{holder}', application '{given} {surname}'"
    return MET, f"'{holder}'"


def check_address_matches(read: DocumentRead, ctx: Context) -> Verdict:
    written, why = _value(read, "address", "address")
    if written is None:
        return NOT_MET, why
    parsed, problem = parse_address(written, ctx.rules)
    if parsed is None:
        return NOT_MET, f"'{written}': {problem}"
    differences = address_differences(parsed, form_address(ctx.form, ctx.rules))
    if differences:
        return NOT_MET, f"'{written}': " + "; ".join(differences)
    return MET, f"'{written}'"


def check_address_age(read: DocumentRead, ctx: Context) -> Verdict:
    written, why = _value(read, "statement_date", "statement date")
    if written is None:
        return NOT_MET, why
    issued = parse_date(written, ctx.rules["date_formats"])
    if issued is None:
        return NOT_MET, f"statement date '{written}' is not a date"
    if issued > ctx.received:
        return NOT_MET, f"dated {issued.isoformat()}, after the case date"
    if issued < ctx.oldest:
        months = ctx.policy["address"]["max_age_months"]
        return NOT_MET, f"dated {issued.isoformat()}, more than {months} months before the case"
    return MET, f"dated {issued.isoformat()}"


IDENTITY_CHECKS: dict[str, Callable[[DocumentRead, Context], Verdict]] = {
    "id_mrz": check_id_mrz,
    "id_name": check_id_name,
    "id_date_of_birth": check_id_date_of_birth,
    "id_number": check_id_number,
    "id_valid": check_id_valid,
}
ADDRESS_CHECKS: dict[str, Callable[[DocumentRead, Context], Verdict]] = {
    "address_type": check_address_type,
    "address_holder": check_address_holder,
    "address_matches": check_address_matches,
    "address_age": check_address_age,
}


def check_form(form: dict[str, str], ctx: Context, result: CaseResult) -> None:
    """Every mandatory form field filled in."""
    blanks = {value.casefold() for value in ctx.policy["form"]["blank_values"]}
    for name, label in ctx.policy["form"]["mandatory"].items():
        value = form.get(name, "").strip()
        requirement = f"form_{name}"
        if not value or value.casefold() in blanks:
            ask = str(ctx.policy["asks"]["form"]).format(field=label)
            result.add(requirement, "", MISSING, f"{label} is blank on the form", ask)
        else:
            result.add(requirement, "", MET, value)


def sort_documents(
    documents: tuple[SubmittedDocument, ...], ctx: Context, result: CaseResult
) -> tuple[list[SubmittedDocument], list[SubmittedDocument]]:
    """The identity and address documents the policy accepts by their declared type, with the
    case-level requirements for each. No document is read yet."""
    identity_policy, address_policy = ctx.policy["identity"], ctx.policy["address"]
    identity = [
        doc
        for doc in documents
        if doc.purpose == "identity" and doc.declared_type in identity_policy["accepted"]
    ]
    identity_ids = {doc.document for doc in identity}
    address: list[SubmittedDocument] = []
    refused: list[str] = []
    for doc in documents:
        if doc.purpose != "address":
            continue
        if doc.declared_type not in address_policy["accepted"]:
            refused.append(f"{doc.document} ({doc.declared_type}) is not an accepted type")
        elif address_policy["separate_from_identity"] and doc.document in identity_ids:
            refused.append(f"{doc.document} is also the identity document")
        else:
            address.append(doc)
    if identity:
        names = ", ".join(f"{doc.document} ({doc.declared_type})" for doc in identity)
        result.add("identity_document", "", MET, names)
    else:
        given = [doc.declared_type for doc in documents if doc.purpose == "identity"]
        detail = "no accepted identity document" + (f"; given: {', '.join(given)}" if given else "")
        result.add("identity_document", "", MISSING, detail, ctx.ask("identity_document"))
    if address:
        names = ", ".join(f"{doc.document} ({doc.declared_type})" for doc in address)
        result.add("address_document", "", MET, names)
    else:
        detail = "no separate, accepted proof of address" + (
            "; " + "; ".join(refused) if refused else ""
        )
        result.add("address_document", "", MISSING, detail, ctx.ask("address_document"))
    return identity, address


def read_document(
    client: SIEClient,
    calls: Calls,
    doc: SubmittedDocument,
    case: str,
    rules: dict[str, Any],
    data_dir: Path,
    result: CaseResult,
) -> DocumentRead:
    """OCR one document, find its fields, and for an ID parse the MRZ."""
    read = DocumentRead(doc)
    where = f"{doc.file} p{doc.page}"
    try:
        text = read_page(client, calls, data_dir / doc.file, doc.page, rules)
    except CallFailed as failure:
        read.problem = f"{where} was not read: {failure}"
        return read
    if text is None:
        read.problem = f"{where}: the OCR reply had no markdown text"
        return read
    if len(text.strip()) < rules["thresholds"]["min_page_chars"]:
        read.problem = f"{where} could not be read (too little text)"
        return read
    if "SPECIMEN" in text.upper():
        if rules["specimen_policy"] == "review":
            read.problem = f"{where} is marked SPECIMEN"
        else:
            result.notes.append(f"{doc.document} is marked SPECIMEN (allowed for the demo)")
    extract_fields(client, calls, f"{case}/{doc.document}", doc.purpose, text, rules, read)
    if doc.purpose == "identity":
        read.mrz = parse_mrz(text)
    elif "issuer" in read.fields:
        kind, problem = issuer_type(read.fields["issuer"], flatten_markdown(text), rules)
        read.issuer_type, read.issuer_problem = kind or "", problem
    return read


def check_documents(
    reads: list[DocumentRead],
    checks: dict[str, Callable[[DocumentRead, Context], Verdict]],
    required: dict[str, str],
    ctx: Context,
    result: CaseResult,
) -> None:
    """Each requirement the policy lists for these documents, on each document. A document
    that couldn't be read leaves every one of its requirements not met, for the analyst."""
    for read in reads:
        for name in required:
            if read.problem:
                result.add(name, read.doc.document, NOT_MET, read.problem)
                continue
            status, detail = checks[name](read, ctx)
            ask = ctx.ask(name) if status != MET else ""
            result.add(name, read.doc.document, status, detail, ask)


def check_addresses_agree(reads: list[DocumentRead], ctx: Context, result: CaseResult) -> None:
    """With two or more address documents, they must give one address between them."""
    if len(reads) < 2:
        return
    parsed: dict[str, Address] = {}
    for read in reads:
        written = read.fields.get("address")
        address = parse_address(written, ctx.rules)[0] if written and not read.problem else None
        if address is None:
            detail = f"{read.doc.document}: no address to compare"
            result.add("address_documents_agree", "", NOT_MET, detail)
            return
        parsed[read.doc.document] = address
    if len(set(parsed.values())) > 1:
        shown = "; ".join(f"{name} '{reads[i].fields['address']}'" for i, name in enumerate(parsed))
        detail = f"conflicting address documents: {shown}"
        result.add(
            "address_documents_agree", "", NOT_MET, detail, ctx.ask("address_documents_agree")
        )
    else:
        result.add("address_documents_agree", "", MET, f"{len(parsed)} documents, one address")


def process_case(
    client: SIEClient,
    calls: Calls,
    application: Application,
    policy: dict[str, Any],
    rules: dict[str, Any],
    data_dir: Path,
) -> CaseResult:
    """The form and document-type checks first, then read each document and check it."""
    result = CaseResult(application.case, application.received)
    ctx = Context(application.form, application.received, policy, rules)
    check_form(application.form, ctx, result)
    identity, address = sort_documents(application.documents, ctx, result)
    identity_reads = [
        read_document(client, calls, doc, result.case, rules, data_dir, result) for doc in identity
    ]
    address_reads = [
        read_document(client, calls, doc, result.case, rules, data_dir, result) for doc in address
    ]
    for read in identity_reads + address_reads:
        result.documents[read.doc.document] = read
    check_documents(
        identity_reads, IDENTITY_CHECKS, policy["identity"]["requirements"], ctx, result
    )
    check_documents(address_reads, ADDRESS_CHECKS, policy["address"]["requirements"], ctx, result)
    check_addresses_agree(address_reads, ctx, result)
    return result


def run(
    client: SIEClient,
    policy: dict[str, Any],
    rules: dict[str, Any],
    data_dir: Path = DATA_DIR,
) -> tuple[list[CaseResult], Calls]:
    system = OnboardingSystem(data_dir)
    calls = Calls()
    results = []
    for case in system.open_cases():
        application = system.get_case(case)
        assert application is not None
        results.append(process_case(client, calls, application, policy, rules, data_dir))
    calls.check_all_failed()
    return results, calls


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


def requests_markdown(results: list[CaseResult]) -> str:
    """The plain list of what to ask each customer for, and what the analyst looks at first."""
    lines = ["# What to ask each customer for", ""]
    for result in results:
        lines.append(f"## {result.case}: {result.status}")
        lines.append("")
        if result.status == CHECKS_PASSED:
            lines += ["Every requirement met. Nothing to ask for.", ""]
            continue
        if result.ask_customer:
            lines.append("Please send us:")
            lines += [f"- {ask}" for ask in result.ask_customer]
            lines.append("")
        if result.for_analyst:
            lines.append("For the analyst to look at first:")
            lines += [
                f"- {req.requirement}"
                + (f" ({req.document})" if req.document else "")
                + f": {req.detail}"
                for req in result.for_analyst
            ]
            lines.append("")
    return "\n".join(lines)


def write_outputs(output_dir: Path, results: list[CaseResult]) -> None:
    (output_dir / "cases").mkdir(parents=True, exist_ok=True)
    for result in results:
        path = output_dir / "cases" / f"{result.case}.json"
        text = json.dumps(result.as_json(), indent=2, ensure_ascii=False) + "\n"
        path.write_text(text, encoding="utf-8")
    checklist = [
        {"case": result.case, **vars(req)} for result in results for req in result.requirements
    ]
    fields = ("case", "requirement", "document", "status", "detail", "ask")
    _write_csv(output_dir / "checklist.csv", fields, checklist)
    cases = [
        {
            "case": result.case,
            "status": result.status,
            "not_met": ";".join(r.requirement for r in result.unmet if r.status == NOT_MET),
            "missing": ";".join(r.requirement for r in result.unmet if r.status == MISSING),
            "ask_customer": "; ".join(result.ask_customer),
        }
        for result in results
    ]
    _write_csv(
        output_dir / "cases.csv", ("case", "status", "not_met", "missing", "ask_customer"), cases
    )
    (output_dir / "requests.md").write_text(requests_markdown(results), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    policy, rules = load_policy(), load_rules()
    started = time.monotonic()
    with make_client() as client:
        results, calls = stop_on_refusal(lambda: run(client, policy, rules))
    elapsed = time.monotonic() - started
    write_outputs(output, results)
    for result in results:
        reasons = "; ".join(
            f"{req.requirement} {req.status}" + (f" ({req.document})" if req.document else "")
            for req in result.unmet
        )
        print(f"{result.case:<7} {result.status:<14} {reasons or '-'}")
    per_model = ", ".join(f"{count} to {model}" for model, count in calls.count.items())
    print(f"\n{calls.total} SIE calls ({per_model}) in {elapsed:.0f} s")
    print("Statuses name what the checks found; the KYC analyst decides what happens next.")
    print(f"checklist: {output / 'checklist.csv'}\nrequests:  {output / 'requests.md'}")


if __name__ == "__main__":
    main()
