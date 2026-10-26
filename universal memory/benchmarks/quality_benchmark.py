"""Small deterministic diagnostic, not a general semantic quality evaluation."""

import argparse
import json
import tempfile
from pathlib import Path

from universal_memory import HashEmbedder, MemoryEngine, SentenceTransformerEmbedder, Settings

CASES = [
    (
        "Alex purchased a red bicycle yesterday.",
        "What did Alex buy?",
        "Alex repaired a blue scooter yesterday.",
    ),
    ("Mira is allergic to peanuts.", "Which food must Mira avoid?", "Mira likes eating apples."),
    (
        "Owen commutes by train every morning.",
        "How does Owen get to work?",
        "Owen watches ships every evening.",
    ),
    (
        "Nia misplaced her house keys in the garden.",
        "Where did Nia lose the door key?",
        "Nia planted roses beside the garage.",
    ),
    (
        "The automobile requires gasoline.",
        "What fuel does the car need?",
        "The bicycle requires new tyres.",
    ),
    (
        "The physician prescribed antibiotics for the infection.",
        "What medicine did the doctor recommend?",
        "The gardener purchased fertilizer for the lawn.",
    ),
    (
        "Sam's appointment was postponed until Friday.",
        "When is Sam's rescheduled meeting?",
        "Sam bought tickets for a concert on Tuesday.",
    ),
    (
        "Lena prefers a quiet room when studying.",
        "What environment helps Lena learn?",
        "Lena enjoys loud concerts on weekends.",
    ),
    (
        "The canine concealed its bone beneath the oak.",
        "Where did the dog hide its treat?",
        "The cat slept beside the fireplace.",
    ),
    (
        "Iris uses a wheelchair and needs step-free entrances.",
        "What accessibility does Iris require?",
        "Iris wants larger windows in the office.",
    ),
    (
        "The laptop battery lasts six hours.",
        "How long can the portable computer run unplugged?",
        "The desktop monitor is six years old.",
    ),
    (
        "Theo cancelled the picnic because of heavy rainfall.",
        "Why did Theo abandon the outdoor meal?",
        "Theo booked a restaurant for his birthday.",
    ),
]


def evaluate(memory):
    targets = []
    for fact, _, distractor in CASES:
        targets.append(memory.remember(fact, reinforce=False).id)
        memory.remember(distractor, reinforce=False)
    rows = []
    for (_, query, _), target in zip(CASES, targets, strict=True):
        results = memory.recall(query, top_k=5, include_archived=False)
        ids = [r.memory.id for r in results]
        rows.append(
            {
                "query": query,
                "rank": ids.index(target) + 1 if target in ids else None,
                "returned": [r.memory.content for r in results],
            }
        )
    return {
        "queries": len(rows),
        "corpus_size": 2 * len(CASES),
        "recall_at_1": sum(r["rank"] == 1 for r in rows) / len(rows),
        "recall_at_5": sum(r["rank"] is not None for r in rows) / len(rows),
        "mrr_at_5": sum(1 / r["rank"] for r in rows if r["rank"]) / len(rows),
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["default", "semantic"], default="default")
    parser.add_argument("--backend", choices=["local", "qdrant"], default="local")
    parser.add_argument("--qdrant-url")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Refusing to overwrite existing results")
    embedder = (
        HashEmbedder()
        if args.mode == "default"
        else SentenceTransformerEmbedder(local_files_only=True)
    )
    with tempfile.TemporaryDirectory(prefix="umem-quality-") as data:
        with MemoryEngine(
            data,
            embedder=embedder,
            vector_backend=args.backend,
            qdrant_url=args.qdrant_url,
            settings=Settings(backup_interval_seconds=0),
        ) as memory:
            report = {
                "mode": args.mode,
                "backend": args.backend,
                "embedding": embedder.fingerprint,
                **evaluate(memory),
            }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}))


if __name__ == "__main__":
    main()
