"""Check the recipe's parsing, CLM lookups, playbook checks and SIE error rule offline.

The checks use hand-written strings, the committed data/ (the CLM intake seed and the .docx
drafts) and hand-written stand-in model replies. Nothing here calls SIE or replays a recorded
response. Run it with `uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import copy
import csv
import dataclasses
import io
import json
import re
import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, cast

import clm
from sie_sdk import SIEClient
from sie_sdk.client.errors import (
    AccountInactiveError,
    InputTooLongError,
    InsufficientCreditsError,
    ModelLoadFailedError,
    ModelLoadingError,
    ProvisioningError,
    RateLimitError,
    RequestError,
    ServerError,
    SIEConnectionError,
    SIEError,
    SpendLimitError,
)

import make_data
import recipe

RULES = recipe.load_rules()
CLM = clm.CLM()
US = "Ostermark Instruments, Inc."
MATCHES, DEVIATES, NOT_FOUND, UNCLEAR = (
    recipe.MATCHES,
    recipe.DEVIATES,
    recipe.NOT_FOUND,
    recipe.UNCLEAR,
)


# --- Hand-written inputs ----------------------------------------------------------------------


def intake(role: str = "both", law: str = "Delaware", counterparty: str = "Acme Ltd") -> Any:
    return clm.IntakeRecord(
        "NDA-TEST", date(2026, 11, 1), counterparty, US, "A. Requester", "Test", "supply",
        "theirs", role, law, "nda-test.docx",
    )  # fmt: skip


def nda(*sections: tuple[str, str], preamble: str = "") -> str:
    """Markdown in docling's shape: a title heading, the preamble, one heading per section."""
    body = preamble or f"This Agreement is made between Acme Ltd and {US}."
    parts = ["# NON-DISCLOSURE AGREEMENT", body]
    parts += [f"## {heading}\n\n{text}" for heading, text in sections]
    return "\n\n".join(parts) + "\n"


def candidates(sections: list[recipe.Section], marks: list[tuple[str, str, str]]) -> list[Any]:
    """Stand-in extractor output: (field, value, heading the value was marked in)."""
    owner = recipe.field_positions(RULES)
    by_heading = {section.heading: section for section in sections}
    out = []
    for name, value, heading in marks:
        section = by_heading[heading]
        out.append(recipe.Candidate(name, owner[name], value, section, section.text))
    return out


def decide(
    markdown: str, marks: list[tuple[str, str, str]], role: str = "both", law: str = "Delaware"
) -> dict[str, recipe.Finding]:
    sections = recipe.split_sections(markdown)
    review = recipe.Review(intake(role, law))
    recipe.decide(sections, candidates(sections, marks), RULES, review)
    return {f.position: f for f in review.findings}


TERM = ("5. Term", "This Agreement and the obligations in it last for two (2) years.")
LAW = ("7. Governing Law", "This Agreement is governed by the laws of the State of Delaware.")
DISCLOSE = (
    "4. Permitted Disclosures",
    "The recipient may disclose Confidential Information to its employees who need to know it. "
    "The recipient may also disclose Confidential Information to the extent required by law, "
    "after giving the other party prompt written notice where lawful.",
)
CLEAN = nda(DISCLOSE, TERM, LAW)
CLEAN_MARKS = [
    ("agreement_term", "two (2) years", "5. Term"),
    ("governing_law", "Delaware", "7. Governing Law"),
    ("permitted_disclosures", "employees", "4. Permitted Disclosures"),
]


# --- Parsing ----------------------------------------------------------------------------------


def check_parsing() -> None:
    years = {
        "five (5) years": 5,
        "36 months": 3,
        "a 3-year period": 3,
        "3years": 3,  # glued
        "twenty-four (24) months": 2,
        "thirty-seven (37) months": 37 / 12,  # the digits win over a number word's tail
        "thirty-seven months": None,  # "seven" alone is not the number
        "in perpetuity": float("inf"),
        "two (2) years, or five (5) years for trade secrets": 5,  # two in one span
        "2.5 years": 2.5,  # a decimal, not the 5 after the point
        "3.5 years": 3.5,
        "3 1/2 years": 3.5,  # a fraction, not the 2 under the bar
        "three and a half years": 3.5,
        "2,5 years": None,  # a decimal comma is not read as 5 years
        "three (3) years and one (1) day": 3 + 1 / 365,  # one day over the limit
        "3 years and 1 day": 3 + 1 / 365,
        "thirty (30) days": None,  # a bare day or week duration is a notice period, not a term
        "90 days": None,
        "until terminated on thirty (30) days notice": None,
        "3 years 1 month": 3 + 1 / 12,
        "twelve (12) months, or 24 months": 2,  # same unit twice: two durations, not one
        "three (3) years and six (6) months": 3.5,  # units joined by "and" are one duration
        "2 years and 18 months": 3.5,
        "two years plus six months": 2.5,
        "UNILATERAL": None,
        "until the Transaction closes": None,
    }
    for text, want in years.items():
        assert recipe.duration_years(text, RULES) == want, (text, want)
    sections = recipe.split_sections(CLEAN)
    assert [s.heading for s in sections] == [
        "NON-DISCLOSURE AGREEMENT",
        "4. Permitted Disclosures",
        "5. Term",
        "7. Governing Law",
    ], sections
    parts = recipe.windows(" ".join(f"w{i}" for i in range(400)), 180, 30)
    assert len(parts) == 3, [p[:10] for p in parts]
    assert parts[1].startswith("w150 "), [p[:10] for p in parts]
    assert recipe.says("after giving notice", ["not"]) == []  # "not" is not "notice"
    assert recipe.says("may not disclose", ["not", "may"]) == ["not", "may"]
    assert recipe.sentences("A party may disclose. It may not. (b) The end") == [
        "A party may disclose.",
        "It may not.",
        "(b) The end",
    ]
    print("parsing: durations (glued, decimal, fraction, joined units), sections, windows ok")


def check_clm() -> None:
    requests = CLM.open_requests()
    assert requests == [f"NDA-2026-0{n}" for n in (412, 447, 453, 468, 471, 479)], requests
    record = CLM.get_request("NDA-2026-0447")
    assert record is not None
    assert (record.counterparty, record.our_role, record.paper) == (
        "Lowfield Ridge Capital Partners LP",
        "receive",
        "theirs",
    )
    assert record.received == date(2026, 9, 14)
    assert CLM.get_request("NDA-2026-447") is None  # a near-identical id is not that request
    assert CLM.get_request("nda-2026-0447") is None
    orlaith = CLM.get_request("NDA-2026-0468")
    assert orlaith is not None
    assert orlaith.governing_law_preference == ""
    with tempfile.TemporaryDirectory() as name:
        rows = (recipe.DATA_DIR / clm.SEED).read_text(encoding="utf-8").replace(",both,", ",all,")
        (Path(name) / clm.SEED).write_text(rows, encoding="utf-8")
        message = ""
        try:
            clm.CLM(Path(name))
        except ValueError as error:
            message = str(error)
        assert "bad role/paper" in message, "a role outside receive/disclose/both was accepted"
    print("clm: 6 open requests, exact-id lookup, bad role rejected ok")


# --- Playbook checks --------------------------------------------------------------------------


def check_statuses() -> None:
    found = decide(CLEAN, CLEAN_MARKS)
    assert {name: f.status for name, f in found.items()} == {
        "term": MATCHES,
        "governing_law": MATCHES,
        "non_solicit": NOT_FOUND,
        "residuals": NOT_FOUND,
        "permitted_disclosures": MATCHES,
    }, found
    assert set(recipe.STATUSES) == {"matches_position", "deviates", "not_found", "unclear"}
    # No old decision-sounding status survives anywhere in the code, the rules or this file.
    for path in ("recipe.py", "rules.yaml"):
        text = (Path(__file__).parent / path).read_text(encoding="utf-8")
        for old in ('"accept"', '"redline"', '"escalate"'):
            assert old not in text, (path, old)
    # Only a deviates row carries the redline wording and the approver.
    for f in found.values():
        assert not f.standard_wording, f
        assert not f.approver, f
    print("statuses: matches_position / deviates / not_found / unclear, no approval words ok")


def check_term() -> None:
    survival = ("9. Survival", "Confidentiality obligations survive for five (5) years after "
                "this Agreement expires or is terminated.")  # fmt: skip
    marks = [*CLEAN_MARKS, ("confidentiality_period", "five (5) years", "9. Survival")]
    term = decide(nda(DISCLOSE, TERM, LAW, survival), marks)["term"]
    assert term.status == DEVIATES, term
    assert "7 years" in term.reason, term.reason
    assert "agreement term of 2 years" in term.reason, term.reason
    assert term.value == "agreement term: two (2) years | confidentiality: five (5) years"
    assert term.standard_wording.startswith("This Agreement and the obligations in it last")
    assert term.approver == "Senior commercial counsel"
    # The survival section is there, but the extractor marked no confidentiality period: the
    # two-year term must not pass on its own.
    term = decide(nda(DISCLOSE, TERM, LAW, survival), CLEAN_MARKS)["term"]
    assert term.status == UNCLEAR, term
    assert "survive" in term.reason, term
    # The extractor read a period from the term section but none from the survival section
    # (gliner2-base-v1 did this on Corrin): not a pass.
    marks = [*CLEAN_MARKS, ("confidentiality_period", "two (2) years", "5. Term")]
    term = decide(nda(DISCLOSE, TERM, LAW, survival), marks)["term"]
    assert term.status == UNCLEAR, term
    assert "'9. Survival' says obligations survive" in term.reason, term.reason
    # A survival period with no agreement term to add it to.
    only = [("confidentiality_period", "five (5) years", "9. Survival")]
    assert decide(nda(survival), only)["term"].status == UNCLEAR
    # Exactly the limit matches; one month over deviates; perpetual deviates.
    three = ("5. Term", "This Agreement lasts for three (3) years.")
    assert (
        decide(nda(three), [("agreement_term", "three (3) years", "5. Term")])["term"].status
        == MATCHES
    )
    over = ("5. Term", "This Agreement lasts for thirty-seven (37) months.")
    for value, status in (("thirty-seven (37) months", DEVIATES), ("in perpetuity", DEVIATES)):
        text = over if "37" in value else ("5. Term", "It lasts in perpetuity.")
        got = decide(nda(text), [("agreement_term", value, "5. Term")])["term"]
        assert got.status == status, (value, got)
    # An evergreen NDA: the notice period the extractor marked is not a term, so no pass.
    evergreen = (
        "5. Term",
        "This Agreement continues until terminated by either party on thirty (30) days' "
        "written notice.",
    )
    got = decide(nda(evergreen), [("agreement_term", "thirty (30) days", "5. Term")])["term"]
    assert got.status == UNCLEAR, got
    # An unparseable value is never hidden behind one that parses.
    mixed = ("5. Term", "This Agreement lasts two (2) years or until the Transaction closes.")
    got = decide(
        nda(mixed),
        [
            ("agreement_term", "two (2) years", "5. Term"),
            ("confidentiality_period", "until the Transaction closes", "5. Term"),
        ],
    )["term"]
    assert got.status == UNCLEAR, got
    # A short confidentiality period doesn't hide a long agreement term.
    long = ("5. Term", "This Agreement lasts five (5) years; confidentiality lasts two (2) years.")
    got = decide(
        nda(long),
        [
            ("agreement_term", "five (5) years", "5. Term"),
            ("confidentiality_period", "two (2) years", "5. Term"),
        ],
    )["term"]
    assert got.status == DEVIATES, got
    assert "agreement term of 5 years" in got.reason, got
    # No term clause anywhere: not_found, never a pass.
    got = decide(nda(DISCLOSE, LAW), CLEAN_MARKS[1:])["term"]
    assert got.status == NOT_FOUND, got
    # The extractor marked a duration in a section without term wording: unclear, not dropped.
    personnel = ("3. Personnel", "Neither party shall engage the other's staff for one (1) year.")
    got = decide(nda(personnel), [("agreement_term", "one (1) year", "3. Personnel")])["term"]
    assert got.status == UNCLEAR, got
    assert "3. Personnel" in got.reason, got
    # Confidentiality runs from each disclosure in a section the extractor read nothing from:
    # the two-year agreement term alone must not pass.
    held = (
        "6. Confidentiality",
        "The Recipient shall hold Confidential Information in confidence for a period of "
        "five (5) years from the date of each disclosure.",
    )
    got = decide(nda(DISCLOSE, TERM, LAW, held), CLEAN_MARKS)["term"]
    assert got.status == UNCLEAR, got
    assert "'6. Confidentiality' says" in got.reason, got.reason
    assert got.section == "5. Term; 6. Confidentiality", got.section
    marks = [*CLEAN_MARKS, ("confidentiality_period", "five (5) years", "6. Confidentiality")]
    assert decide(nda(DISCLOSE, TERM, LAW, held), marks)["term"].status == DEVIATES
    # A short period in that sentence is no reason to hold the pass.
    short = (
        "6. Confidentiality",
        "The Recipient shall hold it in confidence for a period of one (1) year from the date "
        "of each disclosure.",
    )
    assert decide(nda(DISCLOSE, TERM, LAW, short), CLEAN_MARKS)["term"].status == MATCHES
    # "continues to apply ... after the end of" is survival wording: it adds to the term.
    goes_on = (
        "9. Continuing Obligations",
        "Section 6 continues to apply for two (2) years after the end of this Agreement.",
    )
    marks = [*CLEAN_MARKS, ("confidentiality_period", "two (2) years", goes_on[0])]
    got = decide(nda(DISCLOSE, TERM, LAW, goes_on), marks)["term"]
    assert got.status == DEVIATES, got
    assert "4 years" in got.reason, got.reason
    # ... but a marked word that is no duration at all ("UNILATERAL" in the title) is noise.
    noise = decide(nda(), [("agreement_term", "NON-DISCLOSURE", "NON-DISCLOSURE AGREEMENT")])
    assert noise["term"].status == NOT_FOUND, noise["term"]
    print("term: survival adds to the term, unparseable and dropped durations unclear ok")


def check_governing_law() -> None:
    singapore = ("7. Governing Law", "This Agreement is governed by the laws of Singapore.")
    marks = [("governing_law", "Singapore", "7. Governing Law")]
    got = decide(nda(singapore), marks, law="England and Wales")["governing_law"]
    assert got.status == DEVIATES, got
    assert got.standard_wording == (
        "This Agreement is governed by the laws of England and Wales, without regard to its "
        "conflict of laws rules."
    ), got.standard_wording
    assert got.approver == "General Counsel"
    # No preference in the intake record: the playbook's default law goes in the redline.
    got = decide(nda(singapore), marks, law="")["governing_law"]
    assert "laws of Delaware," in got.standard_wording, got.standard_wording
    # Near miss: New Jersey is not New York.
    jersey = ("7. Governing Law", "This Agreement is governed by the laws of New Jersey.")
    law = decide(nda(jersey), [("governing_law", "New Jersey", "7. Governing Law")])
    assert law["governing_law"].status == DEVIATES
    # One span naming two places: never settled by the first.
    either = ("7. Governing Law", "This Agreement is governed by the laws of New York or Singapore")
    law = decide(nda(either), [("governing_law", "New York or Singapore", "7. Governing Law")])
    assert law["governing_law"].status == UNCLEAR, law["governing_law"]
    # A "Delaware corporation" in the preamble is not a governing-law clause.
    pre = "This Agreement is made between Acme Ltd, a Delaware corporation, and " + US + "."
    law = decide(nda(preamble=pre), [("governing_law", "Delaware", "NON-DISCLOSURE AGREEMENT")])
    assert law["governing_law"].status == UNCLEAR, law["governing_law"]
    # An allowed law that isn't the one the request asked for still matches; the reason says so.
    england = ("7. Governing Law", "This Agreement is governed by the law of England and Wales.")
    law = decide(nda(england), [("governing_law", "England and Wales", "7. Governing Law")])
    assert law["governing_law"].status == MATCHES
    assert "(the request asked for Delaware)" in law["governing_law"].reason
    # Whole place names only: an allowed name inside a longer one is not that place.
    for value, status in (
        ("New England", UNCLEAR),
        ("Delaware County, Pennsylvania", UNCLEAR),
        ("Californian law", DEVIATES),
        ("English law", MATCHES),
        ("the State of New York", MATCHES),
        ("the laws of England and Wales", MATCHES),
    ):
        clause = ("7. Governing Law", f"This Agreement is governed by {value}.")
        got = decide(nda(clause), [("governing_law", value, "7. Governing Law")])["governing_law"]
        assert got.status == status, (value, got)
    # A preference the playbook doesn't allow is never pasted into a redline.
    got = decide(nda(singapore), marks, law="Texas")["governing_law"]
    assert got.status == UNCLEAR, got
    assert "'Texas', which is not on the playbook's allowed list" in got.reason, got.reason
    assert not got.standard_wording, got
    print("governing law: redline names the intake preference, near misses and two places ok")


def check_forbidden() -> None:
    solicit = ("4. Non-Solicitation", "Neither party shall solicit any employee of the other "
               "for twelve (12) months.")  # fmt: skip
    got = decide(nda(solicit), [("non_solicit", "twelve (12) months", "4. Non-Solicitation")])
    assert got["non_solicit"].status == DEVIATES
    assert got["non_solicit"].approver == "Head of People and General Counsel"
    assert got["non_solicit"].standard_wording.startswith("Delete the clause.")
    # The wording is there but the extractor found nothing: unclear, not a pass.
    assert decide(nda(solicit), [])["non_solicit"].status == UNCLEAR
    # The reworded non-solicit: no keyword, nothing extracted. Never matches_position.
    engage = ("3. Personnel", "Neither party shall engage any of the other party's personnel.")
    assert decide(nda(engage), [])["non_solicit"].status == NOT_FOUND
    # The extractor marked it, but the section lacks the keywords: unclear, cited.
    got = decide(nda(engage), [("non_solicit", "the other party's personnel", "3. Personnel")])
    assert got["non_solicit"].status == UNCLEAR
    assert got["non_solicit"].section == "3. Personnel"
    # A residuals clause found is deviates.
    residual = ("5. Residuals", "Either party may use Residuals retained in unaided memory.")
    got = decide(nda(residual), [("residuals", "Residuals", "5. Residuals")])
    assert got["residuals"].status == DEVIATES
    # Absence of a forbidden clause is never matches_position, whatever the input.
    for markdown in (CLEAN, nda(), nda(engage)):
        found = decide(markdown, [])
        assert found["non_solicit"].status != MATCHES
        assert found["residuals"].status != MATCHES
    print("forbidden clauses: found deviates, absence not_found, missed wording unclear ok")


def check_permitted_disclosures() -> None:
    mark = [("permitted_disclosures", "employees", "4. Permitted Disclosures")]
    # Codex's case: employees and court are both in the section, but legal-process disclosure
    # needs consent. A keyword check would pass it.
    consent = (
        "4. Permitted Disclosures",
        "The recipient may disclose Confidential Information to its employees who need to know "
        "it. The recipient may not disclose Confidential Information under legal process or to "
        "any court without the discloser's prior written consent.",
    )
    got = decide(nda(consent), mark)["permitted_disclosures"]
    assert got.status == UNCLEAR, got
    assert "disclosure required by law" in got.reason, got
    assert "not" in got.reason, got.reason
    assert "consent" in got.reason, got.reason
    # "without" in the legal-process sentence is a negation too.
    without = (
        "4. Permitted Disclosures",
        "The recipient may disclose Confidential Information to its employees who need to know "
        "it. The recipient may disclose it as required by law without notice to the discloser.",
    )
    got = decide(nda(without), mark)["permitted_disclosures"]
    assert got.status == UNCLEAR, got
    assert "without" in got.reason, got.reason
    # "after giving notice" is not a negation.
    assert decide(CLEAN, CLEAN_MARKS)["permitted_disclosures"].status == MATCHES
    # The carve-out words only in a definition (no permission sentence) don't count.
    definitions = (
        "1. Definitions",
        '"Representatives" means employees and advisers. Information required by law to be '
        "disclosed by a court is not Confidential Information.",
    )
    no_law = (
        "4. Permitted Disclosures",
        "The recipient may disclose Confidential Information to its employees.",
    )
    got = decide(
        nda(definitions, no_law),
        [*mark, ("permitted_disclosures", "employees", "1. Definitions")],
    )["permitted_disclosures"]
    assert got.status == DEVIATES, got
    assert "disclosure required by law" in got.reason, got
    assert got.standard_wording.startswith("The recipient may disclose"), got
    # The marked section lacks a carve-out another section grants: unclear, both cited.
    legal = (
        "6. Legal Process",
        "If the recipient is required by law to disclose Confidential Information, it may do so.",
    )
    got = decide(nda(no_law, legal), mark)["permitted_disclosures"]
    assert got.status == UNCLEAR
    assert got.section == "4. Permitted Disclosures; 6. Legal Process"
    print("permitted disclosures: permission sentences only, negation unclear ok")


def check_role() -> None:
    # We only disclose: our limits as recipient don't bind us, and the review says so.
    sections = recipe.split_sections(CLEAN)
    review = recipe.Review(intake(role="disclose"))
    recipe.decide(sections, candidates(sections, CLEAN_MARKS), RULES, review)
    assert [f.position for f in review.findings] == ["governing_law", "non_solicit", "residuals"]
    notes = [n for n in review.notes if n.startswith("Not checked:")]
    assert len(notes) == 2, notes
    assert "We only disclose under this NDA" in notes[0], notes
    print("our role: recipient-only positions skipped, with a note, when we only disclose ok")


# --- The whole run, with a stand-in SIE -------------------------------------------------------


def markdown_for(stem: str) -> str:
    title, preamble, sections = make_data.NDAS[f"{stem}.docx"]()
    parts = [f"# {title}", *preamble]
    for heading, paragraphs in sections:
        parts += [f"## {heading}", *paragraphs]
    return "\n\n".join(parts) + "\n"


def stand_in_fields(text: str, fields: list[str]) -> dict[str, Any]:
    """A plain stand-in extractor: picks values with regexes, as a good extractor would."""
    heading = text.split("\n", 1)[0]
    out: dict[str, Any] = {}
    duration = re.search(r"\w+ \(\d+\) (?:years?|months)", text)
    if duration and re.search(r"Term|Duration|Survival", heading):
        name = "confidentiality_period" if "survive" in text else "agreement_term"
        out[name] = duration.group(0)
    law = re.search(r"laws? of (?:the )?(?:State of |Republic of )?([A-Z][A-Za-z ]+)", text)
    if law and "govern" in text:
        out["governing_law"] = law.group(1).strip()
    if "solicit" in text:
        out["non_solicit"] = "twelve (12) months"
    if "Residuals" in text:
        out["residuals"] = "Residuals"
    if "may disclose" in text:
        out["permitted_disclosures"] = ["employees"]
    return {name: value for name, value in out.items() if name in fields}


class StandIn:
    """Stands in for SIEClient. docling calls return Markdown built from make_data's NDA text;
    extract calls with an output_schema return `stand_in_fields`. `errors` maps an item-id
    prefix to the errors its calls raise (one per call), `replies` to a raw reply."""

    def __init__(
        self,
        errors: dict[str, list[Exception]] | None = None,
        replies: dict[str, Any] | None = None,
        markdown: Callable[[str], str] = markdown_for,
    ) -> None:
        self.errors, self.replies, self.markdown = errors or {}, replies or {}, markdown
        self.ids: list[str] = []
        self.options: list[dict[str, Any]] = []  # options of each output_schema call

    def extract(self, model: str, item: Any, **options: Any) -> Any:
        item_id = item["id"]
        self.ids.append(item_id)
        for prefix, errors in self.errors.items():
            if item_id.startswith(prefix) and errors:
                raise errors.pop(0)
        for prefix, reply in self.replies.items():
            if item_id.startswith(prefix):
                return reply
        if "output_schema" not in options:
            return {"data": {"markdown": self.markdown(item_id.removeprefix("read/"))}}
        self.options.append(options.get("options", {}))
        fields = list(options["output_schema"]["properties"])
        data = stand_in_fields(item["text"], fields)
        if not data:  # SIE Serverless's other "found nothing" shape
            return {"entities": [], "relations": [], "classifications": [], "objects": []}
        return {"data": data}


LAST: list[StandIn] = []  # the stand-in client of the latest run


def run(**options: Any) -> tuple[dict[str, dict[str, recipe.Finding]], recipe.Calls]:
    client = StandIn(**options)
    LAST[:] = [client]
    with contextlib.redirect_stdout(io.StringIO()):  # the "n of m calls failed" line
        reviews, calls = recipe.run(cast(SIEClient, client), RULES, CLM)
    out = {Path(r.nda).stem: {f.position: f for f in r.findings} for r in reviews}
    return out, calls


def statuses(found: dict[str, recipe.Finding]) -> dict[str, str]:
    return {name: f.status for name, f in found.items()}


def check_committed_data() -> None:
    reviews, calls = run()
    got = {stem: statuses(found) for stem, found in reviews.items()}
    assert got["nda-brackenridge-analytics"] == {
        "term": MATCHES,
        "governing_law": MATCHES,
        "non_solicit": NOT_FOUND,
        "residuals": NOT_FOUND,
        "permitted_disclosures": MATCHES,
    }, got["nda-brackenridge-analytics"]
    assert got["nda-lowfield-ridge-capital"]["term"] == DEVIATES
    assert got["nda-lowfield-ridge-capital"]["governing_law"] == MATCHES
    assert got["nda-quillfeather-logistics"]["term"] == MATCHES  # exactly 3 years
    assert got["nda-quillfeather-logistics"]["non_solicit"] == DEVIATES
    assert got["nda-quillfeather-logistics"]["residuals"] == DEVIATES
    assert got["nda-orlaith-marine"]["governing_law"] == DEVIATES
    assert got["nda-orlaith-marine"]["term"] == NOT_FOUND
    corrin = reviews["nda-corrin-fabrication"]["term"]
    assert corrin.status == DEVIATES, corrin
    assert "7 years" in corrin.reason, corrin
    assert got["nda-velloway-robotics"]["non_solicit"] == NOT_FOUND
    assert calls.count["docling"] == 6, calls
    assert not calls.failures, calls
    # Every GLiNER2 call asks SIE to fail an over-long window rather than cut it short.
    assert LAST[0].options, "no GLiNER2 call was made"
    assert all(o == {"overflow_policy": "error"} for o in LAST[0].options), LAST[0].options
    # The intake seed names exactly the committed drafts, and the drafts name the intake parties.
    files = sorted(p.name for p in recipe.DATA_DIR.glob("*.docx"))
    records = [CLM.get_request(r) for r in CLM.open_requests()]
    assert sorted(r.draft_file for r in records if r) == files, files
    for record in records:
        assert record is not None
        stem = Path(record.draft_file).stem
        assert recipe.parties_missing(markdown_for(stem), record) == [], record
        headings = recipe.docx_headings(recipe.DATA_DIR / record.draft_file)
        assert headings, record.draft_file
        markdown = markdown_for(stem)
        assert recipe.unreadable(markdown, recipe.split_sections(markdown), headings, RULES) == ""
        # The drafts carry no document properties (author, company, dates).
        names = __import__("zipfile").ZipFile(recipe.DATA_DIR / record.draft_file).namelist()
        assert not any(name.startswith("docProps/") for name in names), names
    print("committed data: 6 drafts, intake matches, expected cases with a stand-in extractor ok")


def check_intake_and_conversion() -> None:
    # A draft for another Ostermark entity: near-identical, but not the party on the request.
    def gmbh(stem: str) -> str:
        return markdown_for(stem).replace(US, "Ostermark Instruments GmbH")

    reviews, calls = run(markdown=gmbh)
    for found in reviews.values():
        assert {f.status for f in found.values()} == {UNCLEAR}, found
        assert "Ostermark Instruments, Inc." in next(iter(found.values())).reason
    assert calls.total == 6, calls.count  # no extraction on the wrong draft

    # A conversion that dropped a section: every position unclear, never not_found.
    def dropped(stem: str) -> str:
        return markdown_for(stem).replace("## 5. Term", "5. Term")

    reviews, _ = run(markdown=dropped)
    found = reviews["nda-brackenridge-analytics"]
    assert {f.status for f in found.values()} == {UNCLEAR}, found
    assert "lacks 1 of its 8 headings: 5. Term" in found["term"].reason, found["term"].reason
    assert statuses(reviews["nda-orlaith-marine"])["governing_law"] == DEVIATES
    # A draft file name that is a path ("../rules.yaml") is never opened.
    for name in ("../rules.yaml", "/etc/hosts", "data/nda-orlaith-marine.docx", ".."):
        client = StandIn()
        record = dataclasses.replace(intake(), draft_file=name)
        review = recipe.review_request(recipe.Calls(), cast(SIEClient, client), record, RULES)
        assert {f.status for f in review.findings} == {UNCLEAR}, (name, review.findings)
        assert "not a plain file name" in review.findings[0].reason, review.findings[0]
        assert client.ids == [], client.ids
    print("intake and conversion: wrong entity, dropped heading or a path, all unclear ok")


TRANSIENT: list[SIEError] = [
    SIEConnectionError("connection refused"),
    ServerError("unavailable", status_code=503),
    RateLimitError("slow down"),
    InputTooLongError("too long"),
    ProvisioningError("no GPU yet"),
    ModelLoadingError("loading"),
    ModelLoadFailedError("try later", permanent=False),
    SIEError("an SIE error with no subclass"),
]
FATAL: list[SIEError] = [
    RequestError("bad key", status_code=401),
    RequestError("no such model", status_code=404),
    InsufficientCreditsError("no credits"),
    SpendLimitError("spend limit"),
    AccountInactiveError("inactive"),
    ModelLoadFailedError("gated", permanent=True),
]


def stopped(call: Callable[[], object]) -> str:
    """The SIEStopped message the call raises; fails when it raises nothing."""
    try:
        call()
    except recipe.SIEStopped as error:
        return str(error)
    raise AssertionError("the run did not stop")


def raised(call: Callable[[], object]) -> BaseException:
    """Whatever the call raises, SystemExit included; fails when it raises nothing."""
    try:
        call()
    except BaseException as error:  # SystemExit too: the check is on which one it was
        return error
    raise AssertionError("nothing was raised")


def check_sie_failure() -> None:
    for error in TRANSIENT:
        assert not recipe.is_fatal(error), f"{type(error).__name__} should be transient"
    for error in FATAL:
        assert recipe.is_fatal(error), f"{type(error).__name__} should stop the run"
    # A transient error reading one NDA: its positions are unclear, the others carry on.
    for error in TRANSIENT:
        reviews, _ = run(errors={"read/nda-quillfeather": [error]})
        found = reviews["nda-quillfeather-logistics"]
        assert {f.status for f in found.values()} == {UNCLEAR}, type(error).__name__
        assert "The NDA was not read: the SIE call failed: " in found["term"].reason
        assert reviews["nda-orlaith-marine"]["governing_law"].status == DEVIATES
    # A transient error on one extract call: the positions of that group are unclear, even
    # the ones the rest of the NDA settled; the other groups' positions are untouched.
    group = "extract/nda-quillfeather-logistics/s5-w1/non_solicit"
    reviews, calls = run(errors={group: [ServerError("busy", status_code=503)]})
    found = reviews["nda-quillfeather-logistics"]
    assert found["non_solicit"].status == UNCLEAR, found["non_solicit"]
    assert "window 1 of '4. Non-Solicitation' was not read" in found["non_solicit"].reason
    assert (
        "from the rest: The NDA has this clause" not in found["non_solicit"].reason
    )  # this window held the clause, so the rest found nothing
    assert found["residuals"].status == DEVIATES
    assert found["governing_law"].status == MATCHES
    assert len(calls.failures) == 1
    # A reply missing its key is a failed call on the same path; empty lists are not.
    reviews, _ = run(replies={"extract/nda-orlaith": {"something": []}})
    reason = reviews["nda-orlaith-marine"]["governing_law"].reason
    assert "replied without 'data'" in reason, reason
    reviews, _ = run(replies={"extract/nda-orlaith": {"entities": [{"text": "x"}]}})
    assert reviews["nda-orlaith-marine"]["term"].status == UNCLEAR
    reviews, _ = run(replies={"extract/nda-orlaith": {"data": {}}})
    assert reviews["nda-orlaith-marine"]["term"].status == NOT_FOUND
    reviews, _ = run(replies={"read/nda-orlaith": {"markdown": "x"}})
    assert "replied without 'data'" in reviews["nda-orlaith-marine"]["term"].reason
    # SIE Serverless's empty-result 500 reads as "no fields", not a failed call; from docling it's
    # a failed call.
    empty = ServerError(recipe.EMPTY_RESULT_ERROR, status_code=500)
    reviews, calls = run(errors={"extract/nda-orlaith": [empty]})
    assert not calls.failures
    assert reviews["nda-orlaith-marine"]["term"].status == NOT_FOUND
    reviews, calls = run(errors={"read/nda-orlaith": [empty]})
    assert reviews["nda-orlaith-marine"]["term"].status == UNCLEAR
    assert calls.failures
    # Every call failed (a dead SIE_BASE_URL): the run stops, with the full message.
    long = "connection refused " + "x" * 300
    dead = {"": [SIEConnectionError(long) for _ in range(20)]}
    message = stopped(lambda: run(errors=dead))
    assert message.startswith("Every SIE call failed (6 of 6 to docling)"), message
    assert long in message, message
    assert "Check SIE_BASE_URL and your network." in message, message
    busy = {"": [ModelLoadingError("loading") for _ in range(20)]}
    assert "may still be starting or SIE is busy" in stopped(lambda: run(errors=busy))
    # A refusal stops the run at once, with the full message, HTTP status and code.
    refusal = {"extract/": [InsufficientCreditsError("no credits " + "y" * 300)]}
    message = stopped(lambda: run(errors=refusal))
    assert "InsufficientCreditsError (HTTP 402, code INSUFFICIENT_CREDITS)" in message, message
    assert "y" * 300 in message
    assert "SIE_API_KEY" in message
    message = stopped(lambda: run(errors={"": [ModelLoadFailedError("gated", permanent=True)]}))
    assert message.startswith("SIE can't load docling"), message
    message = stopped(lambda: run(errors={"": [RequestError("unexpected body")]}))
    assert "SIE_API_KEY" not in message, message
    assert "SIE_BASE_URL" in message, message
    # A bug in our own code is raised as is; main turns only SIEStopped into a clean exit.
    try:
        run(errors={"": [TypeError("bad argument")]})
    except TypeError:
        pass
    else:
        raise AssertionError("TypeError was swallowed")

    def bug() -> None:
        raise RuntimeError("a bug in recipe.py")

    def refused() -> None:
        raise recipe.SIEStopped("SIE refused")

    caught = raised(lambda: recipe.stop_on_refusal(bug))
    assert type(caught) is RuntimeError
    assert str(caught) == "a bug in recipe.py"
    caught = raised(lambda: recipe.stop_on_refusal(refused))
    assert type(caught) is SystemExit
    assert str(caught) == "Stopped: SIE refused"
    print("sie failure: transient to unclear; missing key fails; refusals and all-failed stop ok")


def check_outputs() -> None:
    assert recipe.csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert recipe.csv_safe("-2 years") == "'-2 years"
    assert recipe.csv_safe("NDA-2026-0412") == "NDA-2026-0412"
    review = recipe.Review(dataclasses.replace(intake(), counterparty="=cmd()"))
    review.findings = [recipe.Finding("term", UNCLEAR, "+1 test")]
    with tempfile.TemporaryDirectory() as name:
        recipe.write_outputs(Path(name), [review])
        with (Path(name) / "decisions.csv").open(encoding="utf-8") as handle:
            row = next(csv.DictReader(handle))
        assert row["counterparty"] == "'=cmd()", row
        assert row["reason"] == "'+1 test", row
        assert (Path(name) / "reviews" / "nda-test.json").is_file()
    print("outputs: formula-looking cells are quoted ok")


# --- The agent (agent.py): every check in code, with hand-written stand-in agent output ----------
# agent.py imports anthropic only inside the functions that call Claude, so these run without the
# agent dependency group and never call Claude or SIE.

LOWFIELD, CORRIN, ORLAITH = "NDA-2026-0447", "NDA-2026-0471", "NDA-2026-0468"
TAIL = (
    "The Recipient's obligations under this Agreement shall remain in effect for four (4) years "
    "from the date of this Agreement, and, for any trade secret, for so long as it remains a "
    "trade secret."
)
FLAT_FOUR = (
    "The Recipient's obligations under this Agreement shall remain in effect for four (4) years "
    "from the date of this Agreement."
)
ONE_YEAR = (
    "The obligations of confidentiality in this Agreement survive for one (1) year after this "
    "Agreement expires or is terminated."
)
SINGAPORE = (
    "This undertaking shall be governed by and construed in accordance with the laws of the "
    "Republic of Singapore."
)


def box(request: str, **options: Any) -> Any:
    import agent

    record = CLM.get_request(request)
    assert record is not None
    client = StandIn(**options)
    return agent.Box(cast(SIEClient, client), recipe.Calls(), RULES, CLM, record)


def reviewed(request: str, *lawyer: str) -> Any:
    """A conversation's tools after review_nda, as Claude would have called them."""
    b = box(request)
    b.lawyer.extend(lawyer)
    b.run("review_nda", {"request": request})
    return b


def check_agent_leaves_workflow_alone() -> None:
    """The fallback and the agent: block are read only by agent.py: the workflow's results on the
    committed data are the same with and without them."""
    bare = copy.deepcopy(RULES)
    bare.pop("agent")
    for spec in bare["positions"].values():
        spec.pop("fallback", None)
    results = []
    for rules in (RULES, bare):
        with contextlib.redirect_stdout(io.StringIO()):
            reviews, _ = recipe.run(cast(SIEClient, StandIn()), rules, CLM)
        results.append(
            [(r.nda, f.position, f.status, f.reason, f.value) for r in reviews for f in r.findings]
        )
    assert results[0] == results[1]
    # review_request's keep= only stores what it already computed.
    record = CLM.get_request(CORRIN)
    assert record is not None
    kept: dict[str, Any] = {}
    a = recipe.review_request(recipe.Calls(), cast(SIEClient, StandIn()), record, RULES, keep=kept)
    b = recipe.review_request(recipe.Calls(), cast(SIEClient, StandIn()), record, RULES)
    assert [(f.status, f.reason) for f in a.findings] == [(f.status, f.reason) for f in b.findings]
    assert set(kept) == {"markdown", "sections", "candidates"}, kept
    print("agent: the fallback and agent: keys don't change the workflow's results ok")


def check_agent_playbook_lookup() -> None:
    import agent

    lowfield = box(LOWFIELD).run("playbook_position", {"request": LOWFIELD, "position": "term"})
    found = json.loads(lowfield)
    assert found["fallback"]["max_years"] == 4, found
    assert "only_when" not in found["fallback"], found
    corrin = json.loads(
        box(CORRIN).run("playbook_position", {"request": CORRIN, "position": "term"})
    )
    assert corrin["fallback"] is None, corrin
    assert corrin["ask"] == "Senior commercial counsel", corrin
    law = json.loads(
        box(ORLAITH).run("playbook_position", {"request": ORLAITH, "position": "governing_law"})
    )
    assert law["fallback"] is None, law
    assert law["ask"] == "General Counsel", law
    # Both conditions must match: an acquisition where we also disclose gets no fallback.
    both = dataclasses.replace(cast(clm.IntakeRecord, CLM.get_request(LOWFIELD)), our_role="both")
    assert agent.fallback_for("term", both, RULES) is None
    # Wrong request ids and unknown positions or sections go back to Claude as errors.
    for name, args in (
        ("review_nda", {"request": CORRIN}),
        ("playbook_position", {"request": LOWFIELD, "position": "liability_cap"}),
        (
            "check_revised_clause",
            {"request": LOWFIELD, "section_heading": "Section 9", "new_text": "x"},
        ),
        ("check_revised_clause", {"request": LOWFIELD, "section_heading": "4", "new_text": "x"}),
    ):
        try:
            box(LOWFIELD).run(name, args)
        except agent.ToolError:
            continue
        raise AssertionError(f"{name} {args} was not refused")
    print("agent: fallback only when every condition matches; wrong ids and sections refused ok")


def check_agent_clause_in_context() -> None:
    """The tool, not the agent, picks the context: new wording is checked in the full NDA."""
    corrin = reviewed(CORRIN)
    assert corrin.latest["term"]["standard"] == DEVIATES
    out = json.loads(
        corrin.run(
            "check_revised_clause",
            {"request": CORRIN, "section_heading": "7. Survival", "new_text": ONE_YEAR},
        )
    )
    term = next(p for p in out["positions"] if p["position"] == "term")
    assert term["status"] == MATCHES, term
    assert "3 years" in term["reason"], term
    assert "status_against_fallback" not in term  # no fallback for a supply NDA
    # The same Section 7 on its own, with nothing to add the year to, is not a pass.
    alone = recipe.split_sections(nda(("7. Survival", ONE_YEAR)))
    found = decide(
        nda(("7. Survival", ONE_YEAR)), [("confidentiality_period", "one (1) year", "7. Survival")]
    )
    assert found["term"].status == UNCLEAR, found["term"]
    assert alone
    assert alone[-1].heading == "7. Survival"
    # Lowfield: the trade-secret tail passes the fallback. That's the workflow's own blind spot
    # (perpetual_terms has no "for so long as"); the lawyer catches it, not the code.
    lowfield = reviewed(LOWFIELD)
    out = json.loads(
        lowfield.run(
            "check_revised_clause",
            {"request": LOWFIELD, "section_heading": "Section 4. Duration", "new_text": TAIL},
        )
    )
    term = next(p for p in out["positions"] if p["position"] == "term")
    assert term["status"] == DEVIATES, term
    assert term["status_against_fallback"] == MATCHES, term
    assert lowfield.latest["term"] == {"standard": DEVIATES, "fallback": MATCHES}
    # Other sections keep the extractor's first reading: Lowfield's law stays not_found.
    gov = next(p for p in out["positions"] if p["position"] == "governing_law")
    assert gov["status"] == lowfield.review.findings[1].status
    print("agent: clause checked in the full NDA (Corrin 3 years), unclear alone; tail passes ok")


def check_agent_answers() -> None:
    import agent

    def problems(b: Any, text: str, statuses: list[dict[str, str]] | None = None) -> list[str]:
        return agent.check_answer({"text": text, "statuses": statuses or []}, b)

    corrin = reviewed(CORRIN, "Corrin won't drop the five-year survival. What's our fallback?")
    corrin.run("playbook_position", {"request": CORRIN, "position": "term"})
    good = (
        "No fallback in the playbook for this request. The limit is 3 years and the review reads "
        "7 years (two (2) years plus five (5) years after). Ask Senior commercial counsel."
    )
    assert (
        problems(corrin, good, [{"position": "term", "status": DEVIATES, "against": "standard"}])
        == []
    )
    # An invented middle ground: four years is Lowfield's fallback, not Corrin's.
    invented = problems(corrin, "You could offer four (4) years as a compromise.")
    assert any("4 years" in p for p in invented), invented
    assert any("6 years" in p for p in problems(corrin, "Try a 6-year survival instead."))
    assert any("18" in p or "1.5" in p for p in problems(corrin, "Offer eighteen (18) months."))
    # The same four years is grounded on Lowfield, where the fallback applies.
    lowfield = reviewed(LOWFIELD, "What does the playbook let me offer?")
    lowfield.run("playbook_position", {"request": LOWFIELD, "position": "term"})
    assert problems(lowfield, "The fallback is up to four (4) years without approval.") == []
    # A made-up place, and one from the NDA.
    orlaith = reviewed(ORLAITH, "Orlaith says Singapore law or no demo. Fallback?")
    assert problems(orlaith, "Singapore is not on the list; ask General Counsel.") == []
    swiss = problems(orlaith, "Offer Swiss law as a neutral middle ground.")
    assert any("Switzerland" in p for p in swiss), swiss
    # Statuses must be what the latest tool call returned.
    wrong = [{"position": "term", "status": MATCHES, "against": "standard"}]
    assert any("last tool call said deviates" in p for p in problems(corrin, good, wrong))
    no_fb = [{"position": "term", "status": MATCHES, "against": "fallback"}]
    assert any("no tool checked" in p for p in problems(corrin, good, no_fb))
    bogus = [{"position": "liability_cap", "status": DEVIATES, "against": "standard"}]
    assert any("not a playbook position" in p for p in problems(corrin, good, bogus))
    made_up = [{"position": "term", "status": "approved", "against": "standard"}]
    assert any("not a status" in p for p in problems(corrin, good, made_up))
    fresh = box(CORRIN)
    early = [{"position": "term", "status": DEVIATES, "against": "standard"}]
    assert any("no tool returned" in p for p in problems(fresh, "It deviates.", early))
    # After the clause check, the term is a match, and the old status is now wrong.
    corrin.run(
        "check_revised_clause",
        {"request": CORRIN, "section_heading": "7. Survival", "new_text": ONE_YEAR},
    )
    now = [{"position": "term", "status": MATCHES, "against": "standard"}]
    assert problems(corrin, "Yes: 3 years in total, within the limit.", now) == []
    assert any("said matches_position" in p for p in problems(corrin, "No.", early))
    # Durations: compound units stay one; a notice period is not a term.
    assert agent.durations("three (3) years and six (6) months", RULES) == [3.5]
    assert agent.durations("1 year after a 2-year term", RULES) == [1.0, 2.0]
    assert agent.durations("on thirty (30) days' notice", RULES) == []
    print("agent: answers flag invented durations and places, and stale or made-up statuses ok")


def reply(request: str, **fields: Any) -> dict[str, Any]:
    record = CLM.get_request(request)
    assert record is not None
    base: dict[str, Any] = {
        "kind": "reply_to_counterparty",
        "to": record.counterparty,
        "body": "Thank you for your comments.",
        "positions": [],
    }
    return {**base, **fields}


def check_agent_replies() -> None:
    import agent

    def problems(b: Any, draft: dict[str, Any]) -> list[str]:
        return agent.check_reply(draft, b)

    confirm = [
        {"position": "non_solicit", "stance": "lawyer_to_confirm"},
        {"position": "residuals", "stance": "lawyer_to_confirm"},
    ]
    # Corrin: the accepted Section 7, passed by the clause check.
    corrin = reviewed(CORRIN, "Their counsel offers to cut Section 7.")
    corrin.run(
        "check_revised_clause",
        {"request": CORRIN, "section_heading": "7. Survival", "new_text": ONE_YEAR},
    )
    agreed = [{"position": "term", "stance": "agreed", "clause": ONE_YEAR}, *confirm]
    body = f'We accept Section 7 as: "{ONE_YEAR}"'
    assert problems(corrin, reply(CORRIN, body=body, positions=agreed)) == []
    # Completeness from the workflow's review: dropping an open row fails.
    short = problems(corrin, reply(CORRIN, body=body, positions=agreed[:2]))
    assert any("residuals is not_found in the review but missing" in p for p in short), short
    # A clause stitched onto the checked one, or a different one, is not what was checked.
    stitched = [
        {"position": "term", "stance": "agreed", "clause": ONE_YEAR + " Trade secrets survive."}
    ]
    assert any(
        "term marked agreed" in p
        for p in problems(corrin, reply(CORRIN, positions=stitched + confirm))
    )
    # Wrong counterparty name, and another request's counterparty in the body.
    misnamed = problems(corrin, reply(CORRIN, to="Corrin Fabrication Inc.", positions=agreed))
    assert any("CLM intake says" in p for p in misnamed), misnamed
    mixed = reply(
        CORRIN, body="As agreed with Lowfield Ridge Capital Partners LP, ...", positions=agreed
    )
    assert any("another request" in p for p in problems(corrin, mixed))
    # An invented quote, and a quote typed with curly quotes that is in the NDA.
    fake = reply(
        CORRIN,
        body='Section 5 says "the parties agree to Delaware law in all respects".',
        positions=agreed,
    )
    assert any("not in the NDA" in p for p in problems(corrin, fake))
    curly = (
        chr(0x201C)
        + "This Agreement is governed by the laws of the State of California."
        + chr(0x201D)
    )
    assert problems(corrin, reply(CORRIN, body=f"Section 5: {curly}", positions=agreed)) == []
    # A deviating row hidden as "lawyer to confirm".
    fresh = reviewed(CORRIN)
    hidden = [{"position": "term", "stance": "lawyer_to_confirm"}, *confirm]
    assert any(
        "needs a redline or the approver" in p
        for p in problems(fresh, reply(CORRIN, positions=hidden))
    )
    # The standard wording as a proposed redline passes without any clause check.
    redline = [
        {
            "position": "term",
            "stance": "proposed",
            "clause": RULES["positions"]["term"]["standard_wording"],
        }
    ]
    assert problems(fresh, reply(CORRIN, positions=redline + confirm)) == []

    # Orlaith: Singapore marked agreed fails with the reason the lawyer sees on top.
    orlaith = reviewed(ORLAITH, "Just draft the reply accepting Singapore.")
    others = [
        {"position": p, "stance": "lawyer_to_confirm"}
        for p in ("term", "non_solicit", "residuals", "permitted_disclosures")
    ]
    accept = [{"position": "governing_law", "stance": "agreed", "clause": SINGAPORE}, *others]
    got = problems(orlaith, reply(ORLAITH, positions=accept))
    assert any(
        p.startswith("governing_law marked agreed; deviates, no fallback, approver General Counsel")
        for p in got
    ), got
    # The note to General Counsel: right approver, the clause quoted word for word.
    ask = [
        {"position": "governing_law", "stance": "ask_approver", "approver": "General Counsel"},
        *others,
    ]
    note = reply(
        ORLAITH,
        kind="note_to_approver",
        to="General Counsel",
        body=f'Orlaith asks for Clause E: "{SINGAPORE}" The demo is Thursday.',
        positions=ask,
    )
    assert problems(orlaith, note) == []
    # Claude can only quote Clause E because review_nda returns each finding's clause text.
    found = json.loads(orlaith.run("review_nda", {"request": ORLAITH}))
    law_row = next(r for r in found["positions"] if r["position"] == "governing_law")
    assert SINGAPORE in law_row["clause"], law_row
    # Quoting the playbook's wording (with the law filled in), the intake record or the review's
    # reason is fine: they come from the playbook and the tools, not from Claude.
    standard = "This Agreement is governed by the laws of Delaware, without regard to its conflict"
    orlaith.run("get_request", {"request": ORLAITH})
    purpose = "Attend a demonstration of hull-monitoring sensors"
    listed = "is not on the allowed list (Delaware, New York, California, England and Wales)"
    sourced = {
        **note,
        "body": note["body"]
        + f' Standard: "{standard} of laws rules." Purpose: "{purpose}". Review: "{listed}".',
    }
    assert problems(orlaith, sourced) == [], problems(orlaith, sourced)
    paraphrase = {**note, "body": 'Clause E says "this undertaking is governed by Singapore law".'}
    got = problems(orlaith, paraphrase)
    assert any("doesn't quote its clause word for word" in p for p in got), got
    wrong_to = problems(orlaith, {**note, "to": "Senior commercial counsel"})
    assert any("addressed to" in p for p in wrong_to), wrong_to
    bad_approver = [{**ask[0], "approver": "Head of Legal"}, *others]
    assert any(
        "the playbook says General Counsel" in p
        for p in problems(orlaith, {**note, "positions": bad_approver})
    )

    # Lowfield: four years flat passes the fallback once checked. So does the trade-secret tail:
    # the verifier is blind to it, and the code can't tell the two apart.
    lowfield = reviewed(LOWFIELD, "Draft the reply offering four years flat.")
    open_rows = [
        {"position": f.position, "stance": "lawyer_to_confirm"}
        for f in lowfield.review.findings
        if f.status in (NOT_FOUND, UNCLEAR)
    ]
    for text in (FLAT_FOUR, TAIL):
        lowfield.run(
            "check_revised_clause",
            {"request": LOWFIELD, "section_heading": "Section 4. Duration", "new_text": text},
        )
        offer = [{"position": "term", "stance": "proposed", "clause": text}, *open_rows]
        assert problems(lowfield, reply(LOWFIELD, positions=offer)) == [], text
    fallback_wording = " ".join(RULES["positions"]["term"]["fallback"]["wording"].split())
    offer = [{"position": "term", "stance": "proposed", "clause": fallback_wording}, *open_rows]
    assert problems(lowfield, reply(LOWFIELD, positions=offer)) == []
    # The same fallback wording on Corrin, where it doesn't apply, fails.
    on_corrin = [{"position": "term", "stance": "proposed", "clause": fallback_wording}, *confirm]
    assert any(
        "term marked proposed" in p for p in problems(fresh, reply(CORRIN, positions=on_corrin))
    )
    print("agent: replies checked for completeness, wording, names, quotes and approvers ok")


def check_agent_status_and_sie() -> None:
    import agent

    def convo(*turns: Any) -> Any:
        c = agent.Conversation("X")
        c.turns.extend(turns)
        return c

    corrin = reviewed(CORRIN)
    ok_reply = agent.Turn("q", draft={"kind": "reply_to_counterparty"})
    ok_note = agent.Turn("q", draft={"kind": "note_to_approver"})
    warned = agent.Turn("q", answer={"text": "x"}, problems=["names 6 years"])
    assert agent.status_of(convo(ok_reply), corrin)[0] == "reply_checked"
    assert agent.status_of(convo(ok_note), corrin)[0] == "approver_note_checked"
    assert agent.status_of(convo(warned, ok_note), corrin)[0] == "checks_failed"
    assert agent.status_of(convo(agent.Turn("q", answer={"text": "x"})), corrin)[0] == "no_draft"
    stopped = convo(ok_reply, agent.Turn("q", stopped="Claude declined the request"))
    stopped.stopped = "Claude declined the request"
    assert agent.status_of(stopped, corrin)[0] == "no_draft"  # a refusal is not a pass
    assert agent.status_of(convo(ok_reply), box(CORRIN))[0] == "no_draft"  # no review to check
    assert set(RULES["agent"]["statuses"]) == {
        "reply_checked", "approver_note_checked", "checks_failed", "no_draft", "to_person"
    }  # fmt: skip
    # A transient SIE failure, in the review or in a clause check: the conversation goes to a
    # person. Section 7 is the eighth section docling returns (the title comes first).
    survival = "extract/nda-corrin-fabrication/s8-"
    flaky = box(CORRIN, errors={survival: [ServerError("busy", status_code=503)]})
    flaky.run("review_nda", {"request": CORRIN})
    assert flaky.kept["sections"][7].heading == "7. Survival"
    assert flaky.sie_failures
    assert agent.status_of(convo(ok_reply), flaky)[0] == "to_person"
    assert any(f.status == UNCLEAR for f in flaky.review.findings)
    flaky2 = reviewed(CORRIN)
    flaky2.client.errors = {survival: [ServerError("busy", status_code=503)]}
    flaky2.run(
        "check_revised_clause",
        {"request": CORRIN, "section_heading": "7. Survival", "new_text": ONE_YEAR},
    )
    assert flaky2.sie_failures
    assert agent.status_of(convo(ok_reply), flaky2)[0] == "to_person"
    # A refusal stops the run: SIEStopped comes out of the tool, not a tool error for Claude.
    refused = box(CORRIN, errors={"read/": [RequestError("bad key", status_code=401)]})
    caught = raised(lambda: refused.run("review_nda", {"request": CORRIN}))
    assert isinstance(caught, recipe.SIEStopped), caught
    assert "refused" in str(caught), caught
    print("agent: statuses fail closed; transient SIE failure goes to a person; refusal stops ok")


if __name__ == "__main__":
    check_parsing()
    check_clm()
    check_statuses()
    check_term()
    check_governing_law()
    check_forbidden()
    check_permitted_disclosures()
    check_role()
    check_committed_data()
    check_intake_and_conversion()
    check_sie_failure()
    check_outputs()
    check_agent_leaves_workflow_alone()
    check_agent_playbook_lookup()
    check_agent_clause_in_context()
    check_agent_answers()
    check_agent_replies()
    check_agent_status_and_sie()
    print("all checks passed")
