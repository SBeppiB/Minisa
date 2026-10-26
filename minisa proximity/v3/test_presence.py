import sqlite3
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import sys
import types
from contextlib import closing

from presence_service import (Config, EventStore, LineBuffer, Proximity,
                              observation_debug, parse_line, read_session, run)


class DebugTests(unittest.TestCase):
    def test_snapshot_tracks_evidence_without_mutating_state(self):
        p = Proximity(Config())
        p.observe("present", -85, 0)
        p.observe("present", -74, 0.25)
        before = (p.zone, p.filtered_rssi, list(p.samples), p.gone_since)
        data = observation_debug(p, "very_far", -74, 0.25, True)
        candidate = data["candidates"][2]
        self.assertEqual(data["rolling_median"], -79.5)
        self.assertEqual(candidate["candidate_zone"], "far")
        self.assertEqual(candidate["supporting_samples"], 1)
        self.assertEqual(candidate["required_samples"], 3)
        self.assertTrue(candidate["sample_counted_as_evidence"])
        self.assertFalse(candidate["median_gate_passed"])
        self.assertFalse(candidate["confirmed"])
        self.assertEqual(before, (p.zone, p.filtered_rssi, list(p.samples), p.gone_since))
        for now in (0.5, 0.75, 1.0):
            p.observe("present", -74, now)
        candidate = observation_debug(p, "very_far", -74, 1, True)["candidates"][2]
        self.assertEqual(candidate["supporting_samples"], 4)
        self.assertEqual(candidate["required_samples"], 4)
        self.assertEqual(candidate["pending_seconds"], 0.75)
        self.assertFalse(candidate["confirmed"])  # Timing still blocks.
        p.observe("present", -74, 1.25)
        candidate = observation_debug(p, "very_far", -74, 1.25, True)["candidates"][2]
        self.assertTrue(candidate["confirmed"])
        self.assertEqual(p.zone, "far")

    def test_strong_duplicate_and_expired_evidence(self):
        p = Proximity(Config())
        p.observe("present", -85, 0)
        for now in (0.25, 0.6, 0.9):
            p.observe("present", -65, now)
        candidate = observation_debug(p, "very_far", -65, 0.9, True)["candidates"][2]
        self.assertTrue(candidate["confirmed"])
        self.assertEqual(candidate["strong_supporting_samples"], 3)
        self.assertLess(candidate["support_span_seconds"], candidate["confirm_seconds"])
        p.observe("present", -30, 0.9)
        data = observation_debug(p, p.zone, -30, 0.9, False)
        self.assertEqual(data["sample_status"], "non_increasing_timestamp")
        self.assertTrue(all(not c["sample_counted_as_evidence"] for c in data["candidates"]))
        p.observe("present", -85, 10)
        data = observation_debug(p, "far", -85, 10, True)
        self.assertEqual(data["window_samples"], 1)
        self.assertEqual(data["candidates"][2]["pending_seconds"], 0)

    def test_serial_logging_is_opt_in_and_never_stored(self):
        class Port:
            in_waiting = 256
            def __init__(self):
                self.chunks = iter([b"PRESENCE:PRE", b"SENT:-85\r\nSCAN:OK\n",
                    b"bad\nPRESENCE:PRESENT:127\nPRESENCE:PRESENT:-65\n",
                    b"PRESENCE:PRESENT:-65\n"])
            def read(self, count):
                try:
                    return next(self.chunks)
                except StopIteration:
                    raise OSError("done")
        results = []
        with tempfile.TemporaryDirectory() as directory:
            for debug in (False, True):
                store = EventStore(Path(directory) / f"{debug}.db", Config())
                with patch("presence_service.LOG.debug") as log:
                    with self.assertRaises(OSError):
                        read_session(Port(), Config(), store, OSError, lambda: 0, debug=debug)
                    records = [json.loads(call.args[1]) for call in log.call_args_list]
                results.append(store.db.execute(
                    "SELECT event_type,state,zone,rssi FROM sensory_events").fetchall())
                store.close()
                if not debug:
                    self.assertEqual(records, [])
                else:
                    self.assertEqual([r["frame"] for r in records],
                        ["PRESENCE:PRESENT", "SCAN:OK", "INVALID", "INVALID",
                         "PRESENCE:PRESENT", "PRESENCE:PRESENT"])
                    self.assertTrue(all(r["timestamp"].endswith("+00:00") for r in records))
                    self.assertEqual(records[0]["sample_status"], "baseline")
                    self.assertFalse(records[-1]["sample_accepted"])
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(results[1]), 2)  # Online and initial presence only.


class AlgorithmTests(unittest.TestCase):
    def settled(self, rssi, config=None, interval=0.25):
        p = Proximity(config or Config())
        for i in range(int(3 / interval) + 1):
            p.observe("present", rssi, i * interval)
        return p

    def test_sustained_steps_latency_in_time_and_observations(self):
        # Both directions across every boundary, including small changes that
        # would never escape v2's 3 dB hysteresis, and full multi-zone steps.
        for interval in (0.15, 0.25, 0.5):
            for start, end in [(-85, -69), (-69, -85), (-90, -30), (-30, -90)] + [
                    pair for t in (-45, -60, -75)
                    for pair in ((t-2, t+1), (t+2, t-1))]:
                with self.subTest(interval=interval, start=start, end=end):
                    p = self.settled(start, interval=interval)
                    baseline = p.samples[-1][0]
                    target = ("very_close", "nearby", "far", "very_far")[
                        sum(end < t for t in p.c.thresholds)]
                    events = []
                    for count in range(1, int(3 / interval) + 2):
                        elapsed = count * interval
                        event = p.observe("present", end, baseline + elapsed)
                        if event:
                            events.append(event)
                        if p.zone == target:
                            break
                    self.assertEqual(p.zone, target)
                    self.assertGreaterEqual(count, p.c.min_observations)
                    self.assertLessEqual(elapsed, 3)
                    self.assertLessEqual(count, int(3 / interval) + 1)
                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0]["rssi"], end)

    def test_sustained_fast_approach_and_retreat_ramps(self):
        for start, values, target in (
                (-90, [-85, -80, -75, -70, -65, -60, -55, -50, -45, -40, -35, -30], "very_close"),
                (-30, [-35, -40, -45, -50, -55, -60, -65, -70, -75, -80, -85, -90], "very_far")):
            p = self.settled(start)
            events = []
            for i, value in enumerate(values + [values[-1]] * 12, 1):
                event = p.observe("present", value, 3 + i * 0.25)
                if event:
                    events.append((i * 0.25, event["zone"]))
            self.assertEqual(p.zone, target)
            self.assertLessEqual(events[0][0], 3)
            # Last boundary was crossed by the raw ramp at 2.5 seconds.
            self.assertLessEqual(events[-1][0] - 2.5, 3)

    def test_single_large_outlier(self):
        for start, outlier in [(start, outlier)
                               for start in (-90, -76, -74, -61, -59, -46, -44, -30)
                               for outlier in (-20, -110)]:
            p = self.settled(start)
            initial = p.zone
            for i, value in enumerate([outlier] + [start] * 20, 1):
                self.assertIsNone(p.observe("present", value, 3 + i * 0.25))
                self.assertEqual(p.zone, initial)
            # Also protect the sparse startup history.
            p = Proximity(Config())
            p.observe("present", start, 0)
            self.assertIsNone(p.observe("present", outlier, 0.5))
            self.assertIsNone(p.observe("present", start, 1))

    def test_sustained_movement_tolerates_one_opposing_packet(self):
        for start, end, target in ((-85, -65, "far"), (-65, -85, "very_far")):
            p = self.settled(start)
            events = []
            for i, value in enumerate([end, end, start] + [end] * 9, 1):
                event = p.observe("present", value, 3 + i * 0.25)
                if event:
                    events.append((i * 0.25, event))
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0][1]["zone"], target)
            self.assertLessEqual(events[0][0], 3)

    def test_brief_excursion_cancels_and_reversal_confirms(self):
        p = self.settled(-85)
        for i, value in enumerate([-65, -65] + [-85] * 8, 1):
            self.assertIsNone(p.observe("present", value, 3 + i * 0.25))
        for i in range(1, 13):
            p.observe("present", -65, 5.5 + i * 0.25)
        self.assertEqual(p.zone, "far")
        for i in range(1, 13):
            p.observe("present", -85, 8.5 + i * 0.25)
        self.assertEqual(p.zone, "very_far")

    def test_noise_at_every_threshold(self):
        for threshold in (-45, -60, -75):
            for initial in (threshold - 1, threshold + 1):
                for interval in (0.15, 0.25, 0.5):
                    p = self.settled(initial, interval=interval)
                    baseline = p.samples[-1][0]
                    zone = p.zone
                    for i in range(1, 101):
                        value = threshold + (-1 if i % 2 else 1)
                        self.assertIsNone(p.observe("present", value, baseline + i * interval))
                        self.assertEqual(p.zone, zone)

    def test_strong_changes_use_shorter_confirmation(self):
        times = []
        for end in (-74, -65):
            p = Proximity(Config())
            p.observe("present", -85, 0)
            for i in range(1, 30):
                event = p.observe("present", end, i * 0.15)
                if event:
                    times.append(i * 0.15)
                    break
        self.assertEqual(len(times), 2)
        self.assertLess(times[1], times[0])

    def test_stale_evidence_and_serial_burst_do_not_confirm(self):
        p = self.settled(-90)
        for i in range(20):
            self.assertIsNone(p.observe("present", -30, 4))
        self.assertIsNone(p.observe("present", -30, 10))
        self.assertIsNone(p.observe("heartbeat", None, 11))
        self.assertEqual(p.zone, "very_far")
        self.assertIsNone(p.observe("present", -30, 10.5))
        self.assertEqual(p.observe("present", -30, 11)["zone"], "very_close")

    def test_config_changes_confirmation(self):
        p = self.settled(-85, Config(confirm_seconds=2, strong_confirm_seconds=2,
                                   window_seconds=3, min_observations=5))
        for i in range(1, 9):
            self.assertIsNone(p.observe("present", -65, 3 + i * 0.25))
        events = [p.observe("present", -65, 3 + i * 0.25) for i in range(9, 14)]
        self.assertTrue(any(events))
        self.assertEqual(p.zone, "far")

    def test_departure_confirmation_and_recovery(self):
        p = Proximity(Config())
        p.observe("present", -50, 0)
        self.assertIsNone(p.observe("gone", None, 4))
        self.assertIsNone(p.observe("gone", None, 4.2))
        self.assertIsNone(p.observe("present", -50, 4.3))
        self.assertIsNone(p.observe("gone", None, 9))
        event = p.observe("gone", None, 10)
        self.assertEqual(event["previous_zone"], "nearby")
        self.assertEqual(event["zone"], "gone")
        self.assertIsNone(event["rssi"])
        self.assertIsNone(p.observe("gone", None, 11))
        self.assertEqual(p.observe("present", -85, 12)["zone"], "very_far")

    def test_heartbeat_does_not_infer_absence(self):
        p = Proximity(Config())
        p.observe("present", -50, 0)
        self.assertIsNone(p.observe("heartbeat", None, 100))
        self.assertEqual(p.zone, "nearby")

    def test_config_rejects_invalid_values(self):
        for values in ({"min_observations": 2}, {"min_observations": 3.5},
                       {"agreement": 0.5}, {"agreement": 1.1},
                       {"window_seconds": float("nan")}, {"confirm_seconds": 0},
                       {"strong_confirm_seconds": 2}, {"window_seconds": 0.5},
                       {"hysteresis_db": -1},
                       {"thresholds": [-60, -45, -75]}, {"reconnect_seconds": 0}):
            with self.assertRaises(ValueError):
                Config(**values)


class TransportAndStorageTests(unittest.TestCase):
    def test_protocol_and_framing(self):
        b = LineBuffer()
        self.assertEqual(b.feed(b"PRESENCE:PRE"), [])
        lines = b.feed(b"SENT:-42\r\nPRESENCE:GONE\nSCAN:OK\n")
        self.assertEqual([parse_line(x) for x in lines],
                         [("present", -42), ("gone", None), ("heartbeat", None)])
        for line in (b"PRESENCE:PRESENT:127", b"PRESENCE:PRESENT:-128", b"Signal: -42 dBm", b"\xff"):
            self.assertIsNone(parse_line(line))
        self.assertEqual(b.feed(b"x" * 1000 + b"\nSCAN:OK\n"), [b"SCAN:OK"])

    def test_database_transitions_and_parameterization(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            c = Config(person="SBeppi'); DROP TABLE sensory_events;--")
            store = EventStore(path, c)
            p = Proximity(c)
            for i, rssi in enumerate([-80] * 100 + [-30] * 100):
                event = p.observe("present", rssi, i * 0.25)
                if event:
                    store.save(event)
            store.save(dict(event_type="sensor", state="offline"))
            store.save(dict(event_type="sensor", state="offline"))
            rows = store.db.execute("SELECT * FROM sensory_events").fetchall()
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0][3], c.person)
            self.assertTrue(rows[0][1].endswith("+00:00"))
            self.assertEqual(rows[1][7], "very_far")
            self.assertEqual(len(store.db.execute("PRAGMA index_list(sensory_events)").fetchall()), 3)
            store.close()
            with self.assertRaises(sqlite3.ProgrammingError):
                store.db.execute("SELECT 1")
            EventStore(path, c).close()  # Schema initialization is idempotent.

    def test_silent_connection_times_out_without_departure(self):
        class Port:
            in_waiting = 0
            def read(self, count):
                return b""
        events = []
        store = types.SimpleNamespace(save=events.append)
        clock = iter([0, 6])
        with self.assertRaises(OSError):
            read_session(Port(), Config(), store, OSError, lambda: next(clock))
        self.assertEqual(events, [])

    def test_reconnect_and_shutdown_close_database(self):
        class SerialError(OSError):
            pass
        attempts = []
        def open_port(*args, **kwargs):
            attempts.append(1)
            if len(attempts) < 3:
                raise SerialError("unplugged")
            raise KeyboardInterrupt
        fake = types.SimpleNamespace(Serial=open_port, SerialException=SerialError)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            with patch.dict(sys.modules, serial=fake), patch("presence_service.time.sleep"), \
                 patch.object(EventStore, "close", autospec=True, side_effect=EventStore.close) as close:
                run(Config(), path)
                close.assert_called_once()
            self.assertEqual(len(attempts), 3)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT state FROM sensory_events").fetchall(), [("offline",)])

    def test_valid_session_then_disconnect(self):
        class Port:
            in_waiting = 256
            chunks = iter([b"PRESENCE:PRE", b"SENT:-80\nSCAN:OK\n",
                           b"PRESENCE:PRESENT:-30\n"])
            def read(self, count):
                try:
                    return next(self.chunks)
                except StopIteration:
                    raise OSError("unplugged")
        events = []
        with self.assertRaises(OSError):
            read_session(Port(), Config(), types.SimpleNamespace(save=events.append),
                         OSError, lambda: 0)
        self.assertEqual([e.get("zone") for e in events], [None, "very_far"])
        self.assertEqual(events[0]["state"], "online")


if __name__ == "__main__":
    unittest.main()
