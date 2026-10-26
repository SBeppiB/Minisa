#!/usr/bin/env python3
"""Quiesced, verified Minisa recovery capture. Production mode requires root and an explicit flag."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import signal
import stat
from datetime import datetime, timezone
from urllib.parse import quote

SERVICES = ("minisa.service", "minisa-core.service")
SOURCE = Path("/srv/minisa")
ACTIVE_RELEASE = Path("/home/sbeppi/minisa-releases/response-context-20261017T010152Z")
UNITS = (
    Path("/etc/systemd/system/minisa.service"),
    Path("/etc/systemd/system/minisa-core.service"),
    Path("/etc/systemd/system/minisa.service.d"),
    Path("/etc/systemd/system/minisa-core.service.d"),
    Path("/usr/lib/systemd/system/service.d/10-timeout-abort.conf"),
    Path("/etc/systemd/system/multi-user.target.wants/minisa.service"),
    Path("/etc/systemd/system/multi-user.target.wants/minisa-core.service"),
)
DB_REL = Path("data/minisa.db")
NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "sshfs", "fuse.sshfs"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def file_metadata(path: Path) -> dict:
    info = path.stat(follow_symlinks=False)
    attrs = {}
    for name in sorted(os.listxattr(path, follow_symlinks=False)):
        attrs[name] = hashlib.sha256(os.getxattr(path, name, follow_symlinks=False)).hexdigest()
    return {"mode": oct(stat.S_IMODE(info.st_mode)), "uid": info.st_uid, "gid": info.st_gid,
            "mtime_ns": info.st_mtime_ns, "xattr_sha256": attrs}


def copy_file_metadata(source: Path, dest: Path) -> None:
    """Copy metadata only; database contents always come from SQLite backup()."""
    subprocess.run(["cp", "--attributes-only", "--preserve=all", str(source), str(dest)],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def sqlite_backup(source: Path, dest: Path) -> dict:
    """Use SQLite online backup from a read-only source; never invoke app backup hooks."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    uri = f"file:{quote(str(source), safe='/')}?mode=ro"
    src = sqlite3.connect(uri, uri=True, timeout=30)
    out = None
    try:
        out = sqlite3.connect(dest)
        src.backup(out, pages=256, sleep=0.05)
        integrity = [r[0] for r in out.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise RuntimeError("SQLite backup integrity check failed")
        has_migrations = out.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
        schema = (out.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
                  if has_migrations else out.execute("PRAGMA user_version").fetchone()[0])
        user_version = out.execute("PRAGMA user_version").fetchone()[0]
        out.close()
        out = None
        copy_file_metadata(source, dest)
        return {"integrity_check": integrity, "schema_version": schema,
                "sqlite_user_version": user_version,
                "file_metadata": file_metadata(source)}
    finally:
        try:
            if out is not None:
                out.close()
        finally:
            src.close()


def archive_tree(archive: Path, source_root: Path, release: Path, units: tuple[Path, ...]) -> list[dict]:
    """Use GNU tar to preserve owners, ACLs, xattrs, and SELinux labels."""
    paths = [source_root, release, *units]
    for root in paths:
        if not root.exists():
            if root in units and root not in UNITS[:2]:
                continue  # optional unit drop-in directories can be absent
            raise FileNotFoundError(f"required recovery path missing: {root}")
    active_paths = [p for p in paths if p.exists()]
    archive_rel = source_root.as_posix().lstrip("/") + "/" + DB_REL.as_posix()
    excludes = [f"--exclude={archive_rel}{suffix}" for suffix in ("", "-wal", "-shm")]
    args = ["tar", "--acls", "--xattrs", "--xattrs-include=*", "--selinux", "--numeric-owner", "--format=pax",
            "-cpf", str(archive), "-C", "/", *excludes,
            *(p.as_posix().lstrip("/") for p in active_paths)]
    # The input paths are absolute paths under /; passing relative archive names
    # avoids GNU tar's leading-slash stripping ambiguity.
    subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with tarfile.open(archive, "r") as tf:
        records = []
        excluded_db_paths = {archive_rel, archive_rel + "-wal", archive_rel + "-shm"}
        if any(m.name in excluded_db_paths for m in tf.getmembers()):
            raise RuntimeError("raw canonical SQLite database or sidecar unexpectedly entered tar archive")
        for m in tf.getmembers():
            if not (m.isfile() or m.issym() or m.isdir()):
                raise RuntimeError(f"unsupported special or hard-linked archive entry: {m.name}")
            rec = {"path": m.name, "type": "file" if m.isfile() else "symlink" if m.issym() else "directory",
                   "mode": oct(m.mode), "uid": m.uid, "gid": m.gid}
            if m.isfile():
                stream = tf.extractfile(m)
                if stream is None:
                    raise RuntimeError(f"cannot read archived file: {m.name}")
                h = hashlib.sha256()
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    h.update(block)
                stream.close()
                rec.update(size=m.size, sha256=h.hexdigest())
            elif m.issym():
                rec["link_target"] = m.linkname
            records.append(rec)
        return records


def restore_rehearsal(archive: Path, db_copy: Path, scratch: Path, source_root: Path = SOURCE,
                      db_metadata: dict | None = None, expected_files: list[dict] | None = None) -> dict:
    """Extract only to a newly-created disposable directory and verify restored database."""
    scratch.mkdir(mode=0o700, parents=True, exist_ok=False)
    with tarfile.open(archive, "r") as tf:
        members = tf.getmembers()
        # Verify every path before using GNU tar to restore ACL/xattr/SELinux
        # metadata. Symlinks are extracted as links and never followed by tar.
        member_names = set()
        symlink_names = {Path(m.name) for m in members if m.issym()}
        canonical_db_rel = (source_root.as_posix().lstrip("/") + "/" + DB_REL.as_posix())
        for member in members:
            p = Path(member.name)
            normalized = p.as_posix().rstrip("/")
            if (p.is_absolute() or ".." in p.parts or normalized in {"", "."} or
                    member.name.rstrip("/") != normalized or normalized in member_names or
                    member.islnk() or not (member.isdir() or member.isfile() or member.issym())):
                raise RuntimeError("unsafe path in recovery archive")
            member_names.add(normalized)
            if any(parent in symlink_names for parent in p.parents):
                raise RuntimeError("archive contains a path beneath a symlink")
            if normalized in {canonical_db_rel, canonical_db_rel + "-wal", canonical_db_rel + "-shm"}:
                raise RuntimeError("archive conflicts with canonical database destination")
        subprocess.run(["tar", "--extract", "--file", str(archive), "--directory", str(scratch),
                        "--numeric-owner", "--same-owner", "--same-permissions", "--acls", "--xattrs",
                        "--xattrs-include=*", "--selinux"],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    db_dest = scratch / source_root.as_posix().lstrip("/") / DB_REL
    safe_parent = scratch
    for part in (source_root.as_posix().lstrip("/"), DB_REL.parent.as_posix()):
        for component in Path(part).parts:
            safe_parent = safe_parent / component
            if safe_parent.exists() or safe_parent.is_symlink():
                if safe_parent.is_symlink() or not safe_parent.is_dir():
                    raise RuntimeError("database restore destination traverses a non-directory")
            else:
                safe_parent.mkdir(mode=0o755)
    shutil.copyfile(db_copy, db_dest)
    # The protected backup copy is deliberately mode 0600. Restore source
    # metadata only into the private disposable rehearsal tree.
    copy_file_metadata(source_root / DB_REL, db_dest)
    if sha256(db_dest) != sha256(db_copy):
        raise RuntimeError("restored database hash differs from captured online backup")
    if db_metadata:
        if file_metadata(db_dest) != db_metadata["file_metadata"]:
            raise RuntimeError("restored database file metadata differs from captured metadata")
    con = sqlite3.connect(f"file:{quote(str(db_dest), safe='/')}?mode=ro", uri=True)
    try:
        integrity = [r[0] for r in con.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise RuntimeError("restored database integrity check failed")
        has_migrations = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
        schema_version = (con.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
                          if has_migrations else con.execute("PRAGMA user_version").fetchone()[0])
        if db_metadata and schema_version != db_metadata["schema_version"]:
            raise RuntimeError("restored database schema version differs from captured image")
        if expected_files is not None:
            mismatches = []
            for item in expected_files:
                dest = scratch / item["path"]
                try:
                    info = dest.lstat()
                except FileNotFoundError:
                    mismatches.append(item["path"])
                    continue
                expected_kind = {"file": stat.S_ISREG, "directory": stat.S_ISDIR, "symlink": stat.S_ISLNK}[item["type"]]
                if not expected_kind(info.st_mode):
                    mismatches.append(item["path"])
                    continue
                if stat.S_IMODE(info.st_mode) != int(item["mode"], 8):
                    mismatches.append(item["path"])
                    continue
                if (info.st_uid, info.st_gid) != (item["uid"], item["gid"]):
                    mismatches.append(item["path"])
                    continue
                if item["type"] == "file":
                    if info.st_size != item["size"] or sha256(dest) != item["sha256"]:
                        mismatches.append(item["path"])
                elif item["type"] == "symlink" and (not dest.is_symlink() or os.readlink(dest) != item["link_target"]):
                    mismatches.append(item["path"])
            if mismatches:
                raise RuntimeError(f"restore rehearsal content mismatch in {len(mismatches)} archived paths")
        return {"restore_root": str(scratch), "database_integrity_check": integrity,
                "schema_version": schema_version,
                "restored_file_count": sum(1 for p in scratch.rglob("*") if p.is_file()),
                "archive_content_hashes_verified": expected_files is not None}
    finally:
        con.close()


def command(*args: str) -> str:
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE).stdout.strip()


def check_destination(target: Path, confirm_network: bool = False) -> dict:
    target = target.resolve(strict=True)
    if not target.is_dir():
        raise RuntimeError("backup destination must be an existing directory")
    mountpoint = Path(command("findmnt", "-n", "-o", "TARGET", "-T", str(target))).resolve()
    source = command("findmnt", "-n", "-o", "SOURCE", "-T", str(target))
    fstype = command("findmnt", "-n", "-o", "FSTYPE", "-T", str(target))
    if target == mountpoint and target in (Path("/"), Path("/srv")):
        raise RuntimeError("refusing root or Minisa data filesystem as backup destination")
    if fstype in NETWORK_FS:
        if not confirm_network:
            raise RuntimeError("network target requires --confirm-independent-target after administrator review")
        independent = True
    elif source.startswith("/dev/"):
        # Compare physical block-device ancestry, not LV names, since / and /srv
        # are separate LVs on one NVMe device.
        ancestry = lambda dev: set(command("lsblk", "-srno", "NAME", dev).splitlines())
        dest_dev = ancestry(source)
        live_devs = set()
        for path in ("/", "/srv"):
            src = command("findmnt", "-n", "-o", "SOURCE", "-T", path)
            if src.startswith("/dev/"):
                live_devs |= ancestry(src)
        if dest_dev & live_devs:
            raise RuntimeError("backup destination shares a physical block device with production")
        independent = True
    else:
        raise RuntimeError(f"unverified destination filesystem/source: {fstype} {source}")
    return {"path": str(target), "mountpoint": str(mountpoint), "source": source,
            "filesystem": fstype, "independent_from_live_block_devices": independent}


def service_state(service: str) -> str:
    return command("systemctl", "show", "--property=ActiveState", "--value", service)


def acquire_source_locks(source_root: Path) -> list[int]:
    """Hold existing Minisa writer/awake locks without creating or truncating files."""
    fds = []
    flags = os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_NOATIME", 0)
    try:
        for name in (".minisa-awake.lock", ".minisa-writer.lock"):
            lock_path = source_root / name
            fd = os.open(lock_path, flags)  # no O_CREAT: missing lock is a hard stop
            fds.append(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fds
    except Exception:
        for fd in fds:
            os.close(fd)
        raise


def restore_service_states(original: dict[str, str], command_fn=command, state_fn=service_state) -> list[str]:
    """Best-effort restoration for every unit, collecting errors instead of stopping early."""
    errors = []
    for service, wanted in original.items():
        try:
            current = state_fn(service)
            if wanted == "active" and current != "active":
                command_fn("systemctl", "start", service)
            elif wanted != "active" and current == "active":
                command_fn("systemctl", "stop", service)
            restored = state_fn(service)
            if (wanted == "active") != (restored == "active"):
                errors.append(f"{service}: expected {wanted}, observed {restored}")
        except Exception as exc:
            errors.append(f"{service}: {type(exc).__name__}")
    return errors


def coordinated_capture(capture_fn, command_fn=command, state_fn=service_state, lock_fn=acquire_source_locks):
    """Quiesce both services, run capture under existing locks, and restore their states."""
    original = {service: state_fn(service) for service in SERVICES}
    unstable = {service: state for service, state in original.items() if state not in {"active", "inactive", "failed"}}
    if unstable:
        raise RuntimeError(f"services are transitioning; refusing capture: {unstable}")
    lock_fds = []
    capture_error = None
    result = None
    try:
        for service, state in original.items():
            if state == "active":
                command_fn("systemctl", "stop", service)
        for service in SERVICES:
            state = state_fn(service)
            if state == "active":
                command_fn("systemctl", "stop", service)
                state = state_fn(service)
            if state not in {"inactive", "failed"}:
                raise RuntimeError(f"{service} did not quiesce (state={state})")
        lock_fds = lock_fn(SOURCE)
        result = capture_fn(original)
        if any(state_fn(service) == "active" for service in SERVICES):
            raise RuntimeError("a Minisa service became active during the capture")
    except BaseException as exc:
        capture_error = exc
        raise
    finally:
        lock_errors = []
        for fd in lock_fds:
            try:
                os.close(fd)
            except OSError as exc:
                lock_errors.append(type(exc).__name__)
        restore_errors = restore_service_states(original, command_fn, state_fn)
        errors = lock_errors + restore_errors
        if errors:
            detail = "maintenance cleanup failed: " + "; ".join(errors)
            if capture_error is not None:
                capture_error.add_note(detail)
            else:
                raise RuntimeError(detail)
    return result


def estimate_capture_bytes(paths: tuple[Path, ...]) -> int:
    total = 0
    for root in paths:
        if not root.exists():
            continue
        if root.is_file() or root.is_symlink():
            total += root.lstat().st_size
            continue
        def on_walk_error(error):
            raise RuntimeError(f"cannot inventory recovery source {error.filename}: {error.strerror}") from error
        for base, dirs, files in os.walk(root, followlinks=False, onerror=on_walk_error):
            for name in dirs + files:
                item = Path(base) / name
                try:
                    total += item.lstat().st_size
                except FileNotFoundError:
                    raise RuntimeError(f"recovery source changed during size preflight: {item}")
    # Archive + online DB image + full disposable extraction, with headroom.
    return total * 3 + 100 * 1024 * 1024


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def verify_checksums(outdir: Path) -> None:
    for line in (outdir / "SHA256SUMS").read_text().splitlines():
        digest, rel = line.split("  ", 1)
        if sha256(outdir / rel) != digest:
            raise RuntimeError(f"post-write hash verification failed: {rel}")


def is_verified_capture(outdir: Path) -> bool:
    """Validate the completion marker, final manifest digest, and all artifacts."""
    try:
        manifest_path = outdir / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        marker = (outdir / "CAPTURE_VERIFIED").read_text().strip()
        if manifest.get("capture_status") != "verified":
            return False
        if marker != f"manifest_sha256={sha256(manifest_path)}":
            return False
        verify_checksums(outdir)
        return True
    except (OSError, ValueError, KeyError, RuntimeError):
        return False


def write_incomplete_marker(outdir: Path) -> None:
    marker = outdir / "CAPTURE_INCOMPLETE"
    marker.write_text("Capture is not verified. Do not restore or rely on this directory.\n")
    os.chmod(marker, 0o600)


def finalize_capture(outdir: Path, manifest: dict) -> str:
    """Mark a capture usable only after final manifest and artifact hashes pass."""
    manifest["capture_status"] = "verification_pending"
    write_json(outdir / "manifest.json", manifest)

    def write_sums():
        entries = (
            ("file-state.tar", sha256(outdir / "file-state.tar")),
            ("sqlite/minisa.db", sha256(outdir / "sqlite/minisa.db")),
            ("file-manifest.json", sha256(outdir / "file-manifest.json")),
            ("manifest.json", sha256(outdir / "manifest.json")),
        )
        (outdir / "SHA256SUMS").write_text("".join(f"{digest}  {rel}\n" for rel, digest in entries))

    write_sums()
    verify_checksums(outdir)
    manifest["capture_status"] = "verified"
    write_json(outdir / "manifest.json", manifest)
    write_sums()
    verify_checksums(outdir)
    manifest_digest = sha256(outdir / "manifest.json")
    verified_tmp = outdir / "CAPTURE_VERIFIED.tmp"
    verified_tmp.write_text(f"manifest_sha256={manifest_digest}\n")
    os.chmod(verified_tmp, 0o600)
    (outdir / "CAPTURE_INCOMPLETE").unlink()
    os.replace(verified_tmp, outdir / "CAPTURE_VERIFIED")
    return manifest_digest


def preflight_details(destination: Path, dest_info: dict, *, require_root: bool) -> dict:
    paths = (SOURCE, ACTIVE_RELEASE, *UNITS)
    report = {
        "destination": dest_info,
        "required_free_bytes": None,
        "available_bytes": shutil.disk_usage(destination.resolve()).free,
        "included_paths": [],
        "optional_paths_absent": [],
        "sqlite_consistency": "stop both services; SQLite online backup API; raw DB/WAL/SHM excluded from tar",
        "services_to_stop": list(SERVICES),
        "service_states": None,
        "excluded": ["systemd journal", "remote Ollama host and model storage"],
        "expected_interruption": "both services remain stopped through capture, hashing, SQLite checks, disposable restore and service-state restoration",
        "capture_duration_estimate": "not estimated by utility; administrator must use required bytes and target throughput, including restore and service restart time",
        "restore_rehearsal": "new disposable directory only; never production",
        "secret_handling": "outer directory 0700; database/archive 0600; use restricted encrypted destination",
    }
    if require_root and os.geteuid() != 0:
        report["preflight"] = "blocked"
        report["blocker"] = "complete path inventory and capacity estimate require an administrator-authenticated root preflight"
        return report
    for path in (SOURCE, SOURCE / DB_REL, ACTIVE_RELEASE, *UNITS[:2]):
        try:
            path.stat()
        except PermissionError as error:
            report["preflight"] = "blocked"
            report["blocker"] = f"cannot inspect required path: {path}"
            return report
        except FileNotFoundError as error:
            report["preflight"] = "blocked"
            report["blocker"] = f"required path is missing: {path}"
            return report
    for path in paths:
        try:
            path.stat()
            report["included_paths"].append(str(path))
        except FileNotFoundError:
            if path in UNITS[2:]:
                report["optional_paths_absent"].append(str(path))
            else:
                report["preflight"] = "blocked"
                report["blocker"] = f"required path is missing: {path}"
                return report
        except PermissionError:
            report["preflight"] = "blocked"
            report["blocker"] = f"cannot inspect path: {path}"
            return report
    try:
        states = {service: service_state(service) for service in SERVICES}
        enablement = {service: command("systemctl", "show", "--property=UnitFileState", "--value", service)
                      for service in SERVICES}
    except (subprocess.CalledProcessError, OSError):
        report["preflight"] = "blocked"
        report["blocker"] = "cannot read current systemd service states and enablement"
        return report
    report["service_states"] = states
    report["service_enablement"] = enablement
    unstable = {service: state for service, state in states.items()
                if state not in {"active", "inactive", "failed"}}
    if unstable:
        report["preflight"] = "blocked"
        report["blocker"] = f"services are transitioning: {unstable}"
        return report
    try:
        report["required_free_bytes"] = estimate_capture_bytes(paths)
    except (OSError, RuntimeError) as error:
        report["preflight"] = "blocked"
        report["blocker"] = str(error)
        return report
    if report["available_bytes"] < report["required_free_bytes"]:
        report["preflight"] = "blocked"
        report["blocker"] = "destination has insufficient free capacity"
    else:
        report["preflight"] = "passed"
    return report


def main() -> int:
    def interrupt(signum, _frame):
        raise InterruptedError(f"received signal {signum}")

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupt)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("destination", type=Path, help="directory on a verified independent mounted target")
    ap.add_argument("--execute-maintenance", action="store_true", help="authorize stop/capture/verify/restore service state")
    ap.add_argument("--confirm-independent-target", action="store_true", help="admin confirms remote filesystem is independently recoverable")
    args = ap.parse_args()
    outdir = None
    try:
        dest_info = check_destination(args.destination, args.confirm_independent_target)
        if not args.execute_maintenance:
            report = preflight_details(args.destination, dest_info, require_root=True)
            report["capture_performed"] = False
            print(json.dumps(report, indent=2))
            if report["preflight"] != "passed":
                return 2
            print("Preflight only. Rerun with --execute-maintenance during the approved window.")
            return 0
        if os.geteuid() != 0:
            raise RuntimeError("--execute-maintenance requires root; use an administrator's authenticated sudo session")
        for p in (SOURCE / DB_REL, ACTIVE_RELEASE, *UNITS[:2]):
            if not p.exists():
                raise FileNotFoundError(f"required recovery path missing: {p}")
        required_space = estimate_capture_bytes((SOURCE, ACTIVE_RELEASE, *UNITS))
        free_space = shutil.disk_usage(args.destination.resolve()).free
        if free_space < required_space:
            raise RuntimeError(f"destination lacks space: need about {required_space} bytes, have {free_space}")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        outdir = args.destination.resolve() / f"minisa-recovery-{stamp}"
        outdir.mkdir(mode=0o700)
        write_incomplete_marker(outdir)

        def capture(original):
            enablement = {service: command("systemctl", "show", "--property=UnitFileState", "--value", service)
                          for service in SERVICES}
            db = outdir / "sqlite" / "minisa.db"
            db_meta = sqlite_backup(SOURCE / DB_REL, db)
            db_meta.update({"source_mode": oct((SOURCE / DB_REL).stat().st_mode & 0o7777),
                            "source_uid": (SOURCE / DB_REL).stat().st_uid,
                            "source_gid": (SOURCE / DB_REL).stat().st_gid,
                            "source_wal_present": (SOURCE / "data/minisa.db-wal").exists(),
                            "source_shm_present": (SOURCE / "data/minisa.db-shm").exists()})
            os.chmod(db, 0o600)
            archive = outdir / "file-state.tar"
            files = archive_tree(archive, SOURCE, ACTIVE_RELEASE, UNITS)
            os.chmod(archive, 0o600)
            rehearsal = restore_rehearsal(archive, db, outdir / "restore-rehearsal", SOURCE, db_meta, files)
            file_manifest = outdir / "file-manifest.json"
            file_manifest.write_text(json.dumps(files, indent=2) + "\n")
            manifest = {"created_utc": stamp, "destination": dest_info,
                        "services_original_active_state": original, "services_stopped_for_capture": True,
                        "services_original_enablement": enablement,
                        "source_root": str(SOURCE), "active_release": str(ACTIVE_RELEASE),
                        "service_configuration_paths": {str(x): {"present": x.exists()} for x in UNITS},
                        "database": {**db_meta, "path": "sqlite/minisa.db",
                                     "size_bytes": db.stat().st_size,
                                     "sha256": sha256(db), "created_utc": stamp},
                        "archive": {"path": "file-state.tar", "size_bytes": archive.stat().st_size,
                                    "sha256": sha256(archive), "created_utc": stamp},
                        "file_manifest": {"path": "file-manifest.json", "size_bytes": file_manifest.stat().st_size,
                                          "sha256": sha256(file_manifest), "created_utc": stamp},
                        "archive_file_count": len(files),
                        "file_manifest_sha256": sha256(file_manifest),
                        "archive_bytes": archive.stat().st_size,
                        "archive_sha256": sha256(archive), "database_sha256": sha256(db),
                        "restore_rehearsal": rehearsal,
                        "historical_records_policy": "preserved; no source records deleted or rewritten"}
            manifest_digest = finalize_capture(outdir, manifest)
            return {"capture": "verified", "backup_directory": str(outdir),
                    "manifest_sha256": manifest_digest,
                    "archive_sha256": sha256(archive), "database_sha256": sha256(db),
                    "restore_rehearsal": rehearsal}

        result = coordinated_capture(capture)
        print(json.dumps(result, indent=2))
    except Exception as exc:
        notes = " ".join(getattr(exc, "__notes__", []))
        output = f"capture failed safely: {type(exc).__name__}: {exc}"
        if notes:
            output += f"; {notes}"
        if outdir is not None:
            if is_verified_capture(outdir):
                output += f"; verified recovery point retained at {outdir}; inspect both service states before resuming"
            else:
                output += f"; incomplete output retained at {outdir}; do not use it as a recovery point"
        print(output, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
