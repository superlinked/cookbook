"""The CRM: look up a card-services case and the messages filed on it.

Stands in for the CRM's case API (e.g. a GET on the case in Salesforce Service Cloud or Dynamics
365, with its chat transcripts, secure messages, case notes and emails as related records), which
gives you the case reference, the customer, the card on the case and every message in date order.
A chat transcript also comes with the chat system's own record of its turns (a Salesforce
ConversationEntry or LiveChatTranscriptEvent: the time, who sent it and whether that was the
customer or an agent), so who said what never has to be read off the transcript text.
Here the cases come from `data/crm_cases.csv`, the message index from `data/crm_messages.csv` and
the chat turns from `data/crm_chat_turns.csv`, loaded into an in-memory SQLite database; each
message body is a text file in `data/messages/`. Every timestamp carries its UTC offset.
"""

from __future__ import annotations

import csv
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
CASES = DATA / "crm_cases.csv"
MESSAGES = DATA / "crm_messages.csv"
BODIES = DATA / "messages"
TURNS = DATA / "crm_chat_turns.csv"
# Who wrote a message: the customer, bank staff, or both (a chat transcript, where each line
# names its speaker).
ROLES = ("customer", "staff", "chat")
# Who sent one turn of a chat, from the chat system's record.
TURN_ROLES = ("customer", "staff")


@dataclass(frozen=True)
class Case:
    case_ref: str
    customer: str
    customer_id: str
    card_id: str
    card_last4: str
    opened_at: datetime
    team: str


@dataclass(frozen=True)
class ChatTurn:
    """One turn of a chat, as the chat system records it: "14:02", who sent it, and their role."""

    time: str
    speaker: str
    role: str  # customer | staff


@dataclass(frozen=True)
class Message:
    message_id: str
    sent_at: datetime
    channel: str
    author: str
    author_role: str
    text: str
    turns: tuple[ChatTurn, ...] = ()  # a chat's turns in order; empty for any other message


def _when(value: str) -> datetime:
    """An ISO 8601 timestamp with its UTC offset ("Z" or "+00:00"). One without an offset is
    refused: guessing its zone would shift the card system's lookback window."""
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(f"timestamp {value!r} has no UTC offset")
    return moment


def _insert(db: sqlite3.Connection, table: str, rows: Sequence[Sequence[str]]) -> None:
    """Insert seed rows; a repeated primary key is refused with the table's name."""
    marks = ", ".join("?" * len(rows[0])) if rows else ""
    try:
        db.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)
    except sqlite3.IntegrityError as error:
        raise ValueError(f"{table}: {error}") from None


class CRM:
    def __init__(
        self,
        cases: Path = CASES,
        messages: Path = MESSAGES,
        bodies: Path = BODIES,
        turns: Path = TURNS,
    ):
        self.bodies = bodies
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE cases (case_ref TEXT PRIMARY KEY, customer TEXT, customer_id TEXT,"
            " card_id TEXT, card_last4 TEXT, opened_at TEXT, team TEXT)"
        )
        self.db.execute(
            "CREATE TABLE messages (case_ref TEXT, message_id TEXT PRIMARY KEY, sent_at TEXT,"
            " channel TEXT, author TEXT, author_role TEXT, file TEXT)"
        )
        self.db.execute(
            "CREATE TABLE turns (message_id TEXT, turn INTEGER, time TEXT, speaker TEXT,"
            " role TEXT, PRIMARY KEY (message_id, turn))"
        )
        with cases.open(encoding="utf-8", newline="") as handle:
            rows = [
                (
                    r["case_ref"],
                    r["customer"],
                    r["customer_id"],
                    r["card_id"],
                    r["card_last4"],
                    r["opened_at"],
                    r["team"],
                )
                for r in csv.DictReader(handle)
            ]
        for case_row in rows:
            _when(case_row[5])
        _insert(self.db, "cases", rows)
        with messages.open(encoding="utf-8", newline="") as handle:
            found = list(csv.DictReader(handle))
        for r in found:
            if r["author_role"] not in ROLES:
                raise ValueError(f"{r['message_id']}: author_role must be one of {ROLES}")
            if Path(r["file"]).name != r["file"] or not (bodies / r["file"]).is_file():
                raise ValueError(f"{r['message_id']}: no message file {r['file']!r} in {bodies}")
            _when(r["sent_at"])
        _insert(
            self.db,
            "messages",
            [
                (
                    r["case_ref"],
                    r["message_id"],
                    r["sent_at"],
                    r["channel"],
                    r["author"],
                    r["author_role"],
                    r["file"],
                )
                for r in found
            ],
        )
        with turns.open(encoding="utf-8", newline="") as handle:
            turn_rows = list(csv.DictReader(handle))
        chats = {r["message_id"] for r in found if r["author_role"] == "chat"}
        for r in turn_rows:
            if r["message_id"] not in chats:
                raise ValueError(f"{r['message_id']}: chat turns for a message that isn't a chat")
            if r["role"] not in TURN_ROLES:
                raise ValueError(f"{r['message_id']}: a turn's role must be one of {TURN_ROLES}")
        if missing := chats - {r["message_id"] for r in turn_rows}:
            raise ValueError(f"{', '.join(sorted(missing))}: a chat with no turn record")
        _insert(
            self.db,
            "turns",
            [(r["message_id"], r["turn"], r["time"], r["speaker"], r["role"]) for r in turn_rows],
        )

    def find_case(self, case_ref: str) -> Case | None:
        """The case with exactly this reference, or None."""
        row = self.db.execute(
            "SELECT case_ref, customer, customer_id, card_id, card_last4, opened_at, team"
            " FROM cases WHERE case_ref = ?",
            (case_ref,),
        ).fetchone()
        if row is None:
            return None
        return Case(row[0], row[1], row[2], row[3], row[4], _when(row[5]), row[6])

    def list_cases(self) -> list[str]:
        """Every case reference, in order."""
        return [r[0] for r in self.db.execute("SELECT case_ref FROM cases ORDER BY case_ref")]

    def case_messages(self, case_ref: str) -> list[Message]:
        """Every message filed on the case, oldest first."""
        rows = self.db.execute(
            "SELECT message_id, sent_at, channel, author, author_role, file FROM messages"
            " WHERE case_ref = ? ORDER BY sent_at, message_id",
            (case_ref,),
        ).fetchall()
        return [
            Message(
                row[0],
                _when(row[1]),
                row[2],
                row[3],
                row[4],
                (self.bodies / row[5]).read_text(encoding="utf-8"),
                self.chat_turns(row[0]),
            )
            for row in rows
        ]

    def chat_turns(self, message_id: str) -> tuple[ChatTurn, ...]:
        """A chat's turns in order, from the chat system's record; empty for other messages."""
        rows = self.db.execute(
            "SELECT time, speaker, role FROM turns WHERE message_id = ? ORDER BY turn",
            (message_id,),
        ).fetchall()
        return tuple(ChatTurn(row[0], row[1], row[2]) for row in rows)
