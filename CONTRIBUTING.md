# Contributing to the SIE Cookbook

New recipes, fixes and better data are all welcome. Before writing a large
recipe, open an issue with the use case so we can agree on the scope.

## Set up

You need Git and [uv](https://docs.astral.sh/uv/); uv installs Python 3.12 for each recipe.
Or open the repo in GitHub Codespaces, which has both.

```bash
git clone https://github.com/<your-user>/cookbook.git
cd cookbook
cp .env.example .env            # then set SIE_API_KEY
uvx pre-commit install          # ruff, file hygiene, actionlint, tree and lock checks on commit
```

## Add a recipe

1. Create `<industry>/<recipe>/` with `recipe.py`, `README.md`, `rules.yaml`
   and `data/`. Copy an existing recipe's layout; see [AGENTS.md](AGENTS.md)
   for the rules every recipe follows.
2. Add a `pyproject.toml` with `[tool.uv] package = false` that lists only what
   the recipe imports, then run `uv lock`.
3. Run it live: `uv run recipe.py`. Put the planted cases and what came out in
   the README.
4. Add `## Try another model` with at least one swap you ran, and
   `## Run it on your own SIE` with the image for each model (copy the table
   shape from another recipe; the images are in SIE's compatibility matrix).
5. Link it from the root `README.md` under its industry, and add any new model
   to `MODELS.md`.

## Before you open a pull request

```bash
uvx pre-commit run --all-files
cd <industry>/<recipe>
uv run --with mypy mypy --strict --ignore-missing-imports ./*.py
uv run recipe.py --help
```

CI runs the same checks on every recipe. It does not call SIE; a maintainer
runs the `Live` workflow on recipes a pull request changes. Use
[Conventional Commits](https://www.conventionalcommits.org/) for commit
messages and pull request titles.
