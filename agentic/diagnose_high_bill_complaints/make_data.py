"""Rebuild the generated files in data/: accounts, bills, adjustments, hourly readings and photos.

The policy manual and the complaints are written by hand. Everything is synthetic, and Alder Valley
Water isn't a real utility. Run it with `uv run --group data make_data.py`.
"""

from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

DATA = Path(__file__).parent / "data"
MONTHS = [date(2026, month, 1) for month in range(4, 10)]
OLD, NEW = (18.00, 3.20, 4.80, 7.40), (19.50, 3.50, 5.20, 8.10)


def charge(gallons: int, start: date) -> str:
    base, tier1, tier2, tier3 = NEW if start >= date(2026, 7, 1) else OLD
    kgal = gallons / 1000
    volume = min(kgal, 6) * tier1 + max(min(kgal, 15) - 6, 0) * tier2 + max(kgal - 15, 0) * tier3
    return f"{base + volume:.2f}"


def days_in(month: date) -> int:
    following = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (following - month).days


# Automated meters: normal monthly gallons, and anything special about the month.
AUTOMATED: dict[str, dict[date, int]] = {
    "A-1002": {date(2026, 9, 1): 40000},
    "A-1003": {date(2026, 9, 1): 18000},
    "A-1004": {date(2026, 9, 1): 36000},
    "A-1005": {date(2026, 7, 1): 13000, date(2026, 8, 1): 14000, date(2026, 9, 1): 14000},
    "A-1007": {date(2026, 9, 1): 6200},
    "A-1008": {date(2026, 9, 1): 5500},
    "A-1009": {date(2026, 9, 1): 3000},
    "A-1010": {date(2026, 9, 1): 13000},
}
NORMAL = {
    "A-1002": 5000,
    "A-1003": 9000,
    "A-1004": 5500,
    "A-1005": 6000,
    "A-1007": 6000,
    "A-1008": 5500,
    "A-1009": 5000,
    "A-1010": 11000,
}
# Manually read meters: the serial, the last actual read and the three estimated months.
MANUAL: dict[str, dict[str, Any]] = {
    "A-1001": {"serial": "AV-204817", "first": 46000, "estimates": [6000, 6000, 55000]},
    "A-1006": {"serial": "AV-771230", "first": 31500, "estimates": [6000, 6000, 30000]},
}


def household(rng: np.random.Generator, days: int) -> np.ndarray:
    """Hourly use for a household: mornings and evenings, and nothing overnight."""
    shape = np.zeros(24)
    shape[[6, 7, 8]] = [2, 3, 2]
    shape[[17, 18, 19, 20, 21]] = [1, 2, 3, 2, 1]
    shape[[10, 12, 14]] = [0.5, 1, 0.5]
    return np.concatenate([shape * rng.uniform(0.6, 1.4, 24) for _ in range(days)])


def month_hours(rng: np.random.Generator, account: str, month: date, total: int) -> np.ndarray:
    """Hourly gallons for one account and month that add up to exactly `total`."""
    days = days_in(month)
    use = household(rng, days)
    extra = np.zeros(days * 24)
    if month == date(2026, 9, 1):
        if (
            account == "A-1002"
        ):  # continuous flow from the 12th; nobody home from the 5th to the 20th
            use[4 * 24 : 20 * 24] = 0
            extra[11 * 24 :] = 75
        if account == "A-1004":  # continuous flow from the 10th
            extra[9 * 24 :] = 62
        if account == "A-1003":  # four days of heavy flow, then it stops
            extra[13 * 24 : 17 * 24] = 90
    if account == "A-1005" and month >= date(2026, 7, 1):  # sprinklers at 5 a.m. every other day
        extra[5::48] = (total - NORMAL[account]) / (days / 2)
    remaining = total - extra.sum()
    hours = extra + use * (remaining / use.sum())
    rounded = np.round(hours, 1)
    rounded[np.argmax(use)] += round(total - rounded.sum(), 1)
    return cast(np.ndarray, rounded)


def photo(serial: str, reading: str, taken: str, seed: int, path: Path) -> None:
    """A photo of a meter's digital display: slightly tilted, soft and grainy."""
    rng = np.random.default_rng(seed)
    image = Image.new("RGB", (1000, 560), (70, 74, 70))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((70, 50, 930, 510), radius=30, fill=(28, 34, 38))
    draw.rectangle((120, 110, 880, 300), fill=(170, 190, 160))
    draw.text((150, 135), reading, fill=(20, 28, 20), font=ImageFont.load_default(size=120))
    draw.text((720, 200), "GAL", fill=(20, 28, 20), font=ImageFont.load_default(size=56))
    draw.text((150, 350), f"SERIAL {serial}", fill="white", font=ImageFont.load_default(size=64))
    draw.text((150, 440), f"PHOTO DATE {taken}", fill="white", font=ImageFont.load_default(size=36))
    tilted = image.rotate(2, fillcolor=(70, 74, 70)).filter(ImageFilter.GaussianBlur(1))
    pixels = np.asarray(tilted, dtype=np.float32) + rng.normal(0, 5, (560, 1000, 3))
    Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8)).save(path, quality=85)


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    rng = np.random.default_rng(2026)
    accounts: list[dict[str, object]] = []
    bills: list[dict[str, object]] = []
    readings: list[dict[str, object]] = []
    for account, specials in AUTOMATED.items():
        number = account[-4:]
        accounts.append(
            {
                "account_id": account,
                "meter_serial": f"AV-5{number}0",
                "meter_type": "automated",
                "service_status": "open",
            }
        )
        register = 100000 + int(number) * 3
        for month in MONTHS:
            total = specials.get(month, NORMAL[account])
            for hour, gallons in enumerate(month_hours(rng, account, month, total)):
                moment = month + timedelta(hours=hour)
                stamp = f"{moment:%Y-%m-%d}T{hour % 24:02d}"
                readings.append({"account_id": account, "hour": stamp, "gallons": f"{gallons:.1f}"})
            register += total
            bills.append(
                {
                    "account_id": account,
                    "period_start": month,
                    "read_type": "actual",
                    "gallons": total,
                    "end_read": register,
                    "amount": charge(total, month),
                }
            )
    for account, spec in MANUAL.items():
        accounts.append(
            {
                "account_id": account,
                "meter_serial": spec["serial"],
                "meter_type": "manual",
                "service_status": "open",
            }
        )
        register = int(spec["first"]) - 15000
        for month in MONTHS[:3]:
            register += 5000
            bills.append(
                {
                    "account_id": account,
                    "period_start": month,
                    "read_type": "actual",
                    "gallons": 5000,
                    "end_read": register,
                    "amount": charge(5000, month),
                }
            )
        for month, gallons in zip(MONTHS[3:], spec["estimates"], strict=True):
            register += gallons
            bills.append(
                {
                    "account_id": account,
                    "period_start": month,
                    "read_type": "estimated",
                    "gallons": gallons,
                    "end_read": register,
                    "amount": charge(gallons, month),
                }
            )
    accounts.sort(key=lambda row: str(row["account_id"]))
    bills.sort(key=lambda row: (str(row["account_id"]), str(row["period_start"])))
    write_csv(DATA / "accounts.csv", accounts)
    write_csv(DATA / "bills.csv", bills)
    write_csv(DATA / "hourly_readings.csv", readings)
    write_csv(
        DATA / "adjustments.csv",
        [
            {
                "account_id": "A-1002",
                "date": "2025-08-10",
                "type": "Excess Use Adjustment",
                "amount": "128.00",
            }
        ],
    )
    # The real reading on A-1001: the last actual read plus 5,000 gallons a month for three months.
    photo("AV-204817", "061000", "2026-10-07", 1, DATA / "C-2001-meter.jpg")
    # A-1006's photo has two digits of the serial swapped, so it isn't this account's meter.
    photo("AV-771203", "046500", "2026-10-08", 2, DATA / "C-2006-meter.jpg")


if __name__ == "__main__":
    main()
