"""Check the recipe's reading of the note, the chart comparison and the statuses offline.

The checks use hand-written strings, the committed data/ and hand-written stand-in model
replies. Nothing here calls SIE or replays a recorded response. Run it with
`uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import copy
import csv
import io
import re
import shutil
import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, cast

from ehr import EHR, ChartAllergy, ChartMedication, ChartProblem
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
DATA = recipe.DATA_DIR
CODES = recipe.load_codes(RULES)

# --- Hand-written stand-in replies ------------------------------------------------------------

# What a well-behaved GLiNER2 returns for each note's list schema.
LISTS: dict[str, dict[str, Any]] = {
    "note-01": {
        "problems": ["Type 2 diabetes mellitus without complications", "Essential hypertension"],
        "medications": ["Metformin 500 mg", "Lisinopril 10 mg"],
        "allergies": [],
        "follow_up": "Return in 3 months with repeat A1c",
    },
    "note-02": {
        "problems": ["Hypothyroidism", "Gastroesophageal reflux disease without esophagitis"],
        "medications": ["Levothyroxine", "Omeprazole"],
        "allergies": [],
        "follow_up": "6 weeks after TSH result",
    },
    "note-03": {
        "problems": ["Essential hypertension", "Hyperlipidemia"],
        "medications": ["Amlodipine", "Atorvastatin"],
        "allergies": [],
        "follow_up": "Nurse blood pressure check in 2 weeks",
    },
    "note-04": {
        "problems": ["Streptococcal pharyngitis"],
        "medications": ["amoxicillin"],
        "allergies": ["Penicillin (hives)"],
        "follow_up": "Return if not improving in 48 hours",
    },
    "note-05": {
        "problems": ["Hyperlipidemia"],
        "medications": ["Simvastatin", "Atorvastatin"],
        "allergies": [],
        "follow_up": "Return in 8 weeks",
    },
    "note-06": {
        "problems": ["Mild intermittent asthma, uncomplicated", "Allergic rhinitis due to pollen"],
        "medications": ["Albuterol inhaler", "Cetirizine"],
        "allergies": ["Sulfa drugs"],
        "follow_up": "Return in 6 months or sooner if needed",
    },
    "note-07": {
        "problems": ["Plantar fasciitis", "Obesity"],
        "medications": ["Ibuprofen"],
        "allergies": [],
        "follow_up": "Return in 6 weeks",
    },
    "note-08": {
        "problems": ["Acute cystitis without hematuria"],
        "medications": ["Nitrofurantoin"],
        "allergies": [],
    },
    "note-09": {
        "problems": ["Essential hypertension"],
        "medications": ["Lisinopril", "losartan"],
        "allergies": [],
        "follow_up": "Blood pressure and potassium check in 2 weeks",
    },
    "note-10": {
        "problems": ["type 2 diabetes", "Prediabetes"],
        "medications": [],
        "allergies": [],
        "follow_up": "Return in 6 months",
    },
}
# (phrase in the query, the code a reranker should put first, a sibling scored close to it)
CODE_FOR: list[tuple[str, str | None, str | None]] = [
    ("type 2 diabetes", "E11.9", None),
    ("prediabetes", "R73.03", None),
    ("hypertension", "I10", None),
    ("hypothyroidism", "E03.9", None),
    ("reflux", "K21.9", None),
    ("hyperlipidemia", "E78.5", None),
    ("pharyngitis", "J02.0", None),
    ("asthma", "J45.20", "J45.30"),
    ("rhinitis", "J30.1", "J30.9"),
    ("plantar fasciitis", None, None),
    ("obesity", "E66.9", None),
    ("cystitis", "N30.00", None),
]


def stand_in_vitals(text: str) -> dict[str, str]:
    """The reading after each vital's first label, as a well-behaved model would give it."""
    found = {}
    for name in RULES["vital_fields"]:
        readings = recipe.labelled_readings(name, text, RULES)
        if readings:
            found[name] = readings[0]
    return found


def stand_in_details(drug: str, sentence: str) -> dict[str, str]:
    doses = recipe.drug_doses(drug, sentence, [drug], RULES)
    found = {"dose": doses[0]} if doses else {}
    if " PO " in f" {sentence} ":
        found["route"] = "PO"
    frequency = re.search(r"(?i)(twice daily|once daily|three times daily|daily|nightly)", sentence)
    if frequency:
        found["frequency"] = frequency.group(1)
    return found


def stand_in_scores(query: str) -> list[dict[str, Any]]:
    top, close = None, None
    for phrase, code, sibling in CODE_FOR:
        if phrase in query.casefold():
            top, close = code, sibling
            break
    scores = []
    for code in CODES:
        score = 0.98 if code == top else 0.95 if code == close else 0.02
        scores.append({"item_id": code, "score": score})
    if top is None:
        scores[0]["score"] = 0.2  # nothing fits: the best is weak
    return scores


class StandIn:
    """Stands in for SIEClient. `lists` replaces the list reply per note, `fields` the reply for
    any item id that starts with a key, `errors` maps an id prefix to errors its calls raise
    (one per call) and `replies` an id prefix to a raw reply."""

    def __init__(
        self,
        lists: dict[str, dict[str, Any]] | None = None,
        errors: dict[str, list[Exception]] | None = None,
        replies: dict[str, Any] | None = None,
        fields: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.lists = LISTS | (lists or {})
        self.errors, self.replies, self.fields = errors or {}, replies or {}, fields or {}
        self.ids: list[str] = []
        self.extract_options: list[Any] = []

    def _canned(self, item_id: str) -> Any:
        self.ids.append(item_id)
        for prefix, errors in self.errors.items():
            if item_id.startswith(prefix) and errors:
                raise errors.pop(0)
        for prefix, reply in self.replies.items():
            if item_id.startswith(prefix):
                return reply
        for prefix, data in self.fields.items():
            if item_id.startswith(prefix):
                return {"data": data}
        return None

    def extract(self, model: str, item: Any, **options: Any) -> Any:
        item_id, text = item["id"], item["text"]
        self.extract_options.append(options.get("options"))
        canned = self._canned(item_id)
        if canned is not None:
            return canned
        kind, note = item_id.split("/")[:2]
        if kind == "lists":
            return {"data": self.lists.get(note, {})}
        if kind == "vitals":
            return {"data": stand_in_vitals(text)}
        props = options["output_schema"]["properties"]
        drug = re.search(r"dose of (.+?) with", props["dose"]["description"])
        return {"data": stand_in_details(drug.group(1) if drug else "", text)}

    def score(self, model: str, query: Any, items: Any, **options: Any) -> Any:
        canned = self._canned("score/" + query["text"])
        if canned is not None:
            return canned
        return {"scores": stand_in_scores(query["text"])}


def run(
    rules: dict[str, Any] | None = None, data_dir: Path = DATA, **stand_in: Any
) -> dict[str, recipe.Draft]:
    client = StandIn(**stand_in)
    with contextlib.redirect_stdout(io.StringIO()):
        drafts, _ = recipe.run(cast(SIEClient, client), rules or RULES, data_dir)
    # Every extract call asks SIE to fail on a window too long to read whole, never to cut it.
    sent = client.extract_options
    assert sent == [{"overflow_policy": "error"}] * len(sent), sent
    return {draft.document.document_id: draft for draft in drafts}


def reasons(draft: recipe.Draft, reviewer: str | None = None) -> list[str]:
    return [r["reason"] for r in draft.review if reviewer in (None, r["reviewer"])]


def med(draft: recipe.Draft, name: str) -> dict[str, Any]:
    return next(m for m in draft.medications if recipe.squash(m["name"]) == name)


def one_note(text: str, patient: str = "LFP-20417") -> Path:
    """A copy of data/ whose only outside note is `text`, filed in `patient`'s chart."""
    folder = Path(tempfile.mkdtemp())
    shutil.copytree(DATA, folder, dirs_exist_ok=True)
    (folder / "outside_notes" / "note-99.txt").write_text(text, encoding="utf-8")
    (folder / "ehr_documents.csv").write_text(
        "document_id,patient_id,received_on,source_clinic,visit_date,category,file\n"
        f"note-99,{patient},2026-10-30,Test clinic,2026-10-01,outside records,note-99.txt\n",
        encoding="utf-8",
    )
    return folder


NOTE_01 = (DATA / "outside_notes" / "note-01.txt").read_text(encoding="utf-8")


# --- Checks -----------------------------------------------------------------------------------


def check_examples() -> None:
    drafts = run()
    status = {doc: draft.status for doc, draft in drafts.items()}
    assert status == {
        "note-01": "checks_passed",
        "note-02": "clinical_review",
        "note-03": "clinical_review",
        "note-04": "clinical_review",
        "note-05": "clinical_review",
        "note-06": "clinical_review",
        "note-07": "coder_review",
        "note-08": "clinical_review",
        "note-09": "checks_passed",
        "note-10": "checks_passed",
    }, status
    d = drafts["note-01"]
    assert [m["comparison"] for m in d.medications] == ["matches_chart"] * 2, d.medications
    assert [p["code"] for p in d.problems] == ["E11.9", "I10"]
    assert [p["comparison"] for p in d.problems] == ["new", "new"]
    d = drafts["note-02"]
    assert med(d, "levothyroxine")["comparison"] == "differs_from_chart"
    assert "levothyroxine is dosed in mcg" in " ".join(reasons(d)), reasons(d)
    assert med(d, "omeprazole")["comparison"] == "matches_chart"
    assert d.problems[0]["comparison"] == "matches_chart", d.problems[0]
    d = drafts["note-03"]
    assert reasons(d) == [
        "Heart rate 720 is outside 25-250: likely a typo; the chart's last reading is 76 on "
        "2026-10-28"
    ], reasons(d)
    bp = d.vitals["blood_pressure"]
    assert bp["systolic_flag"] == bp["diastolic_flag"] == "high", bp
    assert bp["chart_latest"] == "152/94 on 2026-10-28", bp
    d = drafts["note-04"]
    assert reasons(d) == [
        "amoxicillin (in the note) conflicts with the 'Penicillin' allergy (in the note)"
    ], reasons(d)
    assert d.allergies["items"][0]["comparison"] == "new"
    assert d.problems[0]["code"] == "J02.0"
    d = drafts["note-05"]
    simva, atorva = med(d, "simvastatin"), med(d, "atorvastatin")
    assert (simva["status"], simva["comparison"]) == ("discontinued", "differs_from_chart")
    assert (atorva["status"], atorva["comparison"], atorva["dose"]) == ("active", "new", "20 mg")
    assert d.problems[0]["comparison"] == "matches_chart"
    d = drafts["note-06"]
    assert "Albuterol inhaler: dose not documented" in reasons(d, "clinician"), reasons(d)
    assert len(reasons(d, "coder")) == 2, reasons(d, "coder")
    assert d.allergies["items"][0]["comparison"] == "matches_chart"
    d = drafts["note-07"]
    assert [p["code"] for p in d.problems] == [None, "E66.9"]
    assert reasons(d) == ["No code suggested: best code E11.9 scored 0.20, below 0.5"], reasons(d)
    d = drafts["note-08"]
    assert reasons(d) == ["Allergy status is not documented in the note or chart"], reasons(d)
    assert d.problems[0]["code"] == "N30.00"
    d = drafts["note-09"]
    lisinopril, losartan = med(d, "lisinopril"), med(d, "losartan")
    assert (lisinopril["status"], lisinopril["comparison"]) == ("discontinued", "new")
    assert lisinopril["sentence"] == "Stop lisinopril because of a dry cough."
    assert (losartan["status"], losartan["comparison"]) == ("active", "matches_chart")
    d = drafts["note-10"]
    assert [p["code"] for p in d.problems] == ["R73.03"]
    assert d.left_out[0]["item"] == "type 2 diabetes"
    assert d.left_out[0]["reason"] == "family history"
    print("examples: the ten notes come out as the README says (stand-in replies) ok")


def check_statuses_renamed() -> None:
    names = {recipe.CHECKS_PASSED, recipe.CLINICAL_REVIEW, recipe.CODER_REVIEW}
    assert names == {"checks_passed", "clinical_review", "coder_review"}
    assert {recipe.NEW, recipe.MATCHES, recipe.DIFFERS} == {
        "new",
        "matches_chart",
        "differs_from_chart",
    }
    source = Path(recipe.__file__).read_text(encoding="utf-8")
    assert '"ready"' not in source
    assert "'ready'" not in source
    print("statuses: checks_passed / clinical_review / coder_review, no 'ready' ok")


def check_medication_state() -> None:
    def state(name: str, *said: str) -> str:
        return recipe.med_state(name, list(said), RULES)[0]

    # A later stop wins over the medication list; a later neutral mention does not undo it.
    assert state("lisinopril", "Lisinopril 10 mg daily", "Stop lisinopril.") == "discontinued"
    assert (
        state("lisinopril", "Lisinopril 10 mg daily", "Stop lisinopril.", "Cough on lisinopril.")
        == "discontinued"
    )
    # Started again after a stop: active.
    assert state("lisinopril", "Lisinopril was stopped.", "Restart lisinopril 5 mg.") == "active"
    # One sentence, two drugs: the nearest cue decides each.
    both = "Stop simvastatin and start atorvastatin 20 mg nightly."
    assert state("simvastatin", both) == "discontinued"
    assert state("atorvastatin", both) == "active"
    assert state("simvastatin", "Stop simvastatin; start atorvastatin.") == "discontinued"
    assert state("atorvastatin", "Stop simvastatin; start atorvastatin.") == "active"
    # Negated stops.
    assert state("metformin", "Do not stop metformin before the scan.") == "active"
    assert state("aspirin", "Not taking aspirin.") == "discontinued"
    assert state("simvastatin", "Simvastatin 40 mg was stopped on 09/30/2026.") == "discontinued"
    # A negated start or restart is not a start; a held drug is not active.
    listed = "Metformin 500 mg PO twice daily"
    assert state("metformin", listed, "Do not restart metformin.") == "discontinued"
    assert state("metformin", "Do not start metformin yet.") == "discontinued"
    assert state("metformin", listed, "Don't resume metformin until the scan.") == "discontinued"
    assert state("metformin", listed, "Hold metformin before the scan.") == "discontinued"
    assert state("metformin", listed, "Metformin held for the CT.") == "discontinued"
    assert state("metformin", listed, "Do not hold metformin.") == "active"
    print("medication state: last cue wins, nearest cue in a sentence, negated stop ok")


def check_doses() -> None:
    other = ["simvastatin", "atorvastatin"]
    sentence = "Stop simvastatin 40 mg and start atorvastatin 20 mg nightly."
    assert recipe.drug_doses("atorvastatin", sentence, other, RULES) == ["20 mg"]
    assert recipe.drug_doses("simvastatin", sentence, other, RULES) == ["40 mg"]
    assert recipe.drug_doses("levothyroxine", "Levothyroxine 50mcg daily", [], RULES) == ["50 mcg"]
    assert recipe.drug_doses("iron", "Iron daily. Hgb 12 g/dL.", [], RULES) == []
    assert recipe.check_dose("Levothyroxine", "50 mg", RULES) == (
        "Levothyroxine dose '50 mg' is in mg; levothyroxine is dosed in mcg"
    )
    assert recipe.check_dose("Levothyroxine", "50mcg", RULES) is None
    assert recipe.check_dose("Insulin glargine", "10 mg", RULES) is not None
    assert recipe.check_dose("Metformin", "500", RULES) is not None
    assert recipe.same_dose("500mg", "500 mg")
    assert not recipe.same_dose("50 mg", "50 mcg")
    print("doses: belong to the nearest drug, glued units, mcg vs mg ok")


def check_problem_assertion() -> None:
    def said(problem: str, sentence: str) -> str:
        return recipe.assertion(problem, sentence, RULES)

    assert said("type 2 diabetes", "Mother has type 2 diabetes.") == "family"
    assert said("hypertension", "Family history: father with hypertension.") == "family"
    assert said("diabetes", "No diabetes.") == "negated"
    assert said("hypoglycemia", "No hypoglycemia.") == "negated"
    assert said("hematuria", "1. Acute cystitis without hematuria.") == "negated"
    assert said("pneumonia", "Pneumonia was ruled out by chest x-ray.") == "negated"
    assert said("strep pharyngitis", "Rule out strep pharyngitis.") == "uncertain"
    assert said("pneumonia", "Possible pneumonia, chest x-ray ordered.") == "uncertain"
    assert said("Acute cystitis", "1. Acute cystitis without hematuria.") == "affirmed"
    assert said("Essential hypertension", "1. Essential hypertension, not at goal.") == "affirmed"
    # A negation that still asserts the problem.
    assert said("hypertension", "1. No change in hypertension.") == "affirmed"
    assert said("type 2 diabetes", "2. Not at goal for type 2 diabetes.") == "affirmed"
    assert said("asthma", "Not well controlled asthma.") == "affirmed"
    assert said("asthma", "No asthma since childhood.") == "negated"
    # The last mention that isn't about a relative decides.
    state = recipe.problem_state(
        "type 2 diabetes",
        ["Mother has type 2 diabetes.", "2. Type 2 diabetes, new diagnosis."],
        RULES,
    )
    assert state[0] == "affirmed", state
    print("problems: family history, negated, ruled out and uncertain are not affirmed ok")


def check_chart_comparison() -> None:
    chart = [ChartMedication("Metformin ER", "500 mg", "PO", "daily", "intake form")]
    metformin = {"name": "Metformin", "status": "active", "dose": "500 mg"}
    assert recipe.compare_medication(metformin, chart)[0] == "differs_from_chart"
    chart = [ChartMedication("Lisinopril", "10 mg", "PO", "daily", "intake form")]
    same = {"name": "lisinopril", "status": "active", "dose": "10mg"}
    assert recipe.compare_medication(same, chart)[0] == "matches_chart"
    other = {"name": "Lisinopril", "status": "active", "dose": "20 mg"}
    assert recipe.compare_medication(other, chart)[0] == "differs_from_chart"
    no_dose = {"name": "Lisinopril", "status": "active", "dose": None}
    assert recipe.compare_medication(no_dose, chart)[0] == "differs_from_chart"
    stopped = {"name": "Lisinopril", "status": "discontinued"}
    assert recipe.compare_medication(stopped, chart)[0] == "differs_from_chart"
    assert recipe.compare_medication({"name": "Losartan", "status": "active"}, chart)[0] == "new"
    problems = [ChartProblem("Type 1 diabetes mellitus without complications", "E10.9")]
    assert recipe.compare_problem("Type 2 diabetes", "E11.9", problems)[0] == "new"
    problems = [ChartProblem("Type 2 diabetes mellitus with hyperglycemia", "E11.65")]
    assert recipe.compare_problem("Type 2 diabetes", "E11.9", problems)[0] == "differs_from_chart"
    assert recipe.compare_problem("Type 2 diabetes", None, problems)[0] == "new"
    problems = [ChartProblem("Hypothyroidism", "E03.9")]
    assert recipe.compare_problem("hypothyroidism", None, problems)[0] == "matches_chart"
    assert recipe.compare_problem("Hypothyroid", "E03.9", problems)[0] == "matches_chart"
    print("chart comparison: same name and dose match; near names and doses differ ok")


def check_allergies() -> None:
    conflicts = recipe.allergy_conflicts(
        [("Amoxicillin", "", "in the note")], [("Ampicillin", "on the chart")], RULES
    )
    assert conflicts == [
        "Ampicillin (on the chart) conflicts with the 'Amoxicillin' allergy (in the note)"
    ], conflicts
    chart_class = recipe.allergy_conflicts(
        [("Bactrim", "sulfa", "on the chart")], [("sulfamethoxazole", "in the note")], RULES
    )
    assert len(chart_class) == 1, chart_class
    assert not recipe.allergy_conflicts(
        [("Penicillin", "", "in the note")], [("Cephalexin", "in the note")], RULES
    )
    # A chart allergy the note doesn't mention, while the note says NKDA.
    text = NOTE_01.replace("Allergies: No known drug allergies.", "Allergies: NKDA.")
    folder = one_note(text, patient="LFP-20440")
    lists = {"note-99": LISTS["note-01"]}
    d = run(data_dir=folder, lists=lists)["note-99"]
    assert any("chart lists a Sulfa drugs allergy" in r for r in reasons(d)), reasons(d)
    # "Not allergic to penicillin" is not a penicillin allergy.
    text = NOTE_01.replace(
        "Allergies: No known drug allergies.", "Allergies: Not allergic to penicillin. NKDA."
    )
    folder = one_note(text)
    lists = {"note-99": LISTS["note-01"] | {"allergies": ["penicillin"]}}
    d = run(data_dir=folder, lists=lists)["note-99"]
    assert d.allergies["items"] == [], d.allergies
    assert d.left_out[0]["item"] == "penicillin", d.left_out
    assert d.status == "checks_passed", reasons(d)
    # An allergy line the model read nothing from, in a chart that says no known allergies.
    text = NOTE_01.replace(
        "Allergies: No known drug allergies.", "Allergies: Codeine (anaphylaxis)."
    )
    d = run(data_dir=one_note(text), lists={"note-99": LISTS["note-01"]})["note-99"]
    assert d.allergies["in_note"] == "not_documented", d.allergies
    assert d.status == "clinical_review", reasons(d)
    assert "'Codeine' in 'Allergies: Codeine (anaphylaxis).' was not read: read it by hand" in (
        reasons(d)
    )
    # Two allergies on one line and only one read: the other still goes to the clinician.
    line = "Allergies: Penicillin (hives, rash), Codeine (anaphylaxis)."
    text = NOTE_01.replace("Allergies: No known drug allergies.", line)
    lists = {"note-99": LISTS["note-01"] | {"allergies": ["Penicillin (hives, rash)"]}}
    d = run(data_dir=one_note(text), lists=lists)["note-99"]
    assert d.allergies["in_note"] == "allergies_listed", d.allergies
    assert d.status == "clinical_review", reasons(d)
    unread = [r for r in reasons(d) if "was not read" in r]
    assert unread == [f"'Codeine' in '{line}' was not read: read it by hand"], reasons(d)
    print("allergies: class members, chart classes, chart-only, negated and unread allergies ok")


def check_vitals() -> None:
    text = "BP 116/72, HR 88, Temp 100.9 F, RR 16, SpO2 99%."
    assert recipe.labelled_readings("heart_rate", text, RULES) == ["88"]
    assert recipe.labelled_readings("blood_pressure", text, RULES) == ["116/72"]
    # The 72 inside the blood pressure is not a heart rate, nor is the age.
    entry, reason = recipe.check_vital("heart_rate", "72", ["88"], RULES)
    assert entry is None
    assert reason is not None
    entry, reason = recipe.check_vital("blood_pressure", "160s/90", ["116/72"], RULES)
    assert entry is None
    assert reason is not None
    entry, reason = recipe.check_vital("blood_pressure", "120/80", ["116/72"], RULES)
    assert entry is None
    assert reason is not None
    assert recipe.labelled_readings("heart_rate", "58-year-old woman. HR 72.", RULES) == ["72"]
    entry, reason = recipe.check_vital("heart_rate", "58", ["72"], RULES)
    assert entry is None
    assert reason is not None
    assert recipe.labelled_readings("heart_rate", "Pulse: 72bpm", RULES) == ["72"]
    entry, reason = recipe.check_vital("temperature", "37.2C", ["37.2"], RULES)
    assert reason is None, reason
    assert entry == {"value": "37.2C"}, entry
    # Celsius by range (no unit written), pounds to kilograms, an impossible blood pressure.
    entry, reason = recipe.check_vital("temperature", "37.2", ["37.2"], RULES)
    assert (entry, reason) == ({"value": "37.2"}, None), (entry, reason)
    entry, reason = recipe.check_vital("temperature", "39.5", ["39.5"], RULES)
    assert entry == {"value": "39.5", "flag": "high"}, entry
    entry, reason = recipe.check_vital("temperature", "98.4", ["98.4"], RULES)
    assert (entry, reason) == ({"value": "98.4"}, None), (entry, reason)
    entry, reason = recipe.check_vital("weight", "180 lb", ["180"], RULES)
    assert reason is None, reason
    entry, reason = recipe.check_vital("weight", "800 lbs", ["800"], RULES)
    assert reason == "Weight 800 lb is outside 2-350: likely a typo", reason
    entry, reason = recipe.check_vital("weight", "800", ["800"], RULES)
    assert reason == "Weight 800 kg is outside 2-350: likely a typo", reason
    entry, reason = recipe.check_vital("blood_pressure", "300/80", ["300/80"], RULES)
    assert reason == "Systolic 300 is outside 50-260: likely a typo", reason
    entry, reason = recipe.check_vital("blood_pressure", "120/20", ["120/20"], RULES)
    assert reason == "Diastolic 20 is outside 30-160: likely a typo", reason
    # Two blood pressures, and a reading the model did not return.
    text = NOTE_01.replace("BP 128/78,", "BP 128/78, repeat BP 150/96,")
    folder = one_note(text)
    d = run(data_dir=folder, lists={"note-99": LISTS["note-01"]})["note-99"]
    assert any("more than one blood pressure (128/78, 150/96)" in r for r in reasons(d))
    folder = one_note(NOTE_01)
    d = run(data_dir=folder, lists={"note-99": LISTS["note-01"]}, fields={"vitals/": {}})["note-99"]
    assert "The note gives heart rate 72, which the model did not read" in reasons(d), reasons(d)
    print("vitals: label-bound readings, BP/age not a heart rate, repeats and misses ok")


def check_completeness() -> None:
    # The model misses losartan: its dose in the plan belongs to no medication read.
    note_09 = {"note-09": LISTS["note-09"] | {"medications": ["Lisinopril"]}}
    d = run(lists=note_09)["note-09"]
    assert d.status == "clinical_review"
    assert any("'50 mg' in 'Start losartan 50 mg daily.'" in r for r in reasons(d)), reasons(d)
    # The model misses obesity: its assessment line yields no problem.
    note_07 = {"note-07": LISTS["note-07"] | {"problems": ["Plantar fasciitis"]}}
    d = run(lists=note_07)["note-07"]
    assert "no problem was read from '2. Obesity. Discussed diet and a walking plan.'" in reasons(d)
    # A family-history problem the model gives as the patient's never reaches the codes.
    d = run(lists={"note-01": LISTS["note-01"] | {"problems": ["hypoglycemia"]}})["note-01"]
    assert [p["problem"] for p in d.problems] == [], d.problems
    assert d.left_out[0]["reason"] == "negated or ruled out"
    assert d.status == "clinical_review", reasons(d)  # the assessment lines went unread
    # A dose the model gives for a drug from another drug's span is not drafted.
    text = NOTE_01.replace(
        "- Metformin 500 mg PO twice daily", "- Glipizide 5 mg and metformin 500 mg PO daily"
    )
    folder = one_note(text)
    lists = {
        "note-99": LISTS["note-01"] | {"medications": ["Glipizide", "metformin", "Lisinopril"]}
    }
    d = run(data_dir=folder, lists=lists, fields={"med/note-99/metformin": {"dose": "5 mg"}})[
        "note-99"
    ]
    assert med(d, "metformin")["dose"] is None
    assert any("dose '5 mg' is not written for metformin" in r for r in reasons(d)), reasons(d)
    # An assessment line whose only problem was read as negated goes to the clinician.
    line = "2. No dose change for essential hypertension. Continue lisinopril."
    folder = one_note(
        NOTE_01.replace("2. Essential hypertension, controlled. Continue lisinopril.", line)
    )
    d = run(data_dir=folder, lists={"note-99": LISTS["note-01"]})["note-99"]
    assert d.left_out[0]["item"] == "Essential hypertension", d.left_out
    assert f"the only problem read from '{line}' was read as negated: check it" in reasons(d)
    # A problem negated outside a numbered assessment line goes to the clinician, not away.
    sentence = "No complications from type 2 diabetes."
    folder = one_note(
        NOTE_01.replace("Return in 3 months with repeat A1c.", "Return in 3 months. " + sentence)
    )
    problems = ["Essential hypertension", "type 2 diabetes"]
    d = run(data_dir=folder, lists={"note-99": LISTS["note-01"] | {"problems": problems}})[
        "note-99"
    ]
    assert d.left_out[0]["item"] == "type 2 diabetes", d.left_out
    assert f"read as negated and left out: '{sentence}': check it" in reasons(d), reasons(d)
    # "No change in hypertension" asserts the problem: drafted, nothing to check.
    line = "2. No change in essential hypertension. Continue lisinopril."
    folder = one_note(
        NOTE_01.replace("2. Essential hypertension, controlled. Continue lisinopril.", line)
    )
    d = run(data_dir=folder, lists={"note-99": LISTS["note-01"]})["note-99"]
    assert [p["problem"] for p in d.problems] == LISTS["note-01"]["problems"], d.problems
    assert d.status == "checks_passed", reasons(d)
    print("completeness: unread doses, unread or negated assessment lines, borrowed doses ok")


def check_header() -> None:
    assert recipe.header_patient(NOTE_01, RULES)[0] == "Marisol Quenby"
    folder = one_note(NOTE_01, patient="LFP-20422")
    d = run(data_dir=folder, lists={"note-99": LISTS["note-01"]})["note-99"]
    assert any("check the note is filed in the right chart" in r for r in reasons(d)), reasons(d)
    # Common date-of-birth formats, and a two-digit year never lands in the future.
    arrived = date(2026, 10, 30)
    born = date(1968, 4, 22)
    for raw in ("04/22/1968", "4-22-1968", "1968-04-22", "4/22/68", "April 22, 1968"):
        assert recipe.parse_dob(raw, arrived) == born, raw
    for raw in ("Apr 22 1968", "22 Apr 1968", "22 April 1968", "4/22/1968   Sex: F"):
        assert recipe.parse_dob(raw, arrived) == born, raw
    assert recipe.parse_dob("1/2/49", arrived) == date(1949, 1, 2)
    assert recipe.parse_dob("1/2/19", arrived) == date(2019, 1, 2)
    assert recipe.parse_dob("22.04.1968", arrived) is None
    assert recipe.parse_dob("13/22/1968", arrived) is None
    header = "Patient: Marisol Quenby   DOB: 04/22/1968"

    def header_reasons(line: str, patient: str = "LFP-20417") -> list[str]:
        folder = one_note(NOTE_01.replace(header, line), patient=patient)
        d = run(data_dir=folder, lists={"note-99": LISTS["note-01"]})["note-99"]
        return [r for r in reasons(d, "clinician")]

    # The right patient in other formats: nothing to check.
    for line in (
        "Patient: Marisol Quenby   DOB: 1968-04-22",
        "Patient: Marisol Quenby   D.O.B.: April 22, 1968",
        "Name: Marisol Quenby  Date of birth: 4/22/68",
    ):
        assert header_reasons(line) == [], (line, header_reasons(line))
    # Another patient's name with an ISO date of birth: the name alone is enough to check.
    found = header_reasons("Patient: Rowan Petrakis   DOB: 1979-11-03")
    assert any("names 'Rowan Petrakis'" in r for r in found), found
    found = header_reasons("Patient: Rowan Petrakis")  # no date of birth printed
    assert any("names 'Rowan Petrakis'" in r for r in found), found
    # Same name, another date of birth.
    found = header_reasons("Patient: Marisol Quenby   DOB: 1971-02-03")
    assert any("date of birth of 1971-02-03" in r for r in found), found
    # A date of birth that can't be read goes to the clinician, not past the check.
    found = header_reasons("Patient: Marisol Quenby   DOB: 22.04.1968")
    assert any("'22.04.1968' could not be read" in r for r in found), found
    print("header: another name, another DOB, DOB formats and an unreadable DOB ok")


def check_codes() -> None:
    assert recipe.suggest_code([("E11.9", 0.99), ("E11.65", 0.30)], RULES)[0] == "E11.9"
    code, _, reason = recipe.suggest_code([("J45.20", 0.99), ("J45.30", 0.98)], RULES)
    assert code is None
    assert reason is not None
    assert "too close" in reason
    code, _, reason = recipe.suggest_code([("M79.672", 0.23), ("M79.671", 0.10)], RULES)
    assert code is None
    assert reason is not None
    assert "below 0.5" in reason
    # Each problem is ranked on its own phrase plus its sentence, so two problems in one
    # sentence are two queries: a failed call for one leaves the other ranked.
    assert recipe.code_query("Prediabetes", "Prediabetes") == "Prediabetes"
    both = "1. Asthma and allergic rhinitis."
    folder = one_note(NOTE_01.replace("2. Essential hypertension, controlled.", both))
    lists = {"note-99": LISTS["note-01"] | {"problems": ["Asthma", "allergic rhinitis"]}}
    busy = {"score/allergic rhinitis: ": [ServerError("busy", status_code=503)]}
    d = run(data_dir=folder, lists=lists, errors=busy)["note-99"]
    ranked = {p["problem"]: p["code_status"] for p in d.problems}
    assert ranked == {"Asthma": "to_coder", "allergic rhinitis": "not_ranked"}, ranked
    assert len(CODES) == 73
    assert not any(c.startswith(("M72.2", "M67.A")) for c in CODES)
    print("codes: score and margin thresholds; plantar fasciitis absent from the subset ok")


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
    # A transient error on one note's list window: that note goes to review, the rest don't.
    for error in TRANSIENT:
        drafts = run(errors={"lists/note-01/": [error]})
        assert drafts["note-01"].status == "clinical_review", type(error).__name__
        assert reasons(drafts["note-01"])[0].startswith("window 1 was not read: the SIE call")
        assert drafts["note-10"].status == "checks_passed"
    # A transient error ranking codes, or reading a drug's dose: that note to clinical review.
    drafts = run(
        errors={"score/Prediabetes: 1. Prediabetes.": [ServerError("busy", status_code=503)]}
    )
    assert drafts["note-10"].status == "clinical_review"
    assert "code candidates not ranked: the SIE call failed" in reasons(drafts["note-10"])[0]
    drafts = run(errors={"med/note-01/metformin": [ServerError("busy", status_code=503)]})
    assert any("Metformin details were not read" in r for r in reasons(drafts["note-01"]))
    # A reply missing its key is a failed call; an empty reply is "nothing found".
    drafts = run(replies={"lists/note-01/": {"something": {}}})
    assert "replied without 'data'" in reasons(drafts["note-01"])[0], reasons(drafts["note-01"])
    drafts = run(replies={"score/Prediabetes: 1. Prediabetes.": {"model": "x"}})
    assert "replied without 'scores'" in reasons(drafts["note-10"])[0]
    drafts = run(replies={"score/Prediabetes: 1. Prediabetes.": {"scores": []}})
    assert "scored 0 of 73 codes" in reasons(drafts["note-10"])[0]
    drafts = run(fields={"med/note-01/metformin": {}})
    assert "Metformin: dose not documented" in reasons(drafts["note-01"])
    # SIE Cloud's empty-result 500: a list window that found nothing goes to review; a drug's
    # details that found nothing leave the dose missing, which goes to review.
    empty = ServerError(recipe.EMPTY_RESULT_ERROR, status_code=500)
    drafts = run(errors={"lists/note-01/": [empty]})
    assert drafts["note-01"].status == "clinical_review"
    assert "found no problem, medication or allergy" in reasons(drafts["note-01"])[0]
    drafts = run(errors={"med/note-01/metformin": [empty]})
    assert "Metformin: dose not documented" in reasons(drafts["note-01"])
    assert "the SIE call failed" not in " ".join(reasons(drafts["note-01"]))
    # Every call failed (a dead SIE_BASE_URL): the run stops, with the full message.
    long = "connection refused " + "x" * 300
    dead = {"": [SIEConnectionError(long) for _ in range(40)]}
    message = stopped(lambda: run(errors=dead))
    assert message.startswith("Every SIE call failed (20 of 20 to fastino/gliner2-large-v1)"), (
        message
    )
    assert long in message
    assert "Check SIE_BASE_URL and your network." in message, message
    busy = {"": [ModelLoadingError("loading") for _ in range(40)]}
    assert "run again in a few minutes." in stopped(lambda: run(errors=busy))
    # A refusal stops the run at once, with the full message, HTTP status and code.
    refusal = {"score/": [InsufficientCreditsError("no credits " + "y" * 300)]}
    message = stopped(lambda: run(errors=refusal))
    assert "InsufficientCreditsError (HTTP 402, code INSUFFICIENT_CREDITS)" in message, message
    assert "y" * 300 in message
    assert "SIE_API_KEY" in message
    message = stopped(lambda: run(errors={"": [ModelLoadFailedError("gated", permanent=True)]}))
    assert message.startswith("SIE can't load fastino/gliner2-large-v1"), message
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
    print("sie failure: transient to review; missing key fails; refusals and all-failed stop ok")


def check_outputs() -> None:
    assert recipe.csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert recipe.csv_safe("- Metformin 500 mg") == "'- Metformin 500 mg"
    assert recipe.csv_safe("note-01") == "note-01"
    drafts = list(run().values())
    with tempfile.TemporaryDirectory() as name:
        recipe.write_outputs(Path(name), drafts)
        with (Path(name) / "notes.csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        assert [r["status"] for r in rows][:2] == ["checks_passed", "clinical_review"]
        assert rows[0]["matches_chart"] == "2", rows[0]
        queue = (Path(name) / "review-queue.csv").read_text(encoding="utf-8")
        assert queue.splitlines()[0] == "note,patient_id,status,reviewer,item,reason"
        draft = (Path(name) / "drafts" / "note-05.md").read_text(encoding="utf-8")
        assert "differs_from_chart: the chart lists Simvastatin 40 mg nightly" in draft, draft
    print("outputs: drafts, notes.csv and review queue written; formula cells quoted ok")


def check_committed_data() -> None:
    ehr = EHR(DATA)
    docs = ehr.outside_documents()
    assert [d.document_id for d in docs] == [f"note-{n:02d}" for n in range(1, 11)]
    for doc in docs:
        patient = ehr.patient(doc.patient_id)
        assert patient is not None
        name, born, _ = recipe.header_patient(doc.text, RULES)
        assert name == patient.name
        assert born is not None
        assert born.isoformat() == patient.dob
        assert len(doc.text.split()) <= RULES["thresholds"]["window_words"], doc.document_id
    assert ehr.allergies("LFP-20440") == [ChartAllergy("Sulfa drugs", "rash", "sulfa")]
    assert ehr.patient("LFP-20449") is not None
    assert cast(Any, ehr.patient("LFP-20449")).allergy_status == "not_recorded"
    # A seed with a broken allergy status is refused, not read as "no allergies".
    folder = Path(tempfile.mkdtemp())
    shutil.copytree(DATA, folder, dirs_exist_ok=True)
    seed = folder / "ehr_patients.csv"
    seed.write_text(seed.read_text().replace("allergies_listed", "unknown"), encoding="utf-8")
    message = ""
    try:
        EHR(folder)
    except ValueError as error:
        message = str(error)
    assert "allergy_status must be one of" in message, "a bad allergy status was accepted"
    rules = copy.deepcopy(RULES)
    assert rules["require_follow_up"] is False
    print("committed data: 10 notes in 10 charts, headers match, seeds validated ok")


if __name__ == "__main__":
    check_examples()
    check_statuses_renamed()
    check_medication_state()
    check_doses()
    check_problem_assertion()
    check_chart_comparison()
    check_allergies()
    check_vitals()
    check_completeness()
    check_header()
    check_codes()
    check_sie_failure()
    check_outputs()
    check_committed_data()
    print("all checks passed")
