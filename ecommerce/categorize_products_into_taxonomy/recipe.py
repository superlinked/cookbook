"""Propose a taxonomy category and the required attributes for each new product on a supplier sheet.

Use case:  Catalog onboarding: map a supplier's spreadsheet of new products onto the store's
           category tree and fill in the attributes each category requires.
Who:       A catalog onboarding specialist at a retailer, who approves or corrects every
           proposal in the PIM before the batch is imported.
Problem:   Suppliers send little more than a title and a paragraph per product. Someone picks a
           category from thousands of paths, copies size, color, material, brand, model or net
           quantity out of the prose, and emails the supplier about what's missing.
Input:     data/supplier-products.csv (sku, supplier_title, supplier_description), the PIM's
           records (pim.py over data/pim-products.csv), the Shopify Standard Product Taxonomy
           trimmed to data/shopify-categories-subset.txt, and the rules in rules.yaml.
SIE pipeline (three models):
           (Python)                           stop on a SKU that appears twice; rows whose
                                              supplier SKU is in the PIM keep its category
           encode   Qwen/Qwen3-Embedding-4B   every taxonomy path and every product, in
                                              batches of 128 texts; top 10 paths by cosine
           score    Qwen/Qwen3-Reranker-4B    called once per product: rerank its 10 paths
           (Python)                           low score, thin margin over a different path, or
                                              a top-level pick: review
           extract  fastino/gliner2-large-v1  called once per window of at most 180 words: the
                                              attributes the chosen category requires
           (Python)                           every value must be words from the row and fit
                                              its pattern; missing, conflicting or "for X"
                                              values: review, and a question for the supplier
Output:    catalog.csv (one row per product in an import layout, with its status), review.csv
           (the rows a person must check, with the supplier's own text), questions.csv (what to
           ask the supplier, per SKU) and rows.json (every candidate and score) in
           runs/<timestamp>/. Statuses name what the checks found (checks_passed, review,
           in_pim); the specialist decides what is imported.
Why SIE:   Thousands of categories and no classifier to train: swap the taxonomy file and the
           same calls classify into it. No generative model makes the decision; plain Python
           reading rules.yaml does.
Models on: SIE Serverless, or your own SIE with SIE_BASE_URL.

    uv run recipe.py
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

import numpy as np
import yaml
from dotenv import load_dotenv
from pim import Pim, PimProduct
from sie_sdk import SIEClient
from sie_sdk.client.errors import (
    InputTooLongError,
    ModelLoadFailedError,
    RateLimitError,
    RequestError,
    ServerError,
    SIEConnectionError,
    SIEError,
)
from sie_sdk.types import Item

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PRODUCTS_PATH = DATA_DIR / "supplier-products.csv"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
# How much of an SIE error message goes into a per-row reason or the summary line.
ERROR_CHARS = 200
# A cell starting with one of these is a formula to a spreadsheet; csv_safe quotes it.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
# When GLiNER2 finds nothing, SIE Serverless answers the output_schema call with HTTP 500 and this
# message. It is not a failed call (no retry helps), but the window counts as not read: the row
# goes to review with that reason, so another window can never quietly fill in for it.
EMPTY_RESULT_ERROR = "Inference completed without a valid conserved billing settlement"

# Row status: what the checks found. The specialist approves or corrects every row in the PIM.
CHECKS_PASSED, REVIEW, IN_PIM = "checks_passed", "review", "in_pim"
SHEET_COLUMNS = ("sku", "supplier_title", "supplier_description")
REVIEW_FIELDS = (
    "sku",
    "supplier_title",
    "supplier_description",
    "best_path",
    "best_score",
    "runner_up_path",
    "runner_up_score",
    "logit_margin",
    "attributes",
    "reasons",
)
QUESTION_FIELDS = ("sku", "supplier_title", "attribute", "issue", "supplier_words")


@dataclass(frozen=True)
class Product:
    sku: str
    title: str
    description: str
    line: int = 0  # the row's line in the sheet, header = line 1

    def text(self) -> str:
        return f"{self.title}\n{self.description}"


@dataclass(frozen=True)
class Category:
    gid: str
    path: str

    @property
    def top_level(self) -> str:
        return self.path.split(" > ", 1)[0]

    @property
    def depth(self) -> int:
        return len(self.path.split(" > "))


@dataclass
class Decision:
    product: Product
    pim: PimProduct | None = None  # set when the row keeps the PIM's category
    candidates: list[tuple[Category, float]] = field(default_factory=list)  # by cosine
    ranked: list[tuple[Category, float]] = field(default_factory=list)  # by reranker
    runner_up: tuple[Category, float] | None = None
    margin: float | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)  # attribute -> the supplier's words
    reasons: list[str] = field(default_factory=list)
    questions: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def best(self) -> tuple[Category, float] | None:
        return self.ranked[0] if self.ranked else None

    @property
    def status(self) -> str:
        if self.pim is not None:
            return IN_PIM
        return REVIEW if self.reasons else CHECKS_PASSED

    def category(self) -> tuple[str, str]:
        """(id, path) of the proposed or PIM category, or empty strings."""
        if self.pim is not None:
            return self.pim.category_id, self.pim.category_path
        best = self.best
        return (best[0].gid, best[0].path) if best else ("", "")

    def as_json(self) -> dict[str, Any]:
        best = self.best
        gid, path = self.category()
        return {
            "sku": self.product.sku,
            "supplier_title": self.product.title,
            "supplier_description": self.product.description,
            "status": self.status,
            "category": {"gid": gid, "path": path} if path else None,
            "pim_product_id": self.pim.product_id if self.pim else None,
            "rerank_score": round(best[1], 4) if best else None,
            "runner_up": (
                {"path": self.runner_up[0].path, "score": round(self.runner_up[1], 4)}
                if self.runner_up
                else None
            ),
            "logit_margin": None if self.margin is None else round(self.margin, 4),
            "attributes": {
                name: {"value": value, "supplier_words": self.evidence.get(name, "")}
                for name, value in self.attributes.items()
            },
            "reasons": self.reasons,
            "questions": self.questions,
            "notes": self.notes,
            "reranked": [{"path": c.path, "score": round(s, 4)} for c, s in self.ranked],
            "retrieved": [{"path": c.path, "cosine": round(s, 4)} for c, s in self.candidates],
        }


def make_client() -> SIEClient:
    """Build an SIE client from the API key and SIE_BASE_URL in the environment or `.env`."""
    load_dotenv()
    api_key = os.environ.get("SIE_API_KEY", "").strip()
    base_url = os.environ.get("SIE_BASE_URL", "").strip()
    if not api_key and not base_url:
        raise RuntimeError("Set SIE_API_KEY, or SIE_BASE_URL for your own SIE, in .env")
    base_url = base_url or "https://api.superlinked.com"
    return SIEClient(api_key=api_key or None, base_url=base_url, timeout_s=WAIT_S)


def jsonable(value: Any) -> Any:
    """Turn SDK results (numpy arrays, nested dicts and lists) into plain JSON values."""
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, dict):
        return {str(key): jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(child) for child in value]
    return value


# --- SIE call failures ------------------------------------------------------------------------


class SIEStopped(RuntimeError):
    """SIE refused the call for a reason a retry won't fix, or no call succeeded; the run
    stops without outputs."""


class CallFailed(Exception):
    """One SIE call failed for a reason that may pass; the rows it was for go to review."""


@dataclass
class Calls:
    """Every SIE call the run made, and the transient failures among them."""

    count: dict[str, int] = field(default_factory=dict)  # model -> calls
    failures: list[tuple[str, SIEError]] = field(default_factory=list)  # (model, error)

    @property
    def total(self) -> int:
        return sum(self.count.values())

    def call(self, model: str, work: Callable[[], Any], problem: Callable[[Any], str]) -> Any:
        """Run one SIE call and return its reply, which `problem` must find nothing wrong with.

        A refusal (see `is_fatal`) stops the run with SIEStopped. Any other SIE error, and a
        reply `problem` objects to (a missing key, an item error), is recorded and raised as
        CallFailed, so the caller sends its rows to review and the run goes on. An error that
        isn't an SIE error is a bug in this code and is raised as is.
        """
        self.count[model] = self.count.get(model, 0) + 1
        try:
            reply = jsonable(work())
        except SIEError as error:
            if is_fatal(error):
                raise SIEStopped(stop_message(error, model)) from error
            self.failures.append((model, error))
            raise CallFailed(f"the SIE call failed: {short(error)}") from error
        if wrong := problem(reply):
            bad = SIEError(f"{model} replied {wrong}")
            self.failures.append((model, bad))
            raise CallFailed(f"the SIE call failed: {short(bad)}")
        return reply

    def check_all_failed(self) -> None:
        """Stop the run when every call failed; print a line when only some did."""
        if not self.failures:
            return
        total, (_, first) = self.total, self.failures[0]
        if len(self.failures) == total:
            models = ", ".join(sorted({name for name, _ in self.failures}))
            raise SIEStopped(
                f"Every SIE call failed ({total} of {total} to {models}), so the run stops: "
                f"{describe(first)}: {first}. {every_call_failed_advice(first)} "
                "No outputs were written."
            ) from first
        n = len(self.failures)
        print(f"{n} of {total} SIE calls failed; their rows go to review. First: {short(first)}")


def every_call_failed_advice(error: SIEError) -> str:
    """What to check when every SIE call failed, judged by the first failure.

    A lost connection points at SIE_BASE_URL and the network. Any other transient error (a
    timeout, a model still loading, a 5xx, a rate limit) more likely means the model is still
    starting or SIE is busy, so the advice is to wait and run again.
    """
    if isinstance(error, SIEConnectionError):
        return "Check SIE_BASE_URL and your network."
    return "The model may still be starting or SIE is busy; run again in a few minutes."


def is_fatal(error: SIEError) -> bool:
    """A refusal that fails every call the same way: a 4xx (bad key 401, forbidden 403, model
    not found 404, no credits or spend limit 402, inactive account) or a model the server
    marked as permanently unable to load. A RequestError with no HTTP status or a status below
    400 is a reply the SDK couldn't parse, and is fatal too. A rate limit, an over-long input, a
    timeout, a lost connection, a model still loading and a 5xx are transient or per-item:
    those rows go to review."""
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
    """The error for a per-row reason or the summary line, its message cut short."""
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


def stop_on_refusal[T](work: Callable[[], T]) -> T:
    """Run the work; turn SIEStopped into a clean exit. Any other error keeps its traceback."""
    try:
        return work()
    except SIEStopped as error:
        raise SystemExit(f"Stopped: {error}") from None


# --- Load the sheet, the taxonomy and the rules -----------------------------------------------


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def load_products(path: Path = PRODUCTS_PATH) -> list[Product]:
    """Read the supplier sheet. A missing column, a row with no SKU or no text, or a SKU that
    appears twice stops the run here, before any SIE call, naming the rows."""
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in SHEET_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"{path.name} has no column {', '.join(missing)}; nothing was run.")
        products = [
            Product(
                (row["sku"] or "").strip(),
                (row["supplier_title"] or "").strip(),
                (row["supplier_description"] or "").strip(),
                reader.line_num,
            )
            for row in reader
        ]
    for product in products:
        if not product.sku or not (product.title or product.description):
            raise SystemExit(
                f"{path.name} line {product.line} has no SKU or no title and description; "
                "fix the sheet, then run again. Nothing was run."
            )
    check_unique_skus(products, path.name)
    return products


def check_unique_skus(products: list[Product], sheet: str) -> None:
    """Stop when a SKU appears twice: results are keyed by SKU, so one row's category and
    attributes would silently land on the other."""
    lines: dict[str, list[Product]] = {}
    for product in products:  # "tt-a13" and "TT-A13" are one SKU to a person, so here too
        lines.setdefault(product.sku.casefold(), []).append(product)
    repeated = {sku: rows for sku, rows in lines.items() if len(rows) > 1}
    if repeated:
        listed = "; ".join(
            f"{rows[0].sku} on " + " and ".join(f"line {p.line} ({p.title!r})" for p in rows)
            for rows in repeated.values()
        )
        raise SystemExit(
            f"{sheet}: the same SKU is on more than one row: {listed}. Give each variant its own "
            "SKU, then run again. No SIE call was made and nothing was written."
        )


def load_taxonomy(path: Path) -> list[Category]:
    """Read Shopify's categories.txt: one `<gid> : <Top> > ... > <Leaf>` per line."""
    categories: list[Category] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or " : " not in line:
            continue
        gid, category_path = (part.strip() for part in line.split(" : ", 1))
        categories.append(Category(gid, category_path))
    return categories


def taxonomy_path(rules: dict[str, Any]) -> Path:
    return ROOT / str(rules["taxonomy"]["file"])


# --- The PIM lookup ---------------------------------------------------------------------------


def title_words(title: str) -> list[str]:
    """Words of three letters or more, lower case."""
    return [w for w in re.findall(r"[^\W\d_]{3,}", title.casefold())]


def variant_marks(title: str) -> set[str]:
    """The tokens that tell variants apart and `title_words` skips: anything with a digit (28,
    "2 pk", AV-H700) and words of one or two letters (S, M, XL), lower case."""
    tokens = re.findall(r"[^\W_]+(?:[-./][^\W_]+)*", title.casefold())
    return {t for t in tokens if len(t) <= 2 or re.search(r"\d", t)}


def check_pim(product: Product, pim: Pim, rules: dict[str, Any]) -> Decision:
    """Look the row up in the PIM by supplier and SKU.

    One record whose title agrees with the supplier's: the row keeps the PIM's category and
    attributes (`in_pim`) and no model is called. The titles agree when enough of the sheet
    title's words are in the PIM title, every PIM title word is in the row (a white sock is not
    the PIM's black one), and both name the same sizes, pack counts and model numbers. Several
    records, a record with no category, or one whose title doesn't agree (the SKU may have been
    reused for another product or variant): the row is categorized like a new one and goes to
    review with the reason.
    """
    decision = Decision(product=product)
    found = pim.find_by_supplier_sku(rules["supplier"], product.sku)
    if len(found) > 1:
        ids = ", ".join(record.product_id for record in found)
        decision.reasons.append(f"pim_several: SKU {product.sku} is on PIM products {ids}")
    elif found and not (found[0].category_id.strip() and found[0].category_path.strip()):
        decision.reasons.append(
            f"pim_no_category: SKU {product.sku} is PIM product {found[0].product_id}, which "
            "has no category"
        )
    elif found:
        record = found[0]
        words = title_words(product.title)
        pim_words = set(title_words(record.title))
        share = sum(w in pim_words for w in words) / len(words) if words else 0.0
        unsaid = sorted(pim_words - set(title_words(product.text())))
        marks, pim_marks = variant_marks(product.title), variant_marks(record.title)
        said = f"PIM product {record.product_id} {record.title!r}; the sheet says {product.title!r}"
        if share < rules["pim"]["min_title_share"]:
            decision.reasons.append(
                f"pim_title_differs: SKU {product.sku} is {said} (title words shared "
                f"{share:.2f} < {rules['pim']['min_title_share']}). A reused SKU?"
            )
        elif unsaid:
            decision.reasons.append(
                f"pim_title_differs: SKU {product.sku} is {said}, and the row never says "
                f"{', '.join(unsaid)}. A reused SKU?"
            )
        elif marks != pim_marks:
            listed = [", ".join(sorted(m)) or "none" for m in (marks, pim_marks)]
            decision.reasons.append(
                f"pim_variant_differs: SKU {product.sku} is {said} (size, pack or model "
                f"{listed[0]} vs {listed[1]}). A reused SKU for another variant?"
            )
        else:
            decision.pim = record
            decision.notes.append(
                f"in the PIM as {record.product_id} ({record.title!r}); keeps its category "
                "and attributes"
            )
    return decision


# --- Retrieve: encode, then cosine top-k in Python --------------------------------------------


def _vectors_problem(count: int) -> Callable[[Any], str]:
    def problem(reply: Any) -> str:
        if not isinstance(reply, list) or len(reply) != count:
            return f"with {len(reply) if isinstance(reply, list) else 'no'} results for {count}"
        if not all(isinstance(r, dict) and r.get("dense") for r in reply):
            return "without a `dense` vector for every text"
        return ""

    return problem


def encode_batches(
    client: SIEClient,
    calls: Calls,
    texts: list[str],
    rules: dict[str, Any],
    *,
    is_query: bool,
) -> tuple[list[np.ndarray | None], list[str]]:
    """Unit vectors for `texts` (None where its batch failed) and, per text, why its batch
    failed ("" where it didn't)."""
    model, size = rules["models"]["embed"], rules["taxonomy"]["embed_batch_size"]
    instruction = " ".join(rules["taxonomy"]["embed_instruction"].split()) if is_query else None
    vectors: list[np.ndarray | None] = []
    failed: list[str] = []
    for start in range(0, len(texts), size):
        batch = texts[start : start + size]

        def send(batch: list[str] = batch, start: int = start) -> Any:
            return client.encode(
                model,
                [Item(id=f"t{start + i}", text=text) for i, text in enumerate(batch)],
                is_query=is_query,
                instruction=instruction,
                provision_timeout_s=WAIT_S,
            )

        try:
            reply = calls.call(model, send, _vectors_problem(len(batch)))
        except CallFailed as failure:
            vectors.extend([None] * len(batch))
            failed.extend([str(failure)] * len(batch))
            continue
        failed.extend([""] * len(batch))
        for result in reply:
            vector = np.asarray(result["dense"], dtype=np.float64)
            vectors.append(vector / np.linalg.norm(vector))
    return vectors, failed


def shortlist(
    product_vector: np.ndarray, categories: list[Category], path_vectors: np.ndarray, k: int
) -> list[tuple[Category, float]]:
    """The k paths most similar to the product, best first (all vectors unit-normed)."""
    similarity = path_vectors @ product_vector
    order = np.argsort(-similarity, kind="stable")[:k]
    return [(categories[i], round(float(similarity[i]), 6)) for i in order]


def retrieve(
    client: SIEClient,
    calls: Calls,
    decisions: list[Decision],
    categories: list[Category],
    rules: dict[str, Any],
) -> None:
    """Fill each decision's top-k taxonomy paths by cosine. A failed taxonomy batch leaves its
    paths out of every shortlist and sends every row to review; a failed product batch sends
    its rows to review with no shortlist."""
    path_vectors, path_failures = encode_batches(
        client, calls, [c.path for c in categories], rules, is_query=False
    )
    kept = [i for i, v in enumerate(path_vectors) if v is not None]
    if first_failure := next((why for why in path_failures if why), ""):
        for d in decisions:
            d.reasons.append(
                f"shortlist_incomplete: {len(categories) - len(kept)} of {len(categories)} "
                f"taxonomy paths were not embedded ({first_failure})"
            )
    if not kept:
        return
    matrix = np.vstack([cast(np.ndarray, path_vectors[i]) for i in kept])
    usable = [categories[i] for i in kept]
    product_vectors, product_failures = encode_batches(
        client, calls, [d.product.text() for d in decisions], rules, is_query=True
    )
    k = rules["thresholds"]["top_k"]
    for d, vector, why in zip(decisions, product_vectors, product_failures, strict=True):
        if vector is None:
            d.reasons.append(f"not_embedded: the product's embedding call failed: {why}")
            continue
        d.candidates = shortlist(vector, usable, matrix, k)


# --- Rerank and decide the category -----------------------------------------------------------


def _scores_problem(count: int) -> Callable[[Any], str]:
    def problem(reply: Any) -> str:
        if not isinstance(reply, dict) or not isinstance(reply.get("scores"), list):
            return "without `scores`"
        entries = reply["scores"]
        if not all(isinstance(entry, dict) for entry in entries):
            return "with a score entry that is not an object"
        ids = sorted(str(entry.get("item_id")) for entry in entries)
        if ids != sorted(f"c{i}" for i in range(count)):
            return f"with scores for {ids}, not for the {count} paths sent"
        # A missing, null or NaN score would pass every threshold check (NaN < 0.5 is False).
        bad = [e.get("score") for e in entries if not is_number(e.get("score"))]
        if bad:
            return f"with scores that are not numbers: {bad[:3]}"
        return ""

    return problem


def is_number(value: Any) -> bool:
    """A finite int or float (not a bool, None or NaN)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def rerank(
    client: SIEClient, calls: Calls, decision: Decision, rules: dict[str, Any]
) -> list[tuple[Category, float]]:
    """The shortlist scored by the reranker, best first. Raises CallFailed."""
    model = rules["models"]["rerank"]
    instruction = " ".join(rules["taxonomy"]["rerank_instruction"].split())
    paths = [category for category, _ in decision.candidates]
    product = decision.product
    reply = calls.call(
        model,
        lambda: client.score(
            model,
            Item(id=product.sku, text=product.text()),
            [Item(id=f"c{index}", text=category.path) for index, category in enumerate(paths)],
            instruction=instruction,
            provision_timeout_s=WAIT_S,
        ),
        _scores_problem(len(paths)),
    )
    scored = [
        (paths[int(entry["item_id"][1:])], float(entry["score"])) for entry in reply["scores"]
    ]
    return sorted(scored, key=lambda pair: pair[1], reverse=True)


def log_odds(probability: float) -> float:
    """The reranker's yes-vs-no logit behind its probability score."""
    p = min(max(probability, 1e-9), 1 - 1e-9)
    return math.log(p / (1 - p))


def same_lineage(a: Category, b: Category) -> bool:
    """True when one path is the other or an ancestor of it."""
    return (
        a.path == b.path or b.path.startswith(a.path + " > ") or a.path.startswith(b.path + " > ")
    )


def decide_category(decision: Decision, rules: dict[str, Any]) -> bool:
    """Check the reranker's pick; True when its required attributes can be read.

    Review when the best path scores low, a different category (not its parent or child)
    scores nearly as high, or the pick is a top-level branch. The margin is in log-odds: the
    reranker's probabilities crowd near 1.0, where 0.99 vs 0.98 looks like a tie in
    probability but is a clear lead in logits.

    The required attributes come from the pick's top-level branch, so a thin margin between
    two paths in the same branch (Thermoses or Water Bottles) still says which attributes to
    read: the row goes to review for its category, and the supplier questions are ready too.
    A low score, a top-level pick, or a runner-up in another branch reads nothing.
    """
    limits = rules["thresholds"]
    best = decision.best
    if best is None:
        decision.reasons.append("no_candidate: no category was scored")
        return False
    category, score = best
    others = [pair for pair in decision.ranked[1:] if not same_lineage(category, pair[0])]
    if others:
        decision.runner_up = others[0]
        decision.margin = log_odds(score) - log_odds(others[0][1])
    readable = True
    if score < limits["min_top_score"]:
        decision.reasons.append(
            f"low_score: best path scores {score:.2f} (< {limits['min_top_score']}); "
            "the right category may be missing from the taxonomy file"
        )
        readable = False
    elif decision.margin is not None and decision.margin < limits["min_logit_margin"]:
        assert decision.runner_up is not None
        decision.reasons.append(
            f"thin_margin: '{category.path}' {score:.2f} vs "
            f"'{decision.runner_up[0].path}' {decision.runner_up[1]:.2f} "
            f"(log-odds margin {decision.margin:.2f} < {limits['min_logit_margin']})"
        )
        readable = decision.runner_up[0].top_level == category.top_level
    if category.depth < limits["min_depth"]:
        decision.reasons.append(
            f"top_level_only: the pick '{category.path}' is a top-level branch, not a category"
        )
        readable = False
    return readable


# --- Extract the attributes the category requires ---------------------------------------------


def attribute_specs(top_level: str, rules: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
    """The attributes a top-level category requires, or None if rules.yaml has no entry."""
    specs = rules["attributes"].get(top_level)
    return None if specs is None else cast(dict[str, dict[str, Any]], specs)


def output_schema(specs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            name: {"type": "string", "description": spec["description"]}
            for name, spec in specs.items()
        },
    }


def windows(text: str, size: int, overlap: int) -> list[str]:
    """Split text into windows of at most `size` words, each overlapping the last by `overlap`."""
    tokens = re.findall(r"\S+\s*", text)
    if not tokens:
        return []
    step = max(1, size - overlap)
    parts: list[str] = []
    for start in range(0, len(tokens), step):
        parts.append("".join(tokens[start : start + size]).strip())
        if start + size >= len(tokens):
            break
    return parts


def _data_problem(reply: Any) -> str:
    if not isinstance(reply, dict):
        return "with no result"
    if reply.get("error"):
        return f"with an item error: {reply['error']}"
    if not isinstance(reply.get("data"), dict):
        return "without `data`"
    # A field it didn't find may come back as null; a number, list or object is not a span.
    odd = {k: v for k, v in reply["data"].items() if v is not None and not isinstance(v, str)}
    if odd:
        return f"with values that are not text: {odd}"
    return ""


def extract_attributes(
    client: SIEClient,
    calls: Calls,
    decision: Decision,
    specs: dict[str, dict[str, Any]],
    rules: dict[str, Any],
) -> list[tuple[str, str, str]]:
    """Ask GLiNER2 for the attributes in each window. Returns (name, value, window) triples.

    A window that couldn't be read (a failed call, or SIE's empty-result HTTP 500) adds a
    review reason: what that window says is unknown, so the row can't pass on the others."""
    model, limits = rules["models"]["extract"], rules["thresholds"]
    schema = output_schema(specs)
    product = decision.product
    found: list[tuple[str, str, str]] = []
    parts = windows(product.text(), limits["window_words"], limits["window_overlap_words"])
    for index, window in enumerate(parts, start=1):

        def send(index: int = index, window: str = window) -> Any:
            try:
                return client.extract(
                    model,
                    Item(id=f"{product.sku}#w{index}", text=window),
                    output_schema=schema,
                    # A window too long to read whole fails (InputTooLongError, the transient
                    # path: the row goes to review) instead of being cut short.
                    options={"overflow_policy": "error"},
                    provision_timeout_s=WAIT_S,
                )
            except ServerError as error:
                if EMPTY_RESULT_ERROR not in str(error):
                    raise
                return {"data": {}, "empty_result_error": True}

        try:
            reply = calls.call(model, send, _data_problem)
        except CallFailed as failure:
            decision.reasons.append(f"extract_failed: window {index} was not read: {failure}")
            continue
        if reply.get("empty_result_error"):
            decision.reasons.append(
                f"extract_failed: window {index} was not read: SIE answered HTTP 500 "
                f"({EMPTY_RESULT_ERROR}), which it also sends when nothing is found"
            )
            continue
        if not reply["data"]:
            decision.notes.append(f"window {index}: no attributes found")
        for name, value in reply["data"].items():
            if name in specs and isinstance(value, str) and value.strip():
                found.append((name, value.strip(), window))
    return found


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def occurrences(value: str, text: str) -> list[re.Match[str]]:
    """Where `value` stands in `text` as whole words (case and spacing ignored). "M" is not in
    "Merino", "2.1" is not in "12.1", and "US 9" is not in "US 9.5"."""
    words = value.split()
    if not words:
        return []
    pattern = (
        r"(?<![^\W_])(?<!\d[.,])"
        + r"\s+".join(re.escape(w) for w in words)
        + r"(?![^\W_])(?![.,]\d)"
    )
    return list(re.finditer(pattern, text, re.IGNORECASE))


def not_given(value: str, rules: dict[str, Any]) -> bool:
    """A value that says the attribute was not given ("n/a", "not specified", "no size")."""
    squashed = _squash(value).strip(" .:;-")
    return squashed in {_squash(v) for v in rules["not_given"]} or bool(
        re.match(r"(no|not|without)\b", squashed)
    )


def lists_two_values(value: str) -> bool:
    """A value that lists two ("M or L", "340 g, 500 g", "black and white")."""
    return re.search(r"\s(?:or|and)\s|[,;&]|\s/\s", value) is not None


def follows_fits_word(match: re.Match[str], text: str, rules: dict[str, Any]) -> str:
    """The fits word ("for", "fits", ...) in the few words before the match, or "". A phrase
    of several words counts as one: "compatible with the Aurivox AV-H700" is two words after
    "compatible with"."""
    before = re.findall(r"[^\W_]+", text[: match.start()].casefold())
    for phrase in sorted(rules["fits_words"], key=len, reverse=True):
        words = re.findall(r"[^\W_]+", str(phrase).casefold())
        tail = " " + " ".join(before[-(rules["fits_window_words"] + len(words) - 1) :]) + " "
        if words and f" {' '.join(words)} " in tail:
            return str(phrase)
    return ""


def fits_spots(
    spots: list[re.Match[str]], text: str, rules: dict[str, Any]
) -> list[tuple[str, re.Match[str]]]:
    """(fits word, spot) for each spot of a value that follows a fits word. A value counts as
    the product it fits only when every spot does: "Brand: Aurivox ... for the Aurivox AV-H700"
    still names Aurivox as the brand."""
    found = [(follows_fits_word(spot, text, rules), spot) for spot in spots]
    return [(word, spot) for word, spot in found if word]


def supplier_words(text: str, start: int, end: int, around: int = 40) -> str:
    """The supplier's own words around a span, on one line."""
    left = max(0, start - around)
    right = min(len(text), end + around)
    snippet = " ".join(text[left:right].split())
    return ("..." if left else "") + snippet + ("..." if right < len(text) else "")


def scan_values(text: str, pattern: str) -> list[str]:
    """Every value the `scan` regex finds in the row, normalized ("340g" = "340 g")."""
    values: list[str] = []
    for number, unit in re.findall(pattern, text, re.IGNORECASE):
        value = f"{number.replace(',', '.')} {re.sub(r'\s+', ' ', unit.casefold())}"
        if value not in values:
            values.append(value)
    return values


def merge_attributes(
    found: list[tuple[str, str, str]],
    specs: dict[str, dict[str, Any]],
    decision: Decision,
    rules: dict[str, Any],
    all_read: bool = True,
) -> None:
    """Keep the values that check out; anything missing, conflicting or naming another product
    sends the row to review and adds a question for the supplier.

    When a window was not read (`all_read` False), an attribute not found may be in that
    window, so it is not reported missing and the supplier is not asked for it; the row is
    already in review for the unread window."""
    product = decision.product
    text = product.text()

    def ask(name: str, issue: str, words: str) -> None:
        decision.questions.append(
            {
                "sku": product.sku,
                "supplier_title": product.title,
                "attribute": name,
                "issue": issue,
                "supplier_words": words,
            }
        )

    for name, spec in specs.items():
        values: list[tuple[str, re.Match[str]]] = []
        fits: list[tuple[str, str, re.Match[str]]] = []
        for field_name, value, window in found:
            if field_name != name:
                continue
            spots = occurrences(value, text)
            pattern = spec.get("pattern")
            if not re.search(r"[^\W_]", value) or not occurrences(value, window) or not spots:
                decision.notes.append(f"{name}: dropped '{value}' (not words from the row)")
            elif not_given(value, rules):
                decision.notes.append(f"{name}: dropped '{value}' (says it was not given)")
            elif pattern and re.search(pattern, value, re.IGNORECASE) is None:
                decision.notes.append(f"{name}: dropped '{value}' (doesn't fit {pattern})")
            elif spec.get("one_value") and lists_two_values(value):
                decision.reasons.append(f"conflicting_attribute: {name} '{value}' lists two")
                ask(
                    name, "lists two values", supplier_words(text, spots[0].start(), spots[0].end())
                )
            elif spec.get("fits_check") and len(hosts := fits_spots(spots, text, rules)) == len(
                spots
            ):
                fits.append((value, *hosts[0]))
            elif _squash(value) not in {_squash(v) for v, _ in values}:
                values.append((value, spots[0]))
        for value, word, spot in fits:
            decision.reasons.append(
                f"names_another_product: {name} '{value}' follows '{word}', so it names the "
                "product this fits, not this product"
            )
            ask(
                name,
                f"'{value}' is the product it fits",
                supplier_words(text, spot.start(), spot.end()),
            )
        # A value standing as whole words in a longer one ("navy" in "heather navy") is the
        # same value; "L" in "XL" or "AV-H70" in "AV-H700" is a different one.
        distinct = [
            (v, m) for v, m in values if not any(occurrences(v, o) for o, _ in values if o != v)
        ]
        if scan := spec.get("scan"):
            seen = scan_values(text, scan)
            if len(seen) > 1:
                decision.reasons.append(
                    f"conflicting_attribute: {name}: the row says {' and '.join(seen)}"
                )
                ask(name, "two different values", " / ".join(seen))
        if len(distinct) > 1:
            decision.reasons.append(
                f"conflicting_attribute: {name} = {' / '.join(v for v, _ in distinct)}"
            )
            ask(
                name,
                "two different values",
                " / ".join(supplier_words(text, m.start(), m.end()) for _, m in distinct),
            )
        if distinct:
            value, spot = max(distinct, key=lambda pair: len(pair[0]))
            decision.attributes[name] = value
            decision.evidence[name] = supplier_words(text, spot.start(), spot.end())
        elif all_read and not fits and not any(q["attribute"] == name for q in decision.questions):
            decision.reasons.append(f"missing_attribute: {name} not found in the row")
            ask(name, "missing", product.title)


# --- Run --------------------------------------------------------------------------------------


def tag_product(client: SIEClient, calls: Calls, decision: Decision, rules: dict[str, Any]) -> None:
    """Rerank the shortlist, check the pick, and read the attributes its category requires."""
    if not decision.candidates:
        return  # the embedding step failed; its reason is already on the row
    try:
        decision.ranked = rerank(client, calls, decision, rules)
    except CallFailed as failure:
        decision.reasons.append(f"rerank_failed: {failure}")
        return
    if not decide_category(decision, rules):
        return  # no settled branch, so no attribute list to check against
    best = decision.best
    assert best is not None
    specs = attribute_specs(best[0].top_level, rules)
    if specs is None:
        decision.reasons.append(
            f"no_attribute_rules: rules.yaml has no attributes for '{best[0].top_level}'"
        )
        return
    unread = len(decision.reasons)
    found = extract_attributes(client, calls, decision, specs, rules)
    all_read = not any(r.startswith("extract_failed") for r in decision.reasons[unread:])
    merge_attributes(found, specs, decision, rules, all_read)


def run(
    client: SIEClient,
    rules: dict[str, Any],
    products: list[Product],
    output_dir: Path | None,
    pim: Pim | None = None,
) -> tuple[list[Decision], Calls]:
    """Propose a category and attributes for every row on the sheet.

    Nothing is written until every row is done, so a run that stops on an SIE refusal or on
    every call failing leaves no partial outputs behind.
    """
    check_unique_skus(products, "the sheet")
    pim = pim or Pim()
    decisions = [check_pim(product, pim, rules) for product in products]
    new = [d for d in decisions if d.pim is None]
    calls = Calls()
    if new:
        categories = load_taxonomy(taxonomy_path(rules))
        retrieve(client, calls, new, categories, rules)
        for decision in new:
            tag_product(client, calls, decision, rules)
    calls.check_all_failed()
    if output_dir is not None:
        write_outputs(output_dir, decisions, rules)
    return decisions, calls


def csv_safe(value: Any) -> str:
    """A leading ' on a cell a spreadsheet would run as a formula; anything else as written."""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_safe(value) for key, value in row.items()})


def attribute_columns(rules: dict[str, Any]) -> tuple[str, ...]:
    """Every attribute any category requires, in rules.yaml order."""
    names: dict[str, None] = {}
    for specs in rules["attributes"].values():
        names.update(dict.fromkeys(specs))
    return tuple(names)


def _pim_attributes(text: str) -> dict[str, str]:
    pairs = (part.split("=", 1) for part in text.split(";") if "=" in part)
    return {name.strip(): value.strip() for name, value in pairs}


def _attributes_text(attributes: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in attributes.items())


def write_outputs(output_dir: Path, decisions: list[Decision], rules: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    columns = attribute_columns(rules)
    catalog = []
    for d in decisions:
        gid, path = d.category()
        attributes = _pim_attributes(d.pim.attributes) if d.pim else d.attributes
        catalog.append(
            {
                "sku": d.product.sku,
                "supplier_title": d.product.title,
                "status": d.status,
                "category_id": gid,
                "category_path": path,
                **{name: attributes.get(name, "") for name in columns},
                "reasons": " | ".join(d.reasons),
            }
        )
    catalog_fields = (
        *("sku", "supplier_title", "status", "category_id", "category_path"),
        *columns,
        "reasons",
    )
    _write_csv(output_dir / "catalog.csv", catalog_fields, catalog)
    review = []
    for d in decisions:
        if d.status != REVIEW:
            continue
        best = d.best
        review.append(
            {
                "sku": d.product.sku,
                "supplier_title": d.product.title,
                "supplier_description": d.product.description,
                "best_path": best[0].path if best else "",
                "best_score": f"{best[1]:.4f}" if best else "",
                "runner_up_path": d.runner_up[0].path if d.runner_up else "",
                "runner_up_score": f"{d.runner_up[1]:.4f}" if d.runner_up else "",
                "logit_margin": "" if d.margin is None else f"{d.margin:.4f}",
                "attributes": _attributes_text(d.attributes),
                "reasons": " | ".join(d.reasons),
            }
        )
    _write_csv(output_dir / "review.csv", REVIEW_FIELDS, review)
    questions = [q for d in decisions for q in d.questions]
    _write_csv(output_dir / "questions.csv", QUESTION_FIELDS, questions)
    (output_dir / "rows.json").write_text(
        json.dumps([d.as_json() for d in decisions], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--products", type=Path, default=PRODUCTS_PATH, help="the supplier sheet (CSV)"
    )
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rules = load_rules()
    products = load_products(args.products)
    with make_client() as client:
        decisions, calls = stop_on_refusal(lambda: run(client, rules, products, output))
    for d in decisions:
        _, path = d.category()
        leaf = path.split(" > ")[-1] if path else "-"
        print(f"{d.product.sku:<8} {d.status:<13} {leaf:<36} {' | '.join(d.reasons) or '-'}")
    counts = {s: sum(d.status == s for d in decisions) for s in (CHECKS_PASSED, REVIEW, IN_PIM)}
    print(f"\n{len(decisions)} rows: " + ", ".join(f"{n} {s}" for s, n in counts.items()))
    print(
        f"{calls.total} SIE calls: "
        + ", ".join(f"{n} to {model}" for model, n in calls.count.items())
    )
    print("Proposals only: the specialist approves or corrects every row before import.")
    print(f"outputs: {output / 'catalog.csv'}, review.csv, questions.csv, rows.json")


if __name__ == "__main__":
    main()
