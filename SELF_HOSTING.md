# Run the recipes on your own SIE

Every recipe calls SIE through `SIEClient`, so the same code runs against SIE Cloud or an SIE you
host. Set `SIE_BASE_URL` in `.env` to your server and run the recipe as usual. The API key is
optional then; set `SIE_API_KEY` only if your gateway checks a token.

```bash
# .env at the repo root
SIE_BASE_URL=http://localhost:8080
```

[SIE](https://github.com/superlinked/sie) is open source (Apache 2.0). The model names in each
`rules.yaml` are SIE model ids, the same on SIE Cloud and on your own server. The image tags below
are from SIE v0.10.1. SIE's
[compatibility matrix](https://github.com/superlinked/sie/blob/main/docs/compatibility.md) is the
source for which image serves which model and on which GPU it was smoke-tested.

## Which images a recipe needs

Each SIE image serves one bundle: a set of model families whose dependencies fit together. One
SIE server runs one image. Each recipe README has a "Run it on your own SIE" table with the image
for every model it calls. Grouped:

| Images | Recipes |
| --- | --- |
| `cuda12-default` | [threat_relations](security/threat_relations), [threat_report_tagging](security/threat_report_tagging), [redact_logs_before_sharing](sre/redact_logs_before_sharing) |
| `cuda12-transformers5` | [check_parts_in_assembly_photos](manufacturing/check_parts_in_assembly_photos), [route_alerts_to_owners](sre/route_alerts_to_owners) |
| `cuda13-sglang-cu130` | [draft_postmortem_from_incident_data](sre/draft_postmortem_from_incident_data) |
| `cuda12-default` + `cuda12-sglang-vision-extract` | [check_delivery_against_po](manufacturing/check_delivery_against_po) |
| `cuda12-sglang` + `cuda12-sglang-vision-extract` | [screen_assistant_prompts](security/screen_assistant_prompts) |
| `cuda12-default` + `cuda12-transformers5` | [fill_work_order_from_notes](manufacturing/fill_work_order_from_notes), [route_customer_emails](retail/route_customer_emails), [triage_reported_phishing](security/triage_reported_phishing) |
| `cuda12-default` + `cuda13-sglang-cu130` | [answer_from_equipment_manual](manufacturing/answer_from_equipment_manual), [match_alerts_to_runbooks](sre/match_alerts_to_runbooks) |
| `cuda12-sglang-vision-extract` + `cuda13-sglang-cu130` | [check_returned_item_against_sale](retail/check_returned_item_against_sale) |
| `cuda12-default` + `cuda12-sglang-vision-extract` + `cuda13-sglang-cu130` | [check_shelf_from_photo](retail/check_shelf_from_photo) |

Every model these recipes use passed SIE's smoke test on an NVIDIA L4 (24 GB), except where a
note below says otherwise.

- **`Qwen/Qwen3-Embedding-4B` has no published image in v0.10.1.** Its default profile needs the
  `sglang-embedding` bundle, which SIE does not publish. Where a recipe uses it, set
  `Qwen/Qwen3-Embedding-8B` in `rules.yaml` instead; it runs on `cuda12-default`. The two models
  have different vectors, so similarity thresholds may need a new check.
- **`Qwen/Qwen3.8-27B-FP8` needs a big GPU.** SIE smoke-tested it on an H100 80GB, an H200 and an
  RTX PRO 6000. The `cuda13` image also needs NVIDIA driver 580 or newer.

## One image: one container

A recipe whose models all share one image runs against a single container. You need Docker, the
NVIDIA Container Toolkit and a GPU that runs your models (see above).

```bash
docker run --gpus all -p 8080:8080 \
  -v sie-hf-cache:/app/.cache/huggingface \
  ghcr.io/superlinked/sie-server:v0.10.1-cuda12-default

# in a second terminal
curl http://localhost:8080/readyz      # expect: ok
cd security/threat_report_tagging      # a recipe in the cuda12-default row
uv run recipe.py                       # with SIE_BASE_URL=http://localhost:8080 in .env
```

The server downloads each model from Hugging Face the first time a recipe calls it and keeps it
in the `sie-hf-cache` volume, so the first run is slow and later runs are not.

The `cuda12-default` lane also has a CPU image, `v0.10.1-cpu-default`. SIE declares the same models
for it but has not smoke-tested them there. It works for trying a small recipe on a laptop, but it
is slow.

## Several images: a cluster

A recipe that calls models from two or more images needs one SIE endpoint that routes each model
to a server running the right image. That is what the
[sie-cluster Helm chart](https://github.com/superlinked/sie/tree/main/deploy/helm/sie-cluster)
does. A gateway takes every request and sends it to a worker pool per image, and each pool can
scale to zero. This values file turns on a pool for each SIE image:

```yaml
# values-cookbook.yaml
workers:
  pools:
    l4:                      # NVIDIA L4 nodes
      enabled: true
      bundles:
        default: {minReplicas: 0, maxReplicas: 2}
        transformers5: {minReplicas: 0, maxReplicas: 1}
        sglang: {minReplicas: 0, maxReplicas: 1}
        sglang-vision-extract: {minReplicas: 0, maxReplicas: 1}
    sglang-cu130:            # H100 80GB, H200 or RTX PRO 6000 nodes, for Qwen3.8-27B-FP8
      enabled: true
      gpu: {count: 1, product: NVIDIA-H100-80GB-HBM3}   # your nodes' GPU label
      bundles:
        sglang-cu130: {minReplicas: 0, maxReplicas: 1}
```

```bash
helm install sie-cluster oci://ghcr.io/superlinked/charts/sie-cluster --version 0.10.1 \
  --namespace sie --create-namespace -f values-cookbook.yaml
```

Leave out the pools that your recipes do not need. The chart README covers node selectors per
cloud (`values-aws.yaml`, `values-gke.yaml`, `values-aks.yaml`), the gateway token, ingress and
TLS, and the payload store that requests over 1 MB need. Point `SIE_BASE_URL` at the gateway and
set `SIE_API_KEY` to the gateway token if you turned one on.

With scale-to-zero, the first call to each model waits for a node and a worker to start. The
recipes allow 15 minutes for that (`WAIT_S` in `recipe.py`).

## More

- [SIE deployment docs](https://superlinked.com/docs/deployment/?utm_source=github&utm_medium=docs&utm_campaign=sie-cookbook&utm_content=self-hosting)
- [SIE model catalog](https://github.com/superlinked/sie/tree/main/packages/sie_server/models):
  one YAML per model, with its profiles and the bundle each profile needs
- [MODELS.md](MODELS.md): every model these recipes use, with its license
