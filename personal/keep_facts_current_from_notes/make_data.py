"""Write the synthetic household data in data/: notes, three voice memos, a photo, a lease scan,
the calendar seed and the voice memo app's seed.

Everything belongs to one fictional person, Jordan Ellery. Every name, address, airline, hotel,
garage, landlord and number is invented. The files:

    notes.md                      dated notes, one `## YYYY-MM-DD` heading per note
    memo-2026-10-05.wav           voice memo: the eye exam moved (the app has its transcript)
    memo-2026-10-12.wav           voice memo: the garage's price for the brakes
    memo-2026-10-27.wav           voice memo: only wind noise (the plumber's visit, unreadable)
    boarding-pass-2026-10-20.png  a photo of a paper boarding pass
    lease-2026-07-15.pdf          a two-page scanned lease (image only, no text layer)
    calendar.csv                  the calendar, one row per event (seed for calendar_app.py)
    voice_memos.csv               the voice memo app's titles and transcripts (voice_memos.py)

The spoken memos are read by macOS text-to-speech (SIE has no text-to-speech model), so this
runs on a Mac with the built-in `say` and `afconvert`. The files are committed; another macOS
version can render slightly different audio, so the committed files are the inputs of our live
run. Everything else, the wind-noise memo included, is deterministic.

    uv run make_data.py
"""

from __future__ import annotations

import csv
import io
import math
import random
import subprocess
import tempfile
import wave
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"

SAMPLE_RATE = 16_000
SPEECH_RATE = 175
VOICE = "Samantha"
PAUSE_S = 0.5

NOTES = [
    (
        "2026-03-02",
        "Home wifi (router in the hall closet): network HALLWAY-NET, password copper-kettle-42.",
    ),
    (
        "2026-03-18",
        "Furnace filter size is 16x25x1, MERV 11. Change it every three months; "
        "spares are on the top garage shelf.",
    ),
    (
        "2026-04-06",
        "Book club picked a space-opera novel for April. Meeting at Ines's place, "
        "first Sunday of the month.",
    ),
    (
        "2026-05-11",
        "Mom's birthday is June 14. Gift idea: the glazed ceramic planter from the "
        "Saturday market.",
    ),
    (
        "2026-05-30",
        "Parking spot 27 in the building garage, $95 a month, paid to the building "
        "office separately from the rent.",
    ),
    (
        "2026-06-22",
        "Signed the lease for unit 4B at 1187 Fennimore Street. Move-in day is August 1.",
    ),
    ("2026-07-08", "Gym locker combination is 18-04-33."),
    ("2026-07-21", "Annual physical with Dr. Mensah booked for December 3 at 8:00 am."),
    (
        "2026-08-03",
        "Neighbour in 4C is Tomasz. He has the spare key and waters the plants when I travel.",
    ),
    ("2026-08-19", "Bike tyre size is 700x32c. Pump to 70 psi."),
    (
        "2026-09-02",
        "Car is making a grinding noise when braking. Take it to Ridgeline Auto on Calder Avenue.",
    ),
    (
        "2026-09-15",
        "Booked an eye exam with Dr. Haldane at Clearview Optometry for Thursday, October 22 "
        "at 9:30 am.",
    ),
    ("2026-09-20", "New router installed. The wifi password is now juniper-lantern-77."),
    (
        "2026-09-28",
        "Denver trip October 20 to 23 for Priya's wedding. Booked the Larkspur Hotel on "
        "Wazee Street, confirmation LK-58213.",
    ),
    ("2026-10-01", "Car registration renewal is due by November 30. Do it online on the DMV site."),
    ("2026-10-03", "Groceries: oat milk, coffee beans, lemons, rice."),
    ("2026-10-09", "Fiddle-leaf fig: water once a week, about 500 ml."),
    ("2026-10-14", "Priya's wedding gift: the cookware set from their registry, already ordered."),
]

MEMOS = {
    "memo-2026-10-05.wav": (
        "Note to self.",
        "The eye doctor's office called because I'll be in Denver that week.",
        "My eye exam with Doctor Haldane is moved to Monday, October 26th, at 2:15 in the "
        "afternoon.",
        "Also, pick up the dry cleaning on Friday.",
    ),
    "memo-2026-10-12.wav": (
        "Car update.",
        "Ridgeline Auto called back about the grinding noise.",
        "They quoted $420 for new front brake pads and rotors.",
        "They can do it next Tuesday morning, and the rear tyres are fine for now.",
    ),
}

# Recorded outdoors in a gust: nothing but wind. The voice memo app titled it, nothing more.
WIND_MEMO = "memo-2026-10-27.wav"
WIND_SECONDS = 9

# The voice memo app: each recording's title, and its transcript where the app made one.
VOICE_MEMOS = [
    ("memo-2026-10-05.wav", "Eye doctor", " ".join(MEMOS["memo-2026-10-05.wav"])),
    ("memo-2026-10-12.wav", "Ridgeline Auto", ""),
    (WIND_MEMO, "Plumber, kitchen sink", ""),
]

# The calendar, as Jordan entered it. Nobody moved the eye exam after the office called.
CALENDAR = [
    ("evt-101", "Flight BF 2217 to Denver", "2026-10-20T08:20", "2026-10-20T11:50", "SFO"),
    ("evt-102", "Eye exam, Dr. Haldane", "2026-10-22T09:30", "2026-10-22T10:15", "Clearview"),
    ("evt-103", "Priya's wedding", "2026-10-22T16:00", "2026-10-22T22:00", "Denver"),
    ("evt-104", "Book club", "2026-11-01T15:00", "2026-11-01T17:00", "Ines's place"),
    ("evt-105", "Annual physical, Dr. Mensah", "2026-12-03T08:00", "2026-12-03T08:45", ""),
]

BOARDING_PASS = "boarding-pass-2026-10-20.png"
LEASE = "lease-2026-07-15.pdf"
LINE_HEIGHT = {"title": 50, "heading": 40, "body": 36, "signature": 36}
LEASE_DATE = datetime(2026, 7, 15, 12, 0, tzinfo=UTC).timetuple()  # byte-identical reruns

LEASE_TEXT = [
    ("title", "RESIDENTIAL LEASE AGREEMENT"),
    (
        "body",
        "This lease is made on July 15, 2026 between Cobblestone Row Properties LLC "
        "(Landlord) and Jordan Ellery (Tenant) for Unit 4B, 1187 Fennimore Street, Oakhurst.",
    ),
    ("heading", "1. Term"),
    ("body", "The lease begins on August 1, 2026 and ends on July 31, 2027."),
    ("heading", "2. Rent"),
    (
        "body",
        "Monthly rent is $2,150, due on the first day of each month. A late fee of $75 "
        "applies to rent received after the fifth day of the month.",
    ),
    ("heading", "3. Security Deposit"),
    (
        "body",
        "Tenant has paid a security deposit of $3,225. Landlord will return the deposit, "
        "less any lawful deductions, within 21 days after Tenant moves out.",
    ),
    ("heading", "4. Utilities"),
    ("body", "Tenant pays electricity and internet. Landlord pays water, sewer and trash."),
    ("heading", "5. Pets"),
    ("body", "Tenant may keep one cat. Pet rent is $40 per month."),
    ("page", ""),
    ("heading", "6. Notice to Vacate"),
    (
        "body",
        "Tenant must give Landlord at least sixty (60) days' written notice before moving "
        "out, whether at the end of the term or after it.",
    ),
    ("heading", "7. Repairs"),
    (
        "body",
        "Tenant must report needed repairs in writing through the resident portal. "
        "Landlord will respond to urgent repairs within 24 hours.",
    ),
    ("heading", "8. Quiet Hours"),
    ("body", "Quiet hours are 10:00 pm to 7:00 am every day."),
    ("signature", "Signed: Jordan Ellery, Tenant          Signed: R. Okonkwo, for Landlord"),
]


def notes_markdown() -> str:
    parts = ["# Notes\n"]
    for date, text in NOTES:
        parts.append(f"## {date}\n\n{text}\n")
    return "\n".join(parts)


# --- Voice memos ------------------------------------------------------------------------------


def _speak(text: str, workdir: Path, index: int) -> bytes:
    aiff, wav = workdir / f"s{index:02d}.aiff", workdir / f"s{index:02d}.wav"
    subprocess.run(["say", "-v", VOICE, "-r", str(SPEECH_RATE), "-o", str(aiff), text], check=True)
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", f"LEI16@{SAMPLE_RATE}", "-c", "1", str(aiff), str(wav)],
        check=True,
    )
    with wave.open(str(wav), "rb") as handle:
        return handle.readframes(handle.getnframes())


def render_memo(sentences: tuple[str, ...], path: Path) -> float:
    pause = b"\x00\x00" * int(SAMPLE_RATE * PAUSE_S)
    frames: list[bytes] = []
    with tempfile.TemporaryDirectory(prefix="memory-memo-") as tmp:
        for index, sentence in enumerate(sentences):
            frames += [_speak(sentence, Path(tmp), index), pause]
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(b"".join(frames))
    return sum(len(chunk) for chunk in frames) / 2 / SAMPLE_RATE


def render_wind(path: Path) -> float:
    """Gusty low-frequency noise and no speech: a memo that reads empty."""
    rng = random.Random(1027)
    level, frames = 0.0, bytearray()
    for index in range(SAMPLE_RATE * WIND_SECONDS):
        level = 0.995 * level + rng.gauss(0.0, 1.0)
        gust = 0.55 + 0.45 * math.sin(2 * math.pi * index / (SAMPLE_RATE * 3.1))
        sample = max(-32767, min(32767, int(level * gust * 260)))
        frames += sample.to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(bytes(frames))
    return float(WIND_SECONDS)


def csv_text(header: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


# --- Boarding pass photo ----------------------------------------------------------------------


def render_boarding_pass() -> Image.Image:
    """A boarding pass on a desk, photographed slightly crooked."""
    card = Image.new("RGB", (1500, 620), (250, 250, 246))
    draw = ImageDraw.Draw(card)
    big = ImageFont.load_default(size=56)
    label = ImageFont.load_default(size=24)
    value = ImageFont.load_default(size=40)
    draw.rectangle([(0, 0), (1500, 110)], fill=(24, 74, 120))
    draw.text((40, 26), "BLUEFINCH AIR", font=big, fill=(255, 255, 255))
    draw.text((1080, 40), "BOARDING PASS", font=value, fill=(255, 255, 255))
    fields = [
        ((40, 150), "PASSENGER", "ELLERY / JORDAN"),
        ((620, 150), "FLIGHT", "BF 2217"),
        ((960, 150), "DATE", "20 OCT 2026"),
        ((40, 290), "FROM", "SAN FRANCISCO (SFO)"),
        ((620, 290), "TO", "DENVER (DEN)"),
        ((40, 430), "BOARDING", "07:40"),
        ((340, 430), "GATE", "B12"),
        ((620, 430), "SEAT", "14C"),
        ((880, 430), "ZONE", "3"),
        ((1080, 430), "CONFIRMATION", "QX7T2M"),
    ]
    for (x, y), name, text in fields:
        draw.text((x, y), name, font=label, fill=(90, 90, 90))
        draw.text((x, y + 36), text, font=value, fill=(10, 10, 10))
    desk = Image.new("RGB", (1700, 860), (150, 118, 86))
    tilted = card.rotate(1.2, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=(0, 0, 0))
    mask = Image.new("L", card.size, 255).rotate(1.2, expand=True, fillcolor=0)
    desk.paste(tilted, (90, 110), mask)
    return desk.filter(ImageFilter.GaussianBlur(0.7))


# --- Lease scan -------------------------------------------------------------------------------


def _wrap(text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if current and font.getlength(trial) > width:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def render_lease() -> list[Image.Image]:
    rng = random.Random(1187)
    size, margin = (1240, 1754), 120
    fonts = {
        "title": ImageFont.load_default(size=36),
        "heading": ImageFont.load_default(size=26),
        "body": ImageFont.load_default(size=24),
        "signature": ImageFont.load_default(size=24),
    }
    pages: list[Image.Image] = []
    page = Image.new("L", size, 255)
    draw = ImageDraw.Draw(page)
    y = margin
    for style, text in LEASE_TEXT:
        if style == "page":
            pages.append(page)
            page = Image.new("L", size, 255)
            draw, y = ImageDraw.Draw(page), margin
            continue
        if style == "signature":
            y += 60
        for line in _wrap(text, fonts[style], size[0] - 2 * margin):
            draw.text((margin, y), line, font=fonts[style], fill=0)
            y += LINE_HEIGHT[style]
        y += 22
    pages.append(page)
    scanned = []
    for number, finished in enumerate(pages, start=1):
        ImageDraw.Draw(finished).text(
            (size[0] // 2 - 50, size[1] - 80), f"Page {number} of {len(pages)}", font=fonts["body"]
        )
        angle = rng.uniform(-0.5, 0.5)
        skewed = finished.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=255)
        pixels = skewed.load()
        assert pixels is not None
        for _ in range(2000):
            pixels[rng.randrange(size[0]), rng.randrange(size[1])] = rng.randint(90, 200)
        soft = skewed.filter(ImageFilter.GaussianBlur(0.6))
        scanned.append(soft.point(lambda value: int(18 + value * 0.88)))
    return scanned


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    notes = DATA_DIR / "notes.md"
    notes.write_text(notes_markdown(), encoding="utf-8")
    print(f"wrote {notes.relative_to(ROOT)} ({len(NOTES)} notes)")
    for name, sentences in MEMOS.items():
        seconds = render_memo(sentences, DATA_DIR / name)
        print(f"wrote data/{name} ({seconds:.0f} s)")
    seconds = render_wind(DATA_DIR / WIND_MEMO)
    print(f"wrote data/{WIND_MEMO} ({seconds:.0f} s, wind only)")
    (DATA_DIR / "voice_memos.csv").write_text(
        csv_text(("file", "title", "transcript"), VOICE_MEMOS), encoding="utf-8"
    )
    print(f"wrote data/voice_memos.csv ({len(VOICE_MEMOS)} recordings)")
    (DATA_DIR / "calendar.csv").write_text(
        csv_text(("event_id", "title", "start", "end", "location"), CALENDAR), encoding="utf-8"
    )
    print(f"wrote data/calendar.csv ({len(CALENDAR)} events)")
    render_boarding_pass().save(DATA_DIR / BOARDING_PASS, format="PNG", optimize=True)
    print(f"wrote data/{BOARDING_PASS}")
    pages = render_lease()
    pages[0].save(
        DATA_DIR / LEASE,
        format="PDF",
        save_all=True,
        append_images=pages[1:],
        resolution=150,
        creationDate=LEASE_DATE,
        modDate=LEASE_DATE,
    )
    print(f"wrote data/{LEASE} ({len(pages)} pages)")


if __name__ == "__main__":
    main()
