"""Check the recipe's search rules, the PIM lookup, the gap log and the SIE error rule offline.

The checks use the committed data/, hand-written vectors and hand-written stand-in model
replies. Nothing here calls SIE or replays a recorded response. Run it with
`uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import csv
import functools
import io
import math
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import numpy as np
from PIL import Image
from pim import PIM
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
MODEL = RULES["models"]["embed"]
CATALOG = PIM()
QUERIES = {q.query_id: q for q in recipe.load_queries()}
PRODUCTS = [p.product_id for p in CATALOG.all_products()]
COLORS = list(RULES["close_style"]["colors"])
DIM = 64  # hand-written vectors: one axis per product, then one per color, then query axes
AXES = {name: n for n, name in enumerate([*PRODUCTS, *(f"color:{c}" for c in COLORS)])}
PHOTO_AXIS, WORDS_AXIS = DIM - 2, DIM - 1


def axis(name: str) -> list[float]:
    vector = [0.0] * DIM
    vector[AXES[name] if name in AXES else int(name)] = 1.0
    return vector


def mix(*parts: tuple[float, list[float]]) -> np.ndarray:
    """A unit vector from weighted axes."""
    vector = sum((weight * np.asarray(part) for weight, part in parts), np.zeros(DIM))
    return cast(np.ndarray, vector / np.linalg.norm(vector))


def scored(photo: float, words: float = 0.0, own: str = "") -> np.ndarray:
    """A product vector that scores `photo` against the photo axis and `words` against the words
    axis; the rest of its length sits on its own axis."""
    rest = math.sqrt(1 - photo**2 - words**2)
    vector = np.zeros(DIM)
    vector[PHOTO_AXIS], vector[WORDS_AXIS], vector[AXES[own]] = photo, words, rest
    return vector


PHOTO = mix((1.0, axis(str(PHOTO_AXIS))))
WORDS = mix((1.0, axis(str(WORDS_AXIS))))


def index(products: dict[str, np.ndarray], colors: dict[str, np.ndarray] | None = None) -> Any:
    """An index holding these product vectors; every other product scores 0 against anything."""
    vectors = {p: np.asarray(axis(p)) for p in PRODUCTS} | products
    color_vectors = {c: np.asarray(axis(f"color:{c}")) for c in COLORS} | (colors or {})
    return recipe.Index(products=vectors, colors=color_vectors, not_indexed=[])


def query(kind: str, department: str = "", text: str = "x") -> recipe.Query:
    return recipe.Query(
        query_id="T1",
        kind=kind,
        text="" if kind == "photo" else text,
        department=department,
        box=None if kind == "text" else (0, 0, 256, 256),
        note="",
    )


def search(q: recipe.Query, idx: Any, photo: Any = PHOTO, words: Any = WORDS) -> dict[str, Any]:
    return recipe.search(
        q, photo if q.kind != "text" else None, words if q.kind != "photo" else None, CATALOG,
        idx, RULES,
    )  # fmt: skip


def ids(result: dict[str, Any]) -> list[str]:
    return [h["product_id"] for h in result["results"]]


# --- Data and the PIM lookup -----------------------------------------------------------------


def check_data() -> None:
    assert len(PRODUCTS) == 35, len(PRODUCTS)
    assert sorted(QUERIES) == [f"Q{n:02d}" for n in range(1, 14)], sorted(QUERIES)
    # The two new lamps: same lamp, dark and light wood, both available.
    dark, light = CATALOG.find_product("P34"), CATALOG.find_product("P35")
    assert dark
    assert light
    assert dark.category == light.category == "LAMP"
    assert "Espresso Dark Brown" in dark.title
    assert dark.color == "brown"
    assert dark.available
    assert light.title.endswith("Oak")
    assert light.available
    # Q11 and Q12 reuse Q07's and Q08's photos; Q13 is a photo of P15, which is unavailable.
    assert QUERIES["Q11"].box == QUERIES["Q07"].box
    assert QUERIES["Q11"].kind == "photo_and_text"
    assert QUERIES["Q12"].box == QUERIES["Q08"].box
    assert QUERIES["Q12"].text == "in brown leather"
    assert CATALOG.find_product("P15")
    assert not cast(Any, CATALOG.find_product("P15")).available
    # Every PIM color is a color word the close-style check knows.
    recipe.check_colors(CATALOG, RULES)
    # The committed photos carry no EXIF or XMP metadata.
    for path in (recipe.CATALOG_PHOTOS_PATH, recipe.QUERY_PHOTOS_PATH):
        with Image.open(path) as image:
            left = {"exif", "xmp", "photoshop", "comment"} & set(image.info)
            assert not left, (path.name, left)
            assert not image.getexif(), path.name
    # A search whose kind doesn't fit its inputs is refused before any SIE call.
    folder = Path(tempfile.mkdtemp())
    bad = recipe.QUERIES_PATH.read_text(encoding="utf-8").replace("Q04,text,", "Q04,photo,")
    (folder / "q.csv").write_text(bad, encoding="utf-8")
    assert type(raised(lambda: recipe.load_queries(folder / "q.csv"))) is ValueError
    print("data: 35 products, 13 searches, P34/P35 lamps, P15 unavailable, no metadata ok")


def check_pim() -> None:
    assert CATALOG.has_department("LAMP")
    # A department facet the catalog doesn't use, or a different spelling, is not found.
    assert not CATALOG.has_department("SANDAL")
    assert not CATALOG.has_department("lamp")
    rugs = [p.product_id for p in CATALOG.sellable("RUG")]
    assert rugs == ["P13", "P14", "P16"], rugs  # P15 is unavailable
    assert "P15" not in [p.product_id for p in CATALOG.sellable()]
    assert len(CATALOG.all_products()) == 35
    assert CATALOG.find_product("P99") is None
    # A facet value is a query parameter, never SQL: a crafted one finds nothing.
    assert CATALOG.sellable("RUG' OR '1'='1") == []
    assert not CATALOG.has_department("RUG' OR '1'='1")
    print("pim: exact department facets, unavailable products never sellable ok")


# --- The search rules ------------------------------------------------------------------------


def check_every_hit_has_a_bar() -> None:
    # Q01's shape: three glasses, one above the text bar, two just below. Only one is shown.
    idx = index(
        {
            "P27": scored(0.193, own="P27"),
            "P28": scored(0.098, own="P28"),
            "P26": scored(0.096, own="P26"),
        }
    )
    result = search(query("text", "DRINKING_CUP"), idx, words=PHOTO)
    assert result["status"] == "results", result
    assert ids(result) == ["P27"], result
    assert [h["product_id"] for h in result["candidates"]] == ["P27", "P28", "P26"]
    assert "2 more below the bar not shown" in result["reason"], result["reason"]
    # Nothing clears the bar: no_good_match, nothing shown, the candidates kept for the log.
    idx = index({"P28": scored(0.619, own="P28")})
    result = search(query("photo"), idx)
    assert result["status"] == "no_good_match", result
    assert not result["results"], result
    assert result["candidates"][0]["product_id"] == "P28"
    print("bar: every hit must clear it, not only the best ok")


def check_availability() -> None:
    # The exact match (P15) scores best but is unavailable: never shown, never a candidate.
    idx = index({"P15": scored(0.95, own="P15"), "P16": scored(0.74, own="P16")})
    result = search(query("photo"), idx)
    assert ids(result) == ["P16"], result
    assert "P15" not in [h["product_id"] for h in result["candidates"]]
    # A department with nothing available or indexed: no_good_match, not a crash.
    idx = index({})
    for product in CATALOG.sellable("RUG"):
        del idx.products[product.product_id]
    result = search(query("photo", "RUG"), idx)
    assert result["status"] == "no_good_match", result
    assert result["reason"] == "No available, indexed products in RUG.", result["reason"]
    print("availability: unavailable exact match never shown ok")


def check_photo_and_words() -> None:
    # Q11's shape: the chrome lamp looks most like the photo, the dark wood lamp has the words.
    lamps = {
        "P11": scored(0.876, 0.076, own="P11"),
        "P09": scored(0.788, 0.085, own="P09"),
        "P34": scored(0.774, 0.098, own="P34"),
        "P35": scored(0.772, 0.087, own="P35"),
        "P10": scored(0.711, 0.079, own="P10"),
        "P12": scored(0.691, 0.099, own="P12"),
    }
    photo_only = search(query("photo", "LAMP"), index(lamps))
    assert ids(photo_only)[0] == "P11", photo_only
    result = search(query("photo_and_text", "LAMP"), index(lamps))
    assert result["status"] == "results", result
    # Only the dark wood lamp clears both bars; the light wood near miss and the chrome lamp,
    # which the photo alone puts first, are not shown.
    assert ids(result) == ["P34"], result
    # P12 has the best words score but is sixth by photo: outside the 5-product shortlist.
    assert "P12" not in [h["product_id"] for h in result["candidates"]], result["candidates"]
    assert next(h["product_id"] for h in result["candidates"]) == "P34", result["candidates"]
    # Q12's shape: the photo finds the grey bag, the words ask for brown leather; the leather
    # bags don't look like the photo. Neither half alone makes a result.
    bags = {
        "P17": scored(0.895, 0.041, own="P17"),
        "P18": scored(0.53, 0.073, own="P18"),
        "P19": scored(0.534, 0.12, own="P19"),
    }
    result = search(query("photo_and_text", "HANDBAG"), index(bags))
    assert result["status"] == "no_good_match", result
    assert not result["results"], result
    assert result["reason"].startswith("No good match: P17 0.895 cleared the photo bar"), result
    # P19 has the better words but doesn't look like the photo: the reason names P17's words.
    assert "best words score P17 0.041, below 0.09" in result["reason"], result["reason"]
    # Nothing looks like the photo at all.
    result = search(query("photo_and_text", "HANDBAG"), index({"P19": scored(0.6, 0.2, "P19")}))
    assert "nothing looks like the photo (best P19 0.600" in result["reason"], result["reason"]
    print("photo and words: shortlist by photo, reorder by words, both bars per hit ok")


def check_negation() -> None:
    for words in ("lamp without a wood base", "NOT leather", "a bag that isn't brown", "non-wood"):
        assert recipe.negation(words, RULES), words
    for words in ("but the base should be dark wood", "in brown leather", "a lamp with a knob"):
        assert not recipe.negation(words, RULES), words
    print("negation: words with 'not', 'without' or n't are caught ok")


def check_close_style() -> None:
    q = query("photo", "SHOES")
    idx = index({"P33": scored(0.929, own="P33")})
    result = search(q, idx)
    navy, black = np.asarray(axis("color:navy")), np.asarray(axis("color:black"))
    looks_black = mix((1.0, list(PHOTO)), (0.5, list(black)))
    looks_navy = mix((1.0, list(PHOTO)), (0.5, list(navy)))
    reason = recipe.close_style(q, result, looks_black, CATALOG, idx)
    assert reason.startswith("Close style: the photo looks black, top result P33 is navy"), reason
    assert recipe.close_style(q, result, looks_navy, CATALOG, idx) == ""
    # The product's color has no vector this run: not confirmed, so it is logged.
    del idx.colors["navy"]
    assert "color not checked" in recipe.close_style(q, result, looks_navy, CATALOG, idx)
    # Words searches and a photo with words are not color-checked: the words state the change.
    assert recipe.close_style(query("photo_and_text"), result, looks_black, CATALOG, idx) == ""
    print("close style: a top result in another color than the photo is logged ok")


def check_vectors() -> None:
    assert recipe.unit([0.0, 0.0, 0.0]) is None
    assert recipe.unit([]) is None
    assert recipe.unit([1.0, float("nan")]) is None
    assert recipe.unit([1.0, float("inf")]) is None
    assert recipe.unit("garbage") is None
    vector = recipe.unit([3.0, 4.0])
    assert vector is not None
    assert abs(float(np.linalg.norm(vector)) - 1.0) < 1e-9
    print("vectors: empty, all-zero and non-finite vectors are refused ok")


# --- Whole runs with a stand-in client -------------------------------------------------------


class StandIn:
    """Stands in for SIEClient. Each encode call takes the next listed reply: an exception is
    raised, a function is called with the items, anything else is returned as the reply."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[list[str]] = []

    def encode(self, model: str, items: Any, **_: Any) -> Any:
        assert model == MODEL, model
        self.calls.append([item["id"] for item in items])
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply(items) if callable(reply) else reply


def index_reply(broken: dict[str, Any] | None = None) -> Callable[[Any], list[dict[str, Any]]]:
    """A reply to the catalog index call: each product and color on its own axis."""

    def reply(items: Any) -> list[dict[str, Any]]:
        return [
            {"dense": (broken or {}).get(item["id"], axis(item["id"]))}
            for item in items
        ]  # fmt: skip

    return reply


def search_reply(*vectors: Any) -> list[dict[str, Any]]:
    return [{"dense": list(v)} for v in vectors]


# Two searches: a photo of P33 that looks navy in SHOES, and words that point at P17.
SEARCHES = (
    "query_id,kind,text,department,photo_of,abo_item_id,abo_image_id,cropped,x,y,w,h,note\n"
    "S1,photo,,SHOES,,,,,768,0,256,256,a photo\n"
    'S2,text,=HYPERLINK("x"),,,,,,,,,,words\n'
)
NAVY_P33 = mix((1.0, axis("P33")), (0.3, axis("color:navy")))
P17_WORDS = mix((0.2, axis("P17")), (1.0, axis(str(WORDS_AXIS))))
OK = (search_reply(NAVY_P33), search_reply(P17_WORDS))


def run(*replies: Any, folder: Path | None = None, searches: str = SEARCHES) -> tuple[Any, ...]:
    """Run the recipe on the hand-written searches with stand-in replies; outputs go to
    `folder`/out."""
    folder = folder or Path(tempfile.mkdtemp())
    (folder / "q.csv").write_text(searches, encoding="utf-8")
    client = StandIn(*replies)
    output = folder / "out"
    with contextlib.redirect_stdout(io.StringIO()):
        result = recipe.run(cast(SIEClient, client), RULES, output, CATALOG, folder / "q.csv")
    assert not client.replies, f"{len(client.replies)} replies left unused"
    return result, output, client


def stopped(call: Callable[[], object]) -> str:
    try:
        call()
    except recipe.SIEStopped as error:
        return str(error)
    raise AssertionError("the run did not stop")


def raised(call: Callable[[], object]) -> BaseException:
    try:
        call()
    except BaseException as error:  # SystemExit too: the check is on which one it was
        return error
    raise AssertionError("nothing was raised")


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def check_run_outputs() -> None:
    result, output, client = run(index_reply(), *OK)
    # One index call (35 photos and the color captions), then one call per search.
    assert len(client.calls[0]) == 35 + len(COLORS), len(client.calls[0])
    assert client.calls[1:] == [["S1:photo"], ["S2:words"]], client.calls
    s1, s2 = result["searches"]
    assert s1["status"] == "results", s1
    assert ids(s1) == ["P33"], s1
    assert not s1["close_style"], s1
    assert s2["status"] == "results", s2
    assert ids(s2) == ["P17"], s2
    assert result["sie_calls"] == {"made": 3, "failed": 0}
    shown = rows(output / "results.csv")
    assert [r["product_id"] for r in shown] == ["P33", "P17"], shown
    # The shopper's words are quoted where a spreadsheet would run them as a formula.
    assert shown[1]["words"] == '\'=HYPERLINK("x")', shown[1]
    assert rows(output / "search_gap_log.csv") == []
    # Unknown department and negated words: no SIE call, straight to the log.
    searches = SEARCHES.replace("S1,photo,,SHOES", "S1,photo,,SANDAL")
    searches = searches.replace('=HYPERLINK("x")', "a lamp without a shade")
    result, output, client = run(index_reply(), searches=searches)
    assert len(client.calls) == 1, client.calls
    statuses = [s["status"] for s in result["searches"]]
    assert statuses == ["unknown_department", "no_good_match"], statuses
    log = rows(output / "search_gap_log.csv")
    assert [r["entry"] for r in log] == ["unknown_department", "no_good_match"], log
    assert "negation ('without')" in log[1]["reason"], log[1]
    assert set(statuses) <= set(recipe.STATUSES)
    # A catalog photo with a broken vector is left out of the index and logged for the team.
    result, output, _ = run(index_reply({"P17": [0.0] * DIM}), *OK)
    s2 = result["searches"][1]
    assert s2["status"] == "no_good_match", s2  # P17 can't be found now
    log = rows(output / "search_gap_log.csv")
    assert log[-1]["entry"] == "not_indexed", log
    assert log[-1]["reason"].startswith("P17: "), log
    assert {r["entry"] for r in log} <= set(recipe.LOG_ENTRIES)
    # A close style: the photo of P33 looks black.
    black_p33 = mix((1.0, axis("P33")), (0.3, axis("color:black")))
    result, output, _ = run(index_reply(), search_reply(black_p33), search_reply(P17_WORDS))
    log = rows(output / "search_gap_log.csv")
    assert [r["entry"] for r in log] == ["close_style"], log
    assert log[0]["candidates"].startswith("P33 0.958"), log
    print("run: one index call, one call per search, outputs and gap log ok")


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


def check_sie_failure() -> None:
    for error in TRANSIENT:
        assert not recipe.is_fatal(error), f"{type(error).__name__} should be transient"
    for error in FATAL:
        assert recipe.is_fatal(error), f"{type(error).__name__} should stop the run"

    # A transient failure on one search: that search gets no_good_match with the error, cut
    # short; the other search runs.
    long = "connection refused " + "x" * 300
    for error in [*TRANSIENT, SIEConnectionError(long)]:
        result, output, _ = run(index_reply(), error, OK[1])
        first = result["searches"][0]
        assert first["status"] == "no_good_match", first
        prefix = "No good match: the SIE call failed: "
        assert first["reason"].startswith(prefix), first["reason"]
        assert result["searches"][1]["status"] == "results"
        assert result["sie_calls"] == {"made": 3, "failed": 1}
    assert len(first["reason"]) == len(prefix) + len("SIEConnectionError: ") + 200
    assert rows(output / "search_gap_log.csv")[0]["entry"] == "no_good_match"
    # A reply missing its key, or with too few results, is a failed call on the same path.
    replies: list[Any] = [[{"id": "S1:photo"}], [], {"dense": []}]
    for reply in replies:
        result, _, _ = run(index_reply(), reply, OK[1])
        assert "the SIE call failed: SIEError: the reply has" in result["searches"][0]["reason"]
    # An empty, all-zero, non-finite or wrong-length vector fails the search instead of scoring.
    for vector in ([], [0.0] * DIM, [float("nan")] * DIM, [1.0] * (DIM + 1)):
        result, _, _ = run(index_reply(), [{"dense": vector}], OK[1])
        first = result["searches"][0]
        assert first["status"] == "no_good_match", first
        assert "vector" in first["reason"], first

    def stops(*replies: Any) -> str:
        """The stop message; also checks the run wrote nothing."""
        folder = Path(tempfile.mkdtemp())
        message = stopped(functools.partial(run, *replies, folder=folder))
        assert not (folder / "out").exists(), "a stopped run wrote outputs"
        return message

    # Every search call failed (a dead SIE_BASE_URL): the run stops, writes nothing.
    message = stops(index_reply(), SIEConnectionError(long), SIEConnectionError(long))
    assert message.startswith(f"Every search's SIE call failed (2 of 2 to {MODEL})"), message
    assert long in message
    assert "Check SIE_BASE_URL and your network." in message, message
    assert "No outputs were written." in message
    message = stops(index_reply(), ProvisioningError("no GPU"), ProvisioningError("no GPU"))
    assert "may still be starting or SIE is busy; run again in a few minutes." in message
    # The catalog index call failing, even transiently, stops the run: nothing can be searched.
    message = stops(ServerError("unavailable", status_code=503))
    assert message.startswith(f"The catalog index call to {MODEL} failed"), message
    message = stops(lambda items: [{"id": item["id"]} for item in items])
    assert "the reply has no `dense` vector" in message, message
    # A refusal on any call stops the run at once, with SIE's full message.
    message = stops(index_reply(), OK[0], InsufficientCreditsError("no credits " + "y" * 300))
    assert message.startswith(f"SIE refused the call to {MODEL}, "), message
    assert "InsufficientCreditsError (HTTP 402, code INSUFFICIENT_CREDITS)" in message, message
    assert "y" * 300 in message
    message = stops(RequestError("bad key", status_code=401))
    assert message.startswith(f"SIE refused the call to {MODEL}, RequestError (HTTP 401)")
    message = stops(ModelLoadFailedError("gated", permanent=True))
    assert message.startswith(f"SIE can't load {MODEL}, ModelLoadFailedError"), message
    message = stops(RequestError("unexpected body"))
    assert "SIE_API_KEY" not in message, message
    assert "SIE_BASE_URL" in message, message
    # A bug in our own code is not hidden, and main turns only SIEStopped into a clean exit.
    caught = raised(lambda: run(TypeError("bad argument")))
    assert type(caught) is TypeError, f"TypeError became {type(caught).__name__}"

    def bug() -> None:
        raise RuntimeError("a bug in recipe.py")

    def refusal() -> None:
        raise recipe.SIEStopped("SIE refused")

    assert type(raised(lambda: recipe.stop_on_refusal(bug))) is RuntimeError
    caught = raised(lambda: recipe.stop_on_refusal(refusal))
    assert type(caught) is SystemExit
    assert str(caught) == "Stopped: SIE refused"
    print("sie failure: transient errors fail one search; refusals and all-failed stop ok")


if __name__ == "__main__":
    check_data()
    check_pim()
    check_every_hit_has_a_bar()
    check_availability()
    check_photo_and_words()
    check_negation()
    check_close_style()
    check_vectors()
    check_run_outputs()
    check_sie_failure()
