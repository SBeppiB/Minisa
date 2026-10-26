"""Recall correctness and result isolation tests (hardening area B).

Defects covered:
- one corrupt/binary note must not fail the whole keyword recall or inspect;
- malformed queries (None, non-str, empty) and malformed limits must yield
  bounded empty/default answers, never exceptions, on every backend;
- non-finite similarity scores (NaN/inf) and negative scores must not pass
  the Universal Memory relevance floor;
- recall results must be deterministic (score then id ordering), deduplicated
  by memory id, and fail-soft when backend hits are malformed or the engine
  raises.
"""

import math
import tempfile
import unittest
from pathlib import Path

from runtime.services.memory import LocalMemoryService, bounded_limit

try:
    import universal_memory  # noqa: F401

    UNIVERSAL_MEMORY_AVAILABLE = True
except ImportError:
    UNIVERSAL_MEMORY_AVAILABLE = False

PROJECTION = (
    "# {title}\n\nID: {id}\nType: fact\nSubject: user\nSource: console\n"
    "Status: active\nConfidence: 0.9\nCreated: 2026-10-18\n\n## Fact\n\n{fact}\n"
)


def make_store(base: str) -> Path:
    root = Path(base) / "root"
    active = root / "MEMORY" / "active"
    for directory in ("CORE", "MEMORY/archive", "STATE"):
        (root / directory).mkdir(parents=True)
    active.mkdir(parents=True)
    return root


def write_note(root: Path, name: str, title: str, memory_id: str, fact: str) -> Path:
    path = root / "MEMORY" / "active" / name
    path.write_text(
        PROJECTION.format(title=title, id=memory_id, fact=fact), encoding="utf-8"
    )
    return path


class KeywordRecallResilienceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_store(self._tmp.name)
        write_note(self.root, "memory_001 - Vault.md", "Vault", "memory_001", "The vault code is 4242.")
        write_note(self.root, "memory_002 - Bike.md", "Bike", "memory_002", "User purchased a red bicycle.")
        self.service = LocalMemoryService(self.root, ollama_url=None)

    def test_corrupt_binary_note_does_not_kill_recall(self):
        (self.root / "MEMORY" / "active" / "memory_003 - Corrupt.md").write_bytes(
            b"\xff\xfe\x00garbage\x80\x81"
        )
        results = self.service.recall("vault code", 5, semantic=False)
        self.assertEqual(len(results), 1)
        self.assertIn("4242", results[0])

    def test_corrupt_note_does_not_kill_inspect(self):
        (self.root / "MEMORY" / "active" / "memory_003 - Corrupt.md").write_bytes(b"\x80\x81")
        records = self.service.inspect()["records"]
        self.assertEqual(len(records), 2)

    def test_unreadable_file_does_not_kill_recall(self):
        blocked = self.root / "MEMORY" / "active" / "memory_003 - Blocked.md"
        blocked.write_text("# Blocked\n\n## Fact\n\nirrelevant\n", encoding="utf-8")
        blocked.chmod(0o000)
        try:
            results = self.service.recall("vault code", 5, semantic=False)
            self.assertEqual(len(results), 1)
        finally:
            blocked.chmod(0o644)

    def test_semantic_retriever_skips_corrupt_note(self):
        class FakeEmbeddings:
            host = "fake"
            model = "fake-model"

            def embed(self, text):
                return [1.0, 0.5, 0.25]

            def cache_identity(self):
                return "fake-1"

        from runtime.memory.semantic_retriever import SemanticMemoryRetriever

        (self.root / "MEMORY" / "active" / "memory_003 - Corrupt.md").write_bytes(b"\xff\xfe")
        retriever = SemanticMemoryRetriever(self.service.manager, embeddings=FakeEmbeddings())
        results = retriever.search("vault code", limit=5, threshold=0.0)
        memory_ids = [r["memory_id"] for r in results]
        self.assertNotIn("memory_003 - Corrupt", memory_ids)
        self.assertEqual(len(results), 2)


class MalformedQueryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_store(self._tmp.name)
        write_note(self.root, "memory_001 - A.md", "A", "memory_001", "The vault code is 4242.")
        self.service = LocalMemoryService(self.root, ollama_url=None)

    def test_local_recall_never_raises_on_malformed_query(self):
        for query in (None, 123, ["x"], {"q": 1}, "", "   ", "\n\t"):
            with self.subTest(query=query):
                self.assertEqual(self.service.recall(query, 5, semantic=False), [])

    def test_local_recall_never_raises_on_malformed_limit(self):
        for limit in (None, "x", -3, 0, 2.7, object()):
            with self.subTest(limit=limit):
                result = self.service.recall("vault", limit, semantic=False)
                self.assertIsInstance(result, list)

    def test_negative_limit_returns_nothing_not_wrong_slice(self):
        # Pre-fix, [: -3] silently dropped the last three scored hits.
        self.assertEqual(self.service.recall("vault", -3, semantic=False), [])

    def test_bounded_limit_helper(self):
        self.assertEqual(bounded_limit("5"), 5)
        self.assertEqual(bounded_limit(None), 5)
        self.assertEqual(bounded_limit("x"), 5)
        self.assertEqual(bounded_limit(-10), 0)
        self.assertEqual(bounded_limit(3.9), 3)
        self.assertEqual(bounded_limit(True), 1)


class SemanticOrderingTests(unittest.TestCase):
    def test_ties_ordered_by_memory_id_not_directory_order(self):
        class FakeEmbeddings:
            host = "fake"
            model = "fake-model"

            def embed(self, text):
                # Identical vectors for every input -> all scores tie.
                return [1.0, 1.0, 1.0]

            def cache_identity(self):
                return "fake-1"

        from runtime.memory.semantic_retriever import SemanticMemoryRetriever

        with tempfile.TemporaryDirectory() as base:
            root = make_store(base)
            write_note(root, "memory_002 - B.md", "B", "memory_002", "beta fact about bikes")
            write_note(root, "memory_001 - A.md", "A", "memory_001", "alpha fact about vaults")
            service = LocalMemoryService(root, ollama_url=None)
            retriever = SemanticMemoryRetriever(service.manager, embeddings=FakeEmbeddings())
            first = [r["memory_id"] for r in retriever.search("anything", limit=5, threshold=0.0)]
            second = [r["memory_id"] for r in retriever.search("anything", limit=5, threshold=0.0)]
            self.assertEqual(first, second)
            self.assertEqual(first, sorted(first))


class UniversalAdapterContractTests(unittest.TestCase):
    """Defect probes for the Universal Memory adapter contract."""

    @classmethod
    def setUpClass(cls):
        if not UNIVERSAL_MEMORY_AVAILABLE:
            raise unittest.SkipTest("universal_memory package not installed")
        cls._tmp = tempfile.TemporaryDirectory()
        from runtime.services.universal_memory_readonly import (
            UniversalMemoryReadOnlyAdapter,
        )

        cls.adapter = UniversalMemoryReadOnlyAdapter(Path(cls._tmp.name) / "data")

    @classmethod
    def tearDownClass(cls):
        if UNIVERSAL_MEMORY_AVAILABLE:
            cls.adapter.close()
            cls._tmp.cleanup()

    @staticmethod
    def fake_engine(hits, raise_error=None):
        class Engine:
            def recall(self, query, top_k):
                if raise_error is not None:
                    raise raise_error
                return hits

            def close(self):
                pass

            def stats(self):
                return {"tiers": {"ACTIVE": 1}, "total": 1}

        return Engine()

    @staticmethod
    def hit(score, memory_id, content="content body"):
        return type(
            "Hit",
            (),
            {"score": score, "memory": type("M", (), {"id": memory_id, "content": content})()},
        )()

    def test_nonfinite_and_negative_scores_never_pass_floor(self):
        self.adapter._engine = self.fake_engine([
            self.hit(float("nan"), "nan-id"),
            self.hit(float("inf"), "inf-id"),
            self.hit(-9.0, "neg-id"),
            self.hit(float("0.9"), "good-id"),
        ])
        hits = self.adapter.recall_detailed("q", 5)
        ids = [h["id"] for h in hits]
        self.assertEqual(ids, ["good-id"])
        for hit in hits:
            self.assertTrue(math.isfinite(hit["score"]))

    def test_limit_is_bounded_never_raises(self):
        self.adapter._engine = self.fake_engine([self.hit(0.9, "id0")])
        for limit in (None, "x", -3, 0, 100, 2.7, object()):
            with self.subTest(limit=limit):
                result = self.adapter.recall_detailed("q", limit)
                self.assertIsInstance(result, list)

    def test_limit_above_max_capped_at_max(self):
        from runtime.services.universal_memory_readonly import MAX_LIMIT

        self.adapter._engine = self.fake_engine(
            [self.hit(0.9, f"id{i}") for i in range(30)]
        )
        self.assertEqual(len(self.adapter.recall_detailed("q", 1000)), MAX_LIMIT)

    def test_dedup_by_id_and_deterministic_tie_break(self):
        self.adapter._engine = self.fake_engine([
            self.hit(0.5, "b-id"),
            self.hit(0.5, "a-id"),
            self.hit(0.5, "b-id"),
            self.hit(0.9, "a-id"),
        ])
        first = [h["id"] for h in self.adapter.recall_detailed("q", 10)]
        self.adapter._engine = self.fake_engine([
            self.hit(0.5, "b-id"),
            self.hit(0.5, "a-id"),
            self.hit(0.5, "b-id"),
            self.hit(0.9, "a-id"),
        ])
        second = [h["id"] for h in self.adapter.recall_detailed("q", 10)]
        self.assertEqual(first, ["a-id", "b-id"])
        self.assertEqual(first, second)

    def test_malformed_hits_dropped_not_raised(self):
        missing_attrs = type("H", (), {"score": 0.8})()
        empty_id = type(
            "H", (), {"score": 0.8, "memory": type("M", (), {"id": "", "content": "x"})()}
        )()
        self.adapter._engine = self.fake_engine([
            self.hit(0.9, "ok-id"),
            missing_attrs,
            empty_id,
            "not-a-hit",
        ])
        hits = self.adapter.recall_detailed("q", 5)
        self.assertEqual([h["id"] for h in hits], ["ok-id"])
        self.assertIsNone(self.adapter.last_recall_error)

    def test_engine_failure_returns_empty_with_error(self):
        self.adapter._engine = self.fake_engine([], raise_error=RuntimeError("boom"))
        self.assertEqual(self.adapter.recall_detailed("q", 5), [])
        self.assertIn("RuntimeError", self.adapter.last_recall_error)
        self.assertEqual(self.adapter.recall("q", 5), [])

    def test_malformed_query_never_raises(self):
        self.adapter._engine = self.fake_engine([self.hit(0.9, "id0")])
        for query in (None, 123, ["x"], "", "   "):
            with self.subTest(query=query):
                self.assertEqual(self.adapter.recall_detailed(query, 5), [])

    def test_close_is_guarded(self):
        original = self.adapter._engine
        self.adapter._engine = type("NoClose", (), {})()
        try:
            self.adapter.close()  # must not raise
        finally:
            self.adapter._engine = original


if __name__ == "__main__":
    unittest.main()
