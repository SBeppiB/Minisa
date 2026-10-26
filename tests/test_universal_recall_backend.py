"""Tests for the explicit Universal Memory recall backend (opt-in mode).

Mode selection, result isolation, fallback behavior, and cognition-engine
compatibility. Universal Memory end-to-end construction runs only where the
universal_memory package is importable; setup-failure fallback is asserted
in both environments.
"""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from runtime.cognition.engine import CognitionEngine
from runtime.cognition.router import CognitiveRouter
from runtime.server.config import ServerConfig
from runtime.services.memory import LocalMemoryService
from runtime.services.universal_recall import UniversalRecallService

CANDIDATE_ROOT = Path(__file__).resolve().parents[1]

try:
    import universal_memory  # noqa: F401

    UNIVERSAL_MEMORY_AVAILABLE = True
except ImportError:
    UNIVERSAL_MEMORY_AVAILABLE = False


def make_root(base):
    root = Path(base) / "minisa-root"
    for directory in ("CORE", "MEMORY/active", "MEMORY/archive", "STATE"):
        (root / directory).mkdir(parents=True)
    return root


def base_env(root):
    return {
        "MINISA_ROOT": str(root),
        "MINISA_HOST": "127.0.0.1",
        "MINISA_OLLAMA_URL": "http://127.0.0.1:11434",
    }


class FakeInnerMemory:
    def __init__(self, results=None):
        self.results = list(results if results is not None else ["LOCAL-1"])
        self.recall_calls = []

    def recall(self, query, limit, semantic=False):
        self.recall_calls.append((query, limit, semantic))
        return list(self.results)

    def status(self):
        return {"backend": "fake", "available": True, "active_memories": len(self.results)}

    def inspect(self):
        return {"backend": "fake", "records": list(self.results)}

    def next_memory_id(self):
        return "memory_001"

    def create_memory(self, memory_id, title, content):
        return {"status": "committed", "committed": True}


class FakeAdapter:
    def __init__(self, results=None, error=None):
        self.results = list(results if results is not None else ["[umem:x] (score=0.90) um"])
        self.error = error
        self.calls = 0

    def recall(self, query, limit, semantic=False):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.results)

    def status(self):
        return {"backend": "universal-memory-readonly", "available": True}


class ModeSelectionTests(unittest.TestCase):
    def test_default_is_local_backend(self):
        with tempfile.TemporaryDirectory() as base:
            env = base_env(make_root(base))
            config = ServerConfig.load(environ=dict(env))
            self.assertEqual(config.recall_backend, "local")
            self.assertEqual(config.universal_recall_data, "")

    def test_env_maps_universal_backend_and_data_path(self):
        with tempfile.TemporaryDirectory() as base:
            env = base_env(make_root(base))
            config = ServerConfig.load(environ={
                **env,
                "MINISA_RECALL_BACKEND": "Universal",
                "MINISA_UNIVERSAL_RECALL_DATA": str(Path(base) / "umem-data"),
            })
            self.assertEqual(config.recall_backend, "universal")

    def test_invalid_backend_value_rejected(self):
        with tempfile.TemporaryDirectory() as base:
            env = base_env(make_root(base))
            with self.assertRaises(ValueError):
                ServerConfig.load(environ={**env, "MINISA_RECALL_BACKEND": "semantic"})

    def test_universal_backend_requires_absolute_data_path(self):
        with tempfile.TemporaryDirectory() as base:
            root = make_root(base)
            missing = replace(ServerConfig(root=root), recall_backend="universal")
            with self.assertRaises(ValueError):
                missing.validate()
            relative = replace(
                ServerConfig(root=root),
                recall_backend="universal",
                universal_recall_data="relative-data",
            )
            with self.assertRaises(ValueError):
                relative.validate()
            absolute = replace(
                ServerConfig(root=root),
                recall_backend="universal",
                universal_recall_data=str(Path(base) / "umem-data"),
            )
            absolute.validate()


class FallbackBehaviorTests(unittest.TestCase):
    def service(self, adapter, inner=None):
        return UniversalRecallService(inner or FakeInnerMemory(), adapter)

    def test_universal_hits_are_returned(self):
        service = self.service(FakeAdapter(["[umem:a] (score=0.90) um-hit"]))
        self.assertEqual(service.recall("q", 5), ["[umem:a] (score=0.90) um-hit"])

    def test_empty_universal_falls_back_to_local(self):
        inner = FakeInnerMemory(["LOCAL-HIT"])
        service = self.service(FakeAdapter([]), inner)
        self.assertEqual(service.recall("q", 5), ["LOCAL-HIT"])
        self.assertEqual(inner.recall_calls, [("q", 5, False)])

    def test_universal_failure_falls_back_to_local_without_raising(self):
        inner = FakeInnerMemory(["LOCAL-SURVIVES"])
        service = self.service(FakeAdapter(error=RuntimeError("um exploded")), inner)
        self.assertEqual(service.recall("q", 5), ["LOCAL-SURVIVES"])

    def test_mutations_delegate_to_inner_not_adapter(self):
        inner = FakeInnerMemory()
        service = self.service(FakeAdapter(), inner)
        self.assertEqual(service.next_memory_id(), "memory_001")
        self.assertEqual(service.create_memory("memory_001", "T", "c")["status"], "committed")

    def test_status_declares_backend_and_adapter_health(self):
        service = self.service(FakeAdapter())
        status = service.status()
        self.assertEqual(status["recall_backend"], "universal")
        self.assertEqual(status["backend"], "fake")  # inner store remains authoritative
        self.assertEqual(status["universal_recall"]["available"], True)


class CognitionEngineCompatibilityTests(unittest.TestCase):
    def engine_over(self, memory):
        engine = CognitionEngine.__new__(CognitionEngine)
        engine.memory = memory
        engine.router = CognitiveRouter()
        return engine

    def test_engine_recall_returns_universal_hits_isolated_from_shadow(self):
        service = UniversalRecallService(
            FakeInnerMemory(["LOCAL-1"]),
            FakeAdapter(["[umem:aaa] (score=0.90) um-content"]),
        )
        result = self.engine_over(service).recall("a question", CognitiveRouter.NORMAL)
        self.assertEqual(result["memories"], ["[umem:aaa] (score=0.90) um-content"])
        self.assertEqual(result["memory_status"]["retrieval"], "matched")
        self.assertEqual(result["memory_status"]["recall_backend"], "universal")

    def test_engine_recall_survives_universal_failure_via_local(self):
        service = UniversalRecallService(
            FakeInnerMemory(["LOCAL-BACKUP"]),
            FakeAdapter(error=RuntimeError("um down")),
        )
        result = self.engine_over(service).recall("a question", CognitiveRouter.NORMAL)
        self.assertEqual(result["memories"], ["LOCAL-BACKUP"])
        self.assertEqual(result["memory_status"]["retrieval"], "matched")


class ApplicationWiringTests(unittest.TestCase):
    def test_application_with_universal_backend(self):
        from runtime.server.application import MinisaApplication

        with tempfile.TemporaryDirectory() as base:
            root = make_root(base)
            env = base_env(root)
            env.update({
                "MINISA_RECALL_BACKEND": "universal",
                "MINISA_UNIVERSAL_RECALL_DATA": str(Path(base) / "umem-data"),
            })
            config = ServerConfig.load(environ=env)
            if UNIVERSAL_MEMORY_AVAILABLE:
                app = MinisaApplication(config)
                try:
                    self.assertIsInstance(app.memory, UniversalRecallService)
                    self.assertIsInstance(app.memory._inner, LocalMemoryService)
                    self.assertEqual(app.memory.status()["recall_backend"], "universal")
                finally:
                    adapter = getattr(app.memory, "_adapter", None)
                    if adapter is not None:
                        adapter.close()
            else:
                # Explicit universal selection requires Universal Memory:
                # startup fails predictably instead of degrading silently.
                with self.assertRaises((RuntimeError, ImportError)):
                    MinisaApplication(config)

    def test_universal_setup_failure_does_not_start_partial_app(self):
        """An inaccessible data path (file, not directory) fails construction."""
        if not UNIVERSAL_MEMORY_AVAILABLE:
            self.skipTest("universal_memory package not installed")
        from runtime.server.application import MinisaApplication

        with tempfile.TemporaryDirectory() as base:
            root = make_root(base)
            blocker = Path(base) / "umem-blocker"
            blocker.write_text("regular file, not a store directory\n", encoding="utf-8")
            env = base_env(root)
            env.update({
                "MINISA_RECALL_BACKEND": "universal",
                "MINISA_UNIVERSAL_RECALL_DATA": str(blocker),
            })
            config = ServerConfig.load(environ=env)
            with self.assertRaises(Exception):
                MinisaApplication(config)

    def test_default_application_is_plain_local(self):
        from runtime.server.application import MinisaApplication

        with tempfile.TemporaryDirectory() as base:
            config = ServerConfig.load(environ=base_env(make_root(base)))
            app = MinisaApplication(config)
            self.assertIsInstance(app.memory, LocalMemoryService)
            self.assertNotIsInstance(app.memory, UniversalRecallService)

    def test_shadow_mode_never_selects_universal_backend(self):
        from runtime.server.application import MinisaApplication
        from runtime.services.shadow_recall import ShadowMemoryService

        with tempfile.TemporaryDirectory() as base:
            root = make_root(base)
            env = base_env(root)
            env.update({
                "MINISA_SHADOW_RECALL": "1",
                "MINISA_SHADOW_RECALL_DATA": str(Path(base) / "umem-data"),
                "MINISA_SHADOW_RECALL_LOG": str(Path(base) / "eval.jsonl"),
            })
            config = ServerConfig.load(environ=env)
            self.assertEqual(config.recall_backend, "local")  # shadow does not flip the backend
            app = MinisaApplication(config)
            try:
                if UNIVERSAL_MEMORY_AVAILABLE:
                    self.assertIsInstance(app.memory, ShadowMemoryService)
                    self.assertNotIsInstance(app.memory, UniversalRecallService)
                    # Recall path stays the inner local service.
                    self.assertIsInstance(app.memory._inner, LocalMemoryService)
                else:
                    self.assertIsInstance(app.memory, LocalMemoryService)
            finally:
                evaluator = getattr(app, "shadow_evaluator", None)
                if evaluator is not None:
                    evaluator._adapter.close()


if __name__ == "__main__":
    unittest.main()
