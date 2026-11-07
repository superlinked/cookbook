"""Index the policy manual: split it by section and embed each section.

This is a fixed pipeline, with no agent, because it does the same thing every time. Run it after
the manual changes with `uv run ingest.py`. recipe.py also runs it when the index is missing or
the manual or the embedding model has changed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from sie_calls import WAIT_S, Calls, SIEStopped
from sie_sdk import SIEClient
from sie_sdk.types import Item
from vector_index import VectorIndex

HERE = Path(__file__).parent
MANUAL = HERE / "data" / "policy_manual.md"
RULES_PATH = HERE / "rules.yaml"


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    rules: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return rules


def make_client() -> SIEClient:
    """Build an SIE client from the API key and SIE_BASE_URL in the environment or `.env`."""
    load_dotenv()
    api_key = os.environ.get("SIE_API_KEY", "").strip()
    base_url = os.environ.get("SIE_BASE_URL", "").strip()
    if not api_key and not base_url:
        raise RuntimeError("Set SIE_API_KEY, or SIE_BASE_URL for SIE Cluster or SIE Local, in .env")
    base_url = base_url or "https://api.superlinked.com"
    return SIEClient(api_key=api_key or None, base_url=base_url, timeout_s=WAIT_S)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", text.lower()).strip("-")


def split_sections(markdown: str) -> list[dict[str, str]]:
    """One item per `##` heading. Text above the first one is the title, and is dropped."""
    sections: list[dict[str, str]] = []
    parts = re.split(r"^##[ \t]+(.+?)[ \t]*$", markdown, flags=re.M)
    for heading, body in zip(parts[1::2], parts[2::2], strict=True):
        if body.strip():
            sections.append({"section_id": slug(heading), "heading": heading, "text": body.strip()})
    return sections


def embed_sections(
    calls: Calls, client: SIEClient, sections: list[dict[str, str]], rules: dict[str, Any]
) -> list[Any]:
    model = rules["models"]["embed"]
    items = [
        Item(id=section["section_id"], text=f"{section['heading']}\n{section['text']}")
        for section in sections
    ]
    reply = calls.call(
        model,
        lambda: client.encode(model, items, is_query=False, provision_timeout_s=WAIT_S),
        lambda r: (
            isinstance(r, list)
            and len(r) == len(items)
            and all(isinstance(x, dict) and "dense" in x for x in r)
        ),
        "a 'dense' vector per section",
    )
    return [result["dense"] for result in reply]


def fingerprint(rules: dict[str, Any]) -> str:
    """Changes when the manual or the embedding model changes."""
    settings = {
        "manual": hashlib.sha256(MANUAL.read_bytes()).hexdigest(),
        "embed": rules["models"]["embed"],
    }
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


def build_index(calls: Calls, client: SIEClient, rules: dict[str, Any]) -> VectorIndex:
    sections = split_sections(MANUAL.read_text(encoding="utf-8"))
    if not sections:
        raise SystemExit(f"Stopped: found no sections in {MANUAL.name}")
    index = VectorIndex.build(embed_sections(calls, client, sections, rules), sections)
    index.save(HERE / rules["ingest"]["index_dir"], fingerprint(rules))
    return index


def main() -> None:
    rules = load_rules()
    calls = Calls()
    try:
        index = build_index(calls, make_client(), rules)
    except SIEStopped as error:
        raise SystemExit(f"Stopped: {error}") from None
    print(f"Indexed {len(index.items)} sections with {calls.total} SIE call.")


if __name__ == "__main__":
    main()
