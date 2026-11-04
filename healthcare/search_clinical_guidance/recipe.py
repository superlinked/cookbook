"""Search a clinic's own guidance and put the passage that answers first.

Use case:  The search box clinicians, nurses and medical assistants use between patients to find
           the clinic's own protocol for a question.
Who:       The people searching; and the clinical policy coordinator, who reads the gap log.
Problem:   Keyword search puts the passages that repeat the query's words first, so a retired
           edition can sit above the current one and the adult version comes up for a child.
           What's current and who a document is for are facts the document system already has;
           matching the question to the passage that answers it is the part that needs a model.
Input:     The question and the patient population (data/searches.json for the sample run, or
           the command line), the passage text (data/passages.jsonl, what the intranet search
           indexes) and the document system (document_system.py, seed
           data/document_system.csv: section, title, population, status, version, replaced_by).
SIE pipeline (one model, up to two calls per search):
           score  Qwen/Qwen3-Reranker-4B  every passage against the question
           (Python)                       current passages for this population at or above
                                          min_score are results; a retired passage that scores
                                          above, or within notice_margin log-odds of, the best
                                          of them raises a replaced or withdrawn notice; with
                                          no result, an other-population passage
                                          that clears the bar is shown below a line
           score  Qwen/Qwen3-Reranker-4B  the sentences of what is shown, to pick out the
                                          sentence to read first (the full passage is shown too)
Output:    One search prints its page. With no question, the sample run writes runs/<timestamp>/:
           a results page per search (pages/), results.json, summary.md and gap_log.csv for the
           clinical policy coordinator.
Why SIE:   The reranker runs on SIE Serverless; the filters and notices are plain Python below, and
           no generative model writes anything: the clinician reads the clinic's own words.

    uv run recipe.py                                                   # the sample searches
    uv run recipe.py "asthma action plan how often" --population pediatric   # one search
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import yaml
from document_system import Document, DocumentSystem
from dotenv import load_dotenv
from keyword_search import KeywordSearch
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
SEARCHES_PATH = ROOT / "data" / "searches.json"
PASSAGES_PATH = ROOT / "data" / "passages.jsonl"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
# How much of an SIE error message goes into a page's reason; a stop message keeps all of it.
ERROR_CHARS = 200

# What the search found. None of them is a clinical decision: the clinician reads the passage
# and decides, and the clinical policy coordinator decides what guidance to write.
STATUSES = ("results", "no_good_match", "not_reranked")
# Notices a page can carry, below the results.
NOTICES = ("replaced", "withdrawn", "other_population")
# A search with one of these notices, or with no_good_match, goes on the gap log.
LOGGED_NOTICES = ("withdrawn", "other_population")

# A cell starting with one of these is a formula to a spreadsheet; csv_safe defuses it.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
GAP_FIELDS = (
    "search_id",
    "population",
    "question",
    "status",
    "notices",
    "closest_passage",
    "closest_score",
    "reason",
)


@dataclass(frozen=True)
class Passage:
    """A passage the intranet search indexes, joined with its document system record."""

    doc: Document
    text: str

    @property
    def doc_id(self) -> str:
        return self.doc.doc_id

    def rerank_text(self) -> str:
        """What the reranker reads: section, title and the passage."""
        return f"{self.doc.section}: {self.doc.title}\n{self.text}"


@dataclass(frozen=True)
class Search:
    search_id: str
    population: str
    question: str


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def check_search(question: str, population: str, rules: dict[str, Any]) -> tuple[str, str]:
    """The question and population cleaned up; an empty question or a population rules.yaml
    doesn't list raises an error before any SIE call."""
    population = " ".join(population.split()).lower()
    if population not in rules["population"]["eligible"]:
        known = ", ".join(rules["population"]["eligible"])
        raise ValueError(f"population {population!r} is not in rules.yaml (known: {known})")
    question = " ".join(question.split())
    if not question:
        raise ValueError("the question is empty")
    return question, population


def load_searches(rules: dict[str, Any], path: Path = SEARCHES_PATH) -> list[Search]:
    searches = []
    for entry in json.loads(path.read_text(encoding="utf-8"))["searches"]:
        question, population = check_search(entry["question"], entry["population"], rules)
        searches.append(Search(entry["id"], population, question))
    return searches


def load_passages(
    documents: DocumentSystem, path: Path = PASSAGES_PATH
) -> tuple[list[Passage], list[str]]:
    """Every indexed passage with its document record, and the IDs the document system doesn't
    have. Those are never scored or shown: without a record, status and population are unknown."""
    texts: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry["doc_id"] in texts:
            raise ValueError(f"{path.name}: {entry['doc_id']} is listed twice")
        texts[entry["doc_id"]] = entry["text"]
    records = documents.get_documents(list(texts))
    passages = [Passage(records[i], text) for i, text in texts.items() if i in records]
    return passages, [i for i in texts if i not in records]


def make_client() -> SIEClient:
    """Build an SIE client from the API key and SIE_BASE_URL in the environment or `.env`."""
    load_dotenv()
    api_key = os.environ.get("SIE_API_KEY", "").strip()
    base_url = os.environ.get("SIE_BASE_URL", "").strip()
    if not api_key and not base_url:
        raise RuntimeError("Set SIE_API_KEY, or SIE_BASE_URL for SIE Cluster or SIE Local, in .env")
    base_url = base_url or "https://api.superlinked.com"
    return SIEClient(api_key=api_key or None, base_url=base_url, timeout_s=WAIT_S)


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
    those searches fall back to keyword order."""
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
    """The error for a page's reason or the summary line, its message cut short."""
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
        """No call succeeded, so nothing was reranked: stop instead of writing pages."""
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
    a rate limit, a reply without a score for every item we sent) is logged and the run
    continues: a failed passage call puts that search in keyword order, a failed sentence call
    leaves the page without a picked sentence. A refusal every call would repeat (see
    `is_fatal`) stops the run. An error that isn't an SIE error is a bug in this code and is
    raised as is.
    """
    calls.made += 1
    try:
        return work(), ""
    except SIEError as error:
        if is_fatal(error):
            raise SIEStopped(stop_message(error, model)) from error
        calls.failures.append((model, error))
        return None, f"the SIE call failed: {short(error)}"


def parse_scores(reply: Any, sent: list[str]) -> dict[str, float]:
    """The score per item ID. A reply with no `scores`, or without a numeric score for every
    item we sent, raises SIEError, so it takes the transient path in `guarded`: a page can't be
    ordered on scores it doesn't have. An empty `scores` list for an empty request is fine."""
    entries = reply.get("scores") if isinstance(reply, dict) else None
    if not isinstance(entries, list):
        raise SIEError("the reply has no `scores`")
    scores: dict[str, float] = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("item_id") not in sent:
            continue
        try:
            value = float(entry.get("score"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            value = math.nan
        if not math.isfinite(value):
            raise SIEError(f"the reply's score for {entry['item_id']} is not a number")
        scores[str(entry["item_id"])] = value
    missing = [item for item in sent if item not in scores]
    if missing:
        raise SIEError(f"the reply has no score for {len(missing)} of {len(sent)} items")
    return scores


# --- Sentences --------------------------------------------------------------------------------

# A sentence ends at ". ", "! " or "? " followed by a capital or a digit: "6.0 mmol/L" and
# "38.0 C" stay whole.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


def split_sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_END.split(" ".join(text.split())) if part.strip()]


# --- The page (plain Python) ------------------------------------------------------------------


def classify(doc: Document, population: str, rules: dict[str, Any]) -> str:
    """What a document can be for a search about `population`: `eligible` (may be a result),
    `retired` (may only raise a notice), `other_population` (current, written for another
    population) or `ignored` (any other status, or retired for another population)."""
    status = rules["status"]
    right_population = doc.population in rules["population"]["eligible"][population]
    if doc.status == status["shown"]:
        return "eligible" if right_population else "other_population"
    if doc.status == status["retired"] and right_population:
        return "retired"
    return "ignored"


def entry(passage: Passage, score: float | None) -> dict[str, Any]:
    doc = passage.doc
    row: dict[str, Any] = {
        "id": doc.doc_id,
        "title": doc.title,
        "section": doc.section,
        "population": doc.population,
        "version": doc.version,
        "link": doc.link(),
        "text": passage.text,
    }
    if score is not None:
        row["score"] = round(score, 4)
    return row


def ranked(
    passages: list[Passage],
    scores: dict[str, float],
    kind: str,
    search: Search,
    rules: dict[str, Any],
) -> list[tuple[Passage, float]]:
    """The passages of one kind, best score first; ties go by document ID."""
    picked = [
        (p, scores[p.doc_id]) for p in passages if classify(p.doc, search.population, rules) == kind
    ]
    return sorted(picked, key=lambda pair: (-pair[1], pair[0].doc_id))


def log_odds(score: float) -> float:
    """The reranker's score as log-odds, so scores near 1 are as far apart as they really are."""
    clipped = min(max(score, 1e-6), 1 - 1e-6)
    return math.log(clipped / (1 - clipped))


def close_to_best(score: float, best_current: float | None, margin: float) -> bool:
    """A retired passage raises a notice when it scores above, or within `margin` log-odds of,
    the best current passage: a hair's difference between two editions isn't evidence that the
    clinician meant the current one."""
    return best_current is None or log_odds(score) >= log_odds(best_current) - margin


def retired_notice(
    passage: Passage, score: float, search: Search, documents: DocumentSystem, rules: dict[str, Any]
) -> dict[str, Any]:
    """A replaced notice when the chain of replacements ends at a current document; withdrawn
    when it doesn't (no replacement, a missing ID, a loop)."""
    found = documents.find_replacement(passage.doc_id, rules["status"]["shown"])
    notice: dict[str, Any] = {"kind": "withdrawn", **entry(passage, score)}
    if found.current is None:
        notice["detail"] = found.problem
        return notice
    current = found.current
    notice["kind"] = "replaced"
    notice["replaced_by"] = {
        "id": current.doc_id,
        "title": current.title,
        "version": current.version,
        "population": current.population,
        "link": current.link(),
        "chain": list(found.chain),
    }
    if current.population not in rules["population"]["eligible"][search.population]:
        notice["detail"] = f"the replacement is written for {current.population} patients"
    return notice


def build_page(
    search: Search,
    passages: list[Passage],
    scores: dict[str, float],
    documents: DocumentSystem,
    rules: dict[str, Any],
) -> dict[str, Any]:
    """Results, notices and the status from the passage scores. Retired passages are never
    results, and an other-population passage only goes below the line when nothing for the
    right population clears min_score."""
    min_score = rules["rerank"]["min_score"]
    shown = rules["rerank"]["max_results"]
    margin = rules["rerank"]["notice_margin"]
    eligible = ranked(passages, scores, "eligible", search, rules)
    results = [entry(p, s) for p, s in eligible if s >= min_score][:shown]
    best_current = eligible[0][1] if eligible else None
    notices: list[dict[str, Any]] = []
    for passage, score in ranked(passages, scores, "retired", search, rules):
        if score >= min_score and close_to_best(score, best_current, margin):
            notices.append(retired_notice(passage, score, search, documents, rules))
    below_line: list[dict[str, Any]] = []
    if not results:
        others = ranked(passages, scores, "other_population", search, rules)
        below_line = [entry(p, s) for p, s in others if s >= min_score][:shown]
        if below_line:
            notices.append(
                {
                    "kind": "other_population",
                    "ids": [row["id"] for row in below_line],
                    "detail": f"only guidance for other populations clears {min_score:.2f}",
                }
            )
    page: dict[str, Any] = {
        "search": search.question,
        "population": search.population,
        "results": results,
        "below_line": below_line,
        "notices": notices,
        "closest_current": entry(*eligible[0]) if eligible else None,
        "scores": {p.doc_id: round(scores[p.doc_id], 4) for p in passages},
    }
    if results:
        page["status"] = "results"
        page["reason"] = (
            f"{len(results)} current passage{'s' if len(results) > 1 else ''} for "
            f"{search.population} patients scored {min_score:.2f} or more."
        )
    else:
        best = (
            f"the closest current passage, {eligible[0][0].doc_id}, scored {eligible[0][1]:.2f}"
            if eligible
            else f"no current passage is written for {search.population} patients"
        )
        page["status"] = "no_good_match"
        page["reason"] = (
            f"No good match: {best}, below {min_score:.2f}. {rules['no_good_match']['message']}"
        )
    return page


def keyword_page(
    search: Search,
    passages: list[Passage],
    engine: KeywordSearch,
    failed: str,
    rules: dict[str, Any],
) -> dict[str, Any]:
    """The fallback when the passage call fails: current passages for this population in the
    intranet's keyword order. Without scores no notice can be decided, and the page says so."""
    eligible = {
        p.doc_id: p for p in passages if classify(p.doc, search.population, rules) == "eligible"
    }
    hits = [(eligible[pid], s) for pid, s in engine.search(search.question) if pid in eligible]
    results = [{**entry(p, None), "keyword_score": round(s, 3)} for p, s in hits]
    results = results[: rules["rerank"]["max_results"]]
    found = (
        f"The results are current passages for {search.population} patients in keyword order"
        if results
        else f"No current passage for {search.population} patients shares a word with the search"
    )
    return {
        "search": search.question,
        "population": search.population,
        "results": results,
        "below_line": [],
        "notices": [],
        "closest_current": None,
        "scores": {},
        "status": "not_reranked",
        "reason": (
            f"Not reranked, {failed}. {found}, with the status and population filters applied. "
            "Retired and other-population documents were not checked, so this page has no notices."
        ),
    }


def add_sentences(page: dict[str, Any], scores: dict[str, float] | None, failed: str) -> None:
    """Pick each shown passage's best-scoring sentence. With no scores, say why."""
    for row in [*page["results"], *page["below_line"]]:
        sentences = split_sentences(row["text"])
        if scores is None:
            row["sentence"] = None
            continue
        best = max(range(len(sentences)), key=lambda n: (scores[f"{row['id']}:{n}"], -n))
        row["sentence"] = {
            "text": sentences[best],
            "score": round(scores[f"{row['id']}:{best}"], 4),
        }
    if failed:
        page["sentence_note"] = f"Best sentence not picked, {failed}. Read the full passage."


# --- The search endpoint ----------------------------------------------------------------------


class GuidanceSearch:
    """The search behind the intranet box. `search(question, population)` is the function the
    service would call on every request; everything it needs is built once here."""

    def __init__(
        self,
        client: SIEClient,
        rules: dict[str, Any],
        documents: DocumentSystem,
        passages: list[Passage],
        calls: CallLog | None = None,
    ) -> None:
        self.client = client
        self.rules = rules
        self.documents = documents
        self.passages = passages
        self.calls = calls or CallLog()
        settings = rules["keyword_search"]
        self.engine = KeywordSearch(
            {p.doc_id: f"{p.doc.title} {p.text}" for p in passages}, settings["k1"], settings["b"]
        )

    def score(
        self, question: str, items: dict[str, str], instruction: str
    ) -> tuple[dict[str, float] | None, str]:
        """One reranker call over `items` (ID to text), under the SIE error rule."""
        model = self.rules["models"]["rerank"]
        sent = list(items)

        def call() -> dict[str, float]:
            reply: Any = self.client.score(
                model,
                Item(id="question", text=question),
                [Item(id=item_id, text=text) for item_id, text in items.items()],
                instruction=instruction,
                provision_timeout_s=WAIT_S,
            )
            return parse_scores(reply, sent)

        return guarded(self.calls, model, call)

    def search(self, search: Search) -> dict[str, Any]:
        """The page one clinician sees for this question and population."""
        scores, failed = self.score(
            search.question,
            {p.doc_id: p.rerank_text() for p in self.passages},
            self.rules["rerank"]["instruction"],
        )
        if scores is None:
            page = keyword_page(search, self.passages, self.engine, failed, self.rules)
            add_sentences(page, None, "")
        else:
            page = build_page(search, self.passages, scores, self.documents, self.rules)
            items = {
                f"{row['id']}:{n}": f"{row['section']}: {row['title']}\n{sentence}"
                for row in [*page["results"], *page["below_line"]]
                for n, sentence in enumerate(split_sentences(row["text"]))
            }
            if items:
                sentence_scores, failed = self.score(
                    search.question, items, self.rules["sentences"]["instruction"]
                )
                add_sentences(page, sentence_scores, failed)
        page["keyword_first"] = [pid for pid, _ in self.engine.search(search.question)[:3]]
        return page


def gap_line(search_id: str, page: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any] | None:
    """The gap log line for a search with no good match or a withdrawn or other-population
    notice; None for any other search (a not_reranked search wasn't checked)."""
    kinds = [notice["kind"] for notice in page["notices"]]
    if page["status"] != "no_good_match" and not set(kinds) & set(LOGGED_NOTICES):
        return None
    closest = page["closest_current"] or {}
    return {
        "search_id": search_id,
        "population": page["population"],
        "question": page["search"],
        "status": page["status"],
        "notices": " ".join(dict.fromkeys(kinds)),
        "closest_passage": closest.get("id", ""),
        "closest_score": closest.get("score", ""),
        "reason": " ".join([page["reason"], *(notice_text(n, page) for n in page["notices"])]),
    }


# --- Run --------------------------------------------------------------------------------------


def run(
    client: SIEClient,
    rules: dict[str, Any],
    output_dir: Path | None,
    searches_path: Path = SEARCHES_PATH,
    documents: DocumentSystem | None = None,
    passages_path: Path = PASSAGES_PATH,
) -> tuple[list[tuple[str, dict[str, Any]]], list[dict[str, Any]], list[str]]:
    """Run every sample search and build the gap log.

    Nothing is written until every search is done, so a run that stops on an SIE refusal or on
    every call failing leaves no partial pages behind.
    """
    searches = load_searches(rules, searches_path)
    if not searches:
        raise SystemExit(f"No searches in {searches_path}, so there is nothing to run.")
    documents = documents or DocumentSystem()
    passages, unlisted = load_passages(documents, passages_path)
    if unlisted:
        print(f"Not in the document system, never shown: {', '.join(unlisted)}")
    calls = CallLog()
    endpoint = GuidanceSearch(client, rules, documents, passages, calls)
    pages = [(s.search_id, endpoint.search(s)) for s in searches]
    calls.stop_if_all_failed()
    if calls.failures:
        print(
            f"{len(calls.failures)} of {calls.made} SIE calls failed; those pages say so. "
            f"First: {short(calls.failures[0][1])}"
        )
    gaps = [line for sid, page in pages if (line := gap_line(sid, page, rules)) is not None]
    if output_dir is not None:
        write_outputs(output_dir, pages, gaps, unlisted, rules)
    return pages, gaps, unlisted


def search_once(
    client: SIEClient, rules: dict[str, Any], question: str, population: str
) -> dict[str, Any]:
    """One search from the command line, under the same SIE error rule as the sample run."""
    question, population = check_search(question, population, rules)
    documents = DocumentSystem()
    passages, _ = load_passages(documents)
    calls = CallLog()
    page = GuidanceSearch(client, rules, documents, passages, calls).search(
        Search("search", population, question)
    )
    calls.stop_if_all_failed()
    return page


def csv_safe(value: Any) -> str:
    """A leading ' on a cell a spreadsheet would run as a formula; anything else as written."""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def notice_text(notice: dict[str, Any], page: dict[str, Any]) -> str:
    """The notice as the page states it."""
    if notice["kind"] == "other_population":
        return (
            f"Other population only: nothing written for {page['population']} patients clears "
            f"the bar; {', '.join(notice['ids'])} below the line is written for another "
            "population."
        )
    what = (
        f"{notice['id']} {notice['title']!r}, version {notice['version']} (retired, score "
        f"{notice['score']})"
    )
    if notice["kind"] == "replaced":
        new = notice["replaced_by"]
        extra = f"; {notice['detail']}" if notice.get("detail") else ""
        return (
            f"Replaced: {what} matched about as well as or better than the best current passage. "
            f"It is not shown; it was replaced by {new['id']} {new['title']!r}, "
            f"version {new['version']}, {new['link']}{extra}."
        )
    return (
        f"Withdrawn: {what} matched about as well as or better than the best current passage. "
        f"It is not shown, and nothing current replaces it ({notice['detail']})."
    )


def passage_lines(n: int | str, row: dict[str, Any]) -> list[str]:
    score = f" Score {row['score']}." if "score" in row else ""
    if "keyword_score" in row:
        score = f" Keyword score {row['keyword_score']}."
    lines = [
        f"### {n}. {row['title']} ({row['id']})",
        "",
        f"{row['section']}, version {row['version']}, written for {row['population']} "
        f"patients.{score}  ",
        f"{row['link']}",
        "",
    ]
    if row.get("sentence"):
        lines += [
            f"Best sentence (score {row['sentence']['score']}):",
            "",
            f"> {row['sentence']['text']}",
            "",
        ]
    lines += ["Full passage:", "", f"> {row['text']}", ""]
    return lines


def render_page(search_id: str, page: dict[str, Any]) -> str:
    """The results page a clinician sees for one search."""
    lines = [
        f"# {search_id}: {page['status']}",
        "",
        f"Search: {page['search']!r}  ",
        f"Population: {page['population']}",
        "",
        page["reason"],
        "",
    ]
    if page.get("sentence_note"):
        lines += [page["sentence_note"], ""]
    if page["results"]:
        lines += ["## Results", ""]
        for n, row in enumerate(page["results"], 1):
            lines += passage_lines(n, row)
    if page["below_line"]:
        lines += [
            "---",
            "",
            f"## Below the line: not written for {page['population']} patients",
            "",
        ]
        for n, row in enumerate(page["below_line"], 1):
            lines += passage_lines(n, row)
    if page["notices"]:
        lines += ["## Notices", ""]
        lines += [f"- {notice_text(notice, page)}" for notice in page["notices"]]
        lines.append("")
    lines.append(
        "The clinic's own text, found by search. It is not advice for this patient: read the "
        "full passage and use your clinical judgment."
    )
    return "\n".join(lines) + "\n"


def md_cell(value: Any) -> str:
    """Text for a Markdown table cell: pipes escaped, line breaks flattened."""
    return " ".join(str(value).split()).replace("|", "\\|")


def render_summary(
    pages: list[tuple[str, dict[str, Any]]], gaps: list[dict[str, Any]], unlisted: list[str]
) -> str:
    lines = [
        "# Sample searches",
        "",
        "One results page per search in pages/. The gap log is gap_log.csv.",
        "",
        "| Search | Population | Status | Results | Notices | Keyword search's first three |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for search_id, page in pages:
        results = " ".join(row["id"] for row in page["results"]) or "-"
        below = " ".join(row["id"] for row in page["below_line"])
        if below:
            results += f" (below the line: {below})"
        notices = []
        for notice in page["notices"]:
            if notice["kind"] == "other_population":
                notices.append("other_population")
            else:
                notices.append(f"{notice['kind']} {notice['id']}")
        lines.append(
            f"| [{search_id}](pages/{search_id}.md) | {page['population']} | {page['status']} | "
            f"{results} | {', '.join(notices) or '-'} | {' '.join(page['keyword_first']) or '-'} |"
        )
    lines += ["", f"## Gap log ({len(gaps)} searches)", ""]
    lines += [f"- {line['search_id']}: {md_cell(line['reason'])}" for line in gaps]
    if not gaps:
        lines.append("Nothing: every search found current guidance for its population.")
    if unlisted:
        lines += ["", f"Passages not in the document system, never shown: {', '.join(unlisted)}"]
    return "\n".join(lines) + "\n"


def write_outputs(
    output_dir: Path,
    pages: list[tuple[str, dict[str, Any]]],
    gaps: list[dict[str, Any]],
    unlisted: list[str],
    rules: dict[str, Any],
) -> None:
    (output_dir / "pages").mkdir(parents=True, exist_ok=True)
    for search_id, page in pages:
        (output_dir / "pages" / f"{search_id}.md").write_text(
            render_page(search_id, page), encoding="utf-8"
        )
    results = [{"id": search_id, **page} for search_id, page in pages]
    (output_dir / "results.json").write_text(
        json.dumps(
            {"searches": results, "gap_log": gaps, "not_in_document_system": unlisted},
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (output_dir / "summary.md").write_text(render_summary(pages, gaps, unlisted), encoding="utf-8")
    with (output_dir / "gap_log.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=(*GAP_FIELDS, "owner"))
        writer.writeheader()
        for line in gaps:
            row = {**line, "owner": rules["gap_log"]["owner"]}
            writer.writerow({key: csv_safe(row[key]) for key in (*GAP_FIELDS, "owner")})


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("question", nargs="?", help="one search; leave out to run the samples")
    parser.add_argument("--population", default="adult", help="adult or pediatric (rules.yaml)")
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    rules = load_rules()
    if args.question is not None:
        with make_client() as client:
            page = stop_on_refusal(
                lambda: search_once(client, rules, args.question, args.population)
            )
        print(render_page("search", page))
        return
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    with make_client() as client:
        pages, gaps, _ = stop_on_refusal(lambda: run(client, rules, output))
    for search_id, page in pages:
        print(f"{search_id}  {page['status']:<13} {page['reason']}")
        for notice in page["notices"]:
            print(f"      {notice_text(notice, page)}")
    print(f"\n{len(gaps)} searches on the gap log for the {rules['gap_log']['owner']}.")
    print(f"outputs: {output / 'summary.md'}, pages/, results.json, gap_log.csv")


if __name__ == "__main__":
    main()
