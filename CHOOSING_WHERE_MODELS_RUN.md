# Choosing between SIE Serverless, SIE Cluster and SIE Local

You've got a recipe that calls an embedder, a reranker and an LLM, the code runs on your machine,
and now you have to decide where the models run: SIE Serverless, SIE Cluster in your own cloud, or
SIE Local on your own machine. If you already serve an LLM with SGLang or vLLM on your own GPUs,
you're really comparing four things, the three SIE options and what you have today.

This page helps you pick. I put the three side by side first, then start from your situation, then
cover what you agree to when several models share one GPU, and end with the questions teams ask
before they switch. Facts about SIE's behavior come from the
[SIE SDK](https://github.com/superlinked/sie/blob/main/packages/sie_sdk/README.md) and
[SIE server](https://github.com/superlinked/sie/blob/main/packages/sie_server/README.md) docs. The
cookbook's own numbers all come from SIE Serverless; we haven't run the recipes using SIE Cluster or
SIE Local yet (see [What we haven't checked yet](#what-we-havent-checked-yet)).

## Moving between them is one line

`SIEClient` picks its server in this order: the `base_url` you pass, then `SIE_BASE_URL`, then SIE
Serverless when it finds an API key. The model ids in each `rules.yaml` are the same on all three,
so moving a recipe from SIE Serverless to SIE Local is one line in `.env`:

```bash
SIE_BASE_URL=http://localhost:8080
```

SIE is open source (Apache 2.0), so SIE Cluster and SIE Local run the same server that SIE
Serverless runs. You can start using one and move later without rewriting anything. Easy peasy.

## The three side by side

| | SIE Serverless | SIE Cluster | SIE Local |
| --- | --- | --- | --- |
| Who runs the GPU | Superlinked | You, in your cloud, with Helm | You, on your machine |
| What you pay for | Each call (see [SIE Serverless](https://superlinked.com/serverless?utm_source=github&utm_medium=docs&utm_campaign=sie-cookbook&utm_content=choosing-where-models-run)) | GPU hours, busy or idle | Hardware you already have |
| First call to an unused model | Can take minutes | Minutes if the pool scaled to zero; you decide what stays loaded | Slow once, while the model loads |
| Which models you can use | Every model in [MODELS.md](MODELS.md) | What your GPUs hold | What your machine holds; see below for Apple Silicon |
| What you can see and tune | Error codes, retries, timeouts and timing headers | Metrics, eviction and out-of-memory settings | Same as SIE Cluster, on one machine |
| Where your data goes | To Superlinked, and possibly partner capacity | Stays in your cloud | Stays on your machine |
| Versions | Whatever Superlinked has deployed | The image tags you pin (v0.10.1 in [SELF_HOSTING.md](SELF_HOSTING.md)) | The latest `sie-server` from pip |
| Setup | An [API key](https://console.superlinked.com/console/keys?utm_source=github&utm_medium=docs&utm_campaign=sie-cookbook&utm_content=choosing-where-models-run) | Helm, GPU nodes, one worker pool per SIE image | `pip install "sie-server[local]"` |

## Start from your situation

### Use SIE Serverless

#### You have no GPUs, or none to spare

You find out whether the models do the job before anyone sizes a GPU, and a recipe that needs four
SIE images is still one API key.

#### You're still choosing models

Each model is one line in `rules.yaml`, and [MODELS.md](MODELS.md) lists the swaps we ran. Trying
five rerankers on your own hardware means finding room for five rerankers.

#### Your traffic is spiky, or a batch job runs a few times a day

SIE Serverless fits, as long as you can wait out a cold start: the first call to a model nobody's
using can take minutes while SIE loads it. SIE Cluster can do the same, since each worker pool can
scale to zero, and then you pay the same cold start.

#### You need a model bigger than your GPUs

`Qwen/Qwen3.8-27B-FP8` needs an H100 80GB or larger. Call it using SIE Serverless and run the rest
wherever you like, or have your SIE Cluster pass that one model through to SIE Serverless (see
[Mixing them](#mixing-them)).

### Use SIE Cluster

#### Your data can't leave your cloud

SIE Serverless sends your inputs to Superlinked, and the SDK docs say it can serve some models on
partner capacity through SIE Relay (it answers `403 RelayUnavailableError` when it won't). If your
rules allow only your own cloud, that settles it.

#### You have a few L4s or A100s and already run SGLang for an LLM

Keep SGLang for the LLM and run SIE Cluster for the small models around it: the embedder, reranker,
extractors and OCR. An LLM wants its spare GPU memory for its KV cache (the attention state it
keeps for each request), and small models packed beside it compete for that memory. Every small
model these recipes use passed SIE's smoke test on an NVIDIA L4 (24 GB), so the small models can
share a card of their own. SIE can also pass LLM calls through to your SGLang endpoint under the
same model name (see [We already run SGLang](#we-already-run-sglang-why-add-sie)).

#### Your traffic keeps a GPU busy most of the day

Price SIE Cluster against SIE Serverless. With SIE Serverless you pay per call. With SIE Cluster you
pay for GPU hours whether the GPU is busy or not, so the busier it is, the better SIE Cluster looks.
Where the two cross depends on SIE Serverless pricing and your traffic, so run your own numbers.

### Use SIE Local

#### You're developing, or checking that answers match

On Apple Silicon, SIE Local serves embeddings and reranking, with generation in a separate process,
so a recipe that needs several SIE images may not run on one SIE Local server.

**Pro-tip:** run a recipe using SIE Serverless, run it again using SIE Local, and compare the files
in `runs/`. It's the cheapest way to find out whether the answers match before you trust either.

### Mixing them

An SIE server can serve a model through another SIE deployment or through an OpenAI-compatible
endpoint, and your code still calls it by the same model name. This is on SIE's `main` branch
([remote backends](https://github.com/superlinked/sie/blob/main/packages/sie_server/REMOTE_BACKENDS.md));
check that the release you deploy includes it.

## What sharing a GPU costs you

SIE saves you GPU money by putting several models on one GPU. The catch is that those models
compete for its memory, and I'd rather you know what that looks like before you choose than find
out from a latency graph.

### A busy model can push an idle one off the GPU

When memory runs short in the middle of a request, SIE first clears its cache, then unloads the
least recently used model, then splits the batch in half, down to 16 pieces. Separately, it unloads
least recently used models once the GPU is 95% full. So if your LLM gets a burst of long prompts
while your reranker sits idle for a minute, the reranker may be unloaded, and its next call waits
while it loads again. Your reranker didn't do anything wrong; it just wasn't busy at the wrong
moment.

### When memory runs out, your call gets slower before it fails

| What happens on the server | What your call gets |
| --- | --- |
| SIE frees enough memory by itself | 200, 1 to 3 seconds slower |
| It doesn't, and an SDK retry succeeds | 200, 5 to 35 seconds slower |
| Three retries fail | `ResourceExhaustedError` |

### Some memory isn't counted against the model using it

CUDA graphs (recorded GPU work that SIE replays to run small models faster) can take up to 4% of
the GPU, about 900 MB on an L4. That memory isn't charged to the model that recorded the graphs, so
when SIE picks a model to unload, it may pick a different one.

### What you can do about it depends on where the models run

Using SIE Serverless you see the extra latency but not the cause, and you can only change your side
(e.g. smaller batches, a time budget with `provision_timeout_s`, and a fallback to a smaller model
with `max_oom_retries=0`). Using SIE Cluster or SIE Local you can also see the eviction metrics and
change the settings (`SIE_MEMORY_PRESSURE_THRESHOLD_PERCENT`, `SIE_IDLE_EVICT_S` and the
`SIE_OOM_RECOVERY__*` group).

### When to give a model its own GPU

If a model has a latency target it can never miss, I prefer giving it its own SIE server, the same
way you'd give a hot service its own node. I'd do the same for an LLM with long prompts, because its
KV cache is the thing most likely to push everything else off.

What we don't know yet is what happens to the other models after a GPU fault that isn't
out-of-memory, such as an illegal memory access. That's in the last section.

## Questions teams ask before switching

### We already run SGLang. Why add SIE?

Keep SGLang for your LLM. Most recipes here also need models SGLang isn't built for: rerankers,
GLiNER extractors, OCR, docling, speech and segmentation. SIE serves them behind one API (`encode`,
`score`, `extract`, `chat_completions`), and with remote backends it can pass LLM calls through to
your SGLang endpoint under the same model name.

### Why not MIG, run:ai or HAMi?

Those split a GPU between containers. MIG gives each slice its own memory and isolates faults in
hardware, so a slice that runs out of memory doesn't touch the others. The catch is that each
slice's size is fixed, and memory sits unused while one model is idle and another is short. SIE
shares one GPU among models and moves memory by unloading the least recently used one: better use
of the GPU, with the slowdowns described above. You can use both. Give a model that can't take
those slowdowns its own GPU or its own MIG slice, running its own SIE server.

### Why not dynamic LoRA adapters on one base model?

LoRA works when your models are versions of one base LLM. A reranker, an OCR model and a GLiNER
extractor are different architectures, so they can't be adapters of anything. SIE serves LoRA
adapters too, so you can use both.

### Are we locked in?

SIE is Apache 2.0, the model ids are the same on all three, and moving is one line in `.env`. Don't
take my word for it: run one recipe using SIE Serverless and again using SIE Local, and compare the
outputs.

## What we haven't checked yet

We'll fill these in as we run them. Until then, treat the parts of this page that depend on them as
what the SIE docs say, not what we measured.

- We haven't run the recipes using SIE Cluster or SIE Local, so nobody has compared outputs across
  the three.
- Whether SIE Serverless puts different customers' models on one GPU, and so whether someone else's
  burst can unload your model. To confirm with the SIE team.
- Whether models in one SIE image share one process and CUDA context, and what happens to the
  others after a GPU fault that isn't out-of-memory (an Xid error or an illegal memory access).
  To confirm with the SIE team.
- SIE Serverless pricing, for the point where GPU hours cost less than calls.
