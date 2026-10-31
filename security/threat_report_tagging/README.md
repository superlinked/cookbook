# Tag actors, malware and tools in threat reports

**Problem.** Your CTI team files every vendor and partner report into its threat-intel
platform, tagged with the actors, malware, tools and indicators the report covers. The trouble
is that nobody agrees on names. One report says "LX-0417", another says "Ashen Heron", a third
says "Grey Lantern", and two vendors use that last name for two different groups. Indicators
show up defanged in several styles, and now and then live or truncated. If you merge two names
wrongly, you've pinned the activity on the wrong actor.

**Input.**

- [`data/reports.jsonl`](./data/reports.jsonl) holds eight short synthetic reports, one per line.
- [`data/attack_techniques.csv`](./data/attack_techniques.csv) holds nine MITRE ATT&CK
  Enterprise techniques (ID, name, tactic), just the subset the tool mapping uses.
- [`rules.yaml`](./rules.yaml) holds the labels, the alias table, the tool-to-technique mapping,
  the review policy, the thresholds and the model ID.

**Pipeline.**

| Step | SIE call | Model | Why |
|---|---|---|---|
| Indicators | Python | | CVE IDs, MD5/SHA-1/SHA-256 by length, defanged URLs, domains and IPs by regex; live URLs, malformed hashes and CVE IDs flagged |
| Split | Python | | Sentences packed into windows of at most 150 words (NuNER Zero reads 384 tokens) |
| Tag | `extract` + `labels` | `numind/NuNER_Zero` | `threat actor`, `malware` and `tool` spans, zero-shot |
| Check spans | Python | | Each span must equal the text at its offsets; spans on an indicator, or that are not names ("MD5", "threat group"), are dropped |
| Fill misses | Python | | Alias-table names and mapped tools written in the text, matched exactly |
| Normalise | Python | | Every mention through the alias table to one canonical name; tools to ATT&CK technique IDs |
| Escalate | Python | | Unknown actor or malware, an alias listed under two names, a tool with no mapping, a live URL or a malformed indicator: the report goes to review with the reason |

**Why NuNER Zero and not gliner2-large-v1.** The job is plain span tagging with three labels.
There's no structure to fill, so `output_schema` adds nothing. When NuNER Zero finds nothing, it
returns `entities: []`. gliner2-large-v1 with `output_schema` answers the same empty result
with an HTTP 500, and that fails the whole batch (see
[`healthcare/referral_intake`](../../healthcare/referral_intake)). NuNER Zero's labels are plain
words in `rules.yaml`. The alias table already knows the familiar names, so what you really
need the model for is the names the table doesn't know yet (e.g. "Velvet Ostrich", "Chisel").
Those are exactly the ones an analyst has to see.

**Output.** `tags.json` holds every report with its canonical actors, malware and tools, its
ATT&CK techniques, every mention (offsets, score, and whether it came from the `model` or the
`table`), its indicators and its status, `tagged` or `review`. `entities.csv` has one row per
mention, and `iocs.csv` has one row per indicator, always defanged:

```
report_id,kind,mention,start,end,source,score,canonical,techniques,status,reason
R-02,actor,LX-0417,0,7,model,0.96875,Ashen Heron,,tagged,model said malware; the table lists Ashen Heron as actor
R-03,actor,Grey Lantern,63,75,model,1.0,,,review,ambiguous alias: 'Grey Lantern' is listed under Ashen Heron and Copper Moth

report_id,type,value,live_in_source,note
R-06,url,hxxp://192[.]0[.]2[.]44/q.ps1,True,written live in the report
```

When a name is in the alias table, the table decides whether it's an actor, malware or a tool,
whatever label the model gave it. In R-02 above, the model called `LX-0417` malware, and the
table overruled it.

**Why SIE.** There's one small zero-shot NER model and no LLM. The labels are plain words in
`rules.yaml`, so you can add `vulnerability` or `target sector` without training anything.
Indicators, alias merges and technique mappings are code and tables you can read, so when a
merge goes wrong, a person can see why. All windows go to SIE Cloud in one batched call, and
you host nothing.

## Run it

You need [uv](https://docs.astral.sh/uv/) and an SIE API key
([create one](https://console.superlinked.com/console/api-keys)).

```bash
cp .env.example .env   # then set SIE_API_KEY
uv run recipe.py
```

`uv run` installs this recipe's few dependencies into its own `.venv` on the first run. Each run
writes `runs/<timestamp>/` in this folder. If you'd rather write somewhere else, pass
`--output DIR`. The first call can wait a few minutes while SIE Cloud starts the NuNER Zero
model.

## The planted cases

The reports, actors, malware, victims, products and CVE IDs are synthetic, written for this
recipe. CVE IDs use the year 2099, so they can't collide with a real advisory. Hashes are
SHA-256, SHA-1 and MD5 digests of short demo strings, not of any file. Domains are RFC 2606
reserved names, and IPs are from the RFC 5737 documentation ranges. The tools (Mimikatz, PsExec,
AdFind, rclone, PowerShell, vssadmin, Chisel) are real, widely documented admin and
offensive-security tools.

| Report | Planted | Result on our live run |
|---|---|---|
| R-01 | Canonical names, a CVE, a defanged URL and IP, a SHA-256 | tagged: Ashen Heron, QuillRAT, Mimikatz and PsExec mapped to three techniques |
| R-02 | Aliases `LX-0417`, `QRAT`, `Mothvine Loader` | tagged: normalised to Ashen Heron, QuillRAT, Mothvine |
| R-03 | `Grey Lantern`, listed under two actors | **review**: ambiguous alias, both candidates named |
| R-04 | New actor `Velvet Ostrich` | **review**: not in the alias table; SaltGlass and vssadmin still tagged |
| R-05 | Tool `Chisel` with no ATT&CK mapping | **review**: no mapping. NuNER Zero also tagged the fictional product "Corvane VPN" as a tool (score 0.54), a second review reason |
| R-06 | Live `https://` and `http://` URLs, an MD5 one character short | **review**: two live URLs, defanged in the output, and a malformed hash |
| R-07 | Near miss: `build 4.2.0.118` and `mothvine_cfg.dat` look like an IP and a domain | tagged: only the defanged `cdn-sync[.]example[.]org` counts |
| R-08 | Patch summary, no threats | tagged, nothing found |

The first live run sent R-08, a patch summary with no threats in it, to review, because NuNER
Zero tagged "threat group" as an actor. Technically true, and no use to anyone. It also tagged
"MD5" in R-06 as a tool. We added `ignore_mentions` and a rule that a span made only of
`generic_words` isn't a name, then ran live again, and the results above come from that second
run. Both rules are fitted to the demo data. No threshold was changed.

Treat the planted cases as a demo that the recipe works. They say nothing about accuracy.

## What to change

Everything below lives in `rules.yaml` unless it says otherwise.

- The alias table is `actors` and `malware`: one canonical name each, with the aliases other
  vendors use. If two groups share an alias, list it under both names, and every mention of it
  goes to review. In production, export this table from your threat-intel platform.
- The tool mapping is `tool_techniques`. Every technique ID must be in
  `data/attack_techniques.csv`, or the recipe refuses to start. If you need more techniques,
  swap the CSV for the full ATT&CK Enterprise catalog from
  [attack.mitre.org](https://attack.mitre.org/).
- `policy` turns each review reason on or off. A name reason that's off (unknown actor or
  malware, ambiguous alias, unmapped tool) is recorded as `new` and doesn't send the report to
  review. An indicator reason that's off (live URL, malformed indicator) is dropped; the
  indicator row still keeps its note.
- `labels`, `generic_words` and `ignore_mentions` control what the model looks for and which of
  its spans get thrown away as not being names.
- `min_entity_score`, `max_words_per_window`, `min_hex_run` and `batch_size` are starting
  points. Check them on your own reports.
- `models` holds the model ID.

## Try another model

Each model is one line under `models:` in `rules.yaml`. We swapped one model at a time, changed
nothing else, and ran the recipe on SIE Cloud on 31 Oct 2026.

| `rules.yaml` key | Swap in | What happened on this data |
| --- | --- | --- |
| `entities` | `numind/NuNER_Zero-span` | 3 tagged, 5 to review (default: 4 and 4). R-01, R-02, R-04, R-06 and R-08 came out the same. On R-03 it tagged "PowerShell scripts" as a tool, so T1059.001 was lost and a second review reason added. On R-05 it tagged "RDP" instead of "Corvane VPN". The R-07 near miss went to review over a tool "SvcHealth". |
| `entities` | `urchade/gliner_multi-v2.1` | 2 tagged, 6 to review. Same R-03 "PowerShell scripts" problem. On R-05 it did not tag "Corvane VPN", so only Chisel went to review. It tagged "Defenders" (R-07) and "The IT team" (R-08) as actors, so both clean reports went to review. The 0.50 `min_entity_score` was set for NuNER Zero, and these spans passed it. |

See [MODELS.md](../../MODELS.md) for every model these recipes use.

## Run it on your own SIE

The same code runs against an SIE server you host. Set `SIE_BASE_URL` in `.env`, and the API
key becomes optional. The table gives the SIE v0.10.1 image each model runs on, from SIE's
[compatibility matrix](https://github.com/superlinked/sie/blob/main/docs/compatibility.md).

| `rules.yaml` key | Model | Image | Tested on |
| --- | --- | --- | --- |
| `entities` | `numind/NuNER_Zero` | `cuda12-default` | L4 |

One container serves it:

```bash
docker run --gpus all -p 8080:8080 -v sie-hf-cache:/app/.cache/huggingface \
  ghcr.io/superlinked/sie-server:v0.10.1-cuda12-default
# then set SIE_BASE_URL=http://localhost:8080 in .env and run: uv run recipe.py
```

## Data and licenses

- Reports, actor and malware names, victims, products and CVE IDs: synthetic, written for this
  recipe. Another recipe ([`security/threat_relations`](../../security/threat_relations))
  uses its own fictional names.
- `data/attack_techniques.csv`: technique IDs, names and tactics from
  [MITRE ATT&CK Enterprise](https://attack.mitre.org/). © The MITRE Corporation. This work is
  reproduced and distributed with the permission of The MITRE Corporation, under the
  [ATT&CK terms of use](https://attack.mitre.org/resources/legal-and-branding/terms-of-use/),
  which allow reuse with this attribution.

## Files

- `recipe.py`: the pipeline
- `rules.yaml`: labels, alias table, tool mapping, policy, thresholds, model ID
- `data/`: the reports and the ATT&CK subset
- `pyproject.toml`, `uv.lock`: this recipe's dependencies

See the SIE [information extraction](https://superlinked.com/information-extraction) task page
and the [security](https://superlinked.com/security) industry page.
