"""Offline checks on the masking, the form parsing, the decision checks and the leak check.

No SIE or Claude call: the model replies below are hand-written stand-ins.

    uv run selfcheck.py
"""

from __future__ import annotations

from pathlib import Path

from server import (
    RESUMES_DIR,
    load_applicants,
    load_rules,
    mask,
    record_patterns,
    replace_spans,
    resume_lines,
    tables_to_lines,
)

from recipe import check_decision, expected_decision, identity_leaks, plain

RULES = load_rules()
APPLICANTS = load_applicants()


def check_data() -> None:
    for key, row in APPLICANTS.items():
        assert (RESUMES_DIR / row["resume_file"]).is_file(), f"{key}: no resume file"
        assert Path(row["resume_file"]).name == row["resume_file"], f"{key}: resume_file is a path"
    assert set(RULES["role"]["must_have"]) == {
        "epa_608_universal",
        "commercial_3_years",
        "drivers_license",
    }


def check_replace_spans() -> None:
    text = "marcus.bell@example.com"
    # The name part and the email start together; the longer one names the span.
    masked, counts = replace_spans(text, [(0, 6, "person"), (0, len(text), "email")])
    assert masked == "[email]", masked
    assert counts == {"email": 1}, counts
    masked, _ = replace_spans("ab cd ef", [(3, 5, "x"), (0, 2, "y")])
    assert masked == "[y] [x] ef", masked


def check_record_masking() -> None:
    record = APPLICANTS["A-105"]
    text = "TOMAS HERRERA\nCall Tomas at 555.013.7764 or (555) 013-7764.\nHerrera Plumbing, 2015"
    masked, counts = mask(text, record, RULES, found=[])
    assert "tomas" not in masked.lower(), masked
    assert "herrera" not in masked.lower(), masked
    assert "7764" not in masked, masked
    assert counts["phone"] == 2, counts
    # The record's own patterns are case-blind and whole-word only.
    patterns = [p for p, _ in record_patterns(record, RULES)]
    assert patterns, "no patterns from the record"


def check_model_spans_spread() -> None:
    """A name the model found once is masked everywhere it appears."""
    record = APPLICANTS["A-104"]
    text = (
        "References: Hal Brenner, Facilities Manager, (555) 019-4400\n"
        "Worked for Hal Brenner for two years."
    )
    start = text.index("Hal Brenner")
    stand_in = [(start, start + len("Hal Brenner"), "person")]
    masked, counts = mask(text, record, RULES, found=stand_in)
    assert "Brenner" not in masked, masked
    assert counts["person"] == 2, counts
    assert counts["phone"] == 1, counts


def check_forms_and_lines() -> None:
    html = (
        "# FORM\n<table>\n  <tr>\n    <td>NAME</td>\n    <td>[person]</td>\n  </tr>\n"
        "  <tr>\n    <td>EPA 608 CERTIFICATION</td>\n"
        "    <td>Yes - Type I, Type II and Type III</td>\n"
        "  </tr>\n</table>"
    )
    lines = resume_lines(tables_to_lines(html))
    assert lines == [
        "# FORM",
        "NAME: [person]",
        "EPA 608 CERTIFICATION: Yes - Type I, Type II and Type III",
    ], lines
    wrapped = (
        "- Run maintenance routes: rooftop units, make-up air\n  units and two chillers.\n- Next"
    )
    assert resume_lines(wrapped) == [
        "- Run maintenance routes: rooftop units, make-up air units and two chillers.",
        "- Next",
    ]


MASKED = """[person]
CERTIFICATIONS
- EPA Section 608 Universal certification
- Ohio driver's license, clean record
Lead Service Technician, Crestline Mechanical 2021 - present
- Two 60-ton air-cooled chillers and **rooftop units**"""


def decision(**changes: object) -> dict[str, object]:
    base: dict[str, object] = {
        "applicant_id": "A-101",
        "decision": "advance",
        "must_haves": [
            {
                "requirement_id": "epa_608_universal",
                "verdict": "met",
                "quote": "EPA Section 608 Universal certification",
            },
            {
                "requirement_id": "commercial_3_years",
                "verdict": "met",
                "quote": "Two 60-ton air-cooled chillers and rooftop units",
            },
            {
                "requirement_id": "drivers_license",
                "verdict": "met",
                "quote": "Ohio driver's license, clean record",
            },
        ],
        "note": "Meets every must-have.",
    }
    return base | changes


def check_decisions() -> None:
    assert check_decision(decision(), "A-101", MASKED, RULES) == []
    assert plain("**Rooftop  units**") == "rooftop units"
    wrong_quote = decision(
        must_haves=[
            {
                "requirement_id": "epa_608_universal",
                "verdict": "met",
                "quote": "608 Universal, 2015",
            },
            *decision()["must_haves"][1:],  # type: ignore[index]
        ]
    )
    problems = check_decision(wrong_quote, "A-101", MASKED, RULES)
    assert any("isn't in the resume text" in p for p in problems), problems
    unclear = decision(
        must_haves=[
            *decision()["must_haves"][:2],  # type: ignore[index]
            {"requirement_id": "drivers_license", "verdict": "unclear", "quote": ""},
        ]
    )
    problems = check_decision(unclear, "A-101", MASKED, RULES)
    assert problems == ["With these verdicts the decision is needs_person, not advance."], problems
    missing = decision(must_haves=decision()["must_haves"][:1])  # type: ignore[index]
    assert any("missing" in p for p in check_decision(missing, "A-101", MASKED, RULES))
    assert check_decision(decision(), "A-101", "", RULES)
    assert expected_decision(["met", "unclear", "not_met"]) == "reject"
    assert expected_decision(["met", "unclear"]) == "needs_person"
    assert expected_decision(["met", "met"]) == "advance"


def check_leaks() -> None:
    record = APPLICANTS["A-101"]
    assert identity_leaks(MASKED, record, RULES) == []
    assert identity_leaks("Call MARCUS on 555 014 2290", record, RULES) == ["name", "phone"]
    assert identity_leaks("reach marcus.bell.hvac@example.com", record, RULES) == ["email", "name"]


def main() -> None:
    check_data()
    check_replace_spans()
    check_record_masking()
    check_model_spans_spread()
    check_forms_and_lines()
    check_decisions()
    check_leaks()
    print("selfcheck passed")


if __name__ == "__main__":
    main()
