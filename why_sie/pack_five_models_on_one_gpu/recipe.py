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
import subprocess
import time
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from sie_sdk import SIEAsyncClient

HERE = Path(__file__).resolve().parent


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

    def __init__(self, kind: str, logs: Path, models_dir: str | None) -> None:
        self.kind = kind
        self.logs = logs
        self.models_dir = models_dir
        self.servers: list[Server] = []

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
            cmd = ["python", *args]
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
                subprocess.run(
                    ["docker", "rm", "-f", server.container], capture_output=True, check=False
                )
            if server.running():
                os.killpg(server.process.pid, signal.SIGTERM)
                try:
                    server.process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(server.process.pid, signal.SIGKILL)
                    server.process.wait(timeout=15)
        self.servers.clear()
        wait_for_empty_gpus()


def wait_for_empty_gpus(timeout_s: float = 60.0) -> None:
    if shutil.which("nvidia-smi") is None:
        time.sleep(10)
        return
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        out = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        if not out.strip():
            return
        time.sleep(2)


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


def make_calls(rules: dict[str, Any], route: dict[str, SIEAsyncClient], inputs: Inputs) -> Any:
    models = rules["models"]

    def plan(k: int) -> dict[str, Any]:
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
    plan: Any, steps: list[str], k: int, start: float, timeout_s: float
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
    plan: Any, steps: list[str], in_flight: int, seconds: float, timeout_s: float
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
    plan: Any, steps: list[str], rate: float, seconds: float, timeout_s: float, seed: int
) -> list[dict[str, Any]]:
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
            base = cells.get("one_per_model", {}).get(load, {}).get("p95_ms")
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


def verdicts(rows: list[dict[str, Any]], bar: float) -> list[dict[str, Any]]:
    out = []
    for setup in ("two_gpus", "one_gpu"):
        at_50 = next((r for r in rows if r["setup"] == setup and r["load_pct"] == 50), None)
        if at_50 is None:
            continue
        ratio = at_50["p95_vs_one_per_model"]
        out.append(
            {
                "setup": setup,
                "expected": f"p95 turn at 50% load within {bar}x one server per model",
                "ratio": ratio,
                "passed": ratio is not None and ratio <= bar,
            }
        )
    return out


# --- Running the setups ------------------------------------------------------------------------


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
        route: dict[str, str] = {}
        servers = []
        for gpu, keys in enumerate(groups):
            port = self.rules["first_port"] + gpu
            models = [self.rules["models"][k]["model"] for k in keys]
            image = image_for(self.rules, keys)
            servers.append(self.launcher.start(tag, gpu, port, models, image))
            for k in keys:
                route[k] = f"http://127.0.0.1:{port}"
        if not all(wait_healthy(s, self.load["start_timeout_s"]) for s in servers):
            return None
        return route

    def measure(self, route: dict[str, str], steps: list[str], rates: dict[int, float]) -> Any:
        async def go() -> Any:
            clients: dict[str, SIEAsyncClient] = {}
            for url in set(route.values()):
                clients[url] = SIEAsyncClient(url, timeout_s=self.load["timeout_s"])
            by_key = {k: clients[u] for k, u in route.items()}
            plan = make_calls(self.rules, by_key, self.inputs)
            try:
                await closed_loop(
                    plan,
                    steps,
                    self.load["warmup_in_flight"],
                    self.load["warmup_s"],
                    self.load["timeout_s"],
                )
                if not rates:
                    return await closed_loop(
                        plan,
                        steps,
                        self.load["capacity_in_flight"],
                        self.load["capacity_s"],
                        self.load["timeout_s"],
                    )
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
            finally:
                for client in clients.values():
                    await client.close()

        return asyncio.run(go())

    def passes(self, setup: str) -> list[tuple[list[list[str]], list[str]]]:
        """The (server groups, steps) passes a setup needs on the GPUs available."""
        groups = self.rules["setups"][setup]
        steps = self.rules["steps"]
        if len(groups) <= self.gpus:
            return [(groups, steps)]
        if setup == "one_per_model" and len(groups) == self.gpus + 1:
            last = groups[-1]
            return [(groups[:-1], [s for s in steps if s not in last]), ([last], list(last))]
        raise SystemExit(f"{setup} needs {len(groups)} GPUs; this machine has {self.gpus}")

    def capacity(self) -> float:
        rates = []
        for i, (groups, steps) in enumerate(self.passes("one_per_model")):
            route = self.start(f"capacity-{i}", groups)
            if route is None:
                raise SystemExit("the one-server-per-model setup did not start")
            rows = self.measure(route, steps, {})
            rates.append(turns_per_s(rows, self.load["discard_s"]))
        self.launcher.stop_all()
        return min(rates)

    def run_setup(self, setup: str, rates: dict[int, float]) -> dict[int, list[dict[str, Any]]]:
        results: list[dict[int, list[dict[str, Any]]]] = []
        for i, (groups, steps) in enumerate(self.passes(setup)):
            print(f"{setup}, pass {i + 1}: servers {groups}", flush=True)
            try:
                route = self.start(f"{setup}-{i}", groups)
            except RuntimeError as exc:
                print(f"  skipped: {exc}", flush=True)
                return {}
            if route is None:
                print("  a server did not start; see the logs", flush=True)
                return {}
            got = self.measure(route, steps, rates)
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
    parser.add_argument("--gpus", type=int, default=5, help="GPUs on this machine (4 or more)")
    parser.add_argument("--setups", default="one_per_model,two_gpus,one_gpu")
    parser.add_argument(
        "--models-dir", help="SIE model configs (process launcher; default: the server's own)"
    )
    parser.add_argument("--output", type=Path, help="run folder (default runs/<timestamp>)")
    args = parser.parse_args()

    rules = load_rules(args.rules)
    inputs = Inputs(
        load_texts(HERE / "data" / "messages.json"), load_texts(HERE / "data" / "passages.json")
    )
    out = (args.output or HERE / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")).resolve()
    (out / "logs").mkdir(parents=True, exist_ok=True)
    launcher = Launcher(args.launcher, out / "logs", args.models_dir)
    runner = Runner(rules, launcher, inputs, out, args.gpus)
    summary: dict[str, Any] = {
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "launcher": args.launcher,
        "gpus": args.gpus,
    }
    try:
        launcher.stop_all()
        print("capacity of one server per model", flush=True)
        capacity = runner.capacity()
        summary["capacity_turns_per_s"] = capacity
        print(f"capacity: {capacity} turns a second", flush=True)
        rates = {p: round(capacity * p / 100, 3) for p in rules["load"]["loads_pct"]}
        cells: dict[str, dict[int, dict[str, Any]]] = {}
        for setup in args.setups.split(","):
            got = runner.run_setup(setup, rates)
            if got:
                cells[setup] = {
                    p: cell(rows, rules["load"]["discard_s"]) for p, rows in got.items()
                }
    finally:
        launcher.stop_all()
    gpus = {s: len(g) for s, g in rules["setups"].items()}
    rows = table(cells, gpus, rules["gpu_month_usd"])
    summary.update(
        {
            "rates": rates,
            "table": rows,
            "cells": cells,
            "verdicts": verdicts(rows, rules["p95_ratio_at_50pct"]),
        }
    )
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    for r in rows:
        print(json.dumps(r))
    print(json.dumps(summary["verdicts"], indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
