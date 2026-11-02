"""Check the recipe's indicator regexes, alias rules and review rules offline.

The checks use hand-written strings, the committed data/ and hand-written stand-in model
replies. Nothing here calls SIE or replays a recorded response. Run it with
`uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

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
        self.replies = replies

    def extract(self, model: str, items: list[Any], **_: Any) -> list[dict[str, Any]]:
        return [self.replies.pop(0) for _ in items]


def span(text: str, name: str, label: str) -> dict[str, Any]:
    start = text.index(name)
    return {"text": name, "label": label, "start": start, "end": start + len(name), "score": 0.9}


def run(text: str, replies: list[dict[str, Any]], **policy: bool) -> recipe.TaggedReport:
    rules = {**RULES, "policy": {**RULES["policy"], **policy}}
    techniques = recipe.load_techniques()
    table = recipe.AliasTable.build(rules, techniques)
    client = cast(SIEClient, StandIn(replies))
    report = recipe.Report("T-1", "2099-01-01", "test", text)
    return recipe.tag_reports(client, [report], table, techniques, rules)[0]


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
    bad = {"text": "Heron", "label": "threat actor", "start": 0, "end": 5, "score": 0.9}
    assert "invalid span" in run(text, [{"entities": [bad]}]).reasons[0]
    failed = run(text, [{"error": "model timed out"}])
    assert failed.reasons == ["window 1 was not tagged: model timed out"]
    assert failed.actors == ["Ashen Heron"]  # the alias table still finds known names
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
        "tagged",
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
            return recipe.tag_reports(client, reports, table, techniques, rules)

    # A transient error on one call of two: that report goes to review, the other is tagged.
    for error in TRANSIENT:
        tagged = tag(error, None)
        assert [r.status for r in tagged] == ["review", "tagged"], type(error).__name__
    long = "connection refused " + "x" * 300
    tagged = tag(SIEConnectionError(long), None)
    reason = "window 1 was not tagged: the SIE call failed: SIEConnectionError: connection refused"
    assert tagged[0].reasons[0].startswith(reason)
    assert len(tagged[0].reasons[0]) == len(reason) - len("connection refused") + 200
    assert tagged[0].actors == ["Ashen Heron"]  # the table still finds known names
    with tempfile.TemporaryDirectory() as folder:
        recipe.write_outputs(Path(folder), tagged)
        assert (Path(folder) / "entities.csv").read_text(encoding="utf-8").count("\n") == 5
    # Every call failed (a dead SIE_BASE_URL): the run stops, with the full message.
    message = stopped(lambda: tag(SIEConnectionError(long), SIEConnectionError(long)))
    assert message.startswith("Every SIE call failed (2 of 2 to numind/NuNER_Zero)"), message
    assert long in message
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
        recipe.write_outputs(Path(folder), [report])
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
        assert quiet.status == "tagged"
        assert quiet.candidate_techniques
    # A cue in another sentence doesn't hold the technique back.
    text = "No patch was applied. The operators ran Mimikatz."
    assert run(text, [{"entities": []}]).status == "tagged"
    print("technique cues: R-09 backup and R-10 negation hold candidate techniques ok")


def check_indicator_policy() -> None:
    text = "See https://evil.example/a and 999[.]1[.]1[.]1."
    assert run(text, [{"entities": []}]).reasons == [
        "live URL in the report: hxxps://evil[.]example/a",
        "malformed ip 999[.]1[.]1[.]1: not a valid IPv4 address",
    ]
    quiet = run(text, [{"entities": []}], live_ioc=False, malformed_indicator=False)
    assert (quiet.status, quiet.reasons) == ("tagged", [])
    print("indicator policy: live URL and malformed IP reasons follow the policy ok")


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
