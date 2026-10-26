"""Read-only FTS diagnostic against an existing speed-benchmark SQLite snapshot."""

import argparse
import json
import math
import sqlite3
import statistics
import time
from pathlib import Path

from recall_benchmark import queries

from universal_memory.embeddings import tokens


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Refusing to overwrite results")
    db = sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        count = db.execute("SELECT count(*) FROM memories").fetchone()[0]
        timings = []
        for i, query in enumerate(queries()):
            expression = " OR ".join('"' + w + '"' for w in dict.fromkeys(tokens(query)))
            started = time.perf_counter()
            rows = db.execute(
                """SELECT m.id FROM memory_fts f JOIN memories m ON m.rowid=f.rowid
                WHERE memory_fts MATCH ? AND m.namespace=? AND m.tier IN ('ACTIVE','PRIORITY')
                ORDER BY rank LIMIT 80""",
                (expression, "default"),
            ).fetchall()
            elapsed = (time.perf_counter() - started) * 1000
            assert len(rows) <= 80
            if i >= 10:
                timings.append(elapsed)
        report = {
            "measurement": "SQLite FTS only; not full recall or server performance",
            "database": str(args.database),
            "memories": count,
            "median_fts_ms": statistics.median(timings),
            "p95_fts_ms": sorted(timings)[math.ceil(len(timings) * 0.95) - 1],
            "latencies_ms": timings,
        }
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k != "latencies_ms"}))
    finally:
        db.close()


if __name__ == "__main__":
    main()
