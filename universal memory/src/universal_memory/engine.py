"""Synchronous local SDK. SQLite commits precede recoverable index operations."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from functools import wraps
from pathlib import Path
from time import perf_counter
from urllib.parse import urlparse
from uuid import uuid4

from .admin import Administration
from .embeddings import Embedder, HashEmbedder, tokens
from .index import Hit, LocalQdrantBackend, QdrantServerBackend, VectorIndex
from .models import Memory, MemoryInput, RecallResult, Settings, Tier, now
from .ranking import lexical_overlap, rank
from .storage import SQLiteStore, normalized

logger = logging.getLogger(__name__)


def locked(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            if self._closed:
                raise RuntimeError("MemoryEngine is closed")
            return method(self, *args, **kwargs)

    return wrapper


class MemoryEngine(Administration):
    def __init__(
        self,
        path: str | Path = "./data",
        *,
        namespace: str = "default",
        embedder: Embedder | None = None,
        index: VectorIndex | None = None,
        settings: Settings | None = None,
        vector_backend: str | None = None,
        qdrant_url: str | None = None,
        qdrant_api_key: str | None = None,
    ):
        if not namespace.strip() or len(namespace) > 200:
            raise ValueError("namespace must contain 1 to 200 characters")
        qdrant_url = qdrant_url or os.getenv("UMEM_QDRANT_URL")
        qdrant_api_key = qdrant_api_key or os.getenv("UMEM_QDRANT_API_KEY")
        backend = (
            vector_backend
            or os.getenv("UMEM_VECTOR_BACKEND")
            or ("qdrant" if qdrant_url else "local")
        )
        if backend not in ("local", "qdrant"):
            raise ValueError("vector_backend must be local or qdrant")
        if backend == "local" and qdrant_url:
            raise ValueError("local backend cannot use qdrant_url")
        if backend == "qdrant" and (
            not qdrant_url
            or urlparse(qdrant_url).scheme not in ("http", "https")
            or not urlparse(qdrant_url).hostname
        ):
            raise ValueError("qdrant backend requires an http(s) qdrant_url")
        if qdrant_url:
            parsed = urlparse(qdrant_url)
            if parsed.port == 0 or parsed.query or parsed.fragment:
                raise ValueError("qdrant_url requires a valid port and no query or fragment")
        self.vector_backend = "custom" if index is not None else backend
        self.last_recall_profile: dict[str, float] = {}
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.namespace = namespace
        self.settings = settings or Settings()
        self.embedder = embedder or HashEmbedder()
        self._lock = threading.RLock()
        self._closed = False
        self.store = SQLiteStore(self.path / "memory.sqlite3")
        self.index_error: str | None = None
        self._custom_index = index is not None
        self._defer_index = False
        self.index = index
        previous = self.store.db.execute(
            "SELECT value FROM index_meta WHERE key='embedding'"
        ).fetchone()
        if previous and previous[0] != self.embedder.fingerprint:
            self.store.close()
            raise ValueError(
                "Embedding provider changed; export to a new data directory to migrate"
            )
        self.store.db.execute(
            "INSERT OR IGNORE INTO index_meta VALUES('instance',?)", (uuid4().hex,)
        )
        instance = self.store.db.execute(
            "SELECT value FROM index_meta WHERE key='instance'"
        ).fetchone()[0]
        self._index_options = {
            "dimension": self.embedder.dimension,
            "collection": "memories_"
            + instance
            + "_"
            + hashlib.sha256(self.embedder.fingerprint.encode()).hexdigest()[:16]
            + "_v2",
            "path": str(self.path / "qdrant"),
            "url": qdrant_url,
            "api_key": qdrant_api_key,
        }
        if self.index is None:
            self._connect_index()
        self.store.db.execute(
            "INSERT OR IGNORE INTO index_meta VALUES('embedding',?)", (self.embedder.fingerprint,)
        )
        self._auto_backup()

    def _connect_index(self) -> None:
        try:
            backend = QdrantServerBackend if self.vector_backend == "qdrant" else LocalQdrantBackend
            self.index = backend(**self._index_options)
            target = self._index_options["url"] or "local"
            previous_target = self.store.db.execute(
                "SELECT value FROM index_meta WHERE key='target'"
            ).fetchone()
            if self.index.created or not previous_target or previous_target[0] != target:
                with self.store.transaction():
                    self.store.db.execute(
                        "INSERT OR REPLACE INTO outbox SELECT id,namespace,'upsert' FROM memories"
                    )
                    self.store.db.execute(
                        "UPDATE memories SET data=json_set(data,'$.index_status','pending')"
                    )
            self.store.db.execute("INSERT OR REPLACE INTO index_meta VALUES('target',?)", (target,))
            self.index_error = None
        except Exception as exc:
            if self.index is not None:
                self.index.close()
                self.index = None
            self.index_error = str(exc)
            logger.warning("Qdrant unavailable; using SQLite lexical fallback: %s", exc)

    def _auto_backup(self) -> None:
        if self._defer_index or not self.settings.backup_interval_seconds:
            return
        directory = self.path / "backups"
        backups = sorted(directory.glob("memory-*.sqlite3")) if directory.exists() else []
        if (
            not backups
            or now().timestamp() - backups[-1].stat().st_mtime
            >= self.settings.backup_interval_seconds
        ):
            self.store.backup(directory / f"memory-{now().strftime('%Y%m%dT%H%M%S%f')}.sqlite3")
            backups = sorted(directory.glob("memory-*.sqlite3"))
            for old in backups[: -self.settings.backup_keep]:
                old.unlink()

    @locked
    def sync_index(self, batch_size: int = 100) -> int:
        """Retry one durable outbox batch. Failed work remains queued."""
        if not 1 <= batch_size <= 1000:
            raise ValueError("batch_size must be between 1 and 1000")
        if self._defer_index:
            return 0
        if self.index is None:
            self._connect_index()
        if self.index is None:
            return 0
        # Hold the database write lock through indexing to prevent another writer
        # acknowledging a stale version. Batches bound the lock duration.
        try:
            with self.store.transaction():
                jobs = self.store.db.execute(
                    "SELECT * FROM outbox LIMIT ?", (batch_size,)
                ).fetchall()
                if not jobs:
                    return 0
                removed = [r["id"] for r in jobs if r["operation"] == "delete"]
                memories = [
                    self.store.get(r["id"], r["namespace"])
                    for r in jobs
                    if r["operation"] == "upsert"
                ]
                vectors = self.embedder.encode([m.content for m in memories]) if memories else []
                self.index.delete(removed)
                self.index.upsert(memories, vectors)
                for memory in memories:
                    memory.index_status = "indexed"
                    self.store.db.execute(
                        "UPDATE memories SET data=? WHERE id=?",
                        (memory.model_dump_json(), memory.id),
                    )
                self.store.db.executemany(
                    "DELETE FROM outbox WHERE id=?", [(r["id"],) for r in jobs]
                )
            self.index_error = None
            return len(jobs)
        except Exception as exc:
            self.index_error = str(exc)
            if self.vector_backend == "qdrant" and self.index is not None:
                self.index.close()
                self.index = None
            logger.warning("Index update deferred: %s", exc)
            return 0

    @locked
    def get(self, id: str) -> Memory:
        return self.store.get(id, self.namespace)

    @locked
    def list(self, *, limit: int = 100, **filters) -> list[Memory]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        return self.store.list(self.namespace, limit=limit, **filters)

    @locked
    def remember(self, content: str, *, reinforce: bool = True, **attributes) -> Memory:
        value = MemoryInput(content=content, **attributes)
        duplicate = None
        if reinforce:
            candidates = self.store.db.execute(
                "SELECT data FROM memories WHERE namespace=? AND normalized=? LIMIT 100",
                (self.namespace, normalized(value.content)),
            ).fetchall()
            for row in candidates:
                m = Memory.model_validate_json(row[0])
                if set(m.entity_ids) == set(value.entity_ids) and not m.superseded_by:
                    duplicate = m
                    break
            if duplicate is None:
                # Near-duplicate reinforcement additionally requires near-identical text.
                # Semantically similar but distinct assertions are never auto-merged.
                from difflib import SequenceMatcher

                for id in self.store.lexical(
                    value.content, self.namespace, ["ACTIVE", "PRIORITY"], 20
                ):
                    m = self.get(id)
                    ratio = SequenceMatcher(
                        None, normalized(m.content), normalized(value.content)
                    ).ratio()
                    if (
                        ratio >= self.settings.duplicate_threshold
                        and set(m.entity_ids) == set(value.entity_ids)
                        and tokens(m.content) == tokens(value.content)
                        and not m.superseded_by
                    ):
                        duplicate = m
                        break
        with self.store.transaction():
            # Another process may have inserted the same observation since candidate lookup.
            if reinforce and duplicate is None:
                for row in self.store.db.execute(
                    "SELECT data FROM memories WHERE namespace=? AND normalized=?",
                    (self.namespace, normalized(value.content)),
                ):
                    candidate = Memory.model_validate_json(row[0])
                    if (
                        set(candidate.entity_ids) == set(value.entity_ids)
                        and not candidate.superseded_by
                    ):
                        duplicate = candidate
                        break
            if duplicate:
                duplicate = self.get(duplicate.id)
                duplicate.reinforcement_count += 1
                duplicate.importance = max(duplicate.importance, value.importance)
                duplicate.confidence = max(duplicate.confidence, value.confidence)
                duplicate.tags = sorted(set(duplicate.tags + value.tags))
                duplicate.last_accessed_at = now()
                duplicate.version += 1
                # Preserve each incoming observation, including its source and metadata.
                self.store.db.execute(
                    "INSERT INTO history(memory_id,action,at,snapshot) VALUES(?,?,?,?)",
                    (
                        duplicate.id,
                        "reinforcement_observation",
                        now().isoformat(),
                        value.model_dump_json(),
                    ),
                )
                memory = duplicate
            else:
                stamp = now()
                memory = Memory(
                    **value.model_dump(),
                    id=str(uuid4()),
                    namespace=self.namespace,
                    created_at=stamp,
                    updated_at=stamp,
                    last_accessed_at=stamp,
                )
            self.store.save(memory, "reinforce" if duplicate else "create")
        self.sync_index()
        self._auto_backup()
        return self.get(memory.id)

    @locked
    def remember_many(self, observations: list[MemoryInput | dict]) -> list[Memory]:
        """Validate first, commit the batch atomically, then batch embeddings.

        A crash before commit rolls back the whole batch.
        Limit batches to 1000; stream larger imports with import_file instead.
        """
        if not 1 <= len(observations) <= 1000:
            raise ValueError("Provide 1 to 1000 observations per batch")
        values = [MemoryInput.model_validate(value) for value in observations]
        self._defer_index = True
        try:
            with self.store.transaction():
                memories = [self.remember(**value.model_dump()) for value in values]
        finally:
            self._defer_index = False
        while self.sync_index():
            pass
        self._auto_backup()
        return [self.get(m.id) for m in memories]

    @locked
    def edit(self, id: str, **changes) -> Memory:
        allowed = set(MemoryInput.model_fields)
        if not changes.keys() <= allowed:
            raise ValueError(
                "Only content, scores, tags, category, source, entities and metadata are editable"
            )
        with self.store.transaction():
            memory = self.get(id)
            values = MemoryInput.model_validate(
                {**{k: getattr(memory, k) for k in allowed}, **changes}
            )
            memory = memory.model_copy(update=values.model_dump())
            memory.version += 1
            self.store.save(memory, "edit")
        self.sync_index()
        return self.get(id)

    def _tier(self, id: str, tier: Tier, pinned: bool | None = None) -> Memory:
        with self.store.transaction():
            memory = self.get(id)
            memory.tier = tier
            if pinned is not None:
                memory.pinned = pinned
            memory.version += 1
            self.store.save(memory, tier.value.lower())
        self.sync_index()
        return self.get(id)

    @locked
    def archive(self, id: str) -> Memory:
        return self._tier(id, Tier.ARCHIVED, False)

    def forget(self, id: str) -> Memory:
        """Reduce accessibility without destroying information."""
        return self.archive(id)

    @locked
    def restore(self, id: str) -> Memory:
        return self._tier(id, Tier.ACTIVE, False)

    @locked
    def pin(self, id: str) -> Memory:
        return self._tier(id, Tier.PRIORITY, True)

    @locked
    def unpin(self, id: str) -> Memory:
        return self._tier(id, Tier.ACTIVE, False)

    @locked
    def delete(self, id: str) -> None:
        """Purge content and history; keep only a vector deletion job until acknowledged."""
        with self.store.transaction():
            self.get(id)
            # Remove references from surviving memory documents as well as link rows.
            rows = self.store.db.execute(
                """SELECT data FROM memories WHERE namespace=? AND
                (json_extract(data,'$.superseded_by')=? OR EXISTS(
                 SELECT 1 FROM json_each(data,'$.related_ids') WHERE value=?))""",
                (self.namespace, id, id),
            ).fetchall()
            for row in rows:
                m = Memory.model_validate_json(row[0])
                m.related_ids = [x for x in m.related_ids if x != id]
                if m.superseded_by == id:
                    m.superseded_by = None
                m.version += 1
                self.store.save(m, "unlink_deleted")
            self.store.db.execute("DELETE FROM memories WHERE id=?", (id,))
            # Consolidation inspection contains source snapshots; purge those copies too.
            for row in self.store.db.execute(
                """SELECT id,data FROM consolidations WHERE namespace=? AND EXISTS(
                    SELECT 1 FROM json_each(data,'$.originals')
                    WHERE json_extract(value,'$.id')=?)""",
                (self.namespace, id),
            ).fetchall():
                record = json.loads(row["data"])
                sources = record["originals"]
                record["originals"] = [m for m in sources if m["id"] != id]
                if len(sources) != len(record["originals"]):
                    record["source_deleted"] = True
                    self.store.db.execute(
                        "UPDATE consolidations SET data=? WHERE id=?",
                        (json.dumps(record), row["id"]),
                    )
            self.store.db.execute(
                "INSERT OR REPLACE INTO outbox VALUES(?,?,?)", (id, self.namespace, "delete")
            )
            # Recall logs contain IDs only, but purge this namespace's logs for explicit erasure.
            self.store.db.execute("DELETE FROM recall_logs WHERE namespace=?", (self.namespace,))
        self.sync_index()

    def _candidates(
        self,
        query: str,
        vector: list[float] | None,
        tiers: list[str],
        limit: int,
        entity_ids: list[str] | None = None,
    ) -> dict[str, Hit]:
        hits: dict[str, Hit] = {}
        index_profile = {}
        if vector is not None and self.index:
            try:
                args = (query, vector, self.namespace, tiers, limit)
                found = (
                    self.index.search_filtered(*args, entity_ids=entity_ids)
                    if entity_ids and hasattr(self.index, "search_filtered")
                    else self.index.search(*args)
                )
                hits = {h.id: h for h in found[: 2 * limit]}
                index_profile = getattr(self.index, "last_search_profile", {})
            except Exception as exc:
                self.index_error = str(exc)
                logger.warning("Index search failed; using SQLite: %s", exc)
        for key, value in index_profile.items():
            self._retrieval_profile[key] = self._retrieval_profile.get(key, 0) + value
        lexical_started = perf_counter()
        for id in self.store.lexical(query, self.namespace, tiers, limit, entity_ids):
            hits.setdefault(id, Hit(id))
        self._retrieval_profile["sqlite_fts_ms"] = (
            self._retrieval_profile.get("sqlite_fts_ms", 0)
            + (perf_counter() - lexical_started) * 1000
        )
        return hits

    @locked
    def recall(
        self,
        query: str,
        *,
        top_k: int = 10,
        entity_ids: list[str] | None = None,
        strict_entities: bool = False,
        context: str = "",
        include_archived: bool = True,
        promote: bool | None = None,
    ) -> list[RecallResult]:
        started = perf_counter()
        profile = {}
        self._retrieval_profile = {}
        query = MemoryInput(content=query).content
        if not 1 <= top_k <= self.settings.candidate_limit:
            raise ValueError("top_k must be positive and at most candidate_limit")
        entities = entity_ids or []
        if strict_entities and not entities:
            raise ValueError("strict_entities requires entity_ids")
        try:
            vector = self.embedder.encode([query])[0]
        except Exception as exc:
            logger.warning("Query embedding unavailable: %s", exc)
            vector = None
        profile["query_embedding_ms"] = (perf_counter() - started) * 1000
        stage = perf_counter()
        hits = self._candidates(
            query,
            vector,
            ["ACTIVE", "PRIORITY"],
            self.settings.candidate_limit,
            entities if strict_entities else None,
        )
        profile["active_retrieval_ms"] = (perf_counter() - stage) * 1000
        stage = perf_counter()
        archive_ids: set[str] = set()
        if (
            include_archived
            and self.settings.archive_candidates
            and self.store.db.execute(
                "SELECT 1 FROM memories WHERE namespace=? AND tier='ARCHIVED' LIMIT 1",
                (self.namespace,),
            ).fetchone()
        ):
            archived = self._candidates(
                query,
                vector,
                ["ARCHIVED"],
                self.settings.archive_candidates,
                entities if strict_entities else None,
            )
            archive_ids = set(archived)
            hits.update(archived)
        profile["archive_probe_ms"] = (perf_counter() - stage) * 1000
        stage = perf_counter()
        lookup_seconds = 0.0
        results: dict[str, RecallResult] = {}
        for id, hit in hits.items():
            lookup_started = perf_counter()
            try:
                memory = self.get(
                    id
                )  # Revalidate namespace and tier against authoritative storage.
            except KeyError:
                continue
            finally:
                lookup_seconds += perf_counter() - lookup_started
            if strict_entities and not set(entities).intersection(memory.entity_ids):
                continue
            lexical = lexical_overlap(query, memory.content)
            if memory.index_status != "indexed":
                hit = Hit(id)  # A stale vector must not rank edited content as relevant.
            resurfaced = memory.tier == Tier.ARCHIVED
            if resurfaced and (
                id not in archive_ids
                or not include_archived
                or not (
                    lexical >= self.settings.archive_lexical_threshold
                    and len(set(query.split())) >= 2
                    or hit.semantic >= self.settings.archive_semantic_threshold
                )
            ):
                continue
            if not resurfaced and memory.tier not in (Tier.ACTIVE, Tier.PRIORITY):
                continue
            if max(lexical, hit.semantic) <= 0:
                continue
            result = rank(
                memory,
                hit.semantic,
                max(lexical, hit.lexical),
                entities,
                lexical_overlap(context, memory.content),
                resurfaced=resurfaced,
            )
            results[id] = result
        profile["lookup_reranking_ms"] = (perf_counter() - stage) * 1000
        profile["sqlite_lookup_ms"] = lookup_seconds * 1000
        profile["reranking_ms"] = profile["lookup_reranking_ms"] - profile["sqlite_lookup_ms"]
        stage = perf_counter()
        # A single bounded hop; archived targets cannot sneak in through associations.
        seeds = sorted(results.values(), key=lambda r: (-r.score, r.memory.id))[:top_k]
        remaining = self.settings.chain_limit
        for seed in seeds:
            if remaining <= 0:
                break
            links = self.store.db.execute(
                "SELECT target FROM links WHERE source=? ORDER BY target LIMIT ?",
                (seed.memory.id, remaining),
            ).fetchall()
            for row in links:
                remaining -= 1
                if row[0] in results:
                    continue
                memory = self.get(row[0])
                if memory.tier not in (Tier.ACTIVE, Tier.PRIORITY):
                    continue
                if strict_entities and not set(entities).intersection(memory.entity_ids):
                    continue
                result = rank(
                    memory, 0, lexical_overlap(query, memory.content), entities, chain=True
                )
                result.explanation["chain_source"] = seed.memory.id
                results[memory.id] = result
        selected = sorted(results.values(), key=lambda r: (-r.score, r.memory.id))[:top_k]
        profile["relationship_scoring_ms"] = (perf_counter() - stage) * 1000
        stage = perf_counter()
        should_promote = self.settings.promote_resurfaced if promote is None else promote
        with self.store.transaction():
            for result in selected:
                memory = self.get(result.memory.id)
                memory.access_count += 1
                memory.last_accessed_at = now()
                if should_promote and result.explanation["resurfaced"]:
                    memory.tier = Tier.ACTIVE
                    memory.version += 1
                    self.store.save(memory, "resurface")
                else:
                    self.store.db.execute(
                        "UPDATE memories SET data=? WHERE id=?",
                        (memory.model_dump_json(), memory.id),
                    )
                result.memory = memory
            profile["access_updates_ms"] = (perf_counter() - stage) * 1000
            log_started = perf_counter()
            if self.settings.retrieval_log_keep:
                self.store.db.execute(
                    "INSERT INTO recall_logs(namespace,at,data) VALUES(?,?,?)",
                    (
                        self.namespace,
                        now().isoformat(),
                        json.dumps(
                            [
                                {"id": r.memory.id, "score": r.score, "explanation": r.explanation}
                                for r in selected
                            ]
                        ),
                    ),
                )
                self.store.db.execute(
                    """DELETE FROM recall_logs WHERE namespace=? AND seq NOT IN
                    (SELECT seq FROM recall_logs WHERE namespace=? ORDER BY seq DESC LIMIT ?)""",
                    (self.namespace, self.namespace, self.settings.retrieval_log_keep),
                )
        profile["logging_commit_ms"] = (perf_counter() - log_started) * 1000
        if should_promote:
            self.sync_index()
        profile["access_updates_logging_ms"] = (perf_counter() - stage) * 1000
        profile["total_ms"] = (perf_counter() - started) * 1000
        profile["candidates"] = len(hits)
        profile.update(self._retrieval_profile)
        self.last_recall_profile = profile
        return selected

    @staticmethod
    def explain_recall(result: RecallResult) -> dict:
        return {"id": result.memory.id, "score": result.score, **result.explanation}

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                if self.index:
                    self.index.close()
                self.store.close()
                self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
