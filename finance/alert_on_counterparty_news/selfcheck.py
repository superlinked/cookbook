"""Check the recipe's lookups, linking rules, event limits and SIE error rule offline, and the
agent's tools, judge and statuses.

The checks use the committed data/, hand-written articles, hand-written stand-in replies from
both models and hand-written stand-in agent findings and Claude turns. Nothing here calls SIE or
Claude, or replays a recorded response.
Run it with `uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import csv
import functools
import io
import json
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import agent
from counterparties import CounterpartyList
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
NER = RULES["models"]["entities"]
DECIDE = RULES["models"]["events"]
EVENTS = list(RULES["events"])
CPS = CounterpartyList()


def event(**top: float) -> dict[str, Any]:
    """A stand-in GLiNER2.5-Decide reply: the events given, the rest sharing what's left."""
    rest = [name for name in EVENTS if name not in top]
    share = (1.0 - sum(top.values())) / len(rest) if rest else 0.0
    probs = {name: top.get(name, share) for name in EVENTS}
    choice = max(probs, key=lambda name: probs[name])
    return {
        "data": {recipe.QUESTION_ID: {"type": "choice", "choice": choice, "probabilities": probs}}
    }


ALERTING = event(missed_payment=0.8, none=0.05)
QUIET = event(none=0.9)


class StandIn:
    """Stands in for SIEClient.

    NuNER Zero calls get one result per window: every word or phrase in `tags` is tagged with
    its label wherever it appears (`ner` replaces the whole reply when given: an exception is
    raised, anything else returned). GLiNER2.5-Decide calls take the next listed reply in
    `events`. A call with no reply left is a check failure.
    """

    def __init__(
        self,
        tags: dict[str, str] | None = None,
        events: tuple[Any, ...] = (),
        ner: list[Any] | None = None,
    ) -> None:
        self.tags = tags or {}
        self.events = list(events)
        self.ner = ner
        self.read: list[str] = []  # the text each event call read
        self.ner_calls = 0
        self.ner_options: list[Any] = []  # the options each NuNER Zero call sent

    def extract(self, model: str, items: Any, **kwargs: Any) -> Any:
        if model == NER:
            self.ner_calls += 1
            self.ner_options.append(kwargs.get("options"))
            if self.ner is not None:
                assert self.ner, "NuNER Zero was called again and no reply was planned"
                answer = self.ner.pop(0)
                if isinstance(answer, BaseException):
                    raise answer
                return answer
            return [{"entities": self._spans(item["text"])} for item in items]
        assert model == DECIDE, model
        assert kwargs["output_schema"][recipe.QUESTION_ID]["type"] == "choice"
        assert self.events, f"the event model was asked about {items['id']}, none was planned"
        self.read.append(items["text"])
        answer = self.events.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def _spans(self, text: str) -> list[dict[str, Any]]:
        spans = []
        for phrase, label in self.tags.items():
            start = text.find(phrase)
            while start >= 0:
                end = start + len(phrase)
                spans.append(
                    {"label": label, "text": phrase, "start": start, "end": end, "score": 0.9}
                )
                start = text.find(phrase, end)
        return spans


def article(article_id: str, headline: str, body: str) -> dict[str, str]:
    return {
        "id": article_id,
        "source": "Test Wire",
        "url": f"https://test-wire.example/{article_id}",
        "published": "2026-11-02T09:00:00Z",
        "headline": headline,
        "body": body,
    }


def run(
    articles: list[dict[str, str]],
    client: StandIn | None = None,
    folder: Path | None = None,
) -> tuple[dict[str, recipe.Result], Path, StandIn]:
    """Run the recipe on hand-written articles with stand-in replies; outputs go to folder/out."""
    folder = folder or Path(tempfile.mkdtemp())
    path = folder / "news.jsonl"
    path.write_text("".join(json.dumps(a) + "\n" for a in articles), encoding="utf-8")
    client = client or StandIn()
    output = folder / "out"
    with contextlib.redirect_stdout(io.StringIO()):  # the run's "1 of 2 failed" line
        results = recipe.run(cast(SIEClient, client), RULES, output, path, recipe.DATA_DIR)
    assert not client.events, f"{len(client.events)} event replies left unused"
    return {r.article.id: r for r in results}, output, client


def one(headline: str, body: str, **kwargs: Any) -> recipe.Result:
    """The result for one hand-written article."""
    results, _, _ = run([article("T-1", headline, body)], StandIn(**kwargs))
    return results["T-1"]


def linked(result: recipe.Result) -> set[str]:
    return {link.counterparty_id for link in result.links if link.status == recipe.LINKED}


def raised(call: Callable[[], object]) -> BaseException:
    """Whatever the call raises, SystemExit included; fails when it raises nothing."""
    try:
        call()
    except BaseException as error:  # SystemExit too: the check is on which one it was
        return error
    raise AssertionError("nothing was raised")


def check_data() -> None:
    articles = recipe.load_articles()
    assert [a.id for a in articles] == [f"N-{n:02d}" for n in range(1, 11)], articles
    assert len(CPS.all()) == 9
    # The shapes the examples rely on hold in the committed data.
    northwind = CPS.find_by_alias("Northwind")
    assert [cp.id for cp in northwind] == ["CP-1004", "CP-1005"], northwind
    assert [cp.id for cp in CPS.find_by_parent_ticker("NWEG", "NYSE")] == ["CP-1005"]
    assert CPS.find_by_parent_ticker("NWEG", "LSE") == []
    assert [cp.id for cp in CPS.find_by_alias("Halden")] == ["CP-1003"]
    assert CPS.find_by_alias("Fenmoor Analytics") == CPS.find_by_legal_name("Fenmoor Analytics")
    assert CPS.find_by_alias("Fenmoor Analytics") == []
    assert CPS.find_by_alias("Elmstead Creameries") == []
    # Lookups ignore case and spaces, nothing else; a near name is a different company.
    assert [cp.id for cp in CPS.find_by_legal_name(" pellridge foods LTD ")] == ["CP-1001"]
    for near in ("Northwind Energy Services", "Pellridge Food", "Halden Cold", "Tolland Pack"):
        assert CPS.find_by_alias(near) == CPS.find_by_legal_name(near) == [], near
    folder = Path(tempfile.mkdtemp())
    header = "counterparty_id,legal_name,aliases,registration_number,parent_ticker,"
    header += "relationship_manager,credit_officer\n"
    for rows, words in (
        ("CP-1,A Ltd,,1,,R,C\nCP-1,B Ltd,,2,,R,C\n", "listed twice"),
        ("CP-1,A Ltd,,1,NWEG,R,C\n", "EXCHANGE:SYMBOL"),
        ("CP-1,A Ltd,,1,,R,\n", "no credit officer"),
        ("", "lists no counterparties"),
    ):
        (folder / "counterparties.csv").write_text(header + rows, encoding="utf-8")
        caught = raised(lambda: CounterpartyList(folder))
        assert type(caught) is ValueError, caught
        assert words in str(caught), caught
    print("data: 10 articles, 9 counterparties, shared alias, parent ticker, near names ok")


def check_examples() -> None:
    """The committed articles end to end, with stand-in tags like the live run's and stand-in
    event replies: an edit to the data that changes which borrowers reach the event model, or
    how an article comes out, fails here."""
    articles = [json.loads(line) for line in recipe.NEWS_PATH.read_text().splitlines()]
    client = StandIn(
        tags={"Tolland Packaging": "company", "Halden": "location", "Fenmoor Analytics": "company"},
        events=(
            ALERTING,  # N-01 Pellridge
            QUIET,  # N-02 Sallowmere
            event(rating_action=0.76, none=0.1),  # N-05 Northwind Energy Supply
            QUIET,  # N-06 Quillfield
            QUIET,  # N-09 Ferncastle
        ),
    )
    # N-10's "Halden" is a place, but N-03's must not be: tag Halden as a place only in N-10.
    results, _output, client = run([a for a in articles if a["id"] != "N-03"], client)
    n03, _, _ = run(
        [a for a in articles if a["id"] == "N-03"], StandIn(events=(event(lawsuit=0.85),))
    )
    results |= n03
    got = {aid: r.status for aid, r in results.items()}
    assert got == {
        "N-01": "alert",
        "N-02": "filed",
        "N-03": "alert",
        "N-04": "review",
        "N-05": "alert",
        "N-06": "filed",
        "N-07": "review",
        "N-08": "not_a_borrower",
        "N-09": "filed",
        "N-10": "not_a_borrower",
    }, got
    assert linked(results["N-05"]) == {"CP-1005"}, results["N-05"].links
    assert linked(results["N-07"]) == set(), results["N-07"].links
    assert linked(results["N-06"]) == {"CP-1006"}, "Fenmoor is not a borrower"
    # N-09: the competitor's insolvency is not in what the event model read for Ferncastle.
    ferncastle = client.read[-1]
    assert ferncastle.startswith("Ferncastle Dairy Ltd, which competes"), ferncastle
    assert "administration" not in ferncastle, ferncastle
    print("examples: the 10 committed articles give the README's statuses ok")


def check_sentences() -> None:
    a = recipe.Article(
        "T",
        "S",
        "u",
        "p",
        "Headline here",
        "Pellridge Foods Ltd. missed it. Then 4.25 percent! Last",
    )
    spans = recipe.sentences(a, RULES)
    got = [a.text[s:e] for s, e in spans]
    assert got == [
        "Headline here",
        "Pellridge Foods Ltd. missed it.",
        "Then 4.25 percent!",
        "Last",
    ], got
    # Longest name wins: "Northwind Energy" is not also read as "Northwind".
    found = recipe.list_mentions("Northwind Energy and Northwind's depot", CPS.all())
    assert [(e.text, e.start) for e in found] == [("Northwind Energy", 0), ("Northwind", 21)], found
    # Whole words only: "Haldenby" is not "Halden".
    assert recipe.list_mentions("Haldenby Mills closed", CPS.all()) == []
    print("sentences: Ltd. keeps a name whole, longest name wins, whole words only ok")


def check_linking() -> None:
    # A unique alias links; the event model reads the borrower's sentences.
    r = one("Halden sued", "A buyer sued Halden over stock.", events=(event(lawsuit=0.8),))
    assert (r.status, linked(r)) == (recipe.ALERT, {"CP-1003"}), r
    # Read as a place: not a borrower, no event call.
    r = one("Floods in Halden", "Roads into Halden reopened.", tags={"Halden": "location"})
    assert (r.status, linked(r)) == (recipe.NOT_A_BORROWER, set()), r
    assert all(link.status == recipe.PLACE for link in r.links), r.links
    # A shared alias goes to a person with both candidates; no event call.
    r = one("Northwind news", "Northwind lost a customer.")
    assert r.status == recipe.REVIEW, r
    assert linked(r) == set(), r
    assert "CP-1004" in r.links[0].reason, r.links
    assert "CP-1005" in r.links[0].reason, r.links
    # Settled by the ticker written next to it (N-05), and the bare mention after it too.
    r = one("x", "Northwind (NYSE: NWEG) was cut. Northwind said little.", events=(QUIET,))
    assert linked(r) == {"CP-1005"}, r.links
    assert r.status == recipe.FILED, r.links
    # Settled by the other borrower's legal name elsewhere in the article.
    r = one("x", "Northwind Energy Supply Ltd raised prices. Northwind said so.", events=(QUIET,))
    assert linked(r) == {"CP-1005"}, r.links
    assert r.status == recipe.FILED, r.links
    # A name with a conflicting ticker is never settled by another
    # mention, and the other mention of the same borrower isn't linked either.
    r = one("x", "Northwind (NASDAQ: QQQX) slipped. Northwind Energy Supply Ltd said so.")
    assert r.status == recipe.REVIEW, r.links
    assert linked(r) == set(), r.links
    assert "name and ticker disagree" in r.links[0].reason, r.links
    # A shared alias isn't settled while one of its borrowers is in dispute in the article.
    body = "Northwind Logistics (NYSE: NWEG) won. Northwind Energy Supply Ltd lost. Northwind said."
    r = one("x", body, events=(QUIET,))
    bare = [link for link in r.links if link.mention == "Northwind"]
    assert [link.status for link in bare] == [recipe.REVIEW], r.links
    # N-07: a borrower's name with another borrower's parent ticker; its headline mention
    # isn't linked on its own either (no event call is planned).
    r = one("Tolland Packaging wins", "Tolland Packaging (NYSE: NWEG) won a deal.")
    assert r.status == recipe.REVIEW, r.links
    assert linked(r) == set(), r.links
    # The exchange counts. NWEG trades on NYSE, not LSE.
    r = one("x", "Northwind Energy (LSE: NWEG) was cut.")
    assert r.status == recipe.REVIEW, r.links
    assert linked(r) == set(), r.links
    assert "the list has NYSE:NWEG" in r.links[0].reason, r.links
    r = one("x", "Shares (LSE: NWEG) fell.")
    assert r.status == recipe.REVIEW, r.links
    assert linked(r) == set(), r.links
    r = one("x", "Shares (NYSE: NWEG) fell.", events=(QUIET,))
    assert linked(r) == {"CP-1005"}, r.links
    # A company not on the list, written with a borrower's ticker: review, not ignored.
    r = one("x", "Acme Holdings (NYSE: NWEG) fell.", tags={"Acme Holdings": "company"})
    assert r.status == recipe.REVIEW, r.links
    # A company not on the list is not a borrower (N-06's Fenmoor, N-09's Elmstead).
    r = one("x", "Fenmoor Analytics hired staff.", tags={"Fenmoor Analytics": "company"})
    assert (r.status, r.links) == (recipe.NOT_A_BORROWER, []), r
    # List names match ignoring case, so an all-caps headline still
    # names the borrower, whether the model tagged it or not.
    for tags in ({"PELLRIDGE FOODS LTD": "company"}, {}):
        r = one(
            "PELLRIDGE FOODS LTD MISSES INTEREST PAYMENT",
            "Talks with lenders continue.",
            tags=tags,
            events=(ALERTING,),
        )
        assert (r.status, linked(r)) == (recipe.ALERT, {"CP-1001"}), (tags, r.links)
    print("linking: alias, place, shared alias, ticker, conflicts, exchange, case, others ok")


def check_spans() -> None:
    # Near-identical but different: a longer company name that contains a borrower's name.
    r = one(
        "x", "Northwind Energy Services cut jobs.", tags={"Northwind Energy Services": "company"}
    )
    assert r.status == recipe.REVIEW, r.links
    assert linked(r) == set(), r.links
    assert "different name that contains the borrower name" in r.links[0].reason
    # Legal suffixes and a possessive don't make a different company.
    for phrase in ("Pellridge Foods Group", "Pellridge Foods Ltd.", "Pellridge Foods's"):
        r = one("x", f"{phrase} missed a payment.", tags={phrase: "company"}, events=(ALERTING,))
        assert linked(r) == {"CP-1001"}, (phrase, r.links)
        assert r.status == recipe.ALERT, (phrase, r.links)
    # Two borrowers in one model span: both are linked, each read on its own; neither dropped.
    phrase = "Pellridge Foods and Ferncastle Dairy"
    r = one(
        "x", f"{phrase} missed payments.", tags={phrase: "company"}, events=(ALERTING, ALERTING)
    )
    assert linked(r) == {"CP-1001", "CP-1008"}, r.links
    assert len(r.events) == 2
    assert {e.status for e in r.events} == {recipe.ALERT}
    # Each name in a two-name span is checked on its own words. The
    # longer "Northwind Energy Services" goes to review; Halden Cold Storage still links.
    phrase = "Northwind Energy Services and Halden Cold Storage"
    r = one("x", f"{phrase} opened sites.", tags={phrase: "company"}, events=(QUIET,))
    assert r.status == recipe.REVIEW, r.links
    assert linked(r) == {"CP-1003"}, r.links
    first = next(link for link in r.links if link.mention == "Northwind Energy")
    assert "different name that contains" in first.reason, r.links
    # "and" joins two names; after a single name it is an ordinary extra word.
    r = one("x", "Halden and Sons cut jobs.", tags={"Halden and Sons": "company"})
    assert (r.status, linked(r)) == (recipe.REVIEW, set()), r.links
    # A span whose text isn't the window text at its offsets is dropped and noted.
    bad = [{"entities": [{"label": "company", "text": "Nope", "start": 0, "end": 4, "score": 0.9}]}]
    r = one("x", "Pellridge Foods Ltd opened a site.", ner=[bad], events=(QUIET,))
    assert r.status == recipe.REVIEW, r.reasons
    assert "invalid span" in r.reasons[0], r.reasons
    # Spans under min_entity_score are dropped by SIE: every NuNER Zero call sends the
    # threshold, and asks for an error rather than a window cut short.
    _, _, client = run([article("T-1", "Rates", "Nothing here.")])
    limit = RULES["thresholds"]["min_entity_score"]
    expected = {"threshold": limit, "overflow_policy": "error"}
    assert client.ner_options == [expected], client.ner_options
    print("spans: longer names, suffixes, two names in one span, bad offsets, threshold ok")


def check_context() -> None:
    # N-09 shape: the competitor's insolvency sentences are not read for the borrower.
    client = StandIn(
        tags={"Elmstead Creameries": "company", "Elmstead": "company"}, events=(QUIET,)
    )
    body = (
        "Elmstead Creameries entered administration. Administrators were named. "
        "Ferncastle Dairy Ltd said it was unaffected. Ferncastle expects more orders. "
        "Elmstead Creameries owed 4 million."
    )
    results, _, client = run([article("T-1", "Elmstead Creameries fails", body)], client)
    assert results["T-1"].status == recipe.FILED, results["T-1"]
    assert client.read == [
        "Ferncastle Dairy Ltd said it was unaffected. Ferncastle expects more orders."
    ]
    # The next sentence is read when it names no other company, and the headline gets a stop.
    client = StandIn(events=(ALERTING,))
    run(
        [article("T-1", "Pellridge Foods in trouble", "The firm missed a payment. More soon.")],
        client,
    )
    assert client.read == ["Pellridge Foods in trouble. The firm missed a payment."], client.read
    client = StandIn(events=(ALERTING,))
    body = "Pellridge Foods Ltd is late. The firm missed a payment. Nothing else."
    run([article("T-1", "Update", body)], client)
    assert client.read == ["Pellridge Foods Ltd is late. The firm missed a payment."], client.read
    print("context: borrower sentences plus the next, never a competitor's ok")


def check_events() -> None:
    body = "Pellridge Foods Ltd said something."

    def status(reply: dict[str, Any]) -> recipe.Event:
        return one("x", body, events=(reply,)).events[0]

    assert status(event(missed_payment=0.6, none=0.3)).status == recipe.ALERT
    assert status(event(missed_payment=0.5, none=0.4)).status == recipe.ALERT  # at the limit
    assert status(event(none=0.8, lawsuit=0.1)).status == recipe.FILED
    # A low-score warning that contradicts "none" is kept: review, not filed.
    e = status(event(none=0.6, insolvency=0.3))
    assert e.status == recipe.REVIEW, e
    assert "event unclear: top warning insolvency 0.30" in e.reason, e
    # A tie at the limit is an alert: the warning is at 0.50 and not under `none`.
    assert status(event(missed_payment=0.5, none=0.5)).status == recipe.ALERT
    # Tied under the limit: review.
    assert status(event(missed_payment=0.45, none=0.45)).status == recipe.REVIEW
    # max_warning_when_filed: a warning at exactly 0.25 is not "under" it; just below is filed.
    assert status(event(none=0.7, lawsuit=0.25)).status == recipe.REVIEW
    assert status(event(none=0.7, lawsuit=0.2499)).status == recipe.FILED
    # Neither confident: review.
    assert status(event(none=0.4, lawsuit=0.4)).status == recipe.REVIEW
    # Filed rows keep the warning the model leaned to most, so the credit file shows it.
    e = status(event(none=0.8, lawsuit=0.1))
    assert (e.top_warning, e.top_warning_score) == ("lawsuit", 0.1), e
    # The schema puts the borrower's name, as the article writes it, into the question.
    schema = recipe.event_schema("Halden", RULES)[recipe.QUESTION_ID]
    assert schema["instructions"] == "What does this text report about Halden?", schema
    assert schema["criteria"]["lawsuit"].startswith("Halden is being sued"), schema
    print("events: alert and filed need positive evidence; contrary scores go to review ok")


def check_replies() -> None:
    good = event(lawsuit=0.8)
    probs = good["data"][recipe.QUESTION_ID]["probabilities"]
    assert recipe.read_probabilities(good, EVENTS) == probs
    bad: list[tuple[Any, str]] = [
        ({"error": {"code": "x"}}, "has no `data.event.probabilities` (item error"),
        ({"data": {}}, "has no `data.event.probabilities`"),
        ("not a dict", "has no `data."),
        ({"data": {recipe.QUESTION_ID: {"probabilities": {"lawsuit": 1.0}}}}, "missing ["),
        (
            {"data": {recipe.QUESTION_ID: {"probabilities": probs | {"fraud": 0.0}}}},
            "extra ['fraud']",
        ),
    ]
    for value in (float("nan"), float("inf"), 1.5, -0.1, True, "0.9", None):
        broken = {**probs, "lawsuit": value}
        bad.append(({"data": {recipe.QUESTION_ID: {"probabilities": broken}}}, "not a number"))
    for answer, words in bad:
        caught = raised(functools.partial(recipe.read_probabilities, answer, EVENTS))
        assert type(caught) is SIEError, (answer, caught)
        assert words in str(caught), (answer, caught)
    # NuNER Zero: a result without `entities` is a failed call; an empty list is "nothing".
    assert recipe.read_entities([{"entities": []}], 1) == [[]]
    for answer, words in (
        ([{"error": "boom"}], "no `entities` list (item error: boom)"),
        ([{"entities": None}], "no `entities` list"),
        ([], "0 results for 1 windows"),
    ):
        caught = raised(functools.partial(recipe.read_entities, answer, 1))
        assert type(caught) is SIEError, (answer, caught)
        assert words in str(caught), (answer, caught)
    print("replies: missing keys, wrong events and non-numbers are failed calls ok")


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
TWO = [
    article("A", "Pellridge Foods misses payment", "Pellridge Foods Ltd missed a payment."),
    article("B", "Sallowmere opens site", "Sallowmere Building Supplies Ltd opened a site."),
]


def check_sie_failure() -> None:
    for error in TRANSIENT:
        assert not recipe.is_fatal(error), f"{type(error).__name__} should be transient"
    for error in FATAL:
        assert recipe.is_fatal(error), f"{type(error).__name__} should stop the run"

    # A transient event failure: that borrower goes to review with the error cut short; the
    # other article still comes out and the outputs are written.
    long = "connection refused " + "x" * 300
    for error in [*TRANSIENT, SIEConnectionError(long)]:
        results, output, _ = run(TWO, StandIn(events=(error, QUIET)))
        assert results["A"].status == recipe.REVIEW, results["A"]
        assert results["A"].events[0].reason.startswith("event model: the SIE call failed: ")
        assert results["B"].status == recipe.FILED
        assert (output / "review.csv").exists()
        assert (output / "filed.csv").exists()
    reason = results["A"].events[0].reason
    assert len(reason) == len("event model: the SIE call failed: SIEConnectionError: ") + 200
    # A transient entity-model failure: every article in the batch goes to review, the list
    # names are still linked, and the event model still runs.
    results, _, _ = run(
        TWO, StandIn(ner=[ServerError("busy", status_code=503)], events=(ALERTING, QUIET))
    )
    assert results["A"].status == recipe.ALERT, results["A"]  # the alert still goes out
    assert results["B"].status == recipe.REVIEW, results["B"]
    assert "window 1 was not read by the entity model" in results["A"].reasons[0]
    assert linked(results["A"]) == {"CP-1001"}
    # Fix: with the entity model down, an alias alone isn't linked (it could be a town).
    results, _, _ = run(
        [article("H", "Flood in Halden", "Roads into Halden reopened."), TWO[0]],
        StandIn(ner=[ServerError("busy", status_code=503)], events=(QUIET,)),
    )
    assert linked(results["H"]) == set(), results["H"].links
    assert results["H"].status == recipe.REVIEW, results["H"]
    # A reply missing `entities` takes the same path.
    results, _, _ = run(TWO, StandIn(ner=[[{"oops": 1}, {"oops": 1}]], events=(QUIET, QUIET)))
    assert results["A"].status == recipe.REVIEW
    assert "no `entities` list" in results["A"].reasons[0]

    def stops(client: StandIn) -> str:
        """The stop message; also checks the run wrote nothing."""
        folder = Path(tempfile.mkdtemp())
        try:
            run(TWO, client, folder)
        except recipe.SIEStopped as error:
            assert not (folder / "out").exists(), "a stopped run wrote outputs"
            client.events.clear()
            return str(error)
        raise AssertionError("the run did not stop")

    # Every call failed: the run stops and writes nothing.
    message = stops(StandIn(ner=[SIEConnectionError(long)], events=(SIEConnectionError(long),) * 2))
    assert message.startswith(f"Every SIE call failed (3 of 3 to {NER}, {DECIDE})"), message
    assert long in message, message
    assert "Check SIE_BASE_URL and your network." in message, message
    assert "No outputs were written." in message
    message = stops(StandIn(ner=[ProvisioningError("no GPU")], events=({"data": {}}, {"data": {}})))
    assert "may still be starting or SIE is busy; run again in a few minutes." in message
    # A refusal stops the run at once, with SIE's full message, from either model.
    message = stops(StandIn(ner=[RequestError("bad key " + "y" * 300, status_code=401)]))
    assert message.startswith(f"SIE refused the call to {NER}, RequestError (HTTP 401)"), message
    assert "y" * 300 in message
    assert "SIE_API_KEY" in message
    message = stops(StandIn(events=(QUIET, InsufficientCreditsError("no credits"))))
    assert message.startswith(f"SIE refused the call to {DECIDE}, "), message
    message = stops(StandIn(ner=[ModelLoadFailedError("gated", permanent=True)]))
    assert message.startswith(f"SIE can't load {NER}, ModelLoadFailedError"), message
    message = stops(StandIn(ner=[RequestError("unexpected body")]))
    assert "SIE_API_KEY" not in message, message
    assert "SIE_BASE_URL" in message, message
    # A bug in our own code is not hidden, and main turns only SIEStopped into a clean exit.
    caught = raised(lambda: run(TWO, StandIn(ner=[TypeError("bad argument")])))
    assert type(caught) is TypeError, f"TypeError became {type(caught).__name__}"

    def bug() -> None:
        raise RuntimeError("a bug in recipe.py")

    def refusal() -> None:
        raise recipe.SIEStopped("SIE refused")

    caught = raised(lambda: recipe.stop_on_refusal(bug))
    assert type(caught) is RuntimeError, caught
    caught = raised(lambda: recipe.stop_on_refusal(refusal))
    assert type(caught) is SystemExit, caught
    assert str(caught) == "Stopped: SIE refused", caught
    print("sie failure: transient errors go to review; refusals and all-failed stop ok")


def check_outputs() -> None:
    # The statuses name what the checks found; none of them is a credit decision.
    assert recipe.STATUSES == ("alert", "filed", "review", "not_a_borrower")
    assert not {"downgrade", "watchlist", "default", "approved", "tagged"} & set(recipe.STATUSES)
    risky = article("A", '=HYPERLINK("x")', "Pellridge Foods Ltd missed a payment.")
    second = article("B", "Halden sued", "A buyer sued Halden.")
    third = article("C", "Northwind news", "Northwind lost a customer.")
    _results, output, _ = run(
        [risky, second, third], StandIn(events=(ALERTING, event(lawsuit=0.7)))
    )
    # One message per credit officer: both alerts are Saoirse Mallin's, in one file.
    files = sorted(p.name for p in (output / "alerts").iterdir())
    assert files == ["saoirse-mallin.md"], files
    message = (output / "alerts" / "saoirse-mallin.md").read_text(encoding="utf-8")
    assert "2 news stories" in message
    assert "Halden Cold Storage Ltd (CP-1003" in message
    assert "https://test-wire.example/A" in message
    # The review queue is the analyst's: one row per name and reason, CSV cells safe.
    rows = list(csv.DictReader((output / "review.csv").open(encoding="utf-8")))
    assert [r["article_id"] for r in rows] == ["C"], rows
    assert list(rows[0]) == list(recipe.REVIEW_FIELDS)
    recipe.write_csv(
        output / "x.csv", recipe.REVIEW_FIELDS, [dict(rows[0], headline=risky["headline"])]
    )
    row = next(csv.DictReader((output / "x.csv").open(encoding="utf-8")))
    assert row["headline"] == '\'=HYPERLINK("x")', row
    saved = json.loads((output / "articles.json").read_text(encoding="utf-8"))
    assert [a["status"] for a in saved] == ["alert", "alert", "review"], saved
    print("outputs: four finding statuses, one message per officer, review rows, CSV safe ok")


# --- The agent: tools, judge and statuses ------------------------------------------------------

FILES = agent.CreditFiles()
LOSS = event(customer_loss=0.88, none=0.04)  # what the event check says for N-04 fixed to CP-1004
ARTICLE_QUOTE = "Northwind has lost its largest customer, a national parcel carrier"


def committed(article_id: str) -> recipe.Result:
    """One committed article through the workflow, with stand-in tags like the live run's."""
    articles = [json.loads(line) for line in recipe.NEWS_PATH.read_text().splitlines()]
    picked = [a for a in articles if a["id"] == article_id]
    replies = {"N-01": (ALERTING,), "N-09": (QUIET,)}.get(article_id, ())
    results, _, _ = run(picked, StandIn(tags={"Tolland Packaging": "company"}, events=replies))
    return results[article_id]


def job(result: recipe.Result, *replies: Any, read: tuple[str, ...] = ()) -> agent.Job:
    """A job on one workflow result; `replies` are the event checks' stand-in replies, in call
    order (the judge checks every candidate on a suggested link's row, in row order)."""
    rows = agent.review_items(result, CPS, RULES)
    box = agent.Job(result, rows, CPS, FILES, RULES, StandIn(events=replies), recipe.CallLog())
    for cp_id in read:
        box.run("read_credit_file", {"counterparty_id": cp_id})
    return box


def fresh_for(box: agent.Job) -> Callable[[str], dict[str, Any] | None]:
    """The judge's own event check, as answer() runs it."""

    def fresh(cp_id: str) -> dict[str, Any] | None:
        try:
            return agent.event_check(box.client, box.calls, box.result, cp_id, CPS, RULES)
        except agent.ToolError as error:
            box.sie_failures.append(str(error))
            return None

    return fresh


def judged(box: agent.Job, finding: dict[str, Any]) -> tuple[list[str], str]:
    """The judge's problems and the job's status for one finding."""
    problems, checks = agent.check_finding(finding, box, fresh_for(box))
    return problems, agent.status_of(finding, problems, checks, box, "", False)


def item(row: int = 1, outcome: str = "suggest_link", cp: str = "CP-1004", **kw: Any) -> Any:
    found: dict[str, Any] = {
        "row": row,
        "outcome": outcome,
        "counterparty_id": cp,
        "event": "customer_loss",
        "score": 0.88,
        "statements": [
            {"source": "article", "quote": ARTICLE_QUOTE},
            {"source": "CP-1004", "quote": "national parcel carrier (about a third of revenue)"},
        ],
    }
    found.update(kw)
    return found


def quotes(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"source": source, "quote": quote} for source, quote in pairs]


GOOD_NOTE = (
    "Suggest CP-1004 Northwind Logistics Ltd: its largest customer is a national parcel "
    "carrier, about a third of revenue. CP-1005 supplies regional utilities. With the link "
    "fixed the event check says customer_loss 0.88."
)
BOTH = ("CP-1004", "CP-1005")


def has(problems: list[str], words: str) -> bool:
    return any(words in problem for problem in problems)


def check_agent_reasons() -> None:
    n04, n07 = committed("N-04"), committed("N-07")
    rows = agent.review_items(n04, CPS, RULES)
    assert [(r.kind, r.candidates) for r in rows] == [("shared_alias", ["CP-1004", "CP-1005"])]
    assert rows[0].allowed == ["suggest_link", "left_for_person"]
    # N-07 has two rows, both counted from the workflow's review rows, both ticker conflicts:
    # the agent may only leave them to the analyst.
    rows = agent.review_items(n07, CPS, RULES)
    assert [(r.kind, r.allowed) for r in rows] == [("ticker_conflict", ["left_for_person"])] * 2
    for reason, kind in (
        (
            "event unclear: top warning plant_closure 0.31, none 0.40 (alert needs 0.50)",
            "event_unclear",
        ),
        (
            "'Northwind Energy Services' is a different name that contains the borrower name",
            "longer_name",
        ),
        (
            "window 1 was not read by the entity model: the SIE call failed: ServerError",
            "sie_failed",
        ),
        ("event model: the SIE call failed: RateLimitError", "sie_failed"),
        ("ticker written as LSE: NWEG, but the list has NYSE:NWEG for CP-1005", "ticker_conflict"),
    ):
        assert agent.reason_kind(reason, RULES)[0] == kind, (reason, kind)
    # A reason the rules don't know allows only the person; a failed call goes to no agent.
    assert agent.reason_kind("something new", RULES) == ("other", ["left_for_person"])
    assert agent.reason_kind("the SIE call failed: x", RULES)[1] == []
    # Every allowed outcome is one submit_finding accepts, and the status list is rules.yaml's.
    allowed = {o for spec in RULES["agent"]["review_reasons"].values() for o in spec["allowed"]}
    assert allowed <= set(agent.OUTCOMES), allowed
    assert set(RULES["agent"]["statuses"]) == {
        "finding_checked",
        "call_not_checked",
        "left_for_person",
        "check_failed",
        "out_of_steps",
        "no_finding",
        "sie_failed",
    }
    print("agent reasons: shared alias, ticker conflicts, unknown reasons and failed calls ok")


def check_agent_event_check() -> None:
    # A borrower the workflow linked: the agent's event check reads exactly what it read.
    for article_id in ("N-01", "N-09"):
        result = committed(article_id)
        for e in result.events:
            assert agent.event_text(result, e.counterparty_id, RULES)[0] == e.model_read
    # N-04 with the link fixed to CP-1004 reads the sentences naming Northwind, and with it
    # fixed to CP-1005 the very same sentences: the event check can't tell them apart.
    n04 = committed("N-04")
    text, name = agent.event_text(n04, "CP-1004", RULES)
    assert name == "Northwind", text
    assert "Northwind has lost its largest customer" in text, text
    assert agent.event_text(n04, "CP-1005", RULES) == (text, name)
    box = job(n04, LOSS)
    got = json.loads(box.run("check_event_for", {"counterparty_id": "CP-1004"}))
    assert (got["status"], got["event"], got["score"]) == ("alert", "customer_loss", 0.88), got
    # A borrower no mention lists is refused, not checked on some other text.
    caught = raised(lambda: box.run("check_event_for", {"counterparty_id": "CP-1009"}))
    assert isinstance(caught, agent.ToolError), caught
    # A transient SIE failure is a tool error and marks the job for a person.
    box = job(n04, ServerError("busy", status_code=503))
    caught = raised(lambda: box.run("check_event_for", {"counterparty_id": "CP-1004"}))
    assert isinstance(caught, agent.ToolError), caught
    assert box.sie_failures, caught
    # A refusal stops the whole run, as in the workflow.
    box = job(n04, InsufficientCreditsError("no credits"))
    caught = raised(lambda: box.run("check_event_for", {"counterparty_id": "CP-1004"}))
    assert isinstance(caught, recipe.SIEStopped), caught
    # Tool input is checked against the tool's fields, not left to a TypeError.
    for name, given in (
        ("read_credit_file", {"id": "CP-1004"}),
        ("read_credit_file", {"counterparty_id": 1004}),
        ("get_review_item", {"x": ""}),
        ("lookup_counterparty", {"name": "Northwind"}),
    ):
        caught = raised(functools.partial(box.run, name, given))
        assert isinstance(caught, agent.ToolError), (name, given, caught)
    caught = raised(
        lambda: box.run(
            "lookup_counterparty", {"counterparty_id": "CP-1", "name": "x", "ticker": ""}
        )
    )
    assert isinstance(caught, agent.ToolError), caught
    assert isinstance(raised(lambda: FILES.read("CP-9999")), agent.ToolError)
    assert "Brackwater repair yard (about 60% of revenue); Ostley Quay workshop" in FILES.values(
        "CP-1009"
    )
    print("agent event check: same text as the workflow, link fixed, SIE errors, tool input ok")


def check_agent_scope() -> None:
    """Tools reach only this job's borrowers: the row's candidates and what a lookup returned."""
    n04, n07 = committed("N-04"), committed("N-07")
    box = job(n04)
    # Article text can't steer a read to another borrower.
    for cp_id in ("CP-1001", "CP-1009"):
        caught = raised(functools.partial(box.run, "read_credit_file", {"counterparty_id": cp_id}))
        assert isinstance(caught, agent.ToolError), (cp_id, caught)
    for given in (
        {"counterparty_id": "CP-1001", "name": "", "ticker": ""},
        {"counterparty_id": "", "name": "Pellridge Foods", "ticker": ""},
        {"counterparty_id": "", "name": "", "ticker": "NYSE:NWEG"},
    ):
        caught = raised(functools.partial(box.run, "lookup_counterparty", given))
        assert isinstance(caught, agent.ToolError), (given, caught)
    assert box.read_files == [], box.read_files
    looked = json.loads(
        box.run("lookup_counterparty", {"counterparty_id": "", "name": "Northwind", "ticker": ""})
    )
    assert [m["id"] for m in looked["matches"]] == ["CP-1004", "CP-1005"], looked
    # N-07: CP-1005 isn't a candidate; its file opens only after the ticker the article prints
    # was looked up, and a quote from it counts only then.
    box = job(n07, read=("CP-1007",))
    caught = raised(lambda: box.run("read_credit_file", {"counterparty_id": "CP-1005"}))
    assert isinstance(caught, agent.ToolError), caught
    left = {
        "row": 1,
        "outcome": "left_for_person",
        "counterparty_id": "",
        "event": "",
        "score": None,
        "statements": quotes(("CP-1005", "gas storage and wholesale supply")),
    }
    finding = {"items": [left, {**left, "row": 2}], "note": "The ticker is CP-1005's; you decide."}
    problems, status = judged(box, finding)
    assert has(problems, "doesn't exist in this job: 'CP-1005'"), problems
    assert status == "check_failed", problems
    looked = json.loads(
        box.run("lookup_counterparty", {"counterparty_id": "", "name": "", "ticker": "NYSE:NWEG"})
    )
    assert [m["id"] for m in looked["matches"]] == ["CP-1005"], looked
    box.run("read_credit_file", {"counterparty_id": "CP-1005"})
    problems, status = judged(box, finding)
    assert (problems, status) == ([], "left_for_person"), problems
    print("agent scope: reads and quotes only the row's candidates and looked-up borrowers ok")


def check_agent_judge() -> None:
    n04 = committed("N-04")
    # Both candidates' checks with the link fixed come back the same (they read the same
    # sentences): the evidence holds, but the check can't tell the pick apart, so the call
    # isn't checked, whichever Northwind the agent picks.
    problems, status = judged(
        job(n04, LOSS, LOSS, read=BOTH), {"items": [item()], "note": GOOD_NOTE}
    )
    assert (problems, status) == ([], "call_not_checked"), (problems, status)
    wrong = item(
        cp="CP-1005",
        statements=quotes(
            ("article", ARTICLE_QUOTE),
            ("CP-1005", "gas storage and wholesale supply to regional utilities"),
        ),
    )
    note = "Suggest CP-1005 Northwind Energy Supply; event check customer_loss 0.88."
    problems, status = judged(job(n04, LOSS, LOSS, read=BOTH), {"items": [wrong], "note": note})
    assert status != "finding_checked", (problems, status)
    assert (problems, status) == ([], "call_not_checked"), (problems, status)
    # Checks that differ: the pick whose check agrees is finding_checked, the other fails.
    box = job(n04, LOSS, QUIET, read=BOTH)
    problems, status = judged(box, {"items": [item()], "note": GOOD_NOTE})
    assert (problems, status) == ([], "finding_checked"), (problems, status)
    problems, status = judged(job(n04, LOSS, QUIET, read=BOTH), {"items": [wrong], "note": note})
    assert has(problems, "link fixed to CP-1005 says none"), problems
    assert status == "check_failed", problems
    # Adversarial findings: each must fail, so none can come out finding_checked.
    cases: list[tuple[str, dict[str, Any], tuple[str, ...], str]] = [
        (
            "an invented quote",
            item(
                statements=quotes(
                    ("article", ARTICLE_QUOTE),
                    ("CP-1004", "national parcel carrier (about half of revenue)"),
                )
            ),
            BOTH,
            "isn't word for word in CP-1004",
        ),
        (
            "CP-1005's file quoted as CP-1004's",
            item(
                statements=quotes(
                    ("article", ARTICLE_QUOTE), ("CP-1004", "supply to regional utilities")
                )
            ),
            BOTH,
            "isn't word for word in CP-1004",
        ),
        (
            "only the other borrower's file backs the link",
            item(
                statements=quotes(
                    ("article", ARTICLE_QUOTE), ("CP-1005", "supply to regional utilities")
                )
            ),
            BOTH,
            "needs a quote from CP-1004's credit file",
        ),
        (
            "a quote stitched from two fields",
            item(
                statements=quotes(
                    ("article", ARTICLE_QUOTE),
                    ("CP-1004", "about a third of revenue) term loan"),
                )
            ),
            BOTH,
            "isn't word for word",
        ),
        (
            "a one-letter quote",
            item(statements=quotes(("article", "a"), ("CP-1004", "o"))),
            BOTH,
            "under 4 words",
        ),
        (
            "a field label as a quote",
            item(
                statements=quotes(
                    ("article", ARTICLE_QUOTE), ("CP-1004", "counterparty_id: CP-1004")
                )
            ),
            BOTH,
            "under 4 words",
        ),
        (
            "a field label with its value",
            item(
                statements=quotes(
                    ("article", ARTICLE_QUOTE),
                    ("CP-1004", "largest_customers: national parcel carrier"),
                )
            ),
            BOTH,
            "isn't word for word in CP-1004",
        ),
        (
            "part of a word",
            item(
                statements=quotes(
                    ("article", "orthwind has lost its largest"),
                    ("CP-1004", "national parcel carrier (about a third of revenue)"),
                )
            ),
            BOTH,
            "isn't word for word in article",
        ),
        (
            "an article quote without the mention",
            item(
                statements=quotes(
                    ("article", "a national parcel carrier that accounted for about"),
                    ("CP-1004", "national parcel carrier (about a third of revenue)"),
                )
            ),
            BOTH,
            "contains the mention 'Northwind'",
        ),
        (
            "a borrower outside the job as a source",
            item(
                statements=quotes(
                    ("article", ARTICLE_QUOTE),
                    ("CP-1004", "national parcel carrier (about a third of revenue)"),
                    ("CP-1001", "two supermarket chains (about 70% of revenue)"),
                )
            ),
            BOTH,
            "doesn't exist in this job: 'CP-1001'",
        ),
        ("a borrower the workflow didn't list", item(cp="CP-1009"), BOTH, "not a candidate"),
        ("only the favoured file read", item(), ("CP-1004",), "CP-1005 was never read"),
        ("a stronger score than the check", item(score=0.95), BOTH, "the event check says 0.88"),
        ("a different event", item(event="plant_closure"), BOTH, "says customer_loss 0.88"),
        ("an alert on a name case", item(outcome="suggest_alert"), BOTH, "not allowed"),
        (
            "a score on a row left to the analyst",
            item(outcome="left_for_person", cp="", event="", score=0.9),
            BOTH,
            "leave it null",
        ),
        (
            "a source that doesn't exist",
            item(
                statements=quotes(
                    ("article", ARTICLE_QUOTE), ("loan file", "national parcel carrier (about")
                )
            ),
            BOTH,
            "doesn't exist",
        ),
    ]
    for name, found, read, words in cases:
        box = job(n04, LOSS, QUIET, read=read)
        problems, status = judged(box, {"items": [found], "note": GOOD_NOTE})
        assert has(problems, words), (name, problems)
        assert status == "check_failed", (name, problems)
    # The note: a figure from nowhere, a note naming the other borrower, an empty note, a link
    # or an HTML tag.
    for note, words in (
        (GOOD_NOTE + " It was 45% of revenue.", "45 in the note"),
        (GOOD_NOTE + " See also CP-2001.", "2001 in the note"),
        ("Suggest CP-1005 Northwind Energy Supply.", "doesn't name the suggested borrower CP-1004"),
        ("  ", "note to the analyst is empty"),
        (GOOD_NOTE + " ![x](https://evil.example/p.png)", "a link or an HTML tag"),
        (GOOD_NOTE + " <img src=x>", "a link or an HTML tag"),
    ):
        problems, _ = judged(job(n04, LOSS, QUIET, read=BOTH), {"items": [item()], "note": note})
        assert has(problems, words), (note, problems)
    # Rows: a missing one, an extra one, one twice.
    for items, words in (
        ([], "row 1 ("),
        ([item(), item(row=2)], "row 2 isn't one"),
        ([item(), item()], "more than one item"),
    ):
        problems, status = judged(
            job(n04, LOSS, QUIET, read=BOTH), {"items": items, "note": GOOD_NOTE}
        )
        assert has(problems, words), (words, problems)
        assert status == "check_failed", (words, problems)
    # A link whose event is still unclear with the link fixed: the evidence held, the call didn't.
    unsure = event(customer_loss=0.40, none=0.35)
    box = job(n04, unsure, QUIET, read=BOTH)
    found = item(score=0.40)
    problems, status = judged(box, {"items": [found], "note": GOOD_NOTE.replace("0.88", "0.40")})
    assert problems == [], (problems, status)
    assert status == "call_not_checked", (problems, status)
    # The judge's own event check failed: a person reads it, never finding_checked.
    box = job(n04, ServerError("busy", status_code=503), QUIET, read=BOTH)
    problems, status = judged(box, {"items": [item()], "note": GOOD_NOTE})
    assert status == "sie_failed", (problems, status)
    print(
        f"agent judge: identical checks aren't a checked call; {len(cases) + 9} broken ones fail ok"
    )


def check_agent_ticker_rule() -> None:
    n07 = committed("N-07")
    note = "The ticker NWEG is CP-1005's parent; the business fits Tolland. Leave it to you."
    left = {
        "row": 1,
        "outcome": "left_for_person",
        "counterparty_id": "",
        "event": "",
        "score": None,
        "statements": quotes(("CP-1007", "recycled food trays and cartons")),
    }
    both_rows = [left, {**left, "row": 2}]
    problems, status = judged(job(n07, read=("CP-1007",)), {"items": both_rows, "note": note})
    assert problems == [], problems
    assert status == "left_for_person", problems
    # Covering one row of two is caught: the row count comes from the workflow, not the agent.
    problems, status = judged(job(n07, read=("CP-1007",)), {"items": [left], "note": note})
    assert has(problems, "row 2 ("), problems
    assert status == "check_failed", problems
    # Settling the ticker conflict anyway fails, whichever borrower it picks.
    link = item(
        outcome="suggest_link",
        cp="CP-1007",
        event="none",
        score=0.9,
        statements=quotes(
            ("article", "Tolland Packaging (NYSE: NWEG) won"),
            ("CP-1007", "recycled food trays and cartons"),
        ),
    )
    for found, words in (
        (link, "not allowed"),
        ({**link, "counterparty_id": "CP-1005"}, "not a candidate"),
    ):
        box = job(n07, QUIET, QUIET, read=("CP-1007",))
        finding = {"items": [found, {**found, "row": 2}], "note": note + " CP-1007 CP-1005"}
        problems, status = judged(box, finding)
        assert has(problems, words), (words, problems)
        assert status == "check_failed", (words, problems)
    print("agent ticker rule: only left_for_person, on both N-07 rows ok")


def check_agent_longer_name() -> None:
    result = one(
        "x", "Northwind Energy Services cut jobs.", tags={"Northwind Energy Services": "company"}
    )
    rows = agent.review_items(result, CPS, RULES)
    assert [(r.kind, r.candidates) for r in rows] == [("longer_name", ["CP-1005"])], rows
    other = item(
        outcome="suggest_not_a_borrower",
        cp="",
        event="",
        score=None,
        statements=quotes(
            ("article", "Northwind Energy Services cut jobs"),
            ("CP-1005", "gas storage and wholesale supply to regional utilities"),
        ),
    )
    note = "Northwind Energy Services cut jobs; CP-1005 is a gas storage business."
    problems, status = judged(job(result, read=("CP-1005",)), {"items": [other], "note": note})
    assert (problems, status) == ([], "call_not_checked"), (problems, status)
    # No evidence, or only the article: the judge says what's missing.
    for statements, words in (
        ([], "needs a quote from the article"),
        ([], "needs a quote from the credit file of CP-1005"),
        (other["statements"][:1], "needs a quote from the credit file of CP-1005"),
    ):
        found = {**other, "statements": statements}
        problems, status = judged(job(result, read=("CP-1005",)), {"items": [found], "note": note})
        assert has(problems, words), (words, problems)
        assert status == "check_failed", problems
    print("agent longer name: not a borrower needs the article and the candidate's file ok")


def check_agent_unclear_event() -> None:
    unsure = event(plant_closure=0.38, none=0.42)
    result = one(
        "Ostley Marine reviews yard",
        "Ostley Marine Services is reviewing the future of its Brackwater repair yard.",
        events=(unsure,),
    )
    assert result.status == "review", result.events
    rows = agent.review_items(result, CPS, RULES)
    assert [r.kind for r in rows] == ["event_unclear"], rows
    alert = item(
        outcome="suggest_alert",
        cp="CP-1009",
        event="plant_closure",
        score=0.38,
        statements=quotes(
            ("article", "reviewing the future of its Brackwater repair yard"),
            ("CP-1009", "secured on the Brackwater yard"),
        ),
    )
    note = "Suggest an alert for CP-1009: the Brackwater yard is about 60% of revenue."
    # Every quote holds, so the evidence passes, but code can't confirm the call.
    problems, status = judged(job(result, read=("CP-1009",)), {"items": [alert], "note": note})
    assert problems == [], (problems, status)
    assert status == "call_not_checked", (problems, status)
    for found, words in (
        ({**alert, "event": "none"}, "needs a warning event"),
        ({**alert, "outcome": "suggest_filed"}, "needs the event 'none'"),
        ({**alert, "score": 0.5}, "the workflow's event check gave 0.38"),
        ({**alert, "event": "lawsuit"}, "matches no number"),
    ):
        problems, status = judged(job(result, read=("CP-1009",)), {"items": [found], "note": note})
        assert has(problems, words), problems
        assert status == "check_failed", problems
    # A null score is fine: nothing to match.
    found = {**alert, "score": None}
    problems, status = judged(job(result, read=("CP-1009",)), {"items": [found], "note": note})
    assert (problems, status) == ([], "call_not_checked"), problems
    print("agent unclear event: evidence and score checked, the call left to the analyst ok")


class Block:
    """A stand-in content block from Claude: text or a tool call."""

    def __init__(self, kind: str, **fields: Any) -> None:
        self.type = kind
        for key, value in fields.items():
            setattr(self, key, value)


class Usage:
    input_tokens, output_tokens = 1000, 100
    cache_read_input_tokens, cache_creation_input_tokens = 0, 0


class Reply:
    def __init__(self, stop: str, *content: Block, model: str = "claude-opus-5-5") -> None:
        self.stop_reason, self.content, self.usage = stop, list(content), Usage()
        self.model = model


class Claude:
    """Stands in for the Anthropic client: returns the planned replies in order (or raises a
    planned error)."""

    def __init__(self, *replies: Reply | BaseException) -> None:
        self.replies = list(replies)
        self.beta = self
        self.messages = self

    def create(self, **_: Any) -> Reply:
        assert self.replies, "Claude was asked again and no reply was planned"
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def use(name: str, number: int, **inputs: Any) -> Block:
    return Block("tool_use", name=name, id=f"t{number}", input=inputs)


def check_agent_loop() -> None:
    n04 = committed("N-04")
    reads = [
        use("read_credit_file", n, counterparty_id=c) for n, c in ((1, "CP-1004"), (2, "CP-1005"))
    ]
    wrong = {"items": [item(score=0.95)], "note": GOOD_NOTE}
    right = {"items": [item()], "note": GOOD_NOTE}
    # A finding the check sends back once, then a fixed one: accepted after one revision. The
    # judge's two event checks run once for the job; the revision reuses them (a third SIE
    # call would find no stand-in reply and fail).
    claude = Claude(
        Reply("tool_use", *reads),
        Reply("tool_use", use("submit_finding", 3, **wrong)),
        Reply("tool_use", use("submit_finding", 4, **right)),
    )
    box = job(n04, LOSS, QUIET)
    result = agent.run_agent(claude, box, fresh_for(box))
    assert result.revisions == 1, result.first_review
    assert has(result.first_review, "0.88"), result.first_review
    status = agent.status_of(result.finding, result.problems, result.checks, box, "", False)
    assert status == "finding_checked", (status, result)
    assert result.tool_calls == 4, (status, result)
    assert box.client.events == [], box.client.events
    # A revision that drops the link keeps no checks from the first finding.
    left = item(outcome="left_for_person", cp="", event="", score=None)
    claude = Claude(
        Reply("tool_use", *reads),
        Reply("tool_use", use("submit_finding", 3, **wrong)),
        Reply("tool_use", use("submit_finding", 4, items=[left], note="Leave it to you.")),
    )
    box = job(n04, LOSS, QUIET)
    result = agent.run_agent(claude, box, fresh_for(box))
    assert result.checks == {}, result.checks
    # A tool call with the wrong fields is an error for Claude to read, not a crash.
    claude = Claude(
        Reply("tool_use", use("read_credit_file", 1, id="CP-1004")),
        Reply("end_turn"),
        Reply("end_turn"),
    )
    box = job(n04)
    result = agent.run_agent(claude, box, fresh_for(box))
    assert result.transcript[1]["is_error"], result.transcript
    # A refusal is no finding, never read as the agent's work failing the check.
    box = job(n04)
    result = agent.run_agent(Claude(Reply("refusal")), box, fresh_for(box))
    status = agent.status_of(
        result.finding, result.problems, result.checks, box, result.stopped, result.ran_out
    )
    assert status == "no_finding", (status, result.stopped)
    assert "declined" in result.stopped, (status, result.stopped)
    # Past the tool-call limit: out of steps, even if a finding follows.
    many = [use("get_review_item", n) for n in range(RULES["agent"]["max_lookups"] + 1)]
    box = job(n04, LOSS, QUIET, read=BOTH)
    result = agent.run_agent(
        Claude(Reply("tool_use", *many), Reply("tool_use", use("submit_finding", 99, **right))),
        box,
        fresh_for(box),
    )
    status = agent.status_of(
        result.finding, result.problems, result.checks, box, result.stopped, result.ran_out
    )
    assert status == "out_of_steps", status
    # Claude stops talking without a finding: asked once, then no_finding.
    box = job(n04)
    result = agent.run_agent(
        Claude(Reply("end_turn", Block("text", text="done")), Reply("end_turn")),
        box,
        fresh_for(box),
    )
    assert result.finding is None
    assert "without calling submit_finding" in result.stopped
    # An error that isn't a passing Claude API error stops the run instead of ending one job.
    caught = raised(lambda: agent.run_agent(Claude(ValueError("bug")), job(n04), lambda _: None))
    assert isinstance(caught, ValueError), caught
    print("agent loop: one revision, final checks only, bad input, refusal, limits ok")


def check_agent_claude_errors() -> None:
    """Passing Claude API errors end one job; the rest stop the run (needs the agent group)."""
    try:
        import anthropic
        import httpx2
    except ImportError:
        print("agent Claude errors: skipped, the agent group isn't installed")
        return
    request = httpx2.Request("POST", "https://api.anthropic.example/v1/messages")

    def status(code: int) -> Exception:
        response = httpx2.Response(code, request=request)
        kinds = {400: anthropic.BadRequestError, 401: anthropic.AuthenticationError}
        kind = kinds.get(code, anthropic.APIStatusError)
        return kind("x", response=response, body=None)

    n04 = committed("N-04")
    for error in (
        anthropic.APIConnectionError(request=request),
        anthropic.APITimeoutError(request=request),
        status(408),
        status(409),
        anthropic.RateLimitError("x", response=httpx2.Response(429, request=request), body=None),
        anthropic.InternalServerError(
            "x", response=httpx2.Response(500, request=request), body=None
        ),
    ):
        result = agent.run_agent(Claude(error), job(n04), lambda _: None)
        assert result.stopped.startswith("the Claude API failed"), (error, result.stopped)
    for error in (status(400), status(401), status(422)):
        caught = raised(functools.partial(agent.run_agent, Claude(error), job(n04), lambda _: None))
        assert caught is error, (error, caught)
    print("agent Claude errors: transient ones end the job, the rest stop the run ok")


def check_agent_retry() -> None:
    """A retry where every SIE call fails keeps the first pass; a refusal still stops."""

    class Flaky(StandIn):
        """NuNER Zero answers the first call, then raises `later` on every one after it."""

        def __init__(self, later: BaseException, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.later = later

        def extract(self, model: str, items: Any, **kwargs: Any) -> Any:
            if model == NER and self.ner_calls:
                self.ner_calls += 1
                raise self.later
            return super().extract(model, items, **kwargs)

    folder = Path(tempfile.mkdtemp())
    path = folder / "news.jsonl"
    articles = [
        article("R-1", "Pellridge Foods missed a payment", "Pellridge Foods missed a payment."),
        article("R-2", "Halden Cold Storage missed a payment", "Halden Cold Storage missed it."),
    ]
    path.write_text("".join(json.dumps(a) + "\n" for a in articles), encoding="utf-8")
    tags = {"Pellridge Foods": "company", "Halden Cold Storage": "company"}
    replies = (ALERTING, ServerError("busy", status_code=503))
    client = agent.CountingClient(
        Flaky(ServerError("busy", status_code=503), tags=tags, events=replies)
    )
    with contextlib.redirect_stdout(io.StringIO()):
        results, cost = agent.run_workflow(client, RULES, (path,), folder)
    statuses = {r.article.id: r.status for r in results}
    assert statuses == {"R-1": "alert", "R-2": "review"}, statuses
    assert cost["retried_articles"] == ["R-2"], cost
    assert cost["retry_failed"], cost
    r2 = next(r for r in results if r.article.id == "R-2")
    assert [row.kind for row in agent.review_items(r2, CPS, RULES)] == ["sie_failed"]
    # A refusal on the retry still stops the run.
    client = agent.CountingClient(
        Flaky(InsufficientCreditsError("no credits"), tags=tags, events=replies)
    )
    with contextlib.redirect_stdout(io.StringIO()):
        caught = raised(lambda: agent.run_workflow(client, RULES, (path,), folder))
    assert isinstance(caught, recipe.SIEStopped), caught
    print("agent retry: an all-failed retry keeps the first pass as sie_failed, a refusal stops ok")


def check_agent_outputs() -> None:
    n04 = committed("N-04")
    evil = "![x](https://evil.example/p.png) <img src=x> a | b"
    risky_item = item(statements=quotes(("article", evil)))
    note = "=HYPERLINK(1) " + GOOD_NOTE + "\n```\n# not a heading\n" + evil
    risky = {"items": [risky_item], "note": note}
    record = {
        "article_id": "N-04",
        "headline": evil,
        "status": "finding_checked",
        "rows": [{"number": 1, "mention": evil, "reason": "x\ny"}],
        "finding": risky,
        "problems": [f"the quote isn't word for word in article: {evil!r}"],
        "first_review": [],
        "judge_event_checks": {},
        "sie_failures": [],
        "stopped": "",
        "cost": {"claude_usd": 0.0},
        "transcript": [],
    }
    folder = Path(tempfile.mkdtemp())
    agent.write_outputs(folder, [n04], [record], {"articles": 1})
    written = sorted(p.name for p in folder.iterdir())
    assert written == ["agent"], written  # nothing next to it: no alerts/ for a credit officer
    rows = list(csv.DictReader((folder / "agent" / "review.csv").open(encoding="utf-8")))
    assert [(r["agent_status"], r["agent_outcome"]) for r in rows] == [
        ("finding_checked", "suggest_link CP-1004")
    ], rows
    assert rows[0]["agent_note"].startswith("'="), rows[0]["agent_note"]
    # Text from articles, files and Claude can't render as an image, a tag, a link or a table
    # cell break; the note sits in a fence longer than any backtick run in it.
    notes = (folder / "agent" / "review_notes.md").read_text(encoding="utf-8")
    outside = notes.split("````text")[0] + notes.split("````text")[1].split("\n````\n", 1)[1]
    for bad in (r"!\[", r"<img", r"\|", r"\]\(", r"\n# "):
        assert not re.search(r"(?<!\\)" + bad, outside), (bad, notes)
    assert "\\!\\[x\\]\\(https://evil.example/p.png\\)" in outside, notes
    assert "````text\n" + note + "\n````" in notes, notes
    assert agent.md_safe("a\nb | c") == "a b \\| c"
    # Each response is priced on the model that answered it; an unknown model is reported.
    prices = {
        "claude-opus-5-5": {"input": 1e6, "cache_write": 1e7, "cache_read": 1e8, "output": 1e9},
        "claude-opus-5": {"input": 2e6, "cache_write": 0, "cache_read": 0, "output": 0},
    }
    tokens = Usage()
    result = agent.AgentResult()
    result.count("claude-opus-5-5", tokens)
    result.count("claude-opus-5-20261001", tokens)
    result.count("claude-other", tokens)
    usd, unpriced = agent.claude_usd(result, prices)
    assert usd == (1000 * 1 + 100 * 1000) + 1000 * 2, usd
    assert unpriced == ["claude-other"], unpriced
    assert result.input_tokens == 3000, result.input_tokens
    claude = Claude(
        Reply("end_turn", model="claude-opus-5"), Reply("end_turn", model="claude-opus-5")
    )
    result = agent.run_agent(claude, job(n04), lambda _: None)
    assert list(result.by_model) == ["claude-opus-5"], result.by_model
    counting = agent.CountingClient(StandIn(events=(dict(LOSS, request={"credits_debited": 4}),)))
    counting.extract(DECIDE, {"id": "x", "text": "t"}, output_schema={"event": {"type": "choice"}})
    assert (counting.calls, counting.credits) == (1, 4.0)
    print("agent outputs: the analyst's queue only, CSV and Markdown safe, cost per model ok")


if __name__ == "__main__":
    check_data()
    check_examples()
    check_sentences()
    check_linking()
    check_spans()
    check_context()
    check_events()
    check_replies()
    check_sie_failure()
    check_outputs()
    check_agent_reasons()
    check_agent_event_check()
    check_agent_scope()
    check_agent_judge()
    check_agent_ticker_rule()
    check_agent_longer_name()
    check_agent_unclear_event()
    check_agent_loop()
    check_agent_claude_errors()
    check_agent_retry()
    check_agent_outputs()
