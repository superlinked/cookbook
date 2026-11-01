"""Offline checks of the recipe's own parsing and rules, on hand-written text. No SIE call.

uv run --frozen python selfcheck.py
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import recipe
from recipe import Canon, Claim, Edge, Graph, Node, Source

RULES = recipe.load_rules()
TABLE = recipe.AliasTable.build(RULES)


def tagged(text: str) -> set[str]:
    """The names a model would tag in text: alias-table names (longest first) and CVE IDs."""
    known = "|".join(map(re.escape, sorted(TABLE.by_key, key=len, reverse=True)))
    names = re.finditer(rf"\b(?:{known}|CVE-\d{{4}}-\d{{4,}})\b", text, re.IGNORECASE)
    return {m.group() for m in names}


def verdict(relation: str, marked: str) -> str:
    """The verdict (denial, review or edge) on the claim between the two [names] in marked."""
    head, tail = dict.fromkeys(re.findall(r"\[([^]]+)\]", marked))
    text = marked.replace("[", "").replace("]", "")
    keys = (recipe.name_key(head), recipe.name_key(tail))
    others = [recipe.name_key(n) for n in tagged(text) if recipe.name_key(n) not in keys]
    denied, doubt, _ = recipe.check_negation(text, head, tail, relation, others, RULES)
    return "denial" if denied else "review" if doubt else "edge"


# The claim joins the two [names]; which one is the head doesn't matter to the negation check.
NEGATION: dict[str, list[tuple[str, str]]] = {
    # A negation directly on the claim denies it.
    "denial": [
        ("uses", "We found no evidence that [Saltmarsh Kettle] used [Thornpipe]."),  # E6
        ("uses", "[Gorse Moth] did not deploy [Thornpipe]."),
        ("attributed to", "[Thornpipe] has never been linked to [Saltmarsh Kettle]."),
        ("attributed to", "[Murkbell] can't be attributed to [Velvet Gorse]."),
        ("attributed to", "[Murkbell] has not been linked to [Velvet Gorse]."),
        ("uses", "[Velvet Gorse] never used [Thornpipe]."),
        ("uses", "[Saltmarsh Kettle] isn't using [Quillstep] anymore."),
        ("targets", "[Saltmarsh Kettle] won't be targeting [port operators]."),
        (
            "uses",
            "There is, after a long review, no evidence that [Velvet Gorse] used [Thornpipe].",
        ),
        ("uses", "[Velvet Gorse] did not use [Thornpipe] but used Murkbell."),
    ],
    # Any other negation in the claim's sentence sends it to review, never to a denial.
    "review": [
        ("uses", "[Velvet Gorse] did not use Thornpipe but used [Murkbell]."),
        (
            "attributed to",
            "[Murkbell] is not attributed to Saltmarsh Kettle but to [Velvet Gorse].",
        ),
        ("uses", "[Velvet Gorse], with no link to Saltmarsh Kettle, used [Thornpipe]."),
        ("uses", "[Velvet Gorse], which we have not linked to Saltmarsh Kettle, used [Thornpipe]."),
        ("uses", "[Gorse Moth] did not deploy Thornpipe this time, using [Murkbell] instead."),
        ("uses", "[Velvet Gorse] did not use Murkbell; it used [Thornpipe]."),
        # The same with the other thing untagged: the clause break alone stops a denial.
        ("uses", "[Velvet Gorse] did not use the old loader but used [Murkbell]."),
        ("uses", "[Velvet Gorse], which did not use the loader, used [Murkbell]."),
        ("uses", "[Velvet Gorse] did not use the loader this time, deploying [Murkbell]."),
        # Hedges.
        ("uses", "[Velvet Gorse] has not been observed using [Thornpipe]."),
        ("uses", "[Velvet Gorse] does not appear to use [Thornpipe]."),
        ("uses", "[Velvet Gorse] is not known to use [Thornpipe]."),
        ("uses", "[Velvet Gorse] no longer uses [Thornpipe]."),
        # Earlier rounds.
        ("uses", "[Velvet Gorse], not Saltmarsh Kettle, used [Thornpipe]."),
        ("uses", "[Velvet Gorse] used [Thornpipe] but did not exploit CVE-2099-41877."),
        ("exploits", "[Velvet Gorse] used Thornpipe but did not exploit [CVE-2099-41877]."),
        ("uses", "[Velvet Gorse] used [Thornpipe], but we found no evidence that it stole data."),
        ("uses", "[Velvet Gorse] used [Thornpipe]; investigators ruled out insider involvement."),
        ("uses", "[Velvet Gorse] deployed [Thornpipe], but has not used Murkbell."),
        ("uses", "[Velvet Gorse] deployed Thornpipe, but has not used [Murkbell]."),
        ("uses", "[Velvet Gorse] did not exploit the flaw, and used [Thornpipe]."),
        ("targets", "[Saltmarsh Kettle] did not deploy anything against [port operators]."),
        ("uses", "Although Saltmarsh Kettle never used Murkbell, [Velvet Gorse] used [Thornpipe]."),
        ("uses", "Saltmarsh Kettle never used Murkbell but [Velvet Gorse] used [Thornpipe]."),
        ("uses", "[Saltmarsh Kettle] is active. It has never used [Thornpipe]."),
        # The same pair in two sentences that disagree.
        (
            "uses",
            "[Velvet Gorse] used [Thornpipe] in May. No sign that Velvet Gorse used Thornpipe.",
        ),
    ],
    # No negation word in the sentence: an edge.
    "edge": [
        ("uses", "[Saltmarsh Kettle] is active. It used [Thornpipe]."),
        ("uses", "[Velvet Gorse] used the [Thornpipe backdoor]."),
    ],
}
for expected, cases in NEGATION.items():
    for relation, marked in cases:
        got = verdict(relation, marked)
        assert got == expected, f"{relation} {marked!r}: {got}, expected {expected}"
print(f"negation: {sum(map(len, NEGATION.values()))} crafted claims ok")

# Only E6 in the committed reports is a denial, for any pair of known names and any relation.
denied = set()
for report in recipe.load_reports():
    for _, window in recipe.paragraph_windows(report.text, 200):
        names = tagged(window)
        for head in names:
            for tail in names:
                if TABLE.resolve(head, None) == TABLE.resolve(tail, None):
                    continue
                others = [recipe.name_key(n) for n in names - {head, tail}]
                for relation in RULES["relations"]:
                    if recipe.check_negation(window, head, tail, relation, others, RULES)[0]:
                        denied.add((report.id, frozenset({head, tail})))
assert denied == {("R-04", frozenset({"Saltmarsh Kettle", "Thornpipe"}))}, denied
print("committed reports: one denying sentence (E6, R-04) ok")

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


# Graph: a doubtful claim flags its edge for review; a denial makes it conflicting.
def claim(report: str, publisher: str, denied: bool = False, doubt: str = "") -> Claim:
    vg, tp = TABLE.resolve("Velvet Gorse", None), TABLE.resolve("Thornpipe", None)
    assert vg is not None
    assert tp is not None
    return Claim(vg, "uses", tp, Source(report, publisher, "", "s", 0.9), denied, doubt)


both = [claim("R-01", "A"), claim("R-02", "B")]
assert recipe.build_graph(both, [], RULES).edges[0].status == recipe.CORROBORATED
doubtful = recipe.build_graph([*both, claim("R-03", "C", doubt="a negation")], [], RULES)
assert doubtful.edges[0].status == recipe.REVIEW, doubtful.edges[0]
assert "R-03 has a negation near it: 's'" in doubtful.edges[0].flags
denial = recipe.build_graph([*both, claim("R-03", "C", denied=True)], [], RULES)
assert denial.edges[0].status == recipe.CONFLICTING
print("graph status ok")

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
chart = recipe.mermaid(Graph(nodes, edges, [], [], []))
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
