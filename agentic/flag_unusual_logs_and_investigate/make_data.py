"""Write the synthetic day of logs, last week's known patterns and the deploy log in data/.

    uv run make_data.py

Everything is made up: the shop, its services, the customers and their email addresses. The same
seed gives the same files byte for byte. data/runbooks.md is written by hand, not here.

The day is 5 Nov 2026 (UTC). Four things happen in it, on top of normal traffic:
- 10:40 a payments deploy breaks card decryption: two new error lines, until a rollback at 11:20.
- 13:00 an inventory deploy adds a harmless new line about warming a cache.
- 16:15 a config change makes a familiar auth warning about 50 times as common, until 17:05.
- 12:00 to 20:00 checkout slowly gets slower, with no new line and no change in volume.
At 09:00 a frontend deploy also changes how the access log is worded, which is no fault at all.
"""

from __future__ import annotations

import csv
import gzip
import io
import math
import random
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from log_patterns import pattern_of

HERE = Path(__file__).parent
DATA = HERE / "data"
DAY = datetime(2026, 11, 5, tzinfo=UTC)
WINDOW = timedelta(minutes=15)
NAMES = ("ana.ruiz", "ben.okafor", "chen.wei", "dara.nolan", "eli.haddad", "farah.khan", "gus.berg")
REASONS = ("insufficient funds", "expired card", "do not honor")
REGIONS = ("eu-west-1", "eu-central-1", "us-east-2")


def at(hour: int, minute: int = 0) -> datetime:
    return DAY + timedelta(hours=hour, minutes=minute)


def busy(when: datetime) -> float:
    """How busy the shop is at this time of day: quiet at night, busiest mid-afternoon."""
    hour = (when - when.replace(hour=0, minute=0, second=0)).total_seconds() / 3600
    return 0.4 + 1.2 * max(0.0, math.sin(math.pi * (hour - 6) / 16)) if 6 <= hour <= 22 else 0.4


@dataclass(frozen=True)
class Line:
    service: str
    level: str
    write: Callable[[random.Random, datetime], str]
    per_window: float  # lines in a 15 minute window at an ordinary level of traffic
    start: datetime = DAY
    end: datetime = DAY + timedelta(days=1)
    follows_traffic: bool = True


def email(rng: random.Random) -> str:
    return f"{rng.choice(NAMES)}{rng.randint(1, 99)}@example.com"


def order(rng: random.Random) -> int:
    return rng.randint(10000, 99999)


def checkout_ms(rng: random.Random, when: datetime) -> int:
    """Checkout gets slowly slower from noon to 8 p.m.: 120 ms to about 900 ms."""
    drift = min(max((when - at(12)) / (at(20) - at(12)), 0.0), 1.0) if when >= at(12) else 0.0
    return int(rng.gauss(120 + 780 * drift, 20 + 60 * drift))


def products(rng: random.Random, when: datetime) -> str:
    ms = rng.randint(8, 90)
    if when >= at(9):
        return f"GET /api/products returned 200 after {ms} ms"
    return f"GET /api/products 200 in {ms} ms"


def cart(rng: random.Random, when: datetime) -> str:
    ms = rng.randint(10, 120)
    if when >= at(9):
        return f"GET /api/cart returned 200 after {ms} ms"
    return f"GET /api/cart 200 in {ms} ms"


NORMAL = (
    Line("frontend", "INFO", products, 300),
    Line("frontend", "INFO", cart, 150),
    Line(
        "frontend",
        "WARN",
        lambda r, w: (
            f"GET /api/products 429 rate limited client 10.0.{r.randint(0, 9)}.{r.randint(1, 250)}"
        ),
        4,
    ),
    Line(
        "checkout",
        "INFO",
        lambda r, w: f"order {order(r)} placed, request completed in {checkout_ms(r, w)} ms",
        80,
    ),
    Line(
        "checkout",
        "ERROR",
        lambda r, w: f"payment step failed for order {order(r)}: card declined",
        3,
    ),
    Line(
        "payments",
        "INFO",
        lambda r, w: (
            f"charge ok for order {order(r)} amount {r.randint(5, 400)}.{r.randint(0, 99):02d} EUR"
        ),
        80,
    ),
    Line(
        "payments",
        "WARN",
        lambda r, w: f"card declined for order {order(r)}: {r.choice(REASONS)}",
        3,
    ),
    Line("auth", "INFO", lambda r, w: f"token issued for user {email(r)}", 120),
    Line(
        "auth",
        "WARN",
        lambda r, w: f"token refresh failed for user {email(r)}: upstream timeout",
        2,
    ),
    Line(
        "inventory",
        "INFO",
        lambda r, w: f"stock reserved sku SKU-{r.randint(1000, 9999)} qty {r.randint(1, 5)}",
        90,
    ),
    Line(
        "inventory",
        "WARN",
        lambda r, w: f"stock low for sku SKU-{r.randint(1000, 9999)}: {r.randint(0, 4)} left",
        5,
    ),
    Line(
        "search",
        "INFO",
        lambda r, w: f"query served in {r.randint(5, 60)} ms, {r.randint(0, 200)} hits",
        120,
    ),
    Line("search", "WARN", lambda r, w: f"slow query took {r.randint(800, 3000)} ms", 2),
    Line(
        "worker",
        "INFO",
        lambda r, w: f"job {r.getrandbits(32):08x} finished in {r.randint(50, 900)} ms",
        40,
    ),
    Line(
        "worker",
        "ERROR",
        lambda r, w: f"job {r.getrandbits(32):08x} failed: retry {r.randint(1, 3)} of 3",
        1,
    ),
)

TODAY = (
    # 10:40 payments deploy: decryption fails, and checkout's payment step fails with it.
    Line(
        "payments",
        "ERROR",
        lambda r, w: f"card token decrypt failed for order {order(r)}: key version 7 not found",
        140,
        at(10, 42),
        at(11, 20),
    ),
    Line(
        "checkout",
        "ERROR",
        lambda r, w: f"payment step failed for order {order(r)}: upstream 502",
        130,
        at(10, 42),
        at(11, 20),
    ),
    # 13:00 inventory deploy: a new, harmless line each time an instance warms its cache.
    Line(
        "inventory",
        "INFO",
        lambda r, w: (
            f"stock cache warmup finished for region {r.choice(REGIONS)} "
            f"in {r.randint(300, 2000)} ms"
        ),
        2,
        at(13, 2),
        follows_traffic=False,
    ),
    # 16:15 auth config change: a familiar warning, about 50 times as often, until 17:05.
    Line(
        "auth",
        "WARN",
        lambda r, w: f"token refresh failed for user {email(r)}: upstream timeout",
        100,
        at(16, 16),
        at(17, 5),
        follows_traffic=False,
    ),
)

DEPLOYS = (
    (at(8, 30), "search", "v1.9.2", "Tune the result cache size"),
    (at(9, 0), "frontend", "v5.1.0", "Switch to the new access log format"),
    (at(10, 40), "payments", "v2.14.0", "Move card token decryption to the new key service"),
    (at(11, 20), "payments", "v2.13.4", "Roll back v2.14.0"),
    (at(13, 0), "inventory", "v3.2.0", "Warm the stock cache when an instance starts"),
    (at(14, 30), "worker", "v4.0.1", "Upgrade the job queue client"),
    (
        at(16, 15),
        "auth-config",
        "c-311",
        "Lower the identity provider call timeout from 5000 ms to 500 ms",
    ),
    (at(17, 5), "auth-config", "c-312", "Revert the identity provider call timeout to 5000 ms"),
    (at(19, 0), "frontend", "v5.1.1", "Fix a footer link"),
)


def lines_for(day: datetime, kinds: tuple[Line, ...], rng: random.Random) -> list[list[str]]:
    """Every line of one day, in time order: [time, service, level, message]."""
    rows: list[tuple[datetime, str, str, str]] = []
    window = day
    while window < day + timedelta(days=1):
        for kind in kinds:
            # Today's kinds run on today's clock; on another day they don't happen.
            start = day + (kind.start - DAY)
            end = day + (kind.end - DAY)
            if not (start < window + WINDOW and window < end):
                continue
            mean = kind.per_window * (busy(window) if kind.follows_traffic else 1.0)
            for _ in range(poisson(rng, mean)):
                when = window + timedelta(seconds=rng.uniform(0, WINDOW.total_seconds()))
                if start <= when < end:
                    rows.append((when, kind.service, kind.level, kind.write(rng, when)))
        window += WINDOW
    rows.sort(key=lambda row: row[0])
    stamp = "%Y-%m-%dT%H:%M:%S"
    return [[when.strftime(stamp), service, level, text] for when, service, level, text in rows]


def poisson(rng: random.Random, mean: float) -> int:
    """A Poisson draw: the normal approximation for big means, Knuth's method for small ones."""
    if mean > 30:
        return max(0, round(rng.gauss(mean, math.sqrt(mean))))
    limit, count, product = math.exp(-mean), 0, rng.random()
    while product > limit:
        count += 1
        product *= rng.random()
    return count


def known_patterns(rows: list[list[str]], days: int) -> list[list[str]]:
    """Each pattern from last week and how many lines it had in an average 15 minute window."""
    counts = Counter(pattern_of(service, level, text) for _, service, level, text in rows)
    windows = days * 96
    return [[pattern, f"{count / windows:.2f}"] for pattern, count in sorted(counts.items())]


def write_csv(path: Path, header: list[str], rows: list[list[str]], zipped: bool = False) -> None:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    data = buffer.getvalue().encode("utf-8")
    if zipped:
        # mtime=0, so the same rows give the same bytes.
        with gzip.GzipFile(path, "wb", mtime=0) as file:
            file.write(data)
    else:
        path.write_bytes(data)


def main() -> None:
    DATA.mkdir(exist_ok=True)
    today = lines_for(DAY, NORMAL + TODAY, random.Random(20261105))
    write_csv(DATA / "logs.csv.gz", ["time", "service", "level", "message"], today, zipped=True)
    # Last week: seven ordinary days, before the frontend's new log format and before any of
    # today's changes.
    last_week: list[list[str]] = []
    rng = random.Random(20261029)
    for back in range(7, 0, -1):
        last_week += lines_for(DAY - timedelta(days=back), NORMAL, rng)
    write_csv(DATA / "known_patterns.csv", ["pattern", "per_15_min"], known_patterns(last_week, 7))
    deploys = [[when.strftime("%Y-%m-%dT%H:%M"), *rest] for when, *rest in DEPLOYS]
    write_csv(DATA / "deploys.csv", ["time", "service", "version", "change"], deploys)
    print(f"Wrote {len(today)} log lines, and {len(last_week)} lines' worth of known patterns.")


if __name__ == "__main__":
    main()
