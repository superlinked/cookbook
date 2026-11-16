# Why SIE: the serving jobs you'd otherwise build yourself

Picking a small open model for a job is the part that gets the attention. Running five or ten of them
on GPUs you can afford, through traffic spikes and crashed workers, is a separate problem, and it's
the one SIE (the Superlinked Inference Engine) exists to solve. You can solve it yourself, with one
vLLM or SGLang server per model, a load balancer and some scripts. Plenty of teams do.

The recipes in this folder are how we check whether SIE does it better. Each one takes one serving
job, does it the way you'd build it by hand, then does the same job on SIE, with the same models and
the same load, and prints the numbers. Not every comparison comes out in SIE's favour, and the README
for each one says so. If you run one on your own GPUs and get a different answer, we'd like to hear
about it.

| Job | Built by hand | With SIE | What we found |
| --- | --- | --- | --- |
| [Measure cold starts and bursts](measure_cold_starts_and_bursts) | Our own Kubernetes cluster: 5 to 9 minutes to start a model on a new GPU node | SIE Serverless: a small reranker answered a median of 85 s after a cold call; bursts of 10,000 calls had no errors | SIE wins for spiky traffic. Large models still take 2 to 3 minutes to start. |
| [Two SGLang models on one GPU](share_one_gpu_between_two_sglang_models) | SGLang at default settings: the second server runs out of memory; fractions set by hand took two tries | Both start on the first try, with memory sized from the model files | SIE wins on effort. Throughput is the same either way, about 60% each, because the models share the GPU's compute. |
| [Five models on fewer GPUs](pack_five_models_on_one_gpu) | One GPU per model | All five on one or two GPUs | SIE wins at light load only. At medium load, the slowest 5% of turns took 1.8 times as long on two GPUs. |
| [More models than fit](serve_more_models_than_fit) | | Sixteen models on two GPUs, with unloading and pinning | Shows SIE's limits: about 1,300 unloads an hour per GPU once the models don't fit. |
| [Kill a worker under load](kill_a_worker_under_load) | Round-robin with health checks: 42 failed requests | SIE's queue: 0 lost of 115,337 | SIE wins, at the cost of up to 15 s of waiting after each kill. |
| [Find when a GPU pays off](find_when_a_gpu_pays_off) | An L4 left on all month | Paying per call on SIE Serverless | It depends: paying per call is cheaper below about 11% busy. |

The numbers in this table come from our runs in November 2026. Each recipe's README says what
hardware it ran on and how to run it yourself.
