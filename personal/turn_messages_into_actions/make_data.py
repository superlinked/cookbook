"""Write the synthetic inbox and the lookups' seeds in data/.

Everything here is invented: Jordan Ellery, the family, the school, the dentist, the utility
(Bayview Water & Power) and every address and number. The inbox covers a week from late October
to November 1, 2026 and plants the cases listed in the README.

    data/inbox.jsonl                       one message per line (email, text, voice memo, photo),
                                           as the mail, messaging and memo apps export them
    data/memo-1028-registration.wav        voice memo to self, macOS `say` + `afconvert`
    data/memo-1029-vet.wav                 voice memo to self (the phone also gave a transcript)
    data/bill-1028-bayview-water-power.png photo of a paper utility bill, drawn with Pillow
    data/calendar.csv                      the calendar export (calendar_app.py reads it)
    data/contacts.csv                      the address book (contacts.py)
    data/payees.csv                        the banking app's payee list (payees.py)

SIE has no text-to-speech model, so the memos are rendered locally on a Mac with the built-in
`say` and `afconvert` commands, as in insurance/check_call_against_claim_record. The committed
files are the inputs of our live run: another macOS or Pillow version can give slightly
different bytes.

    uv run make_data.py
"""

from __future__ import annotations

import csv
import io
import json
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"

SAMPLE_RATE = 16_000
SPEECH_RATE = 175  # words per minute for `say -r`
MEMO_VOICE = "Samantha"
FONT_DIR = Path("/System/Library/Fonts/Supplemental")

VET_MEMO = "Reminder. Book a vet appointment for Biscuit. His vaccines are due by November 20th."
MEMOS = {
    "memo-1028-registration.wav": "Note to self. Renew the car registration online before Friday.",
    "memo-1029-vet.wav": VET_MEMO,
}
BILL = "bill-1028-bayview-water-power.png"


def _email(sender: str, subject: str, body: str) -> dict[str, str]:
    return {"channel": "email", "from": sender, "subject": subject, "body": body}


def _text(sender: str, body: str) -> dict[str, str]:
    return {"channel": "sms", "from": sender, "subject": "", "body": body}


def _from_me(channel: str, attachment: str, transcript: str = "") -> dict[str, str]:
    record = {"channel": channel, "from": "me", "subject": "", "body": "", "attachment": attachment}
    return record | ({"transcript": transcript} if transcript else {})


# The cancellation (msg-15) arrives as a reply on top of the original sign-up email.
BOOK_FAIR_BODY = (
    "Sorry, Saturday's book-fair shift is cancelled. We have more volunteers than tables this "
    "year. Thank you for signing up!\n"
    "\n"
    "On Mon, Oct 26, 2026 at 3:02 PM Hillcrest PTA wrote:\n"
    "> Thanks for signing up for the book fair! Your shift is Saturday, November 7 at 10 AM\n"
    "> in the school library. Please check in at the front desk."
)

# (id, sent, message) in sent order; the planted case is in the comment above each one. The ids
# of the two newest cases (msg-14, msg-15) come after the original thirteen.
MESSAGES: list[tuple[str, str, dict[str, str]]] = [
    # Already on the calendar: the school's invitation was accepted a week ago.
    (
        "msg-01",
        "2026-10-26T08:12:00-07:00",
        _email(
            "Hillcrest Elementary Office",
            "Parent-teacher conference for Leo",
            "Dear Jordan, your parent-teacher conference with Ms. Abernathy is scheduled for "
            "Thursday, November 5 at 4:15 PM in Room 12. Please arrive a few minutes early.",
        ),
    ),
    # Practice, part 1: "this Saturday at 9 AM", already on the calendar.
    (
        "msg-02",
        "2026-10-26T18:40:00-07:00",
        _text(
            "Coach Dana Ramos",
            "Hi Falcons parents! Practice is this Saturday at 9 AM at Rossi Field. "
            "Bring water and shin guards.",
        ),
    ),
    # Dentist, part 1: Jordan's cleaning, confirmed by email.
    (
        "msg-03",
        "2026-10-27T10:05:00-07:00",
        _email(
            "Brightsmile Dental",
            "Appointment confirmed",
            "Hi Jordan, your teeth cleaning with Dr. Okafor is confirmed for Tuesday, "
            "November 3 at 10:30 AM at 410 Larkin Street, Suite 2. "
            "Call us at 555-0148 to make changes.",
        ),
    ),
    # New: a second appointment from the same dentist on the same day, for Leo. It must stay
    # its own hold.
    (
        "msg-14",
        "2026-10-27T10:07:00-07:00",
        _email(
            "Brightsmile Dental",
            "Appointment confirmed",
            "Hi Jordan, Leo's teeth cleaning with Dr. Okafor is confirmed for Tuesday, "
            "November 3 at 11:15 AM at 410 Larkin Street, Suite 2. "
            "Call us at 555-0148 to make changes.",
        ),
    ),
    # Reply needed, with a deadline relative to the sent date ("by Friday").
    (
        "msg-04",
        "2026-10-27T21:15:00-07:00",
        _email(
            "Maya Lindqvist",
            "That plumber",
            "Hey Jordan, can you send me the name of the plumber you liked? "
            "Please reply by Friday so I can call him. Maya",
        ),
    ),
    # Newsletter: nothing to do.
    (
        "msg-05",
        "2026-10-28T06:00:00-07:00",
        _email(
            "Sunset Neighborhood News",
            "This week in the Sunset",
            "The farmers market moves indoors for the winter. The branch library has new "
            "evening hours, and the community garden has three open plots. Read the full "
            "stories on our website.",
        ),
    ),
    # Voice-memo to-do with a relative deadline; the phone gave no transcript.
    ("msg-06", "2026-10-28T07:50:00-07:00", _from_me("voice_memo", "memo-1028-registration.wav")),
    # Vague time: "sometime next week" must become a question, not a guessed date.
    (
        "msg-07",
        "2026-10-28T09:30:00-07:00",
        _email(
            "Theo Marsh",
            "Garden plot",
            "Hi Jordan, let's grab coffee sometime next week to plan the community garden "
            "plot. Theo",
        ),
    ),
    # Bill: amount and due date only on the paper, which only the photo has.
    ("msg-08", "2026-10-28T19:02:00-07:00", _from_me("photo", BILL)),
    # Voice-memo to-do with an absolute deadline; the phone's transcript comes with it.
    (
        "msg-09",
        "2026-10-29T08:10:00-07:00",
        _from_me("voice_memo", "memo-1029-vet.wav", transcript=VET_MEMO),
    ),
    # Dentist, part 2: the reminder text, "next Tuesday at 10:30am".
    (
        "msg-10",
        "2026-10-29T09:00:00-07:00",
        _text(
            "Brightsmile Dental",
            "Brightsmile Dental reminder: your cleaning is next Tuesday at 10:30am. See you then!",
        ),
    ),
    # "tomorrow at 3": a relative date and a bare hour.
    (
        "msg-11",
        "2026-10-29T12:20:00-07:00",
        _text("Sam Ellery", "Can you pick up Leo from school tomorrow at 3? I have a work call."),
    ),
    # Delivery notice: nothing to do.
    (
        "msg-12",
        "2026-10-29T14:03:00-07:00",
        _email(
            "Parcel Express",
            "Delivered: your package",
            "Your package was delivered to the front door at 1:58 PM today. "
            "No signature was required.",
        ),
    ),
    # Practice, part 2: the same practice moves to 11 AM.
    (
        "msg-13",
        "2026-10-29T17:45:00-07:00",
        _text(
            "Coach Dana Ramos",
            "Change of plans: practice this Saturday moves to 11 AM, same field. "
            "Sorry for the late notice!",
        ),
    ),
    # New: a cancellation on top of the quoted original. The time in the message (10 AM) is
    # the original's, so it must never become a hold. Sent after clocks went back (UTC-8).
    (
        "msg-15",
        "2026-11-01T10:20:00-08:00",
        _email("Hillcrest PTA", "Re: Book fair volunteer shift", BOOK_FAIR_BODY),
    ),
]


def inbox_records() -> list[dict[str, Any]]:
    return [{"id": message_id, "sent": sent, **message} for message_id, sent, message in MESSAGES]


# --- The lookups' seeds: calendar, contacts, payee list ---------------------------------------

# The calendar export: what Jordan entered or accepted. Practice is a weekly event Jordan
# entered (no organizer); the conference is the school's accepted invitation. Nov 7 practice is
# a near miss for "this Saturday" in msg-13.
CALENDAR = (
    ("event_id", "title", "start", "end", "location", "organizer"),
    (
        "cal-01",
        "Falcons soccer practice",
        "2026-10-31T09:00:00-07:00",
        "2026-10-31T10:30:00-07:00",
        "Rossi Field",
        "",
    ),
    (
        "cal-02",
        "Design review (work)",
        "2026-11-03T14:00:00-08:00",
        "2026-11-03T15:00:00-08:00",
        "Video call",
        "",
    ),
    (
        "cal-03",
        "Leo swim lesson",
        "2026-11-04T17:00:00-08:00",
        "2026-11-04T17:45:00-08:00",
        "Sunset pool",
        "",
    ),
    (
        "cal-04",
        "Parent-teacher conference (Leo)",
        "2026-11-05T16:15:00-08:00",
        "2026-11-05T16:35:00-08:00",
        "Room 12",
        "Hillcrest Elementary Office",
    ),
    (
        "cal-05",
        "Falcons soccer practice",
        "2026-11-07T09:00:00-08:00",
        "2026-11-07T10:30:00-08:00",
        "Rossi Field",
        "",
    ),
    (
        "cal-06",
        "Book fair volunteer shift",
        "2026-11-07T10:00:00-08:00",
        "2026-11-07T12:00:00-08:00",
        "Hillcrest school library",
        "",
    ),
)

CONTACTS = (
    ("contact_id", "name", "aliases", "relationship"),
    ("c-01", "Sam Ellery", "Sam", "partner"),
    ("c-02", "Leo Ellery", "Leo", "son"),
    ("c-03", "Coach Dana Ramos", "Dana Ramos", "soccer coach"),
    ("c-04", "Brightsmile Dental", "Brightsmile", "dentist"),
    ("c-05", "Hillcrest Elementary Office", "Hillcrest Elementary", "school"),
    ("c-06", "Hillcrest PTA", "", "school parents' association"),
    ("c-07", "Maya Lindqvist", "Maya", "friend"),
    ("c-08", "Theo Marsh", "Theo", "neighbor"),
)

# Bayview Water & Power sends paper only, so the bank knows its name and account number and
# nothing else. Bayview Waste Collection is a near miss on the name.
PAYEES = (
    ("payee_id", "name", "aliases", "account_number", "autopay", "balance", "due_date"),
    ("p-01", "Bayview Water & Power", "Bayview Water and Power", "4417-2290-31", "no", "", ""),
    ("p-02", "Bayview Waste Collection", "", "BWC-55120", "no", "", ""),
    ("p-03", "Fogline Mobile", "", "8812-4470", "yes", "64.00", "2026-11-15"),
    (
        "p-04",
        "Harbor Mutual Insurance",
        "Harbor Mutual",
        "HM-220-1185",
        "no",
        "112.50",
        "2026-11-20",
    ),
)


def write_csv(path: Path, rows: tuple[tuple[str, ...], ...]) -> None:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    path.write_text(buffer.getvalue(), encoding="utf-8")


# --- Voice memos ------------------------------------------------------------------------------


def render_memo(text: str, path: Path) -> float:
    """Speak `text` with `say`, convert to 16 kHz mono 16-bit WAV; returns the length in s."""
    with tempfile.TemporaryDirectory(prefix="inbox-memo-") as tmp:
        aiff = Path(tmp) / "memo.aiff"
        subprocess.run(
            ["say", "-v", MEMO_VOICE, "-r", str(SPEECH_RATE), "-o", str(aiff), text], check=True
        )
        subprocess.run(
            [
                "afconvert",
                "-f",
                "WAVE",
                "-d",
                f"LEI16@{SAMPLE_RATE}",
                "-c",
                "1",
                str(aiff),
                str(path),
            ],
            check=True,
        )
    with wave.open(str(path), "rb") as handle:
        if (handle.getframerate(), handle.getnchannels(), handle.getsampwidth()) != (
            SAMPLE_RATE,
            1,
            2,
        ):
            raise RuntimeError(f"afconvert did not produce 16 kHz mono 16-bit audio: {path}")
        return handle.getnframes() / SAMPLE_RATE


# --- Bill photo -------------------------------------------------------------------------------

# (label, amount); the amounts add up: 79.40 - 79.40 + 84.17 = 84.17.
BILL_ROWS = (
    ("Previous balance", "$79.40"),
    ("Payment received Oct 2 - thank you", "-$79.40"),
    ("Water service (6 units)", "$38.25"),
    ("Electric service (212 kWh)", "$41.12"),
    ("City utility users tax", "$4.80"),
    ("Total new charges", "$84.17"),
)


def _font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = FONT_DIR / name
    return ImageFont.truetype(str(path), size) if path.exists() else ImageFont.load_default(size)


def render_bill(path: Path) -> None:
    """A one-page utility bill, photographed slightly askew on a table."""
    page = Image.new("RGB", (1100, 1400), "white")
    draw = ImageDraw.Draw(page)
    title, body, bold = (
        _font("Arial Bold.ttf", 46),
        _font("Arial.ttf", 28),
        _font("Arial Bold.ttf", 30),
    )
    draw.text((70, 60), "Bayview Water & Power", font=title, fill=(20, 60, 120))
    draw.text((70, 120), "PO Box 4410, San Francisco, CA 94120", font=body, fill="black")
    draw.line((70, 175, 1030, 175), fill=(20, 60, 120), width=3)
    header = (
        ("Customer", "Jordan Ellery"),
        ("Service address", "88 Larchmont Way, San Francisco"),
        ("Account number", "4417-2290-31"),
        ("Statement date", "October 20, 2026"),
        ("Billing period", "Sep 18 - Oct 17, 2026"),
    )
    y = 205
    for label, value in header:
        draw.text((70, y), label, font=body, fill="black")
        draw.text((470, y), value, font=body, fill="black")
        y += 46
    y += 30
    draw.text((70, y), "Account summary", font=bold, fill="black")
    y += 56
    for label, amount in BILL_ROWS:
        draw.text((70, y), label, font=body, fill="black")
        width = draw.textlength(amount, font=body)
        draw.text((1030 - width, y), amount, font=body, fill="black")
        y += 46
    y += 20
    draw.rectangle((60, y, 1040, y + 140), outline=(20, 60, 120), width=3)
    draw.text((90, y + 22), "Amount due", font=bold, fill="black")
    draw.text((700, y + 22), "$84.17", font=bold, fill="black")
    draw.text((90, y + 78), "Due date", font=bold, fill="black")
    draw.text((700, y + 78), "November 12, 2026", font=bold, fill="black")
    y += 190
    notes = (
        "Please pay by the due date to avoid a $5.00 late fee.",
        "Pay online at bayview-wp.example or by phone at 555-0190.",
    )
    for line in notes:
        draw.text((70, y), line, font=body, fill="black")
        y += 44
    # A phone photo: the page sits a little rotated on a grey table.
    photo = page.rotate(1.5, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=(96, 96, 96))
    photo.save(path, format="PNG", optimize=True)


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    inbox = DATA_DIR / "inbox.jsonl"
    lines = [json.dumps(record, ensure_ascii=False) for record in inbox_records()]
    inbox.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {inbox.relative_to(ROOT)} ({len(lines)} messages)")
    for name, rows in (
        ("calendar.csv", CALENDAR),
        ("contacts.csv", CONTACTS),
        ("payees.csv", PAYEES),
    ):
        write_csv(DATA_DIR / name, rows)
        print(f"wrote data/{name} ({len(rows) - 1} rows)")
    for name, text in MEMOS.items():
        seconds = render_memo(text, DATA_DIR / name)
        print(f"wrote data/{name} ({seconds:.1f} s)")
    render_bill(DATA_DIR / BILL)
    print(f"wrote data/{BILL}")


if __name__ == "__main__":
    main()
