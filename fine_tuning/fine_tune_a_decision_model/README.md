# Fine-tune your own decision model on 100 rows, and keep the data on your servers

> **This example:** two small decisions a team makes thousands of times a day. One is public: which team
> should handle a banking customer's message (Banking77). One is private: after a document parser runs, what
> is wrong with its output, so the pipeline knows whether to accept it, re-run OCR, re-parse the tables or send
> it to a person.
>
> **How it works:** a small GLiClass decision model (`knowledgator/gliclass-edge-v3.0`, 131 MB) answers each
> decision zero-shot from the label names. Then the recipe fine-tunes it on about 100 labelled rows on your own
> machine and scores both on rows it never saw.
>
> **Use the same pattern for:** any yes/no or pick-one decision you make on text before spending money on it:
> routing to a model, a team or a queue, quality gates, triage, spam, intent.

## Background

Most pipelines are full of small decisions. Is this parsed page good enough to extract from? Which team should
get this message? Should this question go to the cheap model or the expensive one? Teams often send each of these
to a frontier LLM with a prompt, which works, costs something on every call, and sends the text to someone else's
servers.

A decision model is the other option: a classifier of a few hundred megabytes or less that picks one of your
labels. It runs on a CPU or a slice of a GPU you already have, in milliseconds, and the text never leaves your
network. The catch is that a general model doesn't know your labels the way you mean them. That's where 100
labelled rows come in.

## The problem

Zero-shot decision models read the label names and guess. On public, everyday text (a customer saying their
card hasn't arrived) that can be good enough. On your own data, with your own labels, it often isn't: "table
flattened into run-on numbers" means something specific in your pipeline, and the model has never seen your
parser's output. So the questions are:

- How far does zero-shot get on a public dataset, and on a private one?
- How much does fine-tuning on about 100 rows help each?
- How long does that take, and on what hardware?

### Examples

`data/public/` is a slice of [Banking77](https://huggingface.co/datasets/PolyAI/banking77) (PolyAI, CC-BY-4.0):
six intents, 17 training and 25 test messages each. `data/private/` is synthetic, written by `make_data.py` for
this recipe under this repo's Apache 2.0 license: six kinds of parser output, 17 training and 10 test rows each.

1. *"My card got lost."* (public): should be **card lost or stolen**, so the fraud team blocks the card.
2. *"De|ivery N0te frorn Gr`an1t~3 ¦1¬iisurance. P|3a$e quote r3f3r3n(3 B59874on @ll (orr3§$poiideiice..."*
   (private): should be **garbled OCR characters**, so the pipeline re-runs OCR with a stronger model.
3. *"Purchase Order from Northwind Supply. Goods were received in good condition on 24 April 2025. The lease
   agreement covers services delivered between December and..."* (private): should be **clean readable text**,
   so it goes on to extraction. It's the hard one: it reads like a document that stops early.

## How it works

1. `make_data.py` builds both datasets (Banking77 from Hugging Face; the private set from templates with a fixed
   seed).
2. `recipe.py` scores the zero-shot model on each test set: the label with the highest score wins.
3. It fine-tunes the same model on the ~100 training rows with GLiClass's own trainer.
4. It scores the fine-tuned model on the same test set and prints a 2 x 2 table.

```mermaid
flowchart LR
    A["~100 labelled rows<br/>(public or private)"] --> B["Fine-tune GLiClass<br/>on your machine"]
    C["Test rows"] --> D["Zero-shot GLiClass"]
    C --> E["Fine-tuned GLiClass"]
    B --> E
    D --> F["Accuracy, per label"]
    E --> F
    classDef recipe fill:#e8f0fe,stroke:#1a73e8
    class B,E recipe;
```

### Why GLiClass

GLiClass reads the text and all the candidate labels together and scores each label, so the labels are plain
English you can change without retraining, and one forward pass scores all of them. The edge model is small
enough to fine-tune on a laptop in minutes. SIE serves GLiClass models as `extract` with `labels`, and can serve
fine-tuned GLiClass weights the same way (see below).

## How it's built

| Step | Where | What |
| --- | --- | --- |
| Labels, prompts, model, training settings | `rules.yaml` | The decision and what your pipeline does with each answer |
| Build the data | `make_data.py` | `build_public()`, `build_private()` |
| Score | `recipe.py` | `decide()`: one decision per row, accuracy overall and per label |
| Fine-tune | `recipe.py` | `fine_tune()`: GLiClass `Trainer`, no augmentation, fixed seed |
| Serve | `sie_models/*.yaml` | One SIE model config per fine-tuned model, pointing `weights_path` at the saved weights |
| Score through SIE | `serve.py` | `decide()`: one `extract` call per 16 rows, zero-shot and fine-tuned |

## Run it

**Run this recipe using [SIE Serverless](https://superlinked.com/serverless?utm_source=github&utm_medium=docs&utm_campaign=sie-cookbook&utm_content=fine-tuning-fine-tune-a-decision-model), [SIE Cluster](https://superlinked.com/docs/deployment?utm_source=github&utm_medium=docs&utm_campaign=sie-cookbook&utm_content=fine-tuning-fine-tune-a-decision-model) or [SIE Local](https://superlinked.com/docs/quickstart?utm_source=github&utm_medium=docs&utm_campaign=sie-cookbook&utm_content=fine-tuning-fine-tune-a-decision-model).** The code runs on your machine either way. With SIE Serverless you pay for what each call uses and there are no GPUs to manage yourself: get an [SIE API key](https://console.superlinked.com/console/keys?utm_source=github&utm_medium=docs&utm_campaign=sie-cookbook&utm_content=fine-tuning-fine-tune-a-decision-model) and read the [API docs](https://superlinked.com/docs/reference/api?utm_source=github&utm_medium=docs&utm_campaign=sie-cookbook&utm_content=fine-tuning-fine-tune-a-decision-model). To run the models yourself, start from the [SIE repo](https://github.com/superlinked/sie).

Training and the comparison run on your machine (CUDA, Apple MPS or CPU) and need no key:

```sh
uv run --group data make_data.py   # rebuild data/ (downloads Banking77)
uv run recipe.py                   # zero-shot vs fine-tuned, both datasets
uv run recipe.py --only private    # one dataset
uv run serve.py                    # the same test rows through SIE (start SIE first, see below)
```

The first run downloads the model (131 MB). Each run writes `runs/<timestamp>/results.json` with every decision,
and the fine-tuned weights under `runs/<timestamp>/<dataset>/model/`.

## What came out

We ran the recipe on 14 November 2026 on an Apple M-series laptop (MPS), with `knowledgator/gliclass-edge-v3.0`,
8 epochs, learning rate 2e-5, batch size 8, seed 20261114.

### The three examples

1. *"My card got lost."*: zero-shot picked **wrong amount of cash from ATM** (score 0.99). Fine-tuned picked
   **card lost or stolen** (1.00).

- **Verdict:** Pass.

2. The garbled delivery note: zero-shot picked **text not in English** (0.95), which is a fair guess at that mess
   of symbols, and would have sent it to translation. Fine-tuned picked **garbled OCR characters** (1.00).

- **Verdict:** Pass.

3. The purchase order: zero-shot got it right (**clean readable text**, 0.98). Fine-tuned picked **document cut
   off mid-sentence**, with a top score of only 0.09.

- **Verdict:** Miss. A useful one: the model wasn't sure, and a score that low is your cue to send the row to a
  person instead of acting on it.


### Results

| | Zero-shot | Fine-tuned on 102 rows |
| --- | --- | --- |
| Banking77, public (150 test messages) | 67.3% (101) | **93.3% (140)** |
| Parsed-document quality, private (60 test rows) | 30.0% (18) | **86.7% (52)** |

Fine-tuning took 5.5 s for each dataset on the laptop, and scoring took 2 to 3 ms a row. 140 of 150 test messages
and 52 of 60 parsed pages came out as expected after fine-tuning.

Per label:

| Label | Zero-shot | Fine-tuned |
| --- | --- | --- |
| card has not arrived | 8% | 80% |
| card lost or stolen | 44% | 88% |
| exchange rate question | 100% | 100% |
| transfer not received by recipient | 72% | 96% |
| top-up failed | 88% | 100% |
| wrong amount of cash from ATM | 92% | 96% |
| clean readable text | 30% | 50% |
| garbled OCR characters | 10% | 100% |
| table flattened into run-on numbers | 0% | 100% |
| document cut off mid-sentence | 30% | 70% |
| handwriting left untranscribed | 10% | 100% |
| text not in English | 100% | 100% |

**Where it works:** anything a person can see at a glance but the label name doesn't describe well. Zero-shot read
every flattened table as a cut-off document (10 of 10), and most garbled OCR and untranscribed handwriting as "text
not in English" (8 of 10 and 5 of 10), which would send them to translation. A hundred examples fixed all three
(100%). On the public data, the biggest gain was "card has not
arrived", which zero-shot almost never picked (8% to 80%).

**Where it fails:** the two labels that differ only by how a document ends. All 8 private misses are clean text
and cut-off text swapped for each other (5 one way, 3 the other), because our synthetic clean documents also end
mid-thought sometimes. On Banking77, 7 of the 10 misses are "card has not arrived" and "card lost or stolen" swapped,
which people mix up too.

Two cautions. The private set is synthetic and made by templates, so it is easier to learn than your parser's real
output: treat 86.7% as what 100 rows can do on a clear-cut decision, not as a promise. And the test sets are small
(150 and 60 rows), so a few rows move the numbers by several points.

### What you do next

Label about 100 rows of your own decision, put them in `data/private/train.jsonl` in the same format, change the
labels in `rules.yaml`, and run the recipe. Look at the per-label table before the overall number, and add rows
where a label is weak. Then serve the fine-tuned model next to your pipeline with SIE (below).

## Serve your fine-tuned model with SIE

SIE serves any model it has a config for. A config is a small YAML file: SIE's catalog has one per model, and
`sie_models/` here has one for each model `recipe.py` trains. Each is a copy of the catalog's
`knowledgator__gliclass-edge-v3.0.yaml` with two changes: a new `sie_id` (`my-team/parsed-page-check`), and
`weights_path: /weights/private` in place of `hf_id` and `hf_revision`, so SIE loads your weights from a folder
instead of Hugging Face. The weights never leave your machine.

### Start SIE Local in Docker

Mount the two configs into the image's models folder and the weights `recipe.py` saved where the configs say.
Use your own run folder in place of the timestamp:

```sh
RUN=runs/20261114T171002Z
docker run -d --name sie-decision -p 8080:8080 \
  -v sie-hf-cache:/app/.cache/huggingface \
  -v "$PWD/sie_models/public.yaml:/app/models/my-team__banking77-router.yaml:ro" \
  -v "$PWD/sie_models/private.yaml:/app/models/my-team__parsed-page-check.yaml:ro" \
  -v "$PWD/$RUN/public/model:/weights/public:ro" \
  -v "$PWD/$RUN/private/model:/weights/private:ro" \
  ghcr.io/superlinked/sie-server:v0.11.1-cpu-default

curl http://localhost:8080/readyz   # expect: ok
```

`curl http://localhost:8080/v1/models` now lists `my-team/banking77-router` and `my-team/parsed-page-check`
next to the catalog models. On a machine with an NVIDIA GPU, use `v0.11.1-cuda12-default` and add `--gpus all`.
On SIE Cluster, put the config in the cluster's models folder and the weights on a path or bucket its workers can
read.

### Ask it for a decision

```sh
curl -s http://localhost:8080/v1/extract/my-team/parsed-page-check \
  -H 'content-type: application/json' -H 'accept: application/json' \
  -d '{"items": [{"text": "Item Qty Unit price Total Freight 12 418.07 5016.84 Pallets 3 92.10 276.30 Subtotal 5293.14"}],
       "params": {"instruction": "What is wrong with this text a document parser produced?",
                  "labels": ["clean readable text", "garbled OCR characters", "table flattened into run-on numbers",
                             "document cut off mid-sentence", "handwriting left untranscribed", "text not in English"]}}'
```

The first call answers `MODEL_LOADING` while SIE loads the weights (9 s here); call again. Then each label comes
back with a score, highest first:

```json
"classifications": [
  {"label": "table flattened into run-on numbers", "score": 0.971},
  {"label": "document cut off mid-sentence", "score": 0.029},
  {"label": "text not in English", "score": 0.0001},
  ...
]
```

From Python it's the same call the zero-shot model takes, with your model id (the SDK waits through the load):

```python
from sie_sdk import SIEClient
from sie_sdk.types import Item

LABELS = [
    "clean readable text",
    "garbled OCR characters",
    "table flattened into run-on numbers",
    "document cut off mid-sentence",
    "handwriting left untranscribed",
    "text not in English",
]
page = "Item Qty Unit price Total Freight 12 418.07 5016.84 Pallets 3 92.10 276.30 Subtotal 5293.14"

client = SIEClient(base_url="http://localhost:8080")
result = client.extract(
    "my-team/parsed-page-check",
    Item(text=page),
    labels=LABELS,
    instruction="What is wrong with this text a document parser produced?",
)
best = result["classifications"][0]
print(best)  # {'label': 'table flattened into run-on numbers', 'score': 0.971...}
```

### Check SIE gives the same answers

`serve.py` sends every test row to SIE, for the zero-shot model and your fine-tuned one, and writes
`runs/<timestamp>-served/results.json`:

```sh
SIE_BASE_URL=http://localhost:8080 uv run serve.py
```

We ran it on 14 November 2026 against the container above, on an Apple M-series laptop. The image is built for
x86, so Docker emulated it, and it still used under 1 GB of memory with all three models loaded. All 420 decisions
matched the ones `recipe.py` made in its own process, row for row:

| | Zero-shot | Fine-tuned |
| --- | --- | --- |
| Banking77, public | 67.3% (101 of 150), 22 s | 93.3% (140 of 150), 12 s |
| Parsed-document quality, private | 30.0% (18 of 60), 7 s | 86.7% (52 of 60), 7 s |

The times include loading each model on its first call. With SIE Serverless, `uv run serve.py --zero-shot-only`
scores the catalog model; your fine-tuned weights need SIE Local or SIE Cluster, where you control the configs.

## Potential improvements

- **More rows for the labels that miss.** The per-label accuracy in `results.json` says where to spend labelling
  time.
- **Train a LoRA adapter instead of the whole model.** SIE can load GLiClass LoRA adapters at request time, so one
  base model can serve a different adapter per team or per pipeline.
- **Use the score, not only the top label.** A low top score is a good reason to send a row to a person.

## Try another model

`knowledgator/gliclass-base-v3.0` (746 MB) is a larger GLiClass model. Change `model:` in `rules.yaml`; nothing
else changes. We ran the edge model for this README.

## Run it using SIE Cluster or SIE Local

`recipe.py` trains and scores in its own process. `serve.py` sends the same rows to SIE: set `SIE_BASE_URL` in
`.env` to your SIE Cluster or SIE Local (an SIE key is then optional), and start it with the configs in
`sie_models/` as shown in "Serve your fine-tuned model with SIE".

| `rules.yaml` key | Model | Image | Tested on |
| --- | --- | --- | --- |
| `model` | `knowledgator/gliclass-edge-v3.0` | `cuda12-default` (`cpu-default` on CPU) | `v0.11.1-cpu-default` in Docker on an Apple M-series laptop |
| `datasets.public.served_model` | `my-team/banking77-router` (your weights, `sie_models/public.yaml`) | same | same |
| `datasets.private.served_model` | `my-team/parsed-page-check` (your weights, `sie_models/private.yaml`) | same | same |
