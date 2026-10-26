import unittest
import json
from pathlib import Path
from unittest.mock import patch
import sys
import tempfile
from contextlib import closing
from types import SimpleNamespace
from presence_service import Config, configure_target, read_session, run


class TargetTests(unittest.TestCase):
    class Port:
        in_waiting = 256
        def __init__(self, chunks):
            self.chunks = iter(chunks)
            self.writes = []
        def write(self, data):
            self.writes.append(data)
            return len(data)
        def read(self, count):
            return next(self.chunks, b"")

    def test_config_validation(self):
        self.assertEqual(Config().target, "phone")
        for value in ("", "BEACON", "both", None, 123):
            with self.assertRaises(ValueError):
                Config(target=value)

    def test_both_targets_fragmentation_and_stale_frames(self):
        for target in ("phone", "beacon"):
            port = self.Port([b"PRESENCE:PRESENT:-20\nTARGET:OK:WRONG\nTARGET:O",
                              b"K:" + target.upper().encode() + b"\r\n"])
            configure_target(port, Config(target=target), OSError, lambda: 0)
            self.assertEqual(port.writes, [f"TARGET:{target.upper()}\n".encode()])

    def test_old_firmware_cannot_confirm_target(self):
        port = self.Port([b"SCAN:OK\nPRESENCE:PRESENT:-20\n"] * 30)
        ticks = iter(i * 0.2 for i in range(100))
        with self.assertRaisesRegex(OSError, "acknowledgement timed out"):
            configure_target(port, Config(target="beacon"), OSError, lambda: next(ticks))
        self.assertGreater(len(port.writes), 1)

    def test_short_write_and_rejection(self):
        port = self.Port([])
        port.write = lambda data: 1
        with self.assertRaisesRegex(OSError, "Short"):
            configure_target(port, Config(), OSError, lambda: 0)
        port = self.Port([b"TARGET:ERROR\n"])
        with self.assertRaisesRegex(OSError, "rejected"):
            configure_target(port, Config(), OSError, lambda: 0)

    def test_disconnect_during_handshake(self):
        port = self.Port([])
        def disconnected(count):
            raise OSError("unplugged")
        port.read = disconnected
        with self.assertRaisesRegex(OSError, "unplugged"):
            configure_target(port, Config(), OSError, lambda: 0)

    def test_shipped_configs(self):
        folder = Path(__file__).parent
        phone = Config(**json.loads((folder / "config.json").read_text()))
        beacon = Config(**json.loads((folder / "config.beacon.json").read_text()))
        self.assertEqual((phone.target, beacon.target), ("phone", "beacon"))
        self.assertNotEqual(phone.database, beacon.database)
        self.assertNotEqual(phone.person, beacon.person)

    def test_run_reconfigures_after_reconnect(self):
        ports = []
        class Session(self.Port):
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def reset_input_buffer(self):
                pass
            def read(self, count):
                try:
                    return next(self.chunks)
                except StopIteration:
                    raise OSError("disconnected")
        def open_port(*args, **kwargs):
            if len(ports) == 2:
                raise KeyboardInterrupt
            port = Session([b"PRESENCE:PRESENT:-20\n", b"TARGET:OK:BEACON\n",
                            b"PRESENCE:PRESENT:-90\n"])
            ports.append(port)
            return port
        fake = SimpleNamespace(Serial=open_port, SerialException=OSError)
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(sys.modules, serial=fake), patch("presence_service.time.sleep"):
                run(Config(target="beacon"), Path(directory) / "events.db")
            import sqlite3
            with closing(sqlite3.connect(Path(directory) / "events.db")) as db:
                rows = db.execute("SELECT zone,previous_zone FROM sensory_events WHERE zone IS NOT NULL").fetchall()
            self.assertEqual(rows, [("very_far", None), ("very_far", None)])
        self.assertEqual([p.writes for p in ports], [[b"TARGET:BEACON\n"]] * 2)

    def test_beacon_trace_approach_retreat_and_absence(self):
        # Scanner's nominal maximum report rate, with sustained steps both ways.
        frames = ([b"PRESENCE:PRESENT:-90\n"] * 20
                  + [b"PRESENCE:PRESENT:-30\n"] * 25
                  + [b"PRESENCE:PRESENT:-90\n"] * 25
                  + [b"PRESENCE:GONE\n"] * 10)
        port = self.Port(frames)
        events = []
        ticks = iter(i * 0.075 for i in range(1000))
        with self.assertRaises(OSError):
            read_session(port, Config(target="beacon"), SimpleNamespace(save=events.append),
                         OSError, lambda: next(ticks))
        self.assertEqual([e["zone"] for e in events if "zone" in e],
                         ["very_far", "very_close", "very_far", "gone"])


if __name__ == "__main__":
    unittest.main()
