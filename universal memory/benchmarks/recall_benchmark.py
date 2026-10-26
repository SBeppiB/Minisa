"""Reproducible end-to-end local SDK benchmark; run with the project environment."""

import argparse
import importlib.metadata
import json
import math
import os
import platform
import random
import statistics
import tempfile
import time
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from universal_memory import HashEmbedder, MemoryEngine, SentenceTransformerEmbedder
from universal_memory.models import Memory, now

TOPICS = [
    ("astronomy", "observed distant stars through the telescope"),
    ("gardening", "planted tomatoes and watered the vegetable beds"),
    ("cooking", "baked sourdough bread for the family dinner"),
    ("cycling", "rode a bicycle along the river trail"),
    ("music", "practised piano and composed a new melody"),
    ("software", "fixed a database bug and deployed the application"),
    ("travel", "booked a train ticket and visited the museum"),
    ("painting", "painted a mountain landscape with watercolours"),
    ("fitness", "completed a strength workout at the gym"),
    ("reading", "read a historical novel at the library"),
]


def observation(i):
    topic, activity = TOPICS[i % len(TOPICS)]
    return {
        "content": (
            f"Observation {i}: person{i % 997} {activity} in location{i % 113}. "
            f"Project project{i % 251} recorded session{i} with rating {i % 5 + 1}."
        ),
        "tags": [topic],
        "entity_ids": [f"person:{i % 997}"],
        "importance": (i % 9 + 1) / 10,
    }


def queries():
    rng = random.Random(20260918)
    result = []
    for i in range(110):
        topic, activity = TOPICS[i % len(TOPICS)]
        result.append(
            [
                f"What did person{rng.randrange(997)} do about {topic}?",
                f"Who {activity}?",
                f"Find {topic} experiences in location{rng.randrange(113)}",
            ][i % 3]
        )
    return result


def size(path):
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["default", "semantic"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", choices=["local", "qdrant"], default="local")
    parser.add_argument("--qdrant-url")
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 10000, 100000])
    parser.add_argument("--index-wait-seconds", type=int, default=600)
    args = parser.parse_args()
    if args.sizes != sorted(set(args.sizes)) or min(args.sizes) < 5:
        parser.error("sizes must be increasing unique integers >= 5")
    output = args.output / f"{args.backend}-{args.mode}.json"
    if output.exists():
        parser.error(f"Refusing to overwrite {output}; choose a new output directory")
    args.output.mkdir(parents=True, exist_ok=True)
    data = Path(tempfile.mkdtemp(prefix=f"umem-bench-{args.mode}-"))
    started = time.perf_counter()
    embedder = HashEmbedder() if args.mode == "default" else SentenceTransformerEmbedder()
    report = {
        "mode": args.mode,
        "backend": args.backend,
        "data_directory": str(data),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "logical_cpus": os.cpu_count(),
        "python": platform.python_version(),
        "embedding": embedder.fingerprint,
        "model_initialization_seconds": time.perf_counter() - started,
        "ingestion": "public import_file JSONL bulk import; 1000 records per file",
        "versions": {
            name: importlib.metadata.version(name)
            for name in ["universal-memory", "qdrant-client", "numpy", "pydantic"]
        },
        "rows": [],
    }
    if args.mode == "semantic":
        import torch

        report["device"] = str(embedder.model.device)
        report["torch_threads"] = torch.get_num_threads()
        report["versions"].update(
            {
                name: importlib.metadata.version(name)
                for name in ["sentence-transformers", "torch", "transformers"]
            }
        )
    inserted = 0
    insertion_seconds = 0
    for target in args.sizes:
        with MemoryEngine(
            data, embedder=embedder, vector_backend=args.backend, qdrant_url=args.qdrant_url
        ) as memory:
            while inserted < target:
                end = min(inserted + 1000, target)
                source = data / "input.jsonl"
                stamp = now()
                with source.open("w", encoding="utf-8") as stream:
                    for i in range(inserted, end):
                        item = Memory(
                            **observation(i),
                            id=str(uuid5(NAMESPACE_URL, f"umem-benchmark:{i}")),
                            namespace="default",
                            created_at=stamp,
                            updated_at=stamp,
                            last_accessed_at=stamp,
                        )
                        stream.write(
                            json.dumps({"format": 1, "memory": item.model_dump(mode="json")}) + "\n"
                        )
                started = time.perf_counter()
                imported = memory.import_file(source)
                insertion_seconds += time.perf_counter() - started
                assert imported == end - inserted
                inserted = end
                if inserted % 1000 == 0:
                    print(f"{args.mode}: inserted {inserted}, {insertion_seconds:.1f}s", flush=True)
            count = memory.store.db.execute("SELECT count(*) FROM memories").fetchone()[0]
            pending = memory.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0]
            assert count == target and pending == 0 and memory.index_error is None
            assert memory.index.client.count(memory.index.collection, exact=True).count == target
            index_wait_started = time.perf_counter()
            if args.backend == "qdrant":
                while True:
                    info = memory.backend_info()
                    if info.get("status") == "green" and (
                        target < 10000 or (info.get("indexed_vectors") or 0) >= target * 0.9
                    ):
                        break
                    if time.perf_counter() - index_wait_started > args.index_wait_seconds:
                        raise RuntimeError(f"Index not ready for an ANN benchmark: {info}")
                    time.sleep(1)
            index_wait_seconds = time.perf_counter() - index_wait_started
            backend_info = memory.backend_info()
            profiles = []
            latencies = []
            for i, query in enumerate(queries()):
                started = time.perf_counter()
                results = memory.recall(query, top_k=5)
                elapsed = (time.perf_counter() - started) * 1000
                assert len(results) == 5 and memory.index_error is None
                if i >= 10:
                    latencies.append(elapsed)
                    profiles.append(dict(memory.last_recall_profile))
                if (i + 1) % 25 == 0:
                    print(f"{args.mode} {target}: query {i + 1}/110", flush=True)
            memory.store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        row = {
            "memories": target,
            "insertion_seconds": insertion_seconds,
            "insertion_records_per_second": target / insertion_seconds,
            "index_wait_seconds": index_wait_seconds,
            "backend_info": backend_info,
            "stage_median_ms": {
                key: statistics.median(p[key] for p in profiles) for key in profiles[0]
            },
            "median_recall_ms": statistics.median(latencies),
            "p95_recall_ms": sorted(latencies)[math.ceil(0.95 * len(latencies)) - 1],
            "index_bytes": size(data / "qdrant") if args.backend == "local" else None,
            "database_bytes": (data / "memory.sqlite3").stat().st_size,
            "latencies_ms": latencies,
        }
        report["rows"].append(row)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in row.items() if k != "latencies_ms"}), flush=True)


if __name__ == "__main__":
    main()
