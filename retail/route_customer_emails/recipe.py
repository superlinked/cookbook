"""Suggest a care team for each customer email, with safety and legal escalated first.

Use case:  Customer email routing: which team answers this email?
Who:       Retail customer care teams working a shared support inbox.
Problem:   Customer emails arrive in one shared inbox. Someone reads each one, picks a team,
           and copies the order number and store into the ticket. An email about an injury
           or a threat to sue can sit in that inbox behind fifty "where is my order" emails.
Input:     data/emails.json (sender, subject, body and, for a reply, the email it answers),
           data/routing_catalog.yaml (care teams with one-line descriptions, stores with
           aliases) and rules.yaml.
SIE pipeline:
           (Python)                          safety and legal phrases escalate first; every
                                               escalation is written to escalations.csv
                                               before any model call
           extract  fastino/gliner2-large-v1   order number and store spans in every email
           extract  fastino/GLiNER2.5-Decide   only for emails not escalated: pick one team
                                               from the team descriptions, with a probability
                                               for every team
           (Python)                          order number pattern, store list match, and low
                                               score, low top-2 margin or a failed SIE call:
                                               general queue
Output:    escalations.csv (written first), then routes.csv and routes.json: one row per
           email with the sender, the suggested team, method (escalation | model | general), every
           escalation that matched, order numbers, store, the model's suggestion, score,
           margin, notes and the reason. A person confirms each route; nothing is assigned in
           a ticket system.
Why SIE:   Two small encoder models; no generative model makes the decision. The team list is
           the routing catalog itself: add a team with a description and the next run can
           route to it, with no training. Each model is called once per batch of windows;
           nothing to host.
Runs on:   SIE Serverless.

    uv run recipe.py
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import yaml
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
EMAILS_PATH = ROOT / "data" / "emails.json"
CATALOG_PATH = ROOT / "data" / "routing_catalog.yaml"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
# A failed SIE call's message is cut to this many characters in reasons and the summary line.
ERROR_CHARS = 200
# What a stopped run leaves behind: the escalations, written before any model call.
NOT_WRITTEN = "No routes were written; escalations.csv was written before the model calls."

QUESTION_ID = "care_team"
ESCALATION, MODEL, GENERAL = "escalation", "model", "general"
ROUTE_FIELDS = (
    "email_id",
    "from",
    "in_reply_to",
    "team",
    "method",
    "escalations",
    "also_send_to",
    "matched_phrases",
    "order_numbers",
    "store_id",
    "store_name",
    "suggested_team",
    "score",
    "margin",
    "second_team",
    "notes",
    "reason",
)


# --- Inputs -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Email:
    id: str
    sender: str  # the From address
    subject: str
    body: str
    in_reply_to: str = ""  # the email this one answers, when it is a reply in a thread

    @property
    def text(self) -> str:
        """Subject and body as one text, which is what every step reads."""
        subject = self.subject.strip().rstrip(".")
        return f"{subject}. {self.body.strip()}" if subject else self.body.strip()


@dataclass(frozen=True)
class Store:
    id: str
    name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Catalog:
    teams: dict[str, str]  # team id -> one-line description
    stores: dict[str, Store]  # store id -> store

    def store_names(self) -> dict[str, str]:
        """Casefolded store name or alias -> store id."""
        names: dict[str, str] = {}
        for store in self.stores.values():
            for name in (store.name, *store.aliases):
                key = name.casefold()
                if names.get(key, store.id) != store.id:
                    raise ValueError(f"store name {name!r} is used by two stores")
                names[key] = store.id
        return names


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def parse_catalog(raw: dict[str, Any]) -> Catalog:
    teams = {str(team): str(entry["description"]) for team, entry in raw["teams"].items()}
    stores = {
        str(store_id): Store(
            str(store_id), str(entry["name"]), tuple(str(a) for a in entry.get("aliases") or [])
        )
        for store_id, entry in (raw.get("stores") or {}).items()
    }
    catalog = Catalog(teams, stores)
    catalog.store_names()  # refuses a name shared by two stores
    return catalog


def load_catalog(path: Path = CATALOG_PATH) -> Catalog:
    return parse_catalog(yaml.safe_load(path.read_text(encoding="utf-8")))


def parse_emails(records: list[dict[str, Any]]) -> list[Email]:
    """Emails from the records. Refuses a repeated id, since a second email with the same
    id would overwrite the first one's route (and its escalation), and an id with ``#``, which
    separates an email id from its window number."""
    emails: list[Email] = []
    seen: set[str] = set()
    for record in records:
        email_id = str(record["id"])
        if email_id in seen:
            raise ValueError(f"email id {email_id!r} is used twice")
        if "#" in email_id:
            raise ValueError(f"email id {email_id!r} contains '#'")
        seen.add(email_id)
        emails.append(
            Email(
                email_id,
                str(record.get("from") or ""),
                str(record.get("subject") or ""),
                str(record.get("body") or ""),
                str(record.get("in_reply_to") or ""),
            )
        )
    return emails


def load_emails(path: Path = EMAILS_PATH) -> list[Email]:
    return parse_emails(json.loads(path.read_text(encoding="utf-8"))["emails"])


# --- Route row --------------------------------------------------------------------------------


@dataclass
class Route:
    email_id: str
    sender: str = ""
    in_reply_to: str = ""
    team: str = ""
    method: str = ""
    escalations: list[str] = field(default_factory=list)  # every escalation that matched
    also_send_to: list[str] = field(default_factory=list)  # their queues after the first
    matched_phrases: list[str] = field(default_factory=list)
    order_numbers: list[str] = field(default_factory=list)
    store_id: str = ""
    store_name: str = ""
    suggested_team: str = ""
    score: float | None = None
    margin: float | None = None
    second_team: str = ""
    notes: list[str] = field(default_factory=list)
    reason: str = ""

    def as_row(self) -> dict[str, Any]:
        row = dict(self.__dict__)
        row["from"] = row.pop("sender")  # "from" is a Python keyword, so not a field name
        for key in ("escalations", "also_send_to", "matched_phrases", "order_numbers", "notes"):
            row[key] = "; ".join(row[key])
        row["score"] = "" if self.score is None else f"{self.score:.3f}"
        row["margin"] = "" if self.margin is None else f"{self.margin:.3f}"
        return {key: row[key] for key in ROUTE_FIELDS}


# --- 1. Escalations (Python, before any model) ------------------------------------------------


def phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Match whole words in any case, with any run of spaces between them: 'caught  Fire'."""
    words = [re.escape(word) for word in phrase.split()]
    return re.compile(r"\b" + r"\s+".join(words) + r"\b", re.IGNORECASE)


def escalations(email: Email, rules: dict[str, Any]) -> list[tuple[str, str, list[str]]]:
    """(name, queue, matched phrases) for every escalation that matches, in rules order.

    Escalations run before the team model, and the model can't overrule them. An email that
    mentions a burn goes to product safety even if it is mostly about a late refund. An email
    with an injury and a lawyer matches both, and keeps both.
    """
    text = email.text
    found = []
    for rule in rules["escalations"]:
        matched = [str(p) for p in rule["phrases"] if phrase_pattern(str(p)).search(text)]
        if matched:
            found.append((str(rule["name"]), str(rule["queue"]), matched))
    return found


def screen(emails: list[Email], rules: dict[str, Any]) -> dict[str, Route]:
    """One route per email, with the escalations already filled in. No SIE call.

    The first matching escalation in rules order picks the queue; the queues of the others go
    in ``also_send_to``, so a safety case that also threatens a lawsuit reaches legal too.
    """
    routes: dict[str, Route] = {}
    for email in emails:
        route = Route(email.id, email.sender, email.in_reply_to)
        found = escalations(email, rules)
        if found:
            route.team, route.method = found[0][1], ESCALATION
            route.escalations = [name for name, _, _ in found]
            route.also_send_to = list(dict.fromkeys(q for _, q, _ in found[1:] if q != route.team))
            route.matched_phrases = [phrase for _, _, matched in found for phrase in matched]
            route.reason = "; ".join(
                f"{name} escalation: " + ", ".join(repr(p) for p in matched)
                for name, _, matched in found
            )
        routes[email.id] = route
    return routes


def make_client() -> SIEClient:
    """Build an SIE client from the API key and SIE_BASE_URL in the environment or `.env`."""
    load_dotenv()
    api_key = os.environ.get("SIE_API_KEY", "").strip()
    base_url = os.environ.get("SIE_BASE_URL", "").strip()
    if not api_key and not base_url:
        raise RuntimeError("Set SIE_API_KEY, or SIE_BASE_URL for your own SIE, in .env")
    base_url = base_url or "https://api.superlinked.com"
    return SIEClient(api_key=api_key or None, base_url=base_url, timeout_s=WAIT_S)


# --- 2. Windows and model calls ---------------------------------------------------------------


def windows(text: str, max_words: int) -> list[str]:
    """Split the text into consecutive windows of at most ``max_words`` words.

    Both models read about 512 tokens, and the Decide question with the team list takes up to
    half of that. A long email is split here, and the windows' answers are merged afterwards.
    """
    words = text.split()
    if not words:
        return [""]
    return [" ".join(words[i : i + max_words]) for i in range(0, len(words), max_words)]


def team_schema(catalog: Catalog, rules: dict[str, Any]) -> dict[str, Any]:
    """The catalog's teams as one GLiNER2.5-Decide choice question."""
    return {
        QUESTION_ID: {
            "type": "choice",
            "instructions": rules["question"],
            "criteria": dict(catalog.teams),
        }
    }


def call_in_batches(
    client: SIEClient,
    model: str,
    items: list[tuple[str, str]],
    rules: dict[str, Any],
    key: str,
    **request: Any,
) -> dict[str, dict[str, Any]]:
    """Send the windows to one model in batches; return its result per window id.

    A call that fails for a reason that may pass (a lost connection, a timeout, a server
    error, a rate limit) marks every window in its batch with that error, so those emails go
    to the general queue (an escalated email keeps its queue, with a note) and the run still
    writes its outputs. A call whose every window came back with an item error or without
    ``key`` counts as failed too. Two cases stop the run with SIEStopped instead: a refusal
    every batch would repeat (see `is_fatal`), and a run where no call to this model succeeded
    at all. An error that isn't an SIE error is a bug in this code and is raised as is.
    """
    size = rules["thresholds"]["batch_size"]
    results: dict[str, dict[str, Any]] = {}
    failures: list[SIEError] = []
    calls = 0
    for start in range(0, len(items), size):
        batch = items[start : start + size]
        calls += 1
        try:
            found: Any = client.extract(
                model,
                [Item(id=item_id, text=text) for item_id, text in batch],
                provision_timeout_s=WAIT_S,
                **request,
            )
        except SIEError as error:
            if is_fatal(error):
                raise SIEStopped(stop_message(error, model)) from error
            failures.append(error)
            failed = {"error": f"the SIE call failed: {short(error)}"}
            results.update({item_id: failed for item_id, _ in batch})
            continue
        for (item_id, _), result in zip(batch, found, strict=True):
            results[item_id] = result
        # A reply with nothing usable in any window is a failed call, on the transient path.
        unusable = [reply_error(results[item_id], key) for item_id, _ in batch]
        if all(unusable):
            failures.append(SIEError(f"every window's reply failed: {unusable[0]}"))
    if failures and len(failures) == calls:
        raise SIEStopped(
            f"Every SIE call failed ({calls} of {calls} to {model}), so the run stops: "
            f"{describe(failures[0])}: {failures[0]}. {every_call_failed_advice(failures[0])} "
            + NOT_WRITTEN
        ) from failures[0]
    if failures:
        n = len(failures)
        print(
            f"{n} of {calls} SIE calls to {model} failed; their emails go to "
            f"{rules['general_queue']} or keep their escalation. First: {short(failures[0])}"
        )
    return results


def every_call_failed_advice(error: SIEError) -> str:
    """What to check when every SIE call failed, judged by the first failure.

    A lost connection points at SIE_BASE_URL and the network. Any other transient error (a
    timeout, a model still loading, a 5xx, a rate limit) more likely means the model is still
    starting or SIE is busy, so the advice is to wait and run again.
    """
    if isinstance(error, SIEConnectionError):
        return "Check SIE_BASE_URL and your network."
    return "The model may still be starting or SIE is busy; run again in a few minutes."


class SIEStopped(RuntimeError):
    """SIE refused the call for a reason a retry won't fix, or no call succeeded; the run
    stops without routes."""


def is_fatal(error: SIEError) -> bool:
    """A refusal that fails every call the same way: a 4xx (bad key 401, forbidden 403, model
    not found 404, no credits or spend limit 402, inactive account) or a model the server
    marked as permanently unable to load. A RequestError with no HTTP status or a status below
    400 is a reply the SDK couldn't parse, and is fatal too. A rate limit, an over-long input, a
    timeout, a lost connection, a model still loading and a 5xx are transient or per-batch:
    those emails go to the general queue."""
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
    """The error for a per-email reason or the summary line, its message cut short."""
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
            f"rules.yaml, then run again. {NOT_WRITTEN}"
        )
    if status is None or status < 400:
        return (
            f"SIE returned a reply the recipe couldn't read for {model}, {describe(error)}: "
            f"{error}. Check that SIE_BASE_URL points at an SIE server, then run again. "
            + NOT_WRITTEN
        )
    return (
        f"SIE refused the call to {model}, {describe(error)}: {error}. Check SIE_API_KEY, the "
        f"model name in rules.yaml and your SIE account, then run again. {NOT_WRITTEN}"
    )


def stop_on_refusal[T](work: Callable[[], T]) -> T:
    """Run the work; turn SIEStopped into a clean exit. Any other error keeps its traceback."""
    try:
        return work()
    except SIEStopped as error:
        raise SystemExit(f"Stopped: {error}") from None


def classify_windows(
    client: SIEClient, items: list[tuple[str, str]], catalog: Catalog, rules: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Team answer (or error) per window id, batched."""
    return call_in_batches(
        client,
        rules["models"]["team"],
        items,
        rules,
        "data",
        output_schema=team_schema(catalog, rules),
        # Windows are sized to fit; an over-long one fails its call and goes to the general queue.
        options={"overflow_policy": "error"},
    )


def extract_windows(
    client: SIEClient, items: list[tuple[str, str]], rules: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Order-number and store spans (or error) per window id, batched.

    We use plain labels here rather than an output_schema. Plenty of emails have no order
    number and no store, and labels return an empty list for them instead of an error.
    """
    return call_in_batches(
        client,
        rules["models"]["fields"],
        items,
        rules,
        "entities",
        labels=list(rules["field_labels"].values()),
        options={"threshold": rules["thresholds"]["min_entity_score"]},
    )


def _error_text(result: dict[str, Any]) -> str | None:
    error = result.get("error")
    if not error:
        return None
    if isinstance(error, dict):
        return f"{error.get('code', 'ERROR')}: {error.get('message', '')}".strip()
    return str(error)


def reply_error(result: dict[str, Any], key: str) -> str | None:
    """Why a window's reply can't be used, or None. A reply without ``key`` is a failed call,
    not an empty answer; an empty list under the key is a real "nothing found"."""
    if error := _error_text(result):
        return error
    if key not in result:
        return f"the reply has no {key!r}"
    return None


def team_probabilities(
    result: dict[str, Any], catalog: Catalog
) -> tuple[dict[str, float], str | None]:
    """The Decide reply's probability per team, or the reason the reply can't be used. A team
    that isn't in the catalog makes the reply unusable: the route must be a real queue."""
    if error := reply_error(result, "data"):
        return {}, error
    answer = (result["data"] or {}).get(QUESTION_ID)
    if not isinstance(answer, dict) or "probabilities" not in answer:
        return {}, f"the reply has no {QUESTION_ID!r} probabilities"
    probabilities = {str(k): float(v) for k, v in (answer["probabilities"] or {}).items()}
    if unknown := sorted(set(probabilities) - set(catalog.teams)):
        return {}, "the reply names teams not in the catalog: " + ", ".join(unknown)
    return probabilities, None


def merge_probabilities(per_window: list[dict[str, float]]) -> dict[str, float]:
    """Each team's highest probability over the windows, renormalized to sum to 1."""
    if not per_window:
        return {}
    if len(per_window) == 1:
        return dict(per_window[0])
    peak = {team: max(window.get(team, 0.0) for window in per_window) for team in per_window[0]}
    total = sum(peak.values()) or 1.0
    return {team: value / total for team, value in peak.items()}


# --- 3. Check the spans (Python) --------------------------------------------------------------


def normalize_order_number(span: str, rules: dict[str, Any]) -> str | None:
    """'lk 208 331' -> 'LK-208331'; None when the span is not an order number."""
    compact = re.sub(r"[\s\-#]", "", span).upper()
    match = re.fullmatch(rules["order_number"]["pattern"], compact)
    if match is None:
        return None
    return str(rules["order_number"]["format"]).format(*match.groups())


def _strip_filler(span: str, filler: set[str]) -> str:
    words = span.split()
    while words and words[0].casefold() in filler:
        words.pop(0)
    while words and words[-1].casefold() in filler:
        words.pop()
    return " ".join(words)


def match_store(span: str, catalog: Catalog, rules: dict[str, Any]) -> str | None:
    """Store id for a store span: exact name or alias, else a close spelling, else None."""
    names = catalog.store_names()
    filler = {str(word).casefold() for word in rules["store_filler_words"]}
    key = _strip_filler(span.strip(" .,!?"), filler).casefold()
    if key in names:
        return names[key]
    close = difflib.get_close_matches(
        key, list(names), n=1, cutoff=rules["thresholds"]["store_match_cutoff"]
    )
    return names[close[0]] if close else None


def read_fields(
    route: Route,
    email: Email,
    spans: list[dict[str, Any]],
    catalog: Catalog,
    rules: dict[str, Any],
) -> None:
    """Fill order numbers and the store from the model's spans, keeping only checked values.

    A span must appear in the email, an order number must fit the pattern, and a store must
    match the store list. Anything else is dropped with a note for the agent. The model reads
    windows with every run of whitespace collapsed to one space, so the span is looked for in
    the email collapsed the same way: "Riverside\nPlaza" still holds "Riverside Plaza".
    """
    columns = {label: column for column, label in rules["field_labels"].items()}
    text = " ".join(email.text.split()).casefold()
    stores: list[str] = []
    for span in sorted(spans, key=lambda s: -float(s.get("score", 0.0))):
        column = columns.get(str(span.get("label")))
        value = str(span.get("text", "")).strip()
        if not column or not value:
            continue
        if value.casefold() not in text:
            route.notes.append(f"{column} span {value!r} is not in the email; dropped")
        elif column == "order_number":
            number = normalize_order_number(value, rules)
            if number is None:
                route.notes.append(f"{value!r} is not an order number; dropped")
            elif number not in route.order_numbers:
                route.order_numbers.append(number)
        elif column == "store":
            store_id = match_store(value, catalog, rules)
            if store_id is None:
                route.notes.append(f"store {value!r} is not in the store list")
            elif store_id not in stores:
                stores.append(store_id)
    if len(stores) > 1:
        route.notes.append("several stores named: " + ", ".join(stores))
    if stores:
        route.store_id = stores[0]
        route.store_name = catalog.stores[stores[0]].name


# --- 4. Decide --------------------------------------------------------------------------------


def route_by_model(
    route: Route, probabilities: dict[str, float], errors: list[str], rules: dict[str, Any]
) -> None:
    """Route on the model's pick only when it is clear and no call failed; else the general
    queue."""
    limits = rules["thresholds"]
    route.team, route.method = rules["general_queue"], GENERAL
    reasons = list(errors)
    ranked = sorted(probabilities.items(), key=lambda item: item[1], reverse=True)
    if not ranked:
        if not errors:
            reasons.append("no answer from the team model")
        route.reason = "; ".join(reasons)
        return
    top, score = ranked[0]
    second, second_score = ranked[1] if len(ranked) > 1 else ("", 0.0)
    route.suggested_team, route.second_team = top, second
    route.score, route.margin = score, score - second_score
    if score < limits["min_team_score"]:
        reasons.append(f"top team {top} scored {score:.2f} < {limits['min_team_score']:.2f}")
    elif route.margin < limits["min_team_margin"]:
        reasons.append(
            f"{top} {score:.2f} vs {second} {second_score:.2f}: "
            f"margin {route.margin:.2f} < {limits['min_team_margin']:.2f}"
        )
    elif not errors:
        route.team, route.method = top, MODEL
        reasons.append(f"model picked {top} at {score:.2f}, margin {route.margin:.2f}")
    route.reason = "; ".join(reasons)


def route_emails(
    client: SIEClient,
    emails: list[Email],
    routes: dict[str, Route],
    catalog: Catalog,
    rules: dict[str, Any],
) -> list[Route]:
    """Fill in the routes from `screen`: field spans for every email, a team for the rest.

    An escalated route keeps its queue whatever the models return; a failed call only adds a
    note to it. An email that wasn't escalated goes to the general queue when either of its
    model calls failed.
    """
    need_team = [email.id for email in emails if routes[email.id].method != ESCALATION]
    max_words = rules["thresholds"]["max_words_per_window"]
    items = [
        (f"{email.id}#{number}", text)
        for email in emails
        for number, text in enumerate(windows(email.text, max_words), 1)
    ]
    spans = extract_windows(client, items, rules) if items else {}
    team_items = [item for item in items if item[0].split("#", 1)[0] in need_team]
    answers = classify_windows(client, team_items, catalog, rules) if team_items else {}

    found_spans: dict[str, list[dict[str, Any]]] = {email.id: [] for email in emails}
    per_window: dict[str, list[dict[str, float]]] = {email_id: [] for email_id in need_team}
    errors: dict[str, list[str]] = {email.id: [] for email in emails}
    for item_id, _ in items:
        email_id = item_id.split("#", 1)[0]
        if error := reply_error(spans[item_id], "entities"):
            errors[email_id].append(f"field model: {error}")
        else:
            found_spans[email_id] += spans[item_id]["entities"] or []
        if item_id not in answers:
            continue
        probabilities, error = team_probabilities(answers[item_id], catalog)
        if error:
            errors[email_id].append(f"team model: {error}")
        else:
            per_window[email_id].append(probabilities)

    for email in emails:
        route = routes[email.id]
        read_fields(route, email, found_spans[email.id], catalog, rules)
        if route.method == ESCALATION:
            route.notes += errors[email.id]
            continue
        route_by_model(route, merge_probabilities(per_window[email.id]), errors[email.id], rules)
        wanted = route.team if route.method == MODEL else ""
        if wanted in rules["needs_order_number"] and not route.order_numbers:
            route.notes.append("no order number found: ask the customer for it")
    return [routes[email.id] for email in emails]


# --- Run --------------------------------------------------------------------------------------


def write_rows(path: Path, routes: list[Route]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ROUTE_FIELDS)
        writer.writeheader()
        writer.writerows(route.as_row() for route in routes)


def write_escalations(output_dir: Path, routes: dict[str, Route]) -> Path:
    """Write the escalated emails before any model call, so a failed or stopped SIE call
    can't lose them. The file has the routes.csv columns; the model columns are still empty."""
    path = output_dir / "escalations.csv"
    write_rows(path, [route for route in routes.values() if route.method == ESCALATION])
    return path


def write_outputs(output_dir: Path, routes: list[Route]) -> None:
    write_rows(output_dir / "routes.csv", routes)
    rows = [route.as_row() for route in routes]
    (output_dir / "routes.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    emails, catalog, rules = load_emails(), load_catalog(), load_rules()
    screened = screen(emails, rules)
    escalated = write_escalations(output, screened)
    count = sum(route.method == ESCALATION for route in screened.values())
    print(f"{count} escalations written before any model call: {escalated}")
    with make_client() as client:
        routes = stop_on_refusal(lambda: route_emails(client, emails, screened, catalog, rules))
    write_outputs(output, routes)
    for route in routes:
        extras = ", ".join(route.order_numbers + ([route.store_id] if route.store_id else []))
        print(f"{route.email_id}  {route.team:<20} {route.method:<10} [{extras}] {route.reason}")
        if route.also_send_to:
            print(f"        also send to: {', '.join(route.also_send_to)}")
    counts = {m: sum(r.method == m for r in routes) for m in (ESCALATION, MODEL, GENERAL)}
    print(
        f"\n{len(routes)} emails: {counts[ESCALATION]} escalated, {counts[MODEL]} by model, "
        f"{counts[GENERAL]} to {rules['general_queue']}"
    )
    print("team = a suggested queue; a care lead confirms it, nothing is assigned in a ticket tool")
    outputs = ", ".join(str(output / name) for name in ("routes.csv", "routes.json"))
    print(f"outputs: {escalated}, {outputs}")


if __name__ == "__main__":
    main()
