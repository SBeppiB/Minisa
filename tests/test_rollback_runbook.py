"""Tests for the rollback procedure and its read-only verification helper.

Three groups of tests:

* document invariants - RECOVERY_RUNBOOK.md must not depend on the `sqlite3`
  command-line program (it is not installed on this host) and must describe the
  schema-4 recovery as an ordered, code-then-data procedure;
* helper behaviour - `ops/verify_recovery_database.py` must pass on a healthy
  database, name every failing check, refuse to write, and work under both
  Python interpreters that exist on this host;
* the re-migration hazard - a predecessor core tree must leave a restored
  schema-4 database at schema 4, while the candidate core tree re-applies
  migration 5. This is the reason the runbook orders the code rollback before
  the data rollback.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CANDIDATE_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = CANDIDATE_ROOT / "RECOVERY_RUNBOOK.md"
HELPER = CANDIDATE_ROOT / "ops" / "verify_recovery_database.py"
BASE_RELEASE = Path(
    "/home/sbeppi/minisa-releases/response-context-20261017T010152Z")

INTERPRETERS = [sys.executable]
SYSTEM_PYTHON = Path("/usr/bin/python3")
if SYSTEM_PYTHON.exists() and str(SYSTEM_PYTHON) not in INTERPRETERS:
    INTERPRETERS.append(str(SYSTEM_PYTHON))


def read_runbook() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def section(text: str, title: str, next_title: str | None = None) -> str:
    """Return the body of a markdown section."""
    start = text.index(title)
    end = text.index(next_title, start) if next_title else len(text)
    return text[start:end]


def build_schema4_database(path: Path) -> None:
    """Create a genuine schema-4 database by applying migrations 1-4."""
    sys.path.insert(0, str(CANDIDATE_ROOT))
    try:
        from core import migrations
    finally:
        sys.path.pop(0)

    import datetime
    import uuid

    conn = sqlite3.connect(path)
    try:
        for migration in migrations.MIGRATIONS:
            if migration.version > 4:
                break
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            conn.execute("BEGIN IMMEDIATE")
            for statement in migration.sql.split(";\n"):
                if statement.strip():
                    conn.execute(statement)
            if migration.version == 1:
                conn.execute(
                    "INSERT INTO installation VALUES (1, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), now, "minisa-core", migration.version, "4.0.0"),
                )
                conn.execute("INSERT INTO core_state VALUES (1, 0)")
                conn.execute(
                    "INSERT INTO audit_events VALUES (?, NULL, ?, ?, ?, ?, NULL, ?, NULL)",
                    (str(uuid.uuid4()), now, "migration", "schema.migrate", "database", "{}"),
                )
            conn.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) "
                "VALUES (?,?,?,?)",
                (migration.version, migration.name, migration.checksum, now),
            )
            conn.execute(
                "UPDATE installation SET schema_version=?, software_version=? WHERE singleton=1",
                (migration.version, "4.0.0"),
            )
            conn.commit()
    finally:
        conn.close()


def run_helper(arguments: list[str], interpreter: str | None = None) -> subprocess.CompletedProcess:
    command = [interpreter or sys.executable, str(HELPER), *arguments]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run(command, capture_output=True, text=True, timeout=180, env=env)


class RunbookDocumentInvariantTests(unittest.TestCase):
    """The runbook must stay executable on this host and stay ordered."""

    def setUp(self) -> None:
        self.text = read_runbook()

    def test_runbook_does_not_call_the_sqlite3_executable(self) -> None:
        offenders = [
            f"{number}: {line.strip()}"
            for number, line in enumerate(self.text.splitlines(), start=1)
            if re.match(r"^\s*(sudo\s+)?sqlite3\b", line)
            or re.search(r"[\s;&|]sqlite3\s+/", line)
        ]
        self.assertEqual([], offenders,
                         "the sqlite3 command-line program is not installed on this host; "
                         "use ops/verify_recovery_database.py instead")

    def test_runbook_points_at_the_verification_helper(self) -> None:
        self.assertGreaterEqual(self.text.count("ops/verify_recovery_database.py"), 4,
                                "every database verification step must use the helper")

    def test_helper_is_part_of_the_candidate(self) -> None:
        self.assertTrue(HELPER.is_file(), f"{HELPER} is missing")

    def test_rollback_section_exists_and_is_ordered(self) -> None:
        rollback = section(self.text, "## 7. Rollback", "## 8. Restoration procedure")
        self.assertIn("### 7.2", rollback)
        self.assertIn("### 7.3", rollback)
        self.assertIn("### 7.5", rollback)

        code_rollback = rollback.index("20-dream-recovery.conf")
        core_restore = rollback.index("migrations.py.pre-v5-<stamp>")
        database_restore = rollback.index("install -o sbeppi -g sbeppi -m 0644 "
                                          "sqlite/minisa.db /srv/minisa/data/minisa.db")
        self.assertLess(code_rollback, database_restore,
                        "the release pointer must be reverted before the database is restored")
        self.assertLess(core_restore, database_restore,
                        "the predecessor core code must be restored before the database is restored")

        verification = rollback.index("capture_database_match")
        first_start = rollback.index("systemctl start")
        self.assertLess(verification, first_start,
                        "the restored database must be verified before any service starts")

    def test_rollback_documents_the_remigration_hazard(self) -> None:
        rollback = section(self.text, "## 7. Rollback", "## 8. Restoration procedure")
        self.assertIn("database.initialize()", rollback)
        self.assertIn("migration 5", rollback)
        self.assertIn("re-applies", rollback)
        self.assertIn("pre-v5-<stamp>", rollback,
                      "the runbook must name the predecessor core copies")

    def test_rollback_verification_uses_the_helper_and_the_capture(self) -> None:
        rollback = section(self.text, "## 7. Rollback", "## 8. Restoration procedure")
        self.assertIn("ops/verify_recovery_database.py", rollback)
        self.assertIn("--capture-dir", rollback)
        self.assertIn("--expect-no-table memory_records", rollback,
                      "the rollback must prove that migration 5 was undone")

    def test_rollback_invariants_are_listed(self) -> None:
        rollback = section(self.text, "## 7. Rollback", "## 8. Restoration procedure")
        invariants = section(rollback, "### 7.5", None)
        for expected in ("release pointer", "core-service code", "byte-identical",
                         "before the verification step", "code rollback precedes"):
            self.assertIn(expected, invariants,
                          f"the rollback invariants must state: {expected}")

    def test_restoration_procedure_verifies_before_starting(self) -> None:
        restoration = section(self.text, "## 8. Restoration procedure", "## 9. Explicit stop")
        verification = restoration.index("ops/verify_recovery_database.py")
        first_start = restoration.index("systemctl start")
        self.assertLess(verification, first_start)
        self.assertIn("memory_records", restoration)
        self.assertNotRegex(restoration, r"^\s*(sudo\s+)?sqlite3\b", re.MULTILINE)

    def test_runbook_keeps_its_existing_sections(self) -> None:
        for heading in ("## 3. Phase 0", "## 4. Phase 1", "## 5. Phase 2",
                        "## 6. Phase 3", "## 9. Explicit stop conditions",
                        "## 10. Operator sign-off checklist"):
            self.assertIn(heading, self.text)


class VerificationHelperTests(unittest.TestCase):
    """The helper must pass, fail loudly, and never write."""

    def setUp(self) -> None:
        self.scratch = Path(tempfile.mkdtemp(prefix="minisa-verify-tests-"))
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)
        self.database = self.scratch / "minisa.db"
        build_schema4_database(self.database)

    def test_helper_passes_on_a_healthy_database(self) -> None:
        for interpreter in INTERPRETERS:
            with self.subTest(interpreter=interpreter):
                result = run_helper([
                    "--db", str(self.database),
                    "--expect-schema-version", "4",
                    "--expect-migrations", "1,2,3,4",
                    "--expect-integrity", "ok",
                    "--expect-global-revision", "0",
                    "--expect-table", "learned_procedures",
                    "--expect-no-table", "memory_records",
                ], interpreter=interpreter)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("VERDICT: PASSED", result.stdout)

    def test_helper_fails_and_names_the_check_on_a_schema_mismatch(self) -> None:
        result = run_helper(["--db", str(self.database), "--expect-schema-version", "5"])
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("FAIL", result.stdout)
        self.assertIn("schema_version", result.stdout)
        self.assertIn("VERDICT: FAILED", result.stdout)

    def test_helper_fails_when_an_expected_table_is_present(self) -> None:
        result = run_helper(["--db", str(self.database), "--expect-no-table",
                              "learned_procedures"])
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("table_absent:learned_procedures", result.stdout)

    def test_helper_fails_when_a_row_count_does_not_match(self) -> None:
        result = run_helper(["--db", str(self.database), "--expect-row-count",
                              "learned_procedures=3"])
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("row_count:learned_procedures", result.stdout)

    def test_helper_detects_an_incompatible_core_tree(self) -> None:
        result = run_helper(["--db", str(self.database), "--core-tree", str(CANDIDATE_ROOT)])
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("FAIL", result.stdout)
        self.assertIn("migration_compatible", result.stdout)

    def test_helper_reports_a_missing_database_with_exit_code_two(self) -> None:
        result = run_helper(["--db", str(self.scratch / "absent.db")])
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)

    def test_helper_never_writes_to_the_database(self) -> None:
        before = self.database.read_bytes()
        run_helper(["--db", str(self.database), "--expect-schema-version", "4"])
        self.assertEqual(before, self.database.read_bytes(),
                         "the helper must open the database read-only")

    def test_helper_emits_a_json_report(self) -> None:
        result = run_helper(["--db", str(self.database), "--expect-schema-version", "4",
                              "--json"])
        self.assertEqual(0, result.returncode, result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual("passed", report["verdict"])
        self.assertTrue(any(check["check"] == "integrity_check" for check in report["checks"]))

    def test_helper_core_only_reports_the_supported_schema_version(self) -> None:
        result = run_helper(["--core-tree", str(CANDIDATE_ROOT), "--core-only"])
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("supports schema version 5", result.stdout)

    def test_helper_detects_a_capture_that_does_not_match_the_database(self) -> None:
        capture = self.scratch / "capture"
        (capture / "sqlite").mkdir(parents=True)
        captured = capture / "sqlite" / "minisa.db"
        shutil.copyfile(self.database, captured)
        manifest = {
            "capture_status": "verified",
            "capture_id": "test",
            "database": {"integrity_check": "ok", "schema_version": 4},
        }
        manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
        (capture / "manifest.json").write_bytes(manifest_bytes)
        import hashlib
        digest = hashlib.sha256(manifest_bytes).hexdigest()
        (capture / "CAPTURE_VERIFIED").write_text(f"manifest_sha256={digest}\n",
                                                  encoding="utf-8")
        import hashlib as _hashlib
        (capture / "SHA256SUMS").write_text(
            f"{_hashlib.sha256(captured.read_bytes()).hexdigest()}  sqlite/minisa.db\n",
            encoding="utf-8")

        matching = run_helper(["--db", str(self.database), "--capture-dir", str(capture),
                               "--expect-schema-version", "4"])
        self.assertEqual(0, matching.returncode, matching.stdout)
        self.assertIn("capture_database_match", matching.stdout)

        other = self.scratch / "other.db"
        shutil.copyfile(self.database, other)
        with contextlib.closing(sqlite3.connect(other)) as conn:
            conn.execute("UPDATE installation SET schema_version=5 WHERE singleton=1")
            conn.commit()
        mismatching = run_helper(["--db", str(other), "--capture-dir", str(capture)])
        self.assertEqual(1, mismatching.returncode, mismatching.stdout)
        self.assertIn("capture_database_match", mismatching.stdout)

    def test_helper_detects_an_incomplete_capture(self) -> None:
        capture = self.scratch / "incomplete"
        capture.mkdir()
        (capture / "manifest.json").write_text("{}", encoding="utf-8")
        (capture / "CAPTURE_INCOMPLETE").write_text("interrupted\n", encoding="utf-8")
        result = run_helper(["--db", str(self.database), "--capture-dir", str(capture)])
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("capture_complete", result.stdout)


class RemigrationHazardTests(unittest.TestCase):
    """The reason the runbook rolls the code back before the data."""

    def setUp(self) -> None:
        self.scratch = Path(tempfile.mkdtemp(prefix="minisa-remigration-tests-"))
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)

    def _initialize_with(self, tree: Path, database: Path) -> subprocess.CompletedProcess:
        code = (
            "from core.db import Database\n"
            f"db = Database({str(database)!r})\n"
            "print('schema_version =', db.initialize())\n"
        )
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PYTHONPATH"] = str(tree)
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              cwd=self.scratch, env=env, timeout=180)

    def test_predecessor_core_leaves_a_restored_database_at_schema_four(self) -> None:
        if not (BASE_RELEASE / "core" / "migrations.py").is_file():
            self.skipTest(f"the base release is not available at {BASE_RELEASE}")
        database = self.scratch / "restored.db"
        build_schema4_database(database)
        result = self._initialize_with(BASE_RELEASE, database)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("schema_version = 4", result.stdout)
        with contextlib.closing(sqlite3.connect(database)) as conn:
            version = conn.execute(
                "SELECT schema_version FROM installation WHERE singleton=1").fetchone()[0]
            self.assertEqual(4, version)
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_records'"
            ).fetchone())

    def test_candidate_core_reapplies_migration_five(self) -> None:
        database = self.scratch / "restored.db"
        build_schema4_database(database)
        result = self._initialize_with(CANDIDATE_ROOT, database)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("schema_version = 5", result.stdout)
        with contextlib.closing(sqlite3.connect(database)) as conn:
            version = conn.execute(
                "SELECT schema_version FROM installation WHERE singleton=1").fetchone()[0]
            self.assertEqual(5, version)
            self.assertIsNotNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_records'"
            ).fetchone())


if __name__ == "__main__":
    unittest.main()
