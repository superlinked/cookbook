"""Check the recipe's lookups, linking rules, event limits and SIE error rule offline.

The checks use the committed data/, hand-written articles and hand-written stand-in replies
from both models. Nothing here calls SIE or replays a recorded response.
Run it with `uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import csv
import functools
import io
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

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
    # The planted shapes hold in the committed data.
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


def check_planted() -> None:
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
    print("planted: the 10 committed articles give the README's statuses ok")


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


if __name__ == "__main__":
    check_data()
    check_planted()
    check_sentences()
    check_linking()
    check_spans()
    check_context()
    check_events()
    check_replies()
    check_sie_failure()
    check_outputs()
