"""Explicit, inspectable maintenance operations for the local SDK."""

import json
import math
from pathlib import Path
from uuid import uuid4

from .models import Memory, MemoryInput, Tier, now


class Administration:
    """Maintenance methods; mixed into MemoryEngine to keep retrieval focused."""

    def history(self, id: str) -> list[dict]:
        with self._lock:
            return self.store.history(id, self.namespace)

    def link(
        self, source: str, target: str, *, kind: str = "related", bidirectional: bool = True
    ) -> None:
        if source == target or not kind.strip():
            raise ValueError("Links require different memories and a nonempty kind")
        with self._lock, self.store.transaction():
            first, second = self.get(source), self.get(target)
            pairs = [(first, second)] + ([(second, first)] if bidirectional else [])
            for a, b in pairs:
                self.store.db.execute(
                    "INSERT OR IGNORE INTO links VALUES(?,?,?)", (a.id, b.id, kind)
                )
                a.related_ids = sorted(set(a.related_ids + [b.id]))
                a.version += 1
                self.store.save(a, "link", index=False)

    def supersede(self, old_id: str, new_id: str) -> Memory:
        if old_id == new_id:
            raise ValueError("A memory cannot supersede itself")
        with self._lock, self.store.transaction():
            old, new = self.get(old_id), self.get(new_id)
            seen = {old_id}
            cursor = new
            while True:
                if cursor.id in seen:
                    raise ValueError("Supersession cycle")
                seen.add(cursor.id)
                if not cursor.superseded_by:
                    break
                cursor = self.get(cursor.superseded_by)
            if set(old.entity_ids) != set(new.entity_ids):
                raise ValueError("Supersession requires the same entity scope")
            old.superseded_by = new_id
            old.version += 1
            old.related_ids = sorted(set(old.related_ids + [new_id]))
            self.store.db.execute(
                "INSERT OR IGNORE INTO links VALUES(?,?,?)", (old_id, new_id, "superseded_by")
            )
            self.store.save(old, "supersede", index=False)
            return old

    def decay(self, *, dry_run: bool = True, batch_size: int = 100) -> list[str]:
        """Stream active records; priority and pinned records are never auto-archived."""
        if not 1 <= batch_size <= 1000:
            raise ValueError("batch_size must be between 1 and 1000")
        candidates = []
        with self._lock:
            after = ""
            while batch := self.list(tier="ACTIVE", after=after, limit=batch_size):
                after = batch[-1].id
                for memory in batch:
                    age = max(0, (now() - memory.last_accessed_at).total_seconds() / 86400)
                    resistance = 1 + math.log1p(memory.access_count + memory.reinforcement_count)
                    retention = memory.importance + (1 - memory.importance) * 2 ** (
                        -age / (self.settings.decay_half_life_days * resistance)
                    )
                    if not memory.pinned and retention < self.settings.archive_decay_threshold:
                        candidates.append(memory.id)
                        if not dry_run:
                            self.archive(memory.id)
        return candidates

    def duplicates(self, *, limit: int = 100) -> list[dict]:
        """Bounded near-duplicate suggestions, never an instruction to merge facts."""
        from difflib import SequenceMatcher

        from .storage import normalized

        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        results = []
        with self._lock:
            after = ""
            while batch := self.list(after=after):
                after = batch[-1].id
                for memory in batch:
                    ids = self.store.lexical(
                        memory.content, self.namespace, ["ACTIVE", "PRIORITY", "ARCHIVED"], 20
                    )
                    for id in ids:
                        pair = tuple(sorted((memory.id, id)))
                        if id <= memory.id:
                            continue
                        other = self.get(id)
                        similarity = SequenceMatcher(
                            None, normalized(memory.content), normalized(other.content)
                        ).ratio()
                        if similarity >= self.settings.duplicate_threshold and set(
                            memory.entity_ids
                        ) == set(other.entity_ids):
                            results.append(
                                {
                                    "ids": list(pair),
                                    "text_similarity": similarity,
                                    "requires_review": True,
                                }
                            )
                            if len(results) >= limit:
                                return results
        return results

    def consolidate(self, ids: list[str], content: str) -> Memory:
        """Caller supplies an intentional summary; all original records survive."""
        ids = list(dict.fromkeys(ids))
        if not 2 <= len(ids) <= 1000:
            raise ValueError("Consolidation requires 2 to 1000 distinct memories")
        summary = MemoryInput(content=content)
        with self._lock:
            with self.store.transaction():
                originals = [self.get(id) for id in ids]
                if any(set(m.entity_ids) != set(originals[0].entity_ids) for m in originals):
                    raise ValueError("Consolidation requires the same entity scope")
                if any(m.pinned or m.tier == Tier.PRIORITY or m.superseded_by for m in originals):
                    raise ValueError(
                        "Unpin priority memories and resolve supersession before consolidation"
                    )
                stamp = now()
                memory = Memory(
                    content=summary.content,
                    id=str(uuid4()),
                    namespace=self.namespace,
                    created_at=stamp,
                    updated_at=stamp,
                    last_accessed_at=stamp,
                    importance=max(m.importance for m in originals),
                    confidence=min(m.confidence for m in originals),
                    source="consolidation",
                    entity_ids=originals[0].entity_ids,
                    tags=sorted({t for m in originals for t in m.tags}),
                    related_ids=ids,
                )
                memory.consolidation_history = [memory.id]
                self.store.save(memory, "consolidate")
                record = {
                    "originals": [m.model_dump(mode="json") for m in originals],
                    "summary": memory.id,
                    "created_at": stamp.isoformat(),
                }
                for original in originals:
                    original.tier = Tier.ARCHIVED
                    original.version += 1
                    original.consolidation_history.append(memory.id)
                    self.store.save(original, "consolidated_source")
                    self.store.db.execute(
                        "INSERT INTO links VALUES(?,?,?)", (memory.id, original.id, "derived_from")
                    )
                self.store.db.execute(
                    "INSERT INTO consolidations VALUES(?,?,?,0)",
                    (memory.id, self.namespace, json.dumps(record)),
                )
            self.sync_index()
            return self.get(memory.id)

    def consolidation(self, id: str) -> dict:
        with self._lock:
            row = self.store.db.execute(
                "SELECT data,reversed FROM consolidations WHERE id=? AND namespace=?",
                (id, self.namespace),
            ).fetchone()
            if row is None:
                raise KeyError(id)
            return {**json.loads(row[0]), "reversed": bool(row[1])}

    def reverse_consolidation(self, id: str) -> None:
        with self._lock:
            with self.store.transaction():
                record = self.consolidation(id)
                if record["reversed"]:
                    return
                if record.get("source_deleted"):
                    raise ValueError("An original source was explicitly deleted; cannot reverse")
                summary = self.get(id)
                for snapshot in record["originals"]:
                    current = self.get(snapshot["id"])
                    if current.version != snapshot["version"] + 1:
                        raise ValueError(
                            "Source changed after consolidation; manual restoration required"
                        )
                    current.tier = Tier(snapshot["tier"])
                    current.version += 1
                    self.store.save(current, "reverse_consolidation")
                summary.tier = Tier.ARCHIVED
                summary.version += 1
                self.store.save(summary, "reverse_consolidation")
                self.store.db.execute("UPDATE consolidations SET reversed=1 WHERE id=?", (id,))
            self.sync_index()

    def rebuild_index(self, *, batch_size: int = 100) -> dict:
        """Requeue this namespace in bounded batches; interrupted rebuilds are safe to rerun."""
        if not 1 <= batch_size <= 1000:
            raise ValueError("batch_size must be between 1 and 1000")
        queued = 0
        with self._lock:
            after = ""
            while batch := self.list(after=after, limit=batch_size):
                after = batch[-1].id
                with self.store.transaction():
                    for memory in batch:
                        memory.index_status = "pending"
                        self.store.db.execute(
                            "UPDATE memories SET data=? WHERE id=?",
                            (memory.model_dump_json(), memory.id),
                        )
                        self.store.db.execute(
                            "INSERT OR REPLACE INTO outbox VALUES(?,?,?)",
                            (memory.id, self.namespace, "upsert"),
                        )
                queued += len(batch)
                self.sync_index(batch_size)
            while self.sync_index(batch_size):
                pass
            return {"queued": queued, "pending": self.stats()["pending_index_operations"]}

    def repair_local_index(self) -> dict:
        """Explicitly quarantine local index files and rebuild; never discard SQLite."""
        if self._custom_index or self._index_options["url"]:
            raise ValueError(
                "Local repair is only available for the built-in embedded Qdrant index"
            )
        with self._lock:
            if self.index:
                self.index.close()
                self.index = None
            source = self.path / "qdrant"
            quarantine = self.path / f"qdrant-quarantine-{uuid4().hex}"
            if source.exists():
                source.rename(quarantine)
            self._connect_index()
            result = self.rebuild_index()
            return {**result, "quarantine": str(quarantine), "index_error": self.index_error}

    def backup(self, destination: str | Path) -> str:
        destination = Path(destination)
        if destination.resolve() == self.store.path.resolve():
            raise ValueError("Backup must not overwrite the live database")
        with self._lock:
            self.store.backup(destination)
        return str(destination)

    def export(self, destination: str | Path) -> int:
        """Stream a JSONL snapshot of this namespace, including history and links."""
        destination = Path(destination)
        if destination.resolve() == self.store.path.resolve():
            raise ValueError("Export must not overwrite the live database")
        count = 0
        with self._lock, destination.open("w", encoding="utf-8") as output:
            with self.store.transaction():
                after = ""
                while batch := self.list(after=after):
                    after = batch[-1].id
                    for memory in batch:
                        record = {
                            "format": 1,
                            "memory": memory.model_dump(mode="json"),
                            "history": self.history(memory.id),
                            "links": [
                                dict(r)
                                for r in self.store.db.execute(
                                    "SELECT * FROM links WHERE source=?", (memory.id,)
                                )
                            ],
                        }
                        row = self.store.db.execute(
                            "SELECT data,reversed FROM consolidations WHERE id=?", (memory.id,)
                        ).fetchone()
                        if row:
                            record["consolidation"] = {
                                "data": json.loads(row[0]),
                                "reversed": row[1],
                            }
                        output.write(json.dumps(record, ensure_ascii=False) + "\n")
                        count += 1
        return count

    def import_file(self, source: str | Path) -> int:
        """Atomic import with ID preservation; collisions fail instead of overwriting."""
        count = 0
        with self._lock, self.store.transaction():
            with Path(source).open(encoding="utf-8") as input_file:
                for line in input_file:
                    record = json.loads(line)
                    if record.get("format") != 1:
                        raise ValueError("Unsupported export format")
                    memory = Memory.model_validate(record["memory"])
                    if memory.tier == Tier.DELETED:
                        raise ValueError("Deleted records cannot be imported")
                    if self.store.db.execute(
                        "SELECT 1 FROM memories WHERE id=?", (memory.id,)
                    ).fetchone():
                        raise ValueError(f"ID collision: {memory.id}")
                    memory.namespace = self.namespace
                    self.store.save(memory, "import")
                    for event in record.get("history", []):
                        snapshot = dict(event["memory"])
                        if "namespace" in snapshot:
                            snapshot["namespace"] = self.namespace
                        self.store.db.execute(
                            "INSERT INTO history(memory_id,action,at,snapshot) VALUES(?,?,?,?)",
                            (memory.id, event["action"], event["at"], json.dumps(snapshot)),
                        )
                    if "consolidation" in record:
                        c = record["consolidation"]
                        self.store.db.execute(
                            "INSERT INTO consolidations VALUES(?,?,?,?)",
                            (memory.id, self.namespace, json.dumps(c["data"]), c["reversed"]),
                        )
                    count += 1
            # Second streaming pass resolves links after all records exist.
            with Path(source).open(encoding="utf-8") as input_file:
                for line in input_file:
                    record = json.loads(line)
                    memory = self.get(record["memory"]["id"])
                    for id in memory.related_ids + (
                        [memory.superseded_by] if memory.superseded_by else []
                    ):
                        self.get(id)
                    for link in record.get("links", []):
                        if link["source"] != memory.id:
                            raise ValueError("Link source does not match record")
                        self.get(link["target"])
                        self.store.db.execute(
                            "INSERT OR IGNORE INTO links VALUES(?,?,?)",
                            (memory.id, link["target"], link["kind"]),
                        )
        with self._lock:
            while self.sync_index():
                pass
        return count

    def retrieval_logs(self, limit: int = 100) -> list[dict]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._lock:
            return [
                {"at": r[0], "results": json.loads(r[1])}
                for r in self.store.db.execute(
                    "SELECT at,data FROM recall_logs WHERE namespace=? ORDER BY seq DESC LIMIT ?",
                    (self.namespace, limit),
                )
            ]

    def stats(self) -> dict:
        with self._lock:
            counts = dict(
                self.store.db.execute(
                    "SELECT tier,count(*) FROM memories WHERE namespace=? GROUP BY tier",
                    (self.namespace,),
                ).fetchall()
            )
            pending = self.store.db.execute(
                "SELECT count(*) FROM outbox WHERE namespace=?", (self.namespace,)
            ).fetchone()[0]
            return {
                "namespace": self.namespace,
                "total": sum(counts.values()),
                "tiers": counts,
                "pending_index_operations": pending,
                "index_available": self.index is not None,
                "index_error": self.index_error,
                "embedding": self.embedder.fingerprint,
                "schema_version": self.store.db.execute("PRAGMA user_version").fetchone()[0],
            }

    def backend_info(self) -> dict:
        """Inspect actual collection readiness; never expose credentials."""
        with self._lock:
            result = {
                "backend": self.vector_backend,
                "search": "HNSW/ANN"
                if self.vector_backend == "qdrant"
                else ("custom" if self.vector_backend == "custom" else "embedded exact scan"),
                "available": self.index is not None,
                "index_error": self.index_error,
            }
            if self.index is not None and hasattr(self.index, "client"):
                try:
                    info = self.index.client.get_collection(self.index.collection)
                    self.index_error = None
                    result.update(
                        index_error=None,
                        collection=self.index.collection,
                        status=str(info.status),
                        points=info.points_count,
                        indexed_vectors=info.indexed_vectors_count,
                    )
                except Exception as exc:
                    result.update(available=False, index_error=str(exc))
            return result

    def health(self) -> dict:
        info = self.backend_info()
        stats = self.stats()
        return {
            "status": "ok"
            if info["available"]
            and not info["index_error"]
            and not stats["pending_index_operations"]
            else "degraded",
            "sqlite": "ok",
            "index_available": info["available"],
        }
