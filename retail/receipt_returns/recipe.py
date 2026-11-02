"""Read receipt photos sent with returns and missing-points claims, and check them.

Use case:  Read receipt photos for returns and rewards.
Who:       Service-desk and loyalty-support staff at a store chain.
Problem:   Customers return items and claim missing loyalty points with a phone photo of the
           receipt. Staff read the store, date and lines off the photo, add it up, look for the
           returned item, count the days since purchase and work out the refund or the points by
           hand, then check the loyalty system for a receipt that was already credited.
Input:     data/requests.jsonl (one return or missing-points claim per line: member, request date,
           the returned item's SKU, and which page of receipt-photos.pdf holds the receipt),
           data/receipt-photos.pdf (one receipt photo per page) and
           data/credited-transactions.csv (transactions the loyalty system already credited).
           The desk's rules and thresholds are in rules.yaml.
SIE pipeline:
           extract  zai-org/GLM-OCR  read each whole receipt photo as text
Output:    requests/<request>.json per request (status, what was read from the receipt, every
           check with its detail, the refund or points), statuses.csv and service-desk.csv (one
           row per failed check). A status names what the checks found: ready_for_staff (every
           check passed; a person still decides) or service_desk (a check failed).
Why SIE:   One OCR model, called once per photo, on SIE Cloud. Everything after the text is plain
           Python: store, date, SKU lines, receipt arithmetic, the return window, the refund and
           the points. No generative model makes the decision.
Runs on:   SIE Cloud.

    uv run recipe.py
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

import pypdfium2 as pdfium
import yaml
from dotenv import load_dotenv
from sie_sdk import SIEClient
from sie_sdk.client.errors import (
    InputTooLongError,
    ModelLoadFailedError,
    RateLimitError,
    RequestError,
    SIEConnectionError,
    SIEError,
)
from sie_sdk.types import Item

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
# A failed SIE call's message is cut to this many characters in reasons and the summary line.
ERROR_CHARS = 200

# Statuses name what the checks found, not a business decision: every check passed and staff
# make the call, or a check failed and the service desk looks at it.
READY, DESK = "ready_for_staff", "service_desk"
RETURN, POINTS_CLAIM = "return", "points_claim"
CENT = Decimal("0.01")
# A money amount: "34.99", "-10.00", "$1,249.97". Not a percentage ("7.25%").
MONEY = re.compile(r"(?<![\d.])-?\$?\s?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}(?![\d%])")
AMOUNT_ONLY = re.compile(r"\s*-?\$?\s?[\d,]+\.\d{2}\s*")


# --- Requests, receipts and checks ------------------------------------------------------------


@dataclass(frozen=True)
class Page:
    file: str
    page: int


@dataclass
class Check:
    check: str
    passed: bool
    detail: str


@dataclass
class ItemLine:
    sku: str
    name: str
    amount: Decimal  # the line amount
    unit: Decimal  # one unit: the price after "@" on a quantity line, else the line amount
    line: str  # the OCR line


@dataclass
class Receipt:
    page: Page
    text: str = ""
    chain: str | None = None
    store: str | None = None
    transaction: str | None = None  # store-register-trans, "0412-2-08841"
    purchase_date: date | None = None
    member_last4: str | None = None
    items: list[ItemLine] = field(default_factory=list)
    discounts: list[Decimal] = field(default_factory=list)
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    tax_rate: Decimal | None = None  # percent, as printed
    total: Decimal | None = None
    unparsed: list[str] = field(default_factory=list)  # lines with an amount nothing explains

    def as_json(self) -> dict[str, Any]:
        return {
            "page": vars(self.page),
            "chain": self.chain,
            "store": self.store,
            "transaction": self.transaction,
            "purchase_date": self.purchase_date.isoformat() if self.purchase_date else None,
            "member_last4": self.member_last4,
            "items": [
                {"sku": i.sku, "name": i.name, "amount": str(i.amount), "unit": str(i.unit)}
                for i in self.items
            ],
            "discounts": [str(d) for d in self.discounts],
            "subtotal": _str(self.subtotal),
            "tax": _str(self.tax),
            "tax_rate_percent": _str(self.tax_rate),
            "total": _str(self.total),
            "unparsed_lines": self.unparsed,
        }


@dataclass
class RequestResult:
    request: str
    kind: str
    member: str
    requested_on: date
    sku: str | None
    description: str | None
    receipt: Receipt
    checks: list[Check] = field(default_factory=list)
    refund: Decimal | None = None  # returns: what the desk would refund
    points: int | None = None  # claims: points to credit; returns: points to take back (< 0)

    @property
    def failed(self) -> list[Check]:
        return [check for check in self.checks if not check.passed]

    @property
    def failed_names(self) -> list[str]:
        """Each failed check once, in the order first failed."""
        return list(dict.fromkeys(check.check for check in self.failed))

    @property
    def status(self) -> str:
        return DESK if self.failed else READY

    def add(self, check: str, passed: bool, detail: str) -> None:
        self.checks.append(Check(check, passed, detail))

    def as_json(self) -> dict[str, Any]:
        return {
            "request": self.request,
            "type": self.kind,
            "member": self.member,
            "requested_on": self.requested_on.isoformat(),
            "item": {"sku": self.sku, "description": self.description} if self.sku else None,
            "status": self.status,
            "failed_checks": self.failed_names,
            # Worked out whenever the receipt allows; statuses.csv shows it only for
            # ready_for_staff, and a person still decides.
            "computed": {"refund": _str(self.refund), "points": self.points},
            "receipt": self.receipt.as_json(),
            "checks": [vars(check) for check in self.checks],
        }


def _str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def load_requests(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def load_credited(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return {row["transaction"]: row for row in csv.DictReader(handle)}


def make_client() -> SIEClient:
    """Build an SIE client from the API key and SIE_BASE_URL in the environment or `.env`."""
    load_dotenv()
    api_key = os.environ.get("SIE_API_KEY", "").strip()
    base_url = os.environ.get("SIE_BASE_URL", "").strip()
    if not api_key and not base_url:
        raise RuntimeError("Set SIE_API_KEY, or SIE_BASE_URL for your own SIE, in .env")
    base_url = base_url or "https://api.superlinked.com"
    return SIEClient(api_key=api_key or None, base_url=base_url, timeout_s=WAIT_S)


# --- Read photos (GLM-OCR) --------------------------------------------------------------------


def _page_png(page: pdfium.PdfPage, dpi: int, max_edge: int) -> bytes:
    image = page.render(scale=dpi / 72).to_pil().convert("RGB")
    image.thumbnail((max_edge, max_edge))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def read_page(client: SIEClient, key: str, pdf: Path, page: int, rules: dict[str, Any]) -> str:
    """OCR one receipt photo, a page of a PDF (1-based page number)."""
    model, ocr = rules["models"]["ocr"], rules["ocr"]
    document = pdfium.PdfDocument(pdf)
    image = _page_png(document[page - 1], ocr["render_dpi"], ocr["max_image_edge_px"])
    response: Any = client.extract(model, Item(id=key, images=[image]), provision_timeout_s=WAIT_S)
    if not isinstance(response, dict) or not isinstance(response.get("entities"), list):
        # A reply without its entities list is a failed call, not a blank photo.
        raise MissingKeyError(f"the reply from {model} has no 'entities' list")
    for entity in response["entities"]:
        if entity.get("label") == "markdown":
            return str(entity.get("text", ""))
    return ""


def flatten(text: str) -> list[str]:
    """Turn the OCR output into plain lines.

    The text can come back with HTML tables and Markdown marks: each table row becomes one line
    and the marks are dropped. A lone amount on the line after its label ("TOTAL", then
    "118.63") is joined back onto the label's line.
    """

    def table(match: re.Match[str]) -> str:
        rows = [
            " ".join(
                re.sub(r"<[^>]+>", " ", cell).strip()
                for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
            )
            for row in re.findall(r"<tr[^>]*>(.*?)</tr>", match.group(0), re.S)
        ]
        return "\n" + "\n".join(rows) + "\n"

    text = re.sub(r"<table[^>]*>.*?</table>", table, text, flags=re.S)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"\*\*|^\s*#+\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*\|?\s*:?-{3,}.*$", "", text, flags=re.M)  # rules and table separators
    text = text.replace("|", " ")
    raw = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines: list[str] = []
    for line in raw:
        if not line:
            continue
        if lines and AMOUNT_ONLY.fullmatch(line) and not amounts(lines[-1]):
            lines[-1] = f"{lines[-1]} {line}"
        else:
            lines.append(line)
    return lines


# --- Parse the receipt (plain Python) ---------------------------------------------------------


def amounts(line: str) -> list[Decimal]:
    """Every money amount on a line, in order: "-10.00", "$1,249.97", "24.00"."""
    found: list[Decimal] = []
    for match in MONEY.finditer(line):
        try:
            found.append(Decimal(re.sub(r"[$,\s]", "", match.group())))
        except InvalidOperation:
            continue
    return found


def has_label(line: str, labels: list[str]) -> bool:
    lowered = line.casefold()
    return any(re.search(r"(?<![a-z])" + re.escape(label), lowered) for label in labels)


def parse_date(text: str, formats: list[str]) -> date | None:
    for candidate in re.findall(r"\d{1,4}[/.-]\d{1,2}[/.-]\d{2,4}", text):
        for fmt in formats:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def parse_receipt(page: Page, text: str, rules: dict[str, Any]) -> Receipt:
    """Store, date, transaction, member, SKU lines, discounts, tax and total from the OCR text."""
    receipt = Receipt(page, text)
    labels, spec = rules["labels"], rules["receipt"]
    item = re.compile(rf"^(\d{{{spec['sku_digits']}}})\s+(.+)$")
    chains = {name.casefold(): name for name in rules["chain_names"]}
    for line in flatten(text):
        lowered = line.casefold()
        found = amounts(line)
        if receipt.chain is None and lowered in chains:
            receipt.chain = chains[lowered]
        if receipt.purchase_date is None and "date" in lowered:
            receipt.purchase_date = parse_date(line, spec["date_formats"])
        if receipt.transaction is None and (
            ids := re.search(r"store\s*#\s*(\d+)\s+reg\s*(\d+)\s+trans\s*(\d+)", lowered)
        ):
            receipt.store = ids.group(1)
            receipt.transaction = "-".join(ids.groups())
        if receipt.member_last4 is None and (member := re.search(r"member\b.*?(\d{4})$", lowered)):
            receipt.member_last4 = member.group(1)
        if not found:
            continue
        if match := item.match(line):
            name = MONEY.sub("", match.group(2)).strip()
            unit = re.search(r"@\s*\$?([\d,]+\.\d{2})", line)
            name = re.sub(r"\s*\d+\s*@\s*$", "", name)
            receipt.items.append(
                ItemLine(
                    match.group(1),
                    name,
                    found[-1],
                    Decimal(unit.group(1).replace(",", "")) if unit else found[-1],
                    line,
                )
            )
        elif has_label(line, labels["subtotal"]):
            receipt.subtotal = found[-1]
        elif has_label(line, labels["total"]):
            if receipt.total is None:
                receipt.total = found[-1]
        elif has_label(line, labels["tax"]):
            receipt.tax = found[-1]
            rate = re.search(r"(\d+(?:\.\d+)?)\s*%", line)
            receipt.tax_rate = Decimal(rate.group(1)) if rate else None
        elif has_label(line, labels["discount"]):
            receipt.discounts.append(-abs(found[-1]))
        elif not has_label(line, labels["payment"]):
            receipt.unparsed.append(line)  # an amount on a line we don't understand
    return receipt


# --- Money rules (plain Python) ---------------------------------------------------------------


def _starts(sku: str, prefixes: list[str]) -> bool:
    return any(sku.startswith(prefix) for prefix in prefixes)


def eligible_spend(receipt: Receipt, rules: dict[str, Any]) -> Decimal:
    excluded = rules["points"]["excluded_prefixes"]
    items = sum((i.amount for i in receipt.items if not _starts(i.sku, excluded)), Decimal(0))
    return max(Decimal(0), items + sum(receipt.discounts, Decimal(0)))


def taxable_spend(receipt: Receipt, rules: dict[str, Any]) -> Decimal:
    """Item lines + receipt-level discounts, without SKU prefixes that carry no tax."""
    untaxed = rules["receipt"]["untaxed_prefixes"]
    items = sum((i.amount for i in receipt.items if not _starts(i.sku, untaxed)), Decimal(0))
    return items + sum(receipt.discounts, Decimal(0))


def points_for(amount: Decimal, rules: dict[str, Any]) -> int:
    return int(amount.quantize(Decimal(1), ROUND_DOWN)) * int(rules["points"]["per_dollar"])


def return_window(sku: str, rules: dict[str, Any]) -> tuple[str, int]:
    """(department, window in days) for a SKU; the longest matching prefix wins."""
    spec = rules["returns"]
    matches = [prefix for prefix in spec["departments"] if sku.startswith(prefix)]
    if not matches:
        return "general merchandise", int(spec["window_days"])
    department = spec["departments"][max(matches, key=len)]
    return str(department["name"]), int(department["window_days"])


def refund_for(item: ItemLine, receipt: Receipt, rules: dict[str, Any]) -> Decimal | None:
    """One unit, less its prorated share of receipt-level discounts, plus tax at the printed rate.
    None if the tax rate was not read."""
    if receipt.tax_rate is None:
        return None
    pretax = refund_pretax(item, receipt, rules)
    tax = (pretax * receipt.tax_rate / 100).quantize(CENT, ROUND_HALF_UP)
    return pretax + tax


def refund_pretax(item: ItemLine, receipt: Receipt, rules: dict[str, Any]) -> Decimal:
    excluded = rules["returns"]["not_returnable_prefixes"]
    base = sum((i.amount for i in receipt.items if not _starts(i.sku, excluded)), Decimal(0))
    discounts = sum(receipt.discounts, Decimal(0))
    share = (item.unit * discounts / base).quantize(CENT, ROUND_HALF_UP) if base else Decimal(0)
    return item.unit + share


# --- Checks (plain Python) --------------------------------------------------------------------


def check_receipt(result: RequestResult, rules: dict[str, Any]) -> None:
    receipt = result.receipt
    missing = [
        label
        for label, value in (
            ("store", receipt.chain and receipt.store),
            ("purchase date", receipt.purchase_date),
            ("transaction", receipt.transaction),
            ("total", receipt.total),
            ("a priced item", receipt.items),
        )
        if not value
    ]
    if missing:
        result.add("receipt_fields", False, "Not read: " + ", ".join(missing))
    else:
        result.add(
            "receipt_fields",
            True,
            f"{receipt.chain} #{receipt.store}, {receipt.purchase_date}, "
            f"trans {receipt.transaction}, {len(receipt.items)} items",
        )

    if receipt.chain is None or receipt.store is None:
        result.add("receipt_store", False, "Chain name or store number not read")
    elif receipt.store not in {str(store) for store in rules["stores"]}:
        result.add("receipt_store", False, f"Store #{receipt.store} is not one of our stores")
    else:
        result.add("receipt_store", True, f"{receipt.chain} #{receipt.store}")

    tolerance = Decimal(rules["receipt"]["money_tolerance"])
    if receipt.total is None or not receipt.items or receipt.tax is None:
        result.add("receipt_math", False, "Total, tax or items not read; cannot add up")
        return
    if receipt.unparsed:
        result.add(
            "receipt_math",
            False,
            "Lines with an amount not understood: " + "; ".join(receipt.unparsed),
        )
        return
    items = sum((item.amount for item in receipt.items), Decimal(0))
    discounts = sum(receipt.discounts, Decimal(0))
    problems = []
    for item in receipt.items:
        quantity = re.search(r"(\d+)\s*@", item.line)
        if quantity and abs(int(quantity.group(1)) * item.unit - item.amount) > tolerance:
            problems.append(f"{item.sku}: {quantity.group(1)} @ {item.unit} is not {item.amount}")
    if receipt.subtotal is not None and abs(items + discounts - receipt.subtotal) > tolerance:
        problems.append(f"items {items} and discounts {discounts} vs subtotal {receipt.subtotal}")
    added = items + discounts + receipt.tax
    if abs(added - receipt.total) > tolerance:
        problems.append(f"items, discounts and tax add up to {added}, total says {receipt.total}")
    if receipt.tax_rate is not None:
        # The printed rate must explain the printed tax, within a cent of rounding. A misread
        # rate, or a basket with tax-exempt lines, fails here instead of giving a wrong refund.
        taxable = taxable_spend(receipt, rules)
        expected = (taxable * receipt.tax_rate / 100).quantize(CENT, ROUND_HALF_UP)
        if abs(expected - receipt.tax) > max(tolerance, CENT):
            problems.append(
                f"tax {receipt.tax} is not {receipt.tax_rate}% of taxable {taxable} ({expected})"
            )
    result.add(
        "receipt_math",
        not problems,
        "; ".join(problems)
        or f"{len(receipt.items)} items {items}, discounts {discounts}, tax {receipt.tax}"
        f" = total {receipt.total}",
    )


def _days(result: RequestResult) -> int | None:
    bought = result.receipt.purchase_date
    return None if bought is None else (result.requested_on - bought).days


def check_return(result: RequestResult, rules: dict[str, Any]) -> None:
    receipt, sku = result.receipt, result.sku or ""
    lines = [item for item in receipt.items if item.sku == sku]
    line = next((item for item in lines if item.amount > 0), None)
    returnable = False
    if not lines:
        words = set(re.findall(r"[a-z]{3,}", (result.description or "").casefold()))
        similar = [
            f"{item.sku} {item.name}"
            for item in receipt.items
            if words & set(re.findall(r"[a-z]{3,}", item.name.casefold()))
        ]
        detail = f"SKU {sku} is not on the receipt"
        if similar:
            detail += "; similar lines with another SKU: " + ", ".join(similar)
        result.add("returned_item", False, detail)
    elif line is None or any(item.amount < 0 for item in lines):
        # A voided or refunded line ("VOID -20.00") for this SKU: the unit may already be
        # taken off the receipt, so it is not refunded twice.
        amounts_read = ", ".join(str(item.amount) for item in lines)
        result.add(
            "returned_item",
            False,
            f"SKU {sku} has a voided or negative line on the receipt ({amounts_read})",
        )
    elif _starts(sku, rules["returns"]["not_returnable_prefixes"]):
        result.add("returned_item", False, f"{sku} {line.name} is not returnable")
    else:
        returnable = True
        result.add("returned_item", True, f"{sku} {line.name} at {line.unit}")

    department, window = return_window(sku, rules)
    days = _days(result)
    if days is None:
        result.add("return_window", False, "Purchase date not read")
    else:
        result.add(
            "return_window",
            0 <= days <= window,
            f"{days} days after purchase; {department} window is {window} days",
        )

    if returnable and line is not None:  # a refund is worked out only for a returnable line
        result.refund = refund_for(line, receipt, rules)
        result.points = -points_for(refund_pretax(line, receipt, rules), rules)
        if result.refund is not None and (receipt.total is None or result.refund > receipt.total):
            result.add(
                "refund_worked_out",
                False,
                f"Refund {result.refund} is more than the receipt total {receipt.total}",
            )
        elif result.refund is not None:
            result.add(
                "refund_worked_out",
                True,
                f"{result.refund}: {refund_pretax(line, receipt, rules)} before tax, "
                f"tax at {receipt.tax_rate}%",
            )
        elif receipt.tax is not None:
            result.add(
                "refund_worked_out",
                False,
                f"Tax {receipt.tax} read with no rate; the refund needs the printed rate",
            )
        else:
            result.add("refund_worked_out", False, "Tax not read; cannot work out the refund")


def check_points_claim(
    result: RequestResult, credited: dict[str, dict[str, str]], rules: dict[str, Any]
) -> None:
    receipt = result.receipt
    window = int(rules["points"]["claim_window_days"])
    days = _days(result)
    if days is None:
        result.add("claim_window", False, "Purchase date not read")
    else:
        result.add(
            "claim_window", 0 <= days <= window, f"{days} days after purchase; limit {window}"
        )

    last4 = re.sub(r"\D", "", result.member)[-4:]
    if receipt.member_last4 is None:
        result.add("receipt_member", False, "No member number read on the receipt")
    else:
        result.add(
            "receipt_member",
            receipt.member_last4 == last4,
            f"Receipt member ends {receipt.member_last4}; claimant {result.member}",
        )

    if receipt.transaction is None:
        result.add("not_credited", False, "Transaction not read; cannot look it up")
    elif receipt.transaction in credited:
        row = credited[receipt.transaction]
        result.add(
            "not_credited",
            False,
            f"Trans {receipt.transaction} already credited {row['points']} points to "
            f"{row['member']} on {row['credited_on']}",
        )
    else:
        result.add("not_credited", True, f"Trans {receipt.transaction} not credited yet")

    if receipt.items:
        result.points = points_for(eligible_spend(receipt, rules), rules)


def check_request(
    result: RequestResult, credited: dict[str, dict[str, str]], rules: dict[str, Any]
) -> None:
    """Every check for one request; each failure becomes a service-desk item."""
    check_receipt(result, rules)
    if result.kind == RETURN:
        check_return(result, rules)
    elif result.kind == POINTS_CLAIM:
        check_points_claim(result, credited, rules)
    else:
        # Not checked as a claim or a return: a person reads it.
        result.add(
            "request_type",
            False,
            f"unknown request type {result.kind!r}; expected {RETURN!r} or {POINTS_CLAIM!r}",
        )


# --- Run --------------------------------------------------------------------------------------


def unreadable_reason(text: str, rules: dict[str, Any]) -> str | None:
    """Why the OCR text can't be trusted, or None. On a blurred photo GLM-OCR can return
    invented text that runs into one character repeated thousands of times; a separator line of
    dashes or stars is not that."""
    spec = rules["ocr"]
    chars = len("\n".join(flatten(text)))
    if chars < spec["min_chars"]:
        return f"only {chars} characters read"
    run = re.search(rf"([^\s\-=_*.])\1{{{spec['max_repeat_run'] - 1},}}", text)
    if run:
        return f"OCR output repeats '{run.group(1)}' {len(run.group(0))} times; photo not legible"
    return None


def process_request(
    raw: dict[str, Any],
    text: str,
    credited: dict[str, dict[str, str]],
    rules: dict[str, Any],
    failed_call: SIEError | None = None,
) -> RequestResult:
    """Every check for one request, given the OCR text of its receipt photo. A failed SIE call
    (``failed_call``) fails `readable` with the error, so the request goes to the desk."""
    page = Page(**raw["receipt"])
    item = raw.get("item") or {}
    result = RequestResult(
        request=raw["request"],
        kind=raw["type"],
        member=raw["member"],
        requested_on=date.fromisoformat(raw["requested_on"]),
        sku=item.get("sku"),
        description=item.get("description"),
        receipt=Receipt(page, text),
    )
    problem = (
        f"the SIE call failed: {short(failed_call)}"
        if failed_call is not None
        else unreadable_reason(text, rules)
    )
    result.add("readable", problem is None, f"{page.file} p{page.page}: {problem or 'read'}")
    if problem is None:
        result.receipt = parse_receipt(page, text, rules)
    check_request(result, credited, rules)
    return result


def run(client: SIEClient, rules: dict[str, Any], data_dir: Path = DATA_DIR) -> list[RequestResult]:
    """Read each request's receipt photo with one SIE call and check it.

    A call that fails for a reason that may pass (a lost connection, a timeout, a server error,
    a rate limit, a reply with no entities list) sends that request to the service desk with
    the error, and the run still writes its outputs. Two cases stop the run with SIEStopped
    instead: a refusal every call would repeat (see `is_fatal`), and a run where no call
    succeeded at all, since then no receipt was read. An error that isn't an SIE error is a bug
    in this code and is raised as is.
    """
    model = rules["models"]["ocr"]
    credited = load_credited(data_dir / "credited-transactions.csv")
    results: list[RequestResult] = []
    failures: list[SIEError] = []
    calls = 0
    for raw in load_requests(data_dir / "requests.jsonl"):
        page = Page(**raw["receipt"])
        calls += 1
        try:
            key = f"ocr/{raw['request']}"
            text = read_page(client, key, data_dir / page.file, page.page, rules)
        except SIEError as error:
            if is_fatal(error):
                raise SIEStopped(stop_message(error, model)) from error
            failures.append(error)
            results.append(process_request(raw, "", credited, rules, failed_call=error))
            continue
        results.append(process_request(raw, text, credited, rules))
    if failures and len(failures) == calls:
        raise SIEStopped(
            f"Every SIE call failed ({calls} of {calls} to {model}), so the run stops: "
            f"{describe(failures[0])}: {failures[0]}. {every_call_failed_advice(failures[0])} "
            "No outputs were written."
        ) from failures[0]
    if failures:
        n = len(failures)
        print(
            f"{n} of {calls} SIE calls failed; their requests go to the service desk. "
            f"First: {short(failures[0])}"
        )
    return results


# --- SIE failures -----------------------------------------------------------------------------


class MissingKeyError(SIEError):  # type: ignore[misc]  # sie_sdk ships no type stubs
    """A reply without the key the recipe reads. It goes down the transient path: that request
    goes to the service desk, and the run goes on."""


def every_call_failed_advice(error: SIEError) -> str:
    """What to check when every SIE call failed, judged by the first failure.

    A lost connection points at SIE_BASE_URL and the network. Any other transient error (a
    timeout, a model still loading, a 5xx, a rate limit) more likely means the model is still
    starting or SIE is busy, so the advice is to wait and run again.
    """
    if isinstance(error, SIEConnectionError):
        return "Check SIE_BASE_URL and your network."
    return "The model may still be starting or SIE is busy; run again in a few minutes."


class SIEStopped(RuntimeError):
    """SIE refused the call for a reason a retry won't fix, or no call succeeded; the run
    stops without outputs."""


def is_fatal(error: SIEError) -> bool:
    """A refusal that fails every call the same way: a 4xx (bad key 401, forbidden 403, model
    not found 404, no credits or spend limit 402, inactive account) or a model the server
    marked as permanently unable to load. A RequestError with no HTTP status or a status below
    400 is a reply the SDK couldn't parse, and is fatal too. A rate limit, an over-long input, a
    timeout, a lost connection, a model still loading and a 5xx are transient or per-request:
    those requests go to the service desk."""
    if isinstance(error, ModelLoadFailedError):
        return bool(error.permanent)
    return isinstance(error, RequestError) and not isinstance(
        error, (RateLimitError, InputTooLongError)
    )


def describe(error: SIEError) -> str:
    """The error's class, with its HTTP status and SIE error code when the SDK has them."""
    details = [
        f"{label} {value}"
        for label, value in (
            ("HTTP", getattr(error, "status_code", None)),
            ("code", getattr(error, "code", None)),
        )
        if value is not None
    ]
    return f"{type(error).__name__}" + (f" ({', '.join(details)})" if details else "")


def short(error: SIEError) -> str:
    """The error for a per-request reason or the summary line, its message cut short."""
    return f"{describe(error)}: {str(error)[:ERROR_CHARS]}"


def stop_message(error: SIEError, model: str) -> str:
    """The message a fatal error stops the run with: SIE's full message, status and code, and
    what to check.

    A RequestError with no HTTP error status is a reply the SDK couldn't parse (a malformed or
    wrongly shaped body), not a refusal, so the key and model name are not the thing to check.
    """
    status = getattr(error, "status_code", None)
    if isinstance(error, ModelLoadFailedError):
        return (
            f"SIE can't load {model}, {describe(error)}: {error}. Check the model name in "
            "rules.yaml, then run again. No outputs were written."
        )
    if status is None or status < 400:
        return (
            f"SIE returned a reply the recipe couldn't read for {model}, {describe(error)}: "
            f"{error}. Check that SIE_BASE_URL points at an SIE server, then run again. No "
            "outputs were written."
        )
    return (
        f"SIE refused the call to {model}, {describe(error)}: {error}. Check SIE_API_KEY, the "
        "model name in rules.yaml and your SIE account, then run again. No outputs were written."
    )


def stop_on_refusal[T](work: Callable[[], T]) -> T:
    """Run the work; turn SIEStopped into a clean exit. Any other error keeps its traceback."""
    try:
        return work()
    except SIEStopped as error:
        raise SystemExit(f"Stopped: {error}") from None


def write_outputs(output_dir: Path, results: list[RequestResult]) -> None:
    (output_dir / "requests").mkdir(parents=True, exist_ok=True)
    for result in results:
        path = output_dir / "requests" / f"{result.request}.json"
        path.write_text(json.dumps(result.as_json(), indent=2) + "\n", encoding="utf-8")
    with (output_dir / "statuses.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["request", "type", "status", "failed_checks", "refund", "points"])
        for result in results:
            ready = result.status == READY
            writer.writerow(
                [
                    result.request,
                    result.kind,
                    result.status,
                    ";".join(result.failed_names),
                    _str(result.refund) if ready and result.refund is not None else "",
                    result.points if ready and result.points is not None else "",
                ]
            )
    with (output_dir / "service-desk.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["request", "check", "detail"])
        for result in results:
            for check in result.failed:
                writer.writerow([result.request, check.check, check.detail])


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    with make_client() as client:
        results = stop_on_refusal(lambda: run(client, load_rules()))
    write_outputs(output, results)
    for result in results:
        reasons = "; ".join(f"{check.check}: {check.detail}" for check in result.failed)
        money = f"refund {result.refund}" if result.kind == RETURN else ""
        points = f"points {result.points:+d}" if result.points is not None else ""
        summary = reasons or ", ".join(part for part in (money, points) if part)
        print(f"{result.request:<9} {result.status:<15} {summary or '-'}")
    ready = sum(result.status == READY for result in results)
    print(f"\n{len(results)} requests: {ready} ready for staff, {len(results) - ready} to the desk")
    print("ready_for_staff = every check passed; staff inspect the item and decide")
    print(f"requests:     {output / 'requests'}\nservice desk: {output / 'service-desk.csv'}")


if __name__ == "__main__":
    main()
