"""The detection rule catalog: which malware families and tools each rule covers, as lookups.

Each function stands in for a search you'd run against your own rule catalog: in a
detection-as-code repository, a query on the malware or tool tags in each rule's metadata; in a
SIEM or EDR, a rule search filtered on the same tags. Here the rules come from
data/detection_rules.csv, loaded into an in-memory SQLite database, so the recipe reads the
catalog the way it would read a live system and never edits it.

Matching is exact on purpose: a covered name matches when it is the same name without case,
spaces, edge punctuation, a leading "the" or a possessive. A name that is close but different
("Larchwood stealer" next to "Larchwick stealer") is a different tool until someone says
otherwise, so `near_rules` returns it separately and never as a match. A lookup returns every
rule it finds, so several rules for one tool reach the caller as a list.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

SEED = Path(__file__).resolve().parent / "data" / "detection_rules.csv"


@dataclass(frozen=True)
class Rule:
    rule_id: str
    title: str
    platform: str  # where the rule runs: EDR, SIEM, NDR, WAF
    covers: tuple[str, ...]  # the malware families and tools it covers, as the rule names them


def cover_key(name: str) -> str:
    """A name without case, edge punctuation, a leading "the" or a possessive, spaces collapsed."""
    name = re.sub("['\\u2019]s$", "", name.strip().strip(" .,;:()\"'"))
    name = re.sub(r"^the\s+", "", name, flags=re.IGNORECASE)
    return " ".join(name.split()).casefold()


class RuleCatalog:
    """The rule catalog, loaded from a CSV seed into SQLite."""

    def __init__(self, seed: Path = SEED) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE rules (rule_id TEXT PRIMARY KEY, title TEXT, platform TEXT)")
        self.db.execute("CREATE TABLE covers (rule_id TEXT, name TEXT, name_key TEXT)")
        with seed.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                self.db.execute(
                    "INSERT INTO rules VALUES (?, ?, ?)",
                    (row["rule_id"], row["title"], row["platform"]),
                )
                names = [n.strip() for n in row["covers"].split(";") if n.strip()]
                self.db.executemany(
                    "INSERT INTO covers VALUES (?, ?, ?)",
                    [(row["rule_id"], name, cover_key(name)) for name in names],
                )

    def get_rule(self, rule_id: str) -> Rule | None:
        """One rule by its ID, or None when the catalog has no such rule."""
        found = self.db.execute(
            "SELECT rule_id, title, platform FROM rules WHERE rule_id = ?", (rule_id,)
        ).fetchone()
        if found is None:
            return None
        covers = self.db.execute(
            "SELECT name FROM covers WHERE rule_id = ? ORDER BY rowid", (rule_id,)
        )
        rule_id, title, platform = found
        return Rule(rule_id, title, platform, tuple(name for (name,) in covers))

    def find_rules(self, names: Iterable[str]) -> list[Rule]:
        """Every rule that covers any of these names (a tool's canonical name and its aliases)."""
        keys = sorted({cover_key(name) for name in names} - {""})
        if not keys:
            return []
        marks = ", ".join("?" * len(keys))
        found = self.db.execute(
            f"SELECT DISTINCT rule_id FROM covers WHERE name_key IN ({marks}) ORDER BY rule_id",
            keys,
        )
        return [rule for (rule_id,) in found.fetchall() if (rule := self.get_rule(rule_id))]

    def near_rules(self, names: Iterable[str], min_ratio: float) -> list[tuple[Rule, str]]:
        """Rules covering a name close to one of these but not the same, with the covered name.

        Close means a difflib similarity of at least min_ratio between the two keys. These are
        for a person to look at; they are never a match.
        """
        keys = {cover_key(name) for name in names} - {""}
        near: list[tuple[Rule, str]] = []
        rows = self.db.execute("SELECT rule_id, name, name_key FROM covers ORDER BY rule_id, rowid")
        for rule_id, covered, key in rows.fetchall():
            if key in keys or not any(
                SequenceMatcher(None, key, mine).ratio() >= min_ratio for mine in keys
            ):
                continue
            rule = self.get_rule(rule_id)
            if rule is not None and all(r != rule for r, _ in near):
                near.append((rule, covered))
        return near
