"""Count the bolts in inspection photos and flag assemblies with bolts missing.

Use case:  Visual presence check: does every bolt position of an assembly show a bolt before
           it leaves the station? Torque and joint quality come from the tightening record.
Who:       Assembly operators and quality inspectors at stations that photograph each
           assembly against its work order.
Problem:   A flange or cover plate that leaves the line with a bolt missing leaks or loosens in
           service. Counting bolts photo after photo is slow, and late in a shift it is easy to
           get wrong.
Input:     data/inspection-photos.jpg (one sprite sheet, one tile per photo) and
           data/photos.csv (photo id, work order, assembly type, tile box), as the MES would
           give them. rules.yaml holds the expected bolt count per assembly (from the BOM), the
           prompts and every threshold.
SIE pipeline:
           (Python)                check sharpness and brightness first; a blurred or dark
                                   photo is sent back for a retake and never reaches SIE
           extract  facebook/sam3  one call per photo with three text prompts: the part
                                   ("flange" or "metal plate"), "bolt" and "hole"; it returns
                                   a scored box for every instance it finds
           (Python)                keep the bolts that sit on the part, drop duplicates, send a
                                   sure bolt on top of a sure hole to the inspector, split sure
                                   from unsure, and compare the count with the expected count:
                                   all_bolts_seen, defect, or inspector when unsure
Output:    inspections.csv (one row per photo: status, counts, where bolts are missing and the
           reason), defects.csv, inspector.csv, detections.json (every box and why it was kept
           or ignored) and annotated/<photo>.jpg with the boxes drawn.
Why SIE:   One model on SIE Cloud, called once per photo, finds every bolt from a text prompt,
           with no training images and no labelling. No generative model makes the decision:
           the count and the status are plain Python you can read.
Runs on:   SIE Cloud.

    uv run recipe.py
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import numpy as np
import yaml
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageOps
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
SPRITE_PATH = DATA_DIR / "inspection-photos.jpg"
PHOTOS_PATH = DATA_DIR / "photos.csv"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900
# A failed SIE call's message is cut to this many characters in reasons and the summary line.
ERROR_CHARS = 200
# A CSV cell starting with one of these opens as a formula in a spreadsheet.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")

# A status names what the checks found. all_bolts_seen is visual presence only: it says nothing
# about torque, the right bolt or the joint. People decide what happens next.
ALL_BOLTS_SEEN, DEFECT, INSPECTOR = "all_bolts_seen", "defect", "inspector"
DECISIONS = (DEFECT, INSPECTOR, ALL_BOLTS_SEEN)
REPORT_FIELDS = (
    "photo",
    "work_order",
    "assembly",
    "decision",
    "sharpness",
    "brightness",
    "expected_bolts",
    "bolts_sure",
    "bolts_unsure",
    "empty_holes",
    "missing_at",
    "conflict_at",
    "ignored_outside_part",
    "reason",
)
Box = list[float]  # x, y, width, height in pixels of the photo


@dataclass(frozen=True)
class Photo:
    photo: str
    work_order: str
    assembly: str
    box: tuple[int, int, int, int]  # x, y, width, height of the photo in the sprite sheet
    planted: str


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def load_photos(path: Path = PHOTOS_PATH) -> list[Photo]:
    with path.open(encoding="utf-8") as handle:
        return [
            Photo(
                photo=row["photo"],
                work_order=row["work_order"],
                assembly=row["assembly"],
                box=(int(row["x"]), int(row["y"]), int(row["width"]), int(row["height"])),
                planted=row["planted"],
            )
            for row in csv.DictReader(handle)
        ]


def open_photo(path: Path) -> Image.Image:
    """The image upright (its EXIF orientation applied, as a camera records it), in RGB."""
    with Image.open(path) as opened:
        return ImageOps.exif_transpose(opened).convert("RGB")


def tile(sprite: Image.Image, photo: Photo) -> Image.Image:
    x, y, width, height = photo.box
    return sprite.crop((x, y, x + width, y + height))


def _jpeg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


# --- Detect -----------------------------------------------------------------------------------


def prompts(assembly: dict[str, Any], detection: dict[str, Any]) -> list[str]:
    return [assembly["part_prompt"], detection["bolt_prompt"], detection["hole_prompt"]]


def make_client() -> SIEClient:
    """Build an SIE client from the API key and SIE_BASE_URL in the environment or `.env`."""
    load_dotenv()
    api_key = os.environ.get("SIE_API_KEY", "").strip()
    base_url = os.environ.get("SIE_BASE_URL", "").strip()
    if not api_key and not base_url:
        raise RuntimeError("Set SIE_API_KEY, or SIE_BASE_URL for your own SIE, in .env")
    base_url = base_url or "https://api.superlinked.com"
    return SIEClient(api_key=api_key or None, base_url=base_url, timeout_s=WAIT_S)


def jsonable(value: Any) -> Any:
    """Turn SDK results (numpy arrays, nested dicts and lists) into plain JSON values."""
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, dict):
        return {str(key): jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(child) for child in value]
    return value


def detect(
    client: SIEClient, photo: Photo, image: Image.Image, rules: dict[str, Any]
) -> list[dict[str, Any]]:
    """Ask SAM 3 for every part, bolt and hole box in one photo.

    An empty `objects` list is a real "nothing found". A reply with no `objects` key at all is a
    failed call, raised as an SIEError so it takes the same path as a timeout.
    """
    detection = rules["detection"]
    response = jsonable(
        client.extract(
            rules["models"]["detect"],
            Item(id=photo.photo, images=[_jpeg(image)]),
            labels=prompts(rules["assemblies"][photo.assembly], detection),
            options={"score_threshold": detection["score_threshold"]},
            provision_timeout_s=WAIT_S,
        )
    )
    if not isinstance(response, dict) or not isinstance(response.get("objects"), list):
        raise SIEError("the SIE reply had no 'objects' list")
    return cast(list[dict[str, Any]], response["objects"])


# --- Photo quality ----------------------------------------------------------------------------


def photo_quality(image: Image.Image) -> tuple[float, float]:
    """Return (sharpness, brightness): the variance of the grey Laplacian and the mean grey."""
    grey = np.asarray(image.convert("L"), dtype=np.float64)
    laplacian = (
        grey[:-2, 1:-1] + grey[2:, 1:-1] + grey[1:-1, :-2] + grey[1:-1, 2:] - 4 * grey[1:-1, 1:-1]
    )
    return round(float(laplacian.var()), 1), round(float(grey.mean()), 1)


def quality_problem(sharpness: float, brightness: float, rules: dict[str, Any]) -> str:
    """Say why the photo cannot be inspected, or return "" when it can."""
    limits = rules["photo_quality"]
    problems = []
    if sharpness < limits["min_sharpness"]:
        problems.append(f"out of focus (sharpness {sharpness:.1f} < {limits['min_sharpness']})")
    if brightness < limits["min_brightness"]:
        problems.append(f"too dark (brightness {brightness:.1f} < {limits['min_brightness']})")
    return "; ".join(problems)


# --- Count ------------------------------------------------------------------------------------


def _center(box: Box) -> tuple[float, float]:
    return box[0] + box[2] / 2, box[1] + box[3] / 2


def _iou(first: Box, second: Box) -> float:
    left, top = max(first[0], second[0]), max(first[1], second[1])
    right = min(first[0] + first[2], second[0] + second[2])
    bottom = min(first[1] + first[3], second[1] + second[3])
    inter = max(0.0, right - left) * max(0.0, bottom - top)
    union = first[2] * first[3] + second[2] * second[3] - inter
    return inter / union if union > 0 else 0.0


def _contains(outer: Box, point: tuple[float, float]) -> bool:
    x, y = point
    return outer[0] <= x <= outer[0] + outer[2] and outer[1] <= y <= outer[1] + outer[3]


def same_object(first: Box, second: Box, iou: float) -> bool:
    """Treat two boxes as one object when they overlap enough or one holds the other's centre."""
    return (
        _iou(first, second) >= iou
        or _contains(first, _center(second))
        or _contains(second, _center(first))
    )


def inside_part(part: Box, point: tuple[float, float], shape: str, margin: float) -> bool:
    """Check whether `point` is on the part, with the part box grown by `margin`.

    A round part is the ellipse its box encloses, so the corners of a flange's box don't count.
    """
    cx, cy = _center(part)
    half_w, half_h = part[2] / 2 * (1 + margin), part[3] / 2 * (1 + margin)
    if half_w <= 0 or half_h <= 0:
        return False
    dx, dy = (point[0] - cx) / half_w, (point[1] - cy) / half_h
    if shape == "round":
        return dx * dx + dy * dy <= 1.0
    return abs(dx) <= 1.0 and abs(dy) <= 1.0


def clock_position(part: Box, point: tuple[float, float]) -> str:
    """Give `point`'s place on the part as a clock position in the photo (12 is up)."""
    cx, cy = _center(part)
    angle = math.degrees(math.atan2(point[0] - cx, cy - point[1])) % 360
    hour = round(angle / 30) % 12
    return f"{hour or 12} o'clock"


def merge(boxes: list[dict[str, Any]], iou: float) -> tuple[list[dict[str, Any]], list[Any]]:
    """Keep the highest-scoring box for each object and return the duplicates separately."""
    kept: list[dict[str, Any]] = []
    merged: list[Any] = []
    for box in sorted(boxes, key=lambda item: item["score"], reverse=True):
        if any(same_object(box["bbox"], other["bbox"], iou) for other in kept):
            merged.append(box)
        else:
            kept.append(box)
    return kept, merged


def decide(
    expected: int, sure: int, unsure: int, holes: list[str], conflicts: tuple[str, ...] = ()
) -> tuple[str, str]:
    """Decide all_bolts_seen, defect or inspector for one part, with the reason.

    `holes` holds the clock position of each empty hole seen, `conflicts` each position where a
    sure bolt and a sure empty hole were both seen. A conflict always goes to the inspector:
    which score is higher doesn't say which reading is right. A short count is a defect only
    when there is an empty hole for every missing bolt. A bolt we simply can't see (covered,
    out of frame, washed out by glare) sends the photo to the inspector instead.
    """
    low, high = sure, sure + unsure
    where = f"; empty hole at {', '.join(holes)}" if holes else ""
    if conflicts:
        return INSPECTOR, (
            f"a bolt and an empty hole were both seen at {', '.join(conflicts)} "
            f"({low} sure bolt(s) of {expected}){where}: check that position"
        )
    if low == high == expected:
        if holes:
            return (
                INSPECTOR,
                f"{low} of {expected} bolts counted, but an empty hole was seen{where}",
            )
        return ALL_BOLTS_SEEN, f"{low} of {expected} bolts seen"
    if low > expected:
        return INSPECTOR, (
            f"{low} bolts counted on a {expected}-bolt assembly: wrong assembly or a double count"
        )
    if high < expected:
        missing = expected - high
        if len(holes) >= missing:
            return (
                DEFECT,
                f"{missing} bolt(s) missing: {high} of {expected} found{where}",
            )
        return INSPECTOR, (
            f"{missing} bolt(s) not seen ({high} of {expected} found) and only "
            f"{len(holes)} empty hole(s) to show it: covered, out of frame or missing{where}"
        )
    return INSPECTOR, (
        f"{low} sure and {unsure} unsure bolt(s) for {expected} expected: "
        f"the count could be {low} to {high}{where}"
    )


def inspect(
    objects: list[dict[str, Any]], assembly_name: str, rules: dict[str, Any]
) -> dict[str, Any]:
    """Count the bolts on the part and decide all_bolts_seen, defect or inspector, with a reason."""
    assembly = rules["assemblies"][assembly_name]
    detection, limits = rules["detection"], rules["thresholds"]
    expected = int(assembly["expected_bolts"])
    boxes = [
        {"label": o["label"], "score": float(o["score"]), "bbox": [float(v) for v in o["bbox"]]}
        for o in objects
    ]
    notes: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "assembly": assembly_name,
        "expected_bolts": expected,
        "bolts_sure": 0,
        "bolts_unsure": 0,
        "empty_holes": 0,
        "missing_at": "",
        "conflict_at": "",
        "ignored_outside_part": 0,
        "detections": notes,
    }

    def ignore(items: list[dict[str, Any]], why: str) -> None:
        notes.extend({**box, "use": "ignored", "why": why} for box in items)

    parts = sorted(
        (box for box in boxes if box["label"] == assembly["part_prompt"]),
        key=lambda box: box["score"],
        reverse=True,
    )
    if not parts or parts[0]["score"] < limits["min_part_score"]:
        best = f"{parts[0]['score']:.2f}" if parts else "none"
        ignore(boxes, "no part found")
        reason = (
            f"no {assembly['part_prompt']} found (best score {best}, "
            f"needs {limits['min_part_score']:.2f}); check the photo"
        )
        return {**result, "decision": INSPECTOR, "reason": reason}
    part = parts[0]
    notes.append({**part, "use": "part", "why": ""})
    ignore(parts[1:], "lower-scoring part box")
    side = min(part["bbox"][2], part["bbox"][3])

    def candidates(label: str, min_score: float) -> list[dict[str, Any]]:
        """Keep the boxes of one kind that score enough, are bolt-sized and sit on the part."""
        kept = []
        for box in (b for b in boxes if b["label"] == label):
            size = max(box["bbox"][2], box["bbox"][3]) / side if side > 0 else 0.0
            if box["score"] < min_score:
                ignore([box], f"score below {min_score:.2f}")
            elif not limits["min_bolt_size"] <= size <= limits["max_bolt_size"]:
                ignore([box], f"size {size:.2f} of the part")
            elif not inside_part(
                part["bbox"], _center(box["bbox"]), assembly["shape"], limits["part_margin"]
            ):
                ignore([box], "outside the part")
                if label == detection["bolt_prompt"]:
                    result["ignored_outside_part"] += 1
            else:
                kept.append(box)
        return kept

    bolts, merged = merge(
        candidates(detection["bolt_prompt"], limits["bolt_unsure"]), detection["dedupe_iou"]
    )
    ignore(merged, "same bolt as a higher-scoring box")
    holes, merged = merge(
        candidates(detection["hole_prompt"], limits["hole_sure"]), detection["dedupe_iou"]
    )
    ignore(merged, "same hole as a higher-scoring box")
    # SAM 3 can box one spot as both a bolt and a hole (a socket-head screw, a loose bolt lying
    # over an empty hole). Every hole here already scores at least hole_sure. A sure bolt on it
    # is a conflict for the inspector, whatever the scores. An unsure bolt on it was only a
    # maybe, so the hole stands and the unsure bolt is dropped.
    conflicted: list[dict[str, Any]] = []
    for hole in list(holes):
        rivals = [b for b in bolts if same_object(b["bbox"], hole["bbox"], detection["dedupe_iou"])]
        if any(bolt["score"] >= limits["bolt_sure"] for bolt in rivals):
            holes.remove(hole)
            conflicted.append(hole)
        else:
            for bolt in rivals:
                bolts.remove(bolt)
            ignore(rivals, "unsure bolt on a sure empty hole")

    sure = [box for box in bolts if box["score"] >= limits["bolt_sure"]]
    unsure = [box for box in bolts if box["score"] < limits["bolt_sure"]]
    notes.extend({**box, "use": "bolt", "why": ""} for box in sure)
    notes.extend({**box, "use": "unsure bolt", "why": "score below bolt_sure"} for box in unsure)
    notes.extend({**box, "use": "empty hole", "why": ""} for box in holes)
    notes.extend(
        {**box, "use": "conflict", "why": "a sure bolt was also seen here"} for box in conflicted
    )

    def clock(boxes: list[dict[str, Any]]) -> list[str]:
        return sorted(
            (clock_position(part["bbox"], _center(box["bbox"])) for box in boxes),
            key=lambda text: int(text.split()[0]),
        )

    positions, conflicts = clock(holes), clock(conflicted)
    decision, reason = decide(expected, len(sure), len(unsure), positions, tuple(conflicts))
    return {
        **result,
        "bolts_sure": len(sure),
        "bolts_unsure": len(unsure),
        "empty_holes": len(holes),
        "missing_at": "; ".join(positions),
        "conflict_at": "; ".join(conflicts),
        "decision": decision,
        "reason": reason,
    }


# --- Report -----------------------------------------------------------------------------------

COLORS = {
    "part": (60, 140, 255),
    "bolt": (40, 200, 80),
    "unsure bolt": (250, 200, 0),
    "empty hole": (240, 40, 40),
    "conflict": (230, 60, 230),
    "ignored": (150, 150, 150),
}


def annotate(image: Image.Image, row: dict[str, Any]) -> Image.Image:
    """Draw the boxes on the photo, coloured by how each was used.

    Part blue, bolt green, unsure bolt yellow, empty hole red, a hole a sure bolt also covers
    magenta, and grey for boxes ignored because they lie outside the part. Merged duplicates and
    other ignored boxes are not drawn.
    """
    out = image.convert("RGB")
    draw = ImageDraw.Draw(out)
    for note in row["detections"]:
        if note["use"] == "ignored" and note["why"] != "outside the part":
            continue
        x, y, width, height = note["bbox"]
        draw.rectangle((x, y, x + width, y + height), outline=COLORS[note["use"]], width=3)
    draw.rectangle((0, 0, out.width, 22), fill=(0, 0, 0))
    draw.text(
        (6, 5), f"{row['photo']}  {row['decision'].upper()}  {row['reason']}", fill=(255, 255, 255)
    )
    return out


# --- SIE call failures ------------------------------------------------------------------------


class SIEStopped(RuntimeError):
    """SIE refused the call for a reason a retry won't fix, or no call succeeded; the run
    stops without outputs."""


def is_fatal(error: SIEError) -> bool:
    """A refusal that fails every call the same way: a 4xx (bad key 401, forbidden 403, model
    not found 404, no credits or spend limit 402, inactive account) or a model the server
    marked as permanently unable to load. A RequestError with no HTTP status or a status below
    400 is a reply the SDK couldn't parse, and is fatal too. A rate limit, an over-long input, a
    timeout, a lost connection, a model still loading and a 5xx are transient or per-photo:
    those photos go to the inspector."""
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
    """The error for a per-photo reason or the summary line, its message cut short."""
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


def every_call_failed_advice(error: SIEError) -> str:
    """What to check when every SIE call failed, judged by the first failure.

    A lost connection points at SIE_BASE_URL and the network. Any other transient error (a
    timeout, a model still loading, a 5xx, a rate limit) more likely means the model is still
    starting or SIE is busy, so the advice is to wait and run again.
    """
    if isinstance(error, SIEConnectionError):
        return "Check SIE_BASE_URL and your network."
    return "The model may still be starting or SIE is busy; run again in a few minutes."


def stop_on_refusal[T](work: Callable[[], T]) -> T:
    """Run the work; turn SIEStopped into a clean exit. Any other error keeps its traceback."""
    try:
        return work()
    except SIEStopped as error:
        raise SystemExit(f"Stopped: {error}") from None


# --- Run --------------------------------------------------------------------------------------


def run(
    client: SIEClient,
    rules: dict[str, Any],
    output_dir: Path | None,
    sprite_path: Path = SPRITE_PATH,
    photos_path: Path = PHOTOS_PATH,
) -> list[dict[str, Any]]:
    """Inspect every photo in `photos_path`, then write the outputs.

    A call that fails for a reason that may pass (a lost connection, a timeout, a server error,
    a rate limit, a reply with no `objects` list) sends that photo to the inspector, and the
    run still writes its outputs. Two cases stop the run with SIEStopped before anything is
    written: a refusal every photo would repeat (see `is_fatal`), and a run where no call
    succeeded at all. An error that isn't an SIE error is a bug in this code and is raised as
    is.
    """
    model = rules["models"]["detect"]
    sprite = open_photo(sprite_path)
    rows: list[dict[str, Any]] = []
    images: dict[str, Image.Image] = {}
    failures: list[SIEError] = []
    calls = 0
    for photo in load_photos(photos_path):
        image = images[photo.photo] = tile(sprite, photo)
        sharpness, brightness = photo_quality(image)
        quality = {"sharpness": sharpness, "brightness": brightness}
        head = {"photo": photo.photo, "work_order": photo.work_order}
        if problem := quality_problem(sharpness, brightness, rules):
            # Don't spend an SIE call on a photo nobody could inspect; ask for a retake.
            row = {
                **head,
                **inspect([], photo.assembly, rules),
                "decision": INSPECTOR,
                "reason": f"photo {problem}: retake it before counting",
            }
        else:
            calls += 1
            try:
                objects = detect(client, photo, image, rules)
            except SIEError as error:
                if is_fatal(error):
                    raise SIEStopped(stop_message(error, model)) from error
                failures.append(error)
                row = {
                    **head,
                    **inspect([], photo.assembly, rules),
                    "decision": INSPECTOR,
                    "reason": f"not inspected: the SIE call failed: {short(error)}",
                }
            else:
                row = {**head, **inspect(objects, photo.assembly, rules)}
        row.update(quality)
        rows.append(row)
    if failures and len(failures) == calls:
        raise SIEStopped(
            f"Every SIE call failed ({calls} of {calls} to {model}), so the run stops: "
            f"{describe(failures[0])}: {failures[0]}. {every_call_failed_advice(failures[0])} "
            "No outputs were written."
        ) from failures[0]
    if failures:
        n = len(failures)
        print(
            f"{n} of {calls} SIE calls failed; their photos go to the inspector. "
            f"First: {short(failures[0])}"
        )
    if output_dir is not None:
        write_outputs(output_dir, rows, images)
    return rows


def csv_safe(value: Any) -> str:
    """A leading ' on a cell a spreadsheet would run as a formula; anything else as written."""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(
            {key: csv_safe(row[key]) for key in REPORT_FIELDS if key in row} for row in rows
        )


def write_outputs(
    output_dir: Path, rows: list[dict[str, Any]], images: dict[str, Image.Image]
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "inspections.csv", rows)
    _write_csv(output_dir / "defects.csv", [r for r in rows if r["decision"] == DEFECT])
    _write_csv(output_dir / "inspector.csv", [r for r in rows if r["decision"] == INSPECTOR])
    (output_dir / "detections.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    annotated = output_dir / "annotated"
    annotated.mkdir(parents=True, exist_ok=True)
    for row in rows:
        annotate(images[row["photo"]], row).save(annotated / f"{row['photo']}.jpg", quality=85)


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rules = load_rules()
    with make_client() as client:
        rows = stop_on_refusal(lambda: run(client, rules, output))
    for decision in DECISIONS:
        group = [row for row in rows if row["decision"] == decision]
        print(f"\n{decision.upper()} ({len(group)})")
        for row in group:
            where = f"{row['photo']:<8} {row['work_order']:<8} {row['assembly']:<14}"
            print(f"  {where} {row['reason']}")
    print("\nall_bolts_seen = a bolt is visible in every expected position; not torque or fit")
    print(f"reports: {output}/inspections.csv, defects.csv, inspector.csv, annotated/")


if __name__ == "__main__":
    main()
