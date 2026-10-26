import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.db import Database
from runtime.cognition.dream import DreamModelFailure, DreamWorker, run_dream_transport
from runtime.cognition.workspace import Workspace, WorkspacePolicy


class Clock:
    def __init__(self): self.value = 1_800_000_000.0
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds


class Model:
    model = 'disposable-mock'
    usage = {'eval_count': 2}
    def __init__(self, output=None, error=None): self.output, self.error, self.calls = output or {'changes': []}, error, 0
    def propose(self, snapshot, core):
        self.calls += 1
        if self.error: raise self.error
        return self.output


class DreamRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='minisa-dream-test-')
        self.root = Path(self.temp.name)
        (self.root / 'CORE').mkdir()
        for name in ('identity', 'values', 'goals', 'personality', 'instructions'):
            (self.root / 'CORE' / f'{name}.md').write_text('Persistent identity.')
        self.clock = Clock()
        self.db = Database(self.root / 'data/minisa.db')
        self.db.initialize()
        self.w = Workspace(self.db, self.root, WorkspacePolicy(pressure_threshold=0.1, cooldown=1,
                           idle_seconds=1, max_attempts=3, calls_per_hour=10), self.clock)

    def tearDown(self): self.temp.cleanup()

    def add_turn(self, ident, provenance='user'):
        record = {'id': ident, 'provenance': provenance, 'status': 'completed',
                  'user_input': f'Please consider experience {ident}.',
                  'action': {'action': 'respond', 'status': 'completed'}, 'failure': None}
        path = self.root / 'EXPERIENCE' / 'turns' / f'{ident}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))
        return self.w.ingest_turn(record, path)

    def db_state(self):
        with self.db.connect(readonly=True) as c:
            return (c.execute('SELECT global_revision FROM core_state').fetchone()[0],
                    c.execute('SELECT count(*) FROM dream_results').fetchone()[0],
                    c.execute('SELECT count(*) FROM workspace_evidence WHERE processed=0').fetchone()[0],
                    c.execute('SELECT count(*) FROM workspace_evidence').fetchone()[0])

    def test_valid_response_commits_one_auditable_result(self):
        self.add_turn('real-1')
        model = Model()
        result = DreamWorker(self.w, model).tick()
        self.assertEqual(result['revision'], result['source_revision'] + 1)
        self.assertEqual(result['records_considered'], 1)
        self.assertEqual(model.calls, 1)
        self.assertEqual(self.db_state(), (result['revision'], 1, 0, 1))
        with self.db.connect(readonly=True) as c:
            self.assertEqual(c.execute('SELECT last_success FROM dream_state').fetchone()[0], self.clock())
            self.assertEqual(c.execute("SELECT status FROM jobs WHERE job_type='dream'").fetchone()[0], 'completed')

    def test_timeout_fails_without_mutation_then_recovers(self):
        self.add_turn('real-timeout')
        worker = DreamWorker(self.w, Model(error=DreamModelFailure('timeout', 'transport', 'request_deadline')))
        before = self.db_state()
        self.assertEqual(worker.tick(), {'status': 'failed', 'category': 'timeout'})
        self.assertEqual(self.db_state(), before)
        with self.db.connect(readonly=True) as c:
            self.assertEqual(c.execute('SELECT error FROM jobs WHERE job_type="dream"').fetchone()[0], 'timeout')
            audit = json.loads(c.execute("SELECT details_json FROM audit_events WHERE operation='dream.failed'").fetchone()[0])
        self.assertEqual((audit['stage'], audit['kind']), ('transport', 'request_deadline'))
        self.clock.advance(3)
        self.assertEqual(DreamWorker(self.w, Model()).tick()['revision'], before[0] + 1)

    def test_invalid_output_does_not_advance_revision_or_consume_evidence(self):
        self.add_turn('real-invalid')
        before = self.db_state()
        worker = DreamWorker(self.w, Model(error=DreamModelFailure('invalid_output', 'model_output', 'malformed_json')))
        self.assertEqual(worker.tick()['category'], 'invalid_output')
        self.assertEqual(self.db_state(), before)
        with self.db.connect(readonly=True) as c:
            audit = json.loads(c.execute("SELECT details_json FROM audit_events WHERE operation='dream.failed'").fetchone()[0])
        self.assertEqual(audit['kind'], 'malformed_json')

    def test_recovery_circuit_opens_then_allows_bounded_new_job(self):
        self.add_turn('real-circuit')
        for _ in range(3):
            self.assertEqual(DreamWorker(self.w, Model(error=DreamModelFailure('transport', 'transport', 'network_error'))).tick()['status'], 'failed')
            self.clock.advance(100)
        with self.db.connect(readonly=True) as c:
            state = c.execute('SELECT failures,recovery_after FROM dream_state').fetchone()
            recovery_after = state['recovery_after']
            self.assertEqual(state['failures'], 3)
            self.assertGreater(recovery_after, self.clock())
        self.assertIsNone(DreamWorker(self.w, Model()).tick())
        self.clock.value = recovery_after + 1
        previous_revision = self.db_state()[0]
        self.assertEqual(DreamWorker(self.w, Model()).tick()['revision'], previous_revision + 1)
        with self.db.connect(readonly=True) as c:
            self.assertEqual(c.execute('SELECT failures FROM dream_state').fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT count(*) FROM jobs WHERE job_type='dream' AND status='completed'").fetchone()[0], 1)

    def test_retry_apply_is_atomic_and_accepts_exactly_once(self):
        self.add_turn('real-retry')
        original = self.w._rebalance
        attempts = {'n': 0}
        def fail_once(*args):
            attempts['n'] += 1
            if attempts['n'] == 2: raise RuntimeError('simulated transaction failure')
            return original(*args)
        with patch.object(self.w, '_rebalance', side_effect=fail_once):
            before = self.db_state()
            self.assertEqual(DreamWorker(self.w, Model()).tick()['status'], 'failed')
            self.assertEqual(self.db_state(), before)
            self.clock.advance(3)
            recovered = DreamWorker(self.w, Model()).tick()
            self.assertEqual(recovered['revision'], before[0] + 1)
        self.assertEqual(self.db_state(), (before[0] + 1, 1, 0, 1))

    def test_qualification_evidence_never_reaches_dream_snapshot(self):
        self.assertEqual(self.add_turn('qualification', 'qualification'),
                         {'filtered': True, 'reason': 'non_user_provenance'})
        self.add_turn('genuine')
        job_id = self.w.schedule()
        job = self.db.lease_job('test', job_type='dream', now_timestamp=self.clock())
        self.assertEqual(job['job_id'], job_id)
        snapshot = self.w.snapshot()
        self.assertEqual([row['evidence_id'] for row in snapshot['evidence']], ['genuine'])
        self.assertEqual(self.db_state()[3], 1)

    def test_expired_lease_recovers_after_worker_restart_without_duplicate_job(self):
        self.add_turn('restart-evidence')
        job_id = self.w.schedule()
        first_claim = self.db.lease_job('old-worker', job_type='dream', now_timestamp=self.clock())
        self.assertEqual(first_claim['job_id'], job_id)
        self.clock.advance(61)
        result = DreamWorker(self.w, Model()).tick()
        self.assertIsNotNone(result)
        with self.db.connect(readonly=True) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM jobs WHERE job_type='dream'").fetchone()[0], 1)
            job = c.execute("SELECT status,attempt_count FROM jobs WHERE job_type='dream'").fetchone()
            self.assertEqual((job['status'], job['attempt_count']), ('completed', 2))
            self.assertEqual(c.execute('SELECT count(*) FROM dream_results').fetchone()[0], 1)

    def test_transport_deadline_includes_child_with_teardown_margin(self):
        def timeout(*args, **kwargs):
            self.assertEqual(kwargs['timeout'], 42)
            raise subprocess.TimeoutExpired(args[0], kwargs['timeout'])
        with patch('runtime.cognition.dream.subprocess.run', side_effect=timeout):
            with self.assertRaises(DreamModelFailure) as caught:
                run_dream_transport('http://mock', {}, 40, cwd=self.root)
        self.assertEqual((caught.exception.category, caught.exception.stage, caught.exception.kind),
                         ('timeout', 'transport', 'child_deadline'))


if __name__ == '__main__': unittest.main()
