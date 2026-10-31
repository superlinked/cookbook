#!/usr/bin/env python3
"""Check that every recipe is self-contained and the tree is safe to publish.

python3 tools/check_tree.py              # check, exit 1 on any problem
python3 tools/check_tree.py --list-json  # print the recipe folders as JSON (CI matrix)
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDUSTRIES = (
    "ecommerce",
    "finance",
    "healthcare",
    "insurance",
    "legal",
    "manufacturing",
    "personal",
    "retail",
    "security",
    "sre",
)
REQUIRED = (
    "recipe.py",
    "README.md",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    ".env.example",
)
# A recipe must not import another recipe (by its industry folder) or the internal research package.
CROSS_IMPORT = re.compile(
    rb"^\s*(?:from|import)\s+(?:sie_cloud_research|" + "|".join(INDUSTRIES).encode() + rb")\b", re.M
)
# Every recipe README shows how to swap a model and how to self-host it.
README_SECTIONS = ("## Try another model", "## Run it on your own SIE")
PYTHON = "3.12"
FORBIDDEN = (b"sie-" + b"internal", b"sie_cloud" + b"_research", b"verified-" + b"run/")


def recipes() -> list[Path]:
    """Every folder two levels down with a recipe.py or a pyproject.toml, under any top folder.

    Finding them by either file, under any industry, lets the checks below flag a recipe that is
    missing one or that sits under an industry not listed above, instead of skipping it.
    """
    found = {
        path.parent for name in ("recipe.py", "pyproject.toml") for path in ROOT.glob(f"*/*/{name}")
    }
    return sorted(folder for folder in found if not folder.parts[-2].startswith("."))


def tracked_files() -> list[Path]:
    """Tracked and non-ignored untracked files, the set that a commit could publish."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    return [ROOT / item.decode() for item in out.split(b"\0") if item]


def problems() -> list[str]:
    found: list[str] = []
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for recipe in recipes():
        rel = recipe.relative_to(ROOT).as_posix()
        if recipe.parts[-2] not in INDUSTRIES:
            found.append(f"{rel}: {recipe.parts[-2]!r} is not in INDUSTRIES in tools/check_tree.py")
        found += [f"{rel}: missing {name}" for name in REQUIRED if not (recipe / name).is_file()]
        pyproject = recipe / "pyproject.toml"
        if pyproject.is_file():
            config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            if config.get("tool", {}).get("uv", {}).get("package") is not False:
                found.append(f"{rel}: pyproject.toml needs [tool.uv] package = false")
        recipe_readme = recipe / "README.md"
        if recipe_readme.is_file():
            text = recipe_readme.read_text(encoding="utf-8")
            found += [f"{rel}: README.md needs '{h}'" for h in README_SECTIONS if h not in text]
        version = recipe / ".python-version"
        if version.is_file() and version.read_text(encoding="utf-8").strip() != PYTHON:
            found.append(f"{rel}: .python-version must be {PYTHON}, the oldest Python we support")
        if f"]({rel})" not in readme:
            found.append(f"{rel}: not linked from the root README.md")
        for module in recipe.glob("*.py"):
            if CROSS_IMPORT.search(module.read_bytes()):
                found.append(f"{module.relative_to(ROOT)}: imports from outside its recipe")
    for path in tracked_files():
        rel = path.relative_to(ROOT).as_posix()
        if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            found.append(f"{rel}: environment files must not be committed")
        if not path.is_file() or rel == "tools/check_tree.py":
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        found += [f"{rel}: contains {term.decode()!r}" for term in FORBIDDEN if term in data]
    return found


def main() -> int:
    if sys.argv[1:] == ["--list-json"]:
        print(json.dumps([path.relative_to(ROOT).as_posix() for path in recipes()]))
        return 0
    found = problems()
    for problem in found:
        print(problem, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
