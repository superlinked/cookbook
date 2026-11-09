"""Rebuild the two scanned resumes in data/resumes/: a paper application and a photo of a resume.

The text resumes, data/applicants.csv and rules.yaml are written by hand. Everything is synthetic:
Fernhill Facility Services and every applicant, school and employer are made up. Run it with
`uv run --group data make_data.py`.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

RESUMES = Path(__file__).parent / "data" / "resumes"
INK = (24, 26, 40)
PAPER = (246, 244, 236)

# A-102 filled in Fernhill's paper application at a walk-in hiring day. Labels print in capitals;
# the answers are what the applicant wrote.
APPLICATION: list[tuple[str, str]] = [
    ("NAME", "Elena Sokolova"),
    ("PHONE", "(555) 018-5521"),
    ("EMAIL", "e.sokolova@example.com"),
    ("ADDRESS", "41 Linden Ave, Gahanna, OH 43230"),
    ("POSITION APPLIED FOR", "Commercial HVAC Service Technician"),
    ("CURRENT EMPLOYER", "Barrow County Schools, Facilities Dept."),
    ("YEARS IN THIS JOB", "5 (2021 to now)"),
    ("WHAT YOU WORK ON", "Rooftop units and two air-cooled chillers at six schools,"),
    ("", "boilers in winter, VFDs and pumps"),
    ("PREVIOUS EMPLOYER", "Summit Refrigeration, 2019-2021, walk-ins and reach-ins"),
    ("EPA 608 CERTIFICATION", "Yes - Type I, Type II and Type III"),
    ("DRIVER'S LICENSE", "Yes, Ohio, no points"),
    ("OTHER CERTIFICATES", "OSHA 10"),
]

# A-105 is a phone photo of a printed resume, a little tilted and soft.
PHOTO_LINES: list[str] = [
    "TOMAS HERRERA",
    "Whitehall, OH  |  555-013-7764  |  therrera.hvac@example.com",
    "",
    "HVAC SERVICE TECHNICIAN",
    "",
    "Experience",
    "Service Technician, Keystone Climate Control      2019 - present",
    " - Rooftop units, split systems and make-up air units for",
    "   restaurants, clinics and a cold-storage warehouse",
    " - Refrigerant recovery, leak checks, compressor change-outs",
    " - Drive a service van; 6 to 8 calls a day",
    "",
    "Certifications",
    " - EPA 608 Universal (2018)",
    " - Ohio driver's license",
    "",
    "Education",
    " Ridgeway Technical College, HVAC-R program, 2018",
    "",
    "References",
    " Nora Quill, Service Manager, Keystone Climate Control, 555-013-9001",
]


def application(path: Path) -> None:
    image = Image.new("RGB", (1240, 1400), PAPER)
    draw = ImageDraw.Draw(image)
    title = ImageFont.load_default(size=40)
    label = ImageFont.load_default(size=22)
    answer = ImageFont.load_default(size=30)
    draw.text((60, 50), "FERNHILL FACILITY SERVICES", fill=INK, font=title)
    draw.text((60, 105), "EMPLOYMENT APPLICATION - WALK-IN HIRING DAY", fill=INK, font=label)
    y = 180
    for name, value in APPLICATION:
        if name:
            draw.text((60, y), name, fill=(90, 90, 100), font=label)
        draw.text((400, y - 4), value, fill=(20, 40, 120), font=answer)
        draw.line((400, y + 32, 1180, y + 32), fill=(170, 170, 170), width=1)
        y += 78
    draw.text(
        (60, y + 30), "SIGNATURE   E. Sokolova          DATE   10/30/2026", fill=INK, font=label
    )
    soft = image.rotate(-1.2, fillcolor=(225, 223, 215)).filter(ImageFilter.GaussianBlur(0.8))
    soft.save(path, optimize=True)


def photo(path: Path) -> None:
    image = Image.new("RGB", (1240, 1300), (250, 250, 248))
    draw = ImageDraw.Draw(image)
    y = 70
    for number, line in enumerate(PHOTO_LINES):
        size = 44 if number == 0 else 28
        draw.text((80, y), line, fill=INK, font=ImageFont.load_default(size=size))
        y += 62 if number == 0 else 46
    tilted = image.rotate(2.5, fillcolor=(120, 112, 100)).filter(ImageFilter.GaussianBlur(1.1))
    tilted.save(path, optimize=True)


def main() -> None:
    RESUMES.mkdir(parents=True, exist_ok=True)
    application(RESUMES / "A-102.png")
    photo(RESUMES / "A-105.png")
    print(f"Wrote {RESUMES / 'A-102.png'} and {RESUMES / 'A-105.png'}")


if __name__ == "__main__":
    main()
