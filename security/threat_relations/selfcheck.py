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
# with a negation word goes to review, wherever the word sits.
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
        # A sentence ends at "e.g. " too, so a negation before it is missed: a known limit.
        "We found no evidence, e.g. in logs, that [Velvet Gorse] used [Thornpipe].",
    ],
}
for expected, cases in NEGATION.items():
    for marked in cases:
        assert verdict(marked) == expected, f"{marked!r}: expected {expected}"
print(f"negation: {sum(map(len, NEGATION.values()))} crafted claims ok")

# Only E6 in the committed reports has a negation word in a sentence with two known names.
negated = set()
for report in recipe.load_reports():
    for _, window in recipe.paragraph_windows(report.text, 200):
        names = tagged(window)
        for head in names:
            for tail in names - {head}:
                if recipe.negated_sentence(window, head, tail, RULES)[0]:
                    negated.add((report.id, frozenset({head, tail})))
assert negated == {("R-04", frozenset({"Saltmarsh Kettle", "Thornpipe"}))}, negated
print("committed reports: one sentence with a negation word (E6, R-04) ok")

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
assert edge(run(R1, R2), "Velvet Gorse", "uses", "Thornpipe").status == recipe.CORROBORATED
one = edge(run(R1, R3), "Velvet Gorse", "uses", "Thornpipe")
assert one.status == recipe.SINGLE_SOURCE, one
assert "only 1 publisher(s): A" in one.flags, one
print("pipeline: corroborated, single_source ok")

# A paragraph SIE didn't read makes every would-be corroborated edge provisional.
graph = run(R1, R2, ("R-9", "C", "Unread.", {"x": "error"}, []))
assert graph.unread == ["R-9"], graph.unread
assert edge(graph, "Velvet Gorse", "uses", "Thornpipe").status == recipe.PROVISIONAL
print("pipeline: unread report makes edges provisional ok")

# A negation word in the sentence: review, never an edge. The edge other reports make is flagged.
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
assert any(f.startswith("R-4 has it in a sentence with a negation word") for f in flagged.flags)
alone = run(R4)
assert alone.edges == [], alone.edges
assert "R-4: Velvet Gorse uses Thornpipe has a negation word" in alone.review[0], alone.review
print("pipeline: negated claim goes to review ok")

# attributed to is single-valued: two actors for one family conflict.
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
assert [e.status for e in graph.edges] == [recipe.CONFLICTING] * 2, graph.edges
assert "other reports say attributed to Velvet Gorse" in graph.edges[0].flags
print("pipeline: single-valued conflict ok")

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
assert "turned round to fit the relation's types" in turned.flags, turned
assert [d.reason for d in graph.dropped] == ["malware -> threat actor does not fit 'targets'"]
print("pipeline: unknown name and orient ok")

# Dropped: a generic word, an unlisted vulnerability, a name not in the window.
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
        ],
    ),
)
reasons = [d.reason for d in graph.dropped]
assert reasons == [
    "'Operators' is a generic word, not an entity",
    "'code overlap' is a vulnerability that is not a CVE ID or in the alias table",
    "head or tail is not in the report text",
], reasons
assert graph.edges == []
# An entity span whose text is not the window text at its offsets is dropped with a note.
bad = {"entities": [{"text": "Velvet Gorse", "label": ACTOR, "start": 1, "end": 13}]}
assert recipe.window_labels("Velvet Gorse used it.", bad, RULES)[0] == {}
print("pipeline: dropped relations ok")

# Mermaid: quotes escaped, one ID per node, dashed unless corroborated, red when in doubt.
nodes = [
    Node('Say "hi"', "malware", False),
    Node("A b", "malware", True),
    Node("a-b", "sector", True),
]
edges = [
    Edge('Say "hi"', "targets", "a-b", recipe.REVIEW),
    Edge("A b", "targets", "a-b", recipe.PROVISIONAL),
    Edge("A b", "uses", "a-b", recipe.CORROBORATED),
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
