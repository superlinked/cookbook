"""Find the owner of each alert in the fallback queue: records first, a suggested team last.

Use case:  Alert ownership: who owns this alert that no Alertmanager route matched?
Who:       The central SRE or NOC responder watching the shared fallback queue.
Problem:   An alert whose labels match no route lands in a shared queue. The responder looks
           for an owner in the service catalog, the host inventory and the namespace records,
           and when those name nobody, reads the alert and guesses, often at 3 a.m.
Input:     data/alerts.json (the alerts as the fallback receiver got them: labels plus summary
           and description), the service catalog (service_catalog.py over data/catalog_*.csv)
           and the host and namespace inventory (inventory.py over data/inventory_*.csv).
           Label keys, the question, thresholds and the model ID are in rules.yaml.
SIE pipeline (one model):
           (Python)  look up service, app, job, instance and namespace in the catalog and the
                     inventory; an owner found there decides, and the model isn't asked
           extract   fastino/GLiNER2.5-Decide   called once per alert the records can't place:
                                                one choice question over the catalog's teams,
                                                a probability for each team
           (Python)  a top team under the score or margin limit is no_clear_owner
Output:    routes.csv and routes.json in runs/<timestamp>/: per alert, the status
           (owner_in_catalog, ownerless_service, team_suggested, no_clear_owner), the team, how it
           was found, the model's scores where it was asked, and the reason. The recipe
           reassigns nothing: the responder decides every reassignment.
Why SIE:   One small encoder model, called only for the alerts no record places. The team list
           is the catalog itself: add a team with a description and the next run can suggest
           it, with no training. No generative model makes the decision. Nothing to host.
Runs on:   SIE Cloud.

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
from dotenv import load_dotenv
from inventory import Inventory
from service_catalog import ServiceCatalog
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
ALERTS_PATH = DATA_DIR / "alerts.json"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
# How much of an SIE error message goes into a per-alert reason or the summary line.
ERROR_CHARS = 200

QUESTION_ID = "owning_team"
# What the checks found, never a decision: the responder decides every reassignment.
OWNER_IN_CATALOG = "owner_in_catalog"  # a record names the service and its owner
OWNERLESS = "ownerless_service"  # a record names the service; the catalog has no owner for it
SUGGESTED = "team_suggested"  # no record; the model's top team clears the limits
NO_CLEAR_OWNER = "no_clear_owner"  # conflicting records, a split model, or a failed call
STATUSES = (OWNER_IN_CATALOG, OWNERLESS, SUGGESTED, NO_CLEAR_OWNER)
ROUTE_FIELDS = (
    "alert_id",
    "alertname",
    "status",
    "team",
    "found_by",
    "suggested_team",
    "score",
    "runner_up",
    "runner_up_score",
    "margin",
    "labels",
    "model_read",
    "reason",
)
# A cell starting with one of these runs as a formula in a spreadsheet.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


# --- Inputs -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Alert:
    id: str
    labels: dict[str, str]
    annotations: dict[str, str]

    @property
    def name(self) -> str:
        return self.labels.get("alertname", "")


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def parse_alerts(records: list[dict[str, Any]]) -> list[Alert]:
    alerts = [
        Alert(
            str(record["id"]),
            {str(k): str(v) for k, v in (record.get("labels") or {}).items()},
            {str(k): str(v) for k, v in (record.get("annotations") or {}).items()},
        )
        for record in records
    ]
    ids = [alert.id for alert in alerts]
    if len(set(ids)) != len(ids):
        raise ValueError(f"alert ids repeat: {sorted({i for i in ids if ids.count(i) > 1})}")
    return alerts


def load_alerts(path: Path = ALERTS_PATH) -> list[Alert]:
    return parse_alerts(json.loads(path.read_text(encoding="utf-8"))["alerts"])


def make_client() -> SIEClient:
    """Build an SIE client from the API key and SIE_BASE_URL in the environment or `.env`."""
    load_dotenv()
    api_key = os.environ.get("SIE_API_KEY", "").strip()
    base_url = os.environ.get("SIE_BASE_URL", "").strip()
    if not api_key and not base_url:
        raise RuntimeError("Set SIE_API_KEY, or SIE_BASE_URL for your own SIE, in .env")
    base_url = base_url or "https://api.superlinked.com"
    return SIEClient(api_key=api_key or None, base_url=base_url, timeout_s=WAIT_S)


# --- Records: the catalog and the inventory ---------------------------------------------------


@dataclass(frozen=True)
class Found:
    """One label value a record placed: which service it names, and that service's owner."""

    label: str  # "app=checkout-ui"
    service: str
    owner: str | None
    via: str  # how the label led to the service: "an alias of web-frontend in the ..."


def label_values(value: str) -> list[str]:
    """A label value, split where it holds several names ("a,b" or "a b"): each is looked up."""
    return [part for part in re.split(r"[,\s]+", value) if part]


def look_up_labels(
    alert: Alert, catalog: ServiceCatalog, inventory: Inventory, rules: dict[str, Any]
) -> tuple[list[Found], list[str]]:
    """Every owner label the records place, and a note for every one they don't.

    Every label in `owner_labels` is checked; an unknown one never hides a known one.
    """
    lookups = {"catalog", "host", "namespace"}
    if bad := {k: v for k, v in rules["owner_labels"].items() if v not in lookups}:
        raise ValueError(f"owner_labels: unknown lookup {bad}; use one of {sorted(lookups)}")
    found: list[Found] = []
    unknown: list[str] = []
    for key, kind in rules["owner_labels"].items():
        for value in label_values(alert.labels.get(key, "")):
            label = f"{key}={value}"
            if kind == "catalog":
                service, where = value, "the service catalog"
            elif kind == "host":
                service, where = inventory.find_host(value) or "", "the host inventory"
            else:
                service, where = inventory.find_namespace(value) or "", "the namespace records"
            if not service:
                unknown.append(f"{label} is not in {where}")
                continue
            entry = catalog.find_service(service)
            if entry is None:
                note = f"{label} is not in the service catalog"
                if kind != "catalog":
                    note = (
                        f"{label} runs {service} per {where}, which is not in the service catalog"
                    )
                unknown.append(note)
                continue
            if kind == "host":
                via = f"a host of {entry.name} in {where}"
            elif kind == "namespace":
                via = f"the namespace of {entry.name} in {where}"
            elif entry.name.casefold() != value.casefold():
                via = f"an alias of {entry.name} in {where}"
            else:
                via = f"in {where}"
            found.append(Found(label, entry.name, entry.owner, via))
    return found, unknown


@dataclass
class Route:
    alert_id: str
    alertname: str
    status: str
    team: str = ""
    found_by: str = ""
    suggested_team: str = ""
    score: float | None = None
    runner_up: str = ""
    runner_up_score: float | None = None
    margin: float | None = None
    labels: str = ""
    model_read: str = ""
    reason: str = ""

    def as_row(self) -> dict[str, Any]:
        row = asdict(self)
        for key in ("score", "runner_up_score", "margin"):
            row[key] = "" if row[key] is None else f"{row[key]:.3f}"
        return row


def new_route(alert: Alert, status: str) -> Route:
    labels = "; ".join(f"{k}={v}" for k, v in alert.labels.items())
    return Route(alert.id, alert.name, status, labels=labels)


def _owned(item: Found) -> str:
    owner = f"owned by {item.owner}" if item.owner else "with no owner recorded"
    return f"{item.label}: {item.via}, {owner}"


def route_by_records(
    alert: Alert, found: list[Found], unknown: list[str], teams: dict[str, str]
) -> Route | None:
    """The route when a record places the alert, or None when the model has to be asked.

    One owner across every placed label is owner_in_catalog. A placed service with no owner is
    ownerless_service, and the model isn't asked to pick an owner the catalog says nobody has.
    Labels that lead to different owners (an owned and an ownerless service included) are a
    conflict: no_clear_owner with both shown, never settled by which label came first.
    """
    if not found:
        return None
    owners = sorted({item.owner or "" for item in found})
    route = new_route(alert, NO_CLEAR_OWNER)
    route.found_by = "; ".join(item.label for item in found)
    notes = [f"also: {note}" for note in unknown]
    if len(owners) > 1:
        route.reason = "; ".join(
            ["labels lead to different owners: " + "; ".join(_owned(i) for i in found), *notes]
        )
        return route
    owner = owners[0]
    placed = "; ".join(_owned(item) for item in found)
    if not owner:
        route.status = OWNERLESS
        route.reason = "; ".join([placed, *notes])
    elif owner not in teams:
        route.reason = "; ".join([f"{placed}, but {owner} is not a team in the catalog", *notes])
    else:
        route.status, route.team = OWNER_IN_CATALOG, owner
        route.reason = "; ".join([placed, *notes])
    return route


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
    those alerts come out no_clear_owner."""
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
    """The error for a per-alert reason or the summary line, its message cut short."""
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
        """No call succeeded, so no alert was read: stop instead of writing a half answer."""
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
    a rate limit, a reply without the probabilities we asked for) is logged and its alert comes
    out no_clear_owner; the run continues. A refusal every call would repeat (see `is_fatal`)
    stops the run. An error that isn't an SIE error is a bug in this code and is raised as is.
    """
    calls.made += 1
    try:
        return work(), ""
    except SIEError as error:
        if is_fatal(error):
            raise SIEStopped(stop_message(error, model)) from error
        calls.failures.append((model, error))
        return None, f"the SIE call failed: {short(error)}"


# --- The team model ---------------------------------------------------------------------------


def _words(name: str) -> str:
    """'PostgresReplicationLagHigh' -> 'Postgres Replication Lag High'."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)


def alert_text(alert: Alert, rules: dict[str, Any]) -> str:
    """What the model reads: alert name in words, summary, description, the telling labels."""
    hidden = set(rules["hidden_labels"])
    parts = [_words(alert.name)] if alert.name else []
    parts += [
        alert.annotations[key] for key in ("summary", "description") if key in alert.annotations
    ]
    labels = [f"{k}: {v}" for k, v in sorted(alert.labels.items()) if k not in hidden]
    if labels:
        parts.append("; ".join(labels))
    return ". ".join(part.strip().rstrip(".") for part in parts if part.strip()) + "."


def team_schema(teams: dict[str, str], rules: dict[str, Any]) -> dict[str, Any]:
    """The catalog's teams as one GLiNER2.5-Decide choice question."""
    return {QUESTION_ID: {"type": "choice", "instructions": rules["question"], "criteria": teams}}


def jsonable(value: Any) -> Any:
    """Turn SDK results (numpy arrays, nested dicts and lists) into plain JSON values."""
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, dict):
        return {str(key): jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(child) for child in value]
    return value


def read_probabilities(reply: Any, teams: dict[str, str]) -> dict[str, float]:
    """The probability per team from a reply, or an SIEError saying what was wrong with it.

    A reply without `data.owning_team.probabilities`, with a team missing or one the catalog
    doesn't have, or with a value that isn't a number from 0 to 1, is a failed call: it takes
    the transient path, and the alert comes out no_clear_owner.
    """
    answer = reply.get("data", {}).get(QUESTION_ID) if isinstance(reply, dict) else None
    probabilities = answer.get("probabilities") if isinstance(answer, dict) else None
    if not isinstance(probabilities, dict):
        error = reply.get("error") if isinstance(reply, dict) else None
        raise SIEError(
            f"the reply has no `data.{QUESTION_ID}.probabilities`"
            + (f" (item error: {error})" if error else "")
        )
    missing, extra = (
        sorted(set(teams) - set(probabilities)),
        sorted(set(probabilities) - set(teams)),
    )
    if missing or extra:
        raise SIEError(
            f"the reply's teams don't match the catalog: missing {missing}, extra {extra}"
        )
    values: dict[str, float] = {}
    for team, value in probabilities.items():
        number = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        if number is None or not 0.0 <= number <= 1.0:
            raise SIEError(
                f"the reply's probability for {team} is not a number from 0 to 1: {value!r}"
            )
        values[str(team)] = float(number)
    return values


def ask_team(
    client: SIEClient,
    calls: CallLog,
    alert: Alert,
    text: str,
    teams: dict[str, str],
    rules: dict[str, Any],
) -> tuple[dict[str, float] | None, str]:
    """One GLiNER2.5-Decide call for one alert: a probability per team, or why it failed."""
    model = rules["models"]["team"]
    schema = team_schema(teams, rules)

    def call() -> dict[str, float]:
        reply = client.extract(
            model,
            Item(id=alert.id, text=text),
            output_schema=schema,
            options={"overflow_policy": "error"},  # an alert too long to read whole fails
            provision_timeout_s=WAIT_S,
        )
        return read_probabilities(jsonable(reply), teams)

    return guarded(calls, model, call)


def route_by_model(
    route: Route,
    probabilities: dict[str, float] | None,
    failed: str,
    why_model: list[str],
    rules: dict[str, Any],
) -> Route:
    """team_suggested only when the top team clears both limits; otherwise no_clear_owner,
    with the runner-up shown either way."""
    limits = rules["thresholds"]
    reasons = list(why_model)
    if probabilities is None:
        route.reason = "; ".join([*reasons, f"team model: {failed}"])
        return route
    ranked = sorted(probabilities.items(), key=lambda item: item[1], reverse=True)
    top, score = ranked[0]
    second, second_score = ranked[1] if len(ranked) > 1 else ("", 0.0)
    route.suggested_team, route.score = top, score
    route.runner_up, route.runner_up_score = second, second_score
    route.margin = score - second_score
    if score < limits["min_team_score"]:
        reasons.append(
            f"model's top team {top} scored {score:.2f}, under {limits['min_team_score']:.2f} "
            f"(runner-up {second} {second_score:.2f})"
        )
    elif route.margin < limits["min_team_margin"]:
        reasons.append(
            f"model split: {top} {score:.2f} vs {second} {second_score:.2f}, margin "
            f"{route.margin:.2f} under {limits['min_team_margin']:.2f}"
        )
    else:
        route.status, route.team, route.found_by = SUGGESTED, top, "model"
        reasons.append(
            f"model suggests {top} at {score:.2f}, margin {route.margin:.2f} over {second}; "
            "check it before reassigning"
        )
    route.reason = "; ".join(reasons)
    return route


def why_model_asked(unknown: list[str], rules: dict[str, Any]) -> list[str]:
    """Why no record placed the alert: the unknown labels, or that it has none to look up."""
    if unknown:
        return unknown
    *rest, last = rules["owner_labels"]
    keys = f"{', '.join(rest)} or {last}" if rest else last
    return [f"no {keys} label: the alert names no system, so this rests on its text alone"]


# --- Run --------------------------------------------------------------------------------------


def route_alert(
    client: SIEClient,
    calls: CallLog,
    alert: Alert,
    catalog: ServiceCatalog,
    inventory: Inventory,
    rules: dict[str, Any],
) -> Route:
    """Records first; the model only when no record places the alert."""
    teams = catalog.teams()
    found, unknown = look_up_labels(alert, catalog, inventory, rules)
    route = route_by_records(alert, found, unknown, teams)
    if route is not None:
        return route
    route = new_route(alert, NO_CLEAR_OWNER)
    route.model_read = alert_text(alert, rules)
    probabilities, failed = ask_team(client, calls, alert, route.model_read, teams, rules)
    return route_by_model(route, probabilities, failed, why_model_asked(unknown, rules), rules)


def run(
    client: SIEClient,
    rules: dict[str, Any],
    output_dir: Path | None,
    alerts_path: Path = ALERTS_PATH,
    data_dir: Path = DATA_DIR,
) -> list[Route]:
    """Route every alert in `alerts_path`.

    Nothing is written until every alert is done, so a run that stops on an SIE refusal or on
    every call failing leaves no partial outputs behind.
    """
    alerts = load_alerts(alerts_path)
    if not alerts:
        raise SystemExit(f"No alerts in {alerts_path}, so there is nothing to route.")
    catalog, inventory = ServiceCatalog(data_dir), Inventory(data_dir)
    calls = CallLog()
    routes = [route_alert(client, calls, alert, catalog, inventory, rules) for alert in alerts]
    calls.stop_if_all_failed()
    if calls.failures:
        print(
            f"{len(calls.failures)} of {calls.made} SIE calls failed; their alerts come out "
            f"{NO_CLEAR_OWNER}. First: {short(calls.failures[0][1])}"
        )
    if output_dir is not None:
        write_outputs(output_dir, routes)
    return routes


def csv_safe(value: Any) -> str:
    """A leading ' on a cell a spreadsheet would run as a formula; anything else as written."""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def write_outputs(output_dir: Path, routes: list[Route]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = [route.as_row() for route in routes]
    with (output_dir / "routes.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ROUTE_FIELDS)
        writer.writeheader()
        writer.writerows({key: csv_safe(value) for key, value in row.items()} for row in rows)
    (output_dir / "routes.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rules = load_rules()
    with make_client() as client:
        routes = stop_on_refusal(lambda: run(client, rules, output))
    for route in routes:
        print(f"{route.alert_id}  {route.status:<18} {route.team or '-':<14} {route.reason}")
    counts = ", ".join(f"{sum(r.status == s for r in routes)} {s}" for s in STATUSES)
    print(f"\n{len(routes)} alerts: {counts}")
    print("Nothing was reassigned: the responder decides every reassignment.")
    print(f"outputs: {output / 'routes.csv'}, {output / 'routes.json'}")


if __name__ == "__main__":
    main()
