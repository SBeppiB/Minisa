"""One-terminal BLE experiment recorder using the actual v3 production service.

No RSSI algorithm is implemented here. Production logging supplies observations,
evidence, health and persisted transitions. Enter completes a step; 'note TEXT'
annotates it; 'quit' stops and writes a summary. Ctrl+C also stops cleanly.
"""
import argparse
from collections import deque
from contextlib import closing
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import queue
import sqlite3
import threading
import time

from hardware_access import diagnose, serial_devices
from presence_service import Config, LOG, ZONES, run

STEPS = [
    ("baseline", "Keep scanner and beacon stationary; observe 30 seconds", 30),
    ("closer", "Move beacon progressively closer with pauses; keep orientation", 10),
    ("hold_near", "Hold stationary, including near a boundary if practical", 30),
    ("farther", "Move beacon progressively farther with pauses", 10),
    ("hold_far", "Hold stationary", 30),
    ("obstruct", "Temporarily obstruct the RF path; observe without moving scanner", 10),
    ("restore", "Remove obstruction and allow signal to settle", 15),
    ("leave_range", "Take beacon out of range; wait at least 10 seconds after loss", 10),
    ("return", "Return beacon to the baseline position; wait for fresh presence", 15),
    ("usb_reconnect", "Unplug scanner for 6 seconds, reconnect SAME board, then wait 10 seconds", 16),
    ("soak", "Keep both boards stationary for the configured soak duration", 600),
]


def utc():
    return datetime.now(timezone.utc).isoformat()


def new_stats():
    return dict(observations=0, accepted=0, acknowledgements=0, online=0, offline=0,
                present=0, gone=0, approach=0, retreat=0, zone_changes=0,
                beacon_ok=0, beacon_errors=0, invalid=0, max_observation_gap=0.0)


def criteria(stats, step, seconds, minimum):
    """Operational checks only; ambiguous physical evidence stays inconclusive."""
    if seconds < minimum:
        return "INCOMPLETE: minimum observation duration not reached"
    if step in ("baseline", "hold_near", "hold_far", "soak"):
        if stats["observations"] < 10:
            return "FAIL: fewer than ten observations"
        if stats["offline"] or stats["gone"] or stats["max_observation_gap"] > 5:
            return "FAIL: stationary signal/link interruption"
        if stats["zone_changes"] > max(2, math.ceil(seconds / 60) * 2):
            return "FAIL: more than two stationary zone transitions per minute"
        return "PASS: stationary stream criteria; review resource metrics separately"
    if step in ("closer", "farther"):
        direction = "approach" if step == "closer" else "retreat"
        return ("PASS: confirmed directional zone crossing observed" if stats[direction]
                else "INCONCLUSIVE: no confirmed crossing; inspect evidence and repeat route")
    if step == "leave_range":
        return "PASS: GONE observed" if stats["gone"] else "FAIL: no GONE transition"
    if step in ("restore", "return"):
        return ("PASS: fresh observations resumed" if stats["observations"] >= 10
                else "FAIL: insufficient recovery observations")
    if step == "usb_reconnect":
        return ("PASS: offline, acknowledgement, online and fresh observations observed"
                if all(stats[k] for k in ("offline", "acknowledgements", "online"))
                and stats["observations"] >= 3 else "FAIL: incomplete reconnect sequence")
    return "REVIEW: compare obstruction/restore traces; RF loss is not automatically a defect"


class Recorder(logging.Handler):
    def __init__(self, folder, max_bytes=512 * 1024 * 1024, clock=time.monotonic):
        super().__init__(logging.DEBUG)
        self.folder, self.clock, self.max_bytes = folder, clock, max_bytes
        self.stream = (folder / "capture.jsonl").open("x", encoding="utf-8")
        self.guard = threading.RLock()
        self.bytes_written = 0
        self.total, self.stage_stats = new_stats(), new_stats()
        self.started = self.stage_started = clock()
        self.stage = "monitor"
        self.completed = []
        self.latest = None
        self.last_observation = None
        self.stage_last_observation = None
        self.last_valid_frame = None
        self.health, self.zone = "unknown", None
        self.recent = deque(maxlen=200)
        self.notices = deque(maxlen=10)
        self.fatal = None

    def write(self, kind, **data):
        with self.guard:
            item = dict(timestamp=utc(), monotonic_seconds=self.clock(), kind=kind,
                        stage=self.stage, **data)
            line = json.dumps(item, ensure_ascii=True) + "\n"
            if self.bytes_written + len(line.encode("utf-8")) > self.max_bytes:
                self.fatal = "Capture size limit reached; stopped rather than deleting evidence"
                raise RuntimeError(self.fatal)
            self.stream.write(line)
            self.stream.flush()
            self.bytes_written += len(line.encode("utf-8"))

    def count(self, key, value=1):
        for stats in (self.total, self.stage_stats):
            stats[key] += value

    def ingest(self, payload, now):
        with self.guard:
            frame = payload.get("frame")
            if frame in ("PRESENCE:PRESENT", "PRESENCE:GONE", "SCAN:OK"):
                self.last_valid_frame = now
            if frame == "PRESENCE:PRESENT":
                self.count("observations")
                if payload["sample_accepted"]:
                    self.count("accepted")
                for stats, previous in ((self.total, self.last_observation),
                                        (self.stage_stats, self.stage_last_observation)):
                    if previous is not None:
                        stats["max_observation_gap"] = max(stats["max_observation_gap"], now - previous)
                self.last_observation = self.stage_last_observation = now
                self.recent.append(now)
                self.latest = payload
                self.zone = payload["current_zone"]
            elif frame in ("INVALID", "OVERLONG"):
                self.count("invalid", payload.get("dropped_frames", 1))
            if payload.get("event_type") == "sensor":
                self.health = payload["state"]
                self.count(self.health)
                if self.health == "offline":
                    self.zone, self.latest = None, None
                    self.recent.clear()
            elif payload.get("event_type") in ("presence", "proximity"):
                self.zone = payload["zone"]
                if payload["event_type"] == "presence":
                    self.count(payload["state"])
                else:
                    self.count("zone_changes")
                    direction = "approach" if ZONES.index(payload["zone"]) < ZONES.index(payload["previous_zone"]) else "retreat"
                    self.count(direction)
                self.notices.append(f"{utc()} transition {payload}")

    def emit(self, record):
        with self.guard:
            payload = None
            if record.msg == "%s" and record.args:
                value = record.args[0] if isinstance(record.args, tuple) else record.args
                if isinstance(value, dict):
                    payload = value
                elif isinstance(value, str):
                    try:
                        payload = json.loads(value)
                    except json.JSONDecodeError:
                        pass
            self.write("production", level=record.levelname, message=record.getMessage(), payload=payload)
            now = self.clock()
            if payload:
                self.ingest(payload, payload.get("monotonic_seconds", now))
            if record.msg == "Scanner target confirmed: %s":
                self.count("acknowledgements")
                self.notices.append(f"{utc()} scanner acknowledged beacon target")

    def finish_stage(self, minimum, completed=True):
        with self.guard:
            elapsed = self.clock() - self.stage_started
            # Include a trailing silence, not just gaps between received samples.
            previous = (self.stage_started if self.stage_last_observation is None
                        else self.stage_last_observation)
            self.stage_stats["max_observation_gap"] = max(
                self.stage_stats["max_observation_gap"], self.clock() - previous)
            result = dict(step=self.stage, seconds=elapsed, completed=completed,
                          stats=dict(self.stage_stats), result=criteria(
                              self.stage_stats, self.stage, elapsed, minimum) if completed else "INCOMPLETE: stopped")
            self.completed.append(result)
            self.write("step_end", result=result)
            return result

    def begin_stage(self, name, instruction):
        with self.guard:
            self.stage, self.stage_started = name, self.clock()
            self.stage_stats, self.stage_last_observation = new_stats(), None
            self.write("step_begin", instruction=instruction)

    def dashboard(self):
        with self.guard:
            now = self.clock()
            age = None if self.last_observation is None else now - self.last_observation
            rate = sum(t >= now - 5 for t in self.recent) / 5
            latest = self.latest or {}
            evidence = "; ".join(f"{c['candidate_zone']} {c['supporting_samples']}/{c['required_samples']} "
                                  f"span={c['support_span_seconds']:.1f}s strong={c['strong_supporting_samples']}"
                                  for c in latest.get("candidates", []) if c["supporting_samples"])
            age_text = "never" if age is None else f"{age:.1f}s"
            stale = age is None or age > 5
            return (f"[{self.stage}] sensor={self.health} presence="
                    f"{'unknown' if self.zone is None else 'gone' if self.zone == 'gone' else 'present'} "
                    f"zone={self.zone} raw={latest.get('raw_rssi')} median={latest.get('rolling_median')} "
                    f"rate(5s)={rate:.1f}/s age={age_text} {'STALE' if stale else 'fresh'} | {evidence}")

    def close(self):
        if hasattr(self, "stream") and not self.stream.closed:
            self.stream.close()
        super().close()


def beacon_monitor(port, recorder, stop):
    import serial
    while not stop.is_set():
        try:
            with serial.Serial(port, 115200, timeout=0.2) as device:
                while not stop.is_set():
                    raw = device.readline(256)
                    if not raw:
                        continue
                    recorder.write("beacon_serial", port=port, raw_hex=raw.hex())
                    with recorder.guard:
                        if raw.strip() == b"BEACON:OK:Minisa-01:100ms:0dBm":
                            recorder.count("beacon_ok")
                        elif b"BEACON:ERROR" in raw:
                            recorder.count("beacon_errors")
        except (serial.SerialException, OSError) as error:
            recorder.write("beacon_unavailable", error=str(error))
            stop.wait(2)


def memory_bytes():
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
            "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
            "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return None
    return counters.WorkingSetSize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scanner", help="Explicitly selected scanner COM port; never auto-flashed")
    parser.add_argument("--beacon", help="Optional beacon USB status port, distinct from scanner")
    parser.add_argument("--discover", action="store_true", help="Read-only multi-method discovery and exit")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.beacon.json"))
    parser.add_argument("--output", type=Path, default=Path("validation-runs"))
    parser.add_argument("--monitor", action="store_true", help="Unguided monitoring instead of the interactive steps")
    parser.add_argument("--seconds", type=float, help="Stop automatically after this many seconds")
    parser.add_argument("--soak-seconds", type=float, default=600)
    parser.add_argument("--max-log-mib", type=int, default=512)
    args = parser.parse_args()
    if args.discover:
        print(json.dumps(diagnose(), indent=2))
        return 0
    if not args.scanner:
        parser.error("select --scanner COMx, or use --discover")
    if args.beacon and args.beacon.upper() == args.scanner.upper():
        parser.error("scanner and beacon ports must differ")
    if (args.seconds is not None and (not math.isfinite(args.seconds) or args.seconds <= 0)
            or not math.isfinite(args.soak_seconds) or args.soak_seconds < 60 or args.max_log_mib < 1):
        parser.error("durations must be positive/finite; soak >= 60s; max-log-mib >= 1")
    config = Config(**json.loads(args.config.read_text(encoding="utf-8")))
    if config.target != "beacon":
        parser.error("physical beacon workflow requires target=beacon")
    config.port = args.scanner
    folder = args.output / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    folder.mkdir(parents=True, exist_ok=False)
    database = folder / "events.db"
    config.database = str(database.resolve())
    (folder / "config.json").write_text(json.dumps(vars(config), indent=2))
    recorder = Recorder(folder, args.max_log_mib * 1024 * 1024)
    recorder.write("start", config=vars(config), devices=serial_devices(),
                   criteria_file=str(Path(__file__).with_name("PHYSICAL_VALIDATION.md")))
    stop = threading.Event()
    failures = []
    old_level, old_propagate = LOG.level, LOG.propagate
    LOG.setLevel(logging.DEBUG)
    LOG.propagate = False
    LOG.addHandler(recorder)

    def worker():
        try:
            run(config, database, debug=True, stop_event=stop)
        except BaseException as error:
            failures.append(f"production service: {type(error).__name__}: {error}")
            stop.set()

    def beacon_worker():
        try:
            beacon_monitor(args.beacon, recorder, stop)
        except BaseException as error:
            failures.append(f"beacon monitor: {type(error).__name__}: {error}")
            stop.set()

    commands = queue.Queue(maxsize=100)
    def input_worker():
        while not stop.is_set():
            try:
                line = input()
            except EOFError:
                return
            commands.put(line)

    threads = [threading.Thread(target=worker, name="production-v3")]
    if args.beacon:
        threads.append(threading.Thread(target=beacon_worker, name="beacon-status"))
    for thread in threads:
        thread.start()
    threading.Thread(target=input_worker, daemon=True, name="operator-input").start()
    steps = list(STEPS)
    steps[-1] = ("soak", f"Keep stationary for at least {args.soak_seconds:g} seconds", args.soak_seconds)
    index = 0
    if not args.monitor:
        recorder.begin_stage(steps[0][0], steps[0][1])
    print(f"Capture: {folder.resolve()}\nEnter completes a step; note TEXT annotates; quit stops.", flush=True)
    if not args.monitor:
        print(steps[0][1], flush=True)
    started = last_display = last_metrics = time.monotonic()
    cpu_start = time.process_time()
    memory_first = memory_peak = memory_bytes()
    try:
        while not stop.wait(0.1):
            now = time.monotonic()
            if args.seconds is not None and now - started >= args.seconds:
                break
            if now - last_display >= 2:
                print(recorder.dashboard(), flush=True)
                with recorder.guard:
                    while recorder.notices:
                        print(recorder.notices.popleft(), flush=True)
                last_display = now
            if now - last_metrics >= 10:
                rss = memory_bytes()
                memory_peak = max(memory_peak or 0, rss or 0) or None
                recorder.write("resources", elapsed_seconds=now - started,
                    cpu_seconds=time.process_time() - cpu_start, working_set_bytes=rss,
                    database_bytes=database.stat().st_size if database.exists() else 0,
                    observation_age_seconds=None if recorder.last_observation is None else now - recorder.last_observation)
                last_metrics = now
            try:
                command = commands.get_nowait().strip()
            except queue.Empty:
                continue
            if command.lower() == "quit":
                break
            if command.startswith("note "):
                recorder.write("annotation", text=command[5:])
            elif command == "" and not args.monitor:
                result = recorder.finish_stage(steps[index][2])
                print(result["result"], flush=True)
                index += 1
                if index == len(steps):
                    break
                recorder.begin_stage(steps[index][0], steps[index][1])
                print(steps[index][1], flush=True)
            else:
                print("Commands: Enter, note TEXT, quit", flush=True)
    except KeyboardInterrupt:
        pass
    except BaseException as error:
        failures.append(f"recorder: {type(error).__name__}: {error}")
    finally:
        stop.set()
        for thread in threads:
            thread.join(config.link_timeout_seconds + 3)
            if thread.is_alive():
                failures.append(f"Worker did not stop: {thread.name}")
        LOG.removeHandler(recorder)
        LOG.setLevel(old_level)
        LOG.propagate = old_propagate
        if not args.monitor and index < len(steps):
            try:
                recorder.finish_stage(steps[index][2], completed=False)
            except (OSError, RuntimeError) as error:
                failures.append(str(error))
        recorder.close()
    integrity, rows = None, 0
    if database.exists():
        try:
            with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
                rows = db.execute("SELECT count(*) FROM sensory_events").fetchone()[0]
        except sqlite3.Error as error:
            failures.append(f"summary database check: {error}")
    memory_final = memory_bytes()
    memory_peak = max(memory_peak or 0, memory_final or 0) or None
    summary = dict(kind="validation_capture", scanner=args.scanner, beacon=args.beacon,
        seconds=time.monotonic() - started, stats=recorder.total, steps=recorder.completed,
        database_integrity=integrity, database_rows=rows, capture_bytes=recorder.bytes_written,
        working_set_first_bytes=memory_first, working_set_peak_bytes=memory_peak,
        working_set_final_bytes=memory_final, cpu_seconds=time.process_time() - cpu_start,
        failures=failures, beacon_boot="observed advertising status" if recorder.total["beacon_ok"] else "NOT VERIFIED",
        uuid_reception="scanner-reported matching observations" if recorder.total["acknowledgements"] and recorder.total["observations"] else "NOT VERIFIED",
        physical_validation="REQUIRES checklist review and repeated experiments; not automatically certified")
    (folder / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Summary: {folder / 'summary.json'}\n{json.dumps(summary, indent=2)}", flush=True)
    return 1 if failures or not recorder.total["observations"] or integrity != "ok" else 0


if __name__ == "__main__":
    raise SystemExit(main())
