"""Keyword scoring must ignore metadata header boilerplate.

A concrete defect found by tests/retrieval_quality_eval.py: query words that
equal header field names ("status", "created", ...) matched every memory via
its "Status: active"-style projection header, flooding results with
irrelevant notes.
"""

import tempfile
import unittest
from pathlib import Path

from runtime.services.memory import LocalMemoryService, _scoring_text


def make_service(base: str) -> LocalMemoryService:
    root = Path(base) / "root"
    active = root / "MEMORY" / "active"
    active.mkdir(parents=True)
    (root / "CORE").mkdir()
    (root / "STATE").mkdir()
    notes = {
        "memory_001 - Bicycle.md": (
            "# Bicycle\n\nID: memory_001\nType: fact\nSubject: user\nSource: conversation\n"
            "Status: active\nConfidence: 0.90\nCreated: 2026-10-18\n\n## Fact\n\n"
            "User purchased a red bicycle in September 2026.\n"
        ),
        "memory_002 - Vault.md": (
            "# Vault\n\nID: memory_002\nType: fact\nSubject: user\nSource: conversation\n"
            "Status: active\nConfidence: 0.90\nCreated: 2026-10-18\n\n## Fact\n\n"
            "The vault code is 4242 and was rotated in October 2026.\n"
        ),
    }
    for name, content in notes.items():
        (active / name).write_text(content, encoding="utf-8")
    return LocalMemoryService(root, ollama_url=None)


class MetadataBoilerplateTests(unittest.TestCase):
    def test_scoring_text_strips_metadata_keeps_title_and_fact(self):
        content = (
            "# Bicycle\n\nID: memory_001\nType: fact\nSubject: user\nStatus: active\n\n"
            "## Fact\n\nUser purchased a red bicycle.\n"
        )
        scoring = _scoring_text(content)
        self.assertNotIn("Status:", scoring)
        self.assertNotIn("Subject:", scoring)
        self.assertIn("Bicycle", scoring)
        self.assertIn("red bicycle", scoring)

    def test_header_field_name_query_does_not_rank_every_memory(self):
        with tempfile.TemporaryDirectory() as base:
            service = make_service(base)
            results = service.recall("status", limit=5, semantic=False)
            # Neither fact mentions "status"; only the shared header did.
            self.assertEqual(results, [])

    def test_fact_overlap_still_ranks(self):
        with tempfile.TemporaryDirectory() as base:
            service = make_service(base)
            results = service.recall("vault code", limit=5, semantic=False)
            self.assertEqual(len(results), 1)
            self.assertIn("vault code is 4242", results[0])

    def test_title_words_still_count_as_matches(self):
        with tempfile.TemporaryDirectory() as base:
            service = make_service(base)
            # "Bicycle" appears in the title and fact; "Vault" only in its
            # title. A title-only query must still find the note.
            results = service.recall("vault", limit=5, semantic=False)
            self.assertEqual(len(results), 1)
            self.assertIn("vault code is 4242", results[0])

    def test_mixed_query_ranks_by_fact_overlap(self):
        with tempfile.TemporaryDirectory() as base:
            service = make_service(base)
            results = service.recall("project status", limit=5, semantic=False)
            # "project" is in neither fact: zero fact/title overlap for both.
            self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
