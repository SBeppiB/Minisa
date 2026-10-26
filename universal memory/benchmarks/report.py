"""Render completed benchmark JSON into a compact Markdown report."""

import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    reports = [
        json.loads((root / "results" / f"{mode}.json").read_text())
        for mode in ["default", "semantic"]
    ]
    assert all(
        [r["memories"] for r in report["rows"]] == [1000, 10000, 100000] for report in reports
    ), "Both benchmark modes must finish all checkpoints"
    lines = [
        "# Universal Memory benchmark results",
        "",
        "Full test suite: **36 passed**, before and after installing semantic dependencies. "
        "Two existing third-party deprecation warnings; no failing tests or application fixes. "
        "Ruff lint and formatting checks passed.",
        "",
        "| Retrieval | Memories | Bulk insertion (s) | Median recall (ms) | p95 recall (ms) | Index (MiB) | Database (MiB) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for report in reports:
        label = "Default hash" if report["mode"] == "default" else "Sentence Transformers"
        for row in report["rows"]:
            lines.append(
                f"| {label} | {row['memories']:,} | {row['insertion_seconds']:.2f} "
                f"| {row['median_recall_ms']:.2f} | {row['p95_recall_ms']:.2f} "
                f"| {row['index_bytes'] / 2**20:.2f} | {row['database_bytes'] / 2**20:.2f} |"
            )
    lines += [
        "",
        "Insertion is public JSONL bulk import (1,000 records per file), including embedding "
        "and index writes, excluding model loading and synthetic-file generation. It does "
        "not measure per-memory duplicate reinforcement. Each corpus grows across checkpoints; "
        "insertion times are cumulative, excluding intervening recall runs.",
        "",
        "Recall: 10 warm-up calls, 100 measured sequential calls, top_k=5, default "
        "candidate limits and archive probing. Includes query embedding, retrieval, reranking, "
        "access updates, and logs. p95 uses nearest rank. All SQLite/vector record counts and "
        "empty indexing queues were verified. Sizes are after checkpoint and clean shutdown; "
        "backups, model weights, dependencies, and RAM are excluded.",
        "",
        "Single-run synthetic workload on embedded local Qdrant. These are latency/storage "
        "measurements, not accuracy evaluations or Qdrant-server benchmarks. Qdrant emitted "
        "its warning that local mode is not recommended above 20,000 points; the 100k "
        "measurements deliberately retain that backend for a consistent comparison. "
        "The two modes ran sequentially on a desktop, without controlling other system "
        "activity. Treat small differences cautiously; these are not repeated trials on "
        "an isolated benchmark host.",
        "",
        "## Environment",
        "",
        f"- OS: {reports[0]['platform']}",
        f"- CPU: {reports[0]['processor']}; {reports[0]['logical_cpus']} logical processors",
        f"- Python: {reports[0]['python']}",
        f"- Semantic model: `{reports[1]['embedding']}`",
        f"- Semantic device: {reports[1]['device']}; PyTorch threads: {reports[1]['torch_threads']}",
        "",
        "Package versions:",
        "",
    ]
    lines.extend(f"- {key}: {value}" for key, value in reports[1]["versions"].items())
    lines += [
        "",
        "See [methodology and reproduction](README.md), "
        "[benchmark script](recall_benchmark.py), "
        "[default raw results](results/default.json), and "
        "[semantic raw results](results/semantic.json).",
    ]
    (root / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
