"""Check the recipe's field rules, name handling, overlap rule, sweep and SIE error rule offline.

The checks use hand-written strings, the committed data/ and hand-written stand-in model
replies. Nothing here calls SIE or replays a recorded response. Run it with
`uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import csv
import functools
import io
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import log_platform
import support_desk
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
CASE = recipe.load_case()
PII = RULES["models"]["pii"]
KNOWN = recipe.known_customers(CASE)

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
        self.models: list[str] = []

    def extract(self, model: str, item: Any, **_: Any) -> Any:
        self.models.append(model)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return reply(str(item["text"]))
        return reply


def doc(name: str, text: str) -> recipe.Document:
    return recipe.Document(name, text)


def clean(
    *texts: str,
    replies: list[Reply] | None = None,
    known: list[tuple[str, str]] | None = None,
    rules: dict[str, Any] | None = None,
) -> list[recipe.Document]:
    """Run the recipe on hand-written files (one stand-in reply per file, nothing found by
    default) and return the documents."""
    documents = [doc(f"f{i}.log", text) for i, text in enumerate(texts, start=1)]
    client = StandIn(*(replies or [NOTHING] * len(texts)))
    with contextlib.redirect_stdout(io.StringIO()):
        recipe.run(cast(SIEClient, client), rules or RULES, documents, known or [])
    assert not client.replies, f"{len(client.replies)} replies left unused"
    return documents


def bundle_run(*replies: Reply) -> recipe.Result:
    """Run the recipe on the committed bundle with stand-in replies, one per file."""
    client = StandIn(*replies)
    documents = recipe.build_bundle(CASE)
    with contextlib.redirect_stdout(io.StringIO()):
        result = recipe.run(cast(SIEClient, client), RULES, documents, KNOWN)
    assert not client.replies, f"{len(client.replies)} replies left unused"
    return result


def check_lookups() -> None:
    # The log platform returns only the asked-for service inside the window, oldest first.
    events = log_platform.search("checkout-api", CASE.start, CASE.end)
    assert events
    assert all(e.service == "checkout-api" for e in events)
    assert all(CASE.start <= e.timestamp < CASE.end for e in events)
    assert [e.timestamp for e in events] == sorted(e.timestamp for e in events)
    every = log_platform.search("checkout-api", "2026-10-27T00:00:00Z", "2026-10-28T00:00:00Z")
    assert len(every) == len(events) + 1, "the 08:41 line outside the window should be dropped"
    assert not log_platform.search("search-api", "2026-10-28T00:00:00Z", "2026-10-29T00:00:00Z")
    # The support desk returns the incident's ticket only, exact incident ID.
    tickets = support_desk.tickets_for_incident("INC-2207")
    assert [t.ticket_id for t in tickets] == ["SUP-48213"], tickets
    assert not support_desk.tickets_for_incident("INC-220")
    assert KNOWN == [
        ("Delphine Marchetti", "person"),
        ("delphine.m@quarrymail.example", "email address"),
        ("Brightwater Ceramics", "organization"),
    ], KNOWN
    # The bundle: one export per service, then the notes; the support ticket is not in it.
    names = [d.name for d in recipe.build_bundle(CASE)]
    assert names == ["checkout-api.log", "payment-worker.log", "oncall-notes.txt"], names
    for bad in ("../recipe.py", "missing.txt"):
        try:
            recipe.build_bundle(recipe.Case("INC-2207", "cardvault", (), "", "", bad))
        except ValueError:
            continue
        raise AssertionError(f"notes {bad!r} should be refused")
    empty = recipe.Case("INC-2207", "x", ("no-such-service",), "a", "b", "x")
    caught = raised(lambda: recipe.build_bundle(empty))
    assert type(caught) is ValueError, caught
    assert "no no-such-service log lines" in str(caught)
    print("lookups: log platform by service and window, support desk by incident, bundle ok")


def check_field_rules() -> None:
    # Host IPs are kept, client IPs redacted even in a private range, and an IP a host field
    # named is kept in the free-text notes too.
    log = "user_id=u-48213 client_ip=10.20.3.4 host=10.0.4.17 upstream=10.0.7.31:8443\n"
    notes = "pods on 10.0.7.31 restarting; customer was on 10.20.3.4 and 198.51.100.23:51544\n"
    first, second = clean(log, notes)
    assert first.redacted == (
        "user_id=[USER_ID_1] client_ip=[IP_1] host=10.0.4.17 upstream=10.0.7.31:8443\n"
    ), first.redacted
    assert (
        second.redacted == "pods on 10.0.7.31 restarting; customer was on [IP_1] and [IP_2]:51544\n"
    )
    assert first.status == second.status == recipe.READY
    # The same IP logged as one of our hosts and as a client: the rules can't decide it.
    (both,) = clean("host=10.0.4.17 ok\nclient_ip=10.0.4.17 failed\n")
    assert both.status == recipe.BLOCKED, both.errors
    assert "line 2: a client ip that another file logs as one of our own" in both.errors[0]
    # Near misses: a build number fails the octet check, an order ID fails Luhn far from "card".
    (near,) = clean("build=4.18.302.7 order_id=7300018842915562 status=504\n")
    assert near.redacted == "build=4.18.302.7 order_id=7300018842915562 status=504\n"
    assert near.status == recipe.READY
    # A card that fails Luhn right after "card" is still masked, and a person checks it.
    (card,) = clean('"card": "4111 1111 1111 1112", "amount": 10\n')
    assert '"card": "XXXX XXXX XXXX 1112"' in card.redacted, card.redacted
    assert card.status == recipe.REVIEW
    assert "credit card number next to 'card' fails the luhn check" in card.reviews[0]
    # A card that passes Luhn is masked and the file goes to review to fix the logging.
    (good,) = clean("pan=4111-1111-1111-1111\n")
    assert good.redacted == "pan=XXXX-XXXX-XXXX-1111\n"
    assert good.status == recipe.REVIEW
    # A connection string whose user is an allowlisted service: only the password goes; the
    # allowlisted name next to it doesn't hide the password from the rule.
    url = "postgres://payment-worker:Lg8vQ2mZt5Rx@ledger-db:5432/ledger"
    (conn,) = clean(f"DATABASE_URL={url}\n")
    assert conn.redacted == (
        "DATABASE_URL=postgres://payment-worker:[PASSWORD_1]@ledger-db:5432/ledger\n"
    ), conn.redacted
    assert "rotate it" in conn.reviews[0]
    # A session cookie: the value goes, the cookie's name stays; a bearer token the same.
    (headers,) = clean(
        '{"Authorization": "Bearer abcdefghijklmnop1234", "Cookie": '
        '"session_id=k2Vq8sLx41TzRb7mWc0nYe5H; lang=en"}\n'
    )
    assert headers.redacted == (
        '{"Authorization": "Bearer [TOKEN_1]", "Cookie": "session_id=[SESSION_1]; lang=en"}\n'
    ), headers.redacted
    # A client field the rules can't read (IPv6 here) blocks the file instead of passing it.
    (ipv6,) = clean("client_ip=2001:db8::17 status=504\n")
    assert ipv6.status == recipe.BLOCKED
    assert "client address in a format" in ipv6.errors[0]
    # Customer IDs of any shape are redacted, not only UUIDs.
    (ids,) = clean("customer_id=48213 tenant_id=acme-eu\n")
    assert ids.redacted == "customer_id=[USER_ID_1] tenant_id=[USER_ID_2]\n", ids.redacted
    # A UUID no field names blocks the file; request and deploy IDs stay readable.
    (uuid,) = clean(
        "request_id=9f1c2a7e-4b3d-4e8a-9c21-5d7f0b6e3a14 "
        "charge 5b9d2c14-8e3f-4a71-b0d6-2c7e9f1a4b38\n"
    )
    assert uuid.status == recipe.BLOCKED
    assert "9f1c2a7e-4b3d-4e8a-9c21-5d7f0b6e3a14" in uuid.redacted
    print("field rules: host vs client IPs, near misses, cards, passwords, sessions, UUIDs ok")


def check_overlap() -> None:
    # The model tags a whole reset URL; the email rule takes its part and the rest of the URL,
    # token included, is still redacted under one placeholder.
    url = "https://shop.corvane.example/account/reset?email=t.okonkwo@fernmail.example&token=Rk7pQ2vX9mL4"
    (link,) = clean(f"link={url}\n", replies=[tags((url, "url"))])
    assert link.redacted == "link=[URL_1][EMAIL_1][URL_1]\n", link.redacted
    assert "Rk7pQ2vX9mL4" not in link.redacted
    # When the model stops before the token, the sweep blocks the file instead.
    short = url.split("&")[0]
    (cut,) = clean(f"link={url}\n", replies=[tags((short, "url"))])
    assert cut.status == recipe.BLOCKED
    assert "url_secret still readable" in cut.leaks[0], cut.leaks
    # A model span over a kept ID or a field name leaves the field name readable.
    text = (
        "request_id=9f1c2a7e-4b3d-4e8a-9c21-5d7f0b6e3a14 "
        "Authorization: Bearer abcdefghijklmnop1234\n"
    )
    (kept,) = clean(
        text,
        replies=[
            tags(
                ("request_id=9f1c2a7e-4b3d-4e8a-9c21-5d7f0b6e3a14", "username"),
                ("Bearer abcdefghijklmnop1234", "api key"),
            )
        ],
    )
    assert kept.redacted == (
        "request_id=9f1c2a7e-4b3d-4e8a-9c21-5d7f0b6e3a14 Authorization: Bearer [TOKEN_1]\n"
    ), kept.redacted
    assert not recipe.worth_redacting("session_id=", RULES)
    assert recipe.worth_redacting("&token=Rk7pQ2vX", RULES)
    print("overlap: the rule decides its part, the rest of the model span is still redacted ok")


def check_names() -> None:
    # A known customer from the support desk is redacted with no model help, as written in
    # full, in capitals, and as the first name or surname alone; staff stay readable.
    text = (
        "Felix says Brightwater Ceramics (Delphine Marchetti) reported it.\n"
        "MARCHETTI, DELPHINE called again. Felix let Delphine know. Imogen Tarrant on call.\n"
    )
    (notes,) = clean(text, known=KNOWN)
    assert notes.redacted == (
        "Felix says [ORG_1] ([PERSON_1]) reported it.\n"
        "[PERSON_1] called again. Felix let [PERSON_1] know. Imogen Tarrant on call.\n"
    ), notes.redacted
    assert notes.status == recipe.READY
    # The surname alone, possessive or lower case, is the same customer.
    (alone,) = clean("Marchetti's shop; delphine marchetti called\n", known=KNOWN)
    assert alone.redacted == "[PERSON_1]'s shop; [PERSON_1] called\n", alone.redacted
    # A name the model found only in lower case still matches its capitalized surname.
    found = tags(("tomas okonkwo", "person"))
    (lower,) = clean("tomas okonkwo wrote; Okonkwo's order failed\n", replies=[found])
    assert lower.redacted == "[PERSON_1] wrote; [PERSON_1]'s order failed\n", lower.redacted
    # A known customer wins over the allowlist: a requester called Felix Brandt makes every
    # "Felix" a name to redact, and a person checks who is meant.
    brandt = [("Felix Brandt", "person")]
    (clash,) = clean("Felix Brandt wrote in. Felix answered.\n", known=brandt)
    assert "Felix" not in clash.redacted, clash.redacted
    assert clash.status == recipe.REVIEW
    assert "also a known customer's; redacted, check who is meant" in clash.reviews[0]
    # The same name in another file of the bundle gets the same placeholder.
    first, second = clean("name=Delphine Marchetti\n", "Delphine called\n", known=KNOWN)
    assert first.redacted == "name=[PERSON_1]\n"
    assert second.redacted == "[PERSON_1] called\n"
    # A short name that fits two known people is redacted and sent to review.
    two = [("Delphine Marchetti", "person"), ("Delphine Okafor", "person")]
    (both,) = clean("Delphine Marchetti and Delphine Okafor. Delphine called.\n", known=two)
    assert both.status == recipe.REVIEW
    assert "a name that could be" in both.reviews[0], both.reviews
    print("names: known customers, name forms, allowlist collisions, ambiguity ok")


def check_sweep_and_report() -> None:
    # Anything secret-shaped still readable blocks the file.
    for leak, name in (
        ("token=eyJhbGciOiJIUzI1.eyJzdWIiOiJkZW1v.bm90LWEtcmVhbA", "jwt"),
        ("secret=sk_live_abcdef123456", "labelled_secret"),
        ("mail me at someone@else.example", "email"),
        ("sid 4111 1111 1111 1111", "card_number"),
        ("https://x.example/cb?code=Zq81mPx0", "url_secret"),
    ):
        hits = recipe.residual_sweep(leak + "\n", RULES)
        assert any(name in hit for hit in hits), (leak, hits)
    # A secret field with a name no rule lists, or a password in JSON, is caught by the sweep.
    for leak in ("stripe_key=sk_live_51Hx9QpLmN0 ok", '"password": "hunter2hunter2"'):
        (left,) = clean(leak + "\n")
        assert left.status == recipe.BLOCKED
        assert "labelled_secret" in left.leaks[0], left.leaks
    # The sweep reads one line at a time: a placeholder at a line's end is not a hit.
    assert not recipe.residual_sweep("api_key=[TOKEN_1]\n2026-10-27T09:16:41.736Z ok\n", RULES)
    # Placeholders and kept, allowlisted terms are not hits.
    assert not recipe.residual_sweep("api_key=[TOKEN_1] health check from 203.0.113.10\n", RULES)
    # Reasons give the line, never the value; a formula-looking cell is quoted.
    assert recipe.csv_safe({"a": "=HYPERLINK(1)", "b": 2}) == {"a": "'=HYPERLINK(1)", "b": 2}
    assert recipe.by_line(["line 9: b", "window 1 was not read", "line 2: a"]) == [
        "window 1 was not read",
        "line 2: a",
        "line 9: b",
    ]
    print("sweep and report: secrets still readable block the file; csv cells quoted ok")


def check_bundle() -> None:
    # The committed bundle with stand-in replies that find nothing: the rules alone give the
    # planted statuses, and no report or redaction row holds a value it replaced.
    # With no model help, the reset link's token is something no rule names: the sweep finds
    # it still readable and blocks the checkout log.
    alone = bundle_run(NOTHING, NOTHING, NOTHING).documents[0]
    assert alone.status == recipe.BLOCKED
    assert "line 7: url_secret still readable" in alone.leaks, alone.leaks
    # The model tags the token (as the live run did): the file goes to review for the bearer
    # token, like the planted case.
    token = tags(("Rk7pQ2vX9mL4sT8wZ1cB6nH3", "api key"))
    result = bundle_run(token, NOTHING, NOTHING)
    status = {d.name: d.status for d in result.documents}
    assert status == {
        "checkout-api.log": recipe.REVIEW,
        "payment-worker.log": recipe.REVIEW,
        "oncall-notes.txt": recipe.BLOCKED,
    }, status
    assert result.calls == 3
    checkout, worker, notes = result.documents
    for kept in (
        "build=4.18.302.7",
        "host=10.0.4.17",
        "order_id=7300018842915562",
        "203.0.113.10",
        "https://api.cardvault.example/v1/charges",
        "/v2/checkout",
        "upstream=10.0.7.31:8443",
        "request_id=9f1c2a7e-4b3d-4e8a-9c21-5d7f0b6e3a14",
    ):
        assert kept in checkout.redacted + worker.redacted, kept
    assert '"amount": 4210, "currency": "USD", "card": "XXXX XXXX XXXX 1111"' in worker.redacted
    assert "client_ip=[IP_2] host=10.0.4.17" in checkout.redacted
    assert "Brightwater" not in notes.redacted
    assert "Delphine" not in notes.redacted
    assert "build 4.18.301" in notes.redacted
    assert "Imogen Tarrant" in notes.redacted
    assert "pods on 10.0.7.31" in notes.redacted
    # One placeholder per value across files: the customer's IP and user ID.
    assert "client_ip=[IP_1]" in checkout.redacted
    assert "from [IP_1]." in notes.redacted
    assert '"user_id": "[USER_ID_1]"' in worker.redacted
    folder = Path(tempfile.mkdtemp())
    recipe.write_outputs(folder, result)
    assert (folder / "blocked" / "oncall-notes.txt").is_file()
    assert (folder / "review" / "checkout-api.log").is_file()
    secrets = [s for d in result.documents for s in d.spans]
    for name in ("report.csv", "redactions.csv"):
        written = (folder / name).read_text(encoding="utf-8")
        for span in secrets:
            assert span.text not in written, (name, span.label)
    rows = list(csv.DictReader((folder / "report.csv").open(encoding="utf-8")))
    assert [r["status"] for r in rows] == ["review", "review", "blocked"]
    assert set(recipe.STATUSES) == {"ready_for_approval", "review", "blocked"}
    print("bundle: planted statuses with stand-in replies, no values in the reports ok")


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
    # A transient failure blocks that file as not read, the reason cut short; the others run.
    long = "connection refused " + "x" * 300
    for error in [*TRANSIENT, SIEConnectionError(long)]:
        result = bundle_run(error, NOTHING, NOTHING)
        first = result.documents[0]
        assert first.status == recipe.BLOCKED
        why = first.errors[0]
        prefix = "window 1 was not read: the SIE call failed: "
        assert why.startswith(prefix), why
        assert result.documents[1].status == recipe.REVIEW
    assert len(why) == len(prefix) + len("SIEConnectionError: ") + recipe.ERROR_CHARS, why
    # A reply missing `entities` is a failed call on the same path, not "nothing found".
    result = bundle_run({"error": {"code": "x"}}, NOTHING, NOTHING)
    reason = result.documents[0].errors[0]
    assert "SIEError: the reply has no `entities` (item error: {'code': 'x'})" in reason, reason
    result = bundle_run({"id": "no entities"}, NOTHING, NOTHING)
    assert result.documents[0].status == recipe.BLOCKED
    # An empty list is a real "nothing found": the rules decide, the call counts as a success.
    first = bundle_run({"entities": []}, NOTHING, NOTHING).documents[0]
    assert not first.errors, first.errors
    # A span whose offsets don't match its text blocks the file.
    bad = {"entities": [{"start": 0, "end": 4, "label": "person", "text": "nope"}]}
    assert bundle_run(bad, NOTHING, NOTHING).documents[0].status == recipe.BLOCKED

    # Every call failed: the run stops with the full message and nothing is returned to write.
    message = stopped(functools.partial(bundle_run, *[SIEConnectionError(long)] * 3))
    assert message.startswith(f"Every SIE call failed (3 of 3 to {PII})"), message
    assert long in message
    assert "Check SIE_BASE_URL and your network." in message
    assert "No outputs were written." in message
    for error in [ProvisioningError("no GPU yet"), ServerError("unavailable", status_code=503)]:
        message = stopped(functools.partial(bundle_run, *[error] * 3))
        assert "may still be starting or SIE is busy; run again in a few minutes." in message
    # A refusal on a later call stops the run at once, with SIE's full message.
    message = stopped(
        functools.partial(bundle_run, NOTHING, InsufficientCreditsError("no credits " + "y" * 300))
    )
    assert message.startswith(f"SIE refused the call to {PII}, "), message
    assert "InsufficientCreditsError (HTTP 402, code INSUFFICIENT_CREDITS)" in message, message
    assert "y" * 300 in message
    assert "SIE_API_KEY" in message
    message = stopped(functools.partial(bundle_run, ModelLoadFailedError("gated", permanent=True)))
    assert message.startswith(f"SIE can't load {PII}, ModelLoadFailedError"), message
    message = stopped(functools.partial(bundle_run, RequestError("unexpected body")))
    assert "SIE_API_KEY" not in message, message
    assert "SIE_BASE_URL" in message, message
    # A bug in our own code is not hidden, and main turns only SIEStopped into a clean exit.
    caught = raised(lambda: bundle_run(TypeError("bad argument"), NOTHING, NOTHING))
    assert type(caught) is TypeError, f"TypeError became {type(caught).__name__}"

    def bug() -> None:
        raise RuntimeError("a bug in recipe.py")

    def refusal() -> None:
        raise recipe.SIEStopped("SIE refused")

    assert type(raised(lambda: recipe.stop_on_refusal(bug))) is RuntimeError
    caught = raised(lambda: recipe.stop_on_refusal(refusal))
    assert type(caught) is SystemExit
    assert str(caught) == "Stopped: SIE refused"
    print("sie failure: transient errors block the file; refusals and all-failed stop ok")


if __name__ == "__main__":
    check_lookups()
    check_field_rules()
    check_overlap()
    check_names()
    check_sweep_and_report()
    check_bundle()
    check_sie_failure()
