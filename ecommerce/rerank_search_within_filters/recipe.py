"""Rerank a store's search results within the shopper's size, color and stock filters.

Use case:  The reranking step inside a store's search endpoint: one shopper's query and filters
           go in, the page of results that shopper sees comes out, in order.
Who:       The search relevance engineer and the merchandiser, who decide whether to switch the
           reranker on; the catalog and inventory owners, who get the data gap list.
Problem:   Keyword search puts the products that repeat the query's words first, so "cropped
           black shirt" fills the page with black T-shirts. The shopper's size, color and stock
           filters are facts and must hold exactly; a model helps by ordering what passed them.
Input:     The query and filters (data/queries.json for the sample run, or the command line),
           the PIM (pim.py, seed data/pim.csv), the variant stock feed (inventory.py, seed
           data/inventory.csv) and the keyword engine (keyword_search.py, BM25 here).
SIE pipeline (one model):
           (Python)  walk down the keyword hits, applying the filters per variant, until 20
                     products pass or the hits run out; a product with no PIM color or missing
                     from the stock feed is held, never guessed
           score     Qwen/Qwen3-Reranker-4B   called once per search: scores the products that
                                              passed and the held ones against the query
           (Python)  order what passed by score; no good match below min_score; a held product
                     that would make the first five goes on the data gap list
Output:    One search prints the page. With no query, the sample run writes runs/<timestamp>/:
           a before-and-after page per query (pages/), results.json, summary.md and
           data_gaps.csv, one line per product whose missing data kept it off a page.
Why SIE:   The reranker runs on SIE Cloud; the filters and the keyword engine stay in plain
           Python you can read below, and no generative model decides what the shopper sees.

    uv run recipe.py                                              # the sample queries
    uv run recipe.py "black tennis shoes" --color black --size 8  # one search
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import yaml
from dotenv import load_dotenv
from inventory import Inventory, Variant
from keyword_search import KeywordSearch
from pim import PIM, Product
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
QUERIES_PATH = ROOT / "data" / "queries.json"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
# How much of an SIE error message goes into a page's reason; a stop message keeps all of it.
ERROR_CHARS = 200

# What the checks found for a search. None of them is a decision about the store: the shopper
# sees the page, and the relevance engineer and merchandiser decide whether to switch the
# reranker on.
STATUSES = ("ranked", "no_good_match", "no_results", "not_reranked")

# Why a product is held: what the systems couldn't tell us. Each has an owner in rules.yaml.
NO_PIM_COLOR = "color not in the PIM"
NOT_IN_FEED = "not in the stock feed"
NO_VARIANT_COLOR = "variant color not in the stock feed"
HELD_REASONS = (NO_PIM_COLOR, NOT_IN_FEED, NO_VARIANT_COLOR)

# A cell starting with one of these is a formula to a spreadsheet; csv_safe defuses it.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
GAP_FIELDS = ("product_id", "title", "missing", "owner", "queries", "best_score", "best_rank")


@dataclass(frozen=True)
class Filters:
    """The facets the shopper picked. An empty color list or a None size means no filter."""

    color: tuple[str, ...] = ()
    size: str | None = None
    size_system: str | None = None
    in_stock: bool = True

    def size_label(self) -> str:
        """ "women 8", or just "8" when no sizing system was picked; "" with no size filter."""
        return " ".join(part for part in (self.size_system, self.size) if part) if self.size else ""

    def describe(self) -> str:
        parts = (
            "/".join(self.color),
            f"size {self.size_label()}" if self.size else "",
            "in stock" if self.in_stock else "",
        )
        return ", ".join(part for part in parts if part) or "none"


@dataclass(frozen=True)
class SampleQuery:
    query_id: str
    text: str
    filters: Filters


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def load_queries(path: Path = QUERIES_PATH) -> list[SampleQuery]:
    queries = []
    for entry in json.loads(path.read_text(encoding="utf-8"))["queries"]:
        facets = entry["filters"]
        filters = Filters(
            color=tuple(facets.get("color") or ()),
            size=facets.get("size"),
            size_system=facets.get("size_system"),
            in_stock=bool(facets.get("in_stock", True)),
        )
        queries.append(SampleQuery(entry["id"], entry["query"], filters))
    return queries


def make_client() -> SIEClient:
    """Build an SIE client from the API key and SIE_BASE_URL in the environment or `.env`."""
    load_dotenv()
    api_key = os.environ.get("SIE_API_KEY", "").strip()
    base_url = os.environ.get("SIE_BASE_URL", "").strip()
    if not api_key and not base_url:
        raise RuntimeError("Set SIE_API_KEY, or SIE_BASE_URL for your own SIE, in .env")
    base_url = base_url or "https://api.superlinked.com"
    return SIEClient(api_key=api_key or None, base_url=base_url, timeout_s=WAIT_S)


# --- SIE calls and their failures ------------------------------------------------------------


class SIEStopped(RuntimeError):
    """SIE refused the call for a reason a retry won't fix, or no call succeeded; the run
    stops without outputs."""


def is_fatal(error: SIEError) -> bool:
    """A refusal that fails every call the same way: a 4xx (bad key 401, forbidden 403, model
    not found 404, no credits or spend limit 402, inactive account) or a model the server
    marked as permanently unable to load. A RequestError with no HTTP status or a status below
    400 is a reply the SDK couldn't parse, and is fatal too. A rate limit, an over-long input, a
    timeout, a lost connection, a model still loading and a 5xx are transient or per-call:
    those searches fall back to keyword order."""
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
    """The error for a page's reason or the summary line, its message cut short."""
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


def every_call_failed_advice(error: SIEError) -> str:
    """What to check when every SIE call failed, judged by the first failure.

    A lost connection points at SIE_BASE_URL and the network. Any other transient error (a
    timeout, a model still loading, a 5xx, a rate limit) more likely means the model is still
    starting or SIE is busy, so the advice is to wait and run again.
    """
    if isinstance(error, SIEConnectionError):
        return "Check SIE_BASE_URL and your network."
    return "The model may still be starting or SIE is busy; run again in a few minutes."


def stop_on_refusal[T](work: Callable[[], T]) -> T:
    """Run the work; turn SIEStopped into a clean exit. Any other error keeps its traceback."""
    try:
        return work()
    except SIEStopped as error:
        raise SystemExit(f"Stopped: {error}") from None


@dataclass
class CallLog:
    """Every SIE call the run made, and the ones that failed (with the model called)."""

    made: int = 0
    failures: list[tuple[str, SIEError]] = field(default_factory=list)

    def stop_if_all_failed(self) -> None:
        """No call succeeded, so nothing was reranked: stop instead of writing pages."""
        if not self.failures or len(self.failures) < self.made:
            return
        first = self.failures[0][1]
        models = ", ".join(dict.fromkeys(model for model, _ in self.failures))
        raise SIEStopped(
            f"Every SIE call failed ({self.made} of {self.made} to {models}), so the run stops: "
            f"{describe(first)}: {first}. {every_call_failed_advice(first)} "
            "No outputs were written."
        ) from first


def guarded[T](calls: CallLog, model: str, work: Callable[[], T]) -> tuple[T | None, str]:
    """Run one SIE call; return its result, or None and the reason it failed.

    A call that fails for a reason that may pass (a lost connection, a timeout, a server error,
    a rate limit, a reply without a score for every product we sent) is logged and that search
    falls back to keyword order; the run continues. A refusal every call would repeat (see
    `is_fatal`) stops the run. An error that isn't an SIE error is a bug in this code and is
    raised as is.
    """
    calls.made += 1
    try:
        return work(), ""
    except SIEError as error:
        if is_fatal(error):
            raise SIEStopped(stop_message(error, model)) from error
        calls.failures.append((model, error))
        return None, f"the SIE call failed: {short(error)}"


# --- Filters ----------------------------------------------------------------------------------


def norm(value: str) -> str:
    """Sizes and sizing systems compared case- and space-insensitively: "12w" is "12W"."""
    return " ".join(value.split()).casefold()


def color_matches(color: str, wanted: tuple[str, ...], rules: dict[str, Any]) -> bool:
    """True when the color names one of the wanted facets, as a whole word."""
    words = rules["filters"]["color_words"]
    found = set(re.findall(r"[a-z]+", color.lower()))
    return any(word in found for name in wanted for word in words.get(name, [name]))


def check_filters(
    product: Product, variants: list[Variant], filters: Filters, rules: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """(failed, unknown): the filters the product fails, and what it can't be checked on.

    Color comes from the PIM. Size, sizing system and stock come from the variant rows, and
    only from rows in the shopper's color: stock belongs to the variant, not the product. A
    failed filter is a fact, so it wins over anything unknown.
    """
    failed: list[str] = []
    unknown: list[str] = []
    wanted = " or ".join(filters.color)
    color_known = bool(product.color.strip())
    if filters.color:
        if not color_known:
            unknown.append(NO_PIM_COLOR)
        elif not color_matches(product.color, filters.color, rules):
            return [f"color {product.color!r} is not {wanted}"], []
    if not (filters.size or filters.in_stock):
        return failed, unknown
    if not variants:
        return failed, [*unknown, NOT_IN_FEED]
    rows = variants
    size = filters.size_label()
    if filters.size:
        rows = [
            v
            for v in rows
            if norm(v.size) == norm(filters.size)
            and (not filters.size_system or norm(v.size_system) == norm(filters.size_system))
        ]
        if not rows:
            return [f"no variant in size {size}"], []
    if filters.color and color_known:
        in_color = [
            v for v in rows if v.color.strip() and color_matches(v.color, filters.color, rules)
        ]
        if not in_color:
            if any(not v.color.strip() for v in rows):
                return failed, [*unknown, NO_VARIANT_COLOR]
            where = f" in size {size}" if filters.size else ""
            return [f"no {wanted} variant{where}"], []
        rows = in_color
    if filters.in_stock and not any(v.in_stock for v in rows):
        what = [wanted] if filters.color and color_known else []
        what += [f"size {size}"] if filters.size else []
        return ["sold out" + (f" in {', '.join(what)}" if what else "")], []
    return failed, unknown


# --- Rerank -----------------------------------------------------------------------------------


def rerank_text(product: Product) -> str:
    """What the reranker reads: title, brand, color and the start of the listing text."""
    color = product.color or "not listed"
    text = f"{product.title}\nBrand: {product.brand or 'not listed'}. Color: {color}."
    return f"{text}\n{product.listing_text}" if product.listing_text else text


def parse_scores(reply: Any, sent: list[str]) -> dict[str, float]:
    """The score per product ID. A reply with no `scores`, or without a numeric score for
    every product we sent, raises SIEError, so it takes the transient path in `guarded`:
    a search can't be ordered on scores it doesn't have."""
    entries = reply.get("scores") if isinstance(reply, dict) else None
    if not isinstance(entries, list):
        raise SIEError("the reply has no `scores`")
    scores: dict[str, float] = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("item_id") not in sent:
            continue
        try:
            value = float(entry.get("score"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            value = math.nan
        if not math.isfinite(value):
            raise SIEError(f"the reply's score for {entry['item_id']} is not a number")
        scores[str(entry["item_id"])] = value
    missing = [pid for pid in sent if pid not in scores]
    if missing:
        raise SIEError(f"the reply has no score for {len(missing)} of {len(sent)} products")
    return scores


# --- The search endpoint ----------------------------------------------------------------------


@dataclass
class Candidates:
    """What the walk down the keyword hits found, in keyword order."""

    eligible: list[tuple[int, Product]] = field(default_factory=list)
    held: list[tuple[int, Product, list[str]]] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    hits_read: int = 0
    ran_out: bool = False


class SearchEndpoint:
    """The reranking step of a search service. `search(query, filters)` is the function the
    service would call on every request; everything it needs is built once here."""

    def __init__(
        self,
        client: SIEClient,
        rules: dict[str, Any],
        pim: PIM,
        inventory: Inventory,
        calls: CallLog | None = None,
    ) -> None:
        self.client = client
        self.rules = rules
        self.pim = pim
        self.inventory = inventory
        self.calls = calls or CallLog()
        settings = rules["keyword_search"]
        self.engine = KeywordSearch(pim.all_products(), settings["k1"], settings["b"])

    def collect(self, query: str, filters: Filters) -> Candidates:
        """Walk down the keyword hits page by page, applying the filters as we go, until
        `rerank.candidates` products pass, the hits run out, or `max_hits` is reached."""
        settings = self.rules["keyword_search"]
        want = self.rules["rerank"]["candidates"]
        found = Candidates()
        seen: set[str] = set()
        while len(found.eligible) < want and found.hits_read < settings["max_hits"]:
            limit = min(settings["page_size"], settings["max_hits"] - found.hits_read)
            page = self.engine.search(query, found.hits_read, limit)
            if not page:
                found.ran_out = True
                break
            products = self.pim.get_products([hit.product_id for hit in page])
            for hit in page:
                found.hits_read = hit.rank
                if hit.product_id in seen:
                    continue
                seen.add(hit.product_id)
                product = products.get(hit.product_id)
                if product is None:
                    found.dropped.append(
                        {
                            "id": hit.product_id,
                            "keyword_hit": hit.rank,
                            "reasons": ["not in the PIM"],
                        }
                    )
                    continue
                failed, unknown = check_filters(
                    product, self.inventory.find_variants(product.product_id), filters, self.rules
                )
                if failed:
                    found.dropped.append(
                        {"id": product.product_id, "keyword_hit": hit.rank, "reasons": failed}
                    )
                elif unknown:
                    found.held.append((hit.rank, product, unknown))
                else:
                    found.eligible.append((hit.rank, product))
                    if len(found.eligible) == want:
                        break
            if len(page) < limit:
                found.ran_out = True
                break
        return found

    def search(self, query: str, filters: Filters) -> dict[str, Any]:
        """The page one shopper sees for this query and these filters."""
        found = self.collect(query, filters)
        to_score = [product for _, product in found.eligible] + [p for _, p, _ in found.held]
        scores: dict[str, float] | None = None
        failed = ""
        if to_score:
            model = self.rules["models"]["rerank"]
            sent = [product.product_id for product in to_score]

            def call() -> dict[str, float]:
                reply: Any = self.client.score(
                    model,
                    Item(id="query", text=query),
                    [
                        Item(id=product.product_id, text=rerank_text(product))
                        for product in to_score
                    ],
                    instruction=self.rules["rerank"]["instruction"],
                    provision_timeout_s=WAIT_S,
                )
                return parse_scores(reply, sent)

            scores, failed = guarded(self.calls, model, call)
        return build_page(query, filters, found, scores, failed, self.rules)


def build_page(
    query: str,
    filters: Filters,
    found: Candidates,
    scores: dict[str, float] | None,
    failed: str,
    rules: dict[str, Any],
) -> dict[str, Any]:
    """Order what passed, mark matches and related items, place the held products, and pick
    the status. Held products are never in `results`."""
    min_score = rules["rerank"]["min_score"]
    shown = rules["rerank"]["max_results"]
    keyword: list[dict[str, Any]] = [
        {"id": p.product_id, "title": p.title, "keyword_hit": rank} for rank, p in found.eligible
    ]
    ordered = keyword
    if scores is not None:
        known = scores
        ordered = [
            {
                **row,
                "score": round(known[row["id"]], 4),
                "fit": "match" if known[row["id"]] >= min_score else "related",
            }
            for row in sorted(keyword, key=lambda row: (-known[row["id"]], row["keyword_hit"]))
        ]
    held = []
    for rank, product, reasons in found.held:
        entry: dict[str, Any] = {
            "id": product.product_id,
            "title": product.title,
            "keyword_hit": rank,
            "reasons": reasons,
        }
        if scores is not None:
            score = scores[product.product_id]
            entry["score"] = round(score, 4)
            # Where it would rank among the results if its data were complete.
            entry["would_rank"] = 1 + sum(scores[row["id"]] > score for row in keyword)
        held.append(entry)
    held.sort(key=lambda entry: (-entry.get("score", 0.0), entry["keyword_hit"]))
    page: dict[str, Any] = {
        "query": query,
        "filters": {
            "color": list(filters.color),
            "size": filters.size,
            "size_system": filters.size_system,
            "in_stock": filters.in_stock,
        },
        "results": ordered[:shown],
        "keyword_order": keyword[:shown],
        "eligible": len(keyword),
        "held": held,
        "dropped": found.dropped,
        "hits_read": found.hits_read,
        "hits_ran_out": found.ran_out,
    }
    page["status"], page["reason"] = decide(page, found, failed, rules)
    page["gaps"] = gap_ids(page, rules)
    return page


def decide(
    page: dict[str, Any], found: Candidates, failed: str, rules: dict[str, Any]
) -> tuple[str, str]:
    """The search's status and a sentence saying why."""
    min_score = rules["rerank"]["min_score"]
    if not page["results"]:
        where = (
            f"in all {found.hits_read} keyword hits"
            if found.ran_out
            else f"in the first {found.hits_read} keyword hits (max_hits)"
        )
        if not found.hits_read:
            return "no_results", "No results: the keyword search found nothing for this query."
        return (
            "no_results",
            f"No results: no product passed the filters {where}; {len(found.dropped)} failed a "
            f"filter and {len(found.held)} are held for missing data.",
        )
    if failed:
        return (
            "not_reranked",
            f"Not reranked, {failed}. Results are in keyword order, filters applied.",
        )
    top = page["results"][0]
    if top["score"] < min_score:
        return (
            "no_good_match",
            f"No good match: the best result, {top['id']}, scored {top['score']:.2f}, below "
            f"{min_score}. Show the results as related items, not as matches.",
        )
    strong = sum(row["fit"] == "match" for row in page["results"])
    return "ranked", f"{strong} of {len(page['results'])} results at or above {min_score}."


def gap_ids(page: dict[str, Any], rules: dict[str, Any]) -> list[str]:
    """Held products that score as a match and would rank within `data_gaps.held_within`."""
    min_score = rules["rerank"]["min_score"]
    within = rules["data_gaps"]["held_within"]
    return [
        entry["id"]
        for entry in page["held"]
        if entry.get("score", 0.0) >= min_score and entry["would_rank"] <= within
    ]


def data_gaps(
    pages: list[tuple[str, dict[str, Any]]], rules: dict[str, Any]
) -> list[dict[str, Any]]:
    """One line per product, however many searches it was held from, for its owner."""
    owners: dict[str, str] = rules["data_gaps"]["owners"]
    lines: dict[str, dict[str, Any]] = {}
    for query_id, page in pages:
        for entry in page["held"]:
            if entry["id"] not in page["gaps"]:
                continue
            line = lines.setdefault(
                entry["id"],
                {
                    "product_id": entry["id"],
                    "title": entry["title"],
                    "missing": "; ".join(entry["reasons"]),
                    "owner": "; ".join(
                        dict.fromkeys(owners.get(r, "unassigned") for r in entry["reasons"])
                    ),
                    "queries": [],
                    "best_score": entry["score"],
                    "best_rank": entry["would_rank"],
                },
            )
            line["queries"].append(query_id)
            line["best_score"] = max(line["best_score"], entry["score"])
            line["best_rank"] = min(line["best_rank"], entry["would_rank"])
    return sorted(lines.values(), key=lambda line: line["product_id"])


# --- Run --------------------------------------------------------------------------------------


def run(
    client: SIEClient,
    rules: dict[str, Any],
    output_dir: Path | None,
    queries_path: Path = QUERIES_PATH,
    pim: PIM | None = None,
    inventory: Inventory | None = None,
) -> tuple[list[tuple[str, dict[str, Any]]], list[dict[str, Any]]]:
    """Run every sample query through `search` and build the data gap list.

    Nothing is written until every search is done, so a run that stops on an SIE refusal or on
    every call failing leaves no partial pages behind.
    """
    queries = load_queries(queries_path)
    if not queries:
        raise SystemExit(f"No queries in {queries_path}, so there is nothing to search.")
    calls = CallLog()
    endpoint = SearchEndpoint(client, rules, pim or PIM(), inventory or Inventory(), calls)
    pages = [(q.query_id, endpoint.search(q.text, q.filters)) for q in queries]
    calls.stop_if_all_failed()
    if calls.failures:
        print(
            f"{len(calls.failures)} of {calls.made} SIE calls failed; those searches are in "
            f"keyword order. First: {short(calls.failures[0][1])}"
        )
    gaps = data_gaps(pages, rules)
    if output_dir is not None:
        write_outputs(output_dir, pages, gaps)
    return pages, gaps


def search_once(
    client: SIEClient, rules: dict[str, Any], query: str, filters: Filters
) -> dict[str, Any]:
    """One search from the command line, under the same SIE error rule as the sample run."""
    calls = CallLog()
    page = SearchEndpoint(client, rules, PIM(), Inventory(), calls).search(query, filters)
    calls.stop_if_all_failed()
    return page


def csv_safe(value: Any) -> str:
    """A leading ' on a cell a spreadsheet would run as a formula; anything else as written."""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def md_cell(value: Any) -> str:
    """Text for a Markdown table cell: pipes escaped, line breaks flattened."""
    return " ".join(str(value).split()).replace("|", "\\|")


def render_page(query_id: str, page: dict[str, Any], dropped_shown: int | None = None) -> str:
    """The before-and-after page for one search: the reranked page next to keyword order,
    then the held products and the dropped ones (the first `dropped_shown`, None for all)."""
    filters = Filters(
        tuple(page["filters"]["color"]),
        page["filters"]["size"],
        page["filters"]["size_system"],
        page["filters"]["in_stock"],
    )
    lines = [
        f"# {query_id}: {page['status']}",
        "",
        f"Query: {page['query']!r}  ",
        f"Filters: {filters.describe()}  ",
        f"Keyword hits read: {page['hits_read']}; {page['eligible']} passed the filters.",
        "",
        page["reason"],
        "",
    ]
    if page["results"]:
        lines += [
            "| # | Reranked (what the shopper sees) | Score | Shown as "
            "| Keyword order, same filters |",
            "| --- | --- | --- | --- | --- |",
        ]
        for n, (new, old) in enumerate(zip(page["results"], page["keyword_order"], strict=True), 1):
            score = new.get("score", "")
            lines.append(
                f"| {n} | {md_cell(new['title'])} ({new['id']}, hit {new['keyword_hit']}) | "
                f"{score} | {new.get('fit', 'keyword order')} | "
                f"{md_cell(old['title'])} ({old['id']}, hit {old['keyword_hit']}) |"
            )
        lines.append("")
    for entry in page["held"]:
        scored = (
            f", score {entry['score']}, would rank {entry['would_rank']}"
            if "score" in entry
            else ""
        )
        flag = " On the data gap list." if entry["id"] in page["gaps"] else ""
        lines.append(
            f"- Held {entry['id']} (hit {entry['keyword_hit']}{scored}): "
            f"{'; '.join(entry['reasons'])}. Never shown.{flag}"
        )
    for entry in page["dropped"][:dropped_shown]:
        lines.append(
            f"- Dropped {entry['id']} (hit {entry['keyword_hit']}): {'; '.join(entry['reasons'])}"
        )
    hidden = len(page["dropped"]) - len(page["dropped"][:dropped_shown])
    if hidden:
        lines.append(f"- ... and {hidden} more dropped by the filters.")
    return "\n".join(lines) + "\n"


def render_summary(pages: list[tuple[str, dict[str, Any]]], gaps: list[dict[str, Any]]) -> str:
    lines = [
        "# Sample searches, before and after",
        "",
        "One page per query in pages/. The data gap list is data_gaps.csv.",
        "",
        "| Query | Status | Text | Filters | First result |",
        "| --- | --- | --- | --- | --- |",
    ]
    for query_id, page in pages:
        f = page["filters"]
        filters = Filters(tuple(f["color"]), f["size"], f["size_system"], f["in_stock"])
        first = page["results"][0]["title"] if page["results"] else "(none)"
        lines.append(
            f"| [{query_id}](pages/{query_id}.md) | {page['status']} | {md_cell(page['query'])} | "
            f"{filters.describe()} | {md_cell(first)} |"
        )
    lines += ["", "## Data gap list", ""]
    for line in gaps:
        lines.append(
            f"- {line['product_id']} {line['title']}: {line['missing']} ({line['owner']}); "
            f"held from {', '.join(line['queries'])}, score {line['best_score']}, would rank "
            f"{line['best_rank']}"
        )
    if not gaps:
        lines.append("Nothing: no held product would have made the first results.")
    return "\n".join(lines) + "\n"


def write_outputs(
    output_dir: Path, pages: list[tuple[str, dict[str, Any]]], gaps: list[dict[str, Any]]
) -> None:
    (output_dir / "pages").mkdir(parents=True, exist_ok=True)
    for query_id, page in pages:
        (output_dir / "pages" / f"{query_id}.md").write_text(
            render_page(query_id, page), encoding="utf-8"
        )
    results = [{"id": query_id, **page} for query_id, page in pages]
    (output_dir / "results.json").write_text(
        json.dumps({"searches": results, "data_gaps": gaps}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (output_dir / "summary.md").write_text(render_summary(pages, gaps), encoding="utf-8")
    with (output_dir / "data_gaps.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=GAP_FIELDS)
        writer.writeheader()
        for line in gaps:
            row = {**line, "queries": " ".join(line["queries"])}
            writer.writerow({key: csv_safe(row[key]) for key in GAP_FIELDS})


def print_page(page: dict[str, Any]) -> None:
    print(render_page("search", page, dropped_shown=5))


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("query", nargs="?", help="one search; leave out to run the samples")
    parser.add_argument("--color", action="append", default=[], help="color facet (repeatable)")
    parser.add_argument("--size", help="size facet, e.g. 8, 8.5, 12W, XL")
    parser.add_argument("--size-system", help="sizing system, e.g. women, men, toddler")
    parser.add_argument(
        "--any-stock", action="store_true", help="include sold-out variants (default: in stock)"
    )
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    rules = load_rules()
    if args.query is not None:
        filters = Filters(
            tuple(c.lower() for c in args.color), args.size, args.size_system, not args.any_stock
        )
        with make_client() as client:
            page = stop_on_refusal(lambda: search_once(client, rules, args.query, filters))
        print_page(page)
        return
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    with make_client() as client:
        pages, gaps = stop_on_refusal(lambda: run(client, rules, output))
    for query_id, page in pages:
        print(f"{query_id}  {page['status']:<13} {page['reason']}")
    print(f"\n{len(gaps)} products on the data gap list.")
    print(f"outputs: {output / 'summary.md'}, pages/, results.json, data_gaps.csv")


if __name__ == "__main__":
    main()
