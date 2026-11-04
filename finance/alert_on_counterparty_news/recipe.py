"""Alert the right credit officer when untagged trade press reports trouble at a borrower.

Use case:  Credit early warning: is this story about one of our borrowers, and is it a warning?
Who:       The credit analyst who reads the bank's news queue every morning, and the credit
           officers who own each borrower.
Problem:   Regional and trade press arrives without company tags, so nobody's feed says which
           story is about which private borrower. "Halden" is a borrower and a town, two
           borrowers are called "Northwind", and "Halden opens a depot" and "Halden misses a
           payment" name the same company with very different meanings.
Input:     data/news.jsonl (the news queue: source, URL, time, headline, body; synthetic) and
           the counterparty list (counterparties.py over data/counterparties.csv: legal name,
           aliases, registration number, parent ticker, relationship manager, credit officer).
           Labels, events, thresholds and model IDs are in rules.yaml.
SIE pipeline (two models):
           (Python)  split headline and body into sentence windows
           extract   numind/NuNER_Zero          company, ticker and place spans, all windows in
                                                batches of 16
           (Python)  add "(NYSE: NWEG)" tickers and counterparty names the model missed; link
                     each name to the list: a ticker next to a name must be its parent's, then
                     exact legal name, then an alias; shared aliases, longer names and
                     name/ticker conflicts go to review
           extract   fastino/GLiNER2.5-Decide   called once per linked borrower per article:
                                                the event in the sentences around it
           (Python)  a warning event over its limit is an alert; a confident `none` is filed;
                     anything in between is review
Output:    runs/<timestamp>/: alerts/<credit officer>.md (one message per credit officer),
           filed.csv (links saved to the credit file), review.csv (the analyst's queue) and
           articles.json (every article with its entities, links, events and reasons).
           The recipe changes no rating and contacts nobody outside the bank: the credit
           officer decides what an alert means for the loan.
Why SIE:   Two small encoder models and no generative model making the decision. The event list
           is plain words in rules.yaml and the borrower list is data, so neither needs
           training. Every link comes from a lookup you can read here.
Models on: SIE Serverless, or your own SIE with SIE_BASE_URL.

    uv run recipe.py
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import yaml
from counterparties import Counterparty, CounterpartyList
from dotenv import load_dotenv
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
NEWS_PATH = DATA_DIR / "news.jsonl"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
# How much of an SIE error message goes into a per-article reason or the summary line.
ERROR_CHARS = 200

COMPANY, TICKER, LOCATION = "company", "ticker", "location"
QUESTION_ID = "event"
# What the checks found, never a decision: the credit officer decides what an alert means.
ALERT = "alert"  # a linked borrower and a warning event over its limit
FILED = "filed"  # a linked borrower and a confident "no warning"
REVIEW = "review"  # a name that fits two borrowers, a conflict, an unclear event, a failed call
NOT_A_BORROWER = "not_a_borrower"  # no company in the article is on the counterparty list
STATUSES = (ALERT, FILED, REVIEW, NOT_A_BORROWER)
# How one name in the article came out.
LINKED, PLACE = "linked", "place"
FILED_FIELDS = (
    "counterparty_id",
    "legal_name",
    "article_id",
    "published",
    "source",
    "url",
    "headline",
    "top_warning",
    "top_warning_score",
    "none_score",
    "reason",
)
REVIEW_FIELDS = (
    "article_id",
    "published",
    "source",
    "url",
    "headline",
    "mention",
    "candidates",
    "reason",
)
# A cell starting with one of these runs as a formula in a spreadsheet.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


# --- Inputs -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Article:
    id: str
    source: str
    url: str
    published: str
    headline: str
    body: str

    @property
    def text(self) -> str:
        """What the recipe reads: the headline, a blank line, the body. Offsets are into this."""
        return f"{self.headline}\n\n{self.body}"


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def load_articles(path: Path = NEWS_PATH) -> list[Article]:
    lines = path.read_text(encoding="utf-8").splitlines()
    articles = [Article(**json.loads(line)) for line in lines if line.strip()]
    ids = [article.id for article in articles]
    if len(set(ids)) != len(ids):
        raise ValueError(f"article ids repeat: {sorted({i for i in ids if ids.count(i) > 1})}")
    return articles


def make_client() -> SIEClient:
    """Build an SIE client from the API key and SIE_BASE_URL in the environment or `.env`."""
    load_dotenv()
    api_key = os.environ.get("SIE_API_KEY", "").strip()
    base_url = os.environ.get("SIE_BASE_URL", "").strip()
    if not api_key and not base_url:
        raise RuntimeError("Set SIE_API_KEY, or SIE_BASE_URL for your own SIE, in .env")
    base_url = base_url or "https://api.superlinked.com"
    return SIEClient(api_key=api_key or None, base_url=base_url, timeout_s=WAIT_S)


# --- 1. Sentences and windows -----------------------------------------------------------------


def sentences(article: Article, rules: dict[str, Any]) -> list[tuple[int, int]]:
    """(start, end) of each sentence in `article.text`: the headline is one, then the body's.

    A full stop after a word in `no_split_after` ("Ltd.", "Inc.") doesn't end a sentence, so a
    legal name stays in one piece.
    """
    keep = {word.casefold() for word in rules["no_split_after"]}
    spans = [(0, len(article.headline))] if article.headline.strip() else []
    offset = len(article.headline) + 2
    body = article.body
    start = None
    for m in re.finditer(r"\S+", body):
        if start is None:
            start = m.start()
        word = m.group()
        if re.search(r"[.!?][\"')\]]*$", word) and word.rstrip(".!?\"')]").casefold() not in keep:
            spans.append((offset + start, offset + m.end()))
            start = None
    if start is not None:
        spans.append((offset + start, offset + len(body.rstrip())))
    return spans


def windows(spans: list[tuple[int, int]], text: str, max_words: int) -> list[tuple[int, str]]:
    """(offset, window) pairs: whole sentences packed up to `max_words` words.

    NuNER Zero reads 384 tokens. Cutting on sentence ends keeps every name whole; a single
    sentence longer than the limit is cut on words.
    """
    pieces: list[tuple[int, int]] = []
    for start, end in spans:
        words = list(re.finditer(r"\S+", text[start:end]))
        for i in range(0, len(words), max_words):
            chunk = words[i : i + max_words]
            pieces.append((start + chunk[0].start(), start + chunk[-1].end()))
    packed: list[tuple[int, int]] = []
    for start, end in pieces:
        if packed and len(text[packed[-1][0] : end].split()) <= max_words:
            packed[-1] = (packed[-1][0], end)
        else:
            packed.append((start, end))
    return [(start, text[start:end]) for start, end in packed]


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
    those articles go to review."""
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
    """The error for a per-article reason or the summary line, its message cut short."""
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
        """No call succeeded, so no article was read: stop instead of writing a half answer."""
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
    a rate limit, a reply without the key we asked for) is logged and its articles go to
    review; the run continues. A refusal every call would repeat (see `is_fatal`) stops the
    run. An error that isn't an SIE error is a bug in this code and is raised as is.
    """
    calls.made += 1
    try:
        return work(), ""
    except SIEError as error:
        if is_fatal(error):
            raise SIEStopped(stop_message(error, model)) from error
        calls.failures.append((model, error))
        return None, f"the SIE call failed: {short(error)}"


def jsonable(value: Any) -> Any:
    """Turn SDK results (numpy arrays, nested dicts and lists) into plain JSON values."""
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, dict):
        return {str(key): jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(child) for child in value]
    return value


# --- 2. Find names: NuNER Zero, then patterns and the counterparty list ----------------------


@dataclass
class Entity:
    label: str  # company, ticker or location
    text: str
    start: int
    end: int
    score: float | None
    source: str  # "model", "pattern" (a "(NYSE: NWEG)" ticker) or "list" (a counterparty name)
    exchange: str = ""  # for a pattern ticker: the exchange written before it


def read_entities(reply: Any, count: int) -> list[list[dict[str, Any]]]:
    """The `entities` list of each window from one NuNER Zero reply, or an SIEError.

    A reply that isn't one result per window, or a result without an `entities` list, is a
    failed call (the transient path). An empty list is a real answer: nothing found.
    """
    results = reply if isinstance(reply, list) else [reply]
    if len(results) != count:
        raise SIEError(f"the reply has {len(results)} results for {count} windows")
    found: list[list[dict[str, Any]]] = []
    for result in results:
        entities = result.get("entities") if isinstance(result, dict) else None
        if not isinstance(entities, list):
            error = result.get("error") if isinstance(result, dict) else None
            raise SIEError(
                "the reply has no `entities` list" + (f" (item error: {error})" if error else "")
            )
        found.append([e for e in entities if isinstance(e, dict)])
    return found


def tag_windows(
    client: SIEClient, calls: CallLog, items: list[tuple[str, str]], rules: dict[str, Any]
) -> dict[str, list[dict[str, Any]] | str]:
    """NuNER Zero spans per window id, in batches; a window whose call failed gets the reason."""
    model, labels = rules["models"]["entities"], list(rules["labels"])
    # A window too long to read whole fails instead of being cut short.
    options = {"threshold": rules["thresholds"]["min_entity_score"], "overflow_policy": "error"}
    size = rules["thresholds"]["batch_size"]
    results: dict[str, list[dict[str, Any]] | str] = {}
    for start in range(0, len(items), size):
        batch = items[start : start + size]

        def call(batch: list[tuple[str, str]] = batch) -> list[list[dict[str, Any]]]:
            reply = client.extract(
                model,
                [Item(id=item_id, text=text) for item_id, text in batch],
                labels=labels,
                options=options,
                provision_timeout_s=WAIT_S,
            )
            return read_entities(jsonable(reply), len(batch))

        found, failed = guarded(calls, model, call)
        for number, (item_id, _) in enumerate(batch):
            results[item_id] = found[number] if found is not None else failed
    return results


def model_entities(
    window: str, offset: int, spans: list[dict[str, Any]], rules: dict[str, Any]
) -> tuple[list[Entity], list[str]]:
    """Keep the valid model spans, in article offsets, plus a note for each one dropped.

    A span must be one of our labels and its text must be exactly the window text at its
    offsets. NuNER Zero sometimes tags the exchange in "(NYSE: NWEG)" as a ticker; an exchange
    name is not a ticker, so it is dropped.
    """
    labels, exchanges = set(rules["labels"]), {e.upper() for e in rules["exchanges"]}
    entities: list[Entity] = []
    notes: list[str] = []
    for raw in spans:
        start, end, text = raw.get("start"), raw.get("end"), str(raw.get("text", ""))
        label, score = str(raw.get("label", "")), raw.get("score")
        if (
            label not in labels
            or not isinstance(start, int)
            or not isinstance(end, int)
            or window[start:end] != text
            or not text.strip()
        ):
            notes.append(f"invalid span from the model dropped: {raw!r}")
            continue
        if label == TICKER and text.strip().upper() in exchanges:
            continue
        number = float(score) if isinstance(score, (int, float)) else None
        entities.append(Entity(label, text, offset + start, offset + end, number, "model"))
    return entities, notes


def pattern_tickers(text: str, rules: dict[str, Any]) -> list[Entity]:
    """Tickers written as "(EXCHANGE: SYMBOL)": a fact in the text, found without a model."""
    exchanges = "|".join(re.escape(e) for e in rules["exchanges"])
    pattern = rf"\(\s*({exchanges})\s*:\s*([A-Z][A-Z0-9.]{{0,5}})\s*\)"
    return [
        Entity(TICKER, m.group(2), m.start(2), m.end(2), None, "pattern", m.group(1))
        for m in re.finditer(pattern, text)
    ]


def list_mentions(text: str, counterparties: list[Counterparty]) -> list[Entity]:
    """Every counterparty legal name or alias written in the text, ignoring case, whole words.

    The model can miss a name it has never seen. A name already on the list needs no model to
    find it; whether it means the company here is checked later. Longer names win, so
    "Northwind Energy" is not also read as "Northwind". Case is ignored so an all-caps
    headline ("PELLRIDGE FOODS LTD MISSES ...") still names the borrower; the entity keeps the
    text as written.
    """
    names = sorted({n for cp in counterparties for n in cp.names}, key=len, reverse=True)
    found: list[Entity] = []
    for name in names:
        for m in re.finditer(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.IGNORECASE):
            if not any(m.start() < e.end and e.start < m.end() for e in found):
                found.append(Entity(COMPANY, m.group(0), m.start(), m.end(), None, "list"))
    return sorted(found, key=lambda e: e.start)


def _overlaps(a: Entity, b: Entity) -> bool:
    return a.start < b.end and b.start < a.end


def _inside(inner: Entity, outer: Entity) -> bool:
    return outer.start <= inner.start and inner.end <= outer.end


@dataclass
class Mention:
    """A counterparty name found in the article, and what the entity model made of it."""

    entity: Entity
    reading: str  # "company" (the model tagged it), "place", "unread" or "longer"
    longer: str = ""  # for "longer": the model's longer company name that contains this one


def read_mentions(
    text: str,
    model: list[Entity],
    patterns: list[Entity],
    listed: list[Entity],
    rules: dict[str, Any],
) -> tuple[list[Mention], list[Entity], list[Entity]]:
    """Each counterparty name, how the model read it; the tickers; the other companies.

    - A model company span that covers a list name confirms it ("company"). When the span is
      longer, the extra words must be legal suffixes ("Pellridge Foods Ltd."); any other word
      makes it a different company that contains the name ("Northwind Energy Services"), and
      that goes to review. A span holding two list names ("Halden and Pellridge") is checked
      name by name: the words before the first name and after each name up to the next one;
      a `name_joining_words` word ("and") is allowed only between two names.
    - A model location span over a list name makes it a place ("Halden" the town).
    - A list name the model didn't tag at all is "unread": it may be the company or not.
    - Model company spans with no list name in them are other companies, not borrowers.
    """
    suffixes = {s.casefold() for s in rules["legal_suffixes"]}
    joiners = {s.casefold() for s in rules["name_joining_words"]}
    tickers = list(patterns) + [
        e for e in model if e.label == TICKER and not any(_overlaps(e, p) for p in patterns)
    ]
    spans = [e for e in model if e.label != TICKER and not any(_overlaps(e, p) for p in patterns)]
    companies = [e for e in spans if e.label == COMPANY]
    places = [e for e in spans if e.label == LOCATION]
    mentions: list[Mention] = []
    for name in listed:
        covering = [m for m in companies if _overlaps(m, name)]
        longer = [
            m for m in covering if _inside(name, m) and (m.start, m.end) != (name.start, name.end)
        ]
        if longer:
            outer = longer[0]
            inner = sorted((n for n in listed if _inside(n, outer)), key=lambda n: n.start)
            at = next(i for i, n in enumerate(inner) if n.start == name.start)
            after_end = inner[at + 1].start if at + 1 < len(inner) else outer.end
            own = text[name.end : after_end]
            if at == 0:
                own = text[outer.start : name.start] + " " + own
            allowed = suffixes | (joiners if at + 1 < len(inner) else set())
            extra = [w for w in re.split(r"[^\w]+", re.sub(r"['\u2019]s\b", "", own)) if w]
            if any(w.casefold() not in allowed for w in extra):
                mentions.append(Mention(name, "longer", outer.text))
                continue
            mentions.append(Mention(name, COMPANY))
        elif covering:
            mentions.append(Mention(name, COMPANY))
        elif any(_overlaps(p, name) for p in places):
            mentions.append(Mention(name, PLACE))
        else:
            mentions.append(Mention(name, "unread"))
    others = [m for m in companies if not any(_overlaps(m, n) for n in listed)]
    return mentions, sorted(tickers, key=lambda e: e.start), others


# --- 3. Link names to the counterparty list --------------------------------------------------


@dataclass
class Link:
    article_id: str
    mention: str
    start: int
    end: int
    status: str  # linked, review or place
    counterparty_id: str = ""
    legal_name: str = ""
    method: str = ""  # ticker, legal name, alias, or article (settled by another mention)
    candidates: str = ""
    reason: str = ""

    def resolve(self, cp: Counterparty, method: str, reason: str) -> Link:
        self.status, self.method, self.reason = LINKED, method, reason
        self.counterparty_id, self.legal_name = cp.id, cp.legal_name
        return self


def _names(cps: list[Counterparty]) -> str:
    return " and ".join(f"{cp.id} ({cp.legal_name})" for cp in cps)


def paired_ticker(name: Entity, tickers: list[Entity], text: str, max_gap: int) -> Entity | None:
    """The ticker written right after a name, as in "Northwind (NYSE: NWEG)"."""
    for ticker in tickers:
        gap = text[name.end : ticker.start]
        if 0 <= len(gap) <= max_gap and re.fullmatch(r"\s*\(\s*(?:[A-Z]+\s*:\s*)?", gap):
            return ticker
    return None


def link_by_ticker(
    link: Link, candidates: list[Counterparty], ticker: Entity, cps: CounterpartyList
) -> Link:
    """A name with a ticker after it: the ticker must be the parent ticker of that name."""
    symbol = ticker.text.strip().upper()
    written = f"{ticker.exchange}: {symbol}" if ticker.exchange else symbol
    owners = cps.find_by_parent_ticker(symbol)
    match = [cp for cp in candidates if cp in owners]
    link.candidates = _names(candidates)
    if len(match) == 1:
        listed = match[0].parent_ticker
        if ticker.exchange and listed.split(":", 1)[0] != ticker.exchange.upper():
            link.reason = (
                f"ticker written as {written}, but the list has {listed} for {_names(match)}"
            )
            return link
        return link.resolve(match[0], TICKER, f"ticker {written} is the parent ticker on the list")
    if owners:
        link.reason = (
            f"name and ticker disagree: {written} is the parent ticker of {_names(owners)}, "
            f"but {link.mention!r} names {_names(candidates)}"
        )
    else:
        link.reason = (
            f"name and ticker disagree: {written} is not a parent ticker on the list, "
            f"but {link.mention!r} names {_names(candidates)}"
        )
    return link


def link_article(
    article: Article,
    mentions: list[Mention],
    tickers: list[Entity],
    others: list[Entity],
    cps: CounterpartyList,
    rules: dict[str, Any],
) -> list[Link]:
    """One link per counterparty name, plus one per borrower ticker no name claimed.

    In order: a ticker written next to a name must be that name's parent ticker; then an exact
    legal name; then an alias only one borrower has. A name the entity model read as a place
    is not linked. A shared alias ("Northwind") is settled only when the article links exactly
    one of its borrowers elsewhere, by ticker, legal name or an alias of its own. A ticker that
    disagrees with the name it follows puts every link to that borrower in the article in
    review, and is never settled by another mention.
    """
    text = article.text
    max_gap = rules["thresholds"]["ticker_pair_max_gap_chars"]
    claimed: set[int] = set()
    disputed: set[str] = set()
    links: list[Link] = []
    shared: list[tuple[Link, list[Counterparty]]] = []
    for mention in mentions:
        name = mention.entity
        link = Link(article.id, name.text, name.start, name.end, REVIEW)
        legal, alias = cps.find_by_legal_name(name.text), cps.find_by_alias(name.text)
        candidates = legal or alias
        link.candidates = _names(candidates)
        links.append(link)
        if mention.reading == PLACE:
            link.status = PLACE
            link.reason = "the entity model read this as a place, not a company"
            continue
        if mention.reading == "longer":
            link.reason = (
                f"{mention.longer!r} is a different name that contains the borrower name "
                f"{name.text!r}; check whether it is the same company"
            )
            disputed.update(cp.id for cp in candidates)
            continue
        ticker = paired_ticker(name, tickers, text, max_gap)
        if ticker is not None:
            claimed.add(id(ticker))
            if link_by_ticker(link, candidates, ticker, cps).status == REVIEW:
                disputed.update(cp.id for cp in candidates)
        elif len(candidates) == 1:
            read = "read as a company" if mention.reading == COMPANY else "not tagged by the model"
            how = "legal name" if legal else "alias"
            link.resolve(candidates[0], how, f"{how} of {candidates[0].legal_name} ({read})")
        else:
            shared.append((link, candidates))
    for other in others:
        ticker = paired_ticker(other, tickers, text, max_gap)
        if ticker is not None:
            claimed.add(id(ticker))
            owners = cps.find_by_parent_ticker(ticker.text)
            if owners:
                link = Link(article.id, other.text, other.start, other.end, REVIEW)
                link.candidates = _names(owners)
                link.reason = (
                    f"name and ticker disagree: {ticker.text} is the parent ticker of "
                    f"{_names(owners)}, but {other.text!r} is not on the list"
                )
                links.append(link)
                disputed.update(cp.id for cp in owners)
    for ticker in tickers:
        if id(ticker) in claimed:
            continue
        owners = cps.find_by_parent_ticker(ticker.text)
        if not owners:
            continue
        link = Link(article.id, ticker.text, ticker.start, ticker.end, REVIEW)
        link.candidates = _names(owners)
        listed = cps.find_by_parent_ticker(ticker.text, ticker.exchange)
        if len(owners) == 1 and listed:
            link.resolve(owners[0], TICKER, "parent ticker on its own")
        elif not listed:
            link.reason = (
                f"ticker written as {ticker.exchange}: {ticker.text}, but the list has "
                f"{owners[0].parent_ticker} for {_names(owners)}"
            )
        else:
            link.reason = f"ticker {ticker.text} is the parent ticker of {_names(owners)}"
        links.append(link)
    for link in links:
        if link.status == LINKED and link.counterparty_id in disputed:
            link.status = REVIEW
            link.reason = (
                f"{link.reason}, but another mention of {link.legal_name} in this article has a "
                "name or ticker that disagrees"
            )
    firm = {link.counterparty_id for link in links if link.status == LINKED}
    for link, candidates in shared:
        settled = [cp for cp in candidates if cp.id in firm]
        if len(settled) == 1 and not any(cp.id in disputed for cp in candidates):
            link.resolve(
                settled[0],
                "article",
                f"{link.mention!r} is shared by {_names(candidates)}; this article links "
                f"{settled[0].legal_name} elsewhere and none of the others",
            )
        else:
            link.reason = f"{link.mention!r} could be {_names(candidates)}"
    return sorted(links, key=lambda link: link.start)


# --- 4. The event around each linked borrower -------------------------------------------------


def context(
    text: str,
    spans: list[tuple[int, int]],
    own: list[Link],
    names: list[tuple[int, int, str]],
    counterparty_id: str,
    after: int,
) -> str:
    """The sentences that name the borrower, plus up to `after` sentences after each one.

    A following sentence is added only if it names no other company (another borrower, a
    company not on the list, or a name still in review), so a warning about a competitor in
    the same story isn't read as the borrower's.
    """

    def index(position: int) -> int:
        return next(i for i, (s, e) in enumerate(spans) if s <= position < e)

    def names_other(i: int) -> bool:
        s, e = spans[i]
        return any(s <= start < e and owner != counterparty_id for start, _, owner in names)

    picked: set[int] = set()
    for link in own:
        i = index(link.start)
        picked.add(i)
        for j in range(i + 1, min(i + 1 + after, len(spans))):
            if names_other(j):
                break
            picked.add(j)
    parts = [text[spans[i][0] : spans[i][1]].strip() for i in sorted(picked)]
    # The headline has no full stop; give it one so it reads as its own sentence.
    return " ".join(p if re.search(r"[.!?][\"')\]]*$", p) else f"{p}." for p in parts)


def event_schema(name: str, rules: dict[str, Any]) -> dict[str, Any]:
    """The event list as one GLiNER2.5-Decide choice question about the borrower `name`."""
    options = {key: str(text).replace("{name}", name) for key, text in rules["events"].items()}
    question = str(rules["question"]).replace("{name}", name)
    return {QUESTION_ID: {"type": "choice", "instructions": question, "criteria": options}}


def read_probabilities(reply: Any, options: list[str]) -> dict[str, float]:
    """The probability per event from a reply, or an SIEError saying what was wrong with it.

    A reply without `data.event.probabilities`, with an event missing or one we didn't ask
    about, or with a value that isn't a number from 0 to 1, is a failed call: it takes the
    transient path, and the borrower goes to review.
    """
    answer = reply.get("data", {}).get(QUESTION_ID) if isinstance(reply, dict) else None
    probabilities = answer.get("probabilities") if isinstance(answer, dict) else None
    if not isinstance(probabilities, dict):
        error = reply.get("error") if isinstance(reply, dict) else None
        raise SIEError(
            f"the reply has no `data.{QUESTION_ID}.probabilities`"
            + (f" (item error: {error})" if error else "")
        )
    missing = sorted(set(options) - set(probabilities))
    extra = sorted(set(probabilities) - set(options))
    if missing or extra:
        raise SIEError(
            f"the reply's events don't match rules.yaml: missing {missing}, extra {extra}"
        )
    values: dict[str, float] = {}
    for option, value in probabilities.items():
        number = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        if number is None or not 0.0 <= number <= 1.0:
            raise SIEError(
                f"the reply's probability for {option} is not a number from 0 to 1: {value!r}"
            )
        values[str(option)] = float(number)
    return values


def ask_event(
    client: SIEClient, calls: CallLog, item_id: str, text: str, name: str, rules: dict[str, Any]
) -> tuple[dict[str, float] | None, str]:
    """One GLiNER2.5-Decide call for one borrower in one article: a probability per event."""
    model = rules["models"]["events"]

    def call() -> dict[str, float]:
        reply = client.extract(
            model,
            Item(id=item_id, text=text),
            output_schema=event_schema(name, rules),
            options={"overflow_policy": "error"},  # a text too long to read whole fails
            provision_timeout_s=WAIT_S,
        )
        return read_probabilities(jsonable(reply), list(rules["events"]))

    return guarded(calls, model, call)


@dataclass
class Event:
    """What the event check found for one linked borrower in one article."""

    counterparty_id: str
    legal_name: str
    name_in_article: str
    status: str  # alert, filed or review
    event: str = ""
    score: float | None = None
    top_warning: str = ""
    top_warning_score: float | None = None
    none_score: float | None = None
    model_read: str = ""
    reason: str = ""


def judge_event(event: Event, probabilities: dict[str, float], rules: dict[str, Any]) -> Event:
    """alert, filed or review from the event probabilities.

    An alert needs a warning event at or over `min_event_score` that is also the top answer.
    Filed needs positive evidence of no warning: `none` at or over `min_none_score` and every
    warning under `max_warning_when_filed`. Anything in between is review, with both numbers.
    """
    limits = rules["thresholds"]
    no_event = rules["no_event"]
    warnings = {k: v for k, v in probabilities.items() if k != no_event}
    top, score = max(warnings.items(), key=lambda item: item[1])
    none = probabilities[no_event]
    event.top_warning, event.top_warning_score, event.none_score = top, score, none
    if score >= limits["min_event_score"] and score >= none:
        event.status, event.event, event.score = ALERT, top, score
        event.reason = f"{top} {score:.2f} (none {none:.2f})"
    elif none >= limits["min_none_score"] and score < limits["max_warning_when_filed"]:
        event.status, event.event, event.score = FILED, no_event, none
        event.reason = f"no warning: none {none:.2f}, top warning {top} {score:.2f}"
    else:
        event.status = REVIEW
        event.reason = (
            f"event unclear: top warning {top} {score:.2f}, none {none:.2f} "
            f"(alert needs {limits['min_event_score']:.2f}; filed needs none "
            f"{limits['min_none_score']:.2f} and every warning under "
            f"{limits['max_warning_when_filed']:.2f})"
        )
    return event


# --- Run --------------------------------------------------------------------------------------


@dataclass
class Result:
    article: Article
    status: str
    entities: list[Entity]
    links: list[Link]
    events: list[Event]
    reasons: list[str] = field(default_factory=list)  # article-level: failed windows, bad spans


def check_article(
    client: SIEClient,
    calls: CallLog,
    article: Article,
    spans: list[tuple[int, int]],
    tagged: list[list[dict[str, Any]] | str],
    cps: CounterpartyList,
    rules: dict[str, Any],
) -> Result:
    """Names, links and events for one article, from its windows' NuNER Zero results."""
    text = article.text
    max_words = rules["thresholds"]["max_words_per_window"]
    found: list[Entity] = []
    reasons: list[str] = []
    for number, ((offset, window), result) in enumerate(
        zip(windows(spans, text, max_words), tagged, strict=True), start=1
    ):
        if isinstance(result, str):
            reasons.append(f"window {number} was not read by the entity model: {result}")
            continue
        entities, notes = model_entities(window, offset, result, rules)
        found += entities
        reasons += notes
    patterns = pattern_tickers(text, rules)
    listed = list_mentions(text, cps.all())
    mentions, tickers, others = read_mentions(text, found, patterns, listed, rules)
    links = link_article(article, mentions, tickers, others, cps, rules)
    if any(isinstance(result, str) for result in tagged):
        # Without the entity model, an alias can't be told from a town of the same name.
        for link in links:
            if link.status == LINKED and link.method in ("alias", "article"):
                link.status = REVIEW
                link.reason += "; the entity model didn't read this article, so it isn't linked"
    names = [(link.start, link.end, link.counterparty_id) for link in links if link.status != PLACE]
    names += [(o.start, o.end, "") for o in others]
    events: list[Event] = []
    for cp_id in dict.fromkeys(link.counterparty_id for link in links if link.status == LINKED):
        own = [link for link in links if link.status == LINKED and link.counterparty_id == cp_id]
        cp = cps.get(cp_id)
        event = Event(cp_id, cp.legal_name, own[0].mention, REVIEW)
        after = rules["thresholds"]["context_sentences_after"]
        event.model_read = context(text, spans, own, names, cp_id, after)
        item_id = f"{article.id}#{cp_id}"
        probabilities, failed = ask_event(
            client, calls, item_id, event.model_read, event.name_in_article, rules
        )
        if probabilities is None:
            event.reason = f"event model: {failed}"
        else:
            judge_event(event, probabilities, rules)
        events.append(event)
    entities = sorted(found + patterns + listed, key=lambda e: (e.start, e.end))
    return Result(article, article_status(links, events, reasons), entities, links, events, reasons)


def article_status(links: list[Link], events: list[Event], reasons: list[str]) -> str:
    """The article's status: alert if any borrower has one, then review, then filed.

    An alert still goes out when another name in the same article is in review; the review
    item is queued as well.
    """
    found = {event.status for event in events}
    if any(link.status == REVIEW for link in links) or reasons:
        found.add(REVIEW)
    return next((status for status in (ALERT, REVIEW, FILED) if status in found), NOT_A_BORROWER)


def run(
    client: SIEClient,
    rules: dict[str, Any],
    output_dir: Path | None,
    news_path: Path = NEWS_PATH,
    data_dir: Path = DATA_DIR,
) -> list[Result]:
    """Check every article in `news_path`.

    Nothing is written until every article is done, so a run that stops on an SIE refusal or
    on every call failing leaves no partial outputs behind.
    """
    articles = load_articles(news_path)
    if not articles:
        raise SystemExit(f"No articles in {news_path}, so there is nothing to check.")
    cps = CounterpartyList(data_dir)
    calls = CallLog()
    max_words = rules["thresholds"]["max_words_per_window"]
    spans = {article.id: sentences(article, rules) for article in articles}
    parts = {a.id: windows(spans[a.id], a.text, max_words) for a in articles}
    items = [
        (f"{article_id}#w{number}", window)
        for article_id, pieces in parts.items()
        for number, (_, window) in enumerate(pieces, start=1)
    ]
    tagged = tag_windows(client, calls, items, rules)
    results = [
        check_article(
            client,
            calls,
            article,
            spans[article.id],
            [tagged[f"{article.id}#w{n}"] for n in range(1, len(parts[article.id]) + 1)],
            cps,
            rules,
        )
        for article in articles
    ]
    calls.stop_if_all_failed()
    if calls.failures:
        print(
            f"{len(calls.failures)} of {calls.made} SIE calls failed; their articles go to "
            f"{REVIEW}. First: {short(calls.failures[0][1])}"
        )
    if output_dir is not None:
        write_outputs(output_dir, results, cps)
    return results


# --- Outputs ----------------------------------------------------------------------------------


def csv_safe(value: Any) -> str:
    """A leading ' on a cell a spreadsheet would run as a formula; anything else as written."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "unknown"


def alert_messages(results: list[Result], cps: CounterpartyList) -> dict[str, str]:
    """One message per credit officer who has alerts: borrower, event, sentences, link."""
    by_officer: dict[str, list[tuple[Result, Event, Counterparty]]] = {}
    for result in results:
        for event in result.events:
            if event.status == ALERT:
                cp = cps.get(event.counterparty_id)
                by_officer.setdefault(cp.credit_officer, []).append((result, event, cp))
    messages: dict[str, str] = {}
    for officer, items in by_officer.items():
        noun = "story" if len(items) == 1 else "stories"
        lines = [
            f"To: {officer} (credit officer)",
            f"Subject: Early warning: {len(items)} news {noun} on your borrowers",
            "",
            "The news check linked these stories to your borrowers and found a warning event.",
            "Nothing has been changed on the loan file; what it means for the loan is your call.",
        ]
        for number, (result, event, cp) in enumerate(items, start=1):
            article = result.article
            lines += [
                "",
                f"{number}. {cp.legal_name} ({cp.id}, reg. {cp.registration_number}): "
                f"{event.event.replace('_', ' ')} ({event.score:.2f})",
                f"   Relationship manager: {cp.relationship_manager}",
                f"   {article.source}, {article.published}: {article.headline}",
                f"   {article.url}",
                f'   What the model read: "{event.model_read}"',
            ]
        messages[officer] = "\n".join(lines) + "\n"
    return messages


def filed_rows(results: list[Result]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in results:
        a = result.article
        for e in result.events:
            if e.status == FILED:
                rows.append(
                    {
                        "counterparty_id": e.counterparty_id,
                        "legal_name": e.legal_name,
                        "article_id": a.id,
                        "published": a.published,
                        "source": a.source,
                        "url": a.url,
                        "headline": a.headline,
                        "top_warning": e.top_warning,
                        "top_warning_score": f"{e.top_warning_score:.3f}",
                        "none_score": f"{e.none_score:.3f}",
                        "reason": e.reason,
                    }
                )
    return rows


def review_rows(results: list[Result]) -> list[dict[str, Any]]:
    """The analyst's queue: one row per article, name and reason (repeats of a name collapse)."""
    rows: list[dict[str, Any]] = []
    for result in results:
        a = result.article
        base = {
            "article_id": a.id,
            "published": a.published,
            "source": a.source,
            "url": a.url,
            "headline": a.headline,
        }
        rows += [{**base, "mention": "", "candidates": "", "reason": r} for r in result.reasons]
        rows += [
            {**base, "mention": link.mention, "candidates": link.candidates, "reason": link.reason}
            for link in result.links
            if link.status == REVIEW
        ]
        rows += [
            {
                **base,
                "mention": e.name_in_article,
                "candidates": f"{e.counterparty_id} ({e.legal_name})",
                "reason": e.reason,
            }
            for e in result.events
            if e.status == REVIEW
        ]
    unique = {tuple(row[key] for key in REVIEW_FIELDS): row for row in rows}
    return list(unique.values())


def write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: csv_safe(row[key]) for key in fields} for row in rows)


def write_outputs(output_dir: Path, results: list[Result], cps: CounterpartyList) -> None:
    (output_dir / "alerts").mkdir(parents=True, exist_ok=True)
    for officer, message in alert_messages(results, cps).items():
        (output_dir / "alerts" / f"{slug(officer)}.md").write_text(message, encoding="utf-8")
    write_csv(output_dir / "filed.csv", FILED_FIELDS, filed_rows(results))
    write_csv(output_dir / "review.csv", REVIEW_FIELDS, review_rows(results))
    payload = [
        {
            "id": r.article.id,
            "headline": r.article.headline,
            "url": r.article.url,
            "status": r.status,
            "events": [asdict(e) for e in r.events],
            "links": [asdict(link) for link in r.links],
            "entities": [asdict(e) for e in r.entities],
            "reasons": r.reasons,
        }
        for r in results
    ]
    (output_dir / "articles.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rules = load_rules()
    with make_client() as client:
        results = stop_on_refusal(lambda: run(client, rules, output))
    for r in results:
        detail = "; ".join(
            dict.fromkeys(
                [f"{e.legal_name}: {e.status} {e.reason}" for e in r.events]
                + [f"{link.mention}: {link.reason}" for link in r.links if link.status == REVIEW]
                + r.reasons
            )
        )
        print(f"{r.article.id}  {r.status:<15} {detail or '-'}")
    counts = ", ".join(f"{sum(r.status == s for r in results)} {s}" for s in STATUSES)
    print(f"\n{len(results)} articles: {counts}")
    print("No rating or loan was changed: the credit officer decides what an alert means.")
    print(f"outputs: {output}")


if __name__ == "__main__":
    main()
