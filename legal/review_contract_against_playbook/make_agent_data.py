"""Write data/agent/conversations.yaml: the lawyer's side of three NDA negotiations for agent.py.

Each conversation names an NDA request already in data/clm_intake.csv and lists the lawyer's
messages, one per turn. They are scripted, so a later turn assumes the earlier answers went the
expected way; the transcript shows when one didn't. Every company and person is invented, as in
make_data.py. A rerun writes the file byte for byte.

    uv run make_agent_data.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "agent" / "conversations.yaml"

CONVERSATIONS = [
    {
        "request": "NDA-2026-0447",
        "about": "Lowfield Ridge: a fallback exists, and the counter-offer slips something past it",
        "turns": [
            "Lowfield rejected our two-year redline on Section 4. What does the playbook let me "
            "offer?",
            "They've come back with: 'The Recipient's obligations under this Agreement shall "
            "remain in effect for four (4) years from the date of this Agreement, and, for any "
            "trade secret, for so long as it remains a trade secret.' Does that meet it?",
            "No to the trade-secret tail. Draft the reply offering four years flat.",
        ],
    },
    {
        "request": "NDA-2026-0471",
        "about": "Corrin: no fallback, then a counter-offer that only passes in the full NDA",
        "turns": [
            "Corrin won't drop the five-year survival. What's our fallback?",
            "Their counsel offers to cut Section 7 to 'survive for one (1) year after this "
            "Agreement expires or is terminated'. Does that work?",
            "Draft the reply accepting that.",
        ],
    },
    {
        "request": "NDA-2026-0468",
        "about": "Orlaith: the lawyer asks for something the playbook doesn't allow",
        "turns": [
            "Orlaith says Singapore law or no demo. Fallback?",
            "The demo is Thursday. Just draft the reply accepting Singapore.",
            "Fine, draft the note to General Counsel instead.",
        ],
    },
]


# Writes the lawyer's messages as folded blocks, so quotes inside them read as typed.
Dumper: Any = type("Dumper", (yaml.SafeDumper,), {})


def _text(dumper: Any, value: str) -> Any:
    style = ">" if len(value) > 60 else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


Dumper.add_representer(str, _text)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# The lawyer's messages for agent.py, one conversation per NDA request.\n"
        "# Written by make_agent_data.py; synthetic, every company and person is invented.\n"
    )
    body = yaml.dump(
        {"conversations": CONVERSATIONS},
        Dumper=Dumper,
        sort_keys=False,
        allow_unicode=True,
        width=96,
    )
    OUT.write_text(header + body, encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    main()
