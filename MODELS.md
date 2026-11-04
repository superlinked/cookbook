# Models

Every model the recipes call, with the recipes that use it. All of them are open-weight models served
by [SIE](https://github.com/superlinked/sie). The id is what goes in a recipe's `rules.yaml`, the
same on SIE Cloud and on your own SIE. "Image" is the SIE v0.10.1 image that serves the model
when you host it yourself (see [SELF_HOSTING.md](SELF_HOSTING.md)). Check each license on the
model card before you rely on it.

| Model | What the recipes use it for | SIE call | License | Image | Recipes |
| --- | --- | --- | --- | --- | --- |
| [`fastino/gliner2-large-v1`](https://huggingface.co/fastino/gliner2-large-v1) | Pull named fields and entities out of text, to a schema | `extract` | Apache 2.0 | `cuda12-default` | [check_delivery_against_po](manufacturing/check_delivery_against_po), [fill_work_order_from_notes](manufacturing/fill_work_order_from_notes), [route_customer_emails](retail/route_customer_emails), [triage_reported_phishing](security/triage_reported_phishing) |
| [`Qwen/Qwen3-Reranker-4B`](https://huggingface.co/Qwen/Qwen3-Reranker-4B) | Score how well each passage answers a query, with an instruction | `score` | Apache 2.0 | `cuda12-default` | [match_alerts_to_runbooks](sre/match_alerts_to_runbooks) |
| [`Qwen/Qwen3.8-27B-FP8`](https://huggingface.co/Qwen/Qwen3.8-27B-FP8) | Read text or a page image and answer in JSON, with citations | `chat_completions` | Apache 2.0 | `cuda13-sglang-cu130` (H100 80GB or larger) | [answer_from_equipment_manual](manufacturing/answer_from_equipment_manual), [match_alerts_to_runbooks](sre/match_alerts_to_runbooks), [draft_postmortem_from_incident_data](sre/draft_postmortem_from_incident_data), [check_returned_item_against_sale](retail/check_returned_item_against_sale), [check_shelf_from_photo](retail/check_shelf_from_photo) |
| [`lightonai/LightOnOCR-3-4B`](https://huggingface.co/lightonai/LightOnOCR-3-4B) | Turn a scanned page, photo or screenshot into text | `extract` | Apache 2.0 | `cuda12-sglang-vision-extract` | [check_delivery_against_po](manufacturing/check_delivery_against_po), [screen_assistant_prompts](security/screen_assistant_prompts) |
| [`fastino/GLiNER2.5-Decide`](https://huggingface.co/fastino/GLiNER2.5-Decide) | Answer a typed question about a text (pick one, yes or no) with a score | `extract` | Apache 2.0 | `cuda12-transformers5` | [fill_work_order_from_notes](manufacturing/fill_work_order_from_notes), [route_customer_emails](retail/route_customer_emails), [triage_reported_phishing](security/triage_reported_phishing), [route_alerts_to_owners](sre/route_alerts_to_owners) |
| `superlinked/pii-redact` | Find personal data to redact. SIE composes [`urchade/gliner_multi_pii-v1`](https://huggingface.co/urchade/gliner_multi_pii-v1) and [`numind/NuNER_Zero`](https://huggingface.co/numind/NuNER_Zero) on the server | `extract` | Apache 2.0 and MIT | `cuda12-default` | [redact_logs_before_sharing](sre/redact_logs_before_sharing) |
| [`Qwen/Qwen3-Embedding-4B`](https://huggingface.co/Qwen/Qwen3-Embedding-4B) | Embed text for similarity search, with an instruction | `encode` | Apache 2.0 | none in v0.10.1; self-host `Qwen/Qwen3-Embedding-8B` on `cuda12-default` | [match_alerts_to_runbooks](sre/match_alerts_to_runbooks) |
| [`numind/NuNER_Zero`](https://huggingface.co/numind/NuNER_Zero) | Tag entities of any type you name | `extract` | MIT | `cuda12-default` | [threat_report_tagging](security/threat_report_tagging) |
| [`zai-org/GLM-OCR`](https://huggingface.co/zai-org/GLM-OCR) | Read product packaging and shelf tags from photos | `extract` | MIT | `cuda12-sglang-vision-extract` | [check_returned_item_against_sale](retail/check_returned_item_against_sale), [check_shelf_from_photo](retail/check_shelf_from_photo) |
| [`facebook/sam3`](https://huggingface.co/facebook/sam3) | Find every instance of an object named in text | `extract` | SAM License (not OSI) | `cuda12-transformers5` | [check_parts_in_assembly_photos](manufacturing/check_parts_in_assembly_photos) |
| [`TomoroAI/tomoro-colqwen3-embed-4b`](https://huggingface.co/TomoroAI/tomoro-colqwen3-embed-4b) | Embed page images (diagrams included) as many vectors per page | `encode` | Apache 2.0 | `cuda12-default` | [answer_from_equipment_manual](manufacturing/answer_from_equipment_manual) |
| [`IDEA-Research/grounding-dino-base`](https://huggingface.co/IDEA-Research/grounding-dino-base) | Draw boxes around objects named in text | `extract` | Apache 2.0 | `cuda12-default` | [check_shelf_from_photo](retail/check_shelf_from_photo) |
| [`Qwen/Qwen3Guard-Gen-4B`](https://huggingface.co/Qwen/Qwen3Guard-Gen-4B) | Give a safety verdict and categories for a prompt | `chat_completions` | Apache 2.0 | `cuda12-sglang` | [screen_assistant_prompts](security/screen_assistant_prompts) |
| [`knowledgator/gliner-relex-large-v1.0`](https://huggingface.co/knowledgator/gliner-relex-large-v1.0) | Find entities and the relations between them | `extract` | Apache 2.0 | `cuda12-default` | [threat_relations](security/threat_relations) |

## Swaps we ran

On 31 Oct 2026 (2 Nov 2026 for the security and retail recipes, 3 Nov 2026 for manufacturing) we swapped one model at a time in each recipe's `rules.yaml` and ran the recipe on
SIE Cloud with nothing else changed. A row that says a swap did not run was checked but not run.
Each recipe README's "Try another model" section has the details. The short version:

| Default | Swap | How it went |
| --- | --- | --- |
| `fastino/gliner2-large-v1` | `fastino/gliner2-base-v1` | Same verdicts, routes, IOCs and observables in triage_reported_phishing (rerun 2 Nov 2026); only the span columns changed. Clearly worse in fill_work_order_from_notes (3 Nov 2026): 0 of 10 forms passed, actions missed on 5 notes and the planted catalog near miss went unflagged. |
| `Qwen/Qwen3.8-27B-FP8` | `Qwen/Qwen3.5-4B` | Same guidance on all 7 returns in check_returned_item_against_sale; it called the shrink-wrapped pack sealed and the crushed parcel opened with major damage. Matched on the 10 earlier answer_from_equipment_manual questions (31 Oct 2026, before the revision and quote checks). Not tried in check_shelf_from_photo. |
| `Qwen/Qwen3.8-27B-FP8` | `google/gemma-4-31B-it` | Did not run in the document recipes: SIE Cloud answered "Generation route is unavailable for this deployment" every time. |
| `superlinked/pii-redact` | `urchade/gliner_multi_pii-v1` | Same statuses in most documents, different labels. In redact_logs_before_sharing (rerun 4 Nov 2026) it missed a reset token, so the final sweep blocked the checkout log, and it tagged a source line (`worker.py:41`) as a URL. |
| `numind/NuNER_Zero` | `urchade/gliner_multi-v2.1`, `numind/NuNER_Zero-span` | Worse on threat reports: 2 and 3 reports tagged instead of 4, with the 0.50 threshold chosen with NuNER_Zero in mind. |
| `facebook/sam3` | `IDEA-Research/grounding-dino-base`, `google/owlv2-base-patch16-ensemble` | Missed every planted missing bolt in check_parts_in_assembly_photos. The thresholds are set on SAM 3's scores. |
| `zai-org/GLM-OCR` | `lightonai/LightOnOCR-3-4B` | Same guidance on all 7 returns in check_returned_item_against_sale. In check_shelf_from_photo (before the vision step) it read two tag UPCs GLM-OCR missed, but its table output sent other tags to review. |
| `Qwen/Qwen3Guard-Gen-4B` | `ibm-granite/granite-guardian-3.0-2b` | Did not run: it is not a drop-in, because it answers Yes/No, not "Safety: ...". |
| `fastino/GLiNER2.5-Decide`, `knowledgator/gliner-relex-large-v1.0`, `TomoroAI/tomoro-colqwen3-embed-4b` | none | SIE Cloud has no same-API alternative yet; the other tomoro profiles returned HTTP 503. |

## Other models

SIE serves over 200 models: embedders, rerankers, extractors, OCR, detectors, guards and LLMs.
[`packages/sie_server/models`](https://github.com/superlinked/sie/tree/main/packages/sie_server/models)
has one config per model, and [superlinked.com/models](https://superlinked.com/models?utm_source=github&utm_medium=docs&utm_campaign=sie-cookbook&utm_content=models) lists them by
task. SIE Cloud serves a subset; the full list is at `GET https://api.superlinked.com/v1/models`
with your API key.
