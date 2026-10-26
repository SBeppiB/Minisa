import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import sys
import types
from contextlib import closing

from presence_service import Config, EventStore, LineBuffer, Proximity, parse_line, read_session, run


class AlgorithmTests(unittest.TestCase):
    def test_ema(self):
        p = Proximity(Config())
        p.observe("present", -65, 0)
        p.observe("present", -60, 1)
        self.assertAlmostEqual(p.ema, -63)

    def test_noise_at_every_threshold(self):
        for threshold in (-45, -60, -75):
            for initial in (threshold - 1, threshold + 1):
                p = Proximity(Config(alpha=1, fast_alpha=1))
                p.observe("present", initial, 0)
                for i in range(100):
                    self.assertIsNone(p.observe("present", threshold + (-1 if i % 2 else 1), i))

    def test_sudden_approach_skips_zones(self):
        p = Proximity(Config())
        p.observe("present", -90, 0)
        event = p.observe("present", -30, 0.15)
        self.assertEqual(event["zone"], "very_close")
        self.assertEqual(event["previous_zone"], "very_far")
        self.assertAlmostEqual(event["rssi"], -39)

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
        for values in ({"alpha": 0}, {"alpha": float("nan")},
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
            for rssi in [-80] * 100 + [-30] * 100:
                event = p.observe("present", rssi, 0)
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
        self.assertEqual([e.get("zone") for e in events], [None, "very_far", "very_close"])
        self.assertEqual(events[0]["state"], "online")


if __name__ == "__main__":
    unittest.main()
