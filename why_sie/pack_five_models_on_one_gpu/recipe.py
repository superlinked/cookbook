"""Serve an agent's five models three ways and send each the same turns.

One turn is five calls in a row: check the message with a guard model, embed it, rerank 20 passages,
classify it, and check a reply for personal details. Setups, from rules.yaml:

  one_per_model  one SIE server per model, each on its own GPU (the baseline)
  two_gpus       the guard on one GPU, the four encoders sharing a second
  one_gpu        all five models on one GPU

First the baseline's capacity (closed-loop turns), then Poisson turn arrivals at 25%, 50% and 75% of
it for every setup. With four GPUs instead of five, the baseline runs in two passes: four models on
four GPUs, then the PII model alone, and each baseline turn adds a PII call drawn at random from the
second pass. Run it on the machine with the GPUs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from sie_sdk import SIEAsyncClient

HERE = Path(__file__).resolve().parent
BASELINE = "one_per_model"

Call = Callable[[], Awaitable[Any]]
Plan = Callable[[int], dict[str, Call]]


def load_rules(path: Path) -> dict[str, Any]:
    rules: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return rules


def load_texts(path: Path) -> list[str]:
    texts: list[str] = json.loads(path.read_text(encoding="utf-8"))
    return texts


# --- Servers -----------------------------------------------------------------------------------


@dataclass
class Server:
    name: str
    port: int
    process: subprocess.Popen[bytes]
    log: Path
    container: str | None

    def running(self) -> bool:
        return self.process.poll() is None


class Launcher:
    """Starts `sie_server.cli serve` on one GPU, with Docker or as a local process."""

    def __init__(self, kind: str, logs: Path, models_dir: str | None, server_python: str) -> None:
        self.kind = kind
        self.logs = logs
        self.models_dir = models_dir
        self.server_python = server_python
        self.servers: list[Server] = []
        self.warnings: list[str] = []

    def start(self, name: str, gpu: int, port: int, models: list[str], image: str | None) -> Server:
        args = ["-m", "sie_server.cli", "serve", "--host", "127.0.0.1", "--port", str(port)]
        if self.models_dir:
            args += ["--models-dir", self.models_dir]
        args += ["--models", ",".join(models), "--preload", ",".join(models)]
        env = {"SIE_IPC_SOCKET_PATH": f"/tmp/why-sie-ipc-{port}.sock", "PYTHONUNBUFFERED": "1"}
        log = self.logs / f"{name}-{port}.log"
        container = None
        if self.kind == "docker":
            if image is None:
                raise RuntimeError(f"{name}: these models need different SIE images")
            container = f"why-sie-{name}-{port}-{os.getpid()}"
            hf_cache = Path.home() / ".cache" / "huggingface"
            cmd = ["docker", "run", "--rm", "--gpus", f"device={gpu}", "--network", "host"]
            cmd += ["--name", container, "-v", f"{hf_cache}:/app/.cache/huggingface"]
            for key, value in env.items():
                cmd += ["-e", f"{key}={value}"]
            cmd += ["--entrypoint", "python", image, *args]
            child_env = None
        else:
            cmd = [self.server_python, *args]
            child_env = {**os.environ, **env, "CUDA_VISIBLE_DEVICES": str(gpu)}
        with log.open("ab") as fh:
            process = subprocess.Popen(
                cmd, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True, env=child_env
            )
        server = Server(name, port, process, log, container)
        self.servers.append(server)
        return server

    def stop_all(self) -> None:
        for server in self.servers:
            if server.container:
                done = subprocess.run(
                    ["docker", "rm", "-f", server.container],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if done.returncode != 0 and "No such container" not in done.stderr:
                    print(
                        f"docker rm -f {server.container} failed: {done.stderr.strip()}",
                        flush=True,
                    )
            if server.running():
                stop_process(server)
        self.servers.clear()
        warning = wait_for_empty_gpus()
        if warning:
            print(f"WARNING: {warning}", flush=True)
            self.warnings.append(warning)


def stop_process(server: Server) -> None:
    for sig, wait_s in ((signal.SIGTERM, 30), (signal.SIGKILL, 15)):
        try:
            os.killpg(server.process.pid, sig)
        except ProcessLookupError:
            return
        try:
            server.process.wait(timeout=wait_s)
            return
        except subprocess.TimeoutExpired:
            pass
    print(f"{server.name} (pid {server.process.pid}) did not exit after SIGKILL", flush=True)


def gpu_pids() -> list[str] | None:
    """PIDs holding GPU memory, or None when nvidia-smi fails."""
    try:
        done = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def wait_for_empty_gpus(timeout_s: float = 60.0) -> str | None:
    """Wait until no process holds GPU memory; return a warning when that isn't confirmed."""
    if shutil.which("nvidia-smi") is None:
        time.sleep(10)
        return "nvidia-smi not found, so GPU memory was not checked between setups"
    deadline = time.monotonic() + timeout_s
    while True:
        pids = gpu_pids()
        if pids == []:
            return None
        if time.monotonic() >= deadline:
            break
        time.sleep(2)
    if pids is None:
        return "nvidia-smi failed, so GPU memory state between setups is unknown"
    return f"GPU memory still held after {timeout_s:.0f} s by PIDs {', '.join(pids)}"


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def wait_healthy(server: Server, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{server.port}/healthz"
    while time.monotonic() < deadline:
        if not server.running():
            return False
        try:
            with urllib.request.urlopen(url, timeout=5):
                return True
        except OSError:
            time.sleep(2)
    return False


def image_for(rules: dict[str, Any], keys: list[str]) -> str | None:
    """The one SIE image that serves all these models, or None when they need different images."""
    images = {rules["models"][k]["image"] for k in keys}
    return images.pop() if len(images) == 1 else None


# --- Turns -------------------------------------------------------------------------------------


@dataclass
class Inputs:
    messages: list[str]
    passages: list[str]


def make_calls(rules: dict[str, Any], route: dict[str, SIEAsyncClient], inputs: Inputs) -> Plan:
    models = rules["models"]

    def plan(k: int) -> dict[str, Call]:
        msg = inputs.messages[k % len(inputs.messages)]
        count = models["rerank"]["passages"]
        docs = [
            inputs.passages[(k * count + j) % len(inputs.passages)][:1500] for j in range(count)
        ]
        reply = inputs.passages[(k * 7 + 3) % len(inputs.passages)][:1500]
        return {
            "guard": lambda: route["guard"].chat_completions(
                models["guard"]["model"],
                [{"role": "user", "content": msg}],
                max_tokens=models["guard"]["max_tokens"],
                temperature=0.0,
            ),
            "embed": lambda: route["embed"].encode(
                models["embed"]["model"], {"text": msg}, output_types=["dense"]
            ),
            "rerank": lambda: route["rerank"].score(
                models["rerank"]["model"], {"text": msg}, [{"text": d} for d in docs]
            ),
            "classify": lambda: route["classify"].extract(
                models["classify"]["model"], {"text": msg}, labels=models["classify"]["labels"]
            ),
            "pii": lambda: route["pii"].extract(
                models["pii"]["model"], {"text": reply}, labels=models["pii"]["labels"]
            ),
        }

    return plan


async def one_turn(
    plan: Plan, steps: list[str], k: int, start: float, timeout_s: float
) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    sent = time.monotonic()
    ok = True
    todo = plan(k)
    for name in steps:
        t = time.monotonic()
        call: dict[str, Any] = {"step": name}
        try:
            await asyncio.wait_for(todo[name](), timeout_s)
            call["ok"] = True
        except Exception as exc:
            ok = False
            call.update({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
        call["ms"] = round((time.monotonic() - t) * 1000, 2)
        calls.append(call)
        if not ok:
            break
    return {
        "id": k,
        "t": round(sent - start, 3),
        "ok": ok,
        "turn_ms": round((time.monotonic() - sent) * 1000, 2),
        "calls": calls,
    }


async def closed_loop(
    plan: Plan, steps: list[str], in_flight: int, seconds: float, timeout_s: float
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start = time.monotonic()
    deadline = start + seconds
    counter = iter(range(10**9))

    async def worker() -> None:
        while time.monotonic() < deadline:
            rows.append(await one_turn(plan, steps, next(counter), start, timeout_s))

    await asyncio.gather(*(worker() for _ in range(in_flight)))
    return rows


async def poisson(
    plan: Plan, steps: list[str], rate: float, seconds: float, timeout_s: float, seed: int
) -> list[dict[str, Any]]:
    if rate <= 0:
        raise ValueError(f"Poisson arrivals need a positive rate, got {rate}")
    rng = random.Random(seed)
    rows: list[dict[str, Any]] = []
    pending: set[asyncio.Task[None]] = set()
    start = time.monotonic()
    nxt = start
    k = 0

    async def fire(k: int) -> None:
        rows.append(await one_turn(plan, steps, k, start, timeout_s))

    while True:
        nxt += rng.expovariate(rate)
        if nxt - start >= seconds:
            break
        delay = nxt - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        task = asyncio.create_task(fire(k))
        pending.add(task)
        task.add_done_callback(pending.discard)
        k += 1
    if pending:
        await asyncio.gather(*pending)
    return rows


def turns_per_s(rows: list[dict[str, Any]], discard_s: float) -> float:
    kept = [r for r in rows if r["ok"] and r["t"] >= discard_s]
    span = max((r["t"] for r in rows), default=0.0) - discard_s
    return round(len(kept) / span, 3) if span > 0 else 0.0


# --- Summary -----------------------------------------------------------------------------------


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def compose_baseline(
    four: list[dict[str, Any]], pii: list[dict[str, Any]], seed: int
) -> list[dict[str, Any]]:
    """A baseline turn: a four-model turn plus a PII call drawn from the PII-alone pass."""
    rng = random.Random(seed)
    if not pii:
        return []
    out = []
    for row in four:
        extra = rng.choice(pii)
        out.append(
            {
                **row,
                "ok": row["ok"] and extra["ok"],
                "turn_ms": row["turn_ms"] + extra["turn_ms"],
                "calls": row["calls"] + extra["calls"],
            }
        )
    return out


def cell(rows: list[dict[str, Any]], discard_s: float) -> dict[str, Any]:
    kept = [r for r in rows if r["t"] >= discard_s]
    ok = [r for r in kept if r["ok"]]
    per_call: dict[str, list[float]] = defaultdict(list)
    for r in ok:
        for c in r["calls"]:
            per_call[c["step"]].append(c["ms"])
    errors = sorted({c.get("error", "")[:80] for r in kept for c in r["calls"] if not c["ok"]})
    return {
        "turns": len(kept),
        "failed": len(kept) - len(ok),
        "p50_ms": quantile([r["turn_ms"] for r in ok], 0.5),
        "p95_ms": quantile([r["turn_ms"] for r in ok], 0.95),
        "call_p95_ms": {k: quantile(v, 0.95) for k, v in per_call.items()},
        "errors": errors[:3],
    }


def table(
    cells: dict[str, dict[int, dict[str, Any]]], gpus: dict[str, int], month_usd: float
) -> list[dict[str, Any]]:
    out = []
    for setup, by_load in cells.items():
        for load, c in sorted(by_load.items()):
            base = cells.get(BASELINE, {}).get(load, {}).get("p95_ms")
            ratio = round(c["p95_ms"] / base, 2) if base and c["p95_ms"] else None
            out.append(
                {
                    "setup": setup,
                    "load_pct": load,
                    "gpus": gpus[setup],
                    "monthly_usd": gpus[setup] * month_usd,
                    **{k: c[k] for k in ("p50_ms", "p95_ms", "turns", "failed")},
                    "p95_vs_one_per_model": ratio,
                }
            )
    return out


def not_measured(
    setup: str, at_50: dict[str, dict[str, Any]], skipped: dict[str, str]
) -> str | None:
    """Why a setup has no verdict at 50% load, or None when it has one."""
    if setup in skipped:
        return skipped[setup]
    row = at_50.get(setup)
    if row is None:
        return "no result at 50% load"
    if not row["turns"]:
        return "no turns counted at 50% load"
    if setup == BASELINE:
        return None
    base = at_50.get(BASELINE)
    if base is None or not base["turns"] or not base["p95_ms"]:
        why = skipped.get(BASELINE, "it was not run")
        return f"no {BASELINE} result at 50% load to compare with ({why})"
    return None


def verdicts(
    rows: list[dict[str, Any]],
    setups: list[str],
    bar: float,
    max_failed_share: float,
    skipped: dict[str, str],
) -> list[dict[str, Any]]:
    at_50 = {r["setup"]: r for r in rows if r["load_pct"] == 50}
    out = []
    for setup in setups:
        failures = f"at most {max_failed_share:.0%} of turns failed at 50% load"
        if setup == BASELINE:
            expected = f"the baseline, no latency bar; {failures}"
        else:
            expected = f"p95 turn at 50% load within {bar}x {BASELINE}; {failures}"
        verdict: dict[str, Any] = {"setup": setup, "expected": expected}
        reason = not_measured(setup, at_50, skipped)
        if reason:
            out.append({**verdict, "passed": False, "reason": f"not measured: {reason}"})
            continue
        row = at_50[setup]
        verdict.update({"turns": row["turns"], "failed": row["failed"]})
        problems = []
        if row["failed"] / row["turns"] > max_failed_share:
            problems.append(f"{row['failed']} of {row['turns']} turns failed")
        if setup != BASELINE:
            p95 = row["p95_ms"]
            ratio = p95 / at_50[BASELINE]["p95_ms"] if p95 else None
            verdict["ratio"] = None if ratio is None else round(ratio, 3)
            if ratio is None:
                problems.append("no turn succeeded")
            elif ratio > bar:
                problems.append(f"p95 turn {ratio:.3f}x {BASELINE}")
        verdict["passed"] = not problems
        if problems:
            verdict["reason"] = "; ".join(problems)
        out.append(verdict)
    return out


# --- Running the setups ------------------------------------------------------------------------


class NotMeasured(Exception):
    """A setup that could not start or could not run on this machine."""


class Runner:
    def __init__(
        self, rules: dict[str, Any], launcher: Launcher, inputs: Inputs, out: Path, gpus: int
    ) -> None:
        self.rules = rules
        self.launcher = launcher
        self.inputs = inputs
        self.out = out
        self.gpus = gpus
        self.load = rules["load"]

    def start(self, tag: str, groups: list[list[str]]) -> dict[str, str] | None:
        """Start one server per group, one GPU each; return each model key's URL."""
        self.launcher.stop_all()
        ports = [self.rules["first_port"] + gpu for gpu in range(len(groups))]
        busy = [p for p in ports if port_in_use(p)]
        if busy:
            raise SystemExit(
                f"ports {busy} are already in use on 127.0.0.1; stop what holds them "
                "or change first_port in rules.yaml"
            )
        route: dict[str, str] = {}
        servers = []
        for gpu, (port, keys) in enumerate(zip(ports, groups, strict=True)):
            models = [self.rules["models"][k]["model"] for k in keys]
            image = image_for(self.rules, keys)
            servers.append(self.launcher.start(tag, gpu, port, models, image))
            for k in keys:
                route[k] = f"http://127.0.0.1:{port}"
        if not all(wait_healthy(s, self.load["start_timeout_s"]) for s in servers):
            return None
        return route

    async def session[T](
        self, route: dict[str, str], steps: list[str], body: Callable[[Plan], Awaitable[T]]
    ) -> T:
        """Open clients, send the untimed warm-up turns, then run `body`."""
        clients: dict[str, SIEAsyncClient] = {}
        for url in set(route.values()):
            clients[url] = SIEAsyncClient(url, timeout_s=self.load["timeout_s"])
        plan = make_calls(self.rules, {k: clients[u] for k, u in route.items()}, self.inputs)
        try:
            await closed_loop(
                plan,
                steps,
                self.load["warmup_in_flight"],
                self.load["warmup_s"],
                self.load["timeout_s"],
            )
            return await body(plan)
        finally:
            for client in clients.values():
                await client.close()

    def measure_capacity(self, route: dict[str, str], steps: list[str]) -> list[dict[str, Any]]:
        async def body(plan: Plan) -> list[dict[str, Any]]:
            return await closed_loop(
                plan,
                steps,
                self.load["capacity_in_flight"],
                self.load["capacity_s"],
                self.load["timeout_s"],
            )

        return asyncio.run(self.session(route, steps, body))

    def measure_loads(
        self, route: dict[str, str], steps: list[str], rates: dict[int, float]
    ) -> dict[int, list[dict[str, Any]]]:
        async def body(plan: Plan) -> dict[int, list[dict[str, Any]]]:
            results = {}
            for pct, rate in rates.items():
                print(f"  {pct}% load: {rate:.2f} turns a second", flush=True)
                results[pct] = await poisson(
                    plan,
                    steps,
                    rate,
                    self.load["load_s"],
                    self.load["timeout_s"],
                    self.load["seed"] + pct,
                )
            return results

        return asyncio.run(self.session(route, steps, body))

    def passes(self, setup: str) -> list[tuple[list[list[str]], list[str]]]:
        """The (server groups, steps) passes a setup needs on the GPUs available."""
        groups = self.rules["setups"][setup]
        steps = self.rules["steps"]
        if len(groups) <= self.gpus:
            return [(groups, steps)]
        if setup == BASELINE and len(groups) == self.gpus + 1:
            last = groups[-1]
            return [(groups[:-1], [s for s in steps if s not in last]), ([last], list(last))]
        raise NotMeasured(f"{setup} needs {len(groups)} GPUs; this machine has {self.gpus}")

    def capacity(self) -> float:
        """The lower of the passes' closed-loop rates."""
        try:
            passes = self.passes(BASELINE)
        except NotMeasured as exc:
            raise SystemExit(str(exc)) from exc
        rates = []
        for i, (groups, steps) in enumerate(passes):
            route = self.start(f"capacity-{i}", groups)
            if route is None:
                raise SystemExit(f"the {BASELINE} setup did not start; see {self.launcher.logs}")
            rate = turns_per_s(self.measure_capacity(route, steps), self.load["discard_s"])
            if rate <= 0:
                raise SystemExit(f"baseline answered no turns; see logs in {self.launcher.logs}")
            rates.append(rate)
        self.launcher.stop_all()
        return min(rates)

    def run_setup(self, setup: str, rates: dict[int, float]) -> dict[int, list[dict[str, Any]]]:
        results: list[dict[int, list[dict[str, Any]]]] = []
        for i, (groups, steps) in enumerate(self.passes(setup)):
            print(f"{setup}, pass {i + 1}: servers {groups}", flush=True)
            try:
                route = self.start(f"{setup}-{i}", groups)
            except RuntimeError as exc:
                raise NotMeasured(str(exc)) from exc
            if route is None:
                raise NotMeasured(f"a server did not start; see {self.launcher.logs}")
            got = self.measure_loads(route, steps, rates)
            with (self.out / "turns.jsonl").open("a", encoding="utf-8") as fh:
                for pct, rows in got.items():
                    for r in rows:
                        fh.write(
                            json.dumps({"setup": setup, "pass": i, "load_pct": pct, **r}) + "\n"
                        )
            results.append(got)
        self.launcher.stop_all()
        if len(results) == 2:
            seed = self.load["seed"]
            pii_only = {
                p: [r for r in rows if r["t"] >= self.load["discard_s"]]
                for p, rows in results[1].items()
            }
            return {p: compose_baseline(rows, pii_only[p], seed) for p, rows in results[0].items()}
        return results[0]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--rules", type=Path, default=HERE / "rules.yaml")
    parser.add_argument("--launcher", choices=["docker", "process"], default="docker")
    parser.add_argument(
        "--server-python",
        default="python",
        help="process launcher: the Python of an environment with SIE and SGLang installed",
    )
    parser.add_argument("--gpus", type=int, default=5, help="GPUs on this machine (4 or more)")
    parser.add_argument("--setups", default="one_per_model,two_gpus,one_gpu")
    parser.add_argument(
        "--models-dir", help="SIE model configs (process launcher; default: the server's own)"
    )
    parser.add_argument("--output", type=Path, help="run folder (default runs/<timestamp>)")
    args = parser.parse_args()

    rules = load_rules(args.rules)
    setups = args.setups.split(",")
    unknown = [s for s in setups if s not in rules["setups"]]
    if unknown:
        parser.error(f"unknown setups {unknown}; rules.yaml has {list(rules['setups'])}")
    inputs = Inputs(
        load_texts(HERE / "data" / "messages.json"), load_texts(HERE / "data" / "passages.json")
    )
    out = (args.output or HERE / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")).resolve()
    (out / "logs").mkdir(parents=True, exist_ok=True)
    launcher = Launcher(args.launcher, out / "logs", args.models_dir, args.server_python)
    runner = Runner(rules, launcher, inputs, out, args.gpus)
    gpus = {s: len(g) for s, g in rules["setups"].items()}
    cells: dict[str, dict[int, dict[str, Any]]] = {}
    skipped: dict[str, str] = {}
    summary: dict[str, Any] = {
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "launcher": args.launcher,
        "gpus": args.gpus,
        "setups": setups,
        "skipped": skipped,
        "gpu_warnings": launcher.warnings,
    }

    def save() -> None:
        rows = table(cells, gpus, rules["gpu_month_usd"])
        summary.update(
            {
                "table": rows,
                "cells": cells,
                "verdicts": verdicts(
                    rows, setups, rules["p95_ratio_at_50pct"], rules["max_failed_share"], skipped
                ),
            }
        )
        (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    try:
        launcher.stop_all()
        print(f"capacity of {BASELINE}", flush=True)
        capacity = runner.capacity()
        summary["capacity_turns_per_s"] = capacity
        print(f"capacity: {capacity} turns a second", flush=True)
        rates = {p: round(capacity * p / 100, 3) for p in rules["load"]["loads_pct"]}
        summary["rates"] = rates
        for setup in setups:
            try:
                got = runner.run_setup(setup, rates)
            except NotMeasured as exc:
                print(f"  not measured: {exc}", flush=True)
                skipped[setup] = str(exc)
            else:
                cells[setup] = {
                    p: cell(rows, rules["load"]["discard_s"]) for p, rows in got.items()
                }
            save()
    finally:
        launcher.stop_all()
        save()
    for r in summary["table"]:
        print(json.dumps(r))
    print(json.dumps(summary["verdicts"], indent=2))
    print(f"wrote {out}")
    if not all(v["passed"] for v in summary["verdicts"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
