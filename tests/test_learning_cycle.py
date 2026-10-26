"""First verified learning milestone for Minisa's dream worker.

A complete, reproducible cycle in an isolated temporary workspace:

  controlled experiences -> dream -> guarded admission -> durable persistence
  -> downstream retrieval -> demonstrated behavioural benefit -> rejection of
  an unsupported proposal.

The *software* contract uses a scripted model so it is deterministic. The *live*
component runs the configured real dream model through the unmodified transport,
prompt, wire parser and validator; it is skipped unless a host/model is supplied
via environment so the default suite stays hermetic.

Nothing here mutates the live workspace, the production database, CORE, beliefs,
personality, self-model or state.
"""
import json
import os
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
import tempfile

from core.db import Database
from runtime.cognition.engine import CognitionEngine
from runtime.cognition.dream import DreamModel, DreamWorker
from runtime.cognition.dream_wire import CompactDreamModel
from runtime.cognition.workspace import Workspace, WorkspacePolicy
from runtime.services.memory import LocalMemoryService


# ---------------------------------------------------------------------------
# Isolated workspace fixture
# ---------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.value = 1_800_000_000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


CORE_FILES = ('identity', 'values', 'goals', 'personality', 'instructions')


def valid_receipt(path):
    return {'status': 'committed', 'committed': True, 'durable': True,
            'path': str(path), 'warnings': []}


class LearningWorkspace:
    """Builds a throwaway workspace with controlled evidence, never the live one."""

    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix='minisa-learning-')
        self.root = Path(self.temp.name)
        (self.root / 'CORE').mkdir()
        for name in CORE_FILES:
            (self.root / 'CORE' / f'{name}.md').write_text(
                f'# {name}\n\nControlled test {name}.\n', encoding='utf-8')
        (self.root / 'EXPERIENCE/turns').mkdir(parents=True)
        (self.root / 'MEMORY/active').mkdir(parents=True)
        (self.root / 'STATE').mkdir(parents=True)
        self.clock = Clock()
        self.sequence = 0
        self.db = Database(self.root / 'data/minisa.db')
        self.db.initialize()
        self.workspace = Workspace(
            self.db, self.root,
            WorkspacePolicy(pressure_threshold=0.1, cooldown=1, idle_seconds=1,
                            calls_per_hour=10), self.clock)

    def add_turn(self, ident, user_input, action, action_status='completed',
                 mutation_committed=False, failure=None, status='completed'):
        """Publish one final runtime observation and index it as dream evidence.

        The record is a real runtime turn record (schema 1), so the same file
        is readable by the turn store and by the workspace evidence index.
        """
        self.sequence += 1
        record = {'schema': 1, 'id': ident, 'sequence': self.sequence,
                  'started_at': datetime.fromtimestamp(
                      self.clock.value, timezone.utc).isoformat(),
                  'status': status, 'stage': 'final', 'user_input': user_input,
                  'provenance': 'user', 'answer': None, 'returned_text': None,
                  'failure': failure,
                  'action': {'action': action, 'status': action_status,
                             'receipt': valid_receipt(Path(f'EXPERIENCE/{ident}.md'))
                             if mutation_committed else None},
                  'cognitive_outcome': {'status': 'no_changes', 'committed': False},
                  'experience': None, 'reflection': None, 'delivery': 'not_observed'}
        path = self.root / 'EXPERIENCE/turns' / f'{ident}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))
        return self.workspace.ingest_turn(record, path)

    def evidence_ids(self):
        with self.db.connect(readonly=True) as conn:
            return [row['evidence_id'] for row in conn.execute(
                'SELECT evidence_id FROM workspace_evidence ORDER BY created_at, evidence_id')]

    def memory_rows(self):
        with self.db.connect(readonly=True) as conn:
            return [dict(row) for row in conn.execute(
                'SELECT * FROM memory_records ORDER BY revision, memory_id')]

    def close(self):
        self.temp.cleanup()


# ---------------------------------------------------------------------------
# Scripted model: exercises the real propose/validate/commit boundary
# ---------------------------------------------------------------------------

class ScriptedModel:
    """Returns a predetermined proposal; still passed through the real validator."""
    def __init__(self, proposal):
        self.model = 'scripted'
        self.usage = {'eval_count': 1}
        self.proposal = proposal

    def propose(self, snapshot, core):
        return json.loads(json.dumps(self.proposal))


class LiveWireModel(CompactDreamModel):
    """Records the raw transport response for offline, private inspection."""
    def propose(self, snapshot, core):
        result = super().propose(snapshot, core)
        self.last_raw = getattr(self, 'wire_proposal', None)
        return result


def make_engine(ws):
    """A conversation engine over the same durable store; no model, no network."""
    class FakeReasoner:
        def reason(self, context):
            return {'proposed_action': 'respond', 'interpretation': 'x'}

        def respond(self, context, reasoning):
            return {'kind': 'conversation', 'text': 'ok'}
    memory = LocalMemoryService(ws.root)
    # Keep the suite hermetic: no embedding endpoint is contacted.
    memory.recall = lambda query, limit, semantic=False: []
    engine = CognitionEngine(ws.root, FakeReasoner(), memory)
    engine.workspace = ws.workspace
    engine.user_profile_scope = ''
    return engine


# The low-risk, objectively testable, non-protected learning target. The facts
# come from the user's own explicit remember commands (user_claim, not verified).
TOPIC = 'how do we prepare the tea ceremony'
CLAIM = 'The tea ceremony uses two spoons of matcha and water at 80 degrees.'
UNSUPPORTED_CLAIM = 'the tea ceremony must never use water above 40 degrees'


# ---------------------------------------------------------------------------
# Deterministic software contract
# ---------------------------------------------------------------------------

class LearningCycleTests(unittest.TestCase):

    def setUp(self):
        self.ws = LearningWorkspace()
        self.addCleanup(self.ws.close)

    def _seed_supported_experiences(self):
        """Two observations: one explicit remember command and one discussion."""
        self.ws.add_turn('turn_remember_tea',
                         f'remember this: {CLAIM}',
                         'remember', mutation_committed=True)
        self.ws.add_turn('turn_discuss_tea',
                         'lets discuss how we prepare the tea ceremony with matcha',
                         'respond')

    def test_accepted_memory_becomes_durably_retrievable(self):
        self._seed_supported_experiences()
        ids = self.ws.evidence_ids()
        self.assertIn('turn_remember_tea', ids)

        # A supported memory change: the fact is the user's own sentence, and
        # the cited evidence is the observation that actually contains it.
        scripted = {'changes': [{
            'type': 'memory',
            'title': 'Tea ceremony preparation',
            'fact': CLAIM,
            'evidence': ['turn_remember_tea']}]}
        result = DreamWorker(self.ws.workspace, ScriptedModel(scripted)).tick()
        self.assertNotIn(result.get('status'), ('failed', 'deferred'), result)
        self.assertEqual(len(result['accepted']), 1, result)
        self.assertEqual(result['accepted'][0]['type'], 'memory')
        self.assertEqual(result['revision'], result['source_revision'] + 1)
        self.assertEqual(result['records_considered'], len(ids))

        # Durable: one sourced record with its own provenance.
        rows = self.ws.memory_rows()
        self.assertEqual(len(rows), 1)
        details = json.loads(rows[0]['details_json'])
        self.assertIn('two spoons of matcha', details['fact'])
        self.assertEqual(details['evidence'], ['turn_remember_tea'])
        self.assertEqual(details['authority'], 'runtime_sourced_user_claim')
        self.assertTrue(details['source_path'].endswith('turn_remember_tea.json'))

        # The committed dream consumed the evidence it considered.
        with self.ws.db.connect(readonly=True) as conn:
            self.assertEqual(conn.execute(
                'SELECT count(*) FROM workspace_evidence WHERE processed=0').fetchone()[0], 0)

        # A later conversation retrieves the learned memory. A fresh engine
        # stands in for a restarted process: same durable store, new runtime.
        engine = make_engine(self.ws)
        answer = engine._normal_note_response('what do you know about the tea ceremony')
        self.assertIsNotNone(answer, 'accepted memory was not retrievable')
        self.assertIn('two spoons of matcha', answer)

        # The same learned memory reaches the reasoning context of an ordinary
        # conversation turn, which is the behavioural benefit of the cycle.
        context = engine.build_context(TOPIC)
        facts = [str(entry.get('fact', '')) for entry in context['memories']]
        self.assertTrue(any('two spoons of matcha' in fact for fact in facts),
                        context['memories'])

    def test_supported_consolidation_review_becomes_durable_memory(self):
        """The documented review/memory_consolidation schema learns the same way."""
        self._seed_supported_experiences()
        scripted = {'changes': [{
            'type': 'review', 'target': 'memory_consolidation',
            'claim': CLAIM, 'evidence': ['turn_remember_tea']}]}
        result = DreamWorker(self.ws.workspace, ScriptedModel(scripted)).tick()
        self.assertNotIn(result.get('status'), ('failed', 'deferred'), result)
        rows = self.ws.memory_rows()
        self.assertEqual(len(rows), 1)
        details = json.loads(rows[0]['details_json'])
        self.assertIn('two spoons of matcha', details['fact'])
        self.assertEqual(details['evidence'], ['turn_remember_tea'])
        # A consolidation that became durable memory is not outstanding review work.
        self.assertEqual(self.ws.workspace.pending_reviews(), [])

    def test_unsupported_memory_claim_is_rejected(self):
        self._seed_supported_experiences()
        ids = self.ws.evidence_ids()
        scripted = {'changes': [{
            'type': 'memory', 'title': 'Tea ceremony preparation',
            'fact': UNSUPPORTED_CLAIM, 'evidence': ['turn_remember_tea']}]}

        # The proposal is rejected because the claim is absent from the cited
        # observation, not because the proposal shape was wrong.
        with self.assertRaises(ValueError) as raised:
            self.ws.workspace.validate(scripted, self.ws.workspace.snapshot())
        self.assertIn('not supported by cited observations', str(raised.exception))

        result = DreamWorker(self.ws.workspace, ScriptedModel(scripted)).tick()
        self.assertEqual(result.get('status'), 'failed', result)
        self.assertEqual(result.get('category'), 'invalid_output')
        self.assertEqual(self.ws.memory_rows(), [])
        with self.ws.db.connect(readonly=True) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM workspace_evidence '
                                          'WHERE processed=0').fetchone()[0], len(ids))

    def test_invented_evidence_reference_is_rejected(self):
        self._seed_supported_experiences()
        scripted = {'changes': [{
            'type': 'memory', 'title': 'Tea ceremony preparation',
            'fact': CLAIM, 'evidence': ['turn_does_not_exist']}]}

        # The proposal is rejected because the cited evidence does not exist.
        with self.assertRaises(ValueError) as raised:
            self.ws.workspace.validate(scripted, self.ws.workspace.snapshot())
        self.assertIn('Missing snapshot evidence', str(raised.exception))

        result = DreamWorker(self.ws.workspace, ScriptedModel(scripted)).tick()
        self.assertEqual(result.get('status'), 'failed', result)
        self.assertEqual(result.get('category'), 'invalid_output')
        self.assertEqual(self.ws.memory_rows(), [])

    def test_justified_empty_proposal_commits_without_learning(self):
        self._seed_supported_experiences()
        result = DreamWorker(self.ws.workspace,
                             ScriptedModel({'changes': []})).tick()
        self.assertNotIn(result.get('status'), ('failed', 'deferred'), result)
        self.assertEqual(result.get('revision'), result.get('source_revision', 0) + 1)
        self.assertEqual(result.get('accepted'), [])
        self.assertEqual(self.ws.memory_rows(), [])

    def test_learned_procedure_is_stored_as_a_non_executable_hypothesis(self):
        """Three independent committed actions admit one retrievable hypothesis."""
        for index in range(3):
            self.ws.add_turn(f'turn_procedure_{index}',
                             'procedure: when brewing tea -> use two spoons of matcha',
                             'remember', mutation_committed=True)
        ids = self.ws.evidence_ids()
        self.assertEqual(len(ids), 3)
        scripted = {'changes': [{
            'type': 'procedure',
            'situation': 'when brewing tea',
            'procedure': 'use two spoons of matcha',
            'evidence': ids}]}
        result = DreamWorker(self.ws.workspace, ScriptedModel(scripted)).tick()
        self.assertNotIn(result.get('status'), ('failed', 'deferred'), result)
        with self.ws.db.connect(readonly=True) as conn:
            rows = [dict(row) for row in conn.execute('SELECT * FROM learned_procedures')]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['situation'], 'when brewing tea')
        self.assertEqual(rows[0]['procedure'], 'use two spoons of matcha')
        self.assertEqual(rows[0]['usage_count'], 0)
        procedures = self.ws.workspace.procedures('when brewing tea')
        self.assertEqual(len(procedures), 1)
        self.assertFalse(procedures[0]['executable'])
        self.assertEqual(procedures[0]['status'], 'hypothesis')


# ---------------------------------------------------------------------------
# Live model demonstration (opt-in; requires an explicit host/model)
# ---------------------------------------------------------------------------

LIVE_HOST = os.environ.get('MINISA_LIVE_DREAM_HOST', '')
LIVE_MODEL = os.environ.get('MINISA_LIVE_DREAM_MODEL', '')


@unittest.skipUnless(LIVE_HOST and LIVE_MODEL,
                     'set MINISA_LIVE_DREAM_HOST/MODEL to run the live model demo')
class LiveModelLearningTests(unittest.TestCase):

    def setUp(self):
        self.ws = LearningWorkspace()
        self.addCleanup(self.ws.close)

    def test_live_model_learning_or_justified_empty(self):
        ws = self.ws
        ws.add_turn('turn_remember_tea', f'remember this: {CLAIM}',
                    'remember', mutation_committed=True)
        ws.add_turn('turn_discuss_tea',
                    'lets discuss how we prepare the tea ceremony with matcha',
                    'respond')
        model = LiveWireModel(LIVE_HOST, LIVE_MODEL, timeout=60, tokens=512)
        started = time.perf_counter()
        result = DreamWorker(ws.workspace, model).tick()
        latency = time.perf_counter() - started
        report = {'model': LIVE_MODEL, 'latency_seconds': round(latency, 2),
                  'raw_response': getattr(model, 'last_raw', None),
                  'usage': model.usage, 'result_status': result.get('status'),
                  'accepted': len(result.get('accepted', []) or [])}
        printed = Path(os.environ.get('MINISA_LEARNING_REPORT', '/tmp/minisa-learning-report.json'))
        printed.write_text(json.dumps(report, indent=1, ensure_ascii=False))
        print(json.dumps(report, ensure_ascii=False))
        # A justified empty result remains valid: the milestone is the observable
        # safe behaviour, not a forced proposal. A rejected proposal is also a
        # safe outcome; a crash or a silent acceptance of unsourced text is not.
        self.assertIn(result.get('status'), (None, 'deferred', 'failed'))
        if result.get('status') == 'failed':
            self.assertTrue(result.get('category'), result)
            self.assertEqual(self.ws.memory_rows(), [], 'a failed dream must not persist learning')

        # Invariant for every live outcome: whatever was persisted is traceable
        # to a cited user statement. Unsourced model text never becomes a fact.
        statements = {}
        with self.ws.db.connect(readonly=True) as conn:
            for row in conn.execute('SELECT evidence_id, observation_json FROM workspace_evidence'):
                statements[row['evidence_id']] = json.loads(
                    row['observation_json']).get('user_statement', '')
        for row in self.ws.memory_rows():
            details = json.loads(row['details_json'])
            self.assertEqual(details['authority'], 'runtime_sourced_user_claim')
            self.assertTrue(details['evidence'], details)
            for evidence_id in details['evidence']:
                self.assertIn(details['fact'].casefold(), statements.get(evidence_id, '').casefold(),
                              'persisted memory is not supported by its cited observation')

        # Behavioural benefit in the live path: an accepted memory is retrievable
        # by a later conversation through a fresh runtime over the same store.
        memories = self.ws.memory_rows()
        if memories:
            engine = make_engine(self.ws)
            answer = engine._normal_note_response('what do you know about the tea ceremony')
            self.assertIsNotNone(answer, 'live-learned memory was not retrievable')
            self.assertIn('two spoons of matcha', answer)


if __name__ == '__main__':
    unittest.main()
