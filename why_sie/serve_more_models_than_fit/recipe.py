"""Serve more small models than fit on one GPU, with one model pinned, and measure what it costs.

Two setups, one SIE server on one GPU each time, with SIE told to start unloading at half the card:
  fit       ten small models, which fit under that line
  overflow  sixteen, which don't

Traffic: a steady stream to the pinned model, and random arrivals for the others, split by a Zipf
popularity that shifts every few minutes. The recipe reads every load and unload from the server's
log and reports unloads an hour, how long a request waits when its model has to load, the latency
of everything else, and whether the pinned model ever had to load again.

Run it on the machine with the GPU. `--launcher docker` (the default) starts the server from the
SIE image; `--launcher process` runs the server from a local environment.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import random
import re
import shutil
import signal
import statistics
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sie_sdk import SIEAsyncClient, SIEClient
from sie_sdk.types import Item

HERE = Path(__file__).resolve().parent
LOG_LINE = re.compile(
    r"^(\d\d:\d\d:\d\d) .*?(Model '([^']+)' loaded successfully|Unloading model '([^']+)')"
)


def load_rules(path: Path) -> dict[str, Any]:
    rules: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return rules


def load_texts(path: Path, limit: int = 1000) -> list[str]:
    texts: list[str] = json.loads(path.read_text(encoding="utf-8"))
    return [t[:limit] for t in texts]


def models_for(rules: dict[str, Any], setup: str) -> list[dict[str, Any]]:
    """The unpinned models in a setup, in their starting popularity order."""
    models = list(rules["fit"])
    if setup == "overflow":
        models += rules["overflow"]
    return models


# --- The server --------------------------------------------------------------------------------


@dataclass
class Server:
    process: subprocess.Popen[bytes]
    log: Path
    container: str | None

    def running(self) -> bool:
        return self.process.poll() is None


def start_server(launcher: str, rules: dict[str, Any], log: Path, name: str) -> Server:
    port = str(rules["port"])
    env = {
        "SIE_PINNED_MODELS": rules["pinned"]["model"],
        "SIE_MEMORY_PRESSURE_THRESHOLD_PERCENT": str(rules["memory_pressure_percent"]),
        "TZ": "UTC",
        "PYTHONUNBUFFERED": "1",
    }
    args = ["serve", "--host", "127.0.0.1", "--port", port, "--preload", rules["pinned"]["model"]]
    container = None
    if launcher == "docker":
        container = f"why-sie-{name}-{os.getpid()}"
        hf_cache = Path.home() / ".cache" / "huggingface"
        cmd = ["docker", "run", "--rm", "--gpus", "all", "--network", "host", "--name", container]
        cmd += ["-v", f"{hf_cache}:/app/.cache/huggingface"]
        for key, value in env.items():
            cmd += ["-e", f"{key}={value}"]
        cmd += [rules["image"], *args, "--models-dir", "/app/models"]
        process_env = None
    else:
        cmd = ["python", "-m", "sie_server.cli", *args]
        process_env = {**os.environ, **env}
    with log.open("wb") as fh:
        process = subprocess.Popen(
            cmd, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True, env=process_env
        )
    return Server(process, log, container)


def stop_server(server: Server) -> None:
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


def wait_healthy(server: Server, url: str, timeout_s: float = 900.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not server.running():
            return False
        try:
            with urllib.request.urlopen(f"{url}/healthz", timeout=5):
                return True
        except OSError:
            time.sleep(2)
    return False


def warm_pinned(url: str, model: str, timeout_s: float = 900.0) -> None:
    client = SIEClient(url, timeout_s=timeout_s)
    try:
        client.encode(model, Item(text="warm up"))
    finally:
        client.close()


class GpuSampler:
    """Highest GPU memory in use, sampled once a second with nvidia-smi."""

    def __init__(self) -> None:
        self.peak_mib = 0
        self.total_mib = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        if shutil.which("nvidia-smi") is None:
            return
        query = [
            "nvidia-smi",
            "--query-gpu=memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ]
        while not self._stop.is_set():
            out = subprocess.run(query, capture_output=True, text=True, check=False).stdout
            first = out.strip().splitlines()[:1]
            if first:
                used, total = (int(x) for x in first[0].split(","))
                self.peak_mib = max(self.peak_mib, used)
                self.total_mib = total
            self._stop.wait(1.0)

    def __enter__(self) -> GpuSampler:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


# --- Traffic -----------------------------------------------------------------------------------


def zipf_weights(n: int, s: float) -> list[float]:
    weights = [1 / (rank + 1) ** s for rank in range(n)]
    total = sum(weights)
    return [w / total for w in weights]


def ranking_at(
    models: list[dict[str, Any]], elapsed_s: float, rotate_s: float
) -> list[dict[str, Any]]:
    """The popularity order at a moment: shifted by one model every `rotate_s` seconds."""
    shift = int(elapsed_s // rotate_s) % len(models)
    return models[shift:] + models[:shift]


async def call(
    client: SIEAsyncClient, model: dict[str, Any], text: str, labels: dict[str, list[str]]
) -> None:
    kind, name = model["kind"], model["model"]
    if kind == "encode":
        await client.encode(name, Item(text=text))
    elif kind == "score":
        await client.score(name, Item(text=text[:200]), [Item(text=text), Item(text=text[::-1])])
    else:
        chosen = labels["gliner"] if "gliner" in name else labels["zero_shot"]
        await client.extract(name, Item(text=text), labels=chosen)


async def drive(
    url: str, rules: dict[str, Any], models: list[dict[str, Any]], texts: list[str], seconds: float
) -> list[dict[str, Any]]:
    """Send the pinned stream and the Zipf traffic for `seconds`; one record per request."""
    traffic = rules["traffic"]
    pinned = {**rules["pinned"], "pinned": True}
    weights = zipf_weights(len(models), traffic["zipf_s"])
    rng = random.Random(20261113)
    client = SIEAsyncClient(url, timeout_s=traffic["timeout_s"])
    records: list[dict[str, Any]] = []
    pending: set[asyncio.Task[None]] = set()
    start = time.monotonic()

    async def fire(k: int, model: dict[str, Any]) -> None:
        sent = time.monotonic()
        record: dict[str, Any] = {
            "model": model["model"],
            "pinned": bool(model.get("pinned")),
            "t": round(sent - start, 3),
            "wall": round(time.time(), 3),
        }
        try:
            text = texts[k % len(texts)]
            await asyncio.wait_for(call(client, model, text, rules["labels"]), traffic["timeout_s"])
            record["ok"] = True
        except Exception as exc:
            record.update({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
        record["ms"] = round((time.monotonic() - sent) * 1000, 1)
        records.append(record)

    def launch(k: int, model: dict[str, Any]) -> None:
        task = asyncio.create_task(fire(k, model))
        pending.add(task)
        task.add_done_callback(pending.discard)

    k = 0
    next_other = rng.expovariate(traffic["rate_per_s"])
    next_pinned = 0.0
    while True:
        due = min(next_other, next_pinned)
        if due >= seconds:
            break
        delay = due - (time.monotonic() - start)
        if delay > 0:
            await asyncio.sleep(delay)
        if next_pinned <= next_other:
            launch(k, pinned)
            next_pinned += 1 / pinned["rate_per_s"]
        else:
            order = ranking_at(models, next_other, traffic["rotate_every_s"])
            launch(k, rng.choices(order, weights=weights)[0])
            next_other += rng.expovariate(traffic["rate_per_s"])
        k += 1
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await client.close()
    return records


# --- What happened -----------------------------------------------------------------------------


def log_events(log_text: str, day: dt.date) -> list[tuple[float, str, str]]:
    """(UTC timestamp, "load" or "unload", model) for every load and unload in the server log."""
    events = []
    for line in log_text.splitlines():
        match = LOG_LINE.match(line)
        if not match:
            continue
        clock = dt.time.fromisoformat(match.group(1))
        stamp = dt.datetime.combine(day, clock, tzinfo=dt.UTC).timestamp()
        kind = "load" if match.group(3) else "unload"
        events.append((stamp, kind, match.group(3) or match.group(4)))
    return events


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return round(statistics.quantiles(values, n=100, method="inclusive")[round(q * 100) - 1], 1)


@dataclass
class Summary:
    minutes: float
    requests: int
    failed: int
    loads: int
    unloads: int
    unloads_per_hour: float | None
    waited_for_load: int
    waited_p50_ms: float | None
    waited_p95_ms: float | None
    other_p50_ms: float | None
    other_p95_ms: float | None
    pinned_requests: int
    pinned_cold_starts: int
    pinned_failed: int
    pinned_p50_ms: float | None
    pinned_p99_ms: float | None
    peak_gpu_mib: int = 0
    gpu_total_mib: int = 0
    errors: list[str] = field(default_factory=list)


def summarize(
    records: list[dict[str, Any]], events: list[tuple[float, str, str]], warmup_s: float
) -> Summary:
    if not records:
        return Summary(0, 0, 0, 0, 0, None, 0, None, None, None, None, 0, 0, 0, None, None)
    start = min(r["wall"] for r in records)
    end = max(r["wall"] + r["ms"] / 1000 for r in records)
    hours = (end - start) / 3600
    window = [e for e in events if start - 60 <= e[0] <= end + 60]
    load_times: dict[str, list[float]] = {}
    for stamp, kind, model in window:
        if kind == "load":
            load_times.setdefault(model, []).append(stamp)

    def waited(r: dict[str, Any]) -> bool:
        lo, hi = r["wall"] - 1, r["wall"] + r["ms"] / 1000 + 1
        return any(lo <= t <= hi for t in load_times.get(r["model"], []))

    ok = [r for r in records if r["ok"]]
    cold = [r["ms"] for r in ok if waited(r)]
    warm = [r["ms"] for r in ok if not waited(r)]
    pinned = [r for r in records if r["pinned"] and r["wall"] - start >= warmup_s]
    pinned_ok = [r for r in pinned if r["ok"]]
    unloads = sum(e[1] == "unload" for e in window)
    return Summary(
        minutes=round((end - start) / 60, 1),
        requests=len(records),
        failed=len(records) - len(ok),
        loads=sum(e[1] == "load" for e in window),
        unloads=unloads,
        unloads_per_hour=round(unloads / hours) if hours else None,
        waited_for_load=len(cold),
        waited_p50_ms=quantile(cold, 0.50),
        waited_p95_ms=quantile(cold, 0.95),
        other_p50_ms=quantile(warm, 0.50),
        other_p95_ms=quantile(warm, 0.95),
        pinned_requests=len(pinned),
        pinned_cold_starts=sum(waited(r) for r in pinned_ok),
        pinned_failed=len(pinned) - len(pinned_ok),
        pinned_p50_ms=quantile([r["ms"] for r in pinned_ok], 0.50),
        pinned_p99_ms=quantile([r["ms"] for r in pinned_ok], 0.99),
        errors=sorted({(r.get("error") or "")[:100] for r in records if not r["ok"]})[:5],
    )


def verdicts(results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    fit, overflow = results.get("fit"), results.get("overflow")
    if fit:
        out.append(
            {
                "name": "fit",
                "expected": "no unloads once the models are loaded",
                "passed": fit["unloads"] == 0,
            }
        )
    if overflow:
        out.append(
            {
                "name": "overflow",
                "expected": "SIE unloads to make room, and requests whose model was unloaded wait",
                "passed": overflow["unloads"] > 0 and overflow["waited_for_load"] > 0,
            }
        )
    for name, result in results.items():
        out.append(
            {
                "name": f"{name}: pinned",
                "expected": "the pinned model never has to load again",
                "passed": result["pinned_cold_starts"] == 0 and result["pinned_requests"] > 0,
            }
        )
    return out


def run_setup(
    setup: str, launcher: str, rules: dict[str, Any], texts: list[str], out: Path, minutes: float
) -> dict[str, Any]:
    url = f"http://127.0.0.1:{rules['port']}"
    log = out / f"server-{setup}.log"
    server = start_server(launcher, rules, log, setup)
    try:
        if not wait_healthy(server, url):
            return {"error": "the server did not start", "log": log.name}
        warm_pinned(url, rules["pinned"]["model"])
        with GpuSampler() as gpu:
            records = asyncio.run(drive(url, rules, models_for(rules, setup), texts, minutes * 60))
        day = dt.datetime.fromtimestamp(min(r["wall"] for r in records), tz=dt.UTC).date()
        events = log_events(log.read_text(encoding="utf-8", errors="replace"), day)
    finally:
        stop_server(server)
    with (out / f"requests-{setup}.jsonl").open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    summary = summarize(records, events, rules["traffic"]["warmup_s"])
    summary.peak_gpu_mib, summary.gpu_total_mib = gpu.peak_mib, gpu.total_mib
    result = summary.__dict__
    result["models"] = 1 + len(models_for(rules, setup))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--rules", type=Path, default=HERE / "rules.yaml")
    parser.add_argument("--launcher", choices=["docker", "process"], default="docker")
    parser.add_argument("--setups", default="fit,overflow", help="comma-separated: fit,overflow")
    parser.add_argument("--minutes", type=float, help="per setup (default from rules.yaml)")
    parser.add_argument("--output", type=Path, help="run folder (default runs/<timestamp>)")
    args = parser.parse_args()

    rules = load_rules(args.rules)
    texts = load_texts(HERE / "data" / "passages.json")
    out = (args.output or HERE / "runs" / dt.datetime.now().strftime("%Y%m%d-%H%M%S")).resolve()
    out.mkdir(parents=True, exist_ok=True)
    minutes = args.minutes or rules["minutes"]
    results: dict[str, dict[str, Any]] = {}
    for setup in args.setups.split(","):
        count = 1 + len(models_for(rules, setup))
        print(f"{setup}: {count} models, {minutes:g} minutes")
        results[setup] = run_setup(setup, args.launcher, rules, texts, out, minutes)
        print(json.dumps(results[setup], indent=2))
    summary = {
        "started_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "launcher": args.launcher,
        "results": results,
        "verdicts": verdicts({k: v for k, v in results.items() if "error" not in v}),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary["verdicts"], indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
