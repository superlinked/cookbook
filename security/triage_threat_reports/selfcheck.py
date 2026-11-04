"""Check the recipe's indicator regexes, alias rules, lookups and status rules offline.

The checks use hand-written strings, the committed data/ and hand-written stand-in model
replies. Nothing here calls SIE or replays a recorded response. Run it with
`uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import functools
import io
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from asset_inventory import AssetInventory
from scanner import Scanner
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
SCANNER, INVENTORY = Scanner(), AssetInventory()


def iocs(text: str) -> list[tuple[str, str]]:
    """(type, value) for every indicator, with a ! on the type when it's malformed."""
    found = recipe.find_indicators(text, RULES)
    return [(i.type + "!" * i.malformed, i.value) for i in found]


def error_of(call: Callable[[], object]) -> str:
    """The ValueError message the call raises, or "" when it raises nothing."""
    try:
        call()
    except ValueError as error:
        return str(error)
    return ""


def live(text: str) -> list[bool]:
    return [i.live_in_source for i in recipe.find_indicators(text, RULES)]


def check_defang() -> None:
    url = ("url", "hxxps://evil[.]example/a")
    for text in [
        "hxxps://evil[.]example/a",
        "hxxps[:]//evil(.)example/a",
        "https://evil.example/a",
    ]:
        assert iocs(f"see {text}.") == [url], text
    assert iocs("hxxp://evil[.]example:8080/a") == [("url", "hxxp://evil[.]example:8080/a")]
    assert iocs("10(.)0(.)0(.)1 and 198[.]51[.]100[.]23.") == [
        ("ip", "10[.]0[.]0[.]1"),
        ("ip", "198[.]51[.]100[.]23"),
    ]
    assert iocs("see [1]192[.]0[.]2[.]5 now") == [("ip", "192[.]0[.]2[.]5")]  # footnote mark
    assert iocs("at evil[.]example[.]com.Then we") == [("domain", "evil[.]example[.]com")]
    assert recipe.defang("xhttp://a.b") == "xhxxp://a[.]b"  # a scheme after a letter
    print("defang: [.], (.), [:], hxxp, ports, IP after [1], full stop with no space ok")


def check_live() -> None:
    assert live("https://evil.example/login[.]php") == [True]  # a browser still opens it
    assert live("hxxps://evil.example/x https://evil[.]example/x") == [False, False]
    # Nested and percent-encoded schemes are defanged too.
    assert iocs("hxxps://r[.]example/?u=http://evil.example/") == [
        ("url", "hxxps://r[.]example/?u=hxxp://evil[.]example/")
    ]
    assert iocs("hxxps://r[.]example/?u=https%3a%2f%2fevil.example") == [
        ("url", "hxxps://r[.]example/?u=hxxps%3a%2f%2fevil[.]example")
    ]
    # hunt.csv refangs every scheme defang wrote, the nested one too.
    nested = "hxxps://go[.]example[.]com/r?u=hxxp://evil[.]example/x"
    assert recipe.refang(nested) == "https://go.example.com/r?u=http://evil.example/x"
    assert recipe.refang("hxxps://r[.]example/?u=hxxps%3a%2f%2fe") == (
        "https://r.example/?u=https%3a%2f%2fe"
    )
    [url] = recipe.find_indicators("see " + nested.replace("hxxp:", "http:") + " now", RULES)
    assert recipe.refang(url.value) == "https://go.example.com/r?u=http://evil.example/x"
    print("live: live scheme and host, defanged hosts, nested and encoded schemes ok")


def check_emails() -> None:
    domain = ("domain", "evil[.]example[.]com")
    for text in [
        "user@evil[.]example[.]com",
        "billing[@]evil.example.com",
        "from @evil[.]example[.]com today",
        "from 'user'@evil[.]example[.]com today",
        "mail john[.]smith@evil[.]example[.]com now",
        "from " + "ab12" * 8 + "@evil[.]example[.]com",  # a hex local part is not an MD5
    ]:
        assert iocs(text) == [domain], text
    assert iocs("first[.]last@corp.example.com") == [("domain", "corp[.]example[.]com")]
    assert iocs("ceo[.]office[.]dw[@]mailbox[.]example[.]net") == [
        ("domain", "mailbox[.]example[.]net")
    ]
    assert iocs("write to user@example.com") == []  # live emails are not indicators
    print("emails: subdomain, [@] only, missing, quoted, defanged or hex local part ok")


def check_malformed() -> None:
    assert iocs("0f953ecb6bdc95f1aaddb8a98b27db0") == [("hash!", "0f953ecb6bdc95f1aaddb8a98b27db0")]
    assert iocs("CVE-2099-123") == [("cve!", "CVE-2099-123")]
    assert iocs("from 999[.]1[.]1[.]1 now") == [("ip!", "999[.]1[.]1[.]1")]
    assert iocs("from 1[.]2[.]3[.]4[.]5 now") == [("domain!", "1[.]2[.]3[.]4[.]5")]
    # A bad top-level domain is kept whole, not cut back to evil[.]example.
    assert iocs("see evil[.]example[.]c0m now") == [("domain!", "evil[.]example[.]c0m")]
    assert iocs("x@evil[.]example[.]c0m") == [("domain!", "evil[.]example[.]c0m")]
    # Punycode TLDs and defanged versions go to review: the regexes can't vouch for them.
    assert iocs("see evil[.]xn--p1ai now") == [("domain!", "evil[.]xn--p1ai")]
    assert iocs("version 4[.]2[.]0 shipped") == [("domain!", "4[.]2[.]0")]
    print("malformed: short hash and CVE, bad IP, bad TLD, punycode, defanged version ok")


def check_once_and_near_misses() -> None:
    url = "hxxps://nvd[.]example/CVE-2099-41077/" + "a" * 64
    assert iocs(url.replace("hxxps", "https")) == [("url", url)]
    assert iocs("build 4.2.0.118 and the file mothvine_cfg.dat") == []
    print("once: CVE and hash inside a URL counted once; live version and file name skipped ok")


def check_aliases() -> None:
    techniques = recipe.load_techniques()
    table = recipe.AliasTable.build(RULES, techniques)
    text = "ngrok tunnels to hxxps://a1b2[.]ngrok[.]io, then ngrok again"
    found = recipe.table_mentions(text, table, recipe.find_indicators(text, RULES))
    assert [(e.text, e.start) for e in found] == [("ngrok", 0), ("ngrok", 49)]
    clash = {**RULES, "malware": {"Mimikatz": []}}
    assert "listed as both" in error_of(lambda: recipe.AliasTable.build(clash, techniques))
    print("aliases: no tool inside a URL; a name under two kinds is refused ok")


def check_config() -> None:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "rules.yaml"
        path.write_text("labels: {actor: threat actor, malware: malware}\n", encoding="utf-8")
        assert "exactly the keys" in error_of(lambda: recipe.load_rules(path))
    row = recipe.csv_safe({"value": "=HYPERLINK(1)", "plain": "-x", "count": 3, "ok": "a=b"})
    assert row == {"value": "'=HYPERLINK(1)", "plain": "'-x", "count": 3, "ok": "a=b"}
    print("config: label keys checked at load; formula cells quoted ok")


def check_planted() -> None:
    reports = {r.id: r.text for r in recipe.load_reports()}
    assert iocs(reports["R-06"]) == [
        ("url", "hxxps://login-portal[.]example[.]com/owa/"),
        ("url", "hxxp://192[.]0[.]2[.]44/q[.]ps1"),
        ("hash!", "0f953ecb6bdc95f1aaddb8a98b27db0"),
    ]
    assert live(reports["R-06"]) == [True, True, False]
    assert iocs(reports["R-07"]) == [("domain", "cdn-sync[.]example[.]org")]
    print("planted: R-06 two live URLs and a short hash; R-07 one domain ok")


class StandIn:
    """Stands in for SIEClient: returns hand-written entity dicts, one reply per window."""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = list(replies)

    def extract(self, model: str, items: list[Any], **_: Any) -> list[dict[str, Any]]:
        return [self.replies.pop(0) for _ in items]


def span(text: str, name: str, label: str) -> dict[str, Any]:
    start = text.index(name)
    return {"text": name, "label": label, "start": start, "end": start + len(name), "score": 0.9}


def run(
    text: str,
    replies: list[dict[str, Any]],
    scanner: Scanner = SCANNER,
    inventory: AssetInventory = INVENTORY,
    **policy: bool,
) -> recipe.TaggedReport:
    rules = {**RULES, "policy": {**RULES["policy"], **policy}}
    techniques = recipe.load_techniques()
    table = recipe.AliasTable.build(rules, techniques)
    client = cast(SIEClient, StandIn(replies))
    report = recipe.Report("T-1", "2099-01-01", "test", text)
    return recipe.tag_reports(client, [report], table, techniques, rules, scanner, inventory)[0]


def check_review() -> None:
    text = "Ashen Heron ran Mimikatz and Chisel with Velvet Ostrich and Zinc Worm."
    found = [
        span(text, "Ashen Heron", "threat actor"),
        span(text, "Mimikatz", "tool"),
        span(text, "Chisel", "tool"),
        span(text, "Velvet Ostrich", "threat actor"),
        span(text, "Zinc Worm", "malware"),
    ]
    report = run(text, [{"entities": found}])
    assert (report.status, report.actors, report.tools) == ("review", ["Ashen Heron"], ["Mimikatz"])
    assert [t["id"] for t in report.candidate_techniques] == ["T1003.001"]
    assert report.reasons == [
        "Chisel: tool 'Chisel' has no ATT&CK mapping in rules.yaml",
        "Velvet Ostrich: actor 'Velvet Ostrich' is not in the alias table",
        "Zinc Worm: malware 'Zinc Worm' is not in the alias table",
    ]
    quiet = run(text, [{"entities": found}], unknown_actor=False, unknown_malware=False)
    assert [t.status for t in quiet.tags if t.mention != "Chisel"].count("new") == 2
    chisel = run(
        text, [{"entities": found}], unknown_actor=False, unknown_malware=False, unmapped_tool=False
    )
    assert (chisel.status, chisel.reasons) == ("filed", []), chisel.reasons
    bad = {"text": "Heron", "label": "threat actor", "start": 0, "end": 5, "score": 0.9}
    assert "invalid span" in run(text, [{"entities": [bad]}]).reasons[0]
    # A reply with no `entities` is a failed call: with one call, every call failed.
    message = stopped(lambda: run(text, [{"error": "model timed out"}]))
    assert "the reply has no `entities` (item error: model timed out)" in message, message
    print("review: techniques, unknown names, policy off, bad span, failed window ok")


class Broken:
    """Stands in for SIEClient: each call raises the next listed error, or, for None, returns
    no entities for every window."""

    def __init__(self, *errors: Exception | None) -> None:
        self.errors = list(errors)

    def extract(self, model: str, items: list[Any], **_: Any) -> list[dict[str, Any]]:
        error = self.errors.pop(0) if self.errors else None
        if error is not None:
            raise error
        return [{"entities": []} for _ in items]


def check_fallback() -> None:
    # Lower- and upper-case names the model missed are still found by the table.
    text = "LX-0417 ran powershell; ASHEN HERON again."
    report = run(text, [{"entities": []}])
    assert (report.status, report.actors, report.tools) == (
        "filed",
        ["Ashen Heron"],
        ["PowerShell"],
    )
    assert [t.mention for t in report.tags] == ["LX-0417", "powershell", "ASHEN HERON"]
    # An overlapping model span that reads as something else doesn't hide the exact name.
    text = "The operators ran PowerShell scripts."
    report = run(text, [{"entities": [span(text, "PowerShell scripts", "tool")]}])
    assert report.tools == ["PowerShell"]
    assert [t["id"] for t in report.candidate_techniques] == ["T1059.001"]
    assert report.reasons == [
        "PowerShell scripts: tool 'PowerShell scripts' has no ATT&CK mapping in rules.yaml"
    ]
    # A model span that resolves to the same name covers the table match: one tag, not two.
    text = "A Mothvine Loader stage ran."
    report = run(text, [{"entities": [span(text, "Mothvine Loader", "malware")]}])
    assert [(t.mention, t.source) for t in report.tags] == [("Mothvine Loader", "model")]
    print("fallback: any-case table names; an overlapping model span keeps the exact name ok")


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


def check_sie_failure() -> None:
    for error in TRANSIENT:
        assert not recipe.is_fatal(error), f"{type(error).__name__} should be transient"
    for error in FATAL:
        assert recipe.is_fatal(error), f"{type(error).__name__} should stop the run"
    techniques = recipe.load_techniques()
    rules = {**RULES, "thresholds": {**RULES["thresholds"], "batch_size": 1}}
    table = recipe.AliasTable.build(rules, techniques)
    reports = [recipe.Report(i, "2099-01-01", "t", "Ashen Heron ran Mimikatz.") for i in "AB"]

    def tag(*errors: Exception | None) -> list[recipe.TaggedReport]:
        client = cast(SIEClient, Broken(*errors))
        with contextlib.redirect_stdout(io.StringIO()):  # the run's "1 of 2 failed" line
            return recipe.tag_reports(client, reports, table, techniques, rules, SCANNER, INVENTORY)

    # A transient error on one call of two: that report goes to review, the other is tagged.
    for error in TRANSIENT:
        tagged = tag(error, None)
        assert [r.status for r in tagged] == ["review", "filed"], type(error).__name__
    long = "connection refused " + "x" * 300
    tagged = tag(SIEConnectionError(long), None)
    reason = "window 1 was not tagged: the SIE call failed: SIEConnectionError: connection refused"
    assert tagged[0].reasons[0].startswith(reason)
    assert len(tagged[0].reasons[0]) == len(reason) - len("connection refused") + 200
    assert tagged[0].actors == ["Ashen Heron"]  # the table still finds known names
    with tempfile.TemporaryDirectory() as folder:
        recipe.write_outputs(Path(folder), tagged, RULES)
        assert (Path(folder) / "entities.csv").read_text(encoding="utf-8").count("\n") == 5
    # Every call failed (a dead SIE_BASE_URL): the run stops, with the full message.
    message = stopped(lambda: tag(SIEConnectionError(long), SIEConnectionError(long)))
    assert message.startswith("Every SIE call failed (2 of 2 to numind/NuNER_Zero)"), message
    assert long in message
    assert "Check SIE_BASE_URL and your network." in message, message
    assert "still be starting" not in message, message
    # Any other transient error on every call more likely means a cold model or a busy SIE.
    for error in [
        ProvisioningError("no GPU yet"),
        ModelLoadingError("loading"),
        ServerError("unavailable", status_code=503),
        RateLimitError("slow down"),
    ]:
        message = stopped(functools.partial(tag, error, error))
        assert message.startswith("Every SIE call failed (2 of 2 to numind/NuNER_Zero)"), message
        assert "may still be starting or SIE is busy; run again in a few minutes." in message, (
            message
        )
        assert "SIE_BASE_URL" not in message, message
    # A model SIE marked as never loading stops the run and points at the model name.
    message = stopped(lambda: tag(ModelLoadFailedError("gated", permanent=True)))
    assert message.startswith("SIE can't load numind/NuNER_Zero, ModelLoadFailedError"), message
    assert "gated" in message, message
    assert "Check the model name in rules.yaml" in message, message
    assert "SIE_API_KEY" not in message, message
    # A refusal stops the run at once, with the full message, HTTP status and code.
    message = stopped(lambda: tag(None, InsufficientCreditsError("no credits " + "y" * 300)))
    assert "InsufficientCreditsError (HTTP 402, code INSUFFICIENT_CREDITS)" in message, message
    assert "y" * 300 in message
    assert "SIE_API_KEY" in message
    # A malformed reply has no HTTP status: the hint doesn't blame the key.
    message = stopped(lambda: tag(RequestError("unexpected body")))
    assert "SIE_API_KEY" not in message, message
    assert "SIE_BASE_URL" in message, message
    # A bug in our own code is not hidden: it is raised as is, and main doesn't turn a
    # RuntimeError that isn't SIEStopped into a clean exit.
    try:
        tag(TypeError("bad argument"))
    except TypeError:
        pass
    else:
        raise AssertionError("TypeError was swallowed")

    def bug() -> None:
        raise RuntimeError("a bug in recipe.py")

    def refusal() -> None:
        raise recipe.SIEStopped("SIE refused")

    caught = raised(lambda: recipe.stop_on_refusal(bug))
    assert type(caught) is RuntimeError, f"a RuntimeError became {type(caught).__name__}"
    assert str(caught) == "a bug in recipe.py"
    caught = raised(lambda: recipe.stop_on_refusal(refusal))
    assert type(caught) is SystemExit, "SIEStopped did not stop the run cleanly"
    assert str(caught) == "Stopped: SIE refused"
    print("sie failure: transient errors go to review; refusals and all-failed stop; bugs raise ok")


def check_held_reason() -> None:
    # Two tools, a cue in only one sentence: Mimikatz keeps its technique, rclone's is held.
    text = "Operators ran Mimikatz. Administrators used rclone to back up the server."
    report = run(
        text, [{"entities": [span(text, "Mimikatz", "tool"), span(text, "rclone", "tool")]}]
    )
    assert [(t.mention, t.techniques_held) for t in report.tags] == [
        ("Mimikatz", False),
        ("rclone", True),
    ]
    assert [t["id"] for t in report.candidate_techniques] == ["T1003.001"]
    with tempfile.TemporaryDirectory() as folder:
        recipe.write_outputs(Path(folder), [report], RULES)
        rows = (Path(folder) / "entities.csv").read_text(encoding="utf-8").splitlines()[1:]
        assert [row.split(",")[9] for row in rows] == ["False", "True"]
    # Holding techniques keeps the tag's earlier reason ("normalised to ...").
    text = "Administrators ran RCLONE to back up the server."
    report = run(text, [{"entities": [span(text, "RCLONE", "tool")]}])
    assert report.tags[0].reason == (
        "normalised to rclone; candidate techniques T1567.002 held: the sentence says 'back up'"
    ), report.tags[0].reason
    print("held reason: one tool held, the other kept; the earlier reason is kept ok")


def check_technique_cues() -> None:
    reports = {r.id: r.text for r in recipe.load_reports()}
    for report_id, tool, cue in [("R-09", "rclone", "back up"), ("R-10", "Mimikatz", "no")]:
        text = reports[report_id]
        report = run(text, [{"entities": [span(text, tool, "tool")]}])
        assert (report.status, report.tools, report.candidate_techniques) == ("review", [tool], [])
        assert report.reasons[0].endswith(f"held: the sentence says {cue!r}"), report.reasons
        quiet = run(text, [{"entities": [span(text, tool, "tool")]}], technique_cue=False)
        assert quiet.status == "filed"
        assert quiet.candidate_techniques
    # A cue in another sentence doesn't hold the technique back.
    text = "No patch was applied. The operators ran Mimikatz."
    assert run(text, [{"entities": []}]).status == "filed"
    print("technique cues: R-09 backup and R-10 negation hold candidate techniques ok")


def check_indicator_policy() -> None:
    text = "See https://evil.example/a and 999[.]1[.]1[.]1."
    assert run(text, [{"entities": []}]).reasons == [
        "live URL in the report: hxxps://evil[.]example/a",
        "malformed ip 999[.]1[.]1[.]1: not a valid IPv4 address",
    ]
    quiet = run(text, [{"entities": []}], live_ioc=False, malformed_indicator=False)
    assert (quiet.status, quiet.reasons) == ("hunt", [])  # the well-formed URL is huntable
    print("indicator policy: live URL and malformed IP reasons follow the policy ok")


def check_lookups() -> None:
    assert [(f.host, f.status) for f in SCANNER.find_cve("cve-2099-41077")] == [
        ("vpn-gw-01", "patched"),
        ("vpn-gw-02", "open"),
    ]
    assert SCANNER.find_cve("CVE-2099-00001") == []
    hosts = ["vpn-gw-01", "vpn-gw-02"]
    for name in ["Corvane VPN Gateway", "the corvane  gateway", "CVG"]:
        assert [a.host for a in INVENTORY.find_product(name)] == hosts, name
    # Exact names only: a shorter or a different name finds nothing.
    for name in ["Corvane VPN", "Corvane", "Corvane VPN Concentrator", "Corvane VPN Gateway 7"]:
        assert INVENTORY.find_product(name) == [], name
    assert INVENTORY.find_host("db-99") == []
    with tempfile.TemporaryDirectory() as folder:
        seed = Path(folder) / "findings.csv"
        seed.write_text("host,cve,status\nh,CVE-2099-1111,fixed?\n", encoding="utf-8")
        assert "status must be one of" in error_of(lambda: Scanner(seed))
    print("lookups: scanner by CVE any case; inventory by exact name or alias only ok")


def products(text: str) -> list[tuple[str, str, str]]:
    """(as written, inventory product, problem) for every product mention in the text."""
    indicators = recipe.find_indicators(text, RULES)
    found = recipe.find_products(text, INVENTORY, indicators, RULES)
    return [(p.text, p.product, p.problem.split(",")[0].split(";")[0]) for p in found]


def check_product_names() -> None:
    gateway = "Corvane VPN Gateway"
    assert products("in the Corvane VPN gateway.") == [("Corvane VPN gateway", gateway, "")]
    assert products("on the CVG and the Corvane gateway") == [
        ("CVG", gateway, ""),
        ("Corvane gateway", gateway, ""),
    ]
    # The near miss: a vendor we use, a product we don't.
    near = "close to the inventory's Corvane VPN Gateway but not the same name"
    assert products("in the Corvane VPN Concentrator, a separate line.") == [
        ("Corvane VPN Concentrator", "", near)
    ]
    # A longer name that starts with ours is not ours.
    [(written, product, problem)] = products("the Corvane VPN Gateway Manager console")
    assert (written, product) == ("Corvane VPN Gateway Manager", ""), written
    assert problem.startswith("a longer name than the inventory's Corvane VPN Gateway"), problem
    # Another version is not ours either; the one we run is, glued "v" or not.
    [(_, product, problem)] = products("Corvane VPN Gateway v8 is affected.")
    assert (product, problem) == (gateway, "the report names version 8"), problem
    for text in ["Corvane VPN Gateway 7.2 is affected.", "Corvane VPN Gateway v7 is affected."]:
        assert products(text) == [("Corvane VPN Gateway", gateway, "")], text
    assert products("Corvane VPN Gateway 7.20 is affected.")[0][2].startswith("the report names")
    # A version at the end of a sentence, after "version" or in brackets is still read.
    for text in [
        "Exploited in the Corvane VPN Gateway v8.",
        "Exploited in the Corvane VPN Gateway 6.1.",
        "Exploited in the Corvane VPN Gateway version 6.1 only.",
        "Exploited in the Corvane VPN Gateway (6.1).",
        "Exploited in the Corvane VPN Gateway 6.x builds.",
    ]:
        [(_, product, problem)] = products(text)
        assert (product, problem.split(";")[0][:24]) == (gateway, "the report names version"), text
    for text in ["Seen on the Corvane VPN Gateway 7.2.", "Seen on the Corvane VPN Gateway 7.x."]:
        assert products(text) == [("Corvane VPN Gateway", gateway, "")], text
    # Version wording the recipe can't compare is a problem, not a ticket for every host.
    for text in [
        "The flaw is in Corvane VPN Gateway versions 8.0 and later.",
        "Corvane VPN Gateway, 7.4 only, is hit.",
        "Corvane VPN Gateway prior to 7.0 is hit.",
        "Fixed in Corvane VPN Gateway releases before 7.0.",
    ]:
        [(_, product, problem)] = products(text)
        assert product == gateway, text
        assert problem.startswith("the report qualifies the version ("), (text, problem)
    # An indicator after the name is not version wording.
    assert products("the Corvane VPN Gateway from 203.0.113[.]9.") == [
        ("Corvane VPN Gateway", gateway, "")
    ]
    # A CVE right after the name is the CVE, not a longer product name.
    assert products("the Corvane VPN Gateway CVE-2099-41077 flaw") == [
        ("Corvane VPN Gateway", gateway, "")
    ]
    # Inside an indicator, or part of a longer word, a vendor name is not a product mention.
    assert products("served from corvane-update[.]example[.]com and Corvanex") == []
    print("product names: aliases, near miss, longer name, other or unread version, in an IOC ok")


def check_relevance() -> None:
    tool: list[dict[str, Any]] = [{"entities": []}]
    # An open CVE: a ticket for the owning team, the patched host is a note.
    text = "Ashen Heron exploited cve-2099-41077 in the Corvane VPN gateway."
    report = run(text, tool)
    assert report.status == "affects_us", report.reasons
    assert [(t.team, t.host, t.cve) for t in report.relevance.tickets] == [
        ("network", "vpn-gw-02", "CVE-2099-41077")
    ]
    assert report.relevance.notes == ["CVE-2099-41077: patched on vpn-gw-01, per the scanner"]
    # The same product with a patched CVE: no ticket; the indicator goes on the hunt list.
    text = "Access came through CVE-2099-50213 in the Corvane VPN gateway from 203.0.113[.]9."
    assert run(text, tool).status == "hunt"
    assert run(text.replace(" from 203.0.113[.]9", ""), tool).status == "filed"
    # A product with a CVE the scanner never checked on its hosts: review, not a quiet hunt.
    report = run("CVE-2099-61840 hits the Corvane VPN Gateway.", tool)
    assert report.status == "review", report.status
    assert (
        "the scanner has no result for CVE-2099-61840 on vpn-gw-01, vpn-gw-02"
        in (report.reasons[0])
    )
    # A CVE patched on every host doesn't vouch for a second CVE the scanner never checked.
    report = run("CVE-2099-50213 and CVE-2099-61840 hit the Corvane VPN Gateway.", tool)
    assert report.status == "review", report.status
    assert "no result for CVE-2099-61840 on vpn-gw-01, vpn-gw-02;" in report.reasons[0], (
        report.reasons
    )
    # A version range the recipe can't read, with no CVE: review, not a ticket per host.
    for text in [
        "The flaw is in Corvane VPN Gateway versions 8.0 and later.",
        "Corvane VPN Gateway, 7.4 only, is hit.",
        "Corvane VPN Gateway versions 8.0 through 8.2 are hit.",
    ]:
        report = run(text, tool)
        assert (report.status, report.relevance.tickets) == ("review", []), text
        assert "qualifies the version" in report.reasons[0], report.reasons
    # Another version at the end of a sentence, with no CVE: review, not a ticket per host.
    report = run("The group probes the Corvane VPN Gateway 6.1.", tool)
    assert (report.status, report.relevance.tickets) == ("review", []), report.status
    # The same CVE written twice gives one ticket.
    report = run("CVE-2099-41077 hit the CVG. Patch CVE-2099-41077 on the CVG.", tool)
    assert len(report.relevance.tickets) == 1, report.relevance.tickets
    # A product with no CVE: a ticket to check, one per host and team.
    report = run("The group probes the Larkfield Mail Relay and the Corvane gateway.", tool)
    assert report.status == "affects_us"
    assert [(t.team, t.host) for t in report.relevance.tickets] == [
        ("messaging", "mail-relay-01"),
        ("network", "vpn-gw-01"),
        ("network", "vpn-gw-02"),
    ]
    # Negation: "not affected" next to a product is not a ticket.
    for text in [
        "The Larkfield Mail Relay is not affected.",
        "Larkfield MTA isn't vulnerable to this chain.",
    ]:
        report = run(text, tool)
        assert (report.status, report.relevance.tickets) == ("review", []), text
        assert "the sentence says" in report.reasons[0], report.reasons
    # The near miss with an indicator: review, never hunt or affects_us.
    reports = {r.id: r.text for r in recipe.load_reports()}
    report = run(reports["R-11"], tool)
    assert (report.status, report.relevance.tickets) == ("review", [])
    # An open CVE on a host the inventory doesn't know, or an alias two products share.
    with tempfile.TemporaryDirectory() as folder:
        seed = Path(folder) / "findings.csv"
        seed.write_text("host,cve,status\nlab-07,CVE-2099-77777,open\n", encoding="utf-8")
        report = run("Patch CVE-2099-77777 now.", tool, scanner=Scanner(seed))
        assert (report.status, report.relevance.tickets) == ("review", [])
        assert "no owning team" in report.reasons[0], report.reasons
        seed = Path(folder) / "inventory.csv"
        rows = (recipe.ROOT / "data" / "asset_inventory.csv").read_text(encoding="utf-8")
        seed.write_text(rows + "lab-01,Larkfield Gateway,Larkfield,1.0,CVG,lab\n", "utf-8")
        report = run("They target the CVG.", tool, inventory=AssetInventory(seed))
        assert (report.status, report.relevance.tickets) == ("review", [])
        assert "lists this name under" in report.reasons[0], report.reasons
    # A model span on a product is the product, not an unmapped attacker tool.
    text = reports["R-05"]
    replies = [{"entities": [span(text, "Corvane VPN", "tool")]}]
    report = run(text, replies)
    assert "Corvane VPN" not in [t.mention for t in report.tags], report.tags
    assert (report.status, report.reasons) == ("hunt", []), report.reasons
    print("relevance: open CVE, patched CVE, no scanner result, no CVE, negation, near miss ok")


def check_outputs() -> None:
    text = "Ashen Heron used CVE-2099-41077 and https://evil.example/a in one push."
    report = run(text, [{"entities": []}])
    assert report.status == "affects_us"
    assert "live URL in the report: hxxps://evil[.]example/a" in report.reasons
    with tempfile.TemporaryDirectory() as folder:
        recipe.write_outputs(Path(folder), [report], RULES)
        ticket = (Path(folder) / "tickets" / "network.md").read_text(encoding="utf-8")
        assert "hxxps://evil[.]example/a" in ticket, ticket
        assert "https://" not in ticket, ticket
        assert "evil.example" not in ticket, ticket
        assert "- CVE-2099-41077 on `vpn-gw-02` (Corvane VPN Gateway): open" in ticket, ticket
        hunt = (Path(folder) / "hunt.csv").read_text(encoding="utf-8").splitlines()
        assert hunt[1:] == ["T-1,affects_us,url,https://evil.example/a,2099-01-01"], hunt
        iocs = (Path(folder) / "iocs.csv").read_text(encoding="utf-8")
        assert "https://" not in iocs
    # A live link in the title is defanged in tags.json and the ticket heading too.
    rules = RULES
    techniques = recipe.load_techniques()
    table = recipe.AliasTable.build(rules, techniques)
    titled = recipe.Report("T-2", "2099-01-01", "Lure at https://evil.example/t", text)
    client = cast(SIEClient, StandIn([{"entities": []}]))
    [report] = recipe.tag_reports(client, [titled], table, techniques, rules, SCANNER, INVENTORY)
    assert report.title == "Lure at hxxps://evil[.]example/t", report.title
    with tempfile.TemporaryDirectory() as folder:
        recipe.write_outputs(Path(folder), [report], RULES)
        ticket = (Path(folder) / "tickets" / "network.md").read_text(encoding="utf-8")
        assert "## T-2: `Lure at hxxps://evil[.]example/t`" in ticket, ticket
        assert "evil.example" not in (Path(folder) / "tags.json").read_text(encoding="utf-8")
    # A Markdown link in the title or the quoted sentence is quoted as code, not a live link.
    link = "[advisory](//evil.example/fix)"
    text = f"Ashen Heron used CVE-2099-41077, see {link} for `x`."
    titled = recipe.Report("T-3", "2099-01-01", f"Patch notes {link}", text)
    client = cast(SIEClient, StandIn([{"entities": []}]))
    [report] = recipe.tag_reports(client, [titled], table, techniques, rules, SCANNER, INVENTORY)
    with tempfile.TemporaryDirectory() as folder:
        recipe.write_outputs(Path(folder), [report], RULES)
        ticket = (Path(folder) / "tickets" / "network.md").read_text(encoding="utf-8")
        assert f"## T-3: `Patch notes {link}` (published" in ticket, ticket
        assert f"> ``{text}``" in ticket, ticket
    assert recipe.code_span("`a`") == "`` `a` ``"
    # Review and filed reports put nothing on the hunt list; a malformed or CVE indicator never.
    review = run("Velvet Ostrich used 198.51.100[.]7.", [{"entities": []}])
    review.status = "review"
    odd = run("See CVE-2099-12345 and 999[.]1[.]1[.]1.", [{"entities": []}])
    assert recipe.hunt_indicators(odd, RULES) == []
    with tempfile.TemporaryDirectory() as folder:
        recipe.write_outputs(Path(folder), [review, odd], RULES)
        assert (Path(folder) / "hunt.csv").read_text(encoding="utf-8").count("\n") == 1
        assert not any((Path(folder) / "tickets").iterdir())
    print("outputs: tickets defanged, hunt.csv the only live file, review not on the hunt list ok")


def check_missing_entities() -> None:
    techniques = recipe.load_techniques()
    rules = {**RULES, "thresholds": {**RULES["thresholds"], "batch_size": 1}}
    table = recipe.AliasTable.build(rules, techniques)
    reports = [recipe.Report(i, "2099-01-01", "t", "Ashen Heron ran Mimikatz.") for i in "AB"]
    client = cast(SIEClient, StandIn([{"data": []}, {"entities": []}]))
    with contextlib.redirect_stdout(io.StringIO()):
        tagged = recipe.tag_reports(client, reports, table, techniques, rules, SCANNER, INVENTORY)
    assert [r.status for r in tagged] == ["review", "filed"]
    assert tagged[0].reasons == [
        "window 1 was not tagged: the SIE call failed: the reply has no `entities`"
    ], tagged[0].reasons
    print("missing entities: a reply without the key is a failed call, [] is nothing found ok")


class Short:
    """Stands in for SIEClient: the first call returns one result fewer than it was sent, the
    rest return no entities."""

    def __init__(self) -> None:
        self.calls = 0

    def extract(self, model: str, items: list[Any], **_: Any) -> list[dict[str, Any]]:
        self.calls += 1
        return [{"entities": []} for _ in items[: len(items) - (self.calls == 1)]]


def check_short_reply() -> None:
    techniques = recipe.load_techniques()
    rules = {**RULES, "thresholds": {**RULES["thresholds"], "batch_size": 2}}
    table = recipe.AliasTable.build(rules, techniques)
    reports = [recipe.Report(i, "2099-01-01", "t", "Ashen Heron ran Mimikatz.") for i in "ABCD"]
    client = cast(SIEClient, Short())
    with contextlib.redirect_stdout(io.StringIO()):
        tagged = recipe.tag_reports(client, reports, table, techniques, rules, SCANNER, INVENTORY)
    assert [r.status for r in tagged] == ["review", "review", "filed", "filed"]
    assert "replied with 1 results for 2" in tagged[0].reasons[0], tagged[0].reasons
    print("short reply: one result fewer than sent is a failed call, not a crash ok")


def check_planted_status() -> None:
    reports = recipe.load_reports()
    client = cast(SIEClient, StandIn([{"entities": []} for _ in reports]))
    techniques = recipe.load_techniques()
    table = recipe.AliasTable.build(RULES, techniques)
    tagged = recipe.tag_reports(client, reports, table, techniques, RULES, SCANNER, INVENTORY)
    status = {r.id: r.status for r in tagged}
    # With no model spans at all; the live run adds the model's names on top. R-04 is left out:
    # its review reason (Velvet Ostrich, not in the alias table) needs a model span.
    assert {
        k: status[k] for k in ("R-01", "R-02", "R-03", "R-05", "R-06", "R-07", "R-08", "R-11")
    } == {
        "R-01": "affects_us",
        "R-02": "hunt",
        "R-03": "review",
        "R-05": "hunt",
        "R-06": "review",
        "R-07": "hunt",
        "R-08": "filed",
        "R-11": "review",
    }, status
    print(
        "planted status, no model spans: R-01 affects_us, R-02/R-05/R-07 hunt, "
        "R-03/R-06/R-11 review ok"
    )


if __name__ == "__main__":
    check_defang()
    check_live()
    check_emails()
    check_malformed()
    check_once_and_near_misses()
    check_aliases()
    check_config()
    check_review()
    check_indicator_policy()
    check_fallback()
    check_sie_failure()
    check_technique_cues()
    check_held_reason()
    check_planted()
    check_lookups()
    check_product_names()
    check_relevance()
    check_outputs()
    check_missing_entities()
    check_short_reply()
    check_planted_status()
