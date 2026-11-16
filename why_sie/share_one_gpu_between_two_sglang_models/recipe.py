"""Put two SGLang models on one 24 GB GPU three ways, and measure what each model keeps.

Setups, in order:
  defaults  plain SGLang at its default memory settings: the embedder, then the guard next to it
  by_hand   plain SGLang with --mem-fraction-static set by hand for each model
  sie       one SIE server running both, told how many KV-cache tokens each model needs

For by_hand and sie, each model runs alone, then both run at once, with the same requests in
flight, and the recipe reports the share of its alone throughput each model keeps.

Run it on the machine with the GPU. `--launcher docker` (the default) starts every server from the
SIE image; `--launcher process` starts the same commands as processes with `--server-python`, the
interpreter of an environment that has SGLang and SIE installed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from sie_sdk import SIEAsyncClient, SIEClient
from sie_sdk.client.errors import (
    ModelLoadingError,
    ProvisioningError,
    ServerError,
    SIEConnectionError,
)
from sie_sdk.types import Item

HERE = Path(__file__).resolve().parent
OUT_OF_MEMORY = ("out of memory", "not enough memory", "outofmemoryerror")


def load_rules(path: Path) -> dict[str, Any]:
    rules: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return rules


def load_texts(path: Path) -> list[str]:
    texts: list[str] = json.loads(path.read_text(encoding="utf-8"))
    return texts


# --- Starting and stopping servers -------------------------------------------------------------


@dataclass
class Server:
    name: str
    process: subprocess.Popen[bytes]
    log: Path
    container: str | None

    def running(self) -> bool:
        return self.process.poll() is None

    def log_text(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) != 0


class Launcher:
    """Starts a `python ...` command in the SIE image, with Docker or as a local process."""

    def __init__(
        self, kind: str, image: str, logs: Path, mounts: list[Path], python: str = "python"
    ) -> None:
        self.kind = kind
        self.image = image
        self.logs = logs
        self.mounts = mounts
        self.python = python
        self.servers: list[Server] = []
        self.gpu_not_empty: list[dict[str, Any]] = []

    def start(self, name: str, args: list[str], port: int) -> Server:
        if not port_free(port):
            raise RuntimeError(f"{name}: port {port} is already in use")
        log = self.logs / f"{name}.log"
        container = None
        if self.kind == "docker":
            container = f"why-sie-{name}-{os.getpid()}"
            hf_cache = Path.home() / ".cache" / "huggingface"
            cmd = ["docker", "run", "--rm", "--gpus", "all", "--network", "host"]
            cmd += ["--name", container, "-v", f"{hf_cache}:/app/.cache/huggingface"]
            for mount in self.mounts:
                cmd += ["-v", f"{mount}:{mount}"]
            cmd += ["--entrypoint", "python", self.image, *args]
        else:
            cmd = [self.python, *args]
        with log.open("wb") as fh:
            process = subprocess.Popen(
                cmd, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True
            )
        server = Server(name, process, log, container)
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
                if done.returncode != 0 and "no such container" not in done.stderr.lower():
                    print(f"docker rm -f {server.container} failed: {done.stderr.strip()}")
            stop_process(server)
        self.servers.clear()
        holders = wait_for_empty_gpu()
        if holders is None or holders:
            pids = "unknown" if holders is None else ", ".join(holders)
            print(f"WARNING: the GPU is not known to be empty; processes holding memory: {pids}")
            self.gpu_not_empty.append(
                {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "pids": holders}
            )


def stop_process(server: Server) -> None:
    for sig, wait_s in ((signal.SIGTERM, 30), (signal.SIGKILL, 15)):
        if not server.running():
            return
        try:
            os.killpg(server.process.pid, sig)
        except ProcessLookupError:
            return
        try:
            server.process.wait(timeout=wait_s)
            return
        except subprocess.TimeoutExpired:
            print(f"{server.name}: still running {wait_s}s after {sig.name}")


def gpu_pids() -> list[str] | None:
    """PIDs holding GPU memory, or None when nvidia-smi can't say."""
    if shutil.which("nvidia-smi") is None:
        return None
    done = subprocess.run(
        ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        print(f"nvidia-smi failed: {(done.stderr or done.stdout).strip()[:200]}")
        return None
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def wait_for_empty_gpu(timeout_s: float = 60.0) -> list[str] | None:
    """Wait until no process holds GPU memory; return the PIDs left, or None if unknown."""
    deadline = time.monotonic() + timeout_s
    while True:
        pids = gpu_pids()
        if pids == [] or time.monotonic() >= deadline:
            return pids
        time.sleep(2)


def wait_healthy(server: Server, url: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not server.running():
            return False
        try:
            with urllib.request.urlopen(url, timeout=5):
                return True
        except OSError:
            time.sleep(2)
    return False


def sglang_args(model: dict[str, Any], fraction: float | None, embedding: bool) -> list[str]:
    args = ["-m", "sglang.launch_server", "--host", "127.0.0.1", "--port", str(model["port"])]
    args += ["--model-path", model["model"], "--revision", model["revision"]]
    if embedding:
        args.append("--is-embedding")
    if fraction is not None:
        args += ["--mem-fraction-static", str(fraction)]
    return args


def sie_args(models_dir: Path, models: list[str], port: int) -> list[str]:
    args = ["-m", "sie_server.cli", "serve", "--host", "127.0.0.1", "--port", str(port)]
    return [*args, "--models-dir", str(models_dir), "--models", ",".join(models)]


def write_models_dir(rules: dict[str, Any], out: Path) -> Path:
    """Copy the shipped model configs and set each one's KV-cache budget from rules.yaml."""
    out.mkdir(parents=True, exist_ok=True)
    for key in ("embedder", "guard"):
        name = rules[key]["model"].replace("/", "__") + ".yaml"
        config = yaml.safe_load((HERE / "models" / name).read_text(encoding="utf-8"))
        config["profiles"]["default"]["kv_budget_tokens"] = rules[key]["kv_budget_tokens"]
        (out / name).write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return out


def memory_lines(log: str) -> list[str]:
    """The lines where SGLang or SIE says how much GPU memory it took."""
    keys = ("max_total_num_tokens", "mem_fraction", "kv budget", "reserv")
    return [
        line.strip()[:240] for line in log.splitlines() if any(k in line.lower() for k in keys)
    ][:12]


# --- Load ------------------------------------------------------------------------------------


class MissingUsage(Exception):
    """A reply came back without the token count the recipe measures."""


def field(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def token_count(usage: Any, key: str) -> int:
    value = field(usage, key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise MissingUsage(f"the reply has no usage.{key}")
    return value


@dataclass
class Count:
    requests: int = 0
    tokens: int = 0
    errors: int = 0
    missing_usage: int = 0
    first_error: str | None = None

    def error_share(self) -> float:
        failed = self.errors + self.missing_usage
        total = self.requests + failed
        return round(failed / total, 4) if total else 0.0


async def drive(
    calls: dict[str, Any],
    inputs: dict[str, list[str]],
    in_flight: int,
    seconds: float,
    warmup_s: float,
) -> dict[str, dict[str, Any]]:
    """Keep `in_flight` requests going per model; count what finishes after the warm-up."""
    counts = {name: Count() for name in calls}
    start = time.monotonic()
    deadline = start + seconds

    async def worker(name: str, offset: int) -> None:
        texts = inputs[name]
        i = offset
        while time.monotonic() < deadline:
            text = texts[i % len(texts)]
            i += 1
            count = counts[name]
            try:
                tokens = await calls[name](text)
            except MissingUsage as exc:
                if time.monotonic() - start >= warmup_s:
                    count.missing_usage += 1
                    count.first_error = count.first_error or f"MissingUsage: {exc}"[:200]
                continue
            except Exception as exc:
                if time.monotonic() - start >= warmup_s:
                    count.errors += 1
                    count.first_error = count.first_error or f"{type(exc).__name__}: {exc}"[:200]
                continue
            if time.monotonic() - start >= warmup_s:
                count.requests += 1
                count.tokens += tokens

    await asyncio.gather(*(worker(n, k * 97) for n in calls for k in range(in_flight)))
    measured = seconds - warmup_s
    return {
        name: {
            "requests_per_s": round(c.requests / measured, 2),
            "tokens_per_s": round(c.tokens / measured, 1),
            "requests": c.requests,
            "errors": c.errors,
            "missing_usage": c.missing_usage,
            "error_share": c.error_share(),
            "first_error": c.first_error,
        }
        for name, c in counts.items()
    }


def sglang_calls(http: httpx.AsyncClient, rules: dict[str, Any]) -> dict[str, Any]:
    embedder, guard = rules["embedder"], rules["guard"]

    async def embed(text: str) -> int:
        url = f"http://127.0.0.1:{embedder['port']}/v1/embeddings"
        response = await http.post(url, json={"model": embedder["model"], "input": text})
        response.raise_for_status()
        return token_count(field(response.json(), "usage"), "prompt_tokens")

    async def check(prompt: str) -> int:
        url = f"http://127.0.0.1:{guard['port']}/v1/chat/completions"
        body = {
            "model": guard["model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": guard["max_tokens"],
        }
        response = await http.post(url, json=body)
        response.raise_for_status()
        return token_count(field(response.json(), "usage"), "prompt_tokens")

    return {"embedder": embed, "guard": check}


def sie_calls(client: Any, rules: dict[str, Any]) -> dict[str, Any]:
    """`client` is an SIEAsyncClient, or any object with the same two async methods."""
    embedder, guard = rules["embedder"], rules["guard"]

    async def embed(text: str) -> int:
        result = await client.encode(embedder["model"], Item(text=text))
        return token_count(field(field(result, "request"), "usage"), "input_tokens")

    async def check(prompt: str) -> int:
        result = await client.chat_completions(
            guard["model"],
            [{"role": "user", "content": prompt}],
            max_tokens=guard["max_tokens"],
            temperature=0.0,
        )
        return token_count(field(result, "usage"), "prompt_tokens")

    return {"embedder": embed, "guard": check}


def retryable(exc: Exception) -> bool:
    """True for errors that mean the server is still loading: 503s and connection errors."""
    if isinstance(exc, SIEConnectionError | ModelLoadingError | ProvisioningError):
        return True
    return isinstance(exc, ServerError) and exc.status_code == 503


def warm_sie(
    server: Server, url: str, rules: dict[str, Any], names: list[str], timeout_s: float
) -> None:
    """Send one request per model, retrying while SIE loads it."""
    client = SIEClient(url, timeout_s=timeout_s)
    try:
        for name in names:
            model = rules[name]["model"]
            deadline = time.monotonic() + timeout_s
            while True:
                try:
                    if name == "embedder":
                        client.encode(model, Item(text="warm up"))
                    else:
                        client.chat_completions(
                            model, [{"role": "user", "content": "hello"}], max_tokens=4
                        )
                    break
                except Exception as exc:
                    if not retryable(exc) or not server.running():
                        raise
                    if time.monotonic() > deadline:
                        raise
                    print(f"{name}: not ready yet, retrying in 10s ({type(exc).__name__}: {exc})")
                    time.sleep(10)
    finally:
        client.close()


# --- The three setups ---------------------------------------------------------------------------


def kept(alone: dict[str, Any], together: dict[str, Any], name: str) -> float | None:
    metric = "tokens_per_s" if name == "embedder" else "requests_per_s"
    base = alone[name][metric]
    return round(together[name][metric] / base, 3) if base else None


def run_defaults(launcher: Launcher, rules: dict[str, Any]) -> dict[str, Any]:
    embedder, guard = rules["embedder"], rules["guard"]
    timeout = rules["load"]["start_timeout_s"]
    first = launcher.start(
        "defaults-embedder", sglang_args(embedder, None, embedding=True), embedder["port"]
    )
    first_up = wait_healthy(first, f"http://127.0.0.1:{embedder['port']}/health", timeout)
    second_up = False
    second_log = ""
    if first_up:
        second = launcher.start(
            "defaults-guard", sglang_args(guard, None, embedding=False), guard["port"]
        )
        second_up = wait_healthy(second, f"http://127.0.0.1:{guard['port']}/health", timeout)
        second_log = second.log_text()
    result = {
        "embedder_started": first_up,
        "guard_started_next_to_it": second_up,
        "guard_ran_out_of_memory": any(k in second_log.lower() for k in OUT_OF_MEMORY),
        "embedder_memory": memory_lines(first.log_text()),
    }
    launcher.stop_all()
    return result


def measure_pair(
    start_alone: Any,
    start_both: Any,
    calls: Any,
    rules: dict[str, Any],
    inputs: dict[str, list[str]],
) -> dict[str, Any]:
    load = rules["load"]
    phases: dict[str, Any] = {}
    for name in ("embedder", "guard"):
        if not start_alone(name):
            return {"error": f"{name} did not start alone"}
        result = asyncio.run(
            calls({name: inputs[name]}, load["in_flight"], load["alone_s"], load["warmup_s"])
        )
        phases[f"{name}_alone"] = result
    if not start_both():
        return {"error": "the two models did not start together", **phases}
    both = asyncio.run(calls(inputs, load["in_flight"], load["together_s"], load["warmup_s"]))
    alone = {n: phases[f"{n}_alone"][n] for n in ("embedder", "guard")}
    return {
        **phases,
        "together": both,
        "kept": {n: kept(alone, both, n) for n in ("embedder", "guard")},
    }


def run_by_hand(launcher: Launcher, rules: dict[str, Any], inputs: dict[str, list[str]]) -> Any:
    embedder, guard = rules["embedder"], rules["guard"]
    timeout = rules["load"]["start_timeout_s"]

    def start(name: str) -> bool:
        model = rules[name]
        server = launcher.start(
            f"by-hand-{name}",
            sglang_args(model, model["fraction_by_hand"], name == "embedder"),
            model["port"],
        )
        return wait_healthy(server, f"http://127.0.0.1:{model['port']}/health", timeout)

    def start_alone(name: str) -> bool:
        launcher.stop_all()
        return start(name)

    def start_both() -> bool:
        launcher.stop_all()
        return start("embedder") and start("guard")

    def calls(which: dict[str, list[str]], in_flight: int, seconds: float, warmup: float) -> Any:
        async def go() -> Any:
            async with httpx.AsyncClient(timeout=300.0) as http:
                every = sglang_calls(http, rules)
                chosen = {n: every[n] for n in which}
                return await drive(chosen, which, in_flight, seconds, warmup)

        return go()

    result = measure_pair(start_alone, start_both, calls, rules, inputs)
    result["fractions"] = {
        "embedder": embedder["fraction_by_hand"],
        "guard": guard["fraction_by_hand"],
    }
    launcher.stop_all()
    return result


def run_sie(
    launcher: Launcher, rules: dict[str, Any], inputs: dict[str, list[str]], models_dir: Path
) -> Any:
    port = rules["sie_port"]
    url = f"http://127.0.0.1:{port}"
    timeout = rules["load"]["start_timeout_s"]
    logs: list[Server] = []

    def start(names: list[str], tag: str) -> bool:
        launcher.stop_all()
        models = [rules[n]["model"] for n in names]
        server = launcher.start(f"sie-{tag}", sie_args(models_dir, models, port), port)
        logs.append(server)
        if not wait_healthy(server, f"{url}/healthz", timeout):
            return False
        warm_sie(server, url, rules, names, timeout)
        return True

    def calls(which: dict[str, list[str]], in_flight: int, seconds: float, warmup: float) -> Any:
        async def go() -> Any:
            client = SIEAsyncClient(url, timeout_s=300.0)
            try:
                every = sie_calls(client, rules)
                chosen = {n: every[n] for n in which}
                return await drive(chosen, which, in_flight, seconds, warmup)
            finally:
                await client.close()

        return go()

    result = measure_pair(
        lambda name: start([name], f"{name}-alone"),
        lambda: start(["embedder", "guard"], "together"),
        calls,
        rules,
        inputs,
    )
    result["kv_budget_tokens"] = {n: rules[n]["kv_budget_tokens"] for n in ("embedder", "guard")}
    result["memory"] = memory_lines(logs[-1].log_text()) if logs else []
    launcher.stop_all()
    return result


PHASES = ("embedder_alone", "guard_alone", "together")


def defaults_verdict(defaults: dict[str, Any]) -> dict[str, Any]:
    reason = None
    if "error" in defaults:
        reason = f"not measured: {defaults['error']}"
    elif not defaults["embedder_started"]:
        reason = "the embedder did not start on its own"
    elif defaults["guard_started_next_to_it"]:
        reason = "the guard started next to the embedder"
    elif not defaults["guard_ran_out_of_memory"]:
        reason = "the guard failed without an out-of-memory error; read logs/defaults-guard.log"
    verdict = {
        "name": "defaults",
        "expected": "the guard runs out of memory next to the embedder",
        "passed": reason is None,
    }
    if reason:
        verdict["reason"] = reason
    return verdict


def pair_verdict(
    setup: str, result: dict[str, Any], bar: float, max_error_share: float
) -> dict[str, Any]:
    ratios = result.get("kept") or {}
    error_shares = {
        f"{phase}.{name}": stats["error_share"]
        for phase in PHASES
        for name, stats in (result.get(phase) or {}).items()
    }
    reason = None
    if "error" in result:
        reason = f"not measured: {result['error']}"
    elif any(share > max_error_share for share in error_shares.values()):
        worst = max(error_shares, key=lambda k: error_shares[k])
        reason = f"too many failed requests: {worst} {error_shares[worst]:.1%}"
    elif not ratios or any(r is None for r in ratios.values()):
        missing = [n for n in ("embedder", "guard") if ratios.get(n) is None]
        reason = f"not measured: no throughput alone for {', '.join(missing)}"
    elif any(r < bar for r in ratios.values()):
        reason = "a model kept less than the bar"
    verdict = {
        "name": setup,
        "expected": (
            f"both start; each keeps {bar:.0%} of its alone throughput; "
            f"at most {max_error_share:.0%} of requests fail in any phase"
        ),
        "started": "error" not in result,
        "kept": ratios if "error" not in result else "not measured",
        "error_share": error_shares,
        "passed": reason is None,
    }
    if reason:
        verdict["reason"] = reason
    return verdict


def verdicts(summary: dict[str, Any], bar: float, max_error_share: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if summary.get("defaults"):
        out.append(defaults_verdict(summary["defaults"]))
    for setup in ("by_hand", "sie"):
        if summary.get(setup):
            out.append(pair_verdict(setup, summary[setup], bar, max_error_share))
    return out


def write_summary(out: Path, summary: dict[str, Any], rules: dict[str, Any]) -> None:
    summary["verdicts"] = verdicts(summary, rules["keep_at_least"], rules["max_error_share"])
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--rules", type=Path, default=HERE / "rules.yaml")
    parser.add_argument("--launcher", choices=["docker", "process"], default="docker")
    parser.add_argument(
        "--server-python",
        default="python",
        help="with --launcher process: the Python of an environment with SGLang and SIE installed",
    )
    parser.add_argument(
        "--setups", default="defaults,by_hand,sie", help="comma-separated: defaults,by_hand,sie"
    )
    parser.add_argument("--output", type=Path, help="run folder (default runs/<timestamp>)")
    args = parser.parse_args()

    rules = load_rules(args.rules)
    inputs = {
        "embedder": load_texts(HERE / "data" / "passages.json"),
        "guard": load_texts(HERE / "data" / "prompts.json"),
    }
    out = (args.output or HERE / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")).resolve()
    (out / "logs").mkdir(parents=True, exist_ok=True)
    models_dir = write_models_dir(rules, out / "models")
    launcher = Launcher(
        args.launcher, rules["image"], out / "logs", [models_dir], python=args.server_python
    )
    setups = args.setups.split(",")
    summary: dict[str, Any] = {
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "launcher": args.launcher,
        "image": rules["image"],
    }
    if args.launcher == "process":
        summary["server_python"] = args.server_python
    summary["gpu_not_empty"] = launcher.gpu_not_empty
    steps = {
        "defaults": "starting the embedder, then the guard next to it",
        "by_hand": "each model alone, then both",
        "sie": "each model alone, then both, in one SIE server",
    }
    try:
        launcher.stop_all()
        for setup, what in steps.items():
            if setup not in setups:
                continue
            print(f"{setup}: {what}")
            try:
                if setup == "defaults":
                    summary[setup] = run_defaults(launcher, rules)
                elif setup == "by_hand":
                    summary[setup] = run_by_hand(launcher, rules, inputs)
                else:
                    summary[setup] = run_sie(launcher, rules, inputs, models_dir)
            except Exception as exc:
                traceback.print_exc()
                summary[setup] = {"error": f"{type(exc).__name__}: {exc}"[:500]}
                launcher.stop_all()
            print(json.dumps(summary[setup], indent=2))
            write_summary(out, summary, rules)
    finally:
        launcher.stop_all()
        write_summary(out, summary, rules)
    print(json.dumps(summary["verdicts"], indent=2))
    print(f"wrote {out}")
    if not all(v["passed"] for v in summary["verdicts"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
