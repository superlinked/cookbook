"""Find the step, and the agent, that broke a failed agent run.

Who:       A team that runs AI agents and reads the trace of every failed run, so they fix the
           step where the error came in rather than the step where the failure finally showed.
Input:     data/runs.json, three synthetic failed runs of a coding team of agents, from about
           5,500 to 420,000 tokens. data/expected.json holds each run's planted answer; it is
           read only to give each run a verdict at the end.
Whole:     Qwen/Qwen3.8-27B-FP8 on SIE reads a run in one call when it fits `whole_read.max_tokens`.
RLM:       A longer run goes to a recursive language model (RLM). Qwen writes Python against the
           run in a sandbox, one turn at a time, with helpers that search it (grep, show,
           steps_by) and helpers that ask Qwen about pieces of it (scan, ask). No helper sends
           more than `rlm.piece_tokens` in one call.
Output:    runs/<timestamp>/: results.csv, summary.md, and transcripts/<run>.md with the answer
           and, for the RLM, the code of every turn and what it printed.
Run using: SIE Serverless, or SIE Cluster or SIE Local with SIE_BASE_URL. The RLM runs model-written
           Python in DSPy's Deno sandbox (WebAssembly, no file or network access); uv installs Deno.

    uv run recipe.py
    uv run recipe.py --run long-swallowed-error
    uv run recipe.py --rlm-only
    uv run recipe.py --model <model>
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import dspy
import yaml
from dotenv import load_dotenv
from dspy.clients.lm15_boundary import request_kwargs, response_value
from sie_calls import WAIT_S, CallFailed, Calls, SIEStopped
from sie_sdk import SIEClient

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
RUNS_DIR = HERE / "runs"
RULES_PATH = HERE / "rules.yaml"
ANSWER_FIELDS = ("root_step", "responsible_agent", "reason")


# --------------------------------------------------------------------------------------------- data


@dataclass
class Run:
    run_id: str
    events: list[dict[str, Any]]  # {"step", "agent", "content"}, in order


def load_runs() -> list[Run]:
    rows = json.loads((DATA_DIR / "runs.json").read_text(encoding="utf-8"))
    return [Run(row["run_id"], row["events"]) for row in rows]


def load_expected() -> dict[str, dict[str, Any]]:
    rows = json.loads((DATA_DIR / "expected.json").read_text(encoding="utf-8"))
    return {row["run_id"]: row for row in rows}


def render(event: dict[str, Any]) -> str:
    return f"[step {event['step']} {event['agent']}]\n{event['content']}\n"


def estimate_tokens(text: str, rules: dict[str, Any]) -> int:
    """Characters over a fixed ratio: about 3 for English and code with Qwen's tokenizer."""
    return int(len(text) / rules["chars_per_token"]) + 1


def route(run: Run, rules: dict[str, Any], rlm_only: bool) -> str:
    size = estimate_tokens("".join(render(e) for e in run.events), rules)
    return "rlm" if rlm_only or size > rules["whole_read"]["max_tokens"] else "whole"


def overview(run: Run) -> str:
    counts = Counter(e["agent"] for e in run.events)
    return json.dumps(
        {
            "steps": len(run.events),
            "first_step": run.events[0]["step"],
            "last_step": run.events[-1]["step"],
            "steps_per_agent": dict(counts),
        }
    )


# --------------------------------------------------------------------------------------------- SIE


def make_client() -> SIEClient:
    """Build an SIE client from the API key and SIE_BASE_URL in the environment or `.env`."""
    load_dotenv()
    api_key = os.environ.get("SIE_API_KEY", "").strip()
    base_url = os.environ.get("SIE_BASE_URL", "").strip()
    if not api_key and not base_url:
        raise SystemExit("Stopped: set SIE_API_KEY, or SIE_BASE_URL for SIE Cluster or SIE Local.")
    base_url = base_url or "https://api.superlinked.com"
    return SIEClient(api_key=api_key or None, base_url=base_url, timeout_s=WAIT_S)


def chat_ok(reply: Any) -> bool:
    return (
        isinstance(reply, dict)
        and isinstance(reply.get("choices"), list)
        and bool(reply["choices"])
        and isinstance(reply["choices"][0].get("message"), dict)
    )


@dataclass
class Usage:
    """Model calls, tokens and the largest prompt, for one run."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    largest_input: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, reply: dict[str, Any]) -> None:
        usage = reply.get("usage") or {}
        prompt = int(usage.get("prompt_tokens") or 0)
        with self.lock:
            self.calls += 1
            self.input_tokens += prompt
            self.output_tokens += int(usage.get("completion_tokens") or 0)
            self.largest_input = max(self.largest_input, prompt)


@dataclass
class Reader:
    """What a model call needs: the client, the call record, this run's usage and the rules."""

    client: SIEClient
    calls: Calls
    usage: Usage
    rules: dict[str, Any]

    def chat(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        model, sampling = self.rules["models"]["reader"], self.rules["sampling"]
        reply = self.calls.call(
            model,
            lambda: self.client.chat_completions(
                model,
                messages,
                max_completion_tokens=max_tokens,
                temperature=sampling["temperature"],
                top_p=sampling["top_p"],
                response_format=response_format,
                provision_timeout_s=WAIT_S,
            ),
            chat_ok,
            "a message",
        )
        self.usage.add(reply)
        return cast(dict[str, Any], reply)


def text_of(reply: dict[str, Any]) -> str:
    return str(reply["choices"][0]["message"].get("content") or "")


# ------------------------------------------------------------------------------------- answers


ANSWER_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "root_step",
        "schema": {
            "type": "object",
            "properties": {
                "root_step": {"type": "integer"},
                "responsible_agent": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": list(ANSWER_FIELDS),
            "additionalProperties": False,
        },
    },
}


def parse_answer(text: str) -> dict[str, Any] | None:
    """The first JSON object in a reply that has a whole-number root_step and an agent."""
    for match in re.finditer(r"\{.*\}", text, re.S):
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if (
            isinstance(value, dict)
            and str(value.get("root_step", "")).strip().lstrip("-").isdigit()
        ):
            return {
                "root_step": int(value["root_step"]),
                "responsible_agent": str(value.get("responsible_agent", "")),
                "reason": str(value.get("reason", "")),
            }
    return None


def verdict(answer: dict[str, Any] | None, expected: dict[str, Any]) -> str:
    """Pass when the step and the agent are both the planted ones."""
    if not answer:
        return "Miss"
    same_step = answer["root_step"] == expected["root_step"]
    same_agent = (
        answer["responsible_agent"].strip().lower() == expected["responsible_agent"].lower()
    )
    return "Pass" if same_step and same_agent else "Miss"


# ---------------------------------------------------------------------------------- whole read


def whole_read(reader: Reader, run: Run) -> tuple[dict[str, Any] | None, str]:
    rules = reader.rules
    body = "".join(render(e) for e in run.events)
    messages = [
        {"role": "system", "content": rules["task"]},
        {
            "role": "user",
            "content": f"The run, {len(run.events)} steps:\n\n{body}\n{rules['answer_format']}",
        },
    ]
    reply = reader.chat(messages, rules["whole_read"]["max_completion_tokens"], ANSWER_SCHEMA)
    text = text_of(reply)
    return (
        parse_answer(text),
        f"## Whole read\n\nOne call, {reader.usage.largest_input:,} prompt tokens.\n\n"
        f"```json\n{text}\n```\n",
    )


# --------------------------------------------------------------------------------------------- RLM


class SIEEngine:
    """Lets DSPy call a chat model through SIEClient.

    It implements DSPy's engine interface, complete(Request) -> Response.

    DSPy turns its request into an OpenAI-style chat body and an OpenAI-style reply back into its
    response; SIEClient.chat_completions takes that body and returns that reply. With json_object,
    the engine tells DSPy it takes response schemas, so DSPy's JSON adapter sends the signature's
    own schema and SIE constrains the answer to it. Without that, DSPy only asks for JSON in the
    prompt and Qwen can answer in its tool-call syntax or with the wrong keys.
    """

    def __init__(self, reader: Reader, max_tokens: int, json_object: bool = False):
        self.reader, self.max_tokens, self.json_object = reader, max_tokens, json_object
        # Capability hints DSPy reads from a custom engine.
        self.supports_response_schema = json_object
        self.supported_params = ("temperature", "max_tokens") + (
            ("response_format",) if json_object else ()
        )

    def complete(self, request: Any) -> Any:
        body = request_kwargs(request, "chat")
        response_format = body.get("response_format")
        if response_format is None and self.json_object:
            response_format = {"type": "json_object"}
        reply = self.reader.chat(body["messages"], self.max_tokens, response_format)
        return response_value(reply, "chat", request)

    def stream(self, request: Any) -> Any:
        raise NotImplementedError("The recipe does not stream.")

    def close(self) -> None:
        return None


class RunEvents(dspy.SandboxSerializable):  # type: ignore[misc]
    """The whole run as a Python list in the sandbox, described to the model, not previewed."""

    def __init__(self, events: list[dict[str, Any]]):
        self.events = events

    def sandbox_setup(self) -> str:
        return "import json"

    def to_sandbox(self) -> bytes:
        return json.dumps(self.events, ensure_ascii=False).encode("utf-8")

    def sandbox_assignment(self, var_name: str, data_expr: str) -> str:
        return f"{var_name} = json.loads({data_expr})"

    def rlm_preview(self, max_chars: int = 500) -> str:
        return (
            f"Python list of all {len(self.events)} steps, each a dict with 'step', 'agent' "
            "and 'content'. "
            "Not previewed: read it with code or the helpers."
        )


def pieces(run: Run, budget_chars: int) -> list[tuple[int, int, str]]:
    """Consecutive pieces of the run of at most budget_chars, as (first step, last step, text).

    A step longer than a piece is split across pieces; each part keeps the step's header.
    """
    out: list[tuple[int, int, str]] = []
    first, last, text = -1, -1, ""
    for event in run.events:
        block = render(event)
        while block:
            room = budget_chars - len(text)
            if room < 200 and text:
                out.append((first, last, text))
                first, text = -1, ""
                continue
            part, block = block[:room], block[room:]
            if block:
                block = f"[step {event['step']} {event['agent']}, continued]\n{block}"
            first = event["step"] if first < 0 else first
            last = event["step"]
            text += part
    if text:
        out.append((first, last, text))
    return out


def make_helpers(reader: Reader, run: Run) -> list[Any]:
    """grep, show and steps_by read the run in Python; scan and ask send pieces of it to Qwen."""
    rules, rlm = reader.rules, reader.rules["rlm"]
    by_step = {e["step"]: e for e in run.events}
    budget = int(rlm["piece_tokens"] * rules["chars_per_token"])
    used, lock = [0], threading.Lock()

    def sub(prompt: str) -> str:
        with lock:
            if used[0] >= rlm["max_helper_calls"]:
                return "ERROR: the helper call budget is used up; decide from what you have."
            used[0] += 1
        try:
            text = text_of(
                reader.chat([{"role": "user", "content": prompt}], rlm["helper_completion_tokens"])
            )
        except CallFailed as error:
            return f"ERROR: {error}"
        limit = rlm["answer_chars"]
        return (
            text
            if len(text) <= limit
            else text[:limit] + f" [cut: {len(text) - limit} more characters]"
        )

    def grep(pattern: str, k: int = 20) -> list[list[Any]]:
        """Up to k [step, agent, snippet] for steps whose content matches the regex, any case."""
        found = []
        for event in run.events:
            match = re.search(pattern, event["content"], re.I)
            if match:
                start = max(0, match.start() - 120)
                found.append(
                    [event["step"], event["agent"], event["content"][start : match.end() + 120]]
                )
                if len(found) >= k:
                    break
        return found

    def show(step: int, start: int = 0, length: int = 3000) -> str:
        """An excerpt of one step's content, with its header."""
        event = by_step.get(int(step))
        if event is None:
            return f"No step {step}."
        content = str(event["content"])
        return (
            f"[step {step} {event['agent']}, characters {start}-{start + length} "
            f"of {len(content)}]\n" + content[start : start + length]
        )

    def steps_by(agent: str) -> list[int]:
        """The steps written by this agent."""
        return [e["step"] for e in run.events if e["agent"] == agent]

    def scan(question: str) -> list[dict[str, Any]]:
        """Ask the same question about every consecutive piece of the run, in parallel; returns
        [{'steps': [first, last], 'answer': text}]. Use it early, to find where errors,
        decisions and
        repairs are."""
        frame = (
            f"{rules['task']}\n\nAn excerpt of the run follows.\n\n{{text}}\n\n"
            f"Question: {question}\nAnswer briefly (at most 3 sentences) and cite step numbers. "
            "Say 'nothing relevant' if so."
        )
        parts = pieces(run, budget - len(frame))
        with ThreadPoolExecutor(max_workers=rlm["scan_parallel"]) as pool:
            answers = list(pool.map(lambda p: sub(frame.replace("{text}", p[2])), parts))
        return [{"steps": [p[0], p[1]], "answer": a} for p, a in zip(parts, answers, strict=True)]

    def ask(question: str, steps: list[int]) -> str:
        """Ask Qwen about specific steps' content, as many as fit one call, in the given order;
        the answer starts with a note naming any steps that did not fit."""
        frame = (
            f"{rules['task']}\n\nSteps of the run:\n\n{{text}}\n\nQuestion: {question}\n"
            "Answer briefly and cite step numbers."
        )
        text, left_out = "", []
        for step in steps:
            event = by_step.get(int(step))
            block = render(event) if event else ""
            if event and len(frame) + len(text) + len(block) <= budget:
                text += block
            else:
                left_out.append(step)
        note = f"[did not fit or do not exist: {left_out}] " if left_out else ""
        return note + sub(frame.replace("{text}", text)) if text else note + "No step fit."

    return [grep, show, steps_by, scan, ask]


def rlm_read(reader: Reader, run: Run) -> tuple[dict[str, Any] | None, str]:
    rules, rlm = reader.rules, reader.rules["rlm"]
    # The driver answers in JSON (DSPy's JSON adapter); llm_query() in the sandbox gets plain text.
    driver, plain = (
        dspy.LM(
            f"sie/{rules['models']['reader']}",
            engine=SIEEngine(reader, rlm["driver_completion_tokens"], json_object=json_object),
            cache=False,
            temperature=rules["sampling"]["temperature"],
            max_tokens=rlm["driver_completion_tokens"],
        )
        for json_object in (True, False)
    )
    signature = (
        dspy.Signature({}, rules["task"] + "\n" + rules["rlm_rules"])
        .append("agents", dspy.InputField(desc="The agents recorded in the run"), type_=list[str])
        .append(
            "request",
            dspy.InputField(desc="The task the run was given (first step, first 3,000 characters)"),
            type_=str,
        )
        .append(
            "outcome",
            dspy.InputField(desc="The last two steps, last 1,500 characters each"),
            type_=str,
        )
        .append("overview", dspy.InputField(desc="Step counts per agent"), type_=str)
        .append(
            "run_events",
            dspy.InputField(desc="The whole run in Python; not previewed"),
            type_=RunEvents,
        )
        .append(
            "responsible_agent",
            dspy.OutputField(desc="The agent that wrote the root step"),
            type_=str,
        )
        .append(
            "root_step",
            dspy.OutputField(desc="The step that introduced the decisive error"),
            type_=int,
        )
        .append(
            "reason", dspy.OutputField(desc="One or two sentences, quoting the step"), type_=str
        )
    )
    loop = dspy.RLM(
        signature,
        max_iters=rlm["max_turns"],
        max_llm_calls=rlm["max_llm_query_calls"],
        max_output_chars=rlm["output_chars"],
        sub_lm=plain,
        tools=make_helpers(reader, run),
    )
    # DSPy's generic prompt says llm_query takes ~500K characters; here one call takes far less.
    action = loop.generate_action.signature
    limit = rlm["piece_tokens"] * rules["chars_per_token"]
    note = (
        f"(one call to the same model; keep its prompt under {limit:,.0f} characters; "
        "for long text use scan() or ask())"
    )
    if "(~500K char capacity)" in action.instructions:
        action = action.with_instructions(
            action.instructions.replace("(~500K char capacity)", note)
        )
        loop.generate_action.signature = action
    last = run.events[-2:]
    with dspy.context(lm=driver, adapter=dspy.JSONAdapter()):
        result = loop(
            agents=sorted({e["agent"] for e in run.events}),
            request=render(run.events[0])[:3000],
            outcome="".join(render(e)[-1500:] for e in last),
            overview=overview(run),
            run_events=RunEvents(run.events),
        )
    answer = None
    if str(getattr(result, "root_step", "")).strip().lstrip("-").isdigit():
        answer = {
            "root_step": int(result.root_step),
            "responsible_agent": str(result.responsible_agent),
            "reason": str(result.reason),
        }
    turns = getattr(result, "trajectory", None) or []
    lines = [f"## RLM, {len(turns)} turns\n"]
    for i, turn in enumerate(turns, 1):
        lines += [
            f"### Turn {i}\n",
            str(turn.get("reasoning", ""))[:1500],
            "",
            "```python",
            str(turn.get("code", "")),
            "```",
            "",
            "```text",
            str(turn.get("output", ""))[:2000],
            "```",
            "",
        ]
    return answer, "\n".join(lines)


# --------------------------------------------------------------------------------------------- run


def new_run_dir() -> Path:
    """runs/<UTC timestamp>/, with a suffix when two runs start in the same second."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for n in range(100):
        out = RUNS_DIR / (stamp if n == 0 else f"{stamp}-{n}")
        try:
            out.mkdir(parents=True)
        except FileExistsError:
            continue
        (out / "transcripts").mkdir()
        return out
    raise SystemExit("Stopped: could not create a new folder under runs/.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--run", help="only this run_id")
    parser.add_argument("--rlm-only", action="store_true", help="send every run to the RLM")
    parser.add_argument("--model", help="use this model instead of models.reader in rules.yaml")
    args = parser.parse_args()
    rules = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    if args.model:
        rules["models"]["reader"] = args.model
    runs = [r for r in load_runs() if not args.run or r.run_id == args.run]
    if not runs:
        raise SystemExit(f"No run named {args.run}.")
    expected = load_expected()
    client, calls = make_client(), Calls()
    out = new_run_dir()
    rows = []
    for run in runs:
        way = route(run, rules, args.rlm_only)
        reader = Reader(client, calls, Usage(), rules)
        started = time.monotonic()
        print(
            f"{run.run_id}: {len(run.events)} steps, reading it "
            f"{'whole' if way == 'whole' else 'with the RLM'} ...",
            flush=True,
        )
        try:
            answer, transcript = (whole_read if way == "whole" else rlm_read)(reader, run)
            error = ""
        except SIEStopped:
            raise
        except Exception as exc:  # one run's failure is recorded; the others still run
            answer, transcript, error = (
                None,
                f"## Failed\n\n{type(exc).__name__}: {exc}\n",
                f"{type(exc).__name__}: {exc}",
            )
        want = expected[run.run_id]
        row = {
            "run_id": run.run_id,
            "read": way,
            "steps": len(run.events),
            "estimated_tokens": estimate_tokens("".join(render(e) for e in run.events), rules),
            "root_step": answer["root_step"] if answer else "",
            "responsible_agent": answer["responsible_agent"] if answer else "",
            "expected_step": want["root_step"],
            "expected_agent": want["responsible_agent"],
            "verdict": verdict(answer, want),
            "model_calls": reader.usage.calls,
            "input_tokens": reader.usage.input_tokens,
            "output_tokens": reader.usage.output_tokens,
            "largest_prompt_tokens": reader.usage.largest_input,
            "minutes": round((time.monotonic() - started) / 60, 1),
            "reason": answer["reason"] if answer else "",
            "error": error,
        }
        rows.append(row)
        (out / "transcripts" / f"{run.run_id}.md").write_text(
            f"# {run.run_id}\n\nAnswer: {json.dumps(answer)}\nExpected: {json.dumps(want)}\n"
            f"Verdict: {row['verdict']}\n\n{transcript}",
            encoding="utf-8",
        )
        print(
            f"  step {row['root_step']} by {row['responsible_agent']}: {row['verdict']} "
            f"({row['model_calls']} calls, largest prompt {row['largest_prompt_tokens']:,} tokens, "
            f"{row['minutes']} min)",
            flush=True,
        )
    with (out / "results.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    passed = sum(r["verdict"] == "Pass" for r in rows)
    lines = [
        f"# Results\n\n{passed} of {len(rows)} runs came out as expected.\n",
        "| Run | Read | Steps | Answer | Expected | Verdict | Calls | Largest prompt | Minutes |",
        "| --- | --- | ---: | --- | --- | --- | ---: | ---: | ---: |",
    ]
    lines += [
        f"| {r['run_id']} | {r['read']} | {r['steps']} | "
        f"step {r['root_step']} {r['responsible_agent']} | "
        f"step {r['expected_step']} {r['expected_agent']} | {r['verdict']} | {r['model_calls']} | "
        f"{r['largest_prompt_tokens']:,} | {r['minutes']} |"
        for r in rows
    ]
    credits = sum(calls.credits.values())
    lines.append(
        f"\nSIE calls: {calls.total}, failures: {len(calls.failures)}, "
        f"credits charged: {credits:.4f}"
        + (f" ({calls.unreported} calls did not report credits)" if calls.unreported else "")
    )
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"\n{passed} of {len(rows)} runs came out as expected. Written to {out.relative_to(HERE)}/"
    )


if __name__ == "__main__":
    main()
