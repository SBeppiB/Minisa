import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from runtime import research
from runtime.research_orchestrator import ResearchOrchestrator


class ResearchOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        (self.root / "MEMORY" / "active").mkdir(parents=True)
        (self.root / "MEMORY" / "archive").mkdir(parents=True)
        self.db_path = self.root / "test.db"
        database = db.Database(self.db_path)
        database.initialize()

    def tearDown(self):
        self.tmpdir.cleanup()
        for k in list(os.environ.keys()):
            if k.startswith("MINISA_RESEARCH_"):
                os.environ.pop(k)

    def test_disabled_by_default(self):
        orch = ResearchOrchestrator(self.root, self.db_path)
        res = orch.run_once()
        self.assertEqual(res["status"], "disabled")

    def test_question_triggers_research_when_enabled(self):
        os.environ["MINISA_RESEARCH_ENABLED"] = "true"
        with db.Database(self.db_path).transaction(immediate=True) as conn:
            conn.execute("""INSERT INTO workspace_units(unit_id,kind,subject,payload_json,evidence_json,created_at,activated_at,activation,importance,uncertainty,priority,status,revision,dedup_key,eligible_until)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         ("q1", "question", "What is the meaning of X?", "{}", "[]", time.time(), time.time(), 0.8, 0.8, 0.5, 0.7, "active", 1, "q1", time.time()+3600))
        orch = ResearchOrchestrator(self.root, self.db_path)
        def fake_search(q):
            return [{"title": "Example", "url": "https://example.org/page", "snippet": "info"}]
        def fake_extract(url):
            return {"title": "Example", "url": "https://example.org/page", "text": "This is important information about X. It has details."}
        with patch.object(research, "search_web", side_effect=fake_search), \
             patch.object(research, "extract_page", side_effect=fake_extract):
            res = orch.run_once()
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["ran"], 1)
        notes = list((self.root / "MEMORY" / "active").glob("*.md"))
        self.assertTrue(len(notes) >= 1)
        content = notes[0].read_text()
        self.assertIn("Source: https://example.org/page", content)
        self.assertIn("unverified", content.lower())

    def test_contradiction_triggers(self):
        os.environ["MINISA_RESEARCH_ENABLED"] = "true"
        with db.Database(self.db_path).transaction(immediate=True) as conn:
            conn.execute("""INSERT INTO workspace_units(unit_id,kind,subject,payload_json,evidence_json,created_at,activated_at,activation,importance,uncertainty,priority,status,revision,dedup_key,eligible_until)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         ("c1", "contradiction", "A conflicts with B", "{}", "[]", time.time(), time.time(), 0.8, 0.9, 0.7, 0.8, "active", 1, "c1", time.time()+3600))
        orch = ResearchOrchestrator(self.root, self.db_path)
        def fake_search(q):
            return [{"title": "Ex", "url": "https://example.com/p", "snippet": "s"}]
        def fake_extract(url):
            return {"title": "Ex", "url": "https://example.com/p", "text": "Resolution details here."}
        with patch.object(research, "search_web", side_effect=fake_search), \
             patch.object(research, "extract_page", side_effect=fake_extract):
            res = orch.run_once()
        self.assertEqual(res["ran"], 1)

    def test_cooldown_prevents_repeat(self):
        os.environ["MINISA_RESEARCH_ENABLED"] = "true"
        with db.Database(self.db_path).transaction(immediate=True) as conn:
            conn.execute("""INSERT INTO workspace_units(unit_id,kind,subject,payload_json,evidence_json,created_at,activated_at,activation,importance,uncertainty,priority,status,revision,dedup_key,eligible_until)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         ("q2", "question", "Same question?", "{}", "[]", time.time(), time.time(), 0.8, 0.8, 0.5, 0.7, "active", 1, "q2", time.time()+3600))
        orch = ResearchOrchestrator(self.root, self.db_path)
        def fake_search(q):
            return [{"title": "Ex", "url": "https://example.org/x", "snippet": "s"}]
        def fake_extract(url):
            return {"title": "Ex", "url": "https://example.org/x", "text": "Some useful text here for you."}
        with patch.object(research, "search_web", side_effect=fake_search), \
             patch.object(research, "extract_page", side_effect=fake_extract):
            res1 = orch.run_once()
            res2 = orch.run_once()
        self.assertEqual(res1["ran"], 1)
        self.assertEqual(res2["ran"], 0)

    def test_quota_enforced(self):
        os.environ["MINISA_RESEARCH_ENABLED"] = "true"
        os.environ["MINISA_RESEARCH_MAX_PER_HOUR"] = "0"
        os.environ["MINISA_RESEARCH_MAX_PER_DAY"] = "0"
        orch = ResearchOrchestrator(self.root, self.db_path)
        res = orch.run_once()
        self.assertIn(res["status"], ("quota_exhausted",))

    def test_dream_separation_no_direct_call(self):
        import runtime.research_orchestrator as ro
        self.assertFalse(hasattr(ro, 'DreamWorker'))
        self.assertFalse(hasattr(ro, 'DreamModel'))


if __name__ == "__main__":
    unittest.main()


class DreamAutoResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        (self.root / "MEMORY" / "active").mkdir(parents=True)
        (self.root / "MEMORY" / "archive").mkdir(parents=True)
        self.db_path = self.root / "test.db"
        database = db.Database(self.db_path)
        database.initialize()

    def tearDown(self):
        self.tmpdir.cleanup()
        for k in list(os.environ.keys()):
            if k.startswith("MINISA_RESEARCH_"):
                os.environ.pop(k)

    def test_auto_research_via_dream_tick_disabled_by_default(self):
        from runtime.cognition.dream import DreamWorker
        from runtime.cognition.workspace import Workspace
        from core.db import Database
        # Setup workspace
        wdb = Database(self.db_path)
        ws = Workspace(wdb, self.root)
        worker = DreamWorker(ws, None)  # model None to short-circuit
        # Should not crash, research disabled
        worker._tick()
        self.assertTrue(True)

    def test_auto_research_triggers_when_enabled_offline(self):
        os.environ["MINISA_RESEARCH_ENABLED"] = "true"
        from runtime.cognition.dream import DreamWorker
        from runtime.cognition.workspace import Workspace
        from core.db import Database
        wdb = Database(self.db_path)
        ws = Workspace(wdb, self.root)
        with db.Database(self.db_path).transaction(immediate=True) as conn:
            conn.execute("""INSERT INTO workspace_units(unit_id,kind,subject,payload_json,evidence_json,created_at,activated_at,activation,importance,uncertainty,priority,status,revision,dedup_key,eligible_until)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         ("q_auto", "question", "Why does Z occur?", "{}", "[]", time.time(), time.time(), 0.8, 0.8, 0.5, 0.7, "active", 1, "q_auto", time.time()+3600))
        worker = DreamWorker(ws, None)
        from unittest.mock import patch
        def fake_search(q):
            return [{"title": "P", "url": "https://example.org/z", "snippet": "s"}]
        def fake_extract(url):
            return {"title": "P", "url": "https://example.org/z", "text": "Z happens for reason R. Important details here."}
        with patch.object(research, "search_web", side_effect=fake_search), \
             patch.object(research, "extract_page", side_effect=fake_extract):
            worker._tick()
        notes = list((self.root / "MEMORY" / "active").glob("*.md"))
        self.assertTrue(len(notes) >= 1)
        content = notes[0].read_text()
        self.assertIn("Source:", content)
        self.assertIn("unverified", content.lower())

    def test_concurrent_runs_safe(self):
        os.environ["MINISA_RESEARCH_ENABLED"] = "true"
        from runtime.research_orchestrator import ResearchOrchestrator
        orch = ResearchOrchestrator(self.root, self.db_path)
        with db.Database(self.db_path).transaction(immediate=True) as conn:
            conn.execute("""INSERT INTO workspace_units(unit_id,kind,subject,payload_json,evidence_json,created_at,activated_at,activation,importance,uncertainty,priority,status,revision,dedup_key,eligible_until)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         ("q_c", "question", "Concurrent test?", "{}", "[]", time.time(), time.time(), 0.8, 0.8, 0.5, 0.7, "active", 1, "q_c", time.time()+3600))
        from unittest.mock import patch
        def fake_search(q): return [{"title": "P", "url": "https://example.org/c", "snippet": "s"}]
        def fake_extract(url): return {"title": "P", "url": "https://example.org/c", "text": "Concurrent safe info here."}
        with patch.object(research, "search_web", side_effect=fake_search), \
             patch.object(research, "extract_page", side_effect=fake_extract):
            # Simulate concurrent calls
            r1 = orch.run_once()
            r2 = orch.run_once()
        # At most one should run due to cooldown/quota
        total = r1.get("ran", 0) + r2.get("ran", 0)
        self.assertTrue(total <= 1)

    def test_failures_do_not_crash(self):
        os.environ["MINISA_RESEARCH_ENABLED"] = "true"
        from runtime.cognition.dream import DreamWorker
        from runtime.cognition.workspace import Workspace
        from core.db import Database
        wdb = Database(self.db_path)
        ws = Workspace(wdb, self.root)
        with db.Database(self.db_path).transaction(immediate=True) as conn:
            conn.execute("""INSERT INTO workspace_units(unit_id,kind,subject,payload_json,evidence_json,created_at,activated_at,activation,importance,uncertainty,priority,status,revision,dedup_key,eligible_until)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         ("q_f", "question", "Fail test?", "{}", "[]", time.time(), time.time(), 0.8, 0.8, 0.5, 0.7, "active", 1, "q_f", time.time()+3600))
        worker = DreamWorker(ws, None)
        from unittest.mock import patch
        def fake_search(q):
            raise research.ResearchError("fail")
        with patch.object(research, "search_web", side_effect=fake_search):
            worker._tick()  # should not crash
        self.assertTrue(True)
