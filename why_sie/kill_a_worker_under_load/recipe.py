"""Two GPU workers serving bge-m3, behind SIE's queue or behind a round-robin, while workers die.

Three measurements:
  capacity  each setup at stepped request rates: the highest rate with p95 under the target
  kill      steady load while worker 1 is killed and started again, three times
  engine    one SIE server with an SGLang guard model whose engine process is killed twice

The two setups:
  queue        SIE's gateway, a NATS JetStream queue and one worker (server + sidecar) per GPU
  round_robin  one SIE server per GPU, called in turn, skipping a server for a while after a
               connection error (what a proxy's passive health check does)

Run it on the machine with the GPUs. `--launcher docker` (the default) starts every piece from the
published images; `--launcher process` starts the same commands as processes, for a machine that
already has SIE's Python environment (pass its interpreter as `--server-python`), `nats-server` and
the gateway and sidecar binaries. Every piece listens on 127.0.0.1, except the sidecar's
probe port, which SIE v0.11.1 binds on every interface.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import random
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from sie_sdk import SIEAsyncClient, SIEClient

HERE = Path(__file__).resolve().parent
HOST = "127.0.0.1"
GATEWAY_PORT = 8080
WORKER_PORT = 8101
SIDECAR_PORT = 8201
NATS_PORT = 4222
NATS_URL = f"nats://{HOST}:{NATS_PORT}"
MODES = ("queue", "round_robin")
STEPS = ("capacity", "kill", "engine")
CONNECT_ERROR_TYPES = {"ClientConnectionError", "ConnectError", "RemoteProtocolError", "ReadError"}
# SIGKILL the SGLang processes found by command line (only those under the pid in argv[1] when one
# is given) and print how many. It runs inside the container in Docker mode, which has no pkill.
KILL_ENGINE = """
import os, signal, sys

def parent(pid):
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            stat = fh.read()
    except OSError:
        return None
    return int(stat[stat.rindex(b")") + 2 :].split()[1])

def cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read()
    except OSError:
        return b""

root = int(sys.argv[1]) if len(sys.argv) > 1 else None
pids = [int(p) for p in os.listdir("/proc") if p.isdigit() and int(p) != os.getpid()]
parents = {pid: parent(pid) for pid in pids}

def under_root(pid):
    seen = set()
    while pid is not None and pid not in seen:
        if pid == root:
            return True
        seen.add(pid)
        pid = parents.get(pid)
    return False

killed = 0
for pid in pids:
    if root is not None and not under_root(pid):
        continue
    cmd = cmdline(pid)
    if b"sglang.launch_server" in cmd or b"sglang::" in cmd:
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except OSError:
            pass
print(killed)
"""


def load_rules(path: Path) -> dict[str, Any]:
    rules: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return rules


def load_texts(path: Path) -> list[str]:
    texts: list[str] = json.loads(path.read_text(encoding="utf-8"))
    return texts


# --- Starting and stopping the pieces ----------------------------------------------------------


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex((HOST, port)) == 0


def docker_inspect(container: str) -> str | None:
    """The container's running state ("true" or "false"), or None when no such container exists."""
    done = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip() if done.returncode == 0 else None


@dataclass
class Piece:
    name: str
    process: subprocess.Popen[bytes]
    container: str | None
    ports: list[int]


@dataclass
class Launcher:
    """Starts one piece from its image with Docker, or the same command as a local process."""

    kind: str
    logs: Path
    mounts: list[Path]
    bin_dir: Path
    python: str
    pieces: dict[str, Piece] = field(default_factory=dict)

    def start(
        self,
        name: str,
        image: str,
        process_cmd: list[str],
        args: list[str],
        ports: list[int],
        env: dict[str, str] | None = None,
        gpu: int | None = None,
    ) -> Piece:
        for port in ports:
            if port_in_use(port):
                raise RuntimeError(
                    f"{name}: port {port} is already in use; stop what listens there "
                    "(a container left from an earlier run?) and run again"
                )
        env = env or {}
        container = None
        if self.kind == "docker":
            container = f"why-sie-{name}-{os.getpid()}"
            wait_removed(container, 60)
            cmd = ["docker", "run", "--rm", "--network", "host", "--name", container]
            if gpu is not None:
                cmd += ["--gpus", f"device={gpu}"]
            hf_cache = Path.home() / ".cache" / "huggingface"
            cmd += ["-v", f"{hf_cache}:/app/.cache/huggingface"]
            for mount in self.mounts:
                cmd += ["-v", f"{mount}:{mount}"]
            for key, value in env.items():
                cmd += ["-e", f"{key}={value}"]
            cmd += [image, *args]
            process_env = None
        else:
            cmd = [*process_cmd, *args]
            process_env = {**os.environ, **env}
            if gpu is not None:
                process_env["CUDA_VISIBLE_DEVICES"] = str(gpu)
        with (self.logs / f"{name}.log").open("ab") as log:
            process = subprocess.Popen(
                cmd, stdout=log, stderr=subprocess.STDOUT, env=process_env, start_new_session=True
            )
        piece = Piece(name, process, container, ports)
        self.pieces[name] = piece
        return piece

    def kill(self, name: str) -> None:
        """SIGKILL one piece and everything it started, the way a lost node or an OOM kill would.

        Returns only once the piece is gone: its container or process has stopped and its ports
        refuse connections. Raises RuntimeError if it is still up.
        """
        piece = self.pieces.pop(name, None)
        if piece is None:
            return
        if piece.container:
            done = subprocess.run(
                ["docker", "kill", piece.container], capture_output=True, text=True, check=False
            )
            if done.returncode != 0 and docker_inspect(piece.container) == "true":
                raise RuntimeError(f"docker kill {piece.container} failed: {done.stderr.strip()}")
        if piece.process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(piece.process.pid, signal.SIGKILL)
        piece.process.wait(timeout=30)
        confirm_gone(piece, 30)

    def stop_all(self) -> None:
        for name in list(self.pieces):
            try:
                self.kill(name)
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                print(f"could not stop {name}: {exc}", file=sys.stderr, flush=True)
        time.sleep(5)


def wait_removed(container: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while docker_inspect(container) is not None:
        if time.monotonic() > deadline:
            raise RuntimeError(f"container {container} was not removed within {timeout_s:.0f} s")
        time.sleep(0.5)


def confirm_gone(piece: Piece, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        if piece.container:
            running = docker_inspect(piece.container) == "true"
        else:
            running = piece.process.poll() is None
        answering = [p for p in piece.ports if port_in_use(p)]
        if not running and not answering:
            return
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"{piece.name} is still up after the kill (running: {running}, "
                f"ports answering: {answering})"
            )
        time.sleep(0.2)


def wait_http(url: str, timeout_s: float, piece: Piece | None = None) -> None:
    """Wait for `url` to answer 200; raise if the piece serving it exits or time runs out."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if piece is not None and piece.process.poll() is not None:
            raise RuntimeError(f"{piece.name} exited before {url} answered; see its log")
        try:
            with urllib.request.urlopen(url, timeout=5):
                return
        except OSError:
            time.sleep(2)
    raise RuntimeError(f"{url} did not answer within {timeout_s:.0f} s")


@dataclass
class Stack:
    """One of the two setups, with what it needs to kill and restart a worker."""

    launcher: Launcher
    rules: dict[str, Any]
    run_dir: Path
    mode: str
    work_dir: Path

    @property
    def gpus(self) -> list[int]:
        gpus: list[int] = self.rules["gpus"]
        return gpus

    @property
    def urls(self) -> list[str]:
        if self.mode == "queue":
            return [f"http://{HOST}:{GATEWAY_PORT}"]
        return [f"http://{HOST}:{WORKER_PORT + i}" for i in range(len(self.gpus))]

    def server_args(self, i: int, models: list[str] | None) -> list[str]:
        args = ["serve", "--host", HOST, "-p", str(WORKER_PORT + i)]
        args += ["--models-dir", str(self.run_dir / "config" / "models")]
        if models:
            args += ["--models", ",".join(models), "--preload", ",".join(models)]
        return args

    def start_worker(self, i: int) -> None:
        images = self.rules["images"]
        server = [self.launcher.python, "-m", "sie_server.cli"]
        model = self.rules["model"]
        ipc = str(self.work_dir / f"ipc-{i}.sock")
        common = {"SIE_DEPLOYMENT_ENV": "development", "PYTHONUNBUFFERED": "1"}
        port = [WORKER_PORT + i]
        if self.mode == "round_robin":
            env = {**common, "SIE_IPC_SOCKET_PATH": ipc}
            args = self.server_args(i, [model])
            self.launcher.start(
                f"server-{i}", images["server"], server, args, port, env, self.gpus[i]
            )
            return
        worker_env = {
            **common,
            "SIE_NATS_URL": NATS_URL,
            "SIE_CLUSTER_ROUTING": "queue",
            "SIE_IPC_REQUIRE_HEARTBEAT": "true",
            "SIE_MODEL_READY_TIMEOUT_S": "900",
            "SIE_IPC_SOCKET_PATH": ipc,
            "SIE_WORKER_ID": f"worker-{i}",
            "SIE_POOL": "default",
            "SIE_MACHINE_PROFILE": "default",
            "SIE_BUNDLE": "default",
        }
        args = ["serve", "-b", "default", "--host", HOST, "-p", str(WORKER_PORT + i)]
        args += ["--models-dir", str(self.run_dir / "config" / "models"), "--preload", model]
        self.launcher.start(
            f"worker-{i}", images["server"], server, args, port, worker_env, self.gpus[i]
        )
        sidecar_env = {
            "SIE_DEPLOYMENT_ENV": "development",
            "SIE_SIDECAR_INGEST": "nats",
            "SIE_NATS_URL": NATS_URL,
            "SIE_POOL": "default",
            "SIE_MACHINE_PROFILE": "default",
            "SIE_BUNDLE": "default",
            "SIE_IPC_SOCKET_PATH": ipc,
            "SIE_WORKER_ID": f"worker-{i}",
            "SIE_PAYLOAD_STORE_URL": str(self.work_dir / "payloads"),
            "SIE_WORKER_PROBE_PORT": str(SIDECAR_PORT + i),
            "SIE_MODEL_READY_TIMEOUT_S": "900",
            "SIE_POOL_ADMISSION_ENABLED": "false",
            "SIE_HEALTH_PUBLISH_ENABLED": "true",
            "SIE_TRACING_ENABLED": "false",
            "SIE_METRICS_ENABLED": "false",
            "SIE_GPU_COUNT": "1",
            "RUST_LOG": "info",
        }
        sidecar = [str(self.launcher.bin_dir / "sie-server-sidecar")]
        self.launcher.start(
            f"sidecar-{i}", images["sidecar"], sidecar, [], [SIDECAR_PORT + i], sidecar_env
        )

    def worker_ready(self, i: int, timeout_s: float = 900) -> None:
        name = f"worker-{i}" if self.mode == "queue" else f"server-{i}"
        piece = self.launcher.pieces.get(name)
        wait_http(f"http://{HOST}:{WORKER_PORT + i}/readyz", timeout_s, piece)
        if self.mode == "queue":
            sidecar = self.launcher.pieces.get(f"sidecar-{i}")
            wait_http(f"http://{HOST}:{SIDECAR_PORT + i}/readyz", 300, sidecar)

    def kill_worker(self, i: int) -> None:
        if self.mode == "round_robin":
            self.launcher.kill(f"server-{i}")
        else:
            self.launcher.kill(f"worker-{i}")
            self.launcher.kill(f"sidecar-{i}")

    def up(self) -> None:
        """Start every piece and warm it up; raise RuntimeError with the reason if one fails."""
        images = self.rules["images"]
        (self.work_dir / "payloads").mkdir(parents=True, exist_ok=True)
        if self.mode == "queue":
            jetstream = self.work_dir / f"jetstream-{time.monotonic_ns()}"
            nats_args = ["-a", HOST, "-p", str(NATS_PORT), "-js", "-sd", str(jetstream)]
            self.launcher.start("nats", images["nats"], ["nats-server"], nats_args, [NATS_PORT])
            time.sleep(2)
        for i in range(len(self.gpus)):
            self.start_worker(i)
        for i in range(len(self.gpus)):
            self.worker_ready(i)
        if self.mode == "queue":
            workers = ",".join(f"http://{HOST}:{WORKER_PORT + i}" for i in range(len(self.gpus)))
            lanes = [{"bundle": "default", "machineProfile": "default", "pool": "default"}]
            env = {
                "SIE_DEPLOYMENT_ENV": "development",
                "SIE_NATS_URL": NATS_URL,
                "SIE_GATEWAY_PORT": str(GATEWAY_PORT),
                "SIE_GATEWAY_WORKERS": workers,
                "SIE_GATEWAY_HEALTH_MODE": "nats",
                "SIE_PAYLOAD_STORE_URL": str(self.work_dir / "payloads"),
                "SIE_LOG_LEVEL": "info",
                "SIE_GATEWAY_CONFIGURED_GPUS": "default",
                "SIE_GATEWAY_ENABLE_POOLS": "true",
                "SIE_GATEWAY_CONFIGURED_PHYSICAL_LANES": json.dumps(lanes),
            }
            config = self.run_dir / "config"
            args = ["--host", HOST, "--port", str(GATEWAY_PORT)]
            args += [
                "--models-dir",
                str(config / "models"),
                "--bundles-dir",
                str(config / "bundles"),
            ]
            gateway = [str(self.launcher.bin_dir / "sie-gateway"), "serve"]
            piece = self.launcher.start(
                "gateway", images["gateway"], gateway, args, [GATEWAY_PORT], env
            )
            wait_http(f"http://{HOST}:{GATEWAY_PORT}/healthz", 120, piece)
        warm(self.urls, self.rules)


def warm(urls: list[str], rules: dict[str, Any]) -> None:
    batch = [{"text": "warm up"}] * rules["texts_per_request"]
    for url in urls:
        client = SIEClient(url, timeout_s=900)
        try:
            for _ in range(16):
                client.encode(rules["model"], batch, output_types=["dense"])
        finally:
            client.close()


# --- Load --------------------------------------------------------------------------------------


class Targets:
    """One URL, or several called in turn, with a passive health check like a proxy's."""

    def __init__(self, urls: list[str], fail_timeout_s: float, timeout_s: float) -> None:
        self.clients = [SIEAsyncClient(u, timeout_s=timeout_s) for u in urls]
        self._down_until = [0.0] * len(urls)
        self._next = 0
        self._fail_timeout_s = fail_timeout_s

    def pick(self) -> int:
        now = time.monotonic()
        for _ in range(len(self.clients)):
            i = self._next % len(self.clients)
            self._next += 1
            if self._down_until[i] <= now:
                return i
        i = self._next % len(self.clients)
        self._next += 1
        return i

    def mark_down(self, i: int) -> None:
        if self._fail_timeout_s > 0:
            self._down_until[i] = time.monotonic() + self._fail_timeout_s

    async def close(self) -> None:
        for client in self.clients:
            await client.close()


def is_connect_error(exc: BaseException) -> bool:
    """True when the exception, or one it was raised from, is a refused or dropped connection.

    The SDK wraps transport errors in SIEConnectionError, so this walks the cause chain and matches
    the aiohttp and httpx connection error classes by name, plus Python's own ConnectionError.
    Timeouts don't count: the server may still be working on the request.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, TimeoutError):
            return False
        if isinstance(current, ConnectionError):
            return True
        if any(t.__name__ in CONNECT_ERROR_TYPES for t in type(current).__mro__):
            return True
        current = current.__cause__ or current.__context__
    return False


async def poisson(rate: float, seconds: float, fire: Any, rng: random.Random) -> None:
    """Call fire(k) at Poisson arrivals for `seconds`, without waiting for answers."""
    pending: set[asyncio.Task[Any]] = set()
    t0 = time.monotonic()
    nxt = t0
    k = 0
    while True:
        nxt += rng.expovariate(rate)
        if nxt - t0 >= seconds:
            break
        delay = nxt - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        task = asyncio.create_task(fire(k))
        pending.add(task)
        task.add_done_callback(pending.discard)
        k += 1
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def encode_once(
    targets: Targets, texts: list[str], k: int, start: float, rules: dict[str, Any]
) -> dict[str, Any]:
    i = targets.pick()
    n = rules["texts_per_request"]
    batch = [{"text": texts[(k * n + j) % len(texts)]} for j in range(n)]
    sent = time.monotonic()
    record: dict[str, Any] = {"id": k, "target": i, "t": round(sent - start, 3)}
    try:
        await asyncio.wait_for(
            targets.clients[i].encode(rules["model"], batch, output_types=["dense"]),
            rules["request_timeout_s"],
        )
        record["ok"] = True
    except TimeoutError:
        record.update({"ok": False, "error": "client timeout"})
    except Exception as exc:
        if is_connect_error(exc):
            targets.mark_down(i)
        record.update({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
    record["ms"] = round((time.monotonic() - sent) * 1000, 1)
    return record


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, round(q * len(ordered) + 0.5 - 1e-9))
    return ordered[min(rank, len(ordered)) - 1]


def capacity(stack: Stack, texts: list[str]) -> dict[str, Any]:
    rules = stack.rules
    cap = rules["capacity"]
    gpus = len(stack.gpus)

    async def go() -> list[dict[str, Any]]:
        targets = Targets(stack.urls, 0.0, rules["request_timeout_s"])
        rng = random.Random(20261113)
        steps = []
        try:
            for per_gpu in cap["per_gpu_steps"]:
                rows: list[dict[str, Any]] = []
                start = time.monotonic()

                async def fire(k: int, start: float = start, rows: list[Any] = rows) -> None:
                    rows.append(await encode_once(targets, texts, k, start, rules))

                await poisson(per_gpu * gpus, cap["step_s"], fire, rng)
                kept = [r for r in rows if r["t"] >= cap["warmup_s"]]
                ms = [r["ms"] for r in kept if r["ok"]]
                steps.append(
                    {
                        "per_gpu": per_gpu,
                        "rate": per_gpu * gpus,
                        "requests": len(kept),
                        "errors": sum(not r["ok"] for r in kept),
                        "p50_ms": percentile(ms, 0.5),
                        "p95_ms": percentile(ms, 0.95),
                    }
                )
                print(f"capacity {stack.mode}: {per_gpu * gpus} req/s -> {steps[-1]}", flush=True)
        finally:
            await targets.close()
        return steps

    steps = asyncio.run(go())
    return {"steps": steps, "capacity_per_gpu": capacity_from(steps, cap["p95_target_ms"])}


def capacity_from(steps: list[dict[str, Any]], target_ms: float) -> float | None:
    """The highest step reached before the first one that misses the p95 target or has errors."""
    best = None
    for step in steps:
        if step["errors"] or step["p95_ms"] is None or step["p95_ms"] > target_ms:
            break
        best = step["per_gpu"]
    return best


def kill_test(stack: Stack, texts: list[str], out: Path) -> dict[str, Any]:
    rules = stack.rules
    kill = rules["kill"]
    fail_timeout = kill["round_robin_fail_timeout_s"] if stack.mode == "round_robin" else 0.0
    events: list[dict[str, Any]] = []

    async def kills(start: float) -> None:
        for at in kill["kill_at_s"]:
            await asyncio.sleep(max(0.0, start + at - time.monotonic()))
            event: dict[str, Any] = {}
            events.append(event)
            try:
                await asyncio.to_thread(stack.kill_worker, 1)
            except Exception as exc:
                event["error"] = f"kill: {exc}"
                return
            event["kill_s"] = round(time.monotonic() - start, 2)
            await asyncio.sleep(kill["restart_after_s"])
            try:
                await asyncio.to_thread(stack.start_worker, 1)
                event["restart_s"] = round(time.monotonic() - start, 2)
                await asyncio.to_thread(stack.worker_ready, 1)
            except Exception as exc:
                event["error"] = f"restart: {exc}"
                return
            event["restarted_ready_s"] = round(time.monotonic() - start, 2)

    async def go() -> list[dict[str, Any]]:
        targets = Targets(stack.urls, fail_timeout, rules["request_timeout_s"])
        rows: list[dict[str, Any]] = []
        start = time.monotonic()

        async def fire(k: int) -> None:
            rows.append(await encode_once(targets, texts, k, start, rules))

        try:
            rate = kill["per_gpu_rate"] * len(stack.gpus)
            killer = asyncio.create_task(kills(start))
            await poisson(rate, kill["seconds"], fire, random.Random(20261113))
            await killer
        finally:
            await targets.close()
        return rows

    rows = asyncio.run(go())
    with (out / f"kill-{stack.mode}.jsonl").open("w", encoding="utf-8") as fh:
        for row in sorted(rows, key=lambda r: r["id"]):
            fh.write(json.dumps(row) + "\n")
    return {"kills": events, **summarize_kill(rows, kill["slow_ms"])}


def summarize_kill(rows: list[dict[str, Any]], slow_ms: float) -> dict[str, Any]:
    ok = [r["ms"] for r in rows if r["ok"]]
    errors: dict[str, int] = {}
    for r in rows:
        if not r["ok"]:
            kind = str(r["error"]).split(":")[0]
            errors[kind] = errors.get(kind, 0) + 1
    return {
        "sent": len(rows),
        "answered": len(ok),
        "failed": len(rows) - len(ok),
        "errors": errors,
        "over_slow_ms": sum(1 for m in ok if m > slow_ms),
        "slowest_ms": max(ok) if ok else None,
        "p50_ms": percentile(ok, 0.5),
        "p99_ms": percentile(ok, 0.99),
    }


# --- Engine crash ------------------------------------------------------------------------------


def engine_test(launcher: Launcher, rules: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    eng = rules["engine"]
    prompts = load_texts(HERE / "data" / "prompts.json")
    args = ["serve", "--host", HOST, "-p", str(WORKER_PORT)]
    args += ["--models-dir", str(run_dir / "config" / "models")]
    args += ["--models", eng["model"], "--preload", eng["model"]]
    env = {"SIE_DEPLOYMENT_ENV": "development", "PYTHONUNBUFFERED": "1"}
    image = rules["images"]["sglang_server"]
    server = [launcher.python, "-m", "sie_server.cli"]
    piece = launcher.start(
        "engine-server", image, server, args, [WORKER_PORT], env, rules["gpus"][0]
    )
    url = f"http://{HOST}:{WORKER_PORT}"
    wait_http(f"{url}/readyz", 900, piece)
    events: list[dict[str, Any]] = []

    def kill_engine() -> int:
        if piece.container:
            cmd = ["docker", "exec", piece.container, "python", "-c", KILL_ENGINE]
        else:
            cmd = [sys.executable, "-c", KILL_ENGINE, str(piece.process.pid)]
        done = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise RuntimeError(f"the engine kill failed: {done.stderr.strip()[-300:]}")
        killed = int(done.stdout.strip() or "0")
        print(f"engine: killed {killed} SGLang processes", flush=True)
        if killed == 0:
            raise RuntimeError("found no SGLang process to kill")
        return killed

    async def go() -> list[dict[str, Any]]:
        client = SIEAsyncClient(url, timeout_s=60)
        rows: list[dict[str, Any]] = []
        start = time.monotonic()

        async def fire(k: int) -> None:
            sent = time.monotonic()
            row: dict[str, Any] = {"t": round(sent - start, 2)}
            try:
                await client.chat_completions(
                    eng["model"],
                    [{"role": "user", "content": prompts[k % len(prompts)]}],
                    max_tokens=eng["max_tokens"],
                    temperature=0.0,
                )
                row["ok"] = True
            except Exception as exc:
                row.update({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
            row["ms"] = round((time.monotonic() - sent) * 1000, 1)
            rows.append(row)

        async def killer() -> None:
            for at in eng["kill_at_s"]:
                await asyncio.sleep(max(0.0, start + at - time.monotonic()))
                kill_s = round(time.monotonic() - start, 2)
                try:
                    killed = await asyncio.to_thread(kill_engine)
                except Exception as exc:
                    events.append({"kill_s": kill_s, "error": str(exc)})
                    return
                events.append({"kill_s": kill_s, "killed": killed})

        try:
            task = asyncio.create_task(killer())
            await poisson(eng["rate"], eng["seconds"], fire, random.Random(20261113))
            await task
        finally:
            await client.close()
        return rows

    rows = asyncio.run(go())
    kills = [e["kill_s"] for e in events if "killed" in e]
    return {"kills": events, "recoveries": engine_recoveries(rows, kills)}


def engine_recoveries(rows: list[dict[str, Any]], kills: list[float]) -> list[dict[str, Any]]:
    """For each kill: how long until a request sent after it was answered, and how many failed.

    A request sent before the kill that was still running at the kill and then failed counts too.
    """
    out = []
    for n, at in enumerate(kills):
        since = kills[n - 1] if n else float("-inf")
        until = kills[n + 1] if n + 1 < len(kills) else float("inf")
        after = sorted((r for r in rows if at <= r["t"] < until), key=lambda r: r["t"])
        back = next((r for r in after if r["ok"] and r["t"] > at + 1), None)
        in_flight = [
            r
            for r in rows
            if not r["ok"] and since <= r["t"] < at and r["t"] + r["ms"] / 1000 >= at
        ]
        failed = sorted(in_flight, key=lambda r: r["t"]) + [
            r for r in after if not r["ok"] and (back is None or r["t"] < back["t"])
        ]
        out.append(
            {
                "kill_s": at,
                "answering_again_after_s": round(back["t"] - at, 1) if back else None,
                "failed_until_then": len(failed),
                "in_flight_failed": len(in_flight),
                "longest_failure_ms": max((r["ms"] for r in failed), default=None),
                "first_error": failed[0]["error"] if failed else None,
            }
        )
    return out


# --- Verdicts and main -------------------------------------------------------------------------


def not_measured(name: str, summary: dict[str, Any], step: str, modes: list[str]) -> dict[str, Any]:
    reasons = []
    for mode in modes:
        why = summary.get("setup_errors", {}).get(mode) or summary.get("step_errors", {}).get(
            f"{step} {mode}"
        )
        reasons.append(f"{mode}: {why or 'no result'}")
    return {"name": name, "passed": False, "reason": "not measured: " + "; ".join(reasons)}


def verdicts(summary: dict[str, Any], rules: dict[str, Any]) -> list[dict[str, Any]]:
    """One verdict for each step the run asked for; a step with no result is a failed verdict."""
    out: list[dict[str, Any]] = []
    expect = rules["expect"]
    if "capacity" in summary:
        cap = summary["capacity"]
        missing = [m for m in MODES if "steps" not in cap.get(m, {})]
        if missing:
            out.append(not_measured("capacity", summary, "capacity", missing))
        else:
            q, rr = (cap[m]["capacity_per_gpu"] for m in MODES)
            out.append(
                {
                    "name": "capacity",
                    "expected": "the queue handles at least as many requests a second per GPU",
                    "queue_per_gpu": q,
                    "round_robin_per_gpu": rr,
                    "passed": q is not None and rr is not None and q >= rr,
                }
            )
    if "kill" in summary:
        kill = summary["kill"]
        missing = [m for m in MODES if "failed" not in kill.get(m, {})]
        if missing:
            out.append(not_measured("kill", summary, "kill", missing))
        else:
            errors = [f"{m}: {e['error']}" for m in MODES for e in kill[m]["kills"] if "error" in e]
            verdict: dict[str, Any] = {
                "name": "kill",
                "expected": "the queue loses no request when a worker dies",
                "queue_failed": kill["queue"]["failed"],
                "round_robin_failed": kill["round_robin"]["failed"],
                "passed": not errors and kill["queue"]["failed"] <= expect["queue_lost_requests"],
            }
            if errors:
                verdict["reason"] = "a kill or restart did not complete: " + "; ".join(errors)
            out.append(verdict)
    if "engine" in summary:
        engine = summary["engine"]
        recoveries = engine.get("recoveries")
        if not recoveries:
            out.append(not_measured("engine", summary, "engine", ["engine"]))
        else:
            back_s = expect["engine_back_within_s"]
            fast_ms = expect["engine_fail_fast_ms"]
            errors = [e["error"] for e in engine.get("kills", []) if "error" in e]
            backs = [r["answering_again_after_s"] for r in recoveries]
            slowest = max((r["longest_failure_ms"] or 0 for r in recoveries), default=0)
            verdict = {
                "name": "engine",
                "expected": f"answering again within {back_s} s after every kill, "
                f"failed requests fail within {fast_ms} ms",
                "answering_again_after_s": backs,
                "longest_failure_ms": slowest,
                "passed": not errors
                and all(b is not None and b <= back_s for b in backs)
                and slowest < fast_ms,
            }
            if errors:
                verdict["reason"] = "an engine kill did not complete: " + "; ".join(errors)
            out.append(verdict)
    return out


def save(out: Path, summary: dict[str, Any], rules: dict[str, Any]) -> list[dict[str, Any]]:
    summary["verdicts"] = verdicts(summary, rules)
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    results: list[dict[str, Any]] = summary["verdicts"]
    return results


def write_config(out: Path) -> None:
    for sub in ("models", "bundles"):
        target = out / "config" / sub
        target.mkdir(parents=True, exist_ok=True)
        for src in (HERE / "config" / sub).glob("*.yaml"):
            (target / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def run_stacks(
    launcher: Launcher,
    rules: dict[str, Any],
    texts: list[str],
    out: Path,
    work: Path,
    steps: list[str],
    summary: dict[str, Any],
) -> None:
    for mode in MODES:
        launcher.stop_all()
        print(f"{mode}: starting", flush=True)
        stack = Stack(launcher, rules, out, mode, work)
        try:
            stack.up()
        except Exception as exc:
            summary["setup_errors"][mode] = f"{type(exc).__name__}: {exc}"
            print(f"{mode}: {summary['setup_errors'][mode]}", flush=True)
            save(out, summary, rules)
            continue
        if "capacity" in steps:
            try:
                summary["capacity"][mode] = capacity(stack, texts)
            except Exception as exc:
                summary["step_errors"][f"capacity {mode}"] = f"{type(exc).__name__}: {exc}"
            save(out, summary, rules)
        if "kill" in steps:
            print(f"{mode}: killing worker 1 under load", flush=True)
            try:
                summary["kill"][mode] = kill_test(stack, texts, out)
                print(json.dumps(summary["kill"][mode], indent=2), flush=True)
            except Exception as exc:
                summary["step_errors"][f"kill {mode}"] = f"{type(exc).__name__}: {exc}"
            save(out, summary, rules)
        launcher.stop_all()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--rules", type=Path, default=HERE / "rules.yaml")
    parser.add_argument("--launcher", choices=["docker", "process"], default="docker")
    parser.add_argument("--bin-dir", type=Path, default=Path("/usr/local/bin"))
    parser.add_argument(
        "--server-python",
        default="python",
        help="process launcher: the Python of an environment with SIE (and SGLang) installed",
    )
    parser.add_argument("--steps", default=",".join(STEPS), help=",".join(STEPS))
    parser.add_argument("--output", type=Path, help="run folder (default runs/<timestamp>)")
    args = parser.parse_args()
    steps = args.steps.split(",")
    unknown = sorted(set(steps) - set(STEPS))
    if unknown:
        parser.error(f"unknown steps: {', '.join(unknown)}")

    rules = load_rules(args.rules)
    texts = load_texts(HERE / "data" / "texts.json")
    out = (args.output or HERE / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")).resolve()
    (out / "logs").mkdir(parents=True, exist_ok=True)
    write_config(out)
    work = Path(tempfile.mkdtemp(prefix="why-sie-"))
    launcher = Launcher(args.launcher, out / "logs", [out, work], args.bin_dir, args.server_python)
    summary: dict[str, Any] = {
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "launcher": args.launcher,
        "setup_errors": {},
        "step_errors": {},
    }
    for step in STEPS:
        if step in steps:
            summary[step] = {}
    try:
        if "capacity" in steps or "kill" in steps:
            run_stacks(launcher, rules, texts, out, work, steps, summary)
        if "engine" in steps:
            launcher.stop_all()
            print("engine: killing the guard's SGLang process under load", flush=True)
            try:
                summary["engine"] = engine_test(launcher, rules, out)
                print(json.dumps(summary["engine"], indent=2), flush=True)
            except Exception as exc:
                summary["setup_errors"]["engine"] = f"{type(exc).__name__}: {exc}"
            save(out, summary, rules)
    finally:
        launcher.stop_all()
        results = save(out, summary, rules)
    print(json.dumps(results, indent=2))
    print(f"wrote {out}")
    if not all(v["passed"] for v in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
