"""Measure bge-m3 tokens a second on your GPU through SIE, and when renting that GPU pays off.

For each input mix (short queries, long passages, and 70% queries / 30% passages), the recipe keeps
4, 8, 16 and 32 requests of 8 texts in flight against one SIE server and counts the input tokens it
embeds. Then it works out the break-even: how busy that GPU has to be, on average, before renting
it all month costs less than paying per call.

Run it on the machine with the GPU. `--launcher docker` (the default) starts SIE from its image;
`--launcher process` starts the same command as a process, for a machine that already has SIE's
Python environment; `--launcher none` uses a server already running at `--url`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import signal
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from sie_sdk import SIEAsyncClient, SIEClient

HERE = Path(__file__).resolve().parent


def load_rules(path: Path) -> dict[str, Any]:
    rules: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
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


def start_server(kind: str, rules: dict[str, Any], log: Path, models_dir: str | None) -> Server:
    args = ["-m", "sie_server.cli", "serve", "--host", "127.0.0.1", "--port", str(rules["port"])]
    args += ["--models", rules["model"]]
    if models_dir:
        args += ["--models-dir", models_dir]
    if kind == "none":
        return Server(None, None)
    container = None
    if kind == "docker":
        container = f"why-sie-gpu-{os.getpid()}"
        hf_cache = Path.home() / ".cache" / "huggingface"
        cmd = ["docker", "run", "--rm", "--gpus", "all", "--network", "host", "--name", container]
        cmd += [
            "-v",
            f"{hf_cache}:/app/.cache/huggingface",
            "--entrypoint",
            "python",
            rules["image"],
        ]
        cmd += args
    else:
        cmd = ["python", *args]
    with log.open("wb") as fh:
        process = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
    return Server(process, container)


def stop_server(server: Server) -> None:
    if server.container:
        subprocess.run(
            ["docker", "rm", "-f", server.container],
            capture_output=True,
            check=False,
        )
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
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(10)
    finally:
        client.close()


def gpu_busy_percent() -> float | None:
    if shutil.which("nvidia-smi") is None:
        return None
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    return float(out[0]) if out else None


# --- Load ------------------------------------------------------------------------------------


async def measure_cell(
    url: str,
    model: str,
    texts: list[str],
    in_flight: int,
    per_request: int,
    seconds: float,
    warmup_s: float,
) -> dict[str, Any]:
    """Keep `in_flight` encode requests going; count tokens and requests after the warm-up."""
    client = SIEAsyncClient(url, timeout_s=300.0)
    tokens = requests = errors = 0
    latencies: list[float] = []
    busy: list[float] = []
    start = time.monotonic()
    deadline = start + seconds

    async def worker(offset: int) -> None:
        nonlocal tokens, requests, errors
        i = offset
        while time.monotonic() < deadline:
            batch = [{"text": texts[(i + k) % len(texts)]} for k in range(per_request)]
            i += per_request
            sent = time.monotonic()
            try:
                results = await client.encode(model, batch, output_types=["dense"])
                usage = (results[0].get("request") or {}).get("usage") or {}
                if sent - start >= warmup_s:
                    tokens += int(usage.get("input_tokens") or 0)
                    requests += 1
                    latencies.append(time.monotonic() - sent)
            except Exception:
                errors += 1

    async def sampler() -> None:
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            if time.monotonic() - start >= warmup_s:
                value = await asyncio.to_thread(gpu_busy_percent)
                if value is not None:
                    busy.append(value)

    try:
        await asyncio.gather(sampler(), *(worker(k * 389) for k in range(in_flight)))
    finally:
        await client.close()
    measured = seconds - warmup_s
    latencies.sort()
    return {
        "in_flight": in_flight,
        "tokens_per_s": round(tokens / measured),
        "requests": requests,
        "errors": errors,
        "p50_ms": round(latencies[len(latencies) // 2] * 1000) if latencies else None,
        "gpu_busy_pct": round(sum(busy) / len(busy)) if busy else None,
    }


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


def best(cells: list[dict[str, Any]]) -> dict[str, Any]:
    return max(cells, key=lambda c: c["tokens_per_s"])


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
    parser.add_argument("--output", type=Path, help="run folder (default runs/<timestamp>)")
    args = parser.parse_args()

    rules = load_rules(args.rules)
    load, cost = rules["load"], rules["cost"]
    queries = load_texts(HERE / "data" / "queries.json")
    passages = load_texts(HERE / "data" / "passages.json")
    out = (args.output or HERE / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")).resolve()
    out.mkdir(parents=True, exist_ok=True)
    url = args.url or f"http://127.0.0.1:{rules['port']}"

    server = start_server(args.launcher, rules, out / "server.log", args.models_dir)
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
                    measure_cell(
                        url,
                        rules["model"],
                        texts,
                        in_flight,
                        load["texts_per_request"],
                        load["cell_s"],
                        load["warmup_s"],
                    )
                )
                print(f"{mix:>6} {in_flight:>2} in flight: {cell['tokens_per_s']:>7,} tokens/s")
                cells.append(cell)
            top = best(cells)
            summary["mixes"][mix] = {
                "best_tokens_per_s": top["tokens_per_s"],
                "best_in_flight": top["in_flight"],
                "break_even": break_even(top["tokens_per_s"], cost),
                "cells": cells,
            }
    finally:
        stop_server(server)

    headline = summary["mixes"].get(cost["break_even_mix"])
    if headline:
        summary["headline"] = {"mix": cost["break_even_mix"], **headline["break_even"]}
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    for mix, result in summary["mixes"].items():
        share = result["break_even"]["busy_share_to_break_even"]
        print(
            f"{mix}: {result['best_tokens_per_s']:,} tokens/s; "
            f"renting pays off above {share:.0%} busy"
        )
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
