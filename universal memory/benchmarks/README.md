# Recall benchmark

Run from the repository root with the project virtual environment and semantic extra installed:

```powershell
$env:HF_HOME = Join-Path $env:TEMP 'umem-benchmark-model-cache'
$env:HF_HUB_OFFLINE = '1'  # after downloading the model
.\.venv\Scripts\python.exe benchmarks/recall_benchmark.py --mode default --output benchmarks/results
.\.venv\Scripts\python.exe benchmarks/recall_benchmark.py --mode semantic --output benchmarks/results
```

Run the modes sequentially. Each invocation creates a new local temporary data directory,
retained for inspection; its path is recorded in the JSON output. Existing application data
is untouched. Model files are cached separately and excluded from index size.

## Method

- Same deterministic synthetic corpus for both modes: ten everyday activity topics,
  997 people, 113 locations, 251 projects, unique observation/session identifiers,
  tags, entities, and varying importance. All records remain ACTIVE.
- One growing corpus per mode, measured at 1,000, 10,000, and 100,000 records.
- Public `import_file` ingestion of JSONL files containing 1,000 records each,
  with SQLite durability, import history, and full dense/sparse indexing. This is the
  documented bulk-load path. It validates IDs and rejects collisions; it does not do
  `remember_many`'s text-based duplicate reinforcement. Insertion times therefore
  describe bulk import, not individual conversational `remember` calls.
- Insertion time is cumulative time inside ingestion calls, including embedding and
  index persistence. It excludes data generation, engine/model initialization,
  intervening recall measurements, validation, shutdown, and reopening.
  Parsing the generated JSONL files is included; generating them is excluded.
- Default `HashEmbedder` versus `SentenceTransformerEmbedder` using
  `sentence-transformers/all-MiniLM-L6-v2`. Both use embedded local Qdrant and the
  same hybrid recall pipeline. No external Qdrant server.
- Ten warm-up queries, followed by 100 sequential timed queries, `top_k=5`, other
  recall settings unchanged. Queries mix person/topic, activity, and location requests.
  Identical queries and random seed are used at every size and in both modes.
- Full SDK call latency includes query embedding, dense/sparse/SQLite searches,
  default archive probing, reranking, access-count writes, and retrieval logging.
- Median is the ordinary sample median; p95 is nearest-rank (95th of 100 sorted
  observations). Raw latencies are saved for inspection.
- Each checkpoint verifies SQLite and vector counts, an empty indexing outbox,
  no reported index error, and five results per query.
- Database size is the SQLite file after WAL checkpoint and clean close. Index size
  is the sum of logical file sizes under `qdrant/` after close. Both exclude backups,
  model cache, Python packages, and RAM use; units in the report are MiB (2^20 bytes).
- Earlier checkpoint queries leave their normal access counts and retrieval logs in
  the growing database. Each size is a single run, not an average across repetitions.

These measure throughput and latency on this synthetic workload, not retrieval quality,
cold-start latency, concurrent load, or remote/server Qdrant scalability. Embedded Qdrant
uses local in-memory search; do not extrapolate these results to a Qdrant server.

## Setup notes

The initial model download needed Windows' certificate trust store. It was completed
with `huggingface_hub.set_client_factory` and an `httpx.Client` configured with
`verify=ssl.create_default_context()`; TLS verification was never disabled. The measured
semantic run uses the local model cache in offline mode.

This run used an Intel Core i5-13400F (16 logical processors). The cached model
revision was `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`.

An exploratory standard-ingestion run is retained separately as
`results/preliminary-standard-ingestion-1k.json`. It used `remember_many` batches of
100 and overlapped dependency/model setup. It is not part of the comparable bulk-import
results. The exploratory run was stopped during growth toward 10k.

## Hardened backend benchmark commands

Original `results/default.json`, `results/semantic.json` and preliminary results
are preserved. New commands refuse to overwrite existing result files. Use a fresh
output directory for each comparison. Run performance jobs sequentially with no
other benchmark/model initialization in parallel.

```sh
python benchmarks/recall_benchmark.py --mode default --backend local --sizes 1000 10000 --output benchmarks/results/my-local-run
python benchmarks/recall_benchmark.py --mode semantic --backend local --sizes 1000 10000 --output benchmarks/results/my-local-run
python benchmarks/recall_benchmark.py --mode default --backend qdrant --qdrant-url http://localhost:6333 --sizes 1000 10000 100000 --output benchmarks/results/my-server-run
python benchmarks/recall_benchmark.py --mode semantic --backend qdrant --qdrant-url http://localhost:6333 --sizes 1000 10000 100000 --output benchmarks/results/my-server-run
python benchmarks/quality_benchmark.py --mode default --output benchmarks/results/my-quality-hash.json
python benchmarks/quality_benchmark.py --mode semantic --output benchmarks/results/my-quality-semantic.json
```

`--sizes 1000 10000 100000 1000000` enables an optional million-record run; never
run it in ordinary CI. Local 100k remains supported but deliberately omitted from
routine reruns because its vector scans are already documented. Semantic runs
reuse the model loaded once per process. Set `HF_HUB_OFFLINE=1` and the appropriate
`HF_HOME` cache directory for offline speed runs; quality runs require cached models.

The corpus, queries, ten warm-ups and 100 measured recalls match the old runner.
Each run creates a fresh temporary SQLite directory and isolated Qdrant collection.
Files and server collections are retained for inspection; remove only those exact
benchmark artifacts when finished. Server disk size is reported as null, since a
client cannot measure the server filesystem reliably. Peak RAM is not sampled.

Output adds cumulative records/second, stage medians and backend/index readiness.
Server runs wait for optimizer status green and, at 10k+, at least 90% indexed
vectors (600-second timeout configurable with `--index-wait-seconds`). This prevents
calling a freshly uploaded, unindexed collection an ANN benchmark. Small collections
may legitimately use exact search. Index readiness wait is separate from ingestion
and must be included when comparing total time to usable indexed data.

Stage timings overlap: active/archive retrieval include dense, sparse and SQLite
FTS; lookup/reranking includes separate lookup and reranking fields. Total includes
normal access updates and logs. Do not add all columns together.

The quality fixture has 12 paraphrases and 24 memories, including same-person and
lexical distractors and some queries with no shared vocabulary. It reports Recall@1,
Recall@5 and MRR@5, preserving query-level outcomes. This small diagnostic is not a
representative Minisa evaluation, ANN recall measurement, or semantic quality claim
for arbitrary domains. Add real application examples before choosing thresholds.

Real server integration test (otherwise skipped):

```sh
# Set UMEM_TEST_QDRANT_URL to your test server first.
python -m pytest -m integration -q
```

The integration test creates and removes only its own isolated collection. Default
CI uses real embedded Qdrant and fake server transport/configuration tests.


`sqlite_profile.py --database PATH --output NEW_JSON` measures only the SQLite FTS
component against an existing corpus using a read-only connection. It never opens
a MemoryEngine or modifies the snapshot. These figures must not be reported as
end-to-end recall or Qdrant server latency.
