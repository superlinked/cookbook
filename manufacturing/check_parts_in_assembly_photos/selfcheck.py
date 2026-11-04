"""Check the recipe's counting rules, statuses and SIE failure handling offline.

The checks use hand-written boxes, the committed data/ and hand-written stand-in model replies.
Nothing here calls SIE or replays a recorded response. Run it with
`uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import csv
import io
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from PIL import Image
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
FLANGE = "pipe_flange_8"
# A 560-pixel flange centred in a 640 photo, as make_data.py draws it.
PART = {"label": "flange", "score": 0.85, "bbox": [40, 40, 560, 560]}


def box(
    label: str, score: float, cx: float, cy: float, w: float = 80, h: float = 80
) -> dict[str, Any]:
    return {"label": label, "score": score, "bbox": [cx - w / 2, cy - h / 2, w, h]}


def ring(count: int, score: float = 0.75) -> list[dict[str, Any]]:
    """Fitted bolts at the eight flange positions (205 px from the centre), first `count`."""
    spots = [(320, 115), (465, 175), (525, 320), (465, 465), (320, 525), (175, 465), (115, 320)]
    spots.append((175, 175))
    return [box("bolt", score, x, y) for x, y in spots[:count]]


def inspect(objects: list[dict[str, Any]]) -> dict[str, Any]:
    return recipe.inspect([PART, *objects], FLANGE, RULES)


def check_data() -> None:
    photos = recipe.load_photos()
    assert [p.photo for p in photos][-2:] == ["FL-107", "FL-108"], [p.photo for p in photos]
    assert len(photos) == 10
    assert all(p.work_order.startswith("WO-") for p in photos)
    assert len({p.work_order for p in photos}) == len(photos)
    with Image.open(recipe.SPRITE_PATH) as sprite:
        assert not sprite.getexif(), "the committed sprite carries EXIF metadata"
        assert "xmp" not in sprite.info
        assert "XML:com.adobe.xmp" not in sprite.info
    print("data: 10 photos, one work order each, no EXIF or XMP on the sprite ok")


def check_statuses() -> None:
    assert recipe.STATUSES == ("defect", "inspector", "all_bolts_seen")
    assert "pass" not in recipe.STATUSES
    assert recipe.decide(8, 8, 0, []) == ("all_bolts_seen", "8 of 8 bolts seen")
    status, reason = recipe.decide(8, 7, 0, ["6 o'clock"])
    assert status == "defect", reason
    assert "empty hole at 6 o'clock" in reason, reason
    assert recipe.decide(8, 7, 0, [])[0] == "inspector"  # not seen, no hole to show it
    assert recipe.decide(8, 8, 0, ["3 o'clock"])[0] == "inspector"  # complete, but a hole
    assert recipe.decide(8, 7, 1, [])[0] == "inspector"  # the unsure bolt decides it
    assert recipe.decide(8, 9, 0, [])[0] == "inspector"  # more than expected
    assert inspect(ring(8))["status"] == "all_bolts_seen"
    print("statuses: all_bolts_seen, defect and inspector follow the count table ok")


def check_conflict() -> None:
    # FL-107's shape: one bolt missing, a loose bolt lying across that hole. The loose bolt
    # scores 0.81 and the hole 0.80; the higher score must not erase the hole.
    loose = box("bolt", 0.81, 175, 180, 140, 45)
    hole = box("hole", 0.80, 175, 175, 40, 20)
    row = inspect([*ring(7), loose, hole])
    assert row["status"] == "inspector", row["reason"]
    assert row["conflict_at"] == "10 o'clock", row["conflict_at"]
    assert "both seen at 10 o'clock" in row["reason"], row["reason"]
    # The other way round: a hole at 0.95 over a sure bolt at 0.75 is not a defect either.
    socket = box("bolt", 0.75, 175, 175)
    row = inspect([*ring(7), socket, box("hole", 0.95, 175, 175, 30, 30)])
    assert row["status"] == "inspector", row["reason"]
    # FL-102's shape: an unsure bolt (0.33) on a sure hole (0.90) was only a maybe, so the
    # hole stands and the photo is still a defect.
    row = inspect([*ring(7), box("bolt", 0.33, 175, 175), box("hole", 0.90, 175, 175, 40, 40)])
    assert row["status"] == "defect", row["reason"]
    assert (row["missing_at"], row["conflict_at"]) == ("10 o'clock", "")
    print("conflict: sure bolt on sure hole goes to the inspector; unsure bolt leaves a defect ok")


def check_unsure_holes() -> None:
    # 8 sure bolts, one of them a loose bolt lying elsewhere on the face, and a 0.66 hole at
    # the empty position: the count looks complete, but the possible hole is not ignored.
    loose = box("bolt", 0.81, 250, 400, 140, 45)
    row = inspect([*ring(7), loose, box("hole", 0.66, 175, 175, 40, 40)])
    assert row["bolts_sure"] == 8, row
    assert row["status"] == "inspector", row["reason"]
    assert row["unsure_hole_at"] == "10 o'clock", row["unsure_hole_at"]
    assert "possible empty hole at 10 o'clock" in row["reason"], row["reason"]
    # FL-108's shape: a socket head seen as a sure bolt (0.76) and a 0.66 hole on one spot.
    socket = box("bolt", 0.76, 175, 175)
    row = inspect([*ring(7), socket, box("hole", 0.66, 175, 175, 30, 30)])
    assert row["status"] == "inspector", row["reason"]
    assert row["reason"].startswith("a bolt was seen over a possible empty hole at 10 o'clock")
    # A possible hole is never evidence of a missing bolt: 7 bolts and only a 0.66 hole is not
    # a defect.
    row = inspect([*ring(7), box("hole", 0.66, 175, 175, 40, 40)])
    assert row["status"] == "inspector", row["reason"]
    # Below hole_unsure (0.40) a hole box is still ignored: FL-104 had holes at 0.21 to 0.23.
    row = inspect([*ring(8), box("hole", 0.23, 320, 320, 40, 40)])
    assert row["status"] == "all_bolts_seen", row["reason"]
    unsure = [n for n in row["detections"] if n["use"] == "unsure hole"]
    assert not unsure
    row = inspect([*ring(7), socket, box("hole", 0.66, 175, 175, 30, 30)])
    assert [n["use"] for n in row["detections"]].count("unsure hole") == 1  # drawn, orange
    assert "unsure hole" in recipe.COLORS
    print("unsure holes: a 0.66 hole on a complete count or under a bolt goes to the inspector ok")


def check_counting() -> None:
    # FL-104's shape: a spare bolt on the bench, outside the flange, is ignored and counted.
    spare = box("bolt", 0.83, 540, 610, 140, 45)
    row = inspect([*ring(8), spare])
    assert (row["status"], row["ignored_outside_part"]) == ("all_bolts_seen", 1), row
    # A nut and its washer boxed separately are one bolt: the washer box holds the nut's centre.
    washers = [box("bolt", 0.65, x, y, 90, 90) for x, y in [(320, 115), (465, 175)]]
    row = inspect([*ring(8), *washers])
    assert (row["status"], row["bolts_sure"]) == ("all_bolts_seen", 8), row
    merged = [n for n in row["detections"] if n["why"] == "same bolt as a higher-scoring box"]
    assert len(merged) == 2
    # Photo quality: a flat dark grey photo is out of focus and too dark; a noisy bright one is
    # fine.
    flat = Image.new("RGB", (64, 64), (40, 40, 40))
    sharpness, brightness = recipe.photo_quality(flat)
    problem = recipe.quality_problem(sharpness, brightness, RULES)
    assert "out of focus" in problem, problem
    assert "too dark" in problem, problem
    stripes = Image.new("L", (64, 64), 200)
    for x in range(0, 64, 2):
        for y in range(64):
            stripes.putpixel((x, y), 60)
    assert recipe.quality_problem(*recipe.photo_quality(stripes.convert("RGB")), RULES) == ""
    print("counting: spare bolt outside the part, nut-and-washer merge, photo quality ok")


def check_csv_and_photo() -> None:
    assert recipe.csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert recipe.csv_safe("-1 bolt") == "'-1 bolt"
    assert recipe.csv_safe("8 of 8 bolts seen") == "8 of 8 bolts seen"
    assert recipe.csv_safe(3) == "3"
    with tempfile.TemporaryDirectory() as folder:
        sideways = Path(folder) / "sideways.jpg"
        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90 degrees clockwise to display
        Image.new("RGB", (40, 20), "white").save(sideways, exif=exif)
        assert recipe.open_photo(sideways).size == (20, 40)
        row = {
            **inspect(ring(8)),
            "photo": "X",
            "work_order": "=cmd",
            "sharpness": 1.0,
            "brightness": 1.0,
        }
        recipe.write_outputs(Path(folder), [row], {"X": Image.new("RGB", (640, 640))})
        with (Path(folder) / "inspections.csv").open(encoding="utf-8") as handle:
            assert next(csv.DictReader(handle))["work_order"] == "'=cmd"
    print("csv and photo: formula cells quoted, EXIF orientation applied ok")


class Stub:
    """Stands in for SIEClient: each call raises the next listed error, or returns the next
    listed reply. None means a complete flange (part and 8 sure bolts)."""

    def __init__(self, *replies: Exception | dict[str, Any] | None) -> None:
        self.replies = list(replies)
        self.calls = 0

    def extract(self, model: str, item: Any, **_: Any) -> dict[str, Any]:
        self.calls += 1
        reply = self.replies.pop(0) if self.replies else None
        if isinstance(reply, Exception):
            raise reply
        if reply is None:
            part = {**PART, "label": "flange"}
            return {"objects": [part, *ring(8)]}
        return reply


TWO = "photo,work_order,assembly,x,y,width,height,drawn\n"
TWO += "FL-101,WO-4101,pipe_flange_8,0,0,640,640,a\nFL-102,WO-4102,pipe_flange_8,640,0,640,640,b\n"


def run(*replies: Exception | dict[str, Any] | None, output: Path | None = None) -> Any:
    """Run the recipe on FL-101 and FL-102 with stand-in replies; return the rows."""
    with tempfile.TemporaryDirectory() as folder:
        photos = Path(folder) / "photos.csv"
        photos.write_text(TWO, encoding="utf-8")
        client = cast(SIEClient, Stub(*replies))
        with contextlib.redirect_stdout(io.StringIO()):
            return recipe.run(client, RULES, output, photos_path=photos)


def stopped(call: Callable[[], object]) -> str:
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
    for error in [
        SIEConnectionError("refused"),
        ServerError("unavailable", status_code=503),
        RateLimitError("slow down"),
        InputTooLongError("too big"),
        ModelLoadingError("loading"),
        ModelLoadFailedError("try later", permanent=False),
        SIEError("no objects"),
    ]:
        assert not recipe.is_fatal(error), type(error).__name__
    for error in [
        RequestError("bad key", status_code=401),
        InsufficientCreditsError("no credits"),
        ModelLoadFailedError("gated", permanent=True),
        RequestError("unexpected body"),
    ]:
        assert recipe.is_fatal(error), type(error).__name__
    # A transient error on one photo: it goes to the inspector, the other is still inspected.
    rows = run(ServerError("unavailable " + "x" * 300, status_code=503), None)
    assert [r["status"] for r in rows] == ["inspector", "all_bolts_seen"]
    assert rows[0]["reason"].startswith(
        "not inspected: the SIE call failed: ServerError (HTTP 503): unavailable"
    )
    assert len(rows[0]["reason"]) < 300
    # A reply with no objects key is a failed call, not "nothing found".
    rows = run({"something": []}, None)
    assert rows[0]["status"] == "inspector"
    assert "no 'objects' list" in rows[0]["reason"], rows[0]["reason"]
    # An object without a box or a score is a failed call too, not a crash.
    for bad in [{"label": "bolt", "score": 0.9}, {"label": "bolt", "bbox": [1, 2, 3, 4]}]:
        rows = run({"objects": [PART, bad]}, None)
        assert rows[0]["status"] == "inspector", rows[0]
        assert "without a label, score or box" in rows[0]["reason"], rows[0]["reason"]
    # A photo nothing was counted on has empty count cells, not 0.
    assert rows[0]["bolts_sure"] is None
    with tempfile.TemporaryDirectory() as folder:
        recipe.write_outputs(
            Path(folder), rows, {r["photo"]: Image.new("RGB", (9, 9)) for r in rows}
        )
        with (Path(folder) / "inspections.csv").open(encoding="utf-8") as handle:
            first = next(csv.DictReader(handle))
        assert (first["bolts_sure"], first["empty_holes"]) == ("", ""), first
        assert "status" in first, list(first)
        assert "decision" not in first, list(first)
    # An empty objects list is a real "nothing found": no part, so the inspector looks.
    rows = run({"objects": []}, None)
    assert rows[0]["reason"].startswith("no flange found"), rows[0]["reason"]
    # A refusal stops the run at once and writes nothing.
    with tempfile.TemporaryDirectory() as folder:
        output = Path(folder) / "out"
        message = stopped(lambda: run(None, InsufficientCreditsError("no credits"), output=output))
        assert "InsufficientCreditsError (HTTP 402" in message, message
        assert "No outputs were written." in message
        assert not output.exists(), "a refused run wrote outputs"
    # Every call failed: the run stops.
    message = stopped(lambda: run(SIEConnectionError("refused"), SIEConnectionError("refused")))
    assert message.startswith("Every SIE call failed (2 of 2 to facebook/sam3)"), message
    assert "Check SIE_BASE_URL and your network." in message
    message = stopped(lambda: run({"x": 1}, {"x": 1}))
    assert "may still be starting" in message, message

    # A bug is raised as is; SIEStopped becomes a clean exit.
    def bug() -> None:
        raise RuntimeError("a bug")

    def refusal() -> None:
        raise recipe.SIEStopped("SIE refused")

    caught = raised(lambda: recipe.stop_on_refusal(bug))
    assert type(caught) is RuntimeError, f"a RuntimeError became {type(caught).__name__}"
    assert str(caught) == "a bug"
    caught = raised(lambda: recipe.stop_on_refusal(refusal))
    assert type(caught) is SystemExit, "SIEStopped did not stop the run cleanly"
    assert str(caught) == "Stopped: SIE refused"
    print("sie failure: transient and missing key to the inspector; refusal, all-failed stop ok")


if __name__ == "__main__":
    check_data()
    check_statuses()
    check_conflict()
    check_unsure_holes()
    check_counting()
    check_csv_and_photo()
    check_sie_failure()
