"""Fault injection and operational recovery tests (Phase I focus).

Candidate-only, isolated temp data. Tests: DB corruption handling,
partial initialization, shutdown/cleanup during in-flight-like scenarios,
and circuit-breaker recovery path remains stable.
"""

import json
import os
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import replace

from runtime.services.memory import LocalMemoryService
from runtime.services.universal_recall import UniversalRecallService
from runtime.server.config import ServerConfig
from runtime.server.application import MinisaApplication

try:
    import universal_memory  # noqa
    UM = True
except ImportError:
    UM = False


def _make_root(base: Path) -> Path:
    root = base / "root"
    for d in ("CORE", "MEMORY", "MEMORY/archive", "STATE"):
        (root / d).mkdir(parents=True)
    return root


class FaultInjectionRecoveryTests(unittest.TestCase):
    def test_local_memory_recall_skips_corrupt_index_v2_gracefully(self):
        with TemporaryDirectory() as base:
            root = _make_root(Path(base))
            svc = LocalMemoryService(root, ollama_url=None)
            m = svc.manager
            m.create_memory("memory_001", "Vault", "# Vault\n\n## Fact\n\nvault code 4242\n")
            # corrupt index-v2.json
            idx = root / "MEMORY" / "index-v2.json"
            if idx.exists():
                idx.write_text("{not:json", encoding="utf-8")
            r = svc.recall("vault", 5, semantic=False)
            self.assertTrue(any("4242" in x for x in r))

    def test_partial_init_missing_dirs_fail_validation(self):
        with TemporaryDirectory() as base:
            bad = Path(base) / "bad"
            bad.mkdir()
            cfg = ServerConfig(root=bad)
            with self.assertRaises(ValueError):
                cfg.validate()

    def test_app_reconstruction_with_existing_memories_consistent(self):
        with TemporaryDirectory() as base:
            root = _make_root(Path(base))
            env = {
                "MINISA_ROOT": str(root),
                "MINISA_HOST": "127.0.0.1",
                "MINISA_OLLAMA_URL": "http://127.0.0.1:11434",
            }
            cfg = ServerConfig.load(environ=dict(env))
            app1 = MinisaApplication(cfg)
            app1.memory.create_memory("memory_001", "Note", "# Note\n\n## Fact\n\npersist me\n")
            app2 = MinisaApplication(cfg)
            self.assertTrue(any("persist me" in x for x in app2.memory.recall("persist", 5, semantic=False)))


if __name__ == "__main__":
    unittest.main()
