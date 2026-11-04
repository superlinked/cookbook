"""Write the synthetic outside notes and chart seeds in data/ and, given the CMS file, the codes.

Every patient, clinician and clinic is invented. Ten new patients registered at the fictional
Larkspur Family Practice in October 2026; the practice requested their records, and their old
clinics (the fictional Harrow Creek Medical Group and Bellwether Internal Medicine) sent the last
visit note as printed text. The notes plant the cases listed in the README.

    data/outside_notes/note-NN.txt   the outside notes, as filed in each patient's chart
    data/ehr_documents.csv           the EHR's document index: which chart each note is filed in
    data/ehr_patients.csv            the registered patients and the chart's allergy status
    data/ehr_problems.csv            problem-list items already on the chart
    data/ehr_medications.csv         medications from the new-patient intake form
    data/ehr_allergies.csv           allergies on the chart, with the drug class recorded
    data/ehr_vitals.csv              vitals on the chart (one nurse blood-pressure visit)
    data/icd10cm-subset.tsv          the practice's code-set subset: 73 ICD-10-CM codes (FY2027)

The subset is cut from the CMS FY2027 ICD-10-CM code descriptions file
(https://www.cms.gov/files/zip/2027-code-descriptions-tabular-order.zip, icd10cm_codes_2027.txt).
It is only rewritten when that file is passed. Plantar fasciitis (M67.A0-) is left out on
purpose, so one planted problem has no right answer in the subset.

    uv run make_data.py
    uv run make_data.py --cms-codes path/to/icd10cm_codes_2027.txt
"""

from __future__ import annotations

import argparse
import csv
import io
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
NOTES_DIR = DATA_DIR / "outside_notes"
SUBSET_PATH = DATA_DIR / "icd10cm-subset.tsv"

HARROW = "Harrow Creek Medical Group"
BELLWETHER = "Bellwether Internal Medicine"

# Common primary-care codes, with close siblings so the reranker has to tell them apart.
_SUBSET_TEXT = """
E119 E1165 E1122 E1140 E109 R7303 Z7984
I10 I119 I129 R030
E785 E7800 E781 E782 M7910
E039 E063 E0590
K219 K2100
J4520 J45909 J4530 J4540 J45901 R062 R051
J309 J301 J020 J029 J0390 J069 J0100 Z880
H6690 H6590
N390 N3000 R300 R509
R5383 M5450 M5459 M79671 M79672
E669 E6601 Z6830 Z6832
F411 F419 F329 F32A G4700
R519 G44209 I4891 R0789 R079
M109 M170 M179 D649 D509 N1830 N189 E559
Z0000 Z0001 Z23 F17210
"""
SUBSET_CODES = _SUBSET_TEXT.split()


def header(clinic: str, visit: str, clinician: str, patient: str, dob: str) -> str:
    return (
        f"{clinic.upper()}\nOffice visit note. Visit date: {visit}. Clinician: {clinician}.\n"
        f"Printed for a records request.\nPatient: {patient}   DOB: {dob}\n"
    )


# (document id, patient id, clinic, visit date, clinician, patient, DOB, the case, body)
NOTES: list[tuple[str, str, str, str, str, str, str, str, str]] = [
    (
        "note-01",
        "LFP-20417",
        HARROW,
        "08/14/2026",
        "Teodor Vash, MD",
        "Marisol Quenby",
        "04/22/1968",
        "clean diabetes and hypertension follow-up; the chart lists the same doses",
        """Chief complaint: Diabetes and blood pressure follow-up.
HPI: 58-year-old woman seen for follow-up of type 2 diabetes and hypertension. Fasting home glucose
readings 110 to 140. No hypoglycemia. Taking her medications as prescribed.
Allergies: No known drug allergies.
Medications:
- Metformin 500 mg PO twice daily
- Lisinopril 10 mg PO once daily
Vitals: BP 128/78, HR 72, Temp 98.4 F, RR 14, SpO2 98% on room air, Weight 82 kg.
Exam: Well appearing. Lungs clear. Heart regular rate and rhythm. Feet without lesions.
Assessment and plan:
1. Type 2 diabetes mellitus without complications. A1c 6.8%. Continue metformin.
2. Essential hypertension, controlled. Continue lisinopril.
Follow-up: Return in 3 months with repeat A1c.
""",
    ),
    (
        "note-02",
        "LFP-20422",
        HARROW,
        "09/02/2026",
        "Inez Calloway, NP",
        "Rowan Petrakis",
        "11/03/1979",
        "levothyroxine written in mg instead of mcg",
        """Chief complaint: Tiredness, thyroid recheck.
HPI: 46-year-old man with hypothyroidism, diagnosed last year. Reports mild fatigue. Also has
heartburn after meals most days, better with omeprazole.
Allergies: NKDA.
Medications:
- Levothyroxine 50 mg PO daily before breakfast
- Omeprazole 20 mg PO daily
Vitals: BP 122/80, HR 64, Temp 98.1 F, RR 12, SpO2 99%, Weight 79 kg.
Exam: Thyroid not enlarged. Abdomen soft, non-tender.
Assessment and plan:
1. Hypothyroidism. TSH today. Continue levothyroxine.
2. Gastroesophageal reflux disease without esophagitis. Continue omeprazole.
Follow-up: 6 weeks after TSH result.
""",
    ),
    (
        "note-03",
        "LFP-20425",
        HARROW,
        "09/09/2026",
        "Teodor Vash, MD",
        "Delphine Okonkwo-Hale",
        "07/30/1957",
        "heart rate typed as 720; blood pressure high",
        """Chief complaint: Blood pressure check.
HPI: 69-year-old woman with hypertension and high cholesterol. Home readings in the 160s systolic
for two weeks. No chest pain, no headache, no shortness of breath.
Allergies: No known drug allergies.
Medications:
- Amlodipine 10 mg PO once daily (increased today)
- Atorvastatin 40 mg PO at bedtime
Vitals: BP 168/102, HR 720, Temp 98.9 F, RR 16, SpO2 97%, Weight 70 kg.
Exam: Heart regular. No edema.
Assessment and plan:
1. Essential hypertension, not at goal. Amlodipine increased to 10 mg daily.
2. Hyperlipidemia. Continue atorvastatin.
Follow-up: Nurse blood pressure check in 2 weeks.
""",
    ),
    (
        "note-04",
        "LFP-20431",
        BELLWETHER,
        "09/21/2026",
        "Inez Calloway, NP",
        "Caius Wendell-Moore",
        "02/14/1998",
        "amoxicillin started despite a penicillin allergy",
        """Chief complaint: Sore throat for three days.
HPI: 28-year-old man with sore throat, fever and painful swallowing. No cough. Rapid strep test
positive in clinic today.
Allergies: Penicillin (hives).
Medications: None before this visit.
Vitals: BP 118/74, HR 96, Temp 101.2 F, RR 16, SpO2 98%.
Exam: Tonsils enlarged with exudate. Tender anterior cervical nodes.
Assessment and plan:
1. Streptococcal pharyngitis. Start amoxicillin 500 mg PO three times daily for 10 days.
Fluids and rest.
Follow-up: Return if not improving in 48 hours.
""",
    ),
    (
        "note-05",
        "LFP-20436",
        HARROW,
        "10/06/2026",
        "Teodor Vash, MD",
        "Anselm Brightwater",
        "09/09/1962",
        "simvastatin stopped, atorvastatin started; the intake list still has simvastatin",
        """Chief complaint: Muscle aches on cholesterol medicine.
HPI: 64-year-old man with hyperlipidemia. Simvastatin 40 mg was stopped on 09/30/2026 because of
muscle aches, which have since resolved. LDL 162 last month.
Allergies: No known drug allergies.
Medications:
- Atorvastatin 20 mg PO nightly, started today
Vitals: BP 126/82, HR 68, Temp 98.2 F, RR 14, SpO2 98%, Weight 88 kg.
Exam: Full strength in all limbs. No muscle tenderness.
Assessment and plan:
1. Hyperlipidemia. Start atorvastatin 20 mg nightly. Lipid panel and CK in 8 weeks.
Follow-up: Return in 8 weeks.
""",
    ),
    (
        "note-06",
        "LFP-20440",
        BELLWETHER,
        "08/27/2026",
        "Inez Calloway, NP",
        "Priya Halvorsen",
        "05/18/2004",
        "albuterol with no dose written; sibling asthma and rhinitis codes",
        """Chief complaint: Wheezing with exercise and spring allergies.
HPI: 22-year-old woman with wheeze when running, about twice a month. No night symptoms. Sneezing
and itchy eyes each spring.
Allergies: Sulfa drugs (rash).
Medications:
- Albuterol inhaler as needed for wheeze
- Cetirizine 10 mg PO daily
Vitals: BP 110/70, HR 70, Temp 98.0 F, RR 14, SpO2 99%, Weight 58 kg.
Exam: Lungs clear today. Nasal turbinates pale and swollen.
Assessment and plan:
1. Mild intermittent asthma, uncomplicated. Continue albuterol as needed.
2. Allergic rhinitis due to pollen. Continue cetirizine.
Follow-up: Return in 6 months or sooner if needed.
""",
    ),
    (
        "note-07",
        "LFP-20444",
        BELLWETHER,
        "10/01/2026",
        "Teodor Vash, MD",
        "Lucan Merriweather",
        "12/01/1975",
        "plantar fasciitis is not in the code subset (coder review)",
        """Chief complaint: Left heel pain.
HPI: 50-year-old man with sharp pain in the left heel, worst with the first steps in the morning,
for six weeks. Started walking for weight loss this summer.
Allergies: No known drug allergies.
Medications:
- Ibuprofen 400 mg PO every 8 hours as needed for pain
Vitals: BP 130/84, HR 74, Temp 98.6 F, RR 14, SpO2 98%, Weight 104 kg, BMI 32.4.
Exam: Tender at the medial calcaneal tubercle of the left foot. Normal ankle range of motion.
Assessment and plan:
1. Plantar fasciitis, left foot. Stretching, supportive shoes, ice. Ibuprofen as needed.
2. Obesity. Discussed diet and a walking plan.
Follow-up: Return in 6 weeks.
""",
    ),
    (
        "note-08",
        "LFP-20449",
        BELLWETHER,
        "10/08/2026",
        "Inez Calloway, NP",
        "Odile Fairbanks",
        "03/27/1990",
        "no allergy statement in the note and none on file in the chart",
        """Chief complaint: Burning with urination.
HPI: 36-year-old woman with two days of burning on urination and urinary frequency. No flank
pain. Not pregnant. Urine dipstick positive for leukocytes and nitrites.
Medications:
- Nitrofurantoin 100 mg PO twice daily for 5 days
Vitals: BP 116/72, HR 88, Temp 100.9 F, RR 16, SpO2 99%.
Exam: Mild suprapubic tenderness. No costovertebral angle tenderness.
Assessment and plan:
1. Acute cystitis without hematuria. Start nitrofurantoin. Urine culture sent.
""",
    ),
    (
        "note-09",
        "LFP-20453",
        HARROW,
        "10/12/2026",
        "Teodor Vash, MD",
        "Tobias Renfrew",
        "06/12/1971",
        "lisinopril in the medication list, stopped in the plan (a later stop must win)",
        """Chief complaint: Dry cough for a month.
HPI: 55-year-old man with hypertension, on lisinopril for two years. Dry, tickly cough for four
weeks. No fever, no shortness of breath, no wheeze.
Allergies: No known drug allergies.
Medications:
- Lisinopril 10 mg daily
Vitals: BP 134/84, HR 70, Temp 98.2 F, RR 14, SpO2 98%, Weight 91 kg.
Exam: Lungs clear. Throat normal.
Assessment and plan:
1. Essential hypertension. Stop lisinopril because of a dry cough. Start losartan 50 mg daily.
Expect the cough to settle within a month.
Follow-up: Blood pressure and potassium check in 2 weeks.
""",
    ),
    (
        "note-10",
        "LFP-20458",
        BELLWETHER,
        "10/14/2026",
        "Inez Calloway, NP",
        "Wilhelmina Asante",
        "01/19/1983",
        "family history of type 2 diabetes next to a prediabetes assessment",
        """Chief complaint: Annual physical and lab review.
HPI: 43-year-old woman here for a physical. Mother has type 2 diabetes. No increased thirst or
urination. Walks three times a week.
Allergies: No known drug allergies.
Medications: None.
Vitals: BP 118/76, HR 66, Temp 98.0 F, RR 12, SpO2 99%, Weight 74 kg, BMI 27.1.
Labs: A1c 6.0%, in the prediabetes range. Fasting glucose 108.
Assessment and plan:
1. Prediabetes. Diet and exercise counseling. Repeat A1c in 6 months.
Follow-up: Return in 6 months.
""",
    ),
]

# The registered patients: (id, name, DOB, registered, the chart's allergy status).
PATIENTS = [
    ("LFP-20417", "Marisol Quenby", "1968-04-22", "2026-10-05", "no_known_allergies"),
    ("LFP-20422", "Rowan Petrakis", "1979-11-03", "2026-10-07", "no_known_allergies"),
    ("LFP-20425", "Delphine Okonkwo-Hale", "1957-07-30", "2026-10-08", "no_known_allergies"),
    ("LFP-20431", "Caius Wendell-Moore", "1998-02-14", "2026-10-12", "not_recorded"),
    ("LFP-20436", "Anselm Brightwater", "1962-09-09", "2026-10-13", "no_known_allergies"),
    ("LFP-20440", "Priya Halvorsen", "2004-05-18", "2026-10-14", "allergies_listed"),
    ("LFP-20444", "Lucan Merriweather", "1975-12-01", "2026-10-15", "no_known_allergies"),
    ("LFP-20449", "Odile Fairbanks", "1990-03-27", "2026-10-19", "not_recorded"),
    ("LFP-20453", "Tobias Renfrew", "1971-06-12", "2026-10-20", "no_known_allergies"),
    ("LFP-20458", "Wilhelmina Asante", "1983-01-19", "2026-10-21", "no_known_allergies"),
]
# Problem-list items already on the chart, from the intake form.
PROBLEMS = [
    ("LFP-20422", "Hypothyroidism", "E03.9", "2026-10-07"),
    ("LFP-20436", "Hyperlipidemia", "E78.5", "2026-10-13"),
]
# What each patient wrote on the intake form. Rowan wrote 50 mcg; the outside note says 50 mg.
MEDICATIONS = [
    ("LFP-20417", "Metformin", "500 mg", "PO", "twice daily", "intake form"),
    ("LFP-20417", "Lisinopril", "10 mg", "PO", "once daily", "intake form"),
    ("LFP-20422", "Levothyroxine", "50 mcg", "PO", "daily", "intake form"),
    ("LFP-20422", "Omeprazole", "20 mg", "PO", "daily", "intake form"),
    ("LFP-20425", "Amlodipine", "10 mg", "PO", "once daily", "intake form"),
    ("LFP-20425", "Atorvastatin", "40 mg", "PO", "at bedtime", "intake form"),
    ("LFP-20436", "Simvastatin", "40 mg", "PO", "nightly", "intake form"),
    ("LFP-20440", "Cetirizine", "10 mg", "PO", "daily", "intake form"),
    ("LFP-20453", "Losartan", "50 mg", "PO", "daily", "intake form"),
]
ALLERGIES = [("LFP-20440", "Sulfa drugs", "rash", "sulfa")]
# Delphine came in for a nurse blood-pressure check before her first visit.
VITALS = [
    ("LFP-20425", "2026-10-28", "blood_pressure", "152/94"),
    ("LFP-20425", "2026-10-28", "heart_rate", "76"),
]
RECEIVED = {
    "note-01": "2026-10-26",
    "note-02": "2026-10-26",
    "note-03": "2026-10-27",
    "note-04": "2026-10-27",
    "note-05": "2026-10-28",
    "note-06": "2026-10-28",
    "note-07": "2026-10-29",
    "note-08": "2026-10-29",
    "note-09": "2026-10-30",
    "note-10": "2026-10-30",
}


def iso(us_date: str) -> str:
    month, day, year = us_date.split("/")
    return f"{year}-{month}-{day}"


def write_csv(name: str, columns: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> None:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)
    (DATA_DIR / name).write_text(buffer.getvalue(), encoding="utf-8")


def write_data() -> None:
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    documents = []
    for doc_id, patient_id, clinic, visit, clinician, patient, dob, _case, body in NOTES:
        text = header(clinic, visit, clinician, patient, dob) + body
        (NOTES_DIR / f"{doc_id}.txt").write_text(text, encoding="utf-8")
        row = (doc_id, patient_id, RECEIVED[doc_id], clinic, iso(visit))
        documents.append((*row, "outside records", f"{doc_id}.txt"))
    write_csv(
        "ehr_documents.csv",
        (
            *("document_id", "patient_id", "received_on", "source_clinic"),
            *("visit_date", "category", "file"),
        ),
        documents,
    )
    write_csv(
        "ehr_patients.csv",
        ("patient_id", "name", "dob", "registered_on", "allergy_status"),
        PATIENTS,
    )
    write_csv("ehr_problems.csv", ("patient_id", "problem", "code", "noted_on"), PROBLEMS)
    write_csv(
        "ehr_medications.csv",
        ("patient_id", "drug", "dose", "route", "frequency", "source"),
        MEDICATIONS,
    )
    write_csv(
        "ehr_allergies.csv", ("patient_id", "substance", "reaction", "allergy_class"), ALLERGIES
    )
    write_csv("ehr_vitals.csv", ("patient_id", "taken_on", "vital", "value"), VITALS)


def dotted(code: str) -> str:
    """ICD-10-CM codes are printed with a dot after the third character (E119 -> E11.9)."""
    return code if len(code) <= 3 else f"{code[:3]}.{code[3:]}"


def write_subset(cms_codes: Path) -> None:
    """Cut the subset from the CMS code file (`CODE<spaces>Description` per line)."""
    descriptions: dict[str, str] = {}
    for line in cms_codes.read_text(encoding="utf-8").splitlines():
        code, _, description = line.partition(" ")
        descriptions[code] = description.strip()
    missing = [code for code in SUBSET_CODES if code not in descriptions]
    if missing:
        raise SystemExit(f"Codes not in {cms_codes}: {', '.join(missing)}")
    rows = ["code\tdescription"]
    rows += [f"{dotted(code)}\t{descriptions[code]}" for code in SUBSET_CODES]
    SUBSET_PATH.write_text("\n".join(rows) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--cms-codes", type=Path, help="CMS icd10cm_codes_2027.txt, to rewrite the code subset"
    )
    args = parser.parse_args()
    write_data()
    print(f"wrote the outside notes and chart seeds in {DATA_DIR}")
    if args.cms_codes:
        write_subset(args.cms_codes)
        print(f"wrote {SUBSET_PATH}")


if __name__ == "__main__":
    main()
