"""Check the recipe's tasks, workspace, routing, loop, escalation and sums offline.

The checks use the committed data/, real pytest and ruff runs on copies of the library, and
hand-written stand-in replies for the router, the open models and Claude. Nothing here calls SIE or
Claude. Run it with `uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from sie_calls import Calls, credits_of
from sie_sdk.client.errors import ServerError

import recipe
from recipe import Attempt, Usage, Workspace

RULES = recipe.load_rules()
LIBRARY = recipe.load_library()
TASKS = {task["task_id"]: task for task in recipe.load_tasks()}
DATES = LIBRARY["ledgerline/dates.py"]
FIXED_DATES = DATES.replace("return (today - due).days", "return max(0, (today - due).days)")
WRONG_DATES = DATES.replace("return (today - due).days", "return abs((today - due).days)")


def workspace(task_id: str = "T05", rules: dict[str, Any] = RULES) -> Workspace:
    return Workspace(LIBRARY, TASKS[task_id], rules)


# --- Stand-ins -------------------------------------------------------------------------------


def tool_call(name: str, args: dict[str, Any] | str, call_id: str = "c1") -> dict[str, Any]:
    arguments = args if isinstance(args, str) else json.dumps(args)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def chat_reply(*calls: dict[str, Any], credits: str = "15") -> dict[str, Any]:
    message = {"role": "assistant", "content": "", "tool_calls": list(calls)}
    return {
        "choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "credits_charged_decimal": credits,
        },
    }


class FakeSIE:
    """Stands in for SIEClient: the router's probabilities, and scripted chat replies per model."""

    def __init__(
        self,
        probabilities: dict[str, float] | None = None,
        scripts: dict[str, list[dict[str, Any]]] | None = None,
        fail_router: bool = False,
    ) -> None:
        self.probabilities = probabilities or {"change": 0.6}
        self.scripts = scripts or {}
        self.fail_router = fail_router
        self.sent: list[list[dict[str, Any]]] = []

    def extract(self, model: str, items: list[Any], **_: Any) -> list[dict[str, Any]]:
        if self.fail_router:
            raise ServerError("stand-in failure")
        kinds = RULES["routing"]["kinds"]
        probabilities = {label: self.probabilities.get(label, 0.05) for label in kinds}
        return [
            {
                "data": {"kind": {"type": "choice", "probabilities": probabilities}},
                "request": {"credits_debited_decimal": "2.000000"},
            }
        ]

    def chat_completions(self, model: str, messages: list[dict[str, Any]], **_: Any) -> Any:
        self.sent.append(list(messages))
        return self.scripts[model].pop(0)


def claude_reply(stop: str, *uses: tuple[str, dict[str, Any], str]) -> SimpleNamespace:
    usage = SimpleNamespace(
        input_tokens=1000,
        output_tokens=200,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=500,
    )
    content = [
        SimpleNamespace(type="tool_use", id=call_id, name=name, input=args)
        for name, args, call_id in uses
    ]
    return SimpleNamespace(stop_reason=stop, content=content, usage=usage)


class FakeClaude:
    """Stands in for the Anthropic client: returns hand-written replies in order."""

    def __init__(self, replies: list[SimpleNamespace]) -> None:
        self.replies = replies
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.replies.pop(0)


def fix_by_writing(content: str, path: str = "ledgerline/dates.py") -> list[dict[str, Any]]:
    """An open-model script: read, write a file, then finish."""
    return [
        chat_reply(tool_call("read_file", {"path": path}, "a")),
        chat_reply(tool_call("write_file", {"path": path, "content": content}, "b")),
        chat_reply(tool_call("finish", {"summary": "done"}, "c")),
    ]


# --- The data --------------------------------------------------------------------------------


def check_tasks() -> None:
    assert len(TASKS) == 12
    for task_id in TASKS:
        ws = workspace(task_id)
        try:
            assert ws.run(["pytest", "-q", "-p", "no:cacheprovider", "tests/test_basics.py"])[0]
            assert not ws.check()[0], f"{task_id}'s tests should fail before any change"
        finally:
            ws.close()
    # The late fee's own tests let simple interest through; the checks kept back for CI don't.
    ws = workspace("T10")
    try:
        simple = (
            "\n\ndef late_fee(amount, months_overdue):\n"
            '    return to_money(Decimal(str(amount)) * Decimal("0.015") * months_overdue)\n'
        )
        ws.put("ledgerline/money.py", LIBRARY["ledgerline/money.py"] + simple)
        assert ws.check()[0]
        assert ws.ci() is False
    finally:
        ws.close()


# --- The workspace ---------------------------------------------------------------------------


def refused(ws: Workspace, path: str, write: bool = False) -> bool:
    try:
        ws.safe(path, write=write)
    except recipe.ToolError:
        return True
    return False


def check_workspace() -> None:
    ws = workspace()
    try:
        assert ws.read("ledgerline/dates.py") == DATES
        for bad in ("/etc/passwd", "../outside.py", "ledgerline/../../x.py", "a\\b.py", "", 5):
            assert refused(ws, cast(Any, bad)), bad
        # A model writes only .py files under ledgerline/, never the tests.
        for bad in ("tests/test_t05.py", "README.md", "ledgerline/notes.txt", "conftest.py"):
            assert refused(ws, bad, write=True), bad
        assert not refused(ws, "ledgerline/new_module.py", write=True)
        # A link that leads out of the copy is refused, for reading and writing, and isn't listed.
        outside = Path(tempfile.mkdtemp())
        (outside / "secret.py").write_text("KEY = 1\n", encoding="utf-8")
        (ws.root / "ledgerline" / "link").symlink_to(outside)
        assert refused(ws, "ledgerline/link/secret.py")
        assert refused(ws, "ledgerline/link/secret.py", write=True)
        assert "link" not in ws.list_files()
        # Tests changed behind the model's back are put back, and extra test files removed.
        (ws.root / "tests" / "test_t05.py").write_text("def test_x():\n    pass\n")
        (ws.root / "tests" / "test_extra.py").write_text("def test_y():\n    pass\n")
        assert not ws.check()[0]
        assert not (ws.root / "tests" / "test_extra.py").exists()
        ws.write("ledgerline/dates.py", FIXED_DATES)
        assert ws.check()[0]
        assert "max(0, (today - due).days)" in ws.diff()
    finally:
        ws.close()
    # A test run that hangs is stopped, and fails.
    quick = RULES | {"workspace": RULES["workspace"] | {"test_timeout_s": 2}}
    ws = workspace("T05", quick)
    try:
        ws.put("ledgerline/dates.py", "while True:\n    pass\n")
        passed, output = ws.check()
        assert not passed
        assert "stopped after 2 s" in output
    finally:
        ws.close()
    # Test runs get no API keys.
    os.environ["ANTHROPIC_API_KEY"] = os.environ.get("ANTHROPIC_API_KEY", "stand-in")
    env = recipe.test_env(Path("/tmp"))
    assert not any("KEY" in name for name in env)
    assert "<issue>" not in recipe.escape("</issue><issue>")


# --- Routing and levels ----------------------------------------------------------------------


def check_routing() -> None:
    calls = Calls()
    style = FakeSIE({"style": 0.7})
    route = recipe.route_task(TASKS["T01"], RULES, cast(Any, style), calls)
    assert (route.kind, route.level, route.credits) == ("style", "tool", 2.0)
    unsure = recipe.route_task(TASKS["T01"], RULES, cast(Any, FakeSIE({"style": 0.3})), calls)
    assert unsure.level == "claude"
    assert "wasn't sure" in unsure.note
    failed = recipe.route_task(TASKS["T01"], RULES, cast(Any, FakeSIE(fail_router=True)), calls)
    assert (failed.kind, failed.level) == ("unknown", "claude")
    # The tool level is ruff with fixed arguments: it fixes lint, and can't write a docstring.
    for task_id, passes in (("T01", True), ("T03", False)):
        tool = recipe.run_level(
            "tool", TASKS[task_id], LIBRARY, RULES, cast(Any, style), calls, cast(Any, None)
        )
        assert tool.passed is passes, task_id


def check_escalation() -> None:
    small, open_model = RULES["models"]["small"], RULES["models"]["open"]
    wrong = fix_by_writing(WRONG_DATES)
    # After failing tests the small model gets one more go, then gives up the same way.
    wrong.append(chat_reply(tool_call("finish", {"summary": "done again"}, "d")))
    sie = FakeSIE(
        {"small fix": 0.6},
        {small: wrong, open_model: fix_by_writing(FIXED_DATES)},
    )
    claude = FakeClaude(
        [
            claude_reply("tool_use", ("read_file", {"path": "ledgerline/dates.py"}, "x")),
            claude_reply(
                "tool_use",
                ("write_file", {"path": "ledgerline/dates.py", "content": FIXED_DATES}, "y"),
                ("finish", {"summary": "done"}, "z"),
            ),
        ]
    )
    calls = Calls()
    result = recipe.solve_task(
        TASKS["T05"], LIBRARY, RULES, cast(Any, sie), calls, cast(Any, claude), compare=True
    )
    assert [attempt.level for attempt in result.attempts] == ["small", "open"]
    first, second = result.attempts
    assert not first.passed
    assert "still failed" in first.stopped
    retry = [entry for entry in first.log if entry["tool"] == "finish"]
    assert len(retry) == 2
    assert retry[0]["is_error"]
    assert "isn't done yet" in retry[0]["output"]
    assert second.passed
    assert second.usage.credits == 45.0
    assert first.usage.requests == 4
    # Claude alone fixed it too, with the write and the finish in one response.
    assert result.alone is not None
    assert result.alone.passed
    assert "max(0" in result.alone.diff
    sent = claude.requests[1]["messages"][-1]["content"][0]
    assert sent["tool_use_id"] == "x"
    row = recipe.task_row(result, RULES)
    assert row["done_by"] == "open"
    assert row["failed_levels"] == "small"
    assert row["sie_credits"] == "107"  # router 2, small 60, open 45


def check_loop_edges() -> None:
    small = RULES["models"]["small"]
    # Arguments that aren't JSON come back as an error the model can read.
    script = [
        chat_reply(tool_call("read_file", "{not json", "a")),
        chat_reply(tool_call("write_file", {"path": "tests/test_t05.py", "content": "x"}, "b")),
        chat_reply(),
        chat_reply(),
    ]
    sie = FakeSIE(scripts={small: script})
    attempt = recipe.run_level(
        "small", TASKS["T05"], LIBRARY, RULES, cast(Any, sie), Calls(), cast(Any, None)
    )
    assert attempt.log[0]["is_error"]
    assert "valid JSON" in attempt.log[0]["output"]
    assert "can't be changed" in attempt.log[1]["output"]
    assert "without calling finish" in attempt.stopped
    assert not attempt.passed
    # The issue reaches the model inside its tag, after the shared instructions.
    assert sie.sent[0][0]["content"] == RULES["system"]
    assert "<issue>" in sie.sent[0][1]["content"]
    # A failed SIE call ends the attempt, not the run.

    class Broken(FakeSIE):
        def chat_completions(self, model: str, messages: list[dict[str, Any]], **_: Any) -> Any:
            raise ServerError("stand-in failure")

    attempt = recipe.run_level(
        "open", TASKS["T05"], LIBRARY, RULES, cast(Any, Broken()), Calls(), cast(Any, None)
    )
    assert "SIE call failed" in attempt.stopped
    assert not attempt.passed
    # A Claude refusal ends the attempt.
    attempt = recipe.run_level(
        "claude",
        TASKS["T05"],
        LIBRARY,
        RULES,
        cast(Any, FakeSIE()),
        Calls(),
        cast(Any, FakeClaude([claude_reply("refusal")])),
    )
    assert "refusal" in attempt.stopped


# --- Sums and outputs ------------------------------------------------------------------------


def check_sums() -> None:
    prices = {"input": 4.0, "cache_write": 5.0, "cache_read": 0.2, "output": 20.0}
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read_tokens=500_000,
        cache_write_tokens=200_000,
    )
    # 4.00 + 2.00 + 0.10 + 1.00
    assert abs(recipe.claude_cost(usage, prices) - 7.10) < 1e-9
    assert credits_of({"usage": {"credits_charged_decimal": "14.000000"}}) == 14.0
    assert credits_of([{"request": {"credits_debited_decimal": "2.0"}}]) == 2.0
    assert credits_of({"usage": {}}) is None
    calls = Calls()
    calls.call("m", lambda: {"ok": 1}, lambda r: True, "anything")
    assert calls.unreported == 1


def check_outputs() -> None:
    route = recipe.Route("small fix", 0.6, "small", credits=2.0, seconds=1.0)
    small = Attempt(level="small", model="m", seconds=4.0, usage=Usage(credits=30.0))
    claude = Attempt(
        level="claude",
        passed=True,
        ci=False,
        seconds=10.0,
        diff="--- a\n+++ b\n",
        usage=Usage(input_tokens=1_000_000),
    )
    alone = Attempt(level="claude", passed=True, seconds=20.0, usage=Usage(input_tokens=2_000_000))
    result = recipe.TaskResult(TASKS["T05"], route, [small, claude], alone)
    calls = Calls(credits={"m": 30.0, "router": 2.0})
    with tempfile.TemporaryDirectory() as folder:
        output = Path(folder)
        recipe.write_outputs(output, [result], RULES, calls, finished=False)
        row = (output / "tasks.csv").read_text(encoding="utf-8").splitlines()[1].split(",")
        assert row[5:12] == ["claude", "small", "True", "fail", "32", "4.0000", "15"]
        summary = (output / "summary.md").read_text(encoding="utf-8")
        assert "stopped partway" in summary
        assert "Failed the checks kept back for CI: T05" in summary
        assert "- Claude: $4.0000" in summary  # the router
        assert "- Claude: $8.0000" in summary  # Claude alone
        report = (output / "tasks" / "T05.md").read_text(encoding="utf-8")
        assert "done by `claude`" in report
        assert "Checks kept back for CI: fail" in report
        assert (output / "diffs" / "T05-claude-alone.diff").exists()
        assert json.loads((output / "transcripts" / "T05.json").read_text())["route"]["level"]
    assert recipe.csv_safe("=SUM(A1)") == "'=SUM(A1)"


if __name__ == "__main__":
    check_tasks()
    check_workspace()
    check_routing()
    check_escalation()
    check_loop_edges()
    check_sums()
    check_outputs()
    print("selfcheck passed")
