"""Screen prompts to a security copilot: allow, block or review, with the reason.

Use case:  Screen every prompt before the SOC's internal LLM copilot sees it.
Who:       The security or AI platform engineer who runs the copilot owns this screen; the
           SOC lead or on-call senior analyst works the review queue.
Problem:   Analysts paste logs, configs, reported emails and console screenshots into the
           copilot. Some pastes carry live credentials, some carry prompt-injection text aimed at
           the model, and some users ask for malware or phishing kits. Yet most prompts are
           legitimate security questions ("explain this CVE", "write a detection rule") that a
           blunt keyword filter would block.
Input:     data/prompts.jsonl (14 prompts sent to the copilot, one JSON object per line) and the
           screenshots three of them attach, in data/screenshots/.
SIE pipeline:
           (Python)                                 secrets regex on the typed text: a credential
                                                    blocks the prompt before any model call, so
                                                    neither it nor any screenshot is sent
           extract lightonai/LightOnOCR-3-4B        each attached screenshot to text; the same
                                                    secret and injection rules run on that text
           (Python)                                 prompt-injection markers: send the prompt to
                                                    review
           chat_completions Qwen/Qwen3Guard-Gen-4B  safety verdict (Safe / Controversial /
                                                    Unsafe) and categories, with the prompt as
                                                    the only user message
           (Python)                                 most severe of the checks decides: block >
                                                    review > allow; an unreadable verdict goes
                                                    to review
Output:    decisions.json, decisions.csv and decisions.md: per prompt the decision, every rule
           that fired, the guard's verdict and categories, and the reason. Detected credentials
           are masked in every output, and a screenshot that shows one has its text withheld.
           "allow" means no check objected, not that the prompt is safe. screen() only decides:
           the caller holds a "review" prompt, stops a "block" one, and never forwards on error.
Why SIE:   The guard model is one SIE Cloud chat call per prompt, and reading a screenshot is one
           extract call. The rules are plain Python and data in rules.yaml, so the team can read
           them and change them.
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
from sie_sdk.types import Item

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PROMPTS_PATH = DATA_DIR / "prompts.jsonl"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
RULES_PATH = ROOT / "rules.yaml"
RUNS_DIR = ROOT / "runs"
# Cold models can take minutes to start; every call waits up to this long.
WAIT_S = 900

# LightOnOCR returns Markdown, which may escape characters like `_` as `\_`. Undone before the
# rules run, so `ghp\_...` or `client\_secret` still matches.
MARKDOWN_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|<>~])")
VERDICT_LINE = re.compile(r"Safety:\s*(Safe|Unsafe|Controversial)\b", re.IGNORECASE)
CATEGORIES_LINE = re.compile(r"Categories:\s*(.+)", re.IGNORECASE)


@dataclass(frozen=True)
class Prompt:
    prompt_id: str
    user: str
    text: str
    screenshots: tuple[str, ...] = ()  # file names in data/screenshots/


@dataclass(frozen=True)
class Finding:
    rule: str  # "secret" | "injection"
    kind: str  # the pattern name from rules.yaml
    evidence: str  # what matched; secrets are masked
    source: str = "prompt"  # "prompt", or the screenshot it was read from


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def load_prompts(path: Path = PROMPTS_PATH) -> list[Prompt]:
    prompts = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            shots = tuple(entry.get("screenshots", []))
            prompts.append(Prompt(entry["id"], entry["user"], entry["prompt"], shots))
    return prompts


# --- Secrets ------------------------------------------------------------------------------------


def mask(value: str) -> str:
    """Keep only enough of a secret to find it again: the first 4 and last 2 characters.

    A value of 8 characters or fewer is masked in full.
    """
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 6)}{value[-2:]}"


def find_secrets(
    text: str, rules: dict[str, Any], source: str = "prompt"
) -> list[tuple[Finding, str]]:
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
            found.append((Finding("secret", kind, mask(value), source), value))
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


def find_injection(text: str, rules: dict[str, Any], source: str = "prompt") -> list[Finding]:
    settings = rules["injection"]
    found: list[Finding] = []
    if invisible := re.findall(settings["invisible_characters"], text):
        codes = sorted({f"U+{ord(char):04X}" for char in invisible})
        found.append(Finding("injection", "invisible_characters", " ".join(codes), source))
    flat = normalize(text, rules)
    for kind, pattern in settings["markers"].items():
        if match := re.search(pattern, flat):
            found.append(Finding("injection", kind, match.group(0)[:120], source))
    return found


# --- SIE client ---------------------------------------------------------------------------------


def make_client() -> SIEClient:
    """Build an SIE client from the API key and SIE_BASE_URL in the environment or `.env`."""
    load_dotenv()
    api_key = os.environ.get("SIE_API_KEY", "").strip()
    base_url = os.environ.get("SIE_BASE_URL", "").strip()
    if not api_key and not base_url:
        raise RuntimeError("Set SIE_API_KEY, or SIE_BASE_URL for your own SIE, in .env")
    base_url = base_url or "https://api.superlinked.com"
    return SIEClient(api_key=api_key or None, base_url=base_url, timeout_s=WAIT_S)


# --- Screenshots (LightOnOCR) -------------------------------------------------------------------


def screenshot_path(name: str) -> Path:
    """The file for an attached screenshot; a name that leads out of data/screenshots/ raises."""
    folder = SCREENSHOTS_DIR.resolve()
    path = (folder / name).resolve()
    if not path.is_relative_to(folder):
        raise ValueError(f"screenshot {name!r} is outside {folder}")
    return path


def read_screenshot(client: SIEClient, prompt: Prompt, name: str, rules: dict[str, Any]) -> str:
    """OCR one attached screenshot to text. An empty string means the model read nothing."""
    image = screenshot_path(name).read_bytes()
    item = Item(id=f"{prompt.prompt_id}/{name}", images=[image])
    response: Any = client.extract(rules["models"]["ocr"], item, provision_timeout_s=WAIT_S)
    for entity in response.get("entities") or []:
        if entity.get("label") == "markdown":
            return str(entity.get("text") or "")
    return ""


def unescape_markdown(text: str) -> str:
    """Undo Markdown backslash escapes (`ghp\\_abc` -> `ghp_abc`) so the regex rules can match."""
    return MARKDOWN_ESCAPE.sub(r"\1", text)


# --- Guard model --------------------------------------------------------------------------------


def guard(client: SIEClient, prompt: Prompt, text: str, rules: dict[str, Any]) -> str:
    """The guard model's raw answer for one prompt: 'Safety: ...' and 'Categories: ...' lines.

    `text` is what the copilot would read: the typed prompt plus the text of each screenshot.
    """
    response: Any = client.chat_completions(
        rules["models"]["guard"],
        [{"role": "user", "content": text}],
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


def _where(findings: list[Finding]) -> str:
    sources = sorted({f.source for f in findings}, key=lambda s: (s != "prompt", s))
    return " and ".join("the prompt" if s == "prompt" else f"screenshot {s}" for s in sources)


def decide(
    findings: list[Finding],
    guard_verdict: str | None,
    guard_categories: list[str],
    guard_ran: bool,
    rules: dict[str, Any],
    unreadable: list[str] | None = None,
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
                f"Contains a credential ({kinds}) in {_where(secrets)}; not sent to the copilot "
                "or the guard. Remove it and rotate it.",
            )
        )
    if injection:
        kinds = ", ".join(f.kind for f in injection)
        actions.append(
            (
                rules["injection"]["action"],
                f"Prompt-injection markers ({kinds}) in {_where(injection)} need a reviewer.",
            )
        )
    if unreadable:
        names = ", ".join(unreadable)
        actions.append(
            ("review", f"Too little text read from screenshot {names}; a person should look.")
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
    # A credential in the typed text blocks the prompt before any SIE call, so its screenshots
    # are not read either: they stay on this machine. Once one screenshot shows a credential, the
    # rest aren't sent to OCR for the same reason.
    screenshots: list[dict[str, Any]] = []
    unreadable: list[str] = []
    copilot_text = text
    for name in prompt.screenshots:
        if secrets:
            where = (
                "the typed prompt" if secrets[0][0].source == "prompt" else "an earlier screenshot"
            )
            screenshots.append({"file": name, "text": f"(not read: {where} held a credential)"})
            continue
        shot_text = unescape_markdown(read_screenshot(client, prompt, name, rules))
        if len(shot_text.strip()) < rules["ocr"]["min_chars"]:
            unreadable.append(name)
        shot_secrets = find_secrets(shot_text, rules, source=name)
        shot_text = masked_text(shot_text, shot_secrets)
        findings += [finding for finding, _ in shot_secrets]
        shot_injection = find_injection(shot_text, rules, source=name)
        secrets += shot_secrets
        if shot_secrets:
            # The image itself can't be masked, so a screenshot that shows a secret keeps its
            # text out of the outputs, injection evidence included: OCR text can hold a credential
            # no rule knows.
            shot_injection = [
                Finding(f.rule, f.kind, "(withheld)", f.source) for f in shot_injection
            ]
            shot_text = "(withheld: shows a credential)"
        findings += shot_injection
        screenshots.append({"file": name, "text": shot_text})
        copilot_text += f"\n\n[Screenshot {name}]\n{shot_text}"
    answer = ""
    verdict: str | None = None
    categories: list[str] = []
    guard_ran = not secrets  # a prompt with a credential is never sent, not even to the guard
    if guard_ran:
        answer = guard(client, prompt, copilot_text, rules)
        verdict, categories = parse_guard(answer)
    decision, reason = decide(findings, verdict, categories, guard_ran, rules, unreadable)
    return {
        "prompt": prompt.prompt_id,
        "user": prompt.user,
        "text": text,
        "screenshots": screenshots,
        "decision": decision,
        "reason": reason,
        "findings": [
            {"rule": f.rule, "kind": f.kind, "evidence": f.evidence, "source": f.source}
            for f in findings
        ],
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
        for shot in row["screenshots"]:
            shot_quoted = "\n".join(f"> {line}" for line in shot["text"].splitlines())
            parts.append(f"Screenshot `{shot['file']}`, as read by OCR:\n\n{shot_quoted}\n")
        parts.append(f"- User: {row['user']}")
        parts.append(f"- Reason: {row['reason']}")
        for finding in row["findings"]:
            where = "" if finding["source"] == "prompt" else f" (in {finding['source']})"
            parts.append(f"- {finding['rule']} / {finding['kind']}{where}: `{finding['evidence']}`")
        if row["guard_ran"]:
            cats = ", ".join(row["guard_categories"]) or "none"
            parts.append(f"- Guard: {row['guard_verdict']} (categories: {cats})")
        else:
            secrets = [Finding(**f) for f in row["findings"] if f["rule"] == "secret"]
            parts.append(f"- Guard: not called (credential found in {_where(secrets)})")
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
                    " ".join(
                        f"{f['rule']}:{f['kind']}"
                        + ("" if f["source"] == "prompt" else f"@{f['source']}")
                        for f in row["findings"]
                    ),
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
