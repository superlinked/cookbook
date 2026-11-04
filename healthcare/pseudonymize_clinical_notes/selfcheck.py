"""Check the recipe's EHR lookups, identity check, date and clinical-number rules, sweep, names,
draws and SIE error rule offline.

The checks use hand-written strings, the committed data/ and hand-written stand-in model
replies. Nothing here calls SIE or replays a recorded response. Run it with
`uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import csv
import dataclasses
import functools
import io
import random
import shutil
import tempfile
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

import ehr
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

import recipe

RULES = recipe.load_rules()
REQUEST = recipe.load_request()
RECORDS = ehr.Ehr()
PII = RULES["models"]["pii"]

Reply = dict[str, Any] | BaseException | Callable[[str], dict[str, Any]]

# What a PII model would tag in the committed notes: the people and places no EHR field holds.
# Hand-written; the live run's spans are in the README.
STAND_IN_TAGS = (
    ("Ines Vance", "person"),
    ("Corrimore Freight", "organization"),
    ("27 Quillfeather Lane, Harrowgate, MA", "street address"),
    ("Ms. Vance", "person"),
    ("Rafael Ostrander", "person"),
    ("Quill", "person"),
    ("Willa Brightwater", "person"),
    ("willa.brightwater@example.com", "email address"),
    ("6 Tamarack Hollow Road, Pinecliff, NH", "street address"),
    ("Northfield Clinical Laboratories", "organization"),
    ("Hollis Merriweather", "person"),
)


def tags(*phrases: tuple[str, str]) -> Callable[[str], dict[str, Any]]:
    """A stand-in PII reply: every occurrence of each (phrase, label) in the window."""

    def reply(window: str) -> dict[str, Any]:
        entities = []
        for phrase, label in phrases:
            start = window.find(phrase)
            while start >= 0:
                end = start + len(phrase)
                entities.append(
                    {"start": start, "end": end, "label": label, "text": phrase, "score": 0.9}
                )
                start = window.find(phrase, end)
        return {"entities": entities}

    return reply


NOTHING = tags()
STAND_IN_REPLY = tags(*STAND_IN_TAGS)


class StandIn:
    """A stand-in SIE client: hands out the replies in order, one per extract call."""

    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.options: list[Any] = []

    def extract(self, model: str, item: Any, **kwargs: Any) -> Any:
        assert model == PII, model
        self.options.append(kwargs.get("options"))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return reply(str(item["text"]))
        return reply


def seeded(*notes: tuple[str, str, str, str], identifiers: str = "") -> ehr.Ehr:
    """An EHR with the committed patients and these notes as (patient_id, note_type, date, text)."""
    folder = Path(tempfile.mkdtemp())
    (folder / "notes").mkdir()
    shutil.copy(recipe.DATA_DIR / "ehr_patients.csv", folder / "ehr_patients.csv")
    ids = (recipe.DATA_DIR / "ehr_identifiers.csv").read_text(encoding="utf-8") + identifiers
    (folder / "ehr_identifiers.csv").write_text(ids, encoding="utf-8")
    rows = []
    for i, (patient_id, note_type, when, text) in enumerate(notes, start=1):
        (folder / "notes" / f"n{i}.txt").write_text(text, encoding="utf-8")
        rows.append(f"N-{i},{patient_id},{note_type},{when},notes/n{i}.txt")
    header = ",".join(ehr.NOTE_COLUMNS)
    (folder / "ehr_notes.csv").write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return ehr.Ehr(folder)


def request(**changes: Any) -> recipe.Request:
    return dataclasses.replace(REQUEST, **changes)


def run(
    records: ehr.Ehr,
    *replies: Reply,
    req: recipe.Request | None = None,
    seed: int = 7,
    rules: dict[str, Any] | None = None,
) -> recipe.Result:
    """Run the recipe with stand-in replies (one per note window) and a seeded draw."""
    client = StandIn(*replies)
    with contextlib.redirect_stdout(io.StringIO()):
        result = recipe.run(
            cast(SIEClient, client), rules or RULES, req or REQUEST, records, random.Random(seed)
        )
    assert not client.replies, f"{len(client.replies)} replies left unused"
    # Every call asks SIE to fail on a window too long to read whole, never to cut it short.
    assert client.options == [{"overflow_policy": "error"}] * len(client.options), client.options
    return result


def one(text: str, *replies: Reply, patient: str = "P-1001", **kwargs: Any) -> recipe.Work:
    """Run one hand-written note filed under `patient` and return it."""
    records = seeded((patient, "discharge_summary", "2026-09-08", text))
    return run(records, *(replies or (NOTHING,)), **kwargs).works[0]


def committed(*replies: Reply, req: recipe.Request | None = None, seed: int = 7) -> recipe.Result:
    return run(RECORDS, *(replies or (STAND_IN_REPLY,) * 4), req=req, seed=seed)


def refused(call: Callable[[], object]) -> str:
    """The ValueError message the call raises; fails when it raises nothing."""
    try:
        call()
    except ValueError as error:
        return str(error)
    raise AssertionError("the call was not refused")


def check_lookups() -> None:
    teodora = RECORDS.get_patient("P-1001")
    assert teodora is not None
    assert teodora.mrn == "00482913"
    assert teodora.dob == date(1959, 4, 22)
    quill = RECORDS.get_patient("P-1002")
    assert quill is not None
    assert quill.aliases == ("Bart Quill",), quill
    assert ("health_plan_member_id", "QHP-77310-4428-01") in quill.other
    assert RECORDS.get_patient("P-9999") is None
    # An MRN is matched by its digits, exactly: the dashed spelling finds the patient, a
    # neighbouring MRN finds nobody.
    assert [p.patient_id for p in RECORDS.find_by_mrn("0048-2913")] == ["P-1001"]
    assert RECORDS.find_by_mrn("00482918") == []
    assert RECORDS.find_by_mrn("MRN") == []
    # The cohort's notes in cohort order, then date; another note type, a date outside the
    # window and a patient outside the cohort are left out.
    records = seeded(
        ("P-1002", "home_nursing_note", "2026-10-02", "b"),
        ("P-1001", "psychotherapy_note", "2026-09-10", "x"),
        ("P-1001", "referral_letter", "2026-09-22", "a2"),
        ("P-1001", "discharge_summary", "2026-08-31", "x"),
        ("P-1003", "discharge_summary", "2026-09-10", "x"),
        ("P-1001", "discharge_summary", "2026-09-08", "a1"),
    )
    notes = records.notes_for(
        ["P-1001", "P-1002"], list(REQUEST.note_types), REQUEST.start, REQUEST.end
    )
    assert [n.text for n in notes] == ["a1", "a2", "b"], [n.text for n in notes]
    # A note path that leads out of the seed's folder is refused.
    escape = seeded()
    escape.db.execute(
        "INSERT INTO notes VALUES ('N-9', 'P-1001', 'referral_letter', "
        "'2026-09-10', '../../etc/hosts')"
    )
    why = refused(
        lambda: escape.notes_for(["P-1001"], ["referral_letter"], REQUEST.start, REQUEST.end)
    )
    assert "outside" in why, why
    print("lookups: patients, MRN by digits, notes by cohort, type and window ok")


def check_request() -> None:
    assert REQUEST.request_id == "QI-2026-031"
    assert REQUEST.cohort == ("P-1001", "P-1002")
    assert REQUEST.release == {
        "dates": "shift",
        "zip": "three_digits",
        "ages_over_89": "group_90_plus",
    }
    text = recipe.REQUEST_PATH.read_text(encoding="utf-8")
    folder = Path(tempfile.mkdtemp())
    for old, new, why in (
        ("dates: shift", "dates: keep", "release.dates"),
        ("zip: three_digits", "zip: five_digits", "release.zip"),
        (
            "  ages_over_89: group_90_plus\n",
            "  ages_over_89: group_90_plus\n  names: keep\n",
            "doesn't know",
        ),
        ("end: 2026-10-31", "end: 2026-08-01", "ends"),
    ):
        path = folder / "request.yaml"
        path.write_text(text.replace(old, new), encoding="utf-8")
        message = refused(functools.partial(recipe.load_request, path))
        assert why in message, message
    # A cohort patient the EHR doesn't have stops the run before any SIE call.
    message = refused(lambda: run(RECORDS, req=request(cohort=("P-1001", "P-9999"))))
    assert "P-9999" in message, message
    print("request: release rules, window and cohort checked before any call ok")


def check_draw() -> None:
    first = recipe.draw_pseudonyms(REQUEST.cohort, RULES, random.Random(1))
    again = recipe.draw_pseudonyms(REQUEST.cohort, RULES, random.Random(1))
    other = recipe.draw_pseudonyms(REQUEST.cohort, RULES, random.Random(2))
    assert first == again
    assert set(first.study_ids.values()).isdisjoint(other.study_ids.values())
    assert first.offsets != other.offsets
    low, high = RULES["date_shift"]["min_days"], RULES["date_shift"]["max_days"]
    signs = set()
    for seed in range(40):
        drawn = recipe.draw_pseudonyms(REQUEST.cohort, RULES, random.Random(seed))
        assert all(low <= abs(days) <= high for days in drawn.offsets.values()), drawn
        assert len(set(drawn.study_ids.values())) == 2
        signs |= {days > 0 for days in drawn.offsets.values()}
        # A study ID is never built from an identifier.
        for study_id in drawn.study_ids.values():
            assert not any(part in study_id for part in ("0048", "2913", "0051", "1001"))
    assert signs == {True, False}
    # Two requests for the same cohort: different study IDs, offsets and shifted dates, and the
    # note text agrees on everything but those.
    a, b = committed(seed=1), committed(req=request(request_id="RS-2026-044"), seed=2)
    rows_a, rows_b = recipe.manifest_rows(a), recipe.manifest_rows(b)
    assert {r["study_id"] for r in rows_a}.isdisjoint({r["study_id"] for r in rows_b})
    assert [r["note_date"] for r in rows_a] != [r["note_date"] for r in rows_b]
    assert all(r["request_id"] == "RS-2026-044" for r in rows_b)
    print("draw: study IDs and offsets drawn per request, in range, never from an MRN ok")


def check_dates_and_numbers() -> None:
    # Blood pressures and dose fractions stay; a date with no year next to them is removed and
    # the note escalates.
    work = one("Patient: Teodora Vance\nBP 98/64, take 1/2 tablet daily, 98/64 mmHg.\n")
    assert work.status == recipe.CHECKS_PASSED, work.leaks + work.errors
    assert "BP 98/64, take 1/2 tablet daily, 98/64 mmHg." in work.redacted
    work = one("Patient: Teodora Vance\nBP 98/64, take 1/2 tablet, seen on 9/29.\n")
    assert "BP 98/64, take 1/2 tablet, seen on [DATE_1]." in work.redacted, work.redacted
    assert work.status == recipe.ESCALATE, work.errors
    assert "can't shift" in work.errors[0], work.errors
    # A slash pair with no clinical label is not waved through: the sweep holds the note.
    work = one("Patient: Teodora Vance\nPressure 98/64 this morning.\n")
    assert work.status == recipe.ESCALATE
    assert work.leaks == ["line 2: date_without_year still readable"], work.leaks
    # A fraction with no dose unit is a date (Jan 2); a glued "1/2tab" is too.
    for text in ("take 1/2 daily", "take 1/2tab"):
        work = one(f"Patient: Teodora Vance\n{text}\n")
        assert "[DATE_" in work.redacted, work.redacted
        assert work.status == recipe.ESCALATE, work.redacted
    # Ages over 89 in every form, glued ones too; 67 stays.
    work = one(
        "Patient: Teodora Vance\nAge: 92. A 92yo, 93 y/o, aged 94, 95 years old; 67-year-old."
    )
    assert work.redacted.count("[AGE 90+]") == 5, work.redacted
    assert "67-year-old" in work.redacted
    assert work.status == recipe.CHECKS_PASSED
    # Full dates shift by the patient's offset and keep their gaps; the DOB is removed, also
    # written with no "DOB" label (the EHR knows it), never shifted.
    work = one("Patient: Teodora Vance\nAdmitted 09/03/2026, discharged 2026-09-08.\n04/22/1959")
    shifted = recipe.SHIFTED_DATE.findall(work.redacted)
    gap = date.fromisoformat(shifted[1]) - date.fromisoformat(shifted[0])
    assert len(shifted) == 2, work.redacted
    assert gap.days == 5, work.redacted
    assert work.redacted.endswith("[DOB_1]"), work.redacted
    assert date.fromisoformat(shifted[0]) - date(2026, 9, 3) == timedelta(
        days=cast(int, work.offset_days)
    )
    # The release rules decide: with dates removed, nothing is shifted and a yearless date is
    # no reason to escalate; ZIP and age can be removed too.
    req = request(release={"dates": "remove", "zip": "remove", "ages_over_89": "remove"})
    work = one("Patient: Teodora Vance\nOn 09/03/2026 and 9/29. Age: 92. ZIP 02134", req=req)
    assert recipe.SHIFTED_DATE.search(work.redacted) is None
    assert "[DATE_1] and [DATE_2]" in work.redacted
    assert "[AGE_1]" in work.redacted
    assert "[ZIP_1]" in work.redacted, work.redacted
    assert work.status == recipe.CHECKS_PASSED, work.errors
    # ZIP: three digits, or 000 for a restricted area.
    work = one("Patient: Teodora Vance\nMA 02134 and NH 03601")
    assert "MA [ZIP 021] and NH [ZIP 000]" in work.redacted, work.redacted
    print("dates and numbers: BP and doses kept, dates shifted or removed, ages, ZIPs ok")


def check_identity() -> None:
    # The note matches its patient: the MRN in another spelling and the name "SURNAME, FIRST".
    work = one("Re: Vance, Teodora, MRN 0048-2913\nAdmitted 09/03/2026.")
    assert work.status == recipe.CHECKS_PASSED, work.errors
    assert work.offset_days is not None
    # Near miss: a neighbouring MRN, one digit off, is not this patient's.
    work = one("Patient: Teodora Vance\nMRN: 00482918\nCollected 09/03/2026")
    assert work.status == recipe.ESCALATE
    assert work.offset_days is None
    assert any("isn't this patient's (one the EHR doesn't have)" in e for e in work.errors)
    assert "Collected [DATE_1]" in work.redacted, work.redacted
    # Near miss: a name one letter off, or only part of the name, in the patient field.
    for name in ("Teodora Vane", "T. Vance", "Vance"):
        work = one(f"Patient: {name}\nSeen 09/03/2026")
        assert work.status == recipe.ESCALATE, name
        assert work.offset_days is None, name
        assert "a patient field names someone other than the EHR patient" in work.errors[0]
    # The lab fax filed under Teodora names another EHR patient: escalated, held out, and that
    # patient's identifiers are replaced even with no model help.
    fax = (recipe.DATA_DIR / "notes" / "04-lab-fax.txt").read_text(encoding="utf-8")
    work = one(fax)
    assert work.status == recipe.ESCALATE
    assert work.offset_days is None
    assert any("another EHR patient's" in e for e in work.errors), work.errors
    for value in ("Hollis Merriweather", "00993021", "1971-11-02", "2026-10-01"):
        assert value not in work.redacted, value
    # Another cohort patient's MRN in a note filed under Teodora.
    work = one("Patient: Teodora Vance\nRoommate MRN 00517746.")
    assert work.status == recipe.ESCALATE
    assert "another EHR patient's" in work.errors[0]
    print("identity: MRN and patient field checked against the EHR patient ok")


def check_sweep() -> None:
    # A bracket that was already in the note is read like any other text: an SSN in it that
    # neither the model nor a pattern replaced escalates the note.
    work = one("Patient: Teodora Vance\n[SSN: 123-45-6789]\n")
    assert work.status == recipe.ESCALATE
    assert work.leaks == ["line 2: ssn still readable"]
    # Text shaped like one of our placeholders isn't skipped either.
    work = one("Patient: Teodora Vance\nSee [EMAIL_1 tv@example.org]\n")
    assert work.leaks == ["line 2: email still readable"], work.leaks
    # Our own placeholders are skipped, and a bracketed phone is replaced like any other.
    work = one("Patient: Teodora Vance\n[Emergency contact: 603-555-0187]\n")
    assert work.redacted.endswith("[Emergency contact: [PHONE_1]]\n"), work.redacted
    assert work.status == recipe.CHECKS_PASSED, work.leaks
    # An MRN with no "MRN" label, of a patient the recipe doesn't know, escalates.
    work = one("Patient: Teodora Vance\nOutside med rec no. 77104452 on file.")
    assert work.leaks == ["line 2: long_number still readable"], work.leaks
    # An unknown "Mrs. Surname" no one tagged escalates; allowlisted clinicians don't.
    work = one("Patient: Teodora Vance\nMrs. Hale called. Dr. Selwyn Achebe, Dr. Okwuosa.")
    assert work.leaks == ["line 2: honorific_name still readable"], work.leaks
    # Reasons and the log never quote a value; a formula-looking cell is quoted.
    assert recipe.csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert recipe.csv_safe(3) == "3"
    print("sweep: original brackets read, only our placeholders skipped ok")


def check_names_and_overlap() -> None:
    # The patient's known identifiers are replaced with no model help, in any spelling: name,
    # first name, surname, "SURNAME, FIRST", MRN, phone, street and city.
    text = (
        "Patient: Teodora Vance\nTEODORA saw VANCE, TEODORA; Teodora called from 508.555.0187.\n"
        "Lives at 27 Quillfeather Lane in Harrowgate. MRN 0048 2913.\n"
    )
    work = one(text)
    for value in ("Teodora", "TEODORA", "VANCE", "0187", "Quillfeather", "Harrowgate", "2913"):
        assert value not in work.redacted, (value, work.redacted)
    assert work.redacted.count("[PATIENT_") == 4, work.redacted
    # One model span holding a name and a phone: the pattern takes the phone, the name is still
    # replaced, and nothing of either is left.
    span = "Ines Vance (phone 508-555-0164)"
    work = one(f"Patient: Teodora Vance\nDaughter {span}.", tags((span, "person")))
    assert "Ines" not in work.redacted, work.redacted
    assert "0164" not in work.redacted, work.redacted
    assert "[PHONE_1]" in work.redacted, work.redacted
    # A model span that runs over a date: the date is shifted, and the rest of the span, which no
    # pattern decides, is still replaced rather than dropped with it.
    span = "case ZX-88-1234 opened 09/03/2026"
    work = one(f"Patient: Teodora Vance\n{span}", tags((span, "unique identifier")))
    assert "ZX-88" not in work.redacted
    assert recipe.SHIFTED_DATE.search(work.redacted)
    # A model span over a date ("Discharged: 09/08/2026" as a date of birth): the date is still
    # shifted and the label word stays readable.
    span = "Discharged: 09/08/2026"
    work = one(f"Patient: Teodora Vance\n{span}", tags((span, "date of birth")))
    assert recipe.SHIFTED_DATE.search(work.redacted)
    assert "Discharged:" in work.redacted
    # A name that fits two people goes to review with both candidates; "Mr. Quill" joins the
    # patient; the model's "Quill" in another note gets the same placeholder.
    records = seeded(
        ("P-1001", "discharge_summary", "2026-09-08", "Patient: Teodora Vance\nInes Vance."),
        ("P-1001", "referral_letter", "2026-09-22", "Re: Teodora Vance\nMs. Vance called."),
        ("P-1002", "home_nursing_note", "2026-10-02", "Patient: Bartholomew Quill\nMr. Quill."),
    )
    reply = tags(("Ines Vance", "person"), ("Ms. Vance", "person"), ("Quill", "person"))
    result = run(records, reply, reply, reply)
    teo, letter, quill = result.works
    assert teo.status == recipe.CHECKS_PASSED, teo.reviews
    assert letter.status == recipe.REVIEW
    assert "a name that could be" in letter.reviews[0]
    candidates = {result.patients["P-1001"].placeholder, "[PERSON_1]"}
    assert all(c in letter.reviews[0] for c in candidates), letter.reviews
    patient = result.patients["P-1002"].placeholder
    assert quill.redacted.endswith(f"Mr. {patient}."), quill.redacted
    # A cohort patient's name wins over the allowlist: a patient named Ngozi Achebe makes every
    # "Achebe" a name to replace, and the note goes to review to check who is meant.
    rules = {**RULES, "allowlist": {**RULES["allowlist"], "clinicians": ["Achebe"]}}
    records = seeded(
        ("P-1001", "discharge_summary", "2026-09-08", "Patient: Teodora Vance\nDr. Achebe saw her.")
    )
    work = run(records, NOTHING, rules=rules).works[0]
    assert "Achebe" in work.redacted
    assert work.status == recipe.CHECKS_PASSED
    renamed = [
        "patient_id,name,mrn,dob,street,city,state,zip,phone",
        "P-1001,Ngozi Achebe,00482913,1959-04-22,27 Quillfeather Lane,Harrowgate,MA,02134,"
        "508-555-0187",
        "P-1002,Bartholomew Quill,00517746,1934-03-11,6 Tamarack Hollow Road,Pinecliff,NH,03601,"
        "603-555-0142",
    ]
    (records.data_dir / "ehr_patients.csv").write_text("\n".join(renamed) + "\n", encoding="utf-8")
    (records.data_dir / "notes" / "n1.txt").write_text(
        "Patient: Ngozi Achebe\nDr. Achebe saw her.", encoding="utf-8"
    )
    work = run(ehr.Ehr(records.data_dir), NOTHING, rules=rules).works[0]
    assert "Achebe" not in work.redacted, work.redacted
    assert work.status == recipe.REVIEW, work.reviews
    print("names and overlap: known identifiers, split spans, ambiguity, patient over allowlist ok")


def check_committed() -> None:
    # The committed request and notes with a stand-in reply shaped like the live model's: the
    # expected statuses come out.
    result = committed()
    by_type = {w.note.note_type: w for w in result.works}
    status = {kind: w.status for kind, w in by_type.items()}
    assert status == {
        "discharge_summary": recipe.CHECKS_PASSED,
        "referral_letter": recipe.REVIEW,
        "outside_lab_result": recipe.ESCALATE,
        "home_nursing_note": recipe.ESCALATE,
    }, status
    summary, letter = by_type["discharge_summary"], by_type["referral_letter"]
    nursing, fax = by_type["home_nursing_note"], by_type["outside_lab_result"]
    # 01 and 02 share an offset, so the follow-up date in 01 is the letter's date in 02.
    assert summary.offset_days == letter.offset_days is not None
    follow_up = recipe.SHIFTED_DATE.findall(summary.redacted)[-1]
    assert letter.redacted.splitlines()[3] == f"Date: [DATE {follow_up}]", letter.redacted
    assert "BP 98/64" in summary.redacted
    assert "1/2 tablet" in summary.redacted
    assert "[MRN_1]" in summary.redacted
    assert "[MRN_1]" in letter.redacted
    assert "[DOB_1]" in summary.redacted
    assert "[DOB_1]" in letter.redacted
    assert "[ZIP 021]" in summary.redacted
    assert "[ZIP 000]" in nursing.redacted
    assert "67-year-old" in summary.redacted
    assert "[AGE 90+]-year-old" in nursing.redacted
    for kept in ("Dr. Selwyn Achebe", "Hashimoto's thyroiditis", "since 2019"):
        assert kept in summary.redacted, kept
    assert "W. [PERSON_" in nursing.redacted
    assert "0187]" not in nursing.redacted
    assert any("can't shift" in e for e in nursing.errors)
    assert not nursing.leaks
    assert fax.offset_days is None
    assert any("misfiled" in e for e in fax.errors)
    # The outputs: the extract holds the notes that didn't escalate; no log or status row holds
    # a value it replaced; counts add up.
    folder = Path(tempfile.mkdtemp())
    recipe.write_outputs(folder, result)
    extract = sorted(p.name for p in (folder / "extract" / "notes").iterdir())
    held = sorted(p.name for p in (folder / "held").iterdir())
    assert extract == sorted([summary.file, letter.file])
    assert held == sorted([nursing.file, fax.file])
    with (folder / "extract" / "manifest.csv").open(encoding="utf-8") as handle:
        manifest = list(csv.DictReader(handle))
    assert [r["note_file"] for r in manifest] == [summary.file, letter.file]
    assert all(r["approval_reference"] == "PO-2026-118" for r in manifest)
    with (folder / "replacement-log.csv").open(encoding="utf-8") as handle:
        log = list(csv.DictReader(handle))
    with (folder / "note-status.csv").open(encoding="utf-8") as handle:
        statuses = list(csv.DictReader(handle))
    for row in statuses:
        assert int(row["replacements"]) == sum(r["note_file"] == row["note_file"] for r in log)
    assert len(manifest) + len(held) == len(result.works) == 4
    shared = (folder / "replacement-log.csv").read_text(encoding="utf-8")
    shared += (folder / "note-status.csv").read_text(encoding="utf-8")
    shared += (folder / "extract" / "manifest.csv").read_text(encoding="utf-8")
    for work in result.works:
        for span in work.spans:
            assert span.text not in shared or (span.text.isdigit() and len(span.text) < 3), (
                span.text
            )
        assert work.note.note_id not in shared
        assert work.patient.patient_id not in shared
    key = (folder / "private" / "key.json").read_text(encoding="utf-8")
    assert "Teodora Vance" in key
    assert "offset_days" in key
    assert set(recipe.STATUS_FIELDS) >= {"status"}
    assert {
        recipe.CHECKS_PASSED,
        recipe.REVIEW,
        recipe.ESCALATE,
    } == {"checks_passed", "review", "escalate"}
    print("committed: expected statuses, extract vs held, no value in the shared files ok")


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
    # A transient failure escalates that note as not read; its reason names the error class and
    # never echoes SIE's message into note-status.csv. The others run.
    long = "connection refused " + "x" * 300
    prefix = "window 1 was not read: the SIE call failed: "
    for error in [*TRANSIENT, SIEConnectionError(long)]:
        works = committed(error, STAND_IN_REPLY, STAND_IN_REPLY, STAND_IN_REPLY).works
        assert works[0].status == recipe.ESCALATE
        why = works[0].errors[0]
        assert why.startswith(prefix), why
        assert not works[1].errors, works[1].errors
        assert str(error) not in why, why
    assert why == prefix + "SIEConnectionError; the run summary has the message", why
    # A reply missing `entities` is a failed call on the same path, not "nothing found".
    works = committed(
        {"error": {"code": "x"}}, STAND_IN_REPLY, STAND_IN_REPLY, STAND_IN_REPLY
    ).works
    assert works[0].errors[0] == prefix + "SIEError; the run summary has the message"
    works = committed({"id": "no entities"}, STAND_IN_REPLY, STAND_IN_REPLY, STAND_IN_REPLY).works
    assert works[0].status == recipe.ESCALATE
    # An empty list is a real "nothing found": the rules decide, the call counts as a success.
    works = committed({"entities": []}, STAND_IN_REPLY, STAND_IN_REPLY, STAND_IN_REPLY).works
    assert not works[0].errors, works[0].errors
    # A span whose offsets don't match its text escalates the note.
    bad = {"entities": [{"start": 0, "end": 4, "label": "person", "text": "nope"}]}
    assert (
        committed(bad, STAND_IN_REPLY, STAND_IN_REPLY, STAND_IN_REPLY).works[0].status
        == recipe.ESCALATE
    )

    # Every call failed: the run stops with the full message and nothing is returned to write.
    message = stopped(functools.partial(committed, *[SIEConnectionError(long)] * 4))
    assert message.startswith(f"Every SIE call failed (4 of 4 to {PII})"), message
    assert long in message
    assert "Check SIE_BASE_URL and your network." in message
    assert "No outputs were written." in message
    message = stopped(functools.partial(committed, *[ServerError("busy", status_code=503)] * 4))
    assert "may still be starting or SIE is busy; run again in a few minutes." in message
    # A refusal on a later call stops the run at once, with SIE's full message.
    message = stopped(
        functools.partial(
            committed, STAND_IN_REPLY, InsufficientCreditsError("no credits " + "y" * 300)
        )
    )
    assert message.startswith(f"SIE refused the call to {PII}, "), message
    assert "InsufficientCreditsError (HTTP 402, code INSUFFICIENT_CREDITS)" in message, message
    assert "y" * 300 in message
    assert "SIE_API_KEY" in message
    message = stopped(functools.partial(committed, ModelLoadFailedError("gated", permanent=True)))
    assert message.startswith(f"SIE can't load {PII}, ModelLoadFailedError"), message
    message = stopped(functools.partial(committed, RequestError("unexpected body")))
    assert "SIE_API_KEY" not in message, message
    assert "SIE_BASE_URL" in message, message
    # A bug in our own code is not hidden, and main turns only SIEStopped into a clean exit.
    caught = raised(
        lambda: committed(TypeError("bad argument"), STAND_IN_REPLY, STAND_IN_REPLY, STAND_IN_REPLY)
    )
    assert type(caught) is TypeError, f"TypeError became {type(caught).__name__}"

    def bug() -> None:
        raise RuntimeError("a bug in recipe.py")

    def refusal() -> None:
        raise recipe.SIEStopped("SIE refused")

    assert type(raised(lambda: recipe.stop_on_refusal(bug))) is RuntimeError
    caught = raised(lambda: recipe.stop_on_refusal(refusal))
    assert type(caught) is SystemExit
    assert str(caught) == "Stopped: SIE refused"
    print("sie failure: transient errors escalate the note; refusals and all-failed stop ok")


if __name__ == "__main__":
    check_lookups()
    check_request()
    check_draw()
    check_dates_and_numbers()
    check_identity()
    check_sweep()
    check_names_and_overlap()
    check_committed()
    check_sie_failure()
