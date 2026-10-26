# Deployment and operations

## Local: development, games, small agents and offline installations

```python
from universal_memory import MemoryEngine
with MemoryEngine("./data", vector_backend="local") as memory:
    memory.remember("SBeppi showed me his new game")
    results = memory.recall("What games has SBeppi shown me?", top_k=5)
```

No server, Docker, or internet is required. HashEmbedder is a lexical baseline,
not a substitute for a semantic model. Embedded Qdrant performs exact vector
scans internally even though the SDK only reads and reranks bounded candidates.
Use it for small collections; around 20k points is a migration signal, not a
performance guarantee. The saved 100k embedded benchmark took seconds per recall.

## Server: Minisa, persistent assistants and 100k+ collections

Run Qdrant on the same machine or a reachable server. Docker is optional for the
library, and is one convenient way to host Qdrant:

```sh
docker run -d --name umem-qdrant -p 127.0.0.1:6333:6333 -v umem-qdrant:/qdrant/storage qdrant/qdrant:v1.17.0
```

Pin and validate your chosen Qdrant release in deployment. The server requires
named vectors, sparse IDF, and the query API (Qdrant 1.12+). Keep it on loopback
or a private authenticated network; remote credentials use `UMEM_QDRANT_API_KEY`.
See [Qdrant installation](https://qdrant.tech/documentation/quickstart/) for
native Linux/server alternatives.

```python
from universal_memory import MemoryEngine, SentenceTransformerEmbedder, Settings

embedder = SentenceTransformerEmbedder(local_files_only=True, batch_size=64)
with MemoryEngine(
    "./minisa-data",
    vector_backend="qdrant",
    qdrant_url="http://localhost:6333",
    embedder=embedder,
    settings=Settings(backup_interval_seconds=0),
) as memory:
    print(memory.backend_info())
    memory.remember("Alex purchased a red bicycle yesterday.")
    print(memory.recall("What did Alex buy?", top_k=5))
    print(memory.last_recall_profile)
```

Install `.[semantic]` and download/cache the model once before using
`local_files_only=True`, or pass a local model directory consistently across
restarts. The engine retains one embedder instance and batches document encoding.
Query embedding errors fall back to SQLite lexical retrieval; constructor errors
are explicit rather than silently changing embedding identity.

| Setting | Meaning |
|---|---|
| `UMEM_VECTOR_BACKEND=local` | Embedded convenience backend (default without URL) |
| `UMEM_VECTOR_BACKEND=qdrant` | Require an HTTP(S) server URL |
| `UMEM_QDRANT_URL=http://localhost:6333` | Select server mode if backend omitted |
| `UMEM_QDRANT_API_KEY` | Optional server credential |
| `UMEM_EMBEDDING_MODEL` | API embedding model/cache directory |

Explicit SDK/CLI backend and URL arguments override their respective environment
variables. An explicit local backend plus a configured URL is rejected as ambiguous.
The old `qdrant_url=` constructor and custom `index=` protocol remain supported.

```sh
umem --data ./minisa-data --model sentence-transformers/all-MiniLM-L6-v2 --vector-backend qdrant --qdrant-url http://localhost:6333 backend-info
umem --data ./minisa-data --model sentence-transformers/all-MiniLM-L6-v2 --vector-backend qdrant --qdrant-url http://localhost:6333 rebuild-index
python -m uvicorn universal_memory.api:create_app --factory --host 127.0.0.1 --port 8000
```

For the API command, configure `UMEM_DATA`, `UMEM_VECTOR_BACKEND`,
`UMEM_QDRANT_URL`, and optionally `UMEM_EMBEDDING_MODEL` first. OpenAPI remains
at `/docs`; `/backend-info` and `/health` inspect the actual backend. REST callers
send ordinary memory requests with no vector implementation details.

Server collections enable HNSW (`m=16`, `ef_construct=100`), use non-exact dense
queries (`hnsw_ef=128`), and index namespace, tier and entity payloads. The optimizer
indexing threshold and HNSW full-scan threshold are 1,000 KB, not disabled. Qdrant may choose exact search for
small filtered sets or not-yet-indexed segments; `backend-info` reports actual
indexed vector counts. ANN does not mean every segment is indexed immediately.
See [Qdrant indexing](https://qdrant.tech/documentation/manage-data/indexing/).

## Upgrade, recovery and maintenance

SQLite version 1 migrates transactionally to version 2, adding indexes for entity
updates, links, outbox scope and retrieval logs. Memory contents remain unchanged.
This release uses a new `_v2` index collection for entity payload compatibility.
Existing SQLite records and old Qdrant collections remain intact. On first open,
reconstruction jobs are queued; run `rebuild-index` to finish before expecting full
semantic recall. Switching backend targets also queues reconstruction. Unused old
collections may be removed administratively after verifying the rebuilt index.

`sync_index(batch_size=100)` retries one batch; schedule repeated calls until no
jobs remain. If a server collection disappears while running, a failed sync drops
the client; the next sync reconnects, recreates the collection and queues all SQLite
records. `rebuild_index()` drains these jobs in bounded batches. SQLite remains
usable with lexical fallback during outages. Search errors do not discard records.

For Minisa, use one engine/service process and share it across threads. Calls are
serialized with an RLock; SQLite transactions prevent stale outbox acknowledgements.
Local Qdrant permits one owning process. Server mode removes that file lock, but
this remains a single-writer-oriented library: network/embedding work holds the
SQLite write lock during each bounded sync batch. Multi-process startup and high
write concurrency are not production load-tested. Do not share an engine across
forked processes. Use the REST service for multiple application clients.

`remember()` commits each observation durably. `remember_many()` validates up to
1,000 observations, commits the complete batch atomically, then batches indexing.
`import_file()` streams an atomic import and then batches indexing. An index outage
never rolls back an already committed observation; its outbox job remains retryable.

Schedule consolidation, decay, duplicates, rebuilds, exports and backups explicitly
outside interactive work. Automatic backups remain enabled for compatibility;
set `backup_interval_seconds=0` and schedule `backup()` for predictable latency.
Do not run a live database in OneDrive or another actively synchronized directory;
copy consistent backup files there instead.

## Retrieval budgets and profiling

Normal recall merges at most three channels per tier, bounded by `candidate_limit`
(default 80) and `archive_candidates` (8), plus at most `chain_limit` relationship
edges (5). Empty archives skip probing. No SDK Python loop reads the full database.
SQLite FTS can still traverse common-term postings, and embedded Qdrant still scans
vectors; bounded result counts alone are not a claim of constant search cost.

`last_recall_profile` reports embedding, active retrieval, archive probing, dense,
sparse, FTS, SQLite candidate lookup, reranking, relationships, access updates,
logging/commit and total milliseconds. Some fields are nested measurements; do not
sum all fields. This is the most recent call on a serialized engine, not a per-client
telemetry endpoint. Search timings include transport overhead in server mode.

See [benchmark instructions](benchmarks/README.md) and the [engineering report](ENGINEERING_REPORT.md) for
measured results, limitations and remaining release validation.
