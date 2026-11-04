"""Review a counterparty's NDA against the legal team's playbook, with the redline wording ready.

Use case:  First pass on an NDA on the other side's paper: where does it depart from our
           playbook, what is our standard wording for each departure, and who has to approve
           accepting theirs instead?
Who:       In-house commercial lawyers and contracts managers who pick NDA requests up from
           the CLM intake queue.
Problem:   Every NDA should be held to the same positions (how long confidentiality lasts,
           governing law, no non-solicit, no residuals, our permitted disclosures), but each
           counterparty words them differently and puts them in different places, so a lawyer
           reads each one in full and looks the standard wording up in the playbook.
Input:     The CLM intake record for each request (clm.py over data/clm_intake.csv: parties,
           whose paper, our role, governing-law preference, the attached draft), the drafts as
           .docx files in data/, and the playbook in rules.yaml.
SIE pipeline (two models):
           extract  docling                    read each .docx as Markdown with its headings;
                                               called once per NDA
           extract  fastino/gliner2-large-v1   find the agreement term, confidentiality period,
                                               governing law, non-solicit, residuals and
                                               permitted disclosures; called once per window
                                               of about 512 tokens and schema group
           (Python checks the evidence and compares it with the playbook.)
Output:    decisions.csv, one row per NDA and position: matches_position | deviates |
           not_found | unclear, the value, the sections to read, the reason, and for a deviates
           row the playbook's standard wording and approver. reviews/<nda>.json per NDA.
           Statuses name what the checks found; the lawyer decides what to redline, what goes
           to the approver, and when the NDA can be signed.
Why SIE:   Two models, and no generative model makes a decision: a schema extractor returns
           fields, and plain Python reading rules.yaml compares them with the playbook.
Runs on:   SIE Serverless, or your own SIE with SIE_BASE_URL.

    uv run recipe.py
"""

from __future__ import annotations

import argparse
import csv
import functools
import json
import os
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import clm
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
# When GLiNER2 finds none of the fields (most NDA sections), SIE Serverless answers the
# output_schema call with HTTP 500 and this message. We record it as "no fields found". That is
# not proof the window holds nothing: a field the extractor missed reads the same way, and it
# can let a position pass (a governing law naming a second place, a carve-out with a condition).
# The checks that hold a pass on wording the extractor didn't read (an unread survival section,
# an unread longer duration, forbidden-clause wording with nothing extracted) are the guard.
EMPTY_RESULT_ERROR = "Inference completed without a valid conserved billing settlement"
# The other shape of "found nothing": a reply with no `data` at all, only these result lists,
# all empty. Any non-empty list, or a reply without `entities`, is a failed call.
EMPTY_RESULT_LISTS = ("entities", "relations", "classifications", "objects")

# Position statuses: what the checks found. The lawyer decides what happens next.
MATCHES, DEVIATES, NOT_FOUND, UNCLEAR = "matches_position", "deviates", "not_found", "unclear"
STATUSES = (MATCHES, DEVIATES, NOT_FOUND, UNCLEAR)
DECISION_FIELDS = (
    *("request", "nda", "counterparty", "position", "status", "value", "section", "reason"),
    *("standard_wording", "approver", "clause"),
)
W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@dataclass(frozen=True)
class Section:
    """One heading and the text under it, as docling wrote it."""

    number: int
    heading: str
    text: str


@dataclass(frozen=True)
class Candidate:
    """One value the extractor returned for one field in one window."""

    field: str
    position: str
    value: str
    section: Section
    window: str


@dataclass(frozen=True)
class Finding:
    position: str
    status: str
    reason: str
    value: str = ""
    sections: tuple[Section, ...] = ()
    standard_wording: str = ""
    approver: str = ""

    @property
    def section(self) -> str:
        return "; ".join(section.heading for section in self.sections)

    @property
    def clause(self) -> str:
        return "\n\n".join(section.text for section in self.sections)


@dataclass
class Review:
    intake: clm.IntakeRecord
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # position -> why some of its windows were not read (a transient SIE failure)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def nda(self) -> str:
        return self.intake.draft_file

    def summary(self) -> dict[str, int]:
        return {s: n for s in STATUSES if (n := sum(f.status == s for f in self.findings))}

    def as_json(self) -> dict[str, Any]:
        intake = self.intake
        return {
            "request": intake.request,
            "nda": self.nda,
            "counterparty": intake.counterparty,
            "our_entity": intake.our_entity,
            "paper": intake.paper,
            "our_role": intake.our_role,
            "summary": self.summary(),
            "positions": [
                {
                    "position": f.position,
                    "status": f.status,
                    "value": f.value,
                    "sections_to_read": [section.heading for section in f.sections],
                    "reason": f.reason,
                    "standard_wording": f.standard_wording,
                    "approver": f.approver,
                    "clause": f.clause,
                }
                for f in self.findings
            ],
            "notes": self.notes,
        }


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


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def field_positions(rules: dict[str, Any]) -> dict[str, str]:
    """Extract field -> the position it belongs to."""
    return {
        name: position for position, spec in rules["positions"].items() for name in spec["fields"]
    }


def output_schema(rules: dict[str, Any], group: list[str]) -> dict[str, Any]:
    """The GLiNER2 output_schema for one group of fields."""
    owner = field_positions(rules)
    properties: dict[str, Any] = {}
    for name in group:
        spec = rules["positions"][owner[name]]["fields"][name]
        prop: dict[str, Any] = {"type": spec["type"], "description": spec["description"]}
        if spec["type"] == "array":
            prop["items"] = {"type": "string"}
        properties[name] = prop
    return {"type": "object", "properties": properties}


# --- SIE call failures ------------------------------------------------------------------------


class SIEStopped(RuntimeError):
    """SIE refused the call for a reason a retry won't fix, or no call succeeded; the run
    stops without outputs."""


class CallFailed(Exception):
    """One SIE call failed for a reason that may pass; the positions it was for are unclear."""


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
        reply without `key`, is recorded and raised as CallFailed, so the caller marks the
        positions that call was for unclear and the run goes on. With `empty_result_ok` (the
        GLiNER2 calls), SIE Serverless's empty-result HTTP 500, and a reply whose only results are
        empty lists (see `is_empty_result`), are a reply with nothing in it, not a failure. An
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
        if empty_result_ok and is_empty_result(reply, key):
            return {key: {}, "empty_result": True}
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
        print(
            f"{n} of {total} SIE calls failed; their positions are unclear. First: {short(first)}"
        )


def is_empty_result(reply: Any, key: str) -> bool:
    """A reply without `key` whose result lists are all there is, and all empty."""
    return (
        isinstance(reply, dict)
        and key not in reply
        and "entities" in reply
        and all(reply.get(name, []) == [] for name in EMPTY_RESULT_LISTS)
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
    those positions are unclear."""
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
    """The error for a per-position reason or the summary line, its message cut short."""
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


# --- Read and split ---------------------------------------------------------------------------


def read_nda(calls: Calls, client: SIEClient, docx: Path, rules: dict[str, Any]) -> str:
    """Convert one .docx to Markdown with docling. Raises CallFailed on a transient failure."""
    model = rules["models"]["document"]
    item = Item(id=f"read/{docx.stem}", document={"data": docx.read_bytes(), "format": "docx"})
    reply = calls.call(
        model, lambda: client.extract(model, item, provision_timeout_s=WAIT_S), "data"
    )
    data = reply["data"]
    return str(data.get("markdown", "")) if isinstance(data, dict) else ""


def docx_headings(docx: Path) -> list[str] | None:
    """The heading paragraphs Word holds in the .docx, read with the standard library, so the
    converted text can be checked against them. None when the file can't be opened as a .docx.
    """
    try:
        with zipfile.ZipFile(docx) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        return None
    headings: list[str] = []
    for paragraph in root.iter(f"{W_NS}p"):
        style = paragraph.find(f"{W_NS}pPr/{W_NS}pStyle")
        if style is None or not style.get(f"{W_NS}val", "").startswith("Heading"):
            continue
        text = "".join(node.text or "" for node in paragraph.iter(f"{W_NS}t")).strip()
        if text:
            headings.append(text)
    return headings


_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")


def split_sections(markdown: str) -> list[Section]:
    """One section per Markdown heading; text before the first heading is the preamble."""
    sections: list[Section] = []
    heading = "Preamble"
    lines: list[str] = []

    def close() -> None:
        body = "\n".join(lines).strip()
        if body:
            sections.append(Section(len(sections) + 1, heading, f"{heading}\n\n{body}"))

    for line in markdown.splitlines():
        match = _HEADING.match(line)
        if match:
            close()
            heading, lines = match.group(1), []
        else:
            lines.append(line)
    close()
    return sections


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


def section_windows(section: Section, size: int, overlap: int) -> list[str]:
    """Windows of a section's body, each led by the heading so the extractor sees the topic."""
    body = section.text[len(section.heading) :].strip()
    return [f"{section.heading}\n\n{part}" for part in windows(body, size, overlap)]


def unreadable(
    markdown: str, sections: list[Section], headings: list[str] | None, rules: dict[str, Any]
) -> str:
    """Why the converted NDA can't be trusted to hold the whole draft, or "" when it can.

    Too little text, or a heading Word holds that the converted text lacks (a section the
    conversion dropped), means a missing clause could be missing from the conversion, not
    from the NDA.
    """
    if len(markdown.strip()) < rules["thresholds"]["min_document_chars"]:
        return "the NDA could not be read (too little text came back)"
    if headings is None:
        return "the .docx could not be opened to count its headings"
    read = {_squash(section.heading) for section in sections}
    lost = [heading for heading in headings if _squash(heading) not in read]
    if lost:
        return f"the converted NDA lacks {len(lost)} of its {len(headings)} headings: " + (
            "; ".join(lost)
        )
    return ""


def parties_missing(markdown: str, intake: clm.IntakeRecord) -> list[str]:
    """The intake record's party names that the NDA doesn't spell out exactly."""
    text = _squash(markdown)
    return [
        name
        for name in (intake.counterparty, intake.our_entity)
        if not re.search(r"(?<!\w)" + re.escape(_squash(name)) + r"(?!\w)", text)
    ]


# --- Extract ----------------------------------------------------------------------------------


def _values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def extract_candidates(
    calls: Calls, client: SIEClient, sections: list[Section], rules: dict[str, Any], review: Review
) -> list[Candidate]:
    """Run every schema group over every window; a failed call marks its positions unclear."""
    model, limits = rules["models"]["extract"], rules["thresholds"]
    owner = field_positions(rules)
    stem = Path(review.nda).stem
    candidates: list[Candidate] = []
    for section in sections:
        parts = section_windows(section, limits["window_words"], limits["window_overlap_words"])
        for index, window in enumerate(parts, start=1):
            for group in rules["extract_groups"]:
                schema = output_schema(rules, group)
                key = f"extract/{stem}/s{section.number}-w{index}/{'+'.join(group)}"
                item = Item(id=key, text=window)
                try:
                    reply = calls.call(
                        model,
                        functools.partial(
                            client.extract,
                            model,
                            item,
                            output_schema=schema,
                            # Windows are sized to fit; an over-long one fails its call
                            # instead of being cut short.
                            options={"overflow_policy": "error"},
                            provision_timeout_s=WAIT_S,
                        ),
                        "data",
                        empty_result_ok=True,
                    )
                except CallFailed as failed:
                    where = f"window {index} of '{section.heading}' was not read: {failed}"
                    for name in group:
                        review.failed.setdefault(owner[name], where)
                    continue
                data = reply["data"] if isinstance(reply["data"], dict) else {}
                for name, value in data.items():
                    if name not in group:
                        continue
                    for text in _values(value):
                        if text.strip():
                            candidates.append(
                                Candidate(name, owner[name], text.strip(), section, window)
                            )
        if len(parts) > 1:
            review.notes.append(f"Section '{section.heading}' was read in {len(parts)} windows")
    return candidates


# --- Compare to the playbook (plain Python) ---------------------------------------------------


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\\", "")).strip().casefold()


def mentions(text: str, terms: list[str]) -> bool:
    """True when any term starts a word in text ("solicit" matches "solicitation")."""
    return any(re.search(r"\b" + re.escape(term.casefold()), _squash(text)) for term in terms)


def says(text: str, words: list[str]) -> list[str]:
    """The words that appear in text as whole words ("not" doesn't match "notice")."""
    squashed = _squash(text)
    return [w for w in words if re.search(r"\b" + re.escape(w.casefold()) + r"\b", squashed)]


def sentences(text: str) -> list[str]:
    """Sentences of a clause, split after . ; ! or ? before a capital, quote or bracket."""
    flat = re.sub(r"\s+", " ", text.replace("\\", "")).strip()
    return [part for part in re.split(r"(?<=[.;!?])\s+(?=[A-Z\"'(])", flat) if part]


def sentence_with(window: str, value: str) -> str:
    """The sentence of the window that holds the value, or the window when none does."""
    target = _squash(value)
    return next((s for s in sentences(window) if target in _squash(s)), window)


def in_window(candidate: Candidate) -> bool:
    """The value is text the window holds, with at least one letter or digit."""
    value = _squash(candidate.value)
    return value in _squash(candidate.window) and bool(re.search(r"[^\W_]", value))


def is_evidence(candidate: Candidate, spec: dict[str, Any]) -> bool:
    """A value counts only if it is in its window and its section uses the position's wording."""
    return in_window(candidate) and mentions(candidate.section.text, spec["evidence_terms"])


def duration_years(value: str, rules: dict[str, Any]) -> float | None:
    """Years in a duration such as "five (5) years", "36 months", "3-year", "3years", "2.5
    years", "3 1/2 years", "three (3) years and six (6) months" or "3 years and 1 day"; inf for
    "perpetual"; None when the value holds no duration. Days and weeks count only after a year
    or month ("3 years and 1 day"): a bare "thirty (30) days" is a notice period, not a term,
    so it gives None. A value holding two durations ("two years, or five years for trade
    secrets") gives the longer one: it binds us longest."""
    text = _squash(value)
    if mentions(text, rules["positions"]["term"]["perpetual_terms"]):
        return float("inf")
    words = "|".join(re.escape(word) for word in rules["number_words"])
    # Digits in brackets replace the number words before them ("thirty-seven (37) months" is
    # 37 months), and a number word must start the number ("seven" in "thirty-seven" is not 7).
    text = re.sub(rf"(?:(?<![\w-])(?:{words}|[a-z]+-[a-z]+)\s*)?\(\s*(\d+)\s*\)", r" \1 ", text)
    amount = rf"\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?|{words}"
    pattern = rf"(?<![\w.,/-])({amount})(\s+and\s+a\s+half)?\s*-?\s*(year|month|week|day)s?\b"
    per_year = {"year": 1, "month": 12, "week": 52, "day": 365}
    spans: list[tuple[int, int, float, int]] = []  # (start, end, years, per_year of last unit)
    for match in re.finditer(pattern, text):
        number, half, unit = match.groups()
        count = _number(number, rules) + (0.5 if half else 0.0)
        years = count / per_year[unit]
        # "three years and six months", "3 years 1 month": a smaller unit right after a larger
        # one, bare or joined by and / plus, is part of the same duration.
        gap = text[spans[-1][1] : match.start()] if spans else ""
        if (
            spans
            and per_year[unit] > spans[-1][3]
            and re.fullmatch(r"\s*(?:,\s*)?(?:(?:and|plus|\+)\s*)?", gap)
        ):
            start, _, before, _ = spans.pop()
            spans.append((start, match.end(), before + years, per_year[unit]))
        elif unit in ("year", "month"):
            spans.append((match.start(), match.end(), years, per_year[unit]))
        # A bare day or week duration is a notice period ("on thirty (30) days' notice"), not
        # a term: it is read only as the tail of a year or month duration.
    return max(span[2] for span in spans) if spans else None


def _number(text: str, rules: dict[str, Any]) -> float:
    """A whole, decimal ("2.5"), fraction ("1/2") or mixed ("3 1/2") number, or a number word."""
    if text in rules["number_words"]:
        return float(rules["number_words"][text])
    whole, _, fraction = text.rpartition(" ") if "/" in text else ("", "", text)
    if "/" in fraction:
        top, bottom = fraction.split("/")
        return float(whole or 0) + (int(top) / int(bottom) if int(bottom) else float("inf"))
    return float(fraction)


def _unique_sections(candidates: list[Candidate]) -> tuple[Section, ...]:
    seen: dict[int, Section] = {}
    for candidate in candidates:
        seen.setdefault(candidate.section.number, candidate.section)
    return tuple(seen[number] for number in sorted(seen))


def worded(sections: list[Section], terms: list[str]) -> tuple[Section, ...]:
    """Sections that use any of the terms: where a lawyer should look."""
    return tuple(section for section in sections if mentions(section.text, terms))


def _years(value: float) -> str:
    return "no end date" if value == float("inf") else f"{value:g} years"


def deviates(
    name: str,
    reason: str,
    value: str,
    cited: tuple[Section, ...],
    rules: dict[str, Any],
    law: str,
) -> Finding:
    """A deviates row, carrying the playbook's standard wording and approver."""
    spec = rules["positions"][name]
    wording = " ".join(str(spec["standard_wording"]).split()).replace("{governing_law}", law)
    return Finding(name, DEVIATES, reason, value, cited, wording, str(spec["approver"]))


def unconfirmed(name: str, dropped: list[Candidate]) -> Finding:
    """The extractor marked something, but not in a section with the position's wording."""
    first = dropped[0]
    reason = (
        f"The extractor marked '{first.value}' in '{first.section.heading}', which doesn't "
        "use this position's wording; read it"
    )
    values = "; ".join(dict.fromkeys(c.value for c in dropped))
    return Finding(name, UNCLEAR, reason, values, _unique_sections(dropped))


def not_found(name: str, what: str, sections: list[Section], rules: dict[str, Any]) -> Finding:
    """No clause in the passages read: the lawyer confirms by reading the NDA."""
    cited = worded(sections, rules["positions"][name]["evidence_terms"])
    reason = f"No {what} found in the passages read; a person checks the NDA"
    if cited:
        reason += ", starting with the cited sections"
    return Finding(name, NOT_FOUND, reason, "", cited)


def check_term(
    found: list[Candidate],
    dropped: list[Candidate],
    sections: list[Section],
    rules: dict[str, Any],
    law: str,
) -> Finding:
    """Agreement term and confidentiality period, as two clocks.

    A confidentiality period that survives the agreement runs after it, so it adds to the
    agreement term. With no confidentiality period found, the obligations are taken to last
    the agreement term. A section that talks about surviving the agreement with no period read
    from it makes a pass unclear, and so does a sentence with the term wording that states a
    duration over the limit that no extracted value holds. Any value that is not a duration
    makes the position unclear, even when another value parses.
    """
    spec = rules["positions"]["term"]
    limit = float(spec["max_years"])
    dropped = [c for c in dropped if duration_years(c.value, rules) is not None]
    if not found:
        if dropped:
            return unconfirmed("term", dropped)
        return not_found("term", "agreement term or confidentiality period", sections, rules)
    unparsed = [c for c in found if duration_years(c.value, rules) is None]
    if unparsed:
        values = "; ".join(dict.fromkeys(c.value for c in unparsed))
        reason = f"Found a term that is not a duration: {values}"
        return Finding("term", UNCLEAR, reason, values, _unique_sections(found))
    agreement = [c for c in found if c.field == "agreement_term"]
    confidentiality = [c for c in found if c.field == "confidentiality_period"]
    agreement_years = max((duration_years(c.value, rules) or 0.0 for c in agreement), default=None)
    parts = []
    if agreement:
        parts.append("agreement term: " + "; ".join(dict.fromkeys(c.value for c in agreement)))
    if confidentiality:
        parts.append(
            "confidentiality: " + "; ".join(dict.fromkeys(c.value for c in confidentiality))
        )
    value = " | ".join(parts)
    cited = _unique_sections(found)

    totals: list[tuple[float, str]] = []  # (years confidentiality binds us, how)
    read_survival: set[int] = set()  # sections a survival period was read from
    for c in confidentiality:
        years = duration_years(c.value, rules) or 0.0
        if mentions(sentence_with(c.window, c.value), spec["survival_terms"]):
            if agreement_years is None:
                reason = (
                    f"Confidentiality survives {c.value} after the agreement ends, but no "
                    "agreement term was found to add it to"
                )
                return Finding("term", UNCLEAR, reason, value, cited)
            read_survival.add(c.section.number)
            total = agreement_years + years
            how = f"{c.value} after an agreement term of {_years(agreement_years)}"
            totals.append((total, f"{_years(total)} ({how})"))
        else:
            totals.append((years, c.value))
    if not confidentiality:
        assert agreement_years is not None
        totals.append((agreement_years, f"{_years(agreement_years)} (the agreement term)"))
    binding, how = max(totals, key=lambda pair: pair[0])
    if binding > limit:
        reason = f"Confidentiality lasts {how}, longer than {_years(limit)}"
        return deviates("term", reason, value, cited, rules, law)
    if agreement_years is not None and agreement_years > limit:
        reason = f"The agreement term of {_years(agreement_years)} is longer than {_years(limit)}"
        return deviates("term", reason, value, cited, rules, law)
    # A section that says the obligations survive, with no period read from it, could make
    # confidentiality last longer than anything read: not a pass.
    unread = tuple(
        s for s in worded(sections, spec["survival_terms"]) if s.number not in read_survival
    )
    if unread:
        reason = (
            f"Confidentiality lasts {how} on what was read, but '{unread[0].heading}' says "
            "obligations survive the agreement and no period was read from it; read it"
        )
        return Finding("term", UNCLEAR, reason, value, cited + unread)
    if longer := unread_duration(sections, found, spec, rules):
        section, sentence = longer
        reason = (
            f"Confidentiality lasts {how} on what was read, but '{section.heading}' says "
            f'"{sentence}", longer than {_years(limit)}, and no value was read from it; read it'
        )
        cited = cited + ((section,) if section not in cited else ())
        return Finding("term", UNCLEAR, reason, value, cited)
    reason = f"Confidentiality lasts {how}, within {_years(limit)}"
    return Finding("term", MATCHES, reason, value, cited)


def unread_duration(
    sections: list[Section], found: list[Candidate], spec: dict[str, Any], rules: dict[str, Any]
) -> tuple[Section, str] | None:
    """The first sentence using the term wording that states a duration over the limit which no
    extracted value covers ("in confidence for five (5) years from each disclosure", when only
    the agreement term was read), with its section; None when there is none."""
    limit = float(spec["max_years"])
    for section in worded(sections, spec["evidence_terms"]):
        for sentence in sentences(section.text[len(section.heading) :]):
            years = duration_years(sentence, rules)
            if years is None or years <= limit or not mentions(sentence, spec["evidence_terms"]):
                continue
            read = [
                duration_years(c.value, rules) or 0.0
                for c in found
                if _squash(c.value) in _squash(sentence)
            ]
            if max(read, default=0.0) < years:
                return section, sentence
    return None


def place_words(text: str) -> list[str]:
    """The words of a governing-law value, lower case, without punctuation."""
    return re.findall(r"[a-z]+", _squash(text))


def jurisdiction(value: str, allowed: dict[str, list[str]]) -> str | None:
    """The allowed jurisdiction one of whose phrases the value names as whole words."""
    words = " ".join(place_words(value))
    for name, phrases in allowed.items():
        if any(re.search(rf"\b{re.escape(p.casefold())}\b", words) for p in phrases):
            return name
    return None


def extra_place_words(value: str, allowed: dict[str, list[str]], filler: list[str]) -> list[str]:
    """Words of the value that are neither an allowed phrase nor filler ("new" in "New England",
    "county pennsylvania" in "Delaware County, Pennsylvania")."""
    words = " ".join(place_words(value))
    for phrase in sorted((p.casefold() for ps in allowed.values() for p in ps), key=len)[::-1]:
        words = re.sub(rf"\b{re.escape(phrase)}\b", " ", words)
    return [w for w in words.split() if w not in filler]


def check_governing_law(
    found: list[Candidate],
    dropped: list[Candidate],
    sections: list[Section],
    rules: dict[str, Any],
    law: str,
) -> Finding:
    spec = rules["positions"]["governing_law"]
    allowed: dict[str, list[str]] = spec["allowed"]
    if law not in allowed:
        reason = (
            f"The CLM intake asks for governing law '{law}', which is not on the playbook's "
            f"allowed list ({', '.join(allowed)}); the lawyer picks the law for the redline"
        )
        return Finding("governing_law", UNCLEAR, reason, "", _unique_sections(found))
    if not found:
        if dropped:
            return unconfirmed("governing_law", dropped)
        return not_found("governing_law", "governing-law clause", sections, rules)
    # One value naming two places ("New York or Singapore") can't be settled by its first.
    two = [
        c
        for c in found
        if re.search(r"\bor\b", _squash(c.value))
        or sum(jurisdiction(c.value, {k: v}) is not None for k, v in allowed.items()) > 1
    ]
    if two:
        reason = f"Governing law '{two[0].value}' names more than one place; read it"
        return Finding("governing_law", UNCLEAR, reason, two[0].value, _unique_sections(found))
    # An allowed name inside a longer place name ("New England", "Delaware County,
    # Pennsylvania") is not that jurisdiction.
    filler: list[str] = spec["filler_words"]
    longer = [
        c
        for c in found
        if jurisdiction(c.value, allowed) and extra_place_words(c.value, allowed, filler)
    ]
    if longer:
        first = longer[0]
        extra = " ".join(extra_place_words(first.value, allowed, filler))
        reason = (
            f"Governing law '{first.value}' names {jurisdiction(first.value, allowed)} inside a "
            f"longer name ('{extra}'); read it"
        )
        return Finding("governing_law", UNCLEAR, reason, first.value, _unique_sections(found))
    outside = [c for c in found if jurisdiction(c.value, allowed) is None]
    if outside:
        first = outside[0]
        reason = f"Governing law '{first.value}' is not on the allowed list ({', '.join(allowed)})"
        return deviates("governing_law", reason, first.value, (first.section,), rules, law)
    named = sorted({cast(str, jurisdiction(c.value, allowed)) for c in found})
    values = "; ".join(dict.fromkeys(c.value for c in found))
    reason = f"Governing law {' and '.join(named)} is on the allowed list"
    if law not in named:
        reason += f" (the request asked for {law})"
    return Finding("governing_law", MATCHES, reason, values, _unique_sections(found))


def check_forbidden(
    name: str,
    found: list[Candidate],
    dropped: list[Candidate],
    sections: list[Section],
    rules: dict[str, Any],
    law: str,
) -> Finding:
    """A clause we never accept. Its absence is never a pass: at best it is not_found."""
    spec = rules["positions"][name]
    label = spec["label"]
    if found:
        values = "; ".join(dict.fromkeys(c.value for c in found))
        reason = f"The NDA has this clause; the playbook says: {label}"
        return deviates(name, reason, values, _unique_sections(found), rules, law)
    if dropped:
        return unconfirmed(name, dropped)
    if cited := worded(sections, spec["evidence_terms"]):
        reason = f"The wording is there but the extractor found no clause ({label}); read it"
        return Finding(name, UNCLEAR, reason, "", cited)
    return not_found(name, f"{label.casefold().removeprefix('no ')}", sections, rules)


def permission_sentences(section: Section, spec: dict[str, Any]) -> list[str]:
    """Sentences that permit a disclosure: they say disclose and may/permitted/..."""
    body = section.text[len(section.heading) :]
    return [
        s
        for s in sentences(body)
        if mentions(s, spec["evidence_terms"]) and says(s, spec["permission_terms"])
    ]


def check_permitted_disclosures(
    found: list[Candidate],
    dropped: list[Candidate],
    sections: list[Section],
    rules: dict[str, Any],
    law: str,
) -> Finding:
    """Every required carve-out must be granted by a permission sentence in a marked section.

    A carve-out in a sentence that also says not, without or consent is unclear. A carve-out
    the marked sections don't grant is unclear when another section's permission sentence
    might grant it (the extractor may have missed that section), and deviates when none does.
    """
    name = "permitted_disclosures"
    spec = rules["positions"][name]
    required: dict[str, list[str]] = spec["required"]
    if not found:
        return not_found(name, "permitted-disclosures clause", sections, rules)
    cited = _unique_sections(found)
    values = "; ".join(dict.fromkeys(c.value for c in found))
    granted = {s: permission_sentences(s, spec) for s in sections}
    negated: list[str] = []
    flagged: set[str] = set()  # the negation words those sentences use
    missing: list[str] = []
    for carve, words in required.items():
        grants = [s for section in cited for s in granted[section] if mentions(s, words)]
        negations = {w for s in grants for w in says(s, spec["negation_terms"])}
        if not grants:
            missing.append(carve)
        elif negations:
            negated.append(carve)
            flagged |= negations
    if negated:
        reason = (
            f"The carve-out for {', '.join(negated)} sits in a sentence that says "
            f"{', '.join(sorted(flagged))}; read it"
        )
        return Finding(name, UNCLEAR, reason, values, cited)
    if not missing:
        reason = f"Permitted disclosures cover: {', '.join(required)}"
        return Finding(name, MATCHES, reason, values, cited)
    elsewhere = tuple(
        section
        for section in sections
        if section not in cited
        and any(mentions(s, required[c]) for s in granted[section] for c in missing)
    )
    if elsewhere:
        reason = (
            f"The marked clause lacks {', '.join(missing)}, but another section may grant it; "
            "read them"
        )
        return Finding(name, UNCLEAR, reason, values, cited + elsewhere)
    reason = f"Permitted disclosures lack: {', '.join(missing)}"
    return deviates(name, reason, values, cited, rules, law)


def everything(review: Review, rules: dict[str, Any], status: str, reason: str) -> Review:
    """Give every position that applies the same status and reason."""
    review.findings = [Finding(name, status, reason) for name in applicable(review, rules)]
    return review


def applicable(review: Review, rules: dict[str, Any]) -> list[str]:
    """The positions the playbook checks for our role on this NDA (from the CLM intake)."""
    names = []
    for name, spec in rules["positions"].items():
        roles = spec.get("applies_when_we")
        if roles is None or review.intake.our_role in roles:
            names.append(name)
    return names


def decide(
    sections: list[Section], candidates: list[Candidate], rules: dict[str, Any], review: Review
) -> Review:
    positions: dict[str, Any] = rules["positions"]
    law = review.intake.governing_law_preference or str(rules["default_governing_law"])
    found: dict[str, list[Candidate]] = {name: [] for name in positions}
    dropped: dict[str, list[Candidate]] = {name: [] for name in positions}
    for candidate in candidates:
        if candidate.position not in positions:
            continue
        if is_evidence(candidate, positions[candidate.position]):
            found[candidate.position].append(candidate)
        else:
            dropped[candidate.position].append(candidate)
            review.notes.append(
                f"Not evidence: {candidate.field} '{candidate.value}' from "
                f"'{candidate.section.heading}' (not in its window, or the section doesn't use "
                "the position's wording)"
            )
    for name in positions:
        if name not in applicable(review, rules):
            review.notes.append(
                f"Not checked: {positions[name]['label']}. We only {review.intake.our_role} "
                "under this NDA (CLM intake), so the position doesn't bind us"
            )
            continue
        args = (found[name], dropped[name], sections, rules, law)
        if name == "term":
            finding = check_term(*args)
        elif name == "governing_law":
            finding = check_governing_law(*args)
        elif name == "permitted_disclosures":
            finding = check_permitted_disclosures(*args)
        elif positions[name].get("forbidden"):
            finding = check_forbidden(name, *args)
        else:
            raise ValueError(f"rules.yaml position {name!r} has no check")
        if name in review.failed:
            finding = Finding(
                name,
                UNCLEAR,
                f"{review.failed[name]}; from the rest: {finding.reason}",
                finding.value,
                finding.sections,
            )
        review.findings.append(finding)
    return review


# --- Run --------------------------------------------------------------------------------------


def review_request(
    calls: Calls, client: SIEClient, intake: clm.IntakeRecord, rules: dict[str, Any]
) -> Review:
    review = Review(intake)
    if Path(intake.draft_file).name != intake.draft_file or intake.draft_file in ("", ".", ".."):
        reason = f"The draft file name '{intake.draft_file}' is not a plain file name in data/"
        return everything(review, rules, UNCLEAR, reason)
    docx = DATA_DIR / intake.draft_file
    if not docx.is_file():
        return everything(review, rules, UNCLEAR, "The draft attached to the request is missing")
    try:
        markdown = read_nda(calls, client, docx, rules)
    except CallFailed as failed:
        return everything(review, rules, UNCLEAR, f"The NDA was not read: {failed}")
    sections = split_sections(markdown)
    if why := unreadable(markdown, sections, docx_headings(docx), rules):
        return everything(review, rules, UNCLEAR, why[0].upper() + why[1:])
    if missing := parties_missing(markdown, intake):
        reason = (
            f"The NDA doesn't name {' or '.join(missing)} as the CLM intake record does; "
            "check it is the right draft for this request"
        )
        return everything(review, rules, UNCLEAR, reason)
    candidates = extract_candidates(calls, client, sections, rules, review)
    return decide(sections, candidates, rules, review)


def run(client: SIEClient, rules: dict[str, Any], system: clm.CLM) -> tuple[list[Review], Calls]:
    calls = Calls()
    reviews = []
    for request in system.open_requests():
        intake = system.get_request(request)
        assert intake is not None, request
        reviews.append(review_request(calls, client, intake, rules))
    calls.check_all_failed()
    return reviews, calls


def csv_safe(value: Any) -> str:
    """A leading ' on a cell a spreadsheet would run as a formula; anything else as written."""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def decision_rows(reviews: list[Review]) -> list[dict[str, str]]:
    return [
        {
            "request": review.intake.request,
            "nda": review.nda,
            "counterparty": review.intake.counterparty,
            "position": f.position,
            "status": f.status,
            "value": f.value,
            "section": f.section,
            "reason": f.reason,
            "standard_wording": f.standard_wording,
            "approver": f.approver,
            "clause": f.clause,
        }
        for review in reviews
        for f in review.findings
    ]


def write_outputs(output_dir: Path, reviews: list[Review]) -> None:
    (output_dir / "reviews").mkdir(parents=True, exist_ok=True)
    for review in reviews:
        path = output_dir / "reviews" / f"{Path(review.nda).stem}.json"
        text = json.dumps(review.as_json(), indent=2, ensure_ascii=False) + "\n"
        path.write_text(text, encoding="utf-8")
    with (output_dir / "decisions.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=DECISION_FIELDS)
        writer.writeheader()
        for row in decision_rows(reviews):
            writer.writerow({key: csv_safe(value) for key, value in row.items()})


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    started = time.monotonic()
    with make_client() as client:
        reviews, calls = stop_on_refusal(lambda: run(client, load_rules(), clm.CLM()))
    elapsed = time.monotonic() - started
    write_outputs(output, reviews)
    for review in reviews:
        print(f"{review.intake.request}  {review.nda}")
        for f in review.findings:
            print(f"    {f.position:<22} {f.status:<17} {f.reason}")
    per_model = ", ".join(f"{count} to {model}" for model, count in calls.count.items())
    print(f"\n{calls.total} SIE calls ({per_model}) in {elapsed:.0f} s")
    print("Statuses name what the checks found; the lawyer decides what to redline and sign.")
    print(f"decisions: {output / 'decisions.csv'}\nper NDA:   {output / 'reviews'}")


if __name__ == "__main__":
    main()
