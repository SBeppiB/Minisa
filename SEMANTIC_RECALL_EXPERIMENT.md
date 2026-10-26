# Experiment proposal — measurable semantic recall for durable sourced memories

Status: **proposal only**. No experimental retrieval code is part of the
deployment candidate, and nothing here changes the staged release, the
production configuration, or the runtime import graph.

Track separation: the deployment candidate ships the *lexical* projection of
durable sourced memories. This experiment is a separate, later change with its
own acceptance criteria. It must not be merged into deployment work, and it
must not be mixed into the migration window.

---

## 1. The question

A later conversation can only benefit from the learning cycle if it can find
what was learned. Today the durable sourced memories are recalled by a lexical
overlap projection, and the measurement harness quantifies exactly where that
projection fails:

| Query category | retrieval hit | answered | in context | what the query does |
| --- | --- | --- | --- | --- |
| exact | 6/6 (1.000) | 6/6 | 6/6 | reuses the content words of the stored fact |
| morphological | 6/6 (1.000) | 1/6 | 6/6 | another form of the same word |
| paraphrase | 2/6 (0.333) | 0/6 | 2/6 | different words, same meaning |
| vague | 0/6 (0.000) | 0/6 | 0/6 | almost no shared content word |
| **overall** | **14/24 (0.583)** | **7/24 (0.292)** | **14/24 (0.583)** | |

Control source: `ops/measure_retrieval_quality.py` → `/tmp/minisa-retrieval-report.json`;
the floor is pinned by `tests/test_retrieval_quality.py`.

The lexical matcher is at its ceiling for the categories it was designed for.
The gap is entirely in *paraphrase* and *vague* queries.

## 2. Hypothesis

> Adding a semantic projection over the same `memory_records` rows — fused
> with the existing lexical projection, lexical first — raises the overall
> retrieval hit rate from 0.583 to at least 0.75, lifts paraphrase hits from
> 2/6 to at least 4/6, and regresses neither exact nor morphological recall.

If the hypothesis fails, the correct outcome is to keep the lexical projection
and record the negative result; the harness makes the decision measurable
rather than a matter of taste.

## 3. What already exists (no new dependency required)

| Component | Location | Role in this experiment |
| --- | --- | --- |
| lexical sourced-memory projection | `runtime/cognition/workspace.py:783` `sourced_memories()` | the control; ships in the candidate |
| embedding HTTP client | `runtime/memory/embeddings.py` `OllamaEmbeddings` | embeds text via an Ollama-compatible endpoint; bounded 30 s timeout, classified failure kinds, 30 s failure cooldown |
| semantic retriever | `runtime/memory/semantic_retriever.py` `SemanticMemoryRetriever` | existing cosine ranking, threshold 0.45, provider identity in the vector cache key |
| engine recall | `runtime/cognition/engine.py:114` | already calls `memory.recall(..., semantic=True)` and falls back to `semantic=False` on failure (line 123) |
| configuration | `runtime/server/config.py:18` `embedding_model` (default `nomic-embed-text`), env override `MINISA_EMBEDDING_MODEL` | the only switch the experiment needs |

Two facts that constrain the design:

* The engine's semantic path searches the **RP2 memory store**
  (`runtime/memory/manager.py`), *not* the `memory_records` rows of the
  learning cycle. Semantic recall over sourced memories therefore needs its own
  projection; it cannot be switched on with configuration alone.
* `runtime/cognition/engine.py:112` carries a stale comment ("Use simple
  keyword matching for NORMAL route to avoid embedding overhead") that
  contradicts the code beneath it. Fixing the comment is a separate, trivial
  change; it must not be bundled with this experiment.

**No new third-party dependency is proposed.** `sentence-transformers` and
every other local embedding stack stay out of scope: the existing HTTP client
is sufficient, and a local model stack could not be exercised or measured on
this host.

## 4. Blocking prerequisites (verified 2026-10-21, read-only)

| ID | Prerequisite | Status |
| --- | --- | --- |
| E1 | A reachable Ollama-compatible endpoint exposing an **embedding-capable** model | **NOT MET.** Local `http://127.0.0.1:11434` serves only `qwen3.5:2b-q4_K_M` (capabilities: completion, vision, tools, thinking — not embeddings). The configured remote `http://100.123.208.117:11434` does not answer from this host: `curl /api/tags` times out, ICMP unreachable. |
| E2 | Administrator approval to obtain an embedding model (e.g. `ollama pull nomic-embed-text`, ~0.3 GB into `/usr/local/lib/ollama` on `/srv`, 192 G free) | **NOT REQUESTED.** Pulling a model is a download decision, not an engineering one. |
| E3 | Production keeps `recall_backend=local` and sets no embedding override until the experiment passes | **MET and to be preserved.** `/srv/minisa/config/minisa.toml` has no `embedding_model`; `/srv/minisa/config/minisa.env` has only `MINISA_OLLAMA_URL`. |

While E1 is unmet the experiment is **BLOCKED**. It must not be approximated
with a stub embedding, a random vector, or a hand-written synonym table
presented as a semantic result: an unmeasurable approximation is worse than a
documented blocker.

Probe E1 at any time with a read-only request:

```bash
curl -fsS http://127.0.0.1:11434/api/tags | python3 -c "
import json,sys
for m in json.load(sys.stdin)['models']:
    print(m['name'], m.get('capabilities'))
"
```

An embedding-capable model reports `embeddings` in its capabilities.

## 5. Method

Everything runs in a disposable temporary directory. The production database,
the production configuration and the staged release are never touched.

**Step 0 — endpoint probe (fail closed).**
Abort with a distinct `BLOCKED` status if the endpoint is unreachable or the
requested model is absent from `/api/tags`. Never continue with a substitute.

**Step 1 — reuse the control dataset.**
Import the `CASES`, `CATEGORIES` and store builder from
`ops/measure_retrieval_quality.py` so the two measurements are directly
comparable: the same 6 stored facts, the same 24 queries, the same
hit/answered/in-context definitions, the same `memory_records` rows written
through `core.db.append_memory`.

**Step 2 — build the experimental semantic index (separate file, outside `runtime/`).**
Embed each row's `subject + fact` once with `OllamaEmbeddings`, embed each
query, rank by cosine similarity, take the top 5. Record the number of
embedding calls (6 documents + 24 queries) and the per-query latency.

**Step 3 — measure the same three axes for two projections.**

* *semantic only* — what a pure semantic retriever would retrieve;
* *fused* — lexical results first, semantic results appended for the
  remaining slots. **The fused projection is the one that could ship**, because
  the production path is lexical today and the lexical path must keep its
  exact-match behaviour.

**Step 4 — record operational facts.**
Added latency per query (p50/p95), embedding failure count, fallback count
(how often the keyword path carried the answer because the endpoint failed),
the model name and digest, and the provider host.

**Step 5 — decide by the thresholds in §6 and write the report to
`/tmp/minisa-semantic-retrieval-report.json`.**

Proposed file layout (deliberately outside the release tree):

```
ops/measure_semantic_recall.py            # experiment harness, not shipped
ops/SEMANTIC_RECALL_EXPERIMENT.md         # this proposal
```

If the experiment is ever accepted for production, it becomes a new optional
module (`runtime/memory/sourced_semantic.py`) behind an explicit configuration
switch, and it gets its own release, its own manifest and its own tests. It is
not part of the 2026-10-21 candidate.

## 6. Acceptance thresholds

| Metric | Control | Required to accept |
| --- | --- | --- |
| overall retrieval hit rate (24) | 0.583 | ≥ 0.750 |
| paraphrase hits | 2/6 | ≥ 4/6 |
| exact hits (regression guard) | 6/6 | = 6/6 |
| morphological hits (regression guard) | 6/6 | ≥ 6/6 |
| answered (24) | 7/24 | ≥ 11/24 |
| in context (24) | 14/24 | ≥ 18/24 |
| added p95 query latency | — | ≤ 400 ms |
| keyword fallback correctness | — | 100 % of endpoint failures fall back to the lexical path |

Rejection is a normal, publishable outcome: a regression in exact or
morphological recall, an overall gain below 0.167, or a p95 above 1 s rejects
the experiment and keeps the lexical projection unchanged.

## 7. Risks

| Risk | Handling |
| --- | --- |
| embedding endpoint outage | already classified and cooldown-bound in `runtime/memory/embeddings.py`; the engine falls back to the keyword path; the experiment must report the fallback count, not hide it |
| facts leaving the host | embedding a stored fact sends its text to the embedding endpoint. If the endpoint is the remote host, that is a data-handling decision for the administrator, not an experiment decision |
| vector cache staleness | the existing cache key includes adapter, host and model; the report must record the model digest so a model swap invalidates results |
| latency on the request path | measured per query; if p95 exceeds the budget, the accepted design would be an offline index refreshed by the dream cycle rather than an inline call |
| scope creep into deployment | the harness lives in `ops/`, the runtime module does not exist yet, and the staged release manifest is unchanged by this proposal |

## 8. Cheaper alternative to evaluate in parallel

If E1 stays unmet, the remaining gap can be attacked without any model: extend
the lexical projection with query expansion or light stemming over the same
`memory_records` rows and measure it with the *same* harness and the *same*
thresholds. That change is dependency-free and can be exercised today. It
would not be expected to solve the *vague* category, and the experiment above
remains the only path to paraphrase recall.

## 9. Reproduce

```bash
# control (runs today)
python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/measure_retrieval_quality.py \
  --output /tmp/minisa-retrieval-report.json

# prerequisite probe (runs today; shows the blocker)
curl -fsS http://127.0.0.1:11434/api/tags

# experiment (only after E1 and E2 are met)
python3 /home/sbeppi/minisa-working-assistant-20261018/candidate/ops/measure_semantic_recall.py \
  --embedding-url http://127.0.0.1:11434 --embedding-model nomic-embed-text \
  --output /tmp/minisa-semantic-retrieval-report.json
```
