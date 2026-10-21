"""Isolated tests for disabled-by-default shadow recall evaluation.

Runs with stdlib unittest from the candidate root. Never touches production
memory files, the production database, or any external service. Universal
Memory end-to-end tests run only where the universal_memory package is
importable (for example inside the Universal Memory venv) and are skipped
elsewhere.
"""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from runtime.cognition.engine import CognitionEngine
from runtime.cognition.router import CognitiveRouter
from runtime.server.application import MinisaApplication
from runtime.server.config import ServerConfig
from runtime.services.memory import LocalMemoryService
from runtime.services.shadow_recall import (
    ShadowMemoryService,
    ShadowRecallEvaluator,
    attach_shadow_recall,
)

CANDIDATE_ROOT = Path(__file__).resolve().parents[1]

try:
    import universal_memory  # noqa: F401

    UNIVERSAL_MEMORY_AVAILABLE = True
except ImportError:
    UNIVERSAL_MEMORY_AVAILABLE = False

SAMPLE_HIT = "[umem:11111111-2222-3333-4444-555555555555] (score=0.90) the vault code is 4242"


class EmbeddingProbeError(Exception):
    """Stand-in for the local semantic path failing (e.g. offline embeddings)."""


class FakeLocalMemory:
    """Stand-in for LocalMemoryService with deterministic recall output."""

    def __init__(self, results=None):
        self.results = list(results if results is not None else ["LOCAL-1", "LOCAL-2"])
        self.recall_calls = []

    def recall(self, query, limit, semantic=False):
        self.recall_calls.append((query, limit, semantic))
        return list(self.results)

    def find_by_title(self, title):
        return None

    def search_titles(self, query, limit=3):
        return []

    def linked_memories(self, reference, limit=3):
        return []

    def read_memory(self, reference):
        return "unused"

    def memory_exists(self, fact):
        return False

    def next_memory_id(self):
        return "memory_001"

    def create_memory(self, memory_id, title, content):
        return {"status": "committed"}

    def find_forget_candidates(self, query):
        return []

    def archive_memory(self, reference, query):
        return {}

    def status(self):
        return {"backend": "fake", "available": True, "active_memories": len(self.results)}

    def inspect(self):
        return {"backend": "fake", "records": list(self.results)}


class FakeShadowAdapter:
    def __init__(self, results=None, error=None):
        self.results = list(results if results is not None else [SAMPLE_HIT])
        self.error = error
        self.calls = 0

    def recall(self, query, limit, semantic=False):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.results)


def make_evaluator(adapter, log_path, max_entries=200):
    return ShadowRecallEvaluator(adapter, log_path, max_entries=max_entries)


def log_records(log_path):
    text = Path(log_path).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def make_root(base):
    root = Path(base) / "minisa-root"
    for directory in ("CORE", "MEMORY", "STATE"):
        (root / directory).mkdir(parents=True)
    return root


def engine_with_memory(memory):
    engine = CognitionEngine.__new__(CognitionEngine)
    engine.memory = memory
    engine.router = CognitiveRouter()
    return engine


class DefaultOffTests(unittest.TestCase):
    def test_config_defaults_disable_shadow_recall(self):
        config = ServerConfig(root=CANDIDATE_ROOT)
        self.assertFalse(config.shadow_recall_enabled)
        self.assertEqual(config.shadow_recall_data, "")
        self.assertEqual(config.shadow_recall_log, "")

    def test_application_default_has_no_shadow(self):
        app = MinisaApplication(ServerConfig(root=CANDIDATE_ROOT))
        self.assertIsNone(app.shadow_evaluator)
        self.assertIsInstance(app.memory, LocalMemoryService)
        self.assertNotIsInstance(app.memory, ShadowMemoryService)

    def test_load_env_mapping_disabled_by_default_and_enabled_explicitly(self):
        with tempfile.TemporaryDirectory() as base:
            root = make_root(base)
            base_env = {
                "MINISA_ROOT": str(root),
                "MINISA_HOST": "127.0.0.1",
                "MINISA_OLLAMA_URL": "http://127.0.0.1:11434",
            }
            config = ServerConfig.load(environ=dict(base_env))
            self.assertFalse(config.shadow_recall_enabled)
            enabled = ServerConfig.load(
                environ={
                    **base_env,
                    "MINISA_SHADOW_RECALL": "1",
                    "MINISA_SHADOW_RECALL_DATA": str(Path(base) / "umem"),
                    "MINISA_SHADOW_RECALL_LOG": str(Path(base) / "eval.jsonl"),
                }
            )
            self.assertTrue(enabled.shadow_recall_enabled)

    def test_enabled_config_requires_absolute_paths(self):
        with tempfile.TemporaryDirectory() as base:
            root = make_root(base)
            enabled = replace(ServerConfig(root=root), shadow_recall_enabled=True)
            with self.assertRaises(ValueError):
                enabled.validate()
            relative = replace(
                ServerConfig(root=root),
                shadow_recall_enabled=True,
                shadow_recall_data="relative-data",
                shadow_recall_log=str(Path(base) / "eval.jsonl"),
            )
            with self.assertRaises(ValueError):
                relative.validate()


class UnchangedRecallTests(unittest.TestCase):
    def test_wrapper_returns_inner_recall_unchanged(self):
        with tempfile.TemporaryDirectory() as base:
            inner = FakeLocalMemory(["LOCAL-A", "LOCAL-B"])
            adapter = FakeShadowAdapter(["[umem:dead: (score=0.10) shadow-only"])
            wrapper = ShadowMemoryService(inner, make_evaluator(adapter, Path(base) / "log.jsonl"))
            result = wrapper.recall("question", 5, semantic=True)
            self.assertEqual(result, ["LOCAL-A", "LOCAL-B"])
            self.assertEqual(inner.recall_calls, [("question", 5, True)])
            self.assertEqual(adapter.calls, 1)

    def test_cognition_recall_never_contains_shadow_hits(self):
        with tempfile.TemporaryDirectory() as base:
            inner = FakeLocalMemory(["LOCAL-MEMORY-ENTRY"])
            adapter = FakeShadowAdapter()
            wrapper = ShadowMemoryService(inner, make_evaluator(adapter, Path(base) / "log.jsonl"))
            result = engine_with_memory(wrapper).recall("what do you know", CognitiveRouter.NORMAL)
            self.assertEqual(result["memories"], ["LOCAL-MEMORY-ENTRY"])
            serialized = json.dumps(result)
            self.assertNotIn("umem:", serialized)
            self.assertNotIn("4242", serialized)
            self.assertNotIn("shadow", serialized.lower())
            self.assertEqual(result["memory_status"]["retrieval"], "matched")


class BoundedLoggingTests(unittest.TestCase):
    def test_log_never_exceeds_max_entries(self):
        with tempfile.TemporaryDirectory() as base:
            log_path = Path(base) / "eval.jsonl"
            adapter = FakeShadowAdapter()
            wrapper = ShadowMemoryService(FakeLocalMemory(), make_evaluator(adapter, log_path, max_entries=8))
            for index in range(30):
                wrapper.recall(f"query {index}", 5)
            lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertLessEqual(len(lines), 8)
            self.assertTrue(lines)
            last = json.loads(lines[-1])
            self.assertEqual(last["hit_count"], 1)

    def test_log_contains_no_raw_query_or_content(self):
        with tempfile.TemporaryDirectory() as base:
            log_path = Path(base) / "eval.jsonl"
            adapter = FakeShadowAdapter()
            wrapper = ShadowMemoryService(FakeLocalMemory(), make_evaluator(adapter, log_path))
            wrapper.recall("my password is hunter2-secret", 5)
            text = log_path.read_text(encoding="utf-8")
            self.assertNotIn("hunter2-secret", text)
            self.assertNotIn("4242", text)
            record = json.loads(text.splitlines()[-1])
            self.assertIn("query_sha256", record)
            hit = record["hits"][0]
            self.assertEqual(hit["id"], "11111111-2222-3333-4444-555555555555")
            self.assertEqual(hit["score"], 0.90)
            self.assertNotIn("content", hit)


class FailSoftTests(unittest.TestCase):
    def test_shadow_adapter_error_never_affects_normal_recall(self):
        with tempfile.TemporaryDirectory() as base:
            log_path = Path(base) / "eval.jsonl"
            adapter = FakeShadowAdapter(error=RuntimeError("index exploded"))
            inner = FakeLocalMemory(["SURVIVES"])
            wrapper = ShadowMemoryService(inner, make_evaluator(adapter, log_path))
            result = wrapper.recall("anything", 5)
            self.assertEqual(result, ["SURVIVES"])
            record = log_records(log_path)[-1]
            self.assertIn("RuntimeError", record["error"])

    def test_inner_recall_failure_is_still_shadowed_and_reraises(self):
        class ExplodingInner(FakeLocalMemory):
            def recall(self, query, limit, semantic=False):
                raise EmbeddingProbeError("embedding provider unavailable")

        with tempfile.TemporaryDirectory() as base:
            log_path = Path(base) / "eval.jsonl"
            adapter = FakeShadowAdapter(["[umem:aaaa] (score=0.80) shadow-only hit"])
            wrapper = ShadowMemoryService(ExplodingInner(), make_evaluator(adapter, log_path))
            with self.assertRaises(EmbeddingProbeError):
                wrapper.recall("query during outage", 5, semantic=True)
            self.assertEqual(adapter.calls, 1)
            records = log_records(log_path)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["hit_count"], 1)
            self.assertEqual(records[0]["semantic"], True)
            self.assertNotIn("error", records[0])

    def test_error_text_is_truncated_in_log(self):
        with tempfile.TemporaryDirectory() as base:
            log_path = Path(base) / "eval.jsonl"
            adapter = FakeShadowAdapter(error=RuntimeError("x" * 10_000))
            evaluator = make_evaluator(adapter, log_path)
            evaluator.evaluate("q", 5)
            record = log_records(log_path)[-1]
            self.assertLessEqual(len(record["error"]), 200)

    def test_application_setup_failure_keeps_normal_memory(self):
        with tempfile.TemporaryDirectory() as base:
            root = make_root(base)
            config = replace(
                ServerConfig(root=root),
                shadow_recall_enabled=True,
                shadow_recall_data=str(Path(base) / "umem-data"),
                shadow_recall_log=str(Path(base) / "eval.jsonl"),
            )
            app = MinisaApplication(config)
            if UNIVERSAL_MEMORY_AVAILABLE:
                self.assertIsInstance(app.memory, ShadowMemoryService)
                self.assertIsNotNone(app.shadow_evaluator)
                self.assertIsInstance(app.memory._inner, LocalMemoryService)
                app.shadow_evaluator._adapter.close()
            else:
                self.assertIsInstance(app.memory, LocalMemoryService)
                self.assertIsNone(app.shadow_evaluator)


@unittest.skipUnless(UNIVERSAL_MEMORY_AVAILABLE, "universal_memory package not installed")
class RealUniversalMemoryEndToEndTests(unittest.TestCase):
    def test_real_adapter_recall_is_shadowed_and_returned_unchanged(self):
        with tempfile.TemporaryDirectory() as base:
            log_path = Path(base) / "eval.jsonl"
            adapter_holder = attach_shadow_recall(
                FakeLocalMemory(["LOCAL-ONLY"]),
                replace(
                    ServerConfig(root=CANDIDATE_ROOT),
                    shadow_recall_enabled=True,
                    shadow_recall_data=str(Path(base) / "umem-data"),
                    shadow_recall_log=str(log_path),
                ),
            )
            wrapper, evaluator = adapter_holder
            try:
                wrapper._inner  # FakeLocalMemory beneath the wrapper
                adapter = evaluator._adapter
                adapter._engine.remember("Alex purchased a red bicycle")
                result = wrapper.recall("Alex purchased a red bicycle", 5, semantic=True)
                self.assertEqual(result, ["LOCAL-ONLY"])
                record = log_records(log_path)[-1]
                self.assertIsNone(record.get("error"))
                self.assertGreaterEqual(record["hit_count"], 1)
                hit = record["hits"][0]
                self.assertTrue(hit["id"])
                self.assertNotIn("content", hit)
            finally:
                evaluator._adapter.close()

    def test_low_score_noise_is_filtered_by_relevance_floor(self):
        from runtime.services.universal_memory_readonly import MIN_RELEVANCE_SCORE

        with tempfile.TemporaryDirectory() as base:
            adapter_holder = attach_shadow_recall(
                FakeLocalMemory(["LOCAL-ONLY"]),
                replace(
                    ServerConfig(root=CANDIDATE_ROOT),
                    shadow_recall_enabled=True,
                    shadow_recall_data=str(Path(base) / "umem-data"),
                    shadow_recall_log=str(Path(base) / "eval.jsonl"),
                ),
            )
            wrapper, evaluator = adapter_holder
            adapter = evaluator._adapter
            try:
                adapter._engine.remember("User purchased a red bicycle")
                adapter._engine.remember("Weekly team meeting moved to Tuesdays")
                # Unrelated query: baseline lexical noise must not surface.
                hits = adapter.recall_detailed("quantum chromodynamics lecture", 5)
                for hit in hits:
                    self.assertGreaterEqual(hit["score"], MIN_RELEVANCE_SCORE)
                scores = [hit["score"] for hit in adapter.recall_detailed("red bicycle", 5)]
                self.assertTrue(scores)
                self.assertEqual(scores, sorted(scores, reverse=True))
            finally:
                evaluator._adapter.close()


if __name__ == "__main__":
    unittest.main()
