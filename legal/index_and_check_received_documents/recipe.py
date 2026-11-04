"""Index a discovery volume received from the other side, check its Bates numbers and queue
possible privilege.

Use case:  Intake of a discovery volume that arrived as stamped PDFs and natives with no usable
           load file: build an index the review platform can load, check the Bates numbering
           against the cover letter, and spot the other side's privileged documents sent by
           mistake.
Who:       The litigation paralegal or litigation-support specialist at the receiving firm,
           with the supervising lawyer for anything that may be privileged.
Problem:   Today someone opens every PDF and goes page by page: writes down each Bates number,
           decides where each document starts and ends, types date, author, recipients and
           custodian into a spreadsheet, notes gaps and duplicates for the deficiency letter,
           and watches for a letter from the other side's counsel.
Input:     The volume's files in data/<volume>/ (scanned PDFs, PDFs with a text layer, native
           .docx files), the matter system's production log and counsel list (matter.py over
           data/matter_*.csv), and the team's rules in rules.yaml.
SIE pipeline:
           extract  lightonai/LightOnOCR-3-4B   read each scanned page as Markdown; once per
                                                scanned page
           extract  docling                     read each text-layer page and native file; once
                                                per page or file
           extract  fastino/gliner2-large-v1    date, author, recipients, document type; once
                                                per window of about 512 tokens
           extract  fastino/GLiNER2.5-Decide    "potentially privileged" or "not privileged";
                                                once per document, one item per window per page
Output:    index.csv (one load-file-style row per document with its status and reasons),
           privilege-review.csv (the lawyer's queue), bates-check.json (the Bates findings for
           the deficiency letter) and documents.json (the text each model read).
           Statuses name what the checks found: possible_privilege, qc_exception, no_exception.
           The lawyer decides about privilege; litigation support fixes the index or raises a
           deficiency.
Why SIE:   Four models, and no generative model makes a decision. Bates numbering, document
           breaks, custodians, dates, the lawyer list and every status rule are plain Python.
Run using: SIE Serverless, SIE Cluster or SIE Local (with SIE_BASE_URL).

    uv run recipe.py
"""

from __future__ import annotations

import argparse
import csv
import functools
import io
import itertools
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

import pypdfium2 as pdfium
import yaml
from dotenv import load_dotenv
from matter import Lawyer, MatterSystem, Volume
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
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
# A failed SIE call's message is cut to this many characters in reasons and the summary line.
ERROR_CHARS = 200
# A cell starting with one of these runs as a formula when a spreadsheet opens the CSV.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")

# Statuses name what the checks found. The lawyer decides whether a possible_privilege document
# goes back to the producing party; litigation support fixes the index or raises a deficiency
# for a qc_exception. no_exception is a checked index row, not a reviewed document.
POSSIBLE_PRIVILEGE, QC_EXCEPTION, NO_EXCEPTION = (
    "possible_privilege",
    "qc_exception",
    "no_exception",
)
PRIVILEGE_QUESTION_ID = "privilege"
INDEX_FIELDS = (
    *("BEGBATES", "ENDBATES", "PGCOUNT", "CUSTODIAN", "DOCDATE", "AUTHOR", "RECIPIENTS"),
    *("DOCTYPE", "SOURCEFILE", "SOURCEPAGES", "PRIVSCORE", "PRIVLIST", "STATUS", "REASONS"),
)
REVIEW_FIELDS = (
    *("BEGBATES", "ENDBATES", "SOURCEFILE", "SOURCEPAGES", "CUSTODIAN", "DOCDATE"),
    *("AUTHOR", "PRIVSCORE", "PRIVLIST", "REASONS"),
)


@dataclass
class Page:
    """One received page (or one native file) as read."""

    source: str
    number: int  # page number inside its source file; 1 for a native file
    text: str
    reader: str  # the model that read it
    stamps: list[str] = field(default_factory=list)
    native: bool = False
    error: str | None = None  # why the page was not read, when it wasn't

    @property
    def location(self) -> str:
        return self.source if self.native else f"{self.source} p{self.number}"


@dataclass
class Document:
    pages: list[Page]
    fields: dict[str, Any] = field(default_factory=dict)
    privilege_windows: list[dict[str, Any]] = field(default_factory=list)
    privilege_error: str | None = None
    privilege_score: float | None = None  # the strongest window's privileged probability
    privilege_list_hits: list[str] = field(default_factory=list)
    privilege_reasons: list[str] = field(default_factory=list)
    qc_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def source(self) -> str:
        return self.pages[0].source

    @property
    def key(self) -> str:
        """A stable id from the source file and first page, used in SIE item ids."""
        return f"{Path(self.source).stem}-p{self.pages[0].number}"

    @property
    def text(self) -> str:
        return "\n\n".join(page.text for page in self.pages)

    @property
    def begbates(self) -> str:
        return self.pages[0].stamps[0] if len(self.pages[0].stamps) == 1 else ""

    @property
    def endbates(self) -> str:
        return self.pages[-1].stamps[0] if len(self.pages[-1].stamps) == 1 else ""

    @property
    def status(self) -> str:
        if self.privilege_reasons:
            return POSSIBLE_PRIVILEGE
        if self.qc_reasons:
            return QC_EXCEPTION
        return NO_EXCEPTION

    def source_pages(self) -> str:
        if self.pages[0].native:
            return "native"
        first, last = self.pages[0].number, self.pages[-1].number
        return str(first) if first == last else f"{first}-{last}"


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


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


# --- SIE call failures ------------------------------------------------------------------------


class SIEStopped(RuntimeError):
    """SIE refused the call for a reason a retry won't fix, or no call succeeded; the run
    stops without outputs."""


class CallFailed(Exception):
    """One SIE call failed for a reason that may pass; the document it was for goes to review."""


@dataclass
class Calls:
    """Every SIE call the run made, and the transient failures among them."""

    count: dict[str, int] = field(default_factory=dict)  # model -> calls
    failures: list[tuple[str, SIEError]] = field(default_factory=list)  # (model, error)

    @property
    def total(self) -> int:
        return sum(self.count.values())

    def call(self, model: str, work: Callable[[], Any], key: str) -> dict[str, Any]:
        """Run one SIE call and return its reply, which must carry `key`.

        A refusal (see `is_fatal`) stops the run with SIEStopped. Any other SIE error, SIE
        Serverless's empty-result HTTP 500 included, and a reply without `key`, is recorded and
        raised as CallFailed, so the caller sends its document to review and the run goes on.
        An error that isn't an SIE error is a bug in this code and is raised as is.
        """
        self.count[model] = self.count.get(model, 0) + 1
        try:
            reply = jsonable(work())
        except SIEError as error:
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
        print(
            f"{n} of {total} SIE calls failed; their documents go to review. First: {short(first)}"
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
    those documents go to review."""
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


def stop_on_refusal[T](work: Callable[[], T]) -> T:
    """Run the work; turn SIEStopped into a clean exit. Any other error keeps its traceback."""
    try:
        return work()
    except SIEStopped as error:
        raise SystemExit(f"Stopped: {error}") from None


# --- Read pages -------------------------------------------------------------------------------


def _page_png(page: pdfium.PdfPage, dpi: int, max_edge: int) -> bytes:
    image = page.render(scale=dpi / 72).to_pil().convert("RGB")
    image.thumbnail((max_edge, max_edge))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _single_page_pdf(document: pdfium.PdfDocument, index: int) -> bytes:
    single = pdfium.PdfDocument.new()
    single.import_pages(document, [index])
    buffer = io.BytesIO()
    single.save(buffer)
    return buffer.getvalue()


def _markdown_entity(response: dict[str, Any]) -> str | None:
    for entity in response.get("entities") or []:
        if entity.get("label") == "markdown":
            return str(entity.get("text", ""))
    return None


def _docling_text(response: dict[str, Any]) -> str:
    """Docling's plain text plus the page headers and footers it sets aside as furniture.

    The Bates stamp and the custodian legend sit in the footer, which docling leaves out of its
    text and Markdown exports; they are read back from the DoclingDocument it returns.
    """
    data = response.get("data") or {}
    texts = (data.get("document") or {}).get("texts") or []
    furniture = {
        label: [str(t.get("text", "")) for t in texts if t.get("label") == label]
        for label in ("page_header", "page_footer")
    }
    parts = [*furniture["page_header"], str(data.get("text", "")), *furniture["page_footer"]]
    return "\n\n".join(part for part in parts if part.strip())


def read_pdf(client: SIEClient, calls: Calls, pdf: Path, rules: dict[str, Any]) -> list[Page]:
    """Text per page: docling for pages with a text layer, LightOnOCR for scans.

    A page whose call failed comes back with no text and the reason in `error`.
    """
    models, limits = rules["models"], rules["thresholds"]
    document = pdfium.PdfDocument(pdf)
    pages: list[Page] = []
    for index in range(len(document)):
        page = document[index]
        layer = page.get_textpage().get_text_range()
        key = f"read/{pdf.stem}/p{index + 1}"
        if len(layer.strip()) >= limits["min_text_layer_chars"]:
            model = models["document"]
            item = Item(
                id=key, document={"data": _single_page_pdf(document, index), "format": "pdf"}
            )
            try:
                reply = calls.call(
                    model,
                    functools.partial(client.extract, model, item, provision_timeout_s=WAIT_S),
                    "data",
                )
                pages.append(Page(pdf.name, index + 1, _docling_text(reply), model))
            except CallFailed as failed:
                pages.append(Page(pdf.name, index + 1, "", model, error=str(failed)))
        else:
            model = models["ocr"]
            image = _page_png(page, limits["render_dpi"], limits["max_image_edge_px"])
            ocr_item = Item(id=key, images=[image])
            try:
                reply = calls.call(
                    model,
                    functools.partial(client.extract, model, ocr_item, provision_timeout_s=WAIT_S),
                    "entities",
                )
            except CallFailed as failed:
                pages.append(Page(pdf.name, index + 1, "", model, error=str(failed)))
                continue
            text = _markdown_entity(reply)
            error = None if text is not None else "the OCR reply had no markdown text"
            pages.append(Page(pdf.name, index + 1, text or "", model, error=error))
    return pages


def read_native(client: SIEClient, calls: Calls, path: Path, rules: dict[str, Any]) -> Page:
    model = rules["models"]["document"]
    item = Item(
        id=f"read/{path.name}", document={"data": path.read_bytes(), "format": path.suffix[1:]}
    )
    try:
        reply = calls.call(
            model, lambda: client.extract(model, item, provision_timeout_s=WAIT_S), "data"
        )
    except CallFailed as failed:
        return Page(path.name, 1, "", model, native=True, error=str(failed))
    return Page(path.name, 1, _docling_text(reply), model, native=True)


# --- Bates stamps and document breaks (plain Python) -----------------------------------------


def bates_pattern(volume: Volume, rules: dict[str, Any]) -> re.Pattern[str]:
    """Regex for the volume's stamp: OCR lookalikes in the number, a space or dash after prefix."""
    lookalikes = "".join(re.escape(char) for char in rules["bates"]["ocr_digit_lookalikes"])
    prefix = re.escape(volume.bates_prefix)
    digits = int(volume.bates_digits)
    return re.compile(rf"(?<![A-Za-z0-9]){prefix}[\s-]?([0-9{lookalikes}]{{{digits}}})(?![0-9])")


def find_stamps(text: str, volume: Volume, rules: dict[str, Any]) -> list[str]:
    """Every distinct Bates number in the page's footer, normalized to PREFIX + digits.

    The footer is the run of lines at the end of the page that hold nothing but stamps, the
    footer legend, a "Page N of M" and punctuation (Markdown bold, a rule). A Bates number on a
    line with other words ("see TFL0000004 for the memo") is a reference to another page, not
    this page's stamp, and is left out.
    """
    table = str.maketrans(rules["bates"]["ocr_digit_lookalikes"])
    pattern = bates_pattern(volume, rules)
    legend = re.compile(rules["footer_legend_pattern"], re.I)
    numbering = re.compile(rules["document_breaks"]["page_numbering"], re.I)
    found: list[str] = []
    for line in reversed(text.splitlines()):
        stamps = [volume.bates_prefix + m.group(1).translate(table) for m in pattern.finditer(line)]
        rest = numbering.sub(" ", legend.sub(" ", pattern.sub(" ", line)))
        if re.search(r"\w", rest):
            break
        found[:0] = stamps
    return list(dict.fromkeys(found))


def bates_number(stamp: str, volume: Volume) -> int:
    return int(stamp[len(volume.bates_prefix) :])


def bates_label(number: int, volume: Volume) -> str:
    return f"{volume.bates_prefix}{number:0{int(volume.bates_digits)}d}"


def page_numbering(text: str, rules: dict[str, Any]) -> tuple[int, int] | None:
    """The page's "Page N of M" as (N, M), from the last one on the page, or None."""
    matches = re.findall(rules["document_breaks"]["page_numbering"], text, re.I)
    if not matches:
        return None
    n, m = matches[-1]
    return int(n), int(m)


def split_documents(pages: list[Page], rules: dict[str, Any]) -> list[Document]:
    """Group pages into documents, and note every boundary the recipe had to guess.

    A page says "Page N of M" with N above 1 and the page before it in the same file said
    "Page N-1 of M": it continues that document. A page that says "Page 1 of M", begins with
    an email header, or is a native file starts one. Anything else starts a document with a
    boundary note (a QC exception), and so does a document that stops before "Page M of M".
    """
    email_header = re.compile(rules["document_breaks"]["email_header"], re.I | re.S)
    documents: list[Document] = []
    previous: tuple[int, int] | None = None
    for page in pages:
        numbering = None if page.native else page_numbering(page.text, rules)
        before = documents[-1] if documents else None
        if numbering and numbering[0] > 1:
            n, m = numbering
            follows = (
                before is not None
                and before.source == page.source
                and not before.pages[-1].native
                and previous == (n - 1, m)
            )
            if follows and before is not None:
                before.pages.append(page)
                previous = numbering
                continue
            documents.append(Document([page]))
            documents[-1].qc_reasons.append(
                f"Boundary: {page.location} says Page {n} of {m}, but the page before it is not "
                f"Page {n - 1} of {m}; a person confirms where this document starts"
            )
        else:
            documents.append(Document([page]))
            starts = (
                page.native
                or page.error is not None  # an unread page is already an exception
                or (numbering is not None and numbering[0] == 1)
                or email_header.search(page.text) is not None
            )
            if not starts:
                documents[-1].qc_reasons.append(
                    f"Boundary: {page.location} has no page numbering and no email header, so "
                    "it may continue the page before; a person confirms where this document "
                    "starts and ends"
                )
        previous = numbering
    for document in documents:
        last = None if document.pages[-1].native else page_numbering(document.pages[-1].text, rules)
        if last and last[0] < last[1]:
            document.qc_reasons.append(
                f"Boundary: the document stops at Page {last[0]} of {last[1]} "
                f"({document.pages[-1].location}); pages may be missing"
            )
    return documents


def check_bates(documents: list[Document], volume: Volume, rules: dict[str, Any]) -> dict[str, Any]:
    """Check the stamps against the cover letter: one per page, no duplicates or gaps, inside
    the claimed range, consecutive within a document.

    Each problem is added to the QC reasons of the documents it touches. The report lists every
    finding in plain words, which is the list for the deficiency letter.
    """
    limits = rules["thresholds"]
    first, last = volume.bates_first, volume.bates_last
    seen: dict[int, list[tuple[Document, Page]]] = {}
    missing_stamp: list[str] = []
    not_read: list[str] = []
    findings: list[str] = []
    for document in documents:
        for page in document.pages:
            if page.error is not None:
                not_read.append(page.location)
                document.qc_reasons.append(f"{page.location} was not read: {page.error}")
                continue
            if len(page.text.strip()) < limits["min_page_chars"]:
                document.qc_reasons.append(f"{page.location} could not be read")
            if not page.stamps:
                missing_stamp.append(page.location)
                document.qc_reasons.append(f"No Bates stamp read on {page.location}")
                findings.append(f"{page.location}: no Bates stamp read")
                continue
            if len(page.stamps) > 1:
                document.qc_reasons.append(
                    f"More than one Bates stamp on {page.location}: {', '.join(page.stamps)}"
                )
                findings.append(
                    f"{page.location}: more than one Bates stamp ({', '.join(page.stamps)})"
                )
            seen.setdefault(bates_number(page.stamps[0], volume), []).append((document, page))
        numbers = [bates_number(page.stamps[0], volume) for page in document.pages if page.stamps]
        if len(numbers) == len(document.pages) and numbers != list(
            range(numbers[0], numbers[0] + len(numbers))
        ):
            stamps = ", ".join(page.stamps[0] for page in document.pages)
            document.qc_reasons.append(f"Pages are not stamped consecutively: {stamps}")
            findings.append(f"One document's pages are not stamped consecutively: {stamps}")

    duplicates: dict[str, list[str]] = {}
    for number, holders in sorted(seen.items()):
        if len(holders) < 2:
            continue
        stamp = bates_label(number, volume)
        duplicates[stamp] = [page.location for _, page in holders]
        findings.append(
            f"{stamp} is stamped on {len(holders)} pages: {', '.join(duplicates[stamp])}"
        )
        for document, page in holders:
            others = ", ".join(other.location for _, other in holders if other is not page)
            document.qc_reasons.append(f"Bates {stamp} is also stamped on {others}")

    claimed = f"{bates_label(first, volume)}-{bates_label(last, volume)}"
    outside = sorted(bates_label(number, volume) for number in seen if not first <= number <= last)
    for number, holders in seen.items():
        if not first <= number <= last:
            for document, _ in holders:
                document.qc_reasons.append(
                    f"Bates {bates_label(number, volume)} is outside the claimed range {claimed}"
                )
    findings += [
        f"{stamp} is outside the range the cover letter claims ({claimed})" for stamp in outside
    ]

    gaps: list[str] = []
    missing_numbers = [n for n in range(first, last + 1) if n not in seen]
    for _, run in itertools.groupby(enumerate(missing_numbers), key=lambda pair: pair[1] - pair[0]):
        block = [number for _, number in run]
        label = bates_label(block[0], volume)
        if len(block) > 1:
            label += f"-{bates_label(block[-1], volume)}"
        gaps.append(label)
        finding = f"{label} is inside the claimed range {claimed} but was not found"
        if not_read:
            finding += f" ({len(not_read)} pages were not read; read them before raising this)"
        findings.append(finding)
        for document, page in seen.get(block[-1] + 1, []):
            if page is document.pages[0]:
                document.qc_reasons.append(f"Bates gap before this document: {label} not found")
    return {
        "volume": volume.volume,
        "producing_party": volume.producing_party,
        "claimed_range": [bates_label(first, volume), bates_label(last, volume)],
        "pages_claimed": last - first + 1,
        "pages_stamped": sum(len(holders) for holders in seen.values()),
        "first_found": bates_label(min(seen), volume) if seen else None,
        "last_found": bates_label(max(seen), volume) if seen else None,
        "gaps": gaps,
        "duplicates": duplicates,
        "outside_claimed_range": outside,
        "pages_without_stamp": missing_stamp,
        "pages_not_read": not_read,
        "continuous": not (gaps or duplicates or outside or missing_stamp or not_read),
        "findings": findings,
    }


# --- Fields: gliner2-large-v1 ---------------------------------------------------------------------


def word_windows(text: str, size: int, overlap: int = 0) -> list[str]:
    """Split text into windows of at most `size` words, keeping line breaks.

    Each window repeats the last `overlap` words of the one before, so a field or sentence that
    straddles a boundary is still read whole in one of them.
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


def field_schema(rules: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for name, spec in rules["fields"].items():
        if spec.get("kind") == "list":
            prop: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
        else:
            prop = {"type": "string"}
        prop["description"] = spec["description"]
        if "enum" in spec:
            prop["enum"] = list(spec["enum"])
        properties[name] = prop
    return {"type": "object", "properties": properties}


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def in_text(value: str, text: str) -> bool:
    """Keep a model's value only if it has a letter or digit and appears in the text."""
    return bool(re.search(r"[^\W_]", value)) and _squash(value) in _squash(text)


def extract_fields(
    client: SIEClient, calls: Calls, document: Document, rules: dict[str, Any]
) -> dict[str, list[tuple[int, str]]]:
    """Read the fields with one gliner2 call per window; return every kept value per field.

    A value not found in its window is dropped and noted. Recipients are collected from every
    window into document.fields. A window whose call failed is a QC exception. The single-value
    fields come back as (window, value) pairs for `finish_fields` to settle.
    """
    model, limits = rules["models"]["fields"], rules["thresholds"]
    schema = field_schema(rules)
    windows = word_windows(
        document.text, limits["field_window_words"], limits["field_window_overlap_words"]
    )
    found: dict[str, list[tuple[int, str]]] = {}
    for index, window in enumerate(windows, start=1):
        item = Item(id=f"fields/{document.key}/w{index}", text=window)
        try:
            reply = calls.call(
                model,
                functools.partial(
                    client.extract,
                    model,
                    item,
                    output_schema=schema,
                    options={"overflow_policy": "error"},
                    provision_timeout_s=WAIT_S,
                ),
                "data",
            )
        except CallFailed as failed:
            document.qc_reasons.append(
                f"Field window {index} of {len(windows)} was not read: {failed}"
            )
            continue
        data = reply["data"] if isinstance(reply["data"], dict) else {}
        for name, raw in data.items():
            spec = rules["fields"].get(name)
            if spec is None:
                continue
            if spec.get("kind") == "list":
                kept = document.fields.setdefault(name, [])
                for value in raw if isinstance(raw, list) else []:
                    value = str(value).strip().strip(",;")
                    if not in_text(value, window):
                        if value:
                            document.notes.append(f"Dropped {name} '{value}': not in the text")
                    elif _squash(value) not in map(_squash, kept):
                        kept.append(value)
                continue
            if not isinstance(raw, str) or not raw.strip():
                continue
            value = raw.strip().strip(",;")
            if "enum" in spec:
                if value in spec["enum"]:
                    found.setdefault(name, []).append((index, value))
                else:
                    document.notes.append(f"Dropped {name} '{value}': not one of the choices")
            elif in_text(value, window):
                found.setdefault(name, []).append((index, value))
            else:
                document.notes.append(f"Dropped {name} '{value}': not in the text")
    return found


def dates_in(value: str, formats: list[str]) -> list[date]:
    """Every date written in `value`, in order, using the longest run of words that parses.

    "Wednesday, March 12, 2025 4:41 PM" holds one date; "March 12, 2025 and March 18, 2025"
    holds two.
    """
    words = re.sub(r",", ", ", value).split()
    found: list[date] = []
    start = 0
    while start < len(words):
        for length in range(min(4, len(words) - start), 0, -1):
            candidate = re.sub(r"\s+,", ",", " ".join(words[start : start + length])).strip(" .,;")
            parsed = _parse(candidate, formats)
            if parsed is not None:
                found.append(parsed)
                start += length
                break
        else:
            start += 1
    return found


def _parse(candidate: str, formats: list[str]) -> date | None:
    for fmt in formats:
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    return None


# --- Privilege: GLiNER2.5-Decide plus the other side's lawyers --------------------------------


def privilege_schema(rules: dict[str, Any]) -> dict[str, Any]:
    return {
        PRIVILEGE_QUESTION_ID: {
            "type": "choice",
            "instructions": rules["privilege_question"],
            "criteria": dict(rules["privilege_options"]),
        }
    }


def strip_footer_legend(text: str, volume: Volume, rules: dict[str, Any]) -> str:
    """The document as written: no custodian legend and no Bates stamps."""
    legend = re.compile(rules["footer_legend_pattern"], re.I | re.M)
    without = bates_pattern(volume, rules).sub("", legend.sub("", text))
    return re.sub(r"\n{3,}", "\n\n", without).strip()


def window_score(result: dict[str, Any], rules: dict[str, Any]) -> float:
    """The privileged probability in one window's result. Raises ValueError when the result has
    an error, no answer, or a probability that isn't a number between 0 and 1."""
    if error := result.get("error"):
        if isinstance(error, dict):
            error = f"{error.get('code', 'ERROR')}: {error.get('message', '')}".strip()
        raise ValueError(str(error))
    answer = (result.get("data") or {}).get(PRIVILEGE_QUESTION_ID)
    probabilities = answer.get("probabilities") if isinstance(answer, dict) else None
    label = rules["privileged_label"]
    if not isinstance(probabilities, dict) or label not in probabilities:
        raise ValueError(f"the reply had no probability for '{label}'")
    try:
        score = float(probabilities[label])
    except (TypeError, ValueError):
        raise ValueError(f"the probability for '{label}' is not a number") from None
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise ValueError(f"the probability for '{label}' is {score}, not between 0 and 1")
    return score


def score_privilege(
    client: SIEClient, calls: Calls, document: Document, volume: Volume, rules: dict[str, Any]
) -> None:
    """Score every window of every page with one GLiNER2.5-Decide call for the document.

    Windows are cut per page, so a page is scored on its own text. The document's score is its
    strongest window, taken as is: one window that reads privileged is enough. A window, or a
    page, that got no score leaves the document without one.
    """
    model, limits = rules["models"]["privilege"], rules["thresholds"]
    windows: list[tuple[Page, str]] = []
    unread = [page.location for page in document.pages if page.error is not None]
    for page in document.pages:
        if page.error is None:
            text = strip_footer_legend(page.text, volume, rules)
            for window in word_windows(
                text, limits["privilege_window_words"], limits["privilege_window_overlap_words"]
            ):
                windows.append((page, window))
    if unread:
        document.privilege_error = f"{', '.join(unread)} not read"
    if not windows:
        document.privilege_error = document.privilege_error or "no text to score"
        return
    items = [
        Item(id=f"privilege/{document.key}/w{n}", text=window)
        for n, (_, window) in enumerate(windows, start=1)
    ]
    try:
        reply = calls.call(
            model,
            lambda: {
                "results": client.extract(
                    model,
                    items,
                    output_schema=privilege_schema(rules),
                    options={"overflow_policy": "error"},
                    provision_timeout_s=WAIT_S,
                )
            },
            "results",
        )
    except CallFailed as failed:
        document.privilege_error = str(failed)
        return
    results = reply["results"] if isinstance(reply["results"], list) else []
    errors: list[str] = []
    for n, (page, _) in enumerate(windows, start=1):
        entry: dict[str, Any] = {"page": page.location, "window": n, "score": None}
        try:
            entry["score"] = round(window_score(results[n - 1], rules), 4)
        except IndexError:
            entry["error"] = "no result for this window"
        except (ValueError, AttributeError) as error:
            entry["error"] = str(error) or type(error).__name__
        if "error" in entry:
            errors.append(f"window {n} ({page.location}): {entry['error']}")
        document.privilege_windows.append(entry)
    scores = [entry["score"] for entry in document.privilege_windows if entry["score"] is not None]
    document.privilege_score = max(scores) if scores else None
    if errors:
        document.privilege_error = "; ".join(
            [*([document.privilege_error] if document.privilege_error else []), *errors]
        )


def lawyer_hits(text: str, lawyers: list[Lawyer]) -> list[Lawyer]:
    """The other side's lawyers named in the text, by name or alias, as whole words."""
    hits: list[Lawyer] = []
    for lawyer in lawyers:
        for name in [lawyer.name, *lawyer.aliases]:
            words = r"\s+".join(re.escape(word) for word in name.split())
            if re.search(rf"(?<![\w.@-]){words}(?![\w@-])", text, re.I):
                hits.append(lawyer)
                break
    return hits


def decide_privilege(document: Document, lawyers: list[Lawyer], rules: dict[str, Any]) -> None:
    """Queue the document for the lawyer if it names one of the other side's lawyers or the
    score says so.

    "The score says so" covers the strongest window at or above `privileged_at`, one in the
    unsure band, and no score at all: a failed call or an unread page is treated as unsure,
    never as "not privileged".
    """
    limits = rules["thresholds"]
    hits = lawyer_hits(document.text, lawyers)
    document.privilege_list_hits = [lawyer.name for lawyer in hits]
    for lawyer in hits:
        document.privilege_reasons.append(
            f"Names {lawyer.name} ({lawyer.role}), the other side's lawyer"
        )
    if document.privilege_error or document.privilege_score is None:
        reason = document.privilege_error or "no answer"
        document.privilege_reasons.append(f"No privilege score for every window ({reason})")
        return
    score = document.privilege_score
    if score >= limits["privileged_at"]:
        document.privilege_reasons.append(f"Scored potentially privileged ({score:.3f})")
    elif score >= limits["unsure_from"]:
        document.privilege_reasons.append(f"Privilege score unsure ({score:.3f})")


# --- Index -------------------------------------------------------------------------------------


def read_custodians(document: Document, volume: Volume, rules: dict[str, Any]) -> list[str]:
    """The custodians the footer legend names on the document's pages, in page order."""
    legend = re.compile(rules["footer_legend_pattern"], re.I | re.M)
    stamp = bates_pattern(volume, rules)
    found: list[str] = []
    for page in document.pages:
        for match in legend.finditer(page.text):
            # OCR may wrap the legend in Markdown emphasis ("Custodian:** Tom Okoye").
            name = re.sub(r"[*_]+", " ", stamp.sub("", match.group("custodian")))
            name = re.sub(r"\s+", " ", name).strip(" .,;|")
            if name and _squash(name) not in map(_squash, found):
                found.append(name)
    return found


def several_people(value: str) -> bool:
    """True when one author value names more than one person ("A and B", "A; B", "A & B")."""
    return len([part for part in re.split(r"\s+(?:and|&)\s+|;", value) if part.strip()]) > 1


def finish_fields(
    document: Document,
    found: dict[str, list[tuple[int, str]]],
    volume: Volume,
    matter: MatterSystem,
    rules: dict[str, Any],
) -> None:
    """Settle the extracted fields, add the custodian and flag what is missing or conflicting.

    Bates stamps are stripped from values. Two windows giving different values for one field,
    a date value holding two different dates, or an author naming two people is a QC exception
    and leaves the field blank. Dates become YYYY-MM-DD. The custodian comes from the footer
    legend, or for a native file from the cover letter. A required field that is still blank
    is a QC exception.
    """
    stamp = bates_pattern(volume, rules)
    formats = rules["date_formats"]
    unsettled: set[str] = set()
    for name, pairs in found.items():
        values = [(window, stamp.sub("", value).strip()) for window, value in pairs]
        values = [(window, value) for window, value in values if value]
        if name == "date":
            parsed: list[tuple[int, str, list[date]]] = [
                (window, value, dates_in(value, formats)) for window, value in values
            ]
            for _, value, dates in parsed:
                if not dates:
                    document.qc_reasons.append(f"Date '{value}' could not be read as a date")
                elif len(set(dates)) > 1:
                    document.qc_reasons.append(f"Date '{value}' holds more than one date")
                    unsettled.add(name)
            distinct = {d for _, _, dates in parsed for d in dates}
            if len(distinct) == 1 and name not in unsettled:
                document.fields[name] = distinct.pop().isoformat()
            elif len(distinct) > 1 and name not in unsettled:
                listed = ", ".join(f"'{value}' (window {window})" for window, value, _ in parsed)
                document.qc_reasons.append(f"Windows disagree on the date: {listed}")
                unsettled.add(name)
            continue
        distinct_values = {_squash(value) for _, value in values}
        if len(distinct_values) > 1:
            listed = ", ".join(f"'{value}' (window {window})" for window, value in values)
            document.qc_reasons.append(
                f"Windows disagree on the {name.replace('_', ' ')}: {listed}"
            )
            unsettled.add(name)
        elif values:
            value = values[0][1]
            if name == "author" and several_people(value):
                document.qc_reasons.append(f"Author '{value}' names more than one person")
                unsettled.add(name)
            else:
                document.fields[name] = value
    if "recipients" in document.fields:
        cleaned = [stamp.sub("", item).strip() for item in document.fields["recipients"]]
        document.fields["recipients"] = [item for item in cleaned if item]
    if document.pages[0].native:
        native = matter.find_native(volume.volume, document.source)
        if native is None:
            document.qc_reasons.append(f"{document.source} is not listed in the cover letter")
        else:
            document.fields["custodian"] = native.custodian
            document.notes.append("Custodian from the cover letter (native file)")
    else:
        custodians = read_custodians(document, volume, rules)
        if len(custodians) > 1:
            document.qc_reasons.append(f"Pages name different custodians: {', '.join(custodians)}")
            unsettled.add("custodian")
        elif custodians:
            document.fields["custodian"] = custodians[0]
    for name in rules["required_fields"]:
        if not document.fields.get(name) and name not in unsettled:
            document.qc_reasons.append(f"No {name.replace('_', ' ')} found")


def index_row(document: Document) -> dict[str, Any]:
    score = document.privilege_score
    return {
        "BEGBATES": document.begbates,
        "ENDBATES": document.endbates,
        "PGCOUNT": len(document.pages),
        "CUSTODIAN": document.fields.get("custodian", ""),
        "DOCDATE": document.fields.get("date", ""),
        "AUTHOR": document.fields.get("author", ""),
        "RECIPIENTS": "; ".join(document.fields.get("recipients", [])),
        "DOCTYPE": document.fields.get("doc_type", ""),
        "SOURCEFILE": document.source,
        "SOURCEPAGES": document.source_pages(),
        "PRIVSCORE": "" if score is None else f"{score:.3f}",
        "PRIVLIST": "; ".join(document.privilege_list_hits),
        "STATUS": document.status,
        "REASONS": " | ".join([*document.privilege_reasons, *document.qc_reasons]),
    }


@dataclass
class Result:
    documents: list[Document]
    bates: dict[str, Any]
    calls: Calls

    def rows(self) -> list[dict[str, Any]]:
        return [index_row(document) for document in self.documents]


def _sort_key(document: Document) -> tuple[int, str, int]:
    stamp = document.begbates
    digits = re.sub(r"\D", "", stamp)
    return (int(digits) if digits else 10**12, document.source, document.pages[0].number)


def input_files(folder: Path, rules: dict[str, Any]) -> tuple[list[Path], list[Path]]:
    """The volume's PDFs and native files, and any other file the recipe can't read."""
    natives = set(rules["bates"]["native_extensions"])
    files: list[Path] = []
    skipped: list[Path] = []
    for path in sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")):
        (
            files if path.suffix.lower() == ".pdf" or path.suffix.lower() in natives else skipped
        ).append(path)
    return files, skipped


def run(
    client: SIEClient,
    rules: dict[str, Any],
    volume: Volume,
    matter: MatterSystem,
    lawyers: list[Lawyer],
    files: list[Path],
) -> Result:
    """Read the volume, split it into documents, check the stamps and score every document."""
    calls = Calls()
    pages: list[Page] = []
    for path in files:
        if path.suffix.lower() == ".pdf":
            pages.extend(read_pdf(client, calls, path, rules))
        else:
            native = read_native(client, calls, path, rules)
            native.stamps = find_stamps(path.stem, volume, rules)
            pages.append(native)
    for page in pages:
        if not page.native:
            page.stamps = find_stamps(page.text, volume, rules)
    documents = split_documents(pages, rules)
    bates = check_bates(documents, volume, rules)
    for document in documents:
        found = extract_fields(client, calls, document, rules)
        finish_fields(document, found, volume, matter, rules)
        score_privilege(client, calls, document, volume, rules)
        decide_privilege(document, lawyers, rules)
    calls.check_all_failed()
    documents.sort(key=_sort_key)
    return Result(documents, bates, calls)


# --- Outputs -----------------------------------------------------------------------------------


def csv_safe(value: Any) -> str:
    """A leading ' on a cell a spreadsheet would run as a formula; anything else as written."""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_safe(value) for key, value in row.items()})


def write_outputs(output_dir: Path, result: Result) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = result.rows()
    _write_csv(output_dir / "index.csv", INDEX_FIELDS, rows)
    review = [row for row in rows if row["STATUS"] == POSSIBLE_PRIVILEGE]
    _write_csv(output_dir / "privilege-review.csv", REVIEW_FIELDS, review)
    (output_dir / "bates-check.json").write_text(
        json.dumps(result.bates, indent=2) + "\n", encoding="utf-8"
    )
    documents = [
        {
            "begbates": document.begbates,
            "endbates": document.endbates,
            "source": document.source,
            "pages": [
                {"page": page.number, "reader": page.reader, "stamps": page.stamps}
                | ({"error": page.error} if page.error else {})
                for page in document.pages
            ],
            "fields": document.fields,
            "privilege_score": document.privilege_score,
            "privilege_windows": document.privilege_windows,
            "status": document.status,
            "privilege_reasons": document.privilege_reasons,
            "qc_reasons": document.qc_reasons,
            "notes": document.notes,
            "text": document.text,
        }
        for document in result.documents
    ]
    (output_dir / "documents.json").write_text(
        json.dumps(documents, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def volume_folder(data_dir: Path, name: str | None) -> Path:
    """The volume's folder in data/: the one named, or the only one there."""
    if name:
        return data_dir / name
    folders = sorted(path for path in data_dir.iterdir() if path.is_dir())
    if len(folders) != 1:
        found = ", ".join(path.name for path in folders) or "none"
        raise SystemExit(f"Name the volume with --volume (folders in {data_dir}: {found})")
    return folders[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--volume", help="the volume's folder name in data/ (default: the only one)"
    )
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    folder = volume_folder(DATA_DIR, args.volume)
    matter = MatterSystem()
    try:
        volume = matter.find_volume(folder.name)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    if volume is None:
        raise SystemExit(
            f"Volume {folder.name} is not in the matter system's production log "
            "(data/matter_productions.csv). Log it from its cover letter, then run again."
        )
    lawyers = matter.counsel_for(volume.matter_id, volume.producing_party)
    if not lawyers:
        raise SystemExit(
            f"The matter system lists no lawyers for {volume.producing_party} on matter "
            f"{volume.matter_id}, so the privilege check has nothing to look for. Add them to "
            "data/matter_counsel.csv, then run again."
        )
    rules = load_rules()
    files, skipped = input_files(folder, rules)
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    started = time.monotonic()
    with make_client() as client:
        result = stop_on_refusal(lambda: run(client, rules, volume, matter, lawyers, files))
    elapsed = time.monotonic() - started
    result.bates["files_not_read"] = [path.name for path in skipped]
    write_outputs(output, result)
    for row in result.rows():
        bates = row["BEGBATES"] or "?"
        if row["ENDBATES"] and row["ENDBATES"] != row["BEGBATES"]:
            bates += f"-{row['ENDBATES'][-4:]}"
        print(f"{bates:<16} {row['DOCTYPE'] or '?':<17} {row['STATUS']:<19} {row['REASONS']}")
    print("\nBates findings for the deficiency letter:")
    for finding in result.bates["findings"] or ["none"]:
        print(f"  {finding}")
    for path in skipped:
        print(f"Not read (not a PDF or a native type in rules.yaml): {path.name}")
    calls = result.calls
    per_model = ", ".join(f"{count} to {model}" for model, count in calls.count.items())
    print(f"\n{calls.total} SIE calls ({per_model}) in {elapsed:.0f} s")
    print("Statuses name what the checks found; the lawyer decides about privilege.")
    print(f"index: {output / 'index.csv'}\nprivilege queue: {output / 'privilege-review.csv'}")


if __name__ == "__main__":
    main()
