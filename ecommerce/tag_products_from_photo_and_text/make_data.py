"""Build the PIM seed in data/ from Amazon Berkeley Objects (ABO).

ABO (https://amazon-berkeley-objects.s3.amazonaws.com/index.html) is licensed CC BY 4.0,
credit Amazon.com. Each entry in SOURCE is one ABO product listing: the listing ID we give it,
its marketplace (ABO's `domain_name`), its ABO item ID and the ABO image used as its main photo.
Everything else comes from ABO's listings metadata as ABO has it, in English: the title
(`item_name`), `product_type`, `color` (the seller's value and ABO's standardized value),
`material` (ABO's `fabric_type` where `material` is empty), `product_description` and every
`bullet_point`, in ABO's order. Nothing in it was corrected by hand.

The photos come from ABO's original image set, shrunk so the longest side is 512 pixels and
saved as JPEG without any metadata.

Writes:
    data/pim_listings.csv   one row per listing: its current fields, title and photo file
    data/pim_bullets.csv    one row per bullet point
    data/photos/<id>.jpg    the main photo of each listing

It reads all 16 ABO listings files (about 88 MB, streamed, nothing saved) and 18 photos.

    uv run make_data.py
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import urllib.request
from pathlib import Path
from typing import Any

from PIL import Image

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LISTINGS_CSV = DATA_DIR / "pim_listings.csv"
BULLETS_CSV = DATA_DIR / "pim_bullets.csv"
PHOTOS_DIR = DATA_DIR / "photos"
ABO = "https://amazon-berkeley-objects.s3.amazonaws.com/"
LISTINGS_FILES = [f"listings/metadata/listings_{digit}.json.gz" for digit in "0123456789abcdef"]
MAX_EDGE = 512

# One line per listing: "listing_id|domain_name|ABO item_id|ABO image_id|original-image path".
SOURCE = """
P01|amazon.com|B01J24H2K0|81+ADtxHDkL|37/3794b141.jpg
P02|amazon.com|B07B4N29DG|81f4K+PGY-L|93/93b7ae5f.jpg
P03|amazon.com|B07JCDQWM6|71e51Rb1h4L|c4/c4c46850.jpg
P04|amazon.com|B073G7BVBP|81SgCHCMXxL|a5/a5ab1856.jpg
P05|amazon.com|B07QQMCLJL|81smC6p2-ML|20/20aa4a07.jpg
P06|amazon.com|B07CWQMKCC|91AXGmCOvmL|0b/0bd3e1ff.jpg
P07|amazon.com|B083YFL4D7|410N3XWTVEL|08/083a453d.jpg
P08|amazon.com|B071SHLFLM|A1paU8jPXoL|38/3867e9f6.jpg
P09|amazon.com|B076ZZQL24|71p09xIjT6L|4b/4b8f4a06.jpg
P10|amazon.com|B07B4DB65T|91tUvoPjrrL|ae/ae7fecce.jpg
P11|amazon.com|B07CP9YN5Y|81L-I9M1SpL|5f/5fde3231.jpg
P12|amazon.com|B073WGMTCC|81rVi6UdvIL|5e/5e7973a3.jpg
P13|amazon.com|B07CVD9T35|81ruA2WzAmL|c1/c1ab9976.jpg
P14|amazon.com|B08187DL7D|71oQrsd7wdL|b5/b582aa4b.jpg
P15|fresh.amazon.com|B07DDPQ1HZ|71i-lXR63DL|7f/7f72cfb8.jpg
P16|amazon.com|B075Z93NK8|81PTCrN-JkL|b8/b8bbb641.jpg
P17|amazon.com|B079TXJWT8|A1wa6WPO7UL|d0/d046c4f9.jpg
P18|amazon.com|B07M7NRPZR|71iAwmD05PL|85/85b05c7a.jpg
"""

LISTING_FIELDS = (
    "listing_id",
    "marketplace",
    "item_id",
    "title",
    "product_type",
    "color",
    "color_standardized",
    "material",
    "description",
    "photo",
    "abo_image_id",
)
BULLET_FIELDS = ("listing_id", "position", "text")


def source() -> list[dict[str, str]]:
    rows = []
    for line in SOURCE.strip().splitlines():
        listing_id, domain, item_id, image_id, path = line.split("|")
        rows.append(
            {
                "listing_id": listing_id,
                "marketplace": domain,
                "item_id": item_id,
                "abo_image_id": image_id,
                "path": path,
            }
        )
    return rows


def english(entries: list[dict[str, Any]] | None, tag: str) -> list[dict[str, Any]]:
    """The entries in the listing's English language tag, in ABO's order."""
    return [entry for entry in entries or [] if entry.get("language_tag") == tag]


def first_value(entries: list[dict[str, Any]] | None, tag: str) -> str:
    found = english(entries, tag)
    return str(found[0]["value"]) if found else ""


def read_abo(wanted: dict[tuple[str, str], dict[str, str]]) -> dict[tuple[str, str], Any]:
    """The ABO listing record for each (item_id, domain_name) in `wanted`."""
    found: dict[tuple[str, str], Any] = {}
    for name in LISTINGS_FILES:
        with (
            urllib.request.urlopen(ABO + name, timeout=120) as response,
            gzip.open(io.BytesIO(response.read()), "rt", encoding="utf-8") as lines,
        ):
            for line in lines:
                record = json.loads(line)
                key = (record["item_id"], record.get("domain_name", ""))
                if key in wanted:
                    found[key] = record
    missing = sorted(set(wanted) - set(found))
    if missing:
        raise SystemExit(f"not in the ABO listings: {missing}")
    return found


def listing_row(row: dict[str, str], record: Any) -> tuple[dict[str, str], list[str]]:
    """The PIM fields and the bullet points of one listing, from its ABO record."""
    titles = [entry for entry in record["item_name"] if entry["language_tag"].startswith("en")]
    tag = titles[0]["language_tag"]
    colors = english(record.get("color"), tag)
    standardized = (colors[0].get("standardized_values") or [""]) if colors else [""]
    images = [record.get("main_image_id"), *record.get("other_image_id", [])]
    if row["abo_image_id"] not in images:
        raise SystemExit(f"{row['listing_id']}: image {row['abo_image_id']} is not this listing's")
    material = first_value(record.get("material"), tag) or first_value(
        record.get("fabric_type"), tag
    )
    fields = {
        "listing_id": row["listing_id"],
        "marketplace": row["marketplace"],
        "item_id": row["item_id"],
        "title": str(titles[0]["value"]),
        "product_type": str(record["product_type"][0]["value"]),
        "color": str(colors[0]["value"]) if colors else "",
        "color_standardized": str(standardized[0]),
        "material": material,
        "description": first_value(record.get("product_description"), tag),
        "photo": f"{row['listing_id']}.jpg",
        "abo_image_id": row["abo_image_id"],
    }
    bullets = [str(entry["value"]) for entry in english(record.get("bullet_point"), tag)]
    return fields, bullets


def photo(path: str) -> bytes:
    """The ABO original photo, longest side shrunk to MAX_EDGE, as JPEG without metadata."""
    with urllib.request.urlopen(ABO + "images/original/" + path, timeout=120) as response:
        image = Image.open(io.BytesIO(response.read())).convert("RGB")
    image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88, optimize=True)
    return buffer.getvalue()


def main() -> None:
    rows = source()
    wanted = {(row["item_id"], row["marketplace"]): row for row in rows}
    records = read_abo(wanted)
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    listings: list[dict[str, str]] = []
    bullets: list[dict[str, Any]] = []
    for row in rows:
        fields, texts = listing_row(row, records[(row["item_id"], row["marketplace"])])
        listings.append(fields)
        bullets += [
            {"listing_id": row["listing_id"], "position": position, "text": text}
            for position, text in enumerate(texts, start=1)
        ]
        (PHOTOS_DIR / fields["photo"]).write_bytes(photo(row["path"]))
    for path, names, selected in (
        (LISTINGS_CSV, LISTING_FIELDS, listings),
        (BULLETS_CSV, BULLET_FIELDS, bullets),
    ):
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=names)
            writer.writeheader()
            writer.writerows(selected)
    print(f"wrote {LISTINGS_CSV}, {BULLETS_CSV} and {len(rows)} photos in {PHOTOS_DIR}")


if __name__ == "__main__":
    main()
