"""Offline checks of the recipe's own parsing and rules. No SIE call, no recorded responses.

The checks use hand-written sentences, the committed data/reports.jsonl and
data/detection_rules.csv, and hand-written stand-in model replies that feed map_relations the way
SIE's extract would.

uv run --frozen python selfcheck.py
"""

from __future__ import annotations

import contextlib
import copy
import io
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from rule_catalog import RuleCatalog
from sie_sdk.client.errors import (
    AccountInactiveError,
    InputTooLongError,
    InsufficientCreditsError,
    ModelLoadFailedError,
    ModelLoadingError,
    ProvisioningError,
    RateLimitError,
    RequestError,
    ServerError,
    SIEConnectionError,
    SIEError,
    SpendLimitError,
)
from sie_sdk.types import Item

import recipe
from recipe import Canon, Edge, Graph, Node, Row

RULES = recipe.load_rules()
TABLE = recipe.AliasTable.build(RULES)
CATALOG = RuleCatalog()
# The stand-in client answers one window per call, so a failure hits one window only.
ONE_PER_CALL = copy.deepcopy(RULES)
ONE_PER_CALL["thresholds"]["batch_size"] = 1


def tagged(text: str) -> set[str]:
    """The names a model would tag in text: alias-table names (longest first) and CVE IDs."""
    known = "|".join(map(re.escape, sorted(TABLE.by_key, key=len, reverse=True)))
    names = re.finditer(rf"\b(?:{known}|CVE-\d{{4}}-\d{{4,}})\b", text, re.IGNORECASE)
    return {m.group() for m in names}


def verdict(marked: str) -> str:
    """review or edge for the claim between the two [names] in marked."""
    head, tail = dict.fromkeys(re.findall(r"\[([^]]+)\]", marked))
    text = marked.replace("[", "").replace("]", "")
    return "review" if recipe.negated_sentence(text, head, tail, RULES)[0] else "edge"


# The claim joins the two [names]. The recipe never reads a negation as a denial: every sentence
# with a negation or hedging word goes to review, wherever the word sits.
NEGATION: dict[str, list[str]] = {
    "review": [
        "We found no evidence that [Saltmarsh Kettle] used [Thornpipe].",  # E6
        "[Gorse Moth] did not deploy [Thornpipe].",
        "[Thornpipe] has never been linked to [Saltmarsh Kettle].",
        "[Murkbell] can't be attributed to [Velvet Gorse].",
        "[Murkbell] has not been linked to [Velvet Gorse].",
        "[Velvet Gorse] never used [Thornpipe].",
        "[Saltmarsh Kettle] isn\u2019t using [Quillstep] anymore.",
        "[Saltmarsh Kettle] won't be targeting [port operators].",
        "There is, after a long review, no evidence that [Velvet Gorse] used [Thornpipe].",
        # Negations that are not about the claim, or not a denial of it, go to review too.
        "[Velvet Gorse] did not deploy [Thornpipe] until March 2024.",
        "[Velvet Gorse] never used [Thornpipe] before 2023.",
        "There is no sign that [Velvet Gorse] slowed down, and it used [Thornpipe] again.",
        "[Thornpipe] is not linked to any group other than [Velvet Gorse].",
        "[Velvet Gorse] did not use Thornpipe but used [Murkbell].",
        "[Velvet Gorse] did not use [Thornpipe] but used Murkbell.",
        "[Murkbell] is not attributed to Saltmarsh Kettle but to [Velvet Gorse].",
        "[Velvet Gorse], with no link to Saltmarsh Kettle, used [Thornpipe].",
        "[Velvet Gorse], which we have not linked to Saltmarsh Kettle, used [Thornpipe].",
        "[Gorse Moth] did not deploy Thornpipe this time, using [Murkbell] instead.",
        "[Velvet Gorse] did not use Murkbell; it used [Thornpipe].",
        "[Velvet Gorse], which did not use the loader, used [Murkbell].",
        "[Velvet Gorse] has not been observed using [Thornpipe].",
        "[Velvet Gorse] does not appear to use [Thornpipe].",
        "[Velvet Gorse] is not known to use [Thornpipe].",
        "[Velvet Gorse] no longer uses [Thornpipe].",
        "[Velvet Gorse], not Saltmarsh Kettle, used [Thornpipe].",
        "[Velvet Gorse] used [Thornpipe] but did not exploit CVE-2099-41877.",
        "[Velvet Gorse] used [Thornpipe], but we found no evidence that it stole data.",
        "[Velvet Gorse] used [Thornpipe]; investigators ruled out insider involvement.",
        "Although Saltmarsh Kettle never used Murkbell, [Velvet Gorse] used [Thornpipe].",
        "[Saltmarsh Kettle] targeted [port operators]; the attack was unrelated to Murkbell.",
        # Hedges: the report doubts the claim without denying it.
        "[Velvet Gorse] possibly uses [Quillstep].",  # E9
        "We suspect [Velvet Gorse] used [Thornpipe].",
        "[Velvet Gorse] is suspected of using [Thornpipe].",
        "[Velvet Gorse] likely used [Thornpipe].",
        "[Velvet Gorse] may have used [Thornpipe].",
        "May have used: [Velvet Gorse] and [Thornpipe].",
        "[Velvet Gorse] might use [Thornpipe].",
        "[Velvet Gorse] probably used [Thornpipe].",
        "[Velvet Gorse] reportedly used [Thornpipe].",
        # Names in different sentences: the sentences between them count.
        "[Saltmarsh Kettle] is active. It has never used [Thornpipe].",
        # The same pair in two sentences, one with a negation word.
        "[Velvet Gorse] used [Thornpipe] in May. No sign that Velvet Gorse used Thornpipe.",
    ],
    # No negation word: an edge. A denial without one ("stopped using") is a known limit.
    "edge": [
        "[Saltmarsh Kettle] is active. It used [Thornpipe].",
        "[Velvet Gorse] used the [Thornpipe backdoor].",
        "[Velvet Gorse] stopped using [Thornpipe] in 2025.",
        "Noted: [Velvet Gorse] used [Thornpipe] in another note.",
        # The month is not a hedge: only lower-case "may" is.
        "[Velvet Gorse] used [Thornpipe] in May.",
        # A hedge with no word from the list stays an edge: a known limit.
        "We assess with moderate confidence that [Velvet Gorse] used [Thornpipe].",
        # A sentence ends at "e.g. " too, so a negation before it is missed: a known limit.
        "We found no evidence, e.g. in logs, that [Velvet Gorse] used [Thornpipe].",
    ],
}
for expected, cases in NEGATION.items():
    for marked in cases:
        assert verdict(marked) == expected, f"{marked!r}: expected {expected}"
print(f"negation: {sum(map(len, NEGATION.values()))} crafted claims ok")

# In the committed reports only the denial (R-04), the hedged Quillstep claim (R-06) and the
# hedged Murkbell attribution (R-03) have a doubt word in a sentence with two known names.
negated = set()
for report in recipe.load_reports():
    for _, window in recipe.paragraph_windows(report.text, 200):
        names = tagged(window)
        for head in names:
            for tail in names - {head}:
                if recipe.negated_sentence(window, head, tail, RULES)[0]:
                    negated.add((report.id, frozenset({head, tail})))
assert negated == {
    ("R-04", frozenset({"Saltmarsh Kettle", "Thornpipe"})),
    ("R-06", frozenset({"Velvet Gorse", "Quillstep loader"})),
    ("R-03", frozenset({"Murkbell", "Saltmarsh Kettle"})),
}, negated
print("committed reports: three sentences with a doubt word (R-03, R-04, R-06) ok")

# Names: the alias table, unknown names, CVE IDs.
assert recipe.clean_name("  the Thornpipe backdoor's, ") == "Thornpipe backdoor"
assert TABLE.resolve("Gorse Moth", None) == Canon("Velvet Gorse", "threat actor", True)
assert TABLE.resolve("the Thornpipe backdoor,", None) == Canon("Thornpipe", "malware", True)
unknown = {
    TABLE.resolve(s, "threat actor") for s in ["Ashen Lark", "ASHEN LARK", "the Ashen Lark's"]
}
assert unknown == {Canon("Ashen Lark", "threat actor", False)}, unknown
assert TABLE.resolve("Ashen Lark", None) is None  # untagged and unknown: not a node
clash = [
    {"name": name, "type": "malware", "aliases": [alias]}
    for name, alias in [("A", "X"), ("B", "x")]
]
try:
    recipe.AliasTable.build({"aliases": clash})
    raise AssertionError("one alias for two names must fail")
except ValueError:
    pass
print("names ok")

for text in ["CVE-2099-41877", "(cve-2099-41877).", "CVE-2099-41877,"]:
    assert TABLE.resolve(text, None) == Canon("CVE-2099-41877", "vulnerability", True), text
assert TABLE.resolve("CVE-99-41877", None) is None
print("CVE punctuation ok")


# The pipeline after the model: map_relations and build_graph, fed hand-written stand-in replies.
class StandIn:
    """Stands in for SIEClient: extract() returns the hand-written reply for each item ID, or
    raises the error given for it."""

    def __init__(self, replies: dict[str, dict[str, Any] | Exception]) -> None:
        self.replies = replies
        self.calls = 0

    def extract(self, model: str, items: list[Item], **_: Any) -> list[dict[str, Any]]:
        self.calls += 1
        found = [self.replies[str(item["id"])] for item in items]
        for reply in found:
            if isinstance(reply, Exception):
                raise reply
        return [r for r in found if not isinstance(r, Exception)]


def run(*cases: tuple[str, str, str, dict[str, str], list[str]]) -> Graph:
    """map_relations on one-paragraph reports (id, publisher, text, entities, relations).

    entities maps each name to the label the stand-in model gives it, and each relation is
    "head | relation | tail". An entities value of "error" makes SIE fail to read the paragraph.
    """
    reports: list[recipe.Report] = []
    replies: dict[str, dict[str, Any] | Exception] = {}
    for report_id, publisher, text, entities, relations in cases:
        reports.append(recipe.Report(report_id, publisher, "2026-01-01", "t", text))
        spans = [
            {"text": n, "label": label, "start": text.find(n), "end": text.find(n) + len(n)}
            for n, label in entities.items()
        ]
        triples = [
            dict(zip(("head", "relation", "tail"), r.split(" | "), strict=True)) for r in relations
        ]
        replies[f"{report_id}#p1"] = (
            {"error": "timed out"}
            if "error" in entities.values()
            else {"entities": spans, "relations": [{**t, "score": 0.9} for t in triples]}
        )
    client: Any = StandIn(replies)
    return recipe.map_relations(client, reports, ONE_PER_CALL)


def edge(graph: Graph, head: str, relation: str, tail: str) -> Edge:
    return next(e for e in graph.edges if (e.head, e.relation, e.tail) == (head, relation, tail))


ACTOR, MALWARE = "threat actor", "malware"
VG_TP = {"Velvet Gorse": ACTOR, "Thornpipe": MALWARE}
R1 = ("R-1", "A", "Velvet Gorse used Thornpipe.", VG_TP, ["Velvet Gorse | uses | Thornpipe"])
R2 = (
    "R-2",
    "B",
    "Gorse Moth deployed the Thornpipe backdoor.",
    {"Gorse Moth": ACTOR, "Thornpipe backdoor": MALWARE},
    ["Gorse Moth | uses | Thornpipe backdoor"],
)
R3 = ("R-3", "A", "Velvet Gorse used Thornpipe again.", VG_TP, ["Velvet Gorse | uses | Thornpipe"])
both = edge(run(R1, R2), "Velvet Gorse", "uses", "Thornpipe")
assert both.status == recipe.MULTI_PUBLISHER, both
# Each source keeps the names as the report wrote them, before the alias table.
written = [(s.head_text, s.tail_text) for s in both.sources]
assert written == [("Velvet Gorse", "Thornpipe"), ("Gorse Moth", "Thornpipe backdoor")], written
# ... and graph.json carries them.
as_json = recipe.graph_json(run(R1, R2))["edges"][0]["sources"]
written = [(s["head_text"], s["tail_text"]) for s in as_json]
assert written == [("Velvet Gorse", "Thornpipe"), ("Gorse Moth", "Thornpipe backdoor")], as_json
one = edge(run(R1, R3), "Velvet Gorse", "uses", "Thornpipe")
assert one.status == recipe.SINGLE_SOURCE, one
assert "only 1 publisher(s): A" in one.flags, one
print("pipeline: multi_publisher, single_source, names as written ok")

# A paragraph SIE didn't read makes every would-be multi_publisher edge provisional.
graph = run(R1, R2, ("R-9", "C", "Unread.", {"x": "error"}, []))
assert graph.unread == ["R-9"], graph.unread
assert edge(graph, "Velvet Gorse", "uses", "Thornpipe").status == recipe.PROVISIONAL
# Only multi_publisher becomes provisional: single_source and review edges keep their status.
graph = run(
    (
        "R-5",
        "A",
        "Velvet Gorse targets ports.",
        {"Velvet Gorse": ACTOR, "ports": "sector"},
        ["Velvet Gorse | targets | ports"],
    ),
    (
        "R-6",
        "A",
        "Ashen Lark used Thornpipe.",
        {"Ashen Lark": ACTOR, "Thornpipe": MALWARE},
        ["Ashen Lark | uses | Thornpipe"],
    ),
    ("R-9", "C", "Unread.", {"x": "error"}, []),
)
kept = {(e.head, e.tail): e.status for e in graph.edges}
assert kept == {
    ("Velvet Gorse", "port operators"): recipe.SINGLE_SOURCE,
    ("Ashen Lark", "Thornpipe"): recipe.REVIEW,
}, kept
print("pipeline: unread report makes edges provisional ok")

# A doubt word in the sentence: review, never an edge. The edge other reports make is flagged.
R4 = (
    "R-4",
    "C",
    "Velvet Gorse did not deploy Thornpipe until March.",
    VG_TP,
    ["Velvet Gorse | uses | Thornpipe"],
)
graph = run(R1, R2, R4)
flagged = edge(graph, "Velvet Gorse", "uses", "Thornpipe")
assert flagged.status == recipe.REVIEW, flagged
assert flagged.publishers == ["A", "B"], flagged
assert any(f.startswith("R-4 has it in a sentence with a negation or") for f in flagged.flags)
alone = run(R4)
assert alone.edges == [], alone.edges
# The review item carries the model's score, so "the model was sure and wrong" can be checked.
assert alone.review[0].startswith(
    "R-4: Velvet Gorse uses Thornpipe (score 0.90) has a negation or hedging"
), alone.review
print("pipeline: negated claim goes to review ok")

# Two actors for one family is no contradiction by itself (shared or leaked malware): each edge
# keeps its own status and gets a note naming the other actor.
MB = "Murkbell"
graph = run(
    (
        "R-1",
        "A",
        "Murkbell is run by Velvet Gorse.",
        {MB: MALWARE, "Velvet Gorse": ACTOR},
        ["Murkbell | attributed to | Velvet Gorse"],
    ),
    (
        "R-2",
        "B",
        "We attribute Murkbell to Reedcutter.",
        {MB: MALWARE, "Reedcutter": ACTOR},
        ["Murkbell | attributed to | Reedcutter"],
    ),
)
assert [e.status for e in graph.edges] == [recipe.SINGLE_SOURCE] * 2, graph.edges
assert graph.edges[0].flags[0].startswith("also attributed to Velvet Gorse in other claims")
assert graph.edges[1].flags[0].startswith("also attributed to Saltmarsh Kettle in other"), graph
# The note is in the analyst queue too, one item per edge.
assert sorted(r.split(":")[0] for r in graph.review if "also attributed to" in r) == [
    "Murkbell attributed to Saltmarsh Kettle",
    "Murkbell attributed to Velvet Gorse",
], graph.review
# A negated attribution makes no edge, so it never shows up in the note.
graph = run(
    (
        "R-1",
        "A",
        "Murkbell is run by Velvet Gorse.",
        {MB: MALWARE, "Velvet Gorse": ACTOR},
        ["Murkbell | attributed to | Velvet Gorse"],
    ),
    (
        "R-2",
        "B",
        "Murkbell is not run by Reedcutter.",
        {MB: MALWARE, "Reedcutter": ACTOR},
        ["Murkbell | attributed to | Reedcutter"],
    ),
)
only = edge(graph, MB, "attributed to", "Velvet Gorse")
assert not any(f.startswith("also attributed to") for f in only.flags), only.flags
assert not any("also attributed to" in r for r in graph.review), graph.review
# With a second publisher each, both attributions are multi_publisher, still with the note.
graph = run(
    *[
        (f"R-{n}", pub, f"We attribute Murkbell to {actor}.", {MB: MALWARE, actor: ACTOR}, [rel])
        for n, pub, actor in [
            (1, "A", "Velvet Gorse"),
            (2, "B", "Velvet Gorse"),
            (3, "A", "Saltmarsh Kettle"),
            (4, "B", "Saltmarsh Kettle"),
        ]
        for rel in [f"Murkbell | attributed to | {actor}"]
    ]
)
assert [e.status for e in graph.edges] == [recipe.MULTI_PUBLISHER] * 2, graph.edges
print("pipeline: two actors on one family get a note, not a conflict ok")

# Unknown names go to review; orient turns a relation round or drops it.
graph = run(
    (
        "R-1",
        "A",
        "Ashen Lark used Quillstep.",
        {"Ashen Lark": ACTOR, "Quillstep": MALWARE},
        ["Ashen Lark | uses | Quillstep"],
    ),
    (
        "R-2",
        "B",
        "Thornpipe is the tool of Velvet Gorse.",
        VG_TP,
        ["Thornpipe | uses | Velvet Gorse", "Thornpipe | targets | Velvet Gorse"],
    ),
)
assert edge(graph, "Ashen Lark", "uses", "Quillstep").status == recipe.REVIEW
assert "Ashen Lark (threat actor) is not in the alias table: new or an alias?" in graph.review
turned = edge(graph, "Velvet Gorse", "uses", "Thornpipe")
assert recipe.TURNED in turned.flags, turned
assert [(s.head_text, s.tail_text) for s in turned.sources] == [("Velvet Gorse", "Thornpipe")]
assert [d.reason for d in graph.dropped] == ["malware -> threat actor does not fit 'targets'"]
# A turned-round claim sends the edge to review, even when another publisher states it as is.
graph = run(
    R1, ("R-2", "B", "Thornpipe, Velvet Gorse.", VG_TP, ["Thornpipe | uses | Velvet Gorse"])
)
turned = edge(graph, "Velvet Gorse", "uses", "Thornpipe")
assert turned.status == recipe.REVIEW, turned
assert graph.review == ["Velvet Gorse uses Thornpipe: turned round to fit the relation's types"]

# Malware uses malware: the types fit either way round, so they can't vouch for the direction.
# Two publishers that disagree on direction both go to review, neither reaches multi_publisher.
QS = {"Quillstep": MALWARE, "Murkbell": MALWARE}
graph = run(
    ("R-1", "A", "Murkbell drops Quillstep.", QS, ["Murkbell | uses | Quillstep"]),
    ("R-2", "B", "Quillstep drops Murkbell.", QS, ["Quillstep | uses | Murkbell"]),
    ("R-3", "C", "Murkbell loads Quillstep.", QS, ["Murkbell | uses | Quillstep"]),
)
either_way = {(e.head, e.tail): (e.status, e.publishers) for e in graph.edges}
assert either_way == {
    ("Murkbell", "Quillstep"): (recipe.REVIEW, ["A", "C"]),
    ("Quillstep", "Murkbell"): (recipe.REVIEW, ["B"]),
}, either_way
# Each source keeps the ends as its report wrote them: an either-way claim is never turned.
sources = {(e.head, e.tail): [(s.head_text, s.tail_text) for s in e.sources] for e in graph.edges}
assert sources[("Murkbell", "Quillstep")] == [("Murkbell", "Quillstep")] * 2, sources
assert sources[("Quillstep", "Murkbell")] == [("Quillstep", "Murkbell")], sources
assert all(recipe.EITHER_WAY in e.flags for e in graph.edges), graph.edges
print("pipeline: unknown name, orient and unverifiable direction ok")

# Dropped: a generic word, a name not in the window, and malware "using" a victim's product.
# Malware may use software, and an unlisted vulnerability goes to review, not to the dropped
# list.
graph = run(
    (
        "R-1",
        "A",
        "Operators used Halvard Gateway. Murkbell exploits code overlap. Murkbell uses a packer.",
        {
            "Operators": ACTOR,
            "Halvard Gateway": "software",
            MB: MALWARE,
            "code overlap": "vulnerability",
            "packer": "software",
        },
        [
            "Operators | uses | Halvard Gateway",
            "Murkbell | exploits | code overlap",
            "Velvet Gorse | uses | Murkbell",
            "Murkbell | uses | Halvard Gateway",
            "Murkbell | uses | packer",
        ],
    ),
)
reasons = [d.reason for d in graph.dropped]
assert reasons == [
    "'Operators' is a generic word, not an entity",
    "head or tail is not in the report text",
    "malware -> product does not fit 'uses'",
], reasons
statuses = {(e.head, e.relation, e.tail): e.status for e in graph.edges}
assert statuses == {
    ("Murkbell", "exploits", "code overlap"): recipe.REVIEW,
    ("Murkbell", "uses", "packer"): recipe.REVIEW,  # an edge; review only as packer is unknown
}, statuses
question = "code overlap (vulnerability) is not a CVE ID or in the alias table: a new flaw"
assert any(item.startswith(question) for item in graph.review), graph.review
# A victim's product is never a backlog tool, even when a group "uses" it in the model's
# reading and targets one of our sectors. It can still be a target.
graph = run(
    (
        "R-1",
        "A",
        "Velvet Gorse abused Halvard Gateway against port operators. Thornpipe targets Halvard"
        " Gateway.",
        {"Velvet Gorse": ACTOR, "Halvard Gateway": "software", "port operators": "sector"},
        [
            "Velvet Gorse | uses | Halvard Gateway",
            "Velvet Gorse | targets | port operators",
            "Thornpipe | targets | Halvard Gateway",
        ],
    ),
)
assert recipe.build_backlog(graph, CATALOG, TABLE, RULES) == []
assert {(e.head, e.relation, e.tail) for e in graph.edges} == {
    ("Velvet Gorse", "targets", "port operators"),
    ("Thornpipe", "targets", "Halvard Gateway"),
}, graph.edges
# An entity span whose text is not the window text at its offsets is dropped with a note.
bad = {"entities": [{"text": "Velvet Gorse", "label": ACTOR, "start": 1, "end": 13}]}
assert recipe.window_labels("Velvet Gorse used it.", bad, RULES)[0] == {}
# Malformed items are dropped with a note, not a crash: a non-object or a float offset in
# entities, a non-object or a null or non-numeric score in relations.
gorse = {"text": "Velvet Gorse", "label": ACTOR, "start": 0, "end": 12}
malformed = {
    "entities": ["Velvet Gorse", gorse | {"start": 0.0}],
    "relations": [
        "Velvet Gorse uses Thornpipe",
        {"head": "Velvet Gorse", "relation": "uses", "tail": "Thornpipe", "score": None},
        {"head": "Velvet Gorse", "relation": "uses", "tail": "Thornpipe", "score": "high"},
    ],
}
report = recipe.Report("R-X", "p", "2026-01-01", "t", "Velvet Gorse used Thornpipe.")
checked = recipe.window_claims(report, report.text, malformed, TABLE, RULES)
assert (checked[0], checked[1], len(checked[2])) == ([], [], 5), checked
assert all(note.startswith("invalid ") for note in checked[2]), checked
print("pipeline: dropped relations ok")

# Mermaid: quotes escaped, one ID per node, dashed unless multi_publisher, red for review.
nodes = [
    Node('Say "hi"', "malware", False),
    Node("A b", "malware", True),
    Node("a-b", "sector", True),
]
edges = [
    Edge('Say "hi"', "targets", "a-b", recipe.REVIEW),
    Edge("A b", "targets", "a-b", recipe.PROVISIONAL),
    Edge("A b", "uses", "a-b", recipe.MULTI_PUBLISHER),
]
chart = recipe.mermaid(Graph(nodes, edges, [], []))
assert 'n0[["Say #quot;hi#quot;<br/><i>malware</i> ?"]]' in chart, chart
assert all(part in chart for part in ['n1[["A b', 'n2["a-b', "n0 -.->", "n1 -.->", "n1 -->"])
assert "linkStyle 0 stroke" in chart
print("mermaid ok")

# CSV: a cell a spreadsheet would run as a formula gets a leading '.
for raw in ["=1+1", "+x", "-x", "@x", "\tx", "\rx"]:
    assert recipe.csv_safe(raw) == "'" + raw
assert recipe.csv_safe("Velvet Gorse") == "Velvet Gorse"
with tempfile.TemporaryDirectory() as out:
    hostile = Graph([], [], [], [])
    rows = [Row("=HYPERLINK(1)", "malware", recipe.NEEDS_REVIEW, groups=["@x"])]
    recipe.write_outputs(Path(out), hostile, rows, RULES)
    line = (Path(out) / "backlog.csv").read_text(encoding="utf-8").splitlines()[1]
    assert line.startswith("'=HYPERLINK(1),malware,needs_review,,'@x,"), line
print("csv_safe ok")

# Windows: a sentence longer than max_words is cut on words, and offsets point at the text.
text = " ".join(f"w{i}" for i in range(450)) + ". Short one."
windows = recipe.paragraph_windows(text, 200)
assert [len(w.split()) for _, w in windows] == [200, 200, 52], windows
assert all(text[offset : offset + len(w)] == w for offset, w in windows)
print("windows ok")


# --- The backlog: the committed reports with stand-in replies --------------------------------

# Relations per window ("head | relation | tail"), close to what the model returned on our live
# run, including the claims the doubt words hold back. Entity labels come from the alias table;
# names it doesn't know take the label given here.
COMMITTED: dict[str, list[str]] = {
    "R-01#p1": [
        "Velvet Gorse | uses | Thornpipe backdoor",
        "Thornpipe | exploits | CVE-2099-41877",
        "Thornpipe | targets | Halvard Gateway appliances",
    ],
    "R-01#p2": ["Velvet Gorse | targets | water utilities", "Operators | uses | Halvard Gateway"],
    "R-02#p1": [
        "Gorse Moth | uses | Thornpipe",
        "Thornpipe | targets | freight broker",
        "Velvet Gorse | alias of | Gorse Moth",
    ],
    "R-02#p2": ["Murkbell | uses | packer"],
    "R-03#p1": [
        "Saltmarsh Kettle | uses | Quillstep loader",
        "Saltmarsh Kettle | targets | port operators",
    ],
    "R-03#p2": ["Murkbell | attributed to | Saltmarsh Kettle"],
    "R-04#p1": [
        "Thornpipe | exploits | CVE-2099-41877",
        "Saltmarsh Kettle | uses | Quillstep",
    ],
    "R-04#p2": ["Saltmarsh Kettle | uses | Thornpipe"],
    "R-05#p1": ["Reedcutter | targets | port operators"],
    "R-05#p2": [
        "Ashen Lark | uses | Quillstep",
        "Quillstep | targets | regional hospital network",
    ],
    "R-06#p1": ["Velvet Gorse | uses | Larchwick", "Saltmarsh Kettle | uses | Larchwick"],
    "R-06#p2": ["Velvet Gorse | uses | Quillstep loader"],
}
LABELS = {
    "Ashen Lark": ACTOR,
    "Operators": ACTOR,
    "packer": "software",
    "regional hospital network": "sector",
}


def stand_in_reply(window: str, relations: list[str]) -> dict[str, Any]:
    """A reply with a span for every name in the relations, labelled by the alias table."""
    entities, triples = [], []
    for relation in relations:
        head, rel, tail = relation.split(" | ")
        triples.append({"head": head, "relation": rel, "tail": tail, "score": 0.9})
        for name in (head, tail):
            canon = TABLE.resolve(name, LABELS.get(name))
            assert canon is not None, f"give {name!r} a label in LABELS"
            # The model gives only our labels; the table's own type (product) is applied after.
            label = canon.type if canon.type in RULES["entity_labels"] else "software"
            start = window.index(name)
            entities.append(
                {"text": name, "label": label, "start": start, "end": start + len(name)}
            )
    return {"entities": entities, "relations": triples}


def committed_run(
    replace: dict[str, list[str]] | None = None,
    errors: dict[str, Exception] | None = None,
    catalog: RuleCatalog = CATALOG,
    rules: dict[str, Any] = ONE_PER_CALL,
) -> tuple[Graph, list[Row]]:
    """find_gaps on the committed reports, with COMMITTED (and any replacements) as replies."""
    reports = recipe.load_reports()
    relations = {**COMMITTED, **(replace or {})}
    replies: dict[str, dict[str, Any] | Exception] = {}
    for report in reports:
        windows = recipe.paragraph_windows(report.text, 200)
        for number, (_, window) in enumerate(windows, start=1):
            key = f"{report.id}#p{number}"
            replies[key] = (errors or {}).get(key) or stand_in_reply(window, relations[key])
    client: Any = StandIn(replies)
    with contextlib.redirect_stdout(io.StringIO()):  # the "n of m calls failed" line
        return recipe.find_gaps(client, reports, rules, catalog)


def by_tool(rows: list[Row]) -> dict[str, Row]:
    return {row.tool: row for row in rows}


graph, rows = committed_run()
found = {r.tool: (r.status, r.groups) for r in rows}
assert found == {
    # The case the recipe exists for: Saltmarsh Kettle targets port operators, no rule.
    "Quillstep": (recipe.NO_RULE, ["Saltmarsh Kettle"]),
    # Two groups on one commodity stealer, both listed. DR-0103 covers "Larchwood stealer", a
    # near name that never counts as a match.
    "Larchwick": (recipe.NO_RULE, ["Saltmarsh Kettle", "Velvet Gorse"]),
    # Only a hedged attribution.
    "Murkbell": (recipe.NEEDS_REVIEW, ["Saltmarsh Kettle"]),
    # DR-0101 covers it as "Thornpipe implant", an alias in the table. Velvet Gorse targets
    # freight through Thornpipe in one R-02 sentence. The R-04 denial adds no group.
    "Thornpipe": (recipe.RULE_EXISTS, ["Velvet Gorse"]),
}, found
rows_by = by_tool(rows)
assert [r.tool for r in rows] == ["Larchwick", "Quillstep", "Murkbell", "Thornpipe"], rows
assert rows_by["Thornpipe"].rules == ["DR-0101 Thornpipe implant beacon to a staging host"]
assert rows_by["Thornpipe"].sectors == ["freight"], rows_by["Thornpipe"]
denial = rows_by["Thornpipe"].held_back
assert len(denial) == 1
assert denial[0].startswith("R-04: Saltmarsh Kettle uses Thornpipe: a neg")
assert "We found no evidence" not in " ".join(rows_by["Thornpipe"].sentences)
assert rows_by["Larchwick"].notes == [
    "near name in the catalog, not counted: DR-0103 covers 'Larchwood stealer'"
    " (looked up 'Larchwick')"
], rows_by["Larchwick"].notes
assert rows_by["Larchwick"].sectors == ["freight", "port operators"]
assert rows_by["Quillstep"].held_back == [
    "R-06: Velvet Gorse uses Quillstep: a negation or hedging word in its sentence: 'Velvet"
    " Gorse possibly uses the Quillstep loader as well, but we have a single sample.'"
], rows_by["Quillstep"].held_back
# Ashen Lark used Quillstep against a hospital network: not our sector, so not on the row.
assert not any(
    "Ashen Lark" in s for s in rows_by["Quillstep"].sentences + rows_by["Quillstep"].held_back
)
assert rows_by["Quillstep"].publishers == ["Dunmore Intel", "Northfold CERT"], rows_by["Quillstep"]
# A needs_review row still shows the sentence behind it and the publisher.
murkbell = rows_by["Murkbell"]
assert murkbell.sentences[0] == (
    "R-03: Based on code overlap and shared infrastructure, Murkbell is possibly attributable to"
    " Saltmarsh Kettle."
), murkbell.sentences
assert "Northfold CERT" in murkbell.publishers, murkbell
print("backlog: the README's cases on the committed reports ok")

# A tool reaches the backlog only through a group that targets our sectors: without R-02's
# freight broker, Velvet Gorse targets only water utilities, so it drops off Larchwick, and
# Thornpipe is left with the R-04 denial, held back (the rule still exists).
graph, rows = committed_run(replace={"R-02#p1": ["Gorse Moth | uses | Thornpipe"]})
assert by_tool(rows)["Larchwick"].groups == ["Saltmarsh Kettle"], rows
thornpipe = by_tool(rows)["Thornpipe"]
assert thornpipe.status == recipe.RULE_EXISTS
assert thornpipe.groups == ["Saltmarsh Kettle"]
assert thornpipe.held_back[0].startswith("R-04: Saltmarsh Kettle uses Thornpipe: a negation")
# Targeting through a tool counts only in one sentence: R-01 says Velvet Gorse used Thornpipe,
# and R-02 says Thornpipe hit a freight broker, in different reports. Not a chain.
graph, rows = committed_run(replace={"R-02#p1": ["Thornpipe | targets | freight broker"]})
assert "Velvet Gorse" not in by_tool(rows)["Thornpipe"].groups, rows
assert by_tool(rows)["Larchwick"].groups == ["Saltmarsh Kettle"], rows
print("backlog: only groups that target our sectors, through a tool in one sentence ok")


# --- Adversarial stand-ins: each would give a confident wrong status without its fix ----------

# 1. The use is clear, but the only link to our sector is held back (a hedge on targeting).
SK_QS = {"Saltmarsh Kettle": ACTOR, "Quillstep": MALWARE}
SK_PO = {"Saltmarsh Kettle": ACTOR, "port operators": "sector"}
graph = run(
    (
        "R-1",
        "A",
        "Saltmarsh Kettle used Quillstep.",
        SK_QS,
        ["Saltmarsh Kettle | uses | Quillstep"],
    ),
    (
        "R-2",
        "B",
        "Saltmarsh Kettle possibly targets port operators.",
        SK_PO,
        ["Saltmarsh Kettle | targets | port operators"],
    ),
)
rows = recipe.build_backlog(graph, CATALOG, TABLE, RULES)
assert [(r.tool, r.status) for r in rows] == [("Quillstep", recipe.NEEDS_REVIEW)], rows
assert "that Saltmarsh Kettle targets port operators is held back" in rows[0].held_back[0], rows

ADVERSARIAL: list[tuple[str, dict[str, list[str]], dict[str, str]]] = [
    # 2. The model reads the claim backwards ("Larchwick uses Saltmarsh Kettle"): turned round,
    #    so the edge is in review and the chain is held back. Velvet Gorse's claim stays clear.
    (
        "turned round",
        {"R-06#p1": ["Velvet Gorse | uses | Larchwick", "Larchwick | uses | Saltmarsh Kettle"]},
        {"Larchwick": recipe.NO_RULE},
    ),
    (
        "turned round, alone",
        {"R-06#p1": ["Larchwick | uses | Saltmarsh Kettle"]},
        {"Larchwick": recipe.NEEDS_REVIEW},
    ),
]
for case, replace, want in ADVERSARIAL:
    graph, rows = committed_run(replace=replace)
    came_out = {tool: by_tool(rows)[tool].status for tool in want if tool in by_tool(rows)}
    assert came_out == want, (case, came_out, rows)

# 3. One span holding two tools: an unknown name, so its row needs review; never no_rule and
#    never silently dropped.
LABELS["Quillstep loader to gain"] = MALWARE
graph, rows = committed_run(
    replace={"R-03#p1": ["Saltmarsh Kettle | uses | Quillstep loader to gain"]}
)
assert by_tool(rows)["Quillstep loader to gain"].status == recipe.NEEDS_REVIEW, rows

# 4. A shorter name is not a match, and a near name is only a note: "Quill" and "Quillsteps"
#    in the catalog leave Quillstep at no_rule. A rule on an alias ("Quillstep loader") counts.
with tempfile.TemporaryDirectory() as folder:
    seed = Path(folder) / "rules.csv"
    seed.write_text(
        "rule_id,title,platform,covers\nDR-1,Quill,EDR,Quill\nDR-2,Quillsteps,EDR,Quillsteps\n",
        encoding="utf-8",
    )
    graph, rows = committed_run(catalog=RuleCatalog(seed))
    quill = by_tool(rows)["Quillstep"]
    assert quill.status == recipe.NO_RULE, quill
    assert quill.rules == [], quill
    assert quill.notes == [
        "near name in the catalog, not counted: DR-2 covers 'Quillsteps' (looked up 'Quillstep')"
    ], quill.notes
    seed.write_text(
        "rule_id,title,platform,covers\nDR-3,QS,EDR,the quillstep LOADER's\n", encoding="utf-8"
    )
    graph, rows = committed_run(catalog=RuleCatalog(seed))
    assert by_tool(rows)["Quillstep"].status == recipe.RULE_EXISTS, rows
    # A rule that covers the tool is not also a near-name note, even when it lists a near name.
    seed.write_text(
        "rule_id,title,platform,covers\nDR-4,QS,EDR,Quillstep loader;Quillsteps\n",
        encoding="utf-8",
    )
    graph, rows = committed_run(catalog=RuleCatalog(seed))
    quill = by_tool(rows)["Quillstep"]
    assert (quill.status, quill.notes) == (recipe.RULE_EXISTS, []), quill

# 5. A tool the alias table doesn't know: even with clear claims and no rule, its row needs a
#    person (it may be another vendor's name for a tool the catalog covers).
graph = run(
    (
        "R-1",
        "A",
        "Saltmarsh Kettle used Brinefang against port operators.",
        {"Saltmarsh Kettle": ACTOR, "Brinefang": MALWARE, "port operators": "sector"},
        ["Saltmarsh Kettle | uses | Brinefang", "Saltmarsh Kettle | targets | port operators"],
    ),
)
rows = recipe.build_backlog(graph, CATALOG, TABLE, RULES)
assert [(r.tool, r.status) for r in rows] == [("Brinefang", recipe.NEEDS_REVIEW)], rows
assert "Brinefang is not in the alias table" in rows[0].held_back[0], rows
# 6. A denial is the only claim about a tool: held back, never no_rule. With no clear use of
#    Quillstep left (R-03 and R-04 now say nothing about it), the R-06 hedge alone needs review.
graph, rows = committed_run(
    replace={"R-03#p1": ["Saltmarsh Kettle | targets | port operators"], "R-04#p1": []}
)
quill = by_tool(rows)["Quillstep"]
assert quill.status == recipe.NEEDS_REVIEW, quill
assert quill.groups == ["Velvet Gorse"], quill
print("adversarial: every confident-wrong attempt held back ok")


# --- SIE call failures --------------------------------------------------------------------------

TRANSIENT: list[SIEError] = [
    SIEConnectionError("connection refused"),
    ServerError("unavailable", status_code=503),
    RateLimitError("slow down"),
    InputTooLongError("too long"),
    ProvisioningError("no GPU yet"),
    ModelLoadingError("loading"),
    ModelLoadFailedError("try later", permanent=False),
    SIEError("an SIE error with no subclass"),
]
FATAL: list[SIEError] = [
    RequestError("bad key", status_code=401),
    RequestError("no such model", status_code=404),
    InsufficientCreditsError("no credits"),
    SpendLimitError("spend limit"),
    AccountInactiveError("inactive"),
    ModelLoadFailedError("gated", permanent=True),
]


def stopped(call: Callable[[], object]) -> str:
    """The SIEStopped message the call raises; fails when it raises nothing."""
    try:
        call()
    except recipe.SIEStopped as error:
        return str(error)
    raise AssertionError("the run did not stop")


def raised(call: Callable[[], object]) -> BaseException:
    """Whatever the call raises, SystemExit included; fails when it raises nothing."""
    try:
        call()
    except BaseException as error:  # SystemExit too: the check is on which one it was
        return error
    raise AssertionError("nothing was raised")


for error in TRANSIENT:
    assert not recipe.is_fatal(error), f"{type(error).__name__} should be transient"
for error in FATAL:
    assert recipe.is_fatal(error), f"{type(error).__name__} should stop the run"
# A transient error on one window: its report is partly unread, the run goes on, and a row that
# would be no_rule goes to review with the reason. rule_exists stays: it is a catalog fact.
for error in TRANSIENT:
    graph, rows = committed_run(errors={"R-05#p2": error})
    assert graph.unread == ["R-05"], (type(error).__name__, graph.unread)
    assert graph.review[0].startswith("R-05 paragraph 2 was not read: the SIE call failed: ")
    tool_status = {r.tool: r.status for r in rows}
    assert tool_status == {
        "Larchwick": recipe.NEEDS_REVIEW,
        "Quillstep": recipe.NEEDS_REVIEW,
        "Murkbell": recipe.NEEDS_REVIEW,
        "Thornpipe": recipe.RULE_EXISTS,
    }, tool_status
    assert "partly unread" in by_tool(rows)["Quillstep"].notes[-1]
# A reply missing its key is a failed call on the same path; empty lists are "nothing found".
reports = [recipe.Report("R-1", "A", "2026-01-01", "t", "Nothing here.")]
reports.append(recipe.Report("R-2", "A", "2026-01-01", "t", "Nothing here either."))
empty: dict[str, Any] = {"entities": [], "relations": []}
lost: Any = StandIn({"R-1#p1": {"entities": []}, "R-2#p1": dict(empty)})
with contextlib.redirect_stdout(io.StringIO()):
    graph = recipe.map_relations(lost, reports, ONE_PER_CALL)
assert graph.unread == ["R-1"], graph.unread
assert "replied without 'entities' or 'relations'" in graph.review[0], graph.review
# A key that is there but null is a failed call too, not "nothing found".
lost = StandIn({"R-1#p1": {"entities": None, "relations": []}, "R-2#p1": dict(empty)})
with contextlib.redirect_stdout(io.StringIO()):
    graph = recipe.map_relations(lost, reports, ONE_PER_CALL)
assert graph.unread == ["R-1"], graph.unread


class Short(StandIn):
    """The first call returns one result fewer than it was sent."""

    def extract(self, model: str, items: list[Item], **kw: Any) -> list[dict[str, Any]]:
        found = super().extract(model, items, **kw)
        return found[:-1] if self.calls == 1 else found


# A reply with one result fewer than sent is a failed call on the transient path, not a crash.
pairs = copy.deepcopy(RULES)
pairs["thresholds"]["batch_size"] = 2
reports += [recipe.Report(f"R-{n}", "A", "2026-01-01", "t", "Nothing.") for n in (3, 4)]
short: Any = Short({f"R-{n}#p1": dict(empty) for n in range(1, 5)})
with contextlib.redirect_stdout(io.StringIO()):
    graph = recipe.map_relations(short, reports, pairs)
assert graph.unread == ["R-1", "R-2"], graph.unread
assert "replied with 1 results for 2" in graph.review[0], graph.review
# Every call failed (a dead SIE_BASE_URL): the run stops, with the full message.
long = "connection refused " + "x" * 300
model = RULES["models"]["relations"]
dead = {f"R-0{n}#p{p}": SIEConnectionError(long) for n in range(1, 7) for p in (1, 2)}
message = stopped(lambda: committed_run(errors=dead))
assert message.startswith(f"Every SIE call failed (12 of 12 to {model})"), message
assert long in message, message
assert "Check SIE_BASE_URL and your network." in message, message
busy = {key: ModelLoadingError("loading") for key in dead}
message = stopped(lambda: committed_run(errors=busy))
assert "may still be starting or SIE is busy; run again in a few minutes." in message
# A refusal stops the run at once, with the full message, HTTP status and code.
message = stopped(
    lambda: committed_run(errors={"R-03#p1": InsufficientCreditsError("no credits " + "y" * 300)})
)
assert "InsufficientCreditsError (HTTP 402, code INSUFFICIENT_CREDITS)" in message, message
assert "y" * 300 in message
assert "SIE_API_KEY" in message
message = stopped(
    lambda: committed_run(errors={"R-01#p1": ModelLoadFailedError("gated", permanent=True)})
)
assert message.startswith(f"SIE can't load {model}"), message
message = stopped(lambda: committed_run(errors={"R-01#p1": RequestError("unexpected body")}))
assert "SIE_API_KEY" not in message, message
assert "SIE_BASE_URL" in message, message
# A bug in our own code is raised as is; main turns only SIEStopped into a clean exit.
try:
    committed_run(errors={"R-01#p1": TypeError("bad argument")})
except TypeError:
    pass
else:
    raise AssertionError("TypeError was swallowed")


def bug() -> None:
    raise RuntimeError("a bug in recipe.py")


def refused() -> None:
    raise recipe.SIEStopped("SIE refused")


caught = raised(lambda: recipe.stop_on_refusal(bug))
assert type(caught) is RuntimeError
assert str(caught) == "a bug in recipe.py"
caught = raised(lambda: recipe.stop_on_refusal(refused))
assert type(caught) is SystemExit
assert str(caught) == "Stopped: SIE refused"
print("sie failure: transient to unread; missing key fails; refusals and all-failed stop ok")

# Our sectors must be sectors in the alias table, checked before any model call.
typo = copy.deepcopy(ONE_PER_CALL)
typo["our_sectors"] = ["port operators", "fright"]
client = StandIn({})
caught = raised(lambda: recipe.map_relations(cast(Any, client), recipe.load_reports(), typo))
assert "'fright' is not a sector" in str(caught), caught
assert client.calls == 0
print("our_sectors: a typo stops the run before any model call ok")
print("all checks passed")
