"""Tag threat actors, malware and tools in threat reports, with CVEs, hashes and IOCs.

Use case:  Threat report tagging: which actors, malware, tools and indicators is this report about?
Who:       Cyber threat intelligence (CTI) teams that file vendor and partner reports into a
           threat-intel platform.
Problem:   One report says "LX-0417", another "Ashen Heron", a third "Grey Lantern", and the
           same name can mean two different groups. Indicators come defanged in several styles,
           sometimes live, sometimes truncated. Tagging by hand is slow, and a wrong merge
           pins the activity on the wrong actor.
Input:     data/reports.jsonl (synthetic reports about fictional actors and malware),
           data/attack_techniques.csv (a MITRE ATT&CK technique subset) and rules.yaml (the
           alias table and the tool-to-technique mapping).
SIE pipeline:
           (Python)                    pull CVEs, MD5/SHA-1/SHA-256 hashes and defanged URLs,
                                       domains and IPs by regex; flag live URLs and malformed
                                       indicators
           (Python)                    split each report into sentence windows that fit
           extract  numind/NuNER_Zero  tag threat actor, malware and tool spans
           (Python)                    also match alias-table names the model missed;
                                       normalize every name through the alias table, map tools
                                       to ATT&CK techniques; unknown names, colliding aliases
                                       and unmapped tools go to review
Output:    tags.json (every report with entities, indicators, techniques, status and reasons),
           entities.csv (one row per actor, malware or tool mention) and iocs.csv (one row per
           indicator, defanged).
Why SIE:   One small zero-shot NER model, no LLM and no training: the labels are plain words in
           rules.yaml. Indicators and alias merges are code you can read, so a person can see
           why any merge happened. One SIE Cloud call per batch, and you host nothing.
Runs on:   SIE Cloud.

    uv run recipe.py
"""

from __future__ import annotations

import argparse
import csv
import ipaddress
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import yaml
from dotenv import load_dotenv
from sie_sdk import SIEClient
from sie_sdk.types import Item

ROOT = Path(__file__).resolve().parent
REPORTS_PATH = ROOT / "data" / "reports.jsonl"
TECHNIQUES_PATH = ROOT / "data" / "attack_techniques.csv"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900

ACTOR, MALWARE, TOOL = "actor", "malware", "tool"
TAGGED, REVIEW = "tagged", "review"
ENTITY_FIELDS = (
    "report_id",
    "kind",
    "mention",
    "start",
    "end",
    "source",
    "score",
    "canonical",
    "techniques",
    "status",
    "reason",
)
IOC_FIELDS = ("report_id", "type", "value", "live_in_source", "note")
HASH_TYPES = {32: "md5", 40: "sha1", 64: "sha256"}


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
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


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
IP_RE = re.compile(rf"(?<![\w.\[])\d{{1,3}}(?:{DOT}\d{{1,3}}){{3}}(?![\w\[]|\.\d)")
DOMAIN_RE = re.compile(rf"(?<![\w.\[@-])(?:[a-z0-9-]+{DOT})+[a-z]{{2,}}\b", re.IGNORECASE)
TRAILING = ".,;:)]'\""


@dataclass
class Indicator:
    type: str  # cve | md5 | sha1 | sha256 | url | domain | ip
    value: str  # CVE upper-case, hash lower-case, network IOCs defanged
    start: int
    end: int
    live_in_source: bool = False
    note: str = ""


def refang(value: str) -> str:
    value = re.sub(r"^hxxp", "http", value, flags=re.IGNORECASE)
    return value.replace("[.]", ".").replace("(.)", ".").replace("[:]", ":")


def defang(kind: str, value: str) -> str:
    """Write hxxp:// and [.] so the indicator can go into a ticket without becoming a link."""
    if kind == "url":
        scheme, _, rest = value.partition("://")
        host, slash, path = rest.partition("/")
        return f"{re.sub(r'^http', 'hxxp', scheme)}://{host.replace('.', '[.]')}{slash}{path}"
    return value.replace(".", "[.]")


def _is_defanged(raw: str) -> bool:
    return refang(raw) != raw


def find_indicators(text: str, rules: dict[str, Any]) -> tuple[list[Indicator], list[str]]:
    """Find every indicator by regex, with a review reason for each live or malformed one.

    URLs come first and mask their span, so a domain inside a URL is not counted twice.
    Bare domains and IPs count only when defanged: "build 4.2.0.118" and "cfg.dat" are not
    network indicators. A URL written live is kept (defanged in the output) and flagged.
    """
    found: list[Indicator] = []
    reasons: list[str] = []
    taken: list[tuple[int, int]] = []

    def free(start: int, end: int) -> bool:
        return not any(start < e and s < end for s, e in taken)

    def add(indicator: Indicator) -> None:
        found.append(indicator)
        taken.append((indicator.start, indicator.end))

    for m in URL_RE.finditer(text):
        raw = m.group().rstrip(TRAILING)
        live = not _is_defanged(raw)
        indicator = Indicator("url", defang("url", refang(raw)), m.start(), m.start() + len(raw))
        if live:
            indicator.live_in_source, indicator.note = True, "written live in the report"
            reasons.append(f"live URL in the report: {indicator.value}")
        add(indicator)
    for m in CVE_RE.finditer(text):
        cve = m.group().upper()
        if len(m.group(2)) < 4:
            reasons.append(f"malformed CVE ID {cve}: the sequence number needs 4 or more digits")
            add(Indicator("cve", cve, m.start(), m.end(), note="malformed"))
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
            reasons.append(f"malformed hash {value} ({len(value)} hex characters)")
            add(Indicator("hash", value, m.start(), m.end(), note="malformed"))
    for m in IP_RE.finditer(text):
        raw = m.group()
        if not free(m.start(), m.end()) or not _is_defanged(raw):
            continue
        try:
            ip = str(ipaddress.ip_address(refang(raw)))
        except ValueError:
            continue
        add(Indicator("ip", defang("ip", ip), m.start(), m.end()))
    for m in DOMAIN_RE.finditer(text):
        raw = m.group().rstrip(TRAILING)
        if not free(m.start(), m.start() + len(raw)) or not _is_defanged(raw):
            continue
        domain = refang(raw).lower()
        add(Indicator("domain", defang("domain", domain), m.start(), m.start() + len(raw)))
    return sorted(found, key=lambda i: i.start), reasons


# --- 2. Windows -------------------------------------------------------------------------------


def sentence_windows(text: str, max_words: int) -> list[tuple[int, str]]:
    """(offset, window) pairs: whole sentences packed up to ``max_words`` words.

    NuNER Zero reads 384 tokens. Cutting on sentence ends keeps every name whole; a single
    sentence longer than the limit is cut on words.
    """
    sentences = [m.span() for m in re.finditer(r"\S.*?(?:[.!?](?=\s|$)|$)", text, re.DOTALL)]
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

    A window with nothing in it comes back as ``entities: []``, not an error.
    """
    model, labels = rules["models"]["entities"], list(rules["labels"].values())
    options = {"threshold": rules["thresholds"]["min_entity_score"]}
    size = rules["thresholds"]["batch_size"]
    results: dict[str, dict[str, Any]] = {}
    for start in range(0, len(items), size):
        batch = items[start : start + size]
        found = client.extract(
            model,
            [Item(id=item_id, text=text) for item_id, text in batch],
            labels=labels,
            options=options,
            provision_timeout_s=WAIT_S,
        )
        for (item_id, _), result in zip(batch, jsonable(found), strict=True):
            results[item_id] = result
    return results


def model_entities(
    window: str,
    offset: int,
    result: dict[str, Any],
    indicators: list[Indicator],
    rules: dict[str, Any],
) -> tuple[list[Entity], list[str]]:
    """Keep the model spans that check out, moved to report offsets, and note the invalid ones.

    A span must carry one of our labels and its text must be exactly the window text at its
    offsets. A span on top of a CVE, hash or IOC is dropped: those come from regex. So is a
    span that is not a name: one in ``ignore_mentions`` ("MD5") or made only of generic words
    ("threat group").
    """
    kinds = {label: kind for kind, label in rules["labels"].items()}
    ignored = {m.casefold() for m in rules["ignore_mentions"]}
    generic = {w.casefold() for w in rules["generic_words"]}
    entities: list[Entity] = []
    notes: list[str] = []
    for raw in result.get("entities") or []:
        start, end, text = raw.get("start"), raw.get("end"), str(raw.get("text", ""))
        kind = kinds.get(str(raw.get("label", "")))
        if kind is None or start is None or end is None or window[start:end] != text:
            notes.append(f"invalid span from the model dropped: {raw!r}")
            continue
        start, end = offset + start, offset + end
        if any(start < i.end and i.start < end for i in indicators):
            continue
        words = text.casefold().split()
        if text.strip().casefold() in ignored or all(w in generic for w in words):
            continue
        entities.append(Entity(kind, text, start, end, float(raw["score"]), "model"))
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

        def add(surface: str, entry: Canonical) -> None:
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


def table_mentions(text: str, table: AliasTable) -> list[Entity]:
    """Every alias-table name or mapped tool written in the text, exact case, whole words.

    The model can miss a name it has never seen ("LX-0417") or a short second mention. Names
    already in the table need no model to find them; longer names win.
    """
    found: list[Entity] = []
    for surface, kind in table.surfaces:
        for m in re.finditer(r"(?<![\w-])" + re.escape(surface) + r"(?![\w-])", text):
            if not any(m.start() < e.end and e.start < m.end() for e in found):
                found.append(Entity(kind, surface, m.start(), m.end(), None, "table"))
    return found


def merge_entities(model: list[Entity], table: list[Entity]) -> list[Entity]:
    """Model spans plus the table names the model missed, in text order.

    Where a model span and a table match overlap, the model span wins, even when the table
    match is the better name (e.g. "Grey Lantern phishing" over "Grey Lantern").
    """
    kept = list(model)
    kept += [t for t in table if not any(t.start < e.end and e.start < t.end for e in model)]
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
    techniques: list[str] = field(default_factory=list)
    status: str = REVIEW
    reason: str = ""


def resolve(report_id: str, entity: Entity, table: AliasTable, rules: dict[str, Any]) -> Tag:
    """Look one mention up in the alias table and tag it, or say why an analyst should look.

    One canonical name: tagged, with that name, its kind and its techniques. No name or two
    names: a reason, and review if `policy` turns that reason on (otherwise "new").
    """
    tag = Tag(
        report_id, entity.kind, entity.text, entity.start, entity.end, entity.source, entity.score
    )
    entries = table.lookup(entity.text)
    names = sorted({entry.name for entry in entries})
    if len(names) == 1:
        entry = entries[0]
        tag.kind, tag.canonical, tag.techniques = entry.kind, entry.name, list(entry.techniques)
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
    tag.status = REVIEW if flagged else "new"
    return tag


# --- Run --------------------------------------------------------------------------------------


@dataclass
class TaggedReport:
    id: str
    title: str
    status: str
    actors: list[str]
    malware: list[str]
    tools: list[str]
    techniques: list[dict[str, str]]
    tags: list[Tag]
    indicators: list[Indicator]
    reasons: list[str] = field(default_factory=list)


def tag_reports(
    client: SIEClient,
    reports: list[Report],
    table: AliasTable,
    techniques: dict[str, Technique],
    rules: dict[str, Any],
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
        indicators, ioc_reasons = find_indicators(report.text, rules)
        reasons: list[str] = []
        for reason in ioc_reasons:
            live = reason.startswith("live URL")
            if policy["live_ioc" if live else "malformed_indicator"]:
                reasons.append(reason)
        found: list[Entity] = []
        for number, (offset, window) in enumerate(windows[report.id], start=1):
            result = results[f"{report.id}#w{number}"]
            if error := result.get("error"):
                reasons.append(f"window {number} was not tagged: {error}")
                continue
            entities, notes = model_entities(window, offset, result, indicators, rules)
            found += entities
            reasons += notes
        entities = merge_entities(found, table_mentions(report.text, table))
        tags = [resolve(report.id, entity, table, rules) for entity in entities]
        reasons += [f"{tag.mention}: {tag.reason}" for tag in tags if tag.status == REVIEW]

        def names(kind: str, tags: list[Tag] = tags) -> list[str]:
            return sorted({t.canonical for t in tags if t.kind == kind and t.status == TAGGED})

        ids = sorted({t for tag in tags if tag.status == TAGGED for t in tag.techniques})
        mapped = [asdict(techniques[t]) for t in ids]
        tagged.append(
            TaggedReport(
                report.id,
                report.title,
                REVIEW if reasons else TAGGED,
                names(ACTOR),
                names(MALWARE),
                names(TOOL),
                mapped,
                tags,
                indicators,
                reasons,
            )
        )
    return tagged


def write_outputs(output_dir: Path, tagged: list[TaggedReport]) -> None:
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
                row["techniques"] = ";".join(tag.techniques)
                writer.writerow(row)
    with (output_dir / "iocs.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=IOC_FIELDS)
        writer.writeheader()
        for report in tagged:
            for ind in report.indicators:
                writer.writerow(
                    {
                        "report_id": report.id,
                        "type": ind.type,
                        "value": ind.value,
                        "live_in_source": ind.live_in_source,
                        "note": ind.note,
                    }
                )


def _setup() -> tuple[list[Report], AliasTable, dict[str, Technique], dict[str, Any]]:
    rules, techniques = load_rules(), load_techniques()
    return load_reports(), AliasTable.build(rules, techniques), techniques, rules


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    with make_client() as client:
        tagged = tag_reports(client, *_setup())
    write_outputs(output, tagged)
    for report in tagged:
        ids = ", ".join(t["id"] for t in report.techniques) or "-"
        print(f"{report.id}  {report.status:<7} actors: {', '.join(report.actors) or '-'}")
        print(f"        malware: {', '.join(report.malware) or '-'}  techniques: {ids}")
        for reason in report.reasons:
            print(f"        review: {reason}")
    review = sum(report.status == REVIEW for report in tagged)
    print(f"\n{len(tagged)} reports: {len(tagged) - review} tagged, {review} to review")
    print(f"outputs: {output / 'tags.json'}, {output / 'entities.csv'}, {output / 'iocs.csv'}")


if __name__ == "__main__":
    main()
