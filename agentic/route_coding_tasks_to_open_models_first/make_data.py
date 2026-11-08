"""Write the made-up library and the coding tasks the recipe works through.

    uv run make_data.py

Ledgerline is a small invoicing library written for this recipe. It ships as one JSON file,
data/library.json, and not as a folder of Python files, because this repo's own lint hooks fix
anything they find in a .py file, including the lint problems task T01 is about.

data/tasks.json holds twelve tasks written as issues. Each has the tests that decide when it's
done, and some have extra checks the team keeps back for CI. The agent never sees those.
"""

from __future__ import annotations

import json
from pathlib import Path

DATA = Path(__file__).parent / "data"

# --- The library, as it is before any task ---------------------------------------------------

MONEY = '''"""Money amounts, kept as Decimal and rounded half up to the cent."""

import os
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def to_money(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def format_amount(amount):
    return f"{to_money(amount):,.2f}"


def percent_of(amount, percent):
    return to_money(Decimal(str(amount)) * Decimal(str(percent)) / 100)
'''

DATES = '''"""Due dates and overdue days."""

from datetime import timedelta


def due_date(issued, terms_days=30):
    return issued + timedelta(days=terms_days)


def days_overdue(due, today):
    return (today - due).days


def is_overdue(due, today):
    return today > due
'''

HOLIDAYS = '''"""Public holidays, when no invoice falls due."""

from datetime import date

HOLIDAYS = {
    date(2026, 1, 1),
    date(2026, 4, 6),
    date(2026, 5, 25),
    date(2026, 12, 25),
    date(2026, 12, 28),
}
'''

INVOICE = '''"""Invoices and their lines."""

from .money import to_money
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

ZERO = Decimal("0")


@dataclass
class Line:
    description: str
    quantity: int
    unit_price: Decimal
    discount_percent: Decimal = ZERO

    def total(self) -> Decimal:
        if self.discount_percent:
            return to_money(self.unit_price * (100 - self.discount_percent) / 100)
        return to_money(self.unit_price * self.quantity)


@dataclass
class Invoice:
    number: str
    issued: date
    terms_days: int = 30
    tax_rate: Decimal = Decimal("0.20")
    lines: list[Line] = field(default_factory=list)

    def add_line(self, line: Line) -> None:
        if line.quantity <= 0:
            raise ValueError("quantity must be positive")
        self.lines.append(line)

    def subtotal(self) -> Decimal:
        return sum((line.total() for line in self.lines), ZERO)

    def tax(self) -> Decimal:
        return sum(
            (to_money(float(line.total()) * float(self.tax_rate)) for line in self.lines), ZERO
        )

    def total(self) -> Decimal:
        return self.subtotal() + self.tax()

    @property
    def due(self) -> date:
        return self.issued + timedelta(days=self.terms_days)
'''

FORMATS = '''"""Invoice exports."""

import json
from .invoice import Invoice


def to_csv( invoice:Invoice )->str:
    rows=["description,quantity,unit_price,total"]
    for line in invoice.lines:
        rows.append( f"{line.description},{line.quantity},{line.unit_price},{line.total()}" )
    return "\\n".join(rows)+"\\n"
'''

BASICS = """import csv
import io
from datetime import date
from decimal import Decimal

import pytest

from ledgerline import dates, formats, money
from ledgerline.invoice import Invoice, Line


def test_money():
    assert money.to_money(1.005) == Decimal("1.01")
    assert money.format_amount(1234.5) == "1,234.50"
    assert money.percent_of(200, 15) == Decimal("30.00")


def test_dates():
    assert dates.due_date(date(2026, 3, 2), 30) == date(2026, 4, 1)
    assert dates.days_overdue(date(2026, 3, 1), date(2026, 3, 11)) == 10
    assert dates.is_overdue(date(2026, 3, 1), date(2026, 3, 2))
    assert not dates.is_overdue(date(2026, 3, 1), date(2026, 3, 1))


def test_lines():
    assert Line("Widget", 2, Decimal("4.50")).total() == Decimal("9.00")
    assert Line("Service", 1, Decimal("100"), Decimal("10")).total() == Decimal("90.00")


def test_invoice():
    invoice = Invoice("INV-1", date(2026, 3, 2))
    invoice.add_line(Line("Service", 1, Decimal("100.00")))
    assert invoice.subtotal() == Decimal("100.00")
    assert invoice.tax() == Decimal("20.00")
    assert invoice.total() == Decimal("120.00")
    assert invoice.due == date(2026, 4, 1)
    with pytest.raises(ValueError):
        invoice.add_line(Line("Nothing", 0, Decimal("1")))


def test_csv():
    invoice = Invoice("INV-2", date(2026, 3, 2))
    invoice.add_line(Line("Widget", 2, Decimal("4.50")))
    rows = list(csv.reader(io.StringIO(formats.to_csv(invoice))))
    assert rows[0][:4] == ["description", "quantity", "unit_price", "total"]
    assert rows[1][:4] == ["Widget", "2", "4.50", "9.00"]
"""

RUFF_CHECK = "check --isolated --select E,F,I --line-length 100 ledgerline"
RUFF_FORMAT = "format --check --isolated --line-length 100 ledgerline"

STYLE_TEST = """import subprocess
import sys


def test_{name}():
    result = subprocess.run(
        [sys.executable, "-m", "ruff", *"{args}".split()], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
"""

LIBRARY = {
    "ledgerline/__init__.py": '"""Ledgerline: a small invoicing library."""\n',
    "ledgerline/money.py": MONEY,
    "ledgerline/dates.py": DATES,
    "ledgerline/holidays.py": HOLIDAYS,
    "ledgerline/invoice.py": INVOICE,
    "ledgerline/formats.py": FORMATS,
    "tests/test_basics.py": BASICS,
    "README.md": "# Ledgerline\n\nA small invoicing library. Run the tests with `pytest tests`.\n",
}

# --- The tasks, written as issues ------------------------------------------------------------

HEADER = "from datetime import date\nfrom decimal import Decimal\n\nimport pytest\n\n"

TASKS = [
    {
        "task_id": "T01",
        "title": "Lint: unused and unsorted imports",
        "body": (
            "Ruff reports unused imports in money.py and formats.py, and unsorted imports in "
            "invoice.py and formats.py. Clean them up. Nothing else should change."
        ),
        "tests": {"tests/test_t01.py": STYLE_TEST.format(name="lint", args=RUFF_CHECK)},
    },
    {
        "task_id": "T02",
        "title": "Format formats.py",
        "body": (
            "formats.py isn't formatted like the rest of the package. Run the formatter over "
            "ledgerline so the format check passes. No change to behaviour."
        ),
        "tests": {"tests/test_t02.py": STYLE_TEST.format(name="format", args=RUFF_FORMAT)},
    },
    {
        "task_id": "T03",
        "title": "Docstrings for money.py",
        "body": (
            "The public functions in money.py (to_money, format_amount, percent_of) have no "
            "docstrings. Add a short docstring to each saying what it returns."
        ),
        "tests": {
            "tests/test_t03.py": (
                "from ledgerline import money\n\n\n"
                "def test_docstrings():\n"
                "    for function in (money.to_money, money.format_amount, money.percent_of):\n"
                "        assert function.__doc__ and len(function.__doc__.strip()) >= 10\n"
            )
        },
    },
    {
        "task_id": "T04",
        "title": "Type hints for dates.py",
        "body": (
            "Add type hints to every function in dates.py: dates are datetime.date, terms_days "
            "is an int, days_overdue returns an int and is_overdue returns a bool."
        ),
        "tests": {
            "tests/test_t04.py": (
                "import typing\nfrom datetime import date\n\nfrom ledgerline import dates\n\n\n"
                "def test_hints():\n"
                "    assert typing.get_type_hints(dates.due_date) == {\n"
                '        "issued": date, "terms_days": int, "return": date\n    }\n'
                "    assert typing.get_type_hints(dates.days_overdue) == {\n"
                '        "due": date, "today": date, "return": int\n    }\n'
                "    assert typing.get_type_hints(dates.is_overdue) == {\n"
                '        "due": date, "today": date, "return": bool\n    }\n'
            )
        },
    },
    {
        "task_id": "T05",
        "title": "days_overdue goes negative",
        "body": (
            "dates.days_overdue returns a negative number of days before the due date. It should "
            "return 0 until the invoice is overdue."
        ),
        "tests": {
            "tests/test_t05.py": (
                HEADER + "from ledgerline import dates\n\n\n"
                "def test_not_negative():\n"
                "    assert dates.days_overdue(date(2026, 3, 10), date(2026, 3, 1)) == 0\n"
                "    assert dates.days_overdue(date(2026, 3, 10), date(2026, 3, 10)) == 0\n"
                "    assert dates.days_overdue(date(2026, 3, 10), date(2026, 3, 13)) == 3\n"
            )
        },
    },
    {
        "task_id": "T06",
        "title": "Discounted lines ignore the quantity",
        "body": (
            "A line with a discount is charged as if its quantity were 1. Three items at 10.00 "
            "with 10% off should come to 27.00, not 9.00."
        ),
        "tests": {
            "tests/test_t06.py": (
                HEADER + "from ledgerline.invoice import Line\n\n\n"
                "def test_discount_with_quantity():\n"
                '    line = Line("Paint", 3, Decimal("10.00"), Decimal("10"))\n'
                '    assert line.total() == Decimal("27.00")\n'
            )
        },
        "ci_tests": {
            "ci/test_t06.py": (
                HEADER + "from ledgerline.invoice import Line\n\n\n"
                "def test_odd_discount():\n"
                '    line = Line("Brush", 7, Decimal("3.99"), Decimal("12.5"))\n'
                '    assert line.total() == Decimal("24.44")\n'
            )
        },
    },
    {
        "task_id": "T07",
        "title": "Discount on the whole invoice",
        "body": (
            "Add Invoice.apply_discount(percent) for a discount on the whole invoice. It comes off "
            "the subtotal, and tax is worked out on what's left. Refuse a percent below 0 or above "
            "100 with ValueError."
        ),
        "tests": {
            "tests/test_t07.py": (
                HEADER + "from ledgerline.invoice import Invoice, Line\n\n\n"
                "def test_invoice_discount():\n"
                '    invoice = Invoice("INV-7", date(2026, 3, 2))\n'
                '    invoice.add_line(Line("Service", 1, Decimal("100.00")))\n'
                '    invoice.apply_discount(Decimal("10"))\n'
                '    assert invoice.subtotal() == Decimal("90.00")\n'
                '    assert invoice.tax() == Decimal("18.00")\n'
                '    assert invoice.total() == Decimal("108.00")\n'
                "    for bad in (-1, 101):\n"
                "        with pytest.raises(ValueError):\n"
                "            invoice.apply_discount(Decimal(bad))\n"
            )
        },
    },
    {
        "task_id": "T08",
        "title": "CSV export breaks on commas",
        "body": (
            "to_csv writes descriptions as they are, so a description with a comma or a quote "
            "breaks the row. Quote fields properly."
        ),
        "tests": {
            "tests/test_t08.py": (
                "import csv\nimport io\n" + HEADER + "from ledgerline import formats\n"
                "from ledgerline.invoice import Invoice, Line\n\n\n"
                "def test_commas_and_quotes():\n"
                '    invoice = Invoice("INV-8", date(2026, 3, 2))\n'
                '    invoice.add_line(Line(\'Paint, white "matt"\', 2, Decimal("4.50")))\n'
                "    rows = list(csv.reader(io.StringIO(formats.to_csv(invoice))))\n"
                '    assert rows[1][:4] == [\'Paint, white "matt"\', "2", "4.50", "9.00"]\n'
            )
        },
    },
    {
        "task_id": "T09",
        "title": "Tax is a cent off",
        "body": (
            "Our accountant says tax is sometimes a cent off. Tax must be worked out once, on the "
            "invoice subtotal, and rounded half up to the cent: not per line, and not in floats."
        ),
        "tests": {
            "tests/test_t09.py": (
                HEADER + "from ledgerline.invoice import Invoice, Line\n\n\n"
                "def test_tax_on_subtotal():\n"
                '    invoice = Invoice("INV-9", date(2026, 3, 2), tax_rate=Decimal("0.07"))\n'
                "    for _ in range(3):\n"
                '        invoice.add_line(Line("Stamp", 1, Decimal("0.50")))\n'
                '    assert invoice.tax() == Decimal("0.11")\n'
                '    assert invoice.total() == Decimal("1.61")\n'
            )
        },
        "ci_tests": {
            "ci/test_t09.py": (
                HEADER + "from ledgerline.invoice import Invoice, Line\n\n\n"
                "def test_half_up():\n"
                '    invoice = Invoice("INV-9b", date(2026, 3, 2), tax_rate=Decimal("0.10"))\n'
                '    invoice.add_line(Line("Pen", 1, Decimal("1.15")))\n'
                '    assert invoice.tax() == Decimal("0.12")\n'
            )
        },
    },
    {
        "task_id": "T10",
        "title": "Late fee",
        "body": (
            "Add late_fee(amount, months_overdue) to money.py. It charges 1.5% a month on the "
            "amount, compounded monthly, rounded half up to the cent. No fee for 0 months."
        ),
        "tests": {
            "tests/test_t10.py": (
                HEADER + "from ledgerline import money\n\n\n"
                "def test_late_fee():\n"
                '    assert money.late_fee(Decimal("100.00"), 1) == Decimal("1.50")\n'
                '    assert money.late_fee(Decimal("100.00"), 0) == Decimal("0")\n'
            )
        },
        "ci_tests": {
            "ci/test_t10.py": (
                HEADER + "from ledgerline import money\n\n\n"
                "def test_compounded():\n"
                '    assert money.late_fee(Decimal("1000"), 3) == Decimal("45.68")\n'
                '    assert money.late_fee(Decimal("250.00"), 12) == Decimal("48.90")\n'
            )
        },
    },
    {
        "task_id": "T11",
        "title": "Support invoices in other currencies",
        "body": (
            "We're starting to bill customers abroad, so invoices need a currency. Give Invoice "
            "and Line a currency code (default EUR). An invoice must refuse a line in another "
            "currency with ValueError. format_amount should take an optional currency and show it "
            'before the amount, like "USD 1,234.50", and the CSV export needs a currency column at '
            "the end."
        ),
        "tests": {
            "tests/test_t11.py": (
                "import csv\nimport io\n" + HEADER + "from ledgerline import formats, money\n"
                "from ledgerline.invoice import Invoice, Line\n\n\n"
                "def test_currency():\n"
                '    assert money.format_amount(1234.5, "USD") == "USD 1,234.50"\n'
                '    assert money.format_amount(1234.5) == "1,234.50"\n'
                '    invoice = Invoice("INV-11", date(2026, 3, 2), currency="USD")\n'
                '    invoice.add_line(Line("Widget", 2, Decimal("4.50"), currency="USD"))\n'
                "    with pytest.raises(ValueError):\n"
                '        invoice.add_line(Line("Widget", 1, Decimal("4.50"), currency="GBP"))\n'
                "    rows = list(csv.reader(io.StringIO(formats.to_csv(invoice))))\n"
                '    assert rows[0][-1] == "currency"\n'
                '    assert rows[1][-1] == "USD"\n'
                '    assert Invoice("INV-12", date(2026, 3, 2)).currency == "EUR"\n'
            )
        },
    },
    {
        "task_id": "T12",
        "title": "Small fix: due dates on weekends",
        "body": (
            "A due date that lands on a Saturday or Sunday should move to the next working day. "
            "Skip the public holidays in holidays.py too."
        ),
        "tests": {
            "tests/test_t12.py": (
                HEADER
                + "from ledgerline import dates\nfrom ledgerline.invoice import Invoice\n\n\n"
                "def test_weekend_and_holidays():\n"
                "    assert dates.due_date(date(2026, 3, 4), 10) == date(2026, 3, 16)\n"
                "    assert dates.due_date(date(2026, 12, 16), 10) == date(2026, 12, 29)\n"
                "    assert dates.due_date(date(2025, 12, 2), 30) == date(2026, 1, 2)\n"
                "    assert dates.due_date(date(2026, 3, 2), 30) == date(2026, 4, 1)\n"
                '    invoice = Invoice("INV-12", date(2026, 12, 16), terms_days=10)\n'
                "    assert invoice.due == date(2026, 12, 29)\n"
            )
        },
        "ci_tests": {
            "ci/test_t12.py": (
                HEADER + "from ledgerline import dates\n\n\n"
                "def test_easter():\n"
                "    assert dates.due_date(date(2026, 3, 25), 10) == date(2026, 4, 7)\n"
            )
        },
    },
    {
        "task_id": "T13",
        "title": "Holidays for any year",
        "body": (
            "holidays.py only lists 2026, so due dates in other years skip no holidays. Add "
            "holidays_in(year) that returns the set of holidays for any year: New Year's Day, "
            "Easter Monday (the day after Western Easter Sunday), the last Monday in May, "
            "Christmas Day and "
            "Boxing Day. When New Year's Day, Christmas Day or Boxing Day falls on a weekend, the "
            "holiday moves to the next weekday that isn't already a holiday. Keep HOLIDAYS as the "
            "2026 set."
        ),
        "tests": {
            "tests/test_t13.py": (
                HEADER + "from ledgerline import holidays\n\n\n"
                "def test_holidays_in():\n"
                "    assert holidays.holidays_in(2026) == holidays.HOLIDAYS\n"
                "    assert holidays.holidays_in(2027) == {\n"
                "        date(2027, 1, 1), date(2027, 3, 29), date(2027, 5, 31),\n"
                "        date(2027, 12, 27), date(2027, 12, 28),\n"
                "    }\n"
                "    assert holidays.holidays_in(2028) == {\n"
                "        date(2028, 1, 3), date(2028, 4, 17), date(2028, 5, 29),\n"
                "        date(2028, 12, 25), date(2028, 12, 26),\n"
                "    }\n"
            )
        },
        "ci_tests": {
            "ci/test_t13.py": (
                HEADER + "from ledgerline import holidays\n\n\n"
                "def test_other_years():\n"
                "    assert holidays.holidays_in(2035) == {\n"
                "        date(2035, 1, 1), date(2035, 3, 26), date(2035, 5, 28),\n"
                "        date(2035, 12, 25), date(2035, 12, 26),\n"
                "    }\n"
                "    assert holidays.holidays_in(2038) == {\n"
                "        date(2038, 1, 1), date(2038, 4, 26), date(2038, 5, 31),\n"
                "        date(2038, 12, 27), date(2038, 12, 28),\n"
                "    }\n"
                "    assert holidays.holidays_in(2043) == {\n"
                "        date(2043, 1, 1), date(2043, 3, 30), date(2043, 5, 25),\n"
                "        date(2043, 12, 25), date(2043, 12, 28),\n"
                "    }\n"
            )
        },
    },
    {
        "task_id": "T14",
        "title": "Read amounts the way customers type them",
        "body": (
            "Add parse_amount(text) to money.py. It returns the amount as money (to_money). Accept "
            "an optional currency code or symbol before the amount (USD, EUR, GBP, $, €, £), "
            "spaces around it, a leading minus or brackets for a negative amount, and thousands "
            "separators. The last comma or dot is the decimal mark when one or two digits follow "
            "it; otherwise every comma, dot or space in the number is a thousands separator. "
            "Thousands groups must be exactly three digits, and the decimal mark can't also be "
            "used as a thousands separator. Anything else raises ValueError."
        ),
        "tests": {
            "tests/test_t14.py": (
                HEADER + "from ledgerline import money\n\n\n"
                "def test_parse_amount():\n"
                '    assert money.parse_amount("1,234.50") == Decimal("1234.50")\n'
                '    assert money.parse_amount("1.234,50") == Decimal("1234.50")\n'
                '    assert money.parse_amount("USD 1,234.5") == Decimal("1234.50")\n'
                '    assert money.parse_amount("(12.00)") == Decimal("-12.00")\n'
                '    assert money.parse_amount("1,234") == Decimal("1234.00")\n'
                "    with pytest.raises(ValueError):\n"
                '        money.parse_amount("twelve")\n'
            )
        },
        "ci_tests": {
            "ci/test_t14.py": (
                HEADER + "from ledgerline import money\n\n\n"
                "def test_more_amounts():\n"
                '    assert money.parse_amount("€ 0,5") == Decimal("0.50")\n'
                '    assert money.parse_amount(" (1 234,56) ") == Decimal("-1234.56")\n'
                '    assert money.parse_amount("-£1.234.567") == Decimal("-1234567.00")\n'
                '    assert money.parse_amount("1.234") == Decimal("1234.00")\n'
                '    for bad in ("12.345.67", "1,23,456", "", "1,234.567,89", "USD"):\n'
                "        with pytest.raises(ValueError):\n"
                "            money.parse_amount(bad)\n"
            )
        },
    },
    {
        "task_id": "T15",
        "title": "Pay an invoice in installments",
        "body": (
            "Add Invoice.installments(count, every_days=30). It returns a list of (due date, "
            "amount) pairs that add up exactly to the invoice total. Split the total evenly, "
            "rounding each part down to the cent, and add any cents left over one at a time to the "
            "earliest installments. The first falls due on the invoice's due date and each next "
            "one "
            "every_days later. A due date on a weekend or a holiday in holidays.py moves to the "
            "next working day, but the next installment still counts from the date before it "
            "moved. A count below 1 raises ValueError."
        ),
        "tests": {
            "tests/test_t15.py": (
                HEADER + "from ledgerline.invoice import Invoice, Line\n\n\n"
                "def test_installments():\n"
                '    invoice = Invoice("INV-15", date(2026, 11, 27))\n'
                '    invoice.add_line(Line("Service", 1, Decimal("100.01")))\n'
                '    assert invoice.total() == Decimal("120.01")\n'
                "    assert invoice.installments(3) == [\n"
                '        (date(2026, 12, 29), Decimal("40.01")),\n'
                '        (date(2027, 1, 26), Decimal("40.00")),\n'
                '        (date(2027, 2, 25), Decimal("40.00")),\n'
                "    ]\n"
                "    with pytest.raises(ValueError):\n"
                "        invoice.installments(0)\n"
            )
        },
        "ci_tests": {
            "ci/test_t15.py": (
                HEADER + "from ledgerline.invoice import Invoice, Line\n\n\n"
                "def test_weekly():\n"
                '    invoice = Invoice("INV-15b", date(2026, 3, 2), terms_days=5)\n'
                '    invoice.add_line(Line("Service", 1, Decimal("83.33")))\n'
                "    parts = invoice.installments(7, every_days=7)\n"
                '    assert [amount for _, amount in parts] == [Decimal("14.29")] * 4 + [\n'
                '        Decimal("14.28")\n'
                "    ] * 3\n"
                "    assert [due for due, _ in parts] == [\n"
                "        date(2026, 3, 9), date(2026, 3, 16), date(2026, 3, 23),\n"
                "        date(2026, 3, 30),\n"
                "        date(2026, 4, 7), date(2026, 4, 13), date(2026, 4, 20),\n"
                "    ]\n"
            )
        },
    },
]


def main() -> None:
    DATA.mkdir(exist_ok=True)
    (DATA / "library.json").write_text(json.dumps(LIBRARY, indent=1) + "\n", encoding="utf-8")
    (DATA / "tasks.json").write_text(json.dumps(TASKS, indent=1) + "\n", encoding="utf-8")
    print(f"Wrote {len(LIBRARY)} library files and {len(TASKS)} tasks to {DATA}")


if __name__ == "__main__":
    main()
