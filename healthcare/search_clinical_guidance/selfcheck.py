"""Check the recipe's filters, notices, sentence pick, gap log and SIE error rule offline.

The checks use the committed data/ (document system seed, passages, searches), small
hand-written seeds and hand-written stand-in reranker replies. Nothing here calls SIE or replays
a recorded response. Run it with `uv run --frozen python selfcheck.py`; it exits non-zero if a
check fails.
"""

from __future__ import annotations

import contextlib
import copy
import csv
import functools
import io
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from document_system import Document, DocumentSystem
from sie_sdk import SIEClient
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

import recipe

RULES = recipe.load_rules()
MODEL = RULES["models"]["rerank"]
DOCS = DocumentSystem()
PASSAGES, UNLISTED = recipe.load_passages(DOCS)
BY_ID = {p.doc_id: p for p in PASSAGES}
SEARCHES = {s.search_id: s for s in recipe.load_searches(RULES)}
SEED_FIELDS = ("doc_id", "section", "title", "population", "status", "version", "replaced_by")


class StandIn:
    """Stands in for SIEClient. Each score call takes the next listed reply: an exception is
    raised, a function gets the item IDs sent and returns the reply, anything else is returned
    as the reply."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.sent: list[list[str]] = []
        self.texts: dict[str, str] = {}

    def score(self, model: str, query: Any, items: list[Any], **_: Any) -> Any:
        assert model == MODEL, model
        ids = [str(item["id"]) for item in items]
        self.sent.append(ids)
        self.texts.update({str(item["id"]): str(item["text"]) for item in items})
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply(ids) if callable(reply) else reply


def scored(table: dict[str, float], default: float = 0.01) -> Callable[[list[str]], Any]:
    """A stand-in reply: the listed score per item ID, `default` for the rest."""
    return lambda ids: {"scores": [{"item_id": i, "score": table.get(i, default)} for i in ids]}


def scores(table: dict[str, float], default: float = 0.01) -> dict[str, float]:
    return {p.doc_id: table.get(p.doc_id, default) for p in PASSAGES}


def page(
    search_id: str,
    table: dict[str, float],
    passages: list[recipe.Passage] = PASSAGES,
    documents: DocumentSystem = DOCS,
    rules: dict[str, Any] = RULES,
) -> dict[str, Any]:
    """build_page for a sample search, with these passage scores."""
    by_id = {p.doc_id: table.get(p.doc_id, 0.01) for p in passages}
    return recipe.build_page(SEARCHES[search_id], passages, by_id, documents, rules)


def ids(rows: list[dict[str, Any]]) -> list[str]:
    return [row["id"] for row in rows]


def kinds(result: dict[str, Any]) -> list[str]:
    return [notice["kind"] for notice in result["notices"]]


def seed(rows: list[dict[str, str]]) -> DocumentSystem:
    """A document system from hand-written rows (missing fields default to a current adult
    document)."""
    folder = Path(tempfile.mkdtemp())
    path = folder / "documents.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SEED_FIELDS)
        writer.writeheader()
        for row in rows:
            full = {
                "section": "S",
                "title": f"Title {row['doc_id']}",
                "population": "adult",
                "status": "current",
                "version": "1",
                "replaced_by": "",
                **row,
            }
            writer.writerow(full)
    return DocumentSystem(path)


def made_up(documents: DocumentSystem, doc_id: str) -> recipe.Passage:
    return recipe.Passage(documents.get_documents([doc_id])[doc_id], f"Text of {doc_id}.")


def check_data() -> None:
    assert len(PASSAGES) == 24, (len(PASSAGES), UNLISTED)
    assert not UNLISTED, (len(PASSAGES), UNLISTED)
    assert sorted(SEARCHES) == [f"Q-0{n}" for n in range(1, 10)], sorted(SEARCHES)
    assert SEARCHES["Q-03"].population == "pediatric"
    assert SEARCHES["Q-07"].population == "pediatric"
    g05, g13 = BY_ID["G05"].doc, BY_ID["G13"].doc
    assert (g05.status, g05.replaced_by) == ("retired", "G04"), g05
    assert (g13.status, g13.replaced_by) == ("retired", ""), g13
    assert DOCS.find_replacement("G05").current == BY_ID["G04"].doc
    assert DOCS.find_replacement("G13").current is None
    # The only adult retinal passage, the two asthma passages for each population.
    assert [BY_ID[i].doc.population for i in ("G07", "G08", "G12")] == [
        "adult",
        "pediatric",
        "adult",
    ]
    assert BY_ID["G04"].doc.link() == "dms://guidance/G04?version=2025.1"
    print("data: 24 passages joined to the document system, the retired pair and withdrawn ok")


def check_classify() -> None:
    adult, pediatric = "adult", "pediatric"
    assert recipe.classify(BY_ID["G04"].doc, adult, RULES) == "eligible"
    assert recipe.classify(BY_ID["G20"].doc, pediatric, RULES) == "eligible", "all is for both"
    assert recipe.classify(BY_ID["G05"].doc, adult, RULES) == "retired"
    assert recipe.classify(BY_ID["G07"].doc, pediatric, RULES) == "other_population"
    assert recipe.classify(BY_ID["G08"].doc, adult, RULES) == "other_population"
    # A retired passage for another population raises no notice; a draft is never anything.
    assert recipe.classify(BY_ID["G05"].doc, pediatric, RULES) == "ignored"
    docs = seed(
        [
            {"doc_id": "D1", "status": " Current "},
            {"doc_id": "D2", "status": "draft"},
            {"doc_id": "D3", "population": "Pediatric"},
        ]
    )
    found = docs.get_documents(["D1", "D2", "D3"])
    assert recipe.classify(found["D1"], adult, RULES) == "eligible", "case and spaces trimmed"
    assert recipe.classify(found["D2"], adult, RULES) == "ignored", "a draft is never shown"
    assert recipe.classify(found["D3"], pediatric, RULES) == "eligible"
    # A search population rules.yaml doesn't list, or an empty question, stops before SIE.
    for question, population in [("q", "neonatal"), ("q", ""), ("   ", "adult")]:
        try:
            recipe.check_search(question, population, RULES)
        except ValueError:
            continue
        raise AssertionError(f"{question!r}/{population!r} was accepted")
    assert recipe.check_search("  asthma   plan ", " Pediatric ", RULES) == (
        "asthma plan",
        "pediatric",
    )
    print("classify: status and population filters, drafts and unknown populations ok")


def check_page() -> None:
    # Q-01: results best first, only those at or above min_score.
    result = page("Q-01", {"G01": 0.99, "G02": 0.97, "G03": 0.49})
    assert result["status"] == "results", result
    assert ids(result["results"]) == ["G01", "G02"], result
    # At most max_results.
    result = page("Q-01", {"G01": 0.9, "G02": 0.8, "G03": 0.7, "G24": 0.6})
    assert ids(result["results"]) == ["G01", "G02", "G03"]
    # Q-02: the retired edition outscores the current one: replaced notice, never a result.
    result = page("Q-02", {"G05": 0.9999, "G04": 0.9997})
    assert ids(result["results"]) == ["G04"], result
    assert kinds(result) == ["replaced"], result
    notice = result["notices"][0]
    assert notice["id"] == "G05", notice
    assert notice["replaced_by"]["id"] == "G04", notice
    assert "replaced by G04" in recipe.notice_text(notice, result)
    assert recipe.gap_line("Q-02", result, RULES) is None, "replaced alone is not a gap"
    # Within notice_margin log-odds of the current one (slightly under, or the same): still the
    # replaced notice, so a reworded sentence can't flip it off.
    for g05 in (0.9996, 0.9997, 0.999):
        assert kinds(page("Q-02", {"G05": g05, "G04": 0.9997})) == ["replaced"], g05
    # Clearly below the current one (more than the margin in log-odds): no notice.
    for g05 in (0.99, 0.9):
        assert kinds(page("Q-02", {"G05": g05, "G04": 0.9997})) == [], g05
    # A retired passage above every current one but below min_score raises nothing.
    result = page("Q-02", {"G05": 0.4, "G04": 0.2})
    assert result["status"] == "no_good_match", result
    assert kinds(result) == [], result
    # Q-06: the withdrawn protocol above the clinic INR passage: withdrawn notice, logged.
    result = page("Q-06", {"G13": 0.9999, "G14": 0.79})
    assert ids(result["results"]) == ["G14"], result
    assert kinds(result) == ["withdrawn"], result
    line = recipe.gap_line("Q-06", result, RULES)
    assert line is not None, line
    assert line["notices"] == "withdrawn", line
    result = page("Q-06", {"G13": 0.9999, "G14": 0.3})
    assert result["status"] == "no_good_match", result
    assert kinds(result) == ["withdrawn"], result
    # Q-03: the adult passage outscores the child's: it is not a result and not below the line.
    result = page("Q-03", {"G07": 0.99, "G08": 0.98})
    assert ids(result["results"]) == ["G08"], result
    assert not result["below_line"], result
    assert kinds(result) == []
    # Q-07: only the adult retinal passage clears the bar: below the line, labelled, logged.
    result = page("Q-07", {"G12": 0.6})
    assert result["status"] == "no_good_match", result
    assert not result["results"], result
    assert ids(result["below_line"]) == ["G12"]
    assert kinds(result) == ["other_population"]
    line = recipe.gap_line("Q-07", result, RULES)
    assert line is not None, line
    assert line["notices"] == "other_population", line
    rendered = recipe.render_page("Q-07", result)
    assert "## Below the line: not written for pediatric patients" in rendered, rendered
    # An other-population passage below min_score is not shown.
    assert not page("Q-07", {"G12": 0.49})["below_line"]
    # Q-04, Q-05: nothing clears the bar: no_good_match, logged, the page says where to look,
    # and it doesn't claim the guidance lacks an answer.
    result = page("Q-04", {"G15": 0.17})
    assert result["status"] == "no_good_match", result
    assert not result["results"], result
    assert "the search may have missed one" in result["reason"], result["reason"]
    assert "national guideline" in result["reason"]
    assert recipe.gap_line("Q-04", result, RULES) is not None
    # Statuses are what the search found; the old decision-like names are gone.
    assert recipe.STATUSES == ("results", "no_good_match", "not_reranked")
    for old in ("found", "not_in_guidance", "needs_review"):
        assert old not in recipe.STATUSES
    print("page: results, replaced, withdrawn, other population and no_good_match ok")


def check_replacements() -> None:
    docs = seed(
        [
            {"doc_id": "A", "status": "retired", "replaced_by": "B"},
            {"doc_id": "B", "status": "retired", "replaced_by": "C"},
            {"doc_id": "C"},
            {"doc_id": "M", "status": "retired", "replaced_by": "GONE"},
            {"doc_id": "L1", "status": "retired", "replaced_by": "L2"},
            {"doc_id": "L2", "status": "retired", "replaced_by": "L1"},
            {"doc_id": "S", "status": "retired", "replaced_by": "S"},
            {"doc_id": "P", "status": "retired", "population": "all", "replaced_by": "K"},
            {"doc_id": "K", "population": "adult"},
            {"doc_id": "W", "status": "retired"},
            {"doc_id": "D1", "status": "retired", "replaced_by": "D2"},
            {"doc_id": "D2", "status": "draft"},
            {"doc_id": "R1", "status": "retired", "replaced_by": "R2"},
            {"doc_id": "R2", "status": "retired"},
        ]
    )
    # A chain of replacements ends at the current document.
    found = docs.find_replacement("A")
    assert found.current is not None
    assert found.current.doc_id == "C"
    assert found.chain == ("B", "C")
    # A replacement the system lacks, a loop and a document replacing itself: no current one.
    for doc_id, problem in [
        ("M", "replaced by GONE, which the document system lacks"),
        ("L1", "the replacements loop back to L1"),
        ("S", "the replacements loop back to S"),
        ("W", "no replacement"),
        # A chain ending at a draft names the draft, never "retired".
        ("D1", "D2 is draft, not current, with no replacement"),
        ("R1", "R2 is retired, not current, with no replacement"),
    ]:
        found = docs.find_replacement(doc_id)
        assert found.current is None, (doc_id, found)
        assert found.problem == problem, (doc_id, found)
    # On a page: a broken chain is a withdrawn notice, never a replaced one.
    passages = [made_up(docs, i) for i in ("A", "C", "M", "K")]
    search = recipe.Search("X", "adult", "q")
    result = recipe.build_page(
        search, passages, {"A": 0.95, "C": 0.9, "M": 0.97, "K": 0.1}, docs, RULES
    )
    assert [(n["kind"], n["id"]) for n in result["notices"]] == [
        ("withdrawn", "M"),
        ("replaced", "A"),
    ], result["notices"]
    assert result["notices"][1]["replaced_by"]["chain"] == ["B", "C"]
    # A retired "all" document replaced by an adult one, on a pediatric search: said so.
    pediatric = recipe.Search("Y", "pediatric", "q")
    passages = [made_up(docs, "P"), made_up(docs, "K")]
    result = recipe.build_page(pediatric, passages, {"P": 0.9, "K": 0.95}, docs, RULES)
    assert not result["results"], result
    assert result["notices"][0]["kind"] == "replaced", result
    assert "written for adult patients" in recipe.notice_text(result["notices"][0], result)
    print("replacements: chains, missing IDs, loops and other-population replacements ok")


def check_sentences() -> None:
    # G10's two intervals are two sentences; decimals and units don't split.
    assert len(recipe.split_sentences(BY_ID["G10"].text)) == 2
    assert recipe.split_sentences("Above 6.0 mmol/L is critical. The lab phones.") == [
        "Above 6.0 mmol/L is critical.",
        "The lab phones.",
    ]
    assert len(recipe.split_sentences(BY_ID["G19"].text)) == 2, "38.0 C stays in one sentence"
    # The best sentence is picked, not the first; a tie goes to the first.
    result = page("Q-09", {"G10": 0.99})
    recipe.add_sentences(result, {"G10:0": 0.3, "G10:1": 0.98}, "")
    pick = result["results"][0]["sentence"]
    assert pick["text"].startswith("When the result is at target"), pick
    result = page("Q-09", {"G10": 0.99})
    recipe.add_sentences(result, {"G10:0": 0.5, "G10:1": 0.5}, "")
    assert result["results"][0]["sentence"]["text"].startswith("Measure HbA1c every three")
    # The sentence is shown with the full passage, so a "Do not" next to it stays in view.
    result = page("Q-01", {"G01": 0.99})
    recipe.add_sentences(result, {"G01:0": 0.2, "G01:1": 0.3, "G01:2": 0.9}, "")
    rendered = recipe.render_page("Q-01", result)
    assert "> Do not start medication on a single office reading." in rendered
    assert BY_ID["G01"].text in rendered, "the full passage is on the page"
    print("sentences: split, best picked, full passage always shown ok")


def search(search_id: str, *replies: Any) -> tuple[dict[str, Any], StandIn]:
    client = StandIn(*replies)
    endpoint = recipe.GuidanceSearch(cast(SIEClient, client), RULES, DOCS, PASSAGES)
    result = endpoint.search(SEARCHES[search_id])
    assert not client.replies, f"{len(client.replies)} replies left unused"
    return result, client


def check_endpoint() -> None:
    # Two calls: every passage, then the sentences of what is shown, with section and title.
    result, client = search(
        "Q-08", scored({"G20": 0.9999, "G21": 0.89}), scored({"G20:1": 0.99, "G21:0": 0.9})
    )
    assert sorted(client.sent[0]) == sorted(BY_ID), "every passage is scored"
    assert client.sent[1] == ["G20:0", "G20:1", "G21:0"], client.sent[1]
    # A sentence goes with its section and title: "The lab phones" alone doesn't say what about.
    assert client.texts["G20:1"].startswith("Lab follow-up: Critical potassium results\nThe lab")
    assert result["results"][0]["sentence"]["text"].startswith("The lab phones"), result
    # Nothing shown: no sentence call.
    result, client = search("Q-05", scored({}))
    assert len(client.sent) == 1
    assert result["status"] == "no_good_match"
    # A passage the document system lacks is never sent, scored or shown.
    docs = seed([{"doc_id": "G01"}])
    folder = Path(tempfile.mkdtemp())
    (folder / "p.jsonl").write_text(
        '{"doc_id": "G01", "text": "a"}\n{"doc_id": "X9", "text": "b"}\n', encoding="utf-8"
    )
    passages, unlisted = recipe.load_passages(docs, folder / "p.jsonl")
    assert [p.doc_id for p in passages] == ["G01"]
    assert unlisted == ["X9"]
    # A reply that can't order every passage is a failed call: keyword order, filters applied.
    for bad in [
        {"no": "scores"},
        lambda sent: {"scores": [{"item_id": i, "score": 0.5} for i in sent[1:]]},
        lambda sent: {"scores": [{"item_id": i, "score": "nan"} for i in sent]},
        lambda sent: {"scores": [{"item_id": i, "score": None} for i in sent]},
    ]:
        result, _ = search("Q-03", bad)
        assert result["status"] == "not_reranked", result["status"]
        # The keyword engine puts the adult G07 second; the population filter keeps it out.
        assert "G07" not in ids(result["results"]), ids(result["results"])
        assert ids(result["results"])[0] == "G08", ids(result["results"])
        assert not result["notices"], result["reason"]
        assert "no notices" in result["reason"], result["reason"]
        assert recipe.gap_line("Q-03", result, RULES) is None
    # The retired G13 is the keyword engine's first hit for Q-06; the fallback never shows it.
    result, _ = search("Q-06", ServerError("unavailable", status_code=503))
    assert result["keyword_first"][0] == "G13"
    assert "G13" not in ids(result["results"])
    assert ids(result["results"])[0] == "G14", result
    assert "Not reranked, the SIE call failed: ServerError" in result["reason"]
    # Scores for items we didn't send are ignored.
    extra = scored({"G10": 0.99, "ZZ": 1.0})
    result, _ = search("Q-09", extra, scored({}))
    assert ids(result["results"]) == ["G10"]
    # A failed sentence call keeps the results and says the sentence wasn't picked.
    result, _ = search("Q-09", scored({"G10": 0.99}), RateLimitError("slow down"))
    assert result["status"] == "results"
    assert result["results"][0]["sentence"] is None
    assert result["sentence_note"].startswith("Best sentence not picked, the SIE call failed")
    assert "Best sentence not picked" in recipe.render_page("Q-09", result)
    print("endpoint: two calls, unlisted passages, bad replies fall back to keyword order ok")


def run(*replies: Any, folder: Path | None = None) -> tuple[Any, Path]:
    """Run the recipe on Q-02 and Q-07 with stand-in replies; the outputs go to `folder`/out."""
    folder = folder or Path(tempfile.mkdtemp())
    rows = json.loads(recipe.SEARCHES_PATH.read_text(encoding="utf-8"))["searches"]
    picked = [row for row in rows if row["id"] in ("Q-02", "Q-07")]
    (folder / "searches.json").write_text(json.dumps({"searches": picked}), encoding="utf-8")
    client = StandIn(*replies)
    output = folder / "out"
    with contextlib.redirect_stdout(io.StringIO()):  # the run's "1 of 2 failed" line
        result = recipe.run(cast(SIEClient, client), RULES, output, folder / "searches.json")
    assert not client.replies, f"{len(client.replies)} replies left unused"
    return result, output


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
Q02 = scored({"G05": 0.9999, "G04": 0.9997})
Q07 = scored({"G12": 0.6})


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


def check_run_outputs() -> None:
    (pages, gaps, _), output = run(Q02, scored({}), Q07, scored({}))
    assert [sid for sid, _ in pages] == ["Q-02", "Q-07"]
    assert [line["search_id"] for line in gaps] == ["Q-07"], gaps
    assert sorted(p.name for p in (output / "pages").iterdir()) == ["Q-02.md", "Q-07.md"]
    text = (output / "pages" / "Q-02.md").read_text(encoding="utf-8")
    assert "Replaced: G05" in text, text
    assert "dms://guidance/G04?version=2025.1" in text, text
    with (output / "gap_log.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert tuple(rows[0]) == (*recipe.GAP_FIELDS, "owner"), rows
    assert rows[0]["owner"] == "clinical policy coordinator", rows
    assert rows[0]["closest_passage"] == "G08", rows
    saved = json.loads((output / "results.json").read_text(encoding="utf-8"))
    assert [s["id"] for s in saved["searches"]] == ["Q-02", "Q-07"]
    assert "Gap log (1 searches)" in (output / "summary.md").read_text(encoding="utf-8")
    # A cell a spreadsheet would run as a formula is defused, in the gap log too.
    assert recipe.csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert recipe.csv_safe("-15 C") == "'-15 C"
    assert recipe.csv_safe("Q-04") == "Q-04"
    folder = Path(tempfile.mkdtemp())
    rows_in = json.loads(recipe.SEARCHES_PATH.read_text(encoding="utf-8"))["searches"]
    formula_search = [{**rows_in[3], "question": "=cmd|' /C calc'!A0 hypnotherapy"}]
    (folder / "searches.json").write_text(
        json.dumps({"searches": formula_search}), encoding="utf-8"
    )
    with contextlib.redirect_stdout(io.StringIO()):
        recipe.run(
            cast(SIEClient, StandIn(scored({}))), RULES, folder / "out", folder / "searches.json"
        )
    with (folder / "out" / "gap_log.csv").open(encoding="utf-8", newline="") as handle:
        assert next(csv.DictReader(handle))["question"].startswith("'=cmd")
    print("run: pages, results.json, summary and the gap log written ok")


def check_sie_failure() -> None:
    for error in TRANSIENT:
        assert not recipe.is_fatal(error), f"{type(error).__name__} should be transient"
    for error in FATAL:
        assert recipe.is_fatal(error), f"{type(error).__name__} should stop the run"

    # A transient failure: that search falls back to keyword order with the error, cut short;
    # the other search is reranked and the run writes its outputs.
    long = "connection refused " + "x" * 300
    for error in [*TRANSIENT, SIEConnectionError(long)]:
        (pages, gaps, _), output = run(error, Q07, scored({}))
        first = pages[0][1]
        assert first["status"] == "not_reranked", first["status"]
        prefix = "Not reranked, the SIE call failed: "
        assert first["reason"].startswith(prefix), first["reason"]
        assert ids(first["results"])[0] == "G04"
        assert "G05" not in ids(first["results"])
        assert pages[1][1]["status"] == "no_good_match"
        assert [line["search_id"] for line in gaps] == ["Q-07"]
        assert (output / "summary.md").exists()
    why = first["reason"].removeprefix(prefix).split(". ", 1)[0]
    assert len(why) == len("SIEConnectionError: ") + recipe.ERROR_CHARS, why

    def stops(*replies: Any) -> str:
        """The stop message; also checks the run wrote nothing."""
        folder = Path(tempfile.mkdtemp())
        message = stopped(functools.partial(run, *replies, folder=folder))
        assert not (folder / "out").exists(), "a stopped run wrote outputs"
        return message

    # Every call failed (a dead SIE_BASE_URL): the run stops, writes nothing, full message.
    message = stops(*[SIEConnectionError(long)] * 2)
    assert message.startswith(f"Every SIE call failed (2 of 2 to {MODEL})"), message
    assert long in message
    assert "Check SIE_BASE_URL and your network." in message, message
    assert "No outputs were written." in message
    # A reply missing its key on every call is every call failing too.
    message = stops({"no": "scores"}, {"no": "scores"})
    assert "the reply has no `scores`" in message, message
    # Any other transient error on every call more likely means a cold model or a busy SIE.
    for error in [ProvisioningError("no GPU yet"), ServerError("unavailable", status_code=503)]:
        message = stops(*[error] * 2)
        assert "may still be starting or SIE is busy; run again in a few minutes." in message
        assert "SIE_BASE_URL" not in message, message
    # A refusal on a later call (the sentence call) stops the run at once, full message.
    message = stops(Q02, InsufficientCreditsError("no credits " + "y" * 300))
    assert message.startswith(f"SIE refused the call to {MODEL}, "), message
    assert "InsufficientCreditsError (HTTP 402, code INSUFFICIENT_CREDITS)" in message, message
    assert "y" * 300 in message
    assert "SIE_API_KEY" in message
    # A model SIE marked as never loading points at the model name.
    message = stops(ModelLoadFailedError("gated", permanent=True))
    assert message.startswith(f"SIE can't load {MODEL}, ModelLoadFailedError"), message
    assert "SIE_API_KEY" not in message, message
    # A malformed reply has no HTTP status: the hint doesn't blame the key.
    message = stops(RequestError("unexpected body"))
    assert "SIE_API_KEY" not in message, message
    assert "SIE_BASE_URL" in message, message
    # One search from the command line follows the same rule.
    message = stopped(lambda: search("Q-01", RequestError("bad key", status_code=401)))
    assert message.startswith(f"SIE refused the call to {MODEL}, RequestError (HTTP 401)")
    # A bug in our own code is not hidden, and main turns only SIEStopped into a clean exit.
    caught = raised(lambda: run(TypeError("bad argument")))
    assert type(caught) is TypeError, f"TypeError became {type(caught).__name__}"

    def bug() -> None:
        raise RuntimeError("a bug in recipe.py")

    def refusal() -> None:
        raise recipe.SIEStopped("SIE refused")

    caught = raised(lambda: recipe.stop_on_refusal(bug))
    assert type(caught) is RuntimeError, f"a RuntimeError became {type(caught).__name__}"
    caught = raised(lambda: recipe.stop_on_refusal(refusal))
    assert type(caught) is SystemExit
    assert str(caught) == "Stopped: SIE refused"
    print(
        "sie failure: transient errors fall back to keyword order; refusals and all-failed stop ok"
    )


def check_rules_shape() -> None:
    # A changed min_score is read from rules.yaml, not from code.
    rules = copy.deepcopy(RULES)
    rules["rerank"]["min_score"] = 0.95
    result = page("Q-03", {"G08": 0.9}, rules=rules)
    assert result["status"] == "no_good_match", result
    assert "0.95" in result["reason"], result
    assert isinstance(Document("a", "b", "c", "d", "e", "f", "").link(), str)
    print("rules: thresholds come from rules.yaml ok")


if __name__ == "__main__":
    check_data()
    check_classify()
    check_page()
    check_replacements()
    check_sentences()
    check_endpoint()
    check_run_outputs()
    check_sie_failure()
    check_rules_shape()
