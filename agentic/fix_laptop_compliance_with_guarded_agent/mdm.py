"""A stand-in for the company's device management system (MDM), backed by data/devices.json.

Replace `Fleet` with calls to your own MDM and nothing else changes. Everything here works on
records in memory: no fix ever touches the machine the recipe runs on.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

# The device record's fields that identify the owner. The agent never sees them.
OWNER_FIELDS = ("owner_name", "owner_email")


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def meets(value: Any, check: dict[str, Any], today: date) -> bool:
    """Whether one setting's value meets its policy check from rules.yaml."""
    compare, wanted = check["compare"], check["value"]
    if compare == "equal":
        return bool(value == wanted)
    if compare == "at_most":
        return isinstance(value, int) and value <= int(wanted)
    if compare == "at_least_version":
        return version_tuple(str(value)) >= version_tuple(str(wanted))
    if compare == "all_passphrase":
        return all(key.get("has_passphrase") is True for key in value)
    if compare == "within_days":
        return (today - date.fromisoformat(str(value))).days <= int(wanted)
    raise ValueError(f"unknown compare {compare!r} in rules.yaml")


@dataclass
class Fleet:
    """Every device record, and what each fix did, for one run."""

    devices: dict[str, dict[str, Any]] = field(default_factory=dict)
    changes: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> Fleet:
        records = json.loads(path.read_text(encoding="utf-8"))
        return cls({record["device_id"]: record for record in records})

    def record(self, device_id: str) -> dict[str, Any]:
        """The device as the MDM reports it now, without the owner's name or email. A setting a
        configuration profile manages shows the profile's value, whatever was set last."""
        current = {
            key: value
            for key, value in copy.deepcopy(self.devices[device_id]).items()
            if key not in OWNER_FIELDS
        }
        for profile in current["profiles"]:
            current.update(profile["manages"])
        return current

    def set(self, device_id: str, setting: str, value: Any) -> None:
        """Change one setting. A profile that manages it puts it back on the next read."""
        self.devices[device_id][setting] = value
        self.changes.append({"device_id": device_id, "setting": setting, "value": value})

    def failing(
        self, device_id: str, checks: dict[str, dict[str, Any]], today: date
    ) -> dict[str, str]:
        """Each check the device fails now, with the policy section. An approved exception that
        hasn't ended counts as meeting the check."""
        current = self.record(device_id)
        excepted = {
            item["setting"]
            for item in current["exceptions"]
            if date.fromisoformat(item["ends"]) >= today
        }
        return {
            setting: check["section"]
            for setting, check in checks.items()
            if setting not in excepted and not meets(current[setting], check, today)
        }
