"""Opt-in real Qdrant test: set UMEM_TEST_QDRANT_URL to an isolated test server."""

import os

import pytest

from universal_memory import MemoryEngine, Settings


@pytest.mark.integration
def test_real_server_rebuild(tmp_path):
    url = os.getenv("UMEM_TEST_QDRANT_URL")
    if not url:
        pytest.skip("UMEM_TEST_QDRANT_URL is not configured")
    with MemoryEngine(
        tmp_path,
        vector_backend="qdrant",
        qdrant_url=url,
        settings=Settings(backup_interval_seconds=0),
    ) as engine:
        try:
            assert engine.index is not None, engine.index_error
            first = engine.remember("Alex purchased a red bicycle", entity_ids=["alex"])
            engine.remember("Jordan repaired a motorbike", entity_ids=["jordan"])
            assert first.index_status == "indexed", engine.index_error
            engine.archive(first.id)
            result = engine.recall(
                "Alex purchased a red bicycle", entity_ids=["alex"], strict_entities=True
            )
            assert result[0].memory.id == first.id
            engine.index.client.delete_collection(engine.index.collection)
            assert engine.rebuild_index()["pending"] == 0
            assert engine.recall("Alex purchased a red bicycle")[0].memory.id == first.id
        finally:
            if engine.index is not None:
                engine.index.client.delete_collection(engine.index.collection)
