import importlib.util
import fcntl
import errno
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / "ops" / "capture_minisa_recovery.py"
SPEC = importlib.util.spec_from_file_location("capture_minisa_recovery", SCRIPT)
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)


class RecoveryCaptureTests(unittest.TestCase):
    def test_source_lock_acquisition_never_creates_or_changes_lock_files(self):
        with tempfile.TemporaryDirectory(prefix="minisa-lock-test-") as td:
            root = Path(td)
            originals = {}
            for name in (".minisa-awake.lock", ".minisa-writer.lock"):
                p = root / name
                p.write_bytes(b"preexisting-lock-marker")
                originals[p] = p.read_bytes()
            fds = capture.acquire_source_locks(root)
            try:
                self.assertEqual([os.fstat(fd).st_size for fd in fds], [len(b"preexisting-lock-marker")] * 2)
                probe = os.open(root / ".minisa-awake.lock", os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(probe)
            finally:
                for fd in fds:
                    os.close(fd)
            self.assertEqual({p: p.read_bytes() for p in originals}, originals)

    def test_online_wal_backup_and_complete_file_restore_are_disposable(self):
        with tempfile.TemporaryDirectory(prefix="minisa-capture-test-") as td:
            root = Path(td)
            source = root / "srv" / "minisa"
            data = source / "data"
            data.mkdir(parents=True)
            db = data / "minisa.db"
            con = sqlite3.connect(db)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("CREATE TABLE preserved(value TEXT)")
            con.execute("INSERT INTO preserved VALUES ('committed-in-wal')")
            con.commit()
            con.close()
            (source / "CORE").mkdir()
            (source / "CORE" / "identity.md").write_text("original identity\n")
            (source / ".venv").mkdir()
            (source / ".venv" / "python").symlink_to("/usr/bin/python3")
            release = root / "home" / "sbeppi" / "release"
            release.mkdir(parents=True)
            (release / "runtime.py").write_text("active release\n")
            unit = root / "etc" / "systemd" / "system" / "minisa.service"
            unit.parent.mkdir(parents=True)
            unit.write_text("[Service]\nExecStart=original\n")
            wants = root / "etc" / "systemd" / "system" / "multi-user.target.wants"
            wants.mkdir()
            enabled_link = wants / "minisa.service"
            enabled_link.symlink_to(unit)
            backup = root / "backup"
            backup.mkdir()
            db_copy = backup / "sqlite" / "minisa.db"
            metadata = capture.sqlite_backup(db, db_copy)
            self.assertEqual(metadata["integrity_check"], ["ok"])
            with closing(sqlite3.connect(db_copy)) as copied:
                self.assertEqual(copied.execute("SELECT value FROM preserved").fetchone()[0], "committed-in-wal")
            archive = backup / "file-state.tar"
            expected = capture.archive_tree(archive, source, release, (unit, enabled_link))
            self.assertFalse(any(item["path"].endswith("/data/minisa.db") or
                                 item["path"].endswith("/data/minisa.db-wal") or
                                 item["path"].endswith("/data/minisa.db-shm") for item in expected))
            metadata.update(source_mode="0o600", source_uid=__import__('os').getuid(), source_gid=__import__('os').getgid())
            os.chmod(db_copy, 0o600)  # backup artifact is private; rehearsal must restore source metadata
            restored = capture.restore_rehearsal(archive, db_copy, backup / "disposable-restore", source, metadata, expected)
            self.assertEqual(restored["database_integrity_check"], ["ok"])
            restored_root = backup / "disposable-restore"
            restored_source = restored_root / source.as_posix().lstrip("/")
            restored_release = restored_root / release.as_posix().lstrip("/")
            restored_unit = restored_root / unit.as_posix().lstrip("/")
            restored_enabled_link = restored_root / enabled_link.as_posix().lstrip("/")
            self.assertEqual((restored_source / "CORE/identity.md").read_text(), "original identity\n")
            self.assertEqual((restored_release / "runtime.py").read_text(), "active release\n")
            self.assertTrue((restored_source / ".venv/python").is_symlink())
            self.assertEqual(restored_unit.read_text(), unit.read_text())
            self.assertTrue(restored_enabled_link.is_symlink())
            self.assertEqual(os.readlink(restored_enabled_link), str(unit))
            with closing(sqlite3.connect(f"file:{restored_source / 'data/minisa.db'}?mode=ro", uri=True)) as restored_db:
                self.assertEqual(restored_db.execute("SELECT value FROM preserved").fetchone()[0], "committed-in-wal")

    def test_incomplete_capture_requires_verified_marker_and_matching_manifest(self):
        with tempfile.TemporaryDirectory(prefix="minisa-marker-test-") as td:
            root = Path(td)
            for name in ("file-state.tar", "sqlite/minisa.db", "file-manifest.json"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n")
            capture.write_incomplete_marker(root)
            digest = capture.finalize_capture(root, {"created_utc": "fixture"})
            marker = (root / "CAPTURE_VERIFIED").read_text().strip()
            self.assertEqual(marker, f"manifest_sha256={digest}")
            self.assertFalse((root / "CAPTURE_INCOMPLETE").exists())
            manifest = __import__('json').loads((root / "manifest.json").read_text())
            self.assertEqual(manifest["capture_status"], "verified")
            self.assertTrue(capture.is_verified_capture(root))
            capture.verify_checksums(root)
            (root / "file-state.tar").write_text("tampered\n")
            self.assertFalse(capture.is_verified_capture(root))

    def test_size_preflight_fails_closed_on_walk_permission_error(self):
        with tempfile.TemporaryDirectory(prefix="minisa-size-test-") as td:
            root = Path(td)

            def inaccessible_walk(path, followlinks=False, onerror=None):
                onerror(PermissionError(13, "Permission denied", str(path)))
                return iter(())

            with patch.object(capture.os, "walk", side_effect=inaccessible_walk):
                with self.assertRaisesRegex(RuntimeError, "cannot inventory recovery source"):
                    capture.estimate_capture_bytes((root,))

    def test_unprivileged_preflight_is_reported_as_blocked(self):
        with tempfile.TemporaryDirectory(prefix="minisa-preflight-test-") as td:
            with patch.object(capture.os, "geteuid", return_value=1000):
                result = capture.preflight_details(Path(td), {"independent_from_live_block_devices": True}, require_root=True)
            self.assertEqual(result["preflight"], "blocked")
            self.assertIsNone(result["required_free_bytes"])
            self.assertIn("root preflight", result["blocker"])

    def test_service_states_restore_when_a_stop_command_errors_midway(self):
        states = {"minisa.service": "active", "minisa-core.service": "active"}
        calls = []

        def state(service):
            return states[service]

        def command(*args):
            calls.append(args)
            action, service = args[-2:]
            if action == "stop":
                states[service] = "inactive"
                if service == "minisa.service":
                    raise RuntimeError("simulated stop command failure")
            elif action == "start":
                states[service] = "active"
            return ""

        with self.assertRaisesRegex(RuntimeError, "simulated stop command failure"):
            capture.coordinated_capture(lambda _original: self.fail("capture must not start"),
                                        command_fn=command, state_fn=state, lock_fn=lambda _root: [])
        self.assertEqual(states, {"minisa.service": "active", "minisa-core.service": "active"})
        self.assertIn(("systemctl", "start", "minisa.service"), calls)

    def test_initially_inactive_service_remains_inactive_after_capture(self):
        states = {"minisa.service": "active", "minisa-core.service": "inactive"}

        def state(service):
            return states[service]

        def command(*args):
            action, service = args[-2:]
            states[service] = "inactive" if action == "stop" else "active"
            return ""

        result = capture.coordinated_capture(lambda original: dict(original), command_fn=command,
                                             state_fn=state, lock_fn=lambda _root: [])
        self.assertEqual(result["minisa-core.service"], "inactive")
        self.assertEqual(states["minisa-core.service"], "inactive")

    def test_capture_exception_releases_locks_and_restores_both_active_services(self):
        states = {"minisa.service": "active", "minisa-core.service": "active"}
        locks = [os.open(__file__, os.O_RDONLY)]

        def state(service):
            return states[service]

        def command(*args):
            action, service = args[-2:]
            states[service] = "inactive" if action == "stop" else "active"
            return ""

        def fail(_original):
            raise ValueError("synthetic tar failure")

        with self.assertRaisesRegex(ValueError, "synthetic tar failure"):
            capture.coordinated_capture(fail,
                                        command_fn=command, state_fn=state, lock_fn=lambda _root: locks)
        self.assertEqual(states, {"minisa.service": "active", "minisa-core.service": "active"})
        with self.assertRaises(OSError) as caught:
            os.fstat(locks[0])
        self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_service_restore_attempts_every_unit_even_if_one_start_fails(self):
        states = {"minisa.service": "inactive", "minisa-core.service": "inactive"}
        calls = []

        def state(service):
            return states[service]

        def command(*args):
            _systemctl, action, service = args
            calls.append(service)
            if service == "minisa.service":
                raise RuntimeError("simulated service start failure")
            states[service] = "active" if action == "start" else "inactive"

        errors = capture.restore_service_states({"minisa.service": "active", "minisa-core.service": "active"},
                                                command_fn=command, state_fn=state)
        self.assertTrue(errors)
        self.assertEqual(calls, ["minisa.service", "minisa-core.service"])
        self.assertEqual(states["minisa-core.service"], "active")


if __name__ == "__main__":
    unittest.main()
