"""Retrieval quality floor for durable sourced memories.

The learning cycle only pays off if a later conversation can find what was
learned. The measurement lives in ops/measure_retrieval_quality.py; this test
runs it and pins the floor, so a change that silently destroys recall fails
the suite instead of passing quietly.

Categories: exact, morphological (same words, other form), paraphrase (other
words, same meaning) and vague (almost nothing in common). The lexical matcher
cannot be expected to solve the last two without a semantic backend; the floor
below only protects the categories the runtime claims to serve.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
MEASURE = REPOSITORY / 'ops/measure_retrieval_quality.py'


class RetrievalQualityTests(unittest.TestCase):

    def report(self):
        with tempfile.TemporaryDirectory(prefix='minisa-retrieval-test-') as name:
            output = Path(name) / 'report.json'
            inherited = os.environ.get('PYTHONPATH', '')
            environment = dict(os.environ, PYTHONPATH=os.pathsep.join(
                part for part in (str(REPOSITORY), inherited) if part))
            completed = subprocess.run(
                [sys.executable, str(MEASURE), '--output', str(output)],
                capture_output=True, text=True, timeout=300, env=environment)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return json.loads(output.read_text())

    def test_exact_queries_are_always_retrieved_and_answered(self):
        report = self.report()
        exact = report['summary']['exact']
        self.assertEqual(exact['hits'], exact['total'], report['summary'])
        self.assertEqual(exact['answered'], exact['total'], report['summary'])
        self.assertEqual(exact['in_context'], exact['total'], report['summary'])

    def test_overall_retrieval_floor_is_kept(self):
        report = self.report()
        overall = report['summary']['overall']
        self.assertGreaterEqual(overall['hit_rate'], 0.5, report['summary'])
        self.assertEqual(overall['in_context'], overall['hits'], report['summary'])

    def test_every_measured_query_is_classified(self):
        report = self.report()
        for row in report['results']:
            self.assertIn(row['category'], ('exact', 'morphological', 'paraphrase', 'vague'))
            self.assertIn(row['hit'], (True, False))


if __name__ == '__main__':
    unittest.main()
