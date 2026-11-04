"""Find the malware and tools that groups targeting your sectors use and no detection rule covers.

Use case:  Detection gaps: which attacker tools, used by groups that target your sectors, does
           your detection rule catalog not cover yet?
Who:       Detection engineers choosing the next rules to write, and the CTI analysts who read
           the threat reports those choices rest on.
Problem:   The claims sit in report sentences, under each vendor's own names ("Gorse Moth" is
           "Velvet Gorse" in our alias table), sometimes hedged ("possibly") and sometimes denied
           ("we found no evidence that..."). The rule catalog and your sectors are structured;
           the reports aren't.
Input:     data/reports.jsonl (synthetic threat reports from fictional publishers about fictional
           actors and malware), data/detection_rules.csv (the rule catalog, read through
           rule_catalog.py) and rules.yaml (your sectors, labels, alias table, relation type
           rules, doubt words (negations and hedges), thresholds).
SIE pipeline:
           (Python)                                  split each report into paragraphs
           extract  knowledgator/gliner-relex-large-v1.0
                                                     entities plus uses / exploits / targets /
                                                     attributed to / alias of relations; one
                                                     call per batch of paragraphs
           (Python)                                  check spans; merge names by the alias table;
                                                     check relation types (turn round, mark a
                                                     direction the types can't vouch for, or
                                                     drop); a doubt word in the claim's
                                                     sentence holds the claim back; build the
                                                     graph with every source and a status per
                                                     edge; pick the groups that target your
                                                     sectors and every tool they use; look each
                                                     tool up in the rule catalog by canonical
                                                     name and alias
Output:    backlog.csv and backlog.json (one row per malware family or tool: no_rule,
           rule_exists or needs_review, with the groups, sentences and publishers), graph.json
           (nodes, edges with sources and status, dropped relations, review items, unread
           reports) and graph.mmd (Mermaid). The detection engineers decide what to build.
Why SIE:   One model finds entities and relations together. Otherwise you'd need an NER model
           plus a relation classifier, or an LLM per sentence. No generative model makes a
           decision: merging names, checking types, holding back doubtful claims and comparing
           with the catalog is plain Python you can read.
Models on: SIE Serverless, or your own SIE with SIE_BASE_URL.

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
from typing import Any, Final, Literal, cast

import yaml
from dotenv import load_dotenv
from rule_catalog import RuleCatalog
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
REPORTS_PATH = ROOT / "data" / "reports.jsonl"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
# A failed SIE call's message is cut to this many characters in review items and the summary.
ERROR_CHARS = 200

ALIAS_OF = "alias of"
# multi_publisher means at least min_sources distinct publisher names. It is not independent
# evidence: two vendors quoting one original report pass too.
MULTI_PUBLISHER, PROVISIONAL, SINGLE_SOURCE, REVIEW = (
    "multi_publisher",
    "provisional",  # would be multi_publisher, but a report was partly unread
    "single_source",
    "review",
)
TURNED: Final = "turned round to fit the relation's types"
# Both ends' types fit the relation either way round (malware uses malware), so the types can't
# tell whether the model read the sentence the right way round, or two reports that disagree on
# direction would each look fine.
EITHER_WAY: Final = "direction unverifiable: the types fit either way round"
# A claim's direction note: "", TURNED or EITHER_WAY. Literal can't name the constants, so the
# text is repeated here; mypy fails if the two drift apart.
DirectionNote = Literal[
    "",
    "turned round to fit the relation's types",
    "direction unverifiable: the types fit either way round",
]
# Backlog status: what the comparison found. The detection engineers decide what to build.
NO_RULE, NEEDS_REVIEW, RULE_EXISTS = "no_rule", "needs_review", "rule_exists"
STATUSES = (NO_RULE, NEEDS_REVIEW, RULE_EXISTS)  # the order rows are written in
BACKLOG_FIELDS = (
    *("tool", "type", "status", "rules", "groups", "sectors", "publishers", "reports"),
    *("sentences", "held_back", "notes"),
)
# A spreadsheet runs a cell that starts with one of these as a formula.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


# --- Inputs -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Report:
    id: str
    publisher: str
    published: str
    title: str
    text: str


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


def load_reports(path: Path = REPORTS_PATH) -> list[Report]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [Report(**json.loads(line)) for line in lines if line.strip()]


# --- 1. Windows -------------------------------------------------------------------------------


def sentences(text: str) -> list[tuple[int, int]]:
    """(start, end) of each sentence: text up to a ., ! or ? followed by space or the end."""
    return [m.span() for m in re.finditer(r"\S.*?(?:[.!?](?=\s|$)|$)", text, re.DOTALL)]


def paragraph_windows(text: str, max_words: int) -> list[tuple[int, str]]:
    """Split a report into (offset, window) pairs: one per paragraph, long ones split on sentences.

    The model only links two names inside the same window, so a claim has to fit in one. A
    single sentence longer than max_words is cut on words.
    """
    windows: list[tuple[int, str]] = []
    for para in re.finditer(r"[^\n]+(?:\n(?!\s*\n)[^\n]+)*", text):
        start, body = para.start(), para.group()
        if len(body.split()) <= max_words:
            windows.append((start, body))
            continue
        pieces: list[tuple[int, int]] = []
        for s, e in sentences(body):
            words = list(re.finditer(r"\S+", body[s:e]))
            for i in range(0, len(words), max_words):
                chunk = words[i : i + max_words]
                pieces.append((s + chunk[0].start(), s + chunk[-1].end()))
        packed: list[tuple[int, int]] = []
        for s, e in pieces:
            if packed and len(body[packed[-1][0] : e].split()) <= max_words:
                packed[-1] = (packed[-1][0], e)
            else:
                packed.append((s, e))
        windows += [(start + s, body[s:e]) for s, e in packed]
    return windows


# --- 2. Extract entities and relations --------------------------------------------------------


def extract_windows(
    client: SIEClient, items: list[tuple[str, str]], rules: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Entities and relations per window id, in batches.

    A window with nothing in it comes back as empty lists, not an error. A call that fails for
    a reason that may pass (a lost connection, a timeout, a server error, a rate limit), or a
    reply without its `entities` or `relations`, marks every window in its batch with that
    error, so those reports count as partly unread and the run still writes its outputs. Two
    cases stop the run with SIEStopped instead: a refusal every batch would repeat (see
    `is_fatal`), and a run where no call succeeded at all, since then nothing was read. An
    error that isn't an SIE error is a bug in this code and is raised as is.
    """
    model = rules["models"]["relations"]
    labels = list(rules["entity_labels"])
    thresholds = rules["thresholds"]
    options = {
        "relation_labels": list(rules["relations"]),
        "threshold": thresholds["entity_threshold"],
        "relation_threshold": thresholds["relation_threshold"],
    }
    size = thresholds["batch_size"]
    results: dict[str, dict[str, Any]] = {}
    failures: list[SIEError] = []
    calls = 0
    for start in range(0, len(items), size):
        batch = items[start : start + size]
        calls += 1
        try:
            found = jsonable(
                client.extract(
                    model,
                    [Item(id=item_id, text=text) for item_id, text in batch],
                    labels=labels,
                    options=options,
                    provision_timeout_s=WAIT_S,
                )
            )
            if not isinstance(found, list) or len(found) != len(batch):
                raise SIEError(f"{model} replied with {_count(found)} results for {len(batch)}")
            if missing := [r for r in found if not _has_keys(r)]:
                raise SIEError(f"{model} replied without 'entities' or 'relations': {missing[0]!r}")
        except SIEError as error:
            if is_fatal(error):
                raise SIEStopped(stop_message(error, model)) from error
            failures.append(error)
            failed = {"error": f"the SIE call failed: {short(error)}"}
            results.update({item_id: failed for item_id, _ in batch})
            continue
        for (item_id, _), result in zip(batch, found, strict=True):
            results[item_id] = result
    if failures and len(failures) == calls:
        raise SIEStopped(
            f"Every SIE call failed ({calls} of {calls} to {model}), so the run stops: "
            f"{describe(failures[0])}: {failures[0]}. {every_call_failed_advice(failures[0])} "
            "No outputs were written."
        ) from failures[0]
    if failures:
        n = len(failures)
        print(
            f"{n} of {calls} SIE calls failed; their reports count as partly unread. "
            f"First: {short(failures[0])}"
        )
    return results


def _has_keys(result: Any) -> bool:
    """Both keys are there and not null: null is a failed call, an empty list is "nothing found"."""
    return (
        isinstance(result, dict)
        and result.get("entities") is not None
        and result.get("relations") is not None
    )


def _count(found: Any) -> str:
    return str(len(found)) if isinstance(found, list) else "no list of"


# --- SIE call failures ------------------------------------------------------------------------


class SIEStopped(RuntimeError):
    """SIE refused the call for a reason a retry won't fix, or no call succeeded; the run
    stops without outputs."""


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
    timeout, a lost connection, a model still loading and a 5xx are transient or per-batch:
    those reports count as partly unread."""
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
    """The error for a review item or the summary line, its message cut short."""
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


# --- 3. Names: the alias table ----------------------------------------------------------------


def clean_name(name: str) -> str:
    """Drop a leading "the", a possessive and edge punctuation, and collapse spaces."""
    name = re.sub("['\\u2019]s$", "", name.strip().strip(" .,;:()\"'"))
    name = re.sub(r"^the\s+", "", name, flags=re.IGNORECASE)
    return " ".join(name.split())


def name_key(name: str) -> str:
    """The lookup key for a name: clean_name, casefolded."""
    return clean_name(name).casefold()


CVE = re.compile(r"^CVE-\d{4}-\d{4,}$", re.IGNORECASE)


@dataclass(frozen=True)
class Canon:
    name: str
    type: str
    known: bool  # True when the alias table (or the CVE pattern) names it


@dataclass
class AliasTable:
    by_key: dict[str, Canon]
    # Names the table doesn't know, filled in as resolve() meets them, by name_key. The first
    # spelling seen names the node, so "Foo", "FOO" and "the Foo's" become one node, "Foo".
    unknown: dict[str, str] = field(default_factory=dict)
    # Every spelling the table lists for a canonical name, the name first.
    spellings: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def build(cls, rules: dict[str, Any]) -> AliasTable:
        by_key: dict[str, Canon] = {}
        spellings: dict[str, list[str]] = {}
        for entry in rules["aliases"]:
            canon = Canon(entry["name"], entry["type"], True)
            for alias in (entry["name"], *entry.get("aliases", [])):
                key = name_key(alias)
                if key in by_key and by_key[key] != canon:
                    raise ValueError(
                        f"alias {alias!r} names both {by_key[key].name} and {canon.name}"
                    )
                by_key[key] = canon
                spellings.setdefault(canon.name, []).append(alias)
        return cls(by_key, spellings=spellings)

    def names_for(self, name: str) -> list[str]:
        """The canonical name and every alias the table lists for it; a name the table doesn't
        know is only itself."""
        return self.spellings.get(name, [name])

    def resolve(self, text: str, model_label: str | None) -> Canon | None:
        """The canonical node for a name: the table, then a CVE ID, then the model's label."""
        if canon := self.by_key.get(name_key(text)):
            return canon
        if CVE.match(cve := text.strip().strip(" .,;:()\"'")):
            return Canon(cve.upper(), "vulnerability", True)
        if model_label is None:
            return None
        name = self.unknown.setdefault(name_key(text), clean_name(text))
        return Canon(name, model_label, False)


# --- 4. Relations in one window ---------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    report: str
    publisher: str
    published: str
    sentence: str
    score: float  # the model's extraction confidence, not how credible the claim is
    head_text: str  # head and tail as the report wrote them, before the alias table
    tail_text: str


@dataclass
class Claim:
    """One relation the model found in one window, after naming and type checks."""

    head: Canon
    relation: str
    tail: Canon
    source: Source
    negated: bool  # a negation or hedging word in its sentence: review, never the graph
    note: DirectionNote = ""


@dataclass
class Dropped:
    report: str
    head: str
    relation: str
    tail: str
    score: float
    reason: str


def window_labels(
    window: str, result: dict[str, Any], rules: dict[str, Any]
) -> tuple[dict[str, str], list[str]]:
    """Model label per entity text, plus a note for each span that fails the checks.

    A span must be an object with one of our labels and whole-number offsets, and its text must
    be exactly the window text at those offsets.
    """
    labels: dict[str, str] = {}
    notes: list[str] = []
    allowed = set(rules["entity_labels"])
    for raw in result.get("entities") or []:
        if not isinstance(raw, dict):
            notes.append(f"invalid span from the model dropped: {raw!r}")
            continue
        start, end, text = raw.get("start"), raw.get("end"), str(raw.get("text", ""))
        label = str(raw.get("label", ""))
        if (
            label in allowed
            and isinstance(start, int)
            and isinstance(end, int)
            and window[start:end] == text
        ):
            labels.setdefault(name_key(text), label)
        else:
            notes.append(f"invalid span from the model dropped: {raw!r}")
    return labels, notes


def claim_sentences(window: str, head: str, tail: str) -> list[str]:
    """The sentence or sentences the claim may sit in.

    The model's relations carry text, not offsets, so a name that occurs twice can give more than
    one: every sentence that holds both names. With no such sentence, it is the run of sentences
    from the closest pair of names across sentences.
    """
    spans = sentences(window) or [(0, len(window))]

    def sentence_of(pos: int) -> int:
        return max([i for i, (s, _) in enumerate(spans) if s <= pos], default=0)

    def found(text: str) -> list[int]:
        return [m.start() for m in re.finditer(re.escape(text), window)]

    pairs = sorted((sentence_of(h), sentence_of(t)) for h in found(head) for t in found(tail))
    if together := sorted({i for i, j in pairs if i == j}):
        return [window[spans[i][0] : spans[i][1]] for i in together]
    i, j = min(pairs, key=lambda p: abs(p[0] - p[1]))
    return [window[spans[min(i, j)][0] : spans[max(i, j)][1]]]


def negated_sentence(window: str, head: str, tail: str, rules: dict[str, Any]) -> tuple[bool, str]:
    """Whether a doubt word (negation or hedge) sits in the claim's sentence(s), and the sentence.

    The recipe never reads a negation as a denial: "did not use", "no longer uses", "did not
    use X but used Y" and "possibly uses" all send the claim to review. Without one it is an edge.
    """
    found = claim_sentences(window, head, tail)
    negated = [s for s in found if re.search(rules["doubt_words"], s, re.IGNORECASE)]
    return bool(negated), (negated or found)[0]


def orient(
    relation: str, head: Canon, tail: Canon, rules: dict[str, Any]
) -> tuple[Canon, Canon, DirectionNote] | None:
    """Head and tail in the direction the relation's type rule allows, plus a note, or None.

    The note is TURNED when only the other direction fits, EITHER_WAY when both directions fit
    (the types can't vouch for the direction), and "" when only the model's direction fits.
    """
    rule = rules["relations"][relation]
    forward = head.type in rule["head"] and tail.type in rule["tail"]
    backward = tail.type in rule["head"] and head.type in rule["tail"]
    if forward:
        return head, tail, EITHER_WAY if backward else ""
    if backward:
        return tail, head, TURNED
    return None


def unusable_end(text: str, rules: dict[str, Any]) -> str:
    """Why this end cannot be a graph node, or "" when it can: a generic word ("operators",
    "we") is never a node."""
    if name_key(text) in {name_key(word) for word in rules["not_entities"]}:
        return f"{text!r} is a generic word, not an entity"
    return ""


def window_claims(
    report: Report, window: str, result: dict[str, Any], table: AliasTable, rules: dict[str, Any]
) -> tuple[list[Claim], list[Dropped], list[str]]:
    """Checked claims from one window's relations, every relation dropped and why, and notes."""
    labels, notes = window_labels(window, result, rules)
    claims: list[Claim] = []
    dropped: list[Dropped] = []
    for raw in result.get("relations") or []:
        # A relation must be an object, and its score (0.0 when missing) a number.
        if not isinstance(raw, dict) or not isinstance(raw.get("score", 0.0), (int, float)):
            notes.append(f"invalid relation from the model dropped: {raw!r}")
            continue
        checked = check_relation(report, window, raw, labels, table, rules)
        if isinstance(checked, Claim):
            claims.append(checked)
        elif checked:
            dropped.append(
                Dropped(
                    report.id,
                    str(raw.get("head", "")),
                    str(raw.get("relation", "")),
                    str(raw.get("tail", "")),
                    float(raw.get("score", 0.0)),
                    checked,
                )
            )
    return claims, dropped, notes


def check_relation(
    report: Report,
    window: str,
    raw: dict[str, Any],
    labels: dict[str, str],
    table: AliasTable,
    rules: dict[str, Any],
) -> Claim | str:
    """A claim, or the reason the relation is dropped ("" for an alias the table already has)."""
    head_text, tail_text = str(raw.get("head", "")), str(raw.get("tail", ""))
    relation, score = str(raw.get("relation", "")), float(raw.get("score", 0.0))
    if relation not in rules["relations"]:
        return "relation label not in rules.yaml"
    if not head_text or not tail_text or head_text not in window or tail_text not in window:
        return "head or tail is not in the report text"
    head = table.resolve(head_text, labels.get(name_key(head_text)))
    tail = table.resolve(tail_text, labels.get(name_key(tail_text)))
    if head is None or tail is None:
        return "head or tail is not a tagged entity"
    if reason := unusable_end(head_text, rules) or unusable_end(tail_text, rules):
        return reason
    if head.name == tail.name:
        return "" if relation == ALIAS_OF else f"both ends are {head.name}"
    oriented = orient(relation, head, tail, rules)
    if oriented is None:
        return f"{head.type} -> {tail.type} does not fit '{relation}'"
    note = oriented[2]
    turned = note == TURNED
    negated, sentence = negated_sentence(window, head_text, tail_text, rules)
    head_as, tail_as = (tail_text, head_text) if turned else (head_text, tail_text)
    source = Source(
        report.id, report.publisher, report.published, sentence, score, head_as, tail_as
    )
    return Claim(oriented[0], relation, oriented[1], source, negated, note)


# --- 5. Graph ---------------------------------------------------------------------------------


@dataclass
class Node:
    name: str
    type: str
    known: bool
    mentions: list[str] = field(default_factory=list)  # report IDs


@dataclass
class Edge:
    head: str
    relation: str
    tail: str
    status: str = REVIEW  # build_graph sets the real status
    sources: list[Source] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def publishers(self) -> list[str]:
        return sorted({source.publisher for source in self.sources})


@dataclass
class Graph:
    nodes: list[Node]
    edges: list[Edge]
    dropped: list[Dropped]
    review: list[str]
    unread: list[str] = field(default_factory=list)  # report IDs with a paragraph not read
    # Every checked claim except a proposed alias, with or without a doubt word. The backlog
    # reads these, so a held-back claim can still show up as the reason a row needs review.
    claims: list[Claim] = field(default_factory=list)


def build_graph(claims: list[Claim], dropped: list[Dropped], rules: dict[str, Any]) -> Graph:
    """Merge claims into one edge per (head, relation, tail) and flag what needs an analyst.

    - single_source: fewer distinct publishers than min_sources (two reports from one
      publisher are one source).
    - review: an end of the edge is not in the alias table, a claim was turned round to fit
      the types, the types fit either way round so they can't vouch for the direction, or a
      report has the claim with a doubt word in its sentence. A claim with a doubt word never
      makes or supports an edge: it goes to the review list with its sentence and score.
    - multi_publisher: at least min_sources publishers and none of the above.
    A relation in note_other_tails (attributed to) with several tails for one head gets a note
    naming the others, and a review item. Actors share, buy and leak malware, so that alone is
    no contradiction and the status does not change.
    """
    nodes: dict[str, Node] = {}
    types: dict[str, set[str]] = {}
    edges: dict[tuple[str, str, str], Edge] = {}
    negated: list[Claim] = []
    review: list[str] = []
    for claim in claims:
        if claim.relation == ALIAS_OF:
            review.append(
                f"{claim.source.report}: proposed alias {claim.head.name!r} = {claim.tail.name!r}"
                " is not in the alias table; not merged"
            )
            continue
        if claim.negated:
            review.append(
                f"{claim.source.report}: {claim.head.name} {claim.relation} {claim.tail.name}"
                f" (score {claim.source.score:.2f}) has a negation or hedging word in its"
                f" sentence; not an edge: {claim.source.sentence!r}"
            )
            negated.append(claim)
            continue
        for end in (claim.head, claim.tail):
            types.setdefault(end.name, set()).add(end.type)
            node = nodes.setdefault(end.name, Node(end.name, end.type, end.known))
            if claim.source.report not in node.mentions:
                node.mentions.append(claim.source.report)
        key = (claim.head.name, claim.relation, claim.tail.name)
        edge = edges.setdefault(key, Edge(*key))
        edge.sources.append(claim.source)
        if claim.note and claim.note not in edge.flags:
            edge.flags.append(claim.note)

    min_sources = rules["thresholds"]["min_sources"]
    note_other_tails = set(rules["note_other_tails"])
    noted: list[Edge] = []  # edges with an "also attributed to" note
    for edge in edges.values():
        also: list[str] = []
        if edge.relation in note_other_tails:
            others = sorted(
                {
                    e.tail
                    for e in edges.values()
                    if (e.head, e.relation) == (edge.head, edge.relation)
                }
                - {edge.tail}
            )
            if others:
                also.append(
                    f"also {edge.relation} {', '.join(others)} in other claims"
                    " (shared tooling or a disagreement: analyst to check)"
                )
                noted.append(edge)
        unsure = [
            f"{c.source.report} has it in a sentence with a negation or hedging word:"
            f" {c.source.sentence!r}"
            for c in negated
            if (c.head.name, c.relation, c.tail.name) == (edge.head, edge.relation, edge.tail)
        ]
        unknown = [n for n in (edge.head, edge.tail) if not nodes[n].known]
        edge.flags += also + unsure
        edge.flags += [f"{name} is not in the alias table" for name in unknown]
        edge.flags += [
            f"{name} typed as {' and '.join(sorted(types[name]))} in different claims"
            for name in (edge.head, edge.tail)
            if len(types[name]) > 1
        ]
        if len(edge.publishers) < min_sources:
            edge.flags.append(
                f"only {len(edge.publishers)} publisher(s): {', '.join(edge.publishers)}"
            )
        if unknown or unsure or TURNED in edge.flags or EITHER_WAY in edge.flags:
            edge.status = REVIEW
        elif len(edge.publishers) < min_sources:
            edge.status = SINGLE_SOURCE
        else:
            edge.status = MULTI_PUBLISHER
    for node in nodes.values():
        if node.known:
            continue
        if node.type in rules["listed_types_only"]:
            review.append(
                f"{node.name} ({node.type}) is not a CVE ID or in the alias table:"
                " a new flaw, or not a flaw at all?"
            )
        else:
            review.append(f"{node.name} ({node.type}) is not in the alias table: new or an alias?")
    for edge in edges.values():
        # An "also attributed to" note goes to the analyst queue too, without changing status.
        if edge.status == REVIEW or edge in noted:
            review.append(f"{edge.head} {edge.relation} {edge.tail}: {'; '.join(edge.flags)}")
    return Graph(
        sorted(nodes.values(), key=lambda n: (n.type, n.name)),
        sorted(edges.values(), key=lambda e: (e.head, e.relation, e.tail)),
        dropped,
        review,
        claims=[claim for claim in claims if claim.relation != ALIAS_OF],
    )


# --- 6. Mermaid -------------------------------------------------------------------------------


def mermaid(graph: Graph) -> str:
    """A Mermaid flowchart: solid arrows are multi_publisher, the rest dashed, red for review.

    "?" marks a name the alias table does not know. A provisional edge is dashed: it would be
    multi_publisher, but a report was partly unread.
    """
    shapes = {"threat actor": ("([", "])"), "malware": ("[[", "]]"), "vulnerability": ("{{", "}}")}
    node_id = {node.name: f"n{index}" for index, node in enumerate(graph.nodes)}
    lines = ["flowchart LR"]
    for node in graph.nodes:
        left, right = shapes.get(node.type, ("[", "]"))
        name = node.name.replace('"', "#quot;")
        label = f"{name}<br/><i>{node.type}</i>" + ("" if node.known else " ?")
        lines.append(f'    {node_id[node.name]}{left}"{label}"{right}')
    red: list[int] = []
    for index, edge in enumerate(graph.edges):
        arrow = "-->" if edge.status == MULTI_PUBLISHER else "-.->"
        label = f"{edge.relation} ({len(edge.publishers)})"
        lines.append(f'    {node_id[edge.head]} {arrow}|"{label}"| {node_id[edge.tail]}')
        if edge.status == REVIEW:
            red.append(index)
    if red:
        lines.append(f"    linkStyle {','.join(map(str, red))} stroke:#d33,stroke-width:2px")
    return "\n".join(lines) + "\n"


# --- 7. Backlog: groups that target your sectors, their tools, the rule catalog ---------------


@dataclass(frozen=True)
class Link:
    """One claim as the backlog reads it, and why it is held back ("" when it isn't)."""

    claim: Claim
    held: str


@dataclass(frozen=True)
class Use:
    """A group uses a tool: a `uses` claim, or a tool `attributed to` the group."""

    group: str
    tool: Canon
    link: Link


@dataclass(frozen=True)
class Target:
    """A group targets one of your sectors: directly, or with a tool aimed at the sector in the
    same sentence that says the group used it."""

    group: str
    sector: str
    links: tuple[Link, ...]

    @property
    def held(self) -> str:
        return "; ".join(link.held for link in self.links if link.held)


@dataclass
class Row:
    tool: str
    type: str
    status: str = NEEDS_REVIEW  # build_backlog sets the real status
    rules: list[str] = field(default_factory=list)  # "DR-0101 title"
    groups: list[str] = field(default_factory=list)
    sectors: list[str] = field(default_factory=list)
    publishers: list[str] = field(default_factory=list)
    reports: list[str] = field(default_factory=list)
    sentences: list[str] = field(default_factory=list)  # "R-01: sentence"
    held_back: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def our_sectors(rules: dict[str, Any], table: AliasTable) -> list[str]:
    """Your sectors as canonical names. Each must be a sector in the alias table, so a typo
    fails before any model call instead of quietly matching nothing."""
    sectors: list[str] = []
    for name in rules["our_sectors"]:
        canon = table.by_key.get(name_key(name))
        if canon is None or canon.type != "sector":
            raise ValueError(f"our_sectors: {name!r} is not a sector in the alias table")
        sectors.append(canon.name)
    return sectors


def backlog_links(graph: Graph) -> list[Link]:
    """Every claim with the reason it is held back: a doubt word in its sentence, or an edge in
    review (an unknown name, a turned-round claim, a direction the types can't vouch for)."""
    edges = {(e.head, e.relation, e.tail): e for e in graph.edges}
    links: list[Link] = []
    for claim in graph.claims:
        edge = edges.get((claim.head.name, claim.relation, claim.tail.name))
        if claim.negated:
            held = f"a negation or hedging word in its sentence: {claim.source.sentence!r}"
        elif edge is not None and edge.status == REVIEW:
            why = [f for f in edge.flags if not f.startswith("only ")]
            held = f"the edge is in review ({'; '.join(why)})"
        else:
            held = ""
        links.append(Link(claim, held))
    return links


def as_use(link: Link, rules: dict[str, Any]) -> Use | None:
    """The group and tool when the claim says a group uses a tool, else None."""
    groups, tools = set(rules["backlog"]["group_types"]), set(rules["backlog"]["tool_types"])
    claim = link.claim
    if claim.relation == "uses" and claim.head.type in groups and claim.tail.type in tools:
        return Use(claim.head.name, claim.tail, link)
    if claim.relation == "attributed to" and claim.head.type in tools and claim.tail.type in groups:
        return Use(claim.tail.name, claim.head, link)
    return None


def targets(links: list[Link], sectors: list[str], rules: dict[str, Any]) -> list[Target]:
    """Every claim that a group targets one of your sectors.

    Directly: "Saltmarsh Kettle targeted port operators". Or through a tool, when one sentence
    says both: "Gorse Moth deployed Thornpipe against a freight broker" comes back from the
    model as "Gorse Moth uses Thornpipe" and "Thornpipe targets freight broker", and only the
    two together say the group went after the sector.
    """
    groups, tools = set(rules["backlog"]["group_types"]), set(rules["backlog"]["tool_types"])
    found: list[Target] = []
    for link in links:
        claim = link.claim
        if claim.relation != "targets" or claim.tail.name not in sectors:
            continue
        if claim.head.type in groups:
            found.append(Target(claim.head.name, claim.tail.name, (link,)))
        elif claim.head.type in tools:
            for other in links:
                use = as_use(other, rules)
                same = (other.claim.source.report, other.claim.source.sentence) == (
                    claim.source.report,
                    claim.source.sentence,
                )
                if use and use.tool.name == claim.head.name and same:
                    found.append(Target(use.group, claim.tail.name, (other, link)))
    return found


def held_item(use: Use, target: Target) -> str:
    """One held-back chain for the row: the claim as the backlog reads it, and why."""
    claim = use.link.claim
    said = (
        f"{use.group} uses {use.tool.name}"
        if claim.relation == "uses"
        else f"{use.tool.name} attributed to {use.group}"
    )
    why = use.link.held or f"that {use.group} targets {target.sector} is held back: {target.held}"
    return f"{claim.source.report}: {said}: {why}"


def build_backlog(
    graph: Graph,
    catalog: RuleCatalog,
    table: AliasTable,
    rules: dict[str, Any],
) -> list[Row]:
    """One row per tool used by a group that targets your sectors.

    - rule_exists: a catalog rule covers the tool by its canonical name or an alias the table
      lists. A near name ("Larchwood stealer" for Larchwick) never counts; it is a note.
    - no_rule: no rule covers it, and at least one chain is clear: a group uses the tool and
      targets your sector, and neither claim is held back.
    - needs_review: no rule covers it, and every chain has a held-back claim (a doubt word, an
      unknown name, a turned-round claim), or a report SIE didn't fully read leaves the row open.
    """
    sectors = our_sectors(rules, table)
    links = backlog_links(graph)
    by_group: dict[str, list[Target]] = {}
    for target in targets(links, sectors, rules):
        by_group.setdefault(target.group, []).append(target)
    chains: dict[str, list[tuple[Use, Target]]] = {}
    tools: dict[str, Canon] = {}
    for link in links:
        if (use := as_use(link, rules)) is None:
            continue
        for target in by_group.get(use.group, []):
            chains.setdefault(use.tool.name, []).append((use, target))
            tools.setdefault(use.tool.name, use.tool)
    ratio = rules["thresholds"]["near_name_ratio"]
    rows: list[Row] = []
    for name, found in chains.items():
        row = Row(name, tools[name].type)
        clear = [(u, t) for u, t in found if not u.link.held and not t.held]
        shown = clear or found
        used = [u.link.claim for u, _ in shown]
        aimed = [link.claim for _, t in shown for link in t.links]
        row.groups = sorted({u.group for u, _ in shown})
        row.sectors = sorted({t.sector for _, t in shown})
        row.publishers = sorted({c.source.publisher for c in used + aimed})
        row.reports = sorted({c.source.report for c in used + aimed})
        row.sentences = list(dict.fromkeys(f"{c.source.report}: {c.source.sentence}" for c in used))
        row.sentences += [
            s
            for s in dict.fromkeys(f"{c.source.report}: {c.source.sentence}" for c in aimed)
            if s not in row.sentences
        ]
        row.held_back = list(
            dict.fromkeys(held_item(u, t) for u, t in found if (u, t) not in clear)
        )
        names = table.names_for(name)
        covering = catalog.find_rules(names)
        row.rules = [f"{rule.rule_id} {rule.title}" for rule in covering]
        row.notes = [
            f"near name in the catalog, not counted: {rule.rule_id} covers {covered!r}"
            f" (looked up {name!r})"
            for rule, covered in catalog.near_rules(names, ratio)
            if rule not in covering
        ]
        if covering:
            row.status = RULE_EXISTS
        elif clear and not graph.unread:
            row.status = NO_RULE
        else:
            row.status = NEEDS_REVIEW
            if clear:
                row.notes.append(
                    f"reports {', '.join(graph.unread)} were partly unread (an SIE call failed),"
                    " so a doubt or a denial may be missing: check before it goes on the backlog"
                )
        rows.append(row)
    return sorted(rows, key=lambda r: (STATUSES.index(r.status), r.tool))


# --- Run --------------------------------------------------------------------------------------


def map_relations(client: SIEClient, reports: list[Report], rules: dict[str, Any]) -> Graph:
    max_words = rules["thresholds"]["max_words_per_window"]
    windows = {r.id: paragraph_windows(r.text, max_words) for r in reports}
    items = [
        (f"{report_id}#p{number}", window)
        for report_id, parts in windows.items()
        for number, (_, window) in enumerate(parts, start=1)
    ]
    table = AliasTable.build(rules)  # a bad alias table fails before any model call
    our_sectors(rules, table)  # and so does a sector the table doesn't know
    results = extract_windows(client, items, rules) if items else {}
    claims: list[Claim] = []
    dropped: list[Dropped] = []
    review: list[str] = []
    unread: list[str] = []
    for report in reports:
        for number, (_, window) in enumerate(windows[report.id], start=1):
            result = results[f"{report.id}#p{number}"]
            if error := result.get("error"):
                review.append(f"{report.id} paragraph {number} was not read: {error}")
                if report.id not in unread:
                    unread.append(report.id)
                continue
            found, lost, notes = window_claims(report, window, result, table, rules)
            claims += found
            dropped += lost
            review += [f"{report.id} paragraph {number}: {note}" for note in notes]
    graph = build_graph(claims, dropped, rules)
    graph.review = review + graph.review
    graph.unread = unread
    for edge in graph.edges:
        edge.flags += [
            f"report {report_id} partly unread"
            for report_id in unread
            if any(source.report == report_id for source in edge.sources)
        ]
        # An unread paragraph may contradict an edge from any report, or question it.
        if unread and edge.status == MULTI_PUBLISHER:
            edge.status = PROVISIONAL
            edge.flags.append(f"provisional: {', '.join(unread)} partly unread")
    return graph


def graph_json(graph: Graph) -> dict[str, Any]:
    return {
        "nodes": [asdict(node) for node in graph.nodes],
        "edges": [{**asdict(edge), "publishers": edge.publishers} for edge in graph.edges],
        "dropped": [asdict(d) for d in graph.dropped],
        "review": graph.review,
        "unread": graph.unread,
    }


def csv_safe(value: Any) -> str:
    """A leading ' on a cell a spreadsheet would run as a formula; anything else as written."""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def backlog_json(rows: list[Row], graph: Graph, rules: dict[str, Any]) -> dict[str, Any]:
    return {
        "our_sectors": list(rules["our_sectors"]),
        "rows": [asdict(row) for row in rows],
        "unread": graph.unread,
    }


def find_gaps(
    client: SIEClient, reports: list[Report], rules: dict[str, Any], catalog: RuleCatalog
) -> tuple[Graph, list[Row]]:
    """Read the reports into a graph, then compare the tools of groups that target your
    sectors with the rule catalog."""
    graph = map_relations(client, reports, rules)
    return graph, build_backlog(graph, catalog, AliasTable.build(rules), rules)


def write_outputs(output_dir: Path, graph: Graph, rows: list[Row], rules: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "graph.json").write_text(
        json.dumps(graph_json(graph), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (output_dir / "graph.mmd").write_text(mermaid(graph), encoding="utf-8")
    (output_dir / "backlog.json").write_text(
        json.dumps(backlog_json(rows, graph, rules), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    with (output_dir / "backlog.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=BACKLOG_FIELDS)
        writer.writeheader()
        for row in rows:
            cells = {
                key: " | ".join(value) if isinstance(value, list) else value
                for key, value in asdict(row).items()
            }
            writer.writerow({key: csv_safe(value) for key, value in cells.items()})


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rules = load_rules()
    reports, catalog = load_reports(), RuleCatalog()
    with make_client() as client:
        graph, rows = stop_on_refusal(lambda: find_gaps(client, reports, rules, catalog))
    write_outputs(output, graph, rows, rules)
    print(f"Tools of groups that target {', '.join(rules['our_sectors'])}:")
    for row in rows:
        rule_ids = ", ".join(rule.split()[0] for rule in row.rules) or "-"
        print(f"{row.status:<13} {row.tool:<20} groups: {', '.join(row.groups)}  rules: {rule_ids}")
        for item in row.held_back:
            print(f"{'':<14}held back: {item}")
        for note in row.notes:
            print(f"{'':<14}note: {note}")
    for report_id in graph.unread:
        print(
            f"warning: {report_id} was partly unread; tools from it may be missing, and every"
            " row that would be no_rule is needs_review"
        )
    counts = {status: sum(r.status == status for r in rows) for status in STATUSES}
    print(
        f"\n{len(rows)} rows: "
        + ", ".join(f"{n} {status}" for status, n in counts.items())
        + f"; graph: {len(graph.nodes)} nodes, {len(graph.edges)} edges,"
        f" {len(graph.review)} review items"
    )
    print(f"outputs: {output / 'backlog.csv'}, {output / 'backlog.json'}, {output / 'graph.json'}")


if __name__ == "__main__":
    main()
