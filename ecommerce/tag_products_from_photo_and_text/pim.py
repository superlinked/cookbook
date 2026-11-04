"""The PIM lookup: a listing's current fields, its text and its main photo, by listing ID.

This module stands in for your product information management system (PIM) or listing
editor: `get_listing` is the GET on its product endpoint that returns one listing with its
attributes, bullet points and image, and `flagged_batch` is the saved search or export that
lists the listings waiting for a clean-up. Here both read two CSV seeds in data/, loaded into
an in-memory sqlite3 database, so you can read the seed in any spreadsheet and swap the queries
for your PIM's API.

The lookup is by exact listing ID. A listing ID the PIM doesn't have comes back as None; the
recipe never guesses a listing from its title.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
# The listing's own columns, in the order of the Listing fields below.
LISTING_COLUMNS = (
    "listing_id",
    "marketplace",
    "item_id",
    "title",
    "product_type",
    "color",
    "color_standardized",
    "material",
    "description",
)


@dataclass(frozen=True)
class Listing:
    """One listing as the PIM holds it. Empty strings are empty fields."""

    listing_id: str
    marketplace: str
    item_id: str
    title: str
    product_type: str
    color: str  # the seller's color, free text
    color_standardized: str  # the catalog's standardized color, the one the filters use
    material: str
    description: str
    bullets: tuple[str, ...]
    photo: Path


def connect(data_dir: Path = DATA_DIR) -> sqlite3.Connection:
    """Load the PIM seed (pim_listings.csv and pim_bullets.csv) into an in-memory database."""
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE listings (listing_id TEXT PRIMARY KEY, marketplace TEXT, item_id TEXT, "
        "title TEXT, product_type TEXT, color TEXT, color_standardized TEXT, material TEXT, "
        "description TEXT, photo TEXT, abo_image_id TEXT)"
    )
    db.execute(
        "CREATE TABLE bullets (listing_id TEXT, position INTEGER, text TEXT, "
        "PRIMARY KEY (listing_id, position))"
    )
    for table, name in (("listings", "pim_listings.csv"), ("bullets", "pim_bullets.csv")):
        with (data_dir / name).open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        if rows:
            columns = list(rows[0])
            db.executemany(
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                [[row[column] for column in columns] for row in rows],
            )
    db.execute("PRAGMA query_only = ON")
    return db


def flagged_batch(db: sqlite3.Connection) -> list[str]:
    """The listing IDs waiting for a clean-up, in order."""
    return [row[0] for row in db.execute("SELECT listing_id FROM listings ORDER BY listing_id")]


def get_listing(
    db: sqlite3.Connection, listing_id: str, data_dir: Path = DATA_DIR
) -> Listing | None:
    """One listing with its bullet points and main photo, or None when the PIM has no such ID.

    A photo name that leads out of data/photos/ or names a missing file raises an error, so a
    bad seed row fails before any SIE call.
    """
    row = db.execute(
        f"SELECT {', '.join(LISTING_COLUMNS)}, photo FROM listings WHERE listing_id = ?",
        (listing_id,),
    ).fetchone()
    if row is None:
        return None
    photos = data_dir / "photos"
    name = row[-1]
    if Path(name).name != name or not (photos / name).is_file():
        raise ValueError(f"{listing_id}: no photo {name!r} in {photos}")
    bullets = tuple(
        text
        for (text,) in db.execute(
            "SELECT text FROM bullets WHERE listing_id = ? ORDER BY position", (listing_id,)
        )
    )
    fields = dict(zip(LISTING_COLUMNS, row[:-1], strict=True))
    return Listing(**fields, bullets=bullets, photo=photos / name)
