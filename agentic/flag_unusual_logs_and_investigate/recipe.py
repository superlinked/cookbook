"""Group every log line in Python, embed only what's new, and hand the odd cases to a Claude agent.

Who:       The on-call engineer at a small online shop. Nothing here pages anyone or changes
           anything: the agent writes a note, and the engineer decides.
Input:     data/logs.csv.gz, one day of logs. data/known_patterns.csv, last week's patterns and
           their usual counts. data/deploys.csv and data/runbooks.md.
Watcher:   No agent. Every 15 minutes of logs, Python groups the lines into patterns.
           Qwen/Qwen3-Embedding-4B on SIE compares each pattern it hasn't seen with last week's,
           and Python checks each known pattern's count against its usual one.
Agent:     Only the odd cases go to Claude, with their example lines masked by
           superlinked/pii-redact on SIE. Its tools are:
           search_logs      plain Python over the logs, then superlinked/pii-redact on SIE
           get_deploys      plain Python
           search_runbooks  Qwen/Qwen3-Embedding-4B, then Qwen/Qwen3-Reranker-4B, on SIE
           submit_note      plain Python, ends the case
Review:    Python checks every quote and figure in the note, and sentences claiming an action.
           What it finds goes back to Claude once to fix. Python then sets the status.
Output:    runs/<timestamp>/: windows.csv, cases.csv, notes/<case>.md, transcripts/<case>.json and
           summary.md with the counts and the cost comparison.
Run using: SIE Serverless, or SIE Cluster or SIE Local with SIE_BASE_URL, plus ANTHROPIC_API_KEY.

    uv run recipe.py
    uv run recipe.py --until 12:00
    uv run recipe.py --rules my-rules.yaml
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import anthropic
import numpy as np
import yaml
from dotenv import load_dotenv
from log_patterns import pattern_of
from sie_calls import WAIT_S, CallFailed, Calls, SIEStopped
from sie_sdk import SIEClient
from sie_sdk.types import Item

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
RUNS_DIR = HERE / "runs"
RULES_PATH = HERE / "rules.yaml"
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
SAME_CHARACTER = {0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"', 0x2013: "-"}
LOOKUP_TOOLS = ("search_logs", "get_deploys")
STAMP = "%Y-%m-%dT%H:%M:%S"

CASE_ROW = [
    "case_id",
    "raised_at",
    "odd_patterns",
    "status",
    "tool_calls",
    "runbook_searches",
    "revisions",
    "sie_calls",
    "seconds",
    "input_tokens",
    "cache_write_tokens",
    "cache_read_tokens",
    "output_tokens",
    "cost_usd",
]
WINDOW_ROW = ["window", "lines", "patterns", "new_patterns", "same_as_known", "odd", "case_id"]


class ToolError(Exception):
    """A lookup that failed, sent back to Claude as an error it can react to."""


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    rules: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return rules


def make_client() -> SIEClient:
    """Build an SIE client from the API key and SIE_BASE_URL in the environment or `.env`."""
    load_dotenv()
    api_key = os.environ.get("SIE_API_KEY", "").strip()
    base_url = os.environ.get("SIE_BASE_URL", "").strip()
    if not api_key and not base_url:
        raise SystemExit(
            "Stopped: set SIE_API_KEY, or SIE_BASE_URL for SIE Cluster or SIE Local, in .env"
        )
    base_url = base_url or "https://api.superlinked.com"
    return SIEClient(api_key=api_key or None, base_url=base_url, timeout_s=WAIT_S)


def make_claude() -> anthropic.Anthropic:
    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Stopped: set ANTHROPIC_API_KEY, in .env or the environment.")
    return anthropic.Anthropic()


# --- The data --------------------------------------------------------------------------------


@dataclass(frozen=True)
class LogLine:
    time: datetime
    service: str
    level: str
    message: str

    @property
    def pattern(self) -> str:
        return pattern_of(self.service, self.level, self.message)

    def text(self) -> str:
        return f"{self.time.strftime(STAMP)} {self.service} {self.level} {self.message}"


def load_logs(path: Path) -> list[LogLine]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as file:
        return [
            LogLine(
                datetime.strptime(row["time"], STAMP).replace(tzinfo=UTC),
                row["service"],
                row["level"],
                row["message"],
            )
            for row in csv.DictReader(file)
        ]


def load_known(path: Path) -> dict[str, float]:
    with path.open(encoding="utf-8", newline="") as file:
        return {row["pattern"]: float(row["per_15_min"]) for row in csv.DictReader(file)}


def load_deploys(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", text.lower()).strip("-")


def split_sections(markdown: str) -> list[dict[str, str]]:
    """One item per `##` heading. Text above the first one is the title, and is dropped."""
    sections: list[dict[str, str]] = []
    parts = re.split(r"^##[ \t]+(.+?)[ \t]*$", markdown, flags=re.M)
    for heading, body in zip(parts[1::2], parts[2::2], strict=True):
        if body.strip():
            sections.append({"section_id": slug(heading), "heading": heading, "text": body.strip()})
    return sections


# --- Masking personal data -------------------------------------------------------------------


def outside(start: int, end: int, keep: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The parts of a span that don't overlap any of the `keep` ranges."""
    parts: list[tuple[int, int]] = []
    for kept_start, kept_end in sorted(keep):
        if kept_end <= start or kept_start >= end:
            continue
        if kept_start > start:
            parts.append((start, kept_start))
        start = max(start, kept_end)
    return [*parts, (start, end)] if start < end else parts


def word_windows(text: str, size: int, overlap: int) -> list[tuple[int, str]]:
    """Split text into windows of at most `size` words; each is (offset in text, window text)."""
    words = list(re.finditer(r"\S+", text))
    if not words:
        return []
    step = max(1, size - overlap)
    parts: list[tuple[int, str]] = []
    for first in range(0, len(words), step):
        last = words[min(first + size, len(words)) - 1]
        start = words[first].start()
        parts.append((start, text[start : last.end()]))
        if first + size >= len(words):
            break
    return parts


def valid_entity(window: str, entity: Any) -> bool:
    """A span counts only if its offsets are in range."""
    if not isinstance(entity, dict):
        return False
    start, end = entity.get("start"), entity.get("end")
    return (
        isinstance(start, int)
        and isinstance(end, int)
        and 0 <= start < end <= len(window)
        and isinstance(entity.get("label"), str)
    )


def replace_spans(text: str, spans: list[tuple[int, int, str]]) -> tuple[str, dict[str, int]]:
    """Replace each span with [kind], and count what was replaced. Overlapping spans, such as the
    model and a pattern finding the same email address, become one, named by the first."""
    out: list[str] = []
    counts: dict[str, int] = {}
    position = 0
    for start, end, kind in sorted(spans):
        if start >= position:
            out += [text[position:start], f"[{kind}]"]
            counts[kind] = counts.get(kind, 0) + 1
        position = max(position, end)
    return "".join(out) + text[position:], counts


def mask_personal_data(
    calls: Calls, client: SIEClient, key: str, text: str, rules: dict[str, Any]
) -> tuple[str, dict[str, int]]:
    """The text with personal data replaced, and how many of each kind. Raises CallFailed when a
    call fails or a reply has a span it can't place, so that text never reaches Claude unmasked."""
    model = rules["models"]["pii"]
    settings = rules["masking"]
    kinds: dict[str, str] = settings["labels"]
    spans = [
        (match.start(), match.end(), kind)
        for kind, pattern in settings["patterns"].items()
        for match in re.finditer(pattern, text)
    ]
    windows = word_windows(text, settings["window_words"], settings["window_overlap_words"])
    for number, (offset, window) in enumerate(windows, start=1):
        item = Item(id=f"{key}/w{number}", text=window)

        def work(item: Item = item) -> Any:
            # A window too long to read whole fails instead of being cut short, so a name in a
            # cut tail can't reach Claude unmasked.
            return client.extract(
                model,
                item,
                labels=list(kinds),
                options={"overflow_policy": "error"},
                provision_timeout_s=WAIT_S,
            )

        def valid(reply: Any, window: str = window) -> bool:
            return (
                isinstance(reply, dict)
                and isinstance(reply.get("entities"), list)
                and all(valid_entity(window, entity) for entity in reply["entities"])
            )

        reply = calls.call(model, work, valid, "'entities' with valid offsets")
        for entity in reply["entities"]:
            kind = kinds.get(entity["label"], entity["label"])
            spans.append((offset + entity["start"], offset + entity["end"], kind))
    keep = [m.span() for pattern in settings["keep"] for m in re.finditer(pattern, text)]
    parts = [
        (start, end, kind)
        for span_start, span_end, kind in spans
        for start, end in outside(span_start, span_end, keep)
    ]
    return replace_spans(text, parts)


# --- Embeddings and search -------------------------------------------------------------------


def unit_rows(vectors: Any) -> Any:
    matrix = np.asarray(vectors, dtype=np.float32)
    return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)


def embed(
    calls: Calls,
    client: SIEClient,
    model: str,
    texts: list[str],
    is_query: bool,
    instruction: str | None = None,
) -> Any:
    """One unit vector per text, from one SIE call."""
    items = [Item(id=str(number), text=text) for number, text in enumerate(texts)]
    options: dict[str, Any] = {"instruction": instruction} if instruction else {}
    reply = calls.call(
        model,
        lambda: client.encode(
            model, items, is_query=is_query, provision_timeout_s=WAIT_S, **options
        ),
        lambda r: (
            isinstance(r, list)
            and len(r) == len(items)
            and all(isinstance(x, dict) and "dense" in x for x in r)
        ),
        "a 'dense' vector per text",
    )
    return unit_rows([result["dense"] for result in reply])


@dataclass
class Runbooks:
    sections: list[dict[str, str]]
    vectors: Any


# --- The watcher -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Odd:
    """One pattern the watcher found odd in a window, and why."""

    pattern: str
    reason: str  # "new" or "rate"
    count: int
    usual: float | None  # lines per 15 minutes last week; None for a new pattern
    closest: str  # the closest known pattern, for a new one
    similarity: float
    examples: list[LogLine]


@dataclass
class WindowResult:
    start: datetime
    lines: int
    patterns: int
    new_patterns: list[str]
    same_as_known: list[str]
    odd: list[Odd]


# Embeds new patterns and returns, for each, the closest pattern from last week or earlier today,
# and its cosine similarity.
Similar = Callable[[list[str]], list[tuple[str, float]]]


@dataclass
class Watcher:
    """What the watcher remembers through the day. It never sees the lines after a window."""

    known: dict[str, float]  # last week's patterns and their usual count per 15 minutes
    rules: dict[str, Any]
    same_as: dict[str, str] = field(default_factory=dict)  # new pattern -> known pattern
    seen_new: set[str] = field(default_factory=set)  # new patterns already compared
    last_raised: dict[str, datetime] = field(default_factory=dict)

    def step(self, start: datetime, lines: list[LogLine], similar: Similar) -> WindowResult:
        """Read one window of lines and say which patterns are odd in it."""
        settings = self.rules["watch"]
        by_pattern: dict[str, list[LogLine]] = {}
        for line in lines:
            by_pattern.setdefault(line.pattern, []).append(line)
        unseen = sorted(p for p in by_pattern if p not in self.known and p not in self.seen_new)
        new_here: list[str] = []
        same_here: list[str] = []
        odd: list[Odd] = []
        threshold = settings["same_event_similarity"]
        for pattern, (closest, score) in zip(
            unseen, similar(unseen) if unseen else [], strict=True
        ):
            self.seen_new.add(pattern)
            if score >= threshold:
                # The same event as a known pattern, or as a new one seen earlier today, such as
                # the same warmup line for another region.
                if closest in self.known:
                    self.same_as[pattern] = closest
                same_here.append(pattern)
                continue
            new_here.append(pattern)
            if self.quiet(pattern, start):
                group = by_pattern[pattern]
                odd.append(
                    Odd(pattern, "new", len(group), None, closest, score, self.sample(group))
                )
        # A known pattern's count includes the new wordings that mean the same event.
        counts: dict[str, list[LogLine]] = {}
        for pattern, group in by_pattern.items():
            known = pattern if pattern in self.known else self.same_as.get(pattern)
            if known is not None:
                counts.setdefault(known, []).extend(group)
        for known, group in sorted(counts.items()):
            usual = self.known[known]
            busy = len(group) >= settings["rate_min_lines"]
            high = len(group) >= settings["rate_times_usual"] * max(usual, 1.0)
            if busy and high and self.quiet(known, start):
                odd.append(Odd(known, "rate", len(group), usual, "", 1.0, self.sample(group)))
        for item in odd:
            self.last_raised[item.pattern] = start
        return WindowResult(start, len(lines), len(by_pattern), new_here, same_here, odd)

    def quiet(self, pattern: str, start: datetime) -> bool:
        """True when the pattern hasn't raised a case recently."""
        last = self.last_raised.get(pattern)
        quiet_for = timedelta(minutes=self.rules["watch"]["quiet_minutes"])
        return last is None or start - last >= quiet_for

    def sample(self, group: list[LogLine]) -> list[LogLine]:
        return group[: self.rules["watch"]["samples_per_pattern"]]


def windows_of(lines: list[LogLine], minutes: int) -> list[tuple[datetime, list[LogLine]]]:
    """The lines in consecutive windows from midnight, including empty ones."""
    if not lines:
        return []
    size = timedelta(minutes=minutes)
    first = lines[0].time.replace(hour=0, minute=0, second=0)
    count = int((lines[-1].time - first) / size) + 1
    windows: list[tuple[datetime, list[LogLine]]] = [(first + n * size, []) for n in range(count)]
    for line in lines:
        windows[int((line.time - first) / size)][1].append(line)
    return windows


# --- The case the agent gets -----------------------------------------------------------------


def describe(item: Odd) -> str:
    if item.reason == "new":
        return (
            f"new pattern, not seen last week; the closest known pattern is {item.closest!r} at a "
            f"similarity of {item.similarity:.2f}. {item.count} lines in this window."
        )
    assert item.usual is not None
    times = item.count / max(item.usual, 1.0)
    return (
        f"{item.count} lines in this window, where last week it averaged {item.usual:.2f} per 15 "
        f"minutes: about {times:.0f} times its usual count."
    )


def case_text(case_id: str, start: datetime, minutes: int, odd: list[Odd], examples: str) -> str:
    """The case as the agent reads it, before masking. `examples` is the example lines, in order."""
    end = start + timedelta(minutes=minutes)
    lines = [
        f"Case {case_id}, raised at {end.strftime('%Y-%m-%dT%H:%M')} for the window "
        f"{start.strftime('%H:%M')} to {end.strftime('%H:%M')}.",
        "Odd patterns:",
    ]
    lines += [f"- {item.pattern}: {describe(item)}" for item in odd]
    lines += ["Example lines:", examples]
    return "\n".join(lines)


# --- The tools Claude can call ---------------------------------------------------------------


def object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


TIME = {"type": "string", "description": "HH:MM on the case's day, or empty for the default"}
TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_logs",
        "description": (
            "Search the day's log lines up to the time the case was raised. Matches lines whose "
            "message contains the text (any case), on one service or all. Returns how many lines "
            "match, by level and per 15 minutes, and up to 10 example lines with personal data "
            "masked. Empty times mean the two hours before the case."
        ),
        "strict": True,
        "input_schema": object_schema(
            {
                "text": {"type": "string", "description": "words in the message; empty for any"},
                "service": {"type": "string", "description": "one service; empty for all"},
                "start_time": TIME,
                "end_time": TIME,
            }
        ),
    },
    {
        "name": "get_deploys",
        "description": (
            "Deploys and config changes between two times on the case's day, up to the time the "
            "case was raised: time, service, version and the change note."
        ),
        "strict": True,
        "input_schema": object_schema({"start_time": TIME, "end_time": TIME}),
    },
    {
        "name": "search_runbooks",
        "description": (
            "Ask the runbooks one question. Returns the closest sections with a relevance score "
            "from 0 to 1. Write the question in your own words."
        ),
        "strict": True,
        "input_schema": object_schema({"question": {"type": "string"}}),
    },
    {
        "name": "submit_note",
        "description": (
            "Hand in the note for the on-call engineer. This ends your work on the case unless "
            "the review sends it back."
        ),
        "strict": True,
        "input_schema": object_schema(
            {
                "summary": {"type": "string"},
                "likely_cause": {"type": "string"},
                "page_oncall": {"type": "boolean"},
                "next_step_for_oncall": {"type": "string"},
                "statements": {
                    "type": "array",
                    "items": object_schema(
                        {
                            "claim": {"type": "string"},
                            "source": {"type": "string"},
                            "quote": {"type": "string"},
                        }
                    ),
                },
            }
        ),
    },
]


@dataclass
class Toolbox:
    """What Claude's tools can reach for one case: the logs and deploys up to `now`, and the
    runbooks."""

    client: SIEClient
    calls: Calls
    rules: dict[str, Any]
    logs: list[LogLine]
    deploys: list[dict[str, str]]
    runbooks: Runbooks
    now: datetime
    case_id: str = ""
    case: str = ""  # the masked case text, which quotes and figures may come from
    outputs: dict[str, list[str]] = field(default_factory=dict)
    masked_counts: dict[str, int] = field(default_factory=dict)
    failed_calls: int = 0

    def run(self, name: str, tool_input: dict[str, Any]) -> str:
        """Run one lookup and return its result as JSON. Raises ToolError for Claude to read."""
        tools: dict[str, Callable[..., dict[str, Any]]] = {
            "search_logs": self.search_logs,
            "get_deploys": self.get_deploys,
            "search_runbooks": self.search_runbooks,
        }
        if name not in tools:
            raise ToolError(f"there is no tool called {name}")
        try:
            output = json.dumps(tools[name](**tool_input))
        except CallFailed as error:
            # The details stay in Calls.failures: a server's error could repeat unmasked text.
            self.failed_calls += 1
            raise ToolError(f"{name} failed, so you don't have this result") from error
        self.outputs.setdefault(name, []).append(output)
        return output

    def between(self, start_time: str, end_time: str) -> tuple[datetime, datetime]:
        """The range asked for, cut off at `now`. Empty times mean the two hours before `now`."""
        day = self.now.replace(hour=0, minute=0, second=0)

        def parse(value: str, default: datetime) -> datetime:
            if not value:
                return default
            match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
            if not match or int(match[1]) > 23 or int(match[2]) > 59:
                raise ToolError(f"{value!r} isn't a time like 10:30")
            return day + timedelta(hours=int(match[1]), minutes=int(match[2]))

        start = parse(start_time, self.now - timedelta(hours=2))
        end = min(parse(end_time, self.now), self.now)
        if start >= end:
            raise ToolError(f"nothing to show: the case was raised at {self.now:%H:%M}")
        return start, end

    def search_logs(
        self, text: str, service: str, start_time: str, end_time: str
    ) -> dict[str, Any]:
        start, end = self.between(start_time, end_time)
        wanted = text.casefold().strip()
        found = [
            line
            for line in self.logs
            if start <= line.time < end
            and (not service or line.service == service)
            and wanted in line.message.casefold()
        ]
        minutes = self.rules["watch"]["window_minutes"]
        per_window = Counter(
            (line.time - timedelta(minutes=line.time.minute % minutes)).strftime("%H:%M")
            for line in found
        )
        limit = self.rules["logs"]["max_examples"]
        # The first and last matches, so a start and an end both show.
        chosen = found if len(found) <= limit else found[: limit // 2] + found[-(limit // 2) :]
        examples: list[str] = []
        if chosen:
            key = f"{self.case_id}/search{len(self.outputs.get('search_logs', [])) + 1}"
            joined = "\n".join(line.text() for line in chosen)
            masked, counts = mask_personal_data(self.calls, self.client, key, joined, self.rules)
            for kind, count in counts.items():
                self.masked_counts[kind] = self.masked_counts.get(kind, 0) + count
            examples = masked.split("\n")
        return {
            "from": start.strftime("%H:%M"),
            "to": end.strftime("%H:%M"),
            "matching_lines": len(found),
            "by_level": dict(sorted(Counter(line.level for line in found).items())),
            "per_15_min": [{"from": key, "lines": n} for key, n in sorted(per_window.items())],
            "examples": examples,
        }

    def get_deploys(self, start_time: str, end_time: str) -> dict[str, Any]:
        start, end = self.between(start_time, end_time)
        stamp = "%Y-%m-%dT%H:%M"
        chosen = [
            row
            for row in self.deploys
            if start <= datetime.strptime(row["time"], stamp).replace(tzinfo=UTC) < end
        ]
        return {"from": start.strftime("%H:%M"), "to": end.strftime("%H:%M"), "deploys": chosen}

    def search_runbooks(self, question: str) -> dict[str, Any]:
        settings = self.rules["search"]
        model = self.rules["models"]["embed"]
        query = embed(
            self.calls, self.client, model, [question], True, settings["query_instruction"]
        )
        scores = self.runbooks.vectors @ query[0]
        nearest = [int(i) for i in np.argsort(-scores)[: settings["embed_top_k"]]]
        candidates = [self.runbooks.sections[i] for i in nearest]
        by_id = {item["section_id"]: item for item in candidates}
        model = self.rules["models"]["rerank"]
        scored = self.calls.call(
            model,
            lambda: self.client.score(
                model,
                Item(id="question", text=question),
                [
                    Item(id=item["section_id"], text=f"{item['heading']}\n{item['text']}")
                    for item in candidates
                ],
                instruction=settings["rerank_instruction"],
                provision_timeout_s=WAIT_S,
            ),
            lambda r: (
                isinstance(r, dict)
                and isinstance(r.get("scores"), list)
                and {e.get("item_id") for e in r["scores"] if isinstance(e, dict)} == set(by_id)
                and all(isinstance(e.get("score"), (int, float)) for e in r["scores"])
            ),
            "a score for every section",
        )
        ranked = sorted(scored["scores"], key=lambda entry: -float(entry["score"]))
        return {
            "results": [
                {
                    "section_id": entry["item_id"],
                    "heading": by_id[entry["item_id"]]["heading"],
                    "score": round(float(entry["score"]), 3),
                    "text": by_id[entry["item_id"]]["text"],
                }
                for entry in ranked[: settings["return_top_k"]]
            ]
        }


# --- The agent loop --------------------------------------------------------------------------


@dataclass
class AgentResult:
    note: dict[str, Any] | None = None
    ran_out: bool = False  # hit the limit on lookups or requests
    stopped: str = ""  # why there's no note, when there isn't one
    tool_calls: int = 0  # lookups and submissions
    lookups: int = 0
    revisions: int = 0  # times the note went back to Claude
    first_review: list[str] = field(default_factory=list)  # what the first review found
    problems: list[str] = field(default_factory=list)  # what the last review found
    input_tokens: int = 0  # not read from or written to the cache
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    transcript: list[dict[str, Any]] = field(default_factory=list)


def ask_claude(claude: anthropic.Anthropic, messages: list[Any], rules: dict[str, Any]) -> Any:
    settings = rules["agent"]
    extra: dict[str, Any] = {}
    if settings["fallbacks"]:
        extra = {"betas": [settings["fallback_beta"]], "fallbacks": settings["fallbacks"]}
    return claude.beta.messages.create(
        model=rules["models"]["agent"],
        max_tokens=settings["max_tokens"],
        system=settings["system"],
        tools=cast(Any, TOOLS),
        messages=messages,
        output_config={"effort": settings["effort"]},
        cache_control={"type": "ephemeral"},
        **extra,
    )


def case_prompt(case: str) -> str:
    # Log lines can hold anything, so they can't be allowed to close the tag themselves.
    return f"<case>\n{case.replace('</case>', '')}\n</case>"


def text_of(content: list[Any]) -> str:
    return "\n".join(block.text for block in content if block.type == "text")


def one_lookup(box: Toolbox, use: Any, result: AgentResult, max_lookups: int) -> dict[str, Any]:
    """Run one tool call, or refuse it past the limit, and shape the result for Claude."""
    entry: dict[str, Any] = {"tool": use.name, "input": use.input}
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": use.id}
    if result.lookups >= max_lookups:
        result.ran_out = True
        output = "No lookups left. Call submit_note now with what you have."
        block["is_error"] = True
    else:
        result.lookups += 1
        result.tool_calls += 1
        try:
            output = box.run(use.name, use.input)
        except ToolError as error:
            output = str(error)
            block["is_error"] = True
    entry["output"] = output
    entry["is_error"] = block.get("is_error", False)
    result.transcript.append(entry)
    block["content"] = output
    return block


def one_submission(
    box: Toolbox, use: Any, result: AgentResult, tool_results: list[dict[str, Any]]
) -> bool:
    """Check a submitted note. True ends the loop; False sends what was found back to Claude."""
    result.tool_calls += 1
    note = dict(use.input)
    problems = check_note(note, box)
    result.transcript.append({"tool": "submit_note", "input": use.input, "problems": problems})
    if problems and result.revisions < box.rules["agent"]["max_revisions"]:
        result.revisions += 1
        result.first_review = result.first_review or problems
        found = "\n".join(f"- {problem}" for problem in problems)
        tool_results.append(
            {
                "type": "tool_result",
                "tool_use_id": use.id,
                "is_error": True,
                "content": (
                    "The review found these problems. Fix them and call submit_note again.\n"
                    + found
                ),
            }
        )
        return False
    result.note, result.problems = note, problems
    return True


def run_agent(claude: anthropic.Anthropic, box: Toolbox, prompt: str) -> AgentResult:
    """Let Claude look into the case until it submits a note or reaches a limit."""
    settings = box.rules["agent"]
    messages: list[Any] = [{"role": "user", "content": prompt}]
    result = AgentResult(transcript=[{"prompt": prompt}])
    asked_to_submit = False

    for _ in range(settings["max_rounds"]):
        try:
            response = ask_claude(claude, messages, box.rules)
        except (
            anthropic.RateLimitError,
            anthropic.InternalServerError,
            anthropic.APIConnectionError,
        ) as error:
            result.stopped = f"the Claude API failed: {type(error).__name__}"
            return result
        result.input_tokens += response.usage.input_tokens
        result.output_tokens += response.usage.output_tokens
        result.cache_read_tokens += response.usage.cache_read_input_tokens or 0
        result.cache_write_tokens += response.usage.cache_creation_input_tokens or 0

        if response.stop_reason == "refusal":
            result.stopped = "Claude declined the request"
            return result
        if response.stop_reason == "max_tokens":
            result.stopped = "Claude hit max_tokens in rules.yaml"
            return result
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue

        uses = [block for block in response.content if block.type == "tool_use"]
        if not uses:
            if asked_to_submit:
                result.stopped = "Claude ended its turn without calling submit_note"
                return result
            asked_to_submit = True
            result.transcript.append({"said": text_of(response.content)})
            nudge = "Call submit_note now, with what you have."
            messages.append({"role": "user", "content": nudge})
            continue

        tool_results: list[dict[str, Any]] = []
        accepted = False
        # Lookups first, so a note sent in the same response is checked against their results.
        for use in sorted(uses, key=lambda use: use.name == "submit_note"):
            if use.name != "submit_note":
                tool_results.append(one_lookup(box, use, result, settings["max_lookups"]))
            elif one_submission(box, use, result, tool_results):
                accepted = True
                break
        if accepted:
            return result
        messages.append({"role": "user", "content": tool_results})

    result.ran_out = True
    result.stopped = "reached the limit on requests to Claude"
    return result


# --- Checks on the note ----------------------------------------------------------------------


def plain(text: str) -> str:
    """Text for comparing a quote: no markdown marks, one kind of quote and dash, single spaces."""
    text = re.sub(r"[`*\\]", "", text).translate(SAME_CHARACTER)
    return re.sub(r"\s+", " ", text).strip().casefold()


def figures(text: str, min_digits: int) -> set[str]:
    """Decimals such as 40.50, and whole numbers with at least `min_digits` digits."""
    joined = re.sub(r"(?<=\d),(?=\d{3})", "", text)
    pattern = rf"\d+\.\d+|(?<![\d.])\d{{{min_digits},}}(?![\d]|\.\d)"
    # 75.0 and 75 are the same figure.
    return {re.sub(r"\.0+$", "", figure) for figure in re.findall(pattern, joined)}


def readable(output: str) -> str:
    """A tool's JSON result as its plain values, so a line break in a log line is a line break."""

    def values(value: Any) -> list[str]:
        if isinstance(value, dict):
            return [text for item in value.values() for text in values(item)]
        if isinstance(value, list):
            return [text for item in value for text in values(item)]
        return [str(value)]

    return "\n".join(values(json.loads(output)))


def action_claims(text: str, patterns: list[str]) -> list[str]:
    """Where the note claims an action. The agent only writes a note, so none has happened."""
    plain_text = plain(text)
    return [match.group(0) for pattern in patterns for match in re.finditer(pattern, plain_text)]


def check_note(note: dict[str, Any], box: Toolbox) -> list[str]:
    """Every problem in a note. An empty list means every quote and figure checked out."""
    rules = box.rules
    digits = rules["checks"]["figure_min_digits"]
    sections = {item["section_id"]: item for item in box.runbooks.sections}
    found = "\n".join(readable(output) for outputs in box.outputs.values() for output in outputs)
    runbook_text = "\n".join(f"{s['heading']}\n{s['text']}" for s in box.runbooks.sections)
    allowed = figures(f"{box.case}\n{found}\n{runbook_text}", digits)
    problems: list[str] = []
    if not note["summary"].strip():
        problems.append("the summary is empty")
    if not note["statements"]:
        problems.append("the note has no statements with sources")
    for statement in note["statements"]:
        source, quote = statement["source"], statement["quote"]
        if source == "case":
            whole = box.case
        elif source in sections:
            whole = f"{sections[source]['heading']}\n{sections[source]['text']}"
        elif source in LOOKUP_TOOLS:
            whole = "\n".join(f"{o}\n{readable(o)}" for o in box.outputs.get(source, []))
        else:
            problems.append(f"cites a source that doesn't exist: {source}")
            continue
        if not plain(quote) or plain(quote) not in plain(whole):
            problems.append(f"the quote isn't word for word in {source}: {quote!r}")
        for figure in sorted(figures(statement["claim"], digits) - allowed):
            problems.append(f"{figure} in a claim didn't come from the case or any lookup")
    for name in ("summary", "likely_cause"):
        for figure in sorted(figures(note[name], digits) - allowed):
            problems.append(f"{figure} in the {name} didn't come from the case or any lookup")
    told = f"{note['summary']}\n{note['likely_cause']}"
    for claim in action_claims(told, rules["checks"]["action_claims"]):
        problems.append(
            f"the note says {claim!r}, but nobody has done that. Take it out, and put what needs "
            "doing in next_step_for_oncall"
        )
    return problems


def status_of(result: AgentResult, problems: list[str]) -> str:
    """The status, from what happened, what the checks found and Claude's recommendation."""
    if result.ran_out:
        return "out_of_steps"
    if result.note is None:
        return "no_note"
    if problems:
        return "check_failed"
    return "page_recommended" if result.note["page_oncall"] else "no_page_recommended"


# --- Cost ------------------------------------------------------------------------------------


def claude_cost(row: dict[str, Any], prices: dict[str, float]) -> float:
    """What a case's Claude requests cost, in dollars."""
    return (
        float(
            row["input_tokens"] * prices["input"]
            + row["cache_write_tokens"] * prices["cache_write"]
            + row["cache_read_tokens"] * prices["cache_read"]
            + row["output_tokens"] * prices["output"]
        )
        / 1_000_000
    )


def tokens_per_line(
    claude: anthropic.Anthropic, logs: list[LogLine], rules: dict[str, Any]
) -> float:
    """How many input tokens one log line costs Claude, from a sample through the counting API."""
    step = max(1, len(logs) // rules["token_sample_lines"])
    sample = logs[::step][: rules["token_sample_lines"]]
    counted = claude.messages.count_tokens(
        model=rules["models"]["agent"],
        messages=[{"role": "user", "content": "\n".join(line.text() for line in sample)}],
    )
    return float(counted.input_tokens) / len(sample)


# --- Output ----------------------------------------------------------------------------------


def csv_safe(value: Any) -> str:
    """A leading ' on a cell a spreadsheet would run as a formula; anything else as written."""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def note_markdown(case_id: str, status: str, result: AgentResult, box: Toolbox) -> str:
    lines = [f"# {case_id}", "", f"**Status:** `{status}`", ""]
    if result.stopped:
        lines += [f"**Stopped because:** {result.stopped}", ""]
    if box.failed_calls:
        lines += [
            f"**{box.failed_calls} lookup(s) failed, so this note may be missing something.**"
        ]
        lines.append("")
    if result.problems:
        lines += ["**Check these before acting:**", ""]
        lines += [f"- {problem}" for problem in result.problems] + [""]
    if result.first_review:
        lines += ["**The review sent the first note back for these, and Claude revised it:**", ""]
        lines += [f"- {problem}" for problem in result.first_review] + [""]
    if box.masked_counts:
        removed = ", ".join(f"{count} {kind}" for kind, count in sorted(box.masked_counts.items()))
        lines += [f"**Personal data masked before Claude saw it:** {removed}", ""]
    note = result.note
    if note:
        page = "yes" if note["page_oncall"] else "no"
        lines += [f"**Page the on-call engineer:** {page}", ""]
        lines += ["## Summary", "", note["summary"], ""]
        lines += ["## Likely cause", "", note["likely_cause"], ""]
        if note["next_step_for_oncall"]:
            lines += ["## Next step for the on-call engineer", "", note["next_step_for_oncall"], ""]
        lines += ["## Sources", ""]
        for statement in note["statements"]:
            lines.append(f'- {statement["claim"]} `{statement["source"]}`: "{statement["quote"]}"')
        lines.append("")
    lines += ["## The case as the agent got it", "", "```text", box.case, "```", ""]
    lines += ["## Every tool call", ""]
    for entry in result.transcript:
        if "tool" in entry and entry["tool"] != "submit_note":
            failed = " (failed)" if entry["is_error"] else ""
            lines.append(f"- `{entry['tool']}` {json.dumps(entry['input'])}{failed}")
    return "\n".join(lines) + "\n"


def write_outputs(
    output: Path, windows: list[dict[str, Any]], cases: list[dict[str, Any]], summary: str
) -> None:
    (output / "notes").mkdir(parents=True, exist_ok=True)
    (output / "transcripts").mkdir(exist_ok=True)
    for name, header, rows in (
        ("windows.csv", WINDOW_ROW, windows),
        ("cases.csv", CASE_ROW, cases),
    ):
        with (output / name).open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=header)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: csv_safe(row[key]) for key in header})
    for row in cases:
        (output / "notes" / f"{row['case_id']}.md").write_text(row["markdown"], encoding="utf-8")
        (output / "transcripts" / f"{row['case_id']}.json").write_text(
            json.dumps(row["transcript"], indent=1, default=str), encoding="utf-8"
        )
    (output / "summary.md").write_text(summary, encoding="utf-8")


# --- The run ---------------------------------------------------------------------------------


def investigate(
    claude: anthropic.Anthropic, box: Toolbox, window: WindowResult, minutes: int
) -> dict[str, Any]:
    """Mask the case's example lines, let the agent look into it, and shape the result."""
    calls = box.calls
    before = calls.total
    started = time.monotonic()
    examples = "\n".join(line.text() for item in window.odd for line in item.examples)
    raw = case_text(box.case_id, window.start, minutes, window.odd, examples)
    try:
        # The whole case, because a pattern can hold a name the pattern rules don't replace.
        masked, counts = mask_personal_data(
            calls, box.client, f"{box.case_id}/case", raw, box.rules
        )
    except CallFailed as error:
        # Fail closed: unmasked log lines don't go to a third-party API.
        result = AgentResult(stopped=f"personal data couldn't be masked: {error}")
        box.case = "(not shown: personal data couldn't be masked)"
    else:
        box.masked_counts.update(counts)
        box.case = masked
        result = run_agent(claude, box, case_prompt(box.case))
    status = status_of(result, result.problems)
    row: dict[str, Any] = {
        "case_id": box.case_id,
        "raised_at": box.now.strftime("%H:%M"),
        "odd_patterns": " | ".join(f"{item.reason}: {item.pattern}" for item in window.odd),
        "status": status,
        "tool_calls": result.tool_calls,
        "runbook_searches": sum(1 for e in result.transcript if e.get("tool") == "search_runbooks"),
        "revisions": result.revisions,
        "sie_calls": calls.total - before,
        "seconds": round(time.monotonic() - started),
        "input_tokens": result.input_tokens,
        "cache_write_tokens": result.cache_write_tokens,
        "cache_read_tokens": result.cache_read_tokens,
        "output_tokens": result.output_tokens,
        "markdown": note_markdown(box.case_id, status, result, box),
        "transcript": result.transcript,
    }
    row["cost_usd"] = f"{claude_cost(row, box.rules['prices'][box.rules['models']['agent']]):.4f}"
    return row


def summary_markdown(
    rules: dict[str, Any],
    lines_read: int,
    windows: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    calls: Calls,
    per_line: float,
) -> str:
    """The day in numbers, and what Claude reading every line would have cost instead."""
    agent = rules["models"]["agent"]
    spent = sum(float(row["cost_usd"]) for row in cases)
    tokens = lines_read * per_line
    windows_count = len(windows)
    by_model = ", ".join(f"{model}: {count}" for model, count in sorted(calls.count.items()))
    credits = (
        f"{sum(calls.credits.values()):g} ("
        + ", ".join(f"{model}: {amount:g}" for model, amount in sorted(calls.credits.items()))
        + ")"
    )
    if calls.unreported:
        credits += f", and {calls.unreported} calls whose reply didn't say"
    lines = [
        "# The day in numbers",
        "",
        f"- Log lines read by the watcher: {lines_read}",
        f"- 15 minute windows: {windows_count}",
        f"- New patterns compared by the embedding model: "
        f"{sum(int(w['new_patterns']) + int(w['same_as_known']) for w in windows)}",
        f"- Of those, the same event as a pattern already seen: "
        f"{sum(int(w['same_as_known']) for w in windows)}",
        f"- Cases sent to Claude: {len(cases)}",
        f"- SIE calls: {calls.total} ({by_model})",
        f"- SIE credits charged: {credits}",
        "",
        "## Cost",
        "",
        f"- Claude on the cases ({agent}): ${spent:.4f}, from the token counts in cases.csv",
        f"- One log line is about {per_line:.1f} Claude input tokens, counted on a sample of "
        f"{rules['token_sample_lines']} lines",
        f"- Every line through Claude: {lines_read} lines x {per_line:.1f} = {tokens:,.0f} input "
        "tokens, before any instructions or answers",
    ]
    for model, prices in rules["prices"].items():
        cost = tokens * prices["input"] / 1_000_000
        lines.append(f"  - with {model} at ${prices['input']:.2f} per million: ${cost:.2f}")
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> None:
    rules = load_rules(args.rules)
    client = make_client()
    claude = make_claude()
    calls = Calls()
    logs = load_logs(DATA_DIR / "logs.csv.gz")
    known = load_known(DATA_DIR / "known_patterns.csv")
    deploys = load_deploys(DATA_DIR / "deploys.csv")
    sections = split_sections((DATA_DIR / "runbooks.md").read_text(encoding="utf-8"))
    embed_model = rules["models"]["embed"]
    settings = rules["watch"]

    # Fixed work, with no agent: last week's patterns and the runbooks, embedded once.
    known_names = sorted(known)
    known_vectors = embed(calls, client, embed_model, known_names, False)
    runbooks = Runbooks(
        sections,
        embed(
            calls, client, embed_model, [f"{s['heading']}\n{s['text']}" for s in sections], False
        ),
    )

    # New patterns from earlier today are compared too, so one new event raises one case.
    today_names: list[str] = []
    today_vectors: list[Any] = []

    def similar(patterns: list[str]) -> list[tuple[str, float]]:
        vectors = embed(
            calls, client, embed_model, patterns, True, settings["similarity_instruction"]
        )
        names = known_names + today_names
        compared = np.vstack([known_vectors, *today_vectors]) if today_vectors else known_vectors
        scores = vectors @ compared.T
        today_names.extend(patterns)
        today_vectors.append(vectors)
        return [(names[int(row.argmax())], float(row.max())) for row in scores]

    watcher = Watcher(known, rules)
    minutes = settings["window_minutes"]
    window_rows: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    lines_read = 0
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    stop = None
    if args.until:
        hour, minute = (int(part) for part in args.until.split(":"))
        stop = logs[0].time.replace(hour=hour, minute=minute, second=0)
    finished = False
    try:
        for start, lines in windows_of(logs, minutes):
            if stop is not None and start >= stop:
                break
            lines_read += len(lines)
            window = watcher.step(start, lines, similar)
            case_id = ""
            if window.odd:
                case_id = f"case-{start.strftime('%H%M')}"
                now = start + timedelta(minutes=minutes)
                box = Toolbox(client, calls, rules, logs, deploys, runbooks, now, case_id)
                row = investigate(claude, box, window, minutes)
                print(
                    f"{case_id}: {row['status']} ({row['tool_calls']} tool calls, "
                    f"{row['revisions']} revisions, {row['seconds']} s) {row['odd_patterns']}"
                )
                cases.append(row)
            window_rows.append(
                {
                    "window": start.strftime("%H:%M"),
                    "lines": window.lines,
                    "patterns": window.patterns,
                    "new_patterns": len(window.new_patterns),
                    "same_as_known": len(window.same_as_known),
                    "odd": " | ".join(f"{item.reason}: {item.pattern}" for item in window.odd),
                    "case_id": case_id,
                }
            )
        finished = True
    finally:
        # A day that stops partway keeps the notes it already paid for.
        if window_rows:
            per_line = tokens_per_line(claude, logs, rules) if finished else 0.0
            summary = summary_markdown(rules, lines_read, window_rows, cases, calls, per_line)
            if not finished:
                summary += (
                    "\nThe run stopped partway, so these counts cover only part of the day.\n"
                )
            write_outputs(output, window_rows, cases, summary)
    if calls.failures:
        print(
            f"{len(calls.failures)} of {calls.total} SIE calls failed. First: {calls.failures[0]}"
        )
    print(summary)
    print(f"Wrote {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--until", help="stop the day at this time, such as 12:00")
    parser.add_argument("--output", type=Path, help="folder for the outputs")
    parser.add_argument("--rules", type=Path, default=RULES_PATH, help="another rules.yaml to try")
    args = parser.parse_args()
    try:
        run(args)
    except SIEStopped as error:
        raise SystemExit(f"Stopped: {error} No outputs were written.") from None
    except CallFailed as error:
        raise SystemExit(f"Stopped: an SIE call the watcher needs failed: {error}") from None
    except (
        anthropic.AuthenticationError,
        anthropic.PermissionDeniedError,
        anthropic.NotFoundError,
        anthropic.BadRequestError,
    ) as error:
        raise SystemExit(
            f"Stopped: the Claude API refused the request, {type(error).__name__}: {error}. Check "
            "ANTHROPIC_API_KEY and the model name in rules.yaml. No outputs were written."
        ) from None


if __name__ == "__main__":
    main()
