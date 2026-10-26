from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass


class MigrationError(RuntimeError):
    pass


class SchemaTooNewError(MigrationError):
    pass


class MigrationChecksumError(MigrationError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        material = f"{self.version}\n{self.name}\n{self.sql}".encode()
        return hashlib.sha256(material).hexdigest()


MIGRATIONS = (
    Migration(
        1,
        "stage1_persistence_foundation",
        """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
) STRICT;

CREATE TABLE installation (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    installation_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    installation_name TEXT,
    schema_version INTEGER NOT NULL CHECK (schema_version >= 0),
    software_version TEXT NOT NULL
) STRICT;

CREATE TABLE core_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    global_revision INTEGER NOT NULL CHECK (global_revision >= 0)
) STRICT;

CREATE TABLE audit_events (
    event_id TEXT PRIMARY KEY,
    global_revision INTEGER CHECK (global_revision IS NULL OR global_revision >= 0),
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    operation TEXT NOT NULL,
    target_category TEXT NOT NULL,
    target_id TEXT,
    details_json TEXT NOT NULL CHECK (json_valid(details_json)),
    idempotency_key TEXT
) STRICT;
CREATE INDEX audit_events_revision_idx ON audit_events(global_revision);
CREATE INDEX audit_events_target_idx ON audit_events(target_category, target_id);

CREATE TABLE idempotency_records (
    idempotency_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('in_progress', 'completed')),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    response_json TEXT CHECK (response_json IS NULL OR json_valid(response_json)),
    global_revision INTEGER CHECK (global_revision IS NULL OR global_revision >= 0),
    audit_event_id TEXT REFERENCES audit_events(event_id),
    CHECK ((status = 'in_progress' AND completed_at IS NULL AND response_json IS NULL)
        OR (status = 'completed' AND completed_at IS NOT NULL AND response_json IS NOT NULL))
) STRICT;

CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    status TEXT NOT NULL CHECK (status IN ('pending','leased','completed','failed','review_required')),
    created_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    error TEXT,
    completed_at TEXT,
    deduplication_key TEXT UNIQUE,
    CHECK ((status = 'leased' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status <> 'leased'))
) STRICT;
CREATE INDEX jobs_claim_idx ON jobs(status, lease_expires_at, created_at);

CREATE TABLE backups (
    backup_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    source_schema_version INTEGER NOT NULL,
    global_revision INTEGER NOT NULL,
    path TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL,
    verification_status TEXT NOT NULL CHECK (verification_status IN ('pending','verified','failed')),
    verified_at TEXT,
    restore_test_status TEXT CHECK (restore_test_status IS NULL OR restore_test_status IN ('passed','failed')),
    restore_tested_at TEXT
) STRICT;
""".strip(),
    ),
    Migration(
        2,
        "cognitive_state_store",
        """
CREATE TABLE cognitive_records (
    record_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    owner_kind TEXT NOT NULL CHECK(owner_kind IN ('minisa','user')),
    owner_key TEXT,
    authority TEXT NOT NULL CHECK(authority IN ('authoritative','historical','non_authoritative')),
    legacy_label TEXT,
    source_name TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL UNIQUE,
    raw_content TEXT NOT NULL,
    metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
    source_created_at TEXT,
    imported_at TEXT NOT NULL,
    imported_revision INTEGER NOT NULL CHECK(imported_revision > 0),
    migration_id TEXT NOT NULL
) STRICT;
CREATE INDEX cognitive_records_category_idx ON cognitive_records(category, authority);
CREATE INDEX cognitive_records_legacy_label_idx ON cognitive_records(legacy_label);
CREATE INDEX cognitive_records_owner_idx ON cognitive_records(owner_kind, owner_key);
CREATE TABLE cognitive_migrations (
    migration_id TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL UNIQUE,
    source_root TEXT NOT NULL,
    inventory_json TEXT NOT NULL CHECK(json_valid(inventory_json)),
    record_count INTEGER NOT NULL CHECK(record_count >= 0),
    applied_at TEXT NOT NULL,
    global_revision INTEGER NOT NULL CHECK(global_revision > 0)
) STRICT;
""".strip(),
    ),
)

# Versions 1 and 2 retain their exact checksums.
MIGRATIONS += (Migration(3, "persistent_workspace", """
CREATE TABLE workspace_units (
 unit_id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
 created_at REAL NOT NULL, activated_at REAL NOT NULL,
 activation REAL NOT NULL CHECK(activation BETWEEN 0 AND 1),
 importance REAL NOT NULL CHECK(importance BETWEEN 0 AND 1),
 uncertainty REAL NOT NULL CHECK(uncertainty BETWEEN 0 AND 1),
 priority REAL NOT NULL CHECK(priority BETWEEN 0 AND 1),
 status TEXT NOT NULL CHECK(status IN ('active','cold','resolved')),
 revision INTEGER NOT NULL, dedup_key TEXT NOT NULL UNIQUE
) STRICT;
CREATE INDEX workspace_attention ON workspace_units(status,priority,importance);
CREATE TABLE workspace_evidence (
 evidence_id TEXT PRIMARY KEY, category TEXT NOT NULL,
 source_path TEXT NOT NULL, sha256 TEXT NOT NULL,
 observation_json TEXT NOT NULL CHECK(json_valid(observation_json)),
 created_at REAL NOT NULL, processed INTEGER NOT NULL DEFAULT 0 CHECK(processed IN (0,1))
) STRICT;
CREATE TABLE dream_state (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 last_attempt REAL NOT NULL DEFAULT 0, last_success REAL NOT NULL DEFAULT 0,
 next_allowed REAL NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
 window_start REAL NOT NULL DEFAULT 0, calls INTEGER NOT NULL DEFAULT 0,
 deep_calls INTEGER NOT NULL DEFAULT 0
) STRICT;
INSERT INTO dream_state(singleton) VALUES(1);
CREATE TABLE cognitive_interrupts (
 fingerprint TEXT PRIMARY KEY, source TEXT NOT NULL, last_raised REAL NOT NULL,
 priority REAL NOT NULL, evidence_id TEXT NOT NULL REFERENCES workspace_evidence(evidence_id)
) STRICT;
CREATE TABLE dream_results (
 job_id TEXT PRIMARY KEY REFERENCES jobs(job_id), source_revision INTEGER NOT NULL,
 resulting_revision INTEGER NOT NULL, result_json TEXT NOT NULL CHECK(json_valid(result_json)),
 created_at REAL NOT NULL
) STRICT;
CREATE TABLE learned_procedures (
 procedure_id TEXT PRIMARY KEY, situation TEXT NOT NULL, procedure TEXT NOT NULL,
 evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)), confidence REAL NOT NULL,
 revision INTEGER NOT NULL
) STRICT;
""".strip()),)

MIGRATIONS += (Migration(4, "phase5_hardening", """
ALTER TABLE dream_state ADD COLUMN deferrals INTEGER NOT NULL DEFAULT 0;
ALTER TABLE dream_state ADD COLUMN last_failure_category TEXT;
ALTER TABLE dream_state ADD COLUMN recovery_after REAL NOT NULL DEFAULT 0;
ALTER TABLE dream_state ADD COLUMN reflected_revision INTEGER NOT NULL DEFAULT 0;
CREATE INDEX evidence_pending ON workspace_evidence(processed,created_at);
ALTER TABLE workspace_units ADD COLUMN eligible_until REAL NOT NULL DEFAULT 0;
UPDATE workspace_units SET eligible_until=activated_at+21600*(1+3*importance)*4.321928094887363;
CREATE INDEX units_lifecycle ON workspace_units(status,eligible_until);
CREATE INDEX units_eligible ON workspace_units(eligible_until);
CREATE TABLE cognitive_metrics (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), baseline_bytes INTEGER NOT NULL,
 last_bytes INTEGER NOT NULL, sampled_at REAL NOT NULL, ticks INTEGER NOT NULL DEFAULT 0
) STRICT;
INSERT INTO cognitive_metrics VALUES(1,0,0,0,0);
""".strip()),)

MIGRATIONS += (Migration(5, "sourced_memory_records", """
CREATE TABLE learned_procedures_hypothesis (
 procedure_id TEXT PRIMARY KEY, global_revision INTEGER NOT NULL CHECK (global_revision >= 0),
 created_at REAL NOT NULL, last_used_at REAL, title TEXT NOT NULL, situation TEXT NOT NULL,
 procedure TEXT NOT NULL, evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
 usage_count INTEGER NOT NULL DEFAULT 0 CHECK (usage_count >= 0),
 confidence REAL NOT NULL DEFAULT 0.0 CHECK (confidence BETWEEN 0.0 AND 1.0),
 provenance_json TEXT NOT NULL CHECK(json_valid(provenance_json)),
 source_evidence_ids TEXT NOT NULL CHECK(json_valid(source_evidence_ids)),
 signature TEXT NOT NULL
) STRICT;
INSERT INTO learned_procedures_hypothesis
 (procedure_id, global_revision, created_at, title, situation, procedure, evidence_json,
  usage_count, confidence, provenance_json, source_evidence_ids, signature)
 SELECT procedure_id, revision, CAST(strftime('%s','now') AS REAL), situation, situation, procedure,
        evidence_json, 0, confidence,
        json_object('authority','dream_hypothesis','executable',0,'admission','pre_migration_record'),
        evidence_json, procedure_id
 FROM learned_procedures;
DROP TABLE learned_procedures;
ALTER TABLE learned_procedures_hypothesis RENAME TO learned_procedures;
CREATE INDEX procedures_revision ON learned_procedures(global_revision);
CREATE INDEX procedures_title ON learned_procedures(title);
CREATE INDEX procedures_usage ON learned_procedures(usage_count);
CREATE TABLE memory_records (
 id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, details_json TEXT NOT NULL CHECK(json_valid(details_json)),
 revision INTEGER NOT NULL, created_at REAL NOT NULL
) STRICT;
CREATE UNIQUE INDEX memory_id_unique ON memory_records(memory_id);
CREATE INDEX memory_revision ON memory_records(revision);
""".strip()),)

SUPPORTED_SCHEMA_VERSION = MIGRATIONS[-1].version


def _has_migration_table(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone() is not None


def current_schema_version(conn: sqlite3.Connection) -> int:
    if not _has_migration_table(conn):
        return 0
    return int(conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0])


def verify_migrations(conn: sqlite3.Connection) -> int:
    version = current_schema_version(conn)
    if version > SUPPORTED_SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"database schema {version} is newer than supported {SUPPORTED_SCHEMA_VERSION}"
        )
    if not _has_migration_table(conn):
        return version
    known = {m.version: m for m in MIGRATIONS}
    for row in conn.execute("SELECT version, name, checksum FROM schema_migrations ORDER BY version"):
        migration = known.get(row[0])
        if migration is None:
            raise SchemaTooNewError(f"unknown applied migration version {row[0]}")
        if row[1] != migration.name or row[2] != migration.checksum:
            raise MigrationChecksumError(f"applied migration {row[0]} checksum/name mismatch")
    return version


def apply_migrations(conn: sqlite3.Connection, *, installation_name: str, software_version: str) -> int:
    import datetime
    import uuid

    version = verify_migrations(conn)
    for migration in MIGRATIONS:
        if migration.version <= version:
            continue
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in migration.sql.split(";\n"):
                if statement.strip():
                    conn.execute(statement)
            if migration.version == 1:
                conn.execute(
                    "INSERT INTO installation VALUES (1, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), now, installation_name, migration.version, software_version),
                )
                conn.execute("INSERT INTO core_state VALUES (1, 0)")
                conn.execute(
                    "INSERT INTO audit_events VALUES (?, NULL, ?, ?, ?, ?, NULL, ?, NULL)",
                    (str(uuid.uuid4()), now, "migration", "schema.migrate", "database", '{}'),
                )
            conn.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES (?,?,?,?)",
                (migration.version, migration.name, migration.checksum, now),
            )
            conn.execute(
                "UPDATE installation SET schema_version=?, software_version=? WHERE singleton=1",
                (migration.version, software_version),
            )
            conn.commit()
            version = migration.version
        except Exception:
            conn.rollback()
            raise
    return verify_migrations(conn)
