"""Draw the synthetic inspection photos for the check_parts_in_assembly_photos recipe.

Writes data/inspection-photos.jpg (one sprite sheet, one tile per photo) and data/photos.csv
(photo id, work order, assembly, tile box, what was planted). Every photo is drawn with Pillow
and numpy from a fixed seed, so the same code always writes the same files. Pillow saves the
JPEG with no EXIF or XMP block, so there is no camera, GPS or author metadata to strip.

    uv run make_data.py
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
SPRITE_PATH = DATA_DIR / "inspection-photos.jpg"
PHOTOS_PATH = DATA_DIR / "photos.csv"

TILE = 640
COLUMNS = 4


@dataclass(frozen=True)
class Photo:
    photo_id: str
    work_order: str
    assembly: str
    planted: str
    missing: tuple[int, ...] = ()  # bolt positions left empty (0 = first position)
    loose_bolt: bool = False  # a spare bolt lying on the bench, outside the part
    blur: float = 0.0  # Gaussian blur radius: an out-of-focus photo
    exposure: float = 1.0  # below 1: an underexposed photo
    rotation: float = 0.0  # degrees the bolt pattern is turned in the frame
    rag: bool = False  # a shop rag left over one bolt position
    loose_on_part: int | None = None  # a loose bolt lying on the part across this position
    socket_heads: tuple[int, ...] = ()  # positions fitted with a socket-head cap screw
    seed: int = 0


PHOTOS = (
    Photo("FL-101", "WO-4101", "pipe_flange_8", "complete: 8 of 8 bolts", seed=1, rotation=11.0),
    Photo(
        "FL-102",
        "WO-4102",
        "pipe_flange_8",
        "1 bolt missing",
        missing=(2,),
        seed=2,
        rotation=4.0,
    ),
    Photo(
        "FL-103",
        "WO-4103",
        "pipe_flange_8",
        "2 bolts missing",
        missing=(5, 6),
        seed=3,
        rotation=17.0,
    ),
    Photo(
        "FL-104",
        "WO-4104",
        "pipe_flange_8",
        "near miss: complete, plus a spare bolt on the bench",
        loose_bolt=True,
        seed=4,
        rotation=8.0,
    ),
    Photo(
        "FL-105",
        "WO-4105",
        "pipe_flange_8",
        "escalate: out of focus and underexposed",
        blur=9.0,
        exposure=0.45,
        seed=5,
        rotation=14.0,
    ),
    Photo(
        "FL-106",
        "WO-4106",
        "pipe_flange_8",
        "escalate: a rag covers one bolt position",
        rag=True,
        seed=8,
        rotation=2.0,
    ),
    Photo("CV-201", "WO-4201", "cover_plate_6", "complete: 6 of 6 bolts", seed=6),
    Photo("CV-202", "WO-4202", "cover_plate_6", "1 bolt missing", missing=(4,), seed=7),
    # Added after the first eight, so their tiles keep their place in the sheet.
    Photo(
        "FL-107",
        "WO-4107",
        "pipe_flange_8",
        "near miss: 1 bolt missing, a loose bolt lying on the flange across the empty hole",
        missing=(3,),
        loose_on_part=3,
        seed=9,
        rotation=6.0,
    ),
    Photo(
        "FL-108",
        "WO-4108",
        "pipe_flange_8",
        "near miss: complete, one position fitted with a socket-head cap screw",
        socket_heads=(1,),
        seed=10,
        rotation=9.0,
    ),
)


# --- Drawing ----------------------------------------------------------------------------------


def bench(rng: np.random.Generator) -> Image.Image:
    """A grey-green painted steel bench with grain and a soft vignette."""
    noise = rng.normal(0.0, 1.0, (TILE // 8, TILE // 8))
    coarse = np.array(Image.fromarray(noise.astype(np.float32)).resize((TILE, TILE)))
    fine = rng.normal(0.0, 6.0, (TILE, TILE))
    yy, xx = np.mgrid[0:TILE, 0:TILE]
    radius = np.hypot(xx - TILE / 2, yy - TILE / 2) / (TILE / 2)
    light = 1.0 - 0.35 * radius**2
    base = np.array([92.0, 100.0, 92.0])
    shade = (coarse * 8.0 + fine)[..., None]
    pixels = (base[None, None, :] + shade) * light[..., None]
    return Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8), "RGB")


def metal_disc(size: int, rng: np.random.Generator, tone: float) -> Image.Image:
    """A machined steel disc: radial gradient, turning marks and a light from the top left."""
    yy, xx = np.mgrid[0:size, 0:size]
    cx = cy = (size - 1) / 2
    radius = np.hypot(xx - cx, yy - cy)
    rings = 6.0 * np.sin(radius * 0.9) + rng.normal(0.0, 4.0, (size, size))
    light = 1.0 + 0.25 * ((cx - xx) + (cy - yy)) / size
    value = (tone + rings) * light
    rgb = np.stack([value * 0.97, value, value * 1.03], axis=-1)
    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB")


def hexagon(cx: float, cy: float, r: float, turn: float) -> list[tuple[float, float]]:
    return [
        (cx + r * math.cos(turn + k * math.pi / 3), cy + r * math.sin(turn + k * math.pi / 3))
        for k in range(6)
    ]


def draw_nut(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, turn: float) -> None:
    """A washer, a hex nut with shaded flats and the threaded stud end, seen from above."""
    draw.ellipse(
        (cx - r * 1.45 + 3, cy - r * 1.45 + 4, cx + r * 1.45 + 3, cy + r * 1.45 + 4),
        fill=(40, 40, 40),
    )  # shadow
    draw.ellipse(
        (cx - r * 1.45, cy - r * 1.45, cx + r * 1.45, cy + r * 1.45),
        fill=(176, 176, 170),
        outline=(90, 90, 88),
        width=2,
    )
    points = hexagon(cx, cy, r * 1.15, turn)
    for k in range(6):
        a, b = points[k], points[(k + 1) % 6]
        mid = math.atan2((a[1] + b[1]) / 2 - cy, (a[0] + b[0]) / 2 - cx)
        tone = int(120 + 70 * math.cos(mid + 2.4))
        draw.polygon([(cx, cy), a, b], fill=(tone, tone, tone - 4))
    draw.polygon(points, outline=(60, 60, 58))
    draw.ellipse(
        (cx - r * 0.95, cy - r * 0.95, cx + r * 0.95, cy + r * 0.95),
        outline=(105, 105, 100),
        width=2,
    )  # chamfer circle
    stud = r * 0.55
    draw.ellipse((cx - stud, cy - stud, cx + stud, cy + stud), fill=(150, 146, 136))
    for ring in (0.42, 0.30, 0.18):
        s = r * ring
        draw.ellipse((cx - s, cy - s, cx + s, cy + s), outline=(118, 114, 104), width=1)


def draw_hole(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float) -> None:
    """An empty bolt hole: dark bore with a bright machined edge."""
    draw.ellipse(
        (cx - r * 0.75, cy - r * 0.75, cx + r * 0.75, cy + r * 0.75),
        fill=(24, 24, 26),
        outline=(170, 170, 166),
        width=2,
    )
    draw.ellipse((cx - r * 0.45, cy - r * 0.35, cx + r * 0.55, cy + r * 0.6), fill=(12, 12, 14))


def draw_socket_head(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float) -> None:
    """A socket-head cap screw seen from above: a round knurled head with a dark hex socket."""
    draw.ellipse(
        (cx - r * 1.05 + 3, cy - r * 1.05 + 4, cx + r * 1.05 + 3, cy + r * 1.05 + 4),
        fill=(40, 40, 40),
    )  # shadow
    draw.ellipse(
        (cx - r * 1.05, cy - r * 1.05, cx + r * 1.05, cy + r * 1.05),
        fill=(70, 70, 72),
        outline=(150, 150, 146),
        width=2,
    )  # black-oxide head
    draw.polygon(hexagon(cx, cy, r * 0.5, 0.3), fill=(14, 14, 16), outline=(110, 110, 106))


def draw_loose_bolt(draw: ImageDraw.ImageDraw, x: float, y: float, length: float) -> None:
    """A hex bolt lying on its side: head, shank with threads."""
    head_w, shank_h = length * 0.22, length * 0.18
    draw.rectangle(
        (x + 4, y - shank_h / 2 + 5, x + length + 4, y + shank_h / 2 + 5), fill=(45, 48, 45)
    )
    draw.rectangle(
        (x, y - shank_h * 0.9, x + head_w, y + shank_h * 0.9),
        fill=(150, 150, 146),
        outline=(70, 70, 68),
        width=2,
    )
    draw.line((x, y, x + head_w, y), fill=(115, 115, 110), width=2)
    draw.rectangle(
        (x + head_w, y - shank_h / 2, x + length, y + shank_h / 2),
        fill=(168, 166, 158),
        outline=(90, 90, 86),
    )
    step = shank_h * 0.35
    t = x + head_w + length * 0.3
    while t < x + length - 2:
        draw.line((t, y - shank_h / 2, t - step, y + shank_h / 2), fill=(110, 108, 100), width=2)
        t += step


def draw_rag(image: Image.Image, cx: float, cy: float, rng: np.random.Generator) -> None:
    """A crumpled blue shop rag lying over the point (cx, cy): soft folds and a shadow."""
    angles = np.linspace(0.0, 2 * math.pi, 14, endpoint=False)
    radii = 70.0 + rng.uniform(-18.0, 18.0, angles.size)
    outline = [
        (cx + r * math.cos(a), cy + r * 0.8 * math.sin(a))
        for a, r in zip(angles, radii, strict=True)
    ]
    shadow = Image.new("L", image.size, 0)
    ImageDraw.Draw(shadow).polygon([(x + 8, y + 10) for x, y in outline], fill=170)
    image.paste((15, 15, 18), (0, 0), shadow.filter(ImageFilter.GaussianBlur(8)))
    cloth = Image.new("RGB", image.size, (58, 92, 150))
    folds = ImageDraw.Draw(cloth)
    for k in range(9):
        y = cy - 70 + k * 17 + rng.uniform(-4.0, 4.0)
        tone = (44, 74, 126) if k % 2 else (82, 118, 176)
        folds.line((cx - 90, y, cx + 90, y + rng.uniform(-20.0, 20.0)), fill=tone, width=7)
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).polygon(outline, fill=255)
    image.paste(cloth.filter(ImageFilter.GaussianBlur(3)), (0, 0), mask)


def bolt_positions(photo: Photo) -> list[tuple[float, float]]:
    if photo.assembly == "pipe_flange_8":
        cx = cy = TILE / 2
        turn = math.radians(photo.rotation)
        return [
            (
                cx + 205 * math.cos(turn + k * math.pi / 4),
                cy + 205 * math.sin(turn + k * math.pi / 4),
            )
            for k in range(8)
        ]
    # cover plate: three bolts along the top edge, three along the bottom
    return [(x, y) for y in (205.0, 435.0) for x in (175.0, 320.0, 465.0)]


def draw_part(image: Image.Image, photo: Photo, rng: np.random.Generator) -> None:
    draw = ImageDraw.Draw(image)
    if photo.assembly == "pipe_flange_8":
        size = 560
        left = (TILE - size) // 2
        shadow = Image.new("L", (TILE, TILE), 0)
        ImageDraw.Draw(shadow).ellipse(
            (left + 10, left + 14, left + size + 10, left + size + 14), fill=150
        )
        image.paste((20, 22, 20), (0, 0), shadow.filter(ImageFilter.GaussianBlur(12)))
        disc = metal_disc(size, rng, 150.0)
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        image.paste(disc, (left, left), mask)
        c = TILE / 2
        draw.ellipse((c - 150, c - 150, c + 150, c + 150), outline=(118, 118, 114), width=3)
        draw.ellipse(
            (c - 110, c - 110, c + 110, c + 110),
            fill=(30, 30, 32),
            outline=(185, 185, 180),
            width=4,
        )  # pipe bore
        draw.ellipse((c - 95, c - 80, c + 105, c + 110), fill=(16, 16, 18))
        radius = 30.0
    else:
        box = (85, 125, 555, 515)
        draw.rounded_rectangle(
            (box[0] + 10, box[1] + 14, box[2] + 10, box[3] + 14), 28, fill=(38, 40, 38)
        )
        plate = metal_disc(max(box[2] - box[0], box[3] - box[1]), rng, 140.0)
        plate = plate.crop((0, 0, box[2] - box[0], box[3] - box[1]))
        mask = Image.new("L", plate.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, plate.size[0] - 1, plate.size[1] - 1), 28, fill=255
        )
        image.paste(plate, (box[0], box[1]), mask)
        draw.rounded_rectangle(
            (box[0] + 50, 265, box[2] - 50, 375), 12, outline=(110, 110, 106), width=3
        )  # stamped rib
        radius = 28.0
    for number, (x, y) in enumerate(bolt_positions(photo)):
        if number in photo.missing:
            draw_hole(draw, x, y, radius)
        elif number in photo.socket_heads:
            draw_socket_head(draw, x, y, radius)
        else:
            draw_nut(draw, x, y, radius, math.radians(photo.rotation * 3 + number * 7))


def render(photo: Photo) -> Image.Image:
    rng = np.random.default_rng(photo.seed)
    image = bench(rng)
    draw_part(image, photo, rng)
    if photo.rag:
        draw_rag(image, *bolt_positions(photo)[0], rng)
    if photo.loose_bolt:
        draw_loose_bolt(ImageDraw.Draw(image), 470.0, 610.0, 140.0)
    if photo.loose_on_part is not None:
        # Lying across the empty hole, just below its center, so its top edge still shows.
        x, y = bolt_positions(photo)[photo.loose_on_part]
        draw_loose_bolt(ImageDraw.Draw(image), x - 60.0, y + 4.0, 140.0)
    if photo.blur:
        image = image.filter(ImageFilter.GaussianBlur(photo.blur))
    if photo.exposure != 1.0:
        pixels = np.asarray(image, dtype=np.float32) * photo.exposure
        image = Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8), "RGB")
    return image


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rows = math.ceil(len(PHOTOS) / COLUMNS)
    sprite = Image.new("RGB", (COLUMNS * TILE, rows * TILE), (255, 255, 255))
    records = []
    for index, photo in enumerate(PHOTOS):
        x, y = (index % COLUMNS) * TILE, (index // COLUMNS) * TILE
        sprite.paste(render(photo), (x, y))
        records.append(
            {
                "photo": photo.photo_id,
                "work_order": photo.work_order,
                "assembly": photo.assembly,
                "x": x,
                "y": y,
                "width": TILE,
                "height": TILE,
                "planted": photo.planted,
            }
        )
    sprite.save(SPRITE_PATH, quality=92, optimize=True)
    with PHOTOS_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    print(f"wrote {SPRITE_PATH} and {PHOTOS_PATH} ({len(PHOTOS)} photos)")


if __name__ == "__main__":
    main()
