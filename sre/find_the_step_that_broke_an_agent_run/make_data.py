"""Write three synthetic failed agent runs, and the step that broke each one, in data/.

Each run is a coding team of agents working on a small made-up invoicing library, ledgerline:
a coordinator plans and hands work to a coder, a reviewer checks it, and the terminal records
what every command printed. Each run ends in a failed acceptance check, and each has one planted
step where the decisive error came in:

- short-handoff: the coordinator's instruction to the coder already has the error, and the coder
  follows it faithfully. About 5,500 tokens.
- medium-off-by-one: the instruction is right; the coder writes a different rule. About 36,000
  tokens.
- long-swallowed-error: early in a long session the coder hides a date-parsing error; it only
  shows hundreds of steps later. About 420,000 tokens, far more than one model call can read.

Everything is invented: the library, the people and the logs. data/runs.json holds the runs and
data/expected.json the planted answers, which the recipe uses only to give each run a verdict.

    uv run make_data.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent / "data"
SEED = 20261111

Event = dict[str, Any]

INVOICE_PY = '''"""Invoices, payments and balances."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class Payment:
    paid_on: date
    amount: Decimal


@dataclass
class Invoice:
    number: str
    customer: str
    issued_on: date
    due_on: date
    total: Decimal
    currency: str = "EUR"
    payments: list[Payment] = field(default_factory=list)

    @property
    def paid(self) -> Decimal:
        return sum((p.amount for p in self.payments), Decimal("0"))

    @property
    def unpaid(self) -> Decimal:
        return max(self.total - self.paid, Decimal("0"))

    def is_overdue(self, today: date) -> bool:
        return today > self.due_on and self.unpaid > 0
'''

TASK = (
    "Overdue invoices should accrue late-payment interest. Add `late_interest(invoice, today)` to "
    "ledgerline/interest.py. It returns 1% of the invoice's UNPAID balance for every started month "
    "past the due date (a day late counts as one month), capped at 10% of the unpaid balance, "
    "rounded half-up to cents. Invoices that are not overdue owe nothing. Imported invoices keep "
    "their due date as text in `due_text`, usually DD/MM/YYYY; handle those too. A due date that "
    "can't be read must raise an error: never treat it as not overdue. When you are done, "
    "the acceptance check `python -m ledgerline.acceptance` must pass."
)


def ev(events: list[Event], agent: str, content: str) -> None:
    """Append one event; the step number is its position in the run."""
    events.append({"step": len(events), "agent": agent, "content": content})


# ---------------------------------------------------------------------------------------------
# Filler that makes a run long the way real runs are long: test logs, listings, diffs, files.


MODULES = [
    "invoice",
    "payments",
    "currency",
    "reminders",
    "export",
    "taxes",
    "customers",
    "ledger",
    "reports",
    "audit",
    "schedules",
    "importer",
    "statements",
    "credit_notes",
]
VERBS = [
    "compute",
    "load",
    "render",
    "check",
    "merge",
    "split",
    "format",
    "parse",
    "apply",
    "collect",
    "validate",
    "summarize",
    "round",
    "convert",
    "group",
    "filter",
]
NOUNS = [
    "totals",
    "lines",
    "rates",
    "notes",
    "batches",
    "entries",
    "dates",
    "balances",
    "headers",
    "periods",
    "codes",
    "amounts",
    "accounts",
    "reminders",
    "ranges",
]


def fake_module(rng: random.Random, name: str, lines: int) -> str:
    out = [
        f'"""{name.replace("_", " ").capitalize()} for ledgerline."""',
        "",
        "from __future__ import annotations",
        "",
        "from decimal import Decimal",
        "",
    ]
    while len(out) < lines:
        fn = f"{rng.choice(VERBS)}_{rng.choice(NOUNS)}"
        arg = rng.choice(NOUNS)
        out += [
            "",
            f"def {fn}({arg}: list[Decimal], scale: int = 2) -> Decimal:",
            f'    """{fn.replace("_", " ").capitalize()} over the given {arg}."""',
            '    total = Decimal("0")',
            f"    for value in {arg}:",
            "        total += value.quantize(Decimal(1).scaleb(-scale))",
            f'    return total * Decimal("{rng.randint(1, 9)}") / Decimal("{rng.randint(1, 9)}")',
        ]
    return "\n".join(out[:lines])


def numbered(text: str, start: int = 1) -> str:
    return "\n".join(f"{i:>5}\t{line}" for i, line in enumerate(text.split("\n"), start))


def pytest_log(
    rng: random.Random, passed: int, failed: list[str] | None = None, seconds: float | None = None
) -> str:
    names = []
    for _ in range(passed):
        mod = rng.choice(MODULES)
        names.append(
            f"tests/test_{mod}.py::test_{rng.choice(VERBS)}_{rng.choice(NOUNS)}_"
            f"{rng.randint(1, 99)} PASSED"
        )
    lines = [
        "============================= test session starts ==============================",
        "platform linux -- Python 3.12.8, pytest-9.0.2, pluggy-1.6.0",
        "rootdir: /work/ledgerline",
        "configfile: pyproject.toml",
        f"collected {passed + len(failed or [])} items",
        "",
    ]
    lines += names
    for f in failed or []:
        lines.append(f"{f} FAILED")
    lines.append("")
    if failed:
        lines.append(
            "=================================== FAILURES ==================================="
        )
        for f in failed:
            lines += [
                f"___________________________ {f.split('::')[-1]} ___________________________",
                "    def test_case():",
                ">       assert result == expected",
                "E       AssertionError",
                "",
            ]
    secs = seconds if seconds is not None else rng.uniform(0.8, 9.0)
    summary = f"{passed} passed" + (f", {len(failed)} failed" if failed else "")
    lines.append(f"========================= {summary} in {secs:.2f}s =========================")
    return "\n".join(lines)


def listing(rng: random.Random) -> str:
    files = ["pyproject.toml", "README.md", "ledgerline/__init__.py", "ledgerline/acceptance.py"]
    files += [f"ledgerline/{m}.py" for m in MODULES] + [f"tests/test_{m}.py" for m in MODULES]
    return "\n".join(f"{rng.randint(1, 40):>4}K  {f}" for f in sorted(files))


def grep_output(rng: random.Random, word: str, hits: int) -> str:
    return "\n".join(
        f"ledgerline/{rng.choice(MODULES)}.py:{rng.randint(10, 400)}:    "
        f"{rng.choice(VERBS)}_{word}({rng.choice(NOUNS)}, scale={rng.randint(0, 4)})"
        for _ in range(hits)
    )


def diff_output(rng: random.Random, module: str, size: int) -> str:
    out = [
        f"diff --git a/ledgerline/{module}.py b/ledgerline/{module}.py",
        f"--- a/ledgerline/{module}.py",
        f"+++ b/ledgerline/{module}.py",
    ]
    for _ in range(size):
        out.append(f"@@ -{rng.randint(1, 300)},6 +{rng.randint(1, 300)},6 @@")
        out.append(f"-    return {rng.choice(VERBS)}_{rng.choice(NOUNS)}(values)")
        out.append(f"+    return {rng.choice(VERBS)}_{rng.choice(NOUNS)}(values, scale=2)")
    return "\n".join(out)


def busy_work(events: list[Event], rng: random.Random, rounds: int, about: str) -> None:
    """Rounds of ordinary, correct work on other parts of the library, with their command output."""
    for _ in range(rounds):
        module = rng.choice(MODULES)
        kind = rng.randrange(5)
        if kind == 0:
            ev(
                events,
                "coder",
                f"Reading ledgerline/{module}.py to see how {about} is handled there.\n"
                f"```bash\ncat -n ledgerline/{module}.py\n```",
            )
            ev(events, "terminal", numbered(fake_module(rng, module, rng.randint(120, 420))))
        elif kind == 1:
            ev(
                events,
                "coder",
                f"Running the {module} tests after the last change.\n"
                f"```bash\npython -m pytest tests/test_{module}.py -v\n```",
            )
            ev(events, "terminal", pytest_log(rng, rng.randint(40, 160)))
        elif kind == 2:
            word = rng.choice(NOUNS)
            ev(
                events,
                "coder",
                f"Looking for every place that touches {word}.\n"
                f'```bash\ngrep -rn "{word}" ledgerline/\n```',
            )
            ev(events, "terminal", grep_output(rng, word, rng.randint(30, 140)))
        elif kind == 3:
            ev(
                events,
                "coder",
                f"Tidying the rounding calls in {module} so they all pass scale explicitly.\n"
                f"```bash\ngit diff ledgerline/{module}.py\n```",
            )
            ev(events, "terminal", diff_output(rng, module, rng.randint(15, 60)))
        else:
            ev(
                events,
                "coordinator",
                f"Status check: {about} is on track; the {module} module is next. "
                "Keep the public functions' signatures unchanged.",
            )
            ev(events, "coder", "Understood. Continuing with the next module.")


# ---------------------------------------------------------------------------------------------
# The three runs.


def interest_code(base: str, months: str, parse_note: str = "") -> str:
    return f'''"""Late-payment interest."""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

RATE = Decimal("0.01")
CAP = Decimal("0.10")


def due_date(invoice) -> date:
    if getattr(invoice, "due_text", None):
        return datetime.strptime(invoice.due_text, "%d/%m/%Y").date()
    return invoice.due_on


def months_overdue(due: date, today: date) -> int:
{months}


def late_interest(invoice, today: date) -> Decimal:{parse_note}
    due = due_date(invoice)
    if today <= due or invoice.unpaid <= 0:
        return Decimal("0.00")
    base = {base}
    interest = base * RATE * months_overdue(due, today)
    return min(interest, base * CAP).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
'''


STARTED_MONTHS = """    months = (today.year - due.year) * 12 + (today.month - due.month)
    if today.day > due.day:
        months += 1
    return max(months, 1)"""

FULL_MONTHS = """    months = (today.year - due.year) * 12 + (today.month - due.month)
    if today.day < due.day:
        months -= 1
    return max(months, 0)"""


def opening(events: list[Event], rng: random.Random) -> None:
    ev(events, "user", TASK)
    ev(
        events,
        "coordinator",
        "Plan: read the Invoice model, agree the interest rule, hand the implementation to the "
        "coder, then have the reviewer check it against the task.\n"
        "```bash\nls -R ledgerline tests\n```",
    )
    ev(events, "terminal", listing(rng))
    ev(events, "coordinator", "```bash\ncat -n ledgerline/invoice.py\n```")
    ev(events, "terminal", numbered(INVOICE_PY))


def short_handoff(rng: random.Random) -> tuple[list[Event], dict[str, Any]]:
    events: list[Event] = []
    opening(events, rng)
    busy_work(events, rng, 3, "invoices")
    root = len(events)
    ev(
        events,
        "coordinator",
        "To coder: implement late_interest(invoice, today) in ledgerline/interest.py. Charge 1% "
        "of invoice.total for every started month past the due date, capped at 10% of "
        "invoice.total, rounded half-up to cents; nothing if the invoice isn't overdue. Parse "
        "due_text (DD/MM/YYYY) when present. Add tests for each case.",
    )
    ev(
        events,
        "coder",
        "Writing ledgerline/interest.py as instructed.\n```python\n"
        + interest_code("invoice.total", STARTED_MONTHS)
        + "```",
    )
    ev(events, "terminal", "File written: ledgerline/interest.py (34 lines)")
    ev(
        events,
        "coder",
        "Adding tests: not overdue, one day late, three months late, the cap, and due_text.\n"
        "```bash\npython -m pytest tests/test_interest.py -v\n```",
    )
    ev(events, "terminal", pytest_log(rng, 5, seconds=0.41))
    ev(
        events,
        "coordinator",
        "To reviewer: please check ledgerline/interest.py against my instruction to the coder.",
    )
    ev(
        events,
        "reviewer",
        "The code matches the instruction: 1% of the total per started month, capped at 10%, "
        "rounded half-up, due_text parsed. Tests cover each case. Approved.",
    )
    ev(events, "coordinator", "```bash\npython -m ledgerline.acceptance\n```")
    ev(
        events,
        "terminal",
        "acceptance: INV-2042 total 1000.00 paid 600.00 unpaid 400.00, 3 months late: "
        "expected 12.00 got 30.00 FAIL\nacceptance: INV-2051 total 250.00 unpaid 250.00, "
        "1 month late: expected 2.50 got 2.50 ok\n1 of 2 checks failed",
    )
    ev(
        events,
        "coordinator",
        "The partially paid invoice fails. The work is done as planned; the acceptance data "
        "may be wrong. Marking the task complete.",
    )
    return events, {
        "root_step": root,
        "responsible_agent": "coordinator",
        "what_went_wrong": "The handoff told the coder to charge interest on invoice.total; "
        "the task "
        "says the unpaid balance. The coder and reviewer followed the handoff.",
    }


def medium_off_by_one(rng: random.Random) -> tuple[list[Event], dict[str, Any]]:
    events: list[Event] = []
    opening(events, rng)
    ev(
        events,
        "coordinator",
        "To coder: implement late_interest(invoice, today) in ledgerline/interest.py exactly as "
        "the task says: 1% of invoice.unpaid per STARTED month past the due date (one day late "
        "is one month), capped at 10% of invoice.unpaid, rounded half-up; parse due_text "
        "(DD/MM/YYYY). Add tests.",
    )
    busy_work(events, rng, 12, "the due date")
    root = len(events)
    ev(
        events,
        "coder",
        "Implementing it now. I count months between the due date and today.\n```python\n"
        + interest_code("invoice.unpaid", FULL_MONTHS)
        + "```",
    )
    ev(events, "terminal", "File written: ledgerline/interest.py (34 lines)")
    ev(
        events,
        "coder",
        "Tests for three months late, the cap, not overdue and due_text.\n"
        "```bash\npython -m pytest tests/test_interest.py -v\n```",
    )
    ev(events, "terminal", pytest_log(rng, 4, seconds=0.38))
    busy_work(events, rng, 12, "interest")
    ev(
        events,
        "coder",
        "Small clean-up in interest.py: constants moved to the top, behaviour unchanged.",
    )
    ev(events, "terminal", "File written: ledgerline/interest.py (34 lines)")
    ev(events, "coordinator", "To reviewer: please check ledgerline/interest.py against the task.")
    ev(
        events,
        "reviewer",
        "Interest is on the unpaid balance, capped at 10%, rounded half-up, and due_text is "
        "parsed. The tests pass. Approved.",
    )
    ev(events, "coordinator", "```bash\npython -m ledgerline.acceptance\n```")
    ev(
        events,
        "terminal",
        "acceptance: INV-2042 unpaid 400.00, 3 months late: expected 12.00 got 12.00 ok\n"
        "acceptance: INV-2077 unpaid 800.00, 10 days late: expected 8.00 got 0.00 FAIL\n"
        "1 of 2 checks failed",
    )
    ev(
        events,
        "coordinator",
        "One check fails on a short delay. Marking the task complete and flagging the check.",
    )
    return events, {
        "root_step": root,
        "responsible_agent": "coder",
        "what_went_wrong": "The coder counted full months (10 days late gives 0) "
        "instead of started "
        "months, although the handoff said one day late is one month.",
    }


def long_swallowed_error(rng: random.Random) -> tuple[list[Event], dict[str, Any]]:
    events: list[Event] = []
    opening(events, rng)
    ev(
        events,
        "coordinator",
        "To coder: implement late_interest(invoice, today) in ledgerline/interest.py exactly as "
        "the task says, then work through the open tickets on rounding and reminders in every "
        "module. Keep each change small and run the tests after it.",
    )
    ev(
        events,
        "coder",
        "Implementing late_interest first.\n```python\n"
        + interest_code("invoice.unpaid", STARTED_MONTHS)
        + "```",
    )
    ev(events, "terminal", "File written: ledgerline/interest.py (34 lines)")
    ev(
        events,
        "coder",
        "```bash\npython -m pytest tests/test_interest.py tests/test_importer.py -v\n```",
    )
    ev(
        events,
        "terminal",
        pytest_log(
            rng,
            61,
            failed=["tests/test_importer.py::test_interest_on_imported_invoice"],
            seconds=2.71,
        )
        + "\nE       ValueError: time data '2026-03-31' does not match format '%d/%m/%Y'",
    )
    busy_work(events, rng, 6, "imports")
    root = len(events)
    ev(
        events,
        "coder",
        "The importer test is flaky because some fixtures carry ISO dates. Making due_date() "
        "tolerant so the suite is green, then moving on.\n```python\n"
        + interest_code(
            "invoice.unpaid",
            STARTED_MONTHS,
            "\n    try:\n        due_date(invoice)\n    except ValueError:\n"
            '        return Decimal("0.00")  # unparseable due_text: skip',
        )
        + "```",
    )
    ev(events, "terminal", "File written: ledgerline/interest.py (38 lines)")
    ev(
        events,
        "coder",
        "```bash\npython -m pytest tests/test_interest.py tests/test_importer.py -v\n```",
    )
    ev(events, "terminal", pytest_log(rng, 62, seconds=2.66))
    busy_work(events, rng, 230, "rounding and reminders")
    ev(events, "coordinator", "To reviewer: please review the session's changes, interest first.")
    ev(
        events,
        "reviewer",
        "late_interest follows the task: unpaid balance, started months, 10% cap, half-up "
        "rounding, due_text parsed. All 1,412 tests pass. Approved.",
    )
    ev(events, "coordinator", "```bash\npython -m ledgerline.acceptance\n```")
    ev(
        events,
        "terminal",
        "acceptance: INV-2042 unpaid 400.00, 3 months late: expected 12.00 got 12.00 ok\n"
        "acceptance: IMP-0315 (imported, due_text '31-03-2026') unpaid 900.00, 2 months late: "
        "expected 18.00 got 0.00 FAIL\n1 of 2 checks failed",
    )
    ev(
        events,
        "coordinator",
        "The imported invoice fails acceptance; everything else is green. "
        "Marking the task complete.",
    )
    return events, {
        "root_step": root,
        "responsible_agent": "coder",
        "what_went_wrong": "To make a test pass, the coder made late_interest return 0 whenever "
        "due_text doesn't parse, so imported invoices with another date format "
        "silently owe nothing.",
    }


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    rng = random.Random(SEED)
    runs: list[dict[str, Any]] = []
    expected: list[dict[str, Any]] = []
    for run_id, build in (
        ("short-handoff", short_handoff),
        ("medium-off-by-one", medium_off_by_one),
        ("long-swallowed-error", long_swallowed_error),
    ):
        events, answer = build(rng)
        runs.append({"run_id": run_id, "events": events})
        expected.append({"run_id": run_id, **answer})
    (DATA_DIR / "runs.json").write_text(
        json.dumps(runs, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (DATA_DIR / "expected.json").write_text(
        json.dumps(expected, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    for run in runs:
        chars = sum(len(e["content"]) for e in run["events"])
        print(f"{run['run_id']}: {len(run['events'])} steps, {chars:,} characters")


if __name__ == "__main__":
    main()
