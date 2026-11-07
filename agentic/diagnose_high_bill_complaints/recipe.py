"""Work out why a customer's water bill jumped and draft the reply, with a source for every line.

Who:       A billing rep at a water utility. They check every draft and reply, and a supervisor
           approves any credit. Nothing goes to the customer or the account from here.
Input:     data/complaints.csv, a photo of the meter where the customer sent one, and the index
           ingest.py builds from data/policy_manual.md. billing_system.py stands in for the billing
           and meter data systems, using the CSV files in data/.
Agent:     Claude runs the loop and decides what to check next. Its tools are:
           search_policies   Qwen/Qwen3-Embedding-4B, then Qwen/Qwen3-Reranker-4B, on SIE
           read_meter_photo  lightonai/LightOnOCR-3-4B, then superlinked/pii-redact, on SIE
           get_account       plain Python
           get_readings      plain Python
           calculate_bill    plain Python
           submit_draft      plain Python, ends the loop
Fixed:     superlinked/pii-redact on SIE masks personal data before Claude sees the complaint.
Review:    When Claude submits, Python checks every quote and figure, works out any credit again and
           looks for sentences claiming an action nobody took. What it finds goes back to Claude
           once to fix. Python then sets the status.
Output:    runs/<timestamp>/: drafts.csv, drafts/<complaint>.md and transcripts/<complaint>.json
           with every tool call. A status says what the checks found: draft_ready, partly_answered,
           not_in_policy, check_failed, out_of_steps or no_draft.
Run using: SIE Serverless, or SIE Cluster or SIE Local with SIE_BASE_URL, plus ANTHROPIC_API_KEY.

    uv run recipe.py
    uv run recipe.py --complaint C-2001
    uv run recipe.py --rules my-rules.yaml
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import anthropic
from billing_system import BillingSystem, charge_for
from dotenv import load_dotenv
from ingest import HERE, RULES_PATH, build_index, fingerprint, load_rules, make_client
from sie_calls import WAIT_S, CallFailed, Calls, SIEStopped
from sie_sdk import SIEClient
from sie_sdk.types import Item
from vector_index import VectorIndex

DATA_DIR = HERE / "data"
RUNS_DIR = HERE / "runs"
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
SAME_CHARACTER = {0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"', 0x2013: "-"}
LOOKUP_TOOLS = ("get_account", "get_readings", "read_meter_photo", "calculate_bill")

DRAFT_ROW = [
    "complaint_id",
    "account_id",
    "status",
    "tool_calls",
    "searches",
    "revisions",
    "proposed_credit",
    "sie_calls",
    "seconds",
    "input_tokens",
    "output_tokens",
]


class ToolError(Exception):
    """A lookup that failed, sent back to Claude as an error it can react to."""


# --- Complaints ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Complaint:
    complaint_id: str
    account_id: str
    received: str
    subject: str
    body: str
    attachment: str


def load_complaints(path: Path) -> list[Complaint]:
    with path.open(encoding="utf-8", newline="") as file:
        return [Complaint(**row) for row in csv.DictReader(file)]


# --- Masking personal data -------------------------------------------------------------------


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
    call fails, so unmasked customer text never reaches Claude."""
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

        reply = calls.call(
            model,
            work,
            lambda r: isinstance(r, dict) and isinstance(r.get("entities"), list),
            "'entities'",
        )
        for entity in reply["entities"]:
            if valid_entity(window, entity):
                kind = kinds.get(entity["label"], entity["label"])
                spans.append((offset + entity["start"], offset + entity["end"], kind))
    keep = [m.span() for pattern in settings["keep"] for m in re.finditer(pattern, text)]
    spans = [s for s in spans if not any(s[0] < end and start < s[1] for start, end in keep)]
    return replace_spans(text, spans)


# --- The tools Claude can call ---------------------------------------------------------------


def object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


PERIODS = {
    "type": "array",
    "items": object_schema(
        {
            "period_start": {"type": "string", "description": "YYYY-MM-DD, first of the month"},
            "gallons": {"type": "integer"},
        }
    ),
}
TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_account",
        "description": (
            "This complaint's account: meter serial and type, service status, each recent bill "
            "(actual or estimated, gallons, end read, amount) and past adjustments."
        ),
        "strict": True,
        "input_schema": object_schema({}),
    },
    {
        "name": "get_readings",
        "description": (
            "Daily use and lowest overnight flow for this account's automated meter, for a date "
            "range. Dates are YYYY-MM-DD. A range is limited to about two months."
        ),
        "strict": True,
        "input_schema": object_schema(
            {"start_date": {"type": "string"}, "end_date": {"type": "string"}}
        ),
    },
    {
        "name": "read_meter_photo",
        "description": (
            "Read the customer's photo attached to this complaint. Returns the text on it, and "
            "whether the serial number on it matches this account's meter."
        ),
        "strict": True,
        "input_schema": object_schema({}),
    },
    {
        "name": "search_policies",
        "description": (
            "Ask the utility's rules manual one question at a time. Returns the closest sections "
            "with a relevance score from 0 to 1. Write the question in your own words."
        ),
        "strict": True,
        "input_schema": object_schema({"question": {"type": "string"}}),
    },
    {
        "name": "calculate_bill",
        "description": (
            "Work out what one or more billing periods should cost for a given volume, at the "
            "rates in force on each period's first day. Pass all the periods in one call."
        ),
        "strict": True,
        "input_schema": object_schema({"periods": PERIODS}),
    },
    {
        "name": "submit_draft",
        "description": (
            "Hand in the draft reply. This ends your work on the complaint. List each question in "
            "the complaint, whether your lookups answer it, and the statements behind it."
        ),
        "strict": True,
        "input_schema": object_schema(
            {
                "reply": {"type": "string"},
                "likely_cause": {"type": "string"},
                "questions": {
                    "type": "array",
                    "items": object_schema(
                        {
                            "question": {"type": "string"},
                            "answered": {"type": "boolean"},
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
                "questions_for_customer": {
                    "type": "array",
                    "items": object_schema(
                        {
                            "question": {"type": "string"},
                            "needed_before_anyone_can_act": {"type": "boolean"},
                        }
                    ),
                },
                "next_step_for_rep": {
                    "type": "string",
                    "description": "What the rep has to do or find out. Empty when nothing is.",
                },
                "proposed_credit": {
                    "anyOf": [
                        object_schema({"amount": {"type": "number"}, "recharge_periods": PERIODS}),
                        {"type": "null"},
                    ]
                },
            }
        ),
    },
]


def parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ToolError(f"{value!r} isn't a YYYY-MM-DD date") from error


@dataclass
class Toolbox:
    """What Claude's tools can reach for one complaint: it can't name another account or file."""

    client: SIEClient
    calls: Calls
    index: VectorIndex
    system: BillingSystem
    rules: dict[str, Any]
    complaint: Complaint
    data_dir: Path = DATA_DIR
    masked_counts: dict[str, int] = field(default_factory=dict)
    outputs: dict[str, list[str]] = field(default_factory=dict)  # tool -> what it returned
    serial_matches: bool | None = None  # None until the photo is read
    complaint_text: str = ""  # the masked complaint, which figures may come from

    def run(self, name: str, tool_input: dict[str, Any]) -> str:
        """Run one lookup and return its result as JSON. Raises ToolError for Claude to read."""
        tools: dict[str, Callable[..., dict[str, Any]]] = {
            "get_account": self.get_account,
            "get_readings": self.get_readings,
            "read_meter_photo": self.read_meter_photo,
            "search_policies": self.search_policies,
            "calculate_bill": self.calculate_bill,
        }
        if name not in tools:
            raise ToolError(f"there is no tool called {name}")
        try:
            output = json.dumps(tools[name](**tool_input))
        except CallFailed as error:
            raise ToolError(str(error)) from error
        except (KeyError, TypeError) as error:
            raise ToolError(f"bad input for {name}: {error}") from error
        self.outputs.setdefault(name, []).append(output)
        return output

    def get_account(self) -> dict[str, Any]:
        today = date.fromisoformat(self.complaint.received)
        return self.system.account(self.complaint.account_id, today)

    def get_readings(self, start_date: str, end_date: str) -> dict[str, Any]:
        start, end = parse_day(start_date), parse_day(end_date)
        limit = self.rules["readings"]["max_days"]
        if end < start or end - start > timedelta(days=limit):
            raise ToolError(f"ask for a range of 1 to {limit} days")
        overnight = self.rules["readings"]["overnight_hours"]
        return self.system.daily_use(self.complaint.account_id, start, end, overnight)

    def calculate_bill(self, periods: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            return {
                "periods": [
                    charge_for(period["gallons"], parse_day(period["period_start"]))
                    for period in periods
                ]
            }
        except ValueError as error:
            raise ToolError(str(error)) from error

    def search_policies(self, question: str) -> dict[str, Any]:
        settings = self.rules["search"]
        model = self.rules["models"]["embed"]
        reply = self.calls.call(
            model,
            lambda: self.client.encode(
                model,
                [Item(id="question", text=question)],
                is_query=True,
                instruction=settings["query_instruction"],
                provision_timeout_s=WAIT_S,
            ),
            lambda r: (
                isinstance(r, list) and len(r) == 1 and isinstance(r[0], dict) and "dense" in r[0]
            ),
            "a 'dense' vector",
        )
        candidates = self.index.nearest(reply[0]["dense"], settings["embed_top_k"])
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

    def read_meter_photo(self) -> dict[str, Any]:
        if not self.complaint.attachment:
            raise ToolError("this complaint has no photo attached")
        model = self.rules["models"]["ocr"]
        image = (self.data_dir / self.complaint.attachment).read_bytes()
        key = f"{self.complaint.complaint_id}/photo"
        reply = self.calls.call(
            model,
            lambda: self.client.extract(
                model, Item(id=key, images=[image]), provision_timeout_s=WAIT_S
            ),
            lambda r: isinstance(r, dict) and isinstance(r.get("entities"), list),
            "'entities'",
        )
        text = next(
            (
                str(entity.get("text", ""))
                for entity in reply["entities"]
                if isinstance(entity, dict) and entity.get("label") == "markdown"
            ),
            "",
        )
        masked, counts = mask_personal_data(self.calls, self.client, key, text, self.rules)
        for kind, count in counts.items():
            self.masked_counts[kind] = self.masked_counts.get(kind, 0) + count
        expected = self.system.accounts[self.complaint.account_id]["meter_serial"]
        found = re.findall(self.rules["photo"]["serial_pattern"], text)
        self.serial_matches = expected in found
        return {
            "text": masked,
            "serial_numbers_found": found,
            "serial_matches_account": self.serial_matches,
        }


# --- The agent loop --------------------------------------------------------------------------


@dataclass
class AgentResult:
    draft: dict[str, Any] | None = None
    ran_out: bool = False  # hit the limit on tool calls or requests
    stopped: str = ""  # why there's no draft, when there isn't one
    tool_calls: int = 0  # lookups and submissions
    lookups: int = 0
    revisions: int = 0  # times the draft went back to Claude
    first_review: list[str] = field(default_factory=list)  # what the first review found
    problems: list[str] = field(default_factory=list)  # what the last review found
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0  # read from or written to the prompt cache
    transcript: list[dict[str, Any]] = field(default_factory=list)


def make_claude() -> anthropic.Anthropic:
    load_dotenv()
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
        tools=cast(Any, TOOLS),
        messages=messages,
        output_config={"effort": settings["effort"]},
        cache_control={"type": "ephemeral"},
        **extra,
    )


def complaint_prompt(complaint: Complaint, masked_body: str) -> str:
    photo = "one photo of a meter" if complaint.attachment else "none"
    return (
        f"Complaint {complaint.complaint_id}, received {complaint.received}, about account "
        f"{complaint.account_id}. Attachment: {photo}.\n"
        f"<complaint_text>\nSubject: {complaint.subject}\n{masked_body}\n</complaint_text>"
    )


def text_of(content: list[Any]) -> str:
    return "\n".join(block.text for block in content if block.type == "text")


def one_lookup(box: Toolbox, use: Any, result: AgentResult, max_lookups: int) -> dict[str, Any]:
    """Run one tool call, or refuse it past the limit, and shape the result for Claude."""
    entry: dict[str, Any] = {"tool": use.name, "input": use.input}
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": use.id}
    if result.lookups >= max_lookups:
        result.ran_out = True
        output = "No lookups left. Call submit_draft now with what you have."
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
    """Check a submitted draft. True ends the loop; False sends what was found back to Claude."""
    result.tool_calls += 1
    draft = dict(use.input)
    problems = check_draft(draft, box, box.complaint_text)
    result.transcript.append({"tool": "submit_draft", "input": use.input, "problems": problems})
    if problems and result.revisions < box.rules["agent"]["max_revisions"]:
        result.revisions += 1
        result.first_review = problems
        found = "\n".join(f"- {problem}" for problem in problems)
        tool_results.append(
            {
                "type": "tool_result",
                "tool_use_id": use.id,
                "is_error": True,
                "content": (
                    "The review found these problems. Fix them and call submit_draft again.\n"
                    + found
                ),
            }
        )
        return False
    result.draft, result.problems = draft, problems
    return True


def run_agent(claude: anthropic.Anthropic, box: Toolbox, prompt: str) -> AgentResult:
    """Let Claude check things until it submits a draft or reaches a limit."""
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
                result.stopped = "Claude ended its turn without calling submit_draft"
                return result
            asked_to_submit = True
            result.transcript.append({"said": text_of(response.content)})
            nudge = "Call submit_draft now, even if some questions are unanswered."
            messages.append({"role": "user", "content": nudge})
            continue

        tool_results: list[dict[str, Any]] = []
        accepted = False
        for use in uses:
            if use.name == "submit_draft":
                accepted = one_submission(box, use, result, tool_results)
            else:
                tool_results.append(one_lookup(box, use, result, settings["max_lookups"]))
        if accepted:
            return result
        messages.append({"role": "user", "content": tool_results})

    result.ran_out = True
    result.stopped = "reached the limit on requests to Claude"
    return result


# --- Checks on the draft ---------------------------------------------------------------------


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


def action_claims(reply: str, patterns: list[str]) -> list[str]:
    """Where a reply claims or promises an action. The recipe only drafts, so none has happened."""
    text = plain(reply)
    return [match.group(0) for pattern in patterns for match in re.finditer(pattern, text)]


def worked_out_credit(draft: dict[str, Any], box: Toolbox) -> tuple[float | None, list[str]]:
    """The credit Python gets for the draft's recharged periods: billed minus recharged."""
    proposal = draft.get("proposed_credit")
    if not proposal:
        return None, []
    account = box.complaint.account_id
    billed = recharged = 0.0
    if not proposal["recharge_periods"]:
        return None, ["the proposed credit lists no periods to recharge, so it can't be checked"]
    for period in proposal["recharge_periods"]:
        amount = box.system.billed_amount(account, period["period_start"])
        if amount is None:
            return None, [f"the proposed credit recharges {period['period_start']}: no such bill"]
        billed += amount
        day = date.fromisoformat(period["period_start"])
        recharged += float(charge_for(period["gallons"], day)["total"])
    return round(billed - recharged, 2), []


def check_draft(draft: dict[str, Any], box: Toolbox, complaint_text: str) -> list[str]:
    """Every problem in a draft. An empty list means every source and sum checked out."""
    rules = box.rules
    digits = rules["checks"]["figure_min_digits"]
    sections = {item["section_id"]: item for item in box.index.items}
    found = "\n".join(output for outputs in box.outputs.values() for output in outputs)
    allowed = figures(f"{complaint_text}\n{found}", digits)
    problems: list[str] = []

    credit, credit_problems = worked_out_credit(draft, box)
    problems += credit_problems
    proposal = draft.get("proposed_credit")
    if credit is not None and proposal:
        allowed |= figures(f"{credit:.2f}", digits)
        if abs(credit - float(proposal["amount"])) > rules["checks"]["credit_tolerance"]:
            amount = proposal["amount"]
            problems.append(f"the proposed credit is {amount}, but it works out {credit}")
    if proposal and box.serial_matches is False:
        problems.append("a credit is proposed, but the photo's serial doesn't match the account")

    if not draft["reply"].strip():
        problems.append("the reply is empty")
    if not draft["questions"]:
        problems.append("the draft lists no questions")
    for question in draft["questions"]:
        if question["answered"] and not question["statements"]:
            problems.append(f"marked answered with no source: {question['question']!r}")
        for statement in question["statements"]:
            source, quote = statement["source"], statement["quote"]
            if source in sections:
                section = sections[source]
                whole = f"{section['heading']}\n{section['text']}"
            elif source in LOOKUP_TOOLS:
                whole = "\n".join(box.outputs.get(source, []))
            else:
                problems.append(f"cites a source that doesn't exist: {source}")
                continue
            if not plain(quote) or plain(quote) not in plain(whole):
                problems.append(f"the quote isn't word for word in {source}: {quote!r}")
            for figure in sorted(figures(statement["claim"], digits) - allowed):
                problems.append(f"{figure} in a claim didn't come from any lookup")
    for figure in sorted(figures(draft["reply"], digits) - allowed):
        problems.append(f"{figure} in the reply didn't come from any lookup or the complaint")
    for claim in action_claims(draft["reply"], rules["checks"]["action_claims"]):
        problems.append(
            f"the reply says {claim!r}, but nobody has done that. Take it out, and put what needs "
            "doing in next_step_for_rep"
        )
    return problems


def status_of(result: AgentResult, problems: list[str]) -> str:
    """The status, from what happened and what the checks found. Claude doesn't choose it."""
    if result.ran_out:
        return "out_of_steps"
    if result.draft is None:
        return "no_draft"
    if problems:
        return "check_failed"
    answered = [question["answered"] for question in result.draft["questions"]]
    waiting = any(
        question["needed_before_anyone_can_act"]
        for question in result.draft["questions_for_customer"]
    )
    if all(answered) and not waiting:
        return "draft_ready"
    return "partly_answered" if any(answered) else "not_in_policy"


# --- Output ----------------------------------------------------------------------------------


def csv_safe(value: Any) -> str:
    """A leading ' on a cell a spreadsheet would run as a formula; anything else as written."""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def searches_of(result: AgentResult) -> list[str]:
    """The questions the agent asked the manual, in order."""
    return [
        str(entry["input"]["question"])
        for entry in result.transcript
        if entry.get("tool") == "search_policies"
    ]


def draft_markdown(
    complaint: Complaint,
    status: str,
    result: AgentResult,
    problems: list[str],
    masked: dict[str, int],
) -> str:
    lines = [
        f"# {complaint.complaint_id}: {complaint.subject}",
        "",
        f"**Account:** {complaint.account_id}",
        "",
        f"**Status:** `{status}`",
        "",
    ]
    if result.stopped:
        lines += [f"**Stopped because:** {result.stopped}", ""]
    if problems:
        lines += ["**Check these before sending:**", ""]
        lines += [f"- {problem}" for problem in problems] + [""]
    if result.first_review:
        lines += ["**The review sent the first draft back for these, and Claude revised it:**", ""]
        lines += [f"- {problem}" for problem in result.first_review] + [""]
    if masked:
        removed = ", ".join(f"{count} {kind}" for kind, count in sorted(masked.items()))
        lines += [f"**Personal data masked before Claude saw it:** {removed}", ""]
    draft = result.draft
    if draft:
        lines += [f"**Likely cause:** {draft['likely_cause']}", ""]
        proposal = draft.get("proposed_credit")
        if proposal:
            amount = proposal["amount"]
            lines += [f"**Proposed credit, for a supervisor to approve:** ${amount:.2f}", ""]
        if draft["next_step_for_rep"]:
            lines += [f"**Next step for the rep:** {draft['next_step_for_rep']}", ""]
        lines += ["## Draft reply", "", draft["reply"], ""]
        for heading, needed in (("Still needed from", True), ("Worth asking", False)):
            asks = [
                question["question"]
                for question in draft["questions_for_customer"]
                if question["needed_before_anyone_can_act"] is needed
            ]
            if asks:
                lines += [f"## {heading} the customer", ""]
                lines += [f"- {ask}" for ask in asks] + [""]
        lines += ["## Sources", ""]
        for question in draft["questions"]:
            state = "answered" if question["answered"] else "not answered"
            lines += [f"**{question['question']}** ({state})", ""]
            for statement in question["statements"]:
                lines.append(
                    f'- {statement["claim"]} `{statement["source"]}`: "{statement["quote"]}"'
                )
            lines.append("")
    searches = searches_of(result)
    if searches:
        lines += ["## What the agent asked the manual", ""]
        lines += [f"- {question}" for question in searches] + [""]
    lines += ["## Every tool call", ""]
    for entry in result.transcript:
        if "tool" in entry and entry["tool"] != "submit_draft":
            failed = " (failed)" if entry["is_error"] else ""
            lines.append(f"- `{entry['tool']}` {json.dumps(entry['input'])}{failed}")
    return "\n".join(lines) + "\n"


def write_outputs(output: Path, rows: list[dict[str, Any]]) -> None:
    (output / "drafts").mkdir(parents=True, exist_ok=True)
    (output / "transcripts").mkdir(exist_ok=True)
    with (output / "drafts.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=DRAFT_ROW)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_safe(row[key]) for key in DRAFT_ROW})
    for row in rows:
        complaint_id = row["complaint_id"]
        (output / "drafts" / f"{complaint_id}.md").write_text(row["markdown"], encoding="utf-8")
        (output / "transcripts" / f"{complaint_id}.json").write_text(
            json.dumps(row["transcript"], indent=1, default=str), encoding="utf-8"
        )


# --- The run ---------------------------------------------------------------------------------


def answer_complaint(claude: anthropic.Anthropic, box: Toolbox) -> dict[str, Any]:
    calls, complaint = box.calls, box.complaint
    before = calls.total
    started = time.monotonic()
    masked_body = ""
    try:
        masked_body, counts = mask_personal_data(
            calls, box.client, f"{complaint.complaint_id}/body", complaint.body, box.rules
        )
    except CallFailed as error:
        # Fail closed: unmasked customer text doesn't go to a third-party API.
        result = AgentResult(stopped=f"personal data couldn't be masked: {error}")
    else:
        box.masked_counts.update(counts)
        box.complaint_text = masked_body
        result = run_agent(claude, box, complaint_prompt(complaint, masked_body))
    problems = result.problems
    status = status_of(result, problems)
    proposal = result.draft.get("proposed_credit") if result.draft else None
    return {
        "complaint_id": complaint.complaint_id,
        "account_id": complaint.account_id,
        "status": status,
        "tool_calls": result.tool_calls,
        "searches": len(searches_of(result)),
        "revisions": result.revisions,
        "proposed_credit": f"{proposal['amount']:.2f}" if proposal else "",
        "sie_calls": calls.total - before,
        "seconds": round(time.monotonic() - started),
        "input_tokens": result.input_tokens + result.cached_tokens,
        "output_tokens": result.output_tokens,
        "markdown": draft_markdown(complaint, status, result, problems, box.masked_counts),
        "transcript": result.transcript,
    }


def load_or_build_index(calls: Calls, client: SIEClient, rules: dict[str, Any]) -> VectorIndex:
    saved = VectorIndex.load(HERE / rules["ingest"]["index_dir"], fingerprint(rules))
    if saved is not None:
        print(f"Using the saved index ({len(saved.items)} sections).")
        return saved
    print("The index is missing or out of date, so indexing the manual first.")
    return build_index(calls, client, rules)


def run(args: argparse.Namespace) -> None:
    rules = load_rules(args.rules)
    client = make_client()
    claude = make_claude()
    calls = Calls()
    system = BillingSystem.load(DATA_DIR)
    complaints = load_complaints(DATA_DIR / "complaints.csv")
    if args.complaint:
        complaints = [c for c in complaints if c.complaint_id == args.complaint]
        if not complaints:
            raise SystemExit(f"Stopped: no complaint {args.complaint} in data/complaints.csv")
    index = load_or_build_index(calls, client, rules)
    rows: list[dict[str, Any]] = []
    for complaint in complaints:
        box = Toolbox(client, calls, index, system, rules, complaint)
        row = answer_complaint(claude, box)
        print(
            f"{row['complaint_id']}: {row['status']} ({row['tool_calls']} tool calls, "
            f"{row['searches']} searches, {row['revisions']} revisions, {row['seconds']} s)"
        )
        rows.append(row)
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    write_outputs(output, rows)
    if calls.failures:
        failed = len(calls.failures)
        print(f"{failed} of {calls.total} SIE calls failed. First: {calls.failures[0]}")
    in_tokens = sum(row["input_tokens"] for row in rows)
    out_tokens = sum(row["output_tokens"] for row in rows)
    print(f"{calls.total} SIE calls, {in_tokens} Claude input and {out_tokens} output tokens.")
    print(f"Wrote {output}")
    if all(row["status"] == "no_draft" for row in rows):
        raise SystemExit("Stopped: no complaint got a draft. See the drafts for the reason.")


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--complaint", help="run one complaint, such as C-2001")
    parser.add_argument("--output", type=Path, help="folder for the outputs")
    parser.add_argument("--rules", type=Path, default=RULES_PATH, help="another rules.yaml to try")
    args = parser.parse_args()
    try:
        run(args)
    except SIEStopped as error:
        raise SystemExit(f"Stopped: {error} No outputs were written.") from None
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
