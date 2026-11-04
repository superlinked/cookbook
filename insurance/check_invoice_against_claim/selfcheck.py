"""Check the recipe's parsing, claim lookup, claim match and invoice checks offline.

The checks use hand-written strings, the committed data/ and hand-written stand-in model
replies. Nothing here calls SIE or replays a recorded response. Run it with
`uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import csv
import dataclasses
import io
import re
import shutil
import tempfile
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from claims_system import ClaimsSystem
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
CLAIMS = {claim.document: claim for claim in make_data.CLAIMS}


# --- Hand-written OCR pages, in LightOnOCR's shape --------------------------------------------


def money(amount: Decimal) -> str:
    return f"{amount:,.2f}"


def table(*rows: list[str], head: bool = False) -> str:
    def row(cells: list[str], tag: str) -> str:
        return "<tr>" + "".join(f"<{tag}>{cell}</{tag}>" for cell in cells) + "</tr>"

    first = row(rows[0], "th" if head else "td")
    return "<table>" + first + "".join(row(r, "td") for r in rows[1:]) + "</table>"


def page(document: str = "clm-01", **change: Any) -> str:
    """The invoice page for one claim as LightOnOCR writes it: the letterhead, the
    "Label: value" header lines, the line-item table and the totals table. `change` replaces
    a header value or a total (None leaves the line off), or `items` / `extra` (more lines)."""
    claim = CLAIMS[document]
    invoice = claim.invoice
    values: dict[str, str | None] = {
        "vendor": invoice.vendor,
        "Invoice number": invoice.number,
        "Invoice date": make_data._long_date(invoice.issued),
        "Bill to": invoice.bill_to,
        "Claim number": invoice.claim_number,
        ("Vehicle" if claim.vin else "Service address"): invoice.location,
        "Subtotal": money(invoice.subtotal),
        f"Sales tax ({invoice.tax_rate}%)": "" if invoice.tax_blank else money(invoice.tax),
        "Total due": "$" + money(invoice.total),
    }
    values.update(change.get("fields", {}))
    items = change.get(
        "items",
        [
            [item.description, str(item.qty), money(item.unit_price), money(item.amount)]
            for item in invoice.items
        ],
    )
    header = [
        f"{label}: {value}"
        for label, value in values.items()
        if label in ("Invoice number", "Invoice date", "Bill to", "Claim number", "Vehicle")
        or label == "Service address"
        if value is not None
    ]
    totals = [
        [label, value or ""]
        for label, value in values.items()
        if (label.startswith(("Subtotal", "Sales tax", "Total")) and value is not None)
    ]
    return "\n\n".join(
        [
            f"# {values['vendor']}",
            invoice.vendor_address,
            "## INVOICE",
            "  \n".join(header + change.get("extra", [])),
            table(["Description", "Qty", "Unit price", "Amount"], *items, head=True),
            table(*totals),
            "Payment due within 30 days. Thank you for your business.",
        ]
    )


# --- A stand-in for SIEClient -----------------------------------------------------------------


def stand_in_fields(text: str) -> dict[str, str]:
    """What a well-behaved GLiNER2 would return for the schema: the value after each field's
    first label, and the first line as the vendor."""
    first = text.splitlines()[0]
    found: dict[str, str] = {} if " | " in first else {"vendor_name": first}
    for name, spec in RULES["documents"]["invoice"].items():
        for label in spec.get("label", []):
            match = re.search(rf"(?im)^{re.escape(label)}[^|:\n]*[:|] *([^|\n]+)", text)
            if match and match.group(1).strip():
                found[name] = match.group(1).strip()
                break
    return found


def stand_in_entities(text: str) -> list[dict[str, Any]]:
    """What GLiNER2's entity mode would return: every street, every VIN, every person."""
    entities = []
    for match in re.finditer(
        r"\b\d+ [A-Z][a-z]+(?: [A-Z][a-z]+)* "
        r"(?:Road|Lane|Way|Close|Rise|Row|Court|Drive|Street|Avenue|Approach|Ln)\b",
        text,
    ):
        entities.append({"label": "street address", "text": match.group(0), "score": 0.8})
    for vin in re.findall(r"\b[A-Z0-9]{17}\b", text):
        entities.append({"label": "vehicle identification number", "text": vin, "score": 0.9})
    for name in re.findall(r"(?m)^Bill to: (.+)$", text):
        entities.append({"label": "person name", "text": name.strip(), "score": 1.0})
    return entities


class StandIn:
    """Stands in for SIEClient. OCR calls return the page keyed by the call's item id
    ("ocr/repair-invoices/p<page>"); schema calls return `stand_in_fields` (or what `fields`
    gives) and entity calls `stand_in_entities`. `errors` maps an item id prefix to the errors
    its calls raise (one per call), `replies` to a raw reply that replaces the normal one."""

    def __init__(
        self,
        pages: dict[str, str],
        errors: dict[str, list[Exception]] | None = None,
        replies: dict[str, Any] | None = None,
        fields: Callable[[str], dict[str, str]] | None = None,
    ) -> None:
        self.pages, self.errors, self.replies = pages, errors or {}, replies or {}
        self.fields = fields or stand_in_fields
        self.ids: list[str] = []
        self.options: dict[str, Any] = {}  # item id -> the `options` the call carried

    def extract(self, model: str, item: Any, **options: Any) -> Any:
        item_id, text = item["id"], item.get("text", "")
        self.ids.append(item_id)
        self.options[item_id] = options.get("options")
        for prefix, errors in self.errors.items():
            if item_id.startswith(prefix) and errors:
                raise errors.pop(0)
        for prefix, reply in self.replies.items():
            if item_id.startswith(prefix):
                return reply
        if "output_schema" in options:
            return {"data": self.fields(text)}
        if "labels" in options:
            return {"entities": stand_in_entities(text)}
        return {"entities": [{"label": "markdown", "text": self.pages[item_id]}]}


def run(
    pages: dict[str, str],
    authorizations: list[str] | None = None,
    **stand_in: Any,
) -> list[recipe.InvoiceResult]:
    """Run the recipe on the committed claims system, with only the documents in `pages`
    waiting, and the stand-in OCR pages in their place. `authorizations` replaces the rows
    of authorizations.csv."""
    with tempfile.TemporaryDirectory() as name:
        folder = Path(name)
        for path in DATA.iterdir():
            shutil.copy(path, folder / path.name)
        documents = (DATA / "claim_documents.csv").read_text(encoding="utf-8").splitlines()
        kept = [documents[0]] + [row for row in documents[1:] if row.split(",")[0] in pages]
        (folder / "claim_documents.csv").write_text("\n".join(kept) + "\n", encoding="utf-8")
        if authorizations is not None:
            rows = ["claim_number,kind,amount,approved_on", *authorizations]
            (folder / "authorizations.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        by_id = {
            f"ocr/repair-invoices/p{row.split(',')[-1]}": pages[row.split(",")[0]]
            for row in kept[1:]
        }
        client = StandIn(by_id, **stand_in)
        with contextlib.redirect_stdout(io.StringIO()):
            results, _ = recipe.run(cast(SIEClient, client), RULES, folder)
    return results


def one(document: str = "clm-01", **change: Any) -> recipe.InvoiceResult:
    return run({document: page(document, **change)})[0]


def flagged(result: recipe.InvoiceResult) -> list[str]:
    return [check.check for check in result.flagged]


def detail(result: recipe.InvoiceResult, check: str) -> str:
    return next(c.detail for c in result.checks if c.check == check)


# --- Checks -----------------------------------------------------------------------------------


def check_parsing() -> None:
    assert recipe.parse_money("$3,597.64") == Decimal("3597.64")
    assert recipe.parse_money("3,597.64USD") == Decimal("3597.64")  # glued currency
    assert recipe.parse_money("USD 0.00") == Decimal("0.00")
    assert recipe.parse_money("-12.00") == Decimal("-12.00")
    for bad in ("6%", "$10.00 $12.00", "1.346,20", "1,34,6.20", "none", "", "N/A"):
        assert recipe.parse_money(bad) is None, bad
    flat = recipe.flatten_markdown(page())
    assert "Subtotal | 3,394.00" in flat, flat
    assert recipe.labelled_cell("3,597.64", flat, ["total"]) == "$3,597.64"
    assert recipe.labelled_cell("3,394.00", flat, ["total"]) is None  # "Subtotal" isn't "total"
    assert recipe.labelled_cell("203.64", flat, ["sales tax"]) == "203.64"
    assert recipe.labelled_cell("CLM-26-104817", flat, ["claim number"]) == "CLM-26-104817"
    assert recipe.letterhead_line("Dana Whitcombe", flat, ["bill to"]) is None
    items, problems = recipe.line_items(flat, RULES)
    assert len(items) == 4, items
    assert not problems, problems
    issued = recipe.parse_date("October 12, 2026", RULES["date_formats"])
    assert issued is not None
    assert issued.isoformat() == "2026-10-12"
    assert recipe.name_key("Whitcombe, Dana") == recipe.name_key("Dana Whitcombe")
    assert recipe.address_key("41 Fernhollow Ln., Ashby Vale CO 80911", RULES) == (
        recipe.address_key("41 Fernhollow Lane, Ashby Vale, CO 80911", RULES)
    )
    print("parsing: amounts, labelled cells, line items, dates, names, addresses ok")


def check_claims_system() -> None:
    claims = ClaimsSystem(DATA)
    claim = claims.get_claim(" clm-26-104817 ")
    assert claim is not None
    assert claim.named_insured == "Dana Whitcombe"
    assert claims.get_claim("CLM-26-10432") is None  # a missing digit is not the nearest claim
    assert claims.get_claim("CLM-26-10481") is None  # nor is a prefix
    supplemented = claims.get_claim("CLM-26-224417")
    assert supplemented is not None
    assert [a.kind for a in supplemented.authorizations] == ["estimate", "supplement"]
    assert supplemented.authorized == CLAIMS["clm-10"].invoice.total
    assert [d.document for d in claims.waiting_invoices()] == [f"clm-{n:02d}" for n in range(1, 11)]
    print("claims system: exact lookups, no nearest match, supplements add up ok")


# The spec's cases, read from a faithful OCR page: (status, checks not passed).
EXPECTED_RESULTS = {
    "clm-01": ("checks_passed", []),
    "clm-02": ("checks_passed", []),
    "clm-03": ("review", ["invoice_date"]),
    "clm-04": ("review", ["claim_match"]),
    "clm-05": ("review", ["invoice_math"]),
    "clm-06": ("review", ["vendor"]),
    "clm-07": ("review", ["authorized_amount"]),
    "clm-08": ("review", ["invoice_math"]),
    "clm-09": ("review", ["claim_match"]),
    "clm-10": ("checks_passed", []),
}


def check_examples() -> None:
    results = run({document: page(document) for document in EXPECTED_RESULTS})
    got = {r.document: (r.status, flagged(r)) for r in results}
    assert got == EXPECTED_RESULTS, got
    by_id = {r.document: r for r in results}
    assert "by 537.52" in detail(by_id["clm-07"], "authorized_amount")
    assert "Not read: tax (a blank tax line is not zero)" in detail(by_id["clm-08"], "invoice_math")
    assert "supplement 1180.00" in detail(by_id["clm-10"], "authorized_amount")
    # A claim that doesn't match runs no other check: they'd compare it with the wrong claim.
    assert [c.check for c in by_id["clm-09"].checks] == ["claim_match"]
    print("examples: all ten cases come out as the spec says on faithful OCR pages ok")


def check_tax_line() -> None:
    # The rate alone, with no amount, is a blank tax line.
    result = one("clm-08", fields={"Sales tax (6%)": None}, extra=[])
    assert flagged(result) == ["invoice_math"], result.checks
    assert "Not read: tax" in detail(result, "invoice_math")

    def rate_only(text: str) -> dict[str, str]:
        return {**stand_in_fields(text), "tax": "6%"}

    result = run({"clm-08": page("clm-08")}, fields=rate_only)[0]
    assert "Not read: tax" in detail(result, "invoice_math"), result.checks
    # The totals as plain lines, the way the live OCR wrote clm-08's, with the amount blank:
    # the cell after the label is only the rate.
    text = page("clm-08").split("<table><tr><td>Subtotal")[0] + (
        "Subtotal: 1,270.00  \nSales tax (6%)  \nTotal due: $1,346.20"
    )
    result = run({"clm-08": text}, fields=rate_only)[0]
    assert "Not read: tax" in detail(result, "invoice_math"), result.checks
    # "Tax exempt" is not an amount, so it's not zero either.
    result = one("clm-01", fields={"Sales tax (6%)": "exempt"})
    assert "invoice_math" in flagged(result), result.checks
    # A printed 0.00 is a real zero (clm-03's shape, dated after the loss).
    result = one("clm-03", fields={"Invoice date": "August 21, 2026"})
    assert result.status == "checks_passed", result.checks
    print("tax: blank, rate only and 'exempt' go to review; a printed 0.00 passes ok")


def check_near_misses() -> None:
    cases: list[tuple[str, dict[str, str | None], str, str]] = [
        # (document, change, check, words in the detail)
        ("clm-02", {"vendor": "Kestrel Auto Body"}, "vendor", "not a vendor on the claim"),
        ("clm-01", {"Claim number": "CLM-26-104392"}, "claim_match", "another claim (Tomasz"),
        (
            "clm-01",
            {"Claim number": "CLM-26-104817 / CLM-26-104818"},
            "claim_match",
            "invoice says",
        ),
        ("clm-01", {"Bill to": "D. Whitcombe"}, "claim_match", "a possible match only"),
        (
            "clm-01",
            {"Service address": "43 Fernhollow Lane, Ashby Vale, CO 80911"},
            "claim_match",
            "loss address",
        ),
        (
            "clm-01",
            {"Service address": "41 Fernhollow Lane, Ashby Vale, CO 80912"},
            "claim_match",
            "loss address",
        ),
        ("clm-02", {"Vehicle": "2021 Mazda CX-5, VIN JM3QKFBC1M0412388"}, "claim_match", "VIN"),
        ("clm-02", {"Vehicle": "2021 Mazda CX-5"}, "claim_match", "VIN"),
        (
            "clm-02",
            {"Vehicle": "2021 Mazda CX-5, VIN JM3QKFBC1M0412387 / 1HGQV1F34KA052265"},
            "claim_match",
            "alone",
        ),
        ("clm-01", {"Bill to": "Dana Whitcombe c/o Margit Allanby"}, "claim_match", "possible"),
        ("clm-01", {"Claim number": "CLM-26-104817-A"}, "claim_match", "invoice says"),
        ("clm-02", {"vendor": "Kestrel Auto Body and Paint, Inc. INVOICE"}, "vendor", "not a"),
        ("clm-01", {"Invoice number": "BRG-10442 BRG-10443"}, "invoice_number", "not one invoice"),
        ("clm-01", {"Invoice date": "October 21, 2026"}, "invoice_date", "after it arrived"),
        ("clm-01", {"Invoice date": "12/10/2026x"}, "invoice_date", "not a date"),
        ("clm-01", {"Total due": "$3,597.64 $3,650.00"}, "invoice_math", "Not one amount"),
        ("clm-01", {"Total due": "-$3,597.64"}, "invoice_math", "a credit or a misread sign"),
        ("clm-01", {"Claim number": None}, "claim_match", "no claim number"),
    ]
    for document, change, check, words in cases:
        result = one(document, fields=change)
        assert check in flagged(result), (change, result.checks)
        assert words in detail(result, check), (change, detail(result, check))
    # The same shop and person written another way still passes.
    same: list[tuple[str, dict[str, str | None]]] = [
        ("clm-02", {"vendor": "KESTREL AUTO BODY & PAINT INC"}),
        ("clm-01", {"Bill to": "Whitcombe, Dana"}),
        ("clm-01", {"Service address": "41 Fernhollow Ln, Ashby Vale, CO 80911"}),
        ("clm-01", {"Claim number": "clm-26-104817"}),
    ]
    for document, change in same:
        result = one(document, fields=change)
        assert result.status == "checks_passed", (change, result.checks)
    print("near misses: shorter vendor, other claim, neighbour address, VIN off by one ok")


def check_amounts() -> None:
    # A credit note: the arithmetic adds up, but a negative total is not a bill.
    items = [["Refund of deposit", "1", "-100.00", "-100.00"]]
    result = one(
        "clm-01",
        items=items,
        fields={"Subtotal": "-100.00", "Sales tax (6%)": "-6.00", "Total due": "-106.00"},
    )
    assert "a credit or a misread sign" in detail(result, "invoice_math"), result.checks
    # A line whose qty x unit price isn't its amount, and a row that lost a cell.
    items = [["Labor", "3", "85.00", "250.00"], ["Shingles", "24", "42.50", "1,020.00"]]
    result = one("clm-01", items=items)
    assert "3 x 85.00 = 255.00, not 250.00" in detail(result, "invoice_math"), result.checks
    items = [["Labor", "3", "85.00"], ["Shingles", "24", "42.50", "1,020.00"]]
    result = one("clm-01", items=items)
    assert "has 3 cells, the table has 4" in detail(result, "invoice_math"), result.checks
    # On the authorized amount to the cent passes; a cent and a half over goes to review.
    claim = "CLM-26-104817,estimate"
    result = run({"clm-01": page()}, authorizations=[f"{claim},3597.64,2026-10-08"])[0]
    assert result.status == "checks_passed", result.checks
    result = run({"clm-01": page()}, authorizations=[f"{claim},3597.62,2026-10-08"])[0]
    assert "over authorized 3597.62" in detail(result, "authorized_amount"), result.checks
    # Without its supplement, clm-10's total is over the first estimate by exactly 1,180.00.
    rows = ["CLM-26-224417,estimate,2779.00,2026-09-25"]
    result = run({"clm-10": page("clm-10")}, authorizations=rows)[0]
    assert "by 1180.00" in detail(result, "authorized_amount"), result.checks
    # A claim with nothing authorized yet can't pass.
    result = run({"clm-01": page()}, authorizations=[])[0]
    assert "authorized_amount" in flagged(result), result.checks
    print("amounts: credit, line arithmetic, lost cell, limit, supplement, nothing authorized ok")


def check_model_spans() -> None:
    # The model points at an amount that isn't on a total line: dropped, not used as the total.
    def subtotal_as_total(text: str) -> dict[str, str]:
        return {**stand_in_fields(text), "total": "3,394.00"}

    result = run({"clm-01": page()}, fields=subtotal_as_total)[0]
    assert any("'3,394.00' is not on a labelled line" in n for n in result.notes), result.notes
    assert "Not read: total" in detail(result, "authorized_amount"), result.checks

    # The model hands back a value nobody wrote.
    def invented(text: str) -> dict[str, str]:
        return {**stand_in_fields(text), "subtotal": "3,400.00"}

    result = run({"clm-01": page()}, fields=invented)[0]
    assert any("'3,400.00' is not in the text" in note for note in result.notes), result.notes
    assert "Not read: subtotal" in detail(result, "invoice_math")

    # The bill-to name offered as the vendor: the bill-to line is never the letterhead.
    def bill_to_as_vendor(text: str) -> dict[str, str]:
        return {**stand_in_fields(text), "vendor_name": "Dana Whitcombe"}

    result = run({"clm-01": page()}, fields=bill_to_as_vendor)[0]
    assert "Not read: vendor" in detail(result, "vendor"), result.checks

    # Negation: "Claim number: none" is not a claim number that matches anything.
    result = one("clm-01", fields={"Claim number": "none"})
    assert "invoice says claim none" in detail(result, "claim_match"), result.checks
    print("model spans: wrong line, invented value, bill-to as vendor, 'none' dropped ok")


def check_conflicts() -> None:
    """A field read twice with two values goes to review and isn't used."""
    text = page("clm-01", extra=["Balance due: $3,650.00"])
    seen: list[str] = []

    def two_values(window: str) -> dict[str, str]:
        seen.append(window)
        found = stand_in_fields(window)
        found["total"] = "3,650.00" if len(seen) == 1 else "3,597.64"
        return found

    rules_small = {**RULES, "thresholds": {**RULES["thresholds"], "window_words": 70}}
    with tempfile.TemporaryDirectory() as name:
        folder = Path(name)
        for path in DATA.iterdir():
            shutil.copy(path, folder / path.name)
        rows = (DATA / "claim_documents.csv").read_text(encoding="utf-8").splitlines()[:2]
        (folder / "claim_documents.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        client = StandIn({"ocr/repair-invoices/p1": text}, fields=two_values)
        with contextlib.redirect_stdout(io.StringIO()):
            results, _ = recipe.run(cast(SIEClient, client), rules_small, folder)
    result = results[0]
    assert len(seen) > 1, "the page should span two windows"
    assert "conflicting_values" in flagged(result), result.checks
    assert detail(result, "conflicting_values") == "total read as '$3,650.00' / '$3,597.64'"
    assert "Not read: total" in detail(result, "authorized_amount")
    print("conflicts: two totals in two windows go to review, neither used ok")


def check_statuses() -> None:
    source = (recipe.ROOT / "recipe.py").read_text(encoding="utf-8")
    rules = (recipe.ROOT / "rules.yaml").read_text(encoding="utf-8")
    for old in ("fast_track", "adjuster_review", "approved_for_payment"):
        assert old not in source, old
        assert old not in rules, old
    assert recipe.CHECKS_PASSED == "checks_passed"
    assert recipe.REVIEW == "review"
    results = run({"clm-01": page(), "clm-05": page("clm-05")})
    assert {r.status for r in results} == {"checks_passed", "review"}
    print("statuses: checks_passed and review only, no decision names ok")


def check_outputs() -> None:
    results = run({document: page(document) for document in ("clm-01", "clm-07", "clm-09")})
    with tempfile.TemporaryDirectory() as name:
        out = Path(name)
        recipe.write_outputs(out, results, RULES)
        with (out / "payment-queue.csv").open(encoding="utf-8") as handle:
            paid = list(csv.DictReader(handle))
        with (out / "exceptions-queue.csv").open(encoding="utf-8") as handle:
            exceptions = list(csv.DictReader(handle))
    assert [row["document"] for row in paid] == ["clm-01"], paid
    row = paid[0]
    assert (row["invoice_number"], row["invoice_date"], row["total"]) == (
        "BRG-10442",
        "2026-10-12",
        "3597.64",
    ), row
    assert [(r["document"], r["check"]) for r in exceptions] == [
        ("clm-07", "authorized_amount"),
        ("clm-09", "claim_match"),
    ], exceptions
    assert recipe.csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert recipe.csv_safe("-$3,597.64") == "'-$3,597.64"
    assert recipe.csv_safe("BRG-10442") == "BRG-10442"
    result = recipe.InvoiceResult("=cmd()", "CLM-1", make_data.RECEIVED)
    result.add("readable", "review", "+1 page")
    with tempfile.TemporaryDirectory() as name:
        recipe.write_outputs(Path(name), [result], RULES)
        line = (Path(name) / "exceptions-queue.csv").read_text(encoding="utf-8").splitlines()[1]
        assert line == "'=cmd(),CLM-1,readable,'+1 page", line
    print("outputs: payment queue fields, one exceptions row per check, csv_safe ok")


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
    two = {"clm-01": page(), "clm-02": page("clm-02")}
    # A transient error on one invoice's page: it goes to review, the other passes.
    for error in TRANSIENT:
        results = run(two, errors={"ocr/repair-invoices/p1": [error]})
        assert [r.status for r in results] == ["review", "checks_passed"], type(error).__name__
        reason = results[0].flagged[0]
        assert reason.check == "readable", reason
        assert "was not read: the SIE call failed: " in reason.detail, reason
    # A transient error on one extraction window: that invoice goes to review as `readable`.
    results = run(two, errors={"extract/clm-01-w1-fields": [ServerError("busy", status_code=503)]})
    assert "readable" in flagged(results[0])
    assert results[1].status == "checks_passed"
    results = run(two, errors={"extract/clm-01-w1-entities": [RateLimitError("slow")]})
    assert "readable" in flagged(results[0]), results[0].checks
    # A reply missing its key is a failed call on the same path; an empty one is not.
    results = run(two, replies={"ocr/repair-invoices/p1": {"something": []}})
    assert "replied without 'entities'" in results[0].flagged[0].detail, results[0].flagged
    results = run(two, replies={"extract/clm-01-w1-fields": {"no_data": {}}})
    assert "replied without 'data'" in detail(results[0], "readable"), results[0].checks
    results = run(two, replies={"extract/clm-01-w1-entities": {"entities": []}})
    assert "readable" not in flagged(results[0]), results[0].checks
    assert "no service address read" in detail(results[0], "claim_match")
    # SIE Cloud's empty-result 500 reads as "none found" from the extraction model only.
    empty = ServerError(recipe.EMPTY_RESULT_ERROR, status_code=500)
    results = run(two, errors={"extract/clm-01-w1-fields": [empty]})
    assert "readable" not in flagged(results[0])
    assert any("none found" in note for note in results[0].notes), results[0].notes
    results = run(two, errors={"ocr/repair-invoices/p1": [empty]})
    assert "the SIE call failed: ServerError" in detail(results[0], "readable")
    # An OCR reply with no markdown entity is a failed call: counted, so all of them stop.
    results = run(two, replies={"ocr/repair-invoices/p1": {"entities": []}})
    assert "replied without a markdown entity" in detail(results[0], "readable"), results[0]
    message = stopped(lambda: run(two, replies={"ocr/": {"entities": []}}))
    assert message.startswith("Every SIE call failed (2 of 2"), message
    # Every call failed: the run stops with the full message.
    long = "connection refused " + "x" * 300
    dead = {"": [SIEConnectionError(long) for _ in range(20)]}
    message = stopped(lambda: run(two, errors=dead))
    assert message.startswith("Every SIE call failed (2 of 2 to lightonai/LightOnOCR-3-4B)"), (
        message
    )
    assert long in message
    assert "Check SIE_BASE_URL and your network." in message, message
    busy = {"": [ModelLoadingError("loading") for _ in range(20)]}
    message = stopped(lambda: run(two, errors=busy))
    assert "may still be starting or SIE is busy; run again in a few minutes." in message
    # A refusal stops the run at once, with the full message, HTTP status and code.
    refusal = {"ocr/repair-invoices/p2": [InsufficientCreditsError("no credits " + "y" * 300)]}
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


def check_guards() -> None:
    # Every GLiNER2 call asks SIE to fail on a window over 512 tokens, never to cut it.
    client = StandIn({"ocr/repair-invoices/p1": page()})
    claims = ClaimsSystem(DATA)
    document = claims.waiting_invoices()[0]
    with contextlib.redirect_stdout(io.StringIO()):
        read = recipe.process_invoice(
            cast(SIEClient, client), recipe.Calls(), document, claims, RULES, DATA
        )
    extract_calls = {k: v for k, v in client.options.items() if k.startswith("extract/")}
    assert len(extract_calls) == 2, client.options
    assert all(v == {"overflow_policy": "error"} for v in extract_calls.values()), extract_calls
    assert read.status == "checks_passed", read.checks
    # A claim that is not open runs no check, even when every field matches.
    claim = claims.get_claim(document.claim_number)
    assert claim is not None
    result = recipe.InvoiceResult(document.document, document.claim_number, document.received)
    recipe.check_invoice(
        read.fields, dataclasses.replace(claim, status="closed"), claims, RULES, result
    )
    assert [(c.check, c.status) for c in result.checks] == [("claim_match", "review")]
    assert "closed, not open" in result.checks[0].detail, result.checks
    # A page shorter than min_page_chars is unreadable: no field is read from it.
    short_page = "Invoice BRG-10442 Total $3,597.64"
    assert len(short_page) < RULES["thresholds"]["min_page_chars"]
    result = run({"clm-01": short_page})[0]
    assert flagged(result) == ["readable"], result.checks
    assert detail(result, "readable").endswith("could not be read"), result.checks
    # The length is of the flattened text: table markup around the same words doesn't count.
    wrapped = f"<table><tbody><tr><td>{short_page}</td><td></td></tr></tbody></table>\n" * 2
    assert len(wrapped) >= RULES["thresholds"]["min_page_chars"]
    result = run({"clm-01": wrapped})[0]
    assert flagged(result) == ["readable"], result.checks
    # A document file in the seed must be a file name inside data/, not a path.
    with tempfile.TemporaryDirectory() as name:
        folder = Path(name)
        for path in DATA.iterdir():
            shutil.copy(path, folder / path.name)
        seed = folder / "claim_documents.csv"
        original = seed.read_text(encoding="utf-8")
        rows = seed.read_text(encoding="utf-8").replace(
            ",repair-invoices.pdf,", ",./repair-invoices.pdf,", 1
        )
        seed.write_text(rows, encoding="utf-8")
        error = raised(lambda: ClaimsSystem(folder))
        assert isinstance(error, ValueError), error
        assert "./repair-invoices.pdf" in str(error), error
        # The document ID becomes a file name in the run's outputs, so it can't be a path.
        for bad_id in ("../escape", "..", "sub/DOC"):
            seed.write_text(
                "\n".join(
                    ",".join([bad_id, *line.split(",")[1:]]) if i == 1 else line
                    for i, line in enumerate(original.splitlines())
                )
                + "\n",
                encoding="utf-8",
            )
            error = raised(lambda: ClaimsSystem(folder))
            assert isinstance(error, ValueError), (bad_id, error)
            assert "is not a plain name" in str(error), error
    print("guards: overflow is an error, closed claim, short page, document names and files ok")


def check_committed_data() -> None:
    raw = (DATA / "repair-invoices.pdf").read_bytes()
    for marker in (b"/Info", b"/Title", b"/Author", b"/CreationDate", b"Exif", b"xmpmeta"):
        assert marker not in raw, f"repair-invoices.pdf carries {marker!r}"
    claims = ClaimsSystem(DATA)
    for sample in make_data.CLAIMS:
        claim = claims.get_claim(sample.claim_number)
        assert claim is not None, sample.claim_number
        if claim.vin:
            assert "Q" in claim.vin, claim.vin  # real VINs never use Q
            assert len(claim.vin) == 17, claim.vin
    clm02 = claims.get_claim(CLAIMS["clm-02"].claim_number)
    assert clm02 is not None
    assert clm02.authorized == CLAIMS["clm-02"].invoice.total
    clm07 = claims.get_claim(CLAIMS["clm-07"].claim_number)
    assert clm07 is not None
    assert CLAIMS["clm-07"].invoice.total - clm07.authorized == Decimal("537.52")
    clm09 = claims.get_claim(CLAIMS["clm-09"].claim_number)
    assert clm09 is not None
    assert clm09.authorized == CLAIMS["clm-09"].invoice.total
    assert CLAIMS["clm-05"].invoice.subtotal == Decimal("2410.00")
    assert sum((i.amount for i in CLAIMS["clm-05"].invoice.items), Decimal("0")) == Decimal(
        "2140.00"
    )
    print("committed data: no PDF metadata, no real VINs, invoice amounts ok")


if __name__ == "__main__":
    check_parsing()
    check_claims_system()
    check_examples()
    check_tax_line()
    check_near_misses()
    check_amounts()
    check_model_spans()
    check_conflicts()
    check_statuses()
    check_outputs()
    check_sie_failure()
    check_guards()
    check_committed_data()
    print("all checks passed")
