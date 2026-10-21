import unittest

from runtime.cognition.engine import CognitionEngine
from runtime.cognition.router import CognitiveRouter


class MemoryStub:
    def __init__(self, fail_keyword=False):
        self.calls = []
        self.fail_keyword = fail_keyword

    def recall(self, query, limit, semantic=False):
        self.calls.append((query, limit, semantic))
        if semantic:
            raise RuntimeError("embedding service unavailable")
        if self.fail_keyword:
            raise OSError("local memory unavailable")
        return ["existing local memory"]

    def status(self):
        return {"backend": "local", "available": True, "active_memories": 1}


class MemoryRecallFallbackTests(unittest.TestCase):
    def engine(self, memory):
        engine = CognitionEngine.__new__(CognitionEngine)
        engine.memory = memory
        engine.router = CognitiveRouter()
        return engine

    def test_embedding_failure_uses_existing_keyword_memory(self):
        memory = MemoryStub()
        result = self.engine(memory).recall("a normal question", "normal")
        self.assertEqual(result["memories"], ["existing local memory"])
        self.assertEqual(result["memory_status"]["retrieval"], "keyword_fallback")
        self.assertEqual(memory.calls, [
            ("a normal question", 5, True),
            ("a normal question", 5, False),
        ])

    def test_fallback_failure_remains_classified_as_failed(self):
        memory = MemoryStub(fail_keyword=True)
        result = self.engine(memory).recall("a normal question", "normal")
        self.assertEqual(result["memories"], [])
        self.assertEqual(result["memory_status"]["retrieval"], "failed")
        self.assertEqual(result["memory_status"]["retrieval_error"], "OSError: local memory unavailable")


if __name__ == "__main__":
    unittest.main()
