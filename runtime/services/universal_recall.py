"""Explicit Universal Memory-backed recall mode (opt-in, fail-soft).

Selected only by configuration (recall_backend = "universal"); shadow mode
never enables it and the default remains ordinary local recall. Universal
hits are returned when relevant (the adapter applies its relevance floor);
empty or failed Universal recall falls back to the inner local service so
this mode can never leave a recall unanswered that local memory could
answer. Mutations always go to the inner service: the Universal adapter is
read-only and this mode never writes to the Universal store.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

# In-process Universal Memory recall has no internal timeout; a hung engine
# must not stall the caller's request path indefinitely. The watchdog bounds
# every universal attempt and the circuit breaker stops re-entering an
# unhealthy backend at all.
RECALL_TIMEOUT_SECONDS = 10.0
CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_COOLDOWN_SECONDS = 60.0


class UniversalRecallService:
    """MemoryService wrapper: Universal recall first, local recall as fallback.

    Every Universal attempt runs on a watchdog thread joined with
    recall_timeout; timeouts, errors and empty answers all fall back to the
    inner local service. Consecutive failures open a circuit breaker that
    skips the Universal backend entirely until the cooldown elapses, with at
    most one outstanding in-flight Universal recall at any time. counters()
    exposes the operational state.
    """

    def __init__(
        self,
        inner,
        adapter,
        *,
        recall_timeout: float = RECALL_TIMEOUT_SECONDS,
        failure_threshold: int = CIRCUIT_FAILURE_THRESHOLD,
        circuit_cooldown: float = CIRCUIT_COOLDOWN_SECONDS,
    ):
        self._inner = inner
        self._adapter = adapter
        self._recall_timeout = float(recall_timeout)
        self._failure_threshold = max(1, int(failure_threshold))
        self._circuit_cooldown = float(circuit_cooldown)
        self._lock = threading.Lock()
        self._outstanding = False
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._counters = {
            "recalls": 0,
            "hits": 0,
            "fallbacks": 0,
            "failures": 0,
            "timeouts": 0,
            "circuit_open_events": 0,
            "skipped_outstanding": 0,
        }

    def recall(self, query: str, limit: int, semantic: bool = False) -> list[str]:
        results, outcome = self._universal_recall(query, limit, semantic)
        with self._lock:
            self._counters["recalls"] += 1
            if results:
                self._counters["hits"] += 1
            else:
                self._counters["fallbacks"] += 1
        if results:
            return results
        return self._inner.recall(query, limit, semantic=semantic)

    def _universal_recall(self, query: str, limit: int, semantic: bool):
        with self._lock:
            if time.monotonic() < self._circuit_open_until:
                return [], "circuit_open"
            if self._outstanding:
                self._counters["skipped_outstanding"] += 1
                return [], "busy"
            self._outstanding = True
        box: dict = {}

        def worker():
            try:
                box["results"] = self._adapter.recall(query, limit, semantic=semantic)
            except Exception as error:
                box["error"] = error

        thread = threading.Thread(
            target=worker, daemon=True, name="universal-recall-watchdog"
        )
        thread.start()
        thread.join(self._recall_timeout)
        timed_out = thread.is_alive()
        with self._lock:
            self._outstanding = False
        if timed_out:
            with self._lock:
                self._counters["timeouts"] += 1
            self._register_failure()
            logger.warning(
                "Universal recall exceeded %.1fs watchdog; using local recall",
                self._recall_timeout,
            )
            return [], "timeout"
        if "error" in box:
            self._register_failure()
            logger.warning("Universal recall failed; using local recall", exc_info=box["error"])
            return [], "error"
        self._register_success()
        return box.get("results", []), "ok"

    def _register_failure(self) -> None:
        with self._lock:
            self._counters["failures"] += 1
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._failure_threshold:
                self._circuit_open_until = time.monotonic() + self._circuit_cooldown
                self._consecutive_failures = 0
                self._counters["circuit_open_events"] += 1
                logger.error(
                    "Universal recall circuit opened for %.0fs after %d consecutive failures",
                    self._circuit_cooldown,
                    self._failure_threshold,
                )

    def _register_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0

    def counters(self) -> dict:
        with self._lock:
            return {
                **self._counters,
                "circuit_state": (
                    "open" if time.monotonic() < self._circuit_open_until else "closed"
                ),
                "consecutive_failures": self._consecutive_failures,
                "outstanding": self._outstanding,
            }

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
        status = dict(self._inner.status())
        status["recall_backend"] = "universal"
        try:
            status["universal_recall"] = self._adapter.status()
        except Exception as error:
            status["universal_recall"] = {
                "available": False,
                "error": f"{type(error).__name__}: {error}",
            }
        status["universal_recall_stats"] = self.counters()
        return status

    def inspect(self) -> dict:
        return self._inner.inspect()


def attach_universal_recall(memory, config):
    """Build the read-only Universal Memory adapter and wrap the service.

    Raises on setup failure (missing universal_memory package, bad paths);
    callers must catch and keep the original service.
    """
    from runtime.services.universal_memory_readonly import UniversalMemoryReadOnlyAdapter

    adapter = UniversalMemoryReadOnlyAdapter(config.universal_recall_data)
    return UniversalRecallService(memory, adapter)
