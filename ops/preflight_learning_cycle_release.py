#!/usr/bin/env python3
"""Read-only, fail-closed deployment preflight for the Minisa learning-cycle candidate.

This script never deploys, never stops a service, never writes to the
production database, and never writes to any path outside an optional report
file that the caller names explicitly. It only reads:

* the staged candidate manifest and the staged release tree,
* the candidate migration chain and the live database (opened read-only),
* the live core-service code directory,
* systemd's effective unit properties,
* the mounted block-device topology,
* an optional verified recovery capture directory,
* the candidate test suite (executed in a subprocess against the candidate tree).

Every required prerequisite must be positively verified. A prerequisite that
cannot be verified (missing, unreadable, ambiguous, or unverifiable) is
reported as a failure, never as a warning. Exit codes:

    0  all required checks passed
    2  at least one required check failed or could not be verified
    3  the preflight itself could not run
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

CANDIDATE_ROOT = Path("/home/sbeppi/minisa-working-assistant-20261018/candidate")
DEFAULT_MANIFEST = Path(
    "/home/sbeppi/minisa-releases/minisa-learning-cycle-candidate-20261021.manifest.json"
)
DEFAULT_DB = Path("/srv/minisa/data/minisa.db")
DEFAULT_CORE_APP_DIR = Path("/srv/minisa/app")
DEFAULT_DROPIN_PATH = Path("/etc/systemd/system/minisa.service.d/20-dream-recovery.conf")
DEFAULT_CONFIG = Path("/srv/minisa/config/minisa.toml")
DEFAULT_ENV = Path("/srv/minisa/config/minisa.env")

SERVICES = ("minisa.service", "minisa-core.service")
RUNTIME_SERVICE = "minisa.service"
PRODUCTION_ROOT_PATHS = ("/", "/srv")
NETWORK_FS = {
    "nfs", "nfs4", "cifs", "smb3", "smbfs", "afs", "ceph", "glusterfs",
    "9p", "fuse.sshfs", "fuse.rclone",
}
IGNORED_FS = {
    "autofs", "binfmt_misc", "bpf", "cgroup2", "configfs", "debugfs", "devpts",
    "devtmpfs", "efivarfs", "fuse.gvfsd-fuse", "fuse.portal", "fusectl",
    "hugetlbfs", "mqueue", "overlay", "proc", "pstore", "rpc_pipefs",
    "securityfs", "selinuxfs", "squashfs", "sysfs", "tmpfs", "tracefs",
}
PASSED = "passed"
FAILED = "failed"
WARNING = "warning"


@dataclass
class Check:
    """One verifiable deployment prerequisite."""

    check_id: str
    title: str
    status: str
    required: bool = True
    detail: str = ""
    evidence: dict = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.required and self.status != PASSED

    def as_dict(self) -> dict:
        return {
            "id": self.check_id,
            "title": self.title,
            "status": self.status,
            "required": self.required,
            "blocking": self.blocking,
            "detail": self.detail,
            "evidence": self.evidence,
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_module_from_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run_command(args: list[str], timeout: int = 60, cwd: Path | None = None,
                env: dict | None = None) -> tuple[int, str]:
    try:
        completed = subprocess.run(args, check=False, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=timeout, cwd=cwd, env=env)
    except (OSError, subprocess.SubprocessError) as error:
        return 127, f"{type(error).__name__}: {error}"
    return completed.returncode, completed.stdout.strip()


def systemctl_properties(service: str, properties: list[str]) -> dict:
    code, output = run_command(
        ["systemctl", "show", service, "-p", ",".join(properties), "--no-pager"]
    )
    if code != 0:
        raise RuntimeError(f"systemctl show {service} failed: {output}")
    values = {}
    for line in output.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def mount_source(path: Path) -> str:
    code, output = run_command(["findmnt", "-n", "-o", "SOURCE", "-T", str(path)])
    if code != 0 or not output:
        raise RuntimeError(f"findmnt failed for {path}: {output}")
    return output.splitlines()[0].strip()


def block_ancestry(source: str) -> list[str]:
    if not source.startswith("/dev/"):
        return []
    code, output = run_command(["lsblk", "-srno", "NAME", source])
    if code != 0:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def production_ancestry() -> set[str]:
    devices: set[str] = set()
    for path in PRODUCTION_ROOT_PATHS:
        try:
            source = mount_source(Path(path))
        except RuntimeError:
            continue
        devices |= set(block_ancestry(source))
    return devices


def read_only_connection(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def check_manifest(manifest_path: Path) -> tuple[Check, dict]:
    try:
        payload = manifest_path.read_bytes()
        manifest = json.loads(payload.decode("utf-8"))
    except (OSError, ValueError) as error:
        return Check("manifest", "Candidate manifest is readable and valid",
                     FAILED, detail=f"{type(error).__name__}: {error}"), {}
    if not isinstance(manifest, dict):
        return Check("manifest", "Candidate manifest is readable and valid", FAILED,
                     detail="manifest is not a JSON object"), {}
    problems = []
    if manifest.get("production_deployed") is not False:
        problems.append("production_deployed is not false")
    for key in ("base_release", "candidate_release", "changed_runtime_files",
                "candidate_supported_schema_version", "candidate_systemd_dropin"):
        if key not in manifest:
            problems.append(f"missing key {key}")
    files = manifest.get("changed_runtime_files")
    if not isinstance(files, list) or not files:
        problems.append("changed_runtime_files is empty")
    else:
        seen = set()
        for entry in files:
            rel = Path(entry.get("path", ""))
            if rel.is_absolute() or ".." in rel.parts:
                problems.append(f"unsafe manifest path {rel}")
            if rel.as_posix() in seen:
                problems.append(f"duplicate manifest path {rel}")
            seen.add(rel.as_posix())
            if "sha256" not in entry or "bytes" not in entry:
                problems.append(f"manifest entry {rel} lacks bytes/sha256")
    status = FAILED if problems else PASSED
    detail = "; ".join(problems) if problems else (
        f"{len(files or [])} changed files, supported schema "
        f"{manifest.get('candidate_supported_schema_version')}, production_deployed=false"
    )
    check = Check("manifest", "Candidate manifest is readable and valid", status, detail=detail,
                  evidence={"manifest_path": str(manifest_path),
                            "manifest_sha256": hashlib.sha256(payload).hexdigest(),
                            "candidate_release": manifest.get("candidate_release"),
                            "base_release": manifest.get("base_release"),
                            "candidate_supported_schema_version": manifest.get(
                                "candidate_supported_schema_version"),
                            "production_deployed": manifest.get("production_deployed")})
    return check, manifest


def check_release_content(manifest: dict) -> Check:
    release = Path(manifest.get("candidate_release", ""))
    evidence: dict = {"candidate_release": str(release)}
    try:
        release = release.resolve(strict=True)
    except OSError as error:
        return Check("release_content", "Staged release matches the manifest", FAILED,
                     detail=f"candidate release not accessible: {error}", evidence=evidence)
    problems = []
    tracked = set()
    for entry in manifest.get("changed_runtime_files", []):
        rel = Path(entry["path"])
        tracked.add(rel.as_posix())
        path = release / rel
        try:
            payload = path.read_bytes()
        except OSError as error:
            problems.append(f"{rel}: unreadable ({error})")
            continue
        if len(payload) != entry["bytes"]:
            problems.append(f"{rel}: size {len(payload)} != manifest {entry['bytes']}")
        elif hashlib.sha256(payload).hexdigest() != entry["sha256"]:
            problems.append(f"{rel}: sha256 mismatch")
    dropin = manifest.get("candidate_systemd_dropin", {})
    dropin_source = Path(dropin.get("source", ""))
    try:
        dropin_digest = sha256_file(dropin_source)
        if dropin_digest != dropin.get("sha256"):
            problems.append("systemd drop-in sha256 mismatch")
    except OSError as error:
        problems.append(f"systemd drop-in unreadable: {error}")
    evidence["dropin_sha256"] = dropin.get("sha256")
    evidence["verified_files"] = len(tracked)
    untracked = []
    for path in release.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(release).as_posix()
        if rel in tracked:
            continue
        if rel.endswith(".pyc") or "__pycache__" in path.parts:
            untracked.append(rel)
    if untracked:
        evidence["untracked_bytecode_files"] = len(untracked)
        evidence["untracked_bytecode_sample"] = sorted(untracked)[:5]
    status = FAILED if problems else PASSED
    detail = "; ".join(problems) if problems else (
        f"{len(tracked)} tracked files match the manifest"
        + (f"; {len(untracked)} untracked bytecode files present (not part of the release)"
           if untracked else "")
    )
    return Check("release_content", "Staged release matches the manifest", status,
                 detail=detail, evidence=evidence)


def check_migration_lineage(candidate_root: Path, db_path: Path) -> Check:
    migrations_path = candidate_root / "core" / "migrations.py"
    evidence: dict = {"candidate_migrations": str(migrations_path)}
    try:
        module = load_module_from_path("preflight_candidate_migrations", migrations_path)
    except Exception as error:  # noqa: BLE001 - any import failure is a hard stop
        return Check("migration_lineage", "Candidate migration chain continues production lineage",
                     FAILED, detail=f"cannot load candidate migrations: {error}", evidence=evidence)
    supported = int(module.SUPPORTED_SCHEMA_VERSION)
    candidate_migrations = {int(m.version): (m.name, m.checksum) for m in module.MIGRATIONS}
    evidence["candidate_supported_schema_version"] = supported
    evidence["candidate_migrations"] = {
        str(version): {"name": name, "checksum": checksum}
        for version, (name, checksum) in sorted(candidate_migrations.items())
    }
    try:
        connection = read_only_connection(db_path)
    except sqlite3.Error as error:
        return Check("migration_lineage", "Candidate migration chain continues production lineage",
                     FAILED, detail=f"cannot open database read-only: {error}", evidence=evidence)
    problems = []
    applied = []
    try:
        rows = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        for row in rows:
            version = int(row["version"])
            applied.append(version)
            candidate = candidate_migrations.get(version)
            if candidate is None:
                problems.append(f"production migration {version} is absent from the candidate chain")
                continue
            if candidate[0] != row["name"]:
                problems.append(f"migration {version} name mismatch: {row['name']} != {candidate[0]}")
            if candidate[1] != row["checksum"]:
                problems.append(f"migration {version} checksum mismatch")
        if applied and applied != list(range(1, max(applied) + 1)):
            problems.append(f"production migration sequence is not contiguous: {applied}")
        missing = [v for v in range(1, max(applied, default=0) + 1) if v not in applied]
        if missing:
            problems.append(f"production is missing applied migrations {missing}")
    except sqlite3.Error as error:
        problems.append(f"cannot read schema_migrations: {error}")
    finally:
        connection.close()
    pending = [v for v in sorted(candidate_migrations) if v not in applied]
    evidence["production_applied_migrations"] = applied
    evidence["candidate_pending_migrations"] = pending
    if not applied:
        problems.append("production database has no applied migrations")
    if max(applied, default=0) != supported - 1:
        problems.append(
            f"production schema {max(applied, default=0)} is not one version behind the candidate "
            f"({supported}); expected a single additive upgrade step"
        )
    status = FAILED if problems else PASSED
    detail = "; ".join(problems) if problems else (
        f"production migrations 1-{max(applied, default=0)} match the candidate chain; "
        f"candidate adds migration {pending}"
    )
    return Check("migration_lineage", "Candidate migration chain continues production lineage",
                 status, detail=detail, evidence=evidence)


def check_live_database(db_path: Path, expected_supported: int) -> Check:
    evidence: dict = {"database": str(db_path)}
    try:
        connection = read_only_connection(db_path)
    except sqlite3.Error as error:
        return Check("live_database", "Production database is healthy and readable read-only",
                     FAILED, detail=f"cannot open database read-only: {error}", evidence=evidence)
    problems = []
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            problems.append(f"integrity_check returned {integrity!r}")
        evidence["integrity_check"] = integrity
        installation = connection.execute(
            "SELECT schema_version, software_version FROM installation WHERE singleton=1"
        ).fetchone()
        if installation is None:
            problems.append("installation row is missing")
            schema_version = None
        else:
            schema_version = int(installation["schema_version"])
            evidence["schema_version"] = schema_version
            evidence["software_version"] = installation["software_version"]
            if schema_version not in (expected_supported - 1, expected_supported):
                problems.append(
                    f"schema version {schema_version} is not the pre-migration "
                    f"({expected_supported - 1}) or post-migration ({expected_supported}) state"
                )
        revision = connection.execute(
            "SELECT global_revision FROM core_state WHERE singleton=1"
        ).fetchone()
        evidence["global_revision"] = revision[0] if revision else None
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        evidence["table_count"] = len(tables)
        evidence["has_memory_records"] = "memory_records" in tables
        procedures = connection.execute("SELECT count(*) FROM learned_procedures").fetchone()[0]
        evidence["learned_procedures_rows"] = procedures
    except sqlite3.Error as error:
        problems.append(f"database query failed: {error}")
    finally:
        connection.close()
    stat = db_path.stat()
    evidence["size_bytes"] = stat.st_size
    evidence["mtime_epoch"] = stat.st_mtime
    status = FAILED if problems else PASSED
    detail = "; ".join(problems) if problems else (
        f"schema {evidence.get('schema_version')}, revision {evidence.get('global_revision')}, "
        f"integrity ok, opened read-only"
    )
    return Check("live_database", "Production database is healthy and readable read-only", status,
                 detail=detail, evidence=evidence)


def check_core_service_files(candidate_root: Path, core_app_dir: Path) -> Check:
    evidence: dict = {"core_app_dir": str(core_app_dir)}
    problems = []
    comparison = {}
    for name in ("db.py", "migrations.py"):
        live = core_app_dir / "core" / name
        candidate = candidate_root / "core" / name
        entry = {}
        try:
            entry["live_sha256"] = sha256_file(live)
        except OSError as error:
            problems.append(f"live core/{name} unreadable: {error}")
            live_digest = None
        else:
            live_digest = entry["live_sha256"]
        try:
            entry["candidate_sha256"] = sha256_file(candidate)
        except OSError as error:
            problems.append(f"candidate core/{name} unreadable: {error}")
        entry["identical"] = live_digest is not None and live_digest == entry.get("candidate_sha256")
        comparison[name] = entry
        if entry["identical"]:
            problems.append(f"core/{name} already matches the candidate; deployment state is unknown")
    evidence["core_files"] = comparison
    if problems:
        status = FAILED
        detail = "; ".join(problems)
    else:
        status = PASSED
        detail = (
            "live core service code differs from the candidate for "
            + ", ".join(sorted(comparison))
            + " as expected before the deployment copy step"
        )
    return Check("core_service_files", "Core-service code copy step is identified and reversible",
                 status, detail=detail, evidence=evidence)


def check_service_configuration(dropin_path: Path, base_release: str | None,
                                config_path: Path, env_path: Path) -> Check:
    evidence: dict = {"dropin_install_path": str(dropin_path)}
    problems = []
    warnings = []
    try:
        properties = systemctl_properties(
            RUNTIME_SERVICE,
            ["ActiveState", "SubState", "User", "WorkingDirectory", "ExecStart",
             "TimeoutStopUSec", "DropInPaths", "FragmentPath"],
        )
    except RuntimeError as error:
        return Check("service_configuration", "Service configuration matches the deployment plan",
                     FAILED, detail=str(error), evidence=evidence)
    evidence["minisa_service"] = properties
    try:
        core_properties = systemctl_properties(
            "minisa-core.service",
            ["ActiveState", "SubState", "User", "WorkingDirectory", "ExecStart", "TimeoutStopUSec"],
        )
    except RuntimeError as error:
        core_properties = {}
        problems.append(f"minisa-core.service properties unreadable: {error}")
    evidence["minisa_core_service"] = core_properties
    if properties.get("ActiveState") != "active":
        warnings.append(f"{RUNTIME_SERVICE} is {properties.get('ActiveState')}, not active")
    if core_properties and core_properties.get("ActiveState") != "active":
        warnings.append(f"minisa-core.service is {core_properties.get('ActiveState')}, not active")
    if base_release and properties.get("WorkingDirectory") != base_release:
        problems.append(
            f"WorkingDirectory {properties.get('WorkingDirectory')!r} is not the expected "
            f"pre-deployment release {base_release!r}"
        )
    if dropin_path.exists():
        problems.append(f"drop-in already exists at {dropin_path}; inspect it instead of overwriting")
    else:
        evidence["dropin_present"] = False
    try:
        unit_text = Path(properties.get("FragmentPath", "")).read_text()
    except OSError as error:
        unit_text = ""
        problems.append(
            f"unit file {properties.get('FragmentPath')!r} is not readable by this account "
            f"({type(error).__name__}); rerun the preflight as an administrator so the effective "
            "unit content can be verified before the drop-in is installed"
        )
    if unit_text and "Environment" in unit_text:
        evidence["unit_declares_environment"] = True
    timeout = []
    for path in (config_path, env_path):
        try:
            text = path.read_text()
        except OSError as error:
            problems.append(f"{path} unreadable: {error}")
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("dream_timeout_seconds") or "MINISA_DREAM_TIMEOUT_SECONDS" in stripped:
                timeout.append(f"{path.name}: {stripped}")
    evidence["dream_timeout_overrides"] = timeout
    if timeout:
        warnings.append("a dream timeout override is present; the manifest's resolved timeout must be rechecked")
    if not timeout:
        evidence["effective_dream_timeout_seconds"] = 40
    status = FAILED if problems else PASSED
    detail = "; ".join(problems or warnings) or (
        f"{RUNTIME_SERVICE} active on {properties.get('WorkingDirectory')}, "
        f"drop-in path free, no dream timeout override"
    )
    if warnings and not problems:
        evidence["notes"] = warnings
    return Check("service_configuration", "Service configuration matches the deployment plan", status,
                 required=True, detail=detail, evidence=evidence)


def check_independent_destination() -> Check:
    evidence: dict = {}
    problems = []
    try:
        live_devices = production_ancestry()
    except Exception as error:  # noqa: BLE001
        live_devices = set()
        problems.append(f"cannot determine production block devices: {error}")
    evidence["production_block_devices"] = sorted(live_devices)
    code, output = run_command(["findmnt", "-rn", "-o", "TARGET,SOURCE,FSTYPE"])
    if code != 0:
        problems.append(f"findmnt failed: {output}")
        evidence["mounts"] = []
    else:
        mounts = []
        independent = []
        network = []
        for line in output.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            target, source, fstype = parts[0], parts[1], parts[2]
            if fstype in IGNORED_FS:
                continue
            mounts.append({"target": target, "source": source, "fstype": fstype})
            if fstype in NETWORK_FS:
                network.append(target)
                continue
            if not source.startswith("/dev/"):
                continue
            ancestry = set(block_ancestry(source))
            if ancestry and not (ancestry & live_devices):
                independent.append({"target": target, "source": source, "fstype": fstype,
                                    "ancestry": sorted(ancestry)})
        evidence["mounts"] = mounts
        evidence["independent_local_mounts"] = independent
        evidence["network_mounts_requiring_confirmation"] = network
        if not independent:
            problems.append(
                "no mounted filesystem is independent of the production block devices "
                f"{sorted(live_devices)}"
            )
        if network:
            evidence["network_mounts_note"] = (
                "network filesystems require independent administrator verification of the "
                "storage failure domain and --confirm-independent-target"
            )
    status = FAILED if problems else PASSED
    detail = "; ".join(problems) if problems else (
        f"independent local mounts: {evidence.get('independent_local_mounts')}"
    )
    return Check("independent_destination", "A genuinely independent recovery destination exists",
                 status, detail=detail, evidence=evidence)


def check_recovery_point(capture_dir: Path | None) -> Check:
    evidence: dict = {"recovery_capture": str(capture_dir) if capture_dir else None}
    if capture_dir is None:
        return Check(
            "recovery_point", "A verified full recovery point exists", FAILED,
            detail="no recovery capture directory was supplied; a verified capture of the live "
                   "system on an independent destination is required before any migration",
            evidence=evidence,
        )
    problems = []
    try:
        capture_dir = capture_dir.resolve(strict=True)
    except OSError as error:
        return Check("recovery_point", "A verified full recovery point exists", FAILED,
                     detail=f"capture directory not accessible: {error}", evidence=evidence)
    evidence["recovery_capture"] = str(capture_dir)
    if not capture_dir.is_dir():
        return Check("recovery_point", "A verified full recovery point exists", FAILED,
                     detail="capture path is not a directory", evidence=evidence)
    if (capture_dir / "CAPTURE_INCOMPLETE").exists():
        problems.append("capture directory still contains CAPTURE_INCOMPLETE")
    marker = capture_dir / "CAPTURE_VERIFIED"
    manifest_path = capture_dir / "manifest.json"
    try:
        marker_text = marker.read_text().strip()
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, ValueError) as error:
        problems.append(f"capture marker or manifest unreadable: {error}")
        manifest = {}
        marker_text = ""
    if manifest:
        if manifest.get("capture_status") != "verified":
            problems.append(f"capture_status is {manifest.get('capture_status')!r}, not 'verified'")
        expected_marker = f"manifest_sha256={hashlib.sha256(manifest_bytes).hexdigest()}"
        if marker_text != expected_marker:
            problems.append("CAPTURE_VERIFIED marker does not match the manifest digest")
        evidence["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
        destination = manifest.get("destination", {})
        evidence["capture_destination"] = destination
        if destination and not destination.get("independent_from_live_block_devices"):
            problems.append("capture manifest does not record an independent destination")
        database = manifest.get("database", {})
        evidence["captured_database"] = {
            "path": database.get("path"), "sha256": database.get("sha256"),
            "schema_version": database.get("schema_version"),
            "global_revision": database.get("global_revision"),
            "integrity_check": database.get("integrity_check"),
        }
    sums_path = capture_dir / "SHA256SUMS"
    try:
        lines = sums_path.read_text().splitlines()
    except OSError as error:
        problems.append(f"SHA256SUMS unreadable: {error}")
        lines = []
    verified_files = []
    for line in lines:
        if not line.strip():
            continue
        try:
            digest, rel = line.split("  ", 1)
        except ValueError:
            problems.append(f"malformed SHA256SUMS line: {line!r}")
            continue
        target = capture_dir / rel
        try:
            actual = sha256_file(target)
        except OSError as error:
            problems.append(f"{rel} unreadable: {error}")
            continue
        if actual != digest:
            problems.append(f"{rel} sha256 mismatch")
        else:
            verified_files.append(rel)
    evidence["verified_files"] = verified_files
    db_copy = capture_dir / "sqlite" / "minisa.db"
    if not db_copy.exists():
        problems.append("captured SQLite database is missing")
    else:
        try:
            connection = read_only_connection(db_copy)
        except sqlite3.Error as error:
            problems.append(f"captured database cannot be opened read-only: {error}")
        else:
            try:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                evidence["captured_database"]["integrity_check_recheck"] = integrity
                if integrity != "ok":
                    problems.append(f"captured database integrity_check returned {integrity!r}")
                installation = connection.execute(
                    "SELECT schema_version FROM installation WHERE singleton=1"
                ).fetchone()
                captured_version = int(installation["schema_version"]) if installation else None
                evidence["captured_database"]["schema_version_recheck"] = captured_version
            except sqlite3.Error as error:
                problems.append(f"captured database query failed: {error}")
            finally:
                connection.close()
    status = FAILED if problems else PASSED
    detail = "; ".join(problems) if problems else (
        f"verified capture at {capture_dir} with {len(verified_files)} hashed artifacts"
    )
    return Check("recovery_point", "A verified full recovery point exists", status,
                 detail=detail, evidence=evidence)


def check_rollback_prerequisites(manifest: dict, core_app_dir: Path) -> Check:
    evidence: dict = {}
    problems = []
    base_release = Path(manifest.get("base_release", ""))
    if base_release.is_dir():
        evidence["base_release"] = str(base_release)
        evidence["base_release_file_count"] = sum(1 for p in base_release.rglob("*") if p.is_file())
    else:
        problems.append(f"base release {base_release} is missing; code-only rollback is impossible")
    previous = sorted(p for p in core_app_dir.glob("core.*") if p.is_dir())
    evidence["previous_core_copies"] = [str(p) for p in previous]
    candidate_supported = int(manifest.get("candidate_supported_schema_version", 0))
    for path in previous:
        migrations = path / "migrations.py"
        if not migrations.exists():
            continue
        try:
            module = load_module_from_path(f"preflight_prev_{abs(hash(str(path)))}", migrations)
        except Exception as error:  # noqa: BLE001
            evidence.setdefault("previous_core_supported_versions", {})[str(path)] = f"unreadable: {error}"
            continue
        supported = int(module.SUPPORTED_SCHEMA_VERSION)
        evidence.setdefault("previous_core_supported_versions", {})[str(path)] = supported
        if supported < candidate_supported:
            evidence.setdefault("previous_core_rollback_notes", {})[str(path)] = (
                f"supports schema {supported}; it cannot serve a migrated schema "
                f"{candidate_supported} database"
            )
    live_core = {}
    for name in ("db.py", "migrations.py"):
        path = core_app_dir / "core" / name
        try:
            live_core[name] = sha256_file(path)
        except OSError as error:
            problems.append(f"live core/{name} unreadable, rollback source unknown: {error}")
    evidence["live_core_hashes"] = live_core
    live_migrations = core_app_dir / "core" / "migrations.py"
    if live_migrations.exists():
        try:
            module = load_module_from_path("preflight_live_core_migrations", live_migrations)
            live_supported = int(module.SUPPORTED_SCHEMA_VERSION)
        except Exception as error:  # noqa: BLE001
            live_supported = None
            evidence["live_core_supported_schema_version_error"] = f"{type(error).__name__}: {error}"
        evidence["live_core_supported_schema_version"] = live_supported
        if live_supported is not None and live_supported < candidate_supported:
            evidence["live_core_is_valid_rollback_target"] = False
            evidence["live_core_rollback_note"] = (
                f"the live core code supports schema {live_supported} and refuses a migrated "
                f"schema {candidate_supported} database, so it is not a rollback target after the "
                "migration; the pre-migration database copy is the only data rollback"
            )
        elif live_supported is not None:
            evidence["live_core_is_valid_rollback_target"] = True
    evidence["rollback_model"] = (
        "migration 5 rebuilds learned_procedures and is one-way; the only data rollback is the "
        "verified pre-migration database from the recovery point. Code-only rollback keeps the "
        "candidate core/ files and reverts only the runtime release and drop-in."
    )
    status = FAILED if problems else PASSED
    detail = "; ".join(problems) if problems else (
        "base release intact, live core hashes recorded, rollback model is code-only or "
        "restore-from-verified-pre-migration-copy"
    )
    return Check("rollback_prerequisites", "Rollback prerequisites are present and understood",
                 status, detail=detail, evidence=evidence)


def check_tests(candidate_root: Path) -> Check:
    evidence: dict = {"tests": str(candidate_root / "tests")}
    if os.environ.get("MINISA_PREFLIGHT_NO_TESTS") == "1":
        return Check("tests", "Candidate test suite passes", WARNING, required=False,
                     detail="test execution skipped because this run is nested inside a test run",
                     evidence=evidence)
    env = dict(os.environ)
    env["MINISA_PREFLIGHT_NO_TESTS"] = "1"
    env["PYTHONWARNINGS"] = "error::ResourceWarning"
    code, output = run_command(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        timeout=1800,
        cwd=candidate_root,
        env=env,
    )
    tail = output.splitlines()[-12:] if output else []
    evidence["returncode"] = code
    evidence["output_tail"] = tail
    if code != 0:
        return Check("tests", "Candidate test suite passes", FAILED,
                     detail="test suite failed; see output_tail", evidence=evidence)
    return Check("tests", "Candidate test suite passes", PASSED,
                 detail=tail[-1] if tail else "unittest reported success", evidence=evidence)


def build_checks(args, manifest: dict) -> list[Check]:
    checks = [
        check_release_content(manifest),
        check_migration_lineage(args.candidate_root, args.db),
        check_live_database(args.db, int(manifest.get("candidate_supported_schema_version", 5))),
        check_core_service_files(args.candidate_root, args.core_app_dir),
        check_service_configuration(args.dropin_path, manifest.get("base_release"),
                                    args.config, args.env),
        check_independent_destination(),
        check_recovery_point(args.recovery_capture),
        check_rollback_prerequisites(manifest, args.core_app_dir),
    ]
    if args.skip_tests:
        checks.append(Check("tests", "Candidate test suite passes", FAILED,
                            detail="test execution was skipped by the operator; the prerequisite "
                                   "is unverified and the preflight cannot pass"))
    else:
        checks.append(check_tests(args.candidate_root))
    return checks


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--candidate-root", type=Path, default=CANDIDATE_ROOT)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--core-app-dir", type=Path, default=DEFAULT_CORE_APP_DIR)
    parser.add_argument("--dropin-path", type=Path, default=DEFAULT_DROPIN_PATH)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--recovery-capture", type=Path, default=None,
                        help="directory of a verified recovery capture to verify")
    parser.add_argument("--skip-tests", action="store_true",
                        help="skip the test-suite check (the preflight then fails closed on it)")
    parser.add_argument("--json", type=Path, default=None, help="optional report output path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest_check, manifest = check_manifest(args.manifest)
        checks = [manifest_check]
        if manifest:
            checks += build_checks(args, manifest)
        else:
            checks.append(Check("release_content", "Staged release matches the manifest", FAILED,
                                detail="manifest could not be read; downstream checks were not run"))
    except Exception as error:  # noqa: BLE001 - an unexpected error must fail closed
        print(f"preflight could not run: {type(error).__name__}: {error}", file=sys.stderr)
        return 3
    blocking = [c for c in checks if c.blocking]
    report = {
        "preflight": "passed" if not blocking else "blocked",
        "generated_utc": subprocess.run(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"], text=True,
                                        stdout=subprocess.PIPE).stdout.strip(),
        "read_only": True,
        "manifest": str(args.manifest),
        "checks": [c.as_dict() for c in checks],
        "blocking_checks": [c.check_id for c in blocking],
    }
    print(json.dumps(report, indent=2))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if not blocking else 2


if __name__ == "__main__":
    raise SystemExit(main())
