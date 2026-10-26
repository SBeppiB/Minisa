"""Deployment preflight: fail-closed behaviour on disposable fixtures.

The preflight is the gate that must refuse to let a deployment proceed when a
prerequisite cannot be verified. These tests build throwaway manifests,
releases, databases and capture directories in temporary directories. The live
production database, services and release are never opened.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

CANDIDATE_ROOT = Path(__file__).parents[1]
SCRIPT = CANDIDATE_ROOT / "ops" / "preflight_learning_cycle_release.py"
SPEC = importlib.util.spec_from_file_location("preflight_learning_cycle_release", SCRIPT)
preflight = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = preflight
SPEC.loader.exec_module(preflight)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def build_manifest(release: Path, base: Path, files: dict, **overrides) -> dict:
    manifest = {
        "base_release": str(base),
        "candidate_release": str(release),
        "candidate_supported_schema_version": 5,
        "candidate_systemd_dropin": {
            "source": str(release / "dropin.conf"),
            "sha256": sha256_bytes((release / "dropin.conf").read_bytes()),
            "planned_install_path": "/etc/systemd/system/minisa.service.d/20-dream-recovery.conf",
        },
        "changed_runtime_files": [
            {"path": name, "bytes": len(payload), "sha256": sha256_bytes(payload)}
            for name, payload in sorted(files.items())
        ],
        "live_global_revision_at_check": 195,
        "live_schema_version": 4,
        "migration_required": True,
        "core_service_migration_required": True,
        "production_deployed": False,
    }
    manifest.update(overrides)
    return manifest


class PreflightFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="minisa-preflight-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.release = self.tmp / "release"
        self.base = self.tmp / "base"
        self.release.mkdir()
        self.base.mkdir()
        (self.release / "dropin.conf").write_text("[Service]\nTimeoutStopSec=50\n")
        self.files = {"core/db.py": b"# db\n", "runtime/server/application.py": b"# app\n"}
        for name, payload in self.files.items():
            target = self.release / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        self.manifest = build_manifest(self.release, self.base, self.files)
        self.manifest_path = self.tmp / "candidate.manifest.json"
        write_json(self.manifest_path, self.manifest)

    def build_schema_db(self, upto: int = 4) -> Path:
        module = preflight.load_module_from_path(
            f"preflight_test_migrations_{upto}_{id(self)}", CANDIDATE_ROOT / "core" / "migrations.py"
        )
        module.MIGRATIONS = tuple(m for m in module.MIGRATIONS if m.version <= upto)
        db_path = self.tmp / f"minisa-{upto}.db"
        connection = sqlite3.connect(db_path)
        module.apply_migrations(connection, installation_name="preflight-test",
                                software_version="1.0.0-stage1")
        connection.close()
        return db_path


class ManifestChecks(PreflightFixture):
    def test_valid_manifest_passes(self):
        check, manifest = preflight.check_manifest(self.manifest_path)
        self.assertEqual(check.status, preflight.PASSED)
        self.assertFalse(check.blocking)
        self.assertEqual(manifest["candidate_supported_schema_version"], 5)

    def test_unparsable_manifest_fails_closed(self):
        broken = self.tmp / "broken.manifest.json"
        broken.write_text("{not json")
        check, manifest = preflight.check_manifest(broken)
        self.assertEqual(check.status, preflight.FAILED)
        self.assertTrue(check.blocking)
        self.assertEqual(manifest, {})

    def test_manifest_claiming_deployment_fails_closed(self):
        write_json(self.manifest_path, build_manifest(self.release, self.base, self.files,
                                                       production_deployed=True))
        check, _ = preflight.check_manifest(self.manifest_path)
        self.assertEqual(check.status, preflight.FAILED)
        self.assertIn("production_deployed", check.detail)

    def test_duplicate_manifest_path_fails_closed(self):
        manifest = build_manifest(self.release, self.base, self.files)
        manifest["changed_runtime_files"].append(dict(manifest["changed_runtime_files"][0]))
        write_json(self.manifest_path, manifest)
        check, _ = preflight.check_manifest(self.manifest_path)
        self.assertEqual(check.status, preflight.FAILED)
        self.assertIn("duplicate", check.detail)

    def test_missing_required_key_fails_closed(self):
        manifest = build_manifest(self.release, self.base, self.files)
        del manifest["base_release"]
        write_json(self.manifest_path, manifest)
        check, _ = preflight.check_manifest(self.manifest_path)
        self.assertEqual(check.status, preflight.FAILED)


class ReleaseContentChecks(PreflightFixture):
    def test_release_matching_manifest_passes(self):
        check = preflight.check_release_content(self.manifest)
        self.assertEqual(check.status, preflight.PASSED)
        self.assertEqual(check.evidence["verified_files"], 2)

    def test_tampered_release_file_fails_closed(self):
        (self.release / "core" / "db.py").write_bytes(b"# tampered\n")
        check = preflight.check_release_content(self.manifest)
        self.assertEqual(check.status, preflight.FAILED)
        self.assertTrue(check.blocking)

    def test_missing_release_file_fails_closed(self):
        (self.release / "core" / "db.py").unlink()
        check = preflight.check_release_content(self.manifest)
        self.assertEqual(check.status, preflight.FAILED)

    def test_untracked_bytecode_is_reported_without_failing(self):
        cache = self.release / "core" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "db.cpython-314.pyc").write_bytes(b"pyc")
        check = preflight.check_release_content(self.manifest)
        self.assertEqual(check.status, preflight.PASSED)
        self.assertEqual(check.evidence["untracked_bytecode_files"], 1)


class MigrationLineageChecks(PreflightFixture):
    def test_matching_lineage_passes(self):
        db_path = self.build_schema_db(4)
        check = preflight.check_migration_lineage(CANDIDATE_ROOT, db_path)
        self.assertEqual(check.status, preflight.PASSED)
        self.assertEqual(check.evidence["production_applied_migrations"], [1, 2, 3, 4])
        self.assertEqual(check.evidence["candidate_pending_migrations"], [5])

    def test_checksum_divergence_fails_closed(self):
        db_path = self.build_schema_db(4)
        connection = sqlite3.connect(db_path)
        connection.execute("UPDATE schema_migrations SET checksum='tampered' WHERE version=2")
        connection.commit()
        connection.close()
        check = preflight.check_migration_lineage(CANDIDATE_ROOT, db_path)
        self.assertEqual(check.status, preflight.FAILED)
        self.assertIn("checksum mismatch", check.detail)

    def test_missing_migration_fails_closed(self):
        db_path = self.build_schema_db(4)
        connection = sqlite3.connect(db_path)
        connection.execute("DELETE FROM schema_migrations WHERE version=3")
        connection.commit()
        connection.close()
        check = preflight.check_migration_lineage(CANDIDATE_ROOT, db_path)
        self.assertEqual(check.status, preflight.FAILED)

    def test_lineage_check_does_not_modify_the_database(self):
        db_path = self.build_schema_db(4)
        before = hashlib.sha256(db_path.read_bytes()).hexdigest()
        preflight.check_migration_lineage(CANDIDATE_ROOT, db_path)
        preflight.check_live_database(db_path, 5)
        after = hashlib.sha256(db_path.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_unreadable_database_fails_closed(self):
        broken = self.tmp / "broken.db"
        broken.write_bytes(b"this is not a database")
        check = preflight.check_migration_lineage(CANDIDATE_ROOT, broken)
        self.assertEqual(check.status, preflight.FAILED)
        self.assertTrue(check.blocking)


class LiveDatabaseChecks(PreflightFixture):
    def test_pre_migration_state_passes(self):
        db_path = self.build_schema_db(4)
        check = preflight.check_live_database(db_path, 5)
        self.assertEqual(check.status, preflight.PASSED)
        self.assertEqual(check.evidence["schema_version"], 4)
        self.assertEqual(check.evidence["integrity_check"], "ok")

    def test_post_migration_state_passes(self):
        db_path = self.build_schema_db(5)
        check = preflight.check_live_database(db_path, 5)
        self.assertEqual(check.status, preflight.PASSED)
        self.assertEqual(check.evidence["schema_version"], 5)
        self.assertTrue(check.evidence["has_memory_records"])

    def test_unexpected_schema_version_fails_closed(self):
        db_path = self.build_schema_db(3)
        check = preflight.check_live_database(db_path, 5)
        self.assertEqual(check.status, preflight.FAILED)

    def test_corrupt_database_fails_closed(self):
        broken = self.tmp / "corrupt.db"
        broken.write_bytes(b"not a sqlite database at all")
        check = preflight.check_live_database(broken, 5)
        self.assertEqual(check.status, preflight.FAILED)


class RecoveryPointChecks(PreflightFixture):
    def build_capture(self, *, marker_matches=True, incomplete=False, corrupt_db=False) -> Path:
        capture = self.tmp / "minisa-recovery-20261021T000000Z"
        (capture / "sqlite").mkdir(parents=True)
        db_path = self.build_schema_db(4)
        payload = db_path.read_bytes()
        if corrupt_db:
            payload = payload[: len(payload) // 2] + b"\x00" * 64
        (capture / "sqlite" / "minisa.db").write_bytes(payload)
        manifest = {
            "capture_status": "verified",
            "destination": {"independent_from_live_block_devices": True,
                            "path": str(self.tmp)},
            "database": {"path": "sqlite/minisa.db", "sha256": sha256_bytes(payload),
                         "schema_version": 4, "global_revision": 195,
                         "integrity_check": "ok"},
        }
        manifest_path = capture / "manifest.json"
        write_json(manifest_path, manifest)
        entries = {
            "sqlite/minisa.db": sha256_bytes(payload),
            "manifest.json": sha256_bytes(manifest_path.read_bytes()),
        }
        (capture / "SHA256SUMS").write_text(
            "".join(f"{digest}  {rel}\n" for rel, digest in entries.items())
        )
        digest = sha256_bytes(manifest_path.read_bytes())
        if not marker_matches:
            digest = "0" * 64
        (capture / "CAPTURE_VERIFIED").write_text(f"manifest_sha256={digest}\n")
        if incomplete:
            (capture / "CAPTURE_INCOMPLETE").write_text("Capture is not verified.\n")
        return capture

    def test_missing_capture_argument_fails_closed(self):
        check = preflight.check_recovery_point(None)
        self.assertEqual(check.status, preflight.FAILED)
        self.assertTrue(check.blocking)

    def test_absent_capture_directory_fails_closed(self):
        check = preflight.check_recovery_point(self.tmp / "does-not-exist")
        self.assertEqual(check.status, preflight.FAILED)

    def test_verified_capture_passes(self):
        capture = self.build_capture()
        check = preflight.check_recovery_point(capture)
        self.assertEqual(check.status, preflight.PASSED, check.detail)
        self.assertEqual(check.evidence["captured_database"]["integrity_check_recheck"], "ok")

    def test_incomplete_marker_fails_closed(self):
        capture = self.build_capture(incomplete=True)
        check = preflight.check_recovery_point(capture)
        self.assertEqual(check.status, preflight.FAILED)
        self.assertIn("CAPTURE_INCOMPLETE", check.detail)

    def test_marker_digest_mismatch_fails_closed(self):
        capture = self.build_capture(marker_matches=False)
        check = preflight.check_recovery_point(capture)
        self.assertEqual(check.status, preflight.FAILED)

    def test_checksum_mismatch_fails_closed(self):
        capture = self.build_capture()
        sums = capture / "SHA256SUMS"
        sums.write_text(sums.read_text().replace(sums.read_text().split("  ")[0], "f" * 64))
        check = preflight.check_recovery_point(capture)
        self.assertEqual(check.status, preflight.FAILED)

    def test_corrupt_captured_database_fails_closed(self):
        capture = self.build_capture(corrupt_db=True)
        check = preflight.check_recovery_point(capture)
        self.assertEqual(check.status, preflight.FAILED)

    def test_capture_without_independent_destination_fails_closed(self):
        capture = self.build_capture()
        manifest_path = capture / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["destination"]["independent_from_live_block_devices"] = False
        write_json(manifest_path, manifest)
        digest = sha256_bytes(manifest_path.read_bytes())
        (capture / "CAPTURE_VERIFIED").write_text(f"manifest_sha256={digest}\n")
        check = preflight.check_recovery_point(capture)
        self.assertEqual(check.status, preflight.FAILED)
        self.assertIn("independent", check.detail)


class CoreServiceAndRollbackChecks(PreflightFixture):
    def build_core_app(self) -> Path:
        core_app = self.tmp / "app"
        (core_app / "core").mkdir(parents=True)
        (core_app / "core" / "db.py").write_bytes(b"# live db\n")
        # The live core code is the schema-4 predecessor, not the candidate chain.
        (core_app / "core" / "migrations.py").write_text("SUPPORTED_SCHEMA_VERSION = 4\n")
        return core_app

    def test_core_copy_step_is_detected(self):
        core_app = self.build_core_app()
        check = preflight.check_core_service_files(CANDIDATE_ROOT, core_app)
        self.assertEqual(check.status, preflight.PASSED)
        self.assertFalse(check.evidence["core_files"]["db.py"]["identical"])

    def test_identical_core_files_fail_closed(self):
        core_app = self.build_core_app()
        shutil.copyfile(CANDIDATE_ROOT / "core" / "db.py", core_app / "core" / "db.py")
        check = preflight.check_core_service_files(CANDIDATE_ROOT, core_app)
        self.assertEqual(check.status, preflight.FAILED)

    def test_rollback_prerequisites_report_one_way_migration(self):
        core_app = self.build_core_app()
        check = preflight.check_rollback_prerequisites(self.manifest, core_app)
        self.assertEqual(check.status, preflight.PASSED, check.detail)
        self.assertEqual(check.evidence["live_core_supported_schema_version"], 4)
        self.assertFalse(check.evidence["live_core_is_valid_rollback_target"])
        self.assertIn("pre-migration database copy", check.evidence["live_core_rollback_note"])

    def test_previous_core_copy_below_candidate_is_reported_not_fatal(self):
        core_app = self.build_core_app()
        previous = core_app / "core.phase5-previous"
        previous.mkdir()
        (previous / "migrations.py").write_text("SUPPORTED_SCHEMA_VERSION = 2\n")
        check = preflight.check_rollback_prerequisites(self.manifest, core_app)
        self.assertEqual(check.status, preflight.PASSED, check.detail)
        self.assertIn(str(previous), check.evidence["previous_core_copies"])
        self.assertEqual(check.evidence["previous_core_supported_versions"][str(previous)], 2)
        self.assertIn("cannot serve a migrated schema",
                      check.evidence["previous_core_rollback_notes"][str(previous)])

    def test_missing_base_release_fails_closed(self):
        core_app = self.build_core_app()
        manifest = dict(self.manifest)
        manifest["base_release"] = str(self.tmp / "absent-base")
        check = preflight.check_rollback_prerequisites(manifest, core_app)
        self.assertEqual(check.status, preflight.FAILED)


class ServiceConfigurationChecks(PreflightFixture):
    def _properties(self):
        return {
            "ActiveState": "active",
            "SubState": "running",
            "User": "sbeppi",
            "WorkingDirectory": str(self.base),
            "ExecStart": "/srv/minisa/.venv/bin/python3 -m runtime.server.main",
            "TimeoutStopUSec": "1min 30s",
            "DropInPaths": "/usr/lib/systemd/system/service.d/10-timeout-abort.conf",
            "FragmentPath": str(self.tmp / "minisa.service"),
        }

    def test_matching_configuration_passes(self):
        unit = self.tmp / "minisa.service"
        unit.write_text("[Service]\nExecStart=/srv/minisa/.venv/bin/python3\n")
        config = self.tmp / "minisa.toml"
        config.write_text("[server]\nport = 8765\n")
        env = self.tmp / "minisa.env"
        env.write_text("MINISA_OLLAMA_URL=http://example\n")
        dropin = self.tmp / "dropin" / "20-dream-recovery.conf"
        with patch.object(preflight, "systemctl_properties", side_effect=[self._properties(), {}]):
            check = preflight.check_service_configuration(dropin, str(self.base), config, env)
        self.assertEqual(check.status, preflight.PASSED, check.detail)
        self.assertEqual(check.evidence["effective_dream_timeout_seconds"], 40)

    def test_existing_dropin_fails_closed(self):
        unit = self.tmp / "minisa.service"
        unit.write_text("[Service]\n")
        config = self.tmp / "minisa.toml"
        config.write_text("[server]\n")
        env = self.tmp / "minisa.env"
        env.write_text("")
        dropin = self.tmp / "dropin" / "20-dream-recovery.conf"
        dropin.parent.mkdir(parents=True)
        dropin.write_text("[Service]\n")
        with patch.object(preflight, "systemctl_properties", side_effect=[self._properties(), {}]):
            check = preflight.check_service_configuration(dropin, str(self.base), config, env)
        self.assertEqual(check.status, preflight.FAILED)
        self.assertIn("already exists", check.detail)

    def test_unreadable_unit_file_fails_closed(self):
        config = self.tmp / "minisa.toml"
        config.write_text("[server]\n")
        env = self.tmp / "minisa.env"
        env.write_text("")
        dropin = self.tmp / "dropin" / "20-dream-recovery.conf"
        with patch.object(preflight, "systemctl_properties", side_effect=[self._properties(), {}]):
            check = preflight.check_service_configuration(dropin, str(self.base), config, env)
        self.assertEqual(check.status, preflight.FAILED)
        self.assertIn("not readable", check.detail)

    def test_unexpected_working_directory_fails_closed(self):
        unit = self.tmp / "minisa.service"
        unit.write_text("[Service]\n")
        config = self.tmp / "minisa.toml"
        config.write_text("[server]\n")
        env = self.tmp / "minisa.env"
        env.write_text("")
        dropin = self.tmp / "dropin" / "20-dream-recovery.conf"
        with patch.object(preflight, "systemctl_properties", side_effect=[self._properties(), {}]):
            check = preflight.check_service_configuration(dropin, "/some/other/release", config, env)
        self.assertEqual(check.status, preflight.FAILED)


class NestedAndEndToEndChecks(PreflightFixture):
    def test_nested_test_run_is_not_blocking(self):
        with patch.dict("os.environ", {"MINISA_PREFLIGHT_NO_TESTS": "1"}):
            check = preflight.check_tests(CANDIDATE_ROOT)
        self.assertEqual(check.status, preflight.WARNING)
        self.assertFalse(check.blocking)

    def test_main_returns_blocked_without_a_recovery_point(self):
        argv = [
            "--manifest", str(self.manifest_path),
            "--candidate-root", str(CANDIDATE_ROOT),
            "--db", str(self.build_schema_db(4)),
            "--core-app-dir", str(self.tmp / "app"),
            "--skip-tests",
        ]
        with patch.object(preflight, "systemctl_properties",
                          side_effect=[{"ActiveState": "active", "WorkingDirectory": str(self.base),
                                        "FragmentPath": str(self.tmp / "minisa.service"),
                                        "DropInPaths": "", "TimeoutStopUSec": "1min"},
                                       {"ActiveState": "active"}]):
            with contextlib.redirect_stdout(io.StringIO()):
                code = preflight.main(argv)
        self.assertEqual(code, 2)

    def test_main_writes_no_files_outside_the_requested_report(self):
        report_path = self.tmp / "report.json"
        argv = [
            "--manifest", str(self.manifest_path),
            "--candidate-root", str(CANDIDATE_ROOT),
            "--db", str(self.build_schema_db(4)),
            "--core-app-dir", str(self.tmp / "app"),
            "--skip-tests",
            "--json", str(report_path),
        ]
        before = {p: p.stat().st_mtime_ns for p in self.tmp.rglob("*") if p.is_file()}
        with patch.object(preflight, "systemctl_properties",
                          side_effect=[{"ActiveState": "active", "WorkingDirectory": str(self.base),
                                        "FragmentPath": str(self.tmp / "minisa.service"),
                                        "DropInPaths": "", "TimeoutStopUSec": "1min"},
                                       {"ActiveState": "active"}]):
            with contextlib.redirect_stdout(io.StringIO()):
                code = preflight.main(argv)
        self.assertEqual(code, 2)
        report = json.loads(report_path.read_text())
        self.assertEqual(report["preflight"], "blocked")
        self.assertTrue(report["read_only"])
        after = {p: p.stat().st_mtime_ns for p in self.tmp.rglob("*") if p.is_file()}
        changed = {str(p) for p in set(before) | set(after)
                   if before.get(p) != after.get(p)} - {str(report_path)}
        self.assertEqual(changed, set())


if __name__ == "__main__":
    unittest.main()
