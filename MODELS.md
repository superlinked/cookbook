# Models

Every model the recipes call, by how many recipes use it. All of them are open-weight models served
by [SIE](https://github.com/superlinked/sie). The id is what goes in a recipe's `rules.yaml`, the
same on SIE Cloud and on your own SIE. "Image" is the SIE v0.10.1 image that serves the model
when you host it yourself (see [SELF_HOSTING.md](SELF_HOSTING.md)). Check each license on the
model card before you rely on it.

| Model | What the recipes use it for | SIE call | License | Image | Recipes |
| --- | --- | --- | --- | --- | --- |
| [`Qwen/Qwen3Guard-Gen-4B`](https://huggingface.co/Qwen/Qwen3Guard-Gen-4B) | Give a safety verdict and categories for a prompt | `chat_completions` | Apache 2.0 | `cuda12-sglang` | [copilot_prompt_screening](security/copilot_prompt_screening) |

## Swaps we ran

On 31 Oct 2026 we swapped one model at a time in each recipe's `rules.yaml` and ran the recipe on
SIE Cloud with nothing else changed. A row that says a swap did not run was checked but not run.
Each recipe README's "Try another model" section has the details. The short version:

| Default | Swap | How it went |
| --- | --- | --- |
| `Qwen/Qwen3Guard-Gen-4B` | `ibm-granite/granite-guardian-3.0-2b` | Did not run: it is not a drop-in, because it answers Yes/No, not "Safety: ...". |

## Other models

SIE serves over 200 models: embedders, rerankers, extractors, OCR, detectors, guards and LLMs.
[`packages/sie_server/models`](https://github.com/superlinked/sie/tree/main/packages/sie_server/models)
has one config per model, and [superlinked.com/models](https://superlinked.com/models) lists them by
task. SIE Cloud serves a subset; the full list is at `GET https://api.superlinked.com/v1/models`
with your API key.
