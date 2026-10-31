## What this changes

<!-- The recipe(s) touched and why. -->

## Checks

- [ ] `uvx pre-commit run --all-files` passes
- [ ] Each changed recipe: `uv lock --check`, strict mypy and `uv run recipe.py --help` pass
- [ ] Each changed recipe ran live (`uv run recipe.py`), and its README matches what came out
- [ ] Data is synthetic or from a public dataset whose license allows redistribution, named in the README
- [ ] No API keys, `.env` files, `runs/` output or personal data
