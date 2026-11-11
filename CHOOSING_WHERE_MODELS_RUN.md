# Choosing between SIE Serverless, SIE Cluster and SIE Local

You've run a recipe, it does the job, and now you have to decide where its models run: SIE Serverless (we run the GPUs and you pay for what each call uses), SIE Cluster (you run SIE in your own cloud with Helm), or SIE Local (SIE on your own machine). Your recipe code runs on your machine in all three cases. Only the models move.

Start with your recipe, because for most of them the recipe makes part of the choice for you. Then pick from your situation, and read what changes once you run the models yourself. The run times and call counts in each recipe README come from SIE Serverless. We haven't run the recipes using SIE Cluster or SIE Local yet (see [What we haven't checked yet](#what-we-havent-checked-yet)).

## First, check what your recipe needs

SIE packages its models into images. Each image serves a set of model families whose dependencies fit together (e.g. `cuda12-default` for most embedders, rerankers and extractors, `cuda13-sglang-cu130` for the large LLM). One SIE server runs one image. Find your recipe's row in [SELF_HOSTING.md](SELF_HOSTING.md#which-images-a-recipe-needs):

| Your recipe needs | Where it can run | Recipes |
| --- | --- | --- |
| One image, and no `cuda13-sglang-cu130` | All three. One SIE Local server or one `docker run`; each model passed SIE's smoke test on an NVIDIA L4 (24 GB) on its own, but check that your recipe's models fit together (two 4B models in BF16 take about 16 GB before they do any work) | 15 of 46 |
| `cuda13-sglang-cu130` (that's `Qwen/Qwen3.8-27B-FP8`) | SIE Serverless, or your own H100 80GB, H200 or RTX PRO 6000 with NVIDIA driver 580 or newer | 16 of 46 |
| Two or more images | SIE Serverless or SIE Cluster. One SIE Local server can't hold them | 29 of 46 |

The groups overlap: most recipes that use the large LLM also need a second image. If you're in GitHub Codespaces, there's no GPU, so it's SIE Serverless.

One exception: `Qwen/Qwen3-Embedding-4B` runs using SIE Serverless but has no published image in v0.10.1. Seven recipes use it, two of them in the one-image row above (categorize_products_into_taxonomy and flag_unusual_logs_and_investigate). To run those yourself, set `Qwen/Qwen3-Embedding-8B` in `rules.yaml` and check the similarity thresholds again, since the two models give different vectors ([SELF_HOSTING.md](SELF_HOSTING.md#which-images-a-recipe-needs) has the details).

## Moving between them is one line

The model ids in each `rules.yaml` are the same on all three, so once your recipe fits where you want it (and you've swapped `Qwen/Qwen3-Embedding-4B` if it uses it), moving is one line in the root `.env`:

```bash
SIE_BASE_URL=http://localhost:8080   # your SIE Local server or your SIE Cluster gateway
```

With `SIE_BASE_URL` set, the API key is optional; set `SIE_API_KEY` only if your server or gateway checks a token. The recipes send whatever `SIE_API_KEY` is in `.env` to that URL, so remove your SIE Serverless key when you switch, and use https for a gateway that isn't on your machine. With no `SIE_BASE_URL` and an API key, recipes use SIE Serverless. If your key is for the EU region, set `SIE_BASE_URL=https://eu.api.superlinked.com`. Easy peasy.

SIE is open source (Apache 2.0) and SIE Cluster and SIE Local serve the same model configurations and API as SIE Serverless. SIE Serverless puts its own gateway and queue in front, so you'll see slightly different timing and error behavior, but your code doesn't change.

## The three side by side

| | SIE Serverless | SIE Cluster | SIE Local |
| --- | --- | --- | --- |
| Recipes it runs | All 46 | All 46, with a worker pool per image your recipes use | The 15 single-image L4 recipes; fewer on Apple Silicon |
| Who runs the GPU | Superlinked | You, in your cloud, with Helm | You, on your machine |
| What you pay for | Each call (see [SIE Serverless](https://superlinked.com/serverless?utm_source=github&utm_medium=docs&utm_campaign=sie-cookbook&utm_content=choosing-where-models-run)) | GPU hours, busy or idle | Hardware you already have |
| First call to a model nobody's using | Can take minutes; recipes wait up to 15 minutes (`WAIT_S`) | The same if the pool scaled to zero; you decide what stays loaded | Slow once, while the model downloads and loads |
| Where your data goes | To Superlinked, and possibly partner capacity (see below) | Stays in your cloud | Stays on your machine |
| Versions | Whatever Superlinked has deployed | The image tags you pin | The `sie-server` version you install |
| Setup | An [API key](https://console.superlinked.com/console/keys?utm_source=github&utm_medium=docs&utm_campaign=sie-cookbook&utm_content=choosing-where-models-run) | Helm, GPU nodes, one worker pool per image ([SELF_HOSTING.md](SELF_HOSTING.md#several-images-sie-cluster)) | `pip install "sie-server[local]"`, then `sie-server serve` (add `--device cuda` on NVIDIA) |
| Results we've checked | Every recipe README | Not yet | Not yet |

## Start from your situation

### Use SIE Serverless

#### You have no GPUs, or none to spare

You find out whether the models do the job before anyone sizes a GPU, and a recipe that needs four SIE images is still one API key.

#### You're still choosing models

Each model is one line in `rules.yaml`, and [MODELS.md](MODELS.md) lists the swaps we ran. Trying five rerankers on your own hardware means finding room for five rerankers.

#### Your recipe runs a few times a day, or your traffic is spiky

You pay for the calls you make and nothing in between. The catch is the cold start: the first call to a model nobody's using can take minutes, so a recipe run can take several minutes longer than its README says. SIE Cluster can scale each worker pool to zero too, and then you wait the same way.

#### Your recipe uses `Qwen/Qwen3.8-27B-FP8` and you don't have an H100-class GPU

Call it using SIE Serverless and run the rest on your own GPUs (see [Mixing them](#mixing-them)). The inputs to that one model still go to Superlinked.

### Use SIE Cluster

#### Your data can't leave your cloud

SIE Serverless sends your inputs to Superlinked, and it can serve some models on partner capacity through SIE Relay. If your rules allow only your own cloud, use SIE Cluster. If your rule is only "no third parties", [ask us](mailto:support@superlinked.com) whether Relay can be turned off for your key before you rule SIE Serverless out.

#### Your recipe needs more than one image

29 of the 46 recipes do. SIE Cluster puts a gateway in front of one worker pool per image and routes each model to the right pool, so the recipe still has one `SIE_BASE_URL`.

#### Your recipes keep a GPU busy most of the day

With SIE Serverless you pay for what each call uses: tokens for text models, pages for OCR, pairs for a reranker, images, audio or GPU seconds for the rest. With SIE Cluster you pay for GPU hours whether they're busy or not, so the busier the GPU, the better SIE Cluster looks. To work out where the two cross, count what one run uses in those units (each recipe README lists its SIE calls per run, which is a good place to start), multiply by runs per day and the [price per unit](https://superlinked.com/serverless?utm_source=github&utm_medium=docs&utm_campaign=sie-cookbook&utm_content=choosing-where-models-run#pricing), and compare that with the hourly price of an L4 node (and an H100 node if you use the large LLM).

### Use SIE Local

#### You're developing, or your data can't leave your laptop

Iterating on `rules.yaml` costs nothing, and nothing leaves your machine. It works for the 15 recipes that need one image and fit an L4 (the two `cuda12-transformers5` recipes need the Transformers 5 install from the [SIE server README](https://github.com/superlinked/sie/blob/main/packages/sie_server/README.md#installation), since one Python environment holds only one `transformers` version). On Apple Silicon, the [SIE quickstart](https://superlinked.com/docs/quickstart?utm_source=github&utm_medium=docs&utm_campaign=sie-cookbook&utm_content=choosing-where-models-run) says SIE Local serves embeddings and reranking, with generation in a separate process on another port, so start with recipes that only embed and rerank. We haven't run any recipe there yet.

**Pro-tip:** run a recipe using SIE Serverless, run it again using SIE Local, and compare the files in `runs/`. Expect most of what the recipe decides to match and the scores to differ, because batch size, rounding and the GPU all move them. In SIE's own tests, scoring the same pair alone or among 20 candidates moved `Qwen/Qwen3-Reranker-4B`'s score by up to 0.019 on an H100 and up to 0.097 on an L4, and on an L4 `Qwen/Qwen3-Reranker-0.6B` picked a different top candidate in 3 of 60 questions. Compare what the recipe decides, and look twice at anything whose score sits close to a threshold in `rules.yaml`.

### Mixing them

An SIE server can serve one model through another SIE deployment, or through an OpenAI-compatible endpoint, and your code still calls it by the same model name. The usual reason is the one above: keep `Qwen/Qwen3.8-27B-FP8` on SIE Serverless and run everything else on your own L4s. This is SIE's [remote backends](https://github.com/superlinked/sie/blob/main/packages/sie_server/REMOTE_BACKENDS.md) feature, in SIE 0.10.0 and later.

## What changes when you run the models yourself

SIE saves you GPU money by putting several models on one GPU. They share its memory, so when it runs short, SIE unloads the model that was used least recently, and that model's next call waits while it loads again. That's fine for a model you tried once last week and annoying for a reranker that's called every couple of minutes.

- **Keep the models you need loaded** by pinning them with `SIE_PINNED_MODELS`. Pinned models are never unloaded to make room.
- **Models on one GPU also slow each other down**, even when nothing is unloaded, because they share its compute. If a model has a latency target, give it its own SIE server, the same way you'd give a hot service its own node.
- **Under memory pressure, a call can take up to about 35 seconds longer** while the server and the SDK retry, and then fail with `ResourceExhaustedError`. Using SIE Serverless that's all you see. Using SIE Cluster or SIE Local you can also see which model was unloaded and why in SIE's metrics, and tune it ([SIE server docs](https://github.com/superlinked/sie/blob/main/packages/sie_server/README.md#memory--oom-resilience)).

## Questions teams ask before switching

### We already run SGLang or vLLM for our LLM. Why add SIE?

Keep it for your LLM. Most recipes here also need models that sit outside an LLM server: GLiNER extractors, OCR, docling, speech and segmentation, plus the embedders and rerankers. SIE serves them behind one API (`encode`, `score`, `extract`, `chat_completions`), and with remote backends it can pass LLM calls through to your existing endpoint under the same model name.

### Are we locked in?

SIE is Apache 2.0, the model ids are the same on all three, and moving is one line in `.env` (plus the embedding swap above for the seven recipes that use `Qwen/Qwen3-Embedding-4B`). Don't take my word for it: run one recipe using SIE Serverless and again using SIE Local, and compare what it decides.

## What we haven't checked yet

We'll fill these in as we run them. Until then, treat the parts of this page that depend on them as what the SIE docs say, not what we measured.

- We haven't run the recipes using SIE Cluster or SIE Local, so nobody has compared outputs across the three.
- Whether SIE Serverless puts different customers' models on one GPU, so that someone else's traffic can unload your model.
- Whether SIE Relay can be turned off per key.
- SIE Serverless pricing, for the point where GPU hours cost less than calls.
