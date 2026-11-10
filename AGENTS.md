# AGENTS.md — SIE Cookbook contributor guide

## Scope

This repository holds example apps built on SIE. Each recipe is one job a team
does every week, in one industry, built from SIE model calls plus rules in plain
Python and YAML. Every recipe folder is self-contained: a reader can copy one
folder into their own project and it runs with only `uv` and an SIE API key. Recipes in
`agentic/`, and the `agent.py` beside some workflows, also need an `ANTHROPIC_API_KEY`, because
Claude runs their agent loop and their tools call SIE models.

## Layout

- `<industry>/<recipe>/` holds one recipe: `recipe.py`, `README.md`,
  `rules.yaml` (or other config), `data/`, and optionally `make_data.py` or
  `fetch_data.py` that rebuild the data, and `selfcheck.py` (offline checks).
- Each recipe has its own `pyproject.toml` (`[tool.uv] package = false`),
  `uv.lock`, `.python-version` (3.12) and `.env.example`. There is no root
  Python project and no shared package.
- A workflow recipe may also have an `agent.py` for the odd jobs the workflow
  can't do (see "Workflow or agent?" in the root README). It imports the
  workflow's functions from `recipe.py` in the same folder, keeps its extra
  data in `data/agent/` (built by `make_agent_data.py`), and its `anthropic`
  dependency in `[dependency-groups] agent` (`uv run --group agent agent.py`).
  `recipe.py`'s results must not change, and CI never runs `agent.py`.
- `MODELS.md` lists every model the recipes use; `SELF_HOSTING.md` covers
  running them using SIE Cluster or SIE Local.
- The root `README.md` indexes every recipe by industry; add a line there for a
  new recipe.

## Commands

Run everything from inside the recipe folder:

- `uv run recipe.py` runs the recipe live and writes `runs/<timestamp>/`.
- `uv run make_data.py` rebuilds `data/` where the recipe has one. Data-only
  dependencies go in `[dependency-groups] data`, run with `--group data`.
- `uv lock` after changing dependencies.
- `uvx ruff check . && uvx ruff format --check .` (settings in the root
  `ruff.toml`).
- `uv run --with mypy mypy --strict --ignore-missing-imports *.py`.
- `uv run selfcheck.py` where a recipe has one (offline, no SIE call).

A live run needs `SIE_API_KEY` in a `.env` in the
recipe folder or the repo root, or `SIE_BASE_URL` pointing at SIE Cluster or SIE Local
(the key is then optional). Recipes in `agentic/` and every `agent.py` also need `ANTHROPIC_API_KEY`.
The first call can wait minutes while SIE Serverless starts a cold model.

## Development boundaries

- Never import from another recipe. Copy a small helper instead.
- Use `sie_sdk.SIEClient` for SIE calls. Each recipe builds its client in its
  own `make_client()`, following the existing recipes.
- Code must run on Python 3.12, the oldest version we support: no syntax or
  standard-library features from 3.13 or later. `ruff.toml` targets 3.12.
- List only the dependencies a recipe imports. Keep `pillow` below 13 where a
  `make_data.py` claims to rebuild files byte for byte.
- Keep rules, thresholds and model names in `rules.yaml`, not in code, so a
  reader can change them without editing Python.
- Recipes run live only: no recorded responses, replay mode, eval sets or
  scorecards in this repository. A recipe may have a `selfcheck.py`: plain
  asserts on its own parsing and rule functions, using hand-written strings,
  the committed `data/` and hand-written stand-in model replies. It never calls
  SIE and never replays a recorded response. CI runs it.
- All data is synthetic or from a public dataset that allows redistribution.
  Name the source, revision and license in the recipe README. No real people,
  customers, credentials or live URLs.
- Do not commit `.env`, `runs/`, `.venv` or caches.
- When a model's output drifts from what a README states, report the
  difference; do not tune rules to hide it.
- Write READMEs in plain words, like a blog post from an engineer to a peer,
  in this order: background (who does this job and why it matters to the
  company), the problem (ending with `### Examples`: two or three inputs from
  `data/`, a line saying whether they're synthetic or from a public dataset and under which
  license, and what should happen to each), how the flow solves it, why each
  model, the flow as a Mermaid chart, how it's built (rules vs code, a step
  table naming the functions), how to run it (`## Run it` opens with the SIE Serverless line:
  copy it from any recipe and set `utm_content` to the recipe path with dashes, such as
  `sre-route-alerts-to-owners`; `tools/check_tree.py` checks it), what came out (`### The three
  examples`, or `### The two examples`, in the same shape as `### Examples`, ending each one with a `Pass.`
  or `Miss.` verdict, then `### Results` with "N of M emails came out as
  expected" on the live run, using the recipe's own noun for emails, then
  `**Where it works:**` and `**Where it fails:**`, naming every miss, and
  `### What you do next`), and potential improvements. At least one example
  shows a limit or a trade-off. Every README also has
  `## Try another model` (swaps you actually ran, with what changed) and
  `## Run it using SIE Cluster or SIE Local` (the SIE image for each model);
  `tools/check_tree.py` fails without them.
- Recipe code runs on the reader's machine. Only the models run on SIE Serverless, SIE Cluster or
  SIE Local, so write "ran the recipe using SIE Serverless", not "on". Use those three
  names, not "SIE Cloud" or "your own SIE".
- A new model goes in `MODELS.md` with its license and SIE image.
- Use Conventional Commits.
