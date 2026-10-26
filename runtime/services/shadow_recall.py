"""Disabled-by-default shadow evaluation of Universal Memory recall.

When enabled by explicit configuration, every recall through the wrapped
MemoryService is mirrored into a Universal Memory read-only adapter. Shadow
hits are never merged into the normal recall result or the model context;
only bounded, hash-based metadata is appended to a dedicated evaluation log,
which is size-capped. Every shadow operation is fail-soft: errors are recorded
in the log and can never affect the caller's recall.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

HIT_PATTERN = re.compile(r"^\[umem:(?P<id>[^\]]+)\] \(score=(?P<score>[0-9.]+)\) (?P<content>.*)$")
DEFAULT_MAX_ENTRIES = 200
MAX_ERROR_CHARS = 200
# After this many consecutive log write failures the evaluator disables
# itself: a permanently broken log path must not keep hammering the disk or
# accumulate per-recall overhead for an evaluation that can never land.
MAX_LOG_WRITE_FAILURES = 50


class ShadowRecallEvaluator:
    """Runs shadow recall and appends bounded metadata records to a JSONL log.

    Records contain only hashes and sizes for queries and hit content, plus
    scores, ids, timing and truncated error strings. The log never exceeds
    max_entries lines: once exceeded, it is compacted to the newest half.
    """

    def __init__(self, adapter, log_path: str | Path, *, max_entries: int = DEFAULT_MAX_ENTRIES):
        self._adapter = adapter
        self._log_path = Path(log_path)
        self.max_entries = max(1, int(max_entries))
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        # Serializes append + compaction so concurrent recalls never interleave
        # partial lines or race the size-bound rewrite.
        self._write_lock = threading.Lock()
        self._consecutive_write_failures = 0
        self.disabled = False

    def evaluate(self, query: str, limit: int, *, semantic: bool = False) -> None:
        """Shadow one recall. Never raises."""
        if self.disabled:
            return
        from runtime.services.memory import bounded_limit

        record = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "query_sha256": _digest(query),
            "query_len": len(query) if isinstance(query, str) else 0,
            "limit": bounded_limit(limit),
            "semantic": bool(semantic),
        }
        started = time.monotonic()
        try:
            results = self._adapter.recall(query, limit, semantic=semantic)
            record["hit_count"] = len(results)
            record["hits"] = [_describe_hit(result) for result in results[:20]]
        except Exception as error:
            record["error"] = _describe_error(error)
        record["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
        try:
            self._append(record)
        except Exception:
            self._consecutive_write_failures += 1
            if self._consecutive_write_failures == MAX_LOG_WRITE_FAILURES:
                self.disabled = True
                logger.error(
                    "Shadow recall log disabled after %d consecutive write failures",
                    MAX_LOG_WRITE_FAILURES,
                )
            elif self._consecutive_write_failures < MAX_LOG_WRITE_FAILURES:
                logger.warning("Shadow recall log write failed", exc_info=True)

    def _append(self, record: dict) -> None:
        line = json.dumps(record, sort_keys=True) + "\n"
        with self._write_lock:
            with self._log_path.open("a", encoding="utf-8") as stream:
                # Repair a torn final line from an interrupted writer: start
                # our record on a fresh line instead of corrupting both.
                if self._log_path.exists() and self._log_path.stat().st_size > 0:
                    with self._log_path.open("rb") as probe:
                        probe.seek(-1, 2)
                        if probe.read(1) != b"\n":
                            stream.write("\n")
                stream.write(line)
            self._enforce_bound_locked()
        self._consecutive_write_failures = 0

    def _enforce_bound(self) -> None:
        with self._write_lock:
            self._enforce_bound_locked()

    def _enforce_bound_locked(self) -> None:
        try:
            lines = self._log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        if len(lines) <= self.max_entries:
            return
        kept = lines[-(self.max_entries // 2):]
        temporary = self._log_path.with_suffix(".tmp")
        temporary.write_text("\n".join(kept) + "\n", encoding="utf-8")
        temporary.replace(self._log_path)


class ShadowMemoryService:
    """MemoryService wrapper that delegates recall unchanged and shadows it."""

    def __init__(self, inner, evaluator: ShadowRecallEvaluator):
        self._inner = inner
        self.evaluator = evaluator

    def recall(self, query: str, limit: int, semantic: bool = False) -> list[str]:
        try:
            results = self._inner.recall(query, limit, semantic=semantic)
        except Exception:
            # Mirror failed recalls too: the shadow store's answer for a query
            # the local service could not answer is exactly what the evaluation
            # needs. The original exception still propagates unchanged.
            self._shadow_evaluate(query, limit, semantic=semantic)
            raise
        self._shadow_evaluate(query, limit, semantic=semantic)
        return results

    def _shadow_evaluate(self, query: str, limit: int, *, semantic: bool = False) -> None:
        try:
            self.evaluator.evaluate(query, limit, semantic=semantic)
        except Exception:
            logger.warning("Shadow recall evaluation failed", exc_info=True)

    def find_by_title(self, title: str) -> object | None:
        return self._inner.find_by_title(title)

    def search_titles(self, query: str, limit: int = 3) -> list[object]:
        return self._inner.search_titles(query, limit)

    def linked_memories(self, reference: object, limit: int = 3) -> list[object]:
        return self._inner.linked_memories(reference, limit)

    def read_memory(self, reference: object) -> str:
        return self._inner.read_memory(reference)

    def memory_exists(self, fact: str) -> bool:
        return self._inner.memory_exists(fact)

    def next_memory_id(self) -> str:
        return self._inner.next_memory_id()

    def create_memory(self, memory_id: str, title: str, content: str) -> dict:
        return self._inner.create_memory(memory_id, title, content)

    def find_forget_candidates(self, query: str) -> list[object]:
        return self._inner.find_forget_candidates(query)

    def archive_memory(self, reference: object, query: str) -> dict:
        return self._inner.archive_memory(reference, query)

    def status(self) -> dict:
        return self._inner.status()

    def inspect(self) -> dict:
        return self._inner.inspect()


def attach_shadow_recall(memory, config):
    """Build the read-only Universal Memory adapter and wrap the service.

    Raises on setup failure (missing universal_memory package, bad paths);
    callers must catch and keep the original service.
    """
    from runtime.services.universal_memory_readonly import UniversalMemoryReadOnlyAdapter

    adapter = UniversalMemoryReadOnlyAdapter(config.shadow_recall_data)
    evaluator = ShadowRecallEvaluator(adapter, config.shadow_recall_log)
    return ShadowMemoryService(memory, evaluator), evaluator


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _describe_error(error: Exception) -> str:
    """Exception type plus message digest; never the raw message.

    Exception messages can carry query or memory content (for example a
    decode error quoting offending bytes); the shadow log records only the
    type, length and digest for correlation.
    """
    message = str(error)
    return (
        f"{type(error).__name__}: [message redacted "
        f"sha256={_digest(message)[:12]} len={len(message)}]"
    )[:MAX_ERROR_CHARS]


def _describe_hit(result: object) -> dict:
    """Hash-based hit description; never stores raw memory content."""
    if isinstance(result, str):
        match = HIT_PATTERN.match(result)
        if match:
            content = match.group("content")
            return {
                "id": match.group("id"),
                "score": float(match.group("score")),
                "content_len": len(content),
                "content_sha256": _digest(content),
            }
        return {"id": None, "content_len": len(result), "content_sha256": _digest(result)}
    return {"id": None, "content_len": 0, "content_sha256": None}
