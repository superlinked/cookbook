"""Check the recipe's parsing, the policy checks and the SIE failure rule offline.

The checks use hand-written strings, the committed data/ and hand-written stand-in model
replies: the stand-in OCR pages are written here from the values make_data.py draws, and the
stand-in extraction model returns the value after each field's label. Nothing here calls SIE or
replays a recorded response. Run it with `uv run --frozen python selfcheck.py`; it exits
non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import csv
import io
import re
import shutil
import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, cast

from onboarding import OnboardingSystem, SubmittedDocument
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
POLICY = recipe.load_policy()
DATA = recipe.DATA_DIR
RECEIVED = date(2026, 10, 26)


# --- Hand-written OCR pages, in LightOnOCR's shape (form boxes as "label<br>value" cells) -----


def cells(*rows: list[str]) -> str:
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table>{body}</table>"


def id_page(case: make_data.Case, **change: str) -> str:
    """An ID card or passport page for a make_data case; `change` overrides printed boxes."""
    person = case.person
    boxes = {
        "Surname": person.surname,
        "Given names": person.given,
        "Date of birth": make_data._id_date(person.dob),
        "Document no." if not case.passport else "Passport No.": person.document_number,
        "Date of expiry": make_data._id_date(person.expiry),
    }
    boxes.update(change)
    rows = [[f"{label}<br>{value}"] for label, value in boxes.items()]
    if not case.passport:
        rows.append([f"Address<br>{person.address}"])
    title = "PASSPORT" if case.passport else "IDENTITY CARD"
    mrz = "\n".join(make_data.mrz_lines(case))
    return f"# REPUBLIC OF UTOPIA\n\n{title}\n\nSPECIMEN\n\n{cells(*rows)}\n\n{mrz}\n"


def bill_page(bill: make_data.Bill, **change: str) -> str:
    """An address document page for a make_data bill; `change` overrides labelled lines."""
    lines = {
        bill.holder_label: bill.holder,
        bill.address_label: bill.address,
        "Account number": bill.account,
        bill.date_label: make_data._long_date(bill.statement),
    }
    lines.update(change)
    body = "\n".join(f"{label}: {value}" for label, value in lines.items())
    return (
        f"# {bill.issuer}\n\n{bill.subtitle}\n\nCustomer service 555-0142\n\nSPECIMEN\n\n"
        f"{bill.holder}\n{bill.address}\n\n{body}\n\nAmount due: {bill.amount}\n"
        f"Due date: {make_data._long_date(bill.due)}\n"
    )


def committed_pages() -> dict[str, str]:
    """Stand-in OCR pages for every page of the committed PDFs, keyed by the OCR call's id."""
    pages: dict[str, str] = {}
    bills = 0
    for number, case in enumerate(make_data.CASES, start=1):
        pages[f"ocr/id-documents/p{number}"] = id_page(case)
        for bill in case.bills:
            bills += 1
            pages[f"ocr/address-documents/p{bills}"] = bill_page(bill)
    return pages


# --- A stand-in for SIEClient -----------------------------------------------------------------


def stand_in_fields(text: str, purpose: str) -> dict[str, str]:
    """What a well-behaved GLiNER2 would return: the value after each field's first label, and
    for the issuer the first line."""
    found: dict[str, str] = {}
    for name, spec in RULES["documents"][purpose].items():
        if spec.get("cell"):
            found[name] = text.splitlines()[0].strip()
            continue
        for label in spec["label"]:
            match = re.search(rf"(?im)^{re.escape(label)}[^|:\n]*[|:] ?([^|\n]+)", text)
            if match:
                found[name] = match.group(1).strip()
                break
    return found


class StandIn:
    """Stands in for SIEClient. OCR calls return the page Markdown keyed by the call's item id
    ("ocr/<pdf stem>/p<page>"); extract calls return `stand_in_fields` for the window, or what
    `fields` gives for the item id. `errors` maps an item id prefix to the errors its calls
    raise (one per call), `replies` to a raw reply that replaces the normal one."""

    def __init__(
        self,
        pages: dict[str, str],
        errors: dict[str, list[Exception]] | None = None,
        replies: dict[str, Any] | None = None,
        fields: Callable[[str, str], dict[str, str] | None] | None = None,
    ) -> None:
        self.pages, self.errors, self.replies = pages, errors or {}, replies or {}
        self.fields = fields
        self.ids: list[str] = []
        self.extract_options: list[Any] = []  # the `options` of each extraction call

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
        self.extract_options.append(options.get("options"))
        purpose = "identity" if item_id.split("/")[-1].split("-w")[0].endswith("-id") else "address"
        if self.fields is not None:
            given = self.fields(item_id, text)
            if given is not None:
                return {"data": given}
        return {"data": stand_in_fields(text, purpose)}


def one_case_folder(folder: Path, case: str, form: dict[str, str] | None = None) -> None:
    """Copy the committed data into `folder`, keeping only `case`, with `form` changes."""
    for name in (make_data.ID_PDF, make_data.ADDRESS_PDF):
        shutil.copy(DATA / name, folder / name)
    for name in ("onboarding_cases.csv", "onboarding_documents.csv"):
        with (DATA / name).open(newline="", encoding="utf-8") as handle:
            rows = [row for row in csv.DictReader(handle) if row["case"] == case]
        if name == "onboarding_cases.csv":
            rows[0].update(form or {})
        with (folder / name).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def run(
    case: str | None = None,
    pages: dict[str, str] | None = None,
    form: dict[str, str] | None = None,
    policy: dict[str, Any] | None = None,
    **stand_in: Any,
) -> tuple[list[recipe.CaseResult], StandIn]:
    """Run the recipe with the stand-in client: on the committed data, or on one case of it."""
    all_pages = committed_pages()
    all_pages.update(pages or {})
    client = StandIn(all_pages, **stand_in)
    with tempfile.TemporaryDirectory() as name:
        folder = DATA
        if case is not None:
            folder = Path(name)
            one_case_folder(folder, case, form)
        with contextlib.redirect_stdout(io.StringIO()):
            results, _ = recipe.run(cast(SIEClient, client), policy or POLICY, RULES, folder)
    return results, client


def unmet(result: recipe.CaseResult) -> list[tuple[str, str, str]]:
    return [(r.requirement, r.document, r.status) for r in result.unmet]


def page_of(case: str, purpose: str = "id") -> str:
    """The OCR call id of a committed case's first ID or address page."""
    with (DATA / "onboarding_documents.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["case"] == case and row["document"].endswith(f"-{purpose}"):
                return f"ocr/{Path(row['file']).stem}/p{row['page']}"
    raise KeyError(case)


CASE = {case.case: case for case in make_data.CASES}


def ctx(**form: str) -> recipe.Context:
    base = {
        "surname": "Okafor-Bell",
        "given_names": "Tobias James",
        "date_of_birth": "1979-11-02",
        "flat": "2",
        "house_number": "114",
        "street": "Harbour Road",
        "postcode": "UT 40381",
    }
    base.update(form)
    return recipe.Context(base, RECEIVED, POLICY, RULES)


def doc_read(purpose: str, declared: str = "", **fields: str) -> recipe.DocumentRead:
    kind = declared or ("id_card" if purpose == "identity" else "utility_bill")
    return recipe.DocumentRead(SubmittedDocument("d1", purpose, kind, "x.pdf", 1), dict(fields))


# --- Checks -----------------------------------------------------------------------------------


def check_planted_cases() -> None:
    """Every planted case through the whole recipe, with well-behaved stand-in models."""
    results, client = run()
    got = {r.case: unmet(r) for r in results}
    expected: dict[str, list[tuple[str, str, str]]] = {
        "kyc-01": [],
        "kyc-02": [],
        "kyc-03": [("id_valid", "kyc-03-id", "not_met")],
        "kyc-04": [("id_mrz", "kyc-04-id", "not_met")],
        "kyc-05": [("address_age", "kyc-05-poa", "not_met")],
        "kyc-06": [("address_matches", "kyc-06-poa", "not_met")],
        "kyc-07": [("address_holder", "kyc-07-poa", "not_met")],
        "kyc-08": [("id_date_of_birth", "kyc-08-id", "not_met")],
        "kyc-09": [("address_matches", "kyc-09-poa", "not_met")],
        "kyc-10": [
            ("address_matches", "kyc-10-poa2", "not_met"),
            ("address_documents_agree", "", "not_met"),
        ],
        "kyc-11": [("form_occupation", "", "missing"), ("address_document", "", "missing")],
        "kyc-12": [("address_type", "kyc-12-poa", "not_met")],
    }
    assert got == expected, {k: v for k, v in got.items() if v != expected[k]}
    status = {r.case: r.status for r in results}
    assert status["kyc-01"] == status["kyc-02"] == "checks_passed", status
    by_case = {r.case: r for r in results}
    # kyc-11: the verdicts on the form and the address document need no model; only its ID is
    # read, once.
    kyc11 = [i for i in client.ids if "kyc-11" in i or i == page_of("kyc-11")]
    assert kyc11 == [page_of("kyc-11"), "extract/kyc-11/kyc-11-id-w1"], kyc11
    assert by_case["kyc-11"].ask_customer == [
        "your occupation (it was left blank on the form)",
        "a proof of address, separate from your identity document, dated on or after 26 July "
        "2026: a utility bill, bank statement or council tax bill",
    ], by_case["kyc-11"].ask_customer
    detail = next(r.detail for r in by_case["kyc-11"].unmet if r.requirement == "address_document")
    assert "kyc-11-id (id_card) is not an accepted type" in detail, detail
    # kyc-12: the issuer decides the type, whatever the upload said.
    detail = by_case["kyc-12"].unmet[0].detail
    assert detail == (
        "uploaded as utility_bill; 'Larkspur Mobile' issues mobile_phone_bill, which the policy "
        "doesn't accept as proof of address"
    ), detail
    # kyc-04: a failed check digit is for the analyst, not something to ask the customer for.
    assert by_case["kyc-04"].ask_customer == [], by_case["kyc-04"].ask_customer
    assert [r.requirement for r in by_case["kyc-04"].for_analyst] == ["id_mrz"]
    dob = next(r for r in by_case["kyc-04"].requirements if r.requirement == "id_date_of_birth")
    assert dob.status == "met", dob
    assert "fails its check digit" in dob.detail, dob
    detail = by_case["kyc-08"].unmet[0].detail
    assert "ID 1988-03-04, application 1988-04-03 (day and month swapped)" in detail, detail
    detail = by_case["kyc-10"].unmet[1].detail
    assert detail.startswith("conflicting address documents: kyc-10-poa1 '45 Gannet Way"), detail
    ocr = [i for i in client.ids if i.startswith("ocr/")]
    assert len(ocr) == 24, len(ocr)
    # Every extraction window fails rather than being cut when it's too long to read whole.
    assert client.extract_options, client.extract_options
    assert all(o == {"overflow_policy": "error"} for o in client.extract_options), set(
        map(str, client.extract_options)
    )
    print("planted cases: all 12 through the recipe with stand-in models ok")


def check_statuses() -> None:
    """Statuses name what the checks found, never a decision."""
    results, _ = run()
    assert {r.status for r in results} == {"checks_passed", "review"}
    assert {req.status for r in results for req in r.requirements} == {"met", "not_met", "missing"}
    with tempfile.TemporaryDirectory() as name:
        recipe.write_outputs(Path(name), results)
        written = "\n".join(p.read_text(encoding="utf-8") for p in Path(name).rglob("*.*"))
    for word in ("approved", "rejected", "clear", "analyst_review", "decision"):
        assert word not in written, word
    print("statuses: checks_passed / review, met / not_met / missing; no decision words ok")


def check_mrz() -> None:
    td1 = "\n".join(make_data.mrz_lines(CASE["kyc-01"]))
    mrz = recipe.parse_mrz(td1)
    assert mrz is not None
    assert mrz.valid, mrz
    assert mrz.format == "TD1", mrz
    assert mrz.surname == "MARLOWE"
    assert mrz.given_names == "EVELYN ROSE"
    td3 = "\n".join(make_data.mrz_lines(CASE["kyc-08"]))
    mrz = recipe.parse_mrz(td3)
    assert mrz is not None
    assert mrz.valid, mrz
    assert mrz.format == "TD3", mrz
    assert mrz.date_of_birth == "880304"
    # OCR quirks: a space in a line, an entity for "<", a filler run one short.
    l1, l2 = make_data.mrz_lines(CASE["kyc-08"])
    quirky = (
        l1[:10] + " " + l1[10:].replace("<<<<", "&lt;<<", 1) + "\n" + l2.replace("<<<", "<<", 1)
    )
    mrz = recipe.parse_mrz(quirky)
    assert mrz is not None
    assert mrz.valid, mrz
    bad = recipe.parse_mrz("\n".join(make_data.mrz_lines(CASE["kyc-04"])))
    assert bad is not None
    assert bad.checks["date_of_birth"] is False
    assert bad.checks["composite"]
    assert recipe.parse_mrz("no machine-readable zone here") is None
    # The composite digit covers the optional data no field digit covers: one changed filler
    # character there fails only the composite, on a TD3 and on a TD1.
    l1, l2 = make_data.mrz_lines(CASE["kyc-08"])
    mrz = recipe.parse_mrz(l1 + "\n" + l2[:30] + "B" + l2[31:])
    assert mrz is not None
    assert mrz.checks == {
        "document_number": True,
        "date_of_birth": True,
        "date_of_expiry": True,
        "composite": False,
    }, mrz.checks
    t1, t2, t3 = make_data.mrz_lines(CASE["kyc-01"])
    mrz = recipe.parse_mrz("\n".join([t1[:20] + "B" + t1[21:], t2, t3]))
    assert mrz is not None
    assert mrz.format == "TD1", mrz
    assert not mrz.valid
    assert [name for name, ok in mrz.checks.items() if not ok] == ["composite"], mrz.checks
    print("mrz: TD1, TD3, OCR quirks, a planted bad check digit ok")


def check_parsing() -> None:
    flat = recipe.flatten_markdown(id_page(CASE["kyc-02"]))
    assert "Surname | OKAFOR-BELL" in flat.splitlines(), flat
    assert recipe.labelled_cell("OKAFOR", flat, ["surname"]) == "OKAFOR-BELL"
    # A passport's row of labels above a row of values pairs up.
    table = cells(["Surname", "Given names"], ["ADEYEMI", "FOLASADE GRACE"])
    flat = recipe.flatten_markdown(table)
    assert recipe.labelled_cell("GRACE", flat, ["given names"]) == "FOLASADE GRACE", flat
    bill = recipe.flatten_markdown(bill_page(CASE["kyc-02"].bills[0]))
    assert bill.splitlines()[0] == "Brookline Water Co.", bill
    got = recipe.labelled_cell("114 Harbour Rd", bill, ["service address"])
    assert got == "Flat 2, 114 Harbour Rd, Port Ellery, UT 40381", got
    assert recipe.whole_cell("Brookline", bill) == "Brookline Water Co"
    # The holder's name on the unlabelled address block is not a labelled value.
    assert recipe.labelled_cell("Tobias", "Mr Tobias Okafor-Bell", ["account holder"]) is None
    # "Due date" is not the statement date.
    assert recipe.labelled_cell("18 August 2026", bill, ["statement date"]) is None
    print("parsing: flatten, labelled cell, whole cell, label above value ok")


def check_addresses() -> None:
    application = recipe.form_address(ctx().form, RULES)
    assert application == recipe.Address("114", "2", "harbour road", "UT40381"), application

    def differs(written: str) -> list[str] | str:
        parsed, problem = recipe.parse_address(written, RULES)
        return problem if parsed is None else recipe.address_differences(parsed, application)

    for same in (
        "Flat 2, 114 Harbour Rd, Port Ellery, UT 40381",
        "114 HARBOUR ROAD FLAT 2, PORT ELLERY UT 40381",
        "Flat2, 114 Harbour Road, Port Ellery UT40381",
        "Apartment 2, 114 harbour road, port ellery, ut 40381",
    ):
        assert differs(same) == [], (same, differs(same))
    # kyc-09: same words, flat and house number swapped.
    assert differs("Flat 114, 2 Harbour Road, Port Ellery, UT 40381") == [
        "house number '2', application '114'",
        "flat '114', application '2'",
    ]
    assert differs("114A Harbour Road Flat 2, Port Ellery UT 40381") == [
        "house number '114a', application '114'"
    ]
    assert differs("114 Harbour Road, Port Ellery, UT 40381") == ["flat '(none)', application '2'"]
    assert differs("Flat 2, 114 Harbour Road, Port Ellery, UT 40388") == [
        "postcode 'UT40388', application 'UT40381'"
    ]
    assert differs("Flat 2, 114 Harbour Street, Port Ellery, UT 40381") == [
        "street 'harbour street', application 'harbour road'"
    ]
    # One span holding two addresses never compares as one.
    assert str(differs("Flat 2, 114 Harbour Road / 12 Mill Lane, UT 40381")).startswith(
        "two street addresses"
    )
    assert str(differs("114 Harbour Road Flat 2, UT 40381 and UT 40563")).startswith(
        "two postcodes"
    )
    assert differs("Flat 2, 114 Harbour Road, Port Ellery") == "no postcode"
    assert str(differs("Flat 2, Flat 3, 114 Harbour Road, UT 40381")).startswith("two flat numbers")
    assert differs("Harbour Road, Port Ellery, UT 40381") == "no house number and street"
    # kyc-06: one digit, different house.
    eight = recipe.form_address(
        {"house_number": "8", "street": "Quillon St", "postcode": "UT 40561"}, RULES
    )
    parsed, _ = recipe.parse_address("18 Quillon Street, Westmarch, UT 40561", RULES)
    assert parsed is not None
    assert recipe.address_differences(parsed, eight) == ["house number '18', application '8'"]
    print("addresses: house, flat, street, postcode; swaps, near misses, two-in-one ok")


def check_names() -> None:
    def holder(name: str, surname: str = "Okafor-Bell", given: str = "Tobias James") -> bool:
        return recipe.name_matches(surname, given, name, RULES)

    assert holder("Mr Tobias Okafor-Bell")
    assert holder("TOBIAS J OKAFOR-BELL")
    assert not holder("Tobias K Okafor-Bell")  # an initial that is no given name's
    assert not holder("T Okafor-Bell")  # the first given name must be written out
    assert not holder("Tobias Okafor")  # half the surname
    assert not holder("James Okafor-Bell")  # not the first given name
    assert not holder("Siobhan Okafor-Bell")
    assert not holder("Tobias Okafor-Bell and Siobhan Okafor-Bell")  # two people
    assert not holder("Ana Ferreira", "Ferreira Lopes", "Ana Beatriz")
    # On the ID the whole name must be the application's, middle name included.
    read = doc_read("identity", surname="OKAFOR-BELL", given_names="TOBIAS")
    status, detail = recipe.check_id_name(read, ctx())
    assert status == "not_met", detail
    read = doc_read("identity", surname="OKAFOR-BELL", given_names="TOBIAS JAMES")
    assert recipe.check_id_name(read, ctx())[0] == "met"
    # The MRZ name disagreeing is contrary evidence, even when the printed name agrees.
    read.mrz = recipe.Mrz("TD1", "UX1", "791102", "320213", "OKAFOR BELL", "TOBIAS", {})
    assert recipe.check_id_name(read, ctx())[0] == "met"  # a cut MRZ name is a prefix
    read.mrz = recipe.Mrz("TD1", "UX1", "791102", "320213", "OKAFOR", "TOBIAS JAMES", {})
    assert recipe.check_id_name(read, ctx())[0] == "not_met"
    # The MRZ given names must be the application's (or a cut prefix of them), not another's.
    for machine in ("TOBIN", "JAMES", ""):
        read.mrz = recipe.Mrz("TD1", "UX1", "791102", "320213", "OKAFOR BELL", machine, {})
        status, detail = recipe.check_id_name(read, ctx())
        assert status == "not_met", (machine, detail)
        assert detail.startswith("MRZ '"), detail
    # An MRZ on the page is checked whatever type the customer declared: a "driving licence"
    # carrying a passport MRZ with a bad check digit is not met.
    read = doc_read("identity", declared="driving_licence")
    assert recipe.check_id_mrz(read, ctx()) == (
        "met",
        "a driving_licence carries no MRZ to check",
    )
    read.mrz = recipe.parse_mrz("\n".join(make_data.mrz_lines(CASE["kyc-04"])))
    assert read.mrz is not None
    status, detail = recipe.check_id_mrz(read, ctx())
    assert status == "not_met", detail
    assert "date_of_birth" in detail, detail
    read.mrz = recipe.parse_mrz("\n".join(make_data.mrz_lines(CASE["kyc-01"])))
    assert recipe.check_id_mrz(read, ctx())[0] == "met"
    print("names: titles, initials, partial and extra names, MRZ disagreement ok")


def check_issuers() -> None:
    page = "Kestrel Energy\nElectricity and gas"
    assert recipe.issuer_type("Kestrel Energy", page, RULES) == ("utility_bill", "")
    assert recipe.issuer_type("KESTREL ENERGY", page, RULES)[0] == "utility_bill"
    assert recipe.issuer_type("Brookline Water Co", page, RULES)[0] == "utility_bill"
    for near in ("Kestrel Energy Mobile", "Kestrel", "Kestrel Energies"):
        kind, why = recipe.issuer_type(near, page, RULES)
        assert kind is None, near
        assert "not on the issuer list" in why, (near, why)
    # A page that also names a listed issuer of another type is not trusted either way.
    both = "Kestrel Energy\nBundle with Larkspur Mobile and save"
    kind, why = recipe.issuer_type("Kestrel Energy", both, RULES)
    assert kind is None, why
    assert "two types" in why, why
    read = doc_read("address", issuer="Larkspur Mobile")
    read.issuer_type = "mobile_phone_bill"
    assert recipe.check_address_type(read, ctx())[0] == "not_met"
    read = doc_read("address", issuer="Kestrel Energy")
    read.issuer_type = "utility_bill"
    assert recipe.check_address_type(read, ctx()) == ("met", "'Kestrel Energy' issues utility_bill")
    read = doc_read("address")
    assert recipe.check_address_type(read, ctx()) == ("not_met", "issuer not read")
    print("issuers: exact list match, near names, two issuer types on a page ok")


def check_dates() -> None:
    def age(written: str) -> str:
        return recipe.check_address_age(doc_read("address", statement_date=written), ctx())[0]

    assert age("26 July 2026") == "met"  # exactly three months
    assert age("25 July 2026") == "not_met"
    assert age("27 October 2026") == "not_met"  # after the case date
    assert age("18 September 2026 / 18 October 2026") == "not_met"  # two dates in one cell
    assert age("29/07/2026") == "met"

    def dob(printed: str, form: str = "1979-11-02") -> tuple[str, str]:
        read = doc_read("identity", date_of_birth=printed)
        return recipe.check_id_date_of_birth(read, ctx(date_of_birth=form))

    assert dob("02 NOV 1979")[0] == "met"
    assert dob("11 FEB 1979") == (
        "not_met",
        "ID 1979-02-11, application 1979-11-02 (day and month swapped)",
    )
    assert dob("02 NOV 1979", form="")[0] == "not_met"
    # A valid MRZ birth date that disagrees is contrary evidence.
    read = doc_read("identity", date_of_birth="02 NOV 1979")
    read.mrz = recipe.Mrz("TD1", "X", "791103", "320213", "", "", {"date_of_birth": True})
    assert recipe.check_id_date_of_birth(read, ctx())[0] == "not_met"

    def valid(printed: str, mrz_expiry: str | None = None) -> tuple[str, str]:
        read = doc_read("identity", date_of_expiry=printed)
        if mrz_expiry:
            checks = {"date_of_expiry": True}
            read.mrz = recipe.Mrz("TD1", "X", "791102", mrz_expiry, "", "", checks)
        return recipe.check_id_valid(read, ctx())

    assert valid("26 OCT 2026")[0] == "met"  # valid through the case date
    assert valid("25 OCT 2026") == ("not_met", "the ID expired on 2026-10-25")
    assert valid("13 FEB 2032", "320213")[0] == "met"
    assert valid("13 FEB 2032", "250213") == (
        "not_met",
        "expiry printed 2032-02-13, MRZ 2025-02-13",
    )
    # The printed date unread: the MRZ expiry still decides; printed later than an expired MRZ
    # never wins.
    assert valid("", "320213") == ("met", "valid until 2032-02-13")
    assert valid("13 FEB 2032", "251025")[0] == "not_met"
    print("dates: address age boundary, two dates, DOB swap, expiry vs MRZ ok")


def check_form() -> None:
    def missing(**form: str) -> list[str]:
        base = dict(make_data_form("kyc-01"))
        base.update(form)
        result = recipe.CaseResult("x", RECEIVED)
        recipe.check_form(base, recipe.Context(base, RECEIVED, POLICY, RULES), result)
        return [r.requirement for r in result.unmet]

    assert missing() == []
    assert missing(occupation="") == ["form_occupation"]
    assert missing(occupation="  N/A ") == ["form_occupation"]
    assert missing(tax_residency="-", nationality="unknown") == [
        "form_nationality",
        "form_tax_residency",
    ]
    assert missing(flat="") == []  # not mandatory
    print("form: blank and placeholder values are missing ok")


def make_data_form(case: str) -> dict[str, str]:
    application = OnboardingSystem(DATA).get_case(case)
    assert application is not None
    return application.form


def check_document_types() -> None:
    def sort(*docs: tuple[str, str, str]) -> list[tuple[str, str, str]]:
        result = recipe.CaseResult("x", RECEIVED)
        submitted = tuple(SubmittedDocument(d, p, t, "x.pdf", 1) for d, p, t in docs)
        recipe.sort_documents(submitted, ctx(), result)
        return [(r.requirement, r.status, r.detail) for r in result.requirements]

    got = sort(("a", "identity", "passport"), ("b", "address", "bank_statement"))
    assert [g[1] for g in got] == ["met", "met"], got
    got = sort(("a", "identity", "passport"), ("b", "address", "mobile_phone_bill"))
    assert got[1][1] == "missing", got
    assert "b (mobile_phone_bill) is not" in got[1][2], got
    got = sort(("a", "identity", "library_card"), ("b", "address", "utility_bill"))
    assert got[0][:2] == ("identity_document", "missing"), got
    # A utility bill given for both purposes proves address, not identity.
    got = sort(("b", "identity", "utility_bill"), ("b", "address", "utility_bill"))
    assert [g[1] for g in got] == ["missing", "met"], got
    # Even under a policy that took ID cards as proof of address, the ID can't prove both.
    lenient = {**POLICY, "address": {**POLICY["address"], "accepted": {"id_card": "ID card"}}}
    results, _ = run("kyc-11", policy=lenient)
    detail = next(r.detail for r in results[0].unmet if r.requirement == "address_document")
    assert detail.endswith("kyc-11-id is also the identity document"), detail
    print("document types: accepted, separate, declared type checked before any call ok")


def check_conflicting_fields() -> None:
    """Two different values for one field go to review instead of the first one winning."""
    first = bill_page(CASE["kyc-01"].bills[0])

    def two_dates(item_id: str, text: str) -> dict[str, str] | None:
        if "kyc-01-poa" not in item_id:
            return None
        found = stand_in_fields(text, "address")
        found["statement_date"] = "18 September 2026" if item_id.endswith("w1") else "18 May 2026"
        found["issuer"] = "Kestrel Energy"
        return found

    # A page long enough for two windows, with a second statement date in the second.
    filler = " ".join(["word"] * 200)
    page = first + f"\n{filler}\nStatement date: 18 May 2026\n"
    results, _ = run("kyc-01", {page_of("kyc-01", "poa"): page}, fields=two_dates)
    assert unmet(results[0]) == [("address_age", "kyc-01-poa", "not_met")], unmet(results[0])
    detail = results[0].unmet[0].detail
    assert detail == "statement date read two ways: '18 September 2026' / '18 May 2026'", detail
    # A span the model made up, or one not on its label's line, is dropped.
    results, _ = run(
        "kyc-01",
        fields=lambda item_id, text: (
            {**stand_in_fields(text, "address"), "account_holder": "Evelyn Marlowe"}
            if "poa" in item_id
            else None
        ),
    )
    assert unmet(results[0]) == [("address_holder", "kyc-01-poa", "not_met")]
    assert "not in the text" in results[0].documents["kyc-01-poa"].notes[0]
    print("conflicting fields: two values read two ways; made-up spans dropped ok")


def check_adversarial() -> None:
    """Stand-in replies that try to get a confident status out of thin evidence."""
    # The holder's name taken from the unlabelled address block, not the labelled line.
    page = bill_page(CASE["kyc-07"].bills[0])
    results, _ = run(
        "kyc-07",
        fields=lambda item_id, text: (
            {**stand_in_fields(text, "address"), "account_holder": "71 Elderwood Lane"}
            if "poa" in item_id
            else None
        ),
        pages={page_of("kyc-07", "poa"): page},
    )
    assert ("address_holder", "kyc-07-poa", "not_met") in unmet(results[0])
    # The issuer span is the subtitle "Electricity and gas" of a mobile bill: not listed.
    results, _ = run(
        "kyc-12",
        fields=lambda item_id, text: (
            {**stand_in_fields(text, "address"), "issuer": "Your mobile phone bill"}
            if "poa" in item_id
            else None
        ),
    )
    assert unmet(results[0]) == [("address_type", "kyc-12-poa", "not_met")]
    # A mobile bill that says "Kestrel Energy" in its small print still names Larkspur too.
    page = bill_page(CASE["kyc-12"].bills[0]) + "\nPartner offer from Kestrel Energy\n"
    results, _ = run(
        "kyc-12",
        pages={page_of("kyc-12", "poa"): page},
        fields=lambda item_id, text: (
            {**stand_in_fields(text, "address"), "issuer": "Kestrel Energy"}
            if "poa" in item_id
            else None
        ),
    )
    assert unmet(results[0]) == [("address_type", "kyc-12-poa", "not_met")], unmet(results[0])
    # The ID's MRZ is missing altogether: no MRZ, no confident ID.
    page = id_page(CASE["kyc-01"]).split("I<UTO")[0]
    results, _ = run("kyc-01", pages={page_of("kyc-01"): page})
    assert unmet(results[0]) == [("id_mrz", "kyc-01-id", "not_met")], unmet(results[0])
    # A printed document number that isn't the MRZ's.
    page = id_page(CASE["kyc-01"], **{"Document no.": "UX3804171"})
    results, _ = run("kyc-01", pages={page_of("kyc-01"): page})
    assert unmet(results[0]) == [("id_number", "kyc-01-id", "not_met")], unmet(results[0])
    # The envelope window is addressed to the applicant; the account holder is someone else.
    page = bill_page(CASE["kyc-07"].bills[0]).replace(
        "SPECIMEN\n\nSiobhan Brannigan", "SPECIMEN\n\nCormac Brannigan"
    )
    assert "\nCormac Brannigan\n" in page, page
    results, _ = run(
        "kyc-07",
        pages={page_of("kyc-07", "poa"): page},
        fields=lambda item_id, text: (
            {**stand_in_fields(text, "address"), "account_holder": "Cormac Brannigan"}
            if "poa" in item_id
            else None
        ),
    )
    assert unmet(results[0]) == [("address_holder", "kyc-07-poa", "not_met")], unmet(results[0])
    assert results[0].unmet[0].detail == "account holder not read", results[0].unmet
    # Nothing read at all from the bill: every address requirement is not met, none passes.
    results, _ = run("kyc-01", fields=lambda item_id, text: {} if "poa" in item_id else None)
    assert [u[0] for u in unmet(results[0])] == [
        "address_type",
        "address_holder",
        "address_matches",
        "address_age",
    ], unmet(results[0])
    # min_page_chars: an OCR page one character short of the threshold is unreadable, and
    # every ID requirement is not met with that reason; at the threshold it is read.
    least = RULES["thresholds"]["min_page_chars"]
    results, _ = run("kyc-01", pages={page_of("kyc-01"): "x" * (least - 1)})
    reasons = {r.detail for r in results[0].unmet if r.document == "kyc-01-id"}
    assert reasons == {"id-documents.pdf p1 could not be read (too little text)"}, reasons
    results, _ = run("kyc-01", pages={page_of("kyc-01"): "x" * least})
    reasons = {r.detail for r in results[0].unmet if r.document == "kyc-01-id"}
    assert "id-documents.pdf p1 could not be read (too little text)" not in reasons, reasons
    assert "no MRZ found on the ID" in reasons, reasons
    # An occupation of "n/a" is not an occupation.
    results, _ = run("kyc-01", form={"occupation": "n/a"})
    assert unmet(results[0]) == [("form_occupation", "", "missing")]
    print("adversarial: unlabelled holder, subtitle issuer, two issuers, no MRZ, number, n/a ok")


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
    bill = page_of("kyc-01", "poa")
    # A transient error on the bill: its four requirements are not met, for the analyst, and
    # the ID is still checked.
    for error in TRANSIENT:
        results, _ = run("kyc-01", errors={bill: [error]})
        rows = results[0].unmet
        assert [r.requirement for r in rows] == [
            "address_type",
            "address_holder",
            "address_matches",
            "address_age",
        ], type(error).__name__
        assert all("was not read: the SIE call failed: " in r.detail for r in rows), rows
        assert results[0].ask_customer == [], results[0].ask_customer
    # The same on an extraction window.
    results, _ = run("kyc-01", errors={"extract/kyc-01/kyc-01-id": [TRANSIENT[1]]})
    assert {r.requirement for r in results[0].unmet} == set(POLICY["identity"]["requirements"])
    assert "window 1 was not read" in results[0].unmet[0].detail
    # A reply missing its key is a failed call on the same path; an empty one is not.
    results, _ = run("kyc-01", replies={bill: {"something": []}})
    assert "replied without 'entities'" in results[0].unmet[0].detail, results[0].unmet
    results, _ = run("kyc-01", replies={"extract/kyc-01/kyc-01-poa": {"no_data": {}}})
    assert "replied without 'data'" in results[0].unmet[0].detail, results[0].unmet
    results, _ = run("kyc-01", replies={"extract/kyc-01/kyc-01-poa": {"data": {}}})
    assert results[0].unmet[0].detail == "issuer not read", results[0].unmet
    # SIE Cloud's empty-result 500 reads as "no fields", not a failed call.
    empty = ServerError(recipe.EMPTY_RESULT_ERROR, status_code=500)
    results, _ = run("kyc-01", errors={"extract/kyc-01/kyc-01-poa": [empty]})
    assert results[0].unmet[0].detail == "issuer not read", results[0].unmet
    assert "no fields found" in results[0].documents["kyc-01-poa"].notes[0]
    # From the OCR model it's a failed call.
    results, _ = run("kyc-01", errors={bill: [empty]})
    assert "the SIE call failed: ServerError" in results[0].unmet[0].detail
    # An OCR reply with entities but no markdown entity says so.
    results, _ = run("kyc-01", replies={bill: {"entities": []}})
    assert results[0].unmet[0].detail.endswith("the OCR reply had no markdown text")
    # Every call failed (a dead SIE_BASE_URL): the run stops, with the full message.
    long = "connection refused " + "x" * 300
    dead = {"": [SIEConnectionError(long) for _ in range(20)]}
    message = stopped(lambda: run("kyc-01", errors=dead))
    assert message.startswith("Every SIE call failed (2 of 2 to lightonai/LightOnOCR-3-4B)"), (
        message
    )
    assert long in message
    assert "Check SIE_BASE_URL and your network." in message, message
    busy = {"": [ModelLoadingError("loading") for _ in range(20)]}
    message = stopped(lambda: run("kyc-01", errors=busy))
    assert "may still be starting or SIE is busy; run again in a few minutes." in message
    # A refusal stops the run at once, with the full message, HTTP status and code.
    refusal = {bill: [InsufficientCreditsError("no credits " + "y" * 300)]}
    message = stopped(lambda: run("kyc-01", errors=refusal))
    assert "InsufficientCreditsError (HTTP 402, code INSUFFICIENT_CREDITS)" in message, message
    assert "y" * 300 in message
    assert "SIE_API_KEY" in message
    gated = {"": [ModelLoadFailedError("gated", permanent=True)]}
    message = stopped(lambda: run("kyc-01", errors=gated))
    assert message.startswith("SIE can't load lightonai/LightOnOCR-3-4B"), message
    message = stopped(lambda: run("kyc-01", errors={"": [RequestError("unexpected body")]}))
    assert "SIE_API_KEY" not in message, message
    assert "SIE_BASE_URL" in message, message
    # A bug in our own code is raised as is; main turns only SIEStopped into a clean exit.
    try:
        run("kyc-01", errors={"": [TypeError("bad argument")]})
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
    assert recipe.csv_safe("-") == "'-"
    assert recipe.csv_safe("kyc-01") == "kyc-01"
    result = recipe.CaseResult("x", RECEIVED)
    result.add("form_occupation", "", "met", "=cmd()")
    with tempfile.TemporaryDirectory() as name:
        recipe.write_outputs(Path(name), [result])
        row = (Path(name) / "checklist.csv").read_text(encoding="utf-8").splitlines()[1]
        assert row == "x,form_occupation,,met,'=cmd(),", row
    print("csv_safe: formula-looking cells are quoted ok")


def check_policy_and_data() -> None:
    broken = dict(POLICY)
    broken["address"] = {**POLICY["address"], "requirements": {"address_colour": "?"}}
    with tempfile.TemporaryDirectory() as name:
        path = Path(name) / "policy.yaml"
        import yaml

        path.write_text(yaml.safe_dump(broken), encoding="utf-8")
        try:
            recipe.load_policy(path)
        except ValueError as error:
            message = str(error)
        else:
            raise AssertionError("an unknown requirement was accepted")
    assert "address_colour" in message, message
    system = OnboardingSystem(DATA)
    assert system.open_cases() == [f"kyc-{n:02d}" for n in range(1, 13)]
    assert system.get_case("kyc-99") is None
    kyc10 = system.get_case("kyc-10")
    assert kyc10 is not None
    assert [d.document for d in kyc10.documents] == [
        "kyc-10-id",
        "kyc-10-poa1",
        "kyc-10-poa2",
    ]
    kyc11 = system.get_case("kyc-11")
    assert kyc11 is not None
    assert kyc11.form["occupation"] == ""
    assert [(d.document, d.purpose) for d in kyc11.documents] == [
        ("kyc-11-id", "identity"),
        ("kyc-11-id", "address"),
    ]
    for name in (make_data.ID_PDF, make_data.ADDRESS_PDF):
        raw = (DATA / name).read_bytes()
        for marker in (b"/Info", b"/Title", b"/Author", b"/CreationDate", b"Exif", b"xmpmeta"):
            assert marker not in raw, f"{name} carries {marker!r}"
    assert "legal advice" in (recipe.POLICY_PATH).read_text(encoding="utf-8")
    print("policy and data: unknown requirement refused, onboarding lookups, no PDF metadata ok")


if __name__ == "__main__":
    check_mrz()
    check_parsing()
    check_addresses()
    check_names()
    check_issuers()
    check_dates()
    check_form()
    check_document_types()
    check_planted_cases()
    check_statuses()
    check_conflicting_fields()
    check_adversarial()
    check_sie_failure()
    check_csv_safe()
    check_policy_and_data()
    print("all checks passed")
