"""Check the lookups, the protocol rules, the privilege flags, the outputs and the SIE error rule
offline.

The checks use hand-written strings, the committed data/ and hand-written stand-in model
replies. Nothing here calls SIE or replays a recorded response. Run it with
`uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import csv
import functools
import io
import json
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import matter_system
import review_platform
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

import recipe

RULES = recipe.load_rules()
PRODUCTION = recipe.load_production()
PII = RULES["models"]["pii"]
LAWYERS = matter_system.lawyers_on_matter(PRODUCTION["matter"])

Reply = dict[str, Any] | BaseException | Callable[[str], dict[str, Any]]


def tags(*phrases: tuple[str, str]) -> Callable[[str], dict[str, Any]]:
    """A stand-in PII reply: every occurrence of each (phrase, label) in the window."""

    def reply(window: str) -> dict[str, Any]:
        entities = []
        for phrase, label in phrases:
            start = window.find(phrase)
            while start >= 0:
                end = start + len(phrase)
                entities.append(
                    {"start": start, "end": end, "label": label, "text": phrase, "score": 0.9}
                )
                start = window.find(phrase, end)
        return {"entities": entities}

    return reply


NOTHING = tags()


class StandIn:
    """A stand-in SIE client: hands out the replies in order, one per extract call."""

    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.options: list[Any] = []

    def extract(self, model: str, item: Any, **kwargs: Any) -> Any:
        assert model == PII, model
        self.options.append(kwargs.get("options"))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return reply(str(item["text"]))
        return reply


def record(bates: str = "TST0000001", **fields: str) -> review_platform.Record:
    base = {
        "volume": "TST",
        "bates": bates,
        "custodian": "Nell Brandt",
        "date": "2026-06-15T14:41:00Z",
        "author": "Nell Brandt <nell@pellhammarine.example>",
        "recipients": "Marek Halloran <marek@pellhammarine.example>",
        "doc_type": "Email",
        "coding": RULES["redact_coding"],
        "designation": "",
        "privilege_coding": "Not privileged",
    }
    base.update(fields)
    return review_platform.Record(**base)


def run_texts(
    *texts: str,
    replies: list[Reply] | None = None,
    records: list[review_platform.Record] | None = None,
    lawyers: list[matter_system.Lawyer] | None = None,
) -> list[recipe.Document]:
    """Run the recipe on hand-written documents (one stand-in reply per document, nothing found
    by default) and return them."""
    records = records or [record(f"TST{i:07d}") for i in range(1, len(texts) + 1)]
    documents = [recipe.Document(r, t) for r, t in zip(records, texts, strict=True)]
    client = StandIn(*(replies or [NOTHING] * len(texts)))
    with contextlib.redirect_stdout(io.StringIO()):
        recipe.run(
            cast(SIEClient, client), RULES, documents, LAWYERS if lawyers is None else lawyers
        )
    assert not client.replies, f"{len(client.replies)} replies left unused"
    return documents


def one(text: str, reply: Reply = NOTHING, **fields: str) -> recipe.Document:
    return run_texts(text, replies=[reply], records=[record(**fields)])[0]


def logged(document: recipe.Document) -> list[tuple[int, str]]:
    """(line, kind) of each redaction."""
    return [(recipe.line_of(document.text, s.start), s.kind) for s in document.spans]


# Stand-in replies for the committed set, written by hand to look like what a PII model returns:
# names and ordinary dates it finds but the protocol doesn't list, and the listed values.
SET_REPLIES: list[Reply] = [
    tags(("Osric Pellham", "person"), ("Corwin Ashdale", "person"), ("2 March 2026", "date")),
    tags(
        ("Tomas Reyna", "person"),
        ("3 March 1991", "date of birth"),
        ("12 June 2026", "date"),
        ("SSN", "social security number"),
        ("666-52-1187", "social security number"),
        ("9 Gull Lane, Marshfield, MA 02050", "street address"),
    ),
    tags(("4471900321", "account number"), ("781-555-0164", "phone number")),
    tags(("Priya Okonjo", "person"), ("Kittering", "organization")),
    tags(("Leontyne Farrow", "person"), ("4471 9003 21", "account number")),
    tags(("Gullmere Yacht Club", "organization")),
]


def set_run(*replies: Reply) -> recipe.Result:
    """Run the recipe on the committed set with stand-in replies, one per document."""
    client = StandIn(*replies)
    documents = recipe.build_set(PRODUCTION, RULES)
    with contextlib.redirect_stdout(io.StringIO()):
        result = recipe.run(cast(SIEClient, client), RULES, documents, LAWYERS)
    assert not client.replies, f"{len(client.replies)} replies left unused"
    # Every call asks SIE to fail on a window too long to read whole, never to cut it short.
    assert client.options == [{"overflow_policy": "error"}] * len(client.options), client.options
    return result


def check_lookups() -> None:
    records = review_platform.production_set(PRODUCTION["volume"])
    assert [r.bates for r in records] == [f"PMS000000{i}" for i in range(1, 7)]
    assert records[5].designation == "CONFIDENTIAL"
    assert records[3].privilege_coding == RULES["partly_privileged_coding"]
    assert review_platform.production_set("NO-SUCH-VOLUME") == []
    folder = Path(tempfile.mkdtemp())
    try:
        # A Bates number listed twice makes the log ambiguous: the export is refused.
        seed = folder / "export.csv"
        lines = review_platform.SEED.read_text(encoding="utf-8").splitlines()
        seed.write_text("\n".join([*lines, lines[1]]) + "\n", encoding="utf-8")
        error = raised(lambda: review_platform.production_set(PRODUCTION["volume"], seed))
        assert type(error) is ValueError, error
        assert "listed twice" in str(error)
        # A Bates number in another shape can't reach a file outside the export.
        for bad in ["../rules", "PMS0000099", "pms0000001"]:
            caught = raised(functools.partial(review_platform.extracted_text, bad))
            assert type(caught) is ValueError, (bad, caught)
        # The set on disk and the export must agree: a text file with no row stops the run.
        data = folder / "data"
        shutil.copytree(recipe.DATA_DIR, data)
        (data / "text" / "PMS0000099.txt").write_text("stray", encoding="utf-8")
        error = raised(lambda: recipe.build_set(PRODUCTION, RULES, data))
        assert type(error) is ValueError, error
        assert "PMS0000099" in str(error)
    finally:
        shutil.rmtree(folder)
    # Only rows coded for redaction are prepared.
    other = dict(RULES, redact_coding="Produce")
    assert recipe.build_set(PRODUCTION, other) == []
    # The matter system returns this matter's lawyers only, by exact matter number.
    assert [lawyer.name for lawyer in LAWYERS] == ["Priya Okonjo", "Tobias Wren"]
    assert [lawyer.other_names for lawyer in LAWYERS] == [("Okonjo",), ("Wren",)]
    assert matter_system.lawyers_on_matter("PMS-2026") == []
    print("lookups: export, extracted text and matter team ok")


def check_protocol_rules() -> None:
    # Found by label and shape without the model, glued or spaced; only the listed kinds.
    doc = one(
        "SSN:666521187\nacct#4471900321\nDOB:03/03/1991\nHome address: 9 Gull Lane, Marshfield\n"
        "Phone 617-555-0142, email tomas@example.com, passport X1234567"
    )
    assert logged(doc) == [
        (1, "ssn"),
        (2, "account_number"),
        (3, "date_of_birth"),
        (4, "home_address"),
    ], logged(doc)
    assert doc.status == recipe.CHECKED, doc.reviews + doc.leaks
    assert "617-555-0142" in doc.redacted
    assert "tomas@example.com" in doc.redacted
    assert "acct#[REDACTED: account ending 0321]" in doc.redacted
    # Other ways a label is written: "Acct. No.", "SSN No.:", "my SSN is", a non-breaking space.
    for text, new in [
        ("Acct. No. 4471-9003-21", "Acct. No. [REDACTED: account ending 0321]"),
        ("SSN No.: 666-52-1187", "SSN No.: [REDACTED: SSN]"),
        ("my SSN is 666-52-1187", "my SSN is [REDACTED: SSN]"),
        ("Account: 4471\xa09003\xa021", "Account: [REDACTED: account ending 0321]"),
        ("born on March 3, 1991", "born on [REDACTED: DOB]"),
    ]:
        doc = one(text)
        assert doc.redacted == new, doc.redacted
        assert doc.status == recipe.CHECKED, doc.leaks
    # A negation fills nothing: no number, no redaction, nothing for a person.
    doc = one("SSN: none on file. Date of birth: not given. Account: to follow.")
    assert not doc.spans
    assert doc.status == recipe.CHECKED
    # A short account number is redacted in full: its last four would give most of it away.
    assert "[REDACTED: account]" in one("Account no. 503-118").redacted
    # Two account numbers in one model span: both redacted, one log line each.
    two = "Account 4471900321 and account 5582001144 at Granite Harbor."
    doc = one(two, tags(("4471900321 and account 5582001144", "account number")))
    assert doc.replacements == [
        "[REDACTED: account ending 0321]",
        "[REDACTED: account ending 1144]",
    ], doc.replacements
    assert "and account" in doc.redacted
    # Under one label, the second number has the first between it and the label: it is not
    # silently dropped or silently redacted, it goes to a person.
    two = "Accounts: 4471900321 and 5582001144."
    doc = one(two, tags(("4471900321 and 5582001144", "account number")))
    assert logged(doc) == [(1, "account_number")]
    assert doc.status == recipe.REVIEW
    # The label belongs to the number right after it: a claim reference after an account number
    # is not an account, even when the model says so. It is left readable for a person.
    text = "Account 4471900321, claim 404-18-2210"
    doc = one(text, tags(("404-18-2210", "account number")))
    assert logged(doc) == [(1, "account_number")]
    assert "404-18-2210" in doc.redacted
    assert doc.status == recipe.REVIEW
    # The label counts when it starts at most `label_near_chars` (40) characters before the
    # value: 4, 24 or exactly 40 away it counts, 41 away it is cut off; on another line never.
    near = RULES["thresholds"]["label_near_chars"]
    assert near == 40
    for distance, expected in [(4, True), (24, True), (near, True), (near + 1, False)]:
        text = "SSN" + "-" * (distance - 4) + " 666-52-1187"
        assert text.index("666") == distance
        assert recipe.labelled(text, distance, "ssn", RULES) is expected, (distance, expected)
    assert not recipe.labelled("SSN\n666-52-1187", 4, "ssn", RULES)
    gap = "SSN, written by hand on the form: 666-52-1187"
    doc = one(gap, tags(("666-52-1187", "social security number")))
    assert logged(doc) == [(1, "ssn")]
    assert doc.status == recipe.CHECKED, doc.reviews
    far = "SSN, written by hand on the back of the old intake form: 666-52-1187"
    doc = one(far, tags(("666-52-1187", "social security number")))
    assert not doc.spans
    assert doc.status == recipe.REVIEW
    # A model value with no label nearby is not dropped and not redacted: a person decides.
    doc = one(
        "Reference 666-52-1187 for the file.", tags(("666-52-1187", "social security number"))
    )
    assert not doc.spans
    assert doc.status == recipe.REVIEW
    assert doc.reviews == [
        "line 1: the PII model found what may be a social security number with no label "
        "saying so; left readable, a person decides"
    ], doc.reviews
    assert not doc.leaks, "the held value is reported once, not again by the sweep"
    # The delivery date the model mistakes for a date of birth is not redacted.
    doc = one(
        "Delivered on 12 June 2026. Date of birth 3 March 1991.",
        tags(("12 June 2026", "date of birth"), ("3 March 1991", "date of birth")),
    )
    assert logged(doc) == [(1, "date_of_birth")]
    assert "12 June 2026" in doc.redacted
    assert "3 March 1991" not in doc.redacted
    assert doc.status == recipe.REVIEW
    # A business address is not a home address: left readable for a person, not redacted.
    doc = one("Deliver to 18 Harbor Road, Duxbury.", tags(("18 Harbor Road", "street address")))
    assert not doc.spans
    assert doc.status == recipe.REVIEW
    # The model tagging the label word itself holds no value: nothing to redact or review.
    doc = one("SSN: 666-52-1187", tags(("SSN", "social security number")))
    assert logged(doc) == [(1, "ssn")]
    assert doc.status == recipe.CHECKED, doc.reviews
    # A model SSN with digits in no shape the rules know goes to a person.
    doc = one("SSN 66-652-11870", tags(("66-652-11870", "social security number")))
    assert not doc.spans
    assert doc.status == recipe.REVIEW
    print("protocol rules: label and shape, last four, negation, near misses ok")


def check_spread_and_sweep() -> None:
    a, b, c = run_texts(
        "Credit account: 4471900321",
        "It lands in 4471 9003 21 or 4471-9003-21 or 4471.9003.21 by Friday.",
        "Order 144719003210 and invoice 4471900321-7 are other numbers.",
    )
    assert a.redacted == "Credit account: [REDACTED: account ending 0321]"
    assert b.redacted.count("[REDACTED: account ending 0321]") == 3, b.redacted
    assert {s.source for s in b.spans} == {"spread"}
    assert not c.spans, c.redacted
    # The sweep reads what goes out: an SSN-shaped number nobody labelled is still readable.
    doc = one("Memo: cargo claim 404-18-2210")
    assert doc.leaks == ["line 1: an SSN-shaped number is still readable"], doc.leaks
    assert doc.status == recipe.REVIEW
    # And a labelled value the redaction missed (here, nothing was redacted at all).
    assert recipe.sweep("SSN 666 52 1187", [], [], RULES) == [
        "line 1: a labelled social security number is still readable"
    ]
    # A home address that runs on to a second line: the rule stops at the line end, the sweep
    # sees the rest.
    doc = one("Home address: 9 Gull Lane\nMarshfield, MA 02050\n")
    assert doc.leaks == ["line 1: the rest of a home address on the next line is still readable"], (
        doc.leaks
    )
    # Placeholders never count as a hit, and the line numbers are the original's.
    doc = one("x\ny\nHome address: 9 Gull Lane\nSSN: 666-52-1187")
    assert not doc.leaks
    assert logged(doc) == [(3, "home_address"), (4, "ssn")]
    # Overlapping spans merge into one of the longest span's kind, with every way it was found.
    merged = recipe.merge_spans(
        [
            recipe.Span(5, 15, "account_number", "rule"),
            recipe.Span(3, 18, "ssn", "model"),
            recipe.Span(20, 25, "ssn", "spread"),
        ],
        RULES,
    )
    assert merged == [
        recipe.Span(3, 18, "ssn", "model+rule"),
        recipe.Span(20, 25, "ssn", "spread"),
    ], merged
    # On a tie in length, the kind listed first in the protocol wins, whatever the input order.
    for pair in (
        [recipe.Span(0, 10, "account_number", "model"), recipe.Span(2, 12, "ssn", "rule")],
        [recipe.Span(2, 12, "ssn", "rule"), recipe.Span(0, 10, "account_number", "model")],
    ):
        assert recipe.merge_spans(pair, RULES) == [recipe.Span(0, 12, "ssn", "model+rule")]
    # Touching spans (one ends where the next starts) stay apart.
    assert (
        len(
            recipe.merge_spans(
                [recipe.Span(0, 5, "ssn", "rule"), recipe.Span(5, 9, "ssn", "rule")], RULES
            )
        )
        == 2
    )
    print("spread and sweep: every spelling, no longer numbers, unlabelled shapes ok")


def check_privilege() -> None:
    chain = recipe.build_set(PRODUCTION, RULES)[3]
    flags = recipe.privilege_flags(chain, LAWYERS, RULES)
    assert [(f.first_line, f.last_line) for f in flags] == [(6, 8)], flags
    assert flags[0].reasons == ("in a message to or from Priya Okonjo",)
    assert flags[0].recipients.startswith("Priya Okonjo")
    # The paragraph is the one asking for advice; the forwarded email stays readable.
    assert "hold that back" in chain.text[flags[0].start : flags[0].end]

    def flagged(text: str, **fields: str) -> list[tuple[str, ...]]:
        doc = recipe.Document(record(**fields), text)
        return [f.reasons for f in recipe.privilege_flags(doc, LAWYERS, RULES)]

    # Opposing counsel quoted, "their lawyer", the other side's company sharing a word with the
    # firm, a lawyer with a near-identical name, and one on another matter: no flag.
    assert flagged("Their lawyer, Leontyne Farrow of Ashby Quill LLP, wrote to us.") == []
    assert flagged("Kittering Marine said no. Ask Priya Okonkwo at the yard.") == []
    assert flagged("Hollis Street Storage has the buoys.") == []
    assert flagged("Dorran Fyfe called about another case.") == []
    assert flagged("The wren nested on the dock.") == []
    # Our lawyer by name, by surname as listed, by email, the firm, or a privilege phrase: flag.
    assert flagged("I asked Priya Okonjo about the lease.") == [("names Priya Okonjo",)]
    assert flagged("Okonjo says wait.") == [("names Priya Okonjo",)]
    assert flagged("Ask Wren before Friday.") == [("names Tobias Wren",)]
    assert flagged("cc tobias.wren@hollis-crane.example") == [("names Tobias Wren",)]
    assert flagged("Hollis & Crane told us to wait.") == [("names Hollis & Crane",)]
    assert flagged("Our lawyers say we can hold it.") == [('says "Our lawyers"',)]
    assert flagged("PRIVILEGED AND CONFIDENTIAL") == [('says "PRIVILEGED"',)]
    # One body line starting "To:" is a paragraph, not a header that is never flagged.
    assert flagged("Notes.\n\nTo: Priya Okonjo - can I hold the payment?") == [
        ("names Priya Okonjo",)
    ]
    # Windows line endings split paragraphs the same way: the header is not flagged.
    crlf = "From: Osric <o@x.example>\r\nTo: Priya Okonjo <priya.okonjo@hollis-crane.example>"
    assert flagged(crlf + "\r\n\r\nPriya, can I hold it?\r\n") == [
        ("in a message to or from Priya Okonjo",)
    ]
    # The first message's people come from the export too, even without a header in the text.
    assert flagged(
        "Can we hold it back?", recipients="Priya Okonjo <priya.okonjo@hollis-crane.example>"
    ) == [("in a message to or from Priya Okonjo",)]
    # A later message in a chain gets its own people and log fields from its header.
    text = (
        "From: Osric Pellham <osric@pellhammarine.example>\nTo: Nell Brandt\n\nNell, see below.\n\n"
        "-----Original Message-----\nFrom: Priya Okonjo <priya.okonjo@hollis-crane.example>\n"
        "To: Osric Pellham\nDate: Mon, 6 Jul 2026 09:00:00 -0400\n\nOsric, hold the payment."
    )
    doc = recipe.Document(record(), text)
    found = recipe.privilege_flags(doc, LAWYERS, RULES)
    assert [(f.first_line, f.reasons) for f in found] == [
        (11, ("in a message to or from Priya Okonjo",))
    ], found
    assert found[0].author.startswith("Priya Okonjo")
    # Header dates and export dates come out in one form, ISO 8601 UTC.
    assert found[0].date == "2026-07-06T13:00:00Z", found[0].date
    assert recipe.privilege_flags(chain, LAWYERS, RULES)[0].date == "2026-07-07T22:05:00Z"
    assert recipe.utc_date("2026-06-15T14:41:00Z") == "2026-06-15T14:41:00Z"
    assert recipe.utc_date("23 October 2026") == "23 October 2026"
    # Coded partly privileged with nothing flagged: a person finds the privileged part.
    doc = one("Nothing to see here.", privilege_coding=RULES["partly_privileged_coding"])
    assert doc.status == recipe.REVIEW
    assert "coded it partly privileged" in doc.reviews[0]
    # No lawyers on the matter: every document goes to a person.
    doc = run_texts("Hello.", lawyers=[])[0]
    with contextlib.redirect_stdout(io.StringIO()):
        recipe.flag_no_lawyers([], [doc])
    assert doc.status == recipe.REVIEW
    print("privilege: lawyer list, phrases, chains, near misses, coding cross-check ok")


def check_set_and_outputs() -> None:
    result = set_run(*SET_REPLIES)
    statuses = [doc.status for doc in result.documents]
    assert statuses == [
        recipe.CHECKED,
        recipe.CHECKED,
        recipe.REVIEW,
        recipe.PRIVILEGE,
        recipe.CHECKED,
        recipe.CHECKED,
    ], statuses
    assert set(recipe.STATUSES) == {"redactions_checked", "privilege_flag", "needs_review"}
    first, crew, wire, chain, quote, prices = result.documents
    assert not first.spans
    assert first.redacted == first.text
    assert [s.kind for s in crew.spans] == ["date_of_birth", "ssn", "home_address"]
    assert "Tomas Reyna" in crew.redacted
    assert "12 June 2026" in crew.redacted
    assert wire.leaks == ["line 10: an SSN-shaped number is still readable"]
    assert "[REDACTED: account ending 0321]" in wire.redacted
    assert "[REDACTED: account ending 0321]" in quote.redacted
    assert not quote.flags
    assert not chain.spans
    assert not prices.spans
    assert prices.record.designation == "CONFIDENTIAL"

    folder = Path(tempfile.mkdtemp())
    try:
        recipe.write_outputs(folder, result, RULES, PRODUCTION)
        values = ["666-52-1187", "3 March 1991", "9 Gull Lane", "4471900321", "4471 9003 21"]
        for name in ["report.csv", "redaction_log.csv", "privilege_flags.csv", "spans.json"]:
            written = (folder / name).read_text(encoding="utf-8")
            for value in values:
                assert value not in written, (name, value)
        for path in (folder / "redacted").iterdir():
            for value in values:
                assert value not in path.read_text(encoding="utf-8"), (path.name, value)
        log = list(csv.DictReader((folder / "redaction_log.csv").open(encoding="utf-8")))
        assert [(r["bates"], r["line"]) for r in log] == [
            ("PMS0000002", "8"),
            ("PMS0000002", "11"),
            ("PMS0000002", "12"),
            ("PMS0000003", "6"),
            ("PMS0000005", "8"),
        ], log
        assert tuple(log[0]) == recipe.LOG_FIELDS
        flags = list(csv.DictReader((folder / "privilege_flags.csv").open(encoding="utf-8")))
        assert [(f["bates"], f["lines"], f["associate_decision"]) for f in flags] == [
            ("PMS0000004", "6-8", "")
        ]
        spans = json.loads((folder / "spans.json").read_text(encoding="utf-8"))
        crew_spans = spans["documents"][1]["redactions"]
        text = crew.text
        assert [text[s["start"] : s["end"]] for s in crew_spans] == [
            "3 March 1991",
            "666-52-1187",
            "9 Gull Lane, Marshfield, MA 02050",
        ]
        assert crew_spans[1]["found_by"] == ["PII model", "label and shape rule"]
    finally:
        shutil.rmtree(folder)
    # A cell a spreadsheet would run as a formula is quoted, in every CSV the run writes.
    assert recipe.csv_safe({"a": "=HYPERLINK(1)", "b": 3}) == {"a": "'=HYPERLINK(1)", "b": 3}
    folder = Path(tempfile.mkdtemp())
    try:
        recipe.write_csv(folder / "x.csv", ("a",), [{"a": "=HYPERLINK(1)"}])
        assert (folder / "x.csv").read_text(encoding="utf-8").splitlines()[1] == "'=HYPERLINK(1)"
    finally:
        shutil.rmtree(folder)
    # A value inside a flagged paragraph is redacted in the excerpt the associate reads.
    doc = one(
        "Priya,\nmy SSN is 666-52-1187, can I hold the payment?",
        recipients="Priya Okonjo <priya.okonjo@hollis-crane.example>",
    )
    rows = recipe.flag_rows(recipe.Result([doc], 1), RULES)
    assert "666-52-1187" not in rows[0]["paragraph"]
    assert "[REDACTED: SSN]" in rows[0]["paragraph"]
    print("set and outputs: planted statuses with stand-in replies, no values written ok")


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
    rest = SET_REPLIES[1:]
    for error in TRANSIENT:
        assert not recipe.is_fatal(error), f"{type(error).__name__} should be transient"
    for error in FATAL:
        assert recipe.is_fatal(error), f"{type(error).__name__} should stop the run"
    # A transient failure sends that document to review as not read, the reason cut short; the
    # others run.
    long = "connection refused " + "x" * 300
    prefix = "window 1 was not read: the SIE call failed: "
    for error in [*TRANSIENT, SIEConnectionError(long)]:
        result = set_run(error, *rest)
        first = result.documents[0]
        assert first.status == recipe.REVIEW
        why = first.errors[0]
        assert why.startswith(prefix), why
        assert result.documents[3].status == recipe.PRIVILEGE
    assert len(why) == len(prefix) + len("SIEConnectionError: ") + recipe.ERROR_CHARS, why
    # A reply missing `entities` is a failed call on the same path, not "nothing found".
    result = set_run({"error": {"code": "x"}}, *rest)
    reason = result.documents[0].errors[0]
    assert "SIEError: the reply has no `entities` (item error: {'code': 'x'})" in reason, reason
    assert set_run({"id": "no entities"}, *rest).documents[0].status == recipe.REVIEW
    # An empty list is a real "nothing found": the rules decide, the call counts as a success.
    assert not set_run({"entities": []}, *rest).documents[0].errors
    # A span whose offsets don't match its text sends the document to review.
    bad = {"entities": [{"start": 0, "end": 4, "label": "person", "text": "nope"}]}
    assert set_run(bad, *rest).documents[0].status == recipe.REVIEW

    # Every call failed: the run stops with the full message and nothing is returned to write.
    message = stopped(functools.partial(set_run, *[SIEConnectionError(long)] * 6))
    assert message.startswith(f"Every SIE call failed (6 of 6 to {PII})"), message
    assert long in message
    assert "Check SIE_BASE_URL and your network." in message
    assert "No outputs were written." in message
    message = stopped(functools.partial(set_run, *[ServerError("busy", status_code=503)] * 6))
    assert "may still be starting or SIE is busy; run again in a few minutes." in message
    # A refusal on a later call stops the run at once, with SIE's full message.
    message = stopped(
        functools.partial(
            set_run, NOTHING, InsufficientCreditsError("no credits " + "y" * 300), *rest[1:]
        )
    )
    assert message.startswith(f"SIE refused the call to {PII}, "), message
    assert "InsufficientCreditsError (HTTP 402, code INSUFFICIENT_CREDITS)" in message, message
    assert "y" * 300 in message
    assert "SIE_API_KEY" in message
    message = stopped(functools.partial(set_run, ModelLoadFailedError("gated", permanent=True)))
    assert message.startswith(f"SIE can't load {PII}, ModelLoadFailedError"), message
    message = stopped(functools.partial(set_run, RequestError("unexpected body")))
    assert "SIE_API_KEY" not in message, message
    assert "SIE_BASE_URL" in message, message
    # A bug in our own code is not hidden, and main turns only SIEStopped into a clean exit.
    caught = raised(lambda: set_run(TypeError("bad argument"), *rest))
    assert type(caught) is TypeError, f"TypeError became {type(caught).__name__}"

    def bug() -> None:
        raise RuntimeError("a bug in recipe.py")

    def refusal() -> None:
        raise recipe.SIEStopped("SIE refused")

    assert type(raised(lambda: recipe.stop_on_refusal(bug))) is RuntimeError
    caught = raised(lambda: recipe.stop_on_refusal(refusal))
    assert type(caught) is SystemExit
    assert str(caught) == "Stopped: SIE refused"
    print("sie failure: transient errors go to review; refusals and all-failed stop ok")


if __name__ == "__main__":
    check_lookups()
    check_protocol_rules()
    check_spread_and_sweep()
    check_privilege()
    check_set_and_outputs()
    check_sie_failure()
