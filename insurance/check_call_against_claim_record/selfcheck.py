"""Check the recipe's reading, comparison and worklist rules offline.

The checks use hand-written transcript lines, the committed data/ (the claims seed and the
intake requirements) and hand-written stand-in model replies. Nothing here calls SIE or replays
a recorded response. Run it with `uv run --frozen python selfcheck.py`; it exits non-zero if a
check fails.
"""

from __future__ import annotations

import contextlib
import csv
import io
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from claims_system import COLUMNS, FOUND, NOT_FOUND, SEVERAL, ClaimsSystem, SavedClaim
from sie_sdk import SIEClient
from sie_sdk.client.errors import (
    InputTooLongError,
    InsufficientCreditsError,
    ModelLoadFailedError,
    ModelLoadingError,
    RateLimitError,
    RequestError,
    ServerError,
    SIEConnectionError,
    SIEError,
)

import recipe

RULES = recipe.load_rules()
REQS = recipe.load_requirements(RULES)
CLAIMS = ClaimsSystem()
INJURY_WORDS = REQS["urgent_triggers"][recipe.INJURY]["phrases"]
URGENT_WORDS = REQS["urgent_triggers"][recipe.URGENT]["phrases"]
CHECKS: list[tuple[str, Callable[[], None]]] = []


def check(name: str) -> Callable[[Callable[[], None]], Callable[[], None]]:
    def register(fn: Callable[[], None]) -> Callable[[], None]:
        CHECKS.append((name, fn))
        return fn

    return register


def lines(*texts: str) -> list[recipe.Line]:
    return [recipe.Line(n, 2.0 * n, text) for n, text in enumerate(texts, start=1)]


def cand(name: str, value: str, window: list[recipe.Line]) -> recipe.Candidate:
    return recipe.Candidate(name, value, tuple(window))


def saved(name: str = "fnol-1021-quintero.wav") -> SavedClaim:
    found = CLAIMS.find_claim(name).claim
    assert found is not None
    return found


def compare(name: str, saved_value: str, *heard: str) -> recipe.FieldCheck:
    window = lines(*heard) if heard else []
    found = [cand(name, value, window) for value in heard]
    return recipe.compare_field(name, saved_value, found, "Q?", RULES)


# A hand-written call: the Achterberg script as Parakeet would return it.
ACHTERBERG = [
    "Thank you for calling Harborline Mutual Claims.",
    "Do you have your policy number?",
    "Not with me.",
    "When did this happen?",
    "Wednesday afternoon, October 21.",
    "And the address of the property?",
    "87 Maple Street, Unit 2, Oakdale.",
    "What happened?",
    "Flooding, the storm drain backed up and a foot of water came into the basement.",
    "Was anyone injured?",
    "No one was injured.",
    "Thank you, an adjuster will call you back within two business days.",
]
ACHTERBERG_FIELDS = {
    "date_of_loss": "Wednesday afternoon, October 21",
    "loss_location": "87 Maple Street, Unit 2, Oakdale",
    "cause_of_loss": "Flooding",
    "injuries": "No one was injured",
}


class FakeClient:
    """Stands in for SIEClient: hand-written replies, or an error, per item id prefix."""

    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.seen: list[str] = []

    def extract(self, model: str, item: Any, **_: Any) -> Any:
        self.seen.append(item["id"])
        for prefix, reply in self.replies.items():
            if item["id"].startswith(prefix):
                if isinstance(reply, BaseException):
                    raise reply
                return reply
        raise AssertionError(f"no stand-in reply for {item['id']}")


def segments(texts: list[str]) -> dict[str, Any]:
    return {"data": {"segments": [{"start": 2.0 * n, "text": t} for n, t in enumerate(texts)]}}


def run_offline(replies: dict[str, Any], wavs: list[str]) -> list[recipe.CallCheck]:
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for name in wavs:
            path = Path(tmp) / name
            path.write_bytes(b"RIFF")
            paths.append(path)
        client = cast(SIEClient, FakeClient(replies))
        with contextlib.redirect_stdout(io.StringIO()):
            checks, _ = recipe.run(client, RULES, REQS, paths)
    return checks


def stops(replies: dict[str, Any], wavs: list[str]) -> str:
    try:
        run_offline(replies, wavs)
    except recipe.SIEStopped as error:
        return str(error)
    raise AssertionError("the run did not stop")


# --- Claims system lookup ---------------------------------------------------------------------


@check("every committed recording has exactly one saved claim")
def _() -> None:
    for wav in sorted(recipe.DATA_DIR.glob("*.wav")):
        assert CLAIMS.find_claim(wav.name).match == FOUND, wav.name
    assert CLAIMS.find_claim(" FNOL-1021-QUINTERO.wav ").match == FOUND


@check("an unknown recording is not_found; two saved claims for one recording are several")
def _() -> None:
    assert CLAIMS.find_claim("fnol-1021-nobody.wav").match == NOT_FOUND
    # A neighboring recording name is a different recording.
    assert CLAIMS.find_claim("fnol-1021-quintero2.wav").match == NOT_FOUND
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "claims.csv"
        row = dict.fromkeys(COLUMNS, "x") | {"recording": "a.wav"}
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows([row | {"claim_number": "C1"}, row | {"claim_number": "C2"}])
        found = ClaimsSystem(path).find_claim("a.wav")
    assert found.match == SEVERAL
    assert found.claim is None
    assert found.claim_numbers == ("C1", "C2")


@check("a recording with no saved claim goes to review and is never sent to SIE")
def _() -> None:
    client = FakeClient({})
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fnol-1021-nobody.wav"
        path.write_bytes(b"RIFF")
        result = recipe.process_call(
            recipe.Calls(), cast(SIEClient, client), path, CLAIMS, REQS, RULES
        )
    assert result.status == recipe.NEEDS_REVIEW
    assert client.seen == []
    assert "0 saved claims" in result.review[0]


@check("intake requirements naming a field the recipe can't compare stop at the start")
def _() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "bad.yaml"
        bad.write_text(
            "required_fields:\n  vehicle_vin: {label: vin, callback_question: q}\n"
            "urgent_triggers:\n  injury_reported: {phrases: [hurt]}\n"
            "  urgent_assistance: {phrases: [unsafe]}\n",
            encoding="utf-8",
        )
        message = ""
        try:
            recipe.load_requirements(RULES, bad)
        except ValueError as error:
            message = str(error)
    assert "vehicle_vin" in message, "an unknown required field was accepted"


# --- Injuries and urgent help -----------------------------------------------------------------


def injury(text: str) -> str | None:
    return recipe.read_trigger(text, INJURY_WORDS, RULES)


def urgent(text: str) -> str | None:
    return recipe.read_trigger(text, URGENT_WORDS, RULES)


@check("injury sentences: reported, denied, ambiguous, questions skipped")
def _() -> None:
    assert injury("No, nobody was hurt.") == recipe.DENIED
    assert injury("No one was injured.") == recipe.DENIED
    assert injury("My husband cut his arm and he needed stitches.") == recipe.REPORTED
    assert injury("Was anyone hurt?") is None
    assert injury("Is everyone okay?") is None
    assert injury("The roof is gone.") is None


@check("a negation about the ambulance doesn't cancel the injury (planted Okafor case)")
def _() -> None:
    assert injury("He was hurt but he didn't need an ambulance.") == recipe.AMBIGUOUS
    assert injury("No one was hurt, but my husband has a burn on his hand.") == recipe.AMBIGUOUS


@check("a hedged or partial denial implies someone was hurt")
def _() -> None:
    assert injury("He wasn't badly hurt.") == recipe.AMBIGUOUS
    assert injury("Nobody else was hurt.") == recipe.AMBIGUOUS
    assert injury("Nobody else, but my son was hurt.") == recipe.REPORTED


@check("urgent help: still coming in, staying elsewhere; a negation inside a trigger counts")
def _() -> None:
    line = "Rain is still coming through the bedroom ceiling, and we are staying with family."
    assert urgent(line) == recipe.REPORTED
    assert urgent("We have no power since last night.") == recipe.REPORTED
    assert urgent("It's not safe to go upstairs.") == recipe.REPORTED
    # Past tense, no trigger: rain that came in once is not ongoing damage (Quintero).
    assert urgent("Hail broke two skylights, rain came into the kitchen.") is None
    assert urgent("Is water still coming in?") is None


@check("the injury flag: ambiguous sentence, a report and a denial, model-only, model on a denial")
def _() -> None:
    call = lines("Was anyone hurt?", "He was hurt but he didn't need an ambulance.")
    result = recipe.CallCheck("x.wav")
    assert recipe.decide_trigger(recipe.INJURY, call, [], REQS, RULES, result) == "reported"
    assert result.flags[0].ambiguous
    assert "Ambiguous" in result.flags[0].detail

    call = lines("No, nobody was hurt.", "Actually my son cut his hand.")
    result = recipe.CallCheck("x.wav")
    recipe.decide_trigger(recipe.INJURY, call, [], REQS, RULES, result)
    assert result.flags[0].ambiguous
    assert len(result.flags[0].evidence) == 2

    # The model reads an injury no sentence confirms: the flag still goes up, marked.
    call = lines("Was anyone hurt?", "Well, he slipped on the wet stairs.")
    result = recipe.CallCheck("x.wav")
    model = cand("injuries", "slipped on the wet stairs", call)
    assert recipe.decide_trigger(recipe.INJURY, call, [model], REQS, RULES, result) == "reported"
    assert result.flags[0].ambiguous

    # The model's span is the denial itself: dropped, and the call says "No".
    call = lines("Was anyone hurt?", "No, nobody was hurt.")
    result = recipe.CallCheck("x.wav")
    model = cand("injuries", "nobody", call)
    assert recipe.decide_trigger(recipe.INJURY, call, [model], REQS, RULES, result) == "denied"
    assert not result.flags


# --- Comparing fields with the saved claim ----------------------------------------------------


@check("location: transposed house number and a dropped unit differ (planted Achterberg)")
def _() -> None:
    found = compare("loss_location", "78 Maple Street, Oakdale", "87 Maple Street, Unit 2, Oakdale")
    assert found.status == recipe.DIFFERS
    found = compare("loss_location", "87 Maple Street, Oakdale", "87 Maple Street, Unit 2, Oakdale")
    assert found.status == recipe.DIFFERS  # the claim lost the unit
    # A neighboring street type is a different address.
    assert compare("loss_location", "455 Birch Lane", "455 Birch Avenue").status == "differs"


@check("location: formatting is not a difference; a part the call leaves out is not either")
def _() -> None:
    same = "In the claim; the call agrees"
    found = compare(
        "loss_location", "214 Alder Creek Road, Millbrook", "At my house, 214 Alder Creek Rd"
    )
    assert found.status == recipe.IN_CLAIM
    assert found.reason == same
    found = compare("loss_location", "87 Maple St, #2, Oakdale", "87 Maple Street, unit 2")
    assert found.status == recipe.IN_CLAIM
    found = compare("loss_location", "62 Harbor View Road, Millbrook", "62 Harborview Road")
    assert found.status == recipe.IN_CLAIM
    # Another town is a different address, even with the same street.
    found = compare("loss_location", "87 Maple Street, Oakdale", "87 Maple Street, Fenwick")
    assert found.status == recipe.DIFFERS
    # No street number in the call's value: can't compare, so a person checks.
    assert compare("loss_location", "62 Harbor View Road", "the house").status == "differs"


@check("date: spoken dates compare as dates; times and glued ordinals are read right")
def _() -> None:
    assert recipe.same_date("2026-10-20", "Tuesday night, the 20th of October, around 9 o'clock")
    assert recipe.same_date("2026-10-21", "Wednesday afternoon, October 21st")
    assert recipe.same_date("2026-10-21", "10/21")
    assert recipe.same_date("2026-10-20", "October 21st") is False
    assert recipe.same_date("2026-10-20", "Tuesday the 21st") is False


@check("date: relative words and two dates in one span can't be compared, so they differ")
def _() -> None:
    assert recipe.same_date("2026-10-20", "yesterday") is None
    assert recipe.same_date("2026-10-20", "the 20th, or maybe the 21st of October") is None
    assert recipe.same_date("2026-10-20", "Monday or Tuesday") is None
    # A weekday alone could be any week, so it can't be compared either way.
    assert recipe.same_date("2026-10-20", "Tuesday night") is None
    assert recipe.same_date("2026-10-20", "Wednesday night") is None
    assert compare("date_of_loss", "2026-10-20", "Tuesday night").status == recipe.DIFFERS
    assert compare("date_of_loss", "2026-10-20", "yesterday evening").status == recipe.DIFFERS


@check("policy: in the claim and not said is in_claim, never a callback (planted Achterberg)")
def _() -> None:
    found = compare("policy_number", "HM30958821")
    assert found.status == recipe.IN_CLAIM
    assert not found.question
    assert compare("policy_number", "HM44712093", "H M 4471 2093").status == recipe.IN_CLAIM
    # A near-identical number (two digits swapped) is a different policy.
    assert compare("policy_number", "HM44712093", "HM44712039").status == recipe.DIFFERS


@check("policy: a number heard but failing the pattern is kept and sent to the reviewer")
def _() -> None:
    window = lines("It's HM4471209 and HM44712093.")
    value = cand("policy_number", "HM4471209 and HM44712093", window)
    assert recipe.usable(value, RULES, recipe.CallCheck("x.wav"))
    found = recipe.compare_field("policy_number", "HM44712093", [value], "Q?", RULES)
    assert found.status == recipe.DIFFERS
    assert "pattern" in found.reason
    # Not a number at all: dropped with a note.
    words = cand("policy_number", "policy number", lines("Do you have your policy number?"))
    result = recipe.CallCheck("x.wav")
    assert not recipe.usable(words, RULES, result)
    assert result.notes


@check("cause: compared by peril; a peril the claim lacks differs; no peril can't compare")
def _() -> None:
    assert compare("cause_of_loss", "Hail and wind", "Hail").status == recipe.IN_CLAIM
    assert compare("cause_of_loss", "Fallen tree", "oak tree").status == recipe.IN_CLAIM
    assert compare("cause_of_loss", "Hail", "hail and a fire").status == recipe.DIFFERS
    assert compare("cause_of_loss", "Wind", "the storm").status == recipe.DIFFERS


@check("field statuses: blank and said fills, blank and unsaid is a callback, two values differ")
def _() -> None:
    found = compare("date_of_loss", "", "On Tuesday, October 20th")
    assert found.status == recipe.FILL_FROM_CALL
    assert found.evidence[0]["at"] == "00:02"
    found = compare("date_of_loss", "")
    assert found.status == recipe.MISSING
    assert found.question == "Q?"
    # A caller who corrects the house number: both values kept, the reviewer picks.
    found = compare("loss_location", "87 Maple Street", "87 Maple Street", "78 Maple Street")
    assert found.status == recipe.DIFFERS
    assert len(found.heard) == 2


@check("injury field: saved No against a reported injury differs (planted Vellanki)")
def _() -> None:
    flag = recipe.Flag(recipe.INJURY, "d", [{"line": 1, "at": "00:26", "text": "t"}])
    found = recipe.compare_injuries("No", recipe.REPORTED, flag, "Q?")
    assert found.status == recipe.DIFFERS
    assert found.heard == ["Yes"]
    assert recipe.compare_injuries("No", recipe.DENIED, None, "Q?").status == recipe.IN_CLAIM
    assert recipe.compare_injuries("", recipe.DENIED, None, "Q?").status == "fill_from_call"
    assert recipe.compare_injuries("", None, None, "Q?").status == recipe.MISSING
    assert recipe.compare_injuries("Cut arm", recipe.REPORTED, flag, "Q?").status == "in_claim"
    for typed, read in [
        ("None reported", "No"),
        ("No injuries", "No"),
        ("unknown", ""),
        ("Not injured", "No"),
        ("Pending", ""),
        ("Unknown", ""),
        ("Not sure", ""),
    ]:
        assert recipe.saved_injuries(typed) == read, typed
    assert recipe.saved_injuries("Husband cut arm") == "Yes"


@check("adversarial readings: two addresses, another year, a denied trigger, a lost '?'")
def _() -> None:
    two = compare("loss_location", "87 Maple Street", "87 Maple Street or 78 Maple Street")
    assert two.status == recipe.DIFFERS
    assert compare("date_of_loss", "2026-10-20", "October 20th 2025").status == recipe.DIFFERS
    assert compare("cause_of_loss", "Hail", "hail, no wind").status == recipe.DIFFERS
    assert urgent("We're not staying with family, the house is fine.") == recipe.DENIED
    assert urgent("The water stopped coming in.") is None
    # Parakeet drops the question mark: the agent's question reads as a report. The flag errs
    # towards a second look, never away from one.
    assert injury("Was anyone hurt.") == recipe.REPORTED


@check("values differing by a digit stay apart; a value inside a longer one is the same value")
def _() -> None:
    def heard(name: str, *values: str) -> list[str]:
        window = lines(*values)
        found = [cand(name, value, window) for value in values]
        return [c.value for c in recipe.heard_values(name, found, RULES)]

    assert heard("date_of_loss", "October 20", "October 2") == ["October 20", "October 2"]
    assert heard("policy_number", "HM44712093", "HM4471209") == ["HM44712093", "HM4471209"]
    two = heard("loss_location", "214 Alder Creek Road", "14 Alder Creek Road")
    assert two == ["214 Alder Creek Road", "14 Alder Creek Road"], two
    one = heard("loss_location", "214 Alder Creek Road", "Alder Creek Road")
    assert one == ["214 Alder Creek Road"], one


@check("a denial span before a real injury span: the denial is dropped, the injury counts")
def _() -> None:
    call = lines("Nobody was hurt in the kitchen.", "Well, he slipped on the wet stairs.")
    denial = cand("injuries", "Nobody was hurt", call)
    real = cand("injuries", "slipped on the wet stairs", call)
    window = recipe.windows(call, 180, 3)
    result = recipe.build_check(
        call,
        [recipe.Candidate(c.field, c.value, window[0]) for c in (denial, real)],
        saved("fnol-1021-quintero.wav"),
        REQS,
        RULES,
        recipe.CallCheck("x.wav"),
    )
    assert [flag.name for flag in result.flags] == [recipe.INJURY], result.flags
    assert result.fields["injuries"].heard == ["Yes"]


@check("a value not said in its own window is dropped; a short transcript goes to review")
def _() -> None:
    first, second = lines("My policy is HM44712093."), lines("It happened on October 20.")
    assert recipe.usable(cand("policy_number", "HM44712093", first), RULES, recipe.CallCheck("x"))
    assert not recipe.usable(
        cand("policy_number", "HM44712093", second), RULES, recipe.CallCheck("x")
    )
    short = lines("Hello.", "Hail on the roof.")
    result = recipe.build_check(short, [], saved(), REQS, RULES, recipe.CallCheck("x.wav"))
    assert result.status == recipe.NEEDS_REVIEW
    assert "only 5 words" in result.review[0], result.review


@check("every GLiNER2 window call asks SIE to fail on overflow, never to cut the text")
def _() -> None:
    seen: list[Any] = []

    class Recording(FakeClient):
        def extract(self, model: str, item: Any, **kwargs: Any) -> Any:
            if item["id"].startswith("extract/"):
                seen.append(kwargs.get("options"))
            return super().extract(model, item, **kwargs)

    client = cast(SIEClient, Recording({"extract/": {"data": ACHTERBERG_FIELDS}}))
    recipe.extract_candidates(
        recipe.Calls(), client, "x", lines(*ACHTERBERG), RULES, recipe.CallCheck("x.wav")
    )
    assert seen
    assert all(o == {"overflow_policy": "error"} for o in seen), seen


# --- Whole calls with stand-in replies, and the worklist --------------------------------------


@check("Achterberg end to end: location differs, policy in the claim, no callback")
def _() -> None:
    replies = {
        "transcribe/": segments(ACHTERBERG),
        "extract/": {"data": ACHTERBERG_FIELDS},
    }
    (result,) = run_offline(replies, ["fnol-1021-achterberg.wav"])
    statuses = {name: field.status for name, field in result.fields.items()}
    assert statuses == {
        "date_of_loss": "in_claim",
        "loss_location": "differs",
        "cause_of_loss": "in_claim",
        "injuries": "in_claim",
        "policy_number": "in_claim",
    }, statuses
    assert result.status == recipe.DIFFERS
    assert not result.flags


@check("statuses name findings; flagged claims sort first; no decision words")
def _() -> None:
    assert set(recipe.STATUS_ORDER) == {
        "needs_review",
        "callback_needed",
        "differs",
        "fill_from_call",
        "checks_passed",
    }
    plain = recipe.CallCheck("a.wav", saved("fnol-1021-quintero.wav"))
    plain.fields["x"] = recipe.FieldCheck("x", recipe.MISSING, "")
    flagged = recipe.CallCheck("b.wav", saved("fnol-1021-delacroix.wav"))
    flagged.flags.append(recipe.Flag(recipe.URGENT, "still coming in"))
    rows = recipe.worklist_rows([plain, flagged])
    assert [row["recording"] for row in rows] == ["b.wav", "a.wav"]
    assert rows[1]["check_status"] == "callback_needed"
    assert rows[0]["check_status"] == "checks_passed"
    assert rows[0]["flags"] == recipe.URGENT
    for word in ("ready", "priority", "approved"):
        assert word not in set(recipe.STATUS_ORDER), word


@check("CSV cells that a spreadsheet would run as a formula are quoted")
def _() -> None:
    assert recipe.csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert recipe.csv_safe("-15 C") == "'-15 C"
    assert recipe.csv_safe("in_claim") == "in_claim"


# --- SIE call failures ------------------------------------------------------------------------


TRANSIENT: list[SIEError] = [
    SIEConnectionError("connection refused"),
    ServerError("unavailable", status_code=503),
    RateLimitError("slow down"),
    InputTooLongError("too long"),
    ModelLoadingError("loading"),
    ModelLoadFailedError("try later", permanent=False),
]
FATAL: list[SIEError] = [
    RequestError("bad key", status_code=401),
    RequestError("no such model", status_code=404),
    InsufficientCreditsError("no credits"),
    ModelLoadFailedError("gated", permanent=True),
]


@check("is_fatal: refusals stop the run, transient errors don't")
def _() -> None:
    assert not any(recipe.is_fatal(error) for error in TRANSIENT)
    assert all(recipe.is_fatal(error) for error in FATAL)


@check("a transient transcription failure sends that call to review; the run goes on")
def _() -> None:
    replies = {
        "transcribe/fnol-1021-okafor": ServerError("busy", status_code=503),
        "transcribe/": segments(ACHTERBERG),
        "extract/": {"data": ACHTERBERG_FIELDS},
    }
    first, second = run_offline(replies, ["fnol-1021-okafor.wav", "fnol-1021-achterberg.wav"])
    assert first.status == recipe.NEEDS_REVIEW
    assert "not transcribed" in first.review[0]
    assert "HTTP 503" in first.review[0]
    assert second.status == recipe.DIFFERS


@check("a failed window sends the call to review, even when other fields were filled")
def _() -> None:
    long_call = ACHTERBERG * 4  # several windows
    replies = {
        "transcribe/": segments(long_call),
        "extract/fnol-1021-achterberg/w2": RateLimitError("slow down"),
        "extract/": {"data": ACHTERBERG_FIELDS},
    }
    (result,) = run_offline(replies, ["fnol-1021-achterberg.wav"])
    assert result.status == recipe.NEEDS_REVIEW
    assert "window 2" in result.review[0]
    assert "correction" in result.review[0]


@check("a reply without its key is a failed call; an empty data dict is 'nothing found'")
def _() -> None:
    replies = {"transcribe/fnol-1021-okafor": {"text": "hi"}, "transcribe/": segments(ACHTERBERG)}
    replies["extract/"] = {"data": ACHTERBERG_FIELDS}
    first, _ = run_offline(replies, ["fnol-1021-okafor.wav", "fnol-1021-achterberg.wav"])
    assert first.status == recipe.NEEDS_REVIEW
    assert "without 'data'" in first.review[0]
    # data without segments is a failed call too
    replies["transcribe/fnol-1021-okafor"] = {"data": {"text": "hi"}}
    first, _ = run_offline(replies, ["fnol-1021-okafor.wav", "fnol-1021-achterberg.wav"])
    assert first.status == recipe.NEEDS_REVIEW
    assert "segments" in first.review[0]
    # The extractor replies without data: the window failed.
    (result,) = run_offline(
        {"transcribe/": segments(ACHTERBERG), "extract/": {"oops": 1}},
        ["fnol-1021-achterberg.wav"],
    )
    assert result.status == recipe.NEEDS_REVIEW
    assert "without 'data'" in result.review[0]
    # A real empty answer: every field the claim has stays in_claim, nothing goes to review.
    (result,) = run_offline(
        {"transcribe/": segments(ACHTERBERG), "extract/": {"data": {}}},
        ["fnol-1021-achterberg.wav"],
    )
    assert result.status == recipe.CHECKS_PASSED
    assert not result.review


@check("SIE Cloud's empty-result 500 reads as nothing found, with a note, not a failure")
def _() -> None:
    empty = ServerError(recipe.EMPTY_RESULT_ERROR, status_code=500)
    (result,) = run_offline(
        {"transcribe/": segments(ACHTERBERG), "extract/": empty}, ["fnol-1021-vellanki.wav"]
    )
    assert not result.review
    assert "no fields found" in result.notes[0]
    # The Vellanki claim has no date of loss, so an empty read is a callback, never a pass.
    assert result.fields["date_of_loss"].status == recipe.MISSING


@check("a refusal stops the run with SIE's message; every call failing stops it too")
def _() -> None:
    # The first call is refused; the second would succeed, so only the refusal stops the run.
    message = stops(
        {
            "transcribe/fnol-1021-quintero": RequestError("invalid API key", status_code=401),
            "transcribe/": segments(ACHTERBERG),
            "extract/": {"data": ACHTERBERG_FIELDS},
        },
        ["fnol-1021-quintero.wav", "fnol-1021-achterberg.wav"],
    )
    assert message.startswith("SIE refused the call"), message
    assert "invalid API key" in message
    assert "No outputs were written" in message
    message = stops(
        {"transcribe/": SIEConnectionError("refused")},
        ["fnol-1021-quintero.wav", "fnol-1021-okafor.wav"],
    )
    assert message.startswith("Every SIE call failed (2 of 2")
    assert "SIE_BASE_URL" in message
    message = stops({"transcribe/": ModelLoadingError("loading")}, ["fnol-1021-quintero.wav"])
    assert "run again in a few minutes" in message


@check("main catches only SIEStopped")
def _() -> None:
    message = ""
    try:
        recipe.stop_on_refusal(lambda: (_ for _ in ()).throw(recipe.SIEStopped("no")))
    except SystemExit as error:
        message = str(error)
    assert message == "Stopped: no", "SIEStopped did not become SystemExit"
    with contextlib.suppress(ZeroDivisionError):
        recipe.stop_on_refusal(lambda: 1 / 0)
        raise AssertionError("another error was caught")


def main() -> None:
    failed = 0
    for name, fn in CHECKS:
        try:
            fn()
        except AssertionError as error:
            failed += 1
            print(f"FAIL  {name}: {error}")
        else:
            print(f"ok    {name}")
    print(f"\n{len(CHECKS) - failed} of {len(CHECKS)} checks passed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
