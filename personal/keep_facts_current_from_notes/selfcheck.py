"""Check the recipe's reading, value checks, update rule, calendar and unread-file rules offline.

The checks use hand-written strings, the committed data/ and hand-written stand-in model
replies. Nothing here calls SIE or replays a recorded response. Run it with
`uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import re
import shutil
import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, cast

from calendar_app import Calendar
from PIL import Image
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
from voice_memos import VoiceMemos

import recipe

RULES = recipe.load_rules()
FACTS = {fact.id: fact for fact in recipe.load_facts()}
DATA = recipe.DATA_DIR


# --- Hand-written model output, in each model's shape -----------------------------------------

BOARDING_PASS = (
    "BLUEFINCH AIR\n\nBOARDING PASS\n\n<table>"
    "<tr><th>PASSENGER</th><th>FLIGHT</th><th>DATE</th></tr>"
    "<tr><td>ELLERY / JORDAN</td><td>BF 2217</td><td>20 OCT 2026</td></tr>"
    "<tr><th>BOARDING</th><th>GATE</th><th>SEAT</th><th>ZONE</th></tr>"
    "<tr><td>07:40</td><td>B12</td><td>14C</td><td>3</td></tr></table>"
)
LEASE_P1 = (
    "# RESIDENTIAL LEASE AGREEMENT\n\nThis lease is made on July 15, 2026 between Cobblestone "
    "Row Properties LLC (Landlord) and Jordan Ellery (Tenant).\n\n## 2. Rent\n\nMonthly rent "
    "is $2,150, due on the first day of each month."
)
LEASE_P2 = (
    "## 6. Notice to Vacate\n\nTenant must give Landlord at least sixty (60) days' written "
    "notice before moving out, whether at the end of the term or after it."
)
PAGES = {
    "ocr/boarding-pass-2026-10-20": BOARDING_PASS,
    "ocr/lease-2026-07-15/p1": LEASE_P1,
    "ocr/lease-2026-07-15/p2": LEASE_P2,
}
SEGMENTS: dict[str, list[dict[str, Any]]] = {
    "transcribe/memo-2026-10-12": [
        {"text": "Car update.", "start": 0.0, "end": 1.2},
        {"text": "They quoted $420 for new front brake pads and rotors.", "start": 1.5, "end": 5.0},
    ],
    "transcribe/memo-2026-10-27": [],  # wind only
}
EYE_MEMO = "memo-2026-10-05@s1"
BRAKE_MEMO = "memo-2026-10-12@00:00"
PASS = "boarding-pass-2026-10-20#b1"
LEASE = "lease-2026-07-15#p2.1"

# Which chunks the stand-in retriever and reranker find for each fact, with rerank scores.
RELEVANT: dict[str, dict[str, float]] = {
    "furnace_filter": {"notes#L9": 0.99},
    "brake_price": {BRAKE_MEMO: 0.98, "notes#L45": 0.4},
    "denver_seat": {PASS: 0.94},
    "move_out_notice": {LEASE: 0.99},
    "denver_trip": {"notes#L57": 0.99, PASS: 0.9},
    "eye_exam": {EYE_MEMO: 0.94, "notes#L49": 0.9},
    "wifi_password": {"notes#L5": 0.99, "notes#L53": 0.99},
    "blood_type": {},
    "registration_cost": {"notes#L61": 0.38},
    "plumber_visit": {},
}


def source(chunk_id: str, words: str, role: str = "states") -> dict[str, str]:
    return {"chunk_id": chunk_id, "words": words, "role": role}


def reply(
    status: str,
    value: str = "",
    sources: list[dict[str, str]] | None = None,
    previous: str = "",
    day: str = "",
    at: str = "",
) -> dict[str, Any]:
    return {
        "status": status,
        "value": value,
        "previous_value": previous,
        "date": day,
        "time": at,
        "sources": sources or [],
    }


# What a well-behaved model would answer for each fact the reranker lets through.
GOOD: dict[str, dict[str, Any]] = {
    "furnace_filter": reply(
        "found",
        "16x25x1, MERV 11",
        [source("notes#L9", "Furnace filter size is 16x25x1, MERV 11.")],
    ),
    "brake_price": reply("found", "$420", [source(BRAKE_MEMO, "They quoted $420")]),
    "denver_seat": reply("found", "14C", [source(PASS, "SEAT: 14C")]),
    "move_out_notice": reply(
        "found", "sixty (60) days", [source(LEASE, "at least sixty (60) days' written notice")]
    ),
    "denver_trip": reply(
        "found",
        "BF 2217, Larkspur Hotel",
        [source(PASS, "FLIGHT: BF 2217"), source("notes#L57", "Booked the Larkspur Hotel")],
    ),
    "eye_exam": reply(
        "updated",
        "Monday, October 26th, at 2:15 in the afternoon",
        [
            source(EYE_MEMO, "My eye exam with Doctor Haldane is moved to Monday, October 26th"),
            source(EYE_MEMO, "at 2:15 in the afternoon"),
            source("notes#L49", "Thursday, October 22 at 9:30 am", "replaced"),
        ],
        "Thursday, October 22 at 9:30 am",
        "2026-10-26",
        "14:15",
    ),
    "wifi_password": reply(
        "updated",
        "juniper-lantern-77",
        [
            source("notes#L53", "The wifi password is now juniper-lantern-77"),
            source("notes#L5", "password copper-kettle-42", "replaced"),
        ],
        "copper-kettle-42",
    ),
    "registration_cost": reply("not_found"),
}


# One slot per chunk id, shared by every stand-in: a cached run embeds no chunks, and its query
# vector must still point at the slots the first run gave them.
SLOTS: dict[str, int] = {}
DIMENSIONS = 512


class StandIn:
    """Stands in for SIEClient. Transcription and OCR return the hand-written segments and pages
    above by item id. Chunk vectors are one-hot per chunk; a fact's question vector points at
    its RELEVANT chunks, and the reranker returns their scores (0.01 for the rest). The answer
    model returns `answers[fact id]`. `errors` maps an item id prefix ("fact/eye_exam",
    "ocr/lease", "" for every call) to the errors its calls raise, one per call; `replies` maps
    a prefix to a raw reply that replaces the normal one."""

    def __init__(
        self,
        answers: dict[str, dict[str, Any] | str] | None = None,
        relevant: dict[str, dict[str, float]] | None = None,
        errors: dict[str, list[Exception]] | None = None,
        replies: dict[str, Any] | None = None,
        pages: dict[str, str] | None = None,
    ) -> None:
        self.answers = {**GOOD, **(answers or {})}
        self.relevant = relevant or RELEVANT
        self.errors, self.replies = errors or {}, replies or {}
        self.pages = pages or PAGES
        self.slots = SLOTS
        self.ids: list[str] = []

    def _before(self, item_id: str) -> Any:
        self.ids.append(item_id)
        for prefix, errors in self.errors.items():
            if item_id.startswith(prefix) and errors:
                raise errors.pop(0)
        for prefix, raw in self.replies.items():
            if item_id.startswith(prefix):
                return raw
        return None

    def extract(self, model: str, item: Any, **options: Any) -> Any:
        if (raw := self._before(item["id"])) is not None:
            return raw
        if "audio" in item:
            return {"data": {"segments": copy.deepcopy(SEGMENTS[item["id"]])}}
        return {"entities": [{"label": "markdown", "text": self.pages[item["id"]]}]}

    def _vector(self, slots: list[int]) -> list[float]:
        vector = [0.001] * DIMENSIONS
        for slot in slots:
            vector[slot] = 1.0
        return vector

    def encode(self, model: str, items: Any, **options: Any) -> Any:
        if (raw := self._before(items[0]["id"])) is not None:
            return raw
        if options.get("is_query"):
            fact = items[0]["id"].removeprefix("fact/")
            wanted = [self.slots[c] for c in self.relevant.get(fact, {}) if c in self.slots]
            return [{"dense": self._vector(wanted)}]
        out = []
        for item in items:
            self.slots.setdefault(item["id"], len(self.slots))
            assert self.slots[item["id"]] < DIMENSIONS, len(self.slots)
            out.append({"dense": self._vector([self.slots[item["id"]]])})
        return out

    def score(self, model: str, query: Any, items: list[Any], **options: Any) -> Any:
        if (raw := self._before(query["id"] + "/score")) is not None:
            return raw
        scores = self.relevant.get(query["id"].removeprefix("fact/"), {})
        return {
            "scores": [
                {"item_id": item["id"], "score": scores.get(item["id"], 0.01)} for item in items
            ]
        }

    def chat_completions(self, model: str, messages: list[Any], **options: Any) -> Any:
        prompt = messages[0]["content"]
        name = re.search(r"^Fact: (.+)$", prompt, re.MULTILINE)
        fact = next(f.id for f in FACTS.values() if name and f.name == name.group(1))
        if (raw := self._before(f"fact/{fact}/chat")) is not None:
            return raw
        answer = self.answers.get(fact, reply("not_found"))
        content = answer if isinstance(answer, str) else json.dumps(answer)
        return {"choices": [{"message": {"content": content}}]}


def run(
    facts: list[str] | None = None,
    data: Path = DATA,
    cache: Path | None = None,
    rules: dict[str, Any] | None = None,
    **stand_in: Any,
) -> tuple[recipe.Index, dict[str, dict[str, Any]], recipe.Calls, StandIn]:
    client = StandIn(**stand_in)
    chosen = [FACTS[name] for name in facts or FACTS]
    with contextlib.redirect_stdout(io.StringIO()):
        index, lines, calls = recipe.run(
            cast(SIEClient, client), rules or RULES, chosen, data, cache
        )
    return index, {line["id"]: line for line in lines}, calls, client


def one(fact: str, answer: dict[str, Any] | str, **stand_in: Any) -> dict[str, Any]:
    """The checked line for one fact, given the model's reply for it."""
    return run([fact], answers={fact: answer}, **stand_in)[1][fact]


# --- Checks -----------------------------------------------------------------------------------


def check_committed_data() -> None:
    names = sorted(p.name for p in recipe.source_files(DATA, RULES))
    assert names == [
        "boarding-pass-2026-10-20.png",
        "lease-2026-07-15.pdf",
        "memo-2026-10-05.wav",
        "memo-2026-10-12.wav",
        "memo-2026-10-27.wav",
        "notes.md",
    ], names
    notes = (DATA / "notes.md").read_text(encoding="utf-8")
    for planted in (
        "16x25x1, MERV 11",
        "October 22 at 9:30 am",
        "password copper-kettle-42",
        "The wifi password is now juniper-lantern-77",
        "Car registration renewal is due by November 30",
    ):
        assert planted in notes, planted
    for absent in ("blood", "plumber", "ELLERY-HOME", "dentist", "$420"):
        assert absent.casefold() not in notes.casefold(), absent
    with Image.open(DATA / "boarding-pass-2026-10-20.png") as image:
        assert not image.getexif(), image.info
        assert "xmp" not in str(image.info).casefold(), image.info
    raw = (DATA / "lease-2026-07-15.pdf").read_bytes()
    for marker in (b"/Author", b"Exif", b"xmpmeta", b"/Font"):  # no text layer, no metadata
        assert marker not in raw, marker
    assert len(FACTS) == 10, FACTS
    print("committed data: six sources, planted lines, no metadata, ten facts ok")


def check_lookups() -> None:
    calendar = Calendar()
    events = calendar.find_events("eye exam")
    assert [(e.event_id, e.when()) for e in events] == [("evt-102", "2026-10-22 09:30")], events
    # Whole words only: "eye" doesn't find "Eye drops", "exam" alone doesn't find "examine".
    assert calendar.find_events("physical")[0].title == "Annual physical, Dr. Mensah"
    assert calendar.find_events("dentist") == []
    assert calendar.find_events("") == []
    memos = VoiceMemos()
    eye = memos.find_recording("memo-2026-10-05.wav")
    assert eye, eye
    assert "moved to Monday, October 26th" in eye.transcript, eye
    brake = memos.find_recording("memo-2026-10-12.wav")
    assert brake, brake
    assert brake.transcript == "", brake
    assert brake.title == "Ridgeline Auto", brake
    assert memos.find_recording("memo-2026-10-05") is None  # exact file name only
    with tempfile.TemporaryDirectory() as name:
        assert Calendar(Path(name) / "calendar.csv").find_events("eye exam") == []
        assert VoiceMemos(Path(name) / "voice_memos.csv").find_recording("x.wav") is None
    print("lookups: calendar finds one eye exam by whole words; memo app by exact file ok")


def check_parsing() -> None:
    pieces = recipe.paragraphs(BOARDING_PASS)
    assert "PASSENGER: ELLERY / JORDAN; FLIGHT: BF 2217; DATE: 20 OCT 2026" in pieces[-1], pieces
    assert "SEAT: 14C" in pieces[-1], pieces
    # Glued and signed numbers stay one token; ordinals become the number.
    assert recipe.tokens("$2,150 and 26th, 16x25x1, -15 C, 3.5h") == [
        "2150", "and", "26", "16x25x1", "15", "c", "3.5h",
    ]  # fmt: skip
    assert recipe.times_in("at 2:15 in the afternoon") == {"14:15"}
    assert recipe.times_in("9:30am sharp") == {"09:30"}
    assert recipe.times_in("2 pm") == {"14:00"}
    assert recipe.times_in("unit 4B, 1187 Fennimore") == set()  # a bare number is no time
    assert recipe.times_in("at 8:00") == {"08:00", "20:00"}  # no am or pm: either
    assert recipe.day_in_words(date(2026, 10, 26), "moved to Monday, October 26th")
    assert recipe.day_in_words(date(2026, 10, 26), "on 26 Oct")
    assert not recipe.day_in_words(date(2026, 10, 26), "October 22 at 9:30, room 26")
    assert not recipe.day_in_words(date(2026, 10, 2), "October 22")
    print("parsing: OCR tables flattened; glued numbers, times and days read ok")


def check_planted_cases() -> None:
    index, lines, calls, _ = run()
    got = {fact: line["status"] for fact, line in lines.items()}
    assert got == {
        "furnace_filter": "found",
        "brake_price": "found",
        "denver_seat": "found",
        "move_out_notice": "found",
        "denver_trip": "found",
        "eye_exam": "conflict",
        "wifi_password": "updated",
        "blood_type": "not_found",
        "registration_cost": "not_found",
        "plumber_visit": "needs_review",
    }, got
    eye = lines["eye_exam"]
    assert "the calendar still says Thu Oct 22 at 9:30 AM" in eye["reason"], eye["reason"]
    assert "the 2026-10-05 voice memo says Mon Oct 26 at 2:15 PM" in eye["reason"], eye["reason"]
    assert eye["action"] == "Check which is right, then fix the calendar.", eye
    assert lines["wifi_password"]["previous_value"] == "copper-kettle-42"
    assert "no language model call" in lines["blood_type"]["reason"]
    assert "memo-2026-10-27.wav ('Plumber, kitchen sink')" in lines["plumber_visit"]["reason"]
    assert [(u.file, u.reason) for u in index.unread] == [
        ("memo-2026-10-27.wav", "the transcription came back empty")
    ], index.unread
    # The eye memo comes from the app's transcript, so only two memos go to the speech model.
    assert calls.count[RULES["models"]["transcribe"]] == 2, calls.count
    assert calls.count[RULES["models"]["answer"]] == 8, calls.count  # blood and plumber: none
    assert lines["denver_seat"]["note"].startswith("As printed on the paper pass")
    print("planted cases: the ten facts come out as the README says ok")


def check_value_supported() -> None:
    # The right passage, the wrong number: "$240" against "They quoted $420".
    line = one("brake_price", reply("found", "$240", [source(BRAKE_MEMO, "They quoted $420")]))
    assert line["status"] == "needs_review", line
    assert "240" in line["reason"], line
    # Words stitched from two places, or rephrased, are not in the passage.
    stitched = source("notes#L53", "The wifi password is juniper-lantern-77")
    line = one("wifi_password", reply("found", "juniper-lantern-77", [stitched]))
    assert line["status"] == "needs_review", line
    assert "not in that passage" in line["reason"], line
    # A passage it was never shown.
    line = one("furnace_filter", reply("found", "16x25x1", [source("notes#L33", "8:00 am")]))
    assert line["status"] == "needs_review", line
    assert "not shown" in line["reason"], line
    # A value with a word the source doesn't have ("Larkspur Inn" for "Larkspur Hotel").
    hotel = source("notes#L57", "Booked the Larkspur Hotel")
    line = one("denver_trip", reply("found", "BF 2217, Larkspur Inn", [hotel]))
    assert line["status"] == "needs_review", line
    assert "2217, inn" in line["reason"], line
    # Words that run over the limit are not a citation.
    long = source(LEASE, "Tenant must give Landlord at least sixty (60) days'")
    rules = copy.deepcopy(RULES)
    rules["answer"]["max_source_words"] = 3
    answers = {"move_out_notice": reply("found", "sixty (60) days", [long])}
    line = run(["move_out_notice"], rules=rules, answers=answers)[1]["move_out_notice"]
    assert line["status"] == "needs_review", line
    assert "run over the limit" in line["reason"], line
    # An empty value, or "found" with no source.
    assert one("furnace_filter", reply("found", "", [source("notes#L9", "16x25x1")]))["status"] == (
        "needs_review"
    )
    assert one("furnace_filter", reply("found", "16x25x1"))["status"] == "needs_review"
    print("value check: wrong number, stitched words, unseen passage, extra words to review ok")


def check_kinds() -> None:
    # The registration near miss: the topic matches, the fact isn't there.
    due = source("notes#L61", "Car registration renewal is due by November 30")
    line = one("registration_cost", reply("found", "due by November 30", [due]))
    assert line["status"] == "needs_review", line
    assert "needs an amount" in line["reason"], line
    # A date the words don't name, or a time they don't give.
    words = source("notes#L49", "Thursday, October 22 at 9:30 am")
    wrong_day = reply("found", "October 22", [words], day="2026-10-23", at="09:30")
    assert "The day 2026-10-23" in one("eye_exam", wrong_day)["reason"]
    wrong_time = reply("found", "October 22", [words], day="2026-10-22", at="21:30")
    assert "The time 21:30" in one("eye_exam", wrong_time)["reason"]
    no_day = reply("found", "October 22", [words])
    assert one("eye_exam", no_day)["status"] == "needs_review"
    print("kinds: an amount needs money, a date needs the day and time in the words ok")


def check_updates() -> None:
    new = source("notes#L53", "The wifi password is now juniper-lantern-77")
    old = source("notes#L5", "password copper-kettle-42", "replaced")
    assert one("wifi_password", reply("updated", "juniper-lantern-77", [new, old]))["status"] == (
        "updated"
    )
    # No update phrase in the newer source's own words: two values are a conflict.
    plain = source("notes#L53", "juniper-lantern-77")
    line = one("wifi_password", reply("updated", "juniper-lantern-77", [plain, old]))
    assert line["status"] == "conflict", line
    assert "don't say it replaces" in line["reason"], line
    assert line["value"] == "", line
    assert {s["role"] for s in line["sources"]} == {"disagrees"}, line
    # The labels are swapped: the source holding "is now" is the older one.
    backwards = reply(
        "updated",
        "copper-kettle-42",
        [
            source("notes#L5", "password copper-kettle-42"),
            source("notes#L53", "is now", "replaced"),
        ],
    )
    line = one("wifi_password", backwards)
    assert line["status"] == "conflict", line
    # It says "is now", but the passage it replaces is dated after it.
    later_memo = {**RELEVANT, "wifi_password": {"notes#L53": 0.99, EYE_MEMO: 0.5}}
    stale = reply(
        "updated", "juniper-lantern-77", [new, source(EYE_MEMO, "Note to self.", "replaced")]
    )
    line = one("wifi_password", stale, relevant=later_memo)
    assert line["status"] == "conflict", line
    assert "not dated later" in line["reason"], line
    # A negated update phrase doesn't count.
    assert not recipe.has_update_phrase("the exam is not moved to Monday", RULES)
    assert not recipe.has_update_phrase("it was never changed to the new one", RULES)
    assert recipe.has_update_phrase("is moved to Monday, October 26th", RULES)
    # "Updated" with nothing it replaces among the passages read is a found value.
    line = one("wifi_password", reply("updated", "juniper-lantern-77", [new]))
    assert line["status"] == "found", line
    assert "no passage read holds" in line["reason"], line
    # The earlier value must be in the replaced source's words too.
    line = one(
        "wifi_password", reply("updated", "juniper-lantern-77", [new, old], "copper-kettle-24")
    )
    assert line["status"] == "needs_review", line
    # A conflict needs two passages.
    assert one("wifi_password", reply("conflict", "", [new]))["status"] == "needs_review"
    print("updates: phrase, later date and both values checked; else conflict ok")


def check_calendar() -> None:
    memo = source(EYE_MEMO, "moved to Monday, October 26th, at 2:15 in the afternoon")
    note = source("notes#L49", "Thursday, October 22 at 9:30 am")
    # With only the note in the files, the value agrees with the calendar.
    line = one(
        "eye_exam",
        reply("found", "Thursday, October 22 at 9:30 am", [note], day="2026-10-22", at="09:30"),
        relevant={**RELEVANT, "eye_exam": {"notes#L49": 0.9}},
    )
    assert line["status"] == "found", line
    assert "The calendar agrees" in line["reason"], line
    # The memo alone disagrees: a conflict even with no second source in the files.
    line = one(
        "eye_exam", reply("found", "Monday, October 26th", [memo], day="2026-10-26", at="14:15")
    )
    assert line["status"] == "conflict", line
    # Two calendar events match: which one is this is a question for a person.
    with tempfile.TemporaryDirectory() as name:
        folder = copy_data(Path(name))
        with (folder / "calendar.csv").open("a", encoding="utf-8") as handle:
            handle.write("evt-106,Eye exam (Leo),2026-10-29T15:00,2026-10-29T15:30,Clearview\n")
            handle.write("evt-107,Eye drops pickup,2026-10-23T12:00,2026-10-23T12:10,\n")
        line = run(["eye_exam"], data=folder)[1]["eye_exam"]
        assert line["status"] == "needs_review", line
        assert "2 calendar events" in line["reason"], line
        # No matching event: the value stands, and adding it is on the list.
        (folder / "calendar.csv").write_text(
            "event_id,title,start,end,location\n", encoding="utf-8"
        )
        line = run(["eye_exam"], data=folder)[1]["eye_exam"]
        assert line["status"] == "updated", line
        assert line["action"] == "Not on the calendar ('eye exam'): add it.", line
    print("calendar: agreeing stays, disagreeing is a conflict, two matches to review ok")


def copy_data(folder: Path) -> Path:
    for path in DATA.iterdir():
        shutil.copy(path, folder / path.name)
    return folder


def check_unread() -> None:
    plumber = recipe.Unread("memo-2026-10-27.wav", "Plumber, kitchen sink", "came back empty")
    assert recipe.may_hold(plumber, FACTS["plumber_visit"], RULES)
    assert not recipe.may_hold(plumber, FACTS["blood_type"], RULES)
    for title in ("New Recording 3", "memo-2026-10-27.wav", "IMG_2041.jpg"):
        untitled = recipe.Unread("x", title, "came back empty")
        assert recipe.may_hold(untitled, FACTS["blood_type"], RULES), title
    # A found value stays found unless the unread file may hold it; then it goes to review.
    line = {"status": "found", "reason": "ok."}
    assert recipe.check_unread(dict(line), FACTS["wifi_password"], [plumber], RULES) == line
    wifi = recipe.Unread("memo-2026-11-01.wav", "Router reset", "the SIE call failed: x")
    assert recipe.check_unread(dict(line), FACTS["wifi_password"], [wifi], RULES)["status"] == (
        "needs_review"
    )
    # A scan page that reads empty is unread, and the other page still counts.
    pages = {**PAGES, "ocr/lease-2026-07-15/p1": "  "}
    index, lines, _, _ = run(["move_out_notice"], pages=pages)
    assert ("lease-2026-07-15.pdf", "page 1 read empty") in [
        (u.file, u.reason) for u in index.unread
    ]
    assert lines["move_out_notice"]["status"] == "needs_review", lines  # title "lease" matches
    # An image that reads empty.
    pages = {**PAGES, "ocr/boarding-pass-2026-10-20": "\n\n"}
    index, lines, _, _ = run(["denver_seat"], pages=pages)
    assert index.unread[0].reason == "the text read came back empty", index.unread
    assert lines["denver_seat"]["status"] == "needs_review", lines
    print("unread: a file that may hold the fact sends it to review, and only that fact ok")


def check_cut_off() -> None:
    """A passage past the top 4 that scores over the threshold is still read."""
    ranked = [(chunk(f"c{n}"), score) for n, score in enumerate([0.9, 0.8, 0.7, 0.6, 0.5, 0.1])]
    assert [c.chunk_id for c in recipe.passages_to_read(ranked, RULES)] == [
        "c0", "c1", "c2", "c3", "c4",
    ]  # fmt: skip
    low = [(chunk(f"c{n}"), 0.31 if n == 0 else 0.05) for n in range(6)]
    assert len(recipe.passages_to_read(low, RULES)) == 4
    print("cut-off: a contrary passage past the top 4 is read when it scores ok")


def chunk(chunk_id: str) -> recipe.Chunk:
    return recipe.Chunk(chunk_id, "notes.md", "notes", "line 1", "2026-01-01", "text")


def check_later_change() -> None:
    """The model cites the old note and skips the newer memo it was shown: review, not found."""
    note = source("notes#L49", "Thursday, October 22 at 9:30 am")
    stale = reply("found", "Thursday, October 22 at 9:30 am", [note], day="2026-10-22", at="09:30")
    line = one("eye_exam", stale)
    assert line["status"] == "needs_review", line
    assert (
        "memo-2026-10-05.wav transcript sentences 1-4, dated 2026-10-05, is later"
        in (line["reason"])
    ), line["reason"]
    old = reply("found", "copper-kettle-42", [source("notes#L5", "password copper-kettle-42")])
    assert one("wifi_password", old)["status"] == "needs_review"
    # A later passage that scores under the threshold is not a reason.
    low = {**RELEVANT, "wifi_password": {"notes#L5": 0.99, "notes#L53": 0.2}}
    assert one("wifi_password", old, relevant=low)["status"] == "found"
    # A date value whose words give a time needs the time too.
    no_time = reply("found", "October 22", [note], day="2026-10-22")
    assert "give a time, and the reply has none" in one("eye_exam", no_time)["reason"]
    print("later change: a skipped newer passage that says it changed sends the value to review ok")


def check_negation_and_not_found() -> None:
    # "Not found" from the model is not_found; the value says nothing was written down.
    line = one("registration_cost", reply("not_found"))
    assert line["status"] == "not_found", line
    assert line["value"] == recipe.NOT_FOUND, line
    assert line["action"] == "Write it down if you know it."
    # A malformed reply is a review, not a value.
    line = one("furnace_filter", "{not json")
    assert line["status"] == "needs_review", line
    assert "malformed" in line["reason"], line
    line = one("furnace_filter", {"status": "maybe", "value": "x", "sources": []})
    assert line["status"] == "needs_review", line
    print("not found and malformed: no value is filled in ok")


def check_review_fixes() -> None:
    """Confident statuses the value, date, update and calendar checks must not give."""
    # Every word of a text fact is checked: a one-word invented name, a lowercase password, a
    # lowercase word swapped for its opposite, a number word.
    hotel = source("notes#L57", "Booked the Larkspur Hotel")
    assert one("denver_trip", reply("found", "Hilton", [hotel]))["status"] == "needs_review"
    pw = source("notes#L53", "The wifi password is now juniper-lantern-77")
    assert one("wifi_password", reply("found", "bluehouse", [pw]))["status"] == "needs_review"
    assert recipe.unsupported_words("negative", "blood type O positive", [], True) == ["negative"]
    assert recipe.unsupported_words("fifty days", "sixty (60) days", []) == ["fifty"]
    assert recipe.unsupported_words("sixty days", "sixty (60) days", [], True) == []
    # "found" that cites a passage as replaced or disagreeing is not a plain found.
    old = source("notes#L5", "password copper-kettle-42", "replaced")
    line = one("wifi_password", reply("found", "juniper-lantern-77", [pw, old]))
    assert line["status"] == "needs_review", line
    assert "replaced or disagreeing" in line["reason"], line
    # A date in a year neither the words nor their source's date give.
    note = source("notes#L49", "Thursday, October 22 at 9:30 am")
    next_year = reply("found", "October 22", [note], day="2027-10-22", at="09:30")
    line = one("eye_exam", next_year, relevant={**RELEVANT, "eye_exam": {"notes#L49": 0.9}})
    assert line["status"] == "needs_review", line
    assert "The year 2027" in line["reason"], line
    # A day in the basic format "20261022" is the same day as the calendar's, not a conflict.
    basic = reply("found", "October 22", [note], day="20261022", at="09:30")
    line = one("eye_exam", basic, relevant={**RELEVANT, "eye_exam": {"notes#L49": 0.9}})
    assert line["status"] == "found", line
    assert line["date"] == "2026-10-22", line
    # The calendar has the same day at another time: a conflict.
    memo = source(EYE_MEMO, "My eye exam with Doctor Haldane is moved to Monday, October 26th")
    with tempfile.TemporaryDirectory() as name:
        folder = copy_data(Path(name))
        calendar = (folder / "calendar.csv").read_text(encoding="utf-8")
        (folder / "calendar.csv").write_text(
            calendar.replace(
                "2026-10-22T09:30,2026-10-22T10:15", "2026-10-26T09:30,2026-10-26T10:15"
            ),
            encoding="utf-8",
        )
        moved = reply(
            "updated",
            "Monday, October 26th, at 2:15 in the afternoon",
            [memo, source(EYE_MEMO, "at 2:15 in the afternoon"), note | {"role": "replaced"}],
            "Thursday, October 22 at 9:30 am",
            "2026-10-26",
            "14:15",
        )
        line = run(["eye_exam"], data=folder, answers={"eye_exam": moved})[1]["eye_exam"]
        assert line["status"] == "conflict", line
        assert "the calendar still says Mon Oct 26 at 9:30 AM" in line["reason"], line
    # An update dated the same day as what it replaces is not later: a conflict.
    same_day = [
        {"role": "states", "words": "is now b", "date": "2026-09-20", "source": "notes.md"},
        {"role": "replaced", "words": "a", "date": "2026-09-20", "source": "notes.md"},
    ]
    assert recipe.check_update("b", "a", copy.deepcopy(same_day), {}, RULES)["status"] == "conflict"
    # An undated source: which one is newer can't be told, so it goes to review.
    undated = [dict(same_day[0]), same_day[1] | {"date": ""}]
    line = recipe.check_update("b", "a", undated, {}, RULES)
    assert line["status"] == "needs_review", line
    assert "no date" in line["reason"], line
    found = recipe.line_result("found", "a", sources=[same_day[1] | {"chunk_id": "x"}], reason="")
    later = recipe.Chunk("y", "notes.md", "notes", "line 1", None, "the wifi password is now b")
    assert recipe.check_later_change(found, [(later, 0.9)], RULES)["status"] == "needs_review"
    # An update read from a photo (a cafe's "the wifi password is now" sign) goes to review.
    pages = {
        **PAGES,
        "ocr/boarding-pass-2026-10-20": "Corner Cafe\n\nThe wifi password is now bluefinch-9",
    }
    sign = reply(
        "updated",
        "bluefinch-9",
        [source(PASS, "The wifi password is now bluefinch-9"), old],
        "copper-kettle-42",
    )
    relevant = {**RELEVANT, "wifi_password": {PASS: 0.99, "notes#L5": 0.99}}
    line = one("wifi_password", sign, pages=pages, relevant=relevant)
    assert line["status"] == "needs_review", line
    assert "That source is a photo" in line["reason"], line
    # Not found says what the recipe knows: no reliable match in what it read.
    assert recipe.NOT_FOUND == "No reliable match in the passages read; check the source."
    print("review fixes: every word, years, basic dates, undated and photo updates to review ok")


def check_cache() -> None:
    with tempfile.TemporaryDirectory() as name:
        (Path(name) / "data").mkdir()
        folder = copy_data(Path(name) / "data")
        cache = Path(name) / "cache.json"
        _, _, calls, _ = run(["furnace_filter"], data=folder, cache=cache)
        assert calls.total == 13, calls.count  # 2 speech, 3 OCR, 5 file embeds, 3 for the fact
        index, _, calls, client = run(["furnace_filter"], data=folder, cache=cache)
        assert sorted(index.reused) == [
            "boarding-pass-2026-10-20.png", "lease-2026-07-15.pdf", "memo-2026-10-05.wav",
            "memo-2026-10-12.wav", "notes.md",
        ], index.reused  # fmt: skip
        # Only the unread memo is tried again (a transcription and nothing else per file).
        assert [i for i in client.ids if i.startswith(("ocr/", "transcribe/"))] == [
            "transcribe/memo-2026-10-27"
        ], client.ids
        assert calls.total == 4, calls.count  # the unread memo again, and the fact's 3 calls
        # A new memo is read; nothing else is.
        shutil.copy(folder / "memo-2026-10-12.wav", folder / "memo-2026-11-02.wav")
        SEGMENTS["transcribe/memo-2026-11-02"] = SEGMENTS["transcribe/memo-2026-10-12"]
        _, _, _, client = run(["furnace_filter"], data=folder, cache=cache)
        read = [i for i in client.ids if i.startswith(("ocr/", "transcribe/"))]
        assert read == ["transcribe/memo-2026-10-27", "transcribe/memo-2026-11-02"], read
        # A different OCR model reads the photo and the scan again.
        rules = copy.deepcopy(RULES)
        rules["models"]["ocr"] = "zai-org/GLM-OCR"
        _, _, _, client = run(["furnace_filter"], data=folder, cache=cache, rules=rules)
        read = [i for i in client.ids if i.startswith("ocr/")]
        assert len(read) == 3, read
        # A changed transcript in the memo app reads that memo again.
        memos = (folder / "voice_memos.csv").read_text(encoding="utf-8")
        (folder / "voice_memos.csv").write_text(memos.replace("Note to self.", "Note."), "utf-8")
        index, _, _, _ = run(["furnace_filter"], data=folder, cache=cache, rules=rules)
        assert "memo-2026-10-05.wav" not in index.reused, index.reused
        del SEGMENTS["transcribe/memo-2026-11-02"]
    print("cache: per file, keyed on the file, transcript and settings; unread tried again ok")


def check_changes() -> None:
    lines = [
        {"id": "wifi", "name": "Wifi password", "status": "updated", "value": "b"},
        {"id": "seat", "name": "Seat", "status": "found", "value": "14C"},
    ]
    previous = [
        {"id": "wifi", "name": "Wifi password", "status": "found", "value": "a"},
        {"id": "seat", "name": "Seat", "status": "found", "value": "14C"},
        {"id": "old", "name": "Old fact", "status": "found", "value": "x"},
    ]
    assert recipe.changes(lines, None) == ["First build: no earlier sheet to compare with."]
    assert recipe.changes(lines, ("20261101T000000Z", previous)) == [
        "Wifi password: was found 'a', now updated 'b'.",
        "Old fact: no longer tracked.",
    ]
    assert recipe.changes(lines[1:], ("r1", previous[1:2])) == [
        "Nothing changed since the build of r1."
    ]
    with tempfile.TemporaryDirectory() as name:
        runs = Path(name)
        for folder in ("20261101T000000Z", "20261102T000000Z", "20261103T000000Z"):
            (runs / folder).mkdir()
        payload = {"facts": previous}
        (runs / "20261102T000000Z" / "facts.json").write_text(json.dumps(payload), "utf-8")
        found = recipe.previous_sheet(runs, runs / "20261103T000000Z")
        assert found, found
        assert found[0] == "20261102T000000Z", found
    print("changes: compared with the newest earlier sheet ok")


def check_sheet_wording() -> None:
    """The sheet says where each value came from; the word "quote" never appears in what the
    recipe writes, only in the words of a source (the garage "quoted $420")."""
    index, lines, _, _ = run()
    text = recipe.render_markdown(index, list(lines.values()), ["First build."], "2026-11-03")
    assert text.index("## Unread files") < text.index("## To sort out") < text.index("## The sheet")
    assert "Where it came from:" in text
    assert '- notes.md line 9, dated 2026-03-18: "Furnace filter size is 16x25x1, MERV 11."' in text
    assert "(replaced) notes.md line 5" in text
    ours = text
    for line in lines.values():
        for cited in line["sources"]:
            ours = ours.replace(cited["words"], "")
    payload = json.dumps(list(lines.values()))
    for found in (ours, recipe.PROMPT, json.dumps(recipe.ANSWER_SCHEMA)):
        assert "quot" not in found.casefold(), found[found.casefold().find("quot") - 80 :][:160]
    assert "quot" in payload  # the brake memo's own words are kept as they are
    print("sheet: unread first, then the list, then where each value came from ok")


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
    two = ["furnace_filter", "denver_seat"]
    # A transient error on one fact's answer call: that fact goes to review, the other passes.
    for error in TRANSIENT:
        _, lines, _, _ = run(two, errors={"fact/furnace_filter/chat": [error]})
        assert lines["furnace_filter"]["status"] == "needs_review", type(error).__name__
        assert lines["furnace_filter"]["reason"].startswith("Not checked: the SIE call failed: ")
        assert lines["denver_seat"]["status"] == "found", lines["denver_seat"]
    # A transient error reading the photo: it is unread, and the seat goes to review.
    index, lines, _, _ = run(two, errors={"ocr/boarding": [ServerError("busy", status_code=503)]})
    assert index.unread[0].file == "boarding-pass-2026-10-20.png", index.unread
    assert index.unread[0].reason.startswith("the SIE call failed: ServerError (HTTP 503)")
    assert lines["denver_seat"]["status"] == "needs_review", lines["denver_seat"]
    assert lines["furnace_filter"]["status"] == "found", lines["furnace_filter"]
    # A reply missing its key is a failed call on the same path; an empty list is not.
    index, _, _, _ = run(two, replies={"ocr/boarding": {"something": []}})
    assert "replied without 'entities'" in index.unread[0].reason, index.unread
    index, _, _, _ = run(two, replies={"transcribe/memo-2026-10-12": {"no_data": {}}})
    assert "replied without 'data'" in index.unread[0].reason, index.unread
    index, _, _, _ = run(two, replies={"ocr/boarding": {"entities": []}})
    assert index.unread[0].reason == "the text read came back empty", index.unread
    _, lines, _, _ = run(two, replies={"fact/denver_seat/score": {"no_scores": []}})
    assert "replied without a score for every candidate" in lines["denver_seat"]["reason"]
    _, lines, _, _ = run(two, replies={"fact/denver_seat/chat": {"id": "x"}})
    assert "replied without 'choices'" in lines["denver_seat"]["reason"], lines["denver_seat"]
    _, lines, _, _ = run(two, replies={"fact/denver_seat": [{"sparse": {}}]})
    assert "replied without a 'dense' vector" in lines["denver_seat"]["reason"]
    # A file whose chunks couldn't be embedded is unread too.
    index, _, _, _ = run(two, errors={"notes#": [ServerError("busy", status_code=503)]})
    assert index.unread[-1].reason.startswith("not indexed: the SIE call failed"), index.unread
    # Every call failed (a dead SIE_BASE_URL): the run stops, with the full message.
    long = "connection refused " + "x" * 300
    dead = {"": [SIEConnectionError(long) for _ in range(40)]}
    message = stopped(lambda: run(two, errors=dead))
    assert message.startswith("Every SIE call failed ("), message
    assert long in message
    assert "Check SIE_BASE_URL and your network." in message, message
    busy = {"": [ModelLoadingError("loading") for _ in range(40)]}
    message = stopped(lambda: run(two, errors=busy))
    assert "may still be starting or SIE is busy; run again in a few minutes." in message
    # A refusal stops the run at once, with the full message, HTTP status and code.
    refusal = {"ocr/boarding": [InsufficientCreditsError("no credits " + "y" * 300)]}
    message = stopped(lambda: run(two, errors=refusal))
    assert "InsufficientCreditsError (HTTP 402, code INSUFFICIENT_CREDITS)" in message, message
    assert "y" * 300 in message
    assert "SIE_API_KEY" in message
    message = stopped(
        lambda: run(two, errors={"": [ModelLoadFailedError("gated", permanent=True)]})
    )
    assert message.startswith("SIE can't load lightonai/LightOnOCR-3-4B"), message
    message = stopped(lambda: run(two, errors={"": [RequestError("unexpected body")]}))
    assert "SIE_API_KEY" not in message, message
    assert "SIE_BASE_URL" in message, message
    # A bug in our own code is raised as is; main turns only SIEStopped into a clean exit.
    try:
        run(two, errors={"": [TypeError("bad argument")]})
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


def main() -> None:
    check_committed_data()
    check_lookups()
    check_parsing()
    check_planted_cases()
    check_value_supported()
    check_kinds()
    check_updates()
    check_calendar()
    check_unread()
    check_cut_off()
    check_later_change()
    check_negation_and_not_found()
    check_review_fixes()
    check_cache()
    check_changes()
    check_sheet_wording()
    check_sie_failure()
    print("selfcheck: all checks passed")


if __name__ == "__main__":
    main()
