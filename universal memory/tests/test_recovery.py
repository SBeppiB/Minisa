import sqlite3

import pytest

from universal_memory import MemoryEngine


def test_batch_embeddings(engine, monkeypatch):
    batches = []
    original = engine.embedder.encode

    def record(texts):
        batches.append(len(texts))
        return original(texts)

    monkeypatch.setattr(engine.embedder, "encode", record)
    memories = engine.remember_many([{"content": f"Observation number {i}"} for i in range(10)])
    assert len(memories) == 10
    assert batches == [10]
    assert all(m.index_status == "indexed" for m in memories)


def test_corrupt_index_repair(tmp_path):
    with MemoryEngine(tmp_path) as memory:
        a = memory.remember("Recover this observation")
    (tmp_path / "qdrant" / "meta.json").write_text("invalid json", encoding="utf-8")
    with MemoryEngine(tmp_path) as memory:
        assert memory.index is None
        assert memory.recall("Recover observation")[0].memory.id == a.id
        result = memory.repair_local_index()
        assert result["pending"] == 0
        assert result["index_error"] is None
        assert list(tmp_path.glob("qdrant-quarantine-*"))


def test_unknown_future_schema_rejected(tmp_path):
    with sqlite3.connect(tmp_path / "memory.sqlite3") as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError, match="newer"):
        MemoryEngine(tmp_path)


def test_delete_purges_consolidation_snapshot(engine):
    a = engine.remember("private source secret")
    b = engine.remember("other original")
    summary = engine.consolidate([a.id, b.id], "Summary without secret")
    engine.delete(a.id)
    assert "private source secret" not in str(engine.consolidation(summary.id))
    with pytest.raises(ValueError, match="deleted"):
        engine.reverse_consolidation(summary.id)


def test_stale_edited_vector_not_used(engine, monkeypatch):
    a = engine.remember("quasar astronomical")
    monkeypatch.setattr(
        engine.index, "upsert", lambda *a: (_ for _ in ()).throw(RuntimeError("down"))
    )
    engine.edit(a.id, content="sandwich tomato")
    assert engine.recall("quasar astronomical") == []
