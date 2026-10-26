from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

from . import __version__
from .migrations import apply_migrations, current_schema_version, verify_migrations


def utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class IdempotencyConflict(ValueError):
    pass


class _ConnectionScope:
    """Connection scope that commits or rolls back and always closes.

    The raw sqlite3 connection commits on exit but stays open, which leaks
    file descriptors in long-lived services. Callers that use the connection
    directly keep working through attribute delegation.
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def __enter__(self) -> sqlite3.Connection:
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()
        return False


class Database:
    def __init__(self, path: str | Path, *, installation_name: str = "minisa-core"):
        self.path = Path(path)
        self.installation_name = installation_name

    def connect(self, *, readonly: bool = False) -> _ConnectionScope:
        if readonly:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5, isolation_level=None)
        else:
            conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        if not readonly:
            conn.execute("PRAGMA synchronous=FULL")
        if readonly:
            conn.execute("PRAGMA query_only=ON")
        return _ConnectionScope(conn)

    def initialize(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            # WAL persists in the database and permits readers during one serialized writer.
            conn.execute("PRAGMA journal_mode=WAL")
            # FULL protects committed transactions across OS/power failure; durability wins here.
            conn.execute("PRAGMA synchronous=FULL")
            return apply_migrations(
                conn, installation_name=self.installation_name, software_version=__version__
            )

    def status(self) -> dict[str, Any]:
        with self.connect(readonly=True) as conn:
            version = verify_migrations(conn)
            installation = conn.execute("SELECT * FROM installation WHERE singleton=1").fetchone()
            revision = conn.execute("SELECT global_revision FROM core_state WHERE singleton=1").fetchone()[0]
            return {
                "db_reachable": True,
                "schema_version": version,
                "installation_id": installation["installation_id"],
                "global_revision": revision,
            }

    @contextlib.contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def allocate_revision(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "UPDATE core_state SET global_revision=global_revision+1 WHERE singleton=1 RETURNING global_revision"
        ).fetchone()
        if row is None:
            raise RuntimeError("core_state is missing")
        return int(row[0])

    @staticmethod
    def append_audit(
        conn: sqlite3.Connection,
        *,
        actor: str,
        operation: str,
        target_category: str,
        target_id: str | None = None,
        details: Any = None,
        global_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        event_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                global_revision,
                utcnow(),
                actor,
                operation,
                target_category,
                target_id,
                canonical_json({} if details is None else details),
                idempotency_key,
            ),
        )
        return event_id

    def append_memory(self, conn: sqlite3.Connection, identity: str, record: dict, revision: int) -> str:
        mid = record.get('memory_id') or identity
        conn.execute('INSERT OR IGNORE INTO memory_records(id,memory_id,details_json,revision,created_at) VALUES (?,?,?,?,?)',
                     (identity, mid, canonical_json(record), revision, time.time()))
        return identity

    def execute_idempotent(
        self,
        key: str,
        request: Any,
        mutation: Callable[[sqlite3.Connection], tuple[Any, int | None, str | None]],
    ) -> tuple[Any, bool]:
        if not key or len(key) > 255:
            raise ValueError("idempotency key must be 1..255 characters")
        digest = request_hash(request)
        with self.transaction(immediate=True) as conn:
            existing = conn.execute(
                "SELECT * FROM idempotency_records WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing:
                if existing["request_hash"] != digest:
                    raise IdempotencyConflict("idempotency key reused with a different request")
                if existing["status"] != "completed":
                    raise RuntimeError("incomplete idempotency record")
                return json.loads(existing["response_json"]), True
            conn.execute(
                "INSERT INTO idempotency_records(idempotency_key,request_hash,status,created_at) VALUES (?,?,?,?)",
                (key, digest, "in_progress", utcnow()),
            )
            result, revision, audit_id = mutation(conn)
            encoded = canonical_json(result)
            conn.execute(
                "UPDATE idempotency_records SET status='completed',completed_at=?,response_json=?,global_revision=?,audit_event_id=? WHERE idempotency_key=?",
                (utcnow(), encoded, revision, audit_id, key),
            )
            return json.loads(encoded), False

    def create_job(self, job_type: str, payload: Any, *, deduplication_key: str | None = None) -> str:
        job_id = str(uuid.uuid4())
        with self.transaction(immediate=True) as conn:
            if deduplication_key:
                existing = conn.execute(
                    "SELECT job_id FROM jobs WHERE deduplication_key=?", (deduplication_key,)
                ).fetchone()
                if existing:
                    return str(existing[0])
            conn.execute(
                "INSERT INTO jobs(job_id,job_type,payload_json,status,created_at,deduplication_key) VALUES (?,?,?,'pending',?,?)",
                (job_id, job_type, canonical_json(payload), utcnow(), deduplication_key),
            )
        return job_id

    def lease_job(self, owner: str, lease_seconds: int = 60, *, job_type: str | None = None, now_timestamp: float | None = None, max_attempts: int | None = None) -> dict[str, Any] | None:
        now_dt = (datetime.datetime.fromtimestamp(now_timestamp, datetime.timezone.utc)
                  if now_timestamp is not None else datetime.datetime.now(datetime.timezone.utc))
        now = now_dt.isoformat()
        expiry = (now_dt + datetime.timedelta(seconds=lease_seconds)).isoformat()
        with self.transaction(immediate=True) as conn:
            row = conn.execute(
                """SELECT job_id FROM jobs
                   WHERE (status='pending' OR (status='leased' AND lease_expires_at <= ?))
                   AND (? IS NULL OR job_type=?)
                   AND (? IS NULL OR attempt_count < ?)
                   ORDER BY created_at LIMIT 1""",
                (now, job_type, job_type, max_attempts, max_attempts),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE jobs SET status='leased',lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1 WHERE job_id=?",
                (owner, expiry, row[0]),
            )
            leased = conn.execute("SELECT * FROM jobs WHERE job_id=?", (row[0],)).fetchone()
            result = dict(leased)
            result["payload"] = json.loads(result.pop("payload_json"))
            return result

    def finish_job(self, job_id: str, owner: str, status: str, *, result: Any = None, error: str | None = None) -> None:
        if status not in {"completed", "failed", "review_required"}:
            raise ValueError("invalid terminal job status")
        with self.transaction(immediate=True) as conn:
            changed = conn.execute(
                """UPDATE jobs SET status=?,result_json=?,error=?,completed_at=?,lease_owner=NULL,lease_expires_at=NULL
                   WHERE job_id=? AND status='leased' AND lease_owner=?""",
                (status, None if result is None else canonical_json(result), error, utcnow(), job_id, owner),
            ).rowcount
            if changed != 1:
                raise ValueError("job is not leased by this owner")
