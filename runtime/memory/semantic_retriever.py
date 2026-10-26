import math
import re
import hashlib
import json
from uuid import uuid4

from runtime.memory.embeddings import OllamaEmbeddings
from runtime.memory.index import MemoryIndex
from runtime.memory.manager import MemoryManager


class SemanticMemoryRetriever:
    def __init__(
        self,
        memory: MemoryManager,
        embeddings: OllamaEmbeddings | None = None,
    ):
        self.memory = memory
        self.embeddings = embeddings or OllamaEmbeddings()
        # Separate derived cache: RP2 can still read its legacy vector map.
        self.index = MemoryIndex(memory.memory_path.parent, versioned=True)
        self._query_cache = {}
        self._anonymous_provider = uuid4().hex

    def retrieve(
        self,
        query: str,
        limit: int = 5,
        threshold: float = 0.45,
    ) -> list[str]:

        results = self.search(
            query,
            limit=limit,
            threshold=threshold,
        )

        return [
            result["formatted"]
            for result in results
        ]

    def search(
        self,
        query: str,
        limit: int = 5,
        threshold: float = 0.45,
    ) -> list[dict]:

        from runtime.services.memory import bounded_limit

        limit = bounded_limit(limit)
        # Exact input and provider identity, never case-fold model input.
        provider = {
            "adapter": f"{type(self.embeddings).__module__}.{type(self.embeddings).__qualname__}",
            "host": getattr(self.embeddings, "host", None),
            "model": getattr(self.embeddings, "model", None),
        }
        identity = getattr(self.embeddings, "cache_identity", None)
        if identity is not None:
            provider["identity"] = identity()
        # An anonymous adapter supplies no stable cross-instance model identity.
        if not provider["model"]:
            provider["instance"] = self._anonymous_provider
        query_key = (json.dumps(provider, sort_keys=True), query)
        if query_key in self._query_cache:
            query_vector = self._query_cache[query_key]
        else:
            query_vector = self._validated_vector(self.embeddings.embed(query))
            self._query_cache[query_key] = query_vector

            # Limit cache size to prevent memory bloat
            if len(self._query_cache) > 50:
                self._query_cache.pop(next(iter(self._query_cache)))

        scored = []

        for path in self.memory.list_active_memories():
            memory_id = path.stem
            try:
                content = self.memory.read_recallable_memory(path)
            except (OSError, ValueError):
                # A corrupt or unreadable note degrades this one result; it
                # must not fail the entire retrieval.
                continue

            expected = {"schema": 2, "provider": provider,
                        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                        "dimensions": len(query_vector)}
            entry = self.index.get(memory_id)
            memory_vector = None
            if isinstance(entry, dict) and all(entry.get(k) == v for k, v in expected.items()):
                try:
                    memory_vector = self._validated_vector(entry.get("vector"), len(query_vector))
                except ValueError:
                    pass  # Invalid derived entries are rebuilt, never ranked.
            if memory_vector is None:
                memory_vector = self._validated_vector(self.embeddings.embed(content), len(query_vector))
                self.index.set(memory_id, {**expected, "vector": memory_vector})

            similarity = self._cosine_similarity(
                query_vector,
                memory_vector,
            )

            metadata = self._extract_metadata(
                content
            )

            scored.append({
                "memory_id": memory_id,
                "score": similarity,
                "content": content,
                "metadata": metadata,
                "formatted": self._format_memory(
                    memory_id,
                    similarity,
                    metadata,
                    content,
                ),
            })

        if identity is not None and identity() != provider["identity"]:
            raise ValueError("Embedding model changed during retrieval; refusing mixed results")

        # Deterministic ordering: descending score, memory id tie-break so
        # equal-scoring notes never depend on directory iteration order.
        scored.sort(
            key=lambda item: (-item["score"], item["memory_id"]),
        )

        strong_matches = [
            result
            for result in scored
            if result["score"] >= threshold
        ]

        if strong_matches:
            return strong_matches[:limit]

        fallback_threshold = 0.30

        fallback_matches = [
            result
            for result in scored
            if result["score"] >= fallback_threshold
        ]

        return fallback_matches[:limit]

    @staticmethod
    def _validated_vector(vector, dimensions=None):
        if (not isinstance(vector, list) or not vector
                or (dimensions is not None and len(vector) != dimensions)
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)
                or not any(vector)):
            raise ValueError("Invalid or incompatible embedding vector")
        # Normalize using hypot to avoid overflow in cosine arithmetic.
        magnitude = math.hypot(*vector)
        if not math.isfinite(magnitude):
            raise ValueError("Invalid embedding magnitude")
        return [v / magnitude for v in vector]

    def _extract_metadata(
        self,
        content: str,
    ) -> dict:

        metadata = {
            "type": "",
            "subject": "",
            "source": "",
            "status": "",
            "confidence": "",
            "created": "",
            "fact": "",
            "original_input": "",
            "reason": "",
        }

        patterns = {
            "type": r"^Type:\s*(.+)$",
            "subject": r"^Subject:\s*(.+)$",
            "source": r"^Source:\s*(.+)$",
            "status": r"^Status:\s*(.+)$",
            "confidence": r"^Confidence:\s*(.+)$",
            "created": r"^Created:\s*(.+)$",
        }

        for key, pattern in patterns.items():
            match = re.search(
                pattern,
                content,
                re.MULTILINE,
            )

            if match:
                metadata[key] = match.group(1).strip()

        from runtime.cognition.context import memory_record
        metadata["fact"] = memory_record(content).get("fact", "")

        input_match = re.search(
            r"## Original Input\s*\n\n(.*?)(?=\n## |\Z)",
            content,
            re.DOTALL,
        )

        if input_match:
            metadata["original_input"] = (
                input_match.group(1).strip()
            )

        reason_match = re.search(
            r"## Reason\s*\n\n(.*?)(?=\n## |\Z)",
            content,
            re.DOTALL,
        )

        if reason_match:
            metadata["reason"] = (
                reason_match.group(1).strip()
            )

        return metadata

    def _format_memory(
        self,
        memory_id: str,
        score: float,
        metadata: dict,
        content: str,
    ) -> str:

        # Keep the read-time projection (including explicit review diagnostics).
        return f"MEMORY RECORD\nMemory ID: {memory_id}\nRetrieval Similarity: {score:.4f}\n\n{content}"

    def _cosine_similarity(
        self,
        a: list[float],
        b: list[float],
    ) -> float:

        if len(a) != len(b):
            raise ValueError(
                "Embedding dimensions do not match."
            )

        dot = sum(
            x * y
            for x, y in zip(a, b)
        )

        magnitude_a = math.sqrt(
            sum(x * x for x in a)
        )

        magnitude_b = math.sqrt(
            sum(x * x for x in b)
        )

        if magnitude_a == 0 or magnitude_b == 0:
            return 0.0

        return dot / (
            magnitude_a * magnitude_b
        )
