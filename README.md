# SIE Cookbook

[![CI](https://github.com/superlinked/cookbook/actions/workflows/ci.yml/badge.svg)](https://github.com/superlinked/cookbook/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)
[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/superlinked/cookbook)

Example apps built on open models served by [SIE](https://github.com/superlinked/sie), the
Superlinked Inference Engine. Each recipe is one job a team does every week, such as redacting
chats or triaging phishing, built from SIE model calls plus rules in plain Python and YAML that
you can read and change. The same code runs on [SIE Cloud](https://superlinked.com/?utm_source=github&utm_medium=docs&utm_campaign=sie-cookbook&utm_content=readme) or on an SIE
you host yourself.

Every recipe is its own folder with its own `pyproject.toml` and `uv.lock`, so you install only
what that recipe needs. Copy a folder into your own project and it still runs.

## Run a recipe

You need [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for you) and an SIE API key
([create one](https://console.superlinked.com/console/api-keys?utm_source=github&utm_medium=docs&utm_campaign=sie-cookbook&utm_content=readme)).

```bash
cp .env.example .env   # then set SIE_API_KEY; every recipe reads this root .env
cd security/screen_assistant_prompts
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

Run time is a warm live run on SIE Cloud, on the date in each recipe's README (2 to 4 Nov 2026
for most). A run that has to start a cold model can take several minutes longer. Calls counts the
recipe's SIE requests (one request can carry many items).

### Finance

| Recipe | Models | SIE calls | Run time |
| --- | --- | --- | --- |
| [Map financial statements into your template and check the totals](finance/map_financial_statements_to_template) | docling, Qwen3-Reranker-4B | 98 | 30 s |
| [Check KYC documents against your policy and list what's missing](finance/check_kyc_documents_against_policy) | LightOnOCR-3-4B, gliner2-large-v1 | 48 | 90 s |
| [Alert the right person when the news mentions one of your counterparties](finance/alert_on_counterparty_news) | NuNER_Zero, GLiNER2.5-Decide | 7 | 2 s |
| [Answer your standing questions on a new filing, next to last year's answers](finance/answer_questions_on_new_filing) | Qwen3-Reranker-4B, Qwen3.8-27B-FP8 | 28 | 29 s |
| [Keep a case summary current, with a source for every line and no card data](finance/summarize_case_with_sources) | pii-redact, Qwen3.8-27B-FP8 | 6 | 8 s |

### E-commerce

| Recipe | Models | SIE calls | Run time |
| --- | --- | --- | --- |
| [Categorize products into your taxonomy and fill in the required attributes](ecommerce/categorize_products_into_taxonomy) | Qwen3-Embedding-4B, Qwen3-Reranker-4B, gliner2-large-v1 | 118 | 45 s |
| [Match products to your catalog: same product, variant or new](ecommerce/match_products_to_catalog) | siglip-so400m-patch14-384, Qwen3-Embedding-4B, Qwen3.8-27B-FP8 | 15 | 16 s |
| [Tag products from their photo and text against your tag lists](ecommerce/tag_products_from_photo_and_text) | Qwen3.8-27B-FP8 | 18 | 34 s |
| [Search products by photo, by words, or both](ecommerce/search_products_by_photo_and_text) | siglip-so400m-patch14-384 | 13 | 5 s |
| [Rerank search results without breaking the shopper's filters](ecommerce/rerank_search_within_filters) | Qwen3-Reranker-4B | 8 | 5 s |

### Retail

| Recipe | Models | SIE calls | Run time |
| --- | --- | --- | --- |
| [Check a shelf from one photo: in stock, right product, right price](retail/check_shelf_from_photo) | grounding-dino-base, GLM-OCR, Qwen3.8-27B-FP8 | 43 | 43 s |
| [Check a returned item against the sale before taking it back](retail/check_returned_item_against_sale) | GLM-OCR, Qwen3.8-27B-FP8 | 15 | 31 s |
| [Route customer emails to the right team, escalating safety and legal](retail/route_customer_emails) | GLiNER2.5-Decide, gliner2-large-v1 | 2 | 2 s |

### Manufacturing

| Recipe | Models | SIE calls | Run time |
| --- | --- | --- | --- |
| [Fill in a work-order completion form from the technician's notes](manufacturing/fill_work_order_from_notes) | GLiNER2.5-Decide, gliner2-large-v1 | 14 | 4 s |
| [Check a delivery against its purchase order and certificates](manufacturing/check_delivery_against_po) | LightOnOCR-3-4B, gliner2-large-v1 | 52 | 3 min |
| [Answer maintenance questions from the equipment manual, with the page](manufacturing/answer_from_equipment_manual) | tomoro-colqwen3-embed-4b:compact, Qwen3.8-27B-FP8 | 31 | 50 s |
| [Check no part is missing in assembly photos](manufacturing/check_parts_in_assembly_photos) | sam3 | 9 | 6 s |

### SRE

| Recipe | Models | SIE calls | Run time |
| --- | --- | --- | --- |
| [Match a burst of alerts to the runbooks that fix them](sre/match_alerts_to_runbooks) | Qwen3-Embedding-4B, Qwen3-Reranker-4B, Qwen3.8-27B-FP8 | 12 | 18 s |
| [Route alerts to the team that owns them](sre/route_alerts_to_owners) | GLiNER2.5-Decide | 5 | 2 s |
| [Draft a postmortem from the incident channel, alerts and deploys, with a source for every claim](sre/draft_postmortem_from_incident_data) | Qwen3.8-27B-FP8 | 7 | 40 s |
| [Redact secrets and customer data from logs before you share them](sre/redact_logs_before_sharing) | pii-redact | 3 | 2 s |

### Security

| Recipe | Models | SIE calls | Run time |
| --- | --- | --- | --- |
| [Triage reported phishing and pull out the indicators](security/triage_reported_phishing) | GLiNER2.5-Decide, gliner2-large-v1 | 2 | 1 s |
| [Screen prompts before they reach an AI assistant (copilot)](security/screen_assistant_prompts) | Qwen3Guard-Gen-4B, LightOnOCR-3-4B | 15 | 10 s |
| [Tag actors, malware and tools in threat reports](security/threat_report_tagging) | NuNER_Zero | 1 | 1 s |
| [Map how threats relate across reports](security/threat_relations) | gliner-relex-large-v1.0 | 1 | 1 s |

### Personal agents

| Recipe | Models | SIE calls | Run time |
| --- | --- | --- | --- |
| [Turn messages into calendar holds, payments, replies and to-dos](personal/turn_messages_into_actions) | parakeet-tdt-0.6b-v3, LightOnOCR-3-4B, GLiNER2.5-Decide, gliner2-large-v1 | 30 | 11 s |
| [Keep a fact sheet current from your notes, voice memos and scans](personal/keep_facts_current_from_notes) | parakeet-tdt-0.6b-v3, LightOnOCR-3-4B, Qwen3-Embedding-4B, Qwen3-Reranker-4B, Qwen3.8-27B-FP8 | 38 | 26 s |

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
