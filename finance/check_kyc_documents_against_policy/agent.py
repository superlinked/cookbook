"""An agent for the KYC analyst's odd jobs, with the KYC workflow as one of its tools.

Who:       The KYC analyst who runs the daily checks with recipe.py, and gets the odd jobs too.
Jobs:      (a) A customer who just moved (kyc-13): the analyst asks three questions in turn, each
               depending on the last answer, and reads every answer.
           (b) The regulator's letter: every personal account opened in 2025 on a mobile phone
               bill, and what was done about each. The MLRO signs the answer.
Agent:     Claude runs the loop and picks the next tool. The tools are:
           read_handbook   plain lookup in data/agent/cdd_handbook.md, by section number
           lookup_case     the onboarding system (onboarding.py) over data/agent/ and data/
           read_scan       recipe.read_document: lightonai/LightOnOCR-3-4B, then
                           fastino/gliner2-large-v1, both on SIE; then the workflow's own checks
                           against the form
           run_kyc_check   recipe.process_case, the whole workflow on one case
           find_accounts, list_documents, case_notes
                           plain lookups in data/agent/ (the register, uploads, notes)
Checks:    After every answer, in code: quotes word for word, ids that exist, claims about the
           workflow against its real result, the email's asks against the workflow's. After job
           (b), code reads every 2025 address scan itself (read_document and issuer_type) and
           compares the set it finds with the agent's. A gap goes back to the agent once.
Output:    runs/<timestamp>/agent/: chat_kyc-13.md, email_kyc-13.txt, regulator_2025.csv,
           completeness.csv, regulator_2025.md, job_a.json, job_b.json, the transcripts and
           cost.json. Statuses (in rules.yaml) say what the checks found; people decide.
Run using: SIE Serverless, or SIE Cluster or SIE Local with SIE_BASE_URL, plus ANTHROPIC_API_KEY.

    uv run --group agent agent.py
    uv run --group agent agent.py --job b
    uv run --group agent agent.py --chat
"""

from __future__ import annotations

import argparse
import csv
import functools
import inspect
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

import yaml
from onboarding import Application, OnboardingSystem, SubmittedDocument
from sie_sdk import SIEClient

import recipe
from recipe import Calls, CaseResult, SIEStopped

ROOT = Path(__file__).resolve().parent
AGENT_DIR = ROOT / "data" / "agent"
RUNS_DIR = ROOT / "runs"
CHAT_PATH = AGENT_DIR / "chat_kyc-13.yaml"
LETTER_PATH = AGENT_DIR / "regulator_2025.txt"
HANDBOOK_PATH = AGENT_DIR / "cdd_handbook.md"
SAME_CHARACTER = {0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"', 0x2013: "-", 0x2014: "-"}
ADDRESS_REQUIREMENTS_KEY = "address"


class ToolError(Exception):
    """A tool call that can't be answered, sent back to Claude as an error it can react to."""


# --- The bank's records, as plain lookups ------------------------------------------------------


@dataclass(frozen=True)
class DocumentRow:
    case: str
    document: str
    purpose: str
    declared_type: str
    file: str
    page: int
    received: str  # the date the document arrived
    data_dir: Path


@dataclass(frozen=True)
class Account:
    account: str
    case: str
    product: str
    opened: str
    status: str
    status_date: str


@dataclass(frozen=True)
class Note:
    note: str
    case: str
    date: str
    author_role: str
    text: str


def section_number(text: str) -> str:
    """ "4.2", "section 4.2", "Section 4.2" or "§4.2" -> "4.2"."""
    return re.sub(r"^(?:sections?|§)\s*", "", text.strip(), flags=re.I).strip()


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def split_handbook(markdown: str) -> dict[str, tuple[str, str]]:
    """Section number -> (heading, text), from each `## <number> <heading>` line."""
    parts = re.split(r"^##[ \t]+(\d+\.\d+)[ \t]+(.+?)[ \t]*$", markdown, flags=re.M)
    sections: dict[str, tuple[str, str]] = {}
    for number, heading, body in zip(parts[1::3], parts[2::3], parts[3::3], strict=True):
        sections[number] = (heading, " ".join(body.split()))
    return sections


class Records:
    """The onboarding system (data/agent/ first, then the workflow's data/), the account register,
    the case notes and the handbook. Every lookup the tools and the checks make goes through
    here."""

    def __init__(self, agent_dir: Path = AGENT_DIR, workflow_dir: Path = recipe.DATA_DIR) -> None:
        self.systems = [(OnboardingSystem(agent_dir), agent_dir)]
        self.systems.append((OnboardingSystem(workflow_dir), workflow_dir))
        self.sections = split_handbook((agent_dir / "cdd_handbook.md").read_text(encoding="utf-8"))
        self.accounts = {
            row["account"]: Account(**row) for row in read_rows(agent_dir / "accounts.csv")
        }
        self.notes = {row["note"]: Note(**row) for row in read_rows(agent_dir / "case_notes.csv")}
        self.documents: list[DocumentRow] = []
        for _, folder in self.systems:
            received = {
                row["case"]: row["received"] for row in read_rows(folder / "onboarding_cases.csv")
            }
            for row in read_rows(folder / "onboarding_documents.csv"):
                self.documents.append(
                    DocumentRow(
                        row["case"],
                        row["document"],
                        row["purpose"],
                        row["declared_type"],
                        row["file"],
                        int(row["page"]),
                        row.get("received") or received[row["case"]],
                        folder,
                    )
                )

    def application(self, case: str) -> tuple[Application, Path] | None:
        for system, folder in self.systems:
            found = system.get_case(case)
            if found is not None:
                return found, folder
        return None

    def cases(self) -> set[str]:
        return {row.case for row in self.documents}

    def document(self, name: str) -> DocumentRow | None:
        """A document by its id; one given for two purposes comes back as its first row."""
        return next((row for row in self.documents if row.document == name), None)

    def documents_of(self, case: str) -> list[DocumentRow]:
        return [row for row in self.documents if row.case == case]

    def surnames(self) -> dict[str, list[str]]:
        """Case -> the words of the customer's surname, for spotting other customers' names."""
        found: dict[str, list[str]] = {}
        for system, _ in self.systems:
            for case in system.open_cases():
                application = system.get_case(case)
                if application is not None:
                    found[case] = re.findall(r"[a-z]+", application.form["surname"].casefold())
        return found


# --- What the tools return ---------------------------------------------------------------------


def requirement_rows(result: CaseResult) -> list[dict[str, str]]:
    return [
        {
            "requirement": r.requirement,
            "document": r.document,
            "status": r.status,
            "detail": r.detail,
        }
        for r in result.requirements
    ]


def kyc_output(result: CaseResult) -> dict[str, Any]:
    """run_kyc_check's answer: what the workflow found, as the analyst's checklist shows it."""
    return {
        "case": result.case,
        "received": result.received.isoformat(),
        "checked_against": "policy.yaml as it is today",
        "status": result.status,
        "requirements": requirement_rows(result),
        "ask_customer": [
            {"requirement": r.requirement, "ask": r.ask} for r in result.unmet if r.ask
        ],
        "for_analyst": [
            {"requirement": r.requirement, "document": r.document, "detail": r.detail}
            for r in result.for_analyst
        ],
    }


def readable(value: Any) -> str:
    """A tool result as plain text, one value per line and "key: value" lines, for quote checks."""

    def lines(item: Any) -> list[str]:
        if isinstance(item, dict):
            own = [
                f"{key}: {child}"
                for key, child in item.items()
                if not isinstance(child, (dict, list))
            ]
            return own + [text for child in item.values() for text in lines(child)]
        if isinstance(item, list):
            return [text for child in item for text in lines(child)]
        return [str(item)]

    return "\n".join(lines(value))


def scan_rules(rules: dict[str, Any]) -> dict[str, Any]:
    """The workflow's rules with read_scan's extra labels added, for read_scan only."""
    extra = rules["agent"]["read_scan_labels"]
    address = {
        name: {**spec, "label": [*spec.get("label", []), *extra.get(name, [])]}
        if name in extra
        else spec
        for name, spec in rules["documents"]["address"].items()
    }
    return {**rules, "documents": {**rules["documents"], "address": address}}


@dataclass
class Toolbox:
    """The tools for one job, and everything they returned, which the checks read afterwards."""

    client: SIEClient
    calls: Calls
    records: Records
    policy: dict[str, Any]
    rules: dict[str, Any]
    cases: frozenset[str]  # the only cases this job's tools may reach (job_a_cases, job_b_cases)
    kyc: dict[str, CaseResult] = field(default_factory=dict)  # case -> latest run_kyc_check
    kyc_all: dict[str, list[CaseResult]] = field(default_factory=dict)
    seen: dict[str, list[str]] = field(default_factory=dict)  # id -> text tools returned about it
    sie_problems: list[str] = field(default_factory=list)  # transient SIE failures in tools

    def run(self, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        tools: dict[str, Callable[..., dict[str, Any]]] = {
            "read_handbook": self.read_handbook,
            "lookup_case": self.lookup_case,
            "read_scan": self.read_scan,
            "run_kyc_check": self.run_kyc_check,
            "find_accounts": self.find_accounts,
            "list_documents": self.list_documents,
            "case_notes": self.case_notes,
        }
        if name not in tools:
            raise ToolError(f"there is no tool called {name}")
        try:
            inspect.signature(tools[name]).bind(**tool_input)
        except TypeError as error:
            raise ToolError(f"{name} got arguments it doesn't take: {error}") from error
        return tools[name](**tool_input)

    def in_scope(self, case: str) -> None:
        """Refuse a case outside this job, so text in a document or a note can't steer a tool to
        another customer."""
        if case not in self.cases:
            raise ToolError(f"{case!r} is outside this job; the tools reach only its own cases")

    def remember(self, ids: list[str], output: Any) -> None:
        """Keep what a tool said about each id, as plain values and as the JSON Claude saw."""
        text = readable(output) + "\n" + json.dumps(output, ensure_ascii=False)
        for name in dict.fromkeys(ids):
            if name:
                self.seen.setdefault(name, []).append(text)

    def read_handbook(self, section: str) -> dict[str, Any]:
        number = section_number(section)
        if number not in self.records.sections:
            raise ToolError(
                f"no section {section!r}; sections are {', '.join(self.records.sections)}"
            )
        heading, text = self.records.sections[number]
        return {"section": number, "heading": heading, "text": text}

    def _application(self, case: str) -> tuple[Application, Path]:
        found = self.records.application(case)
        if found is None:
            raise ToolError(f"the onboarding system has no case {case!r}")
        return found

    def lookup_case(self, case: str) -> dict[str, Any]:
        self.in_scope(case)
        application, _ = self._application(case)
        output = {
            "case": case,
            "received": application.received.isoformat(),
            "form": application.form,
            "documents": [
                {
                    "document": row.document,
                    "purpose": row.purpose,
                    "declared_type": row.declared_type,
                    "received": row.received,
                }
                for row in self.records.documents_of(case)
            ],
        }
        self.remember([case], output)
        return output

    def read_scan(self, document: str) -> dict[str, Any]:
        row = self.records.document(document)
        if row is None:
            raise ToolError(f"the onboarding system has no document {document!r}")
        self.in_scope(row.case)
        application, folder = self._application(row.case)
        rules = scan_rules(self.rules)
        doc = SubmittedDocument(row.document, row.purpose, row.declared_type, row.file, row.page)
        holder = CaseResult(row.case, application.received)
        failures = len(self.calls.failures)
        read = recipe.read_document(self.client, self.calls, doc, row.case, rules, folder, holder)
        if len(self.calls.failures) > failures:
            self.sie_problems.append(f"read_scan {document}: {read.problem}")
        ctx = recipe.Context(application.form, application.received, self.policy, rules)
        checks = recipe.IDENTITY_CHECKS if row.purpose == "identity" else recipe.ADDRESS_CHECKS
        compared = []
        for name, check in checks.items():
            status, detail = (recipe.NOT_MET, read.problem) if read.problem else check(read, ctx)
            compared.append({"check": name, "status": status, "detail": detail})
        output = {
            "document": document,
            "case": row.case,
            "purpose": row.purpose,
            "declared_type": row.declared_type,
            "received": row.received,
            "problem": read.problem,
            "text": recipe.flatten_markdown(read.text) if read.text else "",
            "fields_read": read.fields,
            "read_two_ways": read.conflicts,
            "issuer_type": read.issuer_type,
            "issuer_problem": read.issuer_problem,
            "compared_with_the_form": compared,
            "note": (
                "OCR turns the page into text. A signature shows at most as a description such "
                "as '(Signature)'; whose it is, and whether it's genuine, needs a person to look."
            ),
        }
        self.remember([document, row.case], output)
        return output

    def run_kyc_check(self, case: str) -> dict[str, Any]:
        self.in_scope(case)
        application, folder = self._application(case)
        failures = len(self.calls.failures)
        result = recipe.process_case(
            self.client, self.calls, application, self.policy, self.rules, folder
        )
        output = kyc_output(result)
        if len(self.calls.failures) > failures:
            problem = f"run_kyc_check {case}: an SIE call failed, so this result is incomplete"
            self.sie_problems.append(problem)
            output["warning"] = problem
        self.kyc[case] = result
        self.kyc_all.setdefault(case, []).append(result)
        self.remember([case, *(r.document for r in result.requirements)], output)
        return output

    def find_accounts(self, product: str, opened_from: str, opened_to: str) -> dict[str, Any]:
        try:
            start, end = date.fromisoformat(opened_from), date.fromisoformat(opened_to)
        except ValueError as error:
            raise ToolError("opened_from and opened_to are dates like 2025-01-31") from error
        rows = [
            vars(account)
            for account in self.records.accounts.values()
            if account.case in self.cases
            and product in ("any", account.product)
            and start <= date.fromisoformat(account.opened) <= end
        ]
        output = {"accounts": rows, "count": len(rows)}
        for row in rows:
            self.remember([row["account"], row["case"]], row)
        return output

    def list_documents(self, cases: list[str], declared_type: str) -> dict[str, Any]:
        for case in cases:
            self.in_scope(case)
        wanted = set(cases) or set(self.cases)
        rows = [
            {
                "case": row.case,
                "document": row.document,
                "purpose": row.purpose,
                "declared_type": row.declared_type,
                "received": row.received,
            }
            for row in self.records.documents
            if row.data_dir == self.records.systems[0][1]
            and row.case in wanted
            and (not declared_type or row.declared_type == declared_type)
        ]
        for row in rows:
            self.remember([row["document"], row["case"]], row)
        return {"documents": rows, "count": len(rows)}

    def case_notes(self, cases: list[str]) -> dict[str, Any]:
        for case in cases:
            self.in_scope(case)
        wanted = set(cases) or set(self.cases)
        rows = [vars(n) for n in self.records.notes.values() if n.case in wanted]
        for row in rows:
            self.remember([row["note"], row["case"]], row)
        return {"notes": rows, "count": len(rows)}


def job_a_cases(case: str) -> frozenset[str]:
    """Job a's tools reach the one case the analyst asks about."""
    return frozenset([case])


def job_b_cases(records: Records, rules: dict[str, Any]) -> frozenset[str]:
    """Job b's tools reach the cases of accounts the register shows opened in the regulator's
    year, of any product, and nothing else."""
    year = str(rules["agent"]["regulator"]["year"])
    return frozenset(a.case for a in records.accounts.values() if a.opened.startswith(year + "-"))


# --- Tool schemas ------------------------------------------------------------------------------


def object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


STRING = {"type": "string"}
STRINGS = {"type": "array", "items": STRING}
CITATIONS = {"type": "array", "items": object_schema({"source": STRING, "quote": STRING})}


TOOL_TEXT = {
    "read_handbook": "One section of the CDD handbook, by its number, such as 3.2.",
    "lookup_case": (
        "A case in the onboarding system: the form as the customer filled it, the date it was "
        "received, and each uploaded document with the type the customer declared."
    ),
    "read_scan": (
        "Read one uploaded document's scan: OCR text, the fields found next to their labels, the "
        "issuer's document type from the bank's issuer list, and the workflow's own checks of "
        "those fields against the case's form."
    ),
    "run_kyc_check": (
        "Run the bank's automated KYC workflow on one case, against policy.yaml as it is today: "
        "every requirement with its status and detail, what to ask the customer for, and what "
        "the analyst looks at."
    ),
    "find_accounts": (
        "Accounts in the account register opened between two dates (inclusive), for product "
        "personal, business or any."
    ),
    "list_documents": (
        "Uploaded documents from the onboarding system for these cases (empty for every case), "
        "with the type the customer declared and the date each arrived. declared_type filters on "
        "the declared type; empty for any."
    ),
    "case_notes": "The onboarding team's free-text notes on these cases (empty for every case).",
}
TOOL_INPUTS: dict[str, dict[str, Any]] = {
    "read_handbook": {"section": STRING},
    "lookup_case": {"case": STRING},
    "read_scan": {"document": STRING},
    "run_kyc_check": {"case": STRING},
    "find_accounts": {
        "product": {"type": "string", "enum": ["personal", "business", "any"]},
        "opened_from": STRING,
        "opened_to": STRING,
    },
    "list_documents": {"cases": STRINGS, "declared_type": STRING},
    "case_notes": {"cases": STRINGS},
}
JOB_TOOLS = {
    "a": ["read_handbook", "lookup_case", "read_scan", "run_kyc_check"],
    "b": [*TOOL_TEXT],
}


def lookup_tools(job: str) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": TOOL_TEXT[name],
            "strict": True,
            "input_schema": object_schema(TOOL_INPUTS[name]),
        }
        for name in JOB_TOOLS[job]
    ]


def submit_answer_tool() -> dict[str, Any]:
    claim = object_schema({"case": STRING, "requirement": STRING, "status": STRING})
    ask = object_schema({"requirement": STRING, "text": STRING})
    waived = object_schema({"requirement": STRING, "analyst_quote": STRING})
    return {
        "name": "submit_answer",
        "description": "Answer the analyst's question. The code checks the answer before the "
        "analyst sees it, and may send it back once.",
        "strict": True,
        "input_schema": object_schema(
            {
                "answer": STRING,
                "citations": CITATIONS,
                "workflow_claims": {"type": "array", "items": claim},
                "email": STRING,
                "email_asks": {"type": "array", "items": ask},
                "waived": {"type": "array", "items": waived},
            }
        ),
    }


def submit_table_tool(outcomes: list[str]) -> dict[str, Any]:
    row = object_schema(
        {
            "account": STRING,
            "case": STRING,
            "document": STRING,
            "outcome": {"type": "string", "enum": outcomes},
            "later_document": STRING,
            "evidence": CITATIONS,
        }
    )
    return {
        "name": "submit_table",
        "description": "Hand in the table for the MLRO, one row per account. The code checks "
        "every row and the table against the register, and may send it back once.",
        "strict": True,
        "input_schema": object_schema({"summary": STRING, "rows": {"type": "array", "items": row}}),
    }


# --- Checks in code ----------------------------------------------------------------------------


def plain(text: str) -> str:
    """Text for comparing a quote: no markdown marks, one kind of quote and dash, single spaces."""
    text = re.sub(r"[`*\\]", "", text).translate(SAME_CHARACTER)
    return re.sub(r"\s+", " ", text).strip().casefold()


def source_text(source: str, box: Toolbox) -> str | None:
    """The text a quote from `source` must be in, or None when there's no such source."""
    records = box.records
    number = section_number(source)
    if number in records.sections:
        heading, text = records.sections[number]
        return f"{heading}\n{text}"
    if source in records.notes:
        return records.notes[source].text
    if source in records.accounts:
        account = records.accounts[source]
        return "\n".join(
            [readable(vars(account)), " ".join(vars(account).values()), json.dumps(vars(account))]
        )
    if source in records.cases() or records.document(source) is not None:
        row = records.document(source)
        listed = readable(vars(row)) if row else ""
        return "\n".join([listed, *box.seen.get(source, [])])
    return None


def check_citations(citations: list[dict[str, str]], box: Toolbox) -> list[str]:
    problems = []
    least = box.rules["agent"]["min_quote_words"]
    for citation in citations:
        source, quote = citation["source"], citation["quote"]
        whole = source_text(source, box)
        if whole is None:
            problems.append(f"cites a source that doesn't exist: {source!r}")
        elif len(plain(quote).split()) < least:
            problems.append(f"the quote from {source} is too short to show anything: {quote!r}")
        elif plain(quote) not in plain(whole):
            problems.append(f"the quote isn't word for word in {source}: {quote!r}")
    return problems


def check_ids(text: str, box: Toolbox) -> list[str]:
    """Every case, document, note, account and handbook section the text names exists."""
    patterns = box.rules["agent"]["ids"]
    records = box.records
    exists: dict[str, Callable[[str], bool]] = {
        "case": lambda name: name in records.cases(),
        "document": lambda name: records.document(name) is not None,
        "note": lambda name: name in records.notes,
        "account": lambda name: name in records.accounts,
        "section": lambda name: name in records.sections,
    }
    problems = []
    for kind, pattern in patterns.items():
        for match in re.finditer(pattern, text):
            name = match.group(1) if match.groups() else match.group(0)
            if not exists[kind](name):
                problems.append(f"names {kind} {name}, which doesn't exist")
    return list(dict.fromkeys(problems))


def check_claims(claims: list[dict[str, str]], box: Toolbox) -> list[str]:
    """Each claim about the workflow is what run_kyc_check returned for that case in this job."""
    problems = []
    for claim in claims:
        case, requirement, status = claim["case"], claim["requirement"], claim["status"]
        results = box.kyc_all.get(case, [])
        if not results:
            problems.append(f"claims the KYC check said something about {case}, but it wasn't run")
            continue
        said = {(r.requirement, r.status) for result in results for r in result.requirements}
        said |= {("status", result.status) for result in results}
        if (requirement, status) not in said:
            problems.append(
                f"claims the KYC check returned {requirement} {status} for {case}; it didn't"
            )
    return problems


def sentences(text: str) -> list[str]:
    return [part for part in re.split(r"(?<=[.!?;])\s+|\n+", text) if part.strip()]


def names_section(section: str, text: str) -> bool:
    """The section number on its own, not inside 14.2 or 4.21."""
    return re.search(rf"(?<![\d.]){re.escape(section)}(?!\.?\d)", text) is not None


def address_talk(text: str, case: str, box: Toolbox) -> str:
    """The first words in the text about the case's proof of address, or "": one of its address
    document ids, the name of a type it was declared as, the words in `address_words`, or a word
    saying something counts. A negation doesn't take a sentence out: "it doesn't count" talks
    about the document as much as "it counts" does."""
    settings, flat = box.rules["agent"], plain(text)
    documents = address_documents(case, box)
    declared = {row.declared_type for row in documents}
    phrases = [row.document for row in documents]
    phrases += [phrase for phrase, kind in settings["document_words"].items() if kind in declared]
    for phrase in phrases:
        found = re.search(rf"(?<![a-z0-9-]){re.escape(phrase.casefold())}s?(?![a-z0-9])", flat)
        if found:
            return found.group(0)
    for pattern in (settings["address_words"], settings["counts_words"]):
        found = re.search(pattern, flat)
        if found:
            return found.group(0)
    return ""


def check_disagreement(answer: dict[str, Any], box: Toolbox) -> list[str]:
    """When this job's latest KYC check on a case shows its proof of address missing, any answer
    or email that talks about that proof of address must name the handbook section, the sign-off
    and the workflow's result."""
    settings = box.rules["agent"]
    text = f"{answer['answer']}\n{answer['email']}"
    claimed = {(c["case"], c["requirement"], c["status"]) for c in answer["workflow_claims"]}
    section = settings["exception_section"]
    problems = []
    for case, result in box.kyc.items():
        missing = any(
            r.requirement == "address_document" and r.status == recipe.MISSING
            for r in result.requirements
        )
        talk = address_talk(text, case, box) if missing else ""
        if not talk:
            continue
        if not names_section(section, text) or not re.search(
            settings["sign_off_words"], plain(text)
        ):
            problems.append(
                f"talks about {talk!r} while the KYC check shows {case}'s address_document "
                f"missing, without naming section {section} and the sign-off"
            )
        if (case, "address_document", recipe.MISSING) not in claimed:
            problems.append(
                f"talks about {talk!r} while the KYC check shows {case}'s address_document "
                "missing, and leaves that result out of workflow_claims"
            )
    return problems


def sign_off_problem(quote: str, analyst_turns: list[str], rules: dict[str, Any]) -> str:
    """Why the analyst's quoted words don't sign anything off, or "" when they do. The quote must
    be in what the analyst typed, hold the sign-off words, and come from sentences that are
    neither negated ("don't sign off ... yet") nor a question ("should I sign off ...?")."""
    settings = rules["agent"]
    wanted = plain(quote)
    said = [plain(part) for turn in analyst_turns for part in sentences(turn)]
    joined = " ".join(said)
    if not wanted or wanted not in joined:
        return " on words the analyst didn't say"
    if not re.search(settings["sign_off_words"], wanted):
        return ", but the analyst's words aren't a sign-off"
    spans, offset = [], 0
    for sentence in said:
        spans.append((offset, offset + len(sentence), sentence))
        offset += len(sentence) + 1
    for found in re.finditer(re.escape(wanted), joined):
        around = [text for a, b, text in spans if a < found.end() and found.start() < b]
        if not any(
            re.search(settings["negation"], text)
            or text.rstrip().endswith("?")
            or re.search(settings["question_start"], text)
            for text in around
        ):
            return ""
    return ", but the analyst's sentence is negated or a question"


def document_types(text: str, rules: dict[str, Any]) -> list[tuple[str, str]]:
    """(phrase, type) for each document type the text names, longest phrase first."""
    flat = f" {plain(text)} "
    found = []
    for phrase, kind in sorted(rules["agent"]["document_words"].items(), key=lambda p: -len(p[0])):
        pattern = rf"(?<![a-z]){re.escape(phrase)}s?(?![a-z])"
        if re.search(pattern, flat):
            found.append((phrase, kind))
            flat = re.sub(pattern, " ", flat)
    return found


def check_email(
    answer: dict[str, Any], case: str, analyst_turns: list[str], box: Toolbox
) -> list[str]:
    """The email asks for everything the workflow asks for that the analyst didn't sign off,
    names only accepted document types, and names no other case, customer or account."""
    email = answer["email"]
    if not email.strip():
        return []
    settings, policy = box.rules["agent"], box.policy
    result = box.kyc.get(case)
    if result is None:
        return [f"an email for {case} without running the KYC check on it in this conversation"]
    problems = []
    asks = {r.requirement for r in result.unmet if r.ask}
    waived = set()
    for item in answer["waived"]:
        requirement, quote = item["requirement"], item["analyst_quote"]
        if requirement not in settings["waivable"]:
            problems.append(
                f"waives {requirement}, which the handbook doesn't let the analyst sign off"
            )
        elif why := sign_off_problem(quote, analyst_turns, box.rules):
            problems.append(f"waives {requirement}{why}: {quote!r}")
        else:
            waived.add(requirement)
    exception, follow_up = settings["exception_section"], settings["follow_up_section"]
    cited = {section_number(c["source"]) for c in answer["citations"]}
    if waived and exception not in cited:
        problems.append(f"signs off {', '.join(sorted(waived))} without citing section {exception}")
    listed = {item["requirement"] for item in answer["email_asks"]}
    for requirement in sorted(asks - waived - listed):
        problems.append(f"the email leaves out what the KYC check asks for: {requirement}")
    for item in answer["email_asks"]:
        requirement, text = item["requirement"], item["text"]
        if requirement in waived:
            problems.append(f"the email asks for {requirement}, which the analyst signed off")
        elif requirement not in asks and requirement not in box.records.sections:
            problems.append(
                f"the email asks for {requirement}, which neither the KYC check nor the handbook "
                "asks for"
            )
        if not plain(text) or plain(text) not in plain(email):
            problems.append(f"an ask isn't word for word in the email: {text!r}")
    if "address_document" in waived and follow_up not in listed:
        problems.append(
            "the address document is signed off, but the email doesn't ask for the section "
            f"{follow_up} follow-up"
        )
    allowed = {*policy["identity"]["accepted"], *policy["address"]["accepted"]}
    allowed |= set(settings["handbook_types"])
    for phrase, kind in document_types(email, box.rules):
        if kind not in allowed:
            problems.append(f"the email names a document type we don't accept: {phrase!r}")
    patterns = settings["ids"]
    for kind in ("case", "document", "note", "account"):
        for match in re.finditer(patterns[kind], email):
            if not match.group(0).startswith(case):
                problems.append(f"the email names another {kind}: {match.group(0)}")
    own = set(box.records.surnames().get(case, []))
    words = set(re.findall(r"[a-z]+", plain(email)))
    for other, surname in box.records.surnames().items():
        if other != case and surname and set(surname) <= words and not set(surname) <= own:
            problems.append(f"the email names another customer: {' '.join(surname)} ({other})")
    return list(dict.fromkeys(problems))


def check_answer(
    answer: dict[str, Any], case: str, analyst_turns: list[str], box: Toolbox
) -> list[str]:
    """Every problem with one answer in the conversation. Empty means every check passed."""
    text = f"{answer['answer']}\n{answer['email']}"
    problems = check_citations(answer["citations"], box)
    problems += check_ids(text, box)
    problems += check_claims(answer["workflow_claims"], box)
    problems += check_disagreement(answer, box)
    problems += check_email(answer, case, analyst_turns, box)
    if not answer["answer"].strip():
        problems.append("the answer is empty")
    return problems


# --- Job b: the rows, and completeness from sources the agent didn't write ----------------------


def address_documents(case: str, box: Toolbox) -> list[DocumentRow]:
    return [row for row in box.records.documents_of(case) if row.purpose == "address"]


def check_row(row: dict[str, Any], box: Toolbox) -> list[str]:
    """Every problem with one row of the regulator's table."""
    records, name = box.records, row["account"]
    account = records.accounts.get(name)
    if account is None:
        return [f"{name}: no such account in the register"]
    problems = []
    if account.case != row["case"]:
        problems.append(f"{name}: the register gives case {account.case}, not {row['case']}")
    document = records.document(row["document"])
    if document is None or document.case != account.case or document.purpose != "address":
        problems.append(f"{name}: {row['document']} isn't an address document of {account.case}")
        return problems
    if not row["evidence"]:
        problems.append(f"{name}: no evidence")
    problems += [f"{name}: {p}" for p in check_citations(row["evidence"], box)]
    later = [
        doc
        for doc in address_documents(account.case, box)
        if doc.received > document.received and doc.document != document.document
    ]
    outcome = row["outcome"]
    if account.status in ("restricted", "closed") and outcome != "restricted_or_closed":
        problems.append(f"{name}: the register shows it {account.status}, not {outcome}")
    if outcome == "replaced":
        problems += [f"{name}: {p}" for p in check_replaced(row, account.case, later, box)]
    elif outcome == "exception_approved":
        notes = [
            e["source"]
            for e in row["evidence"]
            if e["source"] in records.notes and records.notes[e["source"]].case == account.case
        ]
        if not notes:
            problems.append(f"{name}: exception_approved cites no note on {account.case}")
        problems += [f"{name}: {p}" for p in check_not_replaced(account.case, later, box)]
    elif outcome == "restricted_or_closed":
        if account.status not in ("restricted", "closed"):
            problems.append(f"{name}: the register shows it {account.status}")
    elif outcome == "nothing_recorded":
        if later:
            problems.append(
                f"{name}: nothing_recorded, but {later[0].document} arrived on {later[0].received}"
            )
        pattern = box.rules["agent"]["exception_approved_words"]
        approved = [
            note.note
            for note in records.notes.values()
            if note.case == account.case and re.search(pattern, plain(note.text))
        ]
        if approved:
            problems.append(
                f"{name}: nothing_recorded, but note {approved[0]} on {account.case} records an "
                "approved exception"
            )
    return problems


def check_not_replaced(case: str, later: list[DocumentRow], box: Toolbox) -> list[str]:
    """`exception_approved` doesn't stand when a later address document met every address
    requirement: that row is `replaced`. With a later document and no KYC check in this job,
    nothing rules that out."""
    if not later:
        return []
    if case not in box.kyc:
        return [
            f"exception_approved, but {later[0].document} arrived later and the KYC check wasn't "
            f"run on {case} in this job to rule out replaced"
        ]
    met = [
        doc.document
        for doc in later
        if not check_replaced({"later_document": doc.document}, case, later, box)
    ]
    if met:
        return [
            f"exception_approved, but the KYC check met every address requirement on the later "
            f"{met[0]}, so the row is replaced"
        ]
    return []


def check_replaced(
    row: dict[str, Any], case: str, later: list[DocumentRow], box: Toolbox
) -> list[str]:
    """`replaced` names a later address document, and this job's KYC check met every address
    requirement on it."""
    named = row["later_document"]
    if named not in {doc.document for doc in later}:
        return [f"replaced names {named or 'no document'}, which isn't a later address document"]
    result = box.kyc.get(case)
    if result is None:
        return [f"replaced, but the KYC check wasn't run on {case} in this job"]
    wanted = set(box.policy["address"]["requirements"])
    met = {
        r.requirement for r in result.requirements if r.document == named and r.status == recipe.MET
    }
    if not wanted <= met:
        short = ", ".join(sorted(wanted - met))
        return [f"replaced, but the KYC check didn't meet {short} on {named}"]
    return []


@dataclass
class SweptDocument:
    account: str
    case: str
    document: str
    declared_type: str
    issuer: str
    issuer_type: str
    detail: str  # why the type isn't known, when it isn't


@dataclass
class Completeness:
    expected: dict[str, str] = field(default_factory=dict)  # account -> why it belongs
    swept: list[SweptDocument] = field(default_factory=list)
    sie_failed: bool = False

    @property
    def not_recognised(self) -> list[SweptDocument]:
        return [doc for doc in self.swept if not doc.issuer_type]


def denominator(box: Toolbox) -> list[Account]:
    """Personal accounts opened in the regulator's year, from the register: the code's count."""
    settings = box.rules["agent"]["regulator"]
    year = str(settings["year"])
    return [
        account
        for account in box.records.accounts.values()
        if account.product == settings["product"] and account.opened.startswith(year + "-")
    ]


def sweep(box: Toolbox) -> Completeness:
    """Read every address document of the year's personal accounts, whatever the customer called
    it, and keep the accounts whose document was declared, or turns out to be, the type asked
    about. This calls read_document and issuer_type directly, not sort_documents, which would
    refuse a declared mobile phone bill by its label without reading the page."""
    wanted = box.rules["agent"]["regulator"]["document_type"]
    found = Completeness()
    for account in denominator(box):
        application, folder = box._application(account.case)
        for row in address_documents(account.case, box):
            doc = SubmittedDocument(
                row.document, row.purpose, row.declared_type, row.file, row.page
            )
            holder = CaseResult(account.case, application.received)
            failures = len(box.calls.failures)
            read = recipe.read_document(
                box.client, box.calls, doc, account.case, box.rules, folder, holder
            )
            found.sie_failed |= len(box.calls.failures) > failures
            issuer = read.fields.get("issuer", "")
            detail = read.problem or read.issuer_problem
            if not read.problem and not issuer:
                detail = "no issuer read"
            kind = "" if read.problem else read.issuer_type
            found.swept.append(
                SweptDocument(
                    account.account,
                    account.case,
                    row.document,
                    row.declared_type,
                    issuer,
                    kind,
                    detail,
                )
            )
            reasons = []
            if row.declared_type == wanted:
                reasons.append(f"{row.document} declared {wanted}")
            if kind == wanted:
                reasons.append(f"{row.document}: '{issuer}' issues {wanted}")
            if reasons:
                previous = found.expected.get(account.account)
                found.expected[account.account] = "; ".join(filter(None, [previous, *reasons]))
    return found


def table_review(
    rows: list[dict[str, Any]], box: Toolbox, complete: Completeness
) -> dict[str, Any]:
    """What the checks found in a table: row problems, accounts missing, accounts extra."""
    listed = [row["account"] for row in rows]
    problems = [
        f"{name}: listed twice" for name in sorted({n for n in listed if listed.count(n) > 1})
    ]
    for row in rows:
        problems += check_row(row, box)
    missing = {name: why for name, why in complete.expected.items() if name not in listed}
    extra = sorted(set(listed) - set(complete.expected))
    return {"problems": problems, "missing": missing, "extra": extra}


def review_message(review: dict[str, Any], box: Toolbox) -> str:
    lines = ["The checks found these. Fix them and call submit_table again with the whole table."]
    lines += [f"- {problem}" for problem in review["problems"]]
    for name, why in review["missing"].items():
        lines.append(
            f"- Missing: {name} (case {box.records.accounts[name].case}). The completeness check "
            f"read every {box.rules['agent']['regulator']['year']} address scan and found: {why}."
        )
    for name in review["extra"]:
        lines.append(
            f"- Not asked for: {name} isn't a personal account opened in the year asked about "
            "with such a document."
        )
    return "\n".join(lines)


def regulator_status(
    stopped: str, ran_out: bool, review: dict[str, Any] | None, first_missing: list[str],
    complete: Completeness, sie_problems: list[str],
) -> str:  # fmt: skip
    """The job's status. Each status needs positive evidence, and the worst one found wins."""
    if ran_out:
        return "out_of_steps"
    if stopped or review is None:
        return "no_answer"
    if sie_problems or complete.sie_failed or complete.not_recognised:
        return "needs_person"
    if review["problems"] or review["extra"]:
        return "check_failed"
    if review["missing"]:
        return "gap_remains"
    if first_missing:
        return "complete_after_check"
    return "complete"


def turn_status(
    stopped: str, ran_out: bool, problems: list[str] | None, sie_problems: list[str]
) -> str:
    if ran_out:
        return "out_of_steps"
    if stopped or problems is None:
        return "no_answer"
    if sie_problems:
        return "needs_person"
    return "check_failed" if problems else "answer_checked"


# --- The agent loop ----------------------------------------------------------------------------


TOKEN_KINDS = ("input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens")
PRICE_KEYS = {
    "input_tokens": "input",
    "cache_write_tokens": "cache_write",
    "cache_read_tokens": "cache_read",
    "output_tokens": "output",
}


@dataclass
class Usage:
    """Tokens per model that answered: a server-side fallback can answer on another model, and
    its tokens are priced as that model's."""

    by_model: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, usage: Any, model: str) -> None:
        tokens = self.by_model.setdefault(model, dict.fromkeys(TOKEN_KINDS, 0))
        tokens["input_tokens"] += usage.input_tokens
        tokens["cache_write_tokens"] += usage.cache_creation_input_tokens or 0
        tokens["cache_read_tokens"] += usage.cache_read_input_tokens or 0
        tokens["output_tokens"] += usage.output_tokens

    def total(self, kind: str) -> int:
        return sum(tokens[kind] for tokens in self.by_model.values())

    def unpriced(self, prices: dict[str, dict[str, float]]) -> list[str]:
        return sorted(model for model in self.by_model if model not in prices)

    def usd(self, prices: dict[str, dict[str, float]]) -> float:
        """Dollars for the models `prices` lists; `unpriced` names the others."""
        return (
            sum(
                tokens[kind] * prices[model][PRICE_KEYS[kind]]
                for model, tokens in self.by_model.items()
                if model in prices
                for kind in TOKEN_KINDS
            )
            / 1_000_000
        )


@dataclass
class Round:
    """One stretch of the loop: until the agent submits something the code accepts, or stops."""

    submitted: dict[str, Any] | None = None
    submit_id: str = ""
    problems: list[str] | None = None  # what the checks found in the accepted submission
    first_review: list[str] = field(default_factory=list)
    revisions: int = 0
    tool_calls: int = 0
    ran_out: bool = False
    stopped: str = ""
    pending: list[dict[str, Any]] = field(default_factory=list)  # tool results to send next
    log: list[dict[str, Any]] = field(default_factory=list)


def make_claude() -> Any:
    """The Anthropic client. Imported here, so selfcheck.py and --help don't need the group."""
    import anthropic
    from dotenv import load_dotenv

    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Stopped: set ANTHROPIC_API_KEY, in .env or the environment.")
    return anthropic.Anthropic()


def ask_claude(
    claude: Any,
    system: str,
    tools: list[dict[str, Any]],
    messages: list[Any],
    rules: dict[str, Any],
) -> Any:
    settings = rules["agent"]
    extra: dict[str, Any] = {}
    if settings["fallbacks"]:
        extra = {"betas": [settings["fallback_beta"]], "fallbacks": settings["fallbacks"]}
    return claude.beta.messages.create(
        model=settings["model"],
        max_tokens=settings["max_tokens"],
        system=system,
        tools=tools,
        messages=messages,
        output_config={"effort": settings["effort"]},
        cache_control={"type": "ephemeral"},
        **extra,
    )


def transient_claude_error(error: Exception) -> bool:
    """A Claude API failure that may pass if tried later: the turn or job ends with no answer and
    the reason. Anything else stops the run."""
    import anthropic

    transient = (
        anthropic.APIConnectionError,
        anthropic.RateLimitError,
        anthropic.InternalServerError,
    )
    if isinstance(error, transient):  # APITimeoutError is an APIConnectionError
        return True
    return isinstance(error, anthropic.APIStatusError) and error.status_code in (408, 409)


Judge = Callable[[dict[str, Any]], list[str]]


def run_round(
    claude: Any,
    box: Toolbox,
    system: str,
    tools: list[dict[str, Any]],
    messages: list[Any],
    judge: Judge,
    usage: Usage,
    submit_name: str,
    on_review: Callable[[dict[str, Any], list[str]], str] | None = None,
) -> Round:
    """Let Claude call tools until it submits. The judge checks each submission; the first one
    with problems goes back once with what was found. The caller sends `pending` next."""
    settings = box.rules["agent"]
    budget = settings["max_tool_calls"]
    result = Round()
    nudged = False
    for _ in range(settings["max_rounds"]):
        try:
            response = ask_claude(claude, system, tools, messages, box.rules)
        except Exception as error:
            if not transient_claude_error(error):
                raise
            result.stopped = f"the Claude API failed: {type(error).__name__}: {error}"
            return result
        usage.add(response.usage, response.model)
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
            if nudged:
                result.stopped = f"Claude ended its turn without calling {submit_name}"
                return result
            nudged = True
            text = "\n".join(b.text for b in response.content if b.type == "text")
            result.log.append({"said": text})
            messages.append({"role": "user", "content": f"Call {submit_name} now."})
            continue
        replies: list[dict[str, Any]] = []
        accepted: Any = None
        for use in sorted(uses, key=lambda use: use.name == submit_name):
            block: dict[str, Any] = {"type": "tool_result", "tool_use_id": use.id}
            if use.name == submit_name:
                if accepted is not None:
                    block.update(content="Only one submission per response.", is_error=True)
                    replies.append(block)
                    continue
                problems = judge(dict(use.input))
                result.log.append({"tool": use.name, "input": use.input, "problems": problems})
                if problems and result.revisions < settings["max_revisions"]:
                    result.revisions += 1
                    result.first_review = problems
                    budget += settings["max_tool_calls_after_review"]
                    message = (
                        on_review(dict(use.input), problems)
                        if on_review
                        else "The checks found these. Fix them and submit again.\n"
                        + "\n".join(f"- {p}" for p in problems)
                    )
                    left = budget - result.tool_calls
                    message += f"\nYou have {left} more tool calls for this."
                    block.update(content=message, is_error=True)
                    replies.append(block)
                    continue
                accepted = use
                result.submitted, result.submit_id, result.problems = (
                    dict(use.input),
                    use.id,
                    problems,
                )
                continue
            entry: dict[str, Any] = {"tool": use.name, "input": use.input}
            if result.tool_calls >= budget:
                result.ran_out = True
                output = f"No tool calls left. Call {submit_name} now with what you have."
                block["is_error"] = True
            else:
                result.tool_calls += 1
                try:
                    output = json.dumps(box.run(use.name, dict(use.input)), ensure_ascii=False)
                except ToolError as error:
                    output = str(error)
                    block["is_error"] = True
            entry.update(output=output, is_error=block.get("is_error", False))
            result.log.append(entry)
            block["content"] = output
            replies.append(block)
        if accepted is not None:
            result.ran_out = False
            result.pending = replies
            return result
        messages.append({"role": "user", "content": replies})
    result.ran_out = True
    result.stopped = "reached the limit on requests to Claude"
    return result


# --- Job a: the analyst at the screen ----------------------------------------------------------


def section_list(records: Records) -> str:
    return "; ".join(f"{number} {heading}" for number, (heading, _) in records.sections.items())


@dataclass
class JobReport:
    job: str
    status: str
    detail: dict[str, Any]
    usage: Usage
    sie_calls: int
    tool_calls: int
    seconds: float
    transcript: list[dict[str, Any]]


def run_conversation(
    claude: Any, box: Toolbox, case: str, questions: Callable[[int], str | None]
) -> JobReport:
    """Job a: one question at a time; every answer is checked before the analyst reads it."""
    started, before = time.monotonic(), box.calls.total
    usage = Usage()
    system = box.rules["agent"]["system_turn"].replace("{sections}", section_list(box.records))
    tools = [*lookup_tools("a"), submit_answer_tool()]
    messages: list[Any] = []
    pending: list[dict[str, Any]] = []
    asked: list[str] = []
    turns: list[dict[str, Any]] = []
    transcript: list[dict[str, Any]] = []
    while (question := questions(len(asked))) is not None:
        asked.append(question)
        problems_before = len(box.sie_problems)
        messages.append({"role": "user", "content": [*pending, {"type": "text", "text": question}]})
        judge = functools.partial(check_answer, case=case, analyst_turns=list(asked), box=box)
        result = run_round(claude, box, system, tools, messages, judge, usage, "submit_answer")
        sie = box.sie_problems[problems_before:]
        status = turn_status(result.stopped, result.ran_out, result.problems, sie)
        pending = [
            *result.pending,
            {
                "type": "tool_result",
                "tool_use_id": result.submit_id,
                "content": "Shown to the analyst.",
            },
        ]
        turns.append(
            {
                "question": question,
                "status": status,
                "stopped": result.stopped,
                "answer": result.submitted,
                "problems": result.problems or [],
                "first_review": result.first_review,
                "revisions": result.revisions,
                "tool_calls": [
                    {"tool": e["tool"], "input": e["input"], "is_error": e.get("is_error", False)}
                    for e in result.log
                    if "tool" in e and e["tool"] != "submit_answer"
                ],
                "sie_problems": sie,
                "tool_calls_run": result.tool_calls,
            }
        )
        transcript.append({"question": question, "log": result.log})
        print(f"  turn {len(asked)}: {status} ({result.tool_calls} tool calls)")
        if result.submitted and turns[-1]["answer"]:
            print_answer(turns[-1])
        if result.submitted is None:
            break  # the conversation can't go on from here
    statuses = [turn["status"] for turn in turns]
    worst = next(
        (s for s in reversed(box.rules["agent"]["statuses"]["turn"]) if s in statuses), "no_answer"
    )
    return JobReport(
        "a",
        worst,
        {"case": case, "turns": turns},
        usage,
        box.calls.total - before,
        sum(turn["tool_calls_run"] for turn in turns),
        time.monotonic() - started,
        transcript,
    )


def print_answer(turn: dict[str, Any]) -> None:
    answer = turn["answer"]
    print("\n" + answer["answer"].strip())
    if answer["email"].strip():
        print("\n--- email ---\n" + answer["email"].strip())
    for problem in turn["problems"]:
        print(f"  ! {problem}")
    print()


# --- Job b: the regulator's letter -------------------------------------------------------------


def run_letter(claude: Any, box: Toolbox, letter: str) -> JobReport:
    started, before = time.monotonic(), box.calls.total
    usage = Usage()
    settings = box.rules["agent"]
    system = settings["system_regulator"].replace("{sections}", section_list(box.records))
    tools = [*lookup_tools("b"), submit_table_tool(settings["outcomes"])]
    messages: list[Any] = [{"role": "user", "content": letter}]
    complete: Completeness | None = None
    reviews: list[dict[str, Any]] = []

    def judge(table: dict[str, Any]) -> list[str]:
        nonlocal complete
        if complete is None:
            complete = sweep(box)
        review = table_review(table["rows"], box, complete)
        reviews.append(review)
        return [
            *review["problems"],
            *(f"missing: {name}" for name in review["missing"]),
            *(f"extra: {name}" for name in review["extra"]),
        ]

    result = run_round(
        claude, box, system, tools, messages, judge, usage, "submit_table",
        on_review=lambda _table, _problems: review_message(reviews[-1], box),
    )  # fmt: skip
    if complete is None:
        complete = sweep(box)  # the analyst gets the code's own list either way
    review = reviews[-1] if result.submitted is not None and reviews else None
    first_missing = sorted(reviews[0]["missing"]) if reviews else []
    status = regulator_status(
        result.stopped, result.ran_out, review, first_missing, complete, box.sie_problems
    )
    rows = list(result.submitted["rows"]) if result.submitted else []
    listed = {row["account"] for row in rows}
    for row in rows:
        row["added_by"] = (
            "agent, after the completeness check" if row["account"] in first_missing else "agent"
        )
    for name in sorted(set(complete.expected) - listed):
        account = box.records.accounts[name]
        rows.append(
            {
                "account": name,
                "case": account.case,
                "document": "",
                "outcome": "",
                "later_document": "",
                "evidence": [],
                "added_by": "completeness check; nothing looked into",
            }
        )
    detail = {
        "summary": result.submitted["summary"] if result.submitted else "",
        "stopped": result.stopped,
        "rows": rows,
        "problems": review["problems"] if review else [],
        "missing_at_first": {n: reviews[0]["missing"][n] for n in first_missing} if reviews else {},
        "missing_now": review["missing"] if review else dict(complete.expected),
        "extra": review["extra"] if review else [],
        "expected": complete.expected,
        "swept": [vars(doc) for doc in complete.swept],
        "denominator": [a.account for a in denominator(box)],
        "revisions": result.revisions,
        "sie_problems": box.sie_problems,
        "tool_calls": [
            {"tool": e["tool"], "input": e["input"], "is_error": e.get("is_error", False)}
            for e in result.log
            if "tool" in e and e["tool"] != "submit_table"
        ],
    }
    calls = result.tool_calls
    print(f"  {status} ({calls} tool calls, {result.revisions} revisions)")
    return JobReport(
        "b", status, detail, usage, box.calls.total - before, calls,
        time.monotonic() - started, [{"letter": letter, "log": result.log}],
    )  # fmt: skip


# --- Outputs -----------------------------------------------------------------------------------


MARKDOWN_MARKS = re.compile(r"([\\`*_\[\]()<>|!#])")


def md_safe(value: Any) -> str:
    """Text from a document, a tool or Claude, safe in a Markdown line or table cell: every mark
    that could make a link, an image, HTML, a heading or a new cell is escaped, and newlines
    become spaces."""
    return MARKDOWN_MARKS.sub(r"\\\1", " ".join(str(value).split()))


def fenced(text: str) -> list[str]:
    """A free-text body (an answer, an email) as a fenced block, its fence longer than any run of
    backticks inside, so nothing in it renders as Markdown."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [fence + "text", text, fence]


def chat_markdown(report: JobReport) -> str:
    detail = report.detail
    lines = [
        f"# {md_safe(detail['case'])}: the analyst's questions",
        "",
        f"**Status:** {md_safe(report.status)}",
        "",
    ]
    for number, turn in enumerate(detail["turns"], start=1):
        lines += [f"## Turn {number}: {md_safe(turn['status'])}", ""]
        lines += [f"> {md_safe(turn['question'])}", ""]
        if turn["stopped"]:
            lines += [f"**Stopped because:** {md_safe(turn['stopped'])}", ""]
        answer = turn["answer"]
        if answer:
            lines += [*fenced(answer["answer"].strip()), ""]
            for claim in answer["workflow_claims"]:
                said = " ".join(md_safe(claim[k]) for k in ("case", "requirement", "status"))
                lines.append(f"- KYC check: {said}")
            for citation in answer["citations"]:
                lines.append(f'- {md_safe(citation["source"])}: "{md_safe(citation["quote"])}"')
            lines.append("")
            if answer["email"].strip():
                lines += ["**Email draft:**", "", *fenced(answer["email"].strip()), ""]
        if turn["problems"]:
            lines += ["**Check these before using the answer:**", ""]
            lines += [f"- {md_safe(p)}" for p in turn["problems"]] + [""]
        if turn["first_review"]:
            lines += ["**The checks sent the first answer back for these:**", ""]
            lines += [f"- {md_safe(p)}" for p in turn["first_review"]] + [""]
        lines += ["**Tool calls:**", ""]
        calls = turn["tool_calls"]
        lines += [f"- {md_safe(c['tool'])} {md_safe(json.dumps(c['input']))}" for c in calls]
        lines += ["- none"] if not calls else []
        lines.append("")
    return "\n".join(lines)


def regulator_markdown(report: JobReport) -> str:
    detail = report.detail
    lines = [
        "# Personal accounts opened in 2025 on a mobile phone bill",
        "",
        "Draft for the MLRO. Each row's evidence was checked in code; whether a note's words "
        "support its outcome is for you to read.",
        "",
        f"**Status:** {md_safe(report.status)}",
        "",
    ]
    if detail["stopped"]:
        lines += [f"**Stopped because:** {md_safe(detail['stopped'])}", ""]
    columns = ["account", "case", "document", "outcome", "later_document", "added_by"]
    lines += [
        "| Account | Case | Document | Outcome | Later document | Added by |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in detail["rows"]:
        lines.append("| " + " | ".join(md_safe(row[key]) for key in columns) + " |")
    lines.append("")
    if detail["summary"]:
        lines += ["**The agent's summary:**", "", *fenced(detail["summary"].strip()), ""]
    if detail["missing_at_first"]:
        lines += ["**Missing from the agent's first table, found by the completeness check:**", ""]
        lines += [
            f"- {md_safe(n)}: {md_safe(why)}" for n, why in detail["missing_at_first"].items()
        ]
        lines.append("")
    if detail["problems"] or detail["extra"]:
        lines += ["**Check these before using the table:**", ""]
        lines += [f"- {md_safe(p)}" for p in detail["problems"]]
        lines += [f"- {md_safe(n)} isn't asked for" for n in detail["extra"]] + [""]
    unknown = [d for d in detail["swept"] if not d["issuer_type"]]
    if unknown:
        lines += ["**Issuer not recognised (a person checks the type):**", ""]
        lines += [
            f"- {md_safe(d['document'])} ({md_safe(d['account'])}): {md_safe(d['detail'])}"
            for d in unknown
        ] + [""]
    lines += ["## Evidence", ""]
    for row in detail["rows"]:
        for item in row["evidence"]:
            lines.append(
                f'- {md_safe(row["account"])} {md_safe(item["source"])}: "{md_safe(item["quote"])}"'
            )
    lines += [
        "",
        "Not checked by code: anything outside the onboarding system (paper files, migrated "
        "accounts), and the policy in force in 2025 (run_kyc_check uses today's policy.yaml).",
        "",
    ]
    return "\n".join(lines)


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: recipe.csv_safe(row.get(key, "")) for key in fields})


def cost_entry(report: JobReport, rules: dict[str, Any]) -> dict[str, Any]:
    prices = rules["agent"]["prices"]
    return {
        "status": report.status,
        "claude_model": rules["agent"]["model"],
        "answered_by": report.usage.by_model,
        **{kind: report.usage.total(kind) for kind in TOKEN_KINDS},
        "claude_usd": round(report.usage.usd(prices), 4),
        "not_priced": report.usage.unpriced(prices),  # no price in rules.yaml: not in claude_usd
        "sie_calls": report.sie_calls,
        "tool_calls": report.tool_calls,
        "seconds": round(report.seconds),
    }


def write_outputs(folder: Path, reports: list[JobReport], rules: dict[str, Any]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for report in reports:
        name = f"job_{report.job}"
        body = {"job": report.job, "status": report.status, **report.detail}
        (folder / f"{name}.json").write_text(
            json.dumps(body, indent=1, default=str), encoding="utf-8"
        )
        (folder / f"transcript_{report.job}.json").write_text(
            json.dumps(report.transcript, indent=1, default=str), encoding="utf-8"
        )
        if report.job == "a":
            case = report.detail["case"]
            (folder / f"chat_{case}.md").write_text(chat_markdown(report), encoding="utf-8")
            emails = [
                t["answer"]["email"]
                for t in report.detail["turns"]
                if t["answer"] and t["answer"]["email"].strip()
            ]
            if emails:
                (folder / f"email_{case}.txt").write_text(
                    emails[-1].strip() + "\n", encoding="utf-8"
                )
        else:
            (folder / "regulator_2025.md").write_text(regulator_markdown(report), encoding="utf-8")
            rows = [
                {
                    **row,
                    "evidence": "; ".join(f"{e['source']}: {e['quote']}" for e in row["evidence"]),
                }
                for row in report.detail["rows"]
            ]
            fields = [
                "account",
                "case",
                "document",
                "outcome",
                "later_document",
                "evidence",
                "added_by",
            ]
            write_csv(folder / "regulator_2025.csv", fields, rows)
            write_csv(folder / "completeness.csv", COMPLETENESS_FIELDS, completeness_rows(report))
    costs = {f"job_{r.job}": cost_entry(r, rules) for r in reports}
    (folder / "cost.json").write_text(json.dumps(costs, indent=1), encoding="utf-8")


COMPLETENESS_FIELDS = [
    "kind",
    "account",
    "case",
    "document",
    "declared_type",
    "issuer",
    "issuer_type",
    "detail",
]


def completeness_rows(report: JobReport) -> list[dict[str, Any]]:
    """expected, found, missing, extra and not_recognised, one row each per account or scan."""
    detail = report.detail
    found = {
        r["account"]: r["added_by"] for r in detail["rows"] if r["added_by"].startswith("agent")
    }
    rows: list[dict[str, Any]] = []
    for name, why in detail["expected"].items():
        rows.append({"kind": "expected", "account": name, "detail": why})
        if name in found:
            rows.append({"kind": "found", "account": name, "detail": f"by the {found[name]}"})
    for name, why in detail["missing_now"].items():
        rows.append({"kind": "missing", "account": name, "detail": why})
    rows += [{"kind": "extra", "account": name} for name in detail["extra"]]
    rows += [{"kind": "not_recognised", **doc} for doc in detail["swept"] if not doc["issuer_type"]]
    return rows


# --- The run -----------------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--job", choices=["a", "b", "all"], default="all", help="which job to run")
    parser.add_argument(
        "--chat", action="store_true", help="type the analyst's questions yourself (job a only)"
    )
    parser.add_argument(
        "--output", type=Path, help="output folder (default: runs/<timestamp>/agent)"
    )
    return parser.parse_args(argv)


def scripted(path: Path) -> tuple[str, list[str]]:
    script = cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))
    return str(script["case"]), [str(turn) for turn in script["turns"]]


def typed(number: int) -> str | None:
    try:
        text = input(f"analyst [{number + 1}] (empty to stop)> ").strip()
    except EOFError:
        return None
    return text or None


def run(args: argparse.Namespace) -> None:
    rules, policy = recipe.load_rules(), recipe.load_policy()
    records = Records()
    claude = make_claude()
    calls = Calls()
    reports: list[JobReport] = []
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") / "agent"
    with recipe.make_client() as client:
        if args.job in ("a", "all") or args.chat:
            case, turns = scripted(CHAT_PATH)
            print(f"Job a: the analyst's questions about {case}")
            box = Toolbox(client, calls, records, policy, rules, job_a_cases(case))
            ask = typed if args.chat else (lambda n: turns[n] if n < len(turns) else None)
            reports.append(run_conversation(claude, box, case, ask))
        if args.job in ("b", "all") and not args.chat:
            print("Job b: the regulator's letter")
            box = Toolbox(client, calls, records, policy, rules, job_b_cases(records, rules))
            reports.append(run_letter(claude, box, LETTER_PATH.read_text(encoding="utf-8").strip()))
    if calls.failures and len(calls.failures) == calls.total:
        first = calls.failures[0][1]
        raise SIEStopped(
            f"Every SIE call failed ({calls.total} of {calls.total}): {recipe.short(first)}. "
            f"{recipe.every_call_failed_advice(first)} No outputs were written."
        )
    write_outputs(output, reports, rules)
    for report in reports:
        cost = cost_entry(report, rules)
        unpriced = f" (not priced: {', '.join(cost['not_priced'])})" if cost["not_priced"] else ""
        print(
            f"job {report.job}: {report.status}, {cost['tool_calls']} tool calls, "
            f"{cost['sie_calls']} SIE calls, Claude ${cost['claude_usd']:.2f}{unpriced}, "
            f"{cost['seconds']} s"
        )
    print(f"Wrote {output}")


def main() -> None:
    args = parse_args()
    try:
        run(args)
    except SIEStopped as error:
        raise SystemExit(f"Stopped: {error}") from None
    except Exception as error:
        # Any Claude API error that isn't transient (a bad key, an unknown model, a bad request)
        # stops the run: its own message first, then what to check.
        if type(error).__module__.startswith("anthropic"):
            raise SystemExit(
                f"{error}\nStopped: the Claude API returned {type(error).__name__}. Check "
                "ANTHROPIC_API_KEY and agent: model in rules.yaml. No outputs were written."
            ) from None
        raise


if __name__ == "__main__":
    main()
