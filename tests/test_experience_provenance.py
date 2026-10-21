import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from core.db import Database
from runtime.cognition.turns import TurnStore
from runtime.cognition.workspace import Workspace
from runtime.server.http import MinisaHTTPServer
from runtime.server.application import MinisaApplication


class RecordingApplication:
    def __init__(self):
        self.calls = []
        self.config = SimpleNamespace(max_request_bytes=1024, qualification_route_enabled=True)

    def chat(self, message, *, provenance='user'):
        self.calls.append((message, provenance))
        return {'response': 'Recorded by fixture.'}

    def dream_status(self):
        return {'configured': True, 'status': 'idle', 'pending_jobs': 0,
                'unprocessed_evidence': 0, 'recent_failures': []}


class ExperienceProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='minisa-provenance-')
        self.root = Path(self.temp.name)
        (self.root / 'EXPERIENCE/turns').mkdir(parents=True)
        self.db = Database(self.root / 'workspace.db')
        self.db.initialize()
        self.workspace = Workspace(self.db, self.root)
        self.turns = TurnStore(self.root)
        self.turns.workspace = self.workspace

    def tearDown(self):
        self.temp.cleanup()

    def _finish(self, record):
        record.update(status='completed', stage='final', action={'action': 'respond', 'status': 'completed'})
        self.turns.publish(record)
        return self.turns.read(self.root / 'EXPERIENCE/turns' / (record['id'] + '.json'))

    def test_arithmetic_qualification_is_auditable_but_never_dream_evidence(self):
        record = self._finish(self.turns.begin('What is 19 + 23? Answer with the number only.', provenance='qualification'))
        self.assertEqual(record['provenance'], 'qualification')
        receipt = self.workspace.ingest_turn(record, self.root / 'EXPERIENCE/turns' / (record['id'] + '.json'))
        self.assertEqual(receipt, {'filtered': True, 'reason': 'non_user_provenance'})
        with self.db.connect(readonly=True) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM workspace_evidence').fetchone()[0], 0)
        self.assertEqual(self.turns.read(self.root / 'EXPERIENCE/turns' / (record['id'] + '.json'))['user_input'], record['user_input'])

    def test_ordinary_turn_remains_eligible_for_experience_indexing(self):
        record = self._finish(self.turns.begin('Please remember that the meeting is Friday.'))
        self.assertEqual(record['provenance'], 'user')
        receipt = self.workspace.ingest_turn(record, self.root / 'EXPERIENCE/turns' / (record['id'] + '.json'))
        self.assertNotIn('filtered', receipt)
        with self.db.connect(readonly=True) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM workspace_evidence').fetchone()[0], 1)

    def test_client_supplied_provenance_cannot_mark_public_chat_as_test(self):
        app = RecordingApplication()
        server = MinisaHTTPServer(('127.0.0.1', 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payload = json.dumps({'message': '19 + 23', 'provenance': 'qualification'}).encode()
            req = Request(f'http://127.0.0.1:{server.server_port}/v1/chat', data=payload,
                          headers={'Content-Type': 'application/json'})
            with urlopen(req, timeout=3) as response:
                self.assertEqual(json.load(response)['response'], 'Recorded by fixture.')
            self.assertEqual(app.calls, [('19 + 23', 'user')])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_loopback_test_route_assigns_qualification_provenance(self):
        app = RecordingApplication()
        server = MinisaHTTPServer(('127.0.0.1', 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payload = json.dumps({'message': '19 + 23'}).encode()
            req = Request(f'http://127.0.0.1:{server.server_port}/__test/chat', data=payload,
                          headers={'Content-Type': 'application/json'})
            with urlopen(req, timeout=3) as response:
                self.assertEqual(json.load(response)['response'], 'Recorded by fixture.')
            self.assertEqual(app.calls, [('19 + 23', 'qualification')])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_qualification_route_is_disabled_without_explicit_server_config(self):
        app = RecordingApplication()
        app.config.qualification_route_enabled = False
        server = MinisaHTTPServer(('127.0.0.1', 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payload = json.dumps({'message': 'synthetic qualification input'}).encode()
            req = Request(f'http://127.0.0.1:{server.server_port}/__test/chat', data=payload,
                          headers={'Content-Type': 'application/json'})
            with self.assertRaises(HTTPError) as caught:
                urlopen(req, timeout=3)
            self.assertEqual(caught.exception.code, 404)
            caught.exception.close()
            self.assertEqual(app.calls, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_dream_status_is_safe_and_reports_last_cycle_summary(self):
        result = {'records_considered': 7, 'accepted': [{'type': 'unit', 'subject': 'private text'}]}
        with self.db.transaction(immediate=True) as conn:
            conn.execute("INSERT INTO jobs(job_id,job_type,payload_json,status,created_at) VALUES(?,?,?,?,?)",
                         ('fixture-job', 'dream', '{}', 'completed', '2026-10-17T01:00:44+00:00'))
            conn.execute('INSERT INTO dream_results(job_id,source_revision,resulting_revision,result_json,created_at) VALUES(?,?,?,?,?)',
                         ('fixture-job', 4, 5, json.dumps(result), 1792198856.3425632))
        app = MinisaApplication.__new__(MinisaApplication)
        app.config = SimpleNamespace(dream_model='fixture-model')
        app.workspace = self.workspace
        summary = app.dream_status()
        self.assertEqual(summary['last_success']['source_revision'], 4)
        self.assertEqual(summary['last_success']['resulting_revision'], 5)
        self.assertEqual(summary['last_success']['records_considered'], 7)
        self.assertEqual(summary['last_success']['accepted_changes'], 1)
        self.assertNotIn('private text', json.dumps(summary))

    def test_dream_status_http_route_returns_safe_summary(self):
        app = RecordingApplication()
        server = MinisaHTTPServer(('127.0.0.1', 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f'http://127.0.0.1:{server.server_port}/v1/dream-status', timeout=3) as response:
                self.assertEqual(response.status, 200)
                payload = json.load(response)
            self.assertEqual(payload['status'], 'idle')
            self.assertEqual(payload['recent_failures'], [])
            self.assertNotIn('private', json.dumps(payload))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
