"""Draft a book chapter from your own outline with open models, with checked citations.

Use case:  A first draft of a nonfiction chapter from the author's outline, written by open-weight
           models whose licence puts no conditions on the output, with a citation behind every
           claim it can find one for and a record of which sentences the model wrote.
Who:       An author who wants to own the draft outright: no provider terms on what they do with
           it, no provider watermark in it, and a record for rewriting and disclosure.
Input:     data/spine.md: the chapter title, a ## heading per section and the author's bullets.
SIE pipeline, per section:
           chat_completions Qwen/Qwen3.8-27B-FP8  a few short search queries
           (Python)                              search Wikipedia, read the articles, split them
                                                 into passages, keep each revision id
           encode   Qwen/Qwen3-Embedding-4B      embed the passages and each bullet
           score    Qwen/Qwen3-Reranker-4B       rerank the nearest passages against each bullet
           chat_completions Qwen/Qwen3.8-27B-FP8  the draft, sentence by sentence, each with the
                                                 bullet it came from and its citations
           (Python)                              a citation must name a passage the model was
                                                 given and quote it word for word; every bullet
                                                 must be in the draft
Output:    runs/<time>/chapter.md (the draft and its references), review.md (what to check
           first, then where every sentence came from), queries.md (everything sent to
           Wikipedia) and run.json.
Run using: SIE Serverless, or SIE Cluster or SIE Local with SIE_BASE_URL.

    uv run recipe.py
    uv run recipe.py --spine my_chapter.md
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import numpy as np
import yaml
from dotenv import load_dotenv
from sie_calls import WAIT_S, CallFailed, Calls, SIEStopped
from sie_sdk import SIEClient
from sie_sdk.types import Item
from wikipedia import Article, SearchFailed, Wikipedia

ROOT = Path(__file__).resolve().parent
RULES_PATH = ROOT / "rules.yaml"
SPINE_PATH = ROOT / "data" / "spine.md"
RUNS_DIR = ROOT / "runs"
EMBED_BATCH = 32

QUERIES_SCHEMA = {
    "type": "object",
    "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["queries"],
    "additionalProperties": False,
}

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "paragraphs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sentences": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "bullet": {"type": "integer"},
                                "citations": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "passage": {"type": "string"},
                                            "quote": {"type": "string"},
                                        },
                                        "required": ["passage", "quote"],
                                        "additionalProperties": False,
                                    },
                                },
                            },
                            "required": ["text", "bullet", "citations"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["sentences"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["paragraphs"],
    "additionalProperties": False,
}


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    rules: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return rules


def make_client() -> SIEClient:
    """Build an SIE client from the API key and SIE_BASE_URL in the environment or `.env`."""
    load_dotenv()
    api_key = os.environ.get("SIE_API_KEY", "").strip()
    base_url = os.environ.get("SIE_BASE_URL", "").strip()
    if not api_key and not base_url:
        raise SystemExit(
            "Stopped: set SIE_API_KEY, or SIE_BASE_URL for SIE Cluster or SIE Local, in .env"
        )
    base_url = base_url or "https://api.superlinked.com"
    return SIEClient(api_key=api_key or None, base_url=base_url, timeout_s=WAIT_S)


# --- The outline -------------------------------------------------------------------------------


@dataclass
class Section:
    heading: str
    bullets: list[str] = field(default_factory=list)


@dataclass
class Spine:
    title: str
    sections: list[Section]


def parse_spine(text: str) -> Spine:
    """`# Title`, then `## Section` headings, each with `- bullet` lines. An indented line carries
    on the bullet above it. Anything else is ignored."""
    title, sections = "", []
    for raw in text.splitlines():
        line = raw.strip()
        if raw.startswith("# "):
            title = line[2:].strip()
        elif raw.startswith("## "):
            sections.append(Section(line[3:].strip()))
        elif raw.startswith(("- ", "* ")) and sections:
            sections[-1].bullets.append(line[2:].strip())
        elif raw[:1] in (" ", "\t") and line and sections and sections[-1].bullets:
            sections[-1].bullets[-1] += " " + line
    sections = [section for section in sections if section.bullets]
    if not sections:
        raise SystemExit("Stopped: the outline has no `## Section` with `- bullet` lines.")
    return Spine(title or "Untitled chapter", sections)


# --- Passages from Wikipedia -----------------------------------------------------------------


@dataclass(frozen=True)
class Passage:
    title: str
    revision: int
    heading: str
    text: str

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.title, self.revision, self.text[:80])


HEADING = re.compile(r"^(={2,})\s*(.+?)\s*\1\s*$")


def split_article(article: Article, rules: dict[str, Any]) -> list[Passage]:
    """An article's paragraphs, packed into passages of up to max_words words. Sections that
    only list other pages are skipped."""
    settings = rules["passages"]
    skip = {name.lower() for name in rules["search"]["skip_headings"]}
    passages: list[Passage] = []
    heading, skipping = "", False
    buffer: list[str] = []

    def flush() -> None:
        text = " ".join(buffer).strip()
        if len(text.split()) >= settings["min_words"]:
            passages.append(Passage(article.title, article.revision, heading, text))
        buffer.clear()

    for line in article.text.splitlines():
        match = HEADING.match(line.strip())
        if match:
            flush()
            level, name = len(match.group(1)), match.group(2)
            if level == 2:
                skipping = name.lower() in skip
            heading = name
            continue
        line = line.strip()
        if skipping or not line:
            continue
        if buffer and len(" ".join(buffer).split()) + len(line.split()) > settings["max_words"]:
            flush()
        buffer.append(line)
    flush()
    return passages


def clean_queries(reply: Any, rules: dict[str, Any]) -> list[str]:
    """At most queries_per_section short queries, with duplicates and empty ones dropped."""
    settings = rules["search"]
    queries: list[str] = []
    for query in reply.get("queries", []) if isinstance(reply, dict) else []:
        if not isinstance(query, str):
            continue
        words = query.replace('"', " ").split()[: settings["max_query_words"]]
        cleaned = " ".join(words)
        if cleaned and cleaned.lower() not in {q.lower() for q in queries}:
            queries.append(cleaned)
    return queries[: settings["queries_per_section"]]


# --- Calls to SIE ----------------------------------------------------------------------------


def chat_json(
    calls: Calls,
    client: SIEClient,
    rules: dict[str, Any],
    prompt: str,
    name: str,
    schema: dict[str, Any],
) -> dict[str, Any] | None:
    """One chat call with a JSON schema. None if the reply isn't a JSON object."""
    model = rules["models"]["write"]
    reply = calls.call(
        model,
        lambda: client.chat_completions(
            model,
            [{"role": "system", "content": rules["system"]}, {"role": "user", "content": prompt}],
            max_completion_tokens=rules["write"]["max_completion_tokens"],
            temperature=rules["write"]["temperature"],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": name, "schema": schema, "strict": True},
            },
            provision_timeout_s=WAIT_S,
        ),
        lambda r: isinstance(r, dict) and isinstance(r.get("choices"), list) and r["choices"],
        "'choices'",
    )
    try:
        parsed = json.loads(reply["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return None
    return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else None


def unit_rows(rows: list[Any]) -> Any:
    matrix = np.asarray(rows, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms == 0, 1, norms)


def embed(
    calls: Calls, client: SIEClient, rules: dict[str, Any], texts: list[str], is_query: bool
) -> Any:
    """One unit vector per text, in batches of EMBED_BATCH."""
    rows: list[Any] = []
    for start in range(0, len(texts), EMBED_BATCH):
        rows.extend(embed_batch(calls, client, rules, texts[start : start + EMBED_BATCH], is_query))
    return unit_rows(rows)


def embed_batch(
    calls: Calls, client: SIEClient, rules: dict[str, Any], texts: list[str], is_query: bool
) -> list[Any]:
    model = rules["models"]["embed"]
    options: dict[str, Any] = (
        {"instruction": rules["retrieval"]["query_instruction"]} if is_query else {}
    )
    items = [Item(id=str(n), text=text) for n, text in enumerate(texts)]
    reply = calls.call(
        model,
        lambda: client.encode(
            model, items, is_query=is_query, provision_timeout_s=WAIT_S, **options
        ),
        lambda r: (
            isinstance(r, list)
            and len(r) == len(items)
            and all(isinstance(x, dict) and "dense" in x for x in r)
        ),
        "a 'dense' vector per text",
    )
    return [result["dense"] for result in reply]


def rerank(
    calls: Calls,
    client: SIEClient,
    rules: dict[str, Any],
    claim: str,
    candidates: list[Passage],
) -> list[tuple[Passage, float]]:
    """The candidates with their reranker scores, best first."""
    model = rules["models"]["rerank"]
    ids = {str(n): passage for n, passage in enumerate(candidates)}
    scored = calls.call(
        model,
        lambda: client.score(
            model,
            Item(id="claim", text=claim),
            [Item(id=n, text=f"{p.title}: {p.heading}\n{p.text}") for n, p in ids.items()],
            instruction=rules["retrieval"]["rerank_instruction"],
            provision_timeout_s=WAIT_S,
        ),
        lambda r: (
            isinstance(r, dict)
            and isinstance(r.get("scores"), list)
            and {e.get("item_id") for e in r["scores"] if isinstance(e, dict)} == set(ids)
            and all(isinstance(e.get("score"), (int, float)) for e in r["scores"])
        ),
        "a score for every passage",
    )
    ranked = sorted(scored["scores"], key=lambda e: -float(e["score"]))
    return [(ids[e["item_id"]], float(e["score"])) for e in ranked]


# --- Prompts -----------------------------------------------------------------------------------


def escape(text: str) -> str:
    return html.escape(text, quote=False)


def queries_prompt(spine: Spine, section: Section, rules: dict[str, Any]) -> str:
    settings = rules["search"]
    bullets = "\n".join(f"- {escape(b)}" for b in section.bullets)
    return (
        f"Chapter: {escape(spine.title)}\nSection: {escape(section.heading)}\n"
        f"<outline>\n{bullets}\n</outline>\n\n"
        f"Write {settings['queries_per_section']} short encyclopedia search queries, each at most "
        f"{settings['max_query_words']} words, that would find articles to back up these points. "
        "Use the general topic, not the author's wording or opinions."
    )


def draft_prompt(
    spine: Spine, section: Section, shown: dict[str, Passage], rules: dict[str, Any]
) -> str:
    bullets = "\n".join(f"{n}. {escape(b)}" for n, b in enumerate(section.bullets, start=1))
    passages = "\n".join(
        f'<passage id="{pid}" source="{html.escape(p.title)}">\n{escape(p.text)}\n</passage>'
        for pid, p in shown.items()
    )
    return (
        f"Chapter: {escape(spine.title)}\nSection: {escape(section.heading)}\n\n"
        f"The author's bullets, numbered:\n<outline>\n{bullets}\n</outline>\n\n"
        f"Passages from Wikipedia:\n{passages or '(none found)'}\n\n"
        "Write this section as one to three paragraphs. For every sentence give its text, the "
        "number of the bullet it comes from (0 if it only links two points), and its citations: "
        "each the id of a passage that supports it and the supporting words copied exactly from "
        f"that passage, at most {rules['write']['max_quote_words']} words. Leave citations empty "
        "where no passage supports the sentence."
    )


# --- Checks in Python --------------------------------------------------------------------------

_MARKS = str.maketrans({0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"', 0x2013: "-"})


def normalise(text: str) -> str:
    """Lower case, straight quotes, single spaces: how a model's copy can differ harmlessly."""
    return " ".join(text.translate(_MARKS).lower().split())


@dataclass
class Citation:
    passage: str
    quote: str
    ok: bool
    problem: str = ""


@dataclass
class Sentence:
    text: str
    bullet: int
    citations: list[Citation]

    @property
    def cited(self) -> list[Citation]:
        return [c for c in self.citations if c.ok]


@dataclass
class SectionDraft:
    heading: str
    bullets: list[str]
    queries: list[str] = field(default_factory=list)
    shown: dict[str, Passage] = field(default_factory=dict)
    supported: dict[int, list[tuple[str, float]]] = field(default_factory=dict)
    paragraphs: list[list[Sentence]] = field(default_factory=list)
    problem: str = ""

    def sentences(self) -> list[Sentence]:
        return [s for paragraph in self.paragraphs for s in paragraph]

    def missing_bullets(self) -> list[int]:
        used = {s.bullet for s in self.sentences()}
        return [n for n in range(1, len(self.bullets) + 1) if n not in used]

    def uncited_bullets(self) -> list[int]:
        """Bullets in the draft that no checked citation backs."""
        cited = {s.bullet for s in self.sentences() if s.cited}
        used = {s.bullet for s in self.sentences()}
        return [n for n in range(1, len(self.bullets) + 1) if n in used and n not in cited]


def check_draft(
    reply: dict[str, Any] | None, section: SectionDraft, rules: dict[str, Any]
) -> list[list[Sentence]]:
    """The model's draft as sentences whose citations are checked: each must name a passage the
    model was given, and quote it word for word, within the word limit."""
    paragraphs: list[list[Sentence]] = []
    raw_paragraphs = reply.get("paragraphs", []) if isinstance(reply, dict) else []
    for raw_paragraph in raw_paragraphs if isinstance(raw_paragraphs, list) else []:
        sentences: list[Sentence] = []
        raw_sentences = (
            raw_paragraph.get("sentences", []) if isinstance(raw_paragraph, dict) else []
        )
        for raw in raw_sentences if isinstance(raw_sentences, list) else []:
            if not isinstance(raw, dict) or not str(raw.get("text", "")).strip():
                continue
            bullet = raw.get("bullet")
            if not isinstance(bullet, int) or not 0 <= bullet <= len(section.bullets):
                bullet = 0
            citations = [
                check_citation(c, section.shown, rules)
                for c in raw.get("citations", [])
                if isinstance(c, dict)
            ]
            sentences.append(Sentence(str(raw["text"]).strip(), bullet, citations))
        if sentences:
            paragraphs.append(sentences)
    return paragraphs


def check_citation(
    raw: dict[str, Any], shown: dict[str, Passage], rules: dict[str, Any]
) -> Citation:
    passage, quote = str(raw.get("passage", "")).strip(), str(raw.get("quote", "")).strip()
    if passage not in shown:
        return Citation(passage, quote, False, "names a passage the model wasn't given")
    if not quote:
        return Citation(passage, quote, False, "quotes nothing")
    if len(quote.split()) > rules["write"]["max_quote_words"]:
        return Citation(passage, quote, False, "quotes more words than allowed")
    if normalise(quote.strip(" .\"'")) not in normalise(shown[passage].text):
        return Citation(passage, quote, False, "the quote isn't in the passage")
    return Citation(passage, quote, True)


# --- One section, then the chapter -----------------------------------------------------------


def draft_section(
    spine: Spine,
    section: Section,
    calls: Calls,
    client: SIEClient,
    wiki: Wikipedia,
    articles: dict[str, Article | None],
    rules: dict[str, Any],
) -> SectionDraft:
    out = SectionDraft(section.heading, section.bullets)
    settings, search = rules["retrieval"], rules["search"]
    try:
        reply = chat_json(
            calls, client, rules, queries_prompt(spine, section, rules), "queries", QUERIES_SCHEMA
        )
        out.queries = clean_queries(reply, rules)
        passages: list[Passage] = []
        seen: set[tuple[str, int, str]] = set()
        for query in out.queries:
            try:
                titles = wiki.search(query, search["results_per_query"])
            except SearchFailed as error:
                out.problem = str(error)
                continue
            for title in titles:
                if title not in articles:
                    try:
                        articles[title] = wiki.article(title)
                    except SearchFailed as error:
                        out.problem = str(error)
                        articles[title] = None
                article = articles[title]
                for passage in split_article(article, rules) if article else []:
                    if passage.key not in seen:
                        seen.add(passage.key)
                        passages.append(passage)
        if passages:
            vectors = embed(calls, client, rules, [p.text for p in passages], False)
            claims = embed(calls, client, rules, section.bullets, True)
            picked: dict[tuple[str, int, str], tuple[Passage, float]] = {}
            for number, (bullet, claim) in enumerate(zip(section.bullets, claims, strict=True), 1):
                nearest = np.argsort(-(vectors @ claim))[: settings["embed_top_k"]]
                ranked = rerank(calls, client, rules, bullet, [passages[int(i)] for i in nearest])
                kept = [(p, s) for p, s in ranked if s >= settings["min_rerank_score"]]
                for passage, score in kept[: settings["per_bullet"]]:
                    best = picked.get(passage.key)
                    if best is None or score > best[1]:
                        picked[passage.key] = (passage, score)
                    out.supported.setdefault(number, []).append((passage.key[0], round(score, 3)))
            best_first = sorted(picked.values(), key=lambda item: -item[1])
            out.shown = {
                f"P{n}": p for n, (p, _) in enumerate(best_first[: settings["max_passages"]], 1)
            }
        reply = chat_json(
            calls,
            client,
            rules,
            draft_prompt(spine, section, out.shown, rules),
            "draft",
            DRAFT_SCHEMA,
        )
        out.paragraphs = check_draft(reply, out, rules)
        if not out.paragraphs:
            out.problem = out.problem or "the model returned no draft"
    except CallFailed as error:
        out.problem = str(error)
    return out


def references(drafts: list[SectionDraft]) -> dict[tuple[str, int], int]:
    """A number for each article revision the draft cites, in order of first citation."""
    numbers: dict[tuple[str, int], int] = {}
    for draft in drafts:
        for sentence in draft.sentences():
            for citation in sentence.cited:
                passage = draft.shown[citation.passage]
                numbers.setdefault((passage.title, passage.revision), len(numbers) + 1)
    return numbers


def chapter_markdown(spine: Spine, drafts: list[SectionDraft], fetched: str) -> str:
    numbers = references(drafts)
    lines = [f"# {spine.title}", ""]
    for draft in drafts:
        lines += [f"## {draft.heading}", ""]
        if not draft.paragraphs:
            lines += [f"*No draft: {draft.problem}. Your bullets:*", ""]
            lines += [f"- {bullet}" for bullet in draft.bullets] + [""]
        for paragraph in draft.paragraphs:
            parts = []
            for sentence in paragraph:
                marks = sorted(
                    {
                        numbers[(draft.shown[c.passage].title, draft.shown[c.passage].revision)]
                        for c in sentence.cited
                    }
                )
                parts.append(sentence.text + "".join(f" [{n}]" for n in marks))
            lines += [" ".join(parts), ""]
    if numbers:
        lines += ["## References", ""]
        for (title, revision), number in numbers.items():
            url = f"https://en.wikipedia.org/w/index.php?oldid={revision}"
            lines.append(
                f'{number}. Wikipedia, "{title}", revision {revision}, read {fetched}. {url}'
            )
        lines += [
            "",
            "Wikipedia text is under CC BY-SA 4.0. Quoting it needs attribution, as above.",
            "",
        ]
    return "\n".join(lines)


def review_markdown(drafts: list[SectionDraft]) -> str:
    """What to check first, then where every sentence came from."""
    flags = [line for draft in drafts for line in section_flags(draft)]
    lines = ["# Review", "", "## Check these first", "", *(flags or ["- Nothing."]), ""]
    lines += [
        "## Where every sentence came from",
        "",
        "The model wrote every sentence below. Rewrite each in your own words before it goes in "
        "the book.",
        "",
    ]
    for draft in drafts:
        lines += [f"### {draft.heading}", "", "| Sentence | From bullet | Sources it quotes |"]
        lines.append("| --- | --- | --- |")
        for sentence in draft.sentences():
            sources = "; ".join(
                f'{draft.shown[c.passage].title}: "{c.quote}"' for c in sentence.cited
            )
            bullet = str(sentence.bullet) if sentence.bullet else "none"
            lines.append(f"| {cell(sentence.text)} | {bullet} | {cell(sources) or 'none'} |")
        lines.append("")
    return "\n".join(lines)


def section_flags(draft: SectionDraft) -> list[str]:
    """One line per thing the author should look at in this section."""
    at = f"- **{draft.heading}**"
    flags = [
        f'{at}, bullet {n}: left out of the draft: "{draft.bullets[n - 1]}"'
        for n in draft.missing_bullets()
    ]
    for n in draft.uncited_bullets():
        why = "no citation held up" if n in draft.supported else "no passage scored high enough"
        flags.append(
            f"{at}, bullet {n}: no source ({why}). Back it up yourself, or present it as your "
            f'view: "{draft.bullets[n - 1]}"'
        )
    for sentence in draft.sentences():
        flags += [
            f'{at}: a citation was dropped because it {c.problem}: "{sentence.text}"'
            for c in sentence.citations
            if not c.ok
        ]
        if sentence.bullet == 0 and not sentence.cited:
            flags.append(
                f'{at}: the model added this, from no bullet and no source: "{sentence.text}"'
            )
    if draft.problem:
        flags.append(f"{at}: {draft.problem}")
    return flags


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def queries_markdown(drafts: list[SectionDraft], articles: dict[str, Article | None]) -> str:
    lines = ["# What this run sent to Wikipedia", "", "## Search queries", ""]
    for draft in drafts:
        for query in draft.queries:
            lines.append(f'- {draft.heading}: "{query}"')
    lines += ["", "## Articles read", ""]
    for title, article in articles.items():
        lines.append(
            f"- {title}" + (f", revision {article.revision}" if article else ", not found")
        )
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> None:
    rules = load_rules(args.rules)
    spine = parse_spine(args.spine.read_text(encoding="utf-8"))
    client, calls = make_client(), Calls()
    search = rules["search"]
    wiki = Wikipedia(search["api"], search["user_agent"], search["timeout_s"])
    articles: dict[str, Article | None] = {}
    started = time.monotonic()
    fetched = datetime.now(UTC).strftime("%Y-%m-%d")
    drafts = []
    for section in spine.sections:
        print(f"Drafting: {section.heading}", flush=True)
        drafts.append(draft_section(spine, section, calls, client, wiki, articles, rules))
    seconds = time.monotonic() - started

    out_dir = RUNS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "chapter.md").write_text(chapter_markdown(spine, drafts, fetched), encoding="utf-8")
    (out_dir / "review.md").write_text(review_markdown(drafts), encoding="utf-8")
    (out_dir / "queries.md").write_text(queries_markdown(drafts, articles), encoding="utf-8")
    summary = run_summary(drafts, calls, wiki, seconds)
    record = {"summary": summary, "sections": [asdict(d) for d in drafts]}
    (out_dir / "run.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    print("\n".join(f"{key}: {value}" for key, value in summary.items()))
    print(f"Wrote {out_dir.relative_to(ROOT)}/")


def run_summary(
    drafts: list[SectionDraft], calls: Calls, wiki: Wikipedia, seconds: float
) -> dict[str, Any]:
    sentences = [s for d in drafts for s in d.sentences()]
    bullets = sum(len(d.bullets) for d in drafts)
    return {
        "Sections drafted": f"{sum(1 for d in drafts if d.paragraphs)} of {len(drafts)}",
        "Bullets with a checked citation": (
            f"{bullets - sum(len(d.uncited_bullets()) + len(d.missing_bullets()) for d in drafts)}"
            f" of {bullets}"
        ),
        "Bullets left out": sum(len(d.missing_bullets()) for d in drafts),
        "Sentences": len(sentences),
        "Citations kept": sum(len(s.cited) for s in sentences),
        "Citations dropped": sum(len(s.citations) - len(s.cited) for s in sentences),
        "Sentences from no bullet and no source": sum(
            1 for s in sentences if s.bullet == 0 and not s.cited
        ),
        "Wikipedia requests": wiki.requests,
        "SIE calls": f"{calls.total}, of which {len(calls.failures)} failed",
        "SIE credits charged": round(sum(calls.credits.values()), 2),
        "Time": f"{seconds:.0f} s",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--spine", type=Path, default=SPINE_PATH, help="the chapter's outline")
    parser.add_argument("--rules", type=Path, default=RULES_PATH, help="another rules.yaml to try")
    args = parser.parse_args()
    try:
        run(args)
    except SIEStopped as error:
        raise SystemExit(f"Stopped: {error}") from None


if __name__ == "__main__":
    main()
