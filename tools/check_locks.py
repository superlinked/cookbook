#!/usr/bin/env python3
"""Run `uv lock --check` in each recipe whose pyproject.toml or uv.lock is given (all if none)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(paths: list[str]) -> int:
    if paths:
        folders = sorted({(ROOT / path).resolve().parent for path in paths})
    else:
        folders = sorted(path.parent for path in ROOT.glob("*/*/pyproject.toml"))
    failed = [
        folder
        for folder in folders
        if subprocess.run(["uv", "lock", "--check", "--quiet"], cwd=folder, check=False).returncode
    ]
    for folder in failed:
        print(
            f"{folder.relative_to(ROOT)}: `uv lock --check` failed (uv's error is above); "
            "if uv.lock is out of date, run `uv lock`",
            file=sys.stderr,
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
