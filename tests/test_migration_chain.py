"""Migration chain: production lineage, additive upgrade, atomic failure.

The candidate release must *verify* the production schema instead of
diverging from it, and it must upgrade it without losing a single row of the
categories the recovery plan protects.

Every test builds a disposable production-shaped database in a temporary
directory. The live database is never opened for writing.
"""
import hashlib
import json
import sqlite3
import unittest
from pathlib import Path
import tempfile

from core import migrations
from core.db import Database
from core.migrations import apply_migrations, verify_migrations
from runtime.cognition.dream import DreamWorker
from runtime.cognition.workspace import Workspace, WorkspacePolicy

from tests.test_learning_cycle import CLAIM, ScriptedModel

# Checksums recorded by the production database, read-only, on 2026-10-21.
# The candidate chain must match them exactly: a mismatch means the runtime
# would refuse to start against the production schema.
PRODUCTION_LINEAGE = (
    (1, 'stage1_persistence_foundation',
     '4d5491e09cb4dd38f43f634e353a2c8866388be0c6ce4e9e6f4109d8e757f845'),
    (2, 'cognitive_state_store',
     'b08db440d499a9219e14c10ca9faa8059173be2a68d968a46279c326649b999c'),
    (3, 'persistent_workspace',
     'b45c9a577a0aab5711aef9746170d08e0156aaa0769faabd5d343ea12a3a474e'),
    (4, 'phase5_hardening',
     '3c28dcc8572902433f54bb398666e6e0b803d4a8c3aeb7a7d682efadc3d56e4e'),
)

# The failed dream job keeps its source turn on disk: the runtime verifies the
# evidence hash before a dream may commit against it.
FAILED_TURN_RECORD = {
    'schema': 1, 'id': 'turn_failed', 'sequence': 1,
    'started_at': '2026-10-16T22:00:00+00:00', 'status': 'failed', 'stage': 'final',
    'user_input': 'remember this: keep the receipt', 'provenance': 'user',
    'answer': None, 'returned_text': None,
    'failure': {'kind': 'model_timeout', 'message': 'The dream model did not answer.'},
    'action': {'action': 'respond', 'status': 'failed', 'receipt': None},
    'cognitive_outcome': {'status': 'no_changes', 'committed': False},
    'experience': None, 'reflection': None, 'delivery': 'not_observed'}
FAILED_TURN_BYTES = json.dumps(FAILED_TURN_RECORD).encode()
FAILED_TURN_SHA = hashlib.sha256(FAILED_TURN_BYTES).hexdigest()
FAILED_OBSERVATION = json.dumps({'turn_id': 'turn_failed', 'status': 'failed',
                                 'action': 'respond', 'action_status': 'failed',
                                 'failure_kind': 'model_timeout',
                                 'user_statement': 'remember this: keep the receipt',
                                 'verification': 'runtime_outcome_only',
                                 'mutation_committed': False})

# One row per protected category, in the production v4 column shape.
PRESERVED_ROWS = {
    'jobs': ("job_id, job_type, payload_json, status, created_at, attempt_count, "
             "result_json, error, completed_at, deduplication_key",
             ('job_dream_failed', 'dream', '{"kind":"dream"}', 'failed', '2026-10-16T22:00:00+00:00',
              3, None, 'model_timeout', '2026-10-16T22:36:00+00:00', 'dream:failed')),
    'dream_results': ("job_id, source_revision, resulting_revision, result_json, created_at",
                      ('job_dream_ok', 12, 13, '{"accepted":[]}', 1_800_000_000.0)),
    'workspace_evidence': ("evidence_id, category, source_path, sha256, observation_json, "
                            "created_at, processed",
                            ('turn_failed', 'turn', 'EXPERIENCE/turns/turn_failed.json',
                             FAILED_TURN_SHA, FAILED_OBSERVATION, 1_800_000_000.0, 0)),
    'workspace_units': ("unit_id, kind, subject, payload_json, evidence_json, created_at, "
                        "activated_at, activation, importance, uncertainty, priority, status, "
                        "revision, dedup_key, eligible_until",
                        ('unit_failed', 'interrupted_task', 'Interrupted turn',
                         '{"turn_id":"turn_failed"}', '["turn_failed"]', 1_800_000_000.0,
                         1_800_000_000.0, 1.0, 0.9, 0.8, 0.9, 'active', 12, 'turn:turn_failed',
                         1_800_000_900.0)),
    'cognitive_interrupts': ("fingerprint, source, last_raised, priority, evidence_id",
                              ('fp_failed', 'dream', 1_800_000_000.0, 0.9, 'turn_failed')),
    'audit_events': ("event_id, global_revision, occurred_at, actor, operation, target_category, "
                     "target_id, details_json, idempotency_key",
                     ('evt_failed', 12, '2026-10-16T22:00:00+00:00', 'minisa.level0',
                      'dream.failed', 'dream', 'job_dream_failed', '{"reason":"timeout"}', None)),
    'cognitive_records': ("record_id, category, owner_kind, owner_key, authority, legacy_label, "
                          "source_name, source_path, source_sha256, source_fingerprint, raw_content, "
                          "metadata_json, source_created_at, imported_at, imported_revision, "
                          "migration_id",
                          ('rec_1', 'experience', 'minisa', None, 'historical', None, 'EXPERIENCE',
                           'EXPERIENCE/turns/turn_failed.json', 'b' * 64, 'fp1', 'raw',
                           '{}', None, '2026-09-19T18:50:00+00:00', 1, 'migration_1')),
    'idempotency_records': ("idempotency_key, request_hash, status, created_at, completed_at, "
                            "response_json, global_revision, audit_event_id",
                            ('idem_1', 'c' * 64, 'completed', '2026-10-16T22:00:00+00:00',
                             '2026-10-16T22:00:01+00:00', '{}', 12, None)),
    'backups': ("backup_id, created_at, source_schema_version, global_revision, path, size_bytes, "
                "sha256, verification_status, verified_at, restore_test_status, restore_tested_at",
                ('bkp_1', '2026-10-16T22:00:00+00:00', 4, 12, '/srv/backups/one.db', 1024,
                 'd' * 64, 'verified', '2026-10-16T22:01:00+00:00', 'passed',
                 '2026-10-16T22:05:00+00:00')),
    'learned_procedures': ("procedure_id, situation, procedure, evidence_json, confidence, revision",
                           ('proc_1', 'when brewing tea', 'use two spoons of matcha',
                            '["turn_failed"]', 0.4, 12)),
}


def build_production_v4(path):
    """Create the production v4 shape with the candidate chain (v1-v4 only)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    for migration in migrations.MIGRATIONS:
        if migration.version > 4:
            break
        conn.execute("BEGIN IMMEDIATE")
        for statement in migration.sql.split(";\n"):
            if statement.strip():
                conn.execute(statement)
        if migration.version == 1:
            # The production chain seeds these together with the first migration.
            conn.execute("INSERT INTO installation VALUES (1, ?, ?, ?, ?, ?)",
                         ('installation-1', '2026-09-19T18:50:00+00:00', 'minisa', 1, '1.0.0'))
            conn.execute("INSERT INTO core_state VALUES (1, 0)")
            conn.execute("INSERT INTO audit_events VALUES (?, NULL, ?, ?, ?, ?, NULL, ?, NULL)",
                         ('event-migration-1', '2026-09-19T18:50:00+00:00', 'migration',
                          'schema.migrate', 'database', '{}'))
        conn.execute("INSERT INTO schema_migrations(version,name,checksum,applied_at) "
                     "VALUES(?,?,?,?)",
                     (migration.version, migration.name, migration.checksum,
                      '2026-10-16T22:36:19+00:00'))
        conn.commit()
    return conn


def seed_preserved_rows(conn):
    for table, (columns, values) in PRESERVED_ROWS.items():
        placeholders = ','.join('?' * len(values))
        conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", values)
    # The dream singleton already exists from the migration chain; only its
    # failure history is production state worth preserving.
    conn.execute("UPDATE dream_state SET last_failure_category='timeout', failures=1, "
                 "calls=1, deep_calls=0 WHERE singleton=1")
    conn.commit()


def counts(conn, tables):
    return {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in tables}


class MigrationChainTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='minisa-migrations-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'minisa.db'

    def test_candidate_chain_pins_the_production_lineage(self):
        for version, name, checksum in PRODUCTION_LINEAGE:
            migration = next(m for m in migrations.MIGRATIONS if m.version == version)
            self.assertEqual(migration.name, name)
            self.assertEqual(migration.checksum, checksum)
        self.assertEqual(migrations.SUPPORTED_SCHEMA_VERSION, 5)
        self.assertEqual(migrations.MIGRATIONS[-1].name, 'sourced_memory_records')

    def test_verify_accepts_a_production_v4_database(self):
        conn = build_production_v4(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(verify_migrations(conn), 4)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='memory_records'"
        ).fetchone()[0], 0)

    def test_upgrade_preserves_every_protected_row(self):
        conn = build_production_v4(self.path)
        self.addCleanup(conn.close)
        seed_preserved_rows(conn)
        before = counts(conn, PRESERVED_ROWS)

        self.assertEqual(apply_migrations(conn, installation_name='rehearsal',
                                          software_version='rehearsal'), 5)

        after = counts(conn, PRESERVED_ROWS)
        self.assertEqual(before, after)
        self.assertEqual(conn.execute('SELECT count(*) FROM dream_state').fetchone()[0], 1)
        self.assertEqual(conn.execute('SELECT last_failure_category FROM dream_state'
                                      ).fetchone()[0], 'timeout')
        self.assertEqual(conn.execute(
            'SELECT schema_version FROM installation WHERE singleton=1').fetchone()[0], 5)
        self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(verify_migrations(conn), 5)

        # The rebuilt procedure table keeps the pre-migration hypothesis.
        row = conn.execute('SELECT * FROM learned_procedures WHERE procedure_id=?',
                           ('proc_1',)).fetchone()
        self.assertEqual(row['situation'], 'when brewing tea')
        self.assertEqual(row['procedure'], 'use two spoons of matcha')
        self.assertEqual(row['confidence'], 0.4)
        self.assertEqual(row['global_revision'], 12)
        self.assertEqual(row['title'], 'when brewing tea')
        self.assertEqual(row['usage_count'], 0)
        self.assertEqual(row['signature'], 'proc_1')
        self.assertEqual(json.loads(row['provenance_json'])['executable'], 0)

        # The new table exists and is empty until the dream worker fills it.
        self.assertEqual(conn.execute('SELECT count(*) FROM memory_records').fetchone()[0], 0)
        columns = [r[1] for r in conn.execute('PRAGMA table_info(memory_records)')]
        self.assertEqual(columns, ['id', 'memory_id', 'details_json', 'revision', 'created_at'])

    def test_failed_upgrade_leaves_the_database_untouched(self):
        conn = build_production_v4(self.path)
        self.addCleanup(conn.close)
        seed_preserved_rows(conn)
        # A pre-existing table makes the migration fail part-way through.
        conn.execute('CREATE TABLE memory_records (id TEXT PRIMARY KEY) STRICT')
        conn.commit()

        with self.assertRaises(sqlite3.OperationalError):
            apply_migrations(conn, installation_name='rehearsal', software_version='rehearsal')

        self.assertEqual(conn.execute('SELECT count(*) FROM schema_migrations WHERE version=5'
                                      ).fetchone()[0], 0)
        columns = [r[1] for r in conn.execute('PRAGMA table_info(learned_procedures)')]
        self.assertEqual(columns, ['procedure_id', 'situation', 'procedure', 'evidence_json',
                                    'confidence', 'revision'])
        self.assertEqual(conn.execute('SELECT count(*) FROM learned_procedures'
                                      ).fetchone()[0], 1)
        self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_runtime_writes_fit_the_migrated_schema(self):
        conn = build_production_v4(self.path)
        self.addCleanup(conn.close)
        apply_migrations(conn, installation_name='rehearsal', software_version='rehearsal')

        # The exact runtime statements, against the migrated schema.
        conn.execute(
            'INSERT OR IGNORE INTO learned_procedures '
            '(procedure_id, global_revision, created_at, last_used_at, title, situation, '
            'procedure, evidence_json, usage_count, confidence, provenance_json, '
            'source_evidence_ids, signature) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('proc_2', 13, 1_800_000_000.0, None, 'when brewing tea', 'when brewing tea',
             'use two spoons of matcha', '["turn_failed"]', 0, .5,
             json.dumps({'authority': 'dream_hypothesis', 'executable': False}),
             '["turn_failed"]', 'proc_2'))
        conn.execute('INSERT OR IGNORE INTO memory_records(id,memory_id,details_json,revision,'
                     'created_at) VALUES (?,?,?,?,?)',
                     ('id_1', 'mem_1', json.dumps({'fact': CLAIM}), 13, 1_800_000_000.0))
        conn.commit()
        self.assertEqual(conn.execute('SELECT count(*) FROM learned_procedures'
                                      ).fetchone()[0], 1)
        self.assertEqual(conn.execute('SELECT count(*) FROM memory_records').fetchone()[0], 1)
        self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')


class ProductionSchemaRehearsalTests(unittest.TestCase):
    """The learning cycle must work on the upgraded production schema."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='minisa-rehearsal-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'CORE').mkdir()
        for name in ('identity', 'values', 'goals', 'personality', 'instructions'):
            (self.root / 'CORE' / f'{name}.md').write_text(
                f'# {name}\n\nControlled rehearsal {name}.\n', encoding='utf-8')
        (self.root / 'EXPERIENCE/turns').mkdir(parents=True)
        (self.root / 'EXPERIENCE/turns/turn_failed.json').write_bytes(FAILED_TURN_BYTES)
        (self.root / 'MEMORY/active').mkdir(parents=True)
        (self.root / 'STATE').mkdir(parents=True)

    def test_dream_learning_runs_on_the_upgraded_production_schema(self):
        path = self.root / 'data/minisa.db'
        conn = build_production_v4(path)
        self.addCleanup(conn.close)
        seed_preserved_rows(conn)
        apply_migrations(conn, installation_name='rehearsal', software_version='rehearsal')

        db = Database(path)
        workspace = Workspace(db, self.root,
                              WorkspacePolicy(pressure_threshold=0.1, cooldown=1,
                                              idle_seconds=1, calls_per_hour=10),
                              clock=lambda: 1_800_000_000.0)
        record = {'schema': 1, 'id': 'turn_rehearsal', 'sequence': 1,
                  'started_at': '2026-10-16T22:00:00+00:00', 'status': 'completed',
                  'stage': 'final', 'user_input': f'remember this: {CLAIM}',
                  'provenance': 'user', 'answer': None, 'returned_text': None, 'failure': None,
                  'action': {'action': 'remember', 'status': 'completed',
                             'receipt': {'status': 'committed', 'committed': True,
                                         'durable': True, 'path': 'EXPERIENCE/turn_rehearsal.md',
                                         'warnings': []}},
                  'cognitive_outcome': {'status': 'no_changes', 'committed': False},
                  'experience': None, 'reflection': None, 'delivery': 'not_observed'}
        turn_path = self.root / 'EXPERIENCE/turns/turn_rehearsal.json'
        turn_path.write_text(json.dumps(record))
        workspace.ingest_turn(record, turn_path)

        scripted = {'changes': [{'type': 'memory', 'title': 'Tea ceremony preparation',
                                 'fact': CLAIM, 'evidence': ['turn_rehearsal']}]}
        result = DreamWorker(workspace, ScriptedModel(scripted)).tick()
        self.assertNotIn(result.get('status'), ('failed', 'deferred'), result)
        self.assertEqual(len(result['accepted']), 1, result)

        with db.connect(readonly=True) as read:
            stored = read.execute('SELECT details_json FROM memory_records').fetchall()
        self.assertEqual(len(stored), 1)
        self.assertIn('two spoons of matcha', json.loads(stored[0][0])['fact'])

        # The protected rows of the rehearsal database are untouched.
        with db.connect(readonly=True) as read:
            preserved = read.execute(
                "SELECT status, error FROM jobs WHERE job_id='job_dream_failed'").fetchone()
            self.assertEqual(preserved['status'], 'failed')
            self.assertEqual(preserved['error'], 'model_timeout')
            self.assertEqual(read.execute('SELECT count(*) FROM workspace_evidence'
                                          ).fetchone()[0], 2)
            self.assertEqual(read.execute('SELECT count(*) FROM audit_events WHERE event_id=?',
                                          ('evt_failed',)).fetchone()[0], 1)
            self.assertEqual(read.execute('SELECT count(*) FROM dream_results'
                                          ).fetchone()[0], 2)
            self.assertEqual(read.execute("SELECT count(*) FROM dream_results WHERE job_id=?",
                                          ('job_dream_ok',)).fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
