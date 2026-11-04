"""Check the recipe's document breaks, Bates check, field and privilege rules offline.

The checks use hand-written strings, the committed data/ and hand-written stand-in model
replies. Nothing here calls SIE or replays a recorded response. Run it with
`uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import csv
import io
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from matter import MatterSystem, Volume
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
MATTER = MatterSystem()
VOLUME = cast(Volume, MATTER.find_volume("TFL-VOL001"))
LAWYERS = MATTER.counsel_for(VOLUME.matter_id, VOLUME.producing_party)
FOLDER = recipe.DATA_DIR / "TFL-VOL001"
MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"


# --- Hand-written stand-in replies ----------------------------------------------------------


def page_text(page: make_data.Page) -> str:
    """A page as a reader would return it: the body, then the footer legend and the stamp."""
    body = "\n".join(line.removeprefix("# ") for line in page.lines)
    return f"{body}\n\n{make_data.footer(page)}\n{page.bates}"


def committed_pages() -> dict[str, str]:
    """Stand-in reader text for every page of the committed volume, keyed by call id."""
    pages = {}
    for stem, source in (
        ("scans", make_data.SCANS),
        ("emails", make_data.EMAILS),
        ("letter", make_data.LETTER),
    ):
        for number, page in enumerate(source, start=1):
            pages[f"read/{stem}/p{number}"] = page_text(page)
    pages[f"read/{make_data.DOCX_NAME}"] = "\n\n".join(make_data.DOCX_PARAGRAPHS)
    return pages


def stand_in_fields(text: str) -> dict[str, Any]:
    """What a well-behaved gliner2 would return for a window."""
    found: dict[str, Any] = {}
    if match := re.search(rf"{MONTH} \d{{1,2}}, \d{{4}}", text):
        found["date"] = match.group(0)
    if match := re.search(r"From: ([^<\n]+?) <", text):
        found["author"] = match.group(1)
    found["recipients"] = re.findall(r"(?:To|Cc): ([^<\n]+?) <", text)
    for cue, kind in (
        ("Subject:", "email"),
        ("MEMORANDUM", "memo"),
        ("PROOF OF DELIVERY", "delivery receipt"),
        ("AMENDMENT", "contract"),
        ("Dear ", "letter"),
    ):
        if cue in text:
            found["doc_type"] = kind
            break
    return found


def stand_in_score(text: str) -> float:
    """A stand-in privilege probability: high when the window talks about advice or counsel."""
    return 0.9 if re.search(r"advi[cs]e|counsel", text, re.I) else 0.05


def decide_result(score: float) -> dict[str, Any]:
    probabilities = {"potentially privileged": score, "not privileged": 1 - score}
    return {"data": {"privilege": {"probabilities": probabilities}}}


class StandIn:
    """Stands in for SIEClient.

    Read calls return `pages[item id]` in the reader's shape (LightOnOCR entities or a docling
    document). Field calls return `fields(text)`; privilege calls return one result per window
    from `score(text)`. `errors` maps an item id prefix to the errors its calls raise (one per
    call), `replies` to a raw reply that replaces the normal one.
    """

    def __init__(
        self,
        pages: dict[str, str] | None = None,
        errors: dict[str, list[Exception]] | None = None,
        replies: dict[str, Any] | None = None,
        fields: Callable[[str], dict[str, Any]] = stand_in_fields,
        score: Callable[[str], float] = stand_in_score,
    ) -> None:
        self.pages = committed_pages() if pages is None else pages
        self.errors, self.replies = errors or {}, replies or {}
        self.fields, self.score = fields, score
        self.ids: list[str] = []

    def extract(self, model: str, item: Any, **options: Any) -> Any:
        items = item if isinstance(item, list) else [item]
        item_id = items[0]["id"]
        self.ids.append(item_id)
        for prefix, errors in self.errors.items():
            if item_id.startswith(prefix) and errors:
                raise errors.pop(0)
        for prefix, reply in self.replies.items():
            if item_id.startswith(prefix):
                return reply
        if item_id.startswith("read/"):
            text = self.pages[item_id]
            if model == RULES["models"]["ocr"]:
                return {"entities": [{"label": "markdown", "text": text}]}
            return {"data": {"text": text, "document": {"texts": []}}}
        if item_id.startswith("fields/"):
            return {"data": self.fields(items[0]["text"])}
        return [decide_result(self.score(one["text"])) for one in items]


def run(**stand_in: Any) -> recipe.Result:
    files, _ = recipe.input_files(FOLDER, RULES)
    client = StandIn(**stand_in)
    with contextlib.redirect_stdout(io.StringIO()):
        return recipe.run(cast(SIEClient, client), RULES, VOLUME, MATTER, LAWYERS, files)


def by_location(result: recipe.Result) -> dict[str, recipe.Document]:
    return {document.pages[0].location: document for document in result.documents}


def page(text: str, number: int = 1, source: str = "x.pdf") -> recipe.Page:
    stamps = recipe.find_stamps(text, VOLUME, RULES)
    return recipe.Page(source, number, text, "stand-in", stamps=stamps)


def document(*texts: str) -> recipe.Document:
    return recipe.Document([page(text, n) for n, text in enumerate(texts, start=1)])


# --- Checks -----------------------------------------------------------------------------------


def check_examples_volume() -> None:
    """The committed volume end to end with the stand-in replies: every path we wrote in."""
    result = run()
    docs = by_location(result)
    expected = {
        "scans.pdf p1": ("TFL0000001", "TFL0000001", "no_exception"),
        "scans.pdf p2": ("TFL0000002", "TFL0000003", "possible_privilege"),
        "scans.pdf p4": ("TFL0000004", "TFL0000004", "no_exception"),
        "emails.pdf p1": ("TFL0000005", "TFL0000005", "possible_privilege"),
        "emails.pdf p2": ("TFL0000006", "TFL0000006", "possible_privilege"),
        "emails.pdf p3": ("TFL0000006", "TFL0000006", "qc_exception"),
        "emails.pdf p4": ("TFL0000007", "TFL0000007", "possible_privilege"),
        "emails.pdf p5": ("TFL0000009", "TFL0000009", "qc_exception"),
        "TFL0000010.docx": ("TFL0000010", "TFL0000010", "no_exception"),
        "letter.pdf p1": ("TFL0000011", "TFL0000011", "qc_exception"),
        "letter.pdf p2": ("TFL0000012", "TFL0000012", "possible_privilege"),
        "emails.pdf p6": ("TFL0000013", "TFL0000013", "possible_privilege"),
    }
    got = {loc: (d.begbates, d.endbates, d.status) for loc, d in docs.items()}
    assert got == expected, got
    assert docs["scans.pdf p2"].privilege_list_hits == ["Owen Achterberg"]
    assert docs["emails.pdf p2"].privilege_list_hits == ["Priya Raman"]
    assert any("also stamped on emails.pdf p3" in r for r in docs["emails.pdf p2"].qc_reasons)
    assert docs["emails.pdf p3"].qc_reasons == ["Bates TFL0000006 is also stamped on emails.pdf p2"]
    assert docs["emails.pdf p4"].privilege_list_hits == []
    assert docs["emails.pdf p5"].qc_reasons == [
        "Bates gap before this document: TFL0000008 not found"
    ]
    assert docs["TFL0000010.docx"].fields["custodian"] == "Lena Fairbrook"
    # The unnumbered letter: both pages carry a boundary note; page 2 names counsel.
    for loc in ("letter.pdf p1", "letter.pdf p2"):
        assert docs[loc].qc_reasons[0].startswith("Boundary: "), docs[loc].qc_reasons
    assert docs["letter.pdf p2"].privilege_list_hits == ["Imogen Sallis"]
    assert docs["letter.pdf p1"].privilege_reasons == []
    # The long email: two privilege windows, only the second reads privileged.
    windows = docs["emails.pdf p6"].privilege_windows
    assert [w["score"] for w in windows] == [0.05, 0.9], windows
    assert docs["emails.pdf p6"].privilege_list_hits == []
    bates = result.bates
    assert bates["gaps"] == ["TFL0000008"]
    assert bates["duplicates"] == {"TFL0000006": ["emails.pdf p2", "emails.pdf p3"]}
    assert bates["pages_claimed"] == 13
    assert bates["pages_stamped"] == 13
    assert bates["findings"] == [
        "TFL0000006 is stamped on 2 pages: emails.pdf p2, emails.pdf p3",
        "TFL0000008 is inside the claimed range TFL0000001-TFL0000013 but was not found",
    ], bates["findings"]
    print("sample volume: statuses, boundary notes, gap, duplicate and windows ok")


def check_statuses() -> None:
    """The renamed statuses, and that no decision word is left in rules or code."""
    assert {recipe.POSSIBLE_PRIVILEGE, recipe.QC_EXCEPTION, recipe.NO_EXCEPTION} == {
        "possible_privilege",
        "qc_exception",
        "no_exception",
    }
    source = Path(recipe.__file__).read_text(encoding="utf-8")
    for old in ('"privilege_review"', '"qc_review"', '"ok"', "ROUTE"):
        assert old not in source, old
    doc = document("Page 1 of 1\nTFL0000001")
    assert doc.status == "no_exception"
    doc.qc_reasons.append("x")
    assert doc.status == "qc_exception"
    doc.privilege_reasons.append("y")
    assert doc.status == "possible_privilege"
    print("statuses: possible_privilege, qc_exception, no_exception ok")


def check_document_breaks() -> None:
    def split(*texts: str) -> list[tuple[int, list[str]]]:
        pages = [page(text, n) for n, text in enumerate(texts, start=1)]
        docs = recipe.split_documents(pages, RULES)
        return [(len(d.pages), [r.split(":")[0] for r in d.qc_reasons]) for d in docs]

    # Page N of M follows N-1 of the same M: one document, no note.
    assert split("a\nPage 1 of 2", "b\nPage 2 of 2") == [(2, [])]
    # Glued numbering still reads.
    assert split("a\nPage1of2", "b\nPage2of2") == [(2, [])]
    # Page 2 of 2 after Page 1 of 3: not the same M, so a new document with a note, and the
    # first stops short of 3 of 3.
    assert split("a\nPage 1 of 3", "b\nPage 2 of 2") == [(1, ["Boundary"]), (1, ["Boundary"])]
    # Page 3 of 3 straight after Page 1 of 3: a page is missing in between.
    assert split("a\nPage 1 of 3", "c\nPage 3 of 3") == [(1, ["Boundary"]), (1, ["Boundary"])]
    # A page with no numbering and no email header: boundary note.
    assert split("Dear Marcus,\nthe logs", "Kind regards,\nImogen Sallis") == [
        (1, ["Boundary"]),
        (1, ["Boundary"]),
    ]
    # An email header starts a document; Markdown bold around it is fine.
    assert split("**From:** A <a@x.example>\nSubject: hi\nbody") == [(1, [])]
    assert split("From: A\nTo: B\nSubject: hi") == [(1, [])]
    # "From:" further down the page is not a header.
    assert split("Notes\nFrom: A\nSubject: hi") == [(1, ["Boundary"])]
    # The last "Page N of M" on the page counts, not one quoted in the body.
    assert split("see Page 2 of 9 of the contract\nPage 1 of 1") == [(1, [])]
    # A continuation in another file starts its own document.
    first, second = page("a\nPage 1 of 2", 1, "a.pdf"), page("b\nPage 2 of 2", 1, "b.pdf")
    docs = recipe.split_documents([first, second], RULES)
    assert len(docs) == 2
    assert docs[1].qc_reasons[0].startswith("Boundary")
    print("document breaks: numbering, email headers and boundary notes ok")


def check_bates() -> None:
    stamps = recipe.find_stamps
    assert stamps("TFL0000005", VOLUME, RULES) == ["TFL0000005"]
    assert stamps("**TFL-0000005**", VOLUME, RULES) == ["TFL0000005"]
    assert stamps("TFL 00O0005", VOLUME, RULES) == ["TFL0000005"]  # OCR lookalike O for 0
    # Near-identical but different: eight digits, another prefix, a prefix glued to a word.
    assert stamps("TFL00000051", VOLUME, RULES) == []
    assert stamps("TFLX0000005", VOLUME, RULES) == []
    assert stamps("XTFL0000005", VOLUME, RULES) == []

    def checked(*texts: str) -> tuple[dict[str, Any], list[recipe.Document]]:
        docs = [document(text) for text in texts]
        return recipe.check_bates(docs, VOLUME, RULES), docs

    texts = [f"Page 1 of 1 body text long enough to read\nTFL{n:07d}" for n in range(1, 14)]
    report, docs = checked(*texts)
    assert report["continuous"], report
    assert report["findings"] == [], report
    # A page stamped outside the claimed range.
    report, docs = checked(*texts, "Page 1 of 1 a late page with enough text\nTFL0000014")
    assert report["outside_claimed_range"] == ["TFL0000014"]
    assert "outside the claimed range" in docs[-1].qc_reasons[0]
    # The cover letter claims 13 pages; only 12 arrive: the missing last page is a gap.
    report, _ = checked(*texts[:-1])
    assert report["gaps"] == ["TFL0000013"], report
    # A Bates number quoted in the body is a reference, not the page's stamp: the footer's
    # stamp numbers the page, even when the quote sits on the line just above the footer.
    quoted = "Page 1 of 1 the memo is at\nTFL0000002 see above\nCONFIDENTIAL - Custodian: Tom Okoye"
    assert stamps(quoted + "\nTFL0000013", VOLUME, RULES) == ["TFL0000013"]
    report, docs = checked(*texts[:-1], quoted + " TFL0000013")
    assert report["continuous"], report
    assert docs[-1].qc_reasons == [], docs[-1].qc_reasons
    # A page whose only Bates number is quoted in the body has no stamp of its own.
    assert stamps("as stamped on TFL0000002, the memo\n**CONFIDENTIAL**", VOLUME, RULES) == []
    # Two stamps in the footer is an exception.
    report, docs = checked(*texts[:-1], "Page 1 of 1 body text long enough\nTFL0000013 TFL0000002")
    assert any("More than one Bates stamp" in r for r in docs[-1].qc_reasons)
    # A page that reads but holds fewer than min_page_chars characters is an exception, and
    # its stamp still counts.
    short_page = "Page 1 of 1\nTFL0000013"
    assert len(short_page) < RULES["thresholds"]["min_page_chars"]
    report, docs = checked(*texts[:-1], short_page)
    assert report["continuous"], report
    assert docs[-1].qc_reasons == ["x.pdf p1 could not be read"], docs[-1].qc_reasons
    # An unread page: no stamp finding for it, and the gap finding says to read it first.
    unread = recipe.Page("x.pdf", 13, "", "stand-in", error="the SIE call failed: busy")
    docs = [document(text) for text in texts[:-1]] + [recipe.Document([unread])]
    report = recipe.check_bates(docs, VOLUME, RULES)
    assert report["pages_not_read"] == ["x.pdf p13"]
    assert not report["continuous"]
    assert "read them before raising this" in report["findings"][0], report["findings"]
    # A two-page document whose pages are not stamped consecutively.
    two = document("Page 1 of 2 enough text here to read\nTFL0000001", "Page 2 of 2\nTFL0000003")
    recipe.check_bates([two], VOLUME, RULES)
    assert any("not stamped consecutively" in r for r in two.qc_reasons), two.qc_reasons
    print("bates: stamps, near misses, range, gaps, duplicates and unread pages ok")


def settle(*windows: dict[str, Any], custodian: str = "Tom Okoye") -> recipe.Document:
    """Run finish_fields on stand-in field replies, one per window."""
    doc = document(f"From: x\nSubject: y\nCONFIDENTIAL - Custodian: {custodian}\nTFL0000005")
    found: dict[str, list[tuple[int, str]]] = {}
    for index, reply in enumerate(windows, start=1):
        for name, value in reply.items():
            found.setdefault(name, []).append((index, value))
    recipe.finish_fields(doc, found, VOLUME, MATTER, RULES)
    return doc


def check_fields() -> None:
    dates = recipe.dates_in
    fmts = RULES["date_formats"]
    assert [d.isoformat() for d in dates("Wednesday, March 12, 2025 4:41 PM", fmts)] == [
        "2025-03-12"
    ]
    assert len(dates("March 12, 2025 and March 18, 2025", fmts)) == 2
    assert dates("no date given", fmts) == []
    base = {"doc_type": "email"}
    doc = settle({**base, "date": "March 12, 2025", "author": "Marcus Bell"})
    assert doc.fields["date"] == "2025-03-12"
    assert doc.qc_reasons == [], doc.qc_reasons
    # One span holding two dates: flagged, and the date is left blank.
    doc = settle({**base, "date": "March 12, 2025 and March 18, 2025"})
    assert "date" not in doc.fields
    assert doc.qc_reasons == ["Date 'March 12, 2025 and March 18, 2025' holds more than one date"]
    # Two windows with different dates (a quoted thread): flagged, never first-one-wins.
    doc = settle({**base, "date": "March 12, 2025"}, {"date": "March 18, 2025"})
    assert "date" not in doc.fields
    assert "Windows disagree on the date" in doc.qc_reasons[0]
    # The same date written two ways is one date.
    doc = settle({**base, "date": "March 12, 2025"}, {"date": "12 March 2025"})
    assert doc.fields["date"] == "2025-03-12"
    assert doc.qc_reasons == []
    # An author span naming two people.
    doc = settle({**base, "date": "March 12, 2025", "author": "Marcus Bell and Priya Raman"})
    assert "author" not in doc.fields
    assert doc.qc_reasons == ["Author 'Marcus Bell and Priya Raman' names more than one person"]
    # Two windows typing the document differently.
    doc = settle({**base, "date": "March 12, 2025"}, {"doc_type": "letter"})
    assert "doc_type" not in doc.fields
    assert "disagree on the doc type" in doc.qc_reasons[0]
    # A missing required field; a date that is not a date.
    doc = settle({"date": "March 12, 2025"})
    assert doc.qc_reasons == ["No doc type found"]
    doc = settle({**base, "date": "last Tuesday"})
    assert doc.qc_reasons[0] == "Date 'last Tuesday' could not be read as a date"
    # Two custodians on one document's pages.
    two = document(
        "Page 1 of 2\nCONFIDENTIAL - Custodian: Tom Okoye\nTFL0000001",
        "Page 2 of 2\nCONFIDENTIAL - Custodian: Lena Fairbrook\nTFL0000002",
    )
    recipe.finish_fields(
        two, {"date": [(1, "March 1, 2025")], "doc_type": [(1, "memo")]}, VOLUME, MATTER, RULES
    )
    assert two.qc_reasons == ["Pages name different custodians: Tom Okoye, Lena Fairbrook"]
    # OCR's Markdown bold around the legend is stripped from the custodian.
    bold = document("**CONFIDENTIAL - Custodian:** Tom Okoye  \n**TFL0000004")
    assert recipe.read_custodians(bold, VOLUME, RULES) == ["Tom Okoye"]
    # A native the cover letter doesn't list (near-identical name): no custodian, flagged.
    native = recipe.Document(
        [recipe.Page("TFL0000010 (1).docx", 1, "text", "docling", native=True)]
    )
    recipe.finish_fields(
        native,
        {"date": [(1, "April 1, 2025")], "doc_type": [(1, "contract")]},
        VOLUME,
        MATTER,
        RULES,
    )
    assert native.qc_reasons == [
        "TFL0000010 (1).docx is not listed in the cover letter",
        "No custodian found",
    ], native.qc_reasons
    # extract_fields: a value not in the text is dropped; an enum outside the choices too.
    doc = document("From: Marcus Bell <m@x.example>\nSubject: hi\nMarch 12, 2025")
    client = StandIn(
        pages={},
        fields=lambda _: {
            "author": "Owen Achterberg",
            "doc_type": "Email",
            "date": "March 12, 2025",
        },
    )
    found = recipe.extract_fields(cast(SIEClient, client), recipe.Calls(), doc, RULES)
    assert found == {"date": [(1, "March 12, 2025")]}, found
    assert "Dropped author 'Owen Achterberg': not in the text" in doc.notes
    print("fields: two dates, conflicting windows, two authors, custodians, natives ok")


def hits(text: str) -> list[str]:
    return [lawyer.name for lawyer in recipe.lawyer_hits(text, LAWYERS)]


def check_privilege() -> None:
    assert hits("Cc: Priya Raman <praman@wenlowcroft.example>") == ["Priya Raman"]
    assert hits("signed P. Raman") == ["Priya Raman"]
    assert hits("mailto:PRAMAN@WENLOWCROFT.EXAMPLE") == ["Priya Raman"]
    assert hits("Imogen\nSallis") == ["Imogen Sallis"]
    assert hits("**Owen Achterberg**") == ["Owen Achterberg"]
    # Near-identical names are other people.
    assert hits("Priya Ramanathan, Accounts") == []
    assert hits("xpraman@wenlowcroft.example") == []
    # Our own lawyer in the other side's files is not the other side's privileged material.
    assert hits("Letter from Nadia Okafor, Brightwell Lane LLP") == []
    assert [lawyer.name for lawyer in LAWYERS] == [
        "Owen Achterberg",
        "Priya Raman",
        "Imogen Sallis",
    ]

    def decided(*scores: float, text: str = "body") -> recipe.Document:
        doc = document(text + "\nTFL0000001")
        doc.privilege_windows = [{"score": s} for s in scores]
        doc.privilege_score = max(scores) if scores else None
        recipe.decide_privilege(doc, LAWYERS, RULES)
        return doc

    assert decided(0.05).privilege_reasons == []
    assert decided(0.50).privilege_reasons == ["Scored potentially privileged (0.500)"]
    assert decided(0.35).privilege_reasons == ["Privilege score unsure (0.350)"]
    assert decided(0.349).privilege_reasons == []
    # Just under privileged_at reads as unsure, and the number shown doesn't round up to 0.50.
    assert decided(0.4975).privilege_reasons == ["Privilege score unsure (0.497)"]
    assert decided().privilege_reasons == ["No privilege score for every window (no answer)"]
    # The list fires whatever the score says, even beside "not privileged" in the text.
    doc = decided(0.01, text="This email is not privileged. Cc: Priya Raman")
    assert doc.status == "possible_privilege"
    assert doc.privilege_list_hits == ["Priya Raman"]

    # Scoring: the strongest window decides, taken as is. Windows (.45, .55) and (.01, .99)
    # renormalised gave .45 / (.45 + .99) = .31, under the unsure band.
    long = " ".join(f"w{n}" for n in range(200)) + "\nTFL0000001"
    scores = iter([0.45, 0.01])
    doc = document(long)
    client = StandIn(pages={}, score=lambda _: next(scores))
    recipe.score_privilege(cast(SIEClient, client), recipe.Calls(), doc, VOLUME, RULES)
    assert [w["score"] for w in doc.privilege_windows] == [0.45, 0.01]
    assert doc.privilege_score == 0.45
    recipe.decide_privilege(doc, LAWYERS, RULES)
    assert doc.privilege_reasons == ["Privilege score unsure (0.450)"]
    # Windows are cut per page: two short pages are scored separately.
    two = document(
        "first page\nPage 1 of 2\nTFL0000001", "counsel advises\nPage 2 of 2\nTFL0000002"
    )
    recipe.score_privilege(cast(SIEClient, StandIn(pages={})), recipe.Calls(), two, VOLUME, RULES)
    assert [(w["page"], w["score"]) for w in two.privilege_windows] == [
        ("x.pdf p1", 0.05),
        ("x.pdf p2", 0.9),
    ]
    # The legend and stamps are not part of the document as written.
    stripped = recipe.strip_footer_legend(
        "Body\nCONFIDENTIAL - Custodian: A\nTFL0000001", VOLUME, RULES
    )
    assert stripped == "Body", stripped
    print("privilege: lawyer list, near-miss names, our own counsel, strongest window ok")


def check_adversarial() -> None:
    """Stand-in replies that try to get a confident status out of a bad answer."""

    def scored(results: list[Any] | Any) -> recipe.Document:
        doc = document("A plain email body\nTFL0000001")
        client = StandIn(pages={}, replies={"privilege/": results})
        recipe.score_privilege(cast(SIEClient, client), recipe.Calls(), doc, VOLUME, RULES)
        recipe.decide_privilege(doc, LAWYERS, RULES)
        return doc

    bad: list[Any] = [
        [{"data": {"privilege": {"probabilities": {"potentially privileged": float("nan")}}}}],
        [{"data": {"privilege": {"probabilities": {"potentially privileged": 1.7}}}}],
        [{"data": {"privilege": {"probabilities": {"potentially privileged": -0.2}}}}],
        [{"data": {"privilege": {"probabilities": {"potentially privileged": "low"}}}}],
        [{"data": {"privilege": {"probabilities": {"not privileged": 0.99}}}}],
        [{"data": {"privilege": {"label": "not privileged"}}}],
        [{"data": {}}],
        [{"error": {"code": "OVERFLOW", "message": "input too long"}}],
        [],  # fewer results than windows
        [None],
        {"not": "a list"},
    ]
    for reply in bad:
        doc = scored(reply)
        assert doc.status == "possible_privilege", reply
        assert doc.privilege_reasons[-1].startswith("No privilege score for every window"), reply
    # One good low window and one failed window: never a confident "not privileged".
    doc = document(" ".join(f"w{n}" for n in range(200)) + "\nTFL0000001")
    client = StandIn(pages={}, replies={"privilege/": [decide_result(0.02), {"data": {}}]})
    recipe.score_privilege(cast(SIEClient, client), recipe.Calls(), doc, VOLUME, RULES)
    recipe.decide_privilege(doc, LAWYERS, RULES)
    assert doc.status == "possible_privilege", doc.privilege_reasons
    # A field reply with a list where a string belongs, or junk, is dropped, not kept.
    doc = document("From: A <a@x.example>\nSubject: hi\nMarch 12, 2025")
    client = StandIn(
        pages={}, fields=lambda _: {"date": ["March 12, 2025"], "doc_type": 7, "author": "--"}
    )
    found = recipe.extract_fields(cast(SIEClient, client), recipe.Calls(), doc, RULES)
    assert found == {}, found
    print(f"adversarial: {len(bad) + 2} bad replies never give a confident status ok")


# SIE Cloud's answer to a gliner2 output_schema call that finds nothing; a failed call here.
EMPTY_RESULT = "Inference completed without a valid conserved billing settlement"
TRANSIENT: list[SIEError] = [
    SIEConnectionError("connection refused"),
    ServerError("unavailable", status_code=503),
    ServerError(EMPTY_RESULT, status_code=500),
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
    # A transient error reading the memo: not read, so a QC exception and no privilege score.
    for error in TRANSIENT:
        memo = by_location(run(errors={"read/scans/p1": [error]}))["scans.pdf p1"]
        assert memo.status == "possible_privilege", type(error).__name__
        assert memo.qc_reasons[0].startswith("scans.pdf p1 was not read: the SIE call failed: ")
        assert "scans.pdf p1 not read" in memo.privilege_reasons[0], memo.privilege_reasons
    # A transient error on a field window, SIE Cloud's empty-result 500 included: QC exception.
    empty = ServerError(EMPTY_RESULT, status_code=500)
    memo = by_location(run(errors={"fields/scans-p1": [empty]}))["scans.pdf p1"]
    assert memo.status == "qc_exception"
    assert memo.qc_reasons[0].startswith(
        "Field window 1 of 1 was not read: the SIE call failed: ServerError (HTTP 500)"
    ), memo.qc_reasons
    # A transient error on the privilege call: no score, so the lawyer sees it.
    memo = by_location(run(errors={"privilege/scans-p1": [ModelLoadingError("x")]}))["scans.pdf p1"]
    assert memo.status == "possible_privilege"
    assert "the SIE call failed: ModelLoadingError" in memo.privilege_reasons[0]
    # A reply missing its key is a failed call; an empty answer is a real "nothing found".
    memo = by_location(run(replies={"read/scans/p1": {"something": []}}))["scans.pdf p1"]
    assert "replied without 'entities'" in memo.qc_reasons[0], memo.qc_reasons
    email = by_location(run(replies={"read/emails/p1": {"text": "x"}}))["emails.pdf p1"]
    assert "replied without 'data'" in email.qc_reasons[0], email.qc_reasons
    memo = by_location(run(replies={"fields/scans-p1": {"nothing": {}}}))["scans.pdf p1"]
    assert "replied without 'data'" in memo.qc_reasons[0], memo.qc_reasons
    memo = by_location(run(replies={"fields/scans-p1": {"data": {}}}))["scans.pdf p1"]
    assert memo.qc_reasons == ["No date found", "No doc type found"], memo.qc_reasons
    memo = by_location(run(replies={"read/scans/p1": {"entities": []}}))["scans.pdf p1"]
    assert memo.qc_reasons[0] == "scans.pdf p1 was not read: the OCR reply had no markdown text"
    # Every call failed (a dead SIE_BASE_URL): the run stops, with the full message.
    long = "connection refused " + "x" * 300
    dead = {"": [SIEConnectionError(long) for _ in range(100)]}
    message = stopped(lambda: run(errors=dead))
    assert message.startswith("Every SIE call failed ("), message
    assert long in message
    assert "Check SIE_BASE_URL and your network." in message, message
    busy = {"": [ModelLoadingError("loading") for _ in range(100)]}
    message = stopped(lambda: run(errors=busy))
    assert "may still be starting or SIE is busy; run again in a few minutes." in message
    # A refusal stops the run at once, with the full message, HTTP status and code.
    refusal = {"read/scans/p2": [InsufficientCreditsError("no credits " + "y" * 300)]}
    message = stopped(lambda: run(errors=refusal))
    assert "InsufficientCreditsError (HTTP 402, code INSUFFICIENT_CREDITS)" in message, message
    assert "y" * 300 in message
    assert "SIE_API_KEY" in message
    message = stopped(lambda: run(errors={"": [ModelLoadFailedError("gated", permanent=True)]}))
    assert message.startswith("SIE can't load docling"), message
    message = stopped(lambda: run(errors={"": [RequestError("unexpected body")]}))
    assert "SIE_API_KEY" not in message, message
    assert "SIE_BASE_URL" in message, message
    # A bug in our own code is raised as is; main turns only SIEStopped into a clean exit.
    try:
        run(errors={"": [TypeError("bad argument")]})
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


def check_outputs() -> None:
    assert recipe.csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert recipe.csv_safe("-2") == "'-2"
    assert recipe.csv_safe("TFL0000001") == "TFL0000001"
    result = run()
    result.documents[0].fields["author"] = "=cmd()"
    with tempfile.TemporaryDirectory() as name:
        recipe.write_outputs(Path(name), result)
        with (Path(name) / "index.csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        with (Path(name) / "privilege-review.csv").open(encoding="utf-8") as handle:
            queue = list(csv.DictReader(handle))
    assert rows[0]["AUTHOR"] == "'=cmd()", rows[0]
    assert [row["STATUS"] for row in rows].count("possible_privilege") == len(queue) == 6
    assert {row["BEGBATES"] for row in queue} == {
        *("TFL0000002", "TFL0000005", "TFL0000006", "TFL0000007", "TFL0000012", "TFL0000013")
    }
    print("outputs: csv_safe, index and privilege queue ok")


def check_committed_data() -> None:
    assert VOLUME.bates_begin == "TFL0000001"
    assert VOLUME.bates_end == "TFL0000013"
    assert (VOLUME.bates_first, VOLUME.bates_last) == (1, 13)
    assert MATTER.find_volume("tfl-vol001 ") == VOLUME
    assert MATTER.find_volume("TFL-VOL01") is None  # near-identical volume id
    native = MATTER.find_native("TFL-VOL001", "TFL0000010.docx")
    assert native is not None
    assert native.custodian == "Lena Fairbrook"
    files, skipped = recipe.input_files(FOLDER, RULES)
    assert [p.name for p in files] == ["TFL0000010.docx", "emails.pdf", "letter.pdf", "scans.pdf"]
    assert skipped == []
    for path in FOLDER.glob("*.pdf"):
        raw = path.read_bytes()
        for marker in (b"/Info", b"/Title", b"/Author", b"/CreationDate", b"/Producer", b"xmpmeta"):
            assert marker not in raw, f"{path.name} carries {marker!r}"
    # The long email's privileged paragraph starts after the first privilege window ends.
    email = recipe.strip_footer_legend(page_text(make_data.EMAILS[-1]), VOLUME, RULES)
    limits = RULES["thresholds"]
    windows = recipe.word_windows(
        email, limits["privilege_window_words"], limits["privilege_window_overlap_words"]
    )
    assert len(windows) == 2
    assert "General Counsel" not in windows[0]
    assert "advised" in windows[1]
    assert make_data.MATTER.endswith("(fictional)")
    print("committed data: matter seeds, volume files, no PDF metadata, long email windows ok")


def check_claimed_range() -> None:
    """The cover letter's range is read once, as numbers; a dash after the prefix is fine and
    a label that isn't the prefix and digits stops the lookup instead of reading as -1."""
    with tempfile.TemporaryDirectory() as name:
        folder = Path(name)
        for seed in ("matter_natives.csv", "matter_counsel.csv"):
            (folder / seed).write_bytes((recipe.DATA_DIR / seed).read_bytes())
        log = (recipe.DATA_DIR / "matter_productions.csv").read_text(encoding="utf-8")
        dashed = log.replace(",TFL0000001,", ",TFL-0000001,")
        (folder / "matter_productions.csv").write_text(dashed, encoding="utf-8")
        volume = MatterSystem(folder).find_volume("TFL-VOL001")
        assert volume is not None
        assert (volume.bates_first, volume.bates_last) == (1, 13)
        texts = [f"Page 1 of 1 body text long enough to read\nTFL{n:07d}" for n in range(1, 14)]
        report = recipe.check_bates([document(text) for text in texts], volume, RULES)
        assert report["continuous"], report
        assert report["claimed_range"] == ["TFL0000001", "TFL0000013"]
        for bad in (",TFL000001,", ",TF0000001,", ",TFL00000001,"):
            (folder / "matter_productions.csv").write_text(
                log.replace(",TFL0000001,", bad), encoding="utf-8"
            )
            error = raised(lambda: MatterSystem(folder).find_volume("TFL-VOL001"))
            assert isinstance(error, ValueError), bad
            assert "not the prefix TFL" in str(error), bad
    print("claimed range: read once, a dash allowed, a bad label stops the lookup ok")


def check_field_call_options() -> None:
    """Each gliner2 field call asks SIE to fail an over-long window, not to cut it short."""
    seen: list[dict[str, Any]] = []

    class Recording(StandIn):
        def extract(self, model: str, item: Any, **options: Any) -> Any:
            if model == RULES["models"]["fields"]:
                seen.append(options)
            return super().extract(model, item, **options)

    calls = recipe.Calls()
    doc = document("From: A <a@x.example>\nSubject: hi\nbody\nTFL0000001")
    recipe.extract_fields(cast(SIEClient, Recording(pages={})), calls, doc, RULES)
    assert seen
    assert all(o.get("options") == {"overflow_policy": "error"} for o in seen), seen
    print("field calls: overflow_policy error ok")


if __name__ == "__main__":
    check_examples_volume()
    check_statuses()
    check_document_breaks()
    check_bates()
    check_fields()
    check_privilege()
    check_adversarial()
    check_sie_failure()
    check_outputs()
    check_committed_data()
    check_claimed_range()
    check_field_call_options()
    print("all checks passed")
