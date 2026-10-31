"""Screen prompts to a security copilot: allow, block or review, with the reason.

Use case:  Screen every prompt before the SOC's internal LLM copilot sees it.
Who:       The security engineer who runs the copilot, and the analyst on the review queue.
Problem:   Analysts paste logs, configs and reported emails into the copilot. Some pastes carry
           live credentials, some carry prompt-injection text aimed at the model, and some users
           ask for malware or phishing kits. Yet most prompts are legitimate security questions
           ("explain this CVE", "write a detection rule") that a blunt keyword filter would block.
Input:     data/prompts.jsonl (9 prompts sent to the copilot, one JSON object per line).
SIE pipeline:
           (Python)                                 secrets regex: a credential blocks the
                                                    prompt before any model call, so it is
                                                    never sent anywhere
           (Python)                                 prompt-injection markers: send the prompt to
                                                    review
           chat_completions Qwen/Qwen3Guard-Gen-4B  safety verdict (Safe / Controversial /
                                                    Unsafe) and categories, with the prompt as
                                                    the only user message
           (Python)                                 most severe of the checks decides: block >
                                                    review > allow; an unreadable verdict goes
                                                    to review
Output:    decisions.json, decisions.csv and decisions.md: per prompt the decision, every rule
           that fired, the guard's verdict and categories, and the reason. Secrets are masked in
           every output.
Why SIE:   The guard model is one SIE Cloud chat call per prompt. The rules are plain Python and
           data in rules.yaml, so the team can read them and change them.
Runs on:   SIE Cloud, or your own SIE with SIE_BASE_URL set.

    uv run recipe.py
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import yaml
from dotenv import load_dotenv
from sie_sdk import SIEClient

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PROMPTS_PATH = DATA_DIR / "prompts.jsonl"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900

VERDICT_LINE = re.compile(r"Safety:\s*(Safe|Unsafe|Controversial)\b", re.IGNORECASE)
CATEGORIES_LINE = re.compile(r"Categories:\s*(.+)", re.IGNORECASE)


@dataclass(frozen=True)
class Prompt:
    prompt_id: str
    user: str
    text: str


@dataclass(frozen=True)
class Finding:
    rule: str  # "secret" | "injection"
    kind: str  # the pattern name from rules.yaml
    evidence: str  # what matched; secrets are masked


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def load_prompts(path: Path = PROMPTS_PATH) -> list[Prompt]:
    prompts = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            prompts.append(Prompt(entry["id"], entry["user"], entry["prompt"]))
    return prompts


# --- Secrets ------------------------------------------------------------------------------------


def mask(value: str) -> str:
    """Keep only enough of a secret to find it again: the first 4 and last 2 characters.

    A value of 8 characters or fewer is masked in full.
    """
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 6)}{value[-2:]}"


def find_secrets(text: str, rules: dict[str, Any]) -> list[tuple[Finding, str]]:
    """Find every credential-shaped string in the prompt, skipping published placeholders.

    Each finding comes back with the raw matched value, so the caller can mask it in the text.
    For a pattern with a capture group, the value is the group (the value of `key = value`).
    """
    settings = rules["secrets"]
    allowlist = set(settings["allowlist"])
    found: list[tuple[Finding, str]] = []
    seen: set[str] = set()
    for kind, pattern in settings["patterns"].items():
        for match in re.finditer(pattern, text):
            value = match.group(1) if match.groups() else match.group(0)
            if value in allowlist or value in seen:
                continue
            seen.add(value)
            found.append((Finding("secret", kind, mask(value)), value))
    return found


def masked_text(text: str, secrets: list[tuple[Finding, str]]) -> str:
    for finding, value in secrets:
        text = text.replace(value, finding.evidence)
    return text


# --- Prompt injection ---------------------------------------------------------------------------


def normalize(text: str, rules: dict[str, Any]) -> str:
    """Apply NFKC, drop invisible characters, collapse whitespace and casefold.

    This stops a marker from hiding behind full-width letters or zero-width spaces.
    """
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(rules["injection"]["invisible_characters"], "", text)
    return " ".join(text.split()).casefold()


def find_injection(text: str, rules: dict[str, Any]) -> list[Finding]:
    settings = rules["injection"]
    found: list[Finding] = []
    if invisible := re.findall(settings["invisible_characters"], text):
        codes = sorted({f"U+{ord(char):04X}" for char in invisible})
        found.append(Finding("injection", "invisible_characters", " ".join(codes)))
    flat = normalize(text, rules)
    for kind, pattern in settings["markers"].items():
        if match := re.search(pattern, flat):
            found.append(Finding("injection", kind, match.group(0)[:120]))
    return found


# --- Guard model --------------------------------------------------------------------------------


def make_client() -> SIEClient:
    """Build an SIE client from the API key and SIE_BASE_URL in the environment or `.env`."""
    load_dotenv()
    api_key = os.environ.get("SIE_API_KEY", "").strip()
    base_url = os.environ.get("SIE_BASE_URL", "").strip()
    if not api_key and not base_url:
        raise RuntimeError("Set SIE_API_KEY, or SIE_BASE_URL for your own SIE, in .env")
    base_url = base_url or "https://api.superlinked.com"
    return SIEClient(api_key=api_key or None, base_url=base_url, timeout_s=WAIT_S)


def guard(client: SIEClient, prompt: Prompt, rules: dict[str, Any]) -> str:
    """The guard model's raw answer for one prompt: 'Safety: ...' and 'Categories: ...' lines."""
    response: Any = client.chat_completions(
        rules["models"]["guard"],
        [{"role": "user", "content": prompt.text}],
        max_completion_tokens=rules["guard"]["max_completion_tokens"],
        temperature=0.0,
        provision_timeout_s=WAIT_S,
    )
    try:
        return str(response["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as error:
        # A response without the chat shape is an SIE or SDK problem, not a model answer: stop
        # rather than send every prompt to review as if the guard had been unsure.
        raise RuntimeError(
            f"{rules['models']['guard']} returned no chat message for {prompt.prompt_id}: "
            f"{response!r}"
        ) from error


def parse_guard(answer: str) -> tuple[str | None, list[str]]:
    """Read (verdict, categories) from the guard's answer; verdict is None with no Safety line."""
    verdict_match = VERDICT_LINE.search(answer)
    if verdict_match is None:
        return None, []
    verdict = verdict_match.group(1).capitalize()
    categories: list[str] = []
    if categories_match := CATEGORIES_LINE.search(answer):
        categories = [
            name.strip()
            for name in categories_match.group(1).split(",")
            if name.strip() and name.strip().lower() != "none"
        ]
    return verdict, categories


# --- Decide -------------------------------------------------------------------------------------


def decide(
    findings: list[Finding],
    guard_verdict: str | None,
    guard_categories: list[str],
    guard_ran: bool,
    rules: dict[str, Any],
) -> tuple[str, str]:
    """The most severe action any check gives, and a reason that names every check that fired."""
    order: list[str] = rules["decisions"]
    actions: list[tuple[str, str]] = []
    secrets = [f for f in findings if f.rule == "secret"]
    injection = [f for f in findings if f.rule == "injection"]
    if secrets:
        kinds = ", ".join(sorted({f.kind for f in secrets}))
        actions.append(
            (
                rules["secrets"]["action"],
                f"Contains a credential ({kinds}); not sent to any model. Remove it and rotate it.",
            )
        )
    if injection:
        kinds = ", ".join(f.kind for f in injection)
        actions.append(
            (rules["injection"]["action"], f"Prompt-injection markers ({kinds}) need a reviewer.")
        )
    if guard_ran:
        if guard_verdict is None:
            actions.append(("review", "The guard model gave no readable verdict."))
        else:
            action = rules["guard"]["verdicts"][guard_verdict]
            cats = f" ({', '.join(guard_categories)})" if guard_categories else ""
            if action != "allow" or not actions:
                actions.append((action, f"Guard verdict: {guard_verdict}{cats}."))
    decision = min((action for action, _ in actions), key=order.index)
    return decision, " ".join(reason for _, reason in actions)


# --- Run ----------------------------------------------------------------------------------------


def screen(client: SIEClient, prompt: Prompt, rules: dict[str, Any]) -> dict[str, Any]:
    secrets = find_secrets(prompt.text, rules)
    text = masked_text(prompt.text, secrets)
    # Injection evidence quotes the prompt, so it is taken from the masked text.
    findings = [finding for finding, _ in secrets] + find_injection(text, rules)
    answer = ""
    verdict: str | None = None
    categories: list[str] = []
    guard_ran = not secrets  # a prompt with a credential is never sent, not even to the guard
    if guard_ran:
        answer = guard(client, prompt, rules)
        verdict, categories = parse_guard(answer)
    decision, reason = decide(findings, verdict, categories, guard_ran, rules)
    return {
        "prompt": prompt.prompt_id,
        "user": prompt.user,
        "text": text,
        "decision": decision,
        "reason": reason,
        "findings": [{"rule": f.rule, "kind": f.kind, "evidence": f.evidence} for f in findings],
        "guard_ran": guard_ran,
        "guard_verdict": verdict,
        "guard_categories": categories,
        "guard_answer": answer,
    }


def run(client: SIEClient, rules: dict[str, Any], prompts: list[Prompt]) -> list[dict[str, Any]]:
    return [screen(client, prompt, rules) for prompt in prompts]


def render_markdown(rows: list[dict[str, Any]]) -> str:
    parts = ["# Copilot prompt screening\n"]
    for row in rows:
        quoted = "\n".join(f"> {line}" for line in row["text"].splitlines())
        parts.append(f"## {row['prompt']}: {row['decision']}\n\n{quoted}\n")
        parts.append(f"- User: {row['user']}")
        parts.append(f"- Reason: {row['reason']}")
        for finding in row["findings"]:
            parts.append(f"- {finding['rule']} / {finding['kind']}: `{finding['evidence']}`")
        if row["guard_ran"]:
            cats = ", ".join(row["guard_categories"]) or "none"
            parts.append(f"- Guard: {row['guard_verdict']} (categories: {cats})")
        else:
            parts.append("- Guard: not called (credential in prompt)")
        parts.append("")
    return "\n".join(parts)


def write_outputs(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "decisions.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    with (output_dir / "decisions.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["prompt", "user", "decision", "rules", "guard", "reason"])
        for row in rows:
            writer.writerow(
                [
                    row["prompt"],
                    row["user"],
                    row["decision"],
                    " ".join(f"{f['rule']}:{f['kind']}" for f in row["findings"]),
                    row["guard_verdict"] or "",
                    row["reason"],
                ]
            )
    (output_dir / "decisions.md").write_text(render_markdown(rows), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, help="output directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    output = args.output or RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    with make_client() as client:
        rows = run(client, load_rules(), load_prompts())
    write_outputs(output, rows)
    for row in rows:
        guard_text = row["guard_verdict"] or ("not called" if not row["guard_ran"] else "none")
        kinds = " ".join(f["kind"] for f in row["findings"])
        print(f"{row['prompt']}  {row['decision']:<7} guard={guard_text:<13} {kinds}")
    print(f"\ndecisions: {output / 'decisions.md'}")


if __name__ == "__main__":
    main()
