import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from universal_memory import MemoryEngine, Tier
from universal_memory.index import Hit
from universal_memory.models import now


def test_remember_edit_history_and_filters(engine):
    m = engine.remember(
        "Alex built a racing game", importance=0.8, tags=["game"], entity_ids=["alex"]
    )
    assert m.index_status == "indexed"
    assert engine.list(tag="game", entity_id="alex")[0].id == m.id
    engine.edit(m.id, content="Alex built a space game")
    assert engine.history(m.id)[0]["memory"]["content"] == m.content
    assert engine.recall("space game")[0].memory.id == m.id


def test_duplicate_reinforces_and_preserves_observation(engine):
    a = engine.remember("The user enjoys chess", source="first")
    b = engine.remember("The user enjoys chess", source="second", metadata={"turn": 2})
    assert a.id == b.id
    assert b.reinforcement_count == 1
    assert any(h["memory"].get("metadata") == {"turn": 2} for h in engine.history(a.id))
    # Similarity alone must not erase a changed factual assertion.
    c = engine.remember("The user enjoys chess sometimes")
    d = engine.remember("The user does not enjoy chess")
    assert len({a.id, c.id, d.id}) == 3


def test_near_duplicate_punctuation(engine):
    a = engine.remember("The user enjoys playing chess every evening.")
    b = engine.remember("The user enjoys playing chess every evening!")
    assert a.id == b.id


class SemanticEmbedder:
    dimension = 3
    fingerprint = "test-semantic-v1"

    def encode(self, texts):
        return [
            [1.0, 0.0, 0.0]
            if any(t in text.lower() for t in ("automobile", "vehicle"))
            else [0.0, 1.0, 0.0]
            for text in texts
        ]


def test_semantic_recall_without_keyword_overlap(tmp_path):
    with MemoryEngine(tmp_path, embedder=SemanticEmbedder()) as memory:
        a = memory.remember("An automobile")
        memory.remember("A sandwich")
        result = memory.recall("vehicle", top_k=1)[0]
        assert result.memory.id == a.id
        assert result.explanation["semantic_score"] > 0.99
        assert result.explanation["lexical_score"] == 0


def test_keyword_recall_survives_embedding_failure(engine, monkeypatch):
    a = engine.remember("ZXQ9000 custom widget")
    monkeypatch.setattr(
        engine.embedder, "encode", lambda _: (_ for _ in ()).throw(RuntimeError("offline"))
    )
    result = engine.recall("ZXQ9000")[0]
    assert result.memory.id == a.id
    assert result.explanation["semantic_score"] == 0


def test_hybrid_reranking_not_just_semantic(engine, monkeypatch):
    a = engine.remember("game unrelated", importance=0.1)
    b = engine.remember("game chess", importance=1)
    engine.pin(b.id)
    monkeypatch.setattr(
        engine.index, "search", lambda *args: [Hit(a.id, 0.9, 0), Hit(b.id, 0.8, 0.8)]
    )
    result = engine.recall("chess", include_archived=False)[0]
    assert result.memory.id == b.id
    assert result.score == pytest.approx(sum(result.explanation["contributions"].values()))


def test_archive_resurfacing_and_promotion(engine):
    a = engine.remember("John old red car")
    engine.archive(a.id)
    assert engine.recall("John old red car", include_archived=False) == []
    assert engine.recall("unrelated ocean fish") == []
    result = engine.recall("John red car", promote=True)[0]
    assert result.explanation["resurfaced"]
    assert engine.get(a.id).tier == Tier.ACTIVE


def test_decay_priority_and_importance_protection(engine):
    low = engine.remember("Low priority observation", importance=0.01)
    pinned = engine.remember("Critical observation", importance=0.01)
    important = engine.remember("Important observation", importance=0.95)
    engine.pin(pinned.id)
    with engine.store.transaction():
        for id in (low.id, pinned.id, important.id):
            m = engine.get(id)
            m.last_accessed_at = now() - timedelta(days=4000)
            engine.store.save(m, "test_age", index=False)
    assert engine.decay() == [low.id]
    assert engine.get(low.id).tier == Tier.ACTIVE
    engine.decay(dry_run=False)
    assert engine.get(low.id).tier == Tier.ARCHIVED
    assert engine.get(pinned.id).tier == Tier.PRIORITY
    assert engine.get(important.id).tier == Tier.ACTIVE


def test_consolidation_reversible_and_conflict_safe(engine):
    a = engine.remember("Enjoys chess on Monday")
    b = engine.remember("Enjoys chess on Tuesday")
    summary = engine.consolidate([a.id, b.id], "Enjoys chess regularly")
    assert engine.get(a.id).content == a.content
    assert engine.get(a.id).tier == Tier.ARCHIVED
    assert len(engine.consolidation(summary.id)["originals"]) == 2
    engine.reverse_consolidation(summary.id)
    assert engine.get(a.id).tier == Tier.ACTIVE
    assert engine.get(summary.id).tier == Tier.ARCHIVED
    engine.reverse_consolidation(summary.id)
    other = engine.consolidate([a.id, b.id], "Chess habit")
    engine.edit(a.id, importance=0.9)
    with pytest.raises(ValueError, match="changed"):
        engine.reverse_consolidation(other.id)
    assert not engine.consolidation(other.id)["reversed"]
    assert engine.get(b.id).tier == Tier.ARCHIVED


def test_namespace_and_entity_isolation(tmp_path):
    path = tmp_path / "shared"
    with MemoryEngine(path, namespace="alice") as memory:
        secret = memory.remember("Private project violet", entity_ids=["alice"])
    with MemoryEngine(path, namespace="bob") as memory:
        assert memory.recall("Private project violet") == []
        with pytest.raises(KeyError):
            memory.get(secret.id)
        a = memory.remember("Project violet alpha", entity_ids=["project-a"])
        b = memory.remember("Project violet beta", entity_ids=["project-b"])
        memory.link(a.id, b.id)
        found = memory.recall("Project violet", entity_ids=["project-a"], strict_entities=True)
        assert {r.memory.id for r in found} == {a.id}
        with pytest.raises(ValueError):
            memory.consolidate([a.id, b.id], "Projects")


def test_supersession_retains_history_and_prevents_cycles(engine):
    a = engine.remember("The user owns an iPhone", entity_ids=["user"])
    b = engine.remember("The user switched to Android", entity_ids=["user"])
    engine.supersede(a.id, b.id)
    assert engine.get(a.id).superseded_by == b.id
    assert engine.get(a.id).content == a.content
    with pytest.raises(ValueError, match="cycle"):
        engine.supersede(b.id, a.id)


def test_bounded_chain_expansion(engine):
    seed = engine.remember("quasar astronomical")
    linked = engine.remember("secret companion")
    archived = engine.remember("buried association")
    engine.link(seed.id, linked.id)
    engine.link(seed.id, archived.id)
    engine.archive(archived.id)
    results = engine.recall("quasar", include_archived=False)
    assert any(r.memory.id == linked.id and r.explanation["chain_expansion"] for r in results)
    assert archived.id not in {r.memory.id for r in results}


def test_deletion_removes_content_history_index_and_links(engine):
    a = engine.remember("sensitive unique secret")
    b = engine.remember("other public record")
    engine.link(b.id, a.id)
    engine.delete(a.id)
    with pytest.raises(KeyError):
        engine.get(a.id)
    assert a.id not in engine.get(b.id).related_ids
    assert (
        engine.store.db.execute(
            "SELECT count(*) FROM history WHERE memory_id=?", (a.id,)
        ).fetchone()[0]
        == 0
    )
    assert a.id not in {r.memory.id for r in engine.recall("sensitive unique secret")}


def test_failed_index_write_is_recoverable(engine, monkeypatch):
    original = engine.index.upsert
    monkeypatch.setattr(
        engine.index, "upsert", lambda *args: (_ for _ in ()).throw(RuntimeError("index down"))
    )
    a = engine.remember("Durable despite outage")
    assert a.index_status == "pending"
    assert engine.stats()["pending_index_operations"] == 1
    assert engine.recall("Durable outage")[0].memory.id == a.id
    monkeypatch.setattr(engine.index, "upsert", original)
    assert engine.sync_index() == 1
    assert engine.get(a.id).index_status == "indexed"


def test_failed_embedding_is_recoverable(engine, monkeypatch):
    original = engine.embedder.encode
    monkeypatch.setattr(
        engine.embedder, "encode", lambda *args: (_ for _ in ()).throw(RuntimeError("model failed"))
    )
    a = engine.remember("Durable embedding outage")
    assert a.index_status == "pending"
    monkeypatch.setattr(engine.embedder, "encode", original)
    assert engine.sync_index() == 1


def test_delete_failure_cannot_leak_stale_point(engine, monkeypatch):
    a = engine.remember("unique sensitive phrase")
    original = engine.index.delete
    monkeypatch.setattr(
        engine.index, "delete", lambda *args: (_ for _ in ()).throw(RuntimeError("down"))
    )
    engine.delete(a.id)
    assert engine.recall("unique sensitive phrase") == []
    assert engine.stats()["pending_index_operations"] == 1
    monkeypatch.setattr(engine.index, "delete", original)
    engine.sync_index()
    assert engine.stats()["pending_index_operations"] == 0


def test_persistence_and_missing_index_rebuild(tmp_path):
    path = tmp_path / "data"
    with MemoryEngine(path) as memory:
        a = memory.remember("Persistence across restart")
    shutil.rmtree(path / "qdrant")
    with MemoryEngine(path) as memory:
        assert memory.get(a.id).content == a.content
        assert memory.stats()["pending_index_operations"] == 1
        assert memory.rebuild_index()["pending"] == 0
        assert memory.recall("Persistence restart")[0].memory.id == a.id


def test_export_import_backup(engine, tmp_path):
    a = engine.remember("first original")
    b = engine.remember("second original")
    summary = engine.consolidate([a.id, b.id], "Combined originals")
    path = tmp_path / "export.jsonl"
    assert engine.export(path) == 3
    engine.backup(tmp_path / "backup.sqlite3")
    with MemoryEngine(tmp_path / "destination", namespace="imported") as other:
        assert other.import_file(path) == 3
        assert other.get(a.id).namespace == "imported"
        assert len(other.consolidation(summary.id)["originals"]) == 2
        other.reverse_consolidation(summary.id)
        assert other.get(a.id).tier == Tier.ACTIVE
        with pytest.raises(ValueError, match="collision"):
            other.import_file(path)
        assert other.stats()["total"] == 3


def test_import_rolls_back_on_malformed_line(engine, tmp_path):
    engine.remember("Valid")
    file = tmp_path / "bad.jsonl"
    engine.export(file)
    with file.open("a", encoding="utf-8") as output:
        output.write("not json\n")
    with MemoryEngine(tmp_path / "other") as other:
        with pytest.raises(ValueError):
            other.import_file(file)
        assert other.stats()["total"] == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"content": " "},
        {"content": "a", "importance": 2},
        {"content": "a", "confidence": float("nan")},
        {"content": "a", "surprise": True},
    ],
)
def test_malformed_remember(engine, kwargs):
    with pytest.raises(ValueError):
        engine.remember(**kwargs)
    assert engine.stats()["total"] == 0


def test_concurrent_calls_and_pagination(engine):
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda i: engine.remember(f"Record {i}"), range(12)))
    first = engine.list(limit=5)
    second = engine.list(after=first[-1].id, limit=10)
    assert len(first) + len(second) == len(records)
    assert len({m.id for m in first + second}) == 12


def test_duplicates_suggestions(engine):
    a = engine.remember("A very long shared sentence about chess every evening", reinforce=False)
    b = engine.remember("A very long shared sentence about chess every evening!", reinforce=False)
    assert set(engine.duplicates()[0]["ids"]) == {a.id, b.id}


def test_logging_and_explanation(engine):
    engine.remember("Explain this memory")
    result = engine.recall("Explain memory")[0]
    assert "contributions" in engine.explain_recall(result)
    assert engine.retrieval_logs()[0]["results"][0]["id"] == result.memory.id


def test_embedding_change_rejected(tmp_path):
    with MemoryEngine(tmp_path) as memory:
        memory.remember("embedding fingerprint")
    with pytest.raises(ValueError, match="provider changed"):
        MemoryEngine(tmp_path, embedder=SemanticEmbedder())
