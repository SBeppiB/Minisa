"""Read-only Universal Memory adapter for Minisa's MemoryService contract.

Vendored from the offline prototype at /tmp/opencode/um-adapter-proto (kept in
sync manually during the evaluation phase). The universal_memory package is
imported lazily inside the constructor so this module can be imported in
environments where Universal Memory is not installed; construction then raises
ImportError and callers must fail soft.
"""

from __future__ import annotations

import math
from pathlib import Path

MAX_CONTENT_CHARS = 600
MAX_LIMIT = 20
DEFAULT_LIMIT = 5
# Minimum relevance floor for adapter hits, calibrated on the candidate
# evaluation corpus (tests/retrieval_quality_eval.py): unrelated queries
# scored <= 0.197 while the weakest legitimately relevant hit scored 0.269.
# 0.23 separates the observed noise ceiling from the weakest true match.
# Lower than Minisa's 0.30 semantic fallback because Universal Memory's
# default lexical baseline is scored on a different scale than cosine
# similarity; recalibrate if the embedding backend changes.
MIN_RELEVANCE_SCORE = 0.23


def format_hit(memory_id: str, score: float, content: str) -> str:
    """Bounded, provenance-carrying serialization of one recall hit."""
    text = " ".join(content.split())
    if len(text) > MAX_CONTENT_CHARS:
        text = text[: MAX_CONTENT_CHARS - 1] + "…"
    return f"[umem:{memory_id}] (score={score:.2f}) {text}"


def _extract_hit(hit) -> dict | None:
    """Safely pull id/score/content from one backend hit.

    Returns None for malformed hits (missing attributes, non-numeric score,
    non-string content) so a single bad record cannot break the whole recall.
    """
    try:
        content = hit.memory.content
        if not isinstance(content, str) or not content.strip():
            return None
        memory_id = str(hit.memory.id)
        if not memory_id.strip():
            return None
        return {"id": memory_id, "score": float(hit.score), "content": content}
    except (AttributeError, TypeError, ValueError):
        return None


class UniversalMemoryReadOnlyAdapter:
    """Recall-only adapter over Universal Memory's in-process MemoryEngine.

    Mutating MemoryService operations raise NotImplementedError. Title-based
    Markdown lookups have no Universal Memory equivalent and fail safe with
    empty answers. recall() is fail-soft: backend errors return [] and are
    recorded in last_recall_error for status()/inspect().
    """

    def __init__(self, data_dir: str | Path, *, namespace: str = "minisa-shadow"):
        from universal_memory import MemoryEngine, Settings

        self.data_dir = Path(data_dir)
        self.namespace = namespace
        self.last_recall_error: str | None = None
        self._engine = MemoryEngine(
            self.data_dir,
            namespace=namespace,
            settings=Settings(backup_interval_seconds=0),
        )

    def recall_detailed(self, query: str, limit: int) -> list[dict]:
        """Return bounded hit records with id, score and content.

        Contract: never raises for any query/limit input. Scores are finite
        and >= MIN_RELEVANCE_SCORE, ordered by descending score with memory id
        as deterministic tie-break, deduplicated by id, capped at MAX_LIMIT
        with a floor of 1 result (malformed limits fall back to DEFAULT_LIMIT).
        Malformed backend hits are dropped; backend or post-processing failures
        return [] and are recorded in last_recall_error for status()/inspect().
        """
        if not isinstance(query, str) or not query.strip():
            return []
        try:
            bounded = int(limit)
        except (TypeError, ValueError):
            bounded = DEFAULT_LIMIT
        bounded = max(1, min(bounded, MAX_LIMIT))
        try:
            hits = self._engine.recall(query, top_k=bounded)
            extracted = [hit for hit in map(_extract_hit, hits) if hit is not None]
        except Exception as exc:  # fail-soft: recall never raises into the caller
            self.last_recall_error = f"{type(exc).__name__}: {exc}"
            return []
        self.last_recall_error = None
        seen: set[str] = set()
        passing = []
        for hit in extracted:
            score = hit["score"]
            if not math.isfinite(score) or score < MIN_RELEVANCE_SCORE:
                continue
            if hit["id"] in seen:
                continue
            seen.add(hit["id"])
            passing.append(hit)
        passing.sort(key=lambda item: (-item["score"], item["id"]))
        return passing[:bounded]

    def recall(self, query: str, limit: int, semantic: bool = False) -> list[str]:
        del semantic  # Universal Memory always applies its own hybrid ranking.
        return [
            format_hit(hit["id"], hit["score"], hit["content"])
            for hit in self.recall_detailed(query, limit)
        ]

    def find_by_title(self, title: str) -> object | None:
        return None

    def search_titles(self, query: str, limit: int = 3) -> list[object]:
        return []

    def linked_memories(self, reference: object, limit: int = 3) -> list[object]:
        return []

    def read_memory(self, reference: object) -> str:
        return self._engine.get(str(reference)).content

    def memory_exists(self, fact: str) -> bool:
        return False

    def next_memory_id(self) -> str:
        raise NotImplementedError("read-only adapter does not allocate memory ids")

    def create_memory(self, memory_id: str, title: str, content: str) -> dict:
        raise NotImplementedError("read-only adapter does not create memories")

    def find_forget_candidates(self, query: str) -> list[object]:
        return []

    def archive_memory(self, reference: object, query: str) -> dict:
        raise NotImplementedError("read-only adapter does not archive memories")

    def status(self) -> dict:
        try:
            stats = self._engine.stats()
            active = stats["tiers"].get("ACTIVE", 0) + stats["tiers"].get("PRIORITY", 0)
            available = True
        except Exception as exc:
            stats = {}
            active = 0
            available = False
            self.last_recall_error = self.last_recall_error or f"{type(exc).__name__}: {exc}"
        return {
            "backend": "universal-memory-readonly",
            "available": available,
            "active_memories": active,
            "namespace": self.namespace,
            "last_recall_error": self.last_recall_error,
            "total": stats.get("total"),
        }

    def inspect(self) -> dict:
        status = self.status()
        if not status["available"]:
            return {**status, "records": [], "error": status.get("last_recall_error")}
        return {**status, "records": []}

    def close(self) -> None:
        close = getattr(self._engine, "close", None)
        if callable(close):
            close()
