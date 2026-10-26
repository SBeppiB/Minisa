"""Accelerated synthetic stability check; NOT a physical RF/USB soak test."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import random
import tempfile
import time
import tracemalloc

from presence_service import Config, EventStore, Proximity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=24)
    args = parser.parse_args()
    if not 0 < args.hours <= 168:
        parser.error("hours must be in (0, 168]")
    rng = random.Random(20261008)
    model = Proximity(Config(target="beacon"))
    count = int(args.hours * 3600 / 0.15)
    max_window = 0
    started = time.perf_counter()
    cpu_started = time.process_time()
    tracemalloc.start()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "synthetic.db"
        with closing(EventStore(path, model.c)) as store:
            for i in range(count):
                # Stationary just above the -75 boundary, with symmetric jitter
                # and isolated extreme spikes. This is a deliberately specified
                # trace, not a model of all real-world RF conditions.
                rssi = -74 + rng.choice((-1, 0, 1))
                if i and i % 997 == 0:
                    rssi = rng.choice((-110, -20))
                event = model.observe("present", rssi, i * 0.15)
                if event:
                    store.save(event)
                max_window = max(max_window, len(model.samples))
            rows = store.db.execute("SELECT count(*) FROM sensory_events").fetchone()[0]
            integrity = store.db.execute("PRAGMA integrity_check").fetchone()[0]
            current, peak = tracemalloc.get_traced_memory()
        report = dict(kind="accelerated_synthetic_not_hardware", hours=args.hours,
                      observations=count, events=rows, final_zone=model.zone,
                      max_window_samples=max_window, database_bytes=path.stat().st_size,
                      integrity=integrity, traced_current_bytes=current,
                      traced_peak_bytes=peak, cpu_seconds=time.process_time() - cpu_started,
                      elapsed_seconds=time.perf_counter() - started)
    tracemalloc.stop()
    print(json.dumps(report, indent=2))
    if rows != 1 or max_window > 11 or integrity != "ok" or model.zone != "far":
        raise SystemExit("Synthetic stability check failed")


if __name__ == "__main__":
    main()
