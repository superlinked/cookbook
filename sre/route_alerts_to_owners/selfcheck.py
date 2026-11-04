"""Check the recipe's lookups, ownership rules, model checks and SIE error rule offline.

The checks use the committed data/, hand-written alerts and catalog rows, and hand-written
stand-in model replies. Nothing here calls SIE or replays a recorded response.
Run it with `uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import csv
import functools
import io
import json
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from inventory import Inventory, host_part
from service_catalog import Service, ServiceCatalog
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
MODEL = RULES["models"]["team"]
CATALOG = ServiceCatalog()
INVENTORY = Inventory()
TEAMS = CATALOG.teams()


def probabilities(**top: float) -> dict[str, float]:
    """A probability for every catalog team: the ones given, the rest sharing what's left."""
    rest = [team for team in TEAMS if team not in top]
    share = (1.0 - sum(top.values())) / len(rest)
    return {team: top.get(team, share) for team in TEAMS}


def reply(**top: float) -> dict[str, Any]:
    """A stand-in GLiNER2.5-Decide reply to the one choice question."""
    probs = probabilities(**top)
    choice = max(probs, key=lambda team: probs[team])
    return {
        "data": {recipe.QUESTION_ID: {"type": "choice", "choice": choice, "probabilities": probs}}
    }


def exact_reply(**top: float) -> dict[str, Any]:
    """A stand-in reply with exactly these probabilities and 0.0 for every other team."""
    probs = {team: top.get(team, 0.0) for team in TEAMS}
    choice = max(probs, key=lambda team: probs[team])
    return {
        "data": {recipe.QUESTION_ID: {"type": "choice", "choice": choice, "probabilities": probs}}
    }


def alert(alert_id: str = "X-1", **labels: str) -> dict[str, Any]:
    annotations = {"summary": "Something is wrong", "description": "It keeps getting worse."}
    return {
        "id": alert_id,
        "labels": {"alertname": "SomethingWrong", **labels},
        "annotations": annotations,
    }


class StandIn:
    """Stands in for SIEClient. Each call takes the next listed reply: an exception is raised,
    anything else is returned as the reply. A call with no reply left is a check failure."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.texts: list[str] = []
        self.ids: list[str] = []

    def extract(self, model: str, item: Any, **_: Any) -> Any:
        assert model == MODEL, model
        assert self.replies, f"the model was asked about {item['id']}, and no reply was planned"
        self.texts.append(item["text"])
        self.ids.append(item["id"])
        answer = self.replies.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def run(
    alerts: list[dict[str, Any]],
    *replies: Any,
    folder: Path | None = None,
    data_dir: Path = recipe.DATA_DIR,
    rules: dict[str, Any] = RULES,
) -> tuple[dict[str, recipe.Route], Path, StandIn]:
    """Run the recipe on hand-written alerts with stand-in replies; outputs go to folder/out."""
    folder = folder or Path(tempfile.mkdtemp())
    path = folder / "alerts.json"
    path.write_text(json.dumps({"alerts": alerts}), encoding="utf-8")
    client = StandIn(*replies)
    output = folder / "out"
    with contextlib.redirect_stdout(io.StringIO()):  # the run's "1 of 2 failed" line
        routes = recipe.run(cast(SIEClient, client), rules, output, path, data_dir)
    assert not client.replies, f"{len(client.replies)} replies left unused"
    assert len(routes) == len(alerts), "every alert gets exactly one row"
    return {route.alert_id: route for route in routes}, output, client


def one(*replies: Any, **labels: str) -> recipe.Route:
    """The route for one hand-written alert."""
    routes, _, _ = run([alert(**labels)], *replies)
    return routes["X-1"]


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


def check_data() -> None:
    alerts = recipe.load_alerts()
    assert [a.id for a in alerts] == [f"AL-{n:02d}" for n in range(1, 12)], [a.id for a in alerts]
    assert len(TEAMS) == 8, TEAMS
    assert all(TEAMS.values()), TEAMS
    with (recipe.DATA_DIR / "catalog_services.csv").open(encoding="utf-8") as handle:
        services = list(csv.DictReader(handle))
    for row in services:
        assert row["owner"] in TEAMS or not row["owner"], row
    assert CATALOG.find_service("legacy-reports") == Service("legacy-reports", None)
    for name in ("inventory_hosts.csv", "inventory_namespaces.csv"):
        with (recipe.DATA_DIR / name).open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                assert CATALOG.find_service(row["service"]) is not None, (name, row)
    # The near misses stay unknown to the records, so they really reach the model.
    assert CATALOG.find_service("checkout-v3") is None
    assert CATALOG.find_service("loyalty-points") is None
    assert INVENTORY.find_host("10.0.6.14:9308") is None
    assert INVENTORY.find_namespace("edge") is None
    print("data: 11 alerts, 8 teams, every owner a catalog team, inventory services in catalog ok")


def check_examples() -> None:
    """The committed alerts end to end, with stand-in replies scored like the README's run: an
    edit to one of these alerts' labels that changes which alerts reach the model, or how one
    comes out, fails here."""
    records = json.loads(recipe.ALERTS_PATH.read_text(encoding="utf-8"))["alerts"]
    routes, _, client = run(
        records,
        reply(loyalty=0.83, streaming=0.15),  # AL-04
        reply(platform=0.54, storefront=0.38),  # AL-05
        reply(storefront=0.57, loyalty=0.10),  # AL-06
        reply(identity=0.88, storefront=0.05),  # AL-09
        reply(streaming=0.56, storefront=0.19),  # AL-11
    )
    assert client.ids == ["AL-04", "AL-05", "AL-06", "AL-09", "AL-11"], client.ids
    expected = {
        "AL-01": (recipe.OWNER_IN_CATALOG, "payments"),
        "AL-02": (recipe.OWNER_IN_CATALOG, "storefront"),
        "AL-03": (recipe.OWNER_IN_CATALOG, "data-platform"),
        "AL-04": (recipe.SUGGESTED, "loyalty"),
        "AL-05": (recipe.NO_CLEAR_OWNER, ""),
        "AL-06": (recipe.SUGGESTED, "storefront"),
        "AL-07": (recipe.OWNER_IN_CATALOG, "search"),
        "AL-08": (recipe.OWNERLESS, ""),
        "AL-09": (recipe.SUGGESTED, "identity"),
        "AL-10": (recipe.OWNER_IN_CATALOG, "storefront"),
        "AL-11": (recipe.SUGGESTED, "streaming"),
    }
    got = {alert_id: (route.status, route.team) for alert_id, route in routes.items()}
    assert got == expected, got
    assert routes["AL-10"].found_by == "app=checkout-ui", routes["AL-10"]
    print("examples: the 11 committed alerts give the README's statuses and teams ok")


def check_lookups() -> None:
    # Exact match, ignoring case and surrounding spaces; a shorter or longer name is a different
    # service.
    assert CATALOG.find_service(" PAYMENTS-API ") == Service("payments-api", "payments")
    assert CATALOG.find_service("Frontend") == Service("web-frontend", "storefront")
    for near in ("checkout", "checkout-u", "payments-api-v2", "search", "auth-service-2", ""):
        assert CATALOG.find_service(near) is None, near
    # Hosts by address or host name, port dropped; a neighbouring address is not the host.
    assert INVENTORY.find_host("10.0.4.17:9187") == "orders-db"
    assert INVENTORY.find_host("PG-ORDERS-2:9187") == "orders-db"
    assert INVENTORY.find_host("10.0.4.17") == "orders-db"
    for near in ("10.0.4.170:9187", "10.0.4.1:9187", "10.0.4.1", "pg-orders:9187", ""):
        assert INVENTORY.find_host(near) is None, near
    assert host_part("[fd00::7]:9100") == "fd00::7"
    assert host_part("fd00::7") == "fd00::7"
    assert INVENTORY.find_namespace("Checkout") == "checkout-ui"
    assert INVENTORY.find_namespace("checkout-v3") is None
    # A catalog name used by two services is refused, not silently overwritten.
    folder = Path(tempfile.mkdtemp())
    shutil.copy(recipe.DATA_DIR / "catalog_teams.csv", folder)
    (folder / "catalog_services.csv").write_text(
        "service,owner,aliases,note\na-api,payments,shared,\nb-api,search,Shared,\n",
        encoding="utf-8",
    )
    caught = raised(lambda: ServiceCatalog(folder))
    assert type(caught) is ValueError, caught
    assert "'Shared'" in str(caught), caught
    # Fix: an alias written with spaces around it is stored and reported without them.
    (folder / "catalog_services.csv").write_text(
        "service,owner,aliases,note\na-api,payments,shared,\nb-api,search, Shared ,\n",
        encoding="utf-8",
    )
    caught = raised(lambda: ServiceCatalog(folder))
    assert "'Shared'" in str(caught), caught
    # Fix: a team id with spaces around it still matches the services it owns.
    (folder / "catalog_teams.csv").write_text(
        "team,description\n payments , card charges \n", encoding="utf-8"
    )
    (folder / "catalog_services.csv").write_text(
        "service,owner,aliases,note\na-api,payments,,\n", encoding="utf-8"
    )
    spaced = ServiceCatalog(folder)
    assert spaced.teams() == {"payments": "card charges"}, spaced.teams()
    # Fix: a catalog with no teams fails at load with a clear message, not deep in the run.
    (folder / "catalog_teams.csv").write_text("team,description\n", encoding="utf-8")
    caught = raised(lambda: ServiceCatalog(folder))
    assert type(caught) is ValueError, caught
    assert "lists no teams" in str(caught), caught
    # Repeated alert ids would overwrite each other's rows.
    caught = raised(lambda: recipe.parse_alerts([alert("A"), alert("A")]))
    assert type(caught) is ValueError, caught
    print("lookups: exact names, aliases, hosts, namespaces; near misses stay unknown ok")


def check_records() -> None:
    # A known label decides; the model is not asked (StandIn has no reply to give).
    route = one(service="payments-api")
    assert (route.status, route.team, route.found_by) == (
        recipe.OWNER_IN_CATALOG,
        "payments",
        "service=payments-api",
    ), route
    assert route.model_read == ""
    assert route.score is None
    # The text says database, the label says search-es: the label wins, no model call.
    route = one(service="search-es")
    assert (route.status, route.team) == (recipe.OWNER_IN_CATALOG, "search"), route
    # An alias under another label key, another case (AL-02).
    route = one(app="Frontend")
    assert route.team == "storefront", route
    assert "an alias of web-frontend" in route.reason, route
    # The instance's host is in the inventory (AL-03).
    route = one(instance="10.0.4.17:9187")
    assert (route.status, route.team) == (recipe.OWNER_IN_CATALOG, "data-platform"), route
    assert "a host of orders-db in the host inventory" in route.reason, route
    # Fix: an unknown first label doesn't hide a known second one (AL-10).
    route = one(service="checkout-v3", app="checkout-ui")
    assert (route.status, route.team, route.found_by) == (
        recipe.OWNER_IN_CATALOG,
        "storefront",
        "app=checkout-ui",
    ), route
    assert "also: service=checkout-v3 is not in the service catalog" in route.reason, route
    # Two labels, two services, one owner: still owner_in_catalog, both shown.
    route = one(service="search-api", instance="10.0.7.32:9200")
    assert (route.status, route.team) == (recipe.OWNER_IN_CATALOG, "search"), route
    assert route.found_by == "service=search-api; instance=10.0.7.32:9200", route
    # Fix: two labels that lead to different owners are a conflict, never settled by order.
    for labels in (
        {"service": "payments-api", "app": "checkout-ui"},
        {"app": "checkout-ui", "service": "payments-api"},
        {"service": "search-es", "namespace": "payments"},
    ):
        route = one(**labels)
        assert route.status == recipe.NO_CLEAR_OWNER, (labels, route)
        assert route.team == "", (labels, route)
        assert route.reason.startswith("labels lead to different owners: "), route
    # One label value holding two services: both are looked up, the second is never dropped.
    route = one(service="payments-api,search-api")
    assert route.status == recipe.NO_CLEAR_OWNER, route
    assert "service=search-api" in route.reason
    assert "service=payments-api" in route.reason
    route = one(service="payments-api, payments")
    assert (route.status, route.team) == (recipe.OWNER_IN_CATALOG, "payments"), route
    # A known service with no owner: ownerless, and the model isn't asked (AL-08).
    route = one(service="legacy-reports")
    assert (route.status, route.team) == (recipe.OWNERLESS, ""), route
    assert route.reason == "service=legacy-reports: in the service catalog, with no owner recorded"
    # An owned service next to an ownerless one is a conflict, not either answer.
    route = one(service="legacy-reports", app="checkout-ui")
    assert route.status == recipe.NO_CLEAR_OWNER, route
    # An owner the catalog's team list doesn't have, and an inventory row naming a service the
    # catalog doesn't know, are no confident owner either.
    folder = Path(tempfile.mkdtemp())
    for name in ("catalog_teams.csv", "inventory_namespaces.csv"):
        shutil.copy(recipe.DATA_DIR / name, folder)
    (folder / "catalog_services.csv").write_text(
        "service,owner,aliases,note\norphan-api,ghost-team,,\norders-db,data-platform,,\n",
        encoding="utf-8",
    )
    (folder / "inventory_hosts.csv").write_text(
        "address,hostname,service\n10.9.9.9,old-box,retired-svc\n", encoding="utf-8"
    )
    routes, _, _ = run([alert("A", service="orphan-api")], data_dir=folder)
    assert routes["A"].status == recipe.NO_CLEAR_OWNER, routes["A"]
    assert "ghost-team is not a team in the catalog" in routes["A"].reason, routes["A"]
    routes, _, _ = run(
        [alert("B", instance="10.9.9.9:9100")], reply(storefront=0.9), data_dir=folder
    )
    assert "runs retired-svc per the host inventory, which is not in the service catalog" in (
        routes["B"].reason
    ), routes["B"]
    print("records: every label checked, conflicts and two-in-one labels held, ownerless kept ok")


def check_model() -> None:
    # Clear top team and margin: a suggestion, with the runner-up and a "check it" note.
    route = one(reply(identity=0.86, storefront=0.08), service="auth-v2")
    assert (route.status, route.team, route.found_by) == (recipe.SUGGESTED, "identity", "model")
    assert route.runner_up == "storefront", route
    assert route.margin is not None, route
    assert route.reason.startswith("service=auth-v2 is not in the service catalog; "), route
    assert route.reason.endswith("check it before reassigning"), route
    assert "service: auth-v2" in route.model_read
    assert "alertname" not in route.model_read
    # A low top score: no_clear_owner, the model's pick still shown.
    route = one(reply(platform=0.46, storefront=0.40), namespace="edge")
    assert (route.status, route.team, route.suggested_team) == (
        recipe.NO_CLEAR_OWNER,
        "",
        "platform",
    ), route
    assert "scored 0.46, under 0.50 (runner-up storefront 0.40)" in route.reason, route
    # A close runner-up is contrary evidence, kept: a split, not a suggestion.
    route = one(reply(storefront=0.55, platform=0.39), namespace="edge")
    assert route.status == recipe.NO_CLEAR_OWNER, route
    assert "model split: storefront 0.55 vs platform 0.39, margin 0.16 under 0.20" in route.reason
    # Exactly at the limits passes.
    route = one(reply(streaming=0.50, storefront=0.30))
    assert route.status == recipe.SUGGESTED, route
    # Fix: 0.70 - 0.50 is 0.19999999999999996 in floating point; the margin is still 0.20.
    route = one(exact_reply(streaming=0.70, storefront=0.50))
    assert route.status == recipe.SUGGESTED, route
    assert route.margin == 0.2, route
    # No label to look up at all: the reason says the alert names no system (AL-06).
    route = one(reply(storefront=0.77, streaming=0.06))
    assert route.status == recipe.SUGGESTED, route
    assert route.reason.startswith(
        "no service, app, job, instance or namespace label: the alert names no system"
    ), route
    # Hidden labels never reach the model; an unknown instance is still named in the reason.
    route = one(reply(streaming=0.53, storefront=0.22), instance="10.0.6.14:9308", cluster="k")
    assert "10.0.6.14" not in route.model_read
    assert "cluster: k" in route.model_read
    assert route.reason.startswith("instance=10.0.6.14:9308 is not in the host inventory; ")
    print("model: score and margin limits, runner-up kept, no-system reason, hidden labels ok")


def check_replies() -> None:
    good = reply(identity=0.8)
    assert (
        recipe.read_probabilities(good, TEAMS) == good["data"][recipe.QUESTION_ID]["probabilities"]
    )
    probs = probabilities(identity=0.8)
    bad: list[tuple[Any, str]] = [
        ({"error": {"code": "x"}}, "has no `data.owning_team.probabilities` (item error"),
        ({"data": {}}, "has no `data.owning_team.probabilities`"),
        ({"data": {recipe.QUESTION_ID: {"choice": "identity"}}}, "has no `data."),
        ("not a dict", "has no `data."),
        ({"data": {recipe.QUESTION_ID: {"probabilities": {"identity": 1.0}}}}, "missing ["),
        ({"data": {recipe.QUESTION_ID: {"probabilities": probs | {"ops": 0.0}}}}, "extra ['ops']"),
    ]
    for value in (float("nan"), float("inf"), 1.5, -0.1, True, "0.9", None):
        broken: dict[str, Any] = {**probs, "identity": value}
        bad.append(({"data": {recipe.QUESTION_ID: {"probabilities": broken}}}, "not a number"))
    for answer, words in bad:
        caught = raised(functools.partial(recipe.read_probabilities, answer, TEAMS))
        assert type(caught) is SIEError, (answer, caught)
        assert words in str(caught), (answer, caught)
    # A malformed reply in a real run: that alert is no_clear_owner, the run goes on.
    routes, _, _ = run(
        [alert("A"), alert("B", service="auth-v2")],
        {"data": {recipe.QUESTION_ID: {"probabilities": probs | {"identity": float("nan")}}}},
        reply(identity=0.9),
    )
    assert routes["A"].status == recipe.NO_CLEAR_OWNER
    assert routes["A"].suggested_team == ""
    assert (
        "team model: the SIE call failed: SIEError: the reply's probability" in routes["A"].reason
    )
    assert routes["B"].status == recipe.SUGGESTED
    print("replies: missing keys, wrong teams and non-numbers are failed calls ok")


ALERTS = [alert("A", service="payments-api"), alert("B", service="auth-v2"), alert("C")]
OK = reply(identity=0.9)

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


def check_sie_failure() -> None:
    for error in TRANSIENT:
        assert not recipe.is_fatal(error), f"{type(error).__name__} should be transient"
    for error in FATAL:
        assert recipe.is_fatal(error), f"{type(error).__name__} should stop the run"

    # A transient failure: that alert is no_clear_owner with the error cut short; the catalog
    # row and the other model alert still come out, and the outputs are written.
    long = "connection refused " + "x" * 300
    for error in [*TRANSIENT, SIEConnectionError(long)]:
        routes, output, _ = run(ALERTS, error, OK)
        assert routes["A"].status == recipe.OWNER_IN_CATALOG
        assert routes["B"].status == recipe.NO_CLEAR_OWNER
        assert routes["B"].team == ""
        prefix = "service=auth-v2 is not in the service catalog; team model: the SIE call failed: "
        assert routes["B"].reason.startswith(prefix), routes["B"].reason
        assert routes["C"].status == recipe.SUGGESTED
        assert (output / "routes.csv").exists()
        assert (output / "routes.json").exists()
    assert len(routes["B"].reason) == len(prefix) + len("SIEConnectionError: ") + recipe.ERROR_CHARS

    def stops(*replies: Any, alerts: list[dict[str, Any]] = ALERTS) -> str:
        """The stop message; also checks the run wrote nothing."""
        folder = Path(tempfile.mkdtemp())
        message = stopped(functools.partial(run, alerts, *replies, folder=folder))
        assert not (folder / "out").exists(), "a stopped run wrote outputs"
        return message

    # Every call failed: the run stops and writes nothing, even with a catalog row ready.
    message = stops(SIEConnectionError(long), SIEConnectionError(long))
    assert message.startswith(f"Every SIE call failed (2 of 2 to {MODEL})"), message
    assert long in message, message
    assert "Check SIE_BASE_URL and your network." in message, message
    assert "No outputs were written." in message
    for error in [ProvisioningError("no GPU yet"), ServerError("unavailable", status_code=503)]:
        message = stops(error, error)
        assert "may still be starting or SIE is busy; run again in a few minutes." in message
        assert "SIE_BASE_URL" not in message, message
    # A reply missing its key on every call is every call failing too.
    message = stops({"data": {}}, {"data": {}})
    assert message.startswith("Every SIE call failed (2 of 2"), message
    # No model call at all (every alert placed by a record) is not "every call failed".
    routes, _, _ = run([alert("A", service="payments-api")])
    assert routes["A"].status == recipe.OWNER_IN_CATALOG
    # A refusal on a later call stops the run at once, with SIE's full message.
    message = stops(OK, InsufficientCreditsError("no credits " + "y" * 300))
    assert message.startswith(f"SIE refused the call to {MODEL}, "), message
    assert "InsufficientCreditsError (HTTP 402, code INSUFFICIENT_CREDITS)" in message, message
    assert "y" * 300 in message
    assert "SIE_API_KEY" in message
    message = stops(RequestError("bad key", status_code=401))
    assert message.startswith(f"SIE refused the call to {MODEL}, RequestError (HTTP 401)")
    message = stops(ModelLoadFailedError("gated", permanent=True))
    assert message.startswith(f"SIE can't load {MODEL}, ModelLoadFailedError"), message
    assert "SIE_API_KEY" not in message, message
    message = stops(RequestError("unexpected body"))
    assert "SIE_API_KEY" not in message, message
    assert "SIE_BASE_URL" in message, message
    # A bug in our own code is not hidden, and main turns only SIEStopped into a clean exit.
    caught = raised(lambda: run(ALERTS, TypeError("bad argument")))
    assert type(caught) is TypeError, f"TypeError became {type(caught).__name__}"

    def bug() -> None:
        raise RuntimeError("a bug in recipe.py")

    def refusal() -> None:
        raise recipe.SIEStopped("SIE refused")

    caught = raised(lambda: recipe.stop_on_refusal(bug))
    assert type(caught) is RuntimeError, f"a RuntimeError became {type(caught).__name__}"
    caught = raised(lambda: recipe.stop_on_refusal(refusal))
    assert type(caught) is SystemExit, caught
    assert str(caught) == "Stopped: SIE refused", caught
    print("sie failure: transient errors give no_clear_owner; refusals and all-failed stop ok")


def check_outputs() -> None:
    # The four statuses name what the checks found; none of them is a routing decision.
    assert recipe.STATUSES == (
        "owner_in_catalog",
        "ownerless_service",
        "team_suggested",
        "no_clear_owner",
    )
    assert not {"triage", "catalog", "model", "routed", "assigned"} & set(recipe.STATUSES)
    for word in ("triage_queue", "sre-triage", "service_label_keys"):
        assert word not in (recipe.RULES_PATH.read_text(encoding="utf-8")), word
    # Label values reach the CSV as text, never as a spreadsheet formula.
    risky = alert("A", app="checkout-ui")
    risky["labels"]["alertname"] = '=HYPERLINK("x")'
    _routes, output, _ = run([risky])
    rows = list(csv.DictReader((output / "routes.csv").open(encoding="utf-8")))
    assert list(rows[0]) == list(recipe.ROUTE_FIELDS), list(rows[0])
    assert rows[0]["status"] == recipe.OWNER_IN_CATALOG
    assert rows[0]["alertname"] == '\'=HYPERLINK("x")', rows[0]["alertname"]
    for cell in rows[0].values():
        assert not cell.startswith(recipe.FORMULA_START), cell
    saved = json.loads((output / "routes.json").read_text(encoding="utf-8"))
    assert saved[0]["team"] == "storefront"
    assert saved[0]["score"] == ""
    print("outputs: four finding statuses, CSV cells safe, JSON and CSV agree ok")


if __name__ == "__main__":
    check_data()
    check_examples()
    check_lookups()
    check_records()
    check_model()
    check_replies()
    check_sie_failure()
    check_outputs()
