"""Fix a laptop that fails the company's security checks, with small models checking each step.

Who:       An IT helpdesk technician at a freight company. They read every report before anything
           goes back to the user, and approve anything the guard held.
Input:     data/tickets.csv, a screenshot where the user attached one, data/it_security_policy.md,
           and mdm.py, a stand-in for the device management system over data/devices.json.
Agent:     Claude runs the loop and decides what to check and fix next. Its tools are:
           get_device        plain Python, the device record without the owner's name
           search_policy     Qwen/Qwen3-Reranker-4B on SIE, over every policy section
           read_screenshot   lightonai/LightOnOCR-3-4B, then the privacy and shield checks
           apply_fix         plain Python: the guard, then the change, then a re-read of the record
           submit_report     plain Python, checked before it's accepted
Checks:    Shield    phrase rules and fastino/GLiNER2.5-Decide on SIE warn about text for a bot
           Privacy   superlinked/pii-redact on SIE masks names, emails and phone numbers first
           Guard     a list in rules.yaml says which changes run, wait for a yes, or are refused
           Done      Python re-reads the device record after every fix, and checks the report's
                     claims against it. What it finds goes back to Claude once to fix.
Output:    runs/<timestamp>/: reports.csv, reports/<ticket>.md, transcripts/<ticket>.json and
           devices_after.json. A status says what Python found: compliant_now, waiting_on_person,
           needs_tech, check_failed, out_of_steps or no_report.
Run using: SIE Serverless, or SIE Cluster or SIE Local with SIE_BASE_URL, plus ANTHROPIC_API_KEY.
           Fixes change only the stand-in records in memory, never the machine this runs on.

    uv run recipe.py
    uv run recipe.py --ticket T-3001
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

import anthropic
import yaml
from dotenv import load_dotenv
from mdm import OWNER_FIELDS, Fleet, meets
from sie_calls import WAIT_S, CallFailed, Calls, SIEStopped
from sie_sdk import SIEClient
from sie_sdk.types import Item

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
RUNS_DIR = HERE / "runs"
RULES_PATH = HERE / "rules.yaml"
POLICY = DATA_DIR / "it_security_policy.md"
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
SAME_CHARACTER = {0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"', 0x2013: "-"}
LOOKUP_TOOLS = ("get_device", "read_screenshot", "apply_fix")
SHIELD_QUESTION = "written_to_a_bot"
OUTCOMES = ("fixed", "waiting_for_user", "owner_must_do", "did_not_stick", "refused", "not_changed")

REPORT_ROW = [
    "ticket_id",
    "device_id",
    "status",
    "failing_at_start",
    "failing_now",
    "fixed",
    "held",
    "refused",
    "shield_phrases",
    "shield_model_score",
    "tool_calls",
    "revisions",
    "sie_calls",
    "seconds",
    "input_tokens",
    "output_tokens",
]


class ToolError(Exception):
    """A lookup that failed, sent back to Claude as an error it can react to."""


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def make_client() -> SIEClient:
    """Build an SIE client from the API key and SIE_BASE_URL in the environment or `.env`."""
    load_dotenv()
    api_key = os.environ.get("SIE_API_KEY", "").strip()
    base_url = os.environ.get("SIE_BASE_URL", "").strip()
    if not api_key and not base_url:
        raise SystemExit("Stopped: set SIE_API_KEY, or SIE_BASE_URL for SIE Cluster or SIE Local.")
    base_url = base_url or "https://api.superlinked.com"
    return SIEClient(api_key=api_key or None, base_url=base_url, timeout_s=WAIT_S)


# --- Tickets and the policy ------------------------------------------------------------------


@dataclass(frozen=True)
class Ticket:
    ticket_id: str
    device_id: str
    received: str
    subject: str
    body: str
    attachment: str


def load_tickets(path: Path) -> list[Ticket]:
    with path.open(encoding="utf-8", newline="") as file:
        return [Ticket(**row) for row in csv.DictReader(file)]


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


# --- Privacy: masking personal data ----------------------------------------------------------


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
    # Only a span that lies wholly inside a kept match is dropped. Anything wider is masked whole,
    # so a phone number with dots in it isn't cut into a "version" and the digits around it.
    masked = [
        span
        for span in spans
        if not any(start <= span[0] and span[1] <= end for start, end in keep)
    ]
    return replace_spans(text, masked)


# --- Shield: text written to a bot -----------------------------------------------------------


@dataclass
class ShieldResult:
    source: str  # "ticket" or "screenshot"
    phrases: list[str] = field(default_factory=list)  # which phrase rules matched
    model_score: float | None = None  # the warning option's highest probability over the windows
    model_failed: str = ""  # why the model gave no score, when it didn't

    def warned(self, rules: dict[str, Any]) -> bool:
        """Any phrase, a high model score, or a model that couldn't check the text."""
        high = self.model_score is not None and self.model_score >= rules["shield"]["min_score"]
        return bool(self.phrases) or high or bool(self.model_failed)

    def describe(self, rules: dict[str, Any]) -> str:
        parts = []
        if self.phrases:
            parts.append("phrase rules matched " + ", ".join(self.phrases))
        if self.model_score is not None:
            verdict = (
                "warns" if self.model_score >= rules["shield"]["min_score"] else "doesn't warn"
            )
            parts.append(f"the model {verdict} ({self.model_score:.2f})")
        if self.model_failed:
            parts.append(f"the model couldn't check it: {self.model_failed}")
        return f"{self.source}: " + "; ".join(parts)


def shield_phrases(text: str, rules: dict[str, Any]) -> list[str]:
    flat = " ".join(text.split()).casefold()
    return [
        name for name, pattern in rules["shield"]["markers"].items() if re.search(pattern, flat)
    ]


def shield_score(
    calls: Calls, client: SIEClient, key: str, text: str, rules: dict[str, Any]
) -> float:
    """The warning option's highest probability over the text's windows. Raises CallFailed."""
    settings = rules["shield"]
    model = rules["models"]["shield"]
    schema = {
        SHIELD_QUESTION: {
            "type": "choice",
            "instructions": settings["question"],
            "criteria": dict(settings["options"]),
        }
    }
    windows = [window for _, window in word_windows(text, settings["window_words"], 0)] or [""]
    items = [Item(id=f"{key}/s{number}", text=window) for number, window in enumerate(windows)]

    def valid(reply: Any) -> bool:
        if not isinstance(reply, list) or len(reply) != len(items):
            return False
        for result in reply:
            answer = (
                (result.get("data") or {}).get(SHIELD_QUESTION)
                if isinstance(result, dict)
                else None
            )
            probabilities = answer.get("probabilities") if isinstance(answer, dict) else None
            score = (
                probabilities.get(settings["warn_label"])
                if isinstance(probabilities, dict)
                else None
            )
            if not isinstance(score, (int, float)) or not 0 <= score <= 1:
                return False
        return True

    reply = calls.call(
        model,
        lambda: client.extract(model, items, output_schema=schema, provision_timeout_s=WAIT_S),
        valid,
        "a probability for every window",
    )
    label = settings["warn_label"]
    return max(float(result["data"][SHIELD_QUESTION]["probabilities"][label]) for result in reply)


def run_shield(
    calls: Calls, client: SIEClient, key: str, source: str, text: str, rules: dict[str, Any]
) -> ShieldResult:
    result = ShieldResult(source, shield_phrases(text, rules))
    try:
        result.model_score = shield_score(calls, client, key, text, rules)
    except CallFailed as error:
        # The shield only warns, so a failed check warns too, and the run goes on.
        result.model_failed = str(error)
    return result


# --- The tools Claude can call ---------------------------------------------------------------


def object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


STATEMENTS = {
    "type": "array",
    "items": object_schema(
        {"claim": {"type": "string"}, "source": {"type": "string"}, "quote": {"type": "string"}}
    ),
}


def tool_list(rules: dict[str, Any]) -> list[dict[str, Any]]:
    settings = list(rules["checks"])
    return [
        {
            "name": "get_device",
            "description": (
                "This ticket's laptop as the device management system reports it now: each "
                "security setting, SSH keys, last backup, configuration profiles, approved "
                "exceptions, and the policy checks it fails now with their section. The owner's "
                "name and email are left out."
            ),
            "strict": True,
            "input_schema": object_schema({}),
        },
        {
            "name": "search_policy",
            "description": (
                "Ask the company's laptop security policy one question at a time. Returns the "
                "closest sections with a relevance score from 0 to 1."
            ),
            "strict": True,
            "input_schema": object_schema({"question": {"type": "string"}}),
        },
        {
            "name": "read_screenshot",
            "description": (
                "Read the screenshot attached to this ticket. Returns its text with personal data "
                "masked, and any shield warning about text written to a bot."
            ),
            "strict": True,
            "input_schema": object_schema({}),
        },
        {
            "name": "apply_fix",
            "description": (
                "Ask to set one setting on this laptop to the value the policy requires, written "
                'as JSON (true, false, 300, "26.7.2"). A guard decides: applied, waiting for the '
                "user's agreement, only the owner can do it, refused, or not needed when it "
                "already meets the policy. When applied, the result shows the device record read "
                "again afterwards."
            ),
            "strict": True,
            "input_schema": object_schema(
                {"setting": {"type": "string", "enum": settings}, "value": {"type": "string"}}
            ),
        },
        {
            "name": "submit_report",
            "description": (
                "Hand in the report for the technician. This ends your work on the ticket unless "
                "the review sends it back."
            ),
            "strict": True,
            "input_schema": object_schema(
                {
                    "summary": {"type": "string"},
                    "items": {
                        "type": "array",
                        "items": object_schema(
                            {
                                "setting": {"type": "string", "enum": settings},
                                "outcome": {"type": "string", "enum": list(OUTCOMES)},
                                "statements": STATEMENTS,
                            }
                        ),
                    },
                    "open_requests": {"type": "array", "items": {"type": "string"}},
                    "note_to_user": {"type": "string"},
                    "next_step_for_tech": {"type": "string"},
                }
            ),
        },
    ]


def parse_value(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


@dataclass
class Toolbox:
    """What Claude's tools can reach for one ticket: this ticket's laptop and attachment only."""

    client: SIEClient
    calls: Calls
    fleet: Fleet
    sections: list[dict[str, str]]
    rules: dict[str, Any]
    ticket: Ticket
    data_dir: Path = DATA_DIR
    masked_counts: dict[str, int] = field(default_factory=dict)
    outputs: dict[str, list[str]] = field(default_factory=dict)  # tool -> what it returned
    shields: list[ShieldResult] = field(default_factory=list)
    # The guard's latest decision per setting: applied, waiting_for_user, owner_must_do or
    # refused. A later request for the same setting replaces an earlier decision.
    decisions: dict[str, str] = field(default_factory=dict)
    screenshot_read: bool = False  # read_screenshot returned a result
    failed_calls: int = 0
    ticket_text: str = ""  # the masked ticket, which figures may come from
    failing_at_start: dict[str, str] = field(default_factory=dict)  # setting -> policy section

    @property
    def today(self) -> date:
        return date.fromisoformat(self.ticket.received)

    def failing(self) -> dict[str, str]:
        return self.fleet.failing(self.ticket.device_id, self.rules["checks"], self.today)

    def decided(self, outcome: str) -> list[str]:
        """The settings whose latest guard decision is `outcome`, in order."""
        return [setting for setting, latest in self.decisions.items() if latest == outcome]

    def open_reasons(self) -> list[str]:
        """Why Python keeps this ticket open, whatever Claude's report says."""
        reasons = []
        if self.failed_calls:
            reasons.append(
                f"{self.failed_calls} lookup(s) failed, so the report may miss something"
            )
        if self.ticket.attachment and not self.screenshot_read:
            reasons.append("the attached screenshot was never read")
        return reasons

    def run(self, name: str, tool_input: dict[str, Any]) -> str:
        """Run one lookup and return its result as JSON. Raises ToolError for Claude to read."""
        tools: dict[str, Callable[..., dict[str, Any]]] = {
            "get_device": self.get_device,
            "search_policy": self.search_policy,
            "read_screenshot": self.read_screenshot,
            "apply_fix": self.apply_fix,
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

    def get_device(self) -> dict[str, Any]:
        """The record, and Python's own reading of which policy checks it fails now."""
        return {**self.fleet.record(self.ticket.device_id), "fails_policy_checks": self.failing()}

    def search_policy(self, question: str) -> dict[str, Any]:
        model = self.rules["models"]["rerank"]
        by_id = {section["section_id"]: section for section in self.sections}
        scored = self.calls.call(
            model,
            lambda: self.client.score(
                model,
                Item(id="question", text=question),
                [
                    Item(id=s["section_id"], text=f"{s['heading']}\n{s['text']}")
                    for s in self.sections
                ],
                instruction=self.rules["search"]["instruction"],
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
                for entry in ranked[: self.rules["search"]["top_k"]]
            ]
        }

    def read_screenshot(self) -> dict[str, Any]:
        if not self.ticket.attachment:
            raise ToolError("this ticket has no screenshot attached")
        # Only the file's name, so an attachment field can't point outside the data folder.
        path = self.data_dir / Path(self.ticket.attachment).name
        try:
            image = path.read_bytes()
        except OSError as error:
            raise ToolError("the attached screenshot couldn't be opened") from error
        model = self.rules["models"]["ocr"]
        key = f"{self.ticket.ticket_id}/screenshot"
        reply = self.calls.call(
            model,
            lambda: self.client.extract(
                model, Item(id=key, images=[image]), provision_timeout_s=WAIT_S
            ),
            lambda r: (
                isinstance(r, dict)
                and isinstance(r.get("entities"), list)
                and any(isinstance(e, dict) and e.get("label") == "markdown" for e in r["entities"])
            ),
            "any text",
        )
        text = next(
            str(entity.get("text", ""))
            for entity in reply["entities"]
            if isinstance(entity, dict) and entity.get("label") == "markdown"
        )
        masked, counts = mask_personal_data(self.calls, self.client, key, text, self.rules)
        for kind, count in counts.items():
            self.masked_counts[kind] = self.masked_counts.get(kind, 0) + count
        shield = run_shield(self.calls, self.client, key, "screenshot", masked, self.rules)
        self.shields.append(shield)
        self.screenshot_read = True
        warning = shield.describe(self.rules) if shield.warned(self.rules) else ""
        return {"text": masked, "shield_warning": warning}

    def apply_fix(self, setting: str, value: str) -> dict[str, Any]:
        """The guard, then the change, then the record read again. Nothing else changes a laptop.
        Raises ToolError for a value the check can't read, or one that isn't the policy's value."""
        guard, checks = self.rules["guard"], self.rules["checks"]
        if setting not in checks:
            raise ToolError(f"{setting} isn't a setting the policy checks")
        if setting in guard["owner_only"]:
            self.decisions[setting] = "owner_must_do"
            return {"result": "owner_must_do", "why": f"only the user can change {setting}"}
        check, wanted = checks[setting], checks[setting]["value"]
        asked = parse_value(value)
        try:
            weaker = type(asked) is type(wanted) and not meets(asked, check, self.today)
        except (TypeError, ValueError):
            weaker = False
        if weaker:
            self.decisions[setting] = "refused"
            return {
                "result": "refused",
                "why": f"{setting} {json.dumps(asked)} doesn't meet the policy; that needs an "
                "exception from IT Security",
            }
        if asked != wanted:
            raise ToolError(f"expected {json.dumps(wanted)} for {setting}")
        if setting not in self.failing():
            # A stricter value already in place stays: a 60 s screen lock isn't loosened to 300 s.
            return {"result": "not_needed", "why": f"{setting} already meets the policy"}
        if setting in guard["needs_yes"]:
            self.decisions[setting] = "waiting_for_user"
            return {
                "result": "waiting_for_user",
                "why": f"{setting} needs the user's agreement first",
            }
        if setting not in guard["safe"]:
            self.decisions[setting] = "refused"
            return {"result": "refused", "why": f"the guard doesn't allow changes to {setting}"}
        self.fleet.set(self.ticket.device_id, setting, wanted)
        self.decisions[setting] = "applied"
        now = self.fleet.record(self.ticket.device_id)[setting]
        meets_now = setting not in self.failing()
        return {"result": "applied", "record_now": {setting: now}, "meets_policy_now": meets_now}


# --- The agent loop --------------------------------------------------------------------------


@dataclass
class AgentResult:
    report: dict[str, Any] | None = None
    ran_out: bool = False  # hit the limit on lookups or requests
    stopped: str = ""  # why there's no report, when there isn't one
    tool_calls: int = 0  # lookups and submissions
    lookups: int = 0
    revisions: int = 0
    first_review: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    transcript: list[dict[str, Any]] = field(default_factory=list)


def make_claude() -> anthropic.Anthropic:
    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Stopped: set ANTHROPIC_API_KEY, in .env or the environment.")
    return anthropic.Anthropic()


def ask_claude(claude: anthropic.Anthropic, messages: list[Any], rules: dict[str, Any]) -> Any:
    settings = rules["agent"]
    extra: dict[str, Any] = {}
    if settings["fallbacks"]:
        extra = {"betas": [settings["fallback_beta"]], "fallbacks": settings["fallbacks"]}
    return claude.beta.messages.create(
        model=rules["models"]["agent"],
        max_tokens=settings["max_tokens"],
        system=settings["system"],
        tools=cast(Any, tool_list(rules)),
        messages=messages,
        output_config={"effort": settings["effort"]},
        cache_control={"type": "ephemeral"},
        **extra,
    )


def ticket_prompt(
    ticket: Ticket, masked_text: str, shield: ShieldResult, rules: dict[str, Any]
) -> str:
    screenshot = "one screenshot" if ticket.attachment else "none"
    warning = shield.describe(rules) if shield.warned(rules) else "none"
    return (
        f"Ticket {ticket.ticket_id}, received {ticket.received}, about laptop {ticket.device_id}. "
        f"Attachment: {screenshot}. Shield warning on the ticket text: {warning}.\n"
        # The user writes this text, so it can't be allowed to close the tag itself.
        f"<ticket_text>\n{masked_text.replace('</ticket_text>', '')}\n</ticket_text>"
    )


def text_of(content: list[Any]) -> str:
    return "\n".join(block.text for block in content if block.type == "text")


def one_lookup(box: Toolbox, use: Any, result: AgentResult, max_lookups: int) -> dict[str, Any]:
    """Run one tool call, or refuse it past the limit, and shape the result for Claude."""
    entry: dict[str, Any] = {"tool": use.name, "input": use.input}
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": use.id}
    if result.lookups >= max_lookups:
        result.ran_out = True
        output = "No lookups left. Call submit_report now with what you have."
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
    """Check a submitted report. True ends the loop; False sends what was found back to Claude."""
    result.tool_calls += 1
    report = dict(use.input)
    problems = check_report(report, box)
    result.transcript.append({"tool": "submit_report", "input": use.input, "problems": problems})
    if problems and result.revisions < box.rules["agent"]["max_revisions"]:
        result.revisions += 1
        result.first_review = result.first_review or problems
        found = "\n".join(f"- {problem}" for problem in problems)
        tool_results.append(
            {
                "type": "tool_result",
                "tool_use_id": use.id,
                "is_error": True,
                "content": "The review found these problems. Fix them and call submit_report "
                "again.\n" + found,
            }
        )
        return False
    result.report, result.problems = report, problems
    return True


def run_agent(claude: anthropic.Anthropic, box: Toolbox, prompt: str) -> AgentResult:
    """Let Claude check and fix things until it submits a report or reaches a limit."""
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
        result.cached_tokens += response.usage.cache_read_input_tokens or 0
        result.cached_tokens += response.usage.cache_creation_input_tokens or 0

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
                result.stopped = "Claude ended its turn without calling submit_report"
                return result
            asked_to_submit = True
            result.transcript.append({"said": text_of(response.content)})
            nudge = "Call submit_report now, even if some things are still open."
            messages.append({"role": "user", "content": nudge})
            continue

        tool_results: list[dict[str, Any]] = []
        accepted = False
        # Lookups first, so a report sent in the same response is checked against their results.
        for use in sorted(uses, key=lambda use: use.name == "submit_report"):
            if use.name != "submit_report":
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


# --- Done-check: the report against the device record ----------------------------------------


def plain(text: str) -> str:
    """Text for comparing a quote: no markdown marks, one kind of quote and dash, single spaces."""
    text = re.sub(r"[`*\\]", "", text).translate(SAME_CHARACTER)
    return re.sub(r"\s+", " ", text).strip().casefold()


def figures(text: str, min_digits: int) -> set[str]:
    """Decimals such as 26.7, and whole numbers with at least `min_digits` digits."""
    joined = re.sub(r"(?<=\d),(?=\d{3})", "", text)
    pattern = rf"\d+\.\d+|(?<![\d.])\d{{{min_digits},}}(?![\d]|\.\d)"
    return {re.sub(r"\.0+$", "", figure) for figure in re.findall(pattern, joined)}


def readable(output: str) -> str:
    """A tool's JSON result as its plain values, so a line break in screenshot text is one."""

    def values(value: Any) -> list[str]:
        if isinstance(value, dict):
            return [f"{key}: {text}" for key, item in value.items() for text in values(item)] + [
                text for item in value.values() for text in values(item)
            ]
        if isinstance(value, list):
            return [text for item in value for text in values(item)]
        return [json.dumps(value) if isinstance(value, bool) else str(value)]

    return "\n".join(values(json.loads(output)))


def waiting_kind(setting: str, rules: dict[str, Any]) -> str:
    """Whom a failing setting waits for under the guard's list, or "" if the helpdesk may fix it."""
    if setting in rules["guard"]["owner_only"]:
        return "owner_must_do"
    if setting in rules["guard"]["needs_yes"]:
        return "waiting_for_user"
    return ""


def held_now(box: Toolbox) -> dict[str, str]:
    """Every setting failing now that the guard's list holds for the user or the owner, whether or
    not Claude asked to change it."""
    return {
        setting: kind
        for setting in box.failing()
        if (kind := waiting_kind(setting, box.rules)) and box.decisions.get(setting) != "refused"
    }


def outcome_of(setting: str, box: Toolbox) -> str:
    """What happened to one setting, from the guard's log, its list and the record. Claude must
    agree."""
    failing_now = box.failing()
    latest = box.decisions.get(setting)
    if latest == "refused":
        return "refused"
    if latest == "applied":
        return "did_not_stick" if setting in failing_now else "fixed"
    if setting in failing_now and (kind := waiting_kind(setting, box.rules)):
        return kind
    return "not_changed"


def change_claims(text: str, box: Toolbox) -> list[tuple[str, str]]:
    """(setting, sentence) for each sentence saying a setting was changed that the record doesn't
    show as fixed. A sentence with a negation ("we haven't changed anything") doesn't count."""
    settings = box.rules["checks_on_report"]
    found = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        flat = plain(sentence)
        if not re.search(settings["change_words"], flat) or re.search(settings["negation"], flat):
            continue
        for setting, words in settings["setting_words"].items():
            if re.search(words, flat) and outcome_of(setting, box) != "fixed":
                found.append((setting, sentence.strip()))
    return found


def check_report(report: dict[str, Any], box: Toolbox) -> list[str]:
    """Every problem in a report. An empty list means every source and claim checked out."""
    digits = box.rules["checks_on_report"]["figure_min_digits"]
    sections = {section["section_id"]: section for section in box.sections}
    found = "\n".join(readable(output) for outputs in box.outputs.values() for output in outputs)
    ticket = box.ticket
    known = f"{ticket.ticket_id} {ticket.device_id} {ticket.received}\n{box.ticket_text}"
    allowed = figures(f"{known}\n{found}", digits)
    problems: list[str] = []
    listed = [item["setting"] for item in report["items"]]
    for setting in box.failing_at_start:
        if setting not in listed:
            problems.append(
                f"the laptop failed {setting} at the start, and the report leaves it out"
            )
    for item in report["items"]:
        actual = outcome_of(item["setting"], box)
        if item["outcome"] != actual:
            problems.append(
                f"the report says {item['setting']} is {item['outcome']}, but the guard's log and "
                f"the device record say {actual}"
            )
        for statement in item["statements"]:
            source, quote = statement["source"], statement["quote"]
            if source in sections:
                whole = f"{sections[source]['heading']}\n{sections[source]['text']}"
            elif source in LOOKUP_TOOLS:
                whole = "\n".join(f"{o}\n{readable(o)}" for o in box.outputs.get(source, []))
            else:
                problems.append(f"cites a source that doesn't exist: {source}")
                continue
            if not plain(quote) or plain(quote) not in plain(whole):
                problems.append(f"the quote isn't word for word in {source}: {quote!r}")
            for figure in sorted(figures(statement["claim"], digits) - allowed):
                problems.append(f"{figure} in a claim didn't come from any lookup or the ticket")
    for setting, sentence in change_claims(f"{report['summary']}\n{report['note_to_user']}", box):
        problems.append(
            f"the summary or note says {setting} was changed ({sentence!r}), but the guard's log "
            f"and the device record say {outcome_of(setting, box)}"
        )
    if not report["note_to_user"].strip():
        problems.append("the note to the user is empty")
    for figure in sorted(figures(report["note_to_user"], digits) - allowed):
        problems.append(
            f"{figure} in the note to the user didn't come from any lookup or the ticket"
        )
    return problems


def status_of(result: AgentResult, problems: list[str], box: Toolbox) -> str:
    """The status, from what Python found on the device and in the guard's log, plus the requests
    Claude says it didn't carry out."""
    if result.ran_out:
        return "out_of_steps"
    if result.report is None:
        return "no_report"
    if problems:
        return "check_failed"
    failing_now = set(box.failing())
    open_requests = bool(result.report["open_requests"])
    refused = box.decided("refused")
    if refused or open_requests or box.open_reasons() or failing_now - set(held_now(box)):
        return "needs_tech"
    return "waiting_on_person" if failing_now else "compliant_now"


# --- Output ----------------------------------------------------------------------------------


def csv_safe(value: Any) -> str:
    """A leading ' on a cell a spreadsheet would run as a formula; anything else as written."""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def report_markdown(ticket: Ticket, status: str, result: AgentResult, box: Toolbox) -> str:
    lines = [
        f"# {ticket.ticket_id}: {ticket.subject}",
        "",
        f"**Laptop:** {ticket.device_id}",
        "",
        f"**Status:** `{status}`",
        "",
    ]
    if result.stopped:
        lines += [f"**Stopped because:** {result.stopped}", ""]
    warnings = [shield.describe(box.rules) for shield in box.shields if shield.warned(box.rules)]
    if warnings:
        lines += ["**Shield warning, read the ticket yourself:**", ""]
        lines += [f"- {warning}" for warning in warnings] + [""]
    if reasons := box.open_reasons():
        lines += ["**Kept open by Python:**", ""]
        lines += [f"- {reason}" for reason in reasons] + [""]
    if result.problems:
        lines += ["**Check these before replying:**", ""]
        lines += [f"- {problem}" for problem in result.problems] + [""]
    if result.first_review:
        lines += ["**The review sent the first report back for these, and Claude revised it:**", ""]
        lines += [f"- {problem}" for problem in result.first_review] + [""]
    if box.masked_counts:
        removed = ", ".join(f"{count} {kind}" for kind, count in sorted(box.masked_counts.items()))
        lines += [f"**Personal data masked before Claude saw it:** {removed}", ""]
    failing_now = box.failing()
    lines += ["## What the device record shows", ""]
    changed = set(box.failing_at_start) | set(box.decided("applied")) | set(box.decided("refused"))
    for setting in sorted(changed):
        start = "failed" if setting in box.failing_at_start else "passed"
        now = "fails" if setting in failing_now else "passes"
        lines.append(f"- `{setting}`: {start} at the start, {now} now ({outcome_of(setting, box)})")
    if not changed:
        lines.append("- Nothing failed at the start, and nothing was changed.")
    lines.append("")
    report = result.report
    if report:
        lines += [f"**Summary:** {report['summary']}", ""]
        if report["next_step_for_tech"]:
            lines += [f"**Next step for the technician:** {report['next_step_for_tech']}", ""]
        if report["open_requests"]:
            lines += ["**Requests in the ticket that weren't carried out:**", ""]
            lines += [f"- {request}" for request in report["open_requests"]] + [""]
        lines += ["## Note to the user", "", report["note_to_user"], "", "## Sources", ""]
        for item in report["items"]:
            lines += [f"**{item['setting']}** ({item['outcome']})", ""]
            for statement in item["statements"]:
                lines.append(
                    f'- {statement["claim"]} `{statement["source"]}`: "{statement["quote"]}"'
                )
            lines.append("")
    lines += ["## Every tool call", ""]
    for entry in result.transcript:
        if "tool" in entry and entry["tool"] != "submit_report":
            failed = " (failed)" if entry["is_error"] else ""
            lines.append(f"- `{entry['tool']}` {json.dumps(entry['input'])}{failed}")
    return "\n".join(lines) + "\n"


def write_outputs(output: Path, rows: list[dict[str, Any]], fleet: Fleet) -> None:
    (output / "reports").mkdir(parents=True, exist_ok=True)
    (output / "transcripts").mkdir(exist_ok=True)
    with (output / "reports.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=REPORT_ROW)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_safe(row[key]) for key in REPORT_ROW})
    for row in rows:
        ticket_id = row["ticket_id"]
        (output / "reports" / f"{ticket_id}.md").write_text(row["markdown"], encoding="utf-8")
        (output / "transcripts" / f"{ticket_id}.json").write_text(
            json.dumps(row["transcript"], indent=1, default=str), encoding="utf-8"
        )
    after = [
        {key: value for key, value in record.items() if key not in OWNER_FIELDS}
        for record in fleet.devices.values()
    ]
    (output / "devices_after.json").write_text(json.dumps(after, indent=1), encoding="utf-8")


# --- The run ---------------------------------------------------------------------------------


def answer_ticket(claude: anthropic.Anthropic, box: Toolbox) -> dict[str, Any]:
    calls, ticket = box.calls, box.ticket
    before = calls.total
    started = time.monotonic()
    box.failing_at_start = box.failing()
    try:
        masked_text, counts = mask_personal_data(
            calls,
            box.client,
            f"{ticket.ticket_id}/text",
            f"Subject: {ticket.subject}\n{ticket.body}",
            box.rules,
        )
    except CallFailed as error:
        # Fail closed: unmasked text from the user doesn't go to a third-party API.
        result = AgentResult(stopped=f"personal data couldn't be masked: {error}")
    else:
        box.masked_counts.update(counts)
        box.ticket_text = masked_text
        shield = run_shield(calls, box.client, ticket.ticket_id, "ticket", masked_text, box.rules)
        box.shields.append(shield)
        result = run_agent(claude, box, ticket_prompt(ticket, masked_text, shield, box.rules))
    status = status_of(result, result.problems, box)
    scores = [s.model_score for s in box.shields if s.model_score is not None]
    return {
        "ticket_id": ticket.ticket_id,
        "device_id": ticket.device_id,
        "status": status,
        "failing_at_start": " ".join(sorted(box.failing_at_start)),
        "failing_now": " ".join(sorted(box.failing())),
        "fixed": " ".join(s for s in box.decided("applied") if s not in box.failing()),
        "held": " ".join(sorted(held_now(box))),
        "refused": " ".join(box.decided("refused")),
        "shield_phrases": " ".join(sorted({p for s in box.shields for p in s.phrases})),
        "shield_model_score": f"{max(scores):.2f}" if scores else "",
        "tool_calls": result.tool_calls,
        "revisions": result.revisions,
        "sie_calls": calls.total - before,
        "seconds": round(time.monotonic() - started),
        "input_tokens": result.input_tokens + result.cached_tokens,
        "output_tokens": result.output_tokens,
        "markdown": report_markdown(ticket, status, result, box),
        "transcript": result.transcript,
    }


def run(args: argparse.Namespace) -> None:
    rules = load_rules(args.rules)
    client = make_client()
    claude = make_claude()
    calls = Calls()
    fleet = Fleet.load(DATA_DIR / "devices.json")
    sections = split_sections(POLICY.read_text(encoding="utf-8"))
    tickets = load_tickets(DATA_DIR / "tickets.csv")
    if args.ticket:
        tickets = [t for t in tickets if t.ticket_id == args.ticket]
        if not tickets:
            raise SystemExit(f"Stopped: no ticket {args.ticket} in data/tickets.csv")
    rows: list[dict[str, Any]] = []
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    try:
        for ticket in tickets:
            if ticket.device_id not in fleet.devices:
                raise SystemExit(
                    f"Stopped: {ticket.ticket_id} names unknown laptop {ticket.device_id}"
                )
            box = Toolbox(client, calls, fleet, sections, rules, ticket)
            row = answer_ticket(claude, box)
            print(
                f"{row['ticket_id']}: {row['status']} ({row['tool_calls']} tool calls, "
                f"{row['revisions']} revisions, {row['seconds']} s)"
            )
            rows.append(row)
    finally:
        # A run that stops partway keeps the reports it already paid for.
        if rows:
            write_outputs(output, rows, fleet)
    if calls.failures:
        print(
            f"{len(calls.failures)} of {calls.total} SIE calls failed. First: {calls.failures[0]}"
        )
    in_tokens = sum(row["input_tokens"] for row in rows)
    out_tokens = sum(row["output_tokens"] for row in rows)
    print(f"{calls.total} SIE calls, {in_tokens} Claude input and {out_tokens} output tokens.")
    print(f"Wrote {output}")
    if all(row["status"] == "no_report" for row in rows):
        raise SystemExit("Stopped: no ticket got a report. See the reports for the reason.")


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--ticket", help="run one ticket, such as T-3001")
    parser.add_argument("--output", type=Path, help="folder for the outputs")
    parser.add_argument("--rules", type=Path, default=RULES_PATH, help="another rules.yaml to try")
    args = parser.parse_args()
    try:
        run(args)
    except SIEStopped as error:
        raise SystemExit(f"Stopped: {error} Any finished reports are in runs/.") from None
    except (
        anthropic.AuthenticationError,
        anthropic.PermissionDeniedError,
        anthropic.NotFoundError,
        anthropic.BadRequestError,
    ) as error:
        raise SystemExit(
            f"Stopped: the Claude API refused the request, {type(error).__name__}: {error}. Check "
            "ANTHROPIC_API_KEY and the model name in rules.yaml. Any finished reports are in runs/."
        ) from None


if __name__ == "__main__":
    main()
