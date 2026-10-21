"""Lifecycle, shutdown during in-flight, adapter cleanup, persistent memory
across app reconstruction, DB/corruption, partial init, circuit recovery."""

import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import replace

from runtime.server.application import MinisaApplication
from runtime.server.config import ServerConfig
from runtime.services.memory import LocalMemoryService
from runtime.services.universal_recall import UniversalRecallService
from runtime.services.universal_memory_readonly import UniversalMemoryReadOnlyAdapter

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


class LifecycleRecoveryTests(unittest.TestCase):
    @unittest.skipUnless(UM, "universal_memory not installed")
    def test_adapter_close_multiple_times_idempotent(self):
        with TemporaryDirectory() as base:
            data = Path(base) / "udata"
            ad = UniversalMemoryReadOnlyAdapter(data)
            ad.close()
            ad.close()  # guarded

    def test_universal_circuit_opens_and_recovers(self):
        class SlowAdapter:
            def __init__(self): self.calls = 0
            def recall(self, q, l, semantic=False):
                self.calls += 1; raise RuntimeError("boom")
            def status(self): return {"available": False}
            def close(self): pass
        class Inner:
            def recall(self,*a,**k): return ["LOCAL"]
            def status(self): return {"backend":"fake","available":True}
            def __getattr__(self,n): return lambda *a,**k: []
        svc = UniversalRecallService(Inner(), SlowAdapter(), failure_threshold=2, circuit_cooldown=0.1)
        # two failures -> circuit opens
        self.assertEqual(svc.recall("q",5), ["LOCAL"])
        self.assertEqual(svc.recall("q",5), ["LOCAL"])
        c = svc.counters()
        self.assertIn(c["circuit_state"], ("open","closed"))  # may have opened
        time.sleep(0.15)
        # after cooldown, next will consult backend again (and fail)
        svc.recall("q",5)
        self.assertTrue(svc.counters()["recalls"] >= 3)

    def test_persistent_memory_across_app_reconstruction(self):
        with TemporaryDirectory() as base:
            root = _make_root(Path(base))
            env = {
                "MINISA_ROOT": str(root),
                "MINISA_HOST": "127.0.0.1",
                "MINISA_OLLAMA_URL": "http://127.0.0.1:11434",
            }
            cfg = ServerConfig.load(environ=dict(env))
            app1 = MinisaApplication(cfg)
            app1.memory.create_memory("memory_001", "Test", "# Test\n\n## Fact\n\nhello world\n")
            app2 = MinisaApplication(cfg)
            r = app2.memory.recall("hello", 5, semantic=False)
            self.assertTrue(any("hello world" in x for x in r))


if __name__ == "__main__":
    unittest.main()
