# Universal Memory

An application-neutral, local-first persistent memory library for assistants, agents,
NPCs, games, bots, and other applications. SQLite owns the records; Qdrant is a
rebuildable retrieval index. The Python SDK works without an HTTP server.

This is a persistent information retrieval architecture. It does not create
consciousness or reproduce human memory. It has no dependency on Minisa or any
particular application or model vendor.

## Installation

Python 3.11+ and SQLite with FTS5/JSON support are required (included in standard
CPython distributions). From this checkout:

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[api,semantic,dev]"
```

For the lightweight local SDK, install `-e .`. The `api`, `semantic`, and `dev`
extras are independent. Tests never download a language model.

**The default `HashEmbedder` is a deterministic lexical baseline, not a semantic
language model.** It makes offline setup and experimentation immediate. Configure
`SentenceTransformerEmbedder` for semantic recall in an actual application. Its
first initialization downloads the chosen model; subsequent runs use its local
cache. Keep the same embedding model when reopening a data directory.

## Basic Python usage

```python
from universal_memory import MemoryEngine, SentenceTransformerEmbedder

with MemoryEngine(
    "./data",
    namespace="user:42",
    embedder=SentenceTransformerEmbedder(),
) as memory:
    observation = memory.remember(
        "The user showed me their new racing game",
        importance=0.8,
        tags=["game"],
        source="conversation",
        entity_ids=["user:42", "project:racer"],
    )
    results = memory.recall("What games has the user made?", top_k=5)
    for result in results:
        print(result.memory.content, result.score)
        print(memory.explain_recall(result))

    memory.pin(observation.id)
    memory.unpin(observation.id)
    memory.archive(observation.id)  # forget(id) means the same thing
    memory.restore(observation.id)
    memory.edit(observation.id, importance=0.9)
    print(memory.history(observation.id))
```

Use `MemoryEngine("./data")` for the download-free baseline. The directory contains
`memory.sqlite3`, SQLite WAL files, `qdrant/`, and automatic `backups/`.
Call `close()` or use a context manager to release Qdrant's local file lock.

`remember_many([{ "content": "..." }, ...])` validates up to 1,000 observations
before writing and batches embedding/index operations. Each batch commits atomically with FULL synchronous durability; a crash before
commit rolls back the complete batch. Replaying it reinforces exact
duplicates rather than silently replacing records.

## Architecture

```text
External clients: Python / JavaScript / Rust / C# / games / agents
                         │
                 FastAPI REST adapter
                         │
                MemoryEngine Python SDK
                         │
       ┌─────────────────┼──────────────────┐
       │                 │                  │
 SQLiteStore         Embedder           VectorIndex
 records, FTS5,      protocol           protocol
 history, links,        │                  │
 index outbox      SentenceTransformers   Qdrant
       │           or hash baseline    dense + sparse
       └──────── durable authoritative state
```

`models.py` defines validated public contracts. `storage.py` owns the schema and
SQLite operations; `engine.py` coordinates the SDK; `ranking.py` explains scoring;
`admin.py` implements maintenance. Embedding and index protocols are replaceable.
`api.py` and `cli.py` contain transport and presentation code.

SQLite uses WAL, foreign keys, full synchronous durability, parameterized SQL,
explicit write transactions, and schema versioning with `PRAGMA user_version`.
Version 1 databases migrate transactionally to version 2 (additional SQL indexes); unknown future schema versions are
rejected. Future schema upgrades should be added as sequential migrations.

## Retrieval and explanations

1. Encode the query once. Search named dense and sparse vectors in Qdrant with
   namespace and tier filters. Sparse vectors use stable hashed Unicode word IDs,
   saturating term frequency, and Qdrant's IDF modifier. This is BM25-style
   keyword retrieval, not a full BM25 implementation with document-length normalization.
2. Merge dense and sparse candidates by ID, also querying SQLite's indexed FTS5
   fallback. No Python loop scans all archived memories during recall.
3. Read candidate records from SQLite and recheck scope, current tier, and index
   status. Deleted or stale index entries cannot expose deleted records.
4. Rerank using the contributions below; ties use the stable memory ID.
5. Expand one bounded association hop. Rank the combined results, then update
   access counts only for returned records.

| Contribution | Maximum weight |
|---|---:|
| Dense cosine relevance, clamped at zero | 0.40 |
| Lexical relevance: max(query token coverage, normalized sparse score) | 0.25 |
| Importance | 0.08 |
| Confidence | 0.04 |
| Creation recency, exponential with 365-day scale | 0.04 |
| Log-scaled access count | 0.03 |
| Entity intersection with supplied context | 0.07 |
| PRIORITY tier | 0.04 |
| Context string token overlap | 0.05 |
| Superseded historical state | -0.20 |
| Association-only retrieval | -0.03 |

Scores are ranking heuristics, not calibrated probabilities. Each result includes
raw semantic and lexical scores, every weighted contribution, reasons, original
tier, chain source, and archive-resurfacing status. Retrieval logs retain IDs and
explanations, not query text or memory contents. The default keeps 1,000 calls per
namespace. Weights live in the small, inspectable `ranking.py` module.

PRIORITY records receive a boost and are protected from automatic decay. They are
still subject to relevance and candidate limits: pinning does not inject unrelated
content into every response.

## Isolation, entities, and associations

Every engine has a fixed namespace; reads, writes, links, consolidation, and recall
are scoped to it. Use a distinct namespace per independent user or tenant.
Namespaces are an application data boundary; direct filesystem access is not
restricted by this library. A Qdrant collection is scoped to the SQLite instance
and embedding fingerprint.

`entity_ids` may identify people, characters, users, places, projects, or
conversations. Ordinary entity context boosts relevance; for an exclusion filter:

```python
results = memory.recall(
    "What happened?", entity_ids=["project:racer"], strict_entities=True
)
memory.link(first_id, second_id, kind="related")
```

Strict entity filtering uses intersection semantics and also applies to chain
expansion. Built-in backends apply it before candidate retrieval and revalidate against SQLite.
Custom legacy indexes without `search_filtered` retain post-filter compatibility. Never treat a relevance boost as access control.
Links are bidirectional by default. Chain expansion visits at most five edges and
one hop by default; archived targets are excluded from this route.

## Forgetting and archive resurfacing

Tiers are `PRIORITY`, `ACTIVE`, `ARCHIVED`, and `DELETED`. `DELETED` denotes the
terminal lifecycle state: explicit deletion removes the record, so `get()` raises
`KeyError` rather than returning a content-bearing tombstone.

`archive()` and `forget()` preserve the complete record. Normal retrieval searches
PRIORITY and ACTIVE; a separate bounded archive probe searches up to eight
candidates per retrieval channel. A candidate must meet either semantic similarity
0.88 or query token coverage 0.65 (with at least two query words). An unrelated
archive entry is never updated merely because it was probed. Only returned entries
gain access counts. `include_archived=False` disables archive probes entirely.

Resurfacing does not automatically restore a record by default. Set `promote=True`
on recall or configure `promote_resurfaced=True` to move returned archive hits to
ACTIVE. Thresholds require calibration against your model and domain.

`decay()` previews candidates; `decay(dry_run=False)` archives them. It walks active
records in bounded batches, never deletes them, and skips pinned/PRIORITY records.
Retention is:

```text
importance + (1 - importance) × 2^(-days_since_access / (half_life × resistance))
resistance = 1 + log(1 + accesses + reinforcements)
```

The default half-life is 180 days and archive threshold is 0.12. Importance forms a
retention floor. Schedule maintenance explicitly with your application's scheduler
or cron; there is no hidden background thread.

## Reinforcement, consolidation, and changed facts

Normalized exact repeats reinforce a memory in the same entity scope. Very close
punctuation variations may also reinforce it, but token order and words must match.
Each incoming observation's original text, source, and metadata is preserved in
history. Changed words, negations, quantities, and paraphrases are kept separately.
`duplicates()` offers near-duplicate suggestions for review; it does not automatically
merge semantically similar assertions. Set `reinforce=False` to force a separate record.

```python
summary = memory.consolidate([first_id, second_id], "The user regularly enjoys chess")
print(memory.consolidation(summary.id))
memory.reverse_consolidation(summary.id)
```

The caller supplies an intentional summary. Consolidation atomically creates it,
links all sources, records source snapshots, and archives the originals. Sources
must share the same entity scope. Priority/pinned or superseded sources are rejected.
Reversal restores source tiers and archives the summary. It refuses to overwrite
source changes made since consolidation and cannot restore explicitly deleted sources.

For changing factual state, first remember the new observation, then call
`memory.supersede(old_id, new_id)`. The old content remains with an explicit pointer
and a ranking penalty. Cycles and mismatched entity scopes are rejected. Contradictions
are caller-declared; the library does not claim to infer factual truth from text.

## REST API

```sh
python -m uvicorn universal_memory.api:create_app --factory --host 127.0.0.1 --port 8000
```

Interactive OpenAPI docs: `http://127.0.0.1:8000/docs`.

Environment variables: `UMEM_DATA`, `UMEM_NAMESPACE`, `UMEM_EMBEDDING_MODEL`,
`UMEM_QDRANT_URL`, `UMEM_QDRANT_API_KEY`, and `UMEM_API_KEY`.
Set `UMEM_API_KEY` to require `Authorization: Bearer YOUR_KEY` on data endpoints.
Without it, the local API is unauthenticated. The docs and schema remain public.
Deploy behind TLS and a trusted authentication/rate-limiting proxy for external
access. One deployment serves one fixed namespace; clients cannot select a tenant
using request fields. Use an application-authenticated routing layer for multi-tenancy.

```javascript
const base = "http://127.0.0.1:8000";
const headers = {"Content-Type": "application/json"};
await fetch(`${base}/memories`, {
  method: "POST", headers,
  body: JSON.stringify({content: "Alex built a racing game", importance: 0.8})
});
const response = await fetch(`${base}/recall`, {
  method: "POST", headers,
  body: JSON.stringify({query: "What did Alex build?", top_k: 5})
});
console.log(await response.json());
```

| Endpoint | Purpose |
|---|---|
| `POST /memories`, `POST /memories/batch` | Remember or reinforce |
| `POST /recall` | Results with explanations |
| `GET /memories`, `GET /memories/{id}` | List/filter and inspect |
| `PATCH /memories/{id}` | Edit validated fields with history |
| `POST /memories/{id}/archive`, `/restore`, `/forget`, `/pin`, `/unpin` | Lifecycle |
| `DELETE /memories/{id}` | Explicit removal |
| `GET /memories/{id}/history` | Versions and observations |
| `POST /memories/{id}/links`, `/supersede` | Associations and changed facts |
| `POST /consolidations`, `GET /consolidations/{id}`, `POST /consolidations/{id}/reverse` | Consolidation |
| `GET /stats`, `/health`, `/retrieval-logs`, `/duplicates` | Administration |
| `POST /maintenance/decay`, `/maintenance/rebuild-index`, `/maintenance/sync-index` | Maintenance |

Synchronous handlers run in FastAPI's worker thread pool. An engine serializes access
with a reentrant lock. Async Python applications can use `await asyncio.to_thread(...)`;
the basic local API intentionally stays synchronous. Inject a configured engine with
`create_app(engine)` for application-managed lifecycle and settings.

## CLI and administration

```sh
umem --data ./data remember "I met Alex today" --tag people --entity alex
umem --data ./data search "Alex" --top-k 5
umem --data ./data list --tier ARCHIVED --limit 100
umem --data ./data inspect MEMORY_ID
umem --data ./data archive MEMORY_ID
umem --data ./data restore MEMORY_ID
umem --data ./data pin MEMORY_ID
umem --data ./data unpin MEMORY_ID
umem --data ./data edit MEMORY_ID '{"importance":0.9}'
umem --data ./data history MEMORY_ID
umem --data ./data stats
umem --data ./data duplicates
umem --data ./data decay                 # preview
umem --data ./data decay --apply
umem --data ./data rebuild-index
umem --data ./data sync-index
umem --data ./data export memories.jsonl
umem --data ./data backup snapshot.sqlite3
umem --data ./restored import memories.jsonl
```

Place global options before the command. Use `--model MODEL` consistently for a
semantic data directory. Commands print JSON. `list` uses an `--after LAST_ID`
cursor. `link`, `supersede`, `consolidate`, `consolidation`, `reverse-consolidation`,
`forget`, `delete`, and `logs` are also available; see `umem --help`.

JSONL export streams memories, history, links, and consolidation records. Import
preserves IDs, maps records to the target namespace, validates links, rejects
collisions, and rolls back the whole import on failure. Import into an empty
directory to migrate embedding providers. It streams the file twice, so keep the
input file unchanged during import. Backups use SQLite's online backup API and
contain **all namespaces**. To restore a backup, close the engine and place it at
`memory.sqlite3` in a fresh directory, then rebuild the index.

Automatic backups run at initialization and on ingestion when the configured
interval has elapsed; they are activity-driven, not a scheduler. Seven snapshots
are retained by default. No hidden destructive cleanup targets memories.

## Failure recovery and deployment

Every indexed mutation commits a SQLite record and outbox job together. Embedding
or Qdrant failure leaves `index_status="pending"` and a retryable job. Retrieval
falls back to SQLite keyword search. Call `sync_index()` to process a bounded batch,
or `rebuild_index()` to requeue all records in the current namespace and drain the
queue. Outbox processing may also service queued jobs from other namespaces in the
same database. Interrupted rebuilds are safe to rerun.

A missing collection queues reconstruction automatically, without eagerly embedding
an entire database during startup. A corrupt local index leaves SQLite usable.
With all other processes stopped, run:

```sh
umem --data ./data repair-local-index
```

This explicitly renames `qdrant/` to a uniquely named quarantine directory, creates
a fresh index, and rebuilds. It never deletes the old files. For remote Qdrant,
repair its collection administratively and run `rebuild-index`.

Embedded Qdrant is convenient for small projects and **requires a single owning
process**. Do not run a CLI writer alongside a local API process. For larger
collections use a Qdrant server via `qdrant_url` / `--qdrant-url`; payload indexes
are created for namespace and tier. SQLite remains on local disk. Avoid using a
live SQLite WAL database on a network filesystem or actively synchronized cloud
folder. Use a local data directory and copy consistent backup snapshots instead.

SQLite holds a write lock while each bounded index batch is processed to prevent
stale-version acknowledgements by concurrent writers. This favors consistency over
write throughput. Tune batch size and move embeddings to a faster local provider
if indexing latency dominates. There is no distributed queue or background worker.

`delete()` removes the live record, its history, relationships, and its snapshots
inside consolidation records, and queues vector deletion. It is logically immediate
for reads even when Qdrant is unavailable. It does not erase external exports,
old backups, quarantined indexes, SQLite free pages, or independent summaries that
contain similar information. Manage those copies under your application's retention
policy; this library does not promise forensic secure erasure.

## Configuration

```python
from universal_memory import MemoryEngine, Settings

settings = Settings(
    candidate_limit=120,
    archive_candidates=8,
    archive_semantic_threshold=0.90,
    archive_lexical_threshold=0.75,
    promote_resurfaced=False,
    decay_half_life_days=180,
    archive_decay_threshold=0.12,
    duplicate_threshold=0.97,
    chain_limit=5,
    backup_interval_seconds=86400,  # 0 disables automatic backups
    backup_keep=7,
    retrieval_log_keep=1000,       # 0 disables new retrieval logs
)
memory = MemoryEngine("./data", settings=settings)
```

The CLI accepts the same settings as JSON using `--config settings.json`. Unknown
settings and malformed inputs fail validation. Standard Python logging reports
degraded index and embedding operations; configure handlers in your application.

## Testing and limitations

```sh
python -m pip install -e ".[dev,api]"
python -m pytest -q
python -m ruff check src tests examples
python -m ruff format --check src tests examples
```

The suite exercises real local Qdrant dense/sparse indexing with deterministic
embeddings, semantic-only matches through a controlled semantic test provider,
keyword fallback, reranking, duplicates, tier transitions, resurfacing, decay,
consolidation, isolation, supersession, deletion, persistence, import rollback,
concurrent SDK calls, batch embeddings, corruption repair, APIs, and CLI commands.
CI runs on Linux and Windows with Python 3.11 and 3.13.

The current release is an initial implementation, not a claim of million-record
benchmark results or an independent production security audit. Remote Qdrant and
downloaded Sentence Transformers models need integration validation in your
deployment. Embedded Qdrant can scan in memory and is not the million-record target.
Hashed sparse tokens can collide and do not stem words; lexical archive thresholds
can overvalue common words. Model-specific relevance thresholds need evaluation.
Duplicate suggestions are bounded heuristics, not an exhaustive semantic deduplication
service. Decay and duplicate review traverse records in batches; normal recall does
not. Summary accuracy and contradiction detection remain the caller's responsibility.

See [the assistant integration example](examples/assistant.py). It retrieves context
before response generation and persists explicitly selected experiences afterward.

## License

MIT. See [LICENSE](LICENSE).


## Local and production deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for local/server profiles, Minisa configuration,
upgrade and recovery instructions, concurrency, profiling and offline models.
See [benchmarks](benchmarks/README.md) for reproducible speed and quality evaluation.

Measured outcomes and remaining production validation: [engineering report](ENGINEERING_REPORT.md).
