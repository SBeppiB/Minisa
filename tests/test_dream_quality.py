import contextlib
import io
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from core.db import Database
from runtime.cognition.dream import DreamModelFailure, DreamWorker
from runtime.cognition.dream_quality import (
    build_recent_report,
    load_report,
    main,
)
from runtime.cognition.workspace import DreamConflict, Workspace, WorkspacePolicy
from tests.test_migration_chain import build_production_v4

SECRET_FACT = 'The violet kettle requires exactly two spoons of matcha at 80 degrees.'
SECRET_CLAIM = 'A protected belief claim that must never be copied into a report.'


class Clock:
    def __init__(self):
        self.value = 1_800_000_000.0
    def __call__(self):
        return self.value
    def advance(self, seconds):
        self.value += seconds


class Model:
    model = 'disposable-mock'
    usage = {'eval_count': 2}
    def __init__(self, output=None, error=None):
        self.output = {'changes': []} if output is None else output
        self.error = error
        self.calls = 0
    def propose(self, snapshot, core):
        self.calls += 1
        if self.error:
            raise self.error
        return self.output


class DreamQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='minisa-dream-quality-')
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

    def report(self, job_ids, now=None):
        with self.db.connect(readonly=True) as conn:
            return build_recent_report(
                conn, job_ids=job_ids,
                now=self.clock.value if now is None else now,
            )

    def run_dream(self, model):
        result = DreamWorker(self.w, model).tick()
        return result, self.last_job_id()

    def seed_memory_evidence(self):
        self.add_turn('turn-memory-source', f'remember this: {SECRET_FACT}',
                      'remember', mutation_committed=True)
        self.add_turn('turn-discussion', 'let us discuss the kettle preparation')
        return {'changes': [{
            'type': 'memory',
            'title': 'Kettle preparation',
            'fact': SECRET_FACT,
            'evidence': ['turn-memory-source'],
        }]}

    def insert_job(self, job_id, *, status, error=None, result_obj=None,
                   source_revision=None, resulting_revision=None,
                   add_result=False, add_commit=False):
        occurred = datetime.fromtimestamp(
            self.clock.value, timezone.utc).isoformat()
        encoded = None if result_obj is None else json.dumps(result_obj)
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO jobs(job_id,job_type,payload_json,status,created_at,"
                "attempt_count,result_json,error,completed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (job_id, 'dream', '{}', status, occurred, 1, encoded, error,
                 occurred if status in {'completed', 'failed', 'review_required'} else None),
            )
            if add_result:
                conn.execute(
                    "INSERT INTO dream_results(job_id,source_revision,resulting_revision,"
                    "result_json,created_at) VALUES(?,?,?,?,?)",
                    (job_id, source_revision, resulting_revision,
                     json.dumps(result_obj), self.clock.value),
                )
            if add_commit:
                details = {
                    'accepted': len(result_obj.get('accepted', [])),
                    'review': len(result_obj.get('review', [])),
                }
                conn.execute(
                    "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), 1, occurred, 'test', 'dream.committed',
                     'dream', job_id, json.dumps(details), None),
                )

    def test_successful_dream_with_accepted_change_is_verified_and_substantive(self):
        proposal = self.seed_memory_evidence()
        result, job_id = self.run_dream(Model(proposal))
        self.assertEqual(len(result['accepted']), 1, result)

        report = self.report([job_id])
        dream = report['dreams'][0]
        self.assertTrue(report['read_only'])
        self.assertEqual(report['selection']['selected_job_ids'], [job_id])
        self.assertEqual(dream['execution']['outcome'], 'success')
        self.assertEqual(dream['execution']['status'], 'completed')
        self.assertTrue(dream['persistence']['verified'])
        self.assertTrue(dream['persistence']['provenance_verified'])
        self.assertTrue(dream['persistence']['commit_audit_present'])
        self.assertEqual(dream['proposals']['accepted_total'], 1)
        self.assertEqual(dream['proposals']['review_total'], 0)
        self.assertEqual(dream['proposals']['accepted_by_type']['sourced_memories'], 1)
        self.assertEqual(dream['proposals']['dispositions']['accepted'], 1)
        self.assertFalse(dream['proposals']['dispositions']['deferred'])
        self.assertFalse(dream['proposals']['dispositions']['proposal_rejected'])
        self.assertEqual(dream['proposals']['dispositions']['rejected_count'], 0)
        self.assertTrue(dream['knowledge_impact']['assessed'])
        self.assertTrue(dream['knowledge_impact']['substantive_change'])
        self.assertFalse(dream['knowledge_impact']['bookkeeping_only'])
        self.assertFalse(dream['knowledge_impact']['review_only'])
        self.assertEqual(dream['knowledge_impact']['durable_categories'],
                         ['sourced_memories'])
        self.assertFalse(dream['knowledge_impact']['learning_benefit_verified'])
        self.assertEqual(dream['persistence']['revision_delta'], 1)
        self.assertEqual(dream['coverage']['evidence_items_considered'], 2)
        self.assertIsNone(dream['coverage']['cognitive_units_considered'])
        self.assertFalse(dream['model']['verified'])
        self.assertIn('model_identifier', dream['missing_fields'])
        self.assertIn('latency_seconds', dream['missing_fields'])
        self.assertEqual(report['summary']['substantive_changes'], 1)
        self.assertEqual(report['summary']['persistence_verified'], 1)

        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn(SECRET_FACT, serialized)
        self.assertNotIn('"fact"', serialized)
        self.assertNotIn('"claim"', serialized)
        self.assertNotIn('"user_statement"', serialized)

    def test_successful_empty_dream_is_bookkeeping_only(self):
        self.add_turn('turn-empty', 'nothing to learn yet')
        result, job_id = self.run_dream(Model({'changes': []}))
        self.assertEqual(result['accepted'], [])
        self.assertEqual(result['review'], [])

        dream = self.report([job_id])['dreams'][0]
        self.assertEqual(dream['execution']['outcome'], 'success')
        self.assertTrue(dream['persistence']['verified'])
        self.assertEqual(dream['proposals']['accepted_total'], 0)
        self.assertEqual(dream['proposals']['review_total'], 0)
        self.assertTrue(dream['knowledge_impact']['assessed'])
        self.assertFalse(dream['knowledge_impact']['substantive_change'])
        self.assertTrue(dream['knowledge_impact']['bookkeeping_only'])
        self.assertTrue(dream['knowledge_impact']['no_recorded_changes'])
        self.assertFalse(dream['knowledge_impact']['review_only'])
        self.assertEqual(dream['knowledge_impact']['durable_categories'], [])
        self.assertEqual(dream['knowledge_impact']['revision_delta'], 1)

    def test_successful_review_only_dream_is_not_called_learning(self):
        self.add_turn('turn-review', 'consider a protected claim')
        proposal = {'changes': [{
            'type': 'review', 'target': 'belief', 'claim': SECRET_CLAIM,
            'evidence': ['turn-review'],
        }]}
        result, job_id = self.run_dream(Model(proposal))
        self.assertEqual(len(result['accepted']), 0, result)
        self.assertEqual(len(result['review']), 1, result)

        dream = self.report([job_id])['dreams'][0]
        self.assertTrue(dream['persistence']['verified'])
        self.assertEqual(dream['proposals']['accepted_total'], 0)
        self.assertEqual(dream['proposals']['review_total'], 1)
        self.assertEqual(dream['proposals']['review_by_type']['review_items'], 1)
        self.assertTrue(dream['knowledge_impact']['assessed'])
        self.assertFalse(dream['knowledge_impact']['substantive_change'])
        self.assertFalse(dream['knowledge_impact']['bookkeeping_only'])
        self.assertTrue(dream['knowledge_impact']['review_only'])
        self.assertEqual(dream['knowledge_impact']['review_categories'],
                         ['review_items'])
        self.assertEqual(dream['knowledge_impact']['durable_categories'], [])
        self.assertFalse(dream['knowledge_impact']['learning_benefit_verified'])
        self.assertNotIn(SECRET_CLAIM, json.dumps(dream, sort_keys=True))

    def test_timeout_is_reported_without_persistence_or_learning_claims(self):
        self.add_turn('turn-timeout', 'consider a timeout')
        result, job_id = self.run_dream(
            Model(error=DreamModelFailure('timeout', 'transport', 'request_deadline')))
        self.assertEqual(result, {'status': 'failed', 'category': 'timeout'})

        report = self.report([job_id])
        dream = report['dreams'][0]
        self.assertEqual(dream['execution']['outcome'], 'timeout')
        self.assertEqual(dream['execution']['failure']['category'], 'timeout')
        self.assertEqual(dream['execution']['failure']['stage'], 'transport')
        self.assertEqual(dream['execution']['failure']['kind'], 'request_deadline')
        self.assertIsInstance(dream['execution']['failure']['latency_seconds'], float)
        self.assertFalse(dream['persistence']['verified'])
        self.assertFalse(dream['persistence']['commit_audit_present'])
        self.assertIsNone(dream['proposals']['dispositions']['proposal_rejected'])
        self.assertIsNone(dream['proposals']['dispositions']['rejected_count'])
        self.assertFalse(dream['knowledge_impact']['assessed'])
        self.assertIsNone(dream['knowledge_impact']['substantive_change'])
        self.assertFalse(dream['knowledge_impact']['learning_benefit_verified'])
        self.assertFalse(dream['model']['verified'])
        self.assertIsNone(dream['model']['value'])
        self.assertIn('knowledge_impact', dream['missing_fields'])
        self.assertIn('rejected_proposal_count', dream['missing_fields'])
        self.assertIn('model_identifier', dream['missing_fields'])
        self.assertNotIn('latency_seconds', dream['missing_fields'])
        self.assertEqual(report['summary']['failed_executions'], 1)
        self.assertEqual(report['current_gate']['historical_failure_jobs'], 1)

    def test_validation_failure_is_whole_proposal_rejection_without_content(self):
        self.add_turn('turn-invalid', 'remember this: the kettle is blue')
        proposal = {'changes': [{
            'type': 'memory',
            'title': 'Unsupported kettle claim',
            'fact': 'The kettle requires a hidden violet ritual at midnight.',
            'evidence': ['turn-invalid'],
        }]}
        result, job_id = self.run_dream(Model(proposal))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['category'], 'invalid_output')

        report = self.report([job_id])
        dream = report['dreams'][0]
        self.assertEqual(dream['execution']['outcome'], 'validation_failure')
        self.assertEqual(dream['execution']['failure']['category'], 'invalid_output')
        self.assertEqual(dream['execution']['failure']['stage'], 'workspace_validation')
        self.assertEqual(dream['execution']['failure']['kind'], 'schema_rejected')
        self.assertTrue(dream['proposals']['dispositions']['proposal_rejected'])
        self.assertIsNone(dream['proposals']['dispositions']['rejected_count'])
        self.assertFalse(dream['persistence']['verified'])
        self.assertIsNone(dream['knowledge_impact']['substantive_change'])
        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn('hidden violet ritual', serialized)

    def test_conflict_and_writer_busy_deferral_are_not_persistence(self):
        self.add_turn('turn-deferred', 'consider a deferred dream')
        with patch.object(self.w, 'apply', side_effect=DreamConflict('sqlite_revision')):
            first = DreamWorker(self.w, Model({'changes': []})).tick()
        self.assertEqual(first['status'], 'deferred')
        first_job = self.last_job_id()
        self.clock.advance(2_000)
        with patch.object(self.w, 'apply', side_effect=BlockingIOError('busy')):
            second = DreamWorker(self.w, Model({'changes': []})).tick()
        self.assertEqual(second['status'], 'deferred')
        second_job = self.last_job_id()
        self.assertNotEqual(first_job, second_job)

        report = self.report([first_job, second_job])
        first_dream, second_dream = report['dreams']
        self.assertEqual(first_dream['execution']['outcome'], 'conflict')
        self.assertEqual(first_dream['execution']['deferral']['category'],
                         'sqlite_revision')
        self.assertEqual(second_dream['execution']['outcome'], 'deferral')
        self.assertEqual(second_dream['execution']['deferral']['category'],
                         'writer_busy')
        for dream in (first_dream, second_dream):
            self.assertTrue(dream['execution']['deferred'])
            self.assertTrue(dream['proposals']['dispositions']['deferred'])
            self.assertFalse(dream['persistence']['verified'])
            self.assertFalse(dream['knowledge_impact']['assessed'])
            self.assertIsNone(dream['knowledge_impact']['substantive_change'])

    def test_incomplete_records_are_unverified_not_learning(self):
        self.insert_job('completed-without-result', status='completed')
        self.insert_job('completed-with-array-result', status='completed',
                        result_obj=[], add_result=True,
                        source_revision=1, resulting_revision=2)
        self.insert_job('completed-with-revision-mismatch', status='completed',
                        result_obj={'source_revision': 5, 'revision': 6,
                                    'accepted': [], 'review': [],
                                    'records_considered': 0},
                        add_result=True, source_revision=1, resulting_revision=2)

        report = self.report([
            'completed-without-result',
            'completed-with-array-result',
            'completed-with-revision-mismatch',
        ])
        missing, array, mismatch = report['dreams']

        self.assertEqual(missing['execution']['outcome'], 'success')
        self.assertFalse(missing['persistence']['verified'])
        self.assertIsNone(missing['knowledge_impact']['substantive_change'])
        self.assertIn('missing_dream_result',
                      [w['code'] for w in missing['warnings']])

        self.assertTrue(array['persistence']['result_json_valid'])
        self.assertFalse(array['persistence']['result_object_valid'])
        self.assertFalse(array['persistence']['verified'])
        self.assertIn('result_not_object',
                      [w['code'] for w in array['warnings']])
        self.assertIsNone(array['knowledge_impact']['substantive_change'])

        self.assertFalse(mismatch['persistence']['verified'])
        self.assertFalse(mismatch['persistence']['revisions_consistent'])
        self.assertIn('revision_mismatch',
                      [w['code'] for w in mismatch['warnings']])
        self.assertIsNone(mismatch['knowledge_impact']['substantive_change'])
        self.assertEqual(report['summary']['substantive_changes'], 0)

    def test_accepted_change_without_revision_change_is_not_substantive(self):
        result = {
            'source_revision': 1,
            'revision': 1,
            'accepted': [{'type': 'memory'}],
            'review': [],
            'records_considered': 0,
        }
        self.insert_job('accepted-without-revision', status='completed',
                        result_obj=result, add_result=True,
                        source_revision=1, resulting_revision=1,
                        add_commit=True)
        dream = self.report(['accepted-without-revision'])['dreams'][0]
        self.assertTrue(dream['persistence']['verified'])
        self.assertTrue(dream['knowledge_impact']['assessed'])
        self.assertFalse(dream['knowledge_impact']['revision_changed'])
        self.assertEqual(dream['knowledge_impact']['revision_delta'], 0)
        self.assertFalse(dream['knowledge_impact']['substantive_change'])
        self.assertFalse(dream['knowledge_impact']['bookkeeping_only'])
        self.assertFalse(dream['knowledge_impact']['review_only'])
        self.assertFalse(dream['knowledge_impact']['no_recorded_changes'])
        self.assertIn('accepted_without_revision_change',
                      [w['code'] for w in dream['warnings']])
        self.assertIn('no revision change',
                      dream['knowledge_impact']['reason'])
        self.assertFalse(dream['knowledge_impact']['learning_benefit_verified'])

    def test_missing_commit_audit_is_a_provenance_warning_not_persistence_failure(self):
        result = {'source_revision': 1, 'revision': 2,
                  'accepted': [], 'review': [], 'records_considered': 0}
        self.insert_job('completed-without-commit', status='completed',
                        result_obj=result, add_result=True,
                        source_revision=1, resulting_revision=2)
        dream = self.report(['completed-without-commit'])['dreams'][0]
        self.assertTrue(dream['persistence']['verified'])
        self.assertFalse(dream['persistence']['commit_audit_present'])
        self.assertFalse(dream['persistence']['provenance_verified'])
        self.assertIn('missing_commit_audit',
                      [w['code'] for w in dream['warnings']])
        self.assertTrue(dream['knowledge_impact']['bookkeeping_only'])

    def test_historical_failures_do_not_imply_current_gate_state(self):
        for index in range(3):
            self.insert_job(f'historical-{index}', status='failed', error='timeout')
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE dream_state SET failures=0,deferrals=0,recovery_after=0,"
                "next_allowed=0,last_failure_category=NULL WHERE singleton=1"
            )
        with self.db.connect(readonly=True) as conn:
            inactive = build_recent_report(conn, now=self.clock.value)
        self.assertFalse(inactive['current_gate']['active'])
        self.assertEqual(inactive['current_gate']['historical_failure_jobs'], 3)
        self.assertEqual(inactive['summary']['historical_failure_jobs'], 3)
        self.assertEqual(inactive['summary']['failed_executions'], 3)

        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE dream_state SET recovery_after=? WHERE singleton=1",
                (self.clock.value + 60,),
            )
        with self.db.connect(readonly=True) as conn:
            active = build_recent_report(conn, now=self.clock.value)
        self.assertTrue(active['current_gate']['active'])
        self.assertEqual(active['current_gate']['historical_failure_jobs'], 3)

    def test_loader_is_read_only_and_does_not_mutate_the_database(self):
        proposal = self.seed_memory_evidence()
        _, job_id = self.run_dream(Model(proposal))
        with self.db.connect(readonly=True) as conn:
            before = {
                'revision': conn.execute(
                    'SELECT global_revision FROM core_state').fetchone()[0],
                'jobs': conn.execute('SELECT count(*) FROM jobs').fetchone()[0],
                'results': conn.execute(
                    'SELECT count(*) FROM dream_results').fetchone()[0],
                'audits': conn.execute(
                    'SELECT count(*) FROM audit_events').fetchone()[0],
                'dream_state': dict(conn.execute(
                    'SELECT * FROM dream_state').fetchone()),
            }
        report = load_report(self.db.path, job_ids=[job_id], now=self.clock.value)
        self.assertEqual(report['dreams'][0]['job_id'], job_id)
        self.assertTrue(report['read_only'])
        with self.db.connect(readonly=True) as conn:
            after = {
                'revision': conn.execute(
                    'SELECT global_revision FROM core_state').fetchone()[0],
                'jobs': conn.execute('SELECT count(*) FROM jobs').fetchone()[0],
                'results': conn.execute(
                    'SELECT count(*) FROM dream_results').fetchone()[0],
                'audits': conn.execute(
                    'SELECT count(*) FROM audit_events').fetchone()[0],
                'dream_state': dict(conn.execute(
                    'SELECT * FROM dream_state').fetchone()),
            }
        self.assertEqual(before, after)

    def test_unknown_requested_job_is_reported_as_missing(self):
        report = load_report(self.db.path, job_ids=['no-such-dream-job'],
                             now=self.clock.value)
        self.assertEqual(report['dreams'], [])
        self.assertIn('requested_job_missing',
                      [warning['code'] for warning in report['warnings']])

    def test_report_reads_production_schema_four_without_new_tables(self):
        path = self.root / 'schema-four.db'
        conn = build_production_v4(path)
        occurred = datetime.fromtimestamp(
            self.clock.value, timezone.utc).isoformat()
        result = {'source_revision': 0, 'revision': 1,
                  'accepted': [], 'review': [], 'records_considered': 0}
        conn.execute(
            "INSERT INTO jobs(job_id,job_type,payload_json,status,created_at,"
            "attempt_count,result_json,completed_at) VALUES(?,?,?,?,?,?,?,?)",
            ('schema-four-job', 'dream', '{}', 'completed', occurred, 1,
             json.dumps(result), occurred),
        )
        conn.execute(
            "INSERT INTO dream_results(job_id,source_revision,resulting_revision,"
            "result_json,created_at) VALUES(?,?,?,?,?)",
            ('schema-four-job', 0, 1, json.dumps(result), self.clock.value),
        )
        conn.execute(
            "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), 1, occurred, 'test', 'dream.committed',
             'dream', 'schema-four-job',
             json.dumps({'accepted': 0, 'review': 0}), None),
        )
        conn.commit()
        conn.close()

        report = load_report(path, job_ids=['schema-four-job'],
                             now=self.clock.value)
        self.assertEqual(report['database_schema_version'], 4)
        self.assertIn('older_schema',
                      [warning['code'] for warning in report['warnings']])
        self.assertTrue(report['dreams'][0]['persistence']['verified'])
        self.assertTrue(report['dreams'][0]['knowledge_impact']['bookkeeping_only'])

    def test_cli_emits_json_report(self):
        self.add_turn('turn-cli', 'nothing to learn')
        _, job_id = self.run_dream(Model({'changes': []}))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(['--database', str(self.db.path), '--job-id', job_id])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report['report'], 'dream_quality')
        self.assertEqual(report['dreams'][0]['job_id'], job_id)


if __name__ == '__main__':
    unittest.main()
