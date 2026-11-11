"""An overnight agent for the stories the counterparty news workflow puts in review.

Who:       The credit analyst who reads the workflow's review queue every morning. The agent
           does the first reading; the analyst still decides, and forwards anything to a credit
           officer by hand.
Input:     The workflow's results on data/news.jsonl and data/agent/news.jsonl (recipe.run,
           unchanged), the counterparty list, and the loan file's credit files and notes
           (data/agent/credit_files.csv, data/agent/credit_notes.csv).
Agent:     Claude runs one job per article in review and picks its next tool:
           get_review_item       plain Python, the article and the workflow's review rows
           lookup_counterparty   plain Python, the counterparty list (counterparties.py)
           read_credit_file      plain Python, one borrower's credit file and dated notes
           check_event_for       the workflow's own event check (GLiNER2.5-Decide on SIE) with
                                 the link fixed to one borrower
           submit_finding        plain Python, checked before it's accepted
Judge:     `check_finding` in code after the loop: an allowed outcome for the row's reason, a
           listed candidate, every quote whole words from one field of its source, every
           candidate's file read, every figure of two or more digits found somewhere, a score
           that matches an event check, and for a suggested link a fresh run of the event check
           for every candidate on the row. What it finds goes back to Claude once.
Output:    runs/<timestamp>/agent/: review_notes.md and review.csv (the analyst's queue with the
           agent's status and note), jobs/<article>.json (tool calls, checks, status),
           transcripts/<article>.json and cost.json. Nothing goes to a credit officer's alert
           file or to a borrower.
Run using: SIE Serverless, or SIE Cluster or SIE Local with SIE_BASE_URL, plus ANTHROPIC_API_KEY.

    uv run --group agent agent.py
    uv run --group agent agent.py --article N-04
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from counterparties import CounterpartyList
from sie_sdk import SIEClient
from sie_sdk.client.errors import SIEError

import recipe

AGENT_DATA = recipe.DATA_DIR / "agent"
AGENT_NEWS = AGENT_DATA / "news.jsonl"
QUEUES = (recipe.NEWS_PATH, AGENT_NEWS)
SAME_CHARACTER = {0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"', 0x2013: "-", 0x2014: "-"}
OUTCOMES = (
    "suggest_link",
    "suggest_not_a_borrower",
    "suggest_alert",
    "suggest_filed",
    "left_for_person",
)
SUGGESTIONS = ("suggest_link", "suggest_alert", "suggest_filed")  # name one candidate
ARTICLE = "article"
NOTE_MARKUP = re.compile(r"https?://|www\.|<\s*/?\s*[a-z!]", re.IGNORECASE)  # links, HTML tags
MD_SPECIAL = re.compile(r"([\\`*_\[\]()<>|!#])")
QUEUE_FIELDS = (*recipe.REVIEW_FIELDS, "agent_status", "agent_outcome", "agent_note")


class ToolError(Exception):
    """A lookup that failed, sent back to Claude as an error it can react to."""


# --- The loan file: credit files and notes -----------------------------------------------------


class CreditFiles:
    """The credit file per borrower and its dated notes, from the two CSV seeds in data/agent/.

    This stands in for the loan system's credit file view: one row per borrower with what the
    bank lends against, and the notes analysts and relationship managers add over the year.
    """

    def __init__(self, folder: Path = AGENT_DATA) -> None:
        with (folder / "credit_files.csv").open(newline="", encoding="utf-8") as handle:
            self.files = {row["counterparty_id"]: row for row in csv.DictReader(handle)}
        self.notes: dict[str, list[dict[str, str]]] = {}
        with (folder / "credit_notes.csv").open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                self.notes.setdefault(row["counterparty_id"], []).append(row)

    def read(self, counterparty_id: str) -> dict[str, Any]:
        if counterparty_id not in self.files:
            raise ToolError(f"there is no credit file for {counterparty_id}")
        notes = sorted(self.notes.get(counterparty_id, []), key=lambda note: note["date"])
        return {"file": self.files[counterparty_id], "notes": notes}

    def values(self, counterparty_id: str) -> list[str]:
        """The field values and note texts of one borrower's file: what a quote from it is
        checked against, one value at a time. Field names, the id and dates are left out, so
        "counterparty_id: CP-1004" is not a quote from the file."""
        if counterparty_id not in self.files:
            return []
        found = self.read(counterparty_id)
        fields = [v for k, v in found["file"].items() if k not in ("counterparty_id",)]
        return [*fields, *(note["note"] for note in found["notes"])]


# --- SIE calls, counted ------------------------------------------------------------------------


def credits_of(reply: Any) -> float:
    """What one SIE call was charged, from the metadata on its reply (a batch repeats it)."""
    first = reply[0] if isinstance(reply, list) and reply else reply
    request = first.get("request") if isinstance(first, dict) else None
    value = request.get("credits_debited") if isinstance(request, dict) else None
    return float(value) if isinstance(value, (int, float)) else 0.0


class CountingClient:
    """Passes `extract` through to SIE and counts the calls and the credits they were charged."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.calls = 0
        self.credits = 0.0

    def extract(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        reply = self.client.extract(*args, **kwargs)
        self.credits += credits_of(recipe.jsonable(reply))
        return reply


# --- Review rows and what the agent may do with each ------------------------------------------


@dataclass
class Row:
    number: int
    mention: str
    candidates: list[str]  # counterparty ids the workflow listed for this row
    reason: str
    kind: str  # a key of agent.review_reasons, or "other"
    allowed: list[str]
    csv: dict[str, Any] = field(default_factory=dict)  # the workflow's review.csv row


def reason_kind(reason: str, rules: dict[str, Any]) -> tuple[str, list[str]]:
    """The kind of review reason, and the outcomes the agent may submit for it."""
    for kind, spec in rules["agent"]["review_reasons"].items():
        if any(text in reason for text in spec["match"]):
            return kind, list(spec["allowed"])
    return "other", ["left_for_person"]


def candidate_ids(text: str, cps: CounterpartyList) -> list[str]:
    """The counterparty ids in a review row's candidates cell, in list order."""
    return [cp.id for cp in cps.all() if f"{cp.id} (" in text]


def review_items(result: recipe.Result, cps: CounterpartyList, rules: dict[str, Any]) -> list[Row]:
    """The article's rows in the workflow's review.csv, numbered, with their kind."""
    rows = []
    for number, row in enumerate(recipe.review_rows([result]), start=1):
        kind, allowed = reason_kind(row["reason"], rules)
        candidates = candidate_ids(row["candidates"], cps)
        rows.append(Row(number, row["mention"], candidates, row["reason"], kind, allowed, row))
    return rows


# --- The event check with the link fixed -------------------------------------------------------


def other_companies(result: recipe.Result) -> list[recipe.Entity]:
    """Company spans the entity model found that aren't a name on the counterparty list."""
    listed = [e for e in result.entities if e.source == "list"]
    return [
        e
        for e in result.entities
        if e.source == "model"
        and e.label == recipe.COMPANY
        and not any(e.start < n.end and n.start < e.end for n in listed)
    ]


def event_text(
    result: recipe.Result, counterparty_id: str, rules: dict[str, Any]
) -> tuple[str, str]:
    """The sentences the workflow's event check would read for this borrower, with every
    mention that lists it treated as linked, and the name as the article writes it.

    It uses the workflow's own `sentences` and `context`, so a link the workflow already made
    reads exactly what the workflow read.
    """
    article = result.article
    own = [
        link
        for link in result.links
        if link.status != recipe.PLACE
        and (
            link.counterparty_id == counterparty_id
            or counterparty_id in re.findall(r"(\S+) \(", link.candidates)
        )
    ]
    if not own:
        raise ToolError(f"no mention in {article.id} names {counterparty_id} or lists it")
    mine = {id(link) for link in own}
    names = [
        (link.start, link.end, counterparty_id if id(link) in mine else link.counterparty_id)
        for link in result.links
        if link.status != recipe.PLACE
    ]
    names += [(e.start, e.end, "") for e in other_companies(result)]
    spans = recipe.sentences(article, rules)
    after = rules["thresholds"]["context_sentences_after"]
    return recipe.context(article.text, spans, own, names, counterparty_id, after), own[0].mention


def event_check(
    client: Any,
    calls: recipe.CallLog,
    result: recipe.Result,
    counterparty_id: str,
    cps: CounterpartyList,
    rules: dict[str, Any],
) -> dict[str, Any]:
    """The workflow's event check (GLiNER2.5-Decide, then `judge_event`) for one borrower.

    Returns what the workflow would decide: status alert, filed or review, the event (the alert
    event, `none` when filed, the top warning when unclear) and its score, both to 2 places.
    A transient SIE failure raises ToolError; a refusal raises SIEStopped and stops the run.
    """
    try:
        cp = cps.get(counterparty_id)
    except KeyError:
        raise ToolError(f"{counterparty_id} is not on the counterparty list") from None
    text, name = event_text(result, counterparty_id, rules)
    item_id = f"{result.article.id}#{counterparty_id}"
    probabilities, failed = recipe.ask_event(
        cast(SIEClient, client), calls, item_id, text, name, rules
    )
    if probabilities is None:
        raise ToolError(f"the event check couldn't run: {failed}")
    event = recipe.Event(counterparty_id, cp.legal_name, name, recipe.REVIEW)
    recipe.judge_event(event, probabilities, rules)
    if event.status == recipe.REVIEW:
        label, score = event.top_warning, event.top_warning_score
    else:
        label, score = event.event, event.score
    return {
        "counterparty_id": counterparty_id,
        "read": text,
        "probabilities": {key: round(value, 2) for key, value in probabilities.items()},
        "status": event.status,
        "event": label,
        "score": round(float(score or 0.0), 2),
        "reason": event.reason,
    }


# --- The tools Claude can call -----------------------------------------------------------------


def object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def tool_list(rules: dict[str, Any]) -> list[dict[str, Any]]:
    events = [*rules["events"], ""]
    statement = object_schema({"source": {"type": "string"}, "quote": {"type": "string"}})
    item = object_schema(
        {
            "row": {"type": "integer"},
            "outcome": {"type": "string", "enum": list(OUTCOMES)},
            "counterparty_id": {"type": "string"},
            "event": {"type": "string", "enum": events},
            "score": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            "statements": {"type": "array", "items": statement},
        }
    )
    return [
        {
            "name": "get_review_item",
            "description": (
                "This job's article as the workflow read it: headline, body, the company, ticker "
                "and place names it found, the sentences its event check read, and each review "
                "row with its reason, candidate borrowers and the outcomes allowed for it."
            ),
            "strict": True,
            "input_schema": object_schema({}),
        },
        {
            "name": "lookup_counterparty",
            "description": (
                "Look a borrower up in the bank's counterparty list by one of: its id "
                "(CP-1004), a name or alias as written, or a ticker symbol (NWEG). Leave the "
                "others empty. Returns the matching borrowers with their aliases, registration "
                "number, parent ticker and credit officer."
            ),
            "strict": True,
            "input_schema": object_schema(
                {
                    "counterparty_id": {"type": "string"},
                    "name": {"type": "string"},
                    "ticker": {"type": "string"},
                }
            ),
        },
        {
            "name": "read_credit_file",
            "description": (
                "One borrower's credit file: sector, business, main sites, largest customers, "
                "facilities, security, last annual review, and the dated notes on the file."
            ),
            "strict": True,
            "input_schema": object_schema({"counterparty_id": {"type": "string"}}),
        },
        {
            "name": "check_event_for",
            "description": (
                "Run the workflow's own event check on this article with the link fixed to one "
                "borrower: the sentences it reads, a probability per event, and the status the "
                "workflow would give (alert, filed or review) with its event and score."
            ),
            "strict": True,
            "input_schema": object_schema({"counterparty_id": {"type": "string"}}),
        },
        {
            "name": "submit_finding",
            "description": (
                "Hand in the finding for the analyst: one item per review row, and a note. This "
                "ends the job unless the check sends it back."
            ),
            "strict": True,
            "input_schema": object_schema(
                {"items": {"type": "array", "items": item}, "note": {"type": "string"}}
            ),
        },
    ]


@dataclass
class Job:
    """What Claude's tools can reach for one article, and what they did."""

    result: recipe.Result
    rows: list[Row]
    cps: CounterpartyList
    files: CreditFiles
    rules: dict[str, Any]
    client: Any  # CountingClient, or a stand-in in selfcheck.py
    calls: recipe.CallLog
    read_files: list[str] = field(default_factory=list)  # ids read_credit_file returned
    looked_up: list[str] = field(default_factory=list)  # ids lookup_counterparty returned
    outputs: list[str] = field(default_factory=list)  # every tool result, as JSON
    sie_failures: list[str] = field(default_factory=list)
    judge_checks: dict[str, dict[str, Any]] = field(default_factory=dict)  # one SIE call per id

    def scope(self) -> set[str]:
        """The borrowers this job may read: the candidates on its review rows, and any
        borrower lookup_counterparty returned for a name or ticker the article prints."""
        return {c for row in self.rows for c in row.candidates} | set(self.looked_up)

    def run(self, name: str, tool_input: dict[str, Any]) -> str:
        """Run one lookup and return its result as JSON. Raises ToolError for Claude to read."""
        tools: dict[str, Callable[..., dict[str, Any]]] = {
            "get_review_item": self.get_review_item,
            "lookup_counterparty": self.lookup_counterparty,
            "read_credit_file": self.read_credit_file,
            "check_event_for": self.check_event_for,
        }
        if name not in tools:
            raise ToolError(f"there is no tool called {name}")
        schema = next(t["input_schema"] for t in tool_list(self.rules) if t["name"] == name)
        if set(tool_input) != set(schema["properties"]):
            raise ToolError(
                f"{name} takes {sorted(schema['properties']) or 'no fields'}, "
                f"got {sorted(tool_input) or 'no fields'}"
            )
        if not all(isinstance(value, str) for value in tool_input.values()):
            raise ToolError(f"every field of {name} is a string")
        output = json.dumps(tools[name](**tool_input), ensure_ascii=False)
        self.outputs.append(output)
        return output

    def get_review_item(self) -> dict[str, Any]:
        article = self.result.article
        return {
            "article_id": article.id,
            "source": article.source,
            "published": article.published,
            "headline": article.headline,
            "body": article.body,
            "names_found": [
                {"text": e.text, "label": e.label, "found_by": e.source}
                for e in self.result.entities
            ],
            "event_check_read": [
                {"counterparty_id": e.counterparty_id, "status": e.status, "read": e.model_read}
                for e in self.result.events
            ],
            "review_rows": [
                {
                    "row": row.number,
                    "mention": row.mention,
                    "candidates": row.candidates,
                    "reason": row.reason,
                    "allowed_outcomes": row.allowed,
                }
                for row in self.rows
            ],
        }

    def lookup_counterparty(self, counterparty_id: str, name: str, ticker: str) -> dict[str, Any]:
        given = [value for value in (counterparty_id, name, ticker) if value.strip()]
        if len(given) != 1:
            raise ToolError("give exactly one of counterparty_id, name or ticker")
        article = self.result.article.text
        if counterparty_id.strip():
            cp_id = counterparty_id.strip()
            if cp_id not in self.scope():
                raise ToolError(
                    f"{cp_id} isn't a candidate on this article's review rows or a borrower a "
                    "lookup returned; look it up by a name or ticker the article prints"
                )
            found = [self.cps.get(cp_id)]
        elif name.strip():
            if not contains_words(article, name):
                raise ToolError(f"{name!r} isn't a name this article prints")
            found = self.cps.find_by_legal_name(name) or self.cps.find_by_alias(name)
        else:
            symbol = ticker.split(":")[-1].strip()
            if not symbol or not contains_words(article, symbol):
                raise ToolError(f"{ticker!r} isn't a ticker this article prints")
            found = self.cps.find_by_parent_ticker(symbol)
        self.looked_up += [cp.id for cp in found if cp.id not in self.looked_up]
        return {"matches": [asdict(cp) for cp in found]}

    def read_credit_file(self, counterparty_id: str) -> dict[str, Any]:
        cp_id = counterparty_id.strip()
        if cp_id not in self.scope():
            raise ToolError(
                f"{cp_id} isn't a candidate on this article's review rows or a borrower "
                "lookup_counterparty returned in this job; look it up first by a name or ticker "
                "the article prints"
            )
        found = self.files.read(cp_id)
        self.read_files.append(cp_id)
        return found

    def check_event_for(self, counterparty_id: str) -> dict[str, Any]:
        try:
            return event_check(
                self.client, self.calls, self.result, counterparty_id.strip(), self.cps, self.rules
            )
        except ToolError as error:
            if "SIE call failed" in str(error):
                self.sie_failures.append(str(error))
            raise


# --- The judge: what code can check in a finding -----------------------------------------------


def plain(text: str) -> str:
    """Text for comparing a quote: no markdown marks, one kind of quote and dash, single spaces."""
    text = re.sub(r"[`*\\]", "", text).translate(SAME_CHARACTER)
    return re.sub(r"\s+", " ", text).strip().casefold()


def contains_words(whole: str, part: str) -> bool:
    """`part` appears in `whole` as whole words: "Northwind has lost" yes, "orthwind" or "o" no."""
    wanted = plain(part)
    return (
        bool(wanted) and re.search(rf"(?<!\w){re.escape(wanted)}(?!\w)", plain(whole)) is not None
    )


def figures(text: str, min_digits: int) -> set[str]:
    """Decimals such as 0.78, and whole numbers with at least `min_digits` digits."""
    joined = re.sub(r"(?<=\d),(?=\d{3})", "", text)
    pattern = rf"\d+\.\d+|(?<![\d.])\d{{{min_digits},}}(?![\d]|\.\d)"
    return {re.sub(r"\.0+$", "", figure) for figure in re.findall(pattern, joined)}


def source_values(source: str, job: Job) -> list[str] | None:
    """The texts a quote from `source` may come from, one field at a time, or None for a source
    outside this job: the article (headline, body), or the file of a borrower in its scope."""
    if source == ARTICLE:
        return [job.result.article.headline, job.result.article.body]
    if source in job.scope() and source in job.files.files:
        return job.files.values(source)
    return None


def workflow_score(result: recipe.Result, cp_id: str, event: str, no_event: str) -> float | None:
    """The workflow's own number for this borrower and event: `none`, or its top warning."""
    for found in result.events:
        if found.counterparty_id != cp_id:
            continue
        if event == no_event:
            return found.none_score
        if event and event == found.top_warning:
            return found.top_warning_score
    return None


def check_finding(
    finding: dict[str, Any],
    job: Job,
    fresh: Callable[[str], dict[str, Any] | None],
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Every problem in a finding, and the event checks the judge ran for it.

    `fresh(counterparty_id)` runs the workflow's event check for one borrower (None when the SIE
    call failed); each borrower's check runs once per job and is reused on a revision. An empty
    list means everything code can check held up; it never means the suggestion is right.
    """
    rules = job.rules["agent"]
    events = set(job.rules["events"])
    no_event = job.rules["no_event"]
    tolerance = rules["score_tolerance"]
    min_words = rules["quote_min_words"]
    problems: list[str] = []
    checks: dict[str, dict[str, Any]] = {}
    rows = {row.number: row for row in job.rows}
    numbers = [item["row"] for item in finding["items"]]
    for number in sorted(set(numbers) - set(rows)):
        problems.append(f"row {number} isn't one of this article's review rows")
    for number in sorted(set(rows) - set(numbers)):
        problems.append(f"row {number} ({rows[number].reason}) has no item in the finding")
    for number in sorted({n for n in numbers if numbers.count(n) > 1}):
        problems.append(f"row {number} has more than one item")
    for cp_id in dict.fromkeys(c for row in job.rows for c in row.candidates):
        if cp_id not in job.read_files:
            problems.append(f"the credit file of candidate {cp_id} was never read")
    for item in finding["items"]:
        row = rows.get(item["row"])
        if row is None:
            continue
        outcome, cp_id, event = item["outcome"], item["counterparty_id"].strip(), item["event"]
        score = item["score"]
        label = f"row {row.number}"
        if outcome not in row.allowed:
            problems.append(
                f"{label}: {outcome} is not allowed for this reason ({row.kind}); allowed: "
                f"{', '.join(row.allowed) or 'nothing'}"
            )
        if cp_id and cp_id not in row.candidates:
            problems.append(
                f"{label}: {cp_id} is not a candidate the workflow listed "
                f"({', '.join(row.candidates) or 'none'})"
            )
        if outcome in SUGGESTIONS and not cp_id:
            problems.append(f"{label}: {outcome} needs a counterparty_id")
        if event and event not in events:
            problems.append(f"{label}: {event!r} is not an event in rules.yaml")
        if outcome == "suggest_alert" and event in ("", no_event):
            problems.append(f"{label}: suggest_alert needs a warning event")
        if outcome == "suggest_filed" and event != no_event:
            problems.append(f"{label}: suggest_filed needs the event {no_event!r}")
        sources = set()
        mention_quoted = False
        for statement in item["statements"]:
            source, quote = statement["source"].strip(), statement["quote"]
            values = source_values(source, job)
            if values is None:
                problems.append(
                    f"{label}: cites a source that doesn't exist in this job: {source!r} (use "
                    "the article, or the file of a candidate or a borrower you looked up)"
                )
                continue
            if len(re.findall(r"\w+", quote)) < min_words:
                problems.append(
                    f"{label}: the quote from {source} is under {min_words} words: {quote!r}"
                )
                continue
            if not any(contains_words(value, quote) for value in values):
                problems.append(
                    f"{label}: the quote isn't word for word in {source} (whole words, from one "
                    f"field or note): {quote!r}"
                )
                continue
            sources.add(source)
            mention_quoted = mention_quoted or (
                source == ARTICLE and contains_words(quote, row.mention)
            )
        if outcome in SUGGESTIONS and cp_id:
            if ARTICLE not in sources:
                problems.append(f"{label}: {outcome} needs a quote from the article")
            if cp_id not in sources:
                problems.append(f"{label}: {outcome} needs a quote from {cp_id}'s credit file")
        if outcome == "suggest_link" and not mention_quoted:
            problems.append(
                f"{label}: suggest_link needs an article quote that contains the mention "
                f"{row.mention!r}"
            )
        if outcome == "suggest_not_a_borrower":
            if ARTICLE not in sources:
                problems.append(f"{label}: {outcome} needs a quote from the article")
            if not sources & set(row.candidates):
                problems.append(
                    f"{label}: {outcome} needs a quote from the credit file of "
                    f"{' or '.join(row.candidates) or 'the candidate'}"
                )
        if outcome == "suggest_link" and cp_id in row.candidates:
            # Every candidate's check, so status_of can see whether they tell the pick apart.
            for candidate in row.candidates:
                check = job.judge_checks.get(candidate) or fresh(candidate)
                if check is not None:  # None: an SIE failure, recorded on the job
                    job.judge_checks[candidate] = checks[candidate] = check
            pick = checks.get(cp_id)
            if pick is None:
                continue
            if event != pick["event"] or not isinstance(score, (int, float)):
                problems.append(
                    f"{label}: the finding says {event or 'no event'} {score}, but the event "
                    f"check with the link fixed to {cp_id} says {pick['event']} "
                    f"{pick['score']:.2f} ({pick['status']})"
                )
            elif abs(float(score) - pick["score"]) > tolerance:
                problems.append(
                    f"{label}: the finding gives {event} {score}, but the event check says "
                    f"{pick['score']:.2f}"
                )
        elif score is not None:
            number = workflow_score(job.result, cp_id, event, no_event)
            if number is None:
                problems.append(
                    f"{label}: the score {score} matches no number from the workflow's event "
                    f"check for {cp_id or 'no borrower'} and {event or 'no event'}; leave it null"
                )
            elif abs(float(score) - number) > tolerance:
                problems.append(
                    f"{label}: the finding gives {event} {score}, but the workflow's event check "
                    f"gave {number:.2f}"
                )
    note = finding["note"]
    if not note.strip():
        problems.append("the note to the analyst is empty")
    if NOTE_MARKUP.search(note):
        problems.append("the note has a link or an HTML tag; write it as plain text")
    # Figures may come from the article, any tool result, the judge's own event checks, or a
    # counterparty id on the list ("CP-1005" is not a figure to check; "CP-2001" is).
    ids = " ".join(cp.id for cp in job.cps.all())
    known = "\n".join([job.result.article.text, *job.outputs, json.dumps(checks), ids])
    allowed = figures(known, rules["figure_min_digits"])
    for figure in sorted(figures(note, rules["figure_min_digits"]) - allowed):
        problems.append(f"{figure} in the note didn't come from the article, a file or a tool")
    for item in finding["items"]:
        cp_id = item["counterparty_id"].strip()
        if item["outcome"] in SUGGESTIONS and cp_id:
            try:
                legal = job.cps.get(cp_id).legal_name
            except KeyError:
                continue
            if cp_id not in note and plain(legal) not in plain(note):
                problems.append(f"the note doesn't name the suggested borrower {cp_id}")
    return problems, checks


def status_of(
    finding: dict[str, Any] | None,
    problems: list[str],
    checks: dict[str, dict[str, Any]],
    job: Job,
    stopped: str,
    ran_out: bool,
) -> str:
    """The job's status, from what the checks found. Each one needs positive evidence.

    A suggested link is finding_checked only when the event check with the link fixed to the
    pick differs, in event or status, from the check for every other candidate on the row, so
    that check could have caught a wrong pick. When the candidates read the same sentences
    (N-04: both Northwinds), the checks agree and the call isn't checked.
    """
    if job.sie_failures:
        return "sie_failed"
    if ran_out:
        return "out_of_steps"
    if finding is None or stopped:
        return "no_finding"
    if problems:
        return "check_failed"
    outcomes = [item["outcome"] for item in finding["items"]]
    if not outcomes:
        return "check_failed"
    if all(outcome == "left_for_person" for outcome in outcomes):
        return "left_for_person"
    rows = {row.number: row for row in job.rows}
    not_checked = False
    for item in finding["items"]:
        if item["outcome"] == "left_for_person":
            continue
        if item["outcome"] != "suggest_link":
            not_checked = True
            continue
        pick_id = item["counterparty_id"].strip()
        pick = checks.get(pick_id)
        others = [checks.get(c) for c in rows[item["row"]].candidates if c != pick_id]
        if pick is None or any(other is None for other in others):
            return "check_failed"  # never confirmed, so never finding_checked
        same = [
            o for o in others if o and (o["event"], o["status"]) == (pick["event"], pick["status"])
        ]
        if pick["status"] == recipe.REVIEW or same or not others:
            not_checked = True
    return "call_not_checked" if not_checked else "finding_checked"


# --- The agent loop ----------------------------------------------------------------------------


@dataclass
class AgentResult:
    finding: dict[str, Any] | None = None
    ran_out: bool = False
    stopped: str = ""
    tool_calls: int = 0
    lookups: int = 0
    revisions: int = 0
    first_review: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)  # tokens per answering model
    transcript: list[dict[str, Any]] = field(default_factory=list)

    def count(self, model: str, usage: Any) -> None:
        """Add one response's tokens, under the model that actually answered it."""
        tokens = {
            "input_tokens": usage.input_tokens,
            "cache_write_tokens": usage.cache_creation_input_tokens or 0,
            "cache_read_tokens": usage.cache_read_input_tokens or 0,
            "output_tokens": usage.output_tokens,
        }
        mine = self.by_model.setdefault(model, dict.fromkeys(tokens, 0))
        for key, value in tokens.items():
            mine[key] += value
            setattr(self, key, getattr(self, key) + value)


def make_claude() -> Any:
    """The Anthropic client. Imported here, so selfcheck.py runs without the agent group."""
    import anthropic

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Stopped: set ANTHROPIC_API_KEY, in .env or the environment.")
    return anthropic.Anthropic()


def transient_claude_error(error: Exception) -> bool:
    """A Claude API error that may pass (a lost connection or timeout, a rate limit, a 5xx, a
    408 or 409): the job ends without a finding and a person reads it. Any other error stops the
    run. False for everything without the agent group, so selfcheck.py's stand-ins raise."""
    try:
        import anthropic
    except ImportError:
        return False
    passing = (
        anthropic.APIConnectionError,
        anthropic.RateLimitError,
        anthropic.InternalServerError,
    )
    if isinstance(error, passing):  # APIConnectionError covers APITimeoutError
        return True
    return isinstance(error, anthropic.APIStatusError) and error.status_code in (408, 409)


def ask_claude(claude: Any, messages: list[Any], rules: dict[str, Any]) -> Any:
    settings = rules["agent"]
    extra: dict[str, Any] = {}
    if settings["fallbacks"]:
        extra = {"betas": [settings["fallback_beta"]], "fallbacks": settings["fallbacks"]}
    return claude.beta.messages.create(
        model=settings["model"],
        max_tokens=settings["max_tokens"],
        system=settings["system"],
        tools=tool_list(rules),
        messages=messages,
        output_config={"effort": settings["effort"]},
        cache_control={"type": "ephemeral"},
        **extra,
    )


def job_prompt(job: Job) -> str:
    article = job.result.article
    kinds = ", ".join(dict.fromkeys(row.kind for row in job.rows))
    return (
        f"Article {article.id} ({article.source}, {article.published}) is in the review queue "
        f"with {len(job.rows)} review row(s), reason kind: {kinds}. Start with get_review_item."
    )


def text_of(content: list[Any]) -> str:
    return "\n".join(block.text for block in content if block.type == "text")


def one_lookup(job: Job, use: Any, result: AgentResult, max_lookups: int) -> dict[str, Any]:
    """Run one tool call, or refuse it past the limit, and shape the result for Claude."""
    entry: dict[str, Any] = {"tool": use.name, "input": use.input}
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": use.id}
    if result.lookups >= max_lookups:
        result.ran_out = True
        output = "No tool calls left. Call submit_finding now with what you have."
        block["is_error"] = True
    else:
        result.lookups += 1
        result.tool_calls += 1
        try:
            output = job.run(use.name, dict(use.input))
        except ToolError as error:
            output = str(error)
            block["is_error"] = True
    entry["output"] = output
    entry["is_error"] = block.get("is_error", False)
    result.transcript.append(entry)
    block["content"] = output
    return block


def one_submission(
    job: Job,
    use: Any,
    result: AgentResult,
    tool_results: list[dict[str, Any]],
    fresh: Callable[[str], dict[str, Any] | None],
) -> bool:
    """Check a submitted finding. True ends the loop; False sends the problems back to Claude."""
    result.tool_calls += 1
    finding = dict(use.input)
    problems, checks = check_finding(finding, job, fresh)
    result.checks = checks  # the last submission's checks only
    result.transcript.append({"tool": "submit_finding", "input": use.input, "problems": problems})
    if problems and result.revisions < job.rules["agent"]["max_revisions"]:
        result.revisions += 1
        result.first_review = result.first_review or problems
        found = "\n".join(f"- {problem}" for problem in problems)
        tool_results.append(
            {
                "type": "tool_result",
                "tool_use_id": use.id,
                "is_error": True,
                "content": "The check found these problems. Fix them and call submit_finding "
                "again.\n" + found,
            }
        )
        return False
    result.finding, result.problems = finding, problems
    return True


def run_agent(claude: Any, job: Job, fresh: Callable[[str], dict[str, Any] | None]) -> AgentResult:
    """Let Claude read and look things up until it submits a finding or reaches a limit."""
    settings = job.rules["agent"]
    prompt = job_prompt(job)
    messages: list[Any] = [{"role": "user", "content": prompt}]
    result = AgentResult(transcript=[{"prompt": prompt}])
    asked_to_submit = False
    for _ in range(settings["max_rounds"]):
        try:
            response = ask_claude(claude, messages, job.rules)
        except Exception as error:
            if not transient_claude_error(error):
                raise  # main() stops the run with the error's own message
            result.stopped = f"the Claude API failed: {type(error).__name__}: {error}"
            return result
        result.count(getattr(response, "model", None) or settings["model"], response.usage)
        if response.stop_reason == "refusal":
            result.stopped = "Claude declined the request"
            return result
        if response.stop_reason == "max_tokens":
            result.stopped = "Claude hit max_tokens in rules.yaml"
            return result
        messages.append({"role": "assistant", "content": response.content})
        if said := text_of(response.content):
            result.transcript.append({"said": said})
        if response.stop_reason == "pause_turn":
            continue
        uses = [block for block in response.content if block.type == "tool_use"]
        if not uses:
            if asked_to_submit:
                result.stopped = "Claude ended its turn without calling submit_finding"
                return result
            asked_to_submit = True
            messages.append({"role": "user", "content": "Call submit_finding now."})
            continue
        tool_results: list[dict[str, Any]] = []
        accepted = False
        # Lookups first, so a finding sent in the same response is checked against their results.
        for use in sorted(uses, key=lambda use: use.name == "submit_finding"):
            if use.name != "submit_finding":
                tool_results.append(one_lookup(job, use, result, settings["max_lookups"]))
            elif one_submission(job, use, result, tool_results, fresh):
                accepted = True
                break
        if accepted:
            return result
        messages.append({"role": "user", "content": tool_results})
    result.ran_out = True
    result.stopped = "reached the limit on requests to Claude"
    return result


# --- Cost --------------------------------------------------------------------------------------


def price_of(model: str, prices: dict[str, dict[str, float]]) -> dict[str, float] | None:
    """The prices for the model that answered, by its id with any date suffix dropped."""
    return prices.get(model) or prices.get(re.sub(r"-\d{8}$", "", model))


def claude_usd(result: AgentResult, prices: dict[str, dict[str, float]]) -> tuple[float, list[str]]:
    """What one job's Claude requests cost, in dollars, each response priced on the model that
    answered it (a fallback answer on its fallback model), and the models with no price."""
    total, unpriced = 0.0, []
    for model, tokens in result.by_model.items():
        price = price_of(model, prices)
        if price is None:
            unpriced.append(model)
            continue
        total += (
            tokens["input_tokens"] * price["input"]
            + tokens["cache_write_tokens"] * price["cache_write"]
            + tokens["cache_read_tokens"] * price["cache_read"]
            + tokens["output_tokens"] * price["output"]
        ) / 1_000_000
    return total, unpriced


# --- The run -----------------------------------------------------------------------------------


def every_call_failed(error: recipe.SIEStopped) -> bool:
    """True when the run stopped because every SIE call failed for a reason that may pass,
    not because SIE refused the call (`recipe.is_fatal`)."""
    cause = error.__cause__
    return isinstance(cause, SIEError) and not recipe.is_fatal(cause)


def run_workflow(
    client: CountingClient, rules: dict[str, Any], paths: tuple[Path, ...], scratch: Path
) -> tuple[list[recipe.Result], dict[str, Any]]:
    """recipe.run over each queue, unchanged; articles with a failed SIE call get one retry."""
    before, credits, started = client.calls, client.credits, time.monotonic()
    results: list[recipe.Result] = []
    for path in paths:
        results += recipe.run(cast(SIEClient, client), rules, None, path)
    calls, spent = client.calls - before, client.credits - credits
    seconds = time.monotonic() - started
    cps = CounterpartyList()
    failed = [
        r for r in results if any(row.kind == "sie_failed" for row in review_items(r, cps, rules))
    ]
    retry_failed = ""
    if failed:
        retry = scratch / "retry.jsonl"
        retry.write_text(
            "".join(json.dumps(asdict(r.article)) + "\n" for r in failed), encoding="utf-8"
        )
        try:
            redone = recipe.run(cast(SIEClient, client), rules, None, retry)
        except recipe.SIEStopped as error:
            if not every_call_failed(error):
                raise  # a refusal: the run stops, as in the workflow
            # Every retry call failed: keep the first pass, so those articles go to the
            # analyst as sie_failed.
            retry_failed = str(error)
            print(f"The retry failed too; those articles go to the analyst as sie_failed: {error}")
            redone = []
        again = {r.article.id: r for r in redone}
        results = [again.get(r.article.id, r) for r in results]
    cost = {
        "articles": len(results),
        "sie_calls": calls,
        "sie_credits": spent,
        "sie_usd": spent * rules["agent"]["sie_credit_usd"],
        "seconds": round(seconds, 1),
        "retried_articles": [r.article.id for r in failed],
        "retry_sie_calls": client.calls - before - calls,
        "retry_failed": retry_failed,
    }
    return results, cost


def answer(
    claude: Any,
    result: recipe.Result,
    rows: list[Row],
    cps: CounterpartyList,
    files: CreditFiles,
    rules: dict[str, Any],
    client: CountingClient,
    calls: recipe.CallLog,
) -> dict[str, Any]:
    """One job: the agent loop, the judge, the status, and what it cost."""
    before, credits, started = client.calls, client.credits, time.monotonic()
    job = Job(result, rows, cps, files, rules, client, calls)

    def fresh(cp_id: str) -> dict[str, Any] | None:
        try:
            return event_check(client, calls, result, cp_id, cps, rules)
        except ToolError as error:
            job.sie_failures.append(f"the judge's event check: {error}")
            return None

    if any(row.kind == "sie_failed" for row in rows):
        agent = AgentResult(stopped="an SIE call failed in the workflow, and again on its retry")
        job.sie_failures.append(agent.stopped)
    else:
        agent = run_agent(claude, job, fresh)
    status = status_of(
        agent.finding, agent.problems, agent.checks, job, agent.stopped, agent.ran_out
    )
    usd, unpriced = claude_usd(agent, rules["agent"]["prices"])
    spent = client.credits - credits
    return {
        "article_id": result.article.id,
        "headline": result.article.headline,
        "status": status,
        "rows": [asdict(row) for row in rows],
        "finding": agent.finding,
        "problems": agent.problems,
        "first_review": agent.first_review,
        "judge_event_checks": agent.checks,
        "credit_files_read": job.read_files,
        "sie_failures": job.sie_failures,
        "stopped": agent.stopped,
        "tool_calls": agent.tool_calls,
        "revisions": agent.revisions,
        "cost": {
            "input_tokens": agent.input_tokens,
            "cache_write_tokens": agent.cache_write_tokens,
            "cache_read_tokens": agent.cache_read_tokens,
            "output_tokens": agent.output_tokens,
            "claude_usd": round(usd, 4),
            "claude_models": agent.by_model,
            "unpriced_models": unpriced,
            "sie_calls": client.calls - before,
            "sie_credits": spent,
            "sie_usd": spent * rules["agent"]["sie_credit_usd"],
            "seconds": round(time.monotonic() - started, 1),
        },
        "transcript": agent.transcript,
    }


def md_safe(text: Any) -> str:
    """Text from an article, a file, a tool or Claude, safe on one Markdown line: every mark
    Markdown or HTML could act on is escaped, and line breaks become spaces."""
    return MD_SPECIAL.sub(r"\\\1", " ".join(str(text).split()))


def fenced(text: str) -> list[str]:
    """A free-text body in a fenced block whose fence is longer than any backtick run in it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}text", text, fence]


def notes_markdown(jobs: list[dict[str, Any]], workflow: dict[str, Any]) -> str:
    """The analyst's morning read: one section per article the agent looked at."""
    lines = [
        "# Review queue: what the overnight agent found",
        "",
        "Every row below is a suggestion for the credit analyst. Nothing was sent to a credit "
        "officer or changed on a loan file; forward anything yourself after you've checked it.",
        "",
        f"The workflow read {workflow['articles']} articles; {len(jobs)} went to the agent.",
        "",
    ]
    for job in jobs:
        lines += [
            f"## {md_safe(job['article_id'])}: {md_safe(job['headline'])}",
            "",
            f"**Status:** `{job['status']}`",
        ]
        if job["stopped"]:
            lines += ["", f"**Stopped because:** {md_safe(job['stopped'])}"]
        for failure in job["sie_failures"]:
            lines += ["", f"**SIE call failed:** {md_safe(failure)}"]
        lines += ["", "**Review rows:**", ""]
        lines += [
            f"{row['number']}. {md_safe(row['mention'])}: {md_safe(row['reason'])}"
            for row in job["rows"]
        ]
        finding = job["finding"]
        if finding:
            lines += ["", "**Agent's note:**", "", *fenced(finding["note"]), ""]
            for item in finding["items"]:
                pick = f" {md_safe(item['counterparty_id'])}" if item["counterparty_id"] else ""
                event = f", {item['event']}" if item["event"] else ""
                if item["event"] and item["score"] is not None:
                    event += f" {item['score']}"
                lines.append(f"- Row {item['row']}: `{item['outcome']}`{pick}{event}")
                lines += [
                    f'  - {md_safe(s["source"])}: "{md_safe(s["quote"])}"'
                    for s in item["statements"]
                ]
        for cp_id, check in job["judge_event_checks"].items():
            lines += [
                "",
                f"**Judge's own event check, link fixed to {md_safe(cp_id)}:** {check['status']}, "
                f"{check['event']} {check['score']:.2f}",
            ]
        if job["problems"]:
            lines += ["", "**Check these before acting:**", ""]
            lines += [f"- {md_safe(problem)}" for problem in job["problems"]]
        if job["first_review"]:
            lines += ["", "**The check sent the first finding back for these:**", ""]
            lines += [f"- {md_safe(problem)}" for problem in job["first_review"]]
        lines.append("")
    return "\n".join(lines)


def queue_rows(
    results: list[recipe.Result], jobs: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """The workflow's review.csv rows, with the agent's status, outcome and note added."""
    rows = []
    for result in results:
        job = jobs.get(result.article.id)
        for number, row in enumerate(recipe.review_rows([result]), start=1):
            finding = job["finding"] if job else None
            item = next((i for i in (finding or {}).get("items", []) if i["row"] == number), None)
            outcome = ""
            if item:
                outcome = item["outcome"] + (
                    f" {item['counterparty_id']}" if item["counterparty_id"] else ""
                )
            rows.append(
                {
                    **row,
                    "agent_status": job["status"] if job else "",
                    "agent_outcome": outcome,
                    "agent_note": finding["note"] if finding else "",
                }
            )
    return rows


def write_outputs(
    output: Path,
    results: list[recipe.Result],
    jobs: list[dict[str, Any]],
    workflow: dict[str, Any],
) -> None:
    folder = output / "agent"
    (folder / "jobs").mkdir(parents=True, exist_ok=True)
    (folder / "transcripts").mkdir(exist_ok=True)
    for job in jobs:
        record = {key: value for key, value in job.items() if key != "transcript"}
        (folder / "jobs" / f"{job['article_id']}.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (folder / "transcripts" / f"{job['article_id']}.json").write_text(
            json.dumps(job["transcript"], indent=1, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
    by_id = {job["article_id"]: job for job in jobs}
    with (folder / "review.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=QUEUE_FIELDS)
        writer.writeheader()
        for row in queue_rows(results, by_id):
            writer.writerow({key: recipe.csv_safe(row[key]) for key in QUEUE_FIELDS})
    (folder / "review_notes.md").write_text(notes_markdown(jobs, workflow), encoding="utf-8")
    cost = {
        "workflow": workflow,
        "jobs": {job["article_id"]: {**job["cost"], "status": job["status"]} for job in jobs},
    }
    (folder / "cost.json").write_text(json.dumps(cost, indent=2) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> None:
    rules = recipe.load_rules()
    sie = recipe.make_client()
    claude = make_claude()
    client = CountingClient(sie)
    cps, files = CounterpartyList(), CreditFiles()
    output = args.output or recipe.RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    with tempfile.TemporaryDirectory() as scratch:
        results, workflow = run_workflow(client, rules, QUEUES, Path(scratch))
    print(
        f"workflow: {workflow['articles']} articles, {workflow['sie_calls']} SIE calls, "
        f"{workflow['seconds']} s"
    )
    for r in results:
        print(f"  {r.article.id}  {r.status}")
    todo = [(r, review_items(r, cps, rules)) for r in results]
    todo = [(r, rows) for r, rows in todo if rows]
    if args.article:
        todo = [(r, rows) for r, rows in todo if r.article.id == args.article]
        if not todo:
            raise SystemExit(f"Stopped: {args.article} has no review rows on this run.")
    calls = recipe.CallLog()  # the agent's own SIE calls: check_event_for and the judge's
    jobs = []
    for result, rows in todo:
        job = answer(claude, result, rows, cps, files, rules, client, calls)
        cost = job["cost"]
        print(
            f"{job['article_id']}: {job['status']} ({job['tool_calls']} tool calls, "
            f"{job['revisions']} revisions, {cost['sie_calls']} SIE calls, "
            f"${cost['claude_usd']:.4f} Claude, {cost['seconds']} s)"
        )
        jobs.append(job)
    calls.stop_if_all_failed()
    write_outputs(output, results, jobs, workflow)
    print(f"Wrote {output / 'agent'}. Nothing was sent to a credit officer; the analyst decides.")


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--article", help="run the agent on one article, such as N-04")
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    import anthropic

    try:
        recipe.stop_on_refusal(lambda: run(args))
    except anthropic.APIError as error:  # transient errors end one job; these stop the run
        raise SystemExit(
            f"{error}\nStopped: the Claude API failed with {type(error).__name__}, which a "
            "retry won't fix. Check ANTHROPIC_API_KEY, the model name in rules.yaml and your "
            "Anthropic account. No outputs were written."
        ) from None


if __name__ == "__main__":
    main()
