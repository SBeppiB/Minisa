"""Repeatable candidate-only retrieval-quality evaluation.

Compares recall systems over a synthetic corpus with explicit relevance
labels. Isolated temp data only; never touches production stores. Not a
unittest module: run explicitly with

    python3 tests/retrieval_quality_eval.py            # keyword + semantic
    <venv>/python tests/retrieval_quality_eval.py      # adds Universal Memory

Systems measured:
- keyword:      LocalMemoryService keyword recall (production default path).
- semantic:     LocalMemoryService semantic recall against the configured
                Ollama host; measures fallback/failure frequency honestly.
- semantic_hash: SemanticMemoryRetriever pipeline with a deterministic
                hashed bag-of-words stand-in. Validates pipeline mechanics
                (identity, caching, thresholds, ranking) only; it is NOT a
                semantic language model.
- umem:         Universal Memory read-only adapter (when importable). UM's
                default embedder is a lexical hash baseline, not semantic.

Limitations: no sentence-transformer model is available offline in this
environment, so paraphrase/semantic retrieval quality cannot be measured
here; results compare lexical ranking behavior only.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
import time
from pathlib import Path

CANDIDATE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CANDIDATE))

from runtime.memory.semantic_retriever import SemanticMemoryRetriever  # noqa: E402
from runtime.services.memory import LocalMemoryService  # noqa: E402

CORPUS = [
    ("memory_001", "Bicycle Purchase", "User purchased a red bicycle in September 2026."),
    ("memory_002", "Vault Access", "The vault code is 4242 and was rotated in October 2026."),
    ("memory_003", "Aurora Project", "Project codename is aurora; deployment window is Sunday mornings."),
    ("memory_004", "Dentist Appointment", "User has a dentist appointment on November 3rd at 10am."),
    ("memory_005", "Team Meeting Notes", "Weekly team meeting moved to Tuesdays starting in January."),
    ("memory_006", "Retrospective Plan", "The team retrospective is scheduled for the last Friday of each month."),
    ("memory_007", "Software Updates", "The laptop installs operating system updates automatically on Wednesdays."),
    ("memory_008", "Aurora Status", "Aurora project status is green; release candidate ships next week."),
    ("memory_009", "Coffee Preference", "User drinks espresso, no sugar, in the mornings."),
    ("memory_010", "Travel Budget", "Travel budget for the conference is 1500 dollars."),
]

QUERIES = [
    ("exact", "red bicycle purchase", {"memory_001"}),
    ("paraphrase", "what two-wheeler did I buy", {"memory_001"}),
    ("partial", "vault code", {"memory_002"}),
    ("ambiguous", "team meeting schedule", {"memory_005", "memory_006"}),
    ("no_hit", "quantum chromodynamics lecture", set()),
    ("keyword_trap", "project status", {"memory_008"}),
    ("multi", "aurora project", {"memory_003", "memory_008"}),
]

TOP_K = 5


class HashingEmbeddings:
    """Deterministic hashed bag-of-words stand-in for pipeline testing only."""

    def __init__(self, dimensions: int = 256):
        self.model = "hashing-bow-standin"
        self.host = "in-process"
        self.dimensions = dimensions

    def cache_identity(self) -> dict:
        return {"host": "in-process", "model": self.model, "digest": "deterministic-v1"}

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in re.findall(r"[a-z0-9']+", text.lower()):
            slot = int.from_bytes(hashlib.blake2s(token.encode()).digest()[:4], "little")
            vector[slot % self.dimensions] += 1.0
        if not any(vector):
            vector[0] = 1.0
        return vector


def make_root(base: Path) -> Path:
    root = base / "minisa-root"
    for directory in ("CORE", "MEMORY/active", "MEMORY/archive", "STATE"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    for memory_id, title, fact in CORPUS:
        path = root / "MEMORY" / "active" / f"{memory_id} - {title}.md"
        path.write_text(
            f"# {title}\n\nID: {memory_id}\nType: fact\nSubject: user\n"
            f"Source: conversation\nStatus: active\nConfidence: 0.90\n"
            f"Created: 2026-10-18\n\n## Fact\n\n{fact}\n",
            encoding="utf-8",
        )
    return root


def map_projection_ids(results: list[str]) -> list[str]:
    """Map keyword/semantic projections back to corpus ids via fact text."""
    by_fact = {fact: memory_id for memory_id, _, fact in CORPUS}
    mapped = []
    for result in results:
        for fact, memory_id in by_fact.items():
            if fact in result:
                mapped.append(memory_id)
                break
    return mapped


def run_system(system_run, label: str) -> dict:
    rows = []
    for category, query, expected in QUERIES:
        started = time.perf_counter()
        try:
            ids = system_run(query)[:TOP_K]
            error = None
        except Exception as exc:  # measured honestly, never hidden
            ids = []
            error = f"{type(exc).__name__}: {exc}"
        latency_ms = (time.perf_counter() - started) * 1000.0
        hits = [i for i in ids if i in expected]
        irrelevant = [i for i in ids if i not in expected]
        rows.append({
            "category": category,
            "query": query,
            "expected": sorted(expected),
            "retrieved": ids,
            "hits": hits,
            "irrelevant": irrelevant,
            "recall_at_k": (len(hits) / len(expected)) if expected else None,
            "latency_ms": round(latency_ms, 2),
            "error": error,
        })
    scored = [r for r in rows if r["expected"]]
    return {
        "system": label,
        "rows": rows,
        "mean_recall_at_k": round(
            sum(r["recall_at_k"] for r in scored) / len(scored), 3
        ) if scored else None,
        "total_irrelevant_hits": sum(len(r["irrelevant"]) for r in rows),
        "no_hit_correct": next(
            (not r["retrieved"] and not r["error"] for r in rows if r["category"] == "no_hit"),
            None,
        ),
        "failures": sum(1 for r in rows if r["error"]),
        "mean_latency_ms": round(sum(r["latency_ms"] for r in rows) / len(rows), 2),
    }


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="minisa-retrieval-eval-"))
    root = make_root(base)
    results = []

    keyword_service = LocalMemoryService(root, ollama_url=None)
    results.append(run_system(
        lambda query: map_projection_ids(keyword_service.recall(query, TOP_K, semantic=False)),
        "keyword",
    ))

    semantic_service = LocalMemoryService(
        root, ollama_url="http://127.0.0.1:11434", embedding_model="nomic-embed-text"
    )
    results.append(run_system(
        lambda query: map_projection_ids(semantic_service.recall(query, TOP_K, semantic=True)),
        "semantic(ollama)",
    ))

    hash_retriever = SemanticMemoryRetriever(keyword_service.manager, embeddings=HashingEmbeddings())
    results.append(run_system(
        lambda query: map_projection_ids(hash_retriever.retrieve(query, limit=TOP_K)),
        "semantic_hash(standin)",
    ))

    try:
        from runtime.services.universal_memory_readonly import UniversalMemoryReadOnlyAdapter

        adapter = UniversalMemoryReadOnlyAdapter(base / "umem-data")
        for _, _, fact in CORPUS:
            adapter._engine.remember(fact)

        def umem_run(query):
            hits = adapter.recall_detailed(query, TOP_K)
            by_fact = {fact: memory_id for memory_id, _, fact in CORPUS}
            return [by_fact.get(h["content"], f"unknown:{h['id'][:8]}") for h in hits]

        results.append(run_system(umem_run, "umem"))
        adapter.close()
    except ImportError:
        print("universal_memory not importable in this environment; umem skipped")

    summary = {
        "corpus_size": len(CORPUS),
        "queries": len(QUERIES),
        "top_k": TOP_K,
        "eval_dir": str(base),
        "systems": results,
    }
    out = base / "retrieval-eval.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    for system in results:
        print(f"\n=== {system['system']} ===")
        print(f"  mean recall@{TOP_K}: {system['mean_recall_at_k']}  "
              f"irrelevant: {system['total_irrelevant_hits']}  "
              f"no_hit_correct: {system['no_hit_correct']}  "
              f"failures: {system['failures']}  "
              f"mean latency: {system['mean_latency_ms']}ms")
        for row in system["rows"]:
            status = "ERR" if row["error"] else ("ok " if row["hits"] or (not row["expected"] and not row["retrieved"]) else "MISS")
            print(f"  [{status}] {row['category']:12} -> {row['retrieved']}  (expected {row['expected']})")
            if row["error"]:
                print(f"        error: {row['error']}")
    print(f"\nJSON summary: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
