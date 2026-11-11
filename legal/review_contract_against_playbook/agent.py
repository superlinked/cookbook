"""Negotiate an NDA clause against the playbook with the lawyer, checking every answer in code.

Use case:  The workflow (recipe.py) wrote the review. Then the counterparty pushes back: the
           lawyer asks what the playbook lets them offer, whether the other side's new wording
           meets it, and for the reply. Each question depends on the last answer, so nobody can
           write the steps down in advance.
Who:       The in-house commercial lawyer or contracts manager who reads decisions.csv.
Input:     data/agent/conversations.yaml (the lawyer's messages, one conversation per request),
           the CLM intake (clm.py), the NDAs in data/ and the playbook in rules.yaml.
Agent:     Claude (agent.model in rules.yaml) runs the loop and picks the next tool:
           get_request           clm.CLM.get_request, a lookup
           review_nda            recipe.review_request: docling, then fastino/gliner2-large-v1,
                                 on SIE, then the workflow's checks in Python
           playbook_position     a lookup over rules.yaml; code applies the fallback's conditions
           check_revised_clause  new wording put into its section of the full NDA, GLiNER2 on
                                 SIE for that section, then recipe.decide against the standard
                                 position and against the fallback
           answer_lawyer         ends a turn with an answer
           submit_draft          ends a turn with a reply to the counterparty or a note to an
                                 approver
Checks:    After every answer (check_answer) and every draft (check_reply), in Python, shown to
           the lawyer above the answer. No retry: a failing draft is shown with its failures.
Output:    runs/<timestamp>/agent/: <request>.json (turns, tool calls, checks, status, tokens,
           SIE calls), <request>.md (the transcript), reply_<request>.md (the last draft with
           any failure on top) and cost.json. Nothing is sent. The status is one of
           agent.statuses in rules.yaml.
Run using: SIE Serverless, or SIE Cluster or SIE Local with SIE_BASE_URL, plus ANTHROPIC_API_KEY.

    uv run --group agent agent.py
    uv run --group agent agent.py --request NDA-2026-0471
    uv run --group agent agent.py --chat --request NDA-2026-0447
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import clm
import yaml
from dotenv import load_dotenv
from sie_sdk import SIEClient

import recipe

if TYPE_CHECKING:
    import anthropic

ROOT = Path(__file__).resolve().parent
CONVERSATIONS = ROOT / "data" / "agent" / "conversations.yaml"
STANCES = ("agreed", "proposed", "ask_approver", "lawyer_to_confirm")
KINDS = ("reply_to_counterparty", "note_to_approver")
OPEN = (recipe.DEVIATES, recipe.NOT_FOUND, recipe.UNCLEAR)
LOOKUPS = ("get_request", "review_nda", "playbook_position", "check_revised_clause")
ENDS_TURN = ("answer_lawyer", "submit_draft")
# One kind of quote and dash, so a quote typed with curly quotes compares with the NDA's text.
SAME_CHARACTER = str.maketrans({chr(0x2018): "'", chr(0x2019): "'"})
SAME_CHARACTER.update(str.maketrans({chr(0x201C): '"', chr(0x201D): '"'}))
SAME_CHARACTER.update(str.maketrans({chr(0x2013): "-", chr(0x2014): "-"}))


class ToolError(Exception):
    """A tool call Claude made wrongly (an unknown request or section); it gets the message."""


def object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


REQUEST = {"type": "string", "description": "The CLM request id, such as NDA-2026-0447"}
TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_request",
        "description": "The CLM intake record: parties, deal type, our role, law preference.",
        "input_schema": object_schema({"request": REQUEST}, ["request"]),
    },
    {
        "name": "review_nda",
        "description": (
            "The workflow's playbook review of the request's draft: per position its status "
            "(matches_position, deviates, not_found, unclear), value, sections, reason, "
            "standard wording and approver, plus the NDA's section headings."
        ),
        "input_schema": object_schema({"request": REQUEST}, ["request"]),
    },
    {
        "name": "playbook_position",
        "description": (
            "One playbook position: label, limit or allowed list, standard wording, approver, "
            "and the fallback when its conditions match this request (else fallback null)."
        ),
        "input_schema": object_schema(
            {
                "request": REQUEST,
                "position": {
                    "type": "string",
                    "enum": [
                        "term",
                        "governing_law",
                        "non_solicit",
                        "residuals",
                        "permitted_disclosures",
                    ],
                },
            },
            ["request", "position"],
        ),
    },
    {
        "name": "check_revised_clause",
        "description": (
            "Replace one section of the NDA with new wording and rerun the playbook checks on "
            "the whole NDA. Returns every position's status against the standard position, "
            "and against the fallback where one applies. The heading must be one review_nda "
            "listed; new_text is the section's whole text after the change."
        ),
        "input_schema": object_schema(
            {
                "request": REQUEST,
                "section_heading": {"type": "string"},
                "new_text": {"type": "string"},
            },
            ["request", "section_heading", "new_text"],
        ),
    },
    {
        "name": "answer_lawyer",
        "description": "Answer the lawyer's question. Ends your turn.",
        "input_schema": object_schema(
            {
                "text": {"type": "string", "description": "The answer, plain and short"},
                "statuses": {
                    "type": "array",
                    "description": "Each position status the answer relies on, as a tool "
                    "returned it",
                    "items": object_schema(
                        {
                            "position": {"type": "string"},
                            "status": {"type": "string"},
                            "against": {"type": "string", "enum": ["standard", "fallback"]},
                        },
                        ["position", "status", "against"],
                    ),
                },
            },
            ["text", "statuses"],
        ),
    },
    {
        "name": "submit_draft",
        "description": (
            "Submit a reply to the counterparty or a note to an approver for the lawyer to "
            "read. Ends your turn."
        ),
        "input_schema": object_schema(
            {
                "kind": {"type": "string", "enum": list(KINDS)},
                "to": {"type": "string"},
                "body": {"type": "string"},
                "positions": {
                    "type": "array",
                    "items": object_schema(
                        {
                            "position": {"type": "string"},
                            "stance": {"type": "string", "enum": list(STANCES)},
                            "clause": {"type": "string"},
                            "approver": {"type": "string"},
                        },
                        ["position", "stance"],
                    ),
                },
            },
            ["kind", "to", "body", "positions"],
        ),
    },
]


# --- The tools ---------------------------------------------------------------------------------


def fallback_for(position: str, intake: clm.IntakeRecord, rules: dict[str, Any]) -> Any:
    """The position's fallback when every only_when field matches the intake record, else None."""
    fallback = rules["positions"][position].get("fallback")
    if not fallback:
        return None
    for name, allowed in fallback.get("only_when", {}).items():
        if getattr(intake, name, None) not in allowed:
            return None
    return fallback


def fallback_rules(rules: dict[str, Any], intake: clm.IntakeRecord) -> dict[str, Any] | None:
    """The playbook with each applicable fallback's limits in place of the position's own."""
    swapped = copy.deepcopy(rules)
    changed = False
    for name, spec in swapped["positions"].items():
        fallback = fallback_for(name, intake, rules)
        if fallback:
            spec.update({k: v for k, v in fallback.items() if k not in ("only_when", "wording")})
            changed = True
    return swapped if changed else None


def flat(text: str) -> str:
    return " ".join(str(text).split())


@dataclass
class Box:
    """One conversation's tools and everything they returned, for the checks."""

    client: SIEClient
    calls: recipe.Calls
    rules: dict[str, Any]
    system: clm.CLM
    intake: clm.IntakeRecord
    review: recipe.Review | None = None
    kept: dict[str, Any] = field(default_factory=dict)  # markdown, sections, candidates
    # position -> {"standard": status, "fallback": status or None}, from the latest tool call
    latest: dict[str, dict[str, str | None]] = field(default_factory=dict)
    # every clause check: section, new_text, and per position its standard and fallback status
    checked: list[dict[str, Any]] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)  # every tool result, as Claude read it
    lawyer: list[str] = field(default_factory=list)  # the lawyer's messages so far
    sie_failures: list[str] = field(default_factory=list)

    @property
    def request(self) -> str:
        return self.intake.request

    def run(self, name: str, args: dict[str, Any]) -> str:
        if args.get("request") != self.request:
            raise ToolError(
                f"This conversation is about {self.request}; got request "
                f"{args.get('request')!r}. Use {self.request}."
            )
        tools: dict[str, Callable[..., dict[str, Any]]] = {
            "get_request": self.get_request,
            "review_nda": self.review_nda,
            "playbook_position": self.playbook_position,
            "check_revised_clause": self.check_revised_clause,
        }
        if name not in tools:
            raise ToolError(f"Unknown tool {name}")
        kwargs = {k: v for k, v in args.items() if k != "request"}
        try:
            result = tools[name](**kwargs)
        except TypeError as error:
            raise ToolError(f"Bad arguments for {name}: {error}") from None
        output = json.dumps(result, ensure_ascii=False, indent=1)
        self.outputs.append(output)
        return output

    def get_request(self) -> dict[str, Any]:
        record = self.system.get_request(self.request)
        assert record is not None
        return {
            "request": record.request,
            "received": record.received.isoformat(),
            "counterparty": record.counterparty,
            "our_entity": record.our_entity,
            "business_purpose": record.business_purpose,
            "deal_type": record.deal_type,
            "paper": record.paper,
            "our_role": record.our_role,
            "governing_law_preference": record.governing_law_preference or "(left blank)",
        }

    def _sie_failed(self, before: int, what: str) -> None:
        new = self.calls.failures[before:]
        if new:
            self.sie_failures.append(f"{what}: {recipe.short(new[0][1])}")

    def ensure_review(self) -> recipe.Review:
        if self.review is None:
            before = len(self.calls.failures)
            self.review = recipe.review_request(
                self.calls, self.client, self.intake, self.rules, keep=self.kept
            )
            self._sie_failed(before, "review_nda")
            for finding in self.review.findings:
                self.latest[finding.position] = {"standard": finding.status, "fallback": None}
        return self.review

    def review_nda(self) -> dict[str, Any]:
        review = self.ensure_review()
        out = review.as_json()  # with each finding's clause: the text of the sections it cites
        out["sections"] = [s.heading for s in self.kept.get("sections", [])]
        return out

    def playbook_position(self, position: str) -> dict[str, Any]:
        positions = self.rules["positions"]
        if position not in positions:
            raise ToolError(f"No position {position!r}; the playbook has {', '.join(positions)}")
        spec = positions[position]
        law = self.intake.governing_law_preference or str(self.rules["default_governing_law"])
        out: dict[str, Any] = {
            "position": position,
            "label": spec["label"],
            "standard_wording": flat(spec["standard_wording"]).replace("{governing_law}", law),
            "approver": spec["approver"],
        }
        for key in ("max_years", "allowed", "forbidden", "required"):
            if key in spec:
                out[key] = list(spec[key]) if isinstance(spec[key], dict) else spec[key]
        fallback = fallback_for(position, self.intake, self.rules)
        if fallback:
            out["fallback"] = {
                k: (flat(v) if k == "wording" else v)
                for k, v in fallback.items()
                if k != "only_when"
            }
            out["fallback_note"] = (
                "Its conditions match this request; the lawyer may accept it without the approver"
            )
        else:
            out["fallback"] = None
            out["ask"] = spec["approver"]
            out["fallback_note"] = (
                "No fallback in the playbook for this request; accepting anything else needs the "
                "approver"
            )
        return out

    def check_revised_clause(self, section_heading: str, new_text: str) -> dict[str, Any]:
        self.ensure_review()
        sections: list[recipe.Section] = self.kept.get("sections", [])
        if not sections:
            raise ToolError("The NDA was not read, so no clause can be checked; see review_nda")
        target = [
            s for s in sections if flat(s.heading).casefold() == flat(section_heading).casefold()
        ]
        if not target:
            headings = "; ".join(s.heading for s in sections)
            raise ToolError(f"No section headed {section_heading!r}. The headings are: {headings}")
        old = target[0]
        new = recipe.Section(old.number, old.heading, f"{old.heading}\n\n{new_text.strip()}")
        revised = [new if s.number == old.number else s for s in sections]
        scratch = recipe.Review(self.intake)
        before = len(self.calls.failures)
        fresh = recipe.extract_candidates(self.calls, self.client, [new], self.rules, scratch)
        self._sie_failed(before, "check_revised_clause")
        kept = [c for c in self.kept["candidates"] if c.section.number != old.number]
        candidates = kept + fresh
        assert self.review is not None
        failed = {**self.review.failed, **scratch.failed}
        standard = decide(revised, candidates, self.rules, self.intake, failed)
        swapped = fallback_rules(self.rules, self.intake)
        with_fallback = (
            decide(revised, candidates, swapped, self.intake, failed) if swapped else None
        )
        positions: list[dict[str, Any]] = []
        record: dict[str, Any] = {"section": old.heading, "new_text": new_text, "positions": {}}
        for name, finding in standard.items():
            row: dict[str, Any] = {
                "position": name,
                "status": finding.status,
                "reason": finding.reason,
                "value": finding.value,
            }
            fb = None
            if with_fallback and fallback_for(name, self.intake, self.rules):
                fb = with_fallback[name].status
                row["status_against_fallback"] = fb
                row["reason_against_fallback"] = with_fallback[name].reason
            positions.append(row)
            self.latest[name] = {"standard": finding.status, "fallback": fb}
            record["positions"][name] = {"standard": finding.status, "fallback": fb}
        self.checked.append(record)
        return {"section": old.heading, "checked_in": "the full NDA", "positions": positions}


def decide(
    sections: list[recipe.Section],
    candidates: list[recipe.Candidate],
    rules: dict[str, Any],
    intake: clm.IntakeRecord,
    failed: dict[str, str],
) -> dict[str, recipe.Finding]:
    review = recipe.Review(intake, failed=dict(failed))
    recipe.decide(sections, candidates, rules, review)
    return {f.position: f for f in review.findings}


# --- Checks in code ----------------------------------------------------------------------------


def norm(text: str) -> str:
    """Text for comparing: one kind of quote and dash, no Markdown escapes, single spaces."""
    text = str(text).translate(SAME_CHARACTER).replace("\\", "")
    return re.sub(r"\s+", " ", text).strip().casefold()


# Where one duration ends and the next can start: punctuation and joining words. "and" joins
# two durations unless a smaller unit follows it ("three years and six months" is one).
_BETWEEN = (
    r"[.;:!?,+\n]|\b(?:or|plus|then|but|versus|vs|to|from|with|than|instead|over|after|before"
    r"|within|beyond|for|past)\b"
    r"|\band\b(?!\s+(?:a\s+half|\S+\s+(?:\(\d+\)\s*)?(?:months?|weeks?|days?))\b)"
)


def durations(text: str, rules: dict[str, Any]) -> list[float]:
    """Every duration in the text, in years ("four (4) years", "a 4-year term", "18 months").

    recipe.duration_years reads one value and gives its longest duration, so the text is cut
    into pieces between durations first and each piece is read on its own."""
    found: list[float] = []
    for piece in re.split(_BETWEEN, recipe._squash(text)):
        years = recipe.duration_years(piece, rules)
        if years is not None and years != float("inf"):
            found.append(round(years, 4))
    return found


def places(text: str, rules: dict[str, Any]) -> set[str]:
    """Places the answer names: the playbook's allowed list (recipe.jurisdiction) and the extra
    names under agent.places."""
    named: set[str] = set()
    allowed: dict[str, list[str]] = rules["positions"]["governing_law"]["allowed"]
    extra: dict[str, list[str]] = rules["agent"]["places"]
    for sentence in recipe.sentences(text):
        for name, phrases in {**allowed, **extra}.items():
            if recipe.jurisdiction(sentence, {name: phrases}):
                named.add(name)
    return named


def playbook_for(box: Box) -> dict[str, Any]:
    """The playbook's positions as they stand for this request: a fallback whose conditions
    don't match the intake record is left out, so its numbers and wording ground nothing."""
    positions = copy.deepcopy(box.rules["positions"])
    for name, spec in positions.items():
        if not fallback_for(name, box.intake, box.rules):
            spec.pop("fallback", None)
    return cast(dict[str, Any], positions)


def playbook_text(box: Box) -> str:
    return str(yaml.safe_dump(playbook_for(box), sort_keys=False, width=1000))


def sources(box: Box) -> str:
    """Everything an answer may draw a figure or a place from: the playbook, the NDA, every tool
    result and the lawyer's own messages."""
    nda = str(box.kept.get("markdown", ""))
    return "\n".join([playbook_text(box), nda, *box.outputs, *box.lawyer])


def ungrounded(text: str, box: Box) -> list[str]:
    """Durations and places in the text that come from none of the sources."""
    problems: list[str] = []
    source = sources(box)
    known = set(durations(source, box.rules))
    for spec in playbook_for(box).values():
        for limit in (spec.get("max_years"), spec.get("fallback", {}).get("max_years")):
            if limit is not None:
                known.add(float(limit))
    for years in sorted(set(durations(text, box.rules)) - known):
        problems.append(
            f"names a duration of {years:g} years that isn't in the playbook, the NDA, a tool "
            "result or the lawyer's words"
        )
    for place in sorted(places(text, box.rules) - places(source, box.rules)):
        problems.append(
            f"names {place}, which isn't in the playbook, the NDA, a tool result or the "
            "lawyer's words"
        )
    return problems


def check_answer(answer: dict[str, Any], box: Box) -> list[str]:
    """An answer to the lawyer: every duration and place grounded, every status it relies on the
    one the latest tool call returned."""
    problems = ungrounded(str(answer.get("text", "")), box)
    positions = box.rules["positions"]
    for claim in answer.get("statuses") or []:
        name, status, against = claim.get("position"), claim.get("status"), claim.get("against")
        if name not in positions:
            problems.append(f"states a status for {name!r}, which is not a playbook position")
            continue
        if status not in recipe.STATUSES:
            problems.append(f"states {name} is {status!r}, which is not a status")
            continue
        latest = box.latest.get(name)
        if latest is None:
            problems.append(f"states {name} is {status} but no tool returned a status for it")
            continue
        if against not in ("standard", "fallback"):
            problems.append(f"states {name} is {status} against {against!r}")
            continue
        got = latest.get(against)
        if got is None:
            problems.append(
                f"states {name} is {status} against the {against}, which no tool checked"
            )
        elif got != status:
            problems.append(
                f"states {name} is {status} against the {against}; the last tool call said {got}"
            )
    return problems


def quotes(body: str) -> list[str]:
    """Text in double quotes (straight or curly) in a draft's body."""
    return [q.strip() for q in re.findall(r'"([^"]+)"', str(body).translate(SAME_CHARACTER))]


def in_text(quote: str, text: str) -> bool:
    """The quote is in the text, ignoring case, spacing and a trailing full stop or comma."""
    return norm(quote).rstrip(".,;: ") in norm(text)


def allowed_wordings(name: str, box: Box) -> list[str]:
    """Wording a draft may mark agreed or proposed for a position: the playbook's standard
    wording, its fallback wording where the fallback applies, and every new_text a clause check
    passed for it (against the standard, or against an applicable fallback)."""
    spec = box.rules["positions"][name]
    law = box.intake.governing_law_preference or str(box.rules["default_governing_law"])
    out = [flat(spec["standard_wording"]).replace("{governing_law}", law)]
    fallback = fallback_for(name, box.intake, box.rules)
    if fallback and fallback.get("wording"):
        out.append(flat(fallback["wording"]))
    for check in box.checked:
        result = check["positions"].get(name, {})
        if recipe.MATCHES in (result.get("standard"), result.get("fallback")):
            out.append(check["new_text"])
    return out


def check_reply(draft: dict[str, Any], box: Box) -> list[str]:
    """A reply to the counterparty or a note to an approver, checked against the playbook, the
    clause checks and the workflow's review (which the agent didn't write)."""
    problems: list[str] = []
    positions = box.rules["positions"]
    kind, to, body = draft.get("kind"), str(draft.get("to", "")), str(draft.get("body", ""))
    entries = [e for e in draft.get("positions") or [] if isinstance(e, dict)]
    if kind not in KINDS:
        problems.append(f"kind {kind!r} is not one of {', '.join(KINDS)}")
    if box.review is None:
        problems.append("no review_nda in this conversation, so the draft can't be checked")
        return problems
    by_name: dict[str, dict[str, Any]] = {}
    for entry in entries:
        name, stance = entry.get("position"), entry.get("stance")
        if name not in positions:
            problems.append(f"position {name!r} is not in the playbook")
            continue
        if stance not in STANCES:
            problems.append(f"{name} has stance {stance!r}, not one of {', '.join(STANCES)}")
            continue
        if name in by_name:
            problems.append(f"{name} is listed twice")
        by_name[str(name)] = entry
    # Completeness, from the workflow's review: every open row shows up in the draft.
    for finding in box.review.findings:
        if finding.status in OPEN and finding.position not in by_name:
            problems.append(
                f"{finding.position} is {finding.status} in the review but missing from the draft"
            )
    for name, entry in by_name.items():
        stance, spec = entry["stance"], positions[name]
        latest = box.latest.get(name, {})
        fallback = fallback_for(name, box.intake, box.rules)
        if stance in ("agreed", "proposed"):
            clause = str(entry.get("clause", ""))
            if not any(norm(clause) == norm(w) for w in allowed_wordings(name, box)):
                where = "no fallback" if not fallback else f"fallback {latest.get('fallback')}"
                problems.append(
                    f"{name} marked {stance}; {latest.get('standard')}, {where}, approver "
                    f"{spec['approver']}: the clause is not the playbook's wording or wording a "
                    "clause check passed"
                )
        elif stance == "ask_approver":
            if entry.get("approver") != spec["approver"]:
                problems.append(
                    f"{name} asks approver {entry.get('approver')!r}; the playbook says "
                    f"{spec['approver']}"
                )
        elif stance == "lawyer_to_confirm" and latest.get("standard") == recipe.DEVIATES:
            problems.append(
                f"{name} deviates; it needs a redline or the approver ({spec['approver']}), not "
                "lawyer_to_confirm"
            )
    # Names: the counterparty exactly as the CLM has it, and no other request's counterparty.
    others = {
        record.counterparty
        for request in box.system.open_requests()
        if (record := box.system.get_request(request)) and request != box.request
    }
    for other in sorted(others):
        if norm(other) in norm(body) or norm(other) in norm(to):
            problems.append(f"names {other}, the counterparty on another request")
    asking = {str(e.get("approver")) for e in by_name.values() if e["stance"] == "ask_approver"}
    if kind == "reply_to_counterparty" and to != box.intake.counterparty:
        problems.append(f"is addressed to {to!r}; the CLM intake says {box.intake.counterparty}")
    if kind == "note_to_approver":
        if not asking:
            problems.append("is a note to an approver but asks no approver about any position")
        elif asking != {to}:
            problems.append(f"is addressed to {to!r} but asks {', '.join(sorted(asking))}")
        # It quotes, word for word, the clause it asks about.
        nda = str(box.kept.get("markdown", ""))
        for name, entry in by_name.items():
            if entry["stance"] != "ask_approver":
                continue
            finding = next(f for f in box.review.findings if f.position == name)
            clause = finding.clause or nda
            if not any(len(q.split()) >= 4 and in_text(q, clause) for q in quotes(body)):
                problems.append(f"asks about {name} but doesn't quote its clause word for word")
    # Every quote is in the NDA, the playbook as it stands for this request, a tool result, a
    # checked new_text or the lawyer's messages.
    pool = "\n".join(
        [
            str(box.kept.get("markdown", "")),
            *(w for name in positions for w in allowed_wordings(name, box)),
            *box.outputs,
            *box.lawyer,
        ]
    )
    for quote in quotes(body):
        if len(quote.split()) >= box.rules["agent"]["quote_min_words"] and not in_text(quote, pool):
            problems.append(
                f'quotes "{quote[:80]}", which is not in the NDA, the playbook, a tool result or '
                "the lawyer's words"
            )
    problems += [f"the body {p}" for p in ungrounded(body, box)]
    return problems


# --- The loop ----------------------------------------------------------------------------------


@dataclass
class Turn:
    question: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    answer: dict[str, Any] | None = None  # answer_lawyer's input, or {"text": ...}
    draft: dict[str, Any] | None = None
    problems: list[str] = field(default_factory=list)
    stopped: str = ""


@dataclass
class Conversation:
    request: str
    turns: list[Turn] = field(default_factory=list)
    tool_calls: int = 0  # lookups and clause checks
    claude_requests: int = 0
    input_tokens: int = 0  # not read from or written to the cache
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    stopped: str = ""

    @property
    def last_draft(self) -> tuple[Turn, dict[str, Any]] | None:
        drafts = [(t, t.draft) for t in self.turns if t.draft is not None]
        return drafts[-1] if drafts else None


def make_claude() -> anthropic.Anthropic:
    """The Claude client. anthropic is in the agent dependency group, so it's imported here."""
    import anthropic

    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Stopped: set ANTHROPIC_API_KEY, in .env or the environment.")
    return anthropic.Anthropic()


def ask_claude(claude: Any, messages: list[Any], rules: dict[str, Any]) -> Any:
    settings = rules["agent"]
    extra: dict[str, Any] = {}
    if settings["fallbacks"]:
        extra = {"betas": [settings["fallback_beta"]], "fallbacks": settings["fallbacks"]}
    return claude.beta.messages.create(
        model=settings["model"],
        max_tokens=settings["max_tokens"],
        system=settings["system"],
        tools=TOOLS,
        messages=messages,
        output_config={"effort": settings["effort"]},
        cache_control={"type": "ephemeral"},
        **extra,
    )


def first_message(box: Box, question: str) -> str:
    return (
        f"Request {box.request} ({box.intake.counterparty}). The lawyer asks:\n"
        f"<lawyer>\n{question.replace('</lawyer>', '')}\n</lawyer>"
    )


def next_message(question: str) -> str:
    return f"The lawyer asks:\n<lawyer>\n{question.replace('</lawyer>', '')}\n</lawyer>"


def text_of(content: list[Any]) -> str:
    return "\n".join(str(block.text) for block in content if block.type == "text")


def one_lookup(box: Box, use: Any, convo: Conversation, turn: Turn) -> dict[str, Any]:
    """Run one tool call, or refuse it past the cap, and shape the result for Claude."""
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": use.id}
    if convo.tool_calls >= box.rules["agent"]["max_tool_calls"]:
        output, block["is_error"] = "No tool calls left. Answer with what you have.", True
    else:
        convo.tool_calls += 1
        try:
            output = box.run(use.name, dict(use.input))
        except ToolError as error:
            output, block["is_error"] = str(error), True
    turn.tool_calls.append(
        {"tool": use.name, "input": use.input, "output": output, "is_error": "is_error" in block}
    )
    block["content"] = output
    return block


def end_turn(box: Box, use: Any, turn: Turn) -> None:
    """Record the answer or draft that ends the turn, and run its checks."""
    if use.name == "answer_lawyer":
        turn.answer = dict(use.input)
        turn.problems = check_answer(turn.answer, box)
    else:
        turn.draft = dict(use.input)
        turn.problems = check_reply(turn.draft, box)


def shown(turn: Turn) -> str:
    """The tool result for the answer or draft: what the lawyer was shown above it."""
    if not turn.problems:
        return "Shown to the lawyer. The checks found nothing."
    found = "\n".join(f"- {p}" for p in turn.problems)
    return "Shown to the lawyer with these warnings from the checks:\n" + found


def run_turn(claude: Any, box: Box, messages: list[Any], convo: Conversation, turn: Turn) -> Any:
    """Let Claude call tools until it answers or drafts. Returns the tool_use that ended the
    turn (its result goes in front of the next message), or None."""
    import anthropic

    settings = box.rules["agent"]
    for _ in range(settings["max_rounds_per_turn"]):
        try:
            response = ask_claude(claude, messages, box.rules)
        except (
            anthropic.RateLimitError,
            anthropic.InternalServerError,
            anthropic.APIConnectionError,
        ) as error:
            turn.stopped = f"the Claude API failed: {type(error).__name__}"
            return None
        convo.claude_requests += 1
        usage = response.usage
        convo.input_tokens += usage.input_tokens
        convo.output_tokens += usage.output_tokens
        convo.cache_read_tokens += usage.cache_read_input_tokens or 0
        convo.cache_write_tokens += usage.cache_creation_input_tokens or 0
        if response.stop_reason == "refusal":
            turn.stopped = "Claude declined the request"
            return None
        if response.stop_reason == "max_tokens":
            turn.stopped = "Claude hit agent.max_tokens in rules.yaml"
            return None
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue
        uses = [block for block in response.content if block.type == "tool_use"]
        if not uses:
            # A plain-text answer: checked like one, with no statuses to compare.
            turn.answer = {"text": text_of(response.content), "statuses": []}
            turn.problems = check_answer(turn.answer, box)
            return "text"
        results: list[dict[str, Any]] = []
        ender = None
        for use in uses:
            if use.name in ENDS_TURN:
                if ender is None:
                    ender = use
                else:
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": use.id,
                            "is_error": True,
                            "content": "Only one answer or draft per turn; this one was dropped.",
                        }
                    )
            else:
                results.append(one_lookup(box, use, convo, turn))
        if ender is not None:
            end_turn(box, ender, turn)
            results.append({"type": "tool_result", "tool_use_id": ender.id, "content": shown(turn)})
            return results
        messages.append({"role": "user", "content": results})
    turn.stopped = "reached agent.max_rounds_per_turn in rules.yaml"
    return None


def converse(
    claude: Any, box: Box, questions: Callable[[], str | None], show: Callable[[Turn], None]
) -> Conversation:
    """Run the conversation: one turn per lawyer message, until there are none."""
    convo = Conversation(box.request)
    messages: list[Any] = []
    pending: list[dict[str, Any]] = []  # tool results owed to Claude from the last turn
    while (question := questions()) is not None:
        box.lawyer.append(question)
        text = first_message(box, question) if not messages else next_message(question)
        messages.append({"role": "user", "content": [*pending, {"type": "text", "text": text}]})
        turn = Turn(question)
        convo.turns.append(turn)
        ended = run_turn(claude, box, messages, convo, turn)
        show(turn)
        if ended is None:
            convo.stopped = turn.stopped
            break
        pending = ended if isinstance(ended, list) else []
    return convo


def status_of(convo: Conversation, box: Box) -> tuple[str, str]:
    """The conversation's status from rules.yaml agent.statuses, and why. Fail closed: a checked
    status needs a draft, a review to check it against and no warning anywhere."""
    if box.sie_failures:
        return "to_person", "; ".join(box.sie_failures)
    failing = [t for t in convo.turns if t.problems]
    if failing:
        return "checks_failed", f"{len(failing)} of {len(convo.turns)} turns had warnings"
    last = convo.last_draft
    if last is None or box.review is None:
        return "no_draft", convo.stopped or "no draft was asked for or submitted"
    if convo.stopped:
        return "no_draft", convo.stopped
    kind = last[1].get("kind")
    if kind == "reply_to_counterparty":
        return "reply_checked", "the reply and every answer passed the checks"
    if kind == "note_to_approver":
        return "approver_note_checked", "the note and every answer passed the checks"
    return "checks_failed", f"draft of unknown kind {kind!r}"


# --- Outputs -----------------------------------------------------------------------------------


def claude_usd(convo: Conversation, prices: dict[str, float]) -> float:
    return (
        convo.input_tokens * prices["input"]
        + convo.cache_write_tokens * prices["cache_write"]
        + convo.cache_read_tokens * prices["cache_read"]
        + convo.output_tokens * prices["output"]
    ) / 1_000_000


def warnings_block(problems: list[str]) -> str:
    if not problems:
        return "Checks: passed.\n"
    return "**Checks failed. Read these first:**\n" + "".join(f"- {p}\n" for p in problems)


def draft_markdown(turn: Turn, draft: dict[str, Any]) -> str:
    lines = [
        warnings_block(turn.problems),
        f"**{draft.get('kind')}** to {draft.get('to')}\n",
        str(draft.get("body", "")).strip() + "\n",
        "| Position | Stance | Approver | Clause |",
        "| --- | --- | --- | --- |",
    ]
    for entry in draft.get("positions") or []:
        clause = flat(entry.get("clause", "")).replace("|", "\\|")
        lines.append(
            f"| {entry.get('position')} | {entry.get('stance')} | {entry.get('approver', '')} | "
            f"{clause} |"
        )
    return "\n".join(lines) + "\n\nNothing was sent. The lawyer reads this before using it.\n"


def transcript_markdown(convo: Conversation, status: str, why: str) -> str:
    out = [f"# {convo.request}: {status}\n", f"{why}\n"]
    for n, turn in enumerate(convo.turns, start=1):
        out.append(f"## Turn {n}\n\n**Lawyer:** {turn.question}\n")
        for call in turn.tool_calls:
            args = {k: v for k, v in call["input"].items() if k != "request"}
            out.append(f"- `{call['tool']}` {json.dumps(args, ensure_ascii=False)}")
        if turn.tool_calls:
            out.append("")
        if turn.draft is None:  # a draft carries its own warnings
            out.append(warnings_block(turn.problems))
        if turn.answer is not None:
            out.append(f"**Answer:** {turn.answer.get('text', '')}\n")
        if turn.draft is not None:
            out.append(draft_markdown(turn, turn.draft))
        if turn.stopped:
            out.append(f"Stopped: {turn.stopped}\n")
    return "\n".join(out)


def job_record(
    convo: Conversation, box: Box, status: str, why: str, sie_calls: int
) -> dict[str, Any]:
    prices = box.rules["agent"]["prices"][box.rules["agent"]["model"]]
    return {
        "request": convo.request,
        "status": status,
        "why": why,
        "review": box.review.as_json() if box.review else None,
        "turns": [
            {
                "lawyer": t.question,
                "tool_calls": t.tool_calls,
                "answer": t.answer,
                "draft": t.draft,
                "check_problems": t.problems,
                "stopped": t.stopped,
            }
            for t in convo.turns
        ],
        "clause_checks": box.checked,
        "sie_failures": box.sie_failures,
        "tool_calls": convo.tool_calls,
        "claude_requests": convo.claude_requests,
        "tokens": {
            "input": convo.input_tokens,
            "cache_write": convo.cache_write_tokens,
            "cache_read": convo.cache_read_tokens,
            "output": convo.output_tokens,
        },
        "claude_usd": round(claude_usd(convo, prices), 4),
        "sie_calls": sie_calls,
    }


def write_job(out: Path, convo: Conversation, box: Box, record: dict[str, Any]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    text = json.dumps(record, indent=2, ensure_ascii=False, default=str) + "\n"
    (out / f"{convo.request}.json").write_text(text, encoding="utf-8")
    md = transcript_markdown(convo, record["status"], record["why"])
    (out / f"{convo.request}.md").write_text(md, encoding="utf-8")
    if last := convo.last_draft:
        (out / f"reply_{convo.request}.md").write_text(draft_markdown(*last), encoding="utf-8")


def print_turn(turn: Turn) -> None:
    print(f"\nLawyer: {turn.question}")
    for call in turn.tool_calls:
        print(f"  [{call['tool']}{' error' if call['is_error'] else ''}]")
    for problem in turn.problems:
        print(f"  WARNING: {problem}")
    if turn.answer is not None:
        print(f"Agent: {turn.answer.get('text', '')}")
    if turn.draft is not None:
        print(f"Agent drafted a {turn.draft.get('kind')} to {turn.draft.get('to')}:")
        print(str(turn.draft.get("body", "")))
    if turn.stopped:
        print(f"  Stopped: {turn.stopped}")


# --- The run -----------------------------------------------------------------------------------


def load_conversations(path: Path = CONVERSATIONS) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return cast(list[dict[str, Any]], data["conversations"])


def scripted(turns: list[str]) -> Callable[[], str | None]:
    left = list(turns)
    return lambda: left.pop(0) if left else None


def typed() -> str | None:
    try:
        line = input("\nYou (empty line to finish): ").strip()
    except EOFError:
        return None
    return line or None


def run(args: argparse.Namespace) -> None:
    rules = recipe.load_rules(args.rules)
    system = clm.CLM()
    if args.chat:
        if not args.request:
            raise SystemExit("Stopped: --chat needs --request, such as NDA-2026-0447")
        jobs = [{"request": args.request, "turns": None}]
    else:
        jobs = load_conversations()
        if args.request:
            jobs = [j for j in jobs if j["request"] == args.request]
            if not jobs:
                raise SystemExit(f"Stopped: no conversation for {args.request} in {CONVERSATIONS}")
    for job in jobs:
        if system.get_request(job["request"]) is None:
            raise SystemExit(f"Stopped: the CLM holds no request {job['request']}")
    output = (args.output or recipe.RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")) / (
        "agent"
    )
    claude = make_claude()
    calls = recipe.Calls()
    records: list[dict[str, Any]] = []
    finished: list[tuple[Conversation, Box, dict[str, Any]]] = []
    with recipe.make_client() as client:
        for job in jobs:
            intake = system.get_request(job["request"])
            assert intake is not None
            box = Box(client, calls, rules, system, intake)
            before, started = calls.total, time.monotonic()
            print(f"\n=== {intake.request} ({intake.counterparty}) ===")
            questions = typed if job["turns"] is None else scripted(job["turns"])
            convo = converse(claude, box, questions, print_turn)
            # A refusal already stopped the run (SIEStopped); every call failing stops it here.
            calls.check_all_failed()
            status, why = status_of(convo, box)
            record = job_record(convo, box, status, why, calls.total - before)
            record["seconds"] = round(time.monotonic() - started)
            finished.append((convo, box, record))
            records.append(record)
            print(
                f"--> {intake.request}: {status} ({why}); {convo.tool_calls} tool calls, "
                f"{record['sie_calls']} SIE calls, ${record['claude_usd']:.2f} Claude, "
                f"{record['seconds']} s"
            )
    # Written only now: an SIE refusal or every call failing stops the run with no outputs.
    for convo, box, record in finished:
        write_job(output, convo, box, record)
    output.mkdir(parents=True, exist_ok=True)
    cost = {
        "model": rules["agent"]["model"],
        "jobs": {
            r["request"]: {
                k: r[k] for k in ("status", "tokens", "claude_usd", "sie_calls", "tool_calls")
            }
            for r in records
        },
        "total_claude_usd": round(sum(r["claude_usd"] for r in records), 4),
        "total_sie_calls": calls.total,
    }
    (output / "cost.json").write_text(json.dumps(cost, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {output}. Nothing was sent; the lawyer reads each draft first.")


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--request", help="run one conversation, such as NDA-2026-0471")
    parser.add_argument("--chat", action="store_true", help="type the lawyer's messages yourself")
    parser.add_argument("--output", type=Path, help="run folder (default: runs/<timestamp>)")
    parser.add_argument("--rules", type=Path, default=recipe.RULES_PATH, help="another rules.yaml")
    args = parser.parse_args()
    import anthropic

    try:
        recipe.stop_on_refusal(lambda: run(args))
    except (
        anthropic.AuthenticationError,
        anthropic.PermissionDeniedError,
        anthropic.NotFoundError,
        anthropic.BadRequestError,
    ) as error:
        raise SystemExit(
            f"Stopped: the Claude API refused the request, {type(error).__name__}: {error}. Check "
            "ANTHROPIC_API_KEY and agent.model in rules.yaml."
        ) from None


if __name__ == "__main__":
    main()
