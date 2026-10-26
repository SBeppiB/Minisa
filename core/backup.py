from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path
from typing import Any

from . import __version__
from .db import Database, utcnow
from .migrations import SUPPORTED_SCHEMA_VERSION, verify_migrations


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_database(path: Path) -> dict[str, Any]:
    # A completed backup is immutable during inspection. This prevents SQLite
    # from creating unnecessary WAL/SHM sidecars beside the backup artifact.
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"integrity_check failed: {integrity}")
        version = verify_migrations(conn)
        install = conn.execute("SELECT installation_id FROM installation WHERE singleton=1").fetchone()
        revision = conn.execute("SELECT global_revision FROM core_state WHERE singleton=1").fetchone()
        return {
            "integrity_check": integrity,
            "schema_version": version,
            "supported_schema_version": SUPPORTED_SCHEMA_VERSION,
            "installation_id": install[0],
            "global_revision": revision[0],
        }
    finally:
        conn.close()


def create_backup(db: Database, backup_root: str | Path) -> Path:
    root = Path(backup_root)
    root.mkdir(parents=True, exist_ok=True)
    backup_id = str(uuid.uuid4())
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final_dir = root / f"stage1-{stamp}-{backup_id}"
    staging = Path(tempfile.mkdtemp(prefix=".stage1-backup-", dir=root))
    try:
        db_path = staging / "minisa.db"
        source = db.connect(readonly=True)
        destination = sqlite3.connect(db_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        info = inspect_database(db_path)
        digest = sha256_file(db_path)
        manifest = {
            "backup_id": backup_id,
            "created_at": utcnow(),
            "database_file": "minisa.db",
            "global_revision": info["global_revision"],
            "installation_id": info["installation_id"],
            "schema_version": info["schema_version"],
            "sha256": digest,
            "size_bytes": db_path.stat().st_size,
            "software_version": __version__,
        }
        manifest_tmp = staging / "manifest.json.tmp"
        manifest_tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        os.replace(manifest_tmp, staging / "manifest.json")
        os.replace(staging, final_dir)
        with db.transaction(immediate=True) as conn:
            conn.execute(
                """INSERT INTO backups VALUES (?,?,?,?,?,?,?,'verified',?,NULL,NULL)""",
                (
                    backup_id,
                    manifest["created_at"],
                    info["schema_version"],
                    info["global_revision"],
                    str(final_dir),
                    manifest["size_bytes"],
                    digest,
                    utcnow(),
                ),
            )
        return final_dir
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def verify_backup(backup_dir: str | Path) -> dict[str, Any]:
    directory = Path(backup_dir)
    manifest = json.loads((directory / "manifest.json").read_text())
    database = directory / manifest["database_file"]
    actual = sha256_file(database)
    if actual != manifest["sha256"]:
        raise RuntimeError("backup SHA-256 mismatch")
    info = inspect_database(database)
    for key in ("schema_version", "global_revision", "installation_id"):
        if info[key] != manifest[key]:
            raise RuntimeError(f"backup manifest {key} mismatch")
    return {**info, "sha256": actual, "backup_id": manifest["backup_id"], "verified": True}


def restore_test(backup_dir: str | Path) -> dict[str, Any]:
    verified = verify_backup(backup_dir)
    with tempfile.TemporaryDirectory(prefix="minisa-restore-test-") as temp:
        source = Path(backup_dir) / "minisa.db"
        restored = Path(temp) / "restored.db"
        shutil.copy2(source, restored)
        info = inspect_database(restored)
        # Exercise the application DB layer read-only without touching production.
        layer_status = Database(restored).status()
        if layer_status["installation_id"] != info["installation_id"]:
            raise RuntimeError("read-only DB layer restore check mismatch")
    return {**verified, "restore_test": "passed"}
