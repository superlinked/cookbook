"""Check the recipe's filters, candidate walk, ordering rules, data gap list and SIE error rule
offline.

The checks use the committed data/ (PIM seed, stock feed, queries), small hand-written seeds and
hand-written stand-in reranker replies. Nothing here calls SIE or replays a recorded response.
Run it with `uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import copy
import csv
import functools
import io
import json
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest import mock

from inventory import Inventory, Variant
from pim import PIM, Product
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

import make_data
import recipe

RULES = recipe.load_rules()
MODEL = RULES["models"]["rerank"]
CATALOG = PIM()
FEED = Inventory()
QUERIES = {q.query_id: q for q in recipe.load_queries()}
WOMEN_8 = QUERIES["Q-01"].filters


def product(pid: str) -> Product:
    return CATALOG.get_products([pid])[pid]


def check(pid: str, filters: recipe.Filters) -> tuple[list[str], list[str]]:
    return recipe.check_filters(product(pid), FEED.find_variants(pid), filters, RULES)


def made_up(color: str = "Black") -> Product:
    return Product("P-1", "Made-up sneaker", "Brand", color, "")


def variant(color: str, size: str, system: str = "women", in_stock: bool = True) -> Variant:
    return Variant("P-1", color, size, system, in_stock)


class StandIn:
    """Stands in for SIEClient. Each score call takes the next listed reply: an exception is
    raised, a function gets the product IDs sent and returns the reply, anything else is
    returned as the reply."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.sent: list[list[str]] = []

    def score(self, model: str, query: Any, items: list[Any], **_: Any) -> Any:
        assert model == MODEL, model
        ids = [str(item["id"]) for item in items]
        self.sent.append(ids)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply(ids) if callable(reply) else reply


def scored(table: dict[str, float], default: float = 0.1) -> Callable[[list[str]], Any]:
    """A stand-in reply: the listed score per product ID, `default` for the rest."""
    return lambda ids: {"scores": [{"item_id": i, "score": table.get(i, default)} for i in ids]}


def search(query_id: str, *replies: Any, rules: dict[str, Any] = RULES) -> dict[str, Any]:
    query = QUERIES[query_id]
    client = StandIn(*replies)
    endpoint = recipe.SearchEndpoint(cast(SIEClient, client), rules, CATALOG, FEED)
    page = endpoint.search(query.text, query.filters)
    assert not client.replies, f"{len(client.replies)} replies left unused"
    return page


def check_filters() -> None:
    # Stock belongs to the variant: black size 8 sold out, white 8 and black 9 in stock.
    assert check("B071LF8XSF", WOMEN_8) == (["sold out in black, size women 8"], []), check(
        "B071LF8XSF", WOMEN_8
    )
    white_8 = recipe.Filters(("white",), "8", "women")
    assert check("B071LF8XSF", white_8) == ([], []), "the white size 8 is in stock"
    black_9 = recipe.Filters(("black",), "9", "women")
    assert check("B071LF8XSF", black_9) == ([], []), "the black size 9 is in stock"
    # Sold out in every size.
    assert check("B07S387Q2X", WOMEN_8)[0] == ["sold out in black, size women 8"]
    # The half-size sneaker is a size 8.5, not an 8, and passes for an 8.5.
    assert check("B01ESD61JI", WOMEN_8) == (["no variant in size women 8"], [])
    assert check("B01ESD61JI", QUERIES["Q-08"].filters) == ([], [])
    # Near-identical sizes and sizing systems are different sizes; case and spaces aren't.
    black = made_up()
    for size, system, wanted, wanted_system, passes in [
        ("8.5", "women", "8", "women", False),
        ("18", "women", "8", "women", False),
        ("12", "women", "12W", "women", False),
        ("12W", "women", "12w", "women", True),
        ("12 W", "women", "12W", "women", True),
        ("12W", "women", "12 w", "women", True),
        ("8", "toddler", "8", "women", False),
        ("8", "toddler", "8", None, True),
        ("8", " Women ", "8", "women", True),
    ]:
        failed, _ = recipe.check_filters(
            black,
            [variant("Black", size, system)],
            recipe.Filters(("black",), wanted, wanted_system),
            RULES,
        )
        assert (not failed) is passes, (size, system, wanted, wanted_system, failed)
    # Colors are whole words from rules.yaml: a multicolor product counts for each color.
    for color, wanted, passes in [
        ("Black/White/Carbon", "black", True),
        ("Blackberry", "black", False),
        ("Black1", "black", True),
        ("Jet", "black", False),
        ("Jet Black", "black", True),
        ("Midnight", "navy", False),
        ("Midnight Green", "navy", False),
        ("Midnight Blue/White", "navy", True),
        ("Blue Midnight", "navy", False),
        ("Navy Blue", "black", False),
        ("Off-White", "white", True),
    ]:
        assert recipe.color_matches(color, (wanted,), RULES) is passes, (color, wanted)
    # No PIM color: held, never guessed from the title (the Dr. Martens boots say "White").
    assert check("B07K35WGML", QUERIES["Q-04"].filters) == ([], [recipe.NO_PIM_COLOR])
    # Not in the stock feed: held, never shown as in stock.
    assert check("B077ZJXCTH", QUERIES["Q-03"].filters) == ([], [recipe.NOT_IN_FEED])
    # A failed filter is a fact and wins over anything unknown.
    assert check("B077ZJXCTH", QUERIES["Q-04"].filters) == (["color 'Black' is not white"], [])
    unknown_color = made_up("")
    sold_out = [variant("", "8", in_stock=False)]
    assert recipe.check_filters(unknown_color, sold_out, WOMEN_8, RULES)[0] == [
        "sold out in size women 8"
    ]
    # A variant row with no color, for a product whose PIM color matches: held.
    blank = [variant("", "8")]
    assert recipe.check_filters(black, blank, WOMEN_8, RULES) == ([], [recipe.NO_VARIANT_COLOR])
    # The shopper's color has no variant in that size: dropped, even if another color has it.
    assert recipe.check_filters(black, [variant("White", "8")], WOMEN_8, RULES)[0] == [
        "no black variant in size women 8"
    ]
    # No size or stock filter: the feed isn't needed, so a product missing from it passes.
    any_stock = recipe.Filters(("black",), None, None, in_stock=False)
    assert recipe.check_filters(black, [], any_stock, RULES) == ([], [])
    # Any stock still checks the variant color: a black product whose only variant is white.
    assert recipe.check_filters(black, [variant("White", "8")], any_stock, RULES)[0] == [
        "no black variant"
    ]
    # The black 8 is sold out but an in-stock 8 has no color: it may be black, so held.
    mixed = [variant("Black", "8", in_stock=False), variant("", "8")]
    assert recipe.check_filters(black, mixed, WOMEN_8, RULES) == ([], [recipe.NO_VARIANT_COLOR])
    # The colorless 8 is sold out too: sold out whatever its color.
    mixed = [variant("Black", "8", in_stock=False), variant("", "8", in_stock=False)]
    assert recipe.check_filters(black, mixed, WOMEN_8, RULES)[0] == [
        "sold out in black, size women 8"
    ]
    print("filters: per-variant stock, sizes, sizing systems, colors, held vs dropped ok")


def check_walk() -> None:
    # Q-08: the first 30 keyword hits fail the filters; hit 31 is the one product that passes.
    query = QUERIES["Q-08"]
    endpoint = recipe.SearchEndpoint(cast(SIEClient, StandIn()), RULES, CATALOG, FEED)
    found = endpoint.collect(query.text, query.filters)
    assert [(rank, p.product_id) for rank, p in found.eligible] == [(31, "B01ESD61JI")]
    assert found.ran_out, "Q-08 reads every hit"
    assert all(entry["keyword_hit"] < 31 for entry in found.dropped[:29])
    # The walk stops once rerank.candidates products pass, and never reads past max_hits.
    found = endpoint.collect(QUERIES["Q-02"].text, QUERIES["Q-02"].filters)
    assert len(found.eligible) == RULES["rerank"]["candidates"]
    assert not found.ran_out
    capped = copy.deepcopy(RULES)
    capped["keyword_search"]["max_hits"] = 10
    page = search("Q-08", rules=capped)
    assert page["status"] == "no_results", page["hits_read"]
    assert page["hits_read"] == 10, page["hits_read"]
    assert "in the first 10 keyword hits (max_hits)" in page["reason"], page["reason"]
    # Q-08 read every hit the engine has, each once, across three pages.
    q08 = endpoint.collect(query.text, query.filters)
    hits = [h.product_id for h in endpoint.engine.search(query.text, 0, 1000)]
    assert len(set(hits)) == len(hits) == q08.hits_read == 58, (len(hits), q08.hits_read)
    print("walk: filters applied while walking, hit 31 found, max_hits respected ok")


def check_ordering() -> None:
    # Two products pass Q-01; a held sock is scored too. Matches come first, then related.
    page = search("Q-01", scored({"B07M9SFR3T": 0.8, "B06XRSQCLL": 0.3}))
    assert page["status"] == "ranked", page
    assert [(r["id"], r["fit"]) for r in page["results"]] == [
        ("B07M9SFR3T", "match"),
        ("B06XRSQCLL", "related"),
    ], page["results"]
    assert [r["id"] for r in page["keyword_order"]] == ["B06XRSQCLL", "B07M9SFR3T"]
    # Even the best is below min_score: no good match, everything shown as related.
    page = search("Q-05", scored({}, default=0.25))
    assert page["status"] == "no_good_match", page["status"]
    assert {r["fit"] for r in page["results"]} == {"related"}
    # A held product the reranker loves is never shown; it goes on the gap list.
    page = search("Q-03", scored({"B077ZJXCTH": 0.99}, default=0.6))
    assert "B077ZJXCTH" not in [r["id"] for r in page["results"]]
    assert page["held"][0]["would_rank"] == 1, page["held"]
    assert page["gaps"] == ["B077ZJXCTH"], page["held"]
    # Held and strong, but it would rank 6th: not listed. Held and weak: not listed.
    top5 = {
        pid: 0.95 for pid in ("B07B9DWXQ9", "B004F7UASG", "B077ZGZZ3G", "B0002TOZ1O", "B0002TOZ28")
    }
    page = search("Q-03", scored({**top5, "B077ZJXCTH": 0.9}, default=0.6))
    assert page["held"][0]["would_rank"] == 6, page["held"]
    assert page["gaps"] == [], page["held"]
    page = search("Q-03", scored({"B077ZJXCTH": 0.49}, default=0.3))
    assert page["gaps"] == [], page["held"]
    # A score for a product we didn't send (a dropped one) is ignored.
    page = search("Q-01", scored({"B071LF8XSF": 0.99, "B07M9SFR3T": 0.7}))
    assert "B071LF8XSF" not in [r["id"] for r in page["results"]]
    # Nothing passes, some are held: no_results says so instead of "filters removed all".
    page = search("Q-07", scored({}))
    assert page["status"] == "no_results", page["status"]
    assert "61 failed a filter and 1 are held for missing data" in page["reason"], page["reason"]
    assert set(recipe.STATUSES) == {"ranked", "no_good_match", "no_results", "not_reranked"}
    # held_within is inclusive, and a score equal to min_score counts as a match.
    floor, within = RULES["rerank"]["min_score"], RULES["data_gaps"]["held_within"]
    held = [
        {"id": "AT", "score": floor, "would_rank": within},
        {"id": "PAST", "score": 0.99, "would_rank": within + 1},
        {"id": "LOW", "score": floor - 0.01, "would_rank": 1},
    ]
    assert recipe.gap_ids({"held": held}, RULES) == ["AT"], recipe.gap_ids({"held": held}, RULES)
    # A search that wasn't reranked has no scores; a min_score of 0 doesn't list its held ones.
    zero = copy.deepcopy(RULES)
    zero["rerank"]["min_score"] = 0.0
    assert recipe.gap_ids({"held": [{"id": "X", "reasons": ["r"]}]}, zero) == []
    print("ordering: matches then related, no_good_match, held never shown, gap rule ok")


def check_replies() -> None:
    # A reply that can't order every product we sent is a failed call: keyword order, filters
    # applied, and the reason says why. Never a partial reranking.
    def drop_one(ids: list[str]) -> Any:
        return {"scores": [{"item_id": i, "score": 0.9} for i in ids[1:]]}

    bad: list[tuple[Any, str]] = [
        (drop_one, "the reply has no score for 1 of 3 products"),
        ({"error": "x"}, "the reply has no `scores`"),
        ("not a dict", "the reply has no `scores`"),
        (scored({"B07M9SFR3T": float("nan")}), "score for B07M9SFR3T is not a number"),
        (scored({"B07M9SFR3T": cast(float, "high")}), "score for B07M9SFR3T is not a number"),
        (scored({"B07M9SFR3T": float("inf")}), "score for B07M9SFR3T is not a number"),
    ]
    for reply, why in bad:
        page = search("Q-01", reply)
        assert page["status"] == "not_reranked", (why, page["status"])
        assert why in page["reason"], page["reason"]
        assert [r["id"] for r in page["results"]] == ["B06XRSQCLL", "B07M9SFR3T"]
        assert "score" not in page["results"][0]
        assert page["gaps"] == []
    # A numeric string is still a number.
    page = search("Q-01", scored({"B07M9SFR3T": cast(float, "0.9")}))
    assert page["status"] == "ranked"
    assert page["results"][0]["id"] == "B07M9SFR3T"
    print("replies: missing, malformed or non-numeric scores fall back to keyword order ok")


def run(*replies: Any, folder: Path | None = None) -> tuple[Any, Path]:
    """Run the recipe on Q-03 and Q-01 with stand-in replies; the outputs go to `folder`/out."""
    folder = folder or Path(tempfile.mkdtemp())
    rows = json.loads(recipe.QUERIES_PATH.read_text(encoding="utf-8"))["queries"]
    picked = [row for row in rows if row["id"] in ("Q-03", "Q-01")]
    (folder / "queries.json").write_text(json.dumps({"queries": picked}), encoding="utf-8")
    client = StandIn(*replies)
    output = folder / "out"
    with contextlib.redirect_stdout(io.StringIO()):  # the run's "1 of 2 failed" line
        result = recipe.run(cast(SIEClient, client), RULES, output, folder / "queries.json")
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
    # Q-03 lists the held sock; Q-01 holds the same sock again, and it scores high there too.
    (pages, gaps), output = run(
        scored({"B077ZJXCTH": 0.95}, default=0.7), scored({"B077ZJXCTH": 0.9, "B07M9SFR3T": 0.6})
    )
    assert [qid for qid, _ in pages] == ["Q-01", "Q-03"]
    # One line per product, however many searches it was held from.
    assert len(gaps) == 1, gaps
    assert gaps[0]["queries"] == ["Q-01", "Q-03"], gaps
    assert gaps[0]["owner"] == "inventory owner", gaps
    assert gaps[0]["best_score"] == 0.95, gaps
    assert sorted(p.name for p in (output / "pages").iterdir()) == ["Q-01.md", "Q-03.md"]
    page = (output / "pages" / "Q-01.md").read_text(encoding="utf-8")
    assert "Keyword order, same filters" in page, page
    assert "On the data gap list." in page, page
    with (output / "data_gaps.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert tuple(rows[0]) == recipe.GAP_FIELDS, rows
    assert rows[0]["queries"] == "Q-01 Q-03", rows
    saved = json.loads((output / "results.json").read_text(encoding="utf-8"))
    assert [s["id"] for s in saved["searches"]] == ["Q-01", "Q-03"]
    # A cell a spreadsheet would run as a formula is defused.
    assert recipe.csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert recipe.csv_safe("-15 C") == "'-15 C"
    assert recipe.csv_safe("Black") == "Black"
    # Every reason a product can be held has an owner in rules.yaml.
    assert set(recipe.HELD_REASONS) <= set(RULES["data_gaps"]["owners"])
    owners = RULES["data_gaps"]["owners"]
    assert owners[recipe.NO_PIM_COLOR] == "catalog owner", owners
    assert owners[recipe.NOT_IN_FEED] == owners[recipe.NO_VARIANT_COLOR] == "inventory owner"

    # Held for different reasons in two searches: the line lists both, and both owners.
    def held_page(reason: str, score: float) -> dict[str, Any]:
        entry = {"id": "P-1", "title": "T", "reasons": [reason], "score": score, "would_rank": 2}
        return {"held": [entry], "gaps": ["P-1"]}

    pages = [
        ("Q-A", held_page(recipe.NO_PIM_COLOR, 0.8)),
        ("Q-B", held_page(recipe.NOT_IN_FEED, 0.9)),
    ]
    (line,) = recipe.data_gaps(pages, RULES)
    assert line["missing"] == f"{recipe.NO_PIM_COLOR}; {recipe.NOT_IN_FEED}", line
    assert line["owner"] == "catalog owner; inventory owner", line
    assert (line["queries"], line["best_score"]) == (["Q-A", "Q-B"], 0.9), line
    print("run: pages, results.json, summary and a deduplicated data gap list written ok")


def check_sie_failure() -> None:
    for error in TRANSIENT:
        assert not recipe.is_fatal(error), f"{type(error).__name__} should be transient"
    for error in FATAL:
        assert recipe.is_fatal(error), f"{type(error).__name__} should stop the run"

    # A transient failure: that search falls back to keyword order with the error, cut short;
    # the other search is reranked and the run writes its outputs.
    long = "connection refused " + "x" * 300
    for error in [*TRANSIENT, SIEConnectionError(long)]:
        (pages, _), output = run(error, scored({}, default=0.7))
        first = pages[0][1]
        assert first["status"] == "not_reranked", first["status"]
        prefix = "Not reranked, the SIE call failed: "
        assert first["reason"].startswith(prefix), first["reason"]
        assert pages[1][1]["status"] == "ranked"
        assert (output / "summary.md").exists()
    why = (
        first["reason"]
        .removeprefix(prefix)
        .removesuffix(". Results are in keyword order, filters applied.")
    )
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
    # A refusal on a later call stops the run at once, with SIE's full message.
    message = stops(scored({}), InsufficientCreditsError("no credits " + "y" * 300))
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
    query = QUERIES["Q-01"]
    message = stopped(
        lambda: recipe.SearchEndpoint(
            cast(SIEClient, StandIn(RequestError("bad key", status_code=401))),
            RULES,
            CATALOG,
            FEED,
        ).search(query.text, query.filters)
    )
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


def check_queries_and_args() -> None:
    folder = Path(tempfile.mkdtemp())
    path = folder / "queries.json"

    def load(query_id: str, color: list[str]) -> list[recipe.SampleQuery]:
        entry = {"id": query_id, "query": "q", "filters": {"color": color}}
        path.write_text(json.dumps({"queries": [entry]}), encoding="utf-8")
        return recipe.load_queries(path)

    # A query ID becomes a file name under pages/: anything but letters, digits, _ and - stops.
    for bad in ("../escape", "Q 1", "Q/1", ""):
        caught = raised(functools.partial(load, bad, []))
        assert type(caught) is SystemExit, (bad, caught)
        assert "must match" in str(caught), (bad, caught)
    # Color facets are lowercased, as the command line does.
    assert load("Q-1_a", ["Black", "NAVY"])[0].filters.color == ("black", "navy")
    # --size-system without --size is refused before any SIE call.
    argv = ["recipe.py", "shoes", "--size-system", "women"]
    with (
        mock.patch.object(sys, "argv", argv),
        mock.patch.object(recipe, "make_client", side_effect=AssertionError("called SIE")),
        contextlib.redirect_stderr(io.StringIO()) as err,
    ):
        caught = raised(recipe.main)
    assert isinstance(caught, SystemExit), caught
    assert caught.code == 2, caught
    assert "--size-system needs --size" in err.getvalue(), err.getvalue()
    print("queries and args: IDs checked, colors lowercased, --size-system needs --size ok")


def check_data() -> None:
    # The feed and queries are what make_data.py writes from the committed PIM seed.
    pim_rows = make_data.read_pim(recipe.ROOT / "data" / "pim.csv")
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=make_data.FEED_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(make_data.feed_rows(pim_rows))
    feed_text = (recipe.ROOT / "data" / "inventory.csv").read_text(encoding="utf-8")
    assert buffer.getvalue() == feed_text, "inventory.csv is not what make_data.py writes"
    queries_text = (recipe.ROOT / "data" / "queries.json").read_text(encoding="utf-8")
    assert make_data.queries_json() == queries_text, "queries.json is not what make_data.py writes"
    # Every PIM product is in the feed except the one planted missing; 8 sample queries.
    in_feed = {row["product_id"] for row in csv.DictReader(io.StringIO(feed_text))}
    assert set(pim_rows) - in_feed == {"B077ZJXCTH"}, set(pim_rows) - in_feed
    assert sorted(QUERIES) == [f"Q-0{n}" for n in range(1, 9)], sorted(QUERIES)
    assert [(v.size, v.size_system) for v in FEED.find_variants("B01ESD61JI")] == [("8.5", "women")]
    print("data: feed and queries match make_data.py, one product missing from the feed ok")


if __name__ == "__main__":
    check_filters()
    check_walk()
    check_ordering()
    check_replies()
    check_run_outputs()
    check_sie_failure()
    check_queries_and_args()
    check_data()
