"""Regressions for transport boundaries and restart semantics."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from presence_service import Config, EventStore, Proximity, configure_target, read_session


class BytePort:
    def __init__(self, data):
        self.data = bytearray(data)

    @property
    def in_waiting(self):
        return len(self.data)

    def write(self, data):
        return len(data)

    def read(self, count):
        if not self.data:
            raise OSError("end of input")
        result = bytes(self.data[:count])
        del self.data[:count]
        return result


class HardeningTests(unittest.TestCase):
    def test_rolling_median_uses_only_recent_samples(self):
        model = Proximity(Config())
        for now, rssi in ((0, -90), (0.25, -40), (0.5, -70), (0.75, -80)):
            model.observe("present", rssi, now)
        self.assertEqual(model.filtered_rssi, -75)
        model.observe("present", -50, 2)
        self.assertEqual(list(model.samples), [(0.5, -70), (0.75, -80), (2, -50)])
        self.assertEqual(model.filtered_rssi, -70)

    def test_overlong_frame_is_bounded_and_diagnosed(self):
        port = BytePort(b"x" * 1000 + b"\nSCAN:OK\n")
        with patch("presence_service.LOG.debug") as log:
            with self.assertRaises(OSError):
                read_session(port, Config(), SimpleNamespace(save=lambda e: None),
                             OSError, lambda: 0, debug=True)
        records = [json.loads(call.args[1]) for call in log.call_args_list]
        self.assertEqual([r["frame"] for r in records], ["OVERLONG", "SCAN:OK"])
        self.assertEqual(records[0]["dropped_frames"], 1)

    def test_malformed_traffic_does_not_keep_sensor_online(self):
        class NoisePort:
            in_waiting = 4
            def read(self, count):
                return b"bad\n"
        ticks = iter(range(20))
        events = []
        with self.assertRaisesRegex(OSError, "No valid sensor frames"):
            read_session(NoisePort(), Config(), SimpleNamespace(save=events.append),
                         OSError, lambda: next(ticks))
        self.assertEqual(events, [])

    def test_ack_preserves_following_observation_and_partial_line(self):
        for suffix in (b"PRESENCE:PRESENT:-82\n", b"PRESENCE:PRE"):
            with self.subTest(suffix=suffix):
                port = BytePort(b"PRESENCE:PRESENT:-20\nTARGET:OK:BEACON\n" + suffix)
                configure_target(port, Config(target="beacon"), OSError, lambda: 0)
                self.assertEqual(bytes(port.data), suffix)

    def test_one_serial_batch_cannot_accumulate_elapsed_evidence(self):
        port = BytePort(b"PRESENCE:PRESENT:-90\n" + b"PRESENCE:PRESENT:-30\n" * 8)
        ticks = iter(i * 0.4 for i in range(100))
        events = []
        with self.assertRaises(OSError):
            read_session(port, Config(), SimpleNamespace(save=events.append),
                         OSError, lambda: next(ticks))
        self.assertEqual([e["zone"] for e in events if "zone" in e], ["very_far"])

    def test_gone_and_invalid_frames_have_debug_evidence(self):
        port = BytePort(b"bad\xff\nPRESENCE:GONE\n")
        with patch("presence_service.LOG.debug") as log:
            with self.assertRaises(OSError):
                read_session(port, Config(), SimpleNamespace(save=lambda e: None),
                             OSError, lambda: 0, debug=True)
        records = [json.loads(call.args[1]) for call in log.call_args_list]
        self.assertEqual([r["frame"] for r in records], ["INVALID", "PRESENCE:GONE"])
        self.assertEqual(records[0]["raw_hex"], b"bad\xff".hex())
        self.assertEqual(records[1]["gone_pending_seconds"], 0)

    def test_restart_records_new_baseline_without_reusing_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            for rssi in (-30, -90):
                with closing(EventStore(path, Config())) as store:
                    store.save(Proximity(Config()).observe("present", rssi, 0))
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute(
                    "SELECT zone,previous_zone FROM sensory_events ORDER BY id"
                ).fetchall(), [("very_close", None), ("very_far", None)])
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone(), ("ok",))

    def test_database_failure_propagates(self):
        with closing(EventStore(":memory:", Config())) as store:
            store.db.execute("DROP TABLE sensory_events")
            with self.assertRaises(sqlite3.OperationalError):
                store.save(dict(event_type="sensor", state="online"))
            self.assertIsNone(store.sensor_state)


if __name__ == "__main__":
    unittest.main()
