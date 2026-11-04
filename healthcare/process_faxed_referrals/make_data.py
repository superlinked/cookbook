"""Write the synthetic referral fax packets and the EHR and directory seeds in data/.

Every patient, provider, clinic, insurer, phone number, MRN and NPI below is fictional. NPIs
are well-formed (10 digits with a valid check digit) and not assigned in the NPPES NPI Registry
as of 4 Nov 2026 (UTC); phone numbers use the 555-01xx range reserved for fiction.

Each packet has a fax cover sheet, a referral order and a clinic note. The pages are drawn at
fax "fine" resolution (200 DPI), tilted slightly, dithered to pure black and white and saved
with CCITT Group 4 compression, no text layer and no document info, so the recipe has to OCR
them like a real fax and the files rebuild byte for byte.

It also writes the two lookups the recipe checks the fax against:

- `ehr_patients.csv`: the charts the EHR already has (standing in for a FHIR Patient search).
- `provider_directory.csv`: the referring providers the practice knows (by NPI).

    uv run make_data.py
"""

from __future__ import annotations

import csv
import random
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"

DPI = 200
PAGE_SIZE = (1700, 2200)  # US Letter at 200 DPI (fax "fine" mode)
MARGIN = 130
FONT_SIZE = 30
LINE_GAP = 12

RECEIVER = "Larkspur Valley Cardiology Associates"
RECEIVER_FAX = "(555) 010-2200"
BLANK = "____________________"


@dataclass(frozen=True)
class Packet:
    filename: str
    clinic: str
    clinic_phone: str
    clinic_fax: str
    provider: str
    npi: str
    patient: str
    dob: str
    sex: str
    patient_phone: str
    insurer: str
    member_id: str  # "" = left blank on the form
    reason: str
    icd10: str
    priority: str  # the order's priority line, as the referring office wrote it
    cover_urgent: bool  # an "*** URGENT ***" banner on the cover sheet
    signed: bool  # the order's signature line is signed
    sent: str  # fax header timestamp
    note: tuple[str, ...]
    seed: int
    # The clinic note's patient, when the office attached another patient's note.
    note_patient: str = ""
    note_dob: str = ""


PACKETS = (
    # Complete, routine; a new patient.
    Packet(
        filename="fax-1021-brightwater-family-medicine.pdf",
        clinic="Brightwater Family Medicine",
        clinic_phone="(555) 010-4470",
        clinic_fax="(555) 010-4471",
        provider="Maren Okafor, MD",
        npi="1999001013",
        patient="Delphine Arkwright",
        dob="03/14/1961",
        sex="F",
        patient_phone="(555) 010-3381",
        insurer="Cedarpoint Health Plan",
        member_id="CPH-4471-0923",
        reason="Exertional chest pressure, abnormal resting ECG.",
        icd10="R07.89",
        priority="Routine",
        cover_urgent=False,
        signed=True,
        sent="OCT-21-2026 09:14",
        note=(
            "Subjective: 65-year-old woman with six weeks of chest pressure when climbing two "
            "flights of stairs, relieved by rest within five minutes. No pain at rest. "
            "Former smoker.",
            "Objective: BP 142/88, HR 78, BMI 29. Heart sounds normal, no murmur. "
            "Resting ECG shows new T-wave inversion in leads V4 to V6.",
            "Assessment: Exertional chest pressure, possible stable angina.",
            "Plan: Started aspirin 81 mg daily. Refer to cardiology for evaluation and "
            "stress testing.",
        ),
        seed=101,
    ),
    # Planted: member ID left blank, and the patient has no chart here.
    Packet(
        filename="fax-1021-hollis-creek-internal-medicine.pdf",
        clinic="Hollis Creek Internal Medicine",
        clinic_phone="(555) 010-5520",
        clinic_fax="(555) 010-5521",
        provider="Iris Vandermolen, DO",
        npi="1999002029",
        patient="Gideon Pashley",
        dob="07/02/1984",
        sex="M",
        patient_phone="(555) 010-6614",
        insurer="Harborline Mutual",
        member_id="",
        reason="Recurrent palpitations, SVT on Holter monitor.",
        icd10="R00.2",
        priority="Routine",
        cover_urgent=False,
        signed=True,
        sent="OCT-21-2026 10:02",
        note=(
            "Subjective: 42-year-old man with episodes of rapid heartbeat two to three times "
            "a week for four months, lasting up to ten minutes. No fainting.",
            "Objective: BP 128/80, HR 72 and regular. Thyroid tests normal. 48-hour Holter monitor "
            "recorded a 14-beat run of supraventricular tachycardia.",
            "Assessment: Paroxysmal palpitations, likely supraventricular tachycardia.",
            "Plan: Avoid caffeine. Refer to cardiology to discuss management.",
        ),
        seed=202,
    ),
    # Planted: the order is not signed; the clinic note behind it is electronically signed.
    Packet(
        filename="fax-1021-quarry-hill-medical-group.pdf",
        clinic="Quarry Hill Medical Group",
        clinic_phone="(555) 010-7730",
        clinic_fax="(555) 010-7731",
        provider="Samuel Achterberg, MD",
        npi="1999003035",
        patient="Rosalind Teague",
        dob="11/23/1957",
        sex="F",
        patient_phone="(555) 010-2296",
        insurer="Saltmarsh Health Cooperative",
        member_id="SHC-880214-3",
        reason="New systolic murmur, exertional dyspnea.",
        icd10="R01.1",
        priority="Routine",
        cover_urgent=False,
        signed=False,
        sent="OCT-21-2026 11:37",
        note=(
            "Subjective: 68-year-old woman with three months of shortness of breath walking "
            "uphill. No chest pain.",
            "Objective: BP 136/82, HR 84. Grade 3/6 systolic murmur at the right upper sternal "
            "border, not heard at her visit last year.",
            "Assessment: New systolic murmur, possible aortic stenosis.",
            "Plan: Refer to cardiology for echocardiogram and evaluation.",
        ),
        seed=303,
    ),
    # Planted: marked urgent on the cover and the order; an existing patient.
    Packet(
        filename="fax-1021-fenwick-street-clinic.pdf",
        clinic="Fenwick Street Clinic",
        clinic_phone="(555) 010-8840",
        clinic_fax="(555) 010-8841",
        provider="Nadia Querol, MD",
        npi="1999004041",
        patient="Emeric Halvorsen",
        dob="01/09/1949",
        sex="M",
        patient_phone="(555) 010-9157",
        insurer="Cedarpoint Health Plan",
        member_id="CPH-5582-1170",
        reason="Syncope with new left bundle branch block.",
        icd10="R55",
        priority="URGENT - please schedule within 72 hours",
        cover_urgent=True,
        signed=True,
        sent="OCT-21-2026 13:48",
        note=(
            "Subjective: 77-year-old man who fainted while standing in his kitchen two days ago, "
            "with a few seconds of light-headedness beforehand. No injury.",
            "Objective: BP 118/70, HR 54. ECG shows a new left bundle branch block compared with "
            "his ECG from last year.",
            "Assessment: Syncope with new conduction disease.",
            "Plan: Advised to call 911 for any further fainting. Refer to cardiology within "
            "72 hours.",
        ),
        seed=404,
    ),
    # Planted: member ID left blank, but the patient has a chart here with the member ID on
    # file. The EHR also has his son, with the same name and another date of birth.
    Packet(
        filename="fax-1021-millbrook-community-health.pdf",
        clinic="Millbrook Community Health",
        clinic_phone="(555) 010-1180",
        clinic_fax="(555) 010-1181",
        provider="Priya Lindqvist, FNP",
        npi="1999005055",
        patient="Corwin Ashdown",
        dob="05/30/1972",
        sex="M",
        patient_phone="(555) 010-4402",
        insurer="Saltmarsh Health Cooperative",
        member_id="",
        reason="Resistant hypertension on three medications.",
        icd10="I10",
        priority="Routine",
        cover_urgent=False,
        signed=True,
        sent="OCT-21-2026 15:21",
        note=(
            "Subjective: 54-year-old man with high blood pressure for ten years. Takes his "
            "medicines every day and checks his blood pressure at home.",
            "Objective: BP 158/96 and 154/94. Home readings average 150/92. Kidney function "
            "normal.",
            "Assessment: Hypertension not at goal on three medications.",
            "Plan: Refer to cardiology for evaluation of resistant hypertension.",
        ),
        seed=505,
    ),
    # Planted: a time frame with no urgent keyword, member ID blank, a new patient, and a
    # referring provider the directory doesn't know yet.
    Packet(
        filename="fax-1021-juniper-road-family-practice.pdf",
        clinic="Juniper Road Family Practice",
        clinic_phone="(555) 010-2750",
        clinic_fax="(555) 010-2751",
        provider="Tomasz Wierzbicki, MD",
        npi="1999008083",
        patient="Odessa Pruitt",
        dob="09/02/1966",
        sex="F",
        patient_phone="(555) 010-8806",
        insurer="Harborline Mutual",
        member_id="",
        reason="New leg swelling, breathless lying flat, BNP 1,240 pg/mL.",
        icd10="R60.0",
        priority="Please see within 72 hours",
        cover_urgent=False,
        signed=True,
        sent="OCT-21-2026 16:05",
        note=(
            "Subjective: 60-year-old woman with two weeks of ankle swelling. Wakes at night "
            "short of breath and now sleeps on three pillows.",
            "Objective: BP 150/90, HR 96. Crackles at both lung bases, pitting edema to mid "
            "shin. BNP 1,240 pg/mL.",
            "Assessment: New heart failure symptoms, cause not yet known.",
            "Plan: Started furosemide 20 mg daily. Refer to cardiology, please see within "
            "72 hours.",
        ),
        seed=606,
    ),
    # Planted: the order is for Ann Lee; the clinic note attached is Joann Lee's, with
    # another date of birth.
    Packet(
        filename="fax-1021-cobble-lane-health-center.pdf",
        clinic="Cobble Lane Health Center",
        clinic_phone="(555) 010-3340",
        clinic_fax="(555) 010-3341",
        provider="Renata Osei, MD",
        npi="1999006061",
        patient="Ann Lee",
        dob="06/17/1958",
        sex="F",
        patient_phone="(555) 010-5193",
        insurer="Cedarpoint Health Plan",
        member_id="CPH-7720-0418",
        reason="Atrial fibrillation found on a pre-operative ECG.",
        icd10="I48.91",
        priority="Routine",
        cover_urgent=False,
        signed=True,
        sent="OCT-21-2026 16:40",
        note=(
            "Subjective: 55-year-old woman here for a blood pressure check. No complaints.",
            "Objective: BP 132/84, HR 70 and regular. Weight stable.",
            "Assessment: Hypertension, well controlled on lisinopril.",
            "Plan: Continue lisinopril 10 mg daily. Recheck in six months.",
        ),
        seed=707,
        note_patient="Joann Lee",
        note_dob="02/03/1971",
    ),
    # Planted: the patient has a chart here under her maiden name (Helena Voss, the same
    # date of birth and phone); the member ID on file is not hers until someone checks.
    Packet(
        filename="fax-1021-orchard-bend-medical.pdf",
        clinic="Orchard Bend Medical",
        clinic_phone="(555) 010-6620",
        clinic_fax="(555) 010-6621",
        provider="Lucas Brennan, PA-C",
        npi="1999007077",
        patient="Helena Marchetti",
        dob="04/18/1979",
        sex="F",
        patient_phone="(555) 010-6731",
        insurer="Cedarpoint Health Plan",
        member_id="",
        reason="New palpitations, known mitral valve prolapse.",
        icd10="R00.2",
        priority="Routine",
        cover_urgent=False,
        signed=True,
        sent="OCT-21-2026 17:12",
        note=(
            "Subjective: 47-year-old woman with mitral valve prolapse, seen by cardiology "
            "years ago, now with a month of fluttering in the chest most evenings.",
            "Objective: BP 118/74, HR 76 and regular. Mid-systolic click, soft late "
            "systolic murmur.",
            "Assessment: Palpitations in known mitral valve prolapse.",
            "Plan: Refer to cardiology for evaluation and an event monitor.",
        ),
        seed=808,
    ),
)

# The charts the EHR already has. Only Halvorsen and the older Corwin Ashdown are exact
# matches; the younger Corwin Ashdown has the same name and another date of birth, and Helena
# Voss is Helena Marchetti's chart under her maiden name.
EHR_FIELDS = (
    *("mrn", "given_name", "family_name", "birth_date", "phone"),
    *("insurance_plan", "member_id"),
)
EHR_ROWS = (
    ("MRN-100102", "Theodora", "Quill", "1963-02-07", "(555) 010-7714"),
    ("MRN-100231", "Corwin", "Ashdown", "1972-05-30", "(555) 010-4402"),
    ("MRN-100232", "Corwin", "Ashdown", "1998-11-04", "(555) 010-4415"),
    ("MRN-100457", "Emeric", "Halvorsen", "1949-01-09", "(555) 010-9157"),
    ("MRN-100689", "Helena", "Voss", "1979-04-18", "(555) 010-6731"),
    ("MRN-100743", "Bertram", "Oyelaran", "1950-12-19", "(555) 010-3027"),
)
EHR_COVERAGE = {
    "MRN-100102": ("Harborline Mutual", "HBM-55102-7"),
    "MRN-100231": ("Saltmarsh Health Cooperative", "SHC-417730-1"),
    "MRN-100232": ("Saltmarsh Health Cooperative", "SHC-417730-2"),
    "MRN-100457": ("Cedarpoint Health Plan", "CPH-5582-1170"),
    "MRN-100689": ("Cedarpoint Health Plan", "CPH-3390-2214"),
    "MRN-100743": ("Saltmarsh Health Cooperative", "SHC-220981-4"),
}

# The referring providers the practice knows. Juniper Road (Wierzbicki) is new and not here.
DIRECTORY_FIELDS = ("npi", "name", "practice", "phone", "fax")
DIRECTORY_EXTRA = (
    ("1999009099", "Hugo Rasmussen, MD", "Saltmarsh Family Physicians", "(555) 010-9930"),
)


def cover_lines(packet: Packet) -> list[tuple[str, str]]:
    lines = [("title", "FAX COVER SHEET")]
    if packet.cover_urgent:
        lines.append(("title", "*** URGENT ***"))
    lines += [
        ("body", f"Date: {packet.sent.split()[0].replace('OCT-21-', '10/21/')}"),
        ("body", f"To: New Patient Intake, {RECEIVER}"),
        ("body", f"Fax: {RECEIVER_FAX}"),
        ("body", f"From: {packet.clinic}"),
        ("body", f"Phone: {packet.clinic_phone}    Fax: {packet.clinic_fax}"),
        ("body", "Pages (including cover): 3"),
        ("body", "Re: New patient referral"),
        ("gap", ""),
        (
            "body",
            "Please see the attached referral and clinic note. Call our office with any questions.",
        ),
        ("gap", ""),
        (
            "small",
            "CONFIDENTIALITY NOTICE: This fax contains confidential health information intended "
            "only for the recipient named above. If you received it in error, please call the "
            "sender and destroy all copies.",
        ),
    ]
    return lines


def form_lines(packet: Packet) -> list[tuple[str, str]]:
    signature = (
        f"Electronically signed by: {packet.provider}    Date: 10/21/2026"
        if packet.signed
        else f"Provider signature: {BLANK}    Date: __________"
    )
    return [
        ("title", "OUTPATIENT REFERRAL ORDER"),
        ("body", f"Referred to: {RECEIVER}    Specialty: Cardiology"),
        ("heading", "PATIENT"),
        ("body", f"Patient name: {packet.patient}"),
        ("body", f"Date of birth: {packet.dob}    Sex: {packet.sex}"),
        ("body", f"Patient phone: {packet.patient_phone}"),
        ("heading", "INSURANCE"),
        ("body", f"Insurance plan: {packet.insurer}"),
        ("body", f"Member ID: {packet.member_id or BLANK}"),
        ("heading", "REFERRING PROVIDER"),
        ("body", f"Name: {packet.provider}"),
        ("body", f"NPI: {packet.npi}"),
        ("body", f"Practice: {packet.clinic}"),
        ("body", f"Phone: {packet.clinic_phone}    Fax: {packet.clinic_fax}"),
        ("heading", "REFERRAL"),
        ("body", f"Reason for referral: {packet.reason}"),
        ("body", f"Diagnosis code: {packet.icd10}"),
        ("body", f"Priority: {packet.priority}"),
        ("body", "Services requested: Consult and treat."),
        ("heading", "ORDERING PROVIDER"),
        ("body", signature),
    ]


def note_lines(packet: Packet) -> list[tuple[str, str]]:
    patient = packet.note_patient or packet.patient
    dob = packet.note_dob or packet.dob
    lines = [
        ("title", "CLINIC NOTE"),
        ("body", packet.clinic),
        ("body", f"Patient: {patient}    DOB: {dob}    Visit date: 10/20/2026"),
        ("body", f"Provider: {packet.provider}"),
        ("gap", ""),
    ]
    lines += [("body", paragraph) for paragraph in packet.note]
    lines += [
        ("gap", ""),
        ("body", f"Dictated by: {packet.provider}"),
        ("body", f"Electronically signed by: {packet.provider}    Date: 10/20/2026"),
    ]
    return lines


def _wrap(text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if font.getlength(candidate) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def draw_page(packet: Packet, number: int, lines: list[tuple[str, str]]) -> Image.Image:
    sizes = {"title": FONT_SIZE + 12, "heading": FONT_SIZE + 2, "body": FONT_SIZE, "small": 24}
    page = Image.new("L", PAGE_SIZE, 255)
    draw = ImageDraw.Draw(page)
    header = (
        f"{packet.sent}    FROM: {packet.clinic.upper()}    FAX {packet.clinic_fax}    "
        f"TO: {RECEIVER_FAX}    P. {number}/3"
    )
    draw.text((40, 24), header, font=ImageFont.load_default(size=22), fill=0)
    y = MARGIN
    width = PAGE_SIZE[0] - 2 * MARGIN
    for style, text in lines:
        if style == "gap":
            y += FONT_SIZE
            continue
        font = ImageFont.load_default(size=sizes[style])
        if style == "heading":
            y += 14
        for line in _wrap(text, font, width):
            draw.text((MARGIN, y), line, font=font, fill=0)
            y += sizes[style] + LINE_GAP
        y += 10
    return page


def _fax(page: Image.Image, rng: random.Random) -> Image.Image:
    """Make a clean page look like a received fax: skew, smudge, noise, then 1-bit dither."""
    skewed = page.rotate(rng.uniform(-0.8, 0.8), resample=Image.Resampling.BICUBIC, fillcolor=255)
    soft = skewed.filter(ImageFilter.GaussianBlur(0.7))
    pixels = soft.load()
    assert pixels is not None
    width, height = soft.size
    for _ in range(6000):
        x, y = rng.randrange(width), rng.randrange(height)
        pixels[x, y] = rng.randint(0, 160)
    # Floyd-Steinberg dither to pure black and white, as a fax machine sends it.
    return soft.point(lambda value: min(255, int(value * 1.1))).convert("1")


def render_pages(packet: Packet) -> list[Image.Image]:
    rng = random.Random(packet.seed)
    pages = (cover_lines(packet), form_lines(packet), note_lines(packet))
    return [_fax(draw_page(packet, n, lines), rng) for n, lines in enumerate(pages, start=1)]


def _save_pdf(path: Path, pages: list[Image.Image]) -> None:
    """An image-only PDF with no document info (no title, author or dates), so it rebuilds byte
    for byte."""
    pages[0].save(
        path,
        format="PDF",
        save_all=True,
        append_images=pages[1:],
        resolution=DPI,
        title="",
        creationDate="",
        modDate="",
    )


def _write_csv(path: Path, header: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def write_seeds() -> None:
    charts = [(*row, *EHR_COVERAGE[row[0]]) for row in EHR_ROWS]
    _write_csv(DATA_DIR / "ehr_patients.csv", EHR_FIELDS, charts)
    known = [p for p in PACKETS if p.clinic != "Juniper Road Family Practice"]
    providers = [(p.npi, p.provider, p.clinic, p.clinic_phone, p.clinic_fax) for p in known]
    providers += [(*row, row[3][:-1] + "1") for row in DIRECTORY_EXTRA]
    _write_csv(DATA_DIR / "provider_directory.csv", DIRECTORY_FIELDS, sorted(providers))
    print("wrote data/ehr_patients.csv and data/provider_directory.csv")


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    for packet in PACKETS:
        pages = render_pages(packet)
        path = DATA_DIR / packet.filename
        _save_pdf(path, pages)
        print(f"wrote {path.relative_to(ROOT)} ({len(pages)} pages)")
    write_seeds()


if __name__ == "__main__":
    main()
