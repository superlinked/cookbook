"""Write the synthetic KYC cases in data/: the onboarding system's seed and the scanned documents.

Everything is invented and every document page is stamped SPECIMEN. The identity documents are
issued by "Utopia" (code UTO), the fictional state ICAO Doc 9303 uses for its own specimens;
every person, utility, phone company, address and number is made up. The MRZ lines follow ICAO
9303 (TD1 for the ID cards, TD3 for the passport) with real check digits, except where a case
plants a bad one.

    uv run make_data.py

writes the onboarding system's seed, data/onboarding_cases.csv (one application form per case,
as the customer filled it in) and data/onboarding_documents.csv (one row per uploaded document:
its purpose, the type the customer said it is, and where its scan is), and two image-only PDFs,
id-documents.pdf and address-documents.pdf. The PDFs carry no document metadata. Rerunning
rebuilds every file byte for byte.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from recipe import check_digit

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"

DPI = 150
STATE = "UTO"
MONTHS = [
    *("January", "February", "March", "April", "May", "June"),
    *("July", "August", "September", "October", "November", "December"),
]
INK = (25, 25, 30)
RED = (190, 30, 30)
RECEIVED = date(2026, 10, 26)
ID_PDF, ADDRESS_PDF = "id-documents.pdf", "address-documents.pdf"
FORM_FIELDS = (
    *("surname", "given_names", "date_of_birth", "flat", "house_number", "street", "town"),
    *("postcode", "nationality", "occupation", "tax_residency"),
)


@dataclass(frozen=True)
class Person:
    surname: str
    given: str
    sex: str
    dob: date
    document_number: str
    issued: date
    expiry: date
    address: str = ""  # printed on ID cards only
    place_of_birth: str = "PORT ELLERY"


@dataclass(frozen=True)
class Home:
    """The address as the customer typed it on the application form."""

    flat: str
    house_number: str
    street: str
    town: str
    postcode: str


@dataclass(frozen=True)
class Bill:
    issuer: str
    subtitle: str
    holder: str
    address: str
    statement: date
    due: date
    account: str
    amount: str
    holder_label: str = "Account holder"
    address_label: str = "Service address"
    date_label: str = "Statement date"
    extra_line: str = ""  # e.g. the mobile number on a phone bill
    colour: tuple[int, int, int] = (28, 70, 110)


@dataclass(frozen=True)
class Case:
    case: str
    person: Person
    home: Home
    occupation: str
    bills: tuple[Bill, ...]
    passport: bool = False
    # The date of birth the customer typed on the form, when it isn't the one on the ID.
    form_dob: date | None = None
    bad_dob_check_digit: bool = False  # plant a wrong MRZ check digit
    # Documents the customer gave as proof of address, when it's the ID card itself.
    id_as_address: bool = False


def kestrel(holder: str, address: str, statement: date, account: str, amount: str) -> Bill:
    return Bill(
        "Kestrel Energy",
        "Electricity and gas",
        holder,
        address,
        statement,
        statement + timedelta(days=21),
        account,
        amount,
    )


def brookline(holder: str, address: str, statement: date, account: str, amount: str) -> Bill:
    return Bill(
        "Brookline Water Co.",
        "Water and sewerage",
        holder,
        address,
        statement,
        statement + timedelta(days=21),
        account,
        amount,
        colour=(20, 110, 120),
    )


CASES = (
    Case(
        "kyc-01",
        Person(
            "MARLOWE",
            "EVELYN ROSE",
            "F",
            date(1987, 3, 14),
            "UX3804172",
            date(2021, 5, 31),
            date(2031, 5, 30),
            "27 LARCHMERE AVENUE APT 3, EASTBROOK UT 40217",
        ),
        Home("3", "27", "Larchmere Avenue", "Eastbrook", "UT 40217"),
        "Pharmacist",
        (
            kestrel(
                "Evelyn Rose Marlowe",
                "27 Larchmere Avenue, Apt 3, Eastbrook, UT 40217",
                date(2026, 9, 18),
                "KE-55102-874",
                "$96.40",
            ),
        ),
    ),
    Case(
        "kyc-02",
        Person(
            "OKAFOR-BELL",
            "TOBIAS JAMES",
            "M",
            date(1979, 11, 2),
            "UX5521906",
            date(2022, 2, 14),
            date(2032, 2, 13),
            "114 HARBOUR ROAD FLAT 2, PORT ELLERY UT 40381",
        ),
        Home("2", "114", "Harbour Road", "Port Ellery", "UT 40381"),
        "Marine engineer",
        (
            brookline(
                "Mr Tobias Okafor-Bell",
                "Flat 2, 114 Harbour Rd, Port Ellery, UT 40381",
                date(2026, 7, 29),
                "BW-208833",
                "$41.15",
            ),
        ),
    ),
    Case(
        "kyc-03",
        Person(
            "HALVORSEN",
            "INGRID MAEVE",
            "F",
            date(1965, 6, 21),
            "UX1187340",
            date(2016, 9, 1),
            date(2026, 8, 31),
            "9 CINDERFORD CLOSE, WESTMARCH UT 40566",
        ),
        Home("", "9", "Cinderford Close", "Westmarch", "UT 40566"),
        "Retired",
        (
            kestrel(
                "Ingrid Halvorsen",
                "9 Cinderford Close, Westmarch, UT 40566",
                date(2026, 10, 2),
                "KE-55117-302",
                "$128.92",
            ),
        ),
    ),
    Case(
        "kyc-04",
        Person(
            "DESROSIERS",
            "PAUL ANTON",
            "M",
            date(1992, 8, 5),
            "UX6630418",
            date(2023, 4, 3),
            date(2033, 4, 2),
            "301 QUAYSIDE TERRACE, PORT ELLERY UT 40388",
        ),
        Home("", "301", "Quayside Terrace", "Port Ellery", "UT 40388"),
        "Software developer",
        (
            brookline(
                "Paul Desrosiers",
                "301 Quayside Terrace, Port Ellery, UT 40388",
                date(2026, 9, 30),
                "BW-211470",
                "$37.60",
            ),
        ),
        bad_dob_check_digit=True,
    ),
    Case(
        "kyc-05",
        Person(
            "NAKASHIMA",
            "REI",
            "F",
            date(1998, 1, 27),
            "UX7042285",
            date(2024, 1, 15),
            date(2034, 1, 14),
            "56 ORCHARD ROW, EASTBROOK UT 40219",
        ),
        Home("", "56", "Orchard Row", "Eastbrook", "UT 40219"),
        "Graphic designer",
        (
            kestrel(
                "Rei Nakashima",
                "56 Orchard Row, Eastbrook, UT 40219",
                date(2026, 5, 12),
                "KE-55190-415",
                "$72.08",
            ),
        ),
    ),
    Case(
        "kyc-06",
        Person(
            "FERREIRA LOPES",
            "ANA BEATRIZ",
            "F",
            date(1984, 12, 9),
            "UX2958763",
            date(2020, 7, 20),
            date(2030, 7, 19),
            "8 QUILLON STREET, WESTMARCH UT 40561",
        ),
        Home("", "8", "Quillon Street", "Westmarch", "UT 40561"),
        "Nurse",
        (
            brookline(
                "Ana Beatriz Ferreira Lopes",
                "18 Quillon Street, Westmarch, UT 40561",
                date(2026, 10, 7),
                "BW-219006",
                "$52.33",
            ),
        ),
    ),
    Case(
        "kyc-07",
        Person(
            "BRANNIGAN",
            "CORMAC",
            "M",
            date(1975, 4, 30),
            "UX4419027",
            date(2019, 11, 11),
            date(2029, 11, 10),
            "71 ELDERWOOD LANE, EASTBROOK UT 40214",
        ),
        Home("", "71", "Elderwood Lane", "Eastbrook", "UT 40214"),
        "Electrician",
        (
            kestrel(
                "Siobhan Brannigan",
                "71 Elderwood Lane, Eastbrook, UT 40214",
                date(2026, 9, 25),
                "KE-55233-061",
                "$110.75",
            ),
        ),
    ),
    Case(
        "kyc-08",
        Person(
            "ADEYEMI",
            "FOLASADE GRACE",
            "F",
            date(1988, 3, 4),
            "PU1940387",
            date(2022, 6, 6),
            date(2032, 6, 5),
        ),
        Home("", "63", "Saltmarsh Road", "Westmarch", "UT 40558"),
        "Accountant",
        (
            kestrel(
                "Folasade Grace Adeyemi",
                "63 Saltmarsh Road, Westmarch, UT 40558",
                date(2026, 9, 12),
                "KE-80412-117",
                "$88.06",
            ),
        ),
        passport=True,
        form_dob=date(1988, 4, 3),
    ),
    Case(
        "kyc-09",
        Person(
            "KOWALCZYK",
            "MAREK",
            "M",
            date(1971, 4, 17),
            "UX2275509",
            date(2021, 10, 18),
            date(2031, 10, 17),
            "114 HARBOUR ROAD FLAT 2, PORT ELLERY UT 40381",
        ),
        Home("2", "114", "Harbour Road", "Port Ellery", "UT 40381"),
        "Welder",
        (
            brookline(
                "Marek Kowalczyk",
                "Flat 114, 2 Harbour Road, Port Ellery, UT 40381",
                date(2026, 10, 1),
                "BW-300958",
                "$44.50",
            ),
        ),
    ),
    Case(
        "kyc-10",
        Person(
            "ABERNETHY",
            "HOLLIS DEAN",
            "M",
            date(1990, 9, 23),
            "UX8810364",
            date(2024, 3, 9),
            date(2034, 3, 8),
            "45 GANNET WAY, EASTBROOK UT 40222",
        ),
        Home("", "45", "Gannet Way", "Eastbrook", "UT 40222"),
        "Teacher",
        (
            kestrel(
                "Hollis Abernethy",
                "45 Gannet Way, Eastbrook, UT 40222",
                date(2026, 10, 3),
                "KE-55301-288",
                "$64.12",
            ),
            brookline(
                "Hollis Dean Abernethy",
                "12 Mill Lane, Westmarch, UT 40563",
                date(2026, 10, 5),
                "BW-301442",
                "$29.80",
            ),
        ),
    ),
    Case(
        "kyc-11",
        Person(
            "LINDQVIST",
            "ASTRID JOHANNA",
            "F",
            date(1982, 2, 11),
            "UX9136620",
            date(2023, 8, 21),
            date(2033, 8, 20),
            "17 HERON CLOSE, WESTMARCH UT 40564",
        ),
        Home("", "17", "Heron Close", "Westmarch", "UT 40564"),
        "",
        (),
        id_as_address=True,
    ),
    Case(
        "kyc-12",
        Person(
            "MENDOZA RUIZ",
            "CARLOS ANDRES",
            "M",
            date(1995, 6, 30),
            "UX3407718",
            date(2025, 1, 20),
            date(2035, 1, 19),
            "9 PELICAN CRESCENT, PORT ELLERY UT 40385",
        ),
        Home("", "9", "Pelican Crescent", "Port Ellery", "UT 40385"),
        "Chef",
        (
            Bill(
                "Larkspur Mobile",
                "Your mobile phone bill",
                "Carlos Mendoza Ruiz",
                "9 Pelican Crescent, Port Ellery, UT 40385",
                date(2026, 10, 9),
                date(2026, 10, 23),
                "LM-7730158",
                "$35.00",
                address_label="Billing address",
                date_label="Bill date",
                extra_line="Mobile number: 555-0187",
                colour=(96, 40, 120),
            ),
        ),
    ),
)


# --- MRZ (ICAO 9303) --------------------------------------------------------------------------


def _yymmdd(day: date) -> str:
    return day.strftime("%y%m%d")


def _mrz_name(person: Person, width: int) -> str:
    surname = person.surname.replace(" ", "<").replace("-", "<")
    return (surname + "<<" + person.given.replace(" ", "<")).ljust(width, "<")[:width]


def _wrong(digit: str) -> str:
    return str((int(digit) + 3) % 10)


def mrz_lines(case: Case) -> list[str]:
    person = case.person
    dob = _yymmdd(person.dob)
    dob_cd = check_digit(dob)
    if case.bad_dob_check_digit:
        dob_cd = _wrong(dob_cd)
    expiry = _yymmdd(person.expiry)
    expiry_cd = check_digit(expiry)
    number = person.document_number
    number_cd = check_digit(number)
    if case.passport:  # TD3: two lines of 44
        line1 = "P<" + STATE + _mrz_name(person, 39)
        line2 = (
            number + number_cd + STATE + dob + dob_cd + person.sex + expiry + expiry_cd
            + "<" * 14 + "<"
        )  # fmt: skip
        return [line1, line2 + check_digit(line2[0:10] + line2[13:20] + line2[21:43])]
    # TD1: three lines of 30. The composite is computed over the planted digits, so only the
    # planted field's own check digit fails.
    line1 = "I<" + STATE + number + number_cd + "<" * 15
    line2 = dob + dob_cd + person.sex + expiry + expiry_cd + STATE + "<" * 11
    line2 += check_digit(line1[5:30] + line2[0:7] + line2[8:15] + line2[18:29])
    return [line1, line2, _mrz_name(person, 30)]


# --- Drawing ----------------------------------------------------------------------------------


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)  # type: ignore[return-value]


def _id_date(day: date) -> str:
    return f"{day.day:02d} {MONTHS[day.month - 1][:3].upper()} {day.year}"


def _long_date(day: date) -> str:
    return f"{day.day} {MONTHS[day.month - 1]} {day.year}"


def _stamp(draw: ImageDraw.ImageDraw, xy: tuple[int, int], size: int) -> None:
    font = _font(size)
    box = draw.textbbox(xy, "SPECIMEN", font=font)
    pad = size // 4
    draw.rectangle(
        (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad),
        outline=RED,
        width=max(3, pad // 2),
    )
    draw.text(xy, "SPECIMEN", font=font, fill=RED)


def _mrz(draw: ImageDraw.ImageDraw, lines: list[str], x: int, y: int, size: int) -> None:
    """Draw MRZ lines in fixed-width cells, as the OCR-B font on a real document would."""
    font, cell = _font(size), round(size * 0.8)
    for row, line in enumerate(lines):
        for col, char in enumerate(line):
            width = draw.textlength(char, font=font)
            draw.text(
                (x + col * cell + (cell - width) / 2, y + row * size * 1.45),
                char,
                font=font,
                fill=INK,
            )


def _field(draw: ImageDraw.ImageDraw, x: int, y: int, label: str, value: str) -> None:
    draw.text((x, y), label, font=_font(22), fill=(90, 90, 110))
    draw.text((x, y + 28), value, font=_font(34), fill=INK)


def _photo(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int) -> None:
    draw.rectangle((x, y, x + w, y + h), fill=(200, 205, 215), outline=(140, 140, 150), width=2)
    cx = x + w // 2
    draw.ellipse(
        (cx - w // 5, y + h // 6, cx + w // 5, y + h // 6 + 2 * w // 5), fill=(160, 165, 180)
    )
    draw.pieslice(
        (x + w // 8, y + h // 2 + 20, x + w - w // 8, y + h + h // 3),
        180,
        360,
        fill=(160, 165, 180),
    )


def draw_id_card(case: Case) -> Image.Image:
    person = case.person
    page = Image.new("RGB", (1700, 1160), "white")
    draw = ImageDraw.Draw(page)
    x0, y0, w, h = 100, 100, 1500, 946
    draw.rounded_rectangle(
        (x0, y0, x0 + w, y0 + h), radius=40, fill=(236, 240, 246), outline=(120, 130, 150), width=3
    )
    draw.text((x0 + 50, y0 + 36), "REPUBLIC OF UTOPIA", font=_font(40), fill=(30, 60, 120))
    draw.text((x0 + 50, y0 + 88), "IDENTITY CARD", font=_font(30), fill=(30, 60, 120))
    _stamp(draw, (x0 + 1050, y0 + 46), 52)
    _photo(draw, x0 + 50, y0 + 160, 300, 380)
    fx = x0 + 400
    _field(draw, fx, y0 + 160, "Surname", person.surname)
    _field(draw, fx, y0 + 240, "Given names", person.given)
    _field(draw, fx, y0 + 320, "Date of birth", _id_date(person.dob))
    _field(draw, fx + 420, y0 + 320, "Sex", person.sex)
    _field(draw, fx + 600, y0 + 320, "Nationality", STATE)
    _field(draw, fx, y0 + 400, "Document no.", person.document_number)
    _field(draw, fx + 420, y0 + 400, "Date of expiry", _id_date(person.expiry))
    _field(draw, fx, y0 + 480, "Address", person.address)
    _mrz(draw, mrz_lines(case), x0 + 60, y0 + 640, 44)
    return page


def draw_passport(case: Case) -> Image.Image:
    person = case.person
    page = Image.new("RGB", (1700, 1200), "white")
    draw = ImageDraw.Draw(page)
    x0, y0, w, h = 80, 80, 1540, 1040
    draw.rectangle((x0, y0, x0 + w, y0 + h), fill=(240, 236, 228), outline=(130, 120, 110), width=3)
    draw.text(
        (x0 + 50, y0 + 34), "REPUBLIC OF UTOPIA  ·  PASSPORT", font=_font(40), fill=(110, 30, 40)
    )
    _stamp(draw, (x0 + 1120, y0 + 40), 50)
    _photo(draw, x0 + 50, y0 + 140, 320, 410)
    fx = x0 + 420
    _field(draw, fx, y0 + 120, "Type", "P")
    _field(draw, fx + 180, y0 + 120, "Code", STATE)
    _field(draw, fx + 420, y0 + 120, "Passport No.", person.document_number)
    _field(draw, fx, y0 + 200, "Surname", person.surname)
    _field(draw, fx, y0 + 280, "Given names", person.given)
    _field(draw, fx, y0 + 360, "Nationality", "UTOPIAN")
    _field(draw, fx + 420, y0 + 360, "Sex", person.sex)
    _field(draw, fx, y0 + 440, "Date of birth", _id_date(person.dob))
    _field(draw, fx + 420, y0 + 440, "Place of birth", person.place_of_birth)
    _field(draw, fx, y0 + 520, "Date of issue", _id_date(person.issued))
    _field(draw, fx + 420, y0 + 520, "Date of expiry", _id_date(person.expiry))
    _field(draw, fx, y0 + 600, "Authority", "MINISTRY OF THE INTERIOR")
    _mrz(draw, mrz_lines(case), x0 + 60, y0 + 820, 40)
    return page


def _page_lines(draw: ImageDraw.ImageDraw, x: int, y: int, lines: list[tuple[str, int]]) -> int:
    for text, size in lines:
        draw.text((x, y), text, font=_font(size), fill=INK)
        y += round(size * 1.6)
    return y


def draw_bill(bill: Bill) -> Image.Image:
    page = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(page)
    draw.rectangle((0, 0, 1240, 150), fill=bill.colour)
    draw.text((80, 45), bill.issuer, font=_font(52), fill="white")
    _stamp(draw, (900, 210), 44)
    period_start = date(bill.statement.year, bill.statement.month, 1)
    lines = [
        (bill.subtitle, 30),
        ("Customer service 555-0142  ·  PO Box 812, Eastbrook, UT 40210", 22),
        ("", 22),
        (bill.holder, 30),
        (bill.address, 30),
        ("", 22),
        (f"{bill.holder_label}: {bill.holder}", 28),
        (f"{bill.address_label}: {bill.address}", 28),
        (f"Account number: {bill.account}", 28),
    ]
    if bill.extra_line:
        lines.append((bill.extra_line, 28))
    lines += [
        (f"{bill.date_label}: {_long_date(bill.statement)}", 28),
        (f"Billing period: {_long_date(period_start)} to {_long_date(bill.statement)}", 28),
    ]
    y = _page_lines(draw, 80, 200, lines) + 30
    draw.line((80, y, 1160, y), fill=INK, width=2)
    rows = [
        ("Previous balance", "$0.00"),
        ("Payment received - thank you", "$0.00"),
        ("Charges this period", bill.amount),
    ]
    y += 30
    for label, amount in rows:
        draw.text((80, y), label, font=_font(28), fill=INK)
        draw.text(
            (1160 - draw.textlength(amount, font=_font(28)), y), amount, font=_font(28), fill=INK
        )
        y += 50
    draw.rectangle((80, y + 20, 1160, y + 150), outline=INK, width=3)
    draw.text((110, y + 45), f"Amount due: {bill.amount}", font=_font(34), fill=INK)
    draw.text((110, y + 95), f"Due date: {_long_date(bill.due)}", font=_font(30), fill=INK)
    _page_lines(
        draw,
        80,
        y + 220,
        [
            ("This specimen bill was generated for a software demo. It is not a real bill.", 22),
            ("Pay online, by phone or by mail. Keep this page for your records.", 22),
        ],
    )
    return page


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


def form_row(case: Case) -> list[str]:
    person, home = case.person, case.home
    dob = case.form_dob or person.dob
    return [
        *(case.case, RECEIVED.isoformat(), person.surname.title(), person.given.title()),
        *(dob.isoformat(), home.flat, home.house_number, home.street, home.town, home.postcode),
        *("Utopian", case.occupation, "Utopia"),
    ]


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    ids: list[Image.Image] = []
    bills: list[Image.Image] = []
    documents: list[list[str]] = []
    for case in CASES:
        ids.append(draw_passport(case) if case.passport else draw_id_card(case))
        id_type = "passport" if case.passport else "id_card"
        id_row = [f"{case.case}-id", id_type, ID_PDF, str(len(ids))]
        documents.append([case.case, id_row[0], "identity", *id_row[1:]])
        if case.id_as_address:
            documents.append([case.case, id_row[0], "address", *id_row[1:]])
        for number, bill in enumerate(case.bills, start=1):
            bills.append(draw_bill(bill))
            name = f"{case.case}-poa" + (str(number) if len(case.bills) > 1 else "")
            documents.append(
                [case.case, name, "address", "utility_bill", ADDRESS_PDF, str(len(bills))]
            )
    _save_pdf(DATA_DIR / ID_PDF, ids)
    _save_pdf(DATA_DIR / ADDRESS_PDF, bills)
    with (DATA_DIR / "onboarding_cases.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["case", "received", *FORM_FIELDS])
        writer.writerows(form_row(case) for case in CASES)
    with (DATA_DIR / "onboarding_documents.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["case", "document", "purpose", "declared_type", "file", "page"])
        writer.writerows(documents)
    print(f"wrote {len(CASES)} cases and {len(documents)} document rows to {DATA_DIR}")


if __name__ == "__main__":
    main()
