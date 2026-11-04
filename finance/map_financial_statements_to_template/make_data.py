"""Write the synthetic borrower statements and the loan system's seeds in data/.

Everything is invented: the four borrowers, their accountants and every figure. Each page says
so in its footer. The statements follow the usual shape of a small company's annual accounts in
three label styles: a UK company ("Turnover", "Creditors: amounts falling due within one year",
unlabelled subtotals, a prior-year column), a US small business ("Owner's equity", a "$" column,
a draw written "-96,500") and a mid-sized US company reporting "in thousands".

    uv run --group data make_data.py

writes data/statements/<borrower id>.pdf (the income statement and balance sheet as the
borrower sent them through the lending portal, two pages each), data/loan_borrowers.csv and
data/loan_mappings.csv (what the loan system knows: each borrower's fiscal year end, currency
and the period this spread is for, and the labels an analyst mapped and confirmed on last year's
spread). Three PDFs are written by hand with a text layer; one (BRW-40562) is drawn with Pillow
as an image-only PDF, the way a scanner would give it. No PDF carries document info (no title,
author, producer or dates), so rerunning rebuilds every file byte for byte.
"""

from __future__ import annotations

import csv
import random
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
STATEMENTS_DIR = DATA_DIR / "statements"
FOOTER = "Fictional company and figures, generated for a software demo. Not real accounts."

# A4 in PDF points; the scanned page is drawn at SCAN_DPI.
PAGE_W, PAGE_H = 595, 842
SCAN_DPI = 150
LEFT, INDENT = 64, 80
COL_RIGHT = (440, 530)  # right edge of the current and prior-year amount columns
CURRENCY_X = (376, 466)  # where a "$" sits in its column, left of the amount
# Helvetica widths (per 1000 em) for the characters that appear in amounts, for right alignment.
WIDTHS = {**dict.fromkeys("0123456789$£", 556), ",": 278, ".": 278, " ": 278}
WIDTHS.update({"(": 333, ")": 333, "-": 333})


# --- The statements ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One printed row. A heading has no amount; a total's amount is worked out from `parts`
    (keys of earlier rows, "-" in front to subtract) unless `printed` overrides it."""

    key: str
    label: str
    kind: str = "line"  # heading | line | total
    value: int | None = None
    parts: tuple[str, ...] = ()
    printed: int | None = None  # a total printed with this amount instead of its true sum
    prior: int | None = None  # prior-year amount, when it isn't `value * prior_factor`
    indent: bool = False
    currency: bool = False  # print the currency sign in front of the amount


@dataclass(frozen=True)
class Page:
    title: str
    period: str  # the line under the title
    rows: tuple[Row, ...]


@dataclass(frozen=True)
class Borrower:
    borrower_id: str
    name: str
    fiscal_year_end: str  # MM-DD, as the loan system keeps it
    currency: str
    period_due: str  # the fiscal year end this spread is for
    pages: tuple[Page, Page]
    headers: tuple[str, ...]  # the amount column headers, current year first
    negative: str = "parens"  # parens: (8,700); minus: -96,500
    sign: str = ""  # currency sign printed in the amount column ("$"), or ""
    prior_factor: tuple[int, int] | None = None  # prior year = value * a / b (exact)
    scanned: bool = False
    mappings: tuple[tuple[str, str, str, str], ...] = ()  # statement, heading, label, item
    planted: str = ""


def line(key: str, label: str, value: int, **kw: object) -> Row:
    return Row(key, label, "line", value, **kw)  # type: ignore[arg-type]


def total(key: str, label: str, *parts: str, **kw: object) -> Row:
    return Row(key, label, "total", None, parts, **kw)  # type: ignore[arg-type]


def heading(label: str) -> Row:
    return Row("", label, "heading")


SUB = ""  # an unlabelled subtotal, as UK accounts print them
# Last year's confirmed mappings for Harbour Lane; "Distribution costs" and the "Bank loans"
# due within one year are new this year, so they have none.
WITHIN = "Creditors: amounts falling due within one year"
AFTER = "Creditors: amounts falling due after more than one year"


HARBOUR_LANE = Borrower(
    borrower_id="BRW-20417",
    name="Harbour Lane Joinery Ltd",
    fiscal_year_end="03-31",
    currency="GBP",
    period_due="2026-03-31",
    headers=("2026 £", "2025 £"),
    prior_factor=(9, 10),
    planted="UK labels mapped last year; a new 'Bank loans' due within one year",
    pages=(
        Page(
            "Profit and loss account",
            "for the year ended 31 March 2026",
            (
                line("turnover", "Turnover", 2_846_300),
                line("cos", "Cost of sales", 1_912_400),
                total("gp", "Gross profit", "turnover", "-cos"),
                line("dist", "Distribution costs", 118_600),
                line("admin", "Administrative expenses", 512_700),
                total("op", "Operating profit", "gp", "-dist", "-admin"),
                line("interest", "Interest payable and similar expenses", 41_800),
                total("pbt", "Profit before taxation", "op", "-interest"),
                line("tax", "Tax on profit", 62_600),
                total("pat", "Profit for the financial year", "pbt", "-tax"),
            ),
        ),
        Page(
            "Balance sheet",
            "as at 31 March 2026",
            (
                heading("Fixed assets"),
                line("tangible", "Tangible assets", 864_500, indent=True),
                heading("Current assets"),
                line("stocks", "Stocks", 288_400, indent=True),
                line("debtors", "Debtors", 471_900, indent=True),
                line("cash", "Cash at bank and in hand", 96_300, indent=True),
                total("ca", SUB, "stocks", "debtors", "cash"),
                heading(WITHIN),
                line("loan_now", "Bank loans", 120_000, indent=True, prior=0),
                line("trade", "Trade creditors", 264_800, indent=True),
                line("dla", "Directors' loan account", 45_000, indent=True),
                line("accruals", "Other creditors and accruals", 98_700, indent=True),
                total("cl", SUB, "loan_now", "trade", "dla", "accruals"),
                total("nca", "Net current assets", "ca", "-cl"),
                heading(AFTER),
                line("loan_later", "Bank loans", 480_000, indent=True, prior=648_000),
                total("na", "Net assets", "tangible", "nca", "-loan_later"),
                heading("Capital and reserves"),
                line("share", "Called up share capital", 50_000, indent=True, prior=50_000),
                line("pl", "Profit and loss account", 662_600, indent=True, prior=483_340),
                total("funds", "Shareholders' funds", "share", "pl"),
            ),
        ),
    ),
    mappings=(
        ("income_statement", "", "Turnover", "revenue"),
        ("income_statement", "", "Cost of sales", "cost_of_revenue"),
        ("income_statement", "", "Gross profit", "gross_profit"),
        ("income_statement", "", "Administrative expenses", "sga"),
        ("income_statement", "", "Operating profit", "operating_income"),
        ("income_statement", "", "Interest payable and similar expenses", "interest_expense"),
        ("income_statement", "", "Profit before taxation", "pretax_income"),
        ("income_statement", "", "Tax on profit", "income_tax"),
        ("income_statement", "", "Profit for the financial year", "net_income"),
        ("balance_sheet", "Fixed assets", "Tangible assets", "ppe_net"),
        ("balance_sheet", "Current assets", "Stocks", "inventory"),
        ("balance_sheet", "Current assets", "Debtors", "accounts_receivable"),
        ("balance_sheet", "Current assets", "Cash at bank and in hand", "cash"),
        ("balance_sheet", WITHIN, "Trade creditors", "accounts_payable"),
        (
            "balance_sheet",
            WITHIN,
            "Directors' loan account",
            "accrued_and_other_current_liabilities",
        ),
        (
            "balance_sheet",
            WITHIN,
            "Other creditors and accruals",
            "accrued_and_other_current_liabilities",
        ),
        ("balance_sheet", "", "Net current assets", "working_capital"),
        ("balance_sheet", AFTER, "Bank loans", "long_term_debt"),
        ("balance_sheet", "", "Net assets", "total_equity"),
        ("balance_sheet", "Capital and reserves", "Called up share capital", "share_capital"),
        ("balance_sheet", "Capital and reserves", "Profit and loss account", "retained_earnings"),
        ("balance_sheet", "", "Shareholders' funds", "total_equity"),
    ),
)

MESA_VERDE = Borrower(
    borrower_id="BRW-31186",
    name="Mesa Verde Landscaping LLC",
    fiscal_year_end="12-31",
    currency="USD",
    period_due="2025-12-31",
    headers=("2025",),
    negative="minus",
    sign="$",
    planted="US small business; 'Other income' shown negative in an expense list; draws -96,500",
    pages=(
        Page(
            "Statement of Income",
            "For the year ended December 31, 2025",
            (
                line("sales", "Sales", 1_486_200, currency=True),
                line("cogs", "Cost of goods sold", 912_700),
                total("gp", "Gross profit", "sales", "-cogs"),
                heading("Operating expenses:"),
                line("wages", "Salaries and wages", 214_800, indent=True),
                line("rent", "Rent", 48_000, indent=True),
                line("vehicles", "Vehicle and equipment expenses", 61_300, indent=True),
                line("dep", "Depreciation", 39_600, indent=True),
                line("other_opex", "Other operating expenses", 27_900, indent=True),
                total(
                    "opex",
                    "Total operating expenses",
                    "wages",
                    "rent",
                    "vehicles",
                    "dep",
                    "other_opex",
                ),
                total("oi", "Operating income", "gp", "-opex"),
                heading("Other (income) expense:"),
                line("interest", "Interest expense", 22_400, indent=True),
                line("other_income", "Other income", -8_700, indent=True),
                total("other", "Total other (income) expense", "interest", "other_income"),
                total("ni", "Net income", "oi", "-other", currency=True),
            ),
        ),
        Page(
            "Balance Sheet",
            "December 31, 2025",
            (
                heading("ASSETS"),
                heading("Current assets:"),
                line("cash", "Cash", 84_600, indent=True, currency=True),
                line("ar", "Accounts receivable", 196_300, indent=True),
                line("inv", "Inventory", 41_200, indent=True),
                line("prepaid", "Prepaid expenses", 9_800, indent=True),
                total("ca", "Total current assets", "cash", "ar", "inv", "prepaid"),
                line("ppe", "Property and equipment, net", 412_700),
                total("ta", "Total assets", "ca", "ppe", currency=True),
                heading("LIABILITIES AND OWNER'S EQUITY"),
                heading("Current liabilities:"),
                line("ap", "Accounts payable", 72_400, indent=True, currency=True),
                line("accrued", "Accrued expenses", 18_900, indent=True),
                line("loc", "Line of credit", 60_000, indent=True),
                line("cpltd", "Current maturities of long-term debt", 54_000, indent=True),
                total("cl", "Total current liabilities", "ap", "accrued", "loc", "cpltd"),
                line("ltd", "Long-term debt, less current maturities", 186_000),
                total("tl", "Total liabilities", "cl", "ltd"),
                heading("Owner's equity:"),
                line("capital", "Owner's capital", 150_000, indent=True),
                line("earnings", "Accumulated earnings", 299_800, indent=True),
                line("draws", "Owner's draws", -96_500, indent=True),
                total("te", "Total owner's equity", "capital", "earnings", "draws"),
                total("tle", "Total liabilities and owner's equity", "tl", "te", currency=True),
            ),
        ),
    ),
)

NORTHFIELD = Borrower(
    borrower_id="BRW-18820",
    name="Northfield Cold Storage, Inc.",
    fiscal_year_end="12-31",
    currency="USD",
    period_due="2025-12-31",
    headers=("2025",),
    planted="'in thousands'; total current liabilities printed as 7,598, its lines add to 7,538",
    pages=(
        Page(
            "Statement of Operations",
            "Year ended December 31, 2025 (in thousands of U.S. dollars)",
            (
                line("rev", "Net revenues", 38_412),
                line("cor", "Cost of revenues", 27_906),
                total("gp", "Gross profit", "rev", "-cor"),
                line("sga", "Selling, general and administrative", 5_884),
                line("da", "Depreciation and amortization", 2_215),
                total("opex", "Total operating expenses", "sga", "da"),
                total("oi", "Income from operations", "gp", "-opex"),
                line("interest", "Interest expense, net", 1_128),
                line("other", "Other income, net", 164),
                total("pbt", "Income before income taxes", "oi", "-interest", "other"),
                line("tax", "Provision for income taxes", 361),
                total("ni", "Net income", "pbt", "-tax"),
            ),
        ),
        Page(
            "Balance Sheet",
            "December 31, 2025 (in thousands of U.S. dollars)",
            (
                heading("ASSETS"),
                heading("Current assets:"),
                line("cash", "Cash and cash equivalents", 2_318, indent=True),
                line("ar", "Accounts receivable, net", 5_947, indent=True),
                line("inv", "Inventories", 1_206, indent=True),
                line("prepaid", "Prepaid expenses and other current assets", 684, indent=True),
                total("ca", "Total current assets", "cash", "ar", "inv", "prepaid"),
                line("ppe", "Property, plant and equipment, net", 31_742),
                line("intang", "Intangible assets, net", 1_380),
                line("other_assets", "Other assets", 412),
                total("ta", "Total assets", "ca", "ppe", "intang", "other_assets"),
                heading("LIABILITIES AND STOCKHOLDERS' EQUITY"),
                heading("Current liabilities:"),
                line("ap", "Accounts payable", 3_412, indent=True),
                line("accrued", "Accrued liabilities", 1_876, indent=True),
                line("cpltd", "Current portion of long-term debt", 2_250, indent=True),
                # The planted subtotal: the lines add up to 7,538.
                total("cl", "Total current liabilities", "ap", "accrued", "cpltd", printed=7_598),
                line("ltd", "Long-term debt, net of current portion", 18_600),
                line("dtl", "Deferred income taxes", 1_904),
                total("tl", "Total liabilities", "cl", "ltd", "dtl"),
                heading("Stockholders' equity:"),
                line("common", "Common stock", 500, indent=True),
                line("apic", "Additional paid-in capital", 4_500, indent=True),
                line("re", "Retained earnings", 10_587, indent=True),
                total("te", "Total stockholders' equity", "common", "apic", "re"),
                total("tle", "Total liabilities and stockholders' equity", "tl", "te"),
            ),
        ),
    ),
)

KESTREL_VALLEY = Borrower(
    borrower_id="BRW-40562",
    name="Kestrel Valley Dairy Co.",
    fiscal_year_end="12-31",
    currency="USD",
    period_due="2025-12-31",
    headers=("2026",),
    sign="$",
    scanned=True,
    planted="scanned, no text layer; statements for the year ended June 30, 2026",
    pages=(
        Page(
            "Statement of Operations",
            "For the year ended June 30, 2026",
            (
                line("rev", "Revenue", 2_214_600, currency=True),
                line("cor", "Cost of revenue", 1_538_900),
                total("gp", "Gross profit", "rev", "-cor"),
                line("ga", "General and administrative", 402_300),
                line("dep", "Depreciation", 88_400),
                total("opex", "Total operating expenses", "ga", "dep"),
                total("oi", "Income from operations", "gp", "-opex"),
                line("interest", "Interest expense", 31_200),
                total("pbt", "Income before income taxes", "oi", "-interest"),
                line("tax", "Income taxes", 38_500),
                total("ni", "Net income", "pbt", "-tax", currency=True),
            ),
        ),
        Page(
            "Statement of Financial Position",
            "June 30, 2026",
            (
                heading("Current assets:"),
                line("cash", "Cash", 57_300, indent=True, currency=True),
                line("ar", "Accounts receivable", 238_100, indent=True),
                line("inv", "Inventory", 96_400, indent=True),
                total("ca", "Total current assets", "cash", "ar", "inv"),
                line("ppe", "Property and equipment, net", 1_126_500),
                total("ta", "Total assets", "ca", "ppe", currency=True),
                heading("Current liabilities:"),
                line("ap", "Accounts payable", 141_700, indent=True, currency=True),
                line("cpltd", "Current portion of long-term debt", 72_000, indent=True),
                total("cl", "Total current liabilities", "ap", "cpltd"),
                line("ltd", "Long-term debt", 624_000),
                total("tl", "Total liabilities", "cl", "ltd"),
                heading("Stockholders' equity:"),
                line("common", "Common stock", 100_000, indent=True),
                line("re", "Retained earnings", 580_600, indent=True),
                total("te", "Total stockholders' equity", "common", "re"),
                total(
                    "tle", "Total liabilities and stockholders' equity", "tl", "te", currency=True
                ),
            ),
        ),
    ),
)

BORROWERS = (HARBOUR_LANE, MESA_VERDE, NORTHFIELD, KESTREL_VALLEY)


def amounts(borrower: Borrower, page: Page) -> list[tuple[Row, list[int | None]]]:
    """Every row with its printed amounts, current year first. Totals are summed here, so the
    statements tie except where a row says `printed`."""
    columns = len(borrower.headers)
    values: list[dict[str, int]] = [{} for _ in range(columns)]
    out: list[tuple[Row, list[int | None]]] = []
    for row in page.rows:
        printed: list[int | None] = []
        for column in range(columns):
            if row.kind == "heading":
                printed.append(None)
                continue
            if row.kind == "line":
                assert row.value is not None, row
                amount = row.value
                if column == 1:
                    amount = row.prior if row.prior is not None else _prior(borrower, amount)
            else:
                amount = sum(
                    -values[column][part[1:]] if part.startswith("-") else values[column][part]
                    for part in row.parts
                )
                if column == 0 and row.printed is not None:
                    amount = row.printed
            values[column][row.key] = amount
            printed.append(amount)
        out.append((row, printed))
    return out


def _prior(borrower: Borrower, amount: int) -> int:
    assert borrower.prior_factor is not None
    a, b = borrower.prior_factor
    assert amount * a % b == 0, amount
    return amount * a // b


def money(amount: int, negative: str) -> str:
    if amount == 0:
        return "-"
    text = f"{abs(amount):,}"
    if amount > 0:
        return text
    return f"({text})" if negative == "parens" else f"-{text}"


# --- Layout, shared by the PDF writer and the scan --------------------------------------------


@dataclass
class Layout:
    """Text and rules on one page, in PDF points from the top left."""

    texts: list[tuple[float, float, float, bool, str, str]] = field(default_factory=list)
    rules: list[tuple[float, float, float]] = field(default_factory=list)  # x0, x1, y

    def text(self, x: float, y: float, size: float, s: str, bold: bool = False) -> None:
        self.texts.append((x, y, size, bold, s, "left"))

    def right(self, x: float, y: float, size: float, s: str, bold: bool = False) -> None:
        self.texts.append((x, y, size, bold, s, "right"))


def lay_out(borrower: Borrower, page: Page) -> Layout:
    out = Layout()
    out.text(LEFT, 70, 14, borrower.name, bold=True)
    out.text(LEFT, 92, 12, page.title, bold=True)
    out.text(LEFT, 110, 10, page.period)
    y = 150.0
    for column, header in enumerate(borrower.headers):
        out.right(COL_RIGHT[column], y, 10, header, bold=True)
    y += 22
    for row, printed in amounts(borrower, page):
        bold = row.kind != "line"
        x = INDENT if row.indent else LEFT
        if row.label:
            out.text(x, y, 10, row.label, bold=bold)
        if row.kind == "total":
            for column in range(len(printed)):
                out.rules.append((COL_RIGHT[column] - 70, COL_RIGHT[column], y - 11))
        for column, amount in enumerate(printed):
            if amount is None:
                continue
            out.right(COL_RIGHT[column], y, 10, money(amount, borrower.negative), bold=bold)
            if row.currency and borrower.sign:
                out.text(CURRENCY_X[column], y, 10, borrower.sign, bold=bold)
        y += 18 if row.kind != "heading" else 17
    out.text(LEFT, 800, 8, FOOTER)
    return out


# --- A text-layer PDF, written by hand ------------------------------------------------------


def _width(s: str, size: float) -> float:
    return sum(WIDTHS.get(char, 556) for char in s) * size / 1000


def _pdf_string(s: str) -> str:
    raw = s.encode("cp1252").decode("latin-1")
    escaped = raw.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return "".join(c if 32 <= ord(c) < 127 else f"\\{ord(c):03o}" for c in escaped)


def _content(layout: Layout) -> bytes:
    ops: list[str] = []
    for x0, x1, y in layout.rules:
        ops.append(f"0.5 w {x0:.1f} {PAGE_H - y:.1f} m {x1:.1f} {PAGE_H - y:.1f} l S")
    for x, y, size, bold, s, align in layout.texts:
        if align == "right":
            x -= _width(s, size)
        font = "F2" if bold else "F1"
        ops.append(f"BT /{font} {size:g} Tf {x:.1f} {PAGE_H - y:.1f} Td ({_pdf_string(s)}) Tj ET")
    return ("\n".join(ops) + "\n").encode("latin-1")


def write_text_pdf(path: Path, layouts: list[Layout]) -> None:
    """A minimal PDF 1.4 with Helvetica text: a catalog, the pages, two fonts, no info dict."""
    objects: list[bytes] = []
    n_pages = len(layouts)
    page_ids = [5 + 2 * i for i in range(n_pages)]
    kids = " ".join(f"{i} 0 R" for i in page_ids)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    for base in ("Helvetica", "Helvetica-Bold"):
        objects.append(
            f"<< /Type /Font /Subtype /Type1 /BaseFont /{base} "
            "/Encoding /WinAnsiEncoding >>".encode()
        )
    for page_id, layout in zip(page_ids, layouts, strict=True):
        stream = _content(layout)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
            f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
            f"/Contents {page_id + 1} 0 R >>".encode()
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"endstream")
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
    out += f"startxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))


# --- A scanned PDF, drawn with Pillow ---------------------------------------------------------


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)  # type: ignore[return-value]


def draw_scan(layout: Layout, seed: int) -> Image.Image:
    """The page as a slightly grey, slightly skewed 150 dpi scan with a few specks."""
    k = SCAN_DPI / 72
    image = Image.new("L", (round(PAGE_W * k), round(PAGE_H * k)), 246)
    draw = ImageDraw.Draw(image)
    for x0, x1, y in layout.rules:
        draw.line([(x0 * k, y * k), (x1 * k, y * k)], fill=40, width=1)
    for x, y, size, _bold, s, align in layout.texts:
        font = _font(round(size * k))
        if align == "right":
            x -= font.getlength(s) / k
        top = (y - size * 0.8) * k
        draw.text((x * k, top), s, font=font, fill=25)
    specks = random.Random(seed)
    for _ in range(400):
        px, py = specks.randrange(image.width), specks.randrange(image.height)
        draw.point((px, py), fill=specks.randrange(120, 200))
    return image.rotate(0.4, resample=Image.Resampling.BICUBIC, fillcolor=246)


def write_scan_pdf(path: Path, pages: list[Image.Image]) -> None:
    """An image-only PDF with no document info (no title, author or dates)."""
    pages[0].save(
        path,
        format="PDF",
        save_all=True,
        append_images=pages[1:],
        resolution=SCAN_DPI,
        title="",
        creationDate="",
        modDate="",
    )


# --- The loan system's seeds ------------------------------------------------------------------


def write_seeds() -> None:
    with (DATA_DIR / "loan_borrowers.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["borrower_id", "name", "fiscal_year_end", "currency", "period_due"])
        for b in BORROWERS:
            writer.writerow([b.borrower_id, b.name, b.fiscal_year_end, b.currency, b.period_due])
    with (DATA_DIR / "loan_mappings.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            ["borrower_id", "statement", "heading", "label", "item_id", "confirmed_period"]
        )
        for b in BORROWERS:
            last_year = f"{int(b.period_due[:4]) - 1}{b.period_due[4:]}"
            for statement, head, label, item in b.mappings:
                writer.writerow([b.borrower_id, statement, head, label, item, last_year])


def main() -> None:
    STATEMENTS_DIR.mkdir(parents=True, exist_ok=True)
    write_seeds()
    for number, borrower in enumerate(BORROWERS):
        layouts = [lay_out(borrower, page) for page in borrower.pages]
        path = STATEMENTS_DIR / f"{borrower.borrower_id}.pdf"
        if borrower.scanned:
            scans = [draw_scan(layout, seed=100 * number + i) for i, layout in enumerate(layouts)]
            write_scan_pdf(path, scans)
        else:
            write_text_pdf(path, layouts)
        print(f"{path.relative_to(ROOT)}: {borrower.name} ({borrower.planted})")


if __name__ == "__main__":
    main()
