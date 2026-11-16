"""Put two SGLang models on one 24 GB GPU three ways, and measure what each model keeps.

Setups, in order:
  defaults  plain SGLang at its default memory settings: the embedder, then the guard next to it
  by_hand   plain SGLang with --mem-fraction-static set by hand for each model
  sie       one SIE server running both, told how many KV-cache tokens each model needs

For by_hand and sie, each model runs alone, then both run at once, with the same requests in
flight, and the recipe reports the share of its alone throughput each model keeps.

Run it on the machine with the GPU. `--launcher docker` (the default) starts every server from the
SIE image; `--launcher process` starts the same commands as processes, for a machine that already
has that image's Python environment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from sie_sdk import SIEAsyncClient, SIEClient
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


class Launcher:
    """Starts a `python ...` command in the SIE image, with Docker or as a local process."""

    def __init__(self, kind: str, image: str, logs: Path, mounts: list[Path]) -> None:
        self.kind = kind
        self.image = image
        self.logs = logs
        self.mounts = mounts
        self.servers: list[Server] = []

    def start(self, name: str, args: list[str]) -> Server:
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
            cmd = ["python", *args]
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
                subprocess.run(
                    ["docker", "rm", "-f", server.container],
                    capture_output=True,
                    check=False,
                )
            if server.running():
                os.killpg(server.process.pid, signal.SIGTERM)
                try:
                    server.process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(server.process.pid, signal.SIGKILL)
                    server.process.wait(timeout=15)
        self.servers.clear()
        wait_for_empty_gpu()


def wait_for_empty_gpu(timeout_s: float = 60.0) -> None:
    """Wait until no process holds GPU memory, so the next setup starts on an empty card."""
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


@dataclass
class Count:
    requests: int = 0
    tokens: int = 0
    errors: int = 0
    first_error: str | None = None


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
            try:
                tokens = await calls[name](text)
                if time.monotonic() - start >= warmup_s:
                    counts[name].requests += 1
                    counts[name].tokens += tokens
            except Exception as exc:
                counts[name].errors += 1
                counts[name].first_error = (
                    counts[name].first_error or f"{type(exc).__name__}: {exc}"[:200]
                )

    await asyncio.gather(*(worker(n, k * 97) for n in calls for k in range(in_flight)))
    measured = seconds - warmup_s
    return {
        name: {
            "requests_per_s": round(c.requests / measured, 2),
            "tokens_per_s": round(c.tokens / measured, 1),
            "errors": c.errors,
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
        return int((response.json().get("usage") or {}).get("prompt_tokens") or 0)

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
        return int((response.json().get("usage") or {}).get("prompt_tokens") or 0)

    return {"embedder": embed, "guard": check}


def sie_calls(client: SIEAsyncClient, rules: dict[str, Any]) -> dict[str, Any]:
    embedder, guard = rules["embedder"], rules["guard"]

    async def embed(text: str) -> int:
        result = await client.encode(embedder["model"], Item(text=text))
        usage = (result.get("request") or {}).get("usage") or {}
        return int(usage.get("input_tokens") or 0)

    async def check(prompt: str) -> int:
        result = await client.chat_completions(
            guard["model"],
            [{"role": "user", "content": prompt}],
            max_tokens=guard["max_tokens"],
            temperature=0.0,
        )
        usage = result.get("usage") if isinstance(result, dict) else getattr(result, "usage", None)
        if isinstance(usage, dict):
            return int(usage.get("prompt_tokens") or 0)
        return int(getattr(usage, "prompt_tokens", 0) or 0)

    return {"embedder": embed, "guard": check}


def warm_sie(url: str, rules: dict[str, Any], names: list[str], timeout_s: float) -> None:
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
                except Exception:
                    if time.monotonic() > deadline:
                        raise
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
    first = launcher.start("defaults-embedder", sglang_args(embedder, None, embedding=True))
    first_up = wait_healthy(first, f"http://127.0.0.1:{embedder['port']}/health", timeout)
    second_up = False
    second_log = ""
    if first_up:
        second = launcher.start("defaults-guard", sglang_args(guard, None, embedding=False))
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
            f"by-hand-{name}", sglang_args(model, model["fraction_by_hand"], name == "embedder")
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
        server = launcher.start(f"sie-{tag}", sie_args(models_dir, models, port))
        logs.append(server)
        if not wait_healthy(server, f"{url}/healthz", timeout):
            return False
        warm_sie(url, rules, names, timeout)
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


def verdicts(summary: dict[str, Any], bar: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    defaults = summary.get("defaults")
    if defaults:
        out.append(
            {
                "name": "defaults",
                "expected": "the guard fails to start next to the embedder",
                "passed": defaults["embedder_started"] and not defaults["guard_started_next_to_it"],
            }
        )
    for setup in ("by_hand", "sie"):
        result = summary.get(setup)
        if result:
            ratios = result.get("kept") or {}
            out.append(
                {
                    "name": setup,
                    "expected": f"both start; each keeps {bar:.0%} of its alone throughput",
                    "started": "error" not in result,
                    "kept": ratios,
                    "passed": bool(ratios) and all((r or 0) >= bar for r in ratios.values()),
                }
            )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--rules", type=Path, default=HERE / "rules.yaml")
    parser.add_argument("--launcher", choices=["docker", "process"], default="docker")
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
    launcher = Launcher(args.launcher, rules["image"], out / "logs", [models_dir])
    setups = args.setups.split(",")
    summary: dict[str, Any] = {
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "launcher": args.launcher,
        "image": rules["image"],
    }
    try:
        launcher.stop_all()
        if "defaults" in setups:
            print("defaults: starting the embedder, then the guard next to it")
            summary["defaults"] = run_defaults(launcher, rules)
            print(json.dumps(summary["defaults"], indent=2))
        if "by_hand" in setups:
            print("by_hand: each model alone, then both")
            summary["by_hand"] = run_by_hand(launcher, rules, inputs)
            print(json.dumps(summary["by_hand"].get("kept"), indent=2))
        if "sie" in setups:
            print("sie: each model alone, then both, in one SIE server")
            summary["sie"] = run_sie(launcher, rules, inputs, models_dir)
            print(json.dumps(summary["sie"].get("kept"), indent=2))
    finally:
        launcher.stop_all()
    summary["verdicts"] = verdicts(summary, rules["keep_at_least"])
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary["verdicts"], indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
