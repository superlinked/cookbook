"""Write the synthetic equipment manual in data/: a 9-page image-only PDF plus its page text.

The manual is for a fictional machine, the Brindlemoor Thermal Systems CR-220 process chiller.
The company, the model, every value and every procedure are invented for this demo; it is not a
real manual and not service advice. Several answers exist only inside a diagram (a valve tag on
the piping drawing, a terminal number on the wiring drawing), which is what this recipe is for.

The PDF is the site's document set for the CR-220: the 8 pages of manual revision C, then the
strainer page of revision B, kept for the older unit that still runs on it. The two strainer
pages give different cover bolt torques (20 N m in revision C, 25 N m in revision B).

    uv run make_data.py

writes data/manual.pdf (the pages as images, no text layer: what a scanned manual looks like)
and data/manual.json (for each PDF page: its printed page number, its revision, its title and
every string drawn on it, the authoring source the recipe checks quotes against). Rerunning
rebuilds both byte for byte.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"

DPI = 150
WIDTH, HEIGHT = 1275, 1650  # US letter at 150 dpi
MARGIN = 90
FIXED_DATE = date(2026, 10, 29).timetuple()  # byte-identical PDF across reruns
INK = (25, 25, 30)
GREY = (110, 110, 120)
BLUE = (30, 80, 160)
RED = (180, 35, 35)
PALE = (232, 238, 246)
MANUAL = "CR-220 Process Chiller: Operator and Service Manual"
MAKER = "Brindlemoor Thermal Systems (fictional)"
PAGES = 8


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)  # type: ignore[return-value]


class Page:
    """One manual page: draws on an image and keeps every string it draws, in order."""

    def __init__(self, number: int, title: str, revision: str = "C") -> None:
        self.number = number
        self.title = title
        self.revision = revision
        self.image = Image.new("RGB", (WIDTH, HEIGHT), "white")
        self.draw = ImageDraw.Draw(self.image)
        self.lines: list[str] = []
        self.y = 0
        self._chrome()

    def text(
        self,
        xy: tuple[int, int],
        value: str,
        size: int = 22,
        fill: tuple[int, int, int] = INK,
        anchor: str = "la",
    ) -> None:
        self.draw.text(xy, value, font=_font(size), fill=fill, anchor=anchor)
        self.lines.append(value)

    def _chrome(self) -> None:
        self.text((MARGIN, 50), MAKER, 18, GREY)
        self.text((WIDTH - MARGIN, 50), MANUAL, 18, GREY, "ra")
        self.draw.line((MARGIN, 80, WIDTH - MARGIN, 80), fill=GREY, width=2)
        self.draw.line((MARGIN, HEIGHT - 80, WIDTH - MARGIN, HEIGHT - 80), fill=GREY, width=2)
        self.text((MARGIN, HEIGHT - 65), f"CR-220 manual, revision {self.revision}", 18, GREY)
        self.text((WIDTH - MARGIN, HEIGHT - 65), f"Page {self.number} of {PAGES}", 20, INK, "ra")
        self.text((MARGIN, 110), f"{self.number}. {self.title}", 40, BLUE)
        self.y = 190

    def heading(self, value: str) -> None:
        self.y += 14
        self.text((MARGIN, self.y), value, 28, BLUE)
        self.y += 46

    def para(self, lines: list[str], size: int = 22, indent: int = 0) -> None:
        for line in lines:
            self.text((MARGIN + indent, self.y), line, size)
            self.y += round(size * 1.5)
        self.y += 10

    def table(self, columns: list[tuple[str, int]], rows: list[list[str]], size: int = 21) -> None:
        row_h = round(size * 1.9)
        x0, x1 = MARGIN, MARGIN + sum(width for _, width in columns)
        self.draw.rectangle((x0, self.y, x1, self.y + row_h), fill=PALE)
        for index, row in enumerate([[name for name, _ in columns], *rows]):
            x = x0
            for (_, width), cell in zip(columns, row, strict=True):
                self.text((x + 10, self.y + row_h // 2), cell, size, anchor="lm")
                x += width
            self.y += row_h
            self.draw.line((x0, self.y, x1, self.y), fill=GREY, width=1 if index else 2)
        self.y += 24

    def box(self, rect: tuple[int, int, int, int], label: str, size: int = 22) -> None:
        self.draw.rectangle(rect, outline=INK, width=3, fill=PALE)
        cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
        for offset, part in enumerate(label.split("\n")):
            shift = (offset - (label.count("\n")) / 2) * size * 1.3
            self.text((cx, round(cy + shift)), part, size, anchor="mm")

    def pipe(self, points: list[tuple[int, int]], color: tuple[int, int, int] = BLUE) -> None:
        self.draw.line(points, fill=color, width=6, joint="curve")
        (xa, ya), (xb, yb) = points[-2], points[-1]
        dx, dy = (xb > xa) - (xb < xa), (yb > ya) - (yb < ya)
        tip = (xb, yb)
        left = (xb - 18 * dx - 10 * dy, yb - 18 * dy - 10 * dx)
        right = (xb - 18 * dx + 10 * dy, yb - 18 * dy + 10 * dx)
        self.draw.polygon([tip, left, right], fill=color)

    def valve(self, center: tuple[int, int], tag: str, label: str, vertical: bool = False) -> None:
        """A bow-tie valve symbol with its tag and its label next to it."""
        x, y = center
        if vertical:
            self.draw.polygon([(x - 16, y - 22), (x + 16, y - 22), (x, y)], outline=INK, fill=RED)
            self.draw.polygon([(x - 16, y + 22), (x + 16, y + 22), (x, y)], outline=INK, fill=RED)
        else:
            self.draw.polygon([(x - 22, y - 16), (x - 22, y + 16), (x, y)], outline=INK, fill=RED)
            self.draw.polygon([(x + 22, y - 16), (x + 22, y + 16), (x, y)], outline=INK, fill=RED)
            self.text((x, y - 24), tag, 24, RED, "md")
            self.text((x, y + 24), label, 19, anchor="ma")
            return
        self.text((x + 30, y - 34), tag, 24, RED)
        self.text((x + 30, y - 6), label, 19)

    def callout(self, center: tuple[int, int], number: str) -> None:
        x, y = center
        self.draw.ellipse((x - 20, y - 20, x + 20, y + 20), outline=INK, width=3, fill="white")
        self.text((x, y), number, 22, anchor="mm")


def page_contents() -> Page:
    page = Page(1, "Contents and safety")
    page.heading("Contents")
    page.para(
        [
            "1. Contents and safety ................................. 1",
            "2. Specifications ........................................ 2",
            "3. Control panel ......................................... 3",
            "4. Coolant circuit and valves ...................... 4",
            "5. Electrical connections ............................. 5",
            "6. Fault codes ............................................. 6",
            "7. Maintenance schedule ............................... 7",
            "8. Strainer basket replacement .................... 8",
        ]
    )
    page.heading("Safety")
    page.para(
        [
            "Read this manual before installing, operating or servicing the CR-220.",
            "Lock out and tag out the main disconnect before opening any panel. The fan",
            "and pump can start without warning when the controller calls for cooling.",
            "Only a certified refrigeration technician may open the refrigerant circuit.",
            "The CR-220 holds R-513A refrigerant under pressure; wear eye protection and",
            "gloves. Never vent refrigerant to the atmosphere.",
            "Coolant may be hot after operation. Let the reservoir cool below 40 C",
            "before draining it.",
        ]
    )
    page.heading("Warranty")
    page.para(
        [
            "Brindlemoor warrants the CR-220 against defects in materials and workmanship",
            "for 24 months from the date of shipment. Contact your Brindlemoor dealer.",
        ]
    )
    return page


def page_specifications() -> Page:
    page = Page(2, "Specifications")
    page.table(
        [("Item", 520), ("Value", 575)],
        [
            ["Cooling capacity", "22 kW at 15 C leaving coolant, 35 C ambient"],
            ["Refrigerant", "R-513A"],
            ["Refrigerant charge", "4.6 kg"],
            ["Power supply", "400 V, 3 phase, 50 Hz"],
            ["Full load current", "18.5 A"],
            ["Coolant", "Water or up to 30 % propylene glycol"],
            ["Reservoir volume", "60 L"],
            ["Nominal coolant flow", "65 L/min"],
            ["Pump maximum pressure", "3.5 bar"],
            ["Setpoint range", "5 C to 30 C"],
            ["Sound level", "62 dB(A) at 1 m"],
            ["Weight, empty", "210 kg"],
            ["Dimensions (W x D x H)", "760 x 1150 x 1420 mm"],
        ],
    )
    page.para(
        [
            "Ambient operating range: 5 C to 43 C. Above 43 C ambient the controller",
            "reduces capacity and shows fault E04.",
        ]
    )
    return page


def page_control_panel() -> Page:
    page = Page(3, "Control panel")
    page.para(["Figure 3-1: front control panel. Numbers refer to the list below."])
    panel = (MARGIN + 60, 300, WIDTH - MARGIN - 60, 760)
    page.draw.rectangle(panel, outline=INK, width=4, fill=(245, 245, 245))
    page.draw.rectangle((260, 360, 640, 520), outline=INK, width=3, fill=(20, 60, 40))
    page.draw.text((450, 440), "15.0 C", font=_font(64), fill=(120, 240, 150), anchor="mm")
    for x, color in ((740, (40, 160, 70)), (840, (220, 160, 30)), (940, RED)):
        page.draw.ellipse((x - 24, 400, x + 24, 448), outline=INK, width=3, fill=color)
    for x in (300, 420, 540):
        page.draw.rectangle((x - 40, 600, x + 40, 660), outline=INK, width=3, fill="white")
    page.draw.ellipse((880, 570, 1000, 690), outline=INK, width=4, fill=RED)
    for center, number in (
        ((210, 440), "1"),
        ((740, 350), "2"),
        ((840, 350), "3"),
        ((940, 350), "4"),
        ((300, 720), "5"),
        ((420, 720), "6"),
        ((540, 720), "7"),
        ((1050, 630), "8"),
    ):
        page.callout(center, number)
    page.y = 810
    page.para(
        [
            "1  Display: leaving coolant temperature, or the fault code when a fault is active",
            "2  Green lamp: running",
            "3  Amber lamp: warning, the chiller keeps running",
            "4  Red lamp: fault, the compressor is stopped",
            "5  SET: hold 3 s to change the setpoint",
            "6  UP / DOWN: change the value shown",
            "7  RESET: clears a fault once its cause is gone",
            "8  Emergency stop: stops the pump, fan and compressor at once",
        ]
    )
    return page


def page_coolant_circuit() -> Page:
    page = Page(4, "Coolant circuit and valves")
    page.para(["Figure 4-1: coolant circuit. Arrows show the direction of flow."])
    page.box((140, 300, 420, 470), "Process\n(your machine)")
    page.box((140, 1000, 420, 1200), "Reservoir\n60 L")
    page.box((700, 1000, 960, 1200), "Pump P1")
    page.box((700, 560, 960, 760), "Evaporator\nHX-1")
    page.box((700, 300, 960, 440), "Strainer\nS1")
    page.pipe([(280, 470), (280, 1000)])
    page.pipe([(420, 1100), (700, 1100)])
    page.pipe([(830, 1000), (830, 760)])
    page.pipe([(830, 560), (830, 440)])
    page.pipe([(700, 370), (420, 370)])
    page.valve((560, 370), "V1", "process supply isolation")
    page.valve((280, 760), "V2", "process return isolation", vertical=True)
    page.pipe([(280, 1200), (280, 1300)], color=INK)
    page.valve((280, 1250), "V3", "reservoir drain (ball valve, 1/2 in)", vertical=True)
    page.pipe([(960, 1100), (1100, 1100), (1100, 660), (960, 660)], color=GREY)
    page.valve((1100, 880), "V4", "pump bypass", vertical=True)
    page.y = 1340
    page.para(
        [
            "Close V1 and V2 to isolate the chiller from the process without draining it.",
            "Keep V4 closed in normal operation.",
        ]
    )
    return page


def page_electrical() -> Page:
    page = Page(5, "Electrical connections")
    page.para(
        [
            "Figure 5-1: customer terminal block TB1, inside the electrical panel door.",
            "Use 0.75 to 1.5 mm2 wire. Low-voltage signals only on TB1.",
        ]
    )
    top = 380
    page.draw.rectangle((300, top, 520, top + 12 * 60), outline=INK, width=4, fill=PALE)
    labels = [
        ("1", "remote start/stop"),
        ("2", "remote start/stop"),
        ("3", "common alarm relay NO"),
        ("4", "common alarm relay COM"),
        ("5", "flow switch FS1"),
        ("6", "flow switch FS1"),
        ("7", "setpoint 4-20 mA +"),
        ("8", "setpoint 4-20 mA -"),
        ("9", "24 V DC +"),
        ("10", "0 V"),
        ("11", "spare"),
        ("12", "earth"),
    ]
    for index, (terminal, label) in enumerate(labels):
        y = top + 30 + index * 60
        page.draw.ellipse((395, y - 14, 423, y + 14), outline=INK, width=3, fill="white")
        page.text((340, y), terminal, 24, anchor="mm")
        page.draw.line((423, y, 640, y), fill=GREY, width=3)
        page.text((660, y), label, 22, anchor="lm")
    page.text((300, top - 40), "TB1", 30, BLUE)
    page.y = top + 12 * 60 + 50
    page.para(
        [
            "A closed dry contact across the remote start/stop pair runs the chiller; open it",
            "to stop. Remove the factory jumper before connecting a remote switch.",
        ]
    )
    return page


def page_faults() -> Page:
    page = Page(6, "Fault codes")
    page.para(["The display shows the active fault code. Press RESET once the cause is fixed."])
    page.table(
        [("Code", 110), ("Meaning", 430), ("What to do", 555)],
        [
            ["E01", "High refrigerant pressure", "Clean condenser coil; check fan"],
            ["E02", "Low refrigerant pressure", "Call a refrigeration technician"],
            ["E03", "Compressor overload", "Check supply voltage; call service"],
            ["E04", "Ambient temperature high", "Improve ventilation around unit"],
            ["E05", "Leaving coolant too cold", "Check setpoint; check glycol mix"],
            ["E06", "Leaving coolant too warm", "Check process heat load"],
            ["E07", "Low coolant flow", "Open V1 and V2; clean strainer S1"],
            ["E08", "Low reservoir level", "Top up coolant to the MAX mark"],
            ["E09", "Temperature sensor fault", "Check sensor plug T1; call service"],
            ["E10", "Phase loss or reversal", "Check 400 V supply phases"],
        ],
        size=20,
    )
    page.para(
        [
            "Faults E02, E03 and E09 need a service visit if they return after a reset.",
        ]
    )
    return page


def page_maintenance() -> Page:
    page = Page(7, "Maintenance schedule")
    page.para(["Lock out the main disconnect before any task marked with an asterisk (*)."])
    page.table(
        [("Interval", 230), ("Task", 865)],
        [
            ["Daily", "Check the reservoir level on the sight glass"],
            ["Daily", "Check for leaks around the pump and fittings"],
            ["Weekly", "Clean the strainer basket S1 * (see page 8)"],
            ["Monthly", "Clean the condenser coil with compressed air *"],
            ["Monthly", "Check glycol concentration with a refractometer"],
            ["Every 6 months", "Check pump seal for weeping *"],
            ["Every 6 months", "Tighten power terminals *"],
            ["Yearly", "Drain and replace the coolant *"],
            ["Yearly", "Refrigerant leak check by a certified technician"],
        ],
    )
    page.para(
        [
            "Record each task, the date and your initials in the maintenance log at the",
            "back of the electrical panel door.",
        ]
    )
    return page


def page_strainer(revision: str = "C", torque: str = "20 N m") -> Page:
    page = Page(8, "Strainer basket replacement", revision)
    page.para(["Figure 8-1: strainer S1, exploded view."])
    cx = 380
    page.draw.rectangle((cx - 120, 300, cx + 120, 350), outline=INK, width=3, fill=PALE)
    page.draw.rectangle((cx - 90, 420, cx + 90, 640), outline=INK, width=3, fill="white")
    for y in range(440, 640, 25):
        page.draw.line((cx - 85, y, cx + 85, y), fill=GREY, width=1)
    page.draw.ellipse((cx - 100, 700, cx + 100, 730), outline=INK, width=4)
    page.draw.rectangle((cx - 130, 790, cx + 130, 1000), outline=INK, width=4, fill=PALE)
    for center, number, label in (
        ((cx + 200, 325), "A", "cover, 4 x M8 bolts"),
        ((cx + 200, 530), "B", "basket, 60 mesh, part BTS-2207"),
        ((cx + 200, 715), "C", "O-ring 75 x 3 mm, part BTS-2215"),
        ((cx + 200, 895), "D", "strainer housing"),
    ):
        page.callout(center, number)
        page.text((center[0] + 35, center[1]), label, 21, anchor="lm")
    page.y = 1060
    page.para(
        [
            "1. Stop the chiller and lock out the main disconnect.",
            "2. Close V1 and V2 (page 4) and let the pressure fall to zero.",
            "3. Remove the four cover bolts and lift off the cover.",
            "4. Pull out the basket. Rinse it, or fit a new basket BTS-2207.",
            "5. Fit a new O-ring BTS-2215 every time the cover is opened.",
            f"6. Refit the cover and tighten the bolts to {torque} in a cross pattern.",
            "7. Open V1 and V2, start the chiller and check for leaks.",
        ]
    )
    return page


BUILDERS = (
    page_contents,
    page_specifications,
    page_control_panel,
    page_coolant_circuit,
    page_electrical,
    page_faults,
    page_maintenance,
    page_strainer,
)


def page_strainer_revision_b() -> Page:
    """Page 8 of the older revision B, which the site keeps for its unit that still runs on it."""
    return page_strainer(revision="B", torque="25 N m")


def build() -> list[Page]:
    pages = [builder() for builder in BUILDERS]
    if [page.number for page in pages] != list(range(1, PAGES + 1)):
        raise AssertionError("page numbers must run 1..PAGES")
    return [*pages, page_strainer_revision_b()]


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    pages = build()
    pages[0].image.save(
        DATA_DIR / "manual.pdf",
        format="PDF",
        save_all=True,
        append_images=[page.image for page in pages[1:]],
        resolution=DPI,
        creationDate=FIXED_DATE,
        modDate=FIXED_DATE,
    )
    source = {
        "manual": MANUAL,
        "maker": MAKER,
        "pdf": "manual.pdf",
        "pages": [
            {
                "page": index,
                "printed_page": page.number,
                "revision": page.revision,
                "title": page.title,
                "text": page.lines,
            }
            for index, page in enumerate(pages, start=1)
        ],
    }
    (DATA_DIR / "manual.json").write_text(
        json.dumps(source, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(pages)} pages to {DATA_DIR}")


if __name__ == "__main__":
    main()
