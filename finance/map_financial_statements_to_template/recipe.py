"""Map a borrower's statements into the bank's spreading template and check the totals.

Use case:  Spreading a private borrower's annual statements for credit analysis.
Who:       Commercial credit analysts at a bank, who put every borrower's statements into the
           bank's standard template so the credit officer can compare borrowers and work out
           covenant ratios.
Problem:   The statements arrive as PDFs (sometimes a scan), every borrower labels its lines its
           own way ("Turnover", "Creditors: amounts falling due within one year", "Owner's
           equity"), and a line in the wrong row changes the ratios the credit decision rests on.
Input:     data/statements/<borrower id>.pdf (as uploaded through the lending portal), the loan
           system's borrower records and last year's confirmed label mappings (loan_system.py
           over data/loan_*.csv), and the template, tie-outs and thresholds in rules.yaml.
SIE pipeline:
           extract  docling                 read each statement PDF as Markdown tables; once per
                                            PDF ("docling:ocr" for a scan with no text layer)
           (Python)                         parse labels, headings, totals, units, currency and
                                            period; place labels an analyst confirmed last year
           score    Qwen/Qwen3-Reranker-4B  rank the template rows for each new label; once per
                                            new label
           (Python)                         accept above the score and margin thresholds and in
                                            the right balance-sheet group, absorb detail lines
                                            only into a total they add up to, fill, derive, tie
                                            out
Output:    spreads.csv (one row per borrower: checks_passed or review), spread-<borrower>.csv
           (the draft spread, with units and period on every row), exceptions.csv, checks.csv
           and lines.json (every borrower line with its top rows and the decision).
Why SIE:   Two models, and no generative model makes a decision. The reranker reads labels, never
           amounts; Python parses the amounts docling extracted, does every sum and catches what
           doesn't add up. The analyst confirms the draft spread; the credit officer uses only an
           approved one.
Runs on:   SIE Serverless, or your own SIE with SIE_BASE_URL.

    uv run recipe.py
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

import pypdfium2 as pdfium
import yaml
from dotenv import load_dotenv
from loan_system import Borrower, LoanSystem, label_key
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
STATEMENTS_DIR = DATA_DIR / "statements"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
# A failed SIE call's message is cut to this many characters in reasons and the summary line.
ERROR_CHARS = 200
# A cell starting with one of these runs as a formula when a spreadsheet opens the CSV.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
STATEMENTS = ("income_statement", "balance_sheet")

# Spread status: what the checks found. The analyst confirms the spread; the credit officer
# uses only an approved one.
CHECKS_PASSED, REVIEW = "checks_passed", "review"
# A line's decision. ACCEPTED ones put their amount in the spread.
KNOWN, MAPPED = "known", "mapped"
ACCEPTED = (KNOWN, MAPPED)
ABSORBED, SUBTOTAL, NOT_PLACED = "absorbed", "subtotal", "not_placed"
UNMAPPED, AMBIGUOUS, WRONG_GROUP = "unmapped", "ambiguous", "wrong_group"
UNREAD, FAILED = "unread_amount", "sie_call_failed"
ESCALATED = (UNMAPPED, AMBIGUOUS, WRONG_GROUP, UNREAD, FAILED)

SUMMARY_FIELDS = (
    *("borrower_id", "name", "status", "exceptions", "read_with"),
    *("income_statement_period", "balance_sheet_period", "units", "period_due"),
)
SPREAD_FIELDS = (
    *("statement", "item_id", "item", "value", "units", "period", "status"),
    *("borrower_lines", "source", "score", "margin", "review"),
)
EXCEPTION_FIELDS = ("borrower_id", "kind", "statement", "item_id", "line", "value", "reason")
CHECK_FIELDS = (
    *("borrower_id", "statement", "check", "status"),
    *("reported", "computed", "difference", "detail"),
)
TEMPLATE_KEYS = {"id", "name", "description"}


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    rules = cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))
    groups = set(rules["section_groups"])
    for kind in STATEMENTS:
        for item in rules["template"][kind]:
            # An unquoted comma in a YAML flow mapping silently splits a name into extra keys.
            if not TEMPLATE_KEYS <= set(item) <= TEMPLATE_KEYS | {"group"}:
                raise ValueError(f"Template row {item} must have exactly {sorted(TEMPLATE_KEYS)}")
            if "group" in item and item["group"] not in groups:
                raise ValueError(f"Template row {item['id']} names an unknown group")
    return rules


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
    """One SIE call failed for a reason that may pass; its statement or line goes to review."""


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

        A refusal (see `is_fatal`) stops the run with SIEStopped. Any other SIE error, and a
        reply without `key`, is recorded and raised as CallFailed, so the caller sends its
        statement or line to review and the run goes on. An error that isn't an SIE error is a
        bug in this code and is raised as is.
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
        print(f"{n} of {total} SIE calls failed; their lines go to review. First: {short(first)}")


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
    those statements and lines go to review."""
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
    """The error for a per-line reason or the summary line, its message cut short."""
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


# --- 1. Read the statements (docling) ---------------------------------------------------------


def has_text_layer(pdf: Path, min_chars: int) -> bool:
    """True when every page carries at least `min_chars` characters of text; a scan has none."""
    document = pdfium.PdfDocument(pdf)
    try:
        pages = [document[i].get_textpage().get_text_range() for i in range(len(document))]
    finally:
        document.close()
    return bool(pages) and all(len(text.strip()) >= min_chars for text in pages)


def read_markdown(
    client: SIEClient, calls: Calls, pdf: Path, rules: dict[str, Any]
) -> tuple[str, str]:
    """One docling call per PDF: the statements as Markdown, and the model that read them."""
    scanned = not has_text_layer(pdf, int(rules["thresholds"]["min_text_layer_chars"]))
    model = rules["models"]["document_ocr" if scanned else "document"]
    item = Item(id=pdf.stem, document={"data": pdf.read_bytes(), "format": "pdf"})
    reply = calls.call(
        model, lambda: client.extract(model, item, provision_timeout_s=WAIT_S), "data"
    )
    data = reply["data"]
    if not isinstance(data, dict) or "markdown" not in data:
        missing = SIEError(f"{model} replied without 'data.markdown'")
        calls.failures.append((model, missing))
        raise CallFailed(f"the SIE call failed: {short(missing)}")
    return str(data["markdown"] or ""), model


# --- 2. Parse the tables (Python) -------------------------------------------------------------

_NIL = {"-", "\u2014", "\u2013", "nil"}  # hyphen, em dash, en dash, "nil": a nil amount
_MINUS = ("-", "\u2212")  # hyphen-minus and the minus sign
_CURRENCY_SIGNS = "$£€"


def parse_number(cell: str) -> Decimal | None:
    """'$ 35,934' -> 35934, '(14,264)' -> -14264, '-96,500' -> -96500, '-' -> 0.

    Anything else (a footnote mark, two numbers in one cell, a letter) is None: the caller
    treats it as an amount it couldn't read, never as a heading.
    """
    if re.search(r"\d\s+\d", cell):
        return None  # two numbers in one cell, never one glued number
    text = re.sub(r"\s", "", cell).strip(_CURRENCY_SIGNS)
    if text.casefold() in _NIL:
        return Decimal(0)
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1].strip(_CURRENCY_SIGNS)
    elif text.startswith(_MINUS):
        negative, text = True, text[1:].strip(_CURRENCY_SIGNS)
    if not re.fullmatch(r"(\d{1,3}(,\d{3})+|\d+)(\.\d+)?", text):
        return None
    text = text.replace(",", "")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return -value if negative else value


@dataclass
class Line:
    """One borrower line that carries an amount, with the heading it sits under."""

    index: int
    label: str
    context: str  # the section heading the line sits under, "" for none
    value: Decimal | None  # None: the amount couldn't be read
    raw: str  # the amount cell as docling read it
    section: int | None = None  # the open section this line belongs to
    closes: int | None = None  # the section this total closes
    heading: str = ""  # the heading of the section a total closes
    unlabelled: bool = False  # an amount printed with no label (a UK-style subtotal)

    @property
    def query(self) -> str:
        return f"{self.label} (listed under {self.context})" if self.context else self.label

    @property
    def name(self) -> str:
        return f"{self.context} > {self.label}" if self.context else self.label


@dataclass
class Statement:
    kind: str
    caption: str = ""
    headers: list[str] = field(default_factory=list)  # the amount column headers
    cells: str = ""  # every amount cell, for the currency check
    sections: int = 0  # section headings read so far
    lines: list[Line] = field(default_factory=list)


def _cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _statement_kind(text: str, rules: dict[str, Any]) -> str | None:
    folded = text.casefold()
    for kind in STATEMENTS:
        if any(phrase in folded for phrase in rules["statements"][kind]):
            return kind
    return None


def parse_statements(markdown: str, rules: dict[str, Any]) -> dict[str, Statement]:
    """Split docling's Markdown into statements and their lines (first amount column)."""
    statements: dict[str, Statement] = {}
    current: Statement | None = None
    table: list[list[str]] = []

    def flush() -> None:
        if current is not None and table:
            add_table(current, table, rules["parsing"])
        table.clear()

    for raw in markdown.splitlines():
        text = raw.strip()
        if text.startswith("|"):
            cells = _cells(text)
            if not all(re.fullmatch(r":?-+:?", cell) for cell in cells if cell):
                table.append(cells)
            continue
        flush()
        if not text:
            continue
        kind = _statement_kind(text, rules)
        if kind is not None:
            current = statements.setdefault(kind, Statement(kind))
            current.caption += text.lstrip("#").strip() + "\n"
        elif current is not None and not current.lines:
            current.caption += text.lstrip("#").strip() + "\n"
    flush()
    return statements


def _drop_sign_columns(rows: list[list[str]]) -> list[list[str]]:
    """Drop a column whose body holds nothing but currency signs: a "$" printed apart from its
    amount. Its header text, if any, moves to the next column when that one has none."""
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    body = padded[1:]
    drop = {
        column
        for column in range(1, width)
        if not any(row[column] and row[column] not in _CURRENCY_SIGNS for row in body)
        and any(row[column] for row in body)
    }
    header = padded[0]
    for column in sorted(drop):
        if column + 1 < width and not header[column + 1]:
            header[column + 1], header[column] = header[column], ""
    return [[cell for column, cell in enumerate(row) if column not in drop] for row in padded]


def _starts(label: str, prefixes: list[str]) -> bool:
    key = label_key(label)
    return any(key.startswith(label_key(prefix)) for prefix in prefixes)


def add_table(statement: Statement, rows: list[list[str]], parsing: dict[str, Any]) -> None:
    """Read one table: headings, lines and totals, keeping the first amount column.

    A row with a label and no amount in any column is a heading. A heading that arrives while
    the open one already has lines replaces it (a sibling section); one that arrives before any
    line nests inside it ("ASSETS" > "Current assets"). A total ("Total ...", "Shareholders'
    funds"), or an amount with no label, closes the open section. A summary line ("Net assets")
    is read without a heading and closes nothing. A row with an amount Python can't read is
    kept as a line with no value, so it's raised, never lost.
    """
    rows = _drop_sign_columns(rows)
    header_rows = [rows[0]]
    body = rows[1:]
    while body and not body[0][0]:
        header_rows.append(body.pop(0))
    if not statement.headers:
        statement.headers = [
            " ".join(row[c] for row in header_rows if len(row) > c and row[c]).strip()
            for c in range(1, max(len(row) for row in header_rows))
        ]
    stack: list[tuple[int, str, bool]] = []  # (section id, heading, has lines)
    for cells in body:
        label = cells[0].strip()
        amounts = [cell for cell in cells[1:] if cell.strip()]
        if not amounts:
            if not label:
                continue
            if stack and stack[-1][2]:
                stack.pop()
            statement.sections += 1
            stack.append((statement.sections, label.rstrip(":").strip(), False))
            continue
        raw = cells[1].strip() if len(cells) > 1 else ""
        statement.cells += " ".join(cells[1:]) + "\n"
        line = Line(len(statement.lines) + 1, label, "", parse_number(raw) if raw else None, raw)
        if not label or _starts(label, parsing["total_prefixes"]):
            # A total closes the section above it and names itself: no heading context.
            if stack:
                line.closes, line.heading, _ = stack.pop()
                if not label:
                    line.label = f"Total {line.heading[:1].lower()}{line.heading[1:]}"
                    line.unlabelled = True
            elif not label:
                line.label = "Total (unlabelled amount)"
            if stack:
                line.section = stack[-1][0]
        elif _starts(label, parsing["summary_prefixes"]):
            pass  # read on its own: no heading, closes nothing
        elif stack:
            section, heading, _ = stack[-1]
            line.section, line.context = section, heading
            stack[-1] = (section, heading, True)
        statement.lines.append(line)


_MONTHS = {
    name: number
    for number, names in enumerate(
        [("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
         ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
         ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
         ("dec", "december")],
        start=1,
    )
    for name in names
}  # fmt: skip
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_PATTERNS = (
    re.compile(rf"\b(?P<d>\d{{1,2}})\s+(?P<m>{_MONTH})\.?,?\s*(?P<y>\d{{4}})\b", re.I),
    re.compile(rf"\b(?P<m>{_MONTH})\.?\s+(?P<d>\d{{1,2}})\s*,\s*(?P<y>\d{{4}})\b", re.I),
    re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})\b"),
)


def find_dates(text: str) -> list[date]:
    """Every full date written in the text: "31 March 2026", "June 30,2026", "2025-12-31"."""
    found: list[tuple[int, date]] = []
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            month = match["m"]
            number = int(month) if month.isdigit() else _MONTHS[month.casefold()]
            try:
                found.append((match.start(), date(int(match["y"]), number, int(match["d"]))))
            except ValueError:
                continue
    return [day for _, day in sorted(found)]


def read_period(statement: Statement) -> tuple[date | None, str]:
    """The period end the statement's first amount column is for, and a problem if any.

    The column header wins when it carries a full date; otherwise the caption's date. A
    column header that names a different year from that date means the columns aren't in the
    order the recipe reads them (or the caption is wrong), so it's a problem, not a guess.
    """
    header = statement.headers[0] if statement.headers else ""
    dates = list(dict.fromkeys(find_dates(header) or find_dates(statement.caption)))
    if not dates:
        return None, "no period end date found in the caption or the column header"
    if len(dates) > 1:
        listed = ", ".join(day.isoformat() for day in dates)
        return None, f"more than one period end date in the caption: {listed}"
    period = dates[0]
    year = re.search(r"\b(19|20)\d{2}\b", header)
    if year and int(year.group()) != period.year:
        return period, (
            f"the first amount column is headed {header!r} but the statement is for "
            f"{period.isoformat()}: the columns may be in another order"
        )
    return period, ""


def read_units(statement: Statement, rules: dict[str, Any]) -> tuple[int | None, str | None]:
    """(scale, currency code) the statement states. A scale phrase ("in thousands", "£000")
    sets the scale; with none, amounts are whole units, but only when a currency is shown.
    The scale is None when phrases name two different scales ("in millions, except shares
    in thousands"), and the currency is None when the statement shows none or several."""
    stated = f"{statement.caption}\n{' '.join(statement.headers)}"
    folded = stated.casefold()
    scales = {
        int(value) for phrase, value in rules["parsing"]["scales"].items() if phrase in folded
    }
    scale = next(iter(scales)) if len(scales) == 1 else None
    shown = f"{stated}\n{statement.cells}"
    codes = sorted(
        code
        for code, markers in rules["parsing"]["currencies"].items()
        if any(marker in shown for marker in markers)
    )
    currency = codes[0] if len(codes) == 1 else None
    if not scales and currency is not None:
        scale = 1
    return scale, currency


def units_label(scale: int | None, currency: str | None) -> str:
    if scale is None or currency is None:
        return "unknown"
    words = {1: "", 1000: " thousands", 1_000_000: " millions"}
    return f"{currency}{words.get(scale, f' x {scale}')}"


# --- 3. Place each line: last year's mapping, or the reranker ---------------------------------


@dataclass
class Decision:
    line: Line
    status: str  # known | mapped | absorbed | subtotal | not_placed | unmapped | ambiguous ...
    item_id: str | None
    ranked: list[tuple[str, float]] = field(default_factory=list)
    reason: str = ""
    source: str = ""  # last year | section heading | reranker

    @property
    def score(self) -> float | None:
        return self.ranked[0][1] if self.ranked else None

    @property
    def margin(self) -> float | None:
        """Best minus second-best score on the log-odds scale."""
        if len(self.ranked) < 2:
            return None
        return log_odds(self.ranked[0][1]) - log_odds(self.ranked[1][1])


def log_odds(probability: float) -> float:
    p = min(max(probability, 1e-9), 1 - 1e-9)
    return math.log(p / (1 - p))


def item_text(item: dict[str, Any]) -> str:
    return f"{item['name']}: {item['description']}"


def rank_line(
    client: SIEClient,
    calls: Calls,
    line: Line,
    items: list[dict[str, Any]],
    rules: dict[str, Any],
) -> list[tuple[str, float]]:
    """One score call per new label: the query is the line, the items are the template rows."""
    model = rules["models"]["rerank"]
    reply = calls.call(
        model,
        lambda: client.score(
            model,
            Item(id=f"L{line.index:02d}", text=line.query),
            [Item(id=item["id"], text=item_text(item)) for item in items],
            instruction=rules["reranker"]["instruction"],
            provision_timeout_s=WAIT_S,
        ),
        "scores",
    )
    ranked = [(str(entry["item_id"]), float(entry["score"])) for entry in reply["scores"] or []]
    ids = [item_id for item_id, _ in ranked]
    if sorted(ids) != sorted(item["id"] for item in items) or not all(
        0.0 <= score <= 1.0 for _, score in ranked
    ):
        # Every row scored once, each score a probability: a margin needs the runner-up.
        bad = SIEError(f"{model} replied without one score in [0, 1] per template row")
        calls.failures.append((model, bad))
        raise CallFailed(f"the SIE call failed: {short(bad)}")
    return sorted(ranked, key=lambda pair: pair[1], reverse=True)


def heading_group(heading: str, rules: dict[str, Any]) -> str | None:
    """The balance-sheet group a section heading names, or None for a heading that names none."""
    key = label_key(heading)
    for group, spec in rules["section_groups"].items():
        if any(key.startswith(label_key(phrase)) for phrase in spec["headings"]):
            return str(group)
    return None


def judge(
    line: Line,
    ranked: list[tuple[str, float]],
    template: list[dict[str, Any]],
    rules: dict[str, Any],
) -> Decision:
    """Accept the reranker's best row only above min_score, min_margin and in the line's group."""
    thresholds = rules["thresholds"]
    decision = Decision(line, MAPPED, ranked[0][0], ranked, source="reranker")
    top_id, top_score = ranked[0]
    groups = {item["id"]: item.get("group") for item in template}
    section = heading_group(line.context, rules) if line.context else None
    if top_score < thresholds["min_score"]:
        decision.status, decision.item_id = UNMAPPED, None
        decision.reason = (
            f"Best template row {top_id} scores {top_score:.2f}, below min_score "
            f"{thresholds['min_score']}."
        )
    elif decision.margin is not None and decision.margin < thresholds["min_margin"]:
        runner_id, runner_score = ranked[1]
        decision.status, decision.item_id = AMBIGUOUS, None
        decision.reason = (
            f"{top_id} ({top_score:.3f}) and {runner_id} ({runner_score:.3f}) are "
            f"{decision.margin:.2f} apart in log-odds, under min_margin "
            f"{thresholds['min_margin']}; not placed."
        )
    elif section is not None and groups.get(top_id) not in (None, section):
        decision.status, decision.item_id = WRONG_GROUP, None
        decision.reason = (
            f"Listed under {line.context!r} ({section.replace('_', ' ')}), but the best row "
            f"{top_id} is {str(groups[top_id]).replace('_', ' ')}; not placed."
        )
    return decision


def place_lines(
    client: SIEClient,
    calls: Calls,
    borrower_id: str,
    statement: Statement,
    template: list[dict[str, Any]],
    loans: LoanSystem,
    rules: dict[str, Any],
) -> list[Decision]:
    """A decision per line: last year's confirmed row, the section's total row for an
    unlabelled subtotal, or the reranker's best row.

    A line inside a section is a part of it, so the reranker only sees the template's detail
    rows for it, never a total row.
    """
    rows = {item["id"] for item in template}
    details = [item for item in template if item["id"] not in total_rows(rules)]
    decisions: list[Decision] = []
    for line in statement.lines:
        if line.value is None:
            decisions.append(
                Decision(line, UNREAD, None, reason=f"Amount {line.raw!r} could not be read.")
            )
            continue
        known = loans.confirmed_mapping(borrower_id, statement.kind, line.context, line.label)
        if known is not None and known.item_id in rows:
            decisions.append(
                Decision(
                    line,
                    KNOWN,
                    known.item_id,
                    reason=f"Confirmed by an analyst on the {known.confirmed_period} spread.",
                    source="last year",
                )
            )
            continue
        group = heading_group(line.heading, rules) if line.unlabelled else None
        if group is not None and rules["section_groups"][group]["total"] in rows:
            item_id = rules["section_groups"][group]["total"]
            reason = f"An unlabelled subtotal under {line.heading!r}: that section's total row."
            decisions.append(Decision(line, MAPPED, item_id, reason=reason, source="heading"))
            continue
        try:
            candidates = details if line.context else template
            ranked = rank_line(client, calls, line, candidates, rules)
        except CallFailed as failed:
            decisions.append(Decision(line, FAILED, None, reason=f"Not placed: {failed}."))
            continue
        decision = judge(line, ranked, candidates, rules)
        if known is not None:
            decision.reason = (
                f"Last year's row {known.item_id} is not in the template any more. "
                + decision.reason
            ).strip()
        decisions.append(decision)
    return decisions


def settle_totals(decisions: list[Decision], rules: dict[str, Any]) -> list[dict[str, Any]]:
    """Decide what each closing total does with its detail lines; return the sections whose
    lines don't add up to the total the statement prints for them.

    A total is accepted on its own decision first. If its detail lines add up to it and every
    one of them went to the total's row, they are absorbed (the total is the row's value). If
    some went to other rows, the total is a subtotal across rows: checked, not placed. If they
    went to the row but don't add up, the lines are placed and the total isn't. A total that
    wasn't accepted leaves its lines where they are.
    """
    tolerance = Decimal(str(rules["thresholds"]["tie_tolerance"]))
    broken: list[dict[str, Any]] = []
    for total in decisions:
        closes = total.line.closes
        details = [d for d in decisions if closes is not None and d.line.section == closes]
        if not details or total.line.value is None:
            continue
        readable = [d.line.value for d in details if d.line.value is not None]
        added = sum(readable, Decimal(0))
        adds_up = len(readable) == len(details) and abs(added - total.line.value) <= tolerance
        if len(readable) == len(details) and not adds_up:
            broken.append(
                {"heading": total.line.heading, "label": total.line.label,
                 "value": total.line.value, "added": added}
            )  # fmt: skip
        if total.status not in ACCEPTED:
            continue
        same = [d for d in details if d.status in ACCEPTED and d.item_id == total.item_id]
        if not same:
            continue
        if len(same) == len(details) and adds_up:
            for detail in same:
                detail.status = ABSORBED
                detail.reason = f"A detail line of {total.line.label!r}, which is placed instead."
        elif len(same) == len(details):
            total.status = NOT_PLACED
            total.reason = (
                f"Its lines add up to {added}, not {total.line.value}; the lines are placed "
                "instead."
            )
        else:
            total.status = SUBTOTAL
            total.reason = (
                f"A subtotal across several template rows ({total.item_id} and others); its "
                "lines are placed, and it is checked against them."
            )
    return broken


# --- 4. Fill, derive and tie out (Python) -----------------------------------------------------


@dataclass
class Cell:
    value: Decimal | None = None
    status: str = "not reported"  # reported | summed | derived | escalated | not reported
    lines: list[Decision] = field(default_factory=list)
    review: list[str] = field(default_factory=list)


def total_rows(rules: dict[str, Any]) -> set[str]:
    """Template rows that are totals: the ones a tie-out checks."""
    return {str(check["total"]) for check in rules["checks"]}


def fill(
    template: list[dict[str, Any]], decisions: list[Decision], rules: dict[str, Any]
) -> tuple[dict[str, Cell], list[str]]:
    """Put each accepted line in its row. Several lines in a detail row are summed; several in a
    total row must agree, or the row is escalated. Returns the cells and conflict reasons."""
    totals = total_rows(rules)
    cells = {item["id"]: Cell() for item in template}
    conflicts: list[str] = []
    for decision in decisions:
        if decision.status in ACCEPTED and decision.item_id is not None:
            cells[decision.item_id].lines.append(decision)
    for item_id, cell in cells.items():
        values = [d.line.value for d in cell.lines if d.line.value is not None]
        if not values:
            continue
        if item_id in totals and len(set(values)) > 1:
            cell.status = "escalated"
            named = "; ".join(f"{d.line.name} = {d.line.value}" for d in cell.lines)
            cell.review.append("lines disagree")
            conflicts.append(f"{item_id}: two lines for one total disagree ({named}).")
            continue
        if item_id in totals:
            cell.value, cell.status = values[0], "reported"
            if len(values) > 1:
                cell.review.append(f"{len(values)} lines agree")
            continue
        cell.value = sum(values, Decimal(0))
        cell.status = "reported" if len(values) == 1 else "summed"
    for decision in decisions:
        if decision.status in ESCALATED:
            # Point the analyst at the rows the escalated line most likely belongs in.
            for item_id, _ in decision.ranked[: 1 if decision.status == UNMAPPED else 2]:
                cell = cells[item_id]
                cell.review.append(f"{decision.status} line: {decision.line.name}")
                if cell.value is None:
                    cell.status = "escalated"
    return cells, conflicts


def signed_sum(parts: list[str], cells: dict[str, Cell]) -> tuple[Decimal, list[str]]:
    """The signed sum of the parts' values, and the parts with no value (counted as zero)."""
    total, missing = Decimal(0), []
    for part in parts:
        sign, name = (-1, part[1:]) if part.startswith("-") else (1, part)
        value = cells[name].value
        if value is None:
            missing.append(name)
        else:
            total += sign * value
    return total, missing


def derive(cells: dict[str, Cell], rules: dict[str, Any]) -> None:
    """Work out the rows the borrower leaves out, in rules.yaml order. A part with an escalated
    line stops it: the line might belong there. So does a part that wasn't derived for that
    reason."""
    blocked: set[str] = set()
    for item_id, parts in rules["derived"].items():
        cell = cells[item_id]
        if cell.value is not None or cell.status == "escalated":
            continue
        names = [part.lstrip("-") for part in parts]
        if any(cells[name].status == "escalated" or name in blocked for name in names):
            cell.review.append("not derived: a part has an escalated line")
            blocked.add(item_id)
            continue
        value, missing = signed_sum(parts, cells)
        if len(missing) < len(parts):
            cell.value, cell.status = value, "derived"
            cell.review.append(f"derived: {' + '.join(parts)}".replace("+ -", "- "))


def check_rows_in(parts: list[str], cells: dict[str, Cell], rules: dict[str, Any]) -> set[str]:
    """The rows a check's parts are made of, following each derived part down to its parts."""
    rows: set[str] = set()
    todo = [part.lstrip("-") for part in parts]
    while todo:
        row = todo.pop()
        if row in rows:
            continue
        rows.add(row)
        if cells[row].status == "derived":
            todo += [part.lstrip("-") for part in rules["derived"][row]]
    return rows


def tie_out(
    borrower_id: str,
    statement_of: dict[str, str],
    cells: dict[str, Cell],
    decisions: list[Decision],
    rules: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run every check. Return check rows and exceptions.

    A break by exactly twice the amount of one line in the check's parts is named: that line
    is probably shown with the opposite sign to the template's convention. The sign is never
    changed here.
    """
    tolerance = Decimal(str(rules["thresholds"]["tie_tolerance"]))
    rows: list[dict[str, Any]] = []
    exceptions: list[dict[str, Any]] = []
    for check in rules["checks"]:
        total = check["total"]
        cell = cells[total]
        base = {"borrower_id": borrower_id, "statement": statement_of[total], "check": check["id"]}
        if cell.value is None or (cell.status == "derived" and not check.get("derived_ok")):
            why = "not reported" if cell.value is None else "derived, nothing to check"
            rows.append({**base, "status": "skipped", "detail": f"{total} is {why}"})
            continue
        computed, missing = signed_sum(check["parts"], cells)
        difference = cell.value - computed
        ties = abs(difference) <= tolerance
        detail = f"{total} = {' + '.join(check['parts'])}".replace("+ -", "- ")
        if missing:
            named = [f"{n} (escalated)" if cells[n].status == "escalated" else n for n in missing]
            detail += f"; no value, counted as 0: {', '.join(named)}"
        rows.append(
            {
                **base,
                "status": "ties" if ties else "breaks",
                "reported": cell.value,
                "computed": computed,
                "difference": difference,
                "detail": detail,
            }
        )
        if ties:
            continue
        cell.review.append(f"tie-out {check['id']} breaks")
        reason = (
            f"Check {check['id']} breaks by {difference}: {total} is {cell.value}, computed "
            f"{computed} from {detail}."
        )
        rows_in = check_rows_in(check["parts"], cells, rules)
        doubled = [
            d
            for d in decisions
            if d.status in ACCEPTED
            and d.item_id in rows_in
            and d.line.value
            and abs(difference) == 2 * abs(d.line.value)
        ]
        if doubled:
            line = doubled[0].line
            reason += (
                f" The difference is twice {line.label!r} ({line.value}): the statement may "
                f"show that line with the opposite sign to the template's {doubled[0].item_id}."
            )
        exceptions.append(
            {**base, "kind": "tie_out", "item_id": total, "value": cell.value, "reason": reason}
        )
    return rows, exceptions


# --- 5. Spread one borrower -------------------------------------------------------------------


@dataclass
class Spread:
    borrower_id: str
    name: str = ""
    status: str = REVIEW
    read_with: str = ""
    periods: dict[str, str] = field(default_factory=dict)
    units: str = "unknown"
    period_due: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)
    exceptions: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    lines: list[dict[str, Any]] = field(default_factory=list)

    def flag(self, kind: str, reason: str, statement: str = "", **extra: Any) -> None:
        self.exceptions.append(
            {"borrower_id": self.borrower_id, "kind": kind, "statement": statement,
             "reason": reason, **extra}
        )  # fmt: skip

    def summary(self) -> dict[str, Any]:
        return {
            "borrower_id": self.borrower_id,
            "name": self.name,
            "status": self.status,
            "exceptions": len(self.exceptions),
            "read_with": self.read_with,
            "income_statement_period": self.periods.get("income_statement", ""),
            "balance_sheet_period": self.periods.get("balance_sheet", ""),
            "units": self.units,
            "period_due": self.period_due,
        }


def check_statement(
    spread: Spread, statement: Statement, borrower: Borrower | None, rules: dict[str, Any]
) -> str:
    """Period, units and currency for one statement, against the loan system. Returns units."""
    kind = statement.kind
    period, problem = read_period(statement)
    spread.periods[kind] = period.isoformat() if period else ""
    if period is None:
        spread.flag("period_unknown", f"The period could not be read: {problem}.", kind)
    elif problem:
        spread.flag("period", f"The period needs a look: {problem}.", kind)
    elif borrower is not None and period.isoformat() != borrower.period_due:
        spread.flag(
            "period",
            f"The statement is for the period ending {period.isoformat()}; the loan system "
            f"expects the fiscal year ending {borrower.period_due} (fiscal year end "
            f"{borrower.fiscal_year_end}).",
            kind,
        )
    scale, currency = read_units(statement, rules)
    if scale is None or currency is None:
        spread.flag(
            "units_unknown",
            "The statement doesn't show its currency or scale, or shows more than one, so its "
            "units can't be confirmed.",
            kind,
        )
    elif borrower is not None and currency != borrower.currency:
        spread.flag(
            "currency",
            f"The statement is in {currency}; the loan system has {borrower.currency}.",
            kind,
        )
    return units_label(scale, currency)


def spread_borrower(
    client: SIEClient, calls: Calls, pdf: Path, loans: LoanSystem, rules: dict[str, Any]
) -> Spread:
    borrower_id = pdf.stem
    spread = Spread(borrower_id)
    borrower = loans.find_borrower(borrower_id)
    if borrower is None:
        spread.flag(
            "borrower_unknown",
            "No borrower with this ID in the loan system: no fiscal year end, currency or last "
            "year's mappings to check against.",
        )
    else:
        spread.name, spread.period_due = borrower.name, borrower.period_due
    try:
        markdown, spread.read_with = read_markdown(client, calls, pdf, rules)
    except CallFailed as failed:
        spread.read_with = "not read"
        spread.flag("read_failed", f"The PDF was not read: {failed}.")
        markdown = ""
    statements = parse_statements(markdown, rules)
    cells: dict[str, Cell] = {}
    statement_of: dict[str, str] = {}
    decisions: list[Decision] = []
    units: dict[str, str] = {}
    for kind in STATEMENTS:
        template = rules["template"][kind]
        statement_of.update({item["id"]: kind for item in template})
        statement = statements.get(kind)
        if statement is None or not statement.lines:
            if spread.read_with != "not read":
                spread.flag("statement_missing", f"No {kind.replace('_', ' ')} found.", kind)
            cells.update({item["id"]: Cell() for item in template})
            continue
        units[kind] = check_statement(spread, statement, borrower, rules)
        placed = place_lines(client, calls, borrower_id, statement, template, loans, rules)
        for check in settle_totals(placed, rules):
            spread.flag(
                "subtotal",
                f"The lines under {check['heading']!r} add up to {check['added']}; the "
                f"statement prints {check['value']} as their total.",
                kind,
                line=check["label"],
                value=check["value"],
            )
        statement_cells, conflicts = fill(template, placed, rules)
        for conflict in conflicts:
            spread.flag("conflicting_lines", conflict, kind, item_id=conflict.split(":")[0])
        cells.update(statement_cells)
        decisions += placed
        for decision in placed:
            spread.lines.append(line_record(kind, decision))
            if decision.status in ESCALATED:
                spread.flag(
                    decision.status,
                    decision.reason,
                    kind,
                    item_id=decision.ranked[0][0] if decision.ranked else "",
                    line=decision.line.name,
                    value="" if decision.line.value is None else decision.line.value,
                )
    derive(cells, rules)
    check_rows, tie_exceptions = tie_out(borrower_id, statement_of, cells, decisions, rules)
    spread.checks, spread.exceptions = check_rows, spread.exceptions + tie_exceptions
    spread.units = "; ".join(sorted(set(units.values()))) or "unknown"
    for kind in STATEMENTS:
        for item in rules["template"][kind]:
            cell = cells[item["id"]]
            spread.rows.append(
                {
                    "statement": kind,
                    "item_id": item["id"],
                    "item": item["name"],
                    "value": "" if cell.value is None else cell.value,
                    "units": units.get(kind, ""),
                    "period": spread.periods.get(kind, ""),
                    "status": cell.status,
                    "borrower_lines": "; ".join(d.line.name for d in cell.lines),
                    "source": "; ".join(dict.fromkeys(d.source for d in cell.lines)),
                    "score": "; ".join(f"{d.score:.2f}" for d in cell.lines if d.score),
                    "margin": "; ".join(
                        f"{d.margin:.2f}" for d in cell.lines if d.margin is not None
                    ),
                    "review": "; ".join(cell.review),
                }
            )
    spread.status = CHECKS_PASSED if not spread.exceptions else REVIEW
    return spread


def line_record(kind: str, decision: Decision) -> dict[str, Any]:
    line = decision.line
    return {
        "statement": kind,
        "line": line.label,
        "context": line.context,
        "value": "" if line.value is None else str(line.value),
        "decision": decision.status,
        "item_id": decision.item_id,
        "reason": decision.reason,
        "top": [[item, round(score, 4)] for item, score in decision.ranked[:3]],
    }


# --- 6. Run and write -------------------------------------------------------------------------


def inputs(statements_dir: Path = STATEMENTS_DIR) -> list[Path]:
    return sorted(statements_dir.glob("*.pdf"))


def run(
    client: SIEClient,
    rules: dict[str, Any],
    statements_dir: Path = STATEMENTS_DIR,
    loans: LoanSystem | None = None,
) -> tuple[list[Spread], Calls]:
    loans = loans or LoanSystem()
    calls = Calls()
    spreads = [spread_borrower(client, calls, pdf, loans, rules) for pdf in inputs(statements_dir)]
    calls.check_all_failed()
    return spreads, calls


def csv_safe(value: Any) -> Any:
    """A leading ' on a text cell a spreadsheet would run as a formula; numbers as written."""
    if isinstance(value, str) and value.startswith(FORMULA_START):
        return "'" + value
    return value


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_safe(value) for key, value in row.items()})


def write_outputs(output_dir: Path, spreads: list[Spread]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "spreads.csv", SUMMARY_FIELDS, [s.summary() for s in spreads])
    for spread in spreads:
        _write_csv(output_dir / f"spread-{spread.borrower_id}.csv", SPREAD_FIELDS, spread.rows)
    _write_csv(
        output_dir / "exceptions.csv",
        EXCEPTION_FIELDS,
        [row for spread in spreads for row in spread.exceptions],
    )
    _write_csv(output_dir / "checks.csv", CHECK_FIELDS, [row for s in spreads for row in s.checks])
    lines = {s.borrower_id: {**s.summary(), "lines": s.lines} for s in spreads}
    text = json.dumps(lines, indent=2, ensure_ascii=False, default=str) + "\n"
    (output_dir / "lines.json").write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    started = time.monotonic()
    rules = load_rules()
    with make_client() as client:
        spreads, calls = stop_on_refusal(lambda: run(client, rules))
    elapsed = time.monotonic() - started
    write_outputs(output, spreads)
    for spread in spreads:
        print(f"{spread.borrower_id} {spread.name}: {spread.status} ({spread.units})")
        for row in spread.exceptions:
            print(f"  {row['kind']:<18} {row.get('item_id') or '':<24} {row['reason']}")
    per_model = ", ".join(f"{n} to {model}" for model, n in calls.count.items())
    print(f"\n{calls.total} SIE calls ({per_model}) in {elapsed:.0f} s")
    print("Statuses name what the checks found; the analyst confirms and approves the spread.")
    print(f"output: {output}")


if __name__ == "__main__":
    main()
