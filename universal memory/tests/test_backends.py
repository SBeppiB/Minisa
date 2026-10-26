"""Backend contracts, bounded retrieval, migration and outage regression coverage."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from universal_memory import MemoryEngine, Settings
from universal_memory.api import create_app
from universal_memory.index import LocalQdrantBackend, QdrantServerBackend


@pytest.mark.parametrize(
    "kwargs",
    [
        {"vector_backend": "invalid"},
        {"vector_backend": "qdrant"},
        {"qdrant_url": "not-a-url"},
        {"qdrant_url": "http://localhost:invalid"},
        {"qdrant_url": "http://localhost:0"},
        {"vector_backend": "local", "qdrant_url": "http://localhost:6333"},
    ],
)
def test_bad_backend_configuration(tmp_path, kwargs):
    with pytest.raises(ValueError):
        MemoryEngine(tmp_path, **kwargs)
    assert not (tmp_path / "memory.sqlite3").exists()


def test_server_ann_configuration(monkeypatch):
    client = MagicMock()
    client.collection_exists.return_value = False
    client.query_points.return_value = SimpleNamespace(points=[])
    monkeypatch.setattr("universal_memory.index.QdrantClient", lambda **kw: client)
    backend = QdrantServerBackend(dimension=384, collection="test", url="http://localhost:6333")
    config = client.create_collection.call_args.kwargs
    assert config["hnsw_config"].m > 0
    assert config["hnsw_config"].full_scan_threshold == 1000
    assert config["optimizers_config"].indexing_threshold > 0
    assert client.create_payload_index.call_count == 3
    backend.search_filtered("bicycle", [0.0] * 384, "tenant", ["ACTIVE"], 12, entity_ids=["alex"])
    dense = client.query_points.call_args_list[0].kwargs
    assert dense["search_params"].exact is False
    assert dense["limit"] == 12
    assert dense["with_payload"] is False
    assert [c.key for c in dense["query_filter"].must] == ["namespace", "tier", "entity_ids"]


def test_server_outage_recovery_and_api(tmp_path, monkeypatch):
    # In-memory transport exercises server lifecycle without requiring infrastructure.
    client = QdrantClient(":memory:")
    monkeypatch.setattr(client, "close", lambda: None)
    monkeypatch.setattr(client, "create_payload_index", lambda *a, **kw: None)
    online = False

    def connect(**kwargs):
        if not online:
            raise ConnectionError("server unavailable")
        return client

    monkeypatch.setattr("universal_memory.index.QdrantClient", connect)
    monkeypatch.setenv("UMEM_VECTOR_BACKEND", "qdrant")
    monkeypatch.setenv("UMEM_QDRANT_URL", "http://localhost:6333")
    with MemoryEngine(tmp_path, settings=Settings(backup_interval_seconds=0)) as engine:
        with TestClient(create_app(engine)) as api:
            record = api.post("/memories", json={"content": "Alex purchased a bicycle"}).json()
            assert record["index_status"] == "pending"
            assert api.get("/health").json()["status"] == "degraded"
            online = True
            assert engine.sync_index() == 1
            assert api.get("/backend-info").json()["backend"] == "qdrant"
            assert (
                api.post("/recall", json={"query": "Alex bicycle"}).json()[0]["memory"]["id"]
                == record["id"]
            )
            upsert = client.upsert

            def unavailable(*args, **kwargs):
                raise ConnectionError("server unavailable during write")

            monkeypatch.setattr(client, "upsert", unavailable)
            interrupted = engine.remember("Safe during a running server outage")
            assert interrupted.index_status == "pending"
            assert engine.index is None
            monkeypatch.setattr(client, "upsert", upsert)
            assert engine.sync_index() == 1
            client.delete_collection(engine.index.collection)
            engine.remember("Another durable observation")
            assert engine.index is None
            assert engine.sync_index() == 3
            assert engine.stats()["pending_index_operations"] == 0
    with MemoryEngine(tmp_path) as engine:
        assert engine.get(record["id"]).content == record["content"]


def test_atomic_batch_rollback(engine, monkeypatch):
    original = engine.store.save
    calls = 0

    def fail(memory, action, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("write failed")
        return original(memory, action, **kwargs)

    monkeypatch.setattr(engine.store, "save", fail)
    with pytest.raises(RuntimeError):
        engine.remember_many([{"content": "first"}, {"content": "second"}])
    assert engine.stats()["total"] == 0
    assert engine.stats()["pending_index_operations"] == 0


def test_bounded_recall_without_listing(engine, monkeypatch):
    engine.remember_many([{"content": f"Bicycle observation {i}"} for i in range(100)])
    monkeypatch.setattr(
        engine.store, "list", lambda *a, **kw: pytest.fail("full listing in recall")
    )
    engine.settings.candidate_limit = 7
    engine.settings.archive_candidates = 2
    reads = []
    original_get = engine.store.get

    def get(id, namespace):
        reads.append(id)
        return original_get(id, namespace)

    monkeypatch.setattr(engine.store, "get", get)
    result = engine.recall("Bicycle", top_k=5)
    assert len(reads) <= 3 * (7 + 2) + engine.settings.chain_limit + 5
    assert len(result) == 5
    assert engine.last_recall_profile["candidates"] <= 3 * (7 + 2)
    assert engine.last_recall_profile["total_ms"] > 0
    assert "dense_ms" in engine.last_recall_profile


def test_entity_filter_pushdown(engine):
    engine.remember_many(
        [{"content": f"bicycle red {i}", "entity_ids": ["other"]} for i in range(20)]
    )
    target = engine.remember("bicycle blue", entity_ids=["alex"])
    engine.settings.candidate_limit = 1
    result = engine.recall("bicycle", top_k=1, entity_ids=["alex"], strict_entities=True)
    assert result[0].memory.id == target.id


def test_mixed_thread_reads_writes(engine):
    engine.remember("shared bicycle")

    def run(i):
        if i % 2:
            return engine.remember(f"shared bicycle {i}").id
        return engine.recall("shared bicycle", top_k=1)[0].memory.id

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert len(list(pool.map(run, range(20)))) == 20
    assert engine.stats()["total"] == 11


def test_schema_upgrade_preserves_records(tmp_path):
    with MemoryEngine(tmp_path) as engine:
        item = engine.remember("preserved through migration")
        engine.store.db.execute("DROP INDEX entities_memory")
        engine.store.db.execute("PRAGMA user_version=1")
    with MemoryEngine(tmp_path) as engine:
        assert engine.get(item.id).content == item.content
        assert engine.stats()["schema_version"] == 2
        plan = engine.store.db.execute(
            "EXPLAIN QUERY PLAN DELETE FROM entities WHERE memory_id=?", (item.id,)
        ).fetchall()
        assert any("entities_memory" in str(tuple(row)) for row in plan)


def test_empty_archive_does_not_query_backend(engine, monkeypatch):
    engine.remember("bicycle")
    searched = []
    original = engine.index.search

    def search(query, vector, namespace, tiers, limit):
        searched.append(tiers)
        return original(query, vector, namespace, tiers, limit)

    monkeypatch.setattr(engine.index, "search", search)
    engine.recall("bicycle")
    assert searched == [["ACTIVE", "PRIORITY"]]


def test_backup_connection_is_closed(engine, tmp_path):
    import sqlite3

    path = tmp_path / "snapshot.sqlite3"
    engine.backup(path)
    moved = path.with_name("moved.sqlite3")
    path.rename(moved)
    connection = sqlite3.connect(moved)
    try:
        connection.execute("BEGIN EXCLUSIVE")
        connection.rollback()
    finally:
        connection.close()


def test_explicit_backend_requires_location():
    with pytest.raises(ValueError, match="local path"):
        LocalQdrantBackend(dimension=384, collection="test")
    with pytest.raises(ValueError, match="URL"):
        QdrantServerBackend(dimension=384, collection="test")


def test_health_recovers_after_transient_search_error(engine, monkeypatch):
    engine.remember("bicycle")
    original = engine.index.search

    def unavailable(*args):
        raise ConnectionError("temporary outage")

    monkeypatch.setattr(engine.index, "search", unavailable)
    assert engine.recall("bicycle")
    assert engine.index_error is not None
    monkeypatch.setattr(engine.index, "search", original)
    assert engine.health()["status"] == "ok"
