"""Write the synthetic inputs for agent.py in data/agent/. Nothing in data/ changes.

Everything is invented, in the same made-up world as make_data.py (the fictional state of
Utopia, SPECIMEN scans). It writes:

- onboarding_cases.csv and onboarding_documents.csv: the onboarding system's seed for kyc-13 (a
  customer who just moved) and sixteen cases from 2025, k25-001 to k25-016. The
  documents file has one more column than data/'s: `received`, the date each document arrived.
- id-documents.pdf (17 ID cards) and address-documents.pdf (18 pages: kyc-13's tenancy agreement,
  one address document per 2025 case, and k25-007's later water bill). Image-only, no metadata.
- accounts.csv: the account register for the 2025 cases.
- case_notes.csv: free-text notes the onboarding team left on the 2025 cases.

The handbook (cdd_handbook.md), the analyst's scripted turns (chat_kyc-13.yaml) and the
regulator's request (regulator_2025.txt) are hand-written files in data/agent/, not generated.

    uv run make_agent_data.py

Rerunning rebuilds every file byte for byte.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw

import make_data
from make_data import Bill, Case, Home, Person, brookline, kestrel

ROOT = Path(__file__).resolve().parent
AGENT_DIR = ROOT / "data" / "agent"
ID_PDF, ADDRESS_PDF = make_data.ID_PDF, make_data.ADDRESS_PDF
INK = make_data.INK


@dataclass(frozen=True)
class Tenancy:
    """A tenancy agreement's first page, as a letting agent prints it."""

    agent: str
    tenant: str
    address: str
    agreed: date
    starts: date
    rent: str
    landlord: str


@dataclass(frozen=True)
class AgentCase:
    case: Case
    received: date
    declared: str  # the type the customer gave the address document
    tax_residency: str = "Utopia"
    tenancy: Tenancy | None = None
    later_bill: Bill | None = None  # a second address document, sent later
    later_received: date | None = None


def larkspur(holder: str, address: str, statement: date, account: str, number: str) -> Bill:
    return Bill(
        "Larkspur Mobile",
        "Your mobile phone bill",
        holder,
        address,
        statement,
        statement + timedelta(days=14),
        account,
        "$32.00",
        address_label="Billing address",
        date_label="Bill date",
        extra_line=f"Mobile number: {number}",
        colour=(96, 40, 120),
    )


def person(surname: str, given: str, sex: str, dob: date, number: str, issued: date) -> Person:
    return Person(
        surname.upper(),
        given.upper(),
        sex,
        dob,
        number,
        issued,
        issued + timedelta(days=3652),
    )


def k25(
    n: int,
    surname: str,
    given: str,
    sex: str,
    dob: date,
    home: Home,
    occupation: str,
    received: date,
    bill_kind: str,
    declared: str = "utility_bill",
    tax_residency: str = "Utopia",
) -> AgentCase:
    """One 2025 case: an ID card at the form's address and one address document, dated 20 days
    before the case was received, at that address and in the customer's name."""
    name = f"k25-{n:03d}"
    flat = f"Flat {home.flat}, " if home.flat else ""
    address = f"{flat}{home.house_number} {home.street}, {home.town}, {home.postcode}"
    printed = f"{home.house_number} {home.street}{' FLAT ' + home.flat if home.flat else ''}"
    who = person(surname, given, sex, dob, f"UX{n:02d}{dob:%m%d}{n % 10}", date(2022, 3, n))
    who = Person(**{**vars(who), "address": f"{printed}, {home.town} {home.postcode}".upper()})
    holder = f"{given} {surname}"
    statement = received - timedelta(days=20)
    account = f"{n:03d}-{dob:%y%m}"
    bill = {
        "kestrel": lambda: kestrel(holder, address, statement, f"KE-25{account}", "$71.20"),
        "brookline": lambda: brookline(holder, address, statement, f"BW-25{account}", "$38.45"),
        "larkspur": lambda: larkspur(
            holder, address, statement, f"LM-25{account}", f"555-01{n:02d}"
        ),
    }[bill_kind]()
    case = Case(name, who, home, occupation, (bill,))
    return AgentCase(case, received, declared, tax_residency)


MOVER = AgentCase(
    Case(
        "kyc-13",
        Person(
            "OKONKWO-REYES",
            "NADIA",
            "F",
            date(1993, 7, 8),
            "UX6152847",
            date(2023, 5, 2),
            date(2033, 5, 1),
            "7 COPPER BEECH ROAD, EASTBROOK UT 40215",
        ),
        Home("4", "22", "Mallard Wharf", "Port Ellery", "UT 40390"),
        "Physiotherapist",
        (),
    ),
    date(2026, 11, 9),
    "tenancy_agreement",
    tax_residency="tbc",
    tenancy=Tenancy(
        "Saltgate Lettings",
        "Nadia Okonkwo-Reyes",
        "Flat 4, 22 Mallard Wharf, Port Ellery, UT 40390",
        date(2026, 10, 19),
        date(2026, 10, 20),
        "$1,150.00",
        "Harbourline Property Ltd",
    ),
)

# n | surname | given | sex | born | flat | house | street | town | postcode | occupation |
# received | address document issuer | declared type | tax residency
CASE_TABLE = """
1|Ashworth|Helen|F|1984-02-09||14|Fennel Lane|Eastbrook|UT 40213|Librarian|2025-01-08|kestrel|utility_bill|Utopia
2|Mbeki|Joseph|M|1979-06-22||3|Tanner Row|Westmarch|UT 40560|Plumber|2025-01-21|brookline|utility_bill|Utopia
3|Castellanos|Lucia|F|2005-09-14||2|Collegiate Court|Eastbrook|UT 40220|Student|2025-02-11|larkspur|mobile_phone_bill|
4|Brennan|Patrick|M|1968-11-30||88|Kiln Street|Port Ellery|UT 40384|Bus driver|2025-02-19|kestrel|utility_bill|Utopia
5|Sorensen|Maja|F|1990-04-03|1|40|Gull Terrace|Port Ellery|UT 40386|Pharmacist|2025-03-04|kestrel|utility_bill|Utopia
6|Iyer|Arjun|M|1987-08-17||19|Rowan Drive|Westmarch|UT 40562|Civil engineer|2025-03-12|brookline|utility_bill|Utopia
7|Delacroix|Simone|F|1996-01-25||61|Wren Close|Eastbrook|UT 40216|Barista|2025-02-26|larkspur|mobile_phone_bill|Utopia
8|Okoro|Daniel|M|1982-12-01||7|Saddler Way|Westmarch|UT 40559|Accountant|2025-04-15|brookline|utility_bill|Utopia
9|Varga|Tamas|M|1975-05-11|3|12|Ferry Road|Port Ellery|UT 40382|Chef|2025-05-06|larkspur|mobile_phone_bill|Utopia
10|Whitlock|Grace|F|1999-10-19||25|Linnet Avenue|Eastbrook|UT 40218|Nurse|2025-06-03|kestrel|utility_bill|Utopia
11|Haddad|Omar|M|1991-03-28||50|Cooper Street|Westmarch|UT 40565|Electrician|2025-07-09|larkspur|utility_bill|Utopia
12|Lindgren|Elsa|F|1986-07-04||9|Marten Lane|Eastbrook|UT 40212|Teacher|2025-08-20|brookline|utility_bill|Utopia
13|Achterberg|Willem|M|1972-09-09|2|31|Quay Street|Port Ellery|UT 40383|Ship's officer|2025-09-16|kestrel|utility_bill|Utopia
14|Mwangi|Ruth|F|1980-01-15||4|Orchard Row|Eastbrook|UT 40219|Company director|2025-10-07|larkspur|mobile_phone_bill|Utopia
15|Novak|Petra|F|1994-05-27||77|Heron Close|Westmarch|UT 40564|Graphic designer|2025-11-12|brookline|utility_bill|Utopia
16|Fairbairn|Callum|M|1988-02-02||16|Pelican Crescent|Port Ellery|UT 40385|Software developer|2025-12-18|larkspur|mobile_phone_bill|Utopia
"""  # noqa: E501


def case_2025(line: str) -> AgentCase:
    n, surname, given, sex, born, flat, house, street, town, postcode, *rest = line.split("|")
    occupation, received, issuer, declared, tax_residency = rest
    return k25(
        int(n),
        surname,
        given,
        sex,
        date.fromisoformat(born),
        Home(flat, house, street, town, postcode),
        occupation,
        date.fromisoformat(received),
        issuer,
        declared,
        tax_residency,
    )


CASES_2025 = tuple(case_2025(line) for line in CASE_TABLE.strip().splitlines())

# k25-007 sent a water bill five weeks after the mobile bill. It's dated before the case was
# received, so it is within three months of the case date.
_K7 = CASES_2025[6]
CASES_2025 = (
    *CASES_2025[:6],
    AgentCase(
        _K7.case,
        _K7.received,
        _K7.declared,
        _K7.tax_residency,
        later_bill=brookline(
            "Simone Delacroix",
            "61 Wren Close, Eastbrook, UT 40216",
            date(2025, 2, 12),
            "BW-25007-9601",
            "$27.90",
        ),
        later_received=_K7.received + timedelta(days=35),
    ),
    *CASES_2025[7:],
)
ALL_CASES = (MOVER, *CASES_2025)

# account, case, product, opened, status, status_date
ACCOUNTS = (
    ("AC-25001", "k25-001", "personal", "2025-01-13", "active", ""),
    ("AC-25002", "k25-002", "personal", "2025-01-24", "active", ""),
    ("AC-25003", "k25-003", "personal", "2025-02-14", "active", ""),
    ("AC-25004", "k25-004", "personal", "2025-02-21", "active", ""),
    ("AC-25005", "k25-005", "personal", "2025-03-07", "active", ""),
    ("AC-25006", "k25-006", "personal", "2025-03-14", "active", ""),
    ("AC-25007", "k25-007", "personal", "2025-02-28", "active", ""),
    ("AC-25008", "k25-008", "personal", "2025-04-17", "active", ""),
    ("AC-25009", "k25-009", "personal", "2025-05-09", "closed", "2025-08-14"),
    ("AC-25010", "k25-010", "personal", "2025-06-05", "active", ""),
    ("AC-25011", "k25-011", "personal", "2025-07-11", "active", ""),
    ("AC-25012", "k25-012", "personal", "2025-08-22", "restricted", "2025-10-02"),
    ("AC-25013", "k25-013", "personal", "2025-09-18", "active", ""),
    ("AC-25014", "k25-014", "business", "2025-10-10", "active", ""),
    ("AC-25015", "k25-015", "personal", "2025-11-14", "active", ""),
    ("AC-25016", "k25-016", "personal", "2026-01-06", "active", ""),
)

# note, case, date, author_role, text
NOTES_CSV = """
N-0102,k25-001,2025-01-09,analyst,ID and energy bill checked against the form. No issues.
N-0131,k25-002,2025-01-22,analyst,Water bill checked. Customer prefers post to email.
N-0309,k25-003,2025-02-11,analyst,Customer lives in university halls and has no bills in her own name. Uploaded a mobile phone bill instead. Referred to team lead.
N-0311,k25-003,2025-02-13,team_lead,"Exception approved by team lead: student in university halls, no bills in own name; follow up for bank statement in 90 days"
N-0322,k25-004,2025-02-20,analyst,Energy bill fine. Customer asked about overdraft; passed to lending.
N-0335,k25-007,2025-02-27,analyst,"Mobile phone bill not accepted as proof of address (handbook 3.2). Emailed the customer asking for a utility bill, bank statement or council tax bill."
N-0340,k25-007,2025-02-28,team_lead,Account opened with proof of address outstanding; chase in 6 weeks.
N-0402,k25-005,2025-03-04,analyst,"customer asked if a mobile bill would do; told no, sent energy bill"
N-0405,k25-007,2025-04-02,analyst,Water bill received by post and scanned (k25-007-poa2). Proof of address now on file.
N-0417,k25-006,2025-03-13,analyst,Water bill checked. No flat number; it's a house.
N-0466,k25-008,2025-04-16,analyst,ID checked in branch.
N-0512,k25-009,2025-05-07,analyst,"Customer asked for an exception: no bills in own name, lives with partner. Mobile phone bill uploaded. Exception requested from team lead."
N-0519,k25-009,2025-05-09,team_lead,Exception not approved. Account opened with restrictions until an accepted proof of address arrives.
N-0730,k25-009,2025-07-30,analyst,Second reminder sent for proof of address. No reply.
N-0814,k25-009,2025-08-14,analyst,Account closed: no accepted proof of address received after two reminders.
N-0603,k25-010,2025-06-04,analyst,ID checked in branch. Energy bill fine.
N-0821,k25-012,2025-08-21,analyst,Water bill checked. No issues.
N-1002,k25-012,2025-10-02,analyst,Account restricted pending a source of funds review (unrelated to KYC documents).
N-0917,k25-013,2025-09-17,analyst,"ID and energy bill checked. Ship's officer, often abroad; prefers email."
N-1008,k25-014,2025-10-08,analyst,Business account. Director's mobile phone bill held on file with the company documents.
N-1113,k25-015,2025-11-13,analyst,Water bill checked. No issues.
N-1219,k25-016,2025-12-19,analyst,Mobile phone bill not accepted. Customer sent a bank statement by email on 2 January; account to open in the new year.
N-0104,k25-016,2026-01-05,analyst,Bank statement checked. Account opens 6 January 2026.
"""  # noqa: E501
NOTES = tuple(tuple(row) for row in csv.reader(NOTES_CSV.strip().splitlines()))


def draw_tenancy(tenancy: Tenancy) -> Image.Image:
    """The first page of a tenancy agreement, with a signature drawn as ink, not text."""
    font = make_data._font
    page = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(page)
    draw.rectangle((0, 0, 1240, 150), fill=(60, 90, 60))
    draw.text((80, 45), tenancy.agent, font=font(52), fill="white")
    make_data._stamp(draw, (900, 210), 44)
    long = make_data._long_date
    lines = [
        ("Assured shorthold tenancy agreement", 32),
        ("Saltgate Lettings, 5 Dock Street, Port Ellery, UT 40380", 22),
        ("", 22),
        (f"Tenant: {tenancy.tenant}", 28),
        (f"Landlord: {tenancy.landlord}", 28),
        (f"Property address: {tenancy.address}", 28),
        (f"Date of agreement: {long(tenancy.agreed)}", 28),
        (f"Term: 12 months from {long(tenancy.starts)}", 28),
        (f"Rent: {tenancy.rent} per month, payable in advance", 28),
        ("", 22),
        ("1. The landlord lets the property to the tenant for the term at the rent.", 24),
        ("2. The tenant pays the rent on the first day of each month.", 24),
        ("3. The tenant pays the council tax and the utility bills for the property.", 24),
        ("4. The deposit is held in a government-approved scheme.", 24),
    ]
    y = make_data._page_lines(draw, 80, 200, lines) + 60
    for label, x in (("Signed by the tenant", 80), ("Signed for the landlord", 660)):
        draw.text((x, y), label, font=font(24), fill=INK)
        draw.line((x, y + 110, x + 480, y + 110), fill=INK, width=2)
    # The tenant's signature: a pen stroke, which OCR reads as nothing.
    stroke = [(100 + i * 12, y + 95 - (14 if i % 3 == 0 else 0) - (i % 5) * 4) for i in range(30)]
    draw.line(stroke, fill=(20, 30, 110), width=4)
    make_data._page_lines(
        draw,
        80,
        y + 200,
        [("This specimen agreement was generated for a software demo. It is not a real one.", 22)],
    )
    return page


def form_row(item: AgentCase) -> list[str]:
    person, home = item.case.person, item.case.home
    return [
        *(item.case.case, item.received.isoformat(), person.surname.title(), person.given.title()),
        *(person.dob.isoformat(), home.flat, home.house_number, home.street, home.town),
        *(home.postcode, "Utopian", item.case.occupation, item.tax_residency),
    ]


def write_csv(
    path: Path, header: list[str], rows: list[list[str]] | tuple[tuple[str, ...], ...]
) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def main() -> None:
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    ids: list[Image.Image] = []
    addresses: list[Image.Image] = []
    documents: list[list[str]] = []
    for item in ALL_CASES:
        case, received = item.case, item.received.isoformat()
        ids.append(make_data.draw_id_card(case))
        documents.append(
            [case.case, f"{case.case}-id", "identity", "id_card", ID_PDF, str(len(ids)), received]
        )
        addresses.append(
            draw_tenancy(item.tenancy) if item.tenancy else make_data.draw_bill(case.bills[0])
        )
        poa = [f"{case.case}-poa", "address", item.declared, ADDRESS_PDF, str(len(addresses))]
        documents.append([case.case, *poa, received])
    for item in ALL_CASES:  # later documents go at the end of the scan file, as they arrived
        if item.later_bill and item.later_received:
            addresses.append(make_data.draw_bill(item.later_bill))
            later = [f"{item.case.case}-poa2", "address", "utility_bill", ADDRESS_PDF]
            documents.append(
                [item.case.case, *later, str(len(addresses)), item.later_received.isoformat()]
            )
    make_data._save_pdf(AGENT_DIR / ID_PDF, ids)
    make_data._save_pdf(AGENT_DIR / ADDRESS_PDF, addresses)
    write_csv(
        AGENT_DIR / "onboarding_cases.csv",
        ["case", "received", *make_data.FORM_FIELDS],
        [form_row(item) for item in ALL_CASES],
    )
    write_csv(
        AGENT_DIR / "onboarding_documents.csv",
        ["case", "document", "purpose", "declared_type", "file", "page", "received"],
        documents,
    )
    write_csv(
        AGENT_DIR / "accounts.csv",
        ["account", "case", "product", "opened", "status", "status_date"],
        ACCOUNTS,
    )
    notes = tuple(sorted(NOTES, key=lambda note: (note[2], note[0])))
    write_csv(AGENT_DIR / "case_notes.csv", ["note", "case", "date", "author_role", "text"], notes)
    print(
        f"wrote {len(ALL_CASES)} cases, {len(documents)} documents, {len(ACCOUNTS)} accounts "
        f"and {len(NOTES)} notes to {AGENT_DIR}"
    )


if __name__ == "__main__":
    main()
