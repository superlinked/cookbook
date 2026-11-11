"""Look back over one repair shop's year of invoices for the SIU, using the invoice check.

Use case:  A special investigations unit (SIU) analyst asks a one-off question about one repair
           shop: every claim it invoiced this year, what the invoice checks found on each, and
           anything that looks like a pattern across them.
Who:       SIU analysts at a property and auto insurer.
Input:     data/agent/jobs.yaml (the request: shop, year, the analyst's text) and data/agent/
           (a claims-system seed for claims_system.py, and brightwater-invoices.pdf).
Loop:      Claude (rules.yaml agent.model) picks which claims to open and which comparisons to
           make, through four tools:
           claims_for_shop   ClaimsSystem.claims_for_vendor, the job's shop and year only
           get_claim         ClaimsSystem.get_claim, a claim on the job's list only
           check_invoice     recipe.process_invoice: LightOnOCR-3-4B reads the page and
                             gliner2-large-v1 finds the fields (two calls) on SIE, Python checks
           submit_report     ends the loop
Checks:    After the loop, code builds the per-claim table from the workflow's results, and
           checks the report: every claim on the list checked, every quote a value the workflow
           returned, and no finding without a failed check behind it. One revision.
Output:    runs/<timestamp>/agent/: siu-report.md and .json, transcript.json and cost.json. The
           SIU analyst decides what happens next.
Run using: SIE Serverless, or SIE Cluster or SIE Local with SIE_BASE_URL; ANTHROPIC_API_KEY too.

    uv run --group agent agent.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from claims_system import ClaimDocument, ClaimRecord, ClaimsSystem, claim_key
from dotenv import load_dotenv
from sie_sdk import SIEClient

import recipe

if TYPE_CHECKING:
    import anthropic

HERE = Path(__file__).resolve().parent
RUNS_DIR = HERE / "runs"
# Why a claim on the list has no workflow result, in the per-claim table.
NOT_RUN = "the agent didn't run the check"
CLEAR, FINDING, NOT_CHECKED = "clear", "finding", "not_checked"


# --- The job and the tools --------------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    vendor: str
    year: int
    asked_by: str
    text: str


def load_job(path: Path) -> Job:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Job(str(raw["vendor"]), int(raw["year"]), str(raw["asked_by"]), str(raw["text"]))


def object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def tools(rules: dict[str, Any]) -> list[dict[str, Any]]:
    kinds = list(rules["agent"]["pattern_kinds"])
    evidence = object_schema(
        {
            "claim": {"type": "string", "description": "claim number, such as CLM-26-104817"},
            "field_or_line": {
                "type": "string",
                "description": 'a key in "fields", "line N", or "claim.<key>"',
            },
            "quote": {"type": "string", "description": "the whole value, exactly as returned"},
        }
    )
    pattern = object_schema(
        {
            "kind": {"type": "string", "enum": kinds},
            "claims": {"type": "array", "items": {"type": "string"}},
            "evidence": {"type": "array", "items": evidence},
            "note": {"type": "string"},
        }
    )
    return [
        {
            "name": "claims_for_shop",
            "description": (
                "Every claim that lists this vendor (exact name) and has a repair invoice "
                "received in this year: document id, claim number, insured, claim status, loss "
                "date and place, and when the invoice arrived. Only the shop and year in the "
                "request can be looked up."
            ),
            "strict": True,
            "input_schema": object_schema(
                {"vendor": {"type": "string"}, "year": {"type": "integer"}}
            ),
        },
        {
            "name": "get_claim",
            "description": (
                "One claim's record by its exact claim number, for a claim on the shop's list: "
                "status, insured, loss date and place, vendors, estimate and supplements, and "
                "the shop's documents filed on it."
            ),
            "strict": True,
            "input_schema": object_schema({"claim_number": {"type": "string"}}),
        },
        {
            "name": "check_invoice",
            "description": (
                "Run the claims team's invoice check on one document (an id from claims_for_shop "
                "or get_claim): OCR, field extraction and every check against its claim. Returns "
                "the status, the fields read, the line items and each check with its detail."
            ),
            "strict": True,
            "input_schema": object_schema({"document": {"type": "string"}}),
        },
        {
            "name": "submit_report",
            "description": "Send the patterns you found to the analyst. Ends the job.",
            "strict": True,
            "input_schema": object_schema(
                {
                    "summary": {"type": "string", "description": "two or three plain sentences"},
                    "patterns": {"type": "array", "items": pattern},
                }
            ),
        },
    ]


class ToolError(Exception):
    """A tool call Claude can fix (an unknown id, a wrong argument); it goes back as an error."""


def claim_json(claim: ClaimRecord) -> dict[str, Any]:
    return {
        "claim_number": claim.claim_number,
        "status": claim.status,
        "named_insured": claim.named_insured,
        "loss_date": claim.loss_date.isoformat(),
        "loss_location": claim.vin or claim.loss_address,
        "vendors": list(claim.vendors),
        "authorizations": [
            {"kind": a.kind, "amount": f"{a.amount:.2f}", "approved_on": a.approved_on.isoformat()}
            for a in claim.authorizations
        ],
        "authorized": f"{claim.authorized:.2f}",
    }


@dataclass
class Toolbox:
    """What Claude's tools reach for one job: only the job's own records. claims_for_shop takes
    only the job's shop and year, get_claim only a claim on that list, and check_invoice only a
    document id a lookup returned in this job. Text on an invoice can't steer a tool anywhere
    else, because nothing else is reachable."""

    client: SIEClient
    calls: recipe.Calls
    claims: ClaimsSystem
    rules: dict[str, Any]
    data_dir: Path
    job: Job
    seen: dict[str, ClaimDocument] = field(default_factory=dict)  # ids Claude was shown
    results: dict[str, recipe.InvoiceResult] = field(default_factory=dict)  # as returned

    def run(self, name: str, args: dict[str, Any]) -> str:
        if name == "claims_for_shop":
            output: Any = self.claims_for_shop(str(args.get("vendor", "")), args.get("year"))
        elif name == "get_claim":
            output = self.get_claim(str(args.get("claim_number", "")))
        elif name == "check_invoice":
            output = self.check_invoice(str(args.get("document", "")))
        else:
            raise ToolError(f"there is no tool called {name}")
        return json.dumps(output, indent=1, ensure_ascii=False)

    def on_list(self) -> list[tuple[ClaimRecord, ClaimDocument]]:
        """The job's claims: the shop and year in the job file, never Claude's arguments."""
        return self.claims.claims_for_vendor(self.job.vendor, self.job.year)

    def claims_for_shop(self, vendor: str, year: Any) -> list[dict[str, Any]]:
        if not isinstance(year, int):
            raise ToolError("year must be a whole number, such as 2026")
        if vendor.strip().casefold() != self.job.vendor.strip().casefold() or year != self.job.year:
            raise ToolError(
                f"this job covers {self.job.vendor!r} in {self.job.year} only; no other shop or "
                "year can be looked up"
            )
        rows = []
        for claim, document in self.on_list():
            self.seen[document.document] = document
            rows.append(
                {
                    "document": document.document,
                    "claim_number": claim.claim_number,
                    "named_insured": claim.named_insured,
                    "status": claim.status,
                    "loss_date": claim.loss_date.isoformat(),
                    "loss_location": claim.vin or claim.loss_address,
                    "received": document.received.isoformat(),
                }
            )
        return rows

    def get_claim(self, claim_number: str) -> dict[str, Any]:
        listed = [
            (claim, document)
            for claim, document in self.on_list()
            if claim_key(claim.claim_number) == claim_key(claim_number)
        ]
        if not listed:
            raise ToolError(
                f"{claim_number!r} is not a claim on this job's list; use a claim number from "
                "claims_for_shop"
            )
        claim = listed[0][0]
        filed = [document for _, document in listed]
        for document in filed:
            self.seen[document.document] = document
        return {
            **claim_json(claim),
            "documents": [
                {"document": d.document, "kind": d.kind, "received": d.received.isoformat()}
                for d in filed
            ],
        }

    def check_invoice(self, document_id: str) -> dict[str, Any]:
        document = self.seen.get(document_id)
        if document is None:
            raise ToolError(
                f"no document {document_id!r} in this job; use an id from claims_for_shop or "
                "get_claim"
            )
        # One result per document: a second call returns the first, so the report is checked
        # against exactly what Claude was shown.
        if document_id not in self.results:
            self.results[document_id] = recipe.process_invoice(
                self.client, self.calls, document, self.claims, self.rules, self.data_dir
            )
        return self.results[document_id].as_json()


# --- The per-claim table (code, from the workflow's results) ----------------------------------


def unreadable(result: recipe.InvoiceResult) -> list[recipe.Check]:
    return [check for check in result.flagged if check.check == "readable"]


# A claim_match problem that only says a value wasn't read ("no claim number read on the
# invoice", "no vehicle read; the claim's VIN is ...").
UNREAD_MATCH = re.compile(r"no [\w -]+ read(?: on the invoice)?|the claim's VIN is \S+")


def only_unread(check: recipe.Check) -> bool:
    """True when a flagged check only records that the reader didn't get a value: a field that
    wasn't read, or was read two ways. That proves nothing against the shop, so it is not a
    failed check."""
    if check.check in ("readable", "conflicting_values") or check.detail.startswith("Not read"):
        return True
    if check.check == "claim_match":
        problems = check.detail.split(". Other checks not run")[0].split("; ")
        return all(UNREAD_MATCH.fullmatch(problem) for problem in problems)
    return False


def claim_rows(
    expected: list[tuple[ClaimRecord, ClaimDocument]], results: dict[str, recipe.InvoiceResult]
) -> list[dict[str, Any]]:
    """One row per claim on the list, from the claims system and the workflow's results.

    A claim that isn't open is `not_checked`: the workflow declines to check it, and that
    refusal is not a failed check against the shop, whatever check name it is filed under. A
    page that wasn't read (an SIE call failed, too little text) is `not_checked` too, and so is
    an invoice whose only flagged checks say a field wasn't read: an unread field is not a
    failed check. `finding` needs a check that failed on a value that was read, on an open
    claim; `clear` needs the status checks_passed.
    """
    rows = []
    for claim, document in expected:
        row: dict[str, Any] = {
            "document": document.document,
            "claim_number": claim.claim_number,
            "named_insured": claim.named_insured,
            "claim_status": claim.status,
            "workflow_status": "",
            "outcome": NOT_CHECKED,
            "cites": [],
            "reason": "",
        }
        result = results.get(document.document)
        if result is not None:
            row["workflow_status"] = result.status
        if claim.status != "open":
            row["reason"] = (
                f"claim is {claim.status}; the workflow only checks open claims, so this is no "
                "result against the shop"
            )
        elif result is None:
            row["reason"] = NOT_RUN
        elif unreadable(result):
            row["reason"] = "; ".join(check.detail for check in unreadable(result))
        elif result.status == recipe.CHECKS_PASSED:
            row["outcome"] = CLEAR
        elif all(only_unread(check) for check in result.flagged):
            details = "; ".join(f"{c.check}: {c.detail}" for c in result.flagged)
            row["reason"] = f"a field wasn't read ({details})"
        else:
            row["outcome"] = FINDING
            row["cites"] = [c.check for c in result.flagged if not only_unread(c)]
            row["reason"] = "; ".join(f"{c.check}: {c.detail}" for c in result.flagged)
        rows.append(row)
    return rows


# --- Checks on the report (code, never a model) -----------------------------------------------


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().casefold()


def values_at(result: recipe.InvoiceResult, where: str) -> list[str] | None:
    """The values a quote may equal at `where` in a workflow result; None if `where` names
    nothing there. A key in fields, "line N" (1-based, any cell), or "claim.<key>"."""
    as_json = result.as_json()
    if where in as_json["fields"]:
        return [as_json["fields"][where]]
    line = re.fullmatch(r"line\s*(\d+)", where.strip(), re.I)
    if line:
        index = int(line.group(1)) - 1
        items = as_json["line_items"]
        return list(items[index].values()) if 0 <= index < len(items) else None
    if where.startswith("claim.") and as_json["claim"] is not None:
        value = as_json["claim"].get(where.removeprefix("claim."))
        if value is None:
            return None
        return [str(v) for v in value] if isinstance(value, list) else [str(value)]
    return None


def check_report(
    report: dict[str, Any],
    rows: list[dict[str, Any]],
    results: dict[str, recipe.InvoiceResult],
    rules: dict[str, Any],
) -> list[str]:
    """What's wrong with the report, checked against the workflow's results, never against
    Claude's account of them. Empty when it passes."""
    problems: list[str] = []
    # A claim can have more than one invoice on the list, so each claim keeps all its rows.
    by_claim: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_claim.setdefault(claim_key(row["claim_number"]), []).append(row)
    for row in rows:
        if row["reason"] == NOT_RUN:
            problems.append(
                f"{row['claim_number']} ({row['document']}) is on the shop's list, but you didn't "
                "run check_invoice on it"
            )
    kinds = rules["agent"]["pattern_kinds"]
    patterns = report.get("patterns")
    if not isinstance(patterns, list):
        return [*problems, "patterns must be a list"]
    for number, pattern in enumerate(patterns, start=1):
        where = f"pattern {number}"
        kind = pattern.get("kind")
        if kind not in kinds:
            problems.append(f"{where}: kind {kind!r} is not one of {', '.join(kinds)}")
        named = [str(c) for c in pattern.get("claims") or []]
        if not named:
            problems.append(f"{where}: names no claims")
        for claim in named:
            if claim_key(claim) not in by_claim:
                problems.append(f"{where}: {claim} is not a claim on this shop's list for the year")
        evidence = pattern.get("evidence") or []
        if not evidence:
            problems.append(f"{where}: has no evidence")
        for item in evidence:
            problems += evidence_problems(where, item, named, by_claim, results)
        if kind == FINDING:
            failed = [
                claim
                for claim in named
                if any(row["outcome"] == FINDING for row in by_claim.get(claim_key(claim), []))
            ]
            if not failed:
                problems.append(
                    f"{where} is a finding, but none of its claims has a failed check in the "
                    "workflow's result; make it a question, or name the claim whose check failed"
                )
    return problems


def evidence_problems(
    where: str,
    item: dict[str, Any],
    named: list[str],
    by_claim: dict[str, list[dict[str, Any]]],
    results: dict[str, recipe.InvoiceResult],
) -> list[str]:
    """A quote must be the whole value at that place in a check_invoice result for that claim
    (any of its invoices on the list)."""
    claim, place, quote = (str(item.get(k, "")) for k in ("claim", "field_or_line", "quote"))
    if claim_key(claim) not in {claim_key(c) for c in named}:
        return [f"{where}: evidence from {claim}, which the pattern doesn't name"]
    checked = [
        results[row["document"]]
        for row in by_claim.get(claim_key(claim), [])
        if row["document"] in results
    ]
    if not checked:
        return [f"{where}: no check_invoice result for {claim} in this job to quote from"]
    found = [values_at(result, place) for result in checked]
    values = [value for at in found if at is not None for value in at]
    if all(at is None for at in found):
        return [f"{where}: {claim} has no {place!r} in its check_invoice result"]
    if squash(quote) not in {squash(value) for value in values}:
        shown = " / ".join(repr(value) for value in values)
        return [f"{where}: {claim} {place} is {shown} in its check_invoice result, not {quote!r}"]
    return []


def job_status(
    report: dict[str, Any] | None, problems: list[str], rows: list[dict[str, Any]]
) -> str:
    """The job's status, earned by positive evidence: report_ready needs a report, no problem
    left and every claim on the list explained by a workflow result or its claim status."""
    if report is None:
        return "no_report"
    if problems:
        return "check_failed"
    explained = all(
        row["outcome"] in (CLEAR, FINDING) or row["claim_status"] != "open" for row in rows
    )
    return "report_ready" if explained else "incomplete"


# --- The agent loop ---------------------------------------------------------------------------


@dataclass
class AgentResult:
    report: dict[str, Any] | None = None
    stopped: str = ""  # why there's no report, when there isn't one
    ran_out: bool = False
    tool_calls: int = 0  # claims_for_shop, get_claim and check_invoice
    revisions: int = 0
    first_review: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # what the last review found
    input_tokens: int = 0  # not read from or written to the cache
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    # Tokens by the model that actually answered (a server-side fallback can answer instead).
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    transcript: list[dict[str, Any]] = field(default_factory=list)

    def count(self, response: Any, asked: str) -> None:
        usage = response.usage
        tokens = {
            "input": usage.input_tokens,
            "cache_write": usage.cache_creation_input_tokens or 0,
            "cache_read": usage.cache_read_input_tokens or 0,
            "output": usage.output_tokens,
        }
        self.input_tokens += tokens["input"]
        self.cache_write_tokens += tokens["cache_write"]
        self.cache_read_tokens += tokens["cache_read"]
        self.output_tokens += tokens["output"]
        model = str(getattr(response, "model", None) or asked)
        bucket = self.by_model.setdefault(model, dict.fromkeys(tokens, 0))
        for kind, count in tokens.items():
            bucket[kind] += count


def make_claude() -> anthropic.Anthropic:
    import anthropic  # only the agent group installs it; selfcheck.py runs without it

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Stopped: set ANTHROPIC_API_KEY, in .env or the environment.")
    return anthropic.Anthropic()


def ask_claude(claude: Any, messages: list[Any], rules: dict[str, Any]) -> Any:
    settings = rules["agent"]
    extra: dict[str, Any] = {}
    if settings["fallbacks"]:
        extra = {"betas": [settings["fallback_beta"]], "fallbacks": settings["fallbacks"]}
    system = settings["system"].replace("{max_tool_calls}", str(settings["max_tool_calls"]))
    return claude.beta.messages.create(
        model=settings["model"],
        max_tokens=settings["max_tokens"],
        system=system,
        tools=tools(rules),
        messages=messages,
        output_config={"effort": settings["effort"]},
        cache_control={"type": "ephemeral"},
        **extra,
    )


def job_prompt(job: Job) -> str:
    return (
        f"Request from the {job.asked_by}: shop {job.vendor!r}, year {job.year}.\n"
        # The analyst writes this text, so it can't be allowed to close the tag itself.
        f"<request>\n{job.text.replace('</request>', '')}\n</request>"
    )


def one_lookup(box: Toolbox, use: Any, result: AgentResult) -> dict[str, Any]:
    """Run one tool call, or refuse it past the limit, and shape the result for Claude."""
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": use.id}
    if result.tool_calls >= box.rules["agent"]["max_tool_calls"]:
        result.ran_out = True
        output = "No tool calls left. Call submit_report now with what you have."
        block["is_error"] = True
    else:
        result.tool_calls += 1
        try:
            output = box.run(use.name, dict(use.input))
        except ToolError as error:
            output = str(error)
            block["is_error"] = True
    result.transcript.append(
        {"tool": use.name, "input": use.input, "output": output, "is_error": "is_error" in block}
    )
    block["content"] = output
    return block


def one_submission(
    box: Toolbox,
    expected: list[tuple[ClaimRecord, ClaimDocument]],
    use: Any,
    result: AgentResult,
    tool_results: list[dict[str, Any]],
) -> bool:
    """Check a submitted report. True ends the loop; False sends what was found back once."""
    report = dict(use.input)
    rows = claim_rows(expected, box.results)
    problems = check_report(report, rows, box.results, box.rules)
    result.transcript.append({"tool": "submit_report", "input": use.input, "problems": problems})
    if problems and result.revisions < box.rules["agent"]["max_revisions"]:
        result.revisions += 1
        result.first_review = problems
        found = "\n".join(f"- {problem}" for problem in problems)
        tool_results.append(
            {
                "type": "tool_result",
                "tool_use_id": use.id,
                "is_error": True,
                "content": "The code's checks found these problems. Fix them and call "
                "submit_report again.\n" + found,
            }
        )
        return False
    result.report, result.problems = report, problems
    return True


# HTTP statuses from the Claude API that may pass on a retry: timeout, conflict, rate limit.
TRANSIENT_STATUSES = (408, 409, 429)


def transient_claude_errors() -> tuple[type[Exception], ...]:
    """Claude API errors that may pass: the job ends without a report, with the reason. An
    APIStatusError counts only with a status in TRANSIENT_STATUSES or 500 and up (is_transient);
    any other error stops the run in main()."""
    import anthropic

    return (
        anthropic.APIConnectionError,
        anthropic.APITimeoutError,
        anthropic.RateLimitError,
        anthropic.InternalServerError,
        anthropic.APIStatusError,
    )


def is_transient(error: Exception) -> bool:
    """A caught Claude API error that may pass: no HTTP status (a connection error or timeout),
    or a status in TRANSIENT_STATUSES or 500 and up."""
    status = getattr(error, "status_code", None)
    return not isinstance(status, int) or status in TRANSIENT_STATUSES or status >= 500


def run_agent(
    claude: Any,
    box: Toolbox,
    expected: list[tuple[ClaimRecord, ClaimDocument]],
    prompt: str,
    transient: tuple[type[Exception], ...],
) -> AgentResult:
    """Let Claude look things up until it submits a report or reaches a limit. `transient` are
    the Claude API errors that end the job without a report instead of stopping the run."""
    settings = box.rules["agent"]
    messages: list[Any] = [{"role": "user", "content": prompt}]
    result = AgentResult(transcript=[{"prompt": prompt}])
    asked_to_submit = False
    for _ in range(settings["max_rounds"]):
        try:
            response = ask_claude(claude, messages, box.rules)
        except transient as error:
            if not is_transient(error):
                raise
            result.stopped = f"the Claude API failed: {type(error).__name__}"
            return result
        result.count(response, settings["model"])
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
            said = "\n".join(b.text for b in response.content if b.type == "text")
            result.transcript.append({"said": said})
            if asked_to_submit:
                result.stopped = "Claude ended its turn without calling submit_report"
                return result
            asked_to_submit = True
            messages.append({"role": "user", "content": "Call submit_report now."})
            continue
        tool_results: list[dict[str, Any]] = []
        accepted = False
        # Lookups first, so a report sent in the same response is checked against their results.
        for use in sorted(uses, key=lambda use: use.name == "submit_report"):
            if use.name != "submit_report":
                tool_results.append(one_lookup(box, use, result))
            elif one_submission(box, expected, use, result, tool_results):
                accepted = True
                break
        if accepted:
            return result
        messages.append({"role": "user", "content": tool_results})
    result.ran_out = True
    result.stopped = "reached the limit on requests to Claude"
    return result


# --- Output -----------------------------------------------------------------------------------


def claude_usd(
    result: AgentResult, prices: dict[str, dict[str, float]], asked: str
) -> tuple[float, list[str]]:
    """Dollars for the job, each response priced on the model that answered it, and the
    answering models rules.yaml has no price for (priced as the asked model, so check them)."""
    usd, unpriced = 0.0, []
    for model, tokens in result.by_model.items():
        if model not in prices:
            unpriced.append(model)
        rate = prices.get(model, prices[asked])
        usd += sum(tokens[kind] * rate[kind] for kind in tokens) / 1_000_000
    return usd, unpriced


MD_SPECIAL = re.compile(r"([\\`*_\[\]()<>|!#])")


def md_safe(value: Any) -> str:
    """Text from a document, a tool or Claude, made inert for Markdown: one line, with every
    character that could start a link, an image, HTML, emphasis, code, a heading or a table
    column escaped."""
    return MD_SPECIAL.sub(r"\\\1", re.sub(r"\s+", " ", str(value)).strip())


def fenced(text: Any) -> list[str]:
    """Free text (a summary, a note, the request) as a fenced block whose fence is longer than
    any run of backticks inside it, so nothing in it renders or closes the block."""
    body = str(text)
    longest = max((len(run) for run in re.findall(r"`+", body)), default=0)
    fence = "`" * max(3, longest + 1)
    return [fence + "text", *body.splitlines(), fence]


def report_markdown(
    job: Job,
    status: str,
    rows: list[dict[str, Any]],
    result: AgentResult,
    completeness: str,
    rules: dict[str, Any],
) -> str:
    lines = [f"# SIU look-back: {md_safe(job.vendor)}, {job.year}", ""]
    lines.append(f"**Status:** `{status}`: {rules['agent']['statuses'][status]}")
    lines += ["", f"**Asked by:** {md_safe(job.asked_by)}.", "", *fenced(job.text), ""]
    lines += ["## What the code checked", ""]
    if result.report is None:
        lines.append(f"- No report: {md_safe(result.stopped or 'Claude stopped')}.")
    elif result.problems:
        lines.append("- These checks still fail, so read the patterns below with care:")
        lines += [f"  - {md_safe(problem)}" for problem in result.problems]
    else:
        lines.append(
            "- Every claim on the list was checked or has a claim status that explains why not; "
            "every quote is a value the workflow returned; every finding has a failed check."
        )
    lines += [f"- {md_safe(completeness)}", f"- {rules['agent']['cannot_check']}", ""]
    lines += ["## Per-claim results (built by code from the workflow's results)", ""]
    lines += [
        "| Document | Claim | Insured | Claim status | Workflow status | Outcome | Why |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                md_safe(row[key]) or "-"
                for key in (
                    "document",
                    "claim_number",
                    "named_insured",
                    "claim_status",
                    "workflow_status",
                    "outcome",
                    "reason",
                )
            )
            + " |"
        )
    if result.report is not None:
        lines += ["", "## Summary", "", *fenced(result.report.get("summary", ""))]
        for kind, title in (("finding", "Findings"), ("question", "Questions for a person")):
            picked = [p for p in result.report.get("patterns", []) if p.get("kind") == kind]
            lines += ["", f"## {title}", ""]
            if not picked:
                lines.append("None.")
            for number, pattern in enumerate(picked, start=1):
                claims = ", ".join(md_safe(claim) for claim in pattern.get("claims", []))
                lines += [f"### {kind.capitalize()} {number}: {claims}", ""]
                lines += [*fenced(pattern.get("note", "")), ""]
                for item in pattern.get("evidence", []):
                    lines.append(
                        f"- {md_safe(item.get('claim'))} {md_safe(item.get('field_or_line'))}: "
                        f'"{md_safe(item.get("quote"))}"'
                    )
    lines += ["", "The SIU analyst decides what happens next. Nothing here was sent to the shop."]
    return "\n".join(lines) + "\n"


# --- The run ----------------------------------------------------------------------------------


def run(args: argparse.Namespace) -> None:
    rules = recipe.load_rules()
    settings = rules["agent"]
    job = load_job(HERE / settings["job"])
    data_dir = HERE / settings["data_dir"]
    claims = ClaimsSystem(data_dir)
    # The completeness check reads the shop and year from the job file, never from Claude.
    expected = claims.claims_for_vendor(job.vendor, job.year)
    completeness = settings["completeness"].format(
        vendor=job.vendor, year=job.year, count=len(expected)
    )
    load_dotenv()
    claude = make_claude()
    calls = recipe.Calls()
    started = time.monotonic()
    with recipe.make_client() as client:
        box = Toolbox(client, calls, claims, rules, data_dir, job)
        result = run_agent(claude, box, expected, job_prompt(job), transient_claude_errors())
        calls.check_all_failed()  # every SIE call failed: stop, no outputs
    seconds = round(time.monotonic() - started)
    rows = claim_rows(expected, box.results)
    status = job_status(result.report, result.problems, rows)
    usd, unpriced = claude_usd(result, settings["prices"], settings["model"])
    output = (args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")) / "agent"
    output.mkdir(parents=True, exist_ok=True)
    record = {
        "job": vars(job),
        "status": status,
        "completeness": completeness,
        "claims": rows,
        "report": result.report,
        "problems": result.problems,
        "first_review": result.first_review,
        "revisions": result.revisions,
        "tool_calls": result.tool_calls,
        "tools_used": [e["tool"] for e in result.transcript if "tool" in e],
        "stopped": result.stopped,
        "workflow_results": {doc: r.as_json() for doc, r in box.results.items()},
    }
    cost = {
        "model": settings["model"],
        "answered_by": result.by_model,
        "unpriced_models": unpriced,
        "input_tokens": result.input_tokens,
        "cache_write_tokens": result.cache_write_tokens,
        "cache_read_tokens": result.cache_read_tokens,
        "output_tokens": result.output_tokens,
        "claude_usd": round(usd, 4),
        "sie_calls": calls.total,
        "sie_calls_by_model": calls.count,
        "invoices_checked": len(box.results),
        "seconds": seconds,
    }
    for name, value in (
        ("siu-report.json", record),
        ("transcript.json", result.transcript),
        ("cost.json", cost),
    ):
        text = json.dumps(value, indent=1, ensure_ascii=False, default=str) + "\n"
        (output / name).write_text(text, encoding="utf-8")
    markdown = report_markdown(job, status, rows, result, completeness, rules)
    (output / "siu-report.md").write_text(markdown, encoding="utf-8")
    for row in rows:
        print(
            f"{row['document']:<7} {row['claim_number']:<14} {row['outcome']:<12} "
            f"{row['reason'] or '-'}"
        )
    if result.report is not None:
        for pattern in result.report.get("patterns", []):
            print(f"{pattern.get('kind')}: {', '.join(pattern.get('claims', []))}")
    print(f"\nstatus: {status}" + (f" ({result.stopped})" if result.stopped else ""))
    for problem in result.problems:
        print(f"  still failing: {problem}")
    print(
        f"{result.tool_calls} tool calls, {result.revisions} revisions, {calls.total} SIE calls, "
        f"{result.input_tokens + result.cache_write_tokens + result.cache_read_tokens} Claude "
        f"input and {result.output_tokens} output tokens (${usd:.2f}), {seconds} s"
    )
    print(f"Wrote {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="run folder (default: runs/<timestamp>)")
    args = parser.parse_args()
    import anthropic

    hints: dict[type[Exception], str] = {
        anthropic.AuthenticationError: "Check ANTHROPIC_API_KEY.",
        anthropic.PermissionDeniedError: "Check ANTHROPIC_API_KEY and its access to the model.",
        anthropic.NotFoundError: "Check the model name under agent: in rules.yaml.",
        anthropic.BadRequestError: (
            "Check the request settings under agent: in rules.yaml (model, effort, max_tokens, "
            "fallbacks)."
        ),
    }
    try:
        run(args)
    except recipe.SIEStopped as error:
        raise SystemExit(f"Stopped: {error}") from None
    except anthropic.APIError as error:
        # Transient errors end the job inside run_agent; anything that gets here stops the run.
        hint = next((h for kind, h in hints.items() if isinstance(error, kind)), "")
        raise SystemExit(
            f"Stopped: {error} (the Claude API, {type(error).__name__}). {hint} No outputs were "
            "written."
        ) from None


if __name__ == "__main__":
    main()
