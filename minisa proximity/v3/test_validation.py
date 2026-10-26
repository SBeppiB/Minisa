"""Validation tooling tests: fake transport only, never physical-validation claims."""
from contextlib import closing
import json
import logging
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from presence_service import Config, LOG, read_session, run
from validate_hardware import Recorder, criteria, main, new_stats
from hardware_access import serial_devices, diagnose


class ValidationTests(unittest.TestCase):
    def test_production_path_capture_ack_frames_sqlite_and_disconnect(self):
        stop = threading.Event()
        class Port:
            def __init__(self, *args, **kwargs):
                self.data = bytearray(b"TARGET:OK:BEACON\nPRESENCE:PRESENT:-80\nSCAN:OK\n")
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def reset_input_buffer(self):
                pass
            @property
            def in_waiting(self):
                return len(self.data)
            def write(self, data):
                return len(data)
            def read(self, size):
                if not self.data:
                    stop.set()
                    raise OSError("test disconnect")
                data = bytes(self.data[:size])
                del self.data[:size]
                return data
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            recorder = Recorder(folder)
            old_level = LOG.level
            LOG.addHandler(recorder)
            LOG.setLevel(logging.DEBUG)
            try:
                with patch.dict(sys.modules, serial=SimpleNamespace(Serial=Port, SerialException=OSError)):
                    run(Config(target="beacon"), folder / "events.db", debug=True, stop_event=stop)
            finally:
                LOG.removeHandler(recorder)
                LOG.setLevel(old_level)
                recorder.close()
            self.assertEqual(recorder.total["acknowledgements"], 1)
            self.assertEqual(recorder.total["observations"], 1)
            self.assertEqual(recorder.total["offline"], 1)
            self.assertIsNone(recorder.zone)
            records = [json.loads(line) for line in (folder / "capture.jsonl").read_text().splitlines()]
            self.assertTrue(any(r.get("payload", {}).get("raw_rssi") == -80 for r in records if r.get("payload")))
            with closing(sqlite3.connect(folder / "events.db")) as db:
                self.assertEqual(db.execute("SELECT state FROM sensory_events ORDER BY id").fetchall(),
                                 [("online",), ("present",), ("offline",)])

    def test_stop_event_exits_read_without_reading(self):
        stop = threading.Event()
        stop.set()
        read_session(None, Config(), None, OSError, stop_event=stop)

    def test_stop_interrupts_retry_and_closes_database(self):
        stop = threading.Event()
        def unavailable(*args, **kwargs):
            stop.set()
            raise OSError("test unplugged")
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(sys.modules, serial=SimpleNamespace(Serial=unavailable, SerialException=OSError)):
                run(Config(), Path(directory) / "events.db", stop_event=stop)
            with closing(sqlite3.connect(Path(directory) / "events.db")) as db:
                self.assertEqual(db.execute("SELECT state FROM sensory_events").fetchall(), [("offline",)])

    def test_dashboard_freshness_rate_and_offline_unknown(self):
        now = [10.0]
        with tempfile.TemporaryDirectory() as directory:
            with closing(Recorder(Path(directory), clock=lambda: now[0])) as recorder:
                recorder.ingest(dict(frame="PRESENCE:PRESENT", sample_accepted=True,
                                     current_zone="far", raw_rssi=-80, rolling_median=-80), 10)
                self.assertIn("age=0.0s fresh", recorder.dashboard())
                now[0] = 16
                self.assertIn("STALE", recorder.dashboard())
                self.assertIn("rate(5s)=0.0/s", recorder.dashboard())
                recorder.ingest(dict(event_type="sensor", state="offline"), 16)
                self.assertIn("presence=unknown", recorder.dashboard())

    def test_step_annotations_and_incomplete_result(self):
        now = [10.0]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with closing(Recorder(folder, clock=lambda: now[0])) as recorder:
                recorder.begin_stage("baseline", "hold")
                recorder.write("annotation", text="beacon on desk")
                now[0] = 12
                result = recorder.finish_stage(30)
                self.assertTrue(result["result"].startswith("INCOMPLETE"))
            records = [json.loads(line) for line in (folder / "capture.jsonl").read_text().splitlines()]
            self.assertEqual([r["kind"] for r in records], ["step_begin", "annotation", "step_end"])

    def test_capture_limit_does_not_silently_rotate_away_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            with closing(Recorder(Path(directory), max_bytes=1)) as recorder:
                with self.assertRaisesRegex(RuntimeError, "Capture size limit"):
                    recorder.write("test")
                self.assertIsNotNone(recorder.fatal)

    def test_stationary_criteria_reject_gaps_and_flapping(self):
        stats = new_stats()
        stats["observations"] = 100
        self.assertTrue(criteria(stats, "baseline", 30, 30).startswith("PASS"))
        stats["zone_changes"] = 3
        self.assertTrue(criteria(stats, "baseline", 30, 30).startswith("FAIL"))
        stats["zone_changes"] = 0
        stats["max_observation_gap"] = 6
        self.assertTrue(criteria(stats, "baseline", 30, 30).startswith("FAIL"))

    def test_missing_movement_is_inconclusive_and_reconnect_requires_all_evidence(self):
        stats = new_stats()
        self.assertTrue(criteria(stats, "closer", 20, 10).startswith("INCONCLUSIVE"))
        self.assertTrue(criteria(stats, "leave_range", 20, 10).startswith("FAIL"))
        stats.update(observations=10, offline=1, online=1)
        self.assertTrue(criteria(stats, "usb_reconnect", 20, 16).startswith("FAIL"))
        stats["acknowledgements"] = 1
        self.assertTrue(criteria(stats, "usb_reconnect", 20, 16).startswith("PASS"))

    def test_discovery_is_metadata_only(self):
        device = SimpleNamespace(device="COM99", description="USB", hwid="USB VID:PID=303A:1001",
            vid=0x303A, pid=0x1001, serial_number="test", manufacturer="Espressif", location="1")
        with patch("serial.tools.list_ports.comports", return_value=[device]):
            result = serial_devices()
        self.assertTrue(result[0]["candidate"])
        self.assertNotIn("role", result[0])

    def test_diagnostics_preserve_access_errors(self):
        with patch("hardware_access.serial_devices", return_value=[]), \
             patch("hardware_access.os.name", "nt"), \
             patch("hardware_access.subprocess.run", side_effect=PermissionError("denied")):
            report = diagnose()
        self.assertEqual(len(report["methods"]), 5)
        self.assertTrue(all("denied" in item["error"] for item in report["methods"]))

    def test_monitor_without_hardware_writes_failed_summary(self):
        def unavailable(*args, **kwargs):
            raise OSError("no hardware in test")
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(sys, "argv", ["validate", "--scanner", "COM99", "--monitor",
                    "--seconds", "0.1", "--output", directory]), \
                 patch("validate_hardware.serial_devices", return_value=[]), \
                 patch("builtins.input", side_effect=EOFError), patch("builtins.print"), \
                 patch.dict(sys.modules, serial=SimpleNamespace(Serial=unavailable, SerialException=OSError)):
                self.assertEqual(main(), 1)
            summary = json.loads(next(Path(directory).glob("*/summary.json")).read_text())
            self.assertEqual(summary["stats"]["observations"], 0)
            self.assertEqual(summary["uuid_reception"], "NOT VERIFIED")
            self.assertEqual(summary["database_integrity"], "ok")


class FirmwareSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        here = Path(__file__).resolve().parent
        folder = here if (here / "flash_firmware.py").is_file() else here.parent / "beacon"
        # Import these local CLI modules without requiring beacon to be a package.
        sys.path.insert(0, str(folder))
        import flash_firmware
        cls.flash = flash_firmware
        sys.path.remove(str(folder))

    def test_unbuilt_manifest_cannot_be_flashed(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "manifest.json").write_text('{"status":"building"}')
            with self.assertRaisesRegex(ValueError, "successful full build"):
                self.flash.verified_image("scanner", folder)

    def test_tampered_image_cannot_be_flashed(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "scanner.merged.bin").write_bytes(b"tampered")
            (folder / "manifest.json").write_text(json.dumps(dict(status="built", role="scanner",
                chip="esp32s3", image="scanner.merged.bin", image_sha256="wrong")))
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                self.flash.verified_image("scanner", folder)

    def test_flash_requires_current_selected_port(self):
        with patch.object(sys, "argv", ["flash", "--role", "scanner", "--port", "COM99"]), \
             patch.object(self.flash, "verified_image", return_value=Path("unused")), \
             patch("serial.tools.list_ports.comports", return_value=[]), \
             patch.object(self.flash.subprocess, "run") as upload, \
             patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                self.flash.main()
            upload.assert_not_called()

    def test_cancelled_role_confirmation_never_flashes(self):
        device = SimpleNamespace(device="COM99", description="USB", hwid="test")
        with patch.object(sys, "argv", ["flash", "--role", "scanner", "--port", "COM99"]), \
             patch.object(self.flash, "verified_image", return_value=Path("unused")), \
             patch("serial.tools.list_ports.comports", return_value=[device]), \
             patch("builtins.input", return_value="flash beacon COM99"), patch("builtins.print"), \
             patch.object(self.flash.subprocess, "run") as upload:
            with self.assertRaisesRegex(SystemExit, "Cancelled"):
                self.flash.main()
            upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
