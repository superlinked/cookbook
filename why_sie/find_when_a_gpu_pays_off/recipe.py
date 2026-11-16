"""Measure bge-m3 tokens a second on your GPU through SIE, and when renting that GPU pays off.

For each input mix (short queries, long passages, and 70% queries / 30% passages), the recipe keeps
4, 8, 16 and 32 requests of 8 texts in flight against one SIE server and counts the input tokens it
embeds. Then it works out the break-even: how busy that GPU has to be, on average, before renting
it all month costs less than paying per call.

Run it on the machine with the GPU. `--launcher docker` (the default) starts SIE from its image;
`--launcher process` starts the same command with `--server-python`, the interpreter of an
environment that has SIE installed; `--launcher none` uses a server already running at `--url`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from sie_sdk import (
    ModelLoadingError,
    ServerError,
    SIEAsyncClient,
    SIEClient,
    SIEConnectionError,
)

HERE = Path(__file__).resolve().parent


def load_rules(path: Path) -> dict[str, Any]:
    rules: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    mixes = rules["load"]["mixes"]
    named = {
        "cost.break_even_mix": rules["cost"]["break_even_mix"],
        "expect.short_mix": rules["expect"]["short_mix"],
        "expect.long_mix": rules["expect"]["long_mix"],
    }
    for key, mix in named.items():
        if mix not in mixes:
            raise ValueError(f"{key} is {mix!r}, which is not in load.mixes {sorted(mixes)}")
    return rules


def load_texts(path: Path) -> list[str]:
    texts: list[str] = json.loads(path.read_text(encoding="utf-8"))
    return texts


def build_mix(
    queries: list[str], passages: list[str], query_share: float, size: int, seed: int
) -> list[str]:
    """A fixed, shuffled list of `size` texts, `query_share` of them queries."""
    rng = random.Random(seed)
    n_queries = round(size * query_share)
    texts = [rng.choice(queries) for _ in range(n_queries)]
    texts += [rng.choice(passages) for _ in range(size - n_queries)]
    rng.shuffle(texts)
    return texts


# --- The server ------------------------------------------------------------------------------


@dataclass
class Server:
    process: subprocess.Popen[bytes] | None
    container: str | None


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def start_server(
    kind: str, rules: dict[str, Any], log: Path, models_dir: str | None, server_python: str
) -> Server:
    args = ["-m", "sie_server.cli", "serve", "--host", "127.0.0.1", "--port", str(rules["port"])]
    args += ["--models", rules["model"]]
    if models_dir:
        args += ["--models-dir", models_dir]
    if kind == "none":
        return Server(None, None)
    if port_in_use(rules["port"]):
        raise RuntimeError(
            f"port {rules['port']} is already in use; stop that server or use --launcher none"
        )
    gpu = str(rules["gpu_index"])
    env = dict(os.environ)
    container = None
    if kind == "docker":
        container = f"why-sie-gpu-{os.getpid()}"
        hf_cache = Path.home() / ".cache" / "huggingface"
        cmd = ["docker", "run", "--rm", "--gpus", f"device={gpu}", "--network", "host"]
        cmd += ["--name", container]
        cmd += [
            "-v",
            f"{hf_cache}:/app/.cache/huggingface",
            "--entrypoint",
            "python",
            rules["image"],
        ]
        cmd += args
    else:
        cmd = [server_python, *args]
        env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        env["CUDA_VISIBLE_DEVICES"] = gpu
    with log.open("wb") as fh:
        process = subprocess.Popen(
            cmd, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True, env=env
        )
    return Server(process, container)


def stop_server(server: Server) -> None:
    if server.container:
        done = subprocess.run(
            ["docker", "rm", "-f", server.container],
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode != 0:
            print(f"docker rm -f {server.container} failed: {done.stderr.strip()}")
    if server.process and server.process.poll() is None:
        os.killpg(server.process.pid, signal.SIGTERM)
        try:
            server.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(server.process.pid, signal.SIGKILL)


def wait_ready(server: Server, url: str, model: str, timeout_s: float) -> None:
    """Wait for the server to answer, then send one request so the model is loaded."""
    deadline = time.monotonic() + timeout_s
    while True:
        if server.process and server.process.poll() is not None:
            raise RuntimeError("the SIE server exited; see its log in the run folder")
        try:
            with urllib.request.urlopen(f"{url}/healthz", timeout=5):
                break
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(2)
    client = SIEClient(url, timeout_s=timeout_s)
    try:
        while True:
            try:
                client.encode(model, [{"text": "warm up"}], output_types=["dense"])
                return
            except (SIEConnectionError, ModelLoadingError, ServerError) as exc:
                if isinstance(exc, ServerError) and exc.status_code != 503:
                    raise
                if server.process and server.process.poll() is not None:
                    raise RuntimeError(
                        "the SIE server exited; see its log in the run folder"
                    ) from exc
                if time.monotonic() > deadline:
                    raise
                print(f"warm-up: {type(exc).__name__}: {exc}; retrying in 10 s")
                time.sleep(10)
    finally:
        client.close()


def gpu_busy_percent(gpu_index: int) -> float | None:
    if shutil.which("nvidia-smi") is None:
        return None
    out = subprocess.run(
        [
            "nvidia-smi",
            f"--id={gpu_index}",
            "--query-gpu=utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    return float(out[0]) if out else None


# --- Load ------------------------------------------------------------------------------------


def input_tokens(results: list[dict[str, Any]]) -> int | None:
    """The input tokens SIE reports for one response, or None when it carries no usage."""
    usage = (results[0].get("request") or {}).get("usage") if results else None
    if not usage or usage.get("input_tokens") is None:
        return None
    return int(usage["input_tokens"])


def summarize_cell(
    in_flight: int,
    records: list[dict[str, Any]],
    busy: list[float],
    seconds: float,
    warmup_s: float,
    max_bad_share: float,
) -> dict[str, Any]:
    """Tokens a second, p50, errors and missing usage, over the requests sent after the warm-up."""
    counted = [r for r in records if r["sent_s"] >= warmup_s]
    errors = sum(1 for r in counted if r["error"])
    ok = [r for r in counted if not r["error"]]
    missing_usage = sum(1 for r in ok if r["tokens"] is None)
    tokens = sum(r["tokens"] or 0 for r in ok)
    latencies = sorted(r["latency_s"] for r in ok)
    bad = errors + missing_usage
    return {
        "in_flight": in_flight,
        "tokens_per_s": round(tokens / (seconds - warmup_s)),
        "requests": len(counted),
        "errors": errors,
        "missing_usage": missing_usage,
        "excluded": not counted or bad > max_bad_share * len(counted),
        "p50_ms": round(latencies[len(latencies) // 2] * 1000) if latencies else None,
        "gpu_busy_pct": round(sum(busy) / len(busy)) if busy else None,
    }


async def measure_cell(
    url: str,
    model: str,
    texts: list[str],
    in_flight: int,
    load: dict[str, Any],
    gpu_index: int,
) -> dict[str, Any]:
    """Keep `in_flight` encode requests going and record each one."""
    client = SIEAsyncClient(url, timeout_s=300.0)
    per_request, seconds, warmup_s = load["texts_per_request"], load["cell_s"], load["warmup_s"]
    records: list[dict[str, Any]] = []
    busy: list[float] = []
    start = time.monotonic()
    deadline = start + seconds

    async def worker(offset: int) -> None:
        i = offset
        while time.monotonic() < deadline:
            batch = [{"text": texts[(i + k) % len(texts)]} for k in range(per_request)]
            i += per_request
            sent = time.monotonic()
            try:
                results = await client.encode(model, batch, output_types=["dense"])
            except Exception:
                records.append({"sent_s": sent - start, "error": True})
                await asyncio.sleep(load["error_pause_s"])
                continue
            records.append(
                {
                    "sent_s": sent - start,
                    "error": False,
                    "tokens": input_tokens(results),
                    "latency_s": time.monotonic() - sent,
                }
            )

    async def sampler() -> None:
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            if time.monotonic() - start >= warmup_s:
                value = await asyncio.to_thread(gpu_busy_percent, gpu_index)
                if value is not None:
                    busy.append(value)

    try:
        await asyncio.gather(sampler(), *(worker(k * 389) for k in range(in_flight)))
    finally:
        await client.close()
    return summarize_cell(in_flight, records, busy, seconds, warmup_s, load["max_bad_share"])


# --- Break-even ------------------------------------------------------------------------------


def break_even(tokens_per_s: float, cost: dict[str, Any]) -> dict[str, Any]:
    """How busy the GPU must be, on average, before renting it all month beats paying per call."""
    monthly = cost["gpu_price_per_hour"] * cost["hours_per_month"]
    tokens_for_that = monthly / cost["per_call_price_per_million_tokens"] * 1_000_000
    needed_per_s = tokens_for_that / (cost["hours_per_month"] * 3600)
    return {
        "gpu_per_month_usd": round(monthly, 2),
        "tokens_that_buys_per_month": round(tokens_for_that),
        "tokens_per_s_around_the_clock": round(needed_per_s),
        "busy_share_to_break_even": round(needed_per_s / tokens_per_s, 4) if tokens_per_s else None,
    }


def best(cells: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The fastest cell, skipping cells with too many errors or responses without usage."""
    kept = [c for c in cells if not c["excluded"]]
    return max(kept, key=lambda c: c["tokens_per_s"]) if kept else None


def format_share(share: float | None) -> str:
    return "n/a (no tokens measured)" if share is None else f"{share:.1%} busy"


def verdicts(
    mixes: dict[str, dict[str, Any]], expect: dict[str, Any], break_even_mix: str
) -> dict[str, str]:
    """Pass or Miss for each expectation in rules.yaml, from the measured mixes."""
    short, long = mixes.get(expect["short_mix"]), mixes.get(expect["long_mix"])
    out: dict[str, str] = {}

    short_ok = bool(
        short
        and long
        and short["best_tokens_per_s"] is not None
        and long["best_tokens_per_s"]
        and short["best_tokens_per_s"]
        < expect["short_below_share_of_long"] * long["best_tokens_per_s"]
    )
    out[expect["short_mix"]] = "Pass" if short_ok else "Miss"

    long_ok = False
    if long and long["best_tokens_per_s"]:
        kept = [c["tokens_per_s"] for c in long["cells"] if not c["excluded"]]
        floor = (1 - expect["long_cells_within"]) * long["best_tokens_per_s"]
        long_ok = len(kept) == len(long["cells"]) and min(kept) >= floor
    out[expect["long_mix"]] = "Pass" if long_ok else "Miss"

    low, high = expect["break_even_busy_share"]
    headline = mixes.get(break_even_mix)
    share = headline["break_even"]["busy_share_to_break_even"] if headline else None
    out[break_even_mix] = "Pass" if share is not None and low <= share <= high else "Miss"
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--rules", type=Path, default=HERE / "rules.yaml")
    parser.add_argument("--launcher", choices=["docker", "process", "none"], default="docker")
    parser.add_argument(
        "--url", default=None, help="SIE server URL (default http://127.0.0.1:<port>)"
    )
    parser.add_argument("--models-dir", help="SIE models directory, for --launcher process")
    parser.add_argument(
        "--server-python",
        default="python",
        help="for --launcher process: the Python of an environment with SIE installed",
    )
    parser.add_argument("--output", type=Path, help="run folder (default runs/<timestamp>)")
    args = parser.parse_args()

    rules = load_rules(args.rules)
    load, cost = rules["load"], rules["cost"]
    queries = load_texts(HERE / "data" / "queries.json")
    passages = load_texts(HERE / "data" / "passages.json")
    out = (args.output or HERE / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")).resolve()
    out.mkdir(parents=True, exist_ok=True)
    url = args.url or f"http://127.0.0.1:{rules['port']}"

    server = start_server(
        args.launcher, rules, out / "server.log", args.models_dir, args.server_python
    )
    summary: dict[str, Any] = {
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "launcher": args.launcher,
        "model": rules["model"],
        "mixes": {},
    }
    try:
        wait_ready(server, url, rules["model"], load["start_timeout_s"])
        for mix, share in load["mixes"].items():
            texts = build_mix(queries, passages, share, load["texts_per_mix"], load["seed"])
            cells = []
            for in_flight in load["in_flight"]:
                cell = asyncio.run(
                    measure_cell(url, rules["model"], texts, in_flight, load, rules["gpu_index"])
                )
                print(
                    f"{mix:>6} {in_flight:>2} in flight: {cell['tokens_per_s']:>7,} tokens/s, "
                    f"{cell['errors']} errors, {cell['missing_usage']} without usage"
                    + (" (excluded)" if cell["excluded"] else "")
                )
                cells.append(cell)
            top = best(cells)
            top_tokens = top["tokens_per_s"] if top else None
            summary["mixes"][mix] = {
                "best_tokens_per_s": top_tokens,
                "best_in_flight": top["in_flight"] if top else None,
                "break_even": break_even(top_tokens or 0, cost),
                "cells": cells,
            }
    finally:
        stop_server(server)

    headline = summary["mixes"].get(cost["break_even_mix"])
    if headline:
        summary["headline"] = {"mix": cost["break_even_mix"], **headline["break_even"]}
    summary["verdicts"] = verdicts(summary["mixes"], rules["expect"], cost["break_even_mix"])
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    for mix, result in summary["mixes"].items():
        share = result["break_even"]["busy_share_to_break_even"]
        tokens = result["best_tokens_per_s"]
        print(
            f"{mix}: {'n/a' if tokens is None else f'{tokens:,}'} tokens/s; "
            f"renting pays off above {format_share(share)}"
        )
    for mix, verdict in summary["verdicts"].items():
        print(f"{mix}: {verdict}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
