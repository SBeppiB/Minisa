import tempfile
import unittest
from pathlib import Path

from runtime.cognition.engine import CognitionEngine
from runtime.services.memory import LocalMemoryService


class StubReasoner:
    def reason(self, context):
        return {"interpretation": "Fixture", "proposed_action": "respond", "confidence": 1.0}

    def respond(self, context, reasoning, decision=None, action=None):
        return "I cannot verify live weather."


class NormalNoteLookupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="minisa-note-lookup-")
        self.root = Path(self.temp.name)
        for directory in ("CORE", "MEMORY/active", "MEMORY/archive", "STATE",
                          "EXPERIENCE/events", "EXPERIENCE/turns", "REFLECTION/self_reflections"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        for name in ("identity", "values", "goals", "personality", "instructions"):
            body = "Name: Minisa\nCreator: SBeppi" if name == "identity" else name
            (self.root / "CORE" / f"{name}.md").write_text(body, encoding="utf-8")
        (self.root / "MEMORY/active/memory_001 - Live Gate Record.md").write_text(
            "# Live Gate Record\nID: memory_001\nSubject: User\nSource: User\n"
            "Status: user_claim\n\n## Fact\nPrivate saved diagnostic note\n",
            encoding="utf-8",
        )
        self.memory = LocalMemoryService(self.root)
        self.memory.recall = lambda query, limit, semantic=False: []
        self.engine = CognitionEngine(self.root, StubReasoner(), self.memory)

    def tearDown(self):
        self.temp.cleanup()

    def test_unrelated_weather_questions_do_not_return_fuzzy_title_match(self):
        queries = (
            "What did I ask you to do in my last message, and how did you respond? "
            "Also, what is the live weather in London right now?",
            "What is the live weather in London right now?",
        )
        for query in queries:
            with self.subTest(query=query):
                result = self.engine.process(query)
                self.assertEqual(result["response"], "I cannot verify live weather.")

    def test_explicit_full_note_title_still_uses_deterministic_lookup(self):
        result = self.engine.process("Tell me about Live Gate Record.")
        self.assertIn("Private saved diagnostic note", result["response"])

    def test_explicit_wiki_title_still_uses_deterministic_lookup(self):
        result = self.engine.process("[[Live Gate Record]]")
        self.assertIn("Private saved diagnostic note", result["response"])

    def test_explicit_memory_list_still_returns_requested_note(self):
        result = self.engine.process("Show me your memories.")
        self.assertIn("Private saved diagnostic note", result["response"])

    def test_memory_count_request_does_not_dump_saved_notes(self):
        result = self.engine.process("How many memories do you have?")
        self.assertIn("1 active stored memory", result["response"])
        self.assertNotIn("Private saved diagnostic note", result["response"])

    def test_memory_inspection_error_hides_private_diagnostics(self):
        diagnostic = "Permission denied: /srv/minisa/MEMORY/private-secret.md"
        self.memory.inspect = lambda: {"error": diagnostic}
        result = self.engine.process("Show me your memories.")
        self.assertIn("store is unavailable", result["response"])
        self.assertNotIn(diagnostic, result["response"])


if __name__ == "__main__":
    unittest.main()
