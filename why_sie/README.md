# Why SIE: the serving jobs you'd otherwise build yourself

Picking a small open model for a job is the part that gets the attention. Running five or ten of them
on GPUs you can afford, through traffic spikes and crashed workers, is a separate problem, and it's
the one SIE (the Superlinked Inference Engine) exists to solve. You can solve it yourself, with one
vLLM or SGLang server per model, a load balancer and some scripts. Plenty of teams do.

The recipes in this folder are how we check whether SIE does it better. Each one takes one serving
job and compares SIE with what you'd do without it: the same job built by hand where that's practical
to run (plain SGLang, a round-robin in front of the servers), or else a stated baseline (one server per
model, a cluster we measured before, a GPU rented all month). It runs live and prints the numbers. Not
every comparison comes out in SIE's favour, and the README for each one says so. If you run one on your
own GPUs and get a different answer, we'd like to hear about it.

All but the cold-start recipe start their own servers, so they need one to four 24 GB GPUs and Docker
(or an environment with SIE installed). Only the cold-start recipe uses SIE Serverless.

| Job | Compared with | With SIE | What we found |
| --- | --- | --- | --- |
| [Measure cold starts and bursts](measure_cold_starts_and_bursts) | Our own Kubernetes cluster, measured in May 2026: 5 to 9 minutes to start a model on a new GPU node | SIE Serverless: a cold reranker answered after 81.5 s; 2,000 calls from 32 callers had no errors, 95% within 1.4 s | SIE wins for spiky traffic. Large models still take 2 to 3 minutes to start. |
| [Two SGLang models on one GPU](share_one_gpu_between_two_sglang_models) | Plain SGLang: at its defaults the second server runs out of memory; with memory set by hand the embedder kept 64% of its throughput and the guard 47% | Both started on the first try, memory sized from the model files; they kept 66% and 64% | SIE wins on effort. Neither setup kept the 80% we hoped for, because the two models share the GPU's compute. |
| [Five models on fewer GPUs](pack_five_models_on_one_gpu) | One SIE server per model, one GPU each | All five on two GPUs, or on one | Packing missed the 1.25 bar at every load in this run. At medium load the slowest 5% of turns took 2.1 times as long on two GPUs and 3.2 times on one; the reranker slows the models that share its card. |
| [More models than fit](serve_more_models_than_fit) | Ten models that fit, on the same server | Sixteen models on one GPU, with unloading and one model pinned | Shows SIE's limits: about 1,740 unloads an hour once the models don't fit, and a median 10.5 s wait when a request's model had to load. The pinned model never reloaded, but its slowest 1% of requests went from 34 ms to 2.1 s. |
| [Kill a worker under load](kill_a_worker_under_load) | A round-robin with health checks: 26 failed requests over three worker kills | SIE's queue: 0 lost of 38,440 | SIE wins on lost requests. It costs about 8% of capacity, and the slowest request waited 30 s after a kill. |
| [Find when a GPU pays off](find_when_a_gpu_pays_off) | An L4 rented all month | Paying per call on SIE Serverless | It depends: paying per call is cheaper below about 11% busy. |

Unless a row says otherwise, the numbers come from each recipe's own run in November 2026. Each
recipe's README says what hardware it ran on, how it compares with our longer runs, and how to run it
yourself.
