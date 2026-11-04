"""Slack: the workspace directory and an incident channel's messages.

Stands in for Slack's Web API: `users.list` for the directory and `conversations.history` for a
channel's messages. Here both come from `data/slack_users.csv` and `data/slack_messages.csv`
(`ts` in epoch seconds, as Slack gives it), loaded into an in-memory SQLite database.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"


@dataclass(frozen=True)
class User:
    user_id: str
    handle: str  # e.g. "sam.okafor"; action items are owned by a handle
    real_name: str

    @property
    def first_name(self) -> str:
        return self.real_name.split()[0] if self.real_name.split() else ""


@dataclass(frozen=True)
class Message:
    at: datetime
    handle: str  # the author's handle; "unknown:<id>" for a user not in the directory
    text: str


class Slack:
    def __init__(
        self,
        users_seed: Path = DATA / "slack_users.csv",
        messages_seed: Path = DATA / "slack_messages.csv",
    ) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE users (id TEXT, handle TEXT, real_name TEXT)")
        self.db.execute("CREATE TABLE messages (channel TEXT, ts REAL, user TEXT, text TEXT)")
        with users_seed.open(encoding="utf-8", newline="") as handle:
            users = [(r["id"], r["handle"], r["real_name"]) for r in csv.DictReader(handle)]
        with messages_seed.open(encoding="utf-8", newline="") as handle:
            messages = [
                (r["channel"], float(r["ts"]), r["user"], r["text"]) for r in csv.DictReader(handle)
            ]
        self.db.executemany("INSERT INTO users VALUES (?, ?, ?)", users)
        self.db.executemany("INSERT INTO messages VALUES (?, ?, ?, ?)", messages)

    def users(self) -> list[User]:
        """Everyone in the workspace directory, by handle."""
        rows = self.db.execute("SELECT id, handle, real_name FROM users ORDER BY handle")
        return [User(*row) for row in rows.fetchall()]

    def users_with_first_name(self, first_name: str) -> list[User]:
        """Everyone whose real name starts with this first name (exact, case-sensitive). More
        than one means a message that says only the first name doesn't tell you who."""
        return [user for user in self.users() if user.first_name == first_name]

    def channel_history(self, channel: str) -> list[Message]:
        """The channel's messages, oldest first."""
        rows = self.db.execute(
            "SELECT m.ts, COALESCE(u.handle, 'unknown:' || m.user), m.text FROM messages m "
            "LEFT JOIN users u ON u.id = m.user WHERE m.channel = ? ORDER BY m.ts",
            (channel,),
        ).fetchall()
        return [Message(datetime.fromtimestamp(ts, UTC), handle, text) for ts, handle, text in rows]
