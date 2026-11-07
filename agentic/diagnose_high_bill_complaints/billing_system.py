"""A stand-in for the utility's billing and meter data systems, backed by the CSV files in data/.

Replace these functions with calls to your own systems and nothing else changes. Amounts come back
as strings with two decimals, so they read the same way everywhere they're quoted.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

# The volume tiers in Schedule A: the upper limit of tiers 1 and 2, in gallons.
TIER_LIMITS = (6000, 15000)


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def months_between(earlier: date, later: date) -> int:
    """Whole months from one date to a later one."""
    months = (later.year - earlier.year) * 12 + later.month - earlier.month
    return months - 1 if later.day < earlier.day else months


@dataclass(frozen=True)
class Rates:
    effective_from: date
    base: float
    tier1: float
    tier2: float
    tier3: float


# Schedule A in the policy manual, as the billing system holds it.
RATES = (
    Rates(date(2026, 1, 1), 18.00, 3.20, 4.80, 7.40),
    Rates(date(2026, 7, 1), 19.50, 3.50, 5.20, 8.10),
)


def rates_for(period_start: date) -> Rates:
    """The rates in force on the first day of a billing period (section 4.1)."""
    in_force = [rates for rates in RATES if rates.effective_from <= period_start]
    if not in_force:
        raise ValueError(f"no rates in force on {period_start}")
    return in_force[-1]


def charge_for(gallons: int, period_start: date) -> dict[str, str]:
    """The bill for one period: Base Charge plus the volume charge in tiers."""
    rates = rates_for(period_start)
    low, high = TIER_LIMITS
    tier1 = min(gallons, low) / 1000 * rates.tier1
    tier2 = max(min(gallons, high) - low, 0) / 1000 * rates.tier2
    tier3 = max(gallons - high, 0) / 1000 * rates.tier3
    total = rates.base + tier1 + tier2 + tier3
    return {
        "period_start": period_start.isoformat(),
        "gallons": str(gallons),
        "base_charge": f"{rates.base:.2f}",
        "volume_charge": f"{tier1 + tier2 + tier3:.2f}",
        "total": f"{total:.2f}",
    }


def daily_summary(rows: Iterable[dict[str, str]], overnight: list[int]) -> list[dict[str, str]]:
    """Hourly rows boiled down to one row per day: total use and lowest overnight flow."""
    days: dict[str, dict[str, Any]] = {}
    for row in rows:
        day, hour = row["hour"].split("T")
        record = days.setdefault(day, {"total": 0.0, "overnight": []})
        record["total"] += float(row["gallons"])
        if int(hour) in overnight:
            record["overnight"].append(float(row["gallons"]))
    return [
        {
            "date": day,
            "gallons": f"{record['total']:.0f}",
            "lowest_overnight_gallons_per_hour": f"{min(record['overnight'], default=0.0):.1f}",
        }
        for day, record in sorted(days.items())
    ]


@dataclass
class BillingSystem:
    """Everything the stand-in systems hold, loaded once for the whole run."""

    accounts: dict[str, dict[str, str]] = field(default_factory=dict)
    bills: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    adjustments: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    readings: dict[str, list[dict[str, str]]] = field(default_factory=dict)

    @classmethod
    def load(cls, data_dir: Path) -> BillingSystem:
        system = cls()
        system.accounts = {r["account_id"]: r for r in read_rows(data_dir / "accounts.csv")}
        for name, target in (
            ("bills.csv", system.bills),
            ("adjustments.csv", system.adjustments),
            ("hourly_readings.csv", system.readings),
        ):
            for row in read_rows(data_dir / name):
                target.setdefault(row["account_id"], []).append(row)
        return system

    def account(self, account_id: str, today: date) -> dict[str, Any]:
        """The account as the rep sees it, with how long ago each adjustment was."""
        adjustments = [
            {
                "date": row["date"],
                "type": row["type"],
                "amount": row["amount"],
                "months_before_this_complaint": str(
                    months_between(date.fromisoformat(row["date"]), today)
                ),
            }
            for row in self.adjustments.get(account_id, [])
        ]
        bills = [
            {
                key: row[key]
                for key in ("period_start", "read_type", "gallons", "end_read", "amount")
            }
            for row in self.bills.get(account_id, [])
        ]
        return {**self.accounts[account_id], "bills": bills, "adjustments": adjustments}

    def daily_use(
        self, account_id: str, start: date, end: date, overnight: list[int]
    ) -> dict[str, Any]:
        """Daily use and lowest overnight flow for a date range. A manually read meter has none."""
        if self.accounts[account_id]["meter_type"] != "automated":
            return {"note": "This is a manually read meter, so it has no hourly readings."}
        last = (end + timedelta(days=1)).isoformat()
        chosen = [
            row
            for row in self.readings.get(account_id, [])
            if start.isoformat() <= row["hour"] < last
        ]
        return {"days": daily_summary(chosen, overnight)}

    def billed_amount(self, account_id: str, period_start: str) -> float | None:
        """What was billed for one period, or None when the account has no such bill."""
        for row in self.bills.get(account_id, []):
            if row["period_start"] == period_start:
                return float(row["amount"])
        return None
