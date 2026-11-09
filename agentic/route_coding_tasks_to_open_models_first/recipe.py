"""Send each coding task to the cheapest level that passes its tests, and compare with Claude alone.

Who:       A dev team that wants open models as the default for coding work, and a frontier model
           only for the tasks that need it. A developer reviews every change before it merges.
Input:     data/library.json, a small made-up invoicing library with its tests, and data/tasks.json,
           fifteen tasks written as issues, each with the tests that decide when it's done.
Router:    fastino/GLiNER2.5-Decide on SIE sorts each issue into a kind of task, which names the
           level it starts at. Levels, cheapest first:
           tool    ruff with fixed arguments, no model
           open    Qwen/Qwen3.8-27B-FP8 on SIE, in a loop with the tools below
           claude  claude-opus-5-5, the same loop
           A task moves up one level each time the tests still fail.
Tools:     list_files, read_file, write_file (only .py files under ledgerline/) and run_tests
           (a fixed pytest command with a time limit), all inside a temporary copy of the library.
           Python puts the tests back before every run, so a model can't pass by changing them.
Compare:   Every task also goes to Claude alone, with the same tools, from a fresh copy.
Output:    runs/<timestamp>/: tasks.csv, summary.md, tasks/<task>.md with the change and test
           results, diffs/, and transcripts/<task>.json with every tool call.
Run using: SIE Serverless, or SIE Cluster or SIE Local with SIE_BASE_URL, plus ANTHROPIC_API_KEY.
           Model-written code runs on this machine when the tests run: use a container.

    uv run recipe.py
    uv run recipe.py --task T05
    uv run recipe.py --skip-claude-alone
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import difflib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast

import anthropic
import yaml
from dotenv import load_dotenv
from sie_calls import WAIT_S, CallFailed, Calls, SIEStopped
from sie_sdk import SIEClient
from sie_sdk.types import Item

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
RUNS_DIR = HERE / "runs"
RULES_PATH = HERE / "rules.yaml"
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
ROUTER_QUESTION = "kind"
LOG_CHARS = 2000  # how much of each tool input and output the transcript keeps

TASK_ROW = [
    "task_id",
    "title",
    "kind",
    "router_score",
    "start_level",
    "done_by",
    "failed_levels",
    "tests_pass",
    "ci_checks",
    "sie_credits",
    "claude_cost_usd",
    "seconds",
    "alone_tests_pass",
    "alone_ci_checks",
    "alone_claude_cost_usd",
    "alone_seconds",
]


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def make_client() -> SIEClient:
    """Build an SIE client from the API key and SIE_BASE_URL in the environment or `.env`."""
    load_dotenv()
    api_key = os.environ.get("SIE_API_KEY", "").strip()
    base_url = os.environ.get("SIE_BASE_URL", "").strip()
    if not api_key and not base_url:
        raise SystemExit("Stopped: set SIE_API_KEY, or SIE_BASE_URL for SIE Cluster or SIE Local.")
    base_url = base_url or "https://api.superlinked.com"
    return SIEClient(api_key=api_key or None, base_url=base_url, timeout_s=WAIT_S)


def make_claude() -> anthropic.Anthropic:
    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Stopped: set ANTHROPIC_API_KEY, in .env or the environment.")
    return anthropic.Anthropic()


def load_library() -> dict[str, str]:
    return cast(dict[str, str], json.loads((DATA_DIR / "library.json").read_text(encoding="utf-8")))


def load_tasks() -> list[dict[str, Any]]:
    tasks = json.loads((DATA_DIR / "tasks.json").read_text(encoding="utf-8"))
    return cast(list[dict[str, Any]], tasks)


def clip(text: str, chars: int) -> str:
    return text if len(text) <= chars else "...\n" + text[-chars:]


# --- The copy of the library a model works on ------------------------------------------------


class ToolError(Exception):
    """A tool call that can't be carried out, sent back to the model as an error it can read."""


def test_env(root: Path) -> dict[str, str]:
    """What a test run sees: no API keys or anything else from this process's environment."""
    return {
        "PATH": os.defpath,
        "HOME": str(root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "LC_ALL": "C.UTF-8",
    }


def stop_group(process: subprocess.Popen[str]) -> None:
    """Stop a test run and anything it started, since they share its process group."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.communicate()


class Workspace:
    """A temporary copy of the library and one task's tests. Every path a model names must stay
    inside it, and a model may only write .py files in the writable folder."""

    def __init__(self, library: dict[str, str], task: dict[str, Any], rules: dict[str, Any]):
        self.settings = rules["workspace"]
        self.original = dict(library)
        self.tests = {
            **{p: t for p, t in library.items() if p.startswith("tests/")},
            **task["tests"],
        }
        self.ci_tests: dict[str, str] = task.get("ci_tests", {})
        self.root = Path(tempfile.mkdtemp(prefix="ledgerline-")).resolve()
        for path, text in {**library, **task["tests"]}.items():
            self.put(path, text)

    def put(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def safe(self, path: Any, write: bool = False) -> Path:
        """The real path for a path a model named, or ToolError if it leaves the copy."""
        if not isinstance(path, str) or not path.strip() or len(path) > 200:
            raise ToolError("give a path relative to the library, such as ledgerline/money.py")
        if "\\" in path or "\x00" in path:
            raise ToolError(f"{path!r} isn't a plain relative path")
        relative = PurePosixPath(path.strip())
        if relative.is_absolute() or ".." in relative.parts:
            raise ToolError(f"{path!r} leaves the library: use a path inside it")
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ToolError(f"{path!r} goes through a link, which isn't allowed")
        target = self.root.joinpath(*relative.parts)
        if not target.resolve().is_relative_to(self.root):
            raise ToolError(f"{path!r} leaves the library: use a path inside it")
        if write and (relative.parts[0] != self.settings["writable"] or relative.suffix != ".py"):
            raise ToolError(
                f"you can only write .py files under {self.settings['writable']}/; the tests "
                "can't be changed"
            )
        return target

    def inside(self, path: Path) -> bool:
        """True if a path found on disk is in the copy and reached through no link."""
        relative = path.relative_to(self.root)
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return False
        return path.resolve().is_relative_to(self.root)

    def list_files(self) -> str:
        files = [
            path.relative_to(self.root).as_posix()
            for path in sorted(self.root.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts and self.inside(path)
        ]
        return "\n".join(files)

    def read(self, path: Any) -> str:
        target = self.safe(path)
        if not target.is_file():
            raise ToolError(f"there's no file {path!r}")
        return target.read_text(encoding="utf-8", errors="replace")

    def write(self, path: Any, content: Any) -> str:
        target = self.safe(path, write=True)
        if not isinstance(content, str):
            raise ToolError("content must be the whole file, as text")
        if len(content) > self.settings["max_file_chars"]:
            raise ToolError(f"that file is over {self.settings['max_file_chars']} characters")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ToolError(f"couldn't write {path!r}: {type(error).__name__}") from error
        return f"wrote {path} ({len(content)} characters)"

    def run(self, args: list[str]) -> tuple[bool, str]:
        """Run one fixed command inside the copy, with a time limit and no API keys. On the time
        limit, everything the command started is stopped too, not just the command."""
        process = subprocess.Popen(
            [sys.executable, "-m", *args],
            cwd=self.root,
            env=test_env(self.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            output, _ = process.communicate(timeout=self.settings["test_timeout_s"])
        except subprocess.TimeoutExpired:
            stop_group(process)
            return False, f"stopped after {self.settings['test_timeout_s']} s"
        stop_group(process)
        return process.returncode == 0, clip(output, self.settings["output_chars"])

    def restore(self, keep: dict[str, str]) -> None:
        """Put back every file outside the writable folder as it was, and delete any other file
        there, such as a conftest.py that the model's code wrote while tests ran. Without this,
        code from an earlier test run could change how pytest decides the next one."""
        writable = self.settings["writable"]
        expected = {
            **{p: t for p, t in self.original.items() if not p.startswith(f"{writable}/")},
            **self.tests,
            **keep,
        }
        for path in sorted(self.root.rglob("*"), reverse=True):
            relative = path.relative_to(self.root)
            if relative.parts[0] == writable and not path.is_symlink():
                continue
            if path.is_symlink() or path.is_file():
                if relative.as_posix() not in expected or path.is_symlink():
                    path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        for path, text in expected.items():
            self.put(path, text)

    def check(self) -> tuple[bool, str]:
        """Do the tests pass? Python decides this, not the model."""
        self.restore({})
        return self.run(["pytest", "-q", "--no-header", "-p", "no:cacheprovider", "tests"])

    def ci(self) -> bool | None:
        """The checks the team keeps back for CI, which the model never saw. None if none."""
        if not self.ci_tests:
            return None
        self.restore(self.ci_tests)
        return self.run(["pytest", "-q", "--no-header", "-p", "no:cacheprovider", "ci"])[0]

    def diff(self) -> str:
        """The change against the library as it was, as a unified diff."""
        out: list[str] = []
        current = {
            path.relative_to(self.root).as_posix(): path.read_text(
                encoding="utf-8", errors="replace"
            )
            for path in sorted((self.root / "ledgerline").rglob("*.py"))
            if path.is_file() and "__pycache__" not in path.parts and self.inside(path)
        }
        for path in sorted(
            set(current) | {p for p in self.original if p.startswith("ledgerline/")}
        ):
            before = self.original.get(path, "")
            after = current.get(path, "")
            if before != after:
                out += difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    f"a/{path}",
                    f"b/{path}",
                )
        return "".join(out)

    def tool(self, name: str, args: dict[str, Any]) -> str:
        """Run one tool call other than finish. Raises ToolError for the model to read."""
        if name == "list_files":
            return self.list_files()
        if name == "read_file":
            return self.read(args.get("path"))
        if name == "write_file":
            return self.write(args.get("path"), args.get("content"))
        if name == "run_tests":
            passed, output = self.check()
            return ("The tests pass.\n" if passed else "The tests fail.\n") + output
        raise ToolError(f"there is no tool called {name}")


# --- Tools, in the two formats the models read ----------------------------------------------


def object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


TOOL_SPECS = [
    ("list_files", "List every file in the library and its tests.", {}),
    ("read_file", "Read one file, by its path in the library.", {"path": {"type": "string"}}),
    (
        "write_file",
        "Write a whole file under ledgerline/, replacing what's there. Only .py files.",
        {"path": {"type": "string"}, "content": {"type": "string"}},
    ),
    ("run_tests", "Run the tests and see the end of their output.", {}),
    (
        "finish",
        "Say you're done. Python runs the tests: if they fail, you hear why.",
        {"summary": {"type": "string"}},
    ),
]
OPEN_TOOLS = [
    {
        "type": "function",
        "function": {"name": name, "description": text, "parameters": object_schema(props)},
    }
    for name, text, props in TOOL_SPECS
]
CLAUDE_TOOLS = [
    {"name": name, "description": text, "strict": True, "input_schema": object_schema(props)}
    for name, text, props in TOOL_SPECS
]


def escape(text: str) -> str:
    """Issue text with no tags in it, so it can't close the tag it sits in."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def issue_prompt(task: dict[str, Any]) -> str:
    return (
        f"Issue {task['task_id']}.\n<issue>\nTitle: {escape(task['title'])}\n"
        f"{escape(task['body'])}\n</issue>"
    )


# --- The models, behind one interface ---------------------------------------------------------


@dataclass
class ToolCall:
    call_id: str
    name: str
    args: dict[str, Any]
    bad_args: str = ""  # why the arguments couldn't be read, if they couldn't


@dataclass
class Turn:
    calls: list[ToolCall]
    stop: str = ""  # a reason the model can't go on, such as a refusal


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    credits: float = 0.0
    requests: int = 0


class Chat(Protocol):
    usage: Usage

    def ask(self) -> Turn: ...
    def answer(self, results: list[tuple[str, str, bool]]) -> None: ...
    def nudge(self, text: str) -> None: ...


def read_args(raw: Any) -> tuple[dict[str, Any], str]:
    """A tool call's arguments as a dict, or why they couldn't be read."""
    if isinstance(raw, dict):
        return raw, ""
    try:
        args = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
    except json.JSONDecodeError:
        return {}, "the arguments weren't valid JSON"
    return (args, "") if isinstance(args, dict) else ({}, "the arguments must be a JSON object")


class OpenModelChat:
    """An open model on SIE, through its chat endpoint with tools."""

    def __init__(
        self, client: SIEClient, calls: Calls, level: str, rules: dict[str, Any], prompt: str
    ):
        self.client, self.calls, self.model = client, calls, rules["models"][level]
        self.settings = rules["open_models"]
        self.max_tokens = self.settings["max_completion_tokens"][level]
        self.usage = Usage()
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": rules["system"]},
            {"role": "user", "content": prompt},
        ]

    def ask(self) -> Turn:
        before = self.calls.credits.get(self.model, 0.0)
        reply = self.calls.call(
            self.model,
            lambda: self.client.chat_completions(
                self.model,
                cast(Any, self.messages),
                tools=OPEN_TOOLS,
                max_completion_tokens=self.max_tokens,
                temperature=self.settings["temperature"],
                provision_timeout_s=WAIT_S,
            ),
            lambda r: (
                isinstance(r, dict)
                and isinstance(r.get("choices"), list)
                and bool(r["choices"])
                and isinstance(r["choices"][0].get("message"), dict)
            ),
            "a message",
        )
        self.usage.requests += 1
        self.usage.credits += self.calls.credits.get(self.model, 0.0) - before
        usage = reply.get("usage") or {}
        self.usage.input_tokens += int(usage.get("prompt_tokens") or 0)
        self.usage.output_tokens += int(usage.get("completion_tokens") or 0)
        message = reply["choices"][0]["message"]
        raw_calls = message.get("tool_calls") or []
        self.messages.append(
            {"role": "assistant", "content": message.get("content") or "", "tool_calls": raw_calls}
            if raw_calls
            else {"role": "assistant", "content": message.get("content") or ""}
        )
        turn = Turn(calls=[])
        for number, raw in enumerate(raw_calls):
            function = raw.get("function") or {} if isinstance(raw, dict) else {}
            args, bad = read_args(function.get("arguments"))
            call_id = (
                str(raw.get("id") or f"call-{number}") if isinstance(raw, dict) else str(number)
            )
            turn.calls.append(ToolCall(call_id, str(function.get("name", "")), args, bad))
        if reply["choices"][0].get("finish_reason") == "length" and not turn.calls:
            turn.stop = "the model ran out of output tokens"
        return turn

    def answer(self, results: list[tuple[str, str, bool]]) -> None:
        for call_id, content, is_error in results:
            text = f"Error: {content}" if is_error else content
            self.messages.append({"role": "tool", "tool_call_id": call_id, "content": text})

    def nudge(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})


class ClaudeChat:
    """Claude, through the Anthropic API, with the same tools."""

    def __init__(self, claude: anthropic.Anthropic, rules: dict[str, Any], prompt: str):
        self.claude, self.rules = claude, rules
        self.settings = rules["claude"]
        self.usage = Usage()
        self.messages: list[Any] = [{"role": "user", "content": prompt}]

    def ask(self) -> Turn:
        extra: dict[str, Any] = {}
        if self.settings["fallbacks"]:
            extra = {
                "betas": [self.settings["fallback_beta"]],
                "fallbacks": self.settings["fallbacks"],
            }
        response = self.claude.beta.messages.create(
            model=self.rules["models"]["claude"],
            max_tokens=self.settings["max_tokens"],
            system=self.rules["system"],
            tools=cast(Any, CLAUDE_TOOLS),
            messages=self.messages,
            output_config={"effort": self.settings["effort"]},
            cache_control={"type": "ephemeral"},
            **extra,
        )
        self.usage.requests += 1
        self.usage.input_tokens += response.usage.input_tokens
        self.usage.output_tokens += response.usage.output_tokens
        self.usage.cache_read_tokens += response.usage.cache_read_input_tokens or 0
        self.usage.cache_write_tokens += response.usage.cache_creation_input_tokens or 0
        if response.stop_reason in ("refusal", "max_tokens"):
            return Turn(calls=[], stop=f"Claude stopped: {response.stop_reason}")
        if response.content:  # an empty reply can't go back in the conversation
            self.messages.append({"role": "assistant", "content": response.content})
        return Turn(
            calls=[
                ToolCall(block.id, block.name, dict(block.input))
                for block in response.content
                if block.type == "tool_use"
            ]
        )

    def answer(self, results: list[tuple[str, str, bool]]) -> None:
        self.messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": call_id, "content": content}
                    | ({"is_error": True} if is_error else {})
                    for call_id, content, is_error in results
                ],
            }
        )

    def nudge(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})


# --- One attempt at one level ------------------------------------------------------------------


@dataclass
class Attempt:
    level: str
    model: str = ""
    passed: bool = False
    ci: bool | None = None
    stopped: str = ""  # why the attempt ended early, if it did
    test_output: str = ""  # the last test run's output
    tool_calls: int = 0
    test_runs: int = 0
    seconds: float = 0.0
    usage: Usage = field(default_factory=Usage)
    diff: str = ""
    log: list[dict[str, Any]] = field(default_factory=list)


def claude_cost(usage: Usage, prices: dict[str, float]) -> float:
    """What the Claude requests cost, in dollars."""
    return (
        usage.input_tokens * prices["input"]
        + usage.cache_write_tokens * prices["cache_write"]
        + usage.cache_read_tokens * prices["cache_read"]
        + usage.output_tokens * prices["output"]
    ) / 1_000_000


def work_loop(
    chat: Chat, ws: Workspace, attempt: Attempt, rules: dict[str, Any], rounds: int
) -> None:
    """Let a model use the tools until the tests pass, it gives up, or it runs out of requests."""
    retries = rules["retries_after_failed_tests"]
    nudged = False
    for _ in range(rounds):
        turn = chat.ask()
        if turn.stop:
            attempt.stopped = turn.stop
            return
        if not turn.calls:
            if nudged:
                attempt.stopped = "the model stopped without calling finish"
                return
            nudged = True
            chat.nudge("Use the tools to do the work, and call finish when the tests pass.")
            continue
        results: list[tuple[str, str, bool]] = []
        ended = False
        # Lookups and writes first, so a finish sent alongside them is checked after them.
        for call in sorted(turn.calls, key=lambda call: call.name == "finish"):
            entry: dict[str, Any] = {
                "tool": call.name,
                "input": clip(json.dumps(call.args), LOG_CHARS),
            }
            if call.bad_args:
                content, is_error = call.bad_args, True
            elif call.name == "finish":
                attempt.test_runs += 1
                passed, output = ws.check()
                attempt.test_output = output
                if passed:
                    attempt.passed, ended = True, True
                    content, is_error = "The tests pass. Done.", False
                elif retries > 0:
                    retries -= 1
                    content, is_error = "The tests fail, so this isn't done yet.\n" + output, True
                else:
                    ended = True
                    attempt.stopped = "the tests still failed after the model said it was done"
                    content, is_error = "The tests fail.\n" + output, True
            else:
                attempt.tool_calls += 1
                if call.name == "run_tests":
                    attempt.test_runs += 1
                try:
                    content, is_error = ws.tool(call.name, call.args), False
                except ToolError as error:
                    content, is_error = str(error), True
            entry |= {"output": clip(content, LOG_CHARS), "is_error": is_error}
            attempt.log.append(entry)
            results.append((call.call_id, content, is_error))
        if ended:
            return
        chat.answer(results)
    attempt.stopped = "the model ran out of requests"


def run_level(
    level: str,
    task: dict[str, Any],
    library: dict[str, str],
    rules: dict[str, Any],
    client: SIEClient,
    calls: Calls,
    claude: anthropic.Anthropic,
) -> Attempt:
    """One attempt at one level, on a fresh copy of the library."""
    attempt = Attempt(level=level, model=rules["models"].get(level, "ruff"))
    started = time.monotonic()
    ws = Workspace(library, task, rules)
    try:
        if level == "tool":
            for args in (rules["tool"]["fix"], rules["tool"]["format"]):
                _, output = ws.run(["ruff", *args])
                attempt.log.append(
                    {"tool": "ruff " + " ".join(args), "output": clip(output, LOG_CHARS)}
                )
        else:
            chat: Chat
            if level == "claude":
                chat = ClaudeChat(claude, rules, issue_prompt(task))
                rounds = rules["claude"]["max_rounds"]
            else:
                chat = OpenModelChat(client, calls, level, rules, issue_prompt(task))
                rounds = rules["open_models"]["max_rounds"]
            try:
                work_loop(chat, ws, attempt, rules, rounds)
            except CallFailed as error:
                attempt.stopped = f"an SIE call failed: {error}"
            except (
                anthropic.RateLimitError,
                anthropic.InternalServerError,
                anthropic.APIConnectionError,
                anthropic.BadRequestError,  # about this conversation, such as one too long
            ) as error:
                attempt.stopped = f"the Claude API failed: {type(error).__name__}"
            attempt.usage = chat.usage
        # Python has the last word on every attempt, whatever the model said.
        attempt.test_runs += 1
        attempt.passed, attempt.test_output = ws.check()
        attempt.ci = ws.ci() if attempt.passed else None
        attempt.diff = ws.diff()
    finally:
        ws.close()
    attempt.seconds = time.monotonic() - started
    return attempt


# --- Routing ---------------------------------------------------------------------------------


@dataclass
class Route:
    kind: str
    score: float
    level: str
    note: str = ""
    credits: float = 0.0  # what the router's own SIE call cost
    seconds: float = 0.0


def route_task(
    task: dict[str, Any], rules: dict[str, Any], client: SIEClient, calls: Calls
) -> Route:
    """The kind of task, from GLiNER2.5-Decide, and the level it starts at. A pick the model isn't
    sure of, or a failed call, sends the task to Claude."""
    settings = rules["routing"]
    kinds: dict[str, dict[str, str]] = settings["kinds"]
    model = rules["models"]["router"]
    schema = {
        ROUTER_QUESTION: {
            "type": "choice",
            "instructions": settings["question"],
            "criteria": {label: kind["description"] for label, kind in kinds.items()},
        }
    }

    def valid(reply: Any) -> bool:
        if not isinstance(reply, list) or len(reply) != 1 or not isinstance(reply[0], dict):
            return False
        answer = (reply[0].get("data") or {}).get(ROUTER_QUESTION)
        probabilities = answer.get("probabilities") if isinstance(answer, dict) else None
        return isinstance(probabilities, dict) and all(
            isinstance(probabilities.get(label), (int, float)) for label in kinds
        )

    text = f"{task['title']}\n{task['body']}"
    before = calls.credits.get(model, 0.0)
    try:
        reply = calls.call(
            model,
            lambda: client.extract(
                model,
                [Item(id=task["task_id"], text=text)],
                output_schema=schema,
                provision_timeout_s=WAIT_S,
            ),
            valid,
            "a probability for every kind of task",
        )
    except CallFailed as error:
        return Route("unknown", 0.0, "claude", f"the router failed: {error}")
    spent = calls.credits.get(model, 0.0) - before
    probabilities = reply[0]["data"][ROUTER_QUESTION]["probabilities"]
    kind = max(kinds, key=lambda label: float(probabilities[label]))
    score = float(probabilities[kind])
    if score < settings["min_score"]:
        return Route(
            kind, score, "claude", f"the router wasn't sure ({score:.2f} for {kind})", spent
        )
    return Route(kind, score, kinds[kind]["level"], credits=spent)


# --- One task, both ways -----------------------------------------------------------------------


@dataclass
class TaskResult:
    task: dict[str, Any]
    route: Route
    attempts: list[Attempt]
    alone: Attempt | None = None


def solve_task(
    task: dict[str, Any],
    library: dict[str, str],
    rules: dict[str, Any],
    client: SIEClient,
    calls: Calls,
    claude: anthropic.Anthropic,
    compare: bool,
) -> TaskResult:
    started = time.monotonic()
    route = route_task(task, rules, client, calls)
    route.seconds = time.monotonic() - started
    levels: list[str] = rules["levels"]
    result = TaskResult(task, route, [])
    for level in levels[levels.index(route.level) :]:
        attempt = run_level(level, task, library, rules, client, calls, claude)
        result.attempts.append(attempt)
        if attempt.passed:
            break
    if compare:
        result.alone = run_level("claude", task, library, rules, client, calls, claude)
    return result


def router_cost(result: TaskResult, prices: dict[str, float]) -> tuple[float, float, float]:
    """SIE credits, Claude dollars and seconds the router spent on one task, across its attempts."""
    credits = result.route.credits + sum(attempt.usage.credits for attempt in result.attempts)
    dollars = sum(
        claude_cost(attempt.usage, prices)
        for attempt in result.attempts
        if attempt.level == "claude"
    )
    seconds = result.route.seconds + sum(attempt.seconds for attempt in result.attempts)
    return credits, dollars, seconds


def task_row(result: TaskResult, rules: dict[str, Any]) -> dict[str, Any]:
    prices = rules["prices"][rules["models"]["claude"]]
    last = result.attempts[-1]
    router_credits, dollars, seconds = router_cost(result, prices)
    alone = result.alone
    return {
        "task_id": result.task["task_id"],
        "title": result.task["title"],
        "kind": result.route.kind,
        "router_score": f"{result.route.score:.2f}",
        "start_level": result.route.level,
        "done_by": last.level if last.passed else "",
        "failed_levels": " ".join(a.level for a in result.attempts if not a.passed),
        "tests_pass": last.passed,
        "ci_checks": "" if last.ci is None else ("pass" if last.ci else "fail"),
        "sie_credits": f"{router_credits:.0f}",
        "claude_cost_usd": f"{dollars:.4f}",
        "seconds": round(seconds),
        "alone_tests_pass": "" if alone is None else alone.passed,
        "alone_ci_checks": ""
        if alone is None or alone.ci is None
        else ("pass" if alone.ci else "fail"),
        "alone_claude_cost_usd": "" if alone is None else f"{claude_cost(alone.usage, prices):.4f}",
        "alone_seconds": "" if alone is None else round(alone.seconds),
    }


# --- Output ------------------------------------------------------------------------------------


def csv_safe(value: Any) -> str:
    """A leading ' on a cell a spreadsheet would run as a formula; anything else as written."""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def attempt_lines(attempt: Attempt, prices: dict[str, float]) -> list[str]:
    cost = (
        f", ${claude_cost(attempt.usage, prices):.4f} of Claude"
        if attempt.level == "claude"
        else (f", {attempt.usage.credits:.0f} SIE credits" if attempt.level != "tool" else "")
    )
    outcome = "tests pass" if attempt.passed else "tests fail"
    lines = [
        f"- **{attempt.level}** ({attempt.model}): {outcome}, {attempt.tool_calls} tool calls, "
        f"{attempt.usage.requests} requests, {attempt.usage.input_tokens} input and "
        f"{attempt.usage.output_tokens} output tokens{cost}, {attempt.seconds:.0f} s"
    ]
    if attempt.stopped:
        lines.append(f"  - Stopped because: {attempt.stopped}")
    if attempt.ci is not None:
        lines.append(f"  - Checks kept back for CI: {'pass' if attempt.ci else 'fail'}")
    return lines


def task_markdown(result: TaskResult, rules: dict[str, Any]) -> str:
    prices = rules["prices"][rules["models"]["claude"]]
    task, route = result.task, result.route
    last = result.attempts[-1]
    lines = [
        f"# {task['task_id']}: {task['title']}",
        "",
        task["body"],
        "",
        f"**Router:** {route.kind} ({route.score:.2f}), so it started at `{route.level}`."
        + (f" {route.note}." if route.note else ""),
        "",
        "**Result:** "
        + (f"tests pass, done by `{last.level}`." if last.passed else "no level passed the tests."),
        "",
        "## Attempts",
        "",
    ]
    for attempt in result.attempts:
        lines += attempt_lines(attempt, prices)
    lines += ["", "## The change", "", "```diff", last.diff or "(no change)", "```", ""]
    if not last.passed:
        lines += ["## The last test run", "", "```", last.test_output, "```", ""]
    if result.alone is not None:
        lines += ["## Claude alone, for comparison", ""]
        lines += [*attempt_lines(result.alone, prices), ""]
    return "\n".join(lines)


def summary_markdown(
    results: list[TaskResult],
    rows: list[dict[str, Any]],
    calls: Calls,
    finished: bool,
    credit_usd: float,
) -> str:
    done = [r for r in results if r.attempts[-1].passed]
    by_level: dict[str, int] = {}
    for result in done:
        level = result.attempts[-1].level
        by_level[level] = by_level.get(level, 0) + 1
    ci_fail = [r["task_id"] for r in rows if r["ci_checks"] == "fail"]
    router_dollars = sum(float(r["claude_cost_usd"]) for r in rows)
    router_seconds = sum(int(r["seconds"]) for r in rows)
    credits = sum(calls.credits.values())
    lines = [
        "# Run summary",
        "",
    ]
    if not finished:
        lines += ["**This run stopped partway. These numbers cover the tasks it finished.**", ""]
    lines += [
        "## The router",
        "",
        f"- Tasks whose tests pass: {len(done)} of {len(results)}",
        "- Done by level: " + ", ".join(f"{level} {count}" for level, count in by_level.items()),
        f"- Failed the checks kept back for CI: {', '.join(ci_fail) or 'none'}",
        f"- Claude: ${router_dollars:.4f}",
        f"- SIE credits charged: {credits:.0f} ("
        + ", ".join(f"{model}: {value:.0f}" for model, value in sorted(calls.credits.items()))
        + f"), ${credits * credit_usd:.4f}",
        f"- Total: ${router_dollars + credits * credit_usd:.4f}",
        f"- SIE calls: {calls.total}, of which {calls.unreported} didn't report what they cost"
        f" and {len(calls.failures)} failed",
        f"- Time: {router_seconds} s",
    ]
    alone = [r for r in rows if r["alone_tests_pass"] != ""]
    if alone:
        lines += [
            "",
            "## Claude alone, for comparison",
            "",
            f"- Tasks whose tests pass: {sum(r['alone_tests_pass'] is True for r in alone)} "
            f"of {len(alone)}",
            "- Failed the checks kept back for CI: "
            + (", ".join(r["task_id"] for r in alone if r["alone_ci_checks"] == "fail") or "none"),
            f"- Claude: ${sum(float(r['alone_claude_cost_usd']) for r in alone):.4f}",
            f"- Time: {sum(int(r['alone_seconds']) for r in alone)} s",
        ]
    return "\n".join(lines) + "\n"


def write_outputs(
    output: Path, results: list[TaskResult], rules: dict[str, Any], calls: Calls, finished: bool
) -> None:
    for folder in ("tasks", "diffs", "transcripts"):
        (output / folder).mkdir(parents=True, exist_ok=True)
    rows = [task_row(result, rules) for result in results]
    with (output / "tasks.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=TASK_ROW)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_safe(row[key]) for key in TASK_ROW})
    for result in results:
        task_id = result.task["task_id"]
        (output / "tasks" / f"{task_id}.md").write_text(
            task_markdown(result, rules), encoding="utf-8"
        )
        (output / "diffs" / f"{task_id}-router.diff").write_text(
            result.attempts[-1].diff, encoding="utf-8"
        )
        if result.alone is not None:
            (output / "diffs" / f"{task_id}-claude-alone.diff").write_text(
                result.alone.diff, encoding="utf-8"
            )
        transcript = {
            "route": result.route.__dict__,
            "attempts": [attempt_record(a) for a in result.attempts],
            "claude_alone": attempt_record(result.alone) if result.alone else None,
        }
        (output / "transcripts" / f"{task_id}.json").write_text(
            json.dumps(transcript, indent=1, default=str), encoding="utf-8"
        )
    (output / "summary.md").write_text(
        summary_markdown(results, rows, calls, finished, rules["sie_credit_usd"]), encoding="utf-8"
    )


def attempt_record(attempt: Attempt) -> dict[str, Any]:
    record = {key: value for key, value in attempt.__dict__.items() if key not in ("usage", "log")}
    return record | {"usage": attempt.usage.__dict__, "log": attempt.log}


# --- The run ---------------------------------------------------------------------------------


def run(args: argparse.Namespace) -> Path | None:
    rules = load_rules(args.rules)
    client = make_client()
    claude = make_claude()
    calls = Calls()
    library = load_library()
    tasks = load_tasks()
    if args.task:
        tasks = [task for task in tasks if task["task_id"] == args.task]
        if not tasks:
            raise SystemExit(f"Stopped: no task {args.task} in data/tasks.json")
    output: Path = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    results: list[TaskResult] = []
    finished = False
    try:
        for task in tasks:
            result = solve_task(
                task, library, rules, client, calls, claude, not args.skip_claude_alone
            )
            results.append(result)
            last = result.attempts[-1]
            path = " -> ".join(a.level for a in result.attempts)
            alone = (
                ""
                if result.alone is None
                else f"; Claude alone: tests {'pass' if result.alone.passed else 'fail'}"
            )
            print(
                f"{task['task_id']}: {result.route.kind} -> {path}: tests "
                f"{'pass' if last.passed else 'fail'}{alone}"
            )
        finished = True
    finally:
        # A run that stops partway keeps the tasks it already paid for.
        if results:
            write_outputs(output, results, rules, calls, finished)
            print(f"Wrote {output}" + ("" if finished else " (stopped partway)"))
    print((output / "summary.md").read_text(encoding="utf-8"))
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--task", help="run one task, such as T05")
    parser.add_argument("--output", type=Path, help="folder for the outputs")
    parser.add_argument("--rules", type=Path, default=RULES_PATH, help="another rules.yaml to try")
    parser.add_argument(
        "--skip-claude-alone",
        action="store_true",
        help="don't also send every task to Claude alone",
    )
    args = parser.parse_args()
    try:
        run(args)
    except SIEStopped as error:
        raise SystemExit(f"Stopped: {error} Any finished tasks are in the run folder.") from None
    except (
        anthropic.AuthenticationError,
        anthropic.PermissionDeniedError,
        anthropic.NotFoundError,
    ) as error:
        raise SystemExit(
            f"Stopped: the Claude API refused the request, {type(error).__name__}: {error}. Check "
            "ANTHROPIC_API_KEY and the model name in rules.yaml. Any finished tasks are in the run "
            "folder."
        ) from None


if __name__ == "__main__":
    main()
