# SIE Cookbook

[![CI](https://github.com/superlinked/cookbook/actions/workflows/ci.yml/badge.svg)](https://github.com/superlinked/cookbook/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)
[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/superlinked/cookbook)

Example apps built on open models served by [SIE](https://github.com/superlinked/sie), the
Superlinked Inference Engine. Each recipe is one job a team does every week, such as redacting
chats or triaging phishing, built from SIE model calls plus rules in plain Python and YAML that
you can read and change. The same code runs on [SIE Cloud](https://superlinked.com) or on an SIE
you host yourself.

Every recipe is its own folder with its own `pyproject.toml` and `uv.lock`, so you install only
what that recipe needs. Copy a folder into your own project and it still runs.

## Run a recipe

You need [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for you) and an SIE API key
([create one](https://console.superlinked.com/console/api-keys)).

```bash
cp .env.example .env   # then set SIE_API_KEY; every recipe reads this root .env
cd security/copilot_prompt_screening
uv run recipe.py
```

Or open the repo in GitHub Codespaces with the badge above and add `SIE_API_KEY` as a Codespaces
secret; the recipes read it from the environment.

A `.env` inside a recipe folder works too, if you copy one recipe out on its own. Each run writes
its outputs to `runs/<timestamp>/` in the recipe folder.

All the data in these recipes is synthetic or comes from public datasets named in each README.

## Models, swaps and self-hosting

- **[MODELS.md](MODELS.md)** lists the open models the recipes use, with licenses, and what
  happened when we swapped each one for another model.
- Every recipe README has a **Try another model** section. Each model is one line in that
  recipe's `rules.yaml`, so trying one that answers in the same format is a one-line change.
- **[SELF_HOSTING.md](SELF_HOSTING.md)** runs the recipes on your own SIE, with a `docker run`
  command or a Helm cluster. Set `SIE_BASE_URL` in `.env`; nothing else changes.

## Recipes

Run time is the faster of two live runs on SIE Cloud on 31 Oct 2026. A run that has to start a
cold model can take several minutes longer. Calls counts the recipe's SIE
requests (one request can carry many items).

### Security

| Recipe | Models | SIE calls | Run time |
| --- | --- | --- | --- |
| [Triage reported phishing and pull IOCs](security/phishing_triage) | GLiNER2.5-Decide, gliner2-large-v1 | 2 | 1 s |
| [Screen prompts to your security copilot](security/copilot_prompt_screening) | Qwen3Guard-Gen-4B | 8 | 10 s |
| [Tag actors, malware and tools in threat reports](security/threat_report_tagging) | NuNER_Zero | 1 | 1 s |
| [Map how threats relate across reports](security/threat_relations) | gliner-relex-large-v1.0 | 1 | 1 s |

## Contributing a recipe

See [CONTRIBUTING.md](CONTRIBUTING.md). In short: a new folder under its industry with
`recipe.py`, `rules.yaml`, `README.md`, a `pyproject.toml` that lists only what the recipe
imports, and a `uv.lock`. `uvx pre-commit run --all-files` runs the repo-wide checks; CI also
runs the per-recipe checks listed in
[CONTRIBUTING.md](CONTRIBUTING.md#before-you-open-a-pull-request).
[Open an issue](https://github.com/superlinked/cookbook/issues/new/choose) to ask for a recipe.

## Future work

- **Scheduled live runs.** CI checks every recipe without calling SIE; the `Live` workflow runs
  recipes against SIE Cloud only when a maintainer starts it. A weekly scheduled run of a few
  recipes would catch model drift early. It spends SIE credits, so it is not on yet.
