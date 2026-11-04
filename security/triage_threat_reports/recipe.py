"""Triage threat reports against what your company runs: tag them, then check the scanner.

Use case:  Threat report triage: does this vendor or partner report affect us, and what do we
           search the logs for?
Who:       Cyber threat intelligence (CTI) analysts who read and file vendor and partner reports,
           the teams that own the systems, and the SOC that searches the logs.
Problem:   Names and indicators are messy ("LX-0417" is "Ashen Heron"; every vendor defangs
           URLs its own way), and the question that matters, "does this one affect us?", means
           looking CVEs up in the vulnerability scanner and product names up in the asset
           inventory by hand. The report that names an open CVE on your VPN gateway reads like
           the nine that don't concern you.
Input:     data/reports.jsonl (synthetic reports), data/attack_techniques.csv (a MITRE ATT&CK
           technique subset), the scanner's findings (scanner.py over
           data/scanner_findings.csv), the asset inventory (asset_inventory.py over
           data/asset_inventory.csv) and rules.yaml (the alias table, the tool mapping, the
           cues, the model ID and the thresholds).
SIE pipeline:
           (Python)                    pull CVEs, MD5/SHA-1/SHA-256 hashes and defanged URLs,
                                       domains and IPs by regex; find the inventory's product
                                       names, aliases and vendors as whole words
           (Python)                    split each report into sentence windows that fit
           extract  numind/NuNER_Zero  tag threat actor, malware and tool spans, all windows
                                       in one batched call per 16 windows
           (Python)                    normalize names through the alias table, look up each
                                       tool's candidate ATT&CK techniques, look every CVE up in
                                       the scanner and every product in the inventory, and set
                                       the status
Output:    In runs/<timestamp>/: tickets/<team>.md (a ticket draft per owning team), hunt.csv
           (the SOC's hunt list, the only file with live indicators), tags.json (every report
           with its tags, indicators, lookups, status and reasons), entities.csv and iocs.csv
           (defanged). Status per report: affects_us, hunt, filed or review. The analyst and
           the system owner decide what happens next.
Why SIE:   One small zero-shot NER model, no training: the labels are plain words in
           rules.yaml. No generative model makes any decision; the alias merges, lookups and
           statuses are code you can read. Nothing to host.
Runs on:   SIE Serverless.

    uv run recipe.py
"""

from __future__ import annotations

import argparse
import csv
import ipaddress
import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import yaml
from asset_inventory import AssetInventory
from dotenv import load_dotenv
from scanner import OPEN, Finding, Scanner
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
REPORTS_PATH = ROOT / "data" / "reports.jsonl"
TECHNIQUES_PATH = ROOT / "data" / "attack_techniques.csv"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
# A failed SIE call's message is cut to this many characters in reasons and the summary line.
ERROR_CHARS = 200

ACTOR, MALWARE, TOOL = "actor", "malware", "tool"
# Per mention: tagged, review or new. Per report: affects_us, hunt, filed or review.
TAGGED, REVIEW, NEW = "tagged", "review", "new"
AFFECTS_US, HUNT, FILED = "affects_us", "hunt", "filed"
ENTITY_FIELDS = (
    "report_id",
    "kind",
    "mention",
    "start",
    "end",
    "source",
    "score",
    "canonical",
    "candidate_techniques",
    "techniques_held",
    "status",
    "reason",
)
IOC_FIELDS = ("report_id", "type", "value", "live_in_source", "malformed", "note")
HUNT_FIELDS = ("report_id", "report_status", "type", "value", "published")
HASH_TYPES = {32: "md5", 40: "sha1", 64: "sha256"}
# A CSV cell starting with one of these opens as a formula in a spreadsheet.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


# --- Inputs -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Report:
    id: str
    published: str
    title: str
    text: str


@dataclass(frozen=True)
class Technique:
    id: str
    name: str
    tactic: str


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


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    """Read rules.yaml. The `labels` keys are fixed: the code reads actor, malware and tool."""
    rules = cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))
    keys = sorted(rules.get("labels") or {})
    if keys != sorted((ACTOR, MALWARE, TOOL)):
        raise ValueError(
            f"labels in {path.name} must have exactly the keys actor, malware and tool, got {keys}"
        )
    return rules


def load_reports(path: Path = REPORTS_PATH) -> list[Report]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [Report(**json.loads(line)) for line in lines if line.strip()]


def load_techniques(path: Path = TECHNIQUES_PATH) -> dict[str, Technique]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = [
            Technique(r["technique_id"], r["name"], r["tactic"]) for r in csv.DictReader(handle)
        ]
    return {t.id: t for t in rows}


# --- 1. Indicators: CVEs, hashes and IOCs by regex --------------------------------------------

URL_RE = re.compile(r"\b(?:hxxps?|https?)(?:\[:\]|:)//[^\s<>\"']+", re.IGNORECASE)
CVE_RE = re.compile(r"\bCVE-(\d{4})-(\d+)\b", re.IGNORECASE)
DOT = r"(?:\[\.\]|\(\.\)|\.)"
# Nothing starts right after a dot, defanged or not, so "a[.]b[.]example[.]com" is one match
# and "[1]192[.]0[.]2[.]5" (a footnote marker, then an IP) is still the IP.
AFTER_DOT = r"(?<![\w.])(?<!\[\.\])(?<!\(\.\))"
IP_RE = re.compile(rf"{AFTER_DOT}\d{{1,3}}(?:{DOT}\d{{1,3}}){{3}}(?![\w\[(]|\.\d)")
# Every dotted label up to the last one, so a bad top-level domain is kept whole, not cut short.
# A plain dot before a capital letter is a full stop with the space missing ("com.Then").
HOST_DOT = r"(?:\[\.\]|\(\.\)|\.(?![A-Z]))"
HOST = rf"(?:[\w-]+{HOST_DOT})+[\w-]+"
DOMAIN_RE = re.compile(rf"{AFTER_DOT}(?<![@-]){HOST}")
# The local part may be defanged ("john[.]smith"), missing or quoted: "@evil[.]example" still
# gives its domain.
EMAIL_RE = re.compile(rf"(?<![\w.+-])(?:{DOT}|[\w+-])*(?:@|\[@\])({HOST})")
TRAILING = ".,;:)]'\""


@dataclass
class Indicator:
    type: str  # cve | md5 | sha1 | sha256 | hash (malformed) | url | domain | ip
    value: str  # CVE upper-case, hash lower-case, network IOCs defanged
    start: int
    end: int
    live_in_source: bool = False
    malformed: bool = False
    note: str = ""


def refang(value: str) -> str:
    """Undo defang: every hxxp or hxxps before a colon (or %3a) is http or https again, the
    nested one in a query string included, and [.], (.), [:] and [@] are plain again."""
    value = value.replace("[.]", ".").replace("(.)", ".").replace("[:]", ":").replace("[@]", "@")
    return re.sub(r"hxxp(s?)(?=:|%3a)", r"http\1", value, flags=re.IGNORECASE)


def defang(value: str) -> str:
    """Write hxxp and [.] so the indicator can go into a ticket without becoming a link.

    Every http or https before a colon becomes hxxp or hxxps, including one inside a query
    string, one glued to the word before it (xhttp://) and one with its colon percent-encoded
    (http%3a), and every dot in host, path and query becomes [.]. A port stays as it is.
    """
    value = re.sub(r"http(s?)(?=:|%3a)", r"hxxp\1", value, flags=re.IGNORECASE)
    return value.replace(".", "[.]")


def _is_defanged(raw: str) -> bool:
    return refang(raw) != raw


def _is_live_url(raw: str) -> bool:
    """A browser opens it: http:// or https:// and a host with nothing defanged."""
    m = re.match(r"https?://([^/?#]*)", raw, re.IGNORECASE)
    return m is not None and not _is_defanged(m.group(1))


def domain_indicator(raw: str, start: int) -> Indicator:
    """A defanged domain, malformed when its last label is not letters only ("c0m")."""
    domain = refang(raw).lower()
    indicator = Indicator("domain", defang(domain), start, start + len(raw))
    if not re.fullmatch(r"[a-z]{2,}", domain.rsplit(".", 1)[-1]):
        indicator.malformed, indicator.note = True, "the top-level domain is not letters only"
    return indicator


def find_indicators(text: str, rules: dict[str, Any]) -> list[Indicator]:
    """Find every indicator by regex, marking the ones written live or malformed.

    URLs come first and mask their span, so a CVE, hash or domain inside a URL is not counted
    twice. Emails come next: an email defanged anywhere, [@] included, gives its whole domain,
    so user@evil[.]example[.]com is evil[.]example[.]com and not example[.]com. The whole
    email is masked, so a defanged or hex local part is not read as a domain or a hash.
    Bare domains and IPs count only when defanged: "build 4.2.0.118" and "cfg.dat" are not
    network indicators. A URL is live when its scheme and host are written as a browser opens
    them (https://evil.example/login[.]php is live); it's kept, defanged in the output.
    A defanged IP that isn't a valid address, or a domain whose last label isn't letters
    ("evil[.]example[.]c0m"), is kept whole as malformed, like a short hash.
    """
    found: list[Indicator] = []
    taken: list[tuple[int, int]] = []

    def free(start: int, end: int) -> bool:
        return not any(start < e and s < end for s, e in taken)

    def add(indicator: Indicator) -> None:
        found.append(indicator)
        taken.append((indicator.start, indicator.end))

    for m in URL_RE.finditer(text):
        raw = m.group().rstrip(TRAILING)
        indicator = Indicator("url", defang(refang(raw)), m.start(), m.start() + len(raw))
        if _is_live_url(raw):
            indicator.live_in_source, indicator.note = True, "written live in the report"
        add(indicator)
    for m in EMAIL_RE.finditer(text):
        raw = m.group(1).rstrip(TRAILING)
        if free(m.start(), m.end()) and _is_defanged(m.group()):
            add(domain_indicator(raw, m.start(1)))
            taken.append(m.span())
    for m in CVE_RE.finditer(text):
        if not free(m.start(), m.end()):
            continue
        cve = m.group().upper()
        if len(m.group(2)) < 4:
            note = "the sequence number needs 4 or more digits"
            add(Indicator("cve", cve, m.start(), m.end(), malformed=True, note=note))
        else:
            add(Indicator("cve", cve, m.start(), m.end()))
    min_hex = int(rules["thresholds"]["min_hex_run"])
    for m in re.finditer(rf"\b[0-9a-fA-F]{{{min_hex},}}\b", text):
        if not free(m.start(), m.end()):
            continue
        value = m.group().lower()
        if kind := HASH_TYPES.get(len(value)):
            add(Indicator(kind, value, m.start(), m.end()))
        else:
            note = f"{len(value)} hex characters, not an MD5, SHA-1 or SHA-256 length"
            add(Indicator("hash", value, m.start(), m.end(), malformed=True, note=note))
    for m in IP_RE.finditer(text):
        raw = m.group()
        if not free(m.start(), m.end()) or not _is_defanged(raw):
            continue
        try:
            ip = str(ipaddress.ip_address(refang(raw)))
        except ValueError:
            note = "not a valid IPv4 address"
            add(Indicator("ip", defang(refang(raw)), m.start(), m.end(), malformed=True, note=note))
            continue
        add(Indicator("ip", defang(ip), m.start(), m.end()))
    for m in DOMAIN_RE.finditer(text):
        raw = m.group().rstrip(TRAILING)
        if free(m.start(), m.start() + len(raw)) and _is_defanged(raw):
            add(domain_indicator(raw, m.start()))
    return sorted(found, key=lambda i: i.start)


# --- 2. Windows -------------------------------------------------------------------------------

SENTENCE_RE = re.compile(r"\S.*?(?:[.!?](?=\s|$)|$)", re.DOTALL)


def sentence_windows(text: str, max_words: int) -> list[tuple[int, str]]:
    """(offset, window) pairs: whole sentences packed up to ``max_words`` words.

    NuNER Zero reads 384 tokens. Cutting on sentence ends keeps every name whole; a single
    sentence longer than the limit is cut on words.
    """
    sentences = [m.span() for m in SENTENCE_RE.finditer(text)]
    pieces: list[tuple[int, int]] = []
    for start, end in sentences:
        words = list(re.finditer(r"\S+", text[start:end]))
        for i in range(0, len(words), max_words):
            chunk = words[i : i + max_words]
            pieces.append((start + chunk[0].start(), start + chunk[-1].end()))
    windows: list[tuple[int, int]] = []
    for start, end in pieces:
        if windows and len(text[windows[-1][0] : end].split()) <= max_words:
            windows[-1] = (windows[-1][0], end)
        else:
            windows.append((start, end))
    return [(start, text[start:end]) for start, end in windows]


# --- 3. Tag actors, malware and tools ---------------------------------------------------------


@dataclass
class Entity:
    kind: str  # actor | malware | tool
    text: str
    start: int
    end: int
    score: float | None
    source: str  # "model" or "table" (a name from the alias table or tool mapping)


def tag_windows(
    client: SIEClient, items: list[tuple[str, str]], rules: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Send the windows to NuNER Zero in batches; return its result per window id.

    A window with nothing in it comes back as ``entities: []``, not an error. A reply with no
    ``entities`` key fails that window, and counts as a failed call when the whole batch lacks
    it. A call that fails for a reason that may pass (a lost connection, a timeout, a server
    error, a rate limit) marks every window in its batch with that error, so those reports go
    to review and the run still writes its outputs. Two cases stop the run with SIEStopped
    instead: a refusal every batch would repeat (see `is_fatal`), and a run where no call
    succeeded at all, since then nothing was tagged. An error that isn't an SIE error is a bug
    in this code and is raised as is.
    """
    model, labels = rules["models"]["entities"], list(rules["labels"].values())
    options = {"threshold": rules["thresholds"]["min_entity_score"]}
    size = rules["thresholds"]["batch_size"]
    results: dict[str, dict[str, Any]] = {}
    failures: list[SIEError] = []
    calls = 0
    for start in range(0, len(items), size):
        batch = items[start : start + size]
        calls += 1
        try:
            found = jsonable(
                client.extract(
                    model,
                    [Item(id=item_id, text=text) for item_id, text in batch],
                    labels=labels,
                    options=options,
                    provision_timeout_s=WAIT_S,
                )
            )
            # A reply that isn't one result per window is a failed call, not a crash.
            if not isinstance(found, list) or len(found) != len(batch):
                raise SIEError(f"{model} replied with {_count(found)} results for {len(batch)}")
        except SIEError as error:
            if is_fatal(error):
                raise SIEStopped(stop_message(error, model)) from error
            failures.append(error)
            failed = {"error": f"the SIE call failed: {short(error)}"}
            results.update({item_id: failed for item_id, _ in batch})
            continue
        missing: list[str] = []
        for (item_id, _), result in zip(batch, found, strict=True):
            if not isinstance(result, dict) or result.get("entities") is None:
                # No `entities` is a failed call, not "nothing found": an empty list is that.
                item_error = result.get("error") if isinstance(result, dict) else None
                missing.append(
                    "the reply has no `entities`"
                    + (f" (item error: {item_error})" if item_error else "")
                )
                result = {"error": f"the SIE call failed: {missing[-1]}"}
            results[item_id] = result
        if len(missing) == len(batch):
            failures.append(SIEError(missing[0]))
    if failures and len(failures) == calls:
        raise SIEStopped(
            f"Every SIE call failed ({calls} of {calls} to {model}), so the run stops: "
            f"{describe(failures[0])}: {failures[0]}. {every_call_failed_advice(failures[0])} "
            "No outputs were written."
        ) from failures[0]
    if failures:
        n = len(failures)
        print(
            f"{n} of {calls} SIE calls failed; their reports go to review. "
            f"First: {short(failures[0])}"
        )
    return results


def _count(found: Any) -> str:
    return str(len(found)) if isinstance(found, list) else "no list of"


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
    stops without outputs."""


def is_fatal(error: SIEError) -> bool:
    """A refusal that fails every call the same way: a 4xx (bad key 401, forbidden 403, model
    not found 404, no credits or spend limit 402, inactive account) or a model the server
    marked as permanently unable to load. A RequestError with no HTTP status or a status below
    400 is a reply the SDK couldn't parse, and is fatal too. A rate limit, an over-long input, a
    timeout, a lost connection, a model still loading and a 5xx are transient or per-batch:
    those windows go to review."""
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
    """The error for a per-report reason or the summary line, its message cut short."""
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


def model_entities(
    window: str,
    offset: int,
    result: dict[str, Any],
    taken: list[tuple[int, int]],
    rules: dict[str, Any],
) -> tuple[list[Entity], list[str]]:
    """Keep the model spans that check out, moved to report offsets, and note the invalid ones.

    A span must carry one of our labels and a score, and its text must be exactly the window
    text at its offsets. A span on top of a `taken` span is dropped: CVEs, hashes and IOCs come
    from regex, and product names from the asset inventory ("Corvane VPN" is a product we look
    up, not an attacker's tool). So is a span that is not a name: one in ``ignore_mentions``
    ("MD5") or made only of generic words ("threat group").
    """
    kinds = {label: kind for kind, label in rules["labels"].items()}
    ignored = {m.casefold() for m in rules["ignore_mentions"]}
    generic = {w.casefold() for w in rules["generic_words"]}
    entities: list[Entity] = []
    notes: list[str] = []
    for raw in result["entities"]:
        if not isinstance(raw, dict):
            notes.append(f"invalid span from the model dropped: {raw!r}")
            continue
        start, end, text = raw.get("start"), raw.get("end"), str(raw.get("text", ""))
        kind, score = kinds.get(str(raw.get("label", ""))), raw.get("score")
        if (
            kind is None
            or not isinstance(start, int)
            or not isinstance(end, int)
            or not isinstance(score, (int, float))
            or window[start:end] != text
        ):
            notes.append(f"invalid span from the model dropped: {raw!r}")
            continue
        start, end = offset + start, offset + end
        if any(start < e and s < end for s, e in taken):
            continue
        words = text.casefold().split()
        if text.strip().casefold() in ignored or all(w in generic for w in words):
            continue
        entities.append(Entity(kind, text, start, end, float(score), "model"))
    return entities, notes


# --- 4. The alias table and the ATT&CK mapping ------------------------------------------------


def normalize(name: str, generic_words: frozenset[str]) -> str:
    """Casefold, drop a possessive, a leading "the" and trailing generic words."""
    name = re.sub(r"['\u2019]s$", "", name.strip()).strip(" .,;:()\"'")
    words = name.casefold().split()
    if words and words[0] == "the":
        words = words[1:]
    while len(words) > 1 and words[-1] in generic_words:
        words.pop()
    return " ".join(words)


@dataclass(frozen=True)
class Canonical:
    kind: str
    name: str
    techniques: tuple[str, ...] = ()


@dataclass(frozen=True)
class AliasTable:
    by_key: dict[str, tuple[Canonical, ...]]
    surfaces: tuple[tuple[str, str], ...]  # (exact name as listed, kind), longest first
    generic_words: frozenset[str]

    @classmethod
    def build(cls, rules: dict[str, Any], techniques: dict[str, Technique]) -> AliasTable:
        generic = frozenset(w.casefold() for w in rules["generic_words"])
        by_key: dict[str, list[Canonical]] = {}
        surfaces: dict[str, str] = {}
        kind_of: dict[str, str] = {}  # canonical name -> the one kind it is listed under

        def add(surface: str, entry: Canonical) -> None:
            if kind_of.setdefault(entry.name, entry.kind) != entry.kind:
                raise ValueError(
                    f"{entry.name} is listed as both {kind_of[entry.name]} and {entry.kind} "
                    "in rules.yaml; keep it under one"
                )
            listed = by_key.setdefault(normalize(surface, generic), [])
            if entry not in listed:
                listed.append(entry)
            surfaces.setdefault(surface, entry.kind)

        for kind, section in ((ACTOR, "actors"), (MALWARE, "malware")):
            for name, aliases in (rules.get(section) or {}).items():
                entry = Canonical(kind, name)
                for surface in (name, *(aliases or [])):
                    add(surface, entry)
        for tool, ids in (rules.get("tool_techniques") or {}).items():
            unknown = [t for t in ids if t not in techniques]
            if unknown:
                raise ValueError(f"tool {tool} maps to {unknown}, not in {TECHNIQUES_PATH.name}")
            add(tool, Canonical(TOOL, tool, tuple(ids)))
        ordered = tuple(sorted(surfaces.items(), key=lambda s: -len(s[0])))
        return cls({k: tuple(v) for k, v in by_key.items()}, ordered, generic)

    def lookup(self, mention: str) -> tuple[Canonical, ...]:
        return self.by_key.get(normalize(mention, self.generic_words), ())


def table_mentions(text: str, table: AliasTable, indicators: list[Indicator]) -> list[Entity]:
    """Every alias-table name or mapped tool written in the text, any case, whole words.

    The model can miss a name it has never seen ("LX-0417"), a short second mention or a name
    written in another case ("powershell", "ASHEN HERON"). Names already in the table need no
    model to find them; longer names win. A match inside a CVE,
    hash or IOC is dropped, as for model spans, so "ngrok" in hxxps://a1b2[.]ngrok[.]io is part
    of a URL indicator and not the tool.
    """
    found: list[Entity] = []
    taken = [(i.start, i.end) for i in indicators]
    for surface, kind in table.surfaces:
        pattern = r"(?<![\w-])" + re.escape(surface) + r"(?![\w-])"
        for m in re.finditer(pattern, text, re.IGNORECASE):
            if not any(m.start() < e and s < m.end() for s, e in taken):
                found.append(Entity(kind, m.group(), m.start(), m.end(), None, "table"))
                taken.append((m.start(), m.end()))
    return found


def merge_entities(model: list[Entity], table: list[Entity], aliases: AliasTable) -> list[Entity]:
    """Model spans plus the table names the model didn't already cover, in text order.

    A table match is dropped only when an overlapping model span looks up to the same
    canonical names ("Mothvine Loader" covers "Mothvine"). When the model span reads as
    something else ("PowerShell scripts", "Grey Lantern phishing"), both are kept: the exact
    table name keeps its canonical name and mapping, and the model span is resolved on its
    own, which usually sends it to review.
    """

    def covered(t: Entity) -> bool:
        names = aliases.lookup(t.text)
        return any(
            t.start < e.end and e.start < t.end and aliases.lookup(e.text) == names for e in model
        )

    kept = list(model) + [t for t in table if not covered(t)]
    return sorted(kept, key=lambda e: (e.start, e.end))


@dataclass
class Tag:
    report_id: str
    kind: str
    mention: str
    start: int
    end: int
    source: str
    score: float | None
    canonical: str = ""
    # ATT&CK techniques associated with the tool's name in rules.yaml, not observed behavior.
    candidate_techniques: list[str] = field(default_factory=list)
    techniques_held: bool = False  # a cue in the sentence held them back for review
    status: str = REVIEW
    reason: str = ""


def resolve(report_id: str, entity: Entity, table: AliasTable, rules: dict[str, Any]) -> Tag:
    """Look one mention up in the alias table and tag it, or say why an analyst should look.

    One canonical name: tagged, with that name, its kind and its candidate techniques. No name
    or two names: a reason, and review if `policy` turns that reason on (otherwise new).
    """
    tag = Tag(
        report_id, entity.kind, entity.text, entity.start, entity.end, entity.source, entity.score
    )
    entries = table.lookup(entity.text)
    names = sorted({entry.name for entry in entries})
    if len(names) == 1:
        entry = entries[0]
        tag.kind, tag.canonical = entry.kind, entry.name
        tag.candidate_techniques = list(entry.techniques)
        tag.status = TAGGED
        if entry.name != entity.text:
            tag.reason = f"normalised to {entry.name}"
        if entity.kind != entry.kind:
            tag.reason = f"model said {entity.kind}; the table lists {entry.name} as {entry.kind}"
        return tag
    policy = rules["policy"]
    if names:
        tag.reason = f"ambiguous alias: {entity.text!r} is listed under {' and '.join(names)}"
        flagged = policy["ambiguous_alias"]
    elif entity.kind == TOOL:
        tag.reason = f"tool {entity.text!r} has no ATT&CK mapping in rules.yaml"
        flagged = policy["unmapped_tool"]
    else:
        tag.reason = f"{entity.kind} {entity.text!r} is not in the alias table"
        flagged = policy[f"unknown_{entity.kind}"]
    tag.status = REVIEW if flagged else NEW
    return tag


def technique_cue(text: str, tag: Tag, cues: list[str]) -> str:
    """The first `technique_cues` entry (in list order) found in the sentence around a tool
    mention, or "" when none is there.

    A tool's techniques come from its name, not from what the report says it did, so
    "No Mimikatz execution was observed" or "used rclone to back up the file server" must not
    read as credential dumping or exfiltration. This is a keyword check on one sentence, so it
    catches plainly worded cases only.
    """
    return sentence_cue(text, tag.start, cues)


def sentence_of(text: str, position: int) -> tuple[int, int]:
    """The (start, end) of the sentence holding ``position``."""
    return next(
        (m.span() for m in SENTENCE_RE.finditer(text) if m.start() <= position < m.end()),
        (position, position),
    )


def sentence_cue(text: str, position: int, cues: list[str]) -> str:
    """The first cue (in list order) written as whole words in the sentence holding
    ``position``, any case, or "" when none is there."""
    start, end = sentence_of(text, position)
    sentence = text[start:end]
    for cue in cues:
        if re.search(r"(?<![\w-])" + re.escape(cue) + r"(?![\w-])", sentence, re.IGNORECASE):
            return cue
    return ""


# --- 5. Does it affect us? The scanner and the asset inventory --------------------------------

# A capitalized word right after a product name makes it a longer, different name
# ("Corvane VPN Gateway Manager"); a number right after it is the version the report names,
# written "7.2", "v7", "version 7.2" or "(7.2)", and it may end a sentence ("Gateway 7.4.") or
# a family ("7.x" is 7). Any other version wording near the name ("versions 8.0 and later",
# ", 7.4 only", "prior to 7.0") isn't read; see unread_version.
NAME_WORD_RE = re.compile(r" ([A-Z][\w-]*)")
VERSION_RE = re.compile(r" \(?(?:version |v)?(\d+(?:\.\d+)*)(?![\w\[(]|\.\d)")


@dataclass
class ProductMention:
    text: str  # as written in the report
    start: int
    end: int
    product: str = ""  # the inventory product; "" when the name isn't an exact match
    hosts: list[str] = field(default_factory=list)
    version_written: str = ""
    problem: str = ""  # why the name or version isn't an exact match; "" when it is
    cue: str = ""  # a cue word in its sentence ("not affected"), "" when none


def _overlaps(start: int, end: int, taken: list[tuple[int, int]]) -> bool:
    return any(start < e and s < end for s, e in taken)


def _longer_name(text: str, end: int, limit: int, taken: list[tuple[int, int]]) -> int:
    """Where a product name really ends: after the capitalized words that follow it, if any.
    A word inside an indicator ("CVE-2099-41077" after the name) is not part of the name."""
    for _ in range(limit):
        m = NAME_WORD_RE.match(text, end)
        if m is None or _overlaps(m.start(1), m.end(1), taken):
            break
        end = m.end()
    return end


def unread_version(text: str, end: int, taken: list[tuple[int, int]], rules: dict[str, Any]) -> str:
    """The version wording in the next few words after a product name that ends at ``end``,
    in the same sentence, or "" when there is none: a `version_words` entry or any digit.
    Words inside an indicator ("CVE-2099-41077", an IP) don't count. Called when VERSION_RE
    read no version, so whatever this finds is a version the recipe can't compare."""
    window = int(rules["relevance"]["version_window_words"])
    words = [str(w) for w in rules["relevance"]["version_words"]]
    sentence_end = sentence_of(text, max(end - 1, 0))[1]
    tail = "".join(
        " " if _overlaps(i, i + 1, taken) else c
        for i, c in enumerate(text[end:sentence_end], start=end)
    )
    near = " ".join(tail.split()[:window])
    for word in words:
        if re.search(r"(?<![\w-])" + re.escape(word) + r"(?![\w-])", near, re.IGNORECASE):
            return word
    digit = re.search(r"\S*\d\S*", near)
    return digit.group().strip(",;:()") if digit else ""


def find_products(
    text: str, inventory: AssetInventory, indicators: list[Indicator], rules: dict[str, Any]
) -> list[ProductMention]:
    """Every product the report names that is, or looks like, something in the inventory.

    A product name or alias from the inventory written as whole words (any case, longest
    first) is an exact match, unless a capitalized word follows it: "Corvane VPN Gateway
    Manager" is a longer name, so it's a near miss. A number right after the name is the
    version the report names, and a host only counts when its version starts with it. Other
    version wording near the name ("versions 8.0 and later") is a problem: the recipe can't
    compare it, so it doesn't ticket every host running the product. A vendor name with no
    exact product name around it ("Corvane VPN Concentrator") is a near miss too: close to
    something we run, not the same product. Names inside an indicator are part of the
    indicator. Near misses carry a `problem` and go to review.
    """
    limit = int(rules["relevance"]["max_extra_name_words"])
    cues = [str(c) for c in rules["relevance"]["product_cues"]]
    taken = [(i.start, i.end) for i in indicators]
    found: list[ProductMention] = []
    for surface in inventory.names():
        pattern = r"(?<![\w-])" + re.escape(surface) + r"(?![\w-])"
        for m in re.finditer(pattern, text, re.IGNORECASE):
            if _overlaps(m.start(), m.end(), taken):
                continue
            assets = inventory.find_product(m.group())
            products = sorted({a.product for a in assets})
            end = _longer_name(text, m.end(), limit, taken)
            mention = ProductMention(m.group(), m.start(), end)
            if end > m.end():
                mention.text = text[m.start() : end]
                mention.problem = (
                    f"a longer name than the inventory's {' / '.join(products)}, so not the "
                    "same product as far as the inventory knows"
                )
            elif len(products) > 1:
                mention.problem = f"the inventory lists this name under {' and '.join(products)}"
            else:
                mention.product = products[0]
                hosts = assets
                version = VERSION_RE.match(text, m.end())
                if version and not _overlaps(version.start(), version.end(), taken):
                    written = version.group(1)
                    mention.version_written, mention.end = written, version.end()
                    hosts = [a for a in assets if (a.version + ".").startswith(written + ".")]
                    if not hosts:
                        have = ", ".join(sorted({a.version for a in assets}))
                        mention.problem = (
                            f"the report names version {written}; the inventory has {have}"
                        )
                elif unread := unread_version(text, m.end(), taken, rules):
                    mention.problem = (
                        f"the report qualifies the version ({unread!r}) in a way the recipe "
                        "doesn't read; a person checks which versions it covers"
                    )
                    hosts = []
                mention.hosts = [a.host for a in hosts]
            mention.cue = sentence_cue(text, m.start(), cues)
            found.append(mention)
            taken.append((mention.start, mention.end))
    for vendor in inventory.vendors():
        pattern = r"(?<![\w-])" + re.escape(vendor) + r"(?![\w-])"
        for m in re.finditer(pattern, text, re.IGNORECASE):
            end = _longer_name(text, m.end(), limit, taken)
            if _overlaps(m.start(), end, taken):
                continue
            ours = sorted({a.product for a in inventory.find_vendor(vendor)})
            problem = (
                f"close to the inventory's {' / '.join(ours)} but not the same name; a person "
                "checks whether it's a product we run"
            )
            found.append(ProductMention(text[m.start() : end], m.start(), end, problem=problem))
            taken.append((m.start(), end))
    return sorted(found, key=lambda p: p.start)


@dataclass
class Ticket:
    team: str
    host: str
    product: str
    cve: str  # "" when the report names the product without a CVE
    finding: str  # what the lookups found, in words
    excerpt: str  # the report's sentence, indicators defanged


@dataclass
class Relevance:
    products: list[ProductMention] = field(default_factory=list)
    scanner: list[dict[str, str]] = field(default_factory=list)  # host, cve, status
    tickets: list[Ticket] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)  # what sends the report to review
    notes: list[str] = field(default_factory=list)  # what was checked and found closed


def defanged_excerpt(text: str, position: int, indicators: list[Indicator]) -> str:
    """The sentence holding ``position``, with every indicator in it written defanged, so a
    ticket never carries a live link even when the report did."""
    start, end = sentence_of(text, position)
    return defanged_text(text, indicators, start, end)


def defanged_text(text: str, indicators: list[Indicator], start: int = 0, end: int = -1) -> str:
    """``text[start:end]`` (all of it by default) with every indicator inside written defanged.
    A bare domain the regexes don't count (a live "www.example.com" or "user@example.com")
    stays as written."""
    end = len(text) if end < 0 else end
    parts, cursor = [], start
    for ind in sorted(indicators, key=lambda i: i.start):
        if start <= ind.start and ind.end <= end:
            parts += [text[cursor : ind.start], ind.value]
            cursor = ind.end
    return "".join([*parts, text[cursor:end]])


def check_relevance(
    text: str,
    indicators: list[Indicator],
    products: list[ProductMention],
    scanner: Scanner,
    inventory: AssetInventory,
) -> Relevance:
    """Look the report's CVEs up in the scanner and its products in the inventory.

    A CVE the scanner shows open on a host is a ticket for the team that owns the host. A
    product named exactly, with no CVE in the report, is a ticket for each host running it, to
    check. A product named with CVEs is a ticket only through the scanner: a host running it
    that the scanner has no result for, for any of those CVEs, goes to review, and a patched
    CVE is a note. A near-miss name, a cue like "not affected" next to the product, and an open
    CVE on a host with no owner in the inventory go to review too.
    """
    rel = Relevance(products=products)
    cves = [i for i in indicators if i.type == "cve" and not i.malformed]
    findings: dict[str, list[Finding]] = {}
    for ind in cves:
        findings.setdefault(ind.value, scanner.find_cve(ind.value))
    rel.scanner = [asdict(f) for found in findings.values() for f in found]

    def ticket(host: str, cve: str, finding: str, position: int) -> None:
        assets = inventory.find_host(host)
        teams = sorted({a.owner_team for a in assets})
        if not teams:
            rel.reasons.append(
                f"{cve or 'a named product'} on {host}: the host isn't in the asset inventory, "
                "so there is no owning team to send it to"
            )
            return
        product = ", ".join(sorted({a.product for a in assets}))
        excerpt = defanged_excerpt(text, position, indicators)
        for team in teams:
            new = Ticket(team, host, product, cve, finding, excerpt)
            if all((t.team, t.host, t.cve) != (team, host, cve) for t in rel.tickets):
                rel.tickets.append(new)

    for ind in cves:
        for f in findings[ind.value]:
            if f.status == OPEN:
                ticket(f.host, ind.value, "open, per the scanner", ind.start)
    for cve, found in findings.items():
        patched = [f.host for f in found if f.status != OPEN]
        if not found:
            rel.notes.append(f"{cve}: the scanner has no result for it on any host")
        elif patched:
            rel.notes.append(f"{cve}: patched on {', '.join(patched)}, per the scanner")
    for p in products:
        if p.problem:
            rel.reasons.append(f"product {p.text!r}: {p.problem}")
            continue
        if p.cue:
            rel.reasons.append(
                f"product {p.text!r}: the sentence says {p.cue!r}; a person checks whether "
                "it applies"
            )
            continue
        if not cves:
            for host in p.hosts:
                finding = f"the report names {p.product} and gives no CVE; check exposure"
                ticket(host, "", finding, p.start)
            continue
        # Every (host, CVE) pair needs a scanner result: a patched CVE on a host says nothing
        # about another CVE the scanner never checked there.
        unchecked = {
            cve: missing
            for cve in sorted(findings)
            if (missing := [h for h in p.hosts if all(f.host != h for f in findings[cve])])
        }
        if unchecked:
            pairs = " and for ".join(f"{cve} on {', '.join(h)}" for cve, h in unchecked.items())
            rel.reasons.append(
                f"product {p.text!r}: the scanner has no result for {pairs}; a person checks "
                "whether it applies"
            )
    return rel


# --- Run --------------------------------------------------------------------------------------


@dataclass
class TaggedReport:
    id: str
    published: str
    title: str
    status: str  # affects_us | hunt | filed | review
    actors: list[str]
    malware: list[str]
    tools: list[str]
    # Associated with the tools mentioned; an analyst confirms the behavior before use. A tool
    # whose techniques a cue held back adds none here; see Tag.techniques_held.
    candidate_techniques: list[dict[str, str]]
    tags: list[Tag]
    indicators: list[Indicator]  # candidate observables, not vetted indicators
    relevance: Relevance
    reasons: list[str] = field(default_factory=list)


def hunt_indicators(report: TaggedReport, rules: dict[str, Any]) -> list[Indicator]:
    """The indicators the SOC can search the logs for: well-formed, of a `hunt_types` type."""
    types = set(rules["relevance"]["hunt_types"])
    return [i for i in report.indicators if i.type in types and not i.malformed]


def report_status(report: TaggedReport, rules: dict[str, Any]) -> str:
    """affects_us when a ticket was drafted (its reasons stay on the report), else review when
    any check raised a reason, else hunt when there's something to search the logs for, else
    filed. A status says what the checks found; the analyst and the system owner decide."""
    if report.relevance.tickets:
        return AFFECTS_US
    if report.reasons:
        return REVIEW
    return HUNT if hunt_indicators(report, rules) else FILED


def tag_reports(
    client: SIEClient,
    reports: list[Report],
    table: AliasTable,
    techniques: dict[str, Technique],
    rules: dict[str, Any],
    scanner: Scanner,
    inventory: AssetInventory,
) -> list[TaggedReport]:
    max_words = rules["thresholds"]["max_words_per_window"]
    windows = {report.id: sentence_windows(report.text, max_words) for report in reports}
    items = [
        (f"{report_id}#w{number}", window)
        for report_id, parts in windows.items()
        for number, (_, window) in enumerate(parts, start=1)
    ]
    results = tag_windows(client, items, rules) if items else {}
    policy = rules["policy"]
    tagged: list[TaggedReport] = []
    for report in reports:
        indicators = find_indicators(report.text, rules)
        products = find_products(report.text, inventory, indicators, rules)
        taken = [(i.start, i.end) for i in indicators] + [(p.start, p.end) for p in products]
        reasons: list[str] = []
        for ind in indicators:
            if ind.live_in_source and policy["live_ioc"]:
                reasons.append(f"live URL in the report: {ind.value}")
            if ind.malformed and policy["malformed_indicator"]:
                reasons.append(f"malformed {ind.type} {ind.value}: {ind.note}")
        found: list[Entity] = []
        for number, (offset, window) in enumerate(windows[report.id], start=1):
            result = results[f"{report.id}#w{number}"]
            if error := result.get("error"):
                reasons.append(f"window {number} was not tagged: {error}")
                continue
            entities, notes = model_entities(window, offset, result, taken, rules)
            found += entities
            reasons += notes
        entities = merge_entities(found, table_mentions(report.text, table, indicators), table)
        tags = [resolve(report.id, entity, table, rules) for entity in entities]
        for tag in tags:
            if not (tag.status == TAGGED and tag.candidate_techniques and policy["technique_cue"]):
                continue
            if cue := technique_cue(report.text, tag, rules["technique_cues"]):
                tag.techniques_held = True
                held = ", ".join(tag.candidate_techniques)
                note = f"candidate techniques {held} held: the sentence says {cue!r}"
                tag.reason = "; ".join(part for part in (tag.reason, note) if part)
        reasons += [
            f"{tag.mention}: {tag.reason}"
            for tag in tags
            if tag.status == REVIEW or tag.techniques_held
        ]
        relevance = check_relevance(report.text, indicators, products, scanner, inventory)
        reasons += relevance.reasons
        reasons = list(dict.fromkeys(reasons))  # a name mentioned twice gives one reason

        def names(kind: str, tags: list[Tag] = tags) -> list[str]:
            return sorted({t.canonical for t in tags if t.kind == kind and t.status == TAGGED})

        ids = sorted(
            {
                t
                for tag in tags
                if tag.status == TAGGED and not tag.techniques_held
                for t in tag.candidate_techniques
            }
        )
        out = TaggedReport(
            report.id,
            report.published,
            defanged_text(report.title, find_indicators(report.title, rules)),
            "",
            names(ACTOR),
            names(MALWARE),
            names(TOOL),
            [asdict(techniques[t]) for t in ids],
            tags,
            indicators,
            relevance,
            reasons,
        )
        out.status = report_status(out, rules)
        tagged.append(out)
    return tagged


def csv_safe(row: dict[str, Any]) -> dict[str, Any]:
    """Quote a cell that a spreadsheet would run as a formula ("=HYPERLINK(...)") with a '."""
    return {
        key: f"'{value}" if isinstance(value, str) and value.startswith(FORMULA_START) else value
        for key, value in row.items()
    }


def code_span(text: str) -> str:
    """``text`` as a Markdown code span, so a viewer shows it as written: a link in it
    ("[advisory](//evil.example/fix)", "<https://...>") is not made clickable. The fence is one
    backtick longer than the longest run inside, and line breaks become spaces."""
    text = " ".join(text.split())
    runs = [len(run) for run in re.findall(r"`+", text)]
    fence = "`" * (max(runs, default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def ticket_drafts(tagged: list[TaggedReport]) -> dict[str, str]:
    """One Markdown ticket draft per owning team, listing every report that affects its hosts.
    Indicators stay defanged here; the live ones are only in hunt.csv. The report's title and
    sentences go in code spans, so a Markdown link a report carries doesn't render live."""
    by_team: dict[str, list[tuple[TaggedReport, Ticket]]] = {}
    for report in tagged:
        for t in report.relevance.tickets:
            by_team.setdefault(t.team, []).append((report, t))
    drafts: dict[str, str] = {}
    for team, rows in sorted(by_team.items()):
        lines = [f"# Ticket draft for {team}", ""]
        lines += [
            "Drafted from threat reports by the CTI triage recipe. The CTI analyst checks it "
            "before it's filed; your team decides what to do.",
            "",
        ]
        for report_id in dict.fromkeys(r.id for r, _ in rows):
            report = next(r for r, _ in rows if r.id == report_id)
            title = code_span(report.title)
            lines += [f"## {report.id}: {title} (published {report.published})", ""]
            for _, t in (row for row in rows if row[0].id == report_id):
                where = f"{t.cve} on" if t.cve else "On"
                lines.append(f"- {where} `{t.host}` ({t.product}): {t.finding}")
            names = ", ".join(report.actors + report.malware) or "none tagged"
            lines += ["", f"Actors and malware named: {names}", ""]
            for excerpt in dict.fromkeys(t.excerpt for r, t in rows if r.id == report_id):
                lines.append(f"> {code_span(excerpt)}")
            lines.append("")
        drafts[team] = "\n".join(lines).rstrip() + "\n"
    return drafts


def write_outputs(output_dir: Path, tagged: list[TaggedReport], rules: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = [asdict(report) for report in tagged]
    (output_dir / "tags.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    with (output_dir / "entities.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ENTITY_FIELDS)
        writer.writeheader()
        for report in tagged:
            for tag in report.tags:
                row = asdict(tag)
                row["candidate_techniques"] = ";".join(tag.candidate_techniques)
                writer.writerow(csv_safe(row))
    with (output_dir / "iocs.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=IOC_FIELDS)
        writer.writeheader()
        for report in tagged:
            for ind in report.indicators:
                writer.writerow(
                    csv_safe(
                        {
                            "report_id": report.id,
                            "type": ind.type,
                            "value": ind.value,
                            "live_in_source": ind.live_in_source,
                            "malformed": ind.malformed,
                            "note": ind.note,
                        }
                    )
                )
    # The only file with live (refanged) indicators: the SOC pastes them into a log search.
    with (output_dir / "hunt.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HUNT_FIELDS)
        writer.writeheader()
        for report in tagged:
            if report.status not in (AFFECTS_US, HUNT):
                continue
            for ind in hunt_indicators(report, rules):
                row = {"report_id": report.id, "report_status": report.status}
                row |= {"type": ind.type, "value": refang(ind.value), "published": report.published}
                writer.writerow(csv_safe(row))
    tickets = output_dir / "tickets"
    tickets.mkdir(exist_ok=True)
    for team, draft in ticket_drafts(tagged).items():
        name = re.sub(r"[^\w-]", "_", team) or "_"
        (tickets / f"{name}.md").write_text(draft, encoding="utf-8")


def _setup() -> tuple[
    list[Report], AliasTable, dict[str, Technique], dict[str, Any], Scanner, AssetInventory
]:
    rules, techniques = load_rules(), load_techniques()
    table = AliasTable.build(rules, techniques)
    return load_reports(), table, techniques, rules, Scanner(), AssetInventory()


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    reports, table, techniques, rules, scanner, inventory = _setup()
    with make_client() as client:
        tagged = stop_on_refusal(
            lambda: tag_reports(client, reports, table, techniques, rules, scanner, inventory)
        )
    write_outputs(output, tagged, rules)
    for report in tagged:
        print(f"{report.id}  {report.status:<10} actors: {', '.join(report.actors) or '-'}")
        ids = ", ".join(t["id"] for t in report.candidate_techniques) or "-"
        print(f"        malware: {', '.join(report.malware) or '-'}  candidate techniques: {ids}")
        for t in report.relevance.tickets:
            print(f"        ticket: {t.team}  {t.cve or '-'} on {t.host}: {t.finding}")
        for note in report.relevance.notes:
            print(f"        checked: {note}")
        for reason in report.reasons:
            print(f"        review: {reason}")
    counts = {s: sum(r.status == s for r in tagged) for s in (AFFECTS_US, HUNT, FILED, REVIEW)}
    print(f"\n{len(tagged)} reports: " + ", ".join(f"{n} {s}" for s, n in counts.items()))
    print("techniques and indicators are candidates; the analyst and system owners decide")
    print(f"outputs in {output}: tags.json, entities.csv, iocs.csv, hunt.csv, tickets/")


if __name__ == "__main__":
    main()
