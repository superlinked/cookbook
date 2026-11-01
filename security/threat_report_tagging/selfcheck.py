"""Check the recipe's indicator regexes, alias rules and review rules offline.

The checks use hand-written strings, the committed data/ and hand-written stand-in model
replies. Nothing here calls SIE or replays a recorded response. Run it with
`uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from sie_sdk import SIEClient

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
    assert [t["id"] for t in report.techniques] == ["T1003.001"]
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
    check_planted()
