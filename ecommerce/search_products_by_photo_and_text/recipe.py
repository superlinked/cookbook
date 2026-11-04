"""Search a product catalog by photo, by words, or both, with one image-text embedding space.

Use case:  A shopper's camera search: "this lamp, but with a dark wood base". The shopper types
           how it looks, uploads a photo, or both, maybe with a department picked.
Who:       Shoppers see the results; the search and merchandising team reads the search gap
           log weekly.
Problem:   The photo has no words, the shopper's words aren't the catalog's, and "this, but
           different" isn't a keyword. Nearest-neighbor search always returns something, so a
           recliner comes back for "cordless power drill" unless every hit has to clear a bar.
Input:     the PIM lookup (pim.py, seeded from data/catalog.csv: 35 products with category,
           title, color, availability and the main photo's tile in data/catalog-photos.jpg),
           data/queries.csv (13 searches with an optional department facet) and
           data/query-photos.jpg, all photos from Amazon Berkeley Objects (CC BY 4.0). Rules
           and thresholds in rules.yaml.
SIE pipeline (one model):
           encode  google/siglip-so400m-patch14-384  once per run: every catalog photo and the
                                                     color captions (the catalog index)
           encode  google/siglip-so400m-patch14-384  once per search: the photo, the words, or
                                                     both
           (Python) unknown department, negated or over-long words, department and stock filter,
                    cosine ranking, a photo shortlist reordered by the words, a bar on every
                    hit, the close-style color check against the PIM, the search gap log
Output:    results.json (every search with its status, shown results and candidates),
           results.csv (one row per shown result) and search_gap_log.csv (failed searches,
           close styles and catalog photos that could not be indexed), in runs/<timestamp>/.
           The status says what the shopper sees; the search team decides what to fix.
Why SIE:   Photos and words land in one vector space from one model, so the same catalog index
           answers every kind of search. No generative model makes any decision. Nothing to host.
Run using: SIE Serverless, SIE Cluster or SIE Local (with SIE_BASE_URL).

    uv run recipe.py
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import numpy as np
import yaml
from dotenv import load_dotenv
from PIL import Image, ImageOps
from pim import PIM, Box, Product
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
DATA_DIR = ROOT / "data"
CATALOG_PHOTOS_PATH = DATA_DIR / "catalog-photos.jpg"
QUERIES_PATH = DATA_DIR / "queries.csv"
QUERY_PHOTOS_PATH = DATA_DIR / "query-photos.jpg"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
# How much of an SIE error message goes into a per-search reason or the summary line.
ERROR_CHARS = 200
# How many candidates the search gap log keeps as context for a search.
LOG_CANDIDATES = 3
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")

# What the shopper sees. `results`: products that cleared the bar. `no_good_match`: nothing did,
# so the shopper gets a link to browse. `unknown_department`: the facet isn't a catalog
# category, so the shopper is asked to clear it. `words_too_long`: longer than the model reads,
# so the shopper is asked to shorten them. `not_searched`: the search's SIE call failed or gave
# a broken vector, so the shopper is asked to try again; it says nothing about the catalog.
STATUSES = ("results", "no_good_match", "unknown_department", "words_too_long", "not_searched")
KINDS = ("text", "photo", "photo_and_text")
# What goes into the search gap log the search team reads weekly.
LOG_ENTRIES = (
    *("no_good_match", "unknown_department", "words_too_long", "not_searched"),
    *("close_style", "color_not_checked", "not_indexed"),
)
# A curly apostrophe (U+2019, as in a phone keyboard's "isn't") is read as a straight one.
APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'"})


@dataclass(frozen=True)
class Query:
    query_id: str
    kind: str  # text, photo or photo_and_text
    text: str  # the shopper's words; empty for a photo-only search
    department: str  # the department facet; empty means none picked
    box: Box | None  # the photo's tile in data/query-photos.jpg
    note: str


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    """The rules, with every negation word checked to be a string: YAML reads a bare `no` as
    false, which would silently drop it from the list."""
    rules = cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))
    for key in ("negation_words", "negation_kept"):
        bad = [word for word in rules["search"][key] if not isinstance(word, str)]
        if bad:
            raise ValueError(f"search.{key} in {path.name} must be quoted strings, not {bad}")
    return rules


def load_queries(path: Path = QUERIES_PATH) -> list[Query]:
    """Every search in `path`. A kind that doesn't fit the words and photo given raises an error
    before any SIE call."""
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    queries = []
    for row in rows:
        kind, text = row["kind"], row["text"].strip()
        has_photo = bool(row["x"])
        if kind not in KINDS:
            raise ValueError(f"{row['query_id']}: kind must be one of {KINDS}, not {kind!r}")
        if (kind != "photo") != bool(text) or (kind != "text") != has_photo:
            raise ValueError(f"{row['query_id']}: a {kind} search needs exactly that input")
        box = tuple(int(row[key]) for key in ("x", "y", "w", "h")) if has_photo else None
        queries.append(
            Query(
                query_id=row["query_id"],
                kind=kind,
                text=text,
                department=row["department"],
                box=cast(Box | None, box),
                note=row["note"],
            )
        )
    return queries


def check_colors(pim: PIM, rules: dict[str, Any]) -> None:
    """Every PIM color must be a color word the close-style check knows."""
    known = set(rules["close_style"]["colors"])
    unknown = sorted({p.color for p in pim.all_products()} - known)
    if unknown:
        raise ValueError(f"PIM colors not in close_style.colors in rules.yaml: {unknown}")


def make_client() -> SIEClient:
    """Build an SIE client from the API key and SIE_BASE_URL in the environment or `.env`."""
    load_dotenv()
    api_key = os.environ.get("SIE_API_KEY", "").strip()
    base_url = os.environ.get("SIE_BASE_URL", "").strip()
    if not api_key and not base_url:
        raise RuntimeError("Set SIE_API_KEY, or SIE_BASE_URL for SIE Cluster or SIE Local, in .env")
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
    those searches get not_searched and go to the log."""
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
    """The error for a per-search reason or the summary line, its message cut short."""
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
    """Every per-search SIE call the run made, and the ones that failed (with the model)."""

    made: int = 0
    failures: list[tuple[str, SIEError]] = field(default_factory=list)

    def stop_if_all_failed(self) -> None:
        """No search call succeeded, so nothing was searched: stop instead of writing empty
        outputs."""
        if not self.failures or len(self.failures) < self.made:
            return
        first = self.failures[0][1]
        models = ", ".join(dict.fromkeys(model for model, _ in self.failures))
        raise SIEStopped(
            f"Every search's SIE call failed ({self.made} of {self.made} to {models}), so the "
            f"run stops: {describe(first)}: {first}. {every_call_failed_advice(first)} "
            "No outputs were written."
        ) from first


def guarded[T](calls: CallLog, model: str, work: Callable[[], T]) -> tuple[T | None, str]:
    """Run one search's SIE call; return its result, or None and the reason it failed.

    A call that fails for a reason that may pass (a lost connection, a timeout, a server error,
    a rate limit, a reply without the key we asked for) is logged and its search gets
    not_searched; the run continues. A refusal every call would repeat (see `is_fatal`) stops
    the run. An error that isn't an SIE error is a bug in this code and is raised as is.
    """
    calls.made += 1
    try:
        return work(), ""
    except SIEError as error:
        if is_fatal(error):
            raise SIEStopped(stop_message(error, model)) from error
        calls.failures.append((model, error))
        return None, f"the SIE call failed: {short(error)}"


def encode(client: SIEClient, items: list[Item], rules: dict[str, Any]) -> list[np.ndarray | None]:
    """One encode call; one unit vector per item, or None for a vector that is empty, all zeros
    or not finite. A reply without a `dense` vector for every item is a failed call (an SIEError
    on the transient path), not an empty result."""
    reply: Any = client.encode(rules["models"]["embed"], items, provision_timeout_s=WAIT_S)
    if not isinstance(reply, list) or len(reply) != len(items):
        raise SIEError(
            f"the reply has {len(reply) if isinstance(reply, list) else 0} results "
            f"for {len(items)} items"
        )
    vectors: list[np.ndarray | None] = []
    for result in reply:
        dense = result.get("dense") if isinstance(result, dict) else None
        if dense is None:
            raise SIEError("the reply has no `dense` vector")
        vectors.append(unit(dense))
    return vectors


def unit(dense: Any) -> np.ndarray | None:
    """The vector scaled to length 1, or None when it is empty, all zeros or not finite."""
    try:
        array = np.asarray(dense, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError):
        return None
    norm = float(np.linalg.norm(array)) if array.size else 0.0
    if not array.size or not np.all(np.isfinite(array)) or norm == 0.0:
        return None
    return cast(np.ndarray, array / norm)


# --- Photos -----------------------------------------------------------------------------------


def open_sprite(path: Path) -> Image.Image:
    """The sprite sheet upright (EXIF rotation applied), in RGB."""
    with Image.open(path) as opened:
        return ImageOps.exif_transpose(opened).convert("RGB")


def photo_bytes(sprite: Image.Image, box: Box) -> bytes:
    x, y, width, height = box
    buffer = io.BytesIO()
    sprite.crop((x, y, x + width, y + height)).save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def caption(words: str, rules: dict[str, Any]) -> str:
    return str(rules["search"]["text_template"]).replace("{query}", words.strip())


# --- The catalog index (one call per run) -----------------------------------------------------


@dataclass
class Index:
    """One vector per indexed product photo and per color word, and what could not be indexed."""

    products: dict[str, np.ndarray]
    colors: dict[str, np.ndarray]
    not_indexed: list[dict[str, str]]


def build_index(client: SIEClient, pim: PIM, rules: dict[str, Any]) -> Index:
    """Embed every catalog photo and every color caption in one call. In production the photo
    vectors live in a vector index; here they are a dict in memory. A transient failure stops
    the run too: with no index, no search can run."""
    model = rules["models"]["embed"]
    products = pim.all_products()
    colors = list(rules["close_style"]["colors"])
    sprite = open_sprite(CATALOG_PHOTOS_PATH)
    items = [Item(id=p.product_id, images=[photo_bytes(sprite, p.box)]) for p in products]
    template = str(rules["close_style"]["template"])
    items += [Item(id=f"color:{c}", text=template.replace("{color}", c)) for c in colors]
    try:
        vectors = encode(client, items, rules)
    except SIEError as error:
        if is_fatal(error):
            raise SIEStopped(stop_message(error, model)) from error
        raise SIEStopped(
            f"The catalog index call to {model} failed, so no search can run: {short(error)}. "
            f"{every_call_failed_advice(error)} No outputs were written."
        ) from error
    index = Index(products={}, colors={}, not_indexed=[])
    for product, vector in zip(products, vectors, strict=False):
        if vector is None:
            index.not_indexed.append(
                {
                    "product_id": product.product_id,
                    "category": product.category,
                    "reason": "The model returned an empty, all-zero or broken vector for the "
                    "main photo, so no search can find this product.",
                }
            )
        else:
            index.products[product.product_id] = vector
    for color, vector in zip(colors, vectors[len(products) :], strict=True):
        if vector is not None:
            index.colors[color] = vector
    if not index.products:
        raise SIEStopped(
            f"No catalog photo got a usable vector from {model} ({len(products)} broken), so no "
            "search can run. No outputs were written."
        )
    dims = {vector.shape[0] for vector in [*index.products.values(), *index.colors.values()]}
    if len(dims) > 1:
        raise SIEStopped(
            f"The catalog index came back with vectors of different lengths {sorted(dims)}, so "
            "no search can run. No outputs were written."
        )
    return index


# --- Search (plain Python) --------------------------------------------------------------------


def negation(words: str, rules: dict[str, Any]) -> str:
    """The first negation word in the shopper's words, or "". A hyphenated word counts when any
    part of it is a negation word ("non-wood"), unless rules.yaml lists it as a product property
    ("non-stick")."""
    listed = {word.lower() for word in rules["search"]["negation_words"]}
    kept = {word.lower() for word in rules["search"]["negation_kept"]}
    tokens: list[str] = re.findall(r"[a-z'-]+", words.lower().translate(APOSTROPHES))
    for token in tokens:
        if token in kept:
            continue
        if any(part in listed or part.endswith("n't") for part in token.split("-")):
            return token
    return ""


def too_long(words: str, rules: dict[str, Any]) -> str:
    """Why the words are longer than the model reads, or ""."""
    count, limit = len(words.split()), int(rules["search"]["max_words"])
    if count <= limit:
        return ""
    return (
        f"The words are {count} words long, more than the {limit} this model reads; ask the "
        "shopper to shorten them."
    )


def scores(
    vectors: dict[str, np.ndarray], products: list[Product], query: np.ndarray
) -> list[float]:
    """Cosine similarity of the query vector with each product's photo vector, to 4 places."""
    return [round(float(vectors[p.product_id] @ query), 4) for p in products]


def hit(product: Product, **values: float) -> dict[str, Any]:
    return {"product_id": product.product_id, "title": product.title, **values}


def ranked(hits: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Best first by `key`; ties go to the lower product ID."""
    return sorted(hits, key=lambda h: (-h[key], h["product_id"]))


def top(hits: list[dict[str, Any]], key: str = "score") -> str:
    """The first few hits as "P17 0.895", or "P17 photo 0.895 words 0.041" for a photo with
    words."""

    def one(h: dict[str, Any]) -> str:
        if key == "score" and "words" in h and "photo" in h:
            return f"{h['product_id']} photo {h['photo']:.3f} words {h['words']:.3f}"
        return f"{h['product_id']} {h[key]:.3f}"

    return "; ".join(one(h) for h in hits[:LOG_CANDIDATES])


def search(
    query: Query,
    photo: np.ndarray | None,
    words: np.ndarray | None,
    pim: PIM,
    index: Index,
    rules: dict[str, Any],
) -> dict[str, Any]:
    """Rank the department's available, indexed products for one search and apply the bar to
    every hit. Returns the status, the reason, the shown results (best first) and the
    candidates (shown or not) for the gap log."""
    settings = rules["search"]
    top_k = int(settings["top_k"])
    pool = [p for p in pim.sellable(query.department) if p.product_id in index.products]
    if not pool:
        where = f" in {query.department}" if query.department else ""
        return outcome("no_good_match", f"No available, indexed products{where}.", [], [])

    if query.kind in ("text", "photo"):
        vector = photo if query.kind == "photo" else words
        assert vector is not None
        bar = float(settings["min_score"][query.kind])
        candidates = ranked(
            [
                hit(p, score=s)
                for p, s in zip(pool, scores(index.products, pool, vector), strict=True)
            ],
            "score",
        )
        shown = [h for h in candidates if h["score"] >= bar][:top_k]
        best = candidates[0]
        if shown:
            cut = len([h for h in candidates[:top_k] if h["score"] < bar])
            below = f"; {cut} more below the bar not shown" if cut else ""
            reason = (
                f"{len(shown)} cleared {bar:.2f}, best {best['product_id']} "
                f"{best['score']:.3f}{below}."
            )
            return outcome("results", reason, shown, candidates)
        reason = (
            f"No good match: best {best['product_id']} scores {best['score']:.3f}, below "
            f"{bar:.2f} for {query.kind} searches."
        )
        return outcome("no_good_match", reason, [], candidates)

    # A photo with words: the photo picks the shortlist, the words reorder it.
    assert photo is not None
    assert words is not None
    bars = settings["min_score"]["photo_and_text"]
    photo_bar, words_bar = float(bars["photo"]), float(bars["words"])
    photo_weight, words_weight = float(settings["photo_weight"]), float(settings["words_weight"])
    by_photo = ranked(
        [hit(p, photo=s) for p, s in zip(pool, scores(index.products, pool, photo), strict=True)],
        "photo",
    )
    shortlist_ids = {h["product_id"] for h in by_photo[: int(settings["photo_shortlist"])]}
    shortlist = [p for p in pool if p.product_id in shortlist_ids]
    candidates = ranked(
        [
            hit(
                p,
                score=round(photo_weight * ps + words_weight * ws, 4),
                photo=ps,
                words=ws,
            )
            for p, ps, ws in zip(
                shortlist,
                scores(index.products, shortlist, photo),
                scores(index.products, shortlist, words),
                strict=True,
            )
        ],
        "score",
    )
    shown = [h for h in candidates if h["photo"] >= photo_bar and h["words"] >= words_bar][:top_k]
    if shown:
        best = shown[0]
        reason = (
            f"{len(shown)} of the {len(candidates)}-product photo shortlist cleared both bars; "
            f"best {best['product_id']} (photo {best['photo']:.3f}, words {best['words']:.3f})."
        )
        return outcome("results", reason, shown, candidates)
    looks = ranked([h for h in candidates if h["photo"] >= photo_bar], "photo")
    if looks:
        # The best words score among the products that cleared the photo bar: a product with
        # better words that doesn't look like the photo is not what the shopper showed.
        best_words = ranked(looks, "words")[0]
        reason = (
            f"No good match: {top(looks, 'photo')} cleared the photo bar, but none of them has "
            f"what the words ask for (best words score {best_words['product_id']} "
            f"{best_words['words']:.3f}, below {words_bar:.2f})."
        )
    else:
        best_photo = by_photo[0]
        reason = (
            f"No good match: nothing looks like the photo (best {best_photo['product_id']} "
            f"{best_photo['photo']:.3f}, below {photo_bar:.2f})."
        )
    return outcome("no_good_match", reason, [], candidates)


def outcome(
    status: str, reason: str, shown: list[dict[str, Any]], candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    results = [{"rank": rank, **h} for rank, h in enumerate(shown, start=1)]
    return {"status": status, "reason": reason, "results": results, "candidates": candidates}


def close_style(
    query: Query,
    result: dict[str, Any],
    photo: np.ndarray | None,
    pim: PIM,
    index: Index,
    rules: dict[str, Any],
) -> tuple[str, str]:
    """For a photo search with results: the color check's outcome and its reason.

    The outcome is "" (not a photo search with results), "same_color", "close_style" (the
    photo's color is not the top result's PIM color: right look, maybe not the exact item) or
    "not_checked". The photo's color is the color caption that scores highest against the
    photo, so the check needs every caption: with one missing, the photo's real color may be
    the missing one, and the check counts as not done rather than as a match.
    """
    if query.kind != "photo" or result["status"] != "results" or photo is None:
        return "", ""
    product = pim.find_product(result["results"][0]["product_id"])
    assert product is not None
    missing = [c for c in rules["close_style"]["colors"] if c not in index.colors]
    if missing:
        return "not_checked", (
            f"Top result {product.product_id}: color not checked, the color captions for "
            f"{', '.join(missing)} have no vector this run."
        )
    looks = max(index.colors, key=lambda color: float(index.colors[color] @ photo))
    if looks == product.color:
        return "same_color", ""
    return "close_style", (
        f"Close style: the photo looks {looks}, top result {product.product_id} is "
        f"{product.color} in the PIM. Right look, maybe not the exact item."
    )


# --- One search, end to end -------------------------------------------------------------------

# A search that never reached ranking has no color check.
NO_COLOR_CHECK = {"color_check": "", "close_style": ""}
# The gap log entry for a search with results, by its color check.
COLOR_LOG_ENTRY = {"close_style": "close_style", "not_checked": "color_not_checked"}


def run_search(
    client: SIEClient,
    calls: CallLog,
    query: Query,
    sprite: Image.Image,
    pim: PIM,
    index: Index,
    rules: dict[str, Any],
) -> dict[str, Any]:
    base = {
        "query_id": query.query_id,
        "kind": query.kind,
        "words": query.text,
        "department": query.department,
        "note": query.note,
    }
    if query.department and not pim.has_department(query.department):
        reason = (
            f"Department {query.department} is not a catalog category; ask the shopper to "
            "clear the filter and search again."
        )
        return {**base, **outcome("unknown_department", reason, [], []), **NO_COLOR_CHECK}
    if query.text and (word := negation(query.text, rules)):
        reason = (
            f"No good match: the words hold a negation ({word!r}); this model reads "
            "'no wood' much like 'wood', so the words can't rank products."
        )
        return {**base, **outcome("no_good_match", reason, [], []), **NO_COLOR_CHECK}
    if query.text and (reason := too_long(query.text, rules)):
        return {**base, **outcome("words_too_long", reason, [], []), **NO_COLOR_CHECK}

    items = []
    if query.box is not None:
        items.append(Item(id=f"{query.query_id}:photo", images=[photo_bytes(sprite, query.box)]))
    if query.text:
        items.append(Item(id=f"{query.query_id}:words", text=caption(query.text, rules)))
    vectors, failed = guarded(calls, rules["models"]["embed"], lambda: encode(client, items, rules))
    if vectors is None:
        reason = f"Not searched: {failed}"
        return {**base, **outcome("not_searched", reason, [], []), **NO_COLOR_CHECK}
    photo = vectors[0] if query.box is not None else None
    words = vectors[-1] if query.text else None
    dims = {v.shape[0] for v in index.products.values()}
    broken = [v for v in vectors if v is None or v.shape[0] not in dims]
    if broken:
        reason = (
            "Not searched: the model returned an empty, all-zero, broken or wrong-length "
            "vector for this search."
        )
        return {**base, **outcome("not_searched", reason, [], []), **NO_COLOR_CHECK}
    result = search(query, photo, words, pim, index, rules)
    check, note = close_style(query, result, photo, pim, index, rules)
    return {**base, **result, "color_check": check, "close_style": note}


def run(
    client: SIEClient,
    rules: dict[str, Any],
    output_dir: Path | None,
    pim: PIM | None = None,
    queries_path: Path = QUERIES_PATH,
) -> dict[str, Any]:
    """Build the catalog index, then run every search in `queries_path`.

    Nothing is written until every search is done, so a run that stops on an SIE refusal or on
    every call failing leaves no partial outputs behind.
    """
    pim = pim or PIM()
    check_colors(pim, rules)
    queries = load_queries(queries_path)
    if not queries:
        raise SystemExit(f"No searches in {queries_path}, so there is nothing to run.")
    index = build_index(client, pim, rules)
    sprite = open_sprite(QUERY_PHOTOS_PATH)
    calls = CallLog()
    searches = [run_search(client, calls, q, sprite, pim, index, rules) for q in queries]
    calls.stop_if_all_failed()
    if calls.failures:
        print(
            f"{len(calls.failures)} of {calls.made} search calls failed; those searches got "
            f"not_searched. First: {short(calls.failures[0][1])}"
        )
    result = {
        "searches": searches,
        "not_indexed": index.not_indexed,
        "sie_calls": {"made": 1 + calls.made, "failed": len(calls.failures)},
    }
    if output_dir is not None:
        write_outputs(output_dir, result)
    return result


# --- Outputs ----------------------------------------------------------------------------------


def csv_safe(row: dict[str, Any]) -> dict[str, Any]:
    """Quote a cell that a spreadsheet would run as a formula ("=HYPERLINK(...)") with a '."""
    return {
        key: f"'{value}" if isinstance(value, str) and value.startswith(FORMULA_START) else value
        for key, value in row.items()
    }


RESULT_FIELDS = (
    *("query_id", "kind", "words", "department", "rank", "product_id", "title"),
    *("score", "photo_score", "words_score"),
)
LOG_FIELDS = ("entry", "query_id", "kind", "words", "department", "reason", "candidates")


def result_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for s in result["searches"]:
        for h in s["results"]:
            rows.append(
                {
                    **{key: s[key] for key in ("query_id", "kind", "words", "department")},
                    **{key: h[key] for key in ("rank", "product_id", "title")},
                    "score": h.get("score", ""),
                    "photo_score": h.get("photo", ""),
                    "words_score": h.get("words", ""),
                }
            )
    return rows


def gap_log(result: dict[str, Any]) -> list[dict[str, Any]]:
    """The search gap log: every search that got no results, a close style or no color check,
    and every catalog photo that could not be indexed."""
    rows = []
    for s in result["searches"]:
        entry = (
            s["status"] if s["status"] != "results" else COLOR_LOG_ENTRY.get(s["color_check"], "")
        )
        if not entry:
            continue
        rows.append(
            {
                "entry": entry,
                **{k: s[k] for k in ("query_id", "kind", "words", "department")},
                "reason": s["close_style"] or s["reason"],
                "candidates": top(s["candidates"]),
            }
        )
    for product in result["not_indexed"]:
        rows.append(
            {
                "entry": "not_indexed",
                "query_id": "",
                "kind": "",
                "words": "",
                "department": product["category"],
                "reason": f"{product['product_id']}: {product['reason']}",
                "candidates": "",
            }
        )
    return rows


def write_outputs(output_dir: Path, result: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "results.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    for name, fields, rows in (
        ("results.csv", RESULT_FIELDS, result_rows(result)),
        ("search_gap_log.csv", LOG_FIELDS, gap_log(result)),
    ):
        with (output_dir / name).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(csv_safe(row) for row in rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rules = load_rules()
    with make_client() as client:
        result = stop_on_refusal(lambda: run(client, rules, output))
    for s in result["searches"]:
        label = f'"{s["words"]}"' if s["kind"] == "text" else f"[photo] {s['note']}"
        if s["kind"] == "photo_and_text":
            label = f'[photo + "{s["words"]}"] {s["note"]}'
        scope = f" in {s['department']}" if s["department"] else ""
        print(f"\n{s['query_id']}  {label}{scope}")
        print(f"     {s['status']}: {s['reason']}")
        for h in s["results"]:
            both = f" (photo {h['photo']:.3f}, words {h['words']:.3f})" if "words" in h else ""
            print(f"     {h['rank']}. {h['product_id']} {h['score']:.3f}{both}  {h['title'][:60]}")
        if s["close_style"]:
            print(f"     logged: {s['close_style']}")
    counts = {status: sum(s["status"] == status for s in result["searches"]) for status in STATUSES}
    print(
        f"\n{len(result['searches'])} searches: "
        + ", ".join(f"{n} {status}" for status, n in counts.items())
        + f"; {result['sie_calls']['made']} SIE calls"
    )
    print(f"outputs: {output / 'results.json'}, results.csv, search_gap_log.csv")


if __name__ == "__main__":
    main()
