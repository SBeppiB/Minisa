import contextlib
import hashlib
import io
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.db import Database, canonical_json
from runtime.cognition.dream import DreamModelFailure, DreamWorker
from runtime.cognition.dream_provenance import (
    build_provenance_report,
    load_provenance_report,
    main,
)
from runtime.cognition.workspace import Workspace, WorkspacePolicy
from tests.test_dream_quality import Clock, Model
from tests.test_migration_chain import build_production_v4

SECRET_FACT = 'The violet kettle requires exactly two spoons of matcha at 80 degrees.'
SECRET_CLAIM = 'A protected belief claim that must never be copied into a provenance report.'
JOB_COPY_SAME = object()


class DreamProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='minisa-dream-provenance-')
        self.root = Path(self.temp.name)
        (self.root / 'CORE').mkdir()
        for name in ('identity', 'values', 'goals', 'personality', 'instructions'):
            (self.root / 'CORE' / f'{name}.md').write_text('Persistent identity.')
        self.clock = Clock()
        self.sequence = 0
        self.db = Database(self.root / 'data/minisa.db')
        self.db.initialize()
        self.w = Workspace(
            self.db, self.root,
            WorkspacePolicy(pressure_threshold=0.1, cooldown=1, idle_seconds=1,
                            max_attempts=3, calls_per_hour=10),
            self.clock,
        )

    def tearDown(self):
        self.temp.cleanup()

    def add_turn(self, ident, user_input, action='respond', mutation_committed=False):
        self.sequence += 1
        record = {
            'schema': 1,
            'id': ident,
            'sequence': self.sequence,
            'started_at': datetime.fromtimestamp(
                self.clock.value, timezone.utc).isoformat(),
            'status': 'completed',
            'stage': 'final',
            'user_input': user_input,
            'provenance': 'user',
            'answer': None,
            'returned_text': None,
            'failure': None,
            'action': {'action': action, 'status': 'completed',
                       'receipt': None},
            'cognitive_outcome': {'status': 'no_changes', 'committed': False},
            'experience': None,
            'reflection': None,
            'delivery': 'not_observed',
        }
        path = self.root / 'EXPERIENCE' / 'turns' / f'{ident}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))
        return self.w.ingest_turn(record, path)

    def last_job_id(self):
        with self.db.connect(readonly=True) as conn:
            return conn.execute(
                "SELECT job_id FROM jobs WHERE job_type='dream' "
                "ORDER BY created_at DESC,job_id DESC LIMIT 1"
            ).fetchone()[0]

    def report(self, job_ids=None):
        with self.db.connect(readonly=True) as conn:
            return build_provenance_report(
                conn, job_ids=job_ids, now=self.clock.value
            )

    def run_dream(self, model):
        result = DreamWorker(self.w, model).tick()
        return result, self.last_job_id()

    def audit(self, job_id, operation, details, revision=2):
        with self.db.transaction(immediate=True) as conn:
            return self.db.append_audit(
                conn, actor='test', operation=operation,
                target_category='dream', target_id=job_id,
                details=details, global_revision=revision,
            )

    def insert_completed(self, job_id, result_obj, *, source_revision=1,
                         resulting_revision=2, add_result=True,
                         add_commit=True, job_result_obj=JOB_COPY_SAME):
        occurred = datetime.fromtimestamp(
            self.clock.value, timezone.utc).isoformat()
        job_copy = result_obj if job_result_obj is JOB_COPY_SAME else job_result_obj
        encoded_job = None if job_copy is None else json.dumps(job_copy)
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO jobs(job_id,job_type,payload_json,status,created_at,"
                "attempt_count,result_json,error,completed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (job_id, 'dream', '{}', 'completed', occurred, 1, encoded_job,
                 None, occurred),
            )
            if add_result:
                conn.execute(
                    "INSERT INTO dream_results(job_id,source_revision,resulting_revision,"
                    "result_json,created_at) VALUES(?,?,?,?,?)",
                    (job_id, source_revision, resulting_revision,
                     json.dumps(result_obj), self.clock.value),
                )
            if add_commit:
                conn.execute(
                    "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?,?,?)",
                    ('commit-' + job_id, resulting_revision, occurred, 'test',
                     'dream.committed', 'dream', job_id,
                     json.dumps({
                         'accepted': len(result_obj.get('accepted', [])),
                         'review': len(result_obj.get('review', [])),
                     }), None),
                )

    def insert_unit(self, change, revision):
        key = hashlib.sha256(canonical_json(change).encode()).hexdigest()
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO workspace_units(unit_id,kind,subject,payload_json,"
                "evidence_json,created_at,activated_at,activation,importance,"
                "uncertainty,priority,status,revision,dedup_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ('unit-' + key[:12], change['kind'], change['subject'], '{}',
                 json.dumps(change['evidence']), self.clock.value,
                 self.clock.value, 1, .5, .5, .5, 'cold', revision,
                 'dream:' + key),
            )
        return 'unit-' + key[:12]

    def test_accepted_memory_traces_evidence_audit_record_and_revision(self):
        self.add_turn('turn-memory-source', f'remember this: {SECRET_FACT}',
                      'remember', mutation_committed=True)
        self.add_turn('turn-discussion', 'let us discuss the kettle preparation')
        proposal = {'changes': [{
            'type': 'memory',
            'title': 'Kettle preparation',
            'fact': SECRET_FACT,
            'evidence': ['turn-memory-source'],
        }]}
        result, job_id = self.run_dream(Model(proposal))
        self.assertEqual(len(result['accepted']), 1)

        report = self.report([job_id])
        dream = report['dreams'][0]
        change = dream['accepted_changes'][0]
        self.assertEqual(report['report'], 'dream_provenance')
        self.assertTrue(report['read_only'])
        self.assertEqual(report['selection']['selected_job_ids'], [job_id])
        self.assertEqual(dream['validation']['outcome'], 'passed')
        self.assertEqual(dream['validation']['accepted_total'], 1)
        self.assertTrue(dream['persistence']['verified'])
        self.assertEqual(change['type'], 'memory')
        self.assertEqual(change['validation']['decision'], 'accepted')
        self.assertEqual(change['evidence']['link'], 'direct')
        self.assertEqual(change['evidence']['status'], 'direct_and_resolved')
        self.assertEqual(change['evidence']['evidence_ids'], ['turn-memory-source'])
        self.assertEqual(change['evidence']['resolved'][0]['evidence_id'],
                         'turn-memory-source')
        self.assertEqual(change['audit']['status'], 'direct')
        self.assertEqual(change['audit']['events'][0]['operation'],
                         'dream.memory_persisted')
        self.assertEqual(change['persistence']['status'], 'verified')
        self.assertEqual(change['resulting_record']['kind'], 'sourced_memory')
        self.assertEqual(change['resulting_record']['status'], 'verified')
        self.assertEqual(change['resulting_record']['link'], 'direct_audit')
        self.assertIsNotNone(change['resulting_record']['id'])
        self.assertEqual(change['trace_status'], 'complete')
        self.assertFalse(dream['learning_benefit']['verified'])
        self.assertEqual(report['summary']['accepted_changes_complete'], 1)
        self.assertEqual(report['summary']['accepted_changes_with_direct_evidence'], 1)
        self.assertEqual(
            report['summary']['accepted_changes_with_verified_persistence'], 1
        )
        self.assertEqual(report['summary']['validation_passed'], 1)
        self.assertFalse(dream['result_copies']['consistent'] is False)
        self.assertIn('dream.committed',
                      [ref['operation'] for ref in dream['audit_references']['job_events']])

        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn(SECRET_FACT, serialized)
        self.assertNotIn('"fact"', serialized)
        self.assertNotIn('"claim"', serialized)
        self.assertNotIn('"user_statement"', serialized)

    def test_successful_empty_dream_has_no_accepted_change_trace(self):
        self.add_turn('turn-empty', 'nothing to learn yet')
        result, job_id = self.run_dream(Model({'changes': []}))
        self.assertEqual(result['accepted'], [])
        dream = self.report([job_id])['dreams'][0]
        self.assertEqual(dream['validation']['outcome'], 'passed')
        self.assertEqual(dream['accepted_changes'], [])
        self.assertEqual(dream['trace_status'], 'no_accepted_changes')
        self.assertFalse(dream['learning_benefit']['verified'])
        self.assertEqual(self.report([job_id])['summary']['accepted_changes'], 0)

    def test_validation_failure_and_timeout_do_not_invent_decisions(self):
        self.add_turn('turn-invalid', 'remember this: the kettle is blue')
        invalid = {'changes': [{
            'type': 'memory',
            'title': 'Unsupported kettle claim',
            'fact': 'The kettle requires a hidden violet ritual at midnight.',
            'evidence': ['turn-invalid'],
        }]}
        result, invalid_id = self.run_dream(Model(invalid))
        self.assertEqual(result['status'], 'failed')
        self.clock.advance(2_000)
        timeout_result, timeout_id = self.run_dream(
            Model(error=DreamModelFailure(
                'timeout', 'transport', 'request_deadline'))
        )
        self.assertEqual(timeout_result['status'], 'failed')

        dreams = self.report([invalid_id, timeout_id])['dreams']
        invalid_dream, timeout_dream = dreams
        self.assertEqual(invalid_dream['validation']['outcome'], 'failed')
        self.assertEqual(invalid_dream['trace_status'], 'failed')
        self.assertEqual(invalid_dream['accepted_changes'], [])
        self.assertEqual(timeout_dream['validation']['outcome'], 'unavailable')
        self.assertEqual(timeout_dream['trace_status'], 'unavailable')
        self.assertEqual(timeout_dream['accepted_changes'], [])
        self.assertIn(
            'validation_decision_unavailable',
            [item['code'] for item in timeout_dream['missing_or_ambiguous_links']],
        )
        self.assertFalse(invalid_dream['learning_benefit']['verified'])
        self.assertFalse(timeout_dream['learning_benefit']['verified'])
        serialized = json.dumps(dreams, sort_keys=True)
        self.assertNotIn('hidden violet ritual', serialized)

    def test_derived_unit_record_is_complete_without_textual_inference(self):
        self.add_turn('turn-unit-a', 'a safe observed event')
        self.add_turn('turn-unit-b', 'another safe observed event')
        change = {
            'type': 'unit',
            'kind': 'concept',
            'subject': 'A bounded concept',
            'evidence': ['turn-unit-a', 'turn-unit-b'],
        }
        unit_id = self.insert_unit(change, revision=2)
        result_obj = {
            'source_revision': 1,
            'revision': 2,
            'accepted': [change],
            'review': [],
            'records_considered': 2,
        }
        self.insert_completed('derived-unit', result_obj)
        self.audit('derived-unit', 'dream.change', change)

        dream = self.report(['derived-unit'])['dreams'][0]
        accepted = dream['accepted_changes'][0]
        self.assertEqual(accepted['evidence']['status'], 'direct_and_resolved')
        self.assertEqual(accepted['resulting_record']['status'], 'verified')
        self.assertEqual(
            accepted['resulting_record']['link'], 'derived_existing_convention'
        )
        self.assertEqual(accepted['resulting_record']['id'], unit_id)
        self.assertEqual(accepted['trace_status'], 'complete')
        self.assertFalse(accepted['evidence']['inferred'])

    def test_missing_evidence_and_record_are_partial_not_complete(self):
        change = {
            'type': 'unit',
            'kind': 'concept',
            'subject': 'Evidence was pruned',
            'evidence': ['turn-pruned'],
        }
        result_obj = {
            'source_revision': 1,
            'revision': 2,
            'accepted': [change],
            'review': [],
            'records_considered': 1,
        }
        self.insert_completed('missing-evidence', result_obj)
        self.audit('missing-evidence', 'dream.change', change)

        dream = self.report(['missing-evidence'])['dreams'][0]
        accepted = dream['accepted_changes'][0]
        self.assertEqual(accepted['evidence']['link'], 'direct')
        self.assertEqual(accepted['evidence']['status'],
                         'direct_with_missing_records')
        self.assertEqual(accepted['evidence']['missing_ids'], ['turn-pruned'])
        self.assertEqual(accepted['audit']['status'], 'direct')
        self.assertEqual(accepted['persistence']['status'], 'verified')
        self.assertEqual(accepted['resulting_record']['status'], 'unavailable')
        self.assertEqual(accepted['trace_status'], 'partial')
        codes = [item['code'] for item in dream['missing_or_ambiguous_links']]
        self.assertIn('evidence_record_missing', codes)
        self.assertFalse(dream['learning_benefit']['verified'])

    def test_duplicate_change_audits_are_ambiguous(self):
        self.add_turn('turn-amb-a', 'first observed event')
        self.add_turn('turn-amb-b', 'second observed event')
        change = {
            'type': 'unit',
            'kind': 'concept',
            'subject': 'Ambiguous audit concept',
            'evidence': ['turn-amb-a', 'turn-amb-b'],
        }
        unit_id = self.insert_unit(change, revision=2)
        result_obj = {
            'source_revision': 1,
            'revision': 2,
            'accepted': [change],
            'review': [],
            'records_considered': 2,
        }
        self.insert_completed('ambiguous-audit', result_obj)
        self.audit('ambiguous-audit', 'dream.change', change)
        self.audit('ambiguous-audit', 'dream.change', change)

        dream = self.report(['ambiguous-audit'])['dreams'][0]
        accepted = dream['accepted_changes'][0]
        self.assertEqual(accepted['audit']['status'], 'ambiguous')
        self.assertEqual(len(accepted['audit']['events']), 2)
        self.assertEqual(accepted['resulting_record']['id'], unit_id)
        self.assertEqual(accepted['trace_status'], 'ambiguous')
        self.assertIn('ambiguous_change_audit',
                      [item['code'] for item in dream['missing_or_ambiguous_links']])
        self.assertFalse(dream['learning_benefit']['verified'])

    def test_result_copy_mismatch_prevents_persistence_claim(self):
        change = {
            'type': 'unit',
            'kind': 'concept',
            'subject': 'Copies disagree',
            'evidence': ['turn-copy-a'],
        }
        self.add_turn('turn-copy-a', 'observed event for a copy mismatch')
        self.insert_unit(change, revision=2)
        result_obj = {
            'source_revision': 1,
            'revision': 2,
            'accepted': [change],
            'review': [],
            'records_considered': 1,
        }
        job_copy = {
            'source_revision': 1,
            'revision': 2,
            'accepted': [],
            'review': [],
            'records_considered': 1,
        }
        self.insert_completed('copy-mismatch', result_obj,
                              job_result_obj=job_copy)
        self.audit('copy-mismatch', 'dream.change', change)

        dream = self.report(['copy-mismatch'])['dreams'][0]
        accepted = dream['accepted_changes'][0]
        self.assertFalse(dream['result_copies']['consistent'])
        self.assertEqual(accepted['persistence']['status'], 'ambiguous')
        self.assertEqual(accepted['trace_status'], 'ambiguous')
        self.assertIn('result_copy_mismatch',
                      [warning['code'] for warning in dream['warnings']])
        self.assertIn('result_copy_mismatch',
                      [item['code'] for item in dream['missing_or_ambiguous_links']])

    def test_historical_failures_do_not_activate_or_imply_the_gate(self):
        for index in range(3):
            self.insert_completed(
                f'historical-{index}',
                {'source_revision': 1, 'revision': 1,
                 'accepted': [], 'review': [], 'records_considered': 0},
                source_revision=1, resulting_revision=1, add_commit=False,
            )
            with self.db.transaction(immediate=True) as conn:
                conn.execute(
                    "UPDATE jobs SET status='failed',error='timeout',"
                    "completed_at=NULL WHERE job_id=?", (f'historical-{index}',)
                )
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE dream_state SET failures=0,deferrals=0,recovery_after=0,"
                "next_allowed=0,last_failure_category=NULL WHERE singleton=1"
            )
        report = self.report()
        self.assertFalse(report['current_gate']['active'])
        self.assertEqual(report['current_gate']['historical_failure_jobs'], 3)
        self.assertEqual(report['summary']['historical_failure_jobs'], 3)
        self.assertFalse(report['summary']['current_gate_active'])

    def test_schema_four_legacy_procedure_resolves_without_memory_tables(self):
        path = self.root / 'provenance-schema-four.db'
        conn = build_production_v4(path)
        occurred = datetime.fromtimestamp(
            self.clock.value, timezone.utc).isoformat()
        change = {
            'type': 'procedure',
            'situation': 'A safe repeated situation',
            'procedure': 'A safe candidate action',
            'evidence': ['turn-legacy'],
        }
        procedure_id = hashlib.sha256(
            canonical_json(change).encode()).hexdigest()
        conn.execute(
            "INSERT INTO workspace_evidence(evidence_id,category,source_path,"
            "sha256,observation_json,created_at,processed) VALUES(?,?,?,?,?,?,?)",
            ('turn-legacy', 'turn', 'EXPERIENCE/turns/turn-legacy.json',
             '0' * 64, '{}', self.clock.value, 1),
        )
        conn.execute(
            "INSERT INTO learned_procedures(procedure_id,situation,procedure,"
            "evidence_json,confidence,revision) VALUES(?,?,?,?,?,?)",
            (procedure_id, change['situation'], change['procedure'],
             json.dumps(change['evidence']), .5, 1),
        )
        result = {
            'source_revision': 0,
            'revision': 1,
            'accepted': [change],
            'review': [],
            'records_considered': 1,
        }
        conn.execute(
            "INSERT INTO jobs(job_id,job_type,payload_json,status,created_at,"
            "attempt_count,result_json,completed_at) VALUES(?,?,?,?,?,?,?,?)",
            ('schema-four-provenance', 'dream', '{}', 'completed', occurred, 1,
             json.dumps(result), occurred),
        )
        conn.execute(
            "INSERT INTO dream_results(job_id,source_revision,resulting_revision,"
            "result_json,created_at) VALUES(?,?,?,?,?)",
            ('schema-four-provenance', 0, 1, json.dumps(result),
             self.clock.value),
        )
        for operation, details in (
            ('dream.committed', {'accepted': 1, 'review': 0}),
            ('dream.change', change),
        ):
            conn.execute(
                "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), 1, occurred, 'test', operation, 'dream',
                 'schema-four-provenance', json.dumps(details), None),
            )
        conn.commit()
        conn.close()

        report = load_provenance_report(
            path, job_ids=['schema-four-provenance'], now=self.clock.value)
        accepted = report['dreams'][0]['accepted_changes'][0]
        self.assertEqual(report['database_schema_version'], 4)
        self.assertIn('older_schema',
                      [warning['code'] for warning in report['warnings']])
        self.assertEqual(accepted['evidence']['status'], 'direct_and_resolved')
        self.assertEqual(accepted['audit']['status'], 'direct')
        self.assertEqual(accepted['resulting_record']['status'], 'verified')
        self.assertEqual(accepted['resulting_record']['kind'],
                         'learned_procedure')
        self.assertEqual(accepted['resulting_record']['id'], procedure_id)
        self.assertEqual(accepted['trace_status'], 'complete')

    def test_load_report_and_cli_are_read_only_json(self):
        self.add_turn('turn-cli', 'a cli-visible observation')
        result, job_id = self.run_dream(Model({'changes': []}))
        self.assertEqual(result['accepted'], [])
        database_path = self.db.path
        loaded = load_provenance_report(
            database_path, job_ids=[job_id], now=self.clock.value
        )
        self.assertEqual(loaded['selection']['selected_job_ids'], [job_id])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            status = main([
                '--database', str(database_path), '--job-id', job_id,
            ])
        self.assertEqual(status, 0)
        cli = json.loads(buffer.getvalue())
        self.assertEqual(cli['report'], 'dream_provenance')
        self.assertEqual(cli['selection']['selected_job_ids'], [job_id])
        self.assertTrue(cli['read_only'])


if __name__ == '__main__':
    unittest.main()
