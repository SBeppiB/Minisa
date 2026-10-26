#!/usr/bin/env python3
"""Verify a Minisa database, a core-service code tree, or a recovery capture.

The `sqlite3` command-line program is not installed on this host, so every
database verification step in RECOVERY_RUNBOOK.md uses this script instead. It
uses only the Python standard library, opens databases read-only, and exits
non-zero with a named failure when a check does not hold.

Exit codes:
  0  every requested check passed
  1  at least one requested check failed (the failing checks are named; an
     incomplete or mismatched capture package is a failed check, not an error)
  2  the script could not run (usage error, unreadable database, broken code
     tree)

Examples:
  # pre-migration capture: integrity and schema version only
  ops/verify_recovery_database.py \\
      --db /mnt/minisa-recovery/minisa-recovery-<id>/sqlite/minisa.db \\
      --expect-schema-version 4 --expect-integrity ok

  # restored database after an L2 rollback, compared with the capture
  ops/verify_recovery_database.py \\
      --db /srv/minisa/data/minisa.db --core-tree /srv/minisa/app \\
      --expect-schema-version 4 --expect-migrations 1,2,3,4 \\
      --expect-table learned_procedures --expect-no-table memory_records \\
      --capture-dir /mnt/minisa-recovery/minisa-recovery-<id>

  # what schema version does an installed core tree support?
  ops/verify_recovery_database.py --core-tree /srv/minisa/app --core-only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

PASS = "pass"
FAIL = "fail"
ERROR = "error"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def connect_readonly(path: str) -> sqlite3.Connection:
    """Open a database read-only; the file can never be modified by this script."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def core_tree_root(tree: str) -> str:
    """Return the directory that must be on sys.path to import its `core` package."""
    tree = os.path.abspath(tree)
    if os.path.isdir(os.path.join(tree, "core")):
        return tree
    parent = os.path.dirname(tree)
    if os.path.isdir(os.path.join(parent, "core")):
        return parent
    raise FileNotFoundError(f"no `core` package found under {tree}")


def probe_core_tree(tree: str) -> dict:
    """Ask a clean interpreter which schema version a core tree supports.

    The probe runs in its own process with an empty PYTHONPATH and a neutral
    working directory so that it cannot import the candidate workspace's `core`
    package by accident.
    """
    root = core_tree_root(tree)
    code = (
        "import json, sys\n"
        f"sys.path.insert(0, {root!r})\n"
        "from core import migrations\n"
        "print(json.dumps({\n"
        "    'supported_schema_version': migrations.SUPPORTED_SCHEMA_VERSION,\n"
        "    'chain': {str(m.version): [m.name, m.checksum] for m in migrations.MIGRATIONS},\n"
        "}))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    with tempfile.TemporaryDirectory(prefix="minisa-core-probe-") as scratch:
        completed = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            cwd=scratch, env=env, timeout=60)
    if completed.returncode != 0:
        raise RuntimeError(
            f"core tree {tree} could not be interrogated: "
            f"{(completed.stderr or completed.stdout).strip()[-300:]}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


class Checks:
    """Collects named check results; every failure names itself."""

    def __init__(self) -> None:
        self.results: list[dict] = []

    def record(self, name: str, ok: bool, detail: str) -> bool:
        self.results.append({"check": name, "status": PASS if ok else FAIL,
                             "detail": detail})
        return ok

    def error(self, name: str, detail: str) -> None:
        self.results.append({"check": name, "status": ERROR, "detail": detail})

    @property
    def failed(self) -> list[dict]:
        return [r for r in self.results if r["status"] != PASS]

    def render(self) -> str:
        lines = []
        for result in self.results:
            lines.append(f"{result['status'].upper():5} {result['check']}: {result['detail']}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def check_database(path: str, args: argparse.Namespace, checks: Checks,
                   core_info: dict | None) -> sqlite3.Connection | None:
    try:
        conn = connect_readonly(path)
    except sqlite3.Error as error:
        checks.error("database_readable", f"{path} could not be opened read-only: {error}")
        return None
    checks.record("database_readable", True, f"{path} opened read-only")

    integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    expected_integrity = args.expect_integrity
    checks.record("integrity_check", integrity == [expected_integrity],
                  f"integrity_check = {integrity} (expected ['{expected_integrity}'])")

    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    installed = conn.execute(
        "SELECT schema_version FROM installation WHERE singleton=1").fetchone()
    schema_version = int(installed[0]) if installed else None
    if args.expect_schema_version is not None:
        checks.record("schema_version", schema_version == args.expect_schema_version,
                      f"installation.schema_version = {schema_version} "
                      f"(expected {args.expect_schema_version})")
    else:
        checks.record("schema_version", True, f"installation.schema_version = {schema_version}")

    applied = [dict(row) for row in conn.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version")]
    applied_versions = [int(row["version"]) for row in applied]
    if args.expect_migrations:
        expected = [int(v) for v in args.expect_migrations.split(",") if v.strip()]
        checks.record("migration_history", applied_versions == expected,
                      f"applied migrations = {applied_versions} (expected {expected})")
    else:
        checks.record("migration_history", True, f"applied migrations = {applied_versions}")

    if core_info:
        chain = {int(k): v for k, v in core_info["chain"].items()}
        lineage_ok = all(
            int(row["version"]) in chain
            and chain[int(row["version"])][0] == row["name"]
            and chain[int(row["version"])][1] == row["checksum"]
            for row in applied)
        checks.record("migration_lineage", lineage_ok,
                      "applied migration names and checksums match the inspected core tree"
                      if lineage_ok else
                      "an applied migration name/checksum does not match the inspected core tree")
        supported = int(core_info["supported_schema_version"])
        checks.record("migration_compatible", schema_version == supported,
                      f"database schema {schema_version} vs core tree supported version "
                      f"{supported} (services may start only when these are equal)")
    else:
        checks.record("migration_compatible", True,
                      "not checked: no --core-tree was supplied")

    if args.expect_global_revision is not None:
        if "core_state" not in tables:
            checks.record("global_revision", False,
                          "the database has no core_state table")
        else:
            row = conn.execute(
                "SELECT global_revision FROM core_state WHERE singleton=1").fetchone()
            revision = int(row[0]) if row else None
            checks.record("global_revision", revision == args.expect_global_revision,
                          f"global_revision = {revision} "
                          f"(expected {args.expect_global_revision})")

    for table in args.expect_table or []:
        checks.record(f"table_present:{table}", table in tables,
                      f"{'present' if table in tables else 'absent'}")
    for table in args.expect_no_table or []:
        checks.record(f"table_absent:{table}", table not in tables,
                      f"{'absent' if table not in tables else 'still present'}")

    for spec in args.expect_row_count or []:
        name, _, count = spec.partition("=")
        name = name.strip()
        if name not in tables:
            checks.record(f"row_count:{name}", False, "table does not exist")
            continue
        try:
            expected_count = int(count)
        except ValueError:
            checks.record(f"row_count:{name}", False, f"malformed expectation {spec!r}")
            continue
        actual_count = conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        checks.record(f"row_count:{name}", int(actual_count) == expected_count,
                      f"{name} has {actual_count} rows (expected {expected_count})")

    try:
        if "core_state" in tables:
            revision_row = conn.execute(
                "SELECT global_revision FROM core_state WHERE singleton=1").fetchone()
            revision = int(revision_row[0]) if revision_row else None
        else:
            revision = None
        checks.record("read_only_report", True,
                      f"global_revision = {revision}, tables = {len(tables)}")
    except sqlite3.Error as error:
        checks.error("read_only_report", str(error))
    return conn


def check_capture(capture_dir: str, db_path: str | None, checks: Checks) -> None:
    """Verify a capture package and, when possible, that the database is its image."""
    manifest_path = os.path.join(capture_dir, "manifest.json")
    marker_path = os.path.join(capture_dir, "CAPTURE_VERIFIED")
    sums_path = os.path.join(capture_dir, "SHA256SUMS")
    incomplete = os.path.join(capture_dir, "CAPTURE_INCOMPLETE")

    checks.record("capture_complete", not os.path.exists(incomplete),
                  "CAPTURE_INCOMPLETE absent" if not os.path.exists(incomplete)
                  else "CAPTURE_INCOMPLETE is present: the capture is incomplete")

    try:
        with open(manifest_path, "rb") as handle:
            manifest_bytes = handle.read()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, ValueError) as error:
        checks.record("capture_manifest", False,
                      f"{manifest_path} is unreadable: {error}")
        manifest = None
        manifest_bytes = b""
    if manifest is not None:
        checks.record("capture_manifest", True,
                      f"capture_status = {manifest.get('capture_status')!r}")
        checks.record("capture_status", manifest.get("capture_status") == "verified",
                      f"capture_status = {manifest.get('capture_status')!r} "
                      "(expected 'verified')")

    try:
        marker = open(marker_path, encoding="utf-8").read().strip()
    except OSError as error:
        checks.record("capture_marker", False,
                      f"{marker_path} is unreadable: {error}")
        marker = ""
    expected_marker = f"manifest_sha256={hashlib.sha256(manifest_bytes).hexdigest()}"
    checks.record("capture_marker", marker == expected_marker,
                  "CAPTURE_VERIFIED matches the manifest digest" if marker == expected_marker
                  else "CAPTURE_VERIFIED marker does not match the manifest digest")

    try:
        lines = open(sums_path, encoding="utf-8").read().splitlines()
    except OSError as error:
        checks.record("capture_checksums", False, f"{sums_path} is unreadable: {error}")
        lines = []
    digests = {}
    problems = []
    for line in lines:
        if not line.strip():
            continue
        try:
            digest, relative = line.split("  ", 1)
        except ValueError:
            problems.append(f"malformed SHA256SUMS line: {line!r}")
            continue
        digests[relative] = digest
        target = os.path.join(capture_dir, relative)
        if not os.path.isfile(target):
            problems.append(f"missing capture file: {relative}")
            continue
        actual = sha256_file(target)
        if actual != digest:
            problems.append(f"digest mismatch for {relative}")
    checks.record("capture_checksums", not problems,
                  "every SHA256SUMS entry matches" if not problems else "; ".join(problems))

    if db_path:
        captured = os.path.join(capture_dir, "sqlite", "minisa.db")
        if not os.path.isfile(captured):
            checks.record("capture_database_match", False,
                          f"{captured} does not exist")
        elif not os.path.isfile(db_path):
            checks.record("capture_database_match", False,
                          f"{db_path} does not exist")
        else:
            restored = sha256_file(db_path)
            expected = digests.get("sqlite/minisa.db") or sha256_file(captured)
            checks.record("capture_database_match", restored == expected,
                          "the database is byte-identical to the captured image"
                          if restored == expected else
                          f"database digest {restored[:16]}… differs from the captured image "
                          f"{expected[:16]}…")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", help="database file to verify (opened read-only)")
    parser.add_argument("--core-tree",
                        help="directory containing the `core` package to interrogate "
                             "(for example /srv/minisa/app or a release root)")
    parser.add_argument("--core-only", action="store_true",
                        help="only report the core tree's supported schema version and chain")
    parser.add_argument("--expect-schema-version", type=int,
                        help="required installation.schema_version")
    parser.add_argument("--expect-migrations",
                        help="required applied migration versions, comma separated")
    parser.add_argument("--expect-integrity", default="ok",
                        help="required PRAGMA integrity_check result (default: ok)")
    parser.add_argument("--expect-global-revision", type=int,
                        help="required core_state.global_revision")
    parser.add_argument("--expect-table", action="append",
                        help="table that must be present (repeatable)")
    parser.add_argument("--expect-no-table", action="append",
                        help="table that must be absent (repeatable)")
    parser.add_argument("--expect-row-count", action="append", metavar="TABLE=N",
                        help="required row count for a table, for example "
                             "memory_records=0 (repeatable)")
    parser.add_argument("--capture-dir",
                        help="capture package to verify; also compares the database with "
                             "the captured image")
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    args = parser.parse_args(argv)

    if not args.db and not args.core_only:
        parser.error("either --db or --core-only is required")
    if args.db and not os.path.isfile(args.db):
        print(f"ERROR: database not found: {args.db}", file=sys.stderr)
        return 2

    checks = Checks()
    core_info = None
    if args.core_tree:
        try:
            core_info = probe_core_tree(args.core_tree)
        except (OSError, RuntimeError) as error:
            checks.error("core_tree", str(error))
            core_info = None
        else:
            checks.record("core_tree", True,
                          f"{args.core_tree} supports schema version "
                          f"{core_info['supported_schema_version']}")

    conn = None
    if args.db:
        conn = check_database(args.db, args, checks, core_info)
    if args.capture_dir:
        check_capture(args.capture_dir, args.db, checks)
    if conn is not None:
        conn.close()

    failed = checks.failed
    report = {
        "database": args.db,
        "core_tree": args.core_tree,
        "core_supported_schema_version": (core_info or {}).get("supported_schema_version"),
        "capture_dir": args.capture_dir,
        "verdict": "passed" if not failed else "failed",
        "checks": checks.results,
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(checks.render())
        print(f"VERDICT: {report['verdict'].upper()}")
        if failed:
            print("FAILED CHECKS: " + ", ".join(r["check"] for r in failed))

    if any(r["status"] == ERROR for r in checks.results):
        return 2
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
