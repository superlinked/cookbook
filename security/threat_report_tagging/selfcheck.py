"""Check the recipe's indicator regexes and alias rules on hand-written strings. No SIE call.

Run it with `uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

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
    print("defang: [.], (.), [:], hxxp, ports and an IP before a full stop ok")


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
    ]:
        assert iocs(text) == [domain], text
    assert iocs("write to user@example.com") == []  # live emails are not indicators
    print("emails: subdomain, [@] only, no local part, quoted local part, live email ok")


def check_malformed() -> None:
    assert iocs("0f953ecb6bdc95f1aaddb8a98b27db0") == [("hash!", "0f953ecb6bdc95f1aaddb8a98b27db0")]
    assert iocs("CVE-2099-123") == [("cve!", "CVE-2099-123")]
    assert iocs("from 999[.]1[.]1[.]1 now") == [("ip!", "999[.]1[.]1[.]1")]
    assert iocs("from 1[.]2[.]3[.]4[.]5 now") == [("domain!", "1[.]2[.]3[.]4[.]5")]
    # A bad top-level domain is kept whole, not cut back to evil[.]example.
    assert iocs("see evil[.]example[.]c0m now") == [("domain!", "evil[.]example[.]c0m")]
    assert iocs("x@evil[.]example[.]c0m") == [("domain!", "evil[.]example[.]c0m")]
    print("malformed: short hash, short CVE, bad IP, five-part IP, bad TLD kept whole ok")


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


if __name__ == "__main__":
    check_defang()
    check_live()
    check_emails()
    check_malformed()
    check_once_and_near_misses()
    check_aliases()
    check_config()
    check_planted()
