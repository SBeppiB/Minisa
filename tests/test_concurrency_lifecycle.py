"""Concurrency, lifecycle, cleanup and restart recovery tests (Phases F/G/H).

Candidate-only, deterministic, uses isolated temp data. No production changes.
Semantic quality remains explicitly BLOCKED.
"""

import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from runtime.server.application import MinisaApplication
from runtime.server.config import ServerConfig
from runtime.services.memory import LocalMemoryService
from runtime.services.universal_recall import UniversalRecallService
from runtime.services.shadow_recall import ShadowRecallEvaluator, ShadowMemoryService
from runtime.services.universal_memory_readonly import UniversalMemoryReadOnlyAdapter


def _make_root(base: Path) -> Path:
    root = base / "root"
    for d in ("CORE", "MEMORY", "MEMORY/archive", "STATE"):
        (root / d).mkdir(parents=True)
    return root


def _base_env(root: Path, base: Path) -> dict:
    return {
        "MINISA_ROOT": str(root),
        "MINISA_HOST": "127.0.0.1",
        "MINISA_OLLAMA_URL": "http://127.0.0.1:11434",
    }


class ConcurrentRecallTests(unittest.TestCase):
    def test_concurrent_local_keyword_recall(self):
        with TemporaryDirectory() as base:
            root = _make_root(Path(base))
            svc = LocalMemoryService(root, ollama_url=None)
            # create a couple of notes
            m = svc.manager
            m.create_memory("memory_001", "Vault", "# Vault\n\n## Fact\n\nvault code 4242\n")
            m.create_memory("memory_002", "Bike", "# Bike\n\n## Fact\n\nred bicycle\n")
            results = []
            errors = []
            def worker():
                try:
                    r = svc.recall("vault", 5, semantic=False)
                    results.append(r)
                except Exception as e:
                    errors.append(e)
            threads = [threading.Thread(target=worker) for _ in range(20)]
            for t in threads: t.start()
            for t in threads: t.join()
            self.assertEqual(errors, [])
            self.assertTrue(all("vault code 4242" in " ".join(x) for x in results))

    def test_concurrent_shadow_evaluate_is_atomic(self):
        with TemporaryDirectory() as base:
            root = _make_root(Path(base))
            log = Path(base) / "shadow.jsonl"
            class DummyAdapter:
                def recall(self, q, l, semantic=False): return []
            evalr = ShadowRecallEvaluator(DummyAdapter(), log)
            errors = []
            def w(i):
                try:
                    evalr.evaluate(f"q{i}", 5)
                except Exception as e:
                    errors.append(e)
            threads = [threading.Thread(target=w, args=(i,)) for i in range(50)]
            for t in threads: t.start()
            for t in threads: t.join()
            self.assertEqual(errors, [])
            # file has complete lines
            text = log.read_text(encoding="utf-8")
            lines = [l for l in text.splitlines() if l.strip()]
            self.assertEqual(len(lines), 50)
            # each parses
            for l in lines: json.loads(l)


if __name__ == "__main__":
    unittest.main()
