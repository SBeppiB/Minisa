# Universal Memory engineering report - 2026-09-19

Implementation is ready for Minisa integration testing. Large-scale production readiness is **not yet verified**: this Windows host has no Docker or installed WSL distribution, and no real Qdrant endpoint was provided. Server tests use fake transport except for a separately marked, currently skipped integration test. No server latency or million-record claim is made.

## Changes and reasons

- Preserved the existing engine, API, CLI, ranking, tiers, lifecycle, history, import/export, consolidation, relationships, and SQLite authority. There was no Git metadata in this workspace, so no commit was created.
- Added exported VectorIndexBackend, LocalQdrantBackend and QdrantServerBackend contracts/implementations; retained the original VectorIndex alias, QdrantIndex and custom index injection compatibility.
- Added explicit SDK/CLI/environment backend selection and validation before database creation. API configuration uses the same engine path. Added live backend-info and health inspection.
- Server collections explicitly enable HNSW, indexed namespace/tier/entities, bounded non-exact searches, and payload-free/vector-free search responses. Small sets can still use Qdrant's exact planner. Server optimizer/indexed-vector readiness is checked by the benchmark runner.
- Push strict entity filters into both Qdrant and SQLite before candidate selection, then revalidate authoritative records. Ranking weights and candidate budgets remain unchanged.
- Skip archive searches when an indexed SQLite existence check proves the archive empty. Keep bounded resurfacing when archives exist.
- Added nested recall timings for embedding, dense/sparse retrieval, FTS, archive probes, SQLite reads, ranking, relationships, updates and logs.
- SQLite schema v2 adds entity-memory, reverse-link, namespace-outbox and retrieval-log indexes. The entity deletion query previously lacked a supporting index, making repeated saves expensive as the entity table grew.
- remember_many now commits a validated batch atomically instead of committing every observation. Ordinary remember retains FULL synchronous per-call durability. Embeddings and Qdrant upserts remain batched after durable writes.
- Fixed backup SQLite connection cleanup. Added server client cleanup/reconnection, collection-loss reconstruction, and backend-switch reconstruction. Old collections and original SQLite content are retained.
- Added offline model loading and configurable embedding batch size. Reuse the same model instance.
- Repaired missing CI triggers, added Linux/Windows matrices and a manually triggered real-server integration workflow.

## Validation

54 tests passed, including all 36 original tests; 1 real-server integration test skipped. Ruff lint and format checks passed across src, tests, examples and benchmarks. Wheel build succeeded. Remaining warnings are two upstream FastAPI/Starlette deprecations and the expected local-exact-search warning from the fake server transport.

Coverage added: malformed backend URLs/configuration, explicit ANN parameters, environment selection, server API behavior, outage during writes, restart persistence, collection loss, reconstruction, atomic batch rollback, bounded candidate reads without listing the database, entity filter pushdown, mixed threaded reads/writes, schema migration, empty archive probes, backup closure, transient health recovery and quality fixtures.

## Speed: original versus final local measurements

Identical deterministic corpus/query generator; ten warm-ups and 100 measured recalls. Full public recall includes normal access updates and logs. Single runs, not controlled repeated statistical comparisons; storage/CPU variability is visible. No claim that all metrics improved.

| Embedding | Memories | Before median / p95 ms | After median / p95 ms |
|---|---:|---:|---:|
| default | 1,000 | 68.5 / 86.8 | 84.2 / 115.6 |
| default | 10,000 | 633.2 / 771.3 | 681.3 / 886.2 |
| semantic | 1,000 | 128.5 / 153.9 | 104.9 / 183.6 |
| semantic | 10,000 | 1057.0 / 1240.3 | 562.4 / 652.1 |

The semantic 10k median improved by about 47%; hash runs regressed, and semantic 1k p95 regressed. Empty archive probing fell to roughly 0.02-0.04 ms. Embedded dense and sparse searches remain linear and dominate retrieval.

| Embedding | Memories | Before cumulative insertion s | After cumulative insertion s | After records/s | SQLite MiB | Index MiB |
|---|---:|---:|---:|---:|---:|---:|
| default | 1,000 | 5.1 | 11.4 | 88.0 | 3.20 | 4.49 |
| default | 10,000 | 59.2 | 132.0 | 75.7 | 27.59 | 44.82 |
| semantic | 1,000 | 9.7 | 8.9 | 112.6 | 3.20 | 4.49 |
| semantic | 10,000 | 103.5 | 115.3 | 86.7 | 27.59 | 44.82 |

The ingestion measurements use the existing bulk import path, not remember_many. They do not demonstrate a universal throughput improvement; extra indexes also impose write cost. Peak RAM was not measured. Server index size is intentionally null in the runner unless measured separately on its host.

| 10k stage median, ms | Hash | MiniLM |
|---|---:|---:|
| query_embedding_ms | 0.12 | 8.99 |
| dense_ms | 282.51 | 264.29 |
| sparse_ms | 332.95 | 267.62 |
| sqlite_fts_ms | 17.56 | 12.00 |
| sqlite_lookup_ms | 14.30 | 6.33 |
| reranking_ms | 7.29 | 5.01 |
| relationship_scoring_ms | 0.36 | 0.27 |
| logging_commit_ms | 14.18 | 1.85 |

A separate **read-only** diagnostic against the retained 100k SQLite corpus measured FTS alone at 108.9 ms median / 147.6 ms p95. This is not full recall or simulated Qdrant-server performance. Broad lexical postings remain a material cost even after removing embedded vector scans.

Original local/hash 100k results remain 6,389.7 ms median and 11,438.8 ms p95, with 1,262.3 seconds cumulative insertion. Local 100k was deliberately not repeated; the missing semantic 100k run was also not resumed because embedded scans already explain the limitation. The runner supports all requested sizes and optional 1M, but real Qdrant 1k/10k/100k runs for both embedders remain **not run**. The 200/500 ms server target is unverified.

## Retrieval quality (separate from speed)

12 fixed queries and 24 memories, including paraphrases, distractors and vocabulary mismatch. This is a small diagnostic, not a representative Minisa evaluation or an ANN recall estimate.

| Embedder | Recall@1 | Recall@5 | MRR@5 |
|---|---:|---:|---:|
| default | 25.0% | 83.3% | 0.514 |
| semantic | 91.7% | 100.0% | 0.958 |

MiniLM loaded from the existing offline cache. HashEmbedder remains a lightweight lexical fallback, not equivalent semantic retrieval.

## Remaining bottlenecks and release gate

- Embedded local Qdrant remains unsuitable for large collections. Use server mode for Minisa and 100k+ datasets.
- SQLite FTS common-term traversal is not constant-time, despite bounded results. Keep measuring it with real Minisa queries before changing retrieval channels or sacrificing quality.
- SQLite holds the write lock across each bounded embedding/index batch to prevent stale acknowledgements. Use one engine/service process; high write concurrency and multi-process startup remain unverified.
- Normal recall includes durable access/log writes; their commit latency varies. Automatic backups should be explicitly scheduled outside Minisa conversations.
- Run the marked integration test and both 1k/10k/100k server speed runs on a real Qdrant host before production sign-off. Verify indexed vector counts, actual latency, outages/recovery and representative Minisa quality. The provided manual CI workflow can run the integration test, but was not executed here.

## Run and deploy

Lightweight: install `pip install -e .`, then use `MemoryEngine("./data")` in a context manager. No server or internet required.

Minisa: install `pip install -e ".[api,semantic]"`; use one persistent MemoryEngine with a cached MiniLM model, `vector_backend="qdrant"`, a local/private `qdrant_url`, and a database directory outside OneDrive. Schedule backups and maintenance explicitly.

```sh
docker run -d --name umem-qdrant -p 127.0.0.1:6333:6333 -v umem-qdrant:/qdrant/storage qdrant/qdrant:v1.17.0
umem --data ./minisa-data --model sentence-transformers/all-MiniLM-L6-v2 --vector-backend qdrant --qdrant-url http://localhost:6333 backend-info
umem --data ./minisa-data --model sentence-transformers/all-MiniLM-L6-v2 --vector-backend qdrant --qdrant-url http://localhost:6333 rebuild-index
python -m uvicorn universal_memory.api:create_app --factory --host 127.0.0.1 --port 8000
```

For the API, set UMEM_DATA, UMEM_VECTOR_BACKEND, UMEM_QDRANT_URL and UMEM_EMBEDDING_MODEL first. OpenAPI: `/docs`. Backend configuration does not change memory request contracts.

Upgrades queue a new `_v2` collection from authoritative SQLite. Run rebuild-index before expecting complete semantic recall; old collections are retained. Keep the same embedding model identity for an existing database. See [deployment guide](DEPLOYMENT.md) for full configuration and offline setup, and [benchmark commands](benchmarks/README.md) for reproduction.

## Artifacts

- [Final local speed and 100k SQLite diagnostic](benchmarks/results/final-20260919/).
- [Quality outcomes and intermediate diagnostic run](benchmarks/results/hardened-20260919/). The intermediate speed run overlapped model startup and predates empty-archive skipping; it is excluded from the final comparison.
- Original `benchmarks/results/default.json`, `semantic.json` and preliminary result files were not overwritten or deleted.
- [Wheel](dist/hardened/universal_memory-0.1.0-py3-none-any.whl).
- [Official Qdrant indexing reference](https://qdrant.tech/documentation/manage-data/indexing/) informed the HNSW and payload configuration; [v1.17.0 release](https://github.com/qdrant/qdrant/releases/tag/v1.17.0) is pinned in deployment examples.
