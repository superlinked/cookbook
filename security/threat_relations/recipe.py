"""Map how threat actors, malware, vulnerabilities and targets relate across threat reports.

Use case:  Threat relations: who uses what, exploits what, targets whom, attributed to whom.
Who:       Threat-intelligence and SOC teams that read vendor reports and keep a threat graph.
Problem:   Every vendor writes about the same groups under its own names ("Gorse Moth" is
           "Velvet Gorse"), one report attributes a stealer to one group and the next report to
           another, and a single blog post can put an edge in the graph that nobody else has
           seen. An analyst needs each edge with the sentences behind it, and needs to see which
           edges rest on one publisher or disagree.
Input:     data/reports.jsonl (synthetic threat reports from fictional publishers about fictional
           actors and malware) and rules.yaml (labels, alias table, relation type rules,
           negation patterns, thresholds).
SIE pipeline:
           (Python)                                  split each report into paragraphs
           extract  knowledgator/gliner-relex-large-v1.0
                                                     entities plus uses / exploits / targets /
                                                     attributed to / alias of relations
           (Python)                                  check spans; merge names by the alias table;
                                                     check relation types (turn round or drop);
                                                     a negation directly on the claim is a
                                                     denial, not an edge, and any other in its
                                                     sentence sends it to review; build the
                                                     graph with every source; set each edge's
                                                     status and queue conflicting, unsure and
                                                     unknown-entity edges for review
Output:    graph.json (nodes, edges with sources and status, denials, dropped relations, review
           items, unread reports), graph.mmd (Mermaid) and edges.csv (one row per edge).
Why SIE:   One model finds entities and relations together. Otherwise you'd need an NER model
           plus a relation classifier, or an LLM per sentence. The labels and relation types are
           plain words in rules.yaml. Merging names, checking types and flagging edges is plain
           Python you can read.
Runs on:   SIE Cloud.

    uv run recipe.py
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import yaml
from dotenv import load_dotenv
from sie_sdk import SIEClient
from sie_sdk.types import Item

ROOT = Path(__file__).resolve().parent
REPORTS_PATH = ROOT / "data" / "reports.jsonl"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900

ALIAS_OF = "alias of"
CORROBORATED, PROVISIONAL, SINGLE_SOURCE, CONFLICTING, REVIEW = (
    "corroborated",
    "provisional",  # would be corroborated, but a report was partly unread
    "single_source",
    "conflicting",
    "review",
)
EDGE_FIELDS = ("head", "relation", "tail", "status", "publishers", "reports", "flags")
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
    """Entities and relations per window id, in batches."""
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
    for start in range(0, len(items), size):
        batch = items[start : start + size]
        found = client.extract(
            model,
            [Item(id=item_id, text=text) for item_id, text in batch],
            labels=labels,
            options=options,
            provision_timeout_s=WAIT_S,
        )
        for (item_id, _), result in zip(batch, jsonable(found), strict=True):
            results[item_id] = result
    return results


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

    @classmethod
    def build(cls, rules: dict[str, Any]) -> AliasTable:
        by_key: dict[str, Canon] = {}
        for entry in rules["aliases"]:
            canon = Canon(entry["name"], entry["type"], True)
            for alias in (entry["name"], *entry.get("aliases", [])):
                key = name_key(alias)
                if key in by_key and by_key[key] != canon:
                    raise ValueError(
                        f"alias {alias!r} names both {by_key[key].name} and {canon.name}"
                    )
                by_key[key] = canon
        return cls(by_key)

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
    score: float


@dataclass
class Claim:
    """One relation the model found in one window, after naming and type checks."""

    head: Canon
    relation: str
    tail: Canon
    source: Source
    denied: bool  # a negation is tied to this claim: a denial, not an edge
    doubt: str = ""  # why a negation nearby makes the claim unsure: neither edge nor denial
    note: str = ""


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

    A span must carry one of our labels and its text must be exactly the window text at its
    offsets.
    """
    labels: dict[str, str] = {}
    notes: list[str] = []
    allowed = set(rules["entity_labels"])
    for raw in result.get("entities") or []:
        start, end, text = raw.get("start"), raw.get("end"), str(raw.get("text", ""))
        label = str(raw.get("label", ""))
        if label in allowed and start is not None and end is not None and window[start:end] == text:
            labels.setdefault(name_key(text), label)
        else:
            notes.append(f"invalid span from the model dropped: {raw!r}")
    return labels, notes


Span = tuple[int, int]


@dataclass(frozen=True)
class Place:
    """One place the claim may sit: the earlier name, the later name, and their sentence(s)."""

    first: Span
    second: Span
    sentence: Span  # from the first name's sentence to the second name's
    split: bool  # the names are in different sentences


def claim_places(window: str, head: str, tail: str) -> list[Place]:
    """Where the claim may sit: the closest pair of names in each sentence holding both.

    The model's relations carry text, not offsets, so a name that occurs twice gives more than
    one place. With no sentence holding both, it is the closest pair across sentences.
    """
    spans = sentences(window) or [(0, len(window))]

    def sentence_of(pos: int) -> int:
        return max([i for i, (s, _) in enumerate(spans) if s <= pos], default=0)

    def found(text: str) -> list[Span]:
        return [m.span() for m in re.finditer(re.escape(text), window)]

    pairs = [(min(h, t), max(h, t)) for h in found(head) for t in found(tail)]
    pairs = sorted(
        [(a, b) for a, b in pairs if a[1] <= b[0]] or pairs, key=lambda p: p[1][0] - p[0][0]
    )
    places: dict[tuple[int, int], Place] = {}
    for first, second in pairs:
        i, j = sentence_of(first[0]), sentence_of(second[0])
        places.setdefault((i, j), Place(first, second, (spans[i][0], spans[j][1]), i != j))
    if one_sentence := [p for p in places.values() if not p.split]:
        return sorted(one_sentence, key=lambda p: p.sentence)
    return [min(places.values(), key=lambda p: p.second[0] - p.first[0])]


def negation_patterns(rules: dict[str, Any], verbs: list[str]) -> list[re.Pattern[str]]:
    """The negation patterns, with {verb} standing for any of these verbs."""
    alternatives = "(?:" + "|".join(map(re.escape, verbs)) + r")\b"
    return [
        re.compile(p.replace("{verb}", alternatives), re.IGNORECASE)
        for p in rules["negation"]["patterns"]
    ]


def negation_at(
    window: str, place: Place, relation: str, others: list[str], rules: dict[str, Any]
) -> tuple[bool, str]:
    """Whether the claim at this place is denied, and why it is unsure ("" when sure).

    A denial needs the negation directly on the claim: both names in one sentence, nothing
    between them that starts a new clause (clause_breaks) or names another entity (others),
    and a pattern, with this relation's verbs, in that text or in the few words just before the
    earlier name. Anything else with a negation word (doubt_words) in the claim's sentence or
    sentences is unsure: neither edge nor denial.
    """
    negation = rules["negation"]
    sentence = window[place.sentence[0] : place.sentence[1]]
    unsure = re.search(negation["doubt_words"], sentence, re.IGNORECASE)
    if not place.split:
        breaks = "|".join([negation["clause_breaks"], *(re.escape(name) for name in others)])
        between = window[place.first[1] : place.second[0]]
        before = re.split(breaks, window[place.sentence[0] : place.first[0]], flags=re.IGNORECASE)
        words = before[-1].split()
        scope = " ".join(words[max(len(words) - negation["lookback_words"], 0) :]) + " " + between
        verbs = negation["verbs"][relation] + negation["every_relation"]
        if not re.search(breaks, between, re.IGNORECASE) and any(
            p.search(scope) for p in negation_patterns(rules, verbs)
        ):
            return True, ""
    return False, "a negation in the sentence that is not directly on the claim" if unsure else ""


def check_negation(
    window: str, head: str, tail: str, relation: str, others: list[str], rules: dict[str, Any]
) -> tuple[bool, str, str]:
    """Whether the claim is denied, why it is unsure ("" when sure), and its sentence.

    others are the other entity names tagged in the window. When the names share more than one
    sentence and the sentences disagree, the claim is unsure.
    """
    places = claim_places(window, head, tail)
    verdicts = {negation_at(window, place, relation, others, rules) for place in places}
    s, e = places[0].sentence
    if len(verdicts) > 1:
        return False, "sentences that disagree on a negation", window[s:e]
    denied, doubt = verdicts.pop()
    return denied, doubt, window[s:e]


def orient(
    relation: str, head: Canon, tail: Canon, rules: dict[str, Any]
) -> tuple[Canon, Canon] | None:
    """Head and tail in the direction the relation's type rule allows, or None if neither fits."""
    rule = rules["relations"][relation]
    if head.type in rule["head"] and tail.type in rule["tail"]:
        return head, tail
    if tail.type in rule["head"] and head.type in rule["tail"]:
        return tail, head
    return None


def unusable_end(canon: Canon, text: str, rules: dict[str, Any]) -> str:
    """Why this end cannot be a graph node, or "" when it can.

    A generic word ("operators", "we") is never a node, and a type listed in
    listed_types_only (vulnerability) must be a CVE ID or an alias-table entry: the model
    reads "code overlap" or "outbound connections" as vulnerabilities.
    """
    if name_key(text) in {name_key(word) for word in rules["not_entities"]}:
        return f"{text!r} is a generic word, not an entity"
    if canon.type in rules["listed_types_only"] and not canon.known:
        return f"{text!r} is a {canon.type} that is not a CVE ID or in the alias table"
    return ""


def window_claims(
    report: Report, window: str, result: dict[str, Any], table: AliasTable, rules: dict[str, Any]
) -> tuple[list[Claim], list[Dropped], list[str]]:
    """Checked claims from one window's relations, every relation dropped and why, and notes."""
    labels, notes = window_labels(window, result, rules)
    claims: list[Claim] = []
    dropped: list[Dropped] = []
    for raw in result.get("relations") or []:
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
    if reason := unusable_end(head, head_text, rules) or unusable_end(tail, tail_text, rules):
        return reason
    if head.name == tail.name:
        return "" if relation == ALIAS_OF else f"both ends are {head.name}"
    oriented = orient(relation, head, tail, rules)
    if oriented is None:
        return f"{head.type} -> {tail.type} does not fit '{relation}'"
    note = "" if oriented[0] is head else "turned round to fit the relation's types"
    others = [name for name in labels if name not in (name_key(head_text), name_key(tail_text))]
    denied, doubt, sentence = check_negation(window, head_text, tail_text, relation, others, rules)
    source = Source(report.id, report.publisher, report.published, sentence, score)
    return Claim(oriented[0], relation, oriented[1], source, denied, doubt, note)


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
    denials: list[Claim]
    dropped: list[Dropped]
    review: list[str]
    unread: list[str] = field(default_factory=list)  # report IDs with a paragraph not read


def build_graph(claims: list[Claim], dropped: list[Dropped], rules: dict[str, Any]) -> Graph:
    """Merge claims into one edge per (head, relation, tail) and flag what needs an analyst.

    - single_source: fewer distinct publishers than min_sources (two reports from one
      publisher are one source).
    - conflicting: a single-valued relation (attributed to) points to two different tails, or
      a report (any report, the same one included) denies the edge.
    - review: an end of the edge is not in the alias table, or a report has the claim with a
      negation in its sentence that check_negation couldn't tie to it. Such a claim is neither
      an edge nor a denial, and it goes to the review list.
    """
    nodes: dict[str, Node] = {}
    types: dict[str, set[str]] = {}
    edges: dict[tuple[str, str, str], Edge] = {}
    denials: list[Claim] = []
    doubts: list[Claim] = []
    review: list[str] = []
    for claim in claims:
        if claim.relation == ALIAS_OF:
            review.append(
                f"{claim.source.report}: proposed alias {claim.head.name!r} = {claim.tail.name!r}"
                " is not in the alias table; not merged"
            )
            continue
        if claim.doubt:
            review.append(
                f"{claim.source.report}: {claim.head.name} {claim.relation} {claim.tail.name}"
                f" has {claim.doubt}; not an edge or a denial: {claim.source.sentence!r}"
            )
            doubts.append(claim)
            continue
        if claim.denied:
            denials.append(claim)
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
    single_valued = set(rules["single_valued"])
    for edge in edges.values():
        conflict: list[str] = []
        if edge.relation in single_valued:
            others = sorted(
                {
                    e.tail
                    for e in edges.values()
                    if (e.head, e.relation) == (edge.head, edge.relation)
                }
                - {edge.tail}
            )
            if others:
                conflict.append(f"other reports say {edge.relation} {', '.join(others)}")
        key = (edge.head, edge.relation, edge.tail)
        conflict += [
            f"{c.source.report} denies it: {c.source.sentence!r}"
            for c in denials
            if (c.head.name, c.relation, c.tail.name) == key
        ]
        unsure = [
            f"{c.source.report} has a negation near it: {c.source.sentence!r}"
            for c in doubts
            if (c.head.name, c.relation, c.tail.name) == key
        ]
        unknown = [n for n in (edge.head, edge.tail) if not nodes[n].known]
        edge.flags += conflict + unsure
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
        if conflict:
            edge.status = CONFLICTING
        elif unknown or unsure:
            edge.status = REVIEW
        elif len(edge.publishers) < min_sources:
            edge.status = SINGLE_SOURCE
        else:
            edge.status = CORROBORATED
    for node in nodes.values():
        if not node.known:
            review.append(f"{node.name} ({node.type}) is not in the alias table: new or an alias?")
    for edge in edges.values():
        if edge.status in (CONFLICTING, REVIEW):
            review.append(f"{edge.head} {edge.relation} {edge.tail}: {'; '.join(edge.flags)}")
    return Graph(
        sorted(nodes.values(), key=lambda n: (n.type, n.name)),
        sorted(edges.values(), key=lambda e: (e.head, e.relation, e.tail)),
        denials,
        dropped,
        review,
    )


# --- 6. Mermaid -------------------------------------------------------------------------------


def mermaid(graph: Graph) -> str:
    """A Mermaid flowchart: solid arrows are corroborated, the rest dashed, red when in doubt.

    Red marks conflicting and review edges, and "?" marks a name the alias table does not know.
    A provisional edge is dashed: it would be corroborated, but a report was partly unread.
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
        arrow = "-->" if edge.status == CORROBORATED else "-.->"
        label = f"{edge.relation} ({len(edge.publishers)})"
        lines.append(f'    {node_id[edge.head]} {arrow}|"{label}"| {node_id[edge.tail]}')
        if edge.status in (CONFLICTING, REVIEW):
            red.append(index)
    if red:
        lines.append(f"    linkStyle {','.join(map(str, red))} stroke:#d33,stroke-width:2px")
    return "\n".join(lines) + "\n"


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
    if missing := sorted(set(rules["relations"]) - set(rules["negation"]["verbs"])):
        raise ValueError(f"negation verbs missing for relation(s): {', '.join(missing)}")
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
        # An unread paragraph may deny or contradict an edge from any report.
        if unread and edge.status == CORROBORATED:
            edge.status = PROVISIONAL
            edge.flags.append(f"provisional: {', '.join(unread)} partly unread")
    return graph


def graph_json(graph: Graph) -> dict[str, Any]:
    def claim(c: Claim) -> dict[str, Any]:
        return {
            "head": c.head.name,
            "relation": c.relation,
            "tail": c.tail.name,
            "source": asdict(c.source),
        }

    return {
        "nodes": [asdict(node) for node in graph.nodes],
        "edges": [{**asdict(edge), "publishers": edge.publishers} for edge in graph.edges],
        "denials": [claim(c) for c in graph.denials],
        "dropped": [asdict(d) for d in graph.dropped],
        "review": graph.review,
        "unread": graph.unread,
    }


def csv_safe(value: Any) -> str:
    """A leading ' on a cell a spreadsheet would run as a formula; anything else as written."""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def write_outputs(output_dir: Path, graph: Graph) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "graph.json").write_text(
        json.dumps(graph_json(graph), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (output_dir / "graph.mmd").write_text(mermaid(graph), encoding="utf-8")
    with (output_dir / "edges.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=EDGE_FIELDS)
        writer.writeheader()
        for edge in graph.edges:
            row = {
                "head": edge.head,
                "relation": edge.relation,
                "tail": edge.tail,
                "status": edge.status,
                "publishers": ";".join(edge.publishers),
                "reports": ";".join(sorted({s.report for s in edge.sources})),
                "flags": "; ".join(edge.flags),
            }
            writer.writerow({key: csv_safe(value) for key, value in row.items()})


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    with make_client() as client:
        graph = map_relations(client, load_reports(), load_rules())
    write_outputs(output, graph)
    for edge in graph.edges:
        print(
            f"{edge.status:<13} {edge.head} --{edge.relation}--> {edge.tail}"
            f"  [{', '.join(edge.publishers)}]"
        )
    for denial in graph.denials:
        print(
            f"{'denied':<13} {denial.head.name} --{denial.relation}--> {denial.tail.name}"
            f"  [{denial.source.report}]"
        )
    for item in graph.review:
        print(f"review: {item}")
    for report_id in graph.unread:
        print(
            f"warning: {report_id} was partly unread; its edges carry a flag and may be missing,"
            " and the status of any other edge may be wrong too (corroborated edges are"
            " provisional)"
        )
    print(f"\n{len(graph.nodes)} nodes, {len(graph.edges)} edges, {len(graph.review)} review items")
    print(f"outputs: {output / 'graph.json'}, {output / 'graph.mmd'}, {output / 'edges.csv'}")


if __name__ == "__main__":
    main()
