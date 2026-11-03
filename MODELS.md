# Models

Every model the recipes call, with the recipes that use it. All of them are open-weight models served
by [SIE](https://github.com/superlinked/sie). The id is what goes in a recipe's `rules.yaml`, the
same on SIE Cloud and on your own SIE. "Image" is the SIE v0.10.1 image that serves the model
when you host it yourself (see [SELF_HOSTING.md](SELF_HOSTING.md)). Check each license on the
model card before you rely on it.

| Model | What the recipes use it for | SIE call | License | Image | Recipes |
| --- | --- | --- | --- | --- | --- |
| [`fastino/gliner2-large-v1`](https://huggingface.co/fastino/gliner2-large-v1) | Pull named fields and entities out of text, to a schema | `extract` | Apache 2.0 | `cuda12-default` | [inspection_reports](manufacturing/inspection_reports), [work_order_closeout](manufacturing/work_order_closeout), [customer_email_routing](retail/customer_email_routing), [phishing_triage](security/phishing_triage) |
| [`Qwen/Qwen3.8-27B-FP8`](https://huggingface.co/Qwen/Qwen3.8-27B-FP8) | Read text or a page image and answer in JSON, with citations | `chat_completions` | Apache 2.0 | `cuda13-sglang-cu130` (H100 80GB or larger) | [equipment_manual_qa](manufacturing/equipment_manual_qa), [return_item_check](retail/return_item_check), [shelf_inventory_check](retail/shelf_inventory_check) |
| [`lightonai/LightOnOCR-3-4B`](https://huggingface.co/lightonai/LightOnOCR-3-4B) | Turn a scanned page, photo or screenshot into text | `extract` | Apache 2.0 | `cuda12-sglang-vision-extract` | [inspection_reports](manufacturing/inspection_reports), [copilot_prompt_screening](security/copilot_prompt_screening) |
| [`fastino/GLiNER2.5-Decide`](https://huggingface.co/fastino/GLiNER2.5-Decide) | Answer a typed question about a text (pick one, yes or no) with a score | `extract` | Apache 2.0 | `cuda12-transformers5` | [work_order_closeout](manufacturing/work_order_closeout), [customer_email_routing](retail/customer_email_routing), [phishing_triage](security/phishing_triage) |
| [`numind/NuNER_Zero`](https://huggingface.co/numind/NuNER_Zero) | Tag entities of any type you name | `extract` | MIT | `cuda12-default` | [threat_report_tagging](security/threat_report_tagging) |
| [`zai-org/GLM-OCR`](https://huggingface.co/zai-org/GLM-OCR) | Read product packaging and shelf tags from photos | `extract` | MIT | `cuda12-sglang-vision-extract` | [return_item_check](retail/return_item_check), [shelf_inventory_check](retail/shelf_inventory_check) |
| [`facebook/sam3`](https://huggingface.co/facebook/sam3) | Find every instance of an object named in text | `extract` | SAM License (not OSI) | `cuda12-transformers5` | [inspection_photos](manufacturing/inspection_photos) |
| [`TomoroAI/tomoro-colqwen3-embed-4b`](https://huggingface.co/TomoroAI/tomoro-colqwen3-embed-4b) | Embed page images (diagrams included) as many vectors per page | `encode` | Apache 2.0 | `cuda12-default` | [equipment_manual_qa](manufacturing/equipment_manual_qa) |
| [`IDEA-Research/grounding-dino-base`](https://huggingface.co/IDEA-Research/grounding-dino-base) | Draw boxes around objects named in text | `extract` | Apache 2.0 | `cuda12-default` | [shelf_inventory_check](retail/shelf_inventory_check) |
| [`Qwen/Qwen3Guard-Gen-4B`](https://huggingface.co/Qwen/Qwen3Guard-Gen-4B) | Give a safety verdict and categories for a prompt | `chat_completions` | Apache 2.0 | `cuda12-sglang` | [copilot_prompt_screening](security/copilot_prompt_screening) |
| [`knowledgator/gliner-relex-large-v1.0`](https://huggingface.co/knowledgator/gliner-relex-large-v1.0) | Find entities and the relations between them | `extract` | Apache 2.0 | `cuda12-default` | [threat_relations](security/threat_relations) |

## Swaps we ran

On 31 Oct 2026 (2 Nov 2026 for the security and retail recipes, 3 Nov 2026 for manufacturing) we swapped one model at a time in each recipe's `rules.yaml` and ran the recipe on
SIE Cloud with nothing else changed. A row that says a swap did not run was checked but not run.
Each recipe README's "Try another model" section has the details. The short version:

| Default | Swap | How it went |
| --- | --- | --- |
| `fastino/gliner2-large-v1` | `fastino/gliner2-base-v1` | Same verdicts, routes, IOCs and observables in phishing_triage (rerun 2 Nov 2026); only the span columns changed. Clearly worse in work_order_closeout (3 Nov 2026): 0 of 10 forms passed, actions missed on 5 notes and the planted catalog near miss went unflagged. |
| `Qwen/Qwen3.8-27B-FP8` | `Qwen/Qwen3.5-4B` | Same guidance on all 7 returns in return_item_check; it called the shrink-wrapped pack sealed and the crushed parcel opened with major damage. Matched on the 10 earlier equipment_manual_qa questions (31 Oct 2026, before the revision and quote checks). Not tried in shelf_inventory_check. |
| `Qwen/Qwen3.8-27B-FP8` | `google/gemma-4-31B-it` | Did not run in the document recipes: SIE Cloud answered "Generation route is unavailable for this deployment" every time. |
| `numind/NuNER_Zero` | `urchade/gliner_multi-v2.1`, `numind/NuNER_Zero-span` | Worse on threat reports: 2 and 3 reports tagged instead of 4, with the 0.50 threshold chosen with NuNER_Zero in mind. |
| `facebook/sam3` | `IDEA-Research/grounding-dino-base`, `google/owlv2-base-patch16-ensemble` | Missed every planted missing bolt in inspection_photos. The thresholds are set on SAM 3's scores. |
| `zai-org/GLM-OCR` | `lightonai/LightOnOCR-3-4B` | Same guidance on all 7 returns in return_item_check. In shelf_inventory_check (before the vision step) it read two tag UPCs GLM-OCR missed, but its table output sent other tags to review. |
| `Qwen/Qwen3Guard-Gen-4B` | `ibm-granite/granite-guardian-3.0-2b` | Did not run: it is not a drop-in, because it answers Yes/No, not "Safety: ...". |
| `fastino/GLiNER2.5-Decide`, `knowledgator/gliner-relex-large-v1.0`, `TomoroAI/tomoro-colqwen3-embed-4b` | none | SIE Cloud has no same-API alternative yet; the other tomoro profiles returned HTTP 503. |

## Other models

SIE serves over 200 models: embedders, rerankers, extractors, OCR, detectors, guards and LLMs.
[`packages/sie_server/models`](https://github.com/superlinked/sie/tree/main/packages/sie_server/models)
has one config per model, and [superlinked.com/models](https://superlinked.com/models) lists them by
task. SIE Cloud serves a subset; the full list is at `GET https://api.superlinked.com/v1/models`
with your API key.
