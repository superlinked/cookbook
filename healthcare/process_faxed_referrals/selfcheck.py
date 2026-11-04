"""Check the recipe's parsing, lookups, priority wording and statuses offline.

The checks use hand-written strings, the committed data/ and hand-written stand-in model
replies: the stand-in OCR text is built from the lines make_data.py draws on each fax page,
and the stand-in extractor returns the value after each field's label. Nothing here calls SIE
or replays a recorded response. Run it with `uv run --frozen python selfcheck.py`; it exits
non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import io
import re
import shutil
import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, cast

from ehr import EHR
from provider_directory import ProviderDirectory
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
DATA = recipe.DATA_DIR
PACKETS = {Path(p.filename).stem.split("-", 2)[2]: p for p in make_data.PACKETS}


# --- Stand-ins --------------------------------------------------------------------------------


def ocr_text(packet: make_data.Packet, number: int) -> str:
    """A fax page as LightOnOCR writes it: the fax header, then Markdown, spaces collapsed."""
    lines = (make_data.cover_lines, make_data.form_lines, make_data.note_lines)[number - 1]
    header = (
        f"{packet.sent} FROM: {packet.clinic.upper()} FAX {packet.clinic_fax} "
        f"TO: {make_data.RECEIVER_FAX} P. {number}/3"
    )
    out = [header]
    for style, text in lines(packet):
        prefix = {"title": "# ", "heading": "## ", "body": "", "small": "", "gap": ""}[style]
        out.append(prefix + re.sub(r"\s{2,}", " ", text))
    return "\n\n".join(out)


STAND_IN_LABELS = {name: spec.get("labels", []) for name, spec in RULES["fields"].items()}
STAND_IN_LABELS["requested_priority"] = ["priority"]
STAND_IN_LABELS["order_signed_by"] = [*RULES["fields"]["order_signed_by"]["labels"], "e-signed by"]
STAND_IN_LABELS["order_signed_by"].append("electronically signed by")
# Every label printed on the pages, so a value ends where the next label starts.
OTHER_LABELS = ["sex", "visit date", "date", "practice", "diagnosis code", "services requested"]
OTHER_LABELS += ["specialty", "referred to", "from", "to", "re", "dictated by"]
OTHER_LABELS += ["provider signature"]
FIELD_LABELS = [label for labels in STAND_IN_LABELS.values() for label in labels]
EVERY_LABEL = sorted({*OTHER_LABELS, *FIELD_LABELS}, key=len, reverse=True)
LABEL = re.compile(
    r"(?i)(?:^|(?<=\s))(" + "|".join(re.escape(label) for label in EVERY_LABEL) + r")\s*:"
)


def stand_in_fields(text: str) -> dict[str, str]:
    """What a well-behaved GLiNER2 would return: for each field, the value after the first
    label that is exactly one of its labels, up to the next label on the line."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("OCT-"):
            continue  # the fax header
        marks = list(LABEL.finditer(line))
        if not marks:
            continue
        for here, after in zip(marks, [*marks[1:], None], strict=True):
            value = line[here.end() : after.start() if after else len(line)].strip()
            label = here.group(1).casefold()
            for name, labels in STAND_IN_LABELS.items():
                if label in labels and name not in found and value:
                    found[name] = value
    return found


class StandIn:
    """Stands in for SIEClient. OCR calls return the page text in `pages` by item id
    ("ocr/<stem>/p<n>"); extract calls return `stand_in_fields` of the window, changed by
    `fields` (item id prefix -> {field: value or None to drop}). `errors` maps an item id prefix
    to the errors its calls raise (one per call), `replies` to a raw reply."""

    def __init__(
        self,
        pages: dict[str, str],
        errors: dict[str, list[Exception]] | None = None,
        replies: dict[str, Any] | None = None,
        fields: dict[str, dict[str, str | None]] | None = None,
        max_words: int | None = None,
    ) -> None:
        self.pages, self.errors, self.replies = pages, errors or {}, replies or {}
        self.fields = fields or {}
        # A text longer than this is over-long for the extractor: with overflow_policy "error"
        # the call fails, as SIE's does; without it the text is cut short and read silently.
        self.max_words = max_words
        self.ids: list[str] = []

    def extract(self, model: str, item: Any, **options: Any) -> Any:
        item_id, text = item["id"], item.get("text", "")
        self.ids.append(item_id)
        for prefix, errors in self.errors.items():
            if item_id.startswith(prefix) and errors:
                raise errors.pop(0)
        for prefix, reply in self.replies.items():
            if item_id.startswith(prefix):
                return reply
        if "output_schema" not in options:
            return {"entities": [{"label": "markdown", "text": self.pages[item_id]}]}
        if self.max_words is not None and len(text.split()) > self.max_words:
            if options.get("options", {}).get("overflow_policy") == "error":
                raise InputTooLongError(f"{item_id} is longer than the model reads")
            text = " ".join(text.split()[: self.max_words])
        data = stand_in_fields(text)
        for prefix, changes in self.fields.items():
            if item_id.startswith(prefix):
                for name, value in changes.items():
                    if value is None:
                        data.pop(name, None)
                    else:
                        data[name] = value
        return {"data": data}


def run(
    names: list[str],
    pages: dict[str, str] | None = None,
    rules: dict[str, Any] | None = None,
    **stand_in: Any,
) -> dict[str, recipe.Referral]:
    """Run the recipe on some of the committed packets (by short name) with the stand-in."""
    with tempfile.TemporaryDirectory() as name:
        folder = Path(name)
        for seed in ("ehr_patients.csv", "provider_directory.csv"):
            shutil.copy(DATA / seed, folder / seed)
        texts: dict[str, str] = {}
        for short in names:
            packet = PACKETS[short]
            shutil.copy(DATA / packet.filename, folder / packet.filename)
            stem = Path(packet.filename).stem
            for number in (1, 2, 3):
                texts[f"ocr/{stem}/p{number}"] = ocr_text(packet, number)
        texts.update(pages or {})
        client = StandIn(texts, **stand_in)
        with contextlib.redirect_stdout(io.StringIO()):
            referrals, _ = recipe.run(cast(SIEClient, client), rules or RULES, folder)
    return {r.packet.split("-", 2)[2].removesuffix(".pdf"): r for r in referrals}


def one(short: str, **kwargs: Any) -> recipe.Referral:
    return run([short], **kwargs)[short]


def stem(short: str) -> str:
    return Path(PACKETS[short].filename).stem


def everywhere(short: str, changes: dict[str, str]) -> dict[str, str]:
    """The stand-in OCR text of all three pages with each `old` replaced by `new`."""
    pages = {}
    for number in (1, 2, 3):
        text = ocr_text(PACKETS[short], number)
        for old, new in changes.items():
            text = text.replace(old, new)
        pages[f"ocr/{stem(short)}/p{number}"] = text
    return pages


def edited(short: str, number: int, old: str, new: str) -> dict[str, str]:
    """The stand-in OCR text of one page with `old` replaced by `new`, keyed by call id."""
    text = ocr_text(PACKETS[short], number)
    assert old in text, (old, number)
    return {f"ocr/{stem(short)}/p{number}": text.replace(old, new)}


# --- Checks -----------------------------------------------------------------------------------


def check_examples() -> None:
    got = run(list(PACKETS))
    expected = {
        "brightwater-family-medicine": ("checks_passed", "routine", []),
        "hollis-creek-internal-medicine": ("missing_info", "routine", ["member_id"]),
        "quarry-hill-medical-group": ("missing_info", "routine", ["order_signed_by"]),
        "fenwick-street-clinic": ("checks_passed", "urgent", []),
        "millbrook-community-health": ("checks_passed", "routine", []),
        "juniper-road-family-practice": ("missing_info", "time_bound", ["member_id"]),
        "cobble-lane-health-center": ("review", "routine", []),
        "orchard-bend-medical": ("review", "routine", ["member_id"]),
    }
    for short, (status, kind, missing) in expected.items():
        r = got[short]
        assert r.status == status, (short, r.status, r.review, r.missing)
        assert r.priority["kind"] == kind, (short, r.priority)
        assert [m["field"] for m in r.missing] == missing, (short, r.missing)
    # Millbrook: the member ID comes from the chart, and says so; his son's chart is noted.
    mill = got["millbrook-community-health"]
    assert mill.fields["member_id"] == {
        "value": "SHC-417730-1",
        "source": "ehr",
        "mrn": "MRN-100231",
    }
    assert mill.chart["status"] == "found"
    assert any("MRN-100232" in note for note in mill.notes), mill.notes
    # Quarry Hill: the clinic note's e-signature was read and set aside.
    quarry = got["quarry-hill-medical-group"]
    assert any("signs the note page, not the referral order" in n for n in quarry.notes)
    # Fenwick: an existing patient, everything agrees with the chart.
    assert got["fenwick-street-clinic"].chart["status"] == "found"
    assert got["fenwick-street-clinic"].priority["page"] == 2
    # Juniper Road: not in the directory, so the numbers come from the fax.
    juniper = got["juniper-road-family-practice"]
    assert juniper.provider["status"] == "none"
    assert juniper.fields["office_fax"]["source"] == "fax"
    assert juniper.priority["as_written"] == "Please see within 72 hours", juniper.priority
    # Cobble Lane: Ann Lee and Joann Lee never merge.
    cobble = got["cobble-lane-health-center"]
    assert any("Pages disagree on patient_name: 'Ann Lee'" in r for r in cobble.review)
    assert any("Pages disagree on patient_dob" in r for r in cobble.review), cobble.review
    assert cobble.chart["status"] == "not_searched"
    assert cobble.value("patient_name") == "Ann Lee"  # the order's patient, read first
    # Orchard Bend: a possible match under her maiden name; nothing filled from that chart.
    orchard = got["orchard-bend-medical"]
    assert orchard.chart["status"] == "possible_match", orchard.chart
    assert "member_id" not in orchard.fields
    assert any("MRN-100689 Helena Voss" in r for r in orchard.review), orchard.review
    # The callback list: missing_info packets only, time-bound first, with the office numbers.
    rows = recipe.callback_rows(list(got.values()), RULES)
    assert [(r["packet"].split("-", 2)[2], r["item"]) for r in rows] == [
        ("juniper-road-family-practice.pdf", "Insurance member ID"),
        ("hollis-creek-internal-medicine.pdf", "Insurance member ID"),
        ("quarry-hill-medical-group.pdf", "Signed referral order (provider signature)"),
    ], rows
    assert rows[0]["office_phone"] == "(555) 010-2750"
    assert rows[0]["contact_from"] == "fax"
    assert rows[1]["office_fax"] == "(555) 010-5521"
    assert rows[1]["contact_from"] == "directory"
    # The clinical review queue: every packet, urgent then time-bound first.
    queue = recipe.queue_rows(list(got.values()), RULES)
    assert len(queue) == 8
    assert [q["requested_priority"] for q in queue[:3]] == ["urgent", "time_bound", "routine"]
    assert queue[0]["packet"] == PACKETS["fenwick-street-clinic"].filename
    print("examples: 8 packets, statuses, sources, callback list and queue order ok")


def check_statuses() -> None:
    for old in ("ready", "callback", "needs_review"):
        assert old not in (recipe.CHECKS_PASSED, recipe.MISSING_INFO, recipe.REVIEW)
    r = recipe.Referral("x.pdf")
    assert r.status == "checks_passed"
    r.missing.append({"field": "member_id", "item": "Insurance member ID"})
    assert r.status == "missing_info"
    r.flag("Pages disagree on patient_name", "patient_name")
    assert r.status == "review"  # review wins over missing_info
    assert recipe.callback_rows([r], RULES) == []
    print("statuses: checks_passed, missing_info, review; review wins ok")


def check_labels() -> None:
    owner = recipe.label_owner
    assert owner("Patient phone", RULES) == "patient_phone"
    assert owner("Phone", RULES) == "office_phone"
    assert owner("Patient name", RULES) == "patient_name"
    assert owner("Name", RULES) == "referring_provider"
    assert owner("Rosalind Teague DOB", RULES) == "patient_dob"
    assert owner("Visit date", RULES) is None
    assert owner("Dictated by", RULES) is None
    assert owner("Electronically signed by", RULES) == "order_signed_by"
    line = "Patient: Joann Lee DOB: 02/03/1971 Visit date: 10/20/2026"
    assert recipe.on_labelled_line("patient_dob", "02/03/1971", line, RULES)
    assert not recipe.on_labelled_line("patient_dob", "10/20/2026", line, RULES)
    assert recipe.on_labelled_line("order_signed_by", "Maren Okafor", "/s/ Maren Okafor", RULES)
    assert recipe.on_labelled_line(
        "referring_provider", "Achterberg", "Name: Samuel Achterberg", RULES
    )
    phones = "Phone: (555) 010-7730 Fax: (555) 010-7731"
    assert recipe.on_labelled_line("office_fax", "(555) 010-7731", phones, RULES)
    assert not recipe.on_labelled_line("office_phone", "(555) 010-7731", phones, RULES)
    assert not recipe.on_labelled_line(
        "office_phone", "(555) 010-2296", "Patient phone: (555) 010-2296", RULES
    )
    print("labels: a value counts only after its own field's label ok")


def check_partial_evidence() -> None:
    # The insurer read as the member ID (what the base model did) is not after a member label.
    r = one(
        "hollis-creek-internal-medicine", fields={"extract/": {"member_id": "Harborline Mutual"}}
    )
    assert [m["field"] for m in r.missing] == ["member_id"], r.missing
    assert any("'Harborline Mutual' on page 2 is not after a label" in n for n in r.notes)
    # A value that isn't in the page text is not used.
    r = one("brightwater-family-medicine", fields={"extract/": {"member_id": "CPH-9999-0000"}})
    assert any("is not in the page text" in n for n in r.notes)
    assert "member_id" not in r.fields, r.fields
    assert r.status == "missing_info", r.review
    # The practice's own fax number (the cover's "To" line) is never the office's.
    r = one("brightwater-family-medicine", fields={"extract/": {"office_fax": "(555) 010-2200"}})
    assert any("practice's own number" in n for n in r.notes), r.notes
    # A signature on the clinic note never signs the order.
    unsigned = recipe.Candidate(
        "order_signed_by", "Maren Okafor, MD", 3, "Signed by: Maren Okafor, MD"
    )
    referral = recipe.Referral("x.pdf")
    kept = recipe.keep_candidates([unsigned], ["cover", "order", "note"], RULES, referral)
    assert kept == []
    assert "not the referral order" in referral.notes[0]
    # A form blank is never a value.
    blank = recipe.Candidate("member_id", "______", 2, "Member ID: ______")
    assert recipe.keep_candidates([blank], ["cover", "order", "note"], RULES, referral) == []
    print("partial evidence: unlabelled, invented, own-number and note signatures dropped ok")


def check_names() -> None:
    key = recipe.comparison_key
    assert key("patient_name", "Ann Lee", RULES) != key("patient_name", "Joann Lee", RULES)
    assert not recipe.same_value("patient_name", "ann lee", "joann lee")
    assert not recipe.same_value("patient_name", "lee", "ann lee")  # a surname alone isn't her
    assert recipe.same_value(
        "patient_name",
        key("patient_name", "LEE, ANN", RULES),
        key("patient_name", "Ann Lee", RULES),
    )
    assert recipe.same_value(
        "referring_provider",
        key("referring_provider", "Dr. Okafor", RULES),
        key("referring_provider", "Maren Okafor, MD", RULES),
    )
    # Two names in one packet: the second is flagged, never dropped.
    referral = recipe.Referral("x.pdf")
    found = [
        recipe.Candidate("patient_name", "Ann Lee", 2, "Patient name: Ann Lee"),
        recipe.Candidate("patient_name", "Joann Lee", 3, "Patient: Joann Lee"),
    ]
    recipe.merge_field("patient_name", found, RULES, referral)
    assert referral.review == ["Pages disagree on patient_name: 'Ann Lee' (p2); 'Joann Lee' (p3)"]
    print("names: whole words only, Ann Lee is not Joann Lee, conflicts listed ok")


def check_every_page_names_the_patient() -> None:
    # The note page's extraction came back empty (the empty-result 500), and the note is
    # Joann Lee's: the page doesn't name Ann Lee, so the packet goes to review.
    empty = ServerError(recipe.EMPTY_RESULT_ERROR, status_code=500)
    cobble = stem("cobble-lane-health-center")
    r = one("cobble-lane-health-center", errors={f"extract/{cobble}/p3": [empty]})
    assert any(
        "Page 3 (note) names no patient the recipe read; check it is Ann Lee's" in x
        for x in r.review
    ), r.review
    # The same empty reply on Brightwater's note: the note names the patient in its text.
    bright = stem("brightwater-family-medicine")
    r = one("brightwater-family-medicine", errors={f"extract/{bright}/p3": [empty]})
    assert r.status == "checks_passed", r.review
    assert any("no fields found" in n for n in r.notes)
    print("every page names the patient: an empty or foreign page goes to review ok")


def check_page_count() -> None:
    bright = stem("brightwater-family-medicine")
    cover = ocr_text(PACKETS["brightwater-family-medicine"], 1).replace("cover): 3", "cover): 4")
    r = one("brightwater-family-medicine", pages={f"ocr/{bright}/p1": cover})
    assert "The cover sheet says 4 pages, 3 arrived" in r.review, r.review
    print("page count: the cover's count is checked against the pages received ok")


def check_values() -> None:
    assert recipe.npi_is_valid("1999001013")
    assert not recipe.npi_is_valid("1999001014")
    assert recipe.phone_digits("(555) 010-4470") == "5550104470"
    assert recipe.phone_digits("1-555-010-4470") == "5550104470"
    assert recipe.phone_digits("010-4470") == ""
    bright = "brightwater-family-medicine"
    r = one(bright, pages=edited(bright, 2, "NPI: 1999001013", "NPI: 1999001014"))
    assert any("is not 10 digits with a valid check digit" in x for x in r.review), r.review
    r = one(bright, pages=edited(bright, 2, "birth: 03/14/1961", "birth: 03/14/2061"))
    assert any("is not plausible" in x for x in r.review), r.review
    assert r.chart["status"] == "not_searched"
    print("values: NPI check digit, phone digits, DOB plausibility ok")


def check_priority() -> None:
    classify = recipe.classify_priority
    assert classify("Priority: URGENT - please schedule within 72 hours", RULES)[0] == "urgent"
    assert classify("Priority: not urgent", RULES)[0] == "routine"
    assert classify("Priority: Non-urgent", RULES)[0] == "routine"
    assert classify("Please see within 72 hours", RULES) == ("time_bound", "within 72 hours")
    assert classify("Please see within 72h", RULES)[0] == "time_bound"
    assert classify("Status: stable", RULES)[0] == "not_stated"
    assert classify("Priority: STAT echo", RULES)[0] == "urgent"
    # The model reads "Routine" from the form, but the note asks for a time frame: the packet
    # sorts with the time-bound ones and says why.
    note = ocr_text(PACKETS["brightwater-family-medicine"], 3).replace(
        "stress testing.", "stress testing, within 2 weeks."
    )
    bright = stem("brightwater-family-medicine")
    r = one("brightwater-family-medicine", pages={f"ocr/{bright}/p3": note})
    assert r.priority["kind"] == "time_bound", r.priority
    assert r.priority["note"].startswith("The model read 'Routine' (routine)"), r.priority
    assert r.status == "checks_passed"  # a routing hint, not a status
    # The model says urgent where the form says "not urgent": sorted urgent, with the reason.
    form = ocr_text(PACKETS["brightwater-family-medicine"], 2).replace(
        "Priority: Routine", "Priority: not urgent"
    )
    r = one(
        "brightwater-family-medicine",
        pages={f"ocr/{bright}/p2": form},
        fields={"extract/": {"requested_priority": "urgent"}},
    )
    assert r.priority["kind"] == "urgent", r.priority
    assert "The model read 'urgent' (urgent)" in r.priority["note"], r.priority
    # Nothing stated anywhere: "not stated", sorted above routine.
    form = ocr_text(PACKETS["brightwater-family-medicine"], 2).replace("Priority: Routine", "")
    r = one("brightwater-family-medicine", pages={f"ocr/{bright}/p2": form})
    assert r.priority["kind"] == "not_stated", r.priority
    assert recipe.priority_rank("not_stated", RULES) < recipe.priority_rank("routine", RULES)
    print("priority: urgent, time frames, negations, disagreements sort up ok")


def check_lookups() -> None:
    ehr = EHR(DATA)
    found = ehr.find_patient("Corwin Ashdown", date(1972, 5, 30))
    assert found.status == "found"
    assert found.chart
    assert found.chart.mrn == "MRN-100231"
    assert [c.mrn for c in found.same_name_elsewhere] == ["MRN-100232"]
    assert ehr.find_patient("ASHDOWN, CORWIN", date(1972, 5, 30)).status == "found"
    # A misread DOB: two charts with this name, neither with this DOB.
    misread = ehr.find_patient("Corwin Ashdown", date(1972, 5, 31))
    assert misread.status == "several", misread
    assert misread.chart is None
    married = ehr.find_patient("Helena Marchetti", date(1979, 4, 18))
    assert married.status == "possible_match"
    assert married.chart is None
    assert "shares the name helena" in married.why, married.why
    assert ehr.find_patient("Ann Lee", date(1958, 6, 17)).status == "none"
    assert ehr.find_patient("Emerick Halvorsen", date(1949, 1, 9)).status == "possible_match"
    by_id = ehr.find_patient("Delphine Arkwright", date(1961, 3, 14), "CPH-5582-1170")
    assert by_id.status == "possible_match"
    assert "same member ID" in by_id.why
    with tempfile.TemporaryDirectory() as name:
        seed = (DATA / "ehr_patients.csv").read_text(encoding="utf-8")
        twin = "MRN-900001,Corwin,Ashdown,1972-05-30,(555) 010-0000,,\n"
        (Path(name) / "ehr_patients.csv").write_text(seed + twin, encoding="utf-8")
        assert EHR(Path(name)).find_patient("Corwin Ashdown", date(1972, 5, 30)).status == "several"
    directory = ProviderDirectory(DATA, tuple(RULES["name_credentials"]))
    assert directory.find_provider("1999001013", "Maren Okafor, MD").status == "found"
    assert directory.find_provider("", "Dr. Okafor").status == "found"
    wrong = directory.find_provider("1999001013", "Iris Vandermolen, DO")
    assert wrong.status == "possible_match"
    assert wrong.provider is None
    moved = directory.find_provider("1999008083", "Maren Okafor")
    assert moved.status == "possible_match", moved
    assert directory.find_provider("1999008083", "Tomasz Wierzbicki, MD").status == "none"
    # In a packet: the NPI belongs to someone else in the directory, so review.
    bright = "brightwater-family-medicine"
    r = one(bright, pages=edited(bright, 2, "NPI: 1999001013", "NPI: 1999002029"))
    assert any("Provider directory possible_match" in x for x in r.review), r.review
    assert r.fields["office_fax"]["source"] == "fax"
    print("lookups: found, possible_match, several and none for charts and providers ok")


def check_adversarial() -> None:
    """Stand-in replies and inputs that would give a confident wrong status if a check slipped."""
    bright, mill = "brightwater-family-medicine", "millbrook-community-health"
    # The model reads only the first name on the note page: not the same patient, so review.
    r = one(bright, fields={f"extract/{stem(bright)}/p3": {"patient_name": "Delphine"}})
    assert any("'Delphine' on page 3 is only part of" in x for x in r.review), r.review
    # The model reads the clinic note's visit date as the date of birth: not after a DOB label.
    r = one(bright, fields={f"extract/{stem(bright)}/p3": {"patient_dob": "10/20/2026"}})
    assert r.status == "checks_passed", r.review
    assert any("'10/20/2026' on page 3 is not after a label" in n for n in r.notes), r.notes
    # The model reads the patient's phone as the office's: "Patient phone" is not "Phone".
    r = one(bright, fields={f"extract/{stem(bright)}/p2": {"office_phone": "(555) 010-3381"}})
    assert any("office_phone '(555) 010-3381' on page 2 is not after" in n for n in r.notes)
    # Two member IDs, or two patients, in one span: review, never one value.
    two_ids = edited(bright, 2, "ID: CPH-4471-0923", "ID: CPH-4471-0923 / CPH-4471-0924")
    r = one(bright, pages=two_ids)
    assert any("member_id 'CPH-4471-0923 / CPH-4471-0924' doesn't look like" in x for x in r.review)
    two_names = edited(bright, 2, "name: Delphine Arkwright", "name: Delphine and Ivo Arkwright")
    r = one(bright, pages=two_names)
    assert "patient_name 'Delphine and Ivo Arkwright' doesn't look like one value" in r.review[1]
    # A glued phone number still reads as one.
    r = one(bright, pages=edited(bright, 2, "phone: (555) 010-3381", "phone:(555)010-3381"))
    assert r.fields["patient_phone"]["value"] == "(555) 010-3381", r.fields
    # The fax misprints Corwin Ashdown's DOB by a day: two charts have his name, neither this
    # DOB, so nothing is filled from either.
    dob = {
        **edited(mill, 2, "05/30/1972", "05/31/1972"),
        **edited(mill, 3, "05/30/1972", "05/31/1972"),
    }
    r = one(mill, pages=dob)
    assert r.chart["status"] == "several", r.chart
    assert "member_id" not in r.fields
    assert r.status == "review"
    # A nickname with the same DOB is a possible match, not his chart.
    nick = {
        **edited(mill, 2, "name: Corwin", "name: Corey"),
        **edited(mill, 3, ": Corwin", ": Corey"),
    }
    r = one(mill, pages=nick)
    assert r.chart["status"] == "possible_match", r.chart
    assert "member_id" not in r.fields
    # A span with no priority wording next to the form's "Routine" doesn't make it "not stated".
    r = one(bright, fields={f"extract/{stem(bright)}/p3": {"requested_priority": "six weeks"}})
    assert r.priority["kind"] == "routine", r.priority
    assert r.priority["as_written"] == "Routine", r.priority
    print("adversarial: partial names, wrong labels, two values in a span, misread DOB ok")


def check_review_fixes() -> None:
    """One case per fix from the PR review; each fails when its fix is undone."""
    bright, mill = "brightwater-family-medicine", "millbrook-community-health"
    # An over-long window is a failed call (overflow_policy "error"), never read cut short.
    r = one(bright, max_words=40)
    assert r.status == "review", (r.review, r.missing)
    assert "Page 1 window 1 was not read: the SIE call failed: InputTooLongError" in r.review[0]
    # Part of a name after its label is not the patient's or the signer's whole name.
    r = one(bright, fields={"extract/": {"patient_name": "Arkwright"}})
    assert r.status == "review", (r.review, r.missing)
    assert (
        "patient_name 'Arkwright' on page 2 is only part of what follows its label" in (r.review[0])
    ), r.review
    assert "delphine arkwright" in r.review[0], r.review
    r = one(bright, fields={f"extract/{stem(bright)}/p2": {"order_signed_by": "Okafor"}})
    assert any("order_signed_by 'Okafor' on page 2 is only part" in x for x in r.review), r.review
    whole = recipe.whole_value
    assert whole("order_signed_by", "Maren Okafor", "Signed by: Dr. Maren Okafor, MD", RULES)[0]
    line = "Electronically signed by: Maren Okafor, MD Date: 10/21/2026"
    assert whole("order_signed_by", "Maren Okafor, MD", line, RULES)[0]
    line = "Patient: Joann Lee DOB: 02/03/1971 Visit date: 10/20/2026"
    assert whole("patient_name", "Joann Lee", line, RULES)[0]
    assert not whole("patient_name", "Joann", line, RULES)[0]
    assert not whole("patient_name", "Lee", "Patient name: Ann Lee | Sex: F", RULES)[0]
    assert whole("patient_name", "Ann Lee", "Patient name: Ann Lee | Sex: F", RULES)[0]
    # No NPI, a surname alone, and the fax's practice and numbers aren't the directory's: a
    # possible match for review, nothing filled from the directory.
    elsewhere = {
        "NPI: 1999001013": "",
        "Maren Okafor, MD": "Okafor",
        "Brightwater Family Medicine": "Kestrel Point Clinic",
        "BRIGHTWATER FAMILY MEDICINE": "KESTREL POINT CLINIC",
        "(555) 010-4470": "(555) 010-4490",
        "(555) 010-4471": "(555) 010-4491",
    }
    r = one(bright, pages=everywhere(bright, elsewhere))
    assert r.provider["status"] == "possible_match", r.provider
    assert any(x.startswith("Provider directory possible_match: no NPI") for x in r.review)
    assert r.fields["office_fax"] == {"value": "(555) 010-4491", "source": "fax", "page": 2}
    assert r.fields["practice"]["source"] == "fax", r.fields["practice"]
    # The same surname with the practice and numbers agreeing: found, numbers from the directory.
    r = one(bright, pages=everywhere(bright, {"NPI: 1999001013": "", "Maren Okafor, MD": "Okafor"}))
    assert r.provider["status"] == "found", r.provider
    assert r.fields["office_fax"]["source"] == "directory"
    # A two-digit year is read in the century that puts it in the past: 1961, not 2061.
    assert recipe.parse_date("03/14/49", RULES["date_formats"]) == date(1949, 3, 14)
    assert recipe.parse_date("14 Mar 1961", RULES["date_formats"]) == date(1961, 3, 14)
    r = one(bright, pages=everywhere(bright, {"03/14/1961": "03/14/61"}))
    assert r.status == "checks_passed", r.review
    assert r.value("patient_dob") == "1961-03-14"
    # No page reads as a referral order: review.
    r = one(bright, pages=edited(bright, 2, "OUTPATIENT REFERRAL ORDER", "OUTPATIENT FORM"))
    assert any(x.startswith("No referral order page found") for x in r.review), r.review
    # on_conflict first keeps the earliest page's value without review; review lists both.
    reasons = [
        recipe.Candidate("referral_reason", "Chest pressure", 2, ""),
        recipe.Candidate("referral_reason", "Palpitations", 3, ""),
    ]
    referral = recipe.Referral("x.pdf")
    best = recipe.merge_field("referral_reason", reasons[::-1], RULES, referral)
    assert best is not None
    assert best.value == "Chest pressure"
    assert referral.review == []
    strict = {**RULES, "fields": {**RULES["fields"], "referral_reason": {"on_conflict": "review"}}}
    recipe.merge_field("referral_reason", reasons, strict, referral)
    assert referral.review[0].startswith("Pages disagree on referral_reason"), referral.review
    # A date of birth further back than max_age_years is not plausible.
    r = one(bright, pages=everywhere(bright, {"03/14/1961": "03/14/1899"}))
    assert any("Date of birth 1899-03-14 is not plausible" in x for x in r.review), r.review
    # The same date written two ways on two pages is one date of birth.
    r = one(bright, pages=edited(bright, 3, "03/14/1961", "1961-03-14"))
    assert not any("patient_dob" in x for x in r.review), r.review
    assert r.status == "checks_passed", r.review
    # The fax and the matched chart disagree on the patient's phone: the fax's is used, noted.
    r = one(mill, pages=edited(mill, 2, "phone: (555) 010-4402", "phone: (555) 010-4499"))
    assert r.fields["patient_phone"]["source"] == "fax", r.fields["patient_phone"]
    assert any(
        n.startswith("patient_phone: the fax says '(555) 010-4499', chart MRN-100231 has")
        for n in r.notes
    ), r.notes
    print("review fixes: over-long window, partial names, name-only provider, dates ok")


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
    two = ["brightwater-family-medicine", "fenwick-street-clinic"]
    bright = stem("brightwater-family-medicine")
    # A transient error on one packet's page: that packet goes to review, the other passes.
    for error in TRANSIENT:
        got = run(two, errors={f"ocr/{bright}/p2": [error]})
        assert got["brightwater-family-medicine"].status == "review", type(error).__name__
        assert got["fenwick-street-clinic"].status == "checks_passed", type(error).__name__
        reason = got["brightwater-family-medicine"].review[0]
        assert reason.startswith("Page 2 was not read: the SIE call failed: "), reason
    got = run(two, errors={f"extract/{bright}/p2": [ServerError("busy", status_code=503)]})
    assert got["brightwater-family-medicine"].review[0].startswith("Page 2 window 1 was not read")
    # A reply missing its key is a failed call on the same path; an empty one is not.
    r = one("brightwater-family-medicine", replies={f"ocr/{bright}/p1": {"something": []}})
    assert "replied without 'entities'" in r.review[0], r.review
    r = one("brightwater-family-medicine", replies={f"extract/{bright}/p2": {"no_data": {}}})
    assert "replied without 'data'" in r.review[0], r.review
    r = one("brightwater-family-medicine", replies={f"extract/{bright}/p1": {"data": {}}})
    assert r.status == "checks_passed", r.review
    r = one("brightwater-family-medicine", replies={f"ocr/{bright}/p1": {"entities": []}})
    assert r.review[0] == "Page 1: the OCR reply had no markdown text", r.review
    # SIE Serverless's empty-result 500 reads as "no fields" from the extractor only.
    empty = ServerError(recipe.EMPTY_RESULT_ERROR, status_code=500)
    r = one("brightwater-family-medicine", errors={f"extract/{bright}/p1": [empty]})
    assert r.status == "checks_passed"
    assert any("no fields found" in n for n in r.notes)
    r = one("brightwater-family-medicine", errors={f"ocr/{bright}/p1": [empty]})
    assert "the SIE call failed: ServerError" in r.review[0], r.review
    # Every call failed: the run stops, with the full message.
    long = "connection refused " + "x" * 300
    dead = {"": [SIEConnectionError(long) for _ in range(20)]}
    message = stopped(lambda: run(["brightwater-family-medicine"], errors=dead))
    assert message.startswith("Every SIE call failed (3 of 3 to lightonai/LightOnOCR-3-4B)"), (
        message
    )
    assert long in message
    assert "Check SIE_BASE_URL and your network." in message, message
    busy = {"": [ModelLoadingError("loading") for _ in range(20)]}
    message = stopped(lambda: run(["brightwater-family-medicine"], errors=busy))
    assert "may still be starting or SIE is busy; run again in a few minutes." in message
    # A refusal stops the run at once, with the full message, HTTP status and code.
    refusal = {f"ocr/{bright}/p2": [InsufficientCreditsError("no credits " + "y" * 300)]}
    message = stopped(lambda: run(["brightwater-family-medicine"], errors=refusal))
    assert "InsufficientCreditsError (HTTP 402, code INSUFFICIENT_CREDITS)" in message, message
    assert "y" * 300 in message
    assert "SIE_API_KEY" in message
    gated = {"": [ModelLoadFailedError("gated", permanent=True)]}
    message = stopped(lambda: run(["brightwater-family-medicine"], errors=gated))
    assert message.startswith("SIE can't load lightonai/LightOnOCR-3-4B"), message
    unparsed = {"": [RequestError("unexpected body")]}
    message = stopped(lambda: run(["brightwater-family-medicine"], errors=unparsed))
    assert "SIE_API_KEY" not in message, message
    assert "SIE_BASE_URL" in message, message
    # A bug in our own code is raised as is; main turns only SIEStopped into a clean exit.
    try:
        run(["brightwater-family-medicine"], errors={"": [TypeError("bad argument")]})
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


def check_csv_safe() -> None:
    assert recipe.csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert recipe.csv_safe("+1 555 010 4470") == "'+1 555 010 4470"
    assert recipe.csv_safe("(555) 010-4470") == "(555) 010-4470"
    r = recipe.Referral("x.pdf")
    r.fields["patient_name"] = {"value": "=cmd()", "source": "fax", "page": 2}
    r.priority = {"kind": "routine", "as_written": "", "line": "", "page": None, "note": ""}
    with tempfile.TemporaryDirectory() as name:
        recipe.write_outputs(Path(name), [r], RULES)
        row = (Path(name) / "clinical-review-queue.csv").read_text(encoding="utf-8")
        assert ",x.pdf,'=cmd()," in row, row
    print("csv_safe: formula-looking cells are quoted ok")


def check_committed_data() -> None:
    pdfs = sorted(DATA.glob("*.pdf"))
    assert [p.name for p in pdfs] == sorted(p.filename for p in make_data.PACKETS)
    for pdf in pdfs:
        raw = pdf.read_bytes()
        for marker in (b"/Title", b"/Author", b"/CreationDate", b"Exif", b"xmpmeta"):
            assert marker not in raw, f"{pdf.name} carries {marker!r}"
    for packet in make_data.PACKETS:
        assert recipe.npi_is_valid(packet.npi), packet.npi
    directory = ProviderDirectory(DATA)
    known = [p for p in make_data.PACKETS if directory.find_provider(p.npi, p.provider).provider]
    assert len(known) == 7, [p.clinic for p in known]
    assert set(RULES["required"]) <= set(RULES["fields"])
    print("committed data: 8 packets, no PDF metadata, valid NPIs, seeds load ok")


if __name__ == "__main__":
    check_committed_data()
    check_statuses()
    check_labels()
    check_partial_evidence()
    check_names()
    check_values()
    check_priority()
    check_lookups()
    check_every_page_names_the_patient()
    check_page_count()
    check_adversarial()
    check_review_fixes()
    check_examples()
    check_sie_failure()
    check_csv_safe()
    print("all checks passed")
