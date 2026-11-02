"""Offline checks of the recipe's own parsing and rules. No SIE call, no recorded responses.

The checks use hand-written sentences, the committed data/reports.jsonl, and hand-written
stand-in model replies that feed map_relations the way SIE's extract would.

uv run --frozen python selfcheck.py
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

from sie_sdk.types import Item

import recipe
from recipe import Canon, Edge, Graph, Node

RULES = recipe.load_rules()
TABLE = recipe.AliasTable.build(RULES)


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

# In the committed reports only E6 (a negation) and E9 (a hedge) have a doubt word in a sentence
# with two known names.
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
}, negated
print("committed reports: two sentences with a doubt word (E6 R-04, E9 R-06) ok")

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
    """Stands in for SIEClient: extract() returns the hand-written reply for each item ID."""

    def __init__(self, replies: dict[str, dict[str, Any]]) -> None:
        self.replies = replies

    def extract(self, model: str, items: list[Item], **_: Any) -> list[dict[str, Any]]:
        return [self.replies[str(item["id"])] for item in items]


def run(*cases: tuple[str, str, str, dict[str, str], list[str]]) -> Graph:
    """map_relations on one-paragraph reports (id, publisher, text, entities, relations).

    entities maps each name to the label the stand-in model gives it, and each relation is
    "head | relation | tail". An entities value of "error" makes SIE fail to read the paragraph.
    """
    reports, replies = [], {}
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
    return recipe.map_relations(client, reports, RULES)


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
assert all(recipe.EITHER_WAY in e.flags for e in graph.edges), graph.edges
print("pipeline: unknown name, orient and unverifiable direction ok")

# Dropped: a generic word and a name not in the window. Malware may use software, and an
# unlisted vulnerability goes to review, not to the dropped list.
graph = run(
    (
        "R-1",
        "A",
        "Operators used Halvard Gateway. Murkbell exploits code overlap.",
        {
            "Operators": ACTOR,
            "Halvard Gateway": "software",
            MB: MALWARE,
            "code overlap": "vulnerability",
        },
        [
            "Operators | uses | Halvard Gateway",
            "Murkbell | exploits | code overlap",
            "Velvet Gorse | uses | Murkbell",
            "Murkbell | uses | Halvard Gateway",
        ],
    ),
)
reasons = [d.reason for d in graph.dropped]
assert reasons == [
    "'Operators' is a generic word, not an entity",
    "head or tail is not in the report text",
], reasons
statuses = {(e.head, e.relation, e.tail): e.status for e in graph.edges}
assert statuses == {
    ("Murkbell", "exploits", "code overlap"): recipe.REVIEW,
    ("Murkbell", "uses", "Halvard Gateway"): recipe.SINGLE_SOURCE,
}, statuses
question = "code overlap (vulnerability) is not a CVE ID or in the alias table: a new flaw"
assert any(item.startswith(question) for item in graph.review), graph.review
# An entity span whose text is not the window text at its offsets is dropped with a note.
bad = {"entities": [{"text": "Velvet Gorse", "label": ACTOR, "start": 1, "end": 13}]}
assert recipe.window_labels("Velvet Gorse used it.", bad, RULES)[0] == {}
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
    hostile = Graph(
        [Node("=HYPERLINK(1)", "malware", False), Node("@x", "software", False)],
        [Edge("=HYPERLINK(1)", "uses", "@x", recipe.REVIEW)],
        [],
        [],
    )
    recipe.write_outputs(Path(out), hostile)
    row = (Path(out) / "edges.csv").read_text(encoding="utf-8").splitlines()[1]
    assert row.startswith("'=HYPERLINK(1),uses,'@x,review"), row
print("csv_safe ok")

# Windows: a sentence longer than max_words is cut on words, and offsets point at the text.
text = " ".join(f"w{i}" for i in range(450)) + ". Short one."
windows = recipe.paragraph_windows(text, 200)
assert [len(w.split()) for _, w in windows] == [200, 200, 52], windows
assert all(text[offset : offset + len(w)] == w for offset, w in windows)
print("windows ok")
