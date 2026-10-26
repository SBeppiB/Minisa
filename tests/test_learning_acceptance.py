"""End-to-end acceptance for the learning path, at conversation level.

One continuous chain, using the production code paths only:

    conversation turn -> durable turn record -> workspace index job
    -> dream evidence -> guarded admission -> durable memory
    -> later conversation answer (a restarted runtime over the same store)

The dream model is scripted so the software contract is deterministic; the
admission guard, the persistence layer, the workspace indexer and the answer
path are the real ones.

Nothing here mutates the live workspace, the production database, CORE,
beliefs, personality, self-model or state.
"""
import unittest

from runtime.cognition.dream import DreamWorker
from runtime.server.application import MinisaApplication
from runtime.server.config import ServerConfig

from tests.test_learning_cycle import (
    CLAIM,
    UNSUPPORTED_CLAIM,
    LearningWorkspace,
    ScriptedModel,
    make_engine,
)


class LearningAcceptanceTests(unittest.TestCase):
    """Acceptance: a real conversation must make the runtime measurably better."""

    def setUp(self):
        self.ws = LearningWorkspace()
        self.addCleanup(self.ws.close)
        self.engine = make_engine(self.ws)
        # The turn store hands final turns to the workspace for indexing.
        self.engine.turns.workspace = self.ws.workspace

    def _application(self):
        """The real indexing path, over the same store as the fixture."""
        config = ServerConfig(root=self.ws.root,
                              workspace_database=str(self.ws.root / 'data/minisa.db'),
                              dream_model='')
        application = MinisaApplication(config, memory=self.engine.memory,
                                        engine=self.engine)
        application.workspace = self.ws.workspace
        return application

    def _converse(self, message):
        result = self.engine.process(message)
        self.assertEqual(result.get('turn_status'), 'completed', result)
        return result

    def test_remembered_fact_is_answered_in_a_later_conversation(self):
        # 1. A normal conversation turn states the fact explicitly.
        stated = self._converse(f'remember this: {CLAIM}')
        turn_id = stated['turn_id']

        # 2. The real indexer turns the final turn record into dream evidence.
        self._application()._index_turns()
        self.assertIn(turn_id, self.ws.evidence_ids())

        # 3. The dream worker proposes the fact, citing that observation.
        scripted = {'changes': [{
            'type': 'memory',
            'title': 'Tea ceremony preparation',
            'fact': CLAIM,
            'evidence': [turn_id]}]}
        result = DreamWorker(self.ws.workspace, ScriptedModel(scripted)).tick()
        self.assertNotIn(result.get('status'), ('failed', 'deferred'), result)
        self.assertEqual(len(result['accepted']), 1, result)
        self.assertEqual(len(self.ws.memory_rows()), 1)

        # 4. A restarted runtime answers a later question from the durable memory.
        restarted = make_engine(self.ws)
        answer = restarted.process('what do you know about the tea ceremony')
        self.assertIn('two spoons of matcha', answer['response'], answer)
        self.assertEqual(answer['turn_status'], 'completed', answer)

    def test_invented_fact_is_never_persisted_or_answered(self):
        stated = self._converse(f'remember this: {CLAIM}')
        turn_id = stated['turn_id']
        self._application()._index_turns()

        # The dream invents a detail that the cited observation does not contain.
        scripted = {'changes': [{
            'type': 'memory',
            'title': 'Tea ceremony preparation',
            'fact': UNSUPPORTED_CLAIM,
            'evidence': [turn_id]}]}
        result = DreamWorker(self.ws.workspace, ScriptedModel(scripted)).tick()
        self.assertEqual(result.get('status'), 'failed', result)
        self.assertEqual(result.get('category'), 'invalid_output', result)
        self.assertEqual(self.ws.memory_rows(), [])

        # A later conversation must not contain the invented detail either.
        restarted = make_engine(self.ws)
        answer = restarted.process('what do you know about the tea ceremony')
        self.assertNotIn('40 degrees', answer['response'], answer)

    def test_second_conversation_turn_becomes_evidence_without_dream(self):
        first = self._converse('remember this: ' + CLAIM)
        second = self._converse('we should also talk about the tea ceremony')
        self._application()._index_turns()
        ids = self.ws.evidence_ids()
        self.assertIn(first['turn_id'], ids)
        self.assertIn(second['turn_id'], ids)


if __name__ == '__main__':
    unittest.main()
