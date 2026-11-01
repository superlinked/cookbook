# Map how threats relate across reports

**Problem.** Your threat-intelligence team reads reports from several vendors and keeps a graph
of who does what: which group uses which malware, which malware exploits which vulnerability,
who targets whom, and which actor a malware family is attributed to. Every vendor has its own
name for the same group ("Gorse Moth" is "Velvet Gorse"). Reports also disagree (one says
Murkbell belongs to Velvet Gorse, another says Saltmarsh Kettle), and a single blog post can add
an edge nobody else has seen. Your analyst needs each edge with the sentences behind it, and
needs to see which edges rest on one publisher and which ones contradict each other.

**Input.**

- [`data/reports.jsonl`](./data/reports.jsonl) holds five short reports from three fictional
  publishers (Brindle Point Security, Dunmore Intel, Northfold CERT), one JSON object per line
  with `id`, `publisher`, `published` (the date), `title` and `text`.
- [`rules.yaml`](./rules.yaml) holds the model ID, the entity and relation labels, the alias
  table, which entity types each relation may join, the negation words, the generic words and
  the thresholds.

**Pipeline.**

| Step | SIE call | Model | Why |
|---|---|---|---|
| Split | Python | | One window per paragraph (split on sentences above 200 words, and a longer sentence on words). A relation is read inside one window |
| Entities + relations | `extract` + `labels` + `options.relation_labels` | `knowledgator/gliner-relex-large-v1.0` | threat actor, malware, vulnerability, sector, software, campaign; uses, exploits, targets, attributed to, alias of |
| Names | Python | | Merge names by the alias table, which also sets the type; CVE IDs are upper-cased; generic words ("operators") and vulnerabilities that are not a CVE ID or a table entry are dropped |
| Types | Python | | Each relation must join the types in `rules.yaml`. One that fits only the other way round is turned round (and flagged); one that fits neither way is dropped with a reason |
| Negation | Python | | Any negation word in the claim's sentence ("not", "n't", "never", "no longer", "no evidence", ...) sends the claim to review. It never becomes an edge, and the recipe never records a denial on its own |
| Graph | Python | | One edge per (head, relation, tail), carrying every source: report, publisher, date, sentence, score |
| Flags | Python | | `single_source`: fewer than 2 publishers (two reports from one publisher count once). `conflicting`: a family attributed to two actors. `review`: an end that is not in the alias table, or a report that states the claim in a sentence with a negation word. `provisional`: it would be corroborated, but a report was partly unread. A proposed `alias of` is never merged; it goes to review |

One gotcha: the model's `relations` give the head and tail as plain text, with no type. The code
looks each end up in the alias table, and for a name the table doesn't know, it uses the label
the model gave that same text in the same window. So an unknown name can get a different type
in another window; its edges then carry a "typed as A and B in different claims" flag. Unknown
names are matched like table names (case, a leading "the" and a possessive don't count), so
"Ashen Lark", "ASHEN LARK" and "Ashen Lark's" are one node, named by the first spelling the run
meets. The relations also carry no offsets, so when a name occurs twice the negation check reads
every sentence that holds both names, and a negation word in any of them sends the claim to
review. When no sentence holds both, it reads every sentence from one name to the other. A
sentence here ends at `.`, `!` or `?` followed by a space, so "e.g. " ends one too, and a
negation word before such a break isn't read with the claim ("no evidence, e.g. in logs, that
X used Y" stays an edge). An entity span whose text doesn't match the window at its offsets is dropped with a
note in the review list.

**Output.**

- `graph.json` has the nodes (name, type, known, mentions), the edges (status, flags, sources
  with the sentence), the dropped relations with the reason, the review list, and `unread`: the reports with a paragraph SIE didn't read. Every edge from such a report gets a
  "report R-0x partly unread" flag. The unread paragraph might also contradict or question an
  edge from another report, so every edge that would be corroborated gets status `provisional`
  instead, with a flag naming the unread reports, and the run prints a warning that edges may
  be missing and that statuses elsewhere may be wrong too. There's no `denials` list any more
  (earlier versions had one): a claim with a negation word is a review item that quotes its
  sentence.
- `graph.mmd` is a Mermaid flowchart. Solid arrows are corroborated, dashed arrows are every
  other status (provisional included), red arrows are conflicting or need review, and `?` marks
  a name not in the table.
- `edges.csv` has one row per edge. A cell that starts with `=`, `+`, `-`, `@`, a tab or a
  carriage return gets a leading `'`, so a spreadsheet won't run it as a formula.

```
head,relation,tail,status,publishers,reports,flags
Velvet Gorse,uses,Thornpipe,corroborated,Brindle Point Security;Dunmore Intel,R-01;R-02,
Murkbell,attributed to,Velvet Gorse,conflicting,Dunmore Intel,R-02,other reports say attributed to Saltmarsh Kettle; only 1 publisher(s): Dunmore Intel
Saltmarsh Kettle,uses,Quillstep,single_source,Northfold CERT,R-03;R-04,only 1 publisher(s): Northfold CERT
```

**Why SIE.** One model reads the entities and the relations between them in a single call.
Without it you'd run an NER model (named entity recognition) plus a relation classifier, or an
LLM per sentence. The labels and relation types are plain words in `rules.yaml`. Merging names,
checking types and setting flags is Python you can read, so every edge in the graph comes with a
reason a person can check. The five reports go to SIE Cloud in one batched call, and you host
nothing.

## Run it

You need [uv](https://docs.astral.sh/uv/) and an SIE API key
([create one](https://console.superlinked.com/console/api-keys)).

```bash
cp .env.example .env   # then set SIE_API_KEY
uv run recipe.py
```

`uv run` installs this recipe's few dependencies into its own `.venv` on the first run. Each run
writes `runs/<timestamp>/` in this folder. If you'd rather write somewhere else, pass
`--output DIR`. The first call can wait a few minutes while SIE Cloud starts the relation
extraction model.

## The planted cases

The reports, publishers, actors, malware, products and the CVE ID are all made up for this
recipe. `CVE-2099-41877` uses the year 2099 so it can't be a real CVE (not yet, anyway). The
reports contain no indicators, URLs or code.

| Case | Planted | Result on our live run |
|---|---|---|
| E1 | Velvet Gorse uses Thornpipe; Dunmore writes "Gorse Moth" | **corroborated**: the alias merges, 2 publishers |
| E2 | Thornpipe exploits CVE-2099-41877 (Brindle Point, Northfold) | **corroborated** |
| E3 | Velvet Gorse targets water utilities, one report | **single_source** |
| E4 | Murkbell attributed to Velvet Gorse (Dunmore) and to Saltmarsh Kettle (Northfold) | both edges **conflicting** |
| E5 | Saltmarsh Kettle uses Quillstep in two Northfold reports | **single_source**: one publisher counts once |
| E6 | Near miss: "We found no evidence that Saltmarsh Kettle used Thornpipe." | the model returned the relation (score 0.92); the negation word sends it to **review**, with no edge. It's the only sentence in the reports with a negation word |
| E7 | Ashen Lark, a name not in the table, uses Quillstep | **review** |
| E8 | Saltmarsh Kettle targets port operators; Dunmore writes "Reedcutter" | **corroborated** |

We ran it again live on 1 Nov 2026 with the current negation rule: E6 went to review, the other
seven cases came out as before, and the run gave 12 nodes, 11 edges and 9 review items.

The run also produced edges nobody planted. "Thornpipe targets Halvard Gateway" came out
corroborated, which is a fair reading of the text. "Thornpipe targets freight broker" and
"Quillstep targets regional hospital network" went to review, because neither target is in the
table. The model also returned relations such as "Murkbell uses packer" (in the type rules,
malware can't `use` anything) and "Reedcutter uses phishing wave" (a campaign isn't malware).
Both were dropped, and `graph.json` lists them with the reason.

**Two rules were added after the first live run.** That run returned "Operators uses Halvard
Gateway" (from "Operators should patch..."), plus "Murkbell exploits code overlap", "...
shared infrastructure" and "Velvet Gorse exploits outbound connections", scored 0.84 to 0.98.
Apparently "code overlap" is a vulnerability now. The planted edges scored 0.88 to 1.0, so no
score threshold could separate the two groups. Instead we added two rules to `rules.yaml`:
`not_entities` (generic words that are never nodes) and `listed_types_only` (a vulnerability must
be a CVE ID or a table entry). Both were fitted to this demo data, and no threshold changed. The
results above come from a later live run with the final rules, and the planted cases matched the
first run.

Treat the planted cases as a demo of how the recipe behaves. They say nothing about accuracy.

## What this recipe covers, and what it doesn't

Deciding which claim a "not" belongs to, and whether it denies the claim or only limits it, has
no natural end. So the recipe doesn't try: it never records a denial by itself, and any sentence
with a negation word goes to an analyst.

- Every claim whose sentence has a negation word goes to review, not to the graph. That covers
  plain denials ("we found no evidence that X used Y", "X did not deploy Y"), hedges ("has not
  been observed using", "no longer uses"), limits that still mean yes ("did not deploy Thornpipe
  until March 2024", "is not linked to any group other than Velvet Gorse") and a "not" about
  something else in the same sentence ("used Thornpipe but did not exploit CVE-2099-41877"). If
  other reports make the same edge, it gets status `review` with the sentence in its flags.
- That means some true claims wait for a person. On reports that hedge a lot, expect a longer
  review list.
- It doesn't catch a denial phrased without a negation word ("Velvet Gorse stopped using
  Thornpipe", "the Thornpipe claim was retracted"). The model's relation then stands as an edge.
- Names in different sentences ("Saltmarsh Kettle is active. It has never used Thornpipe.") go to
  review when any sentence from one name to the other has a negation word.

`selfcheck.py` holds every sentence above that names two entities, with the outcome the rule
gives it.

## What to change

- The alias table is `aliases` in `rules.yaml`: the canonical name, its type and its aliases.
  In practice you'd export it from your threat-intel platform. The recipe refuses to run if one
  name points to two different entries.
- The labels and relation rules are `entity_labels`, plus `relations` with the allowed head and
  tail types. `single_valued` lists the relations that may have only one tail per head.
- Negation words live in `doubt_words`, one regex. If your reports deny a link in words ours
  don't (e.g. "stopped using", "retracted"), add them there so those claims go to review too.
  Run `uv run --frozen python selfcheck.py` after a change.
- Generic words live in `not_entities`: if your reports use one ours don't (e.g. "intruders"
  where ours say "attackers"), add it there.
- `listed_types_only` names the types whose names must be a CVE ID or an alias-table entry.
- The thresholds are `entity_threshold`, `relation_threshold` (the model card suggests 0.7 to
  0.9), `min_sources`, `max_words_per_window` and `batch_size`. Treat them as starting points and
  check them on your own reports.
- The model is `models` in `rules.yaml`.

## Try another model

Each model is one line under `models:` in `rules.yaml`. As of 31 Oct 2026, SIE Cloud has no
other relation extraction model for `relations`, so there's nothing to swap in here yet.
[MODELS.md](../../MODELS.md) lists every model these recipes use and links SIE's full catalog.

## Run it on your own SIE

The same code runs against an SIE server you host. Set `SIE_BASE_URL` in `.env`, and the API
key becomes optional. The table gives the SIE v0.10.1 image each model runs on, from SIE's
[compatibility matrix](https://github.com/superlinked/sie/blob/main/docs/compatibility.md).

| `rules.yaml` key | Model | Image | Tested on |
| --- | --- | --- | --- |
| `relations` | `knowledgator/gliner-relex-large-v1.0` | `cuda12-default` | L4 |

One container serves it:

```bash
docker run --gpus all -p 8080:8080 -v sie-hf-cache:/app/.cache/huggingface \
  ghcr.io/superlinked/sie-server:v0.10.1-cuda12-default
# then set SIE_BASE_URL=http://localhost:8080 in .env and run: uv run recipe.py
```

## Files

- `recipe.py`: the pipeline
- `selfcheck.py`: offline checks of the negation rule, name matching, the edge statuses and
  dropped relations from `map_relations` and `build_graph`, Mermaid and CSV output and window
  splitting. It uses hand-written sentences, the committed reports and hand-written stand-in
  model replies; it never calls SIE
- `rules.yaml`: labels, relation rules, alias table, negation words, thresholds and the model ID
- `data/reports.jsonl`: the synthetic reports
- `pyproject.toml`, `uv.lock`: this recipe's dependencies

See the [security](https://superlinked.com/security) industry page and the SIE
[information extraction](https://superlinked.com/information-extraction) task page.
