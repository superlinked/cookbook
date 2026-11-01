# AGENTS.md — SIE Cookbook contributor guide

## Scope

This repository holds example apps built on SIE. Each recipe is one job a team
does every week, in one industry, built from SIE model calls plus rules in plain
Python and YAML. Every recipe folder is self-contained: a reader can copy one
folder into their own project and it runs with only `uv` and an SIE API key.

## Layout

- `<industry>/<recipe>/` holds one recipe: `recipe.py`, `README.md`,
  `rules.yaml` (or other config), `data/`, and optionally `make_data.py` or
  `fetch_data.py` that rebuild the data, and `selfcheck.py` (offline checks).
- Each recipe has its own `pyproject.toml` (`[tool.uv] package = false`),
  `uv.lock`, `.python-version` (3.12) and `.env.example`. There is no root
  Python project and no shared package.
- `MODELS.md` lists every model the recipes use; `SELF_HOSTING.md` covers
  running them on your own SIE.
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
recipe folder or the repo root, or `SIE_BASE_URL` pointing at a self-hosted SIE
(the key is then optional).
The first call can wait minutes while SIE Cloud starts a cold model.

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
- Write READMEs in plain words: the problem, input, pipeline table, output,
  how to run it, the planted cases, and what to change. Every README also has
  `## Try another model` (swaps you actually ran, with what changed) and
  `## Run it on your own SIE` (the SIE image for each model);
  `tools/check_tree.py` fails without them.
- A new model goes in `MODELS.md` with its license and SIE image.
- Use Conventional Commits.
