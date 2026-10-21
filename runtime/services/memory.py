"""Memory service contract and the adapter for Minisa's existing local memory.

Future memory systems belong behind :class:`MemoryService`; callers in the
cognition and action layers must not depend on their storage or transport.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol, runtime_checkable

from runtime.memory.manager import MemoryManager
from runtime.memory.semantic_retriever import SemanticMemoryRetriever

# Metadata header fields of a memory projection; boilerplate present in every
# note. Keyword scoring must ignore them so field names like "status" or
# "created" never match every memory in the store.
_METADATA_LINE = re.compile(
    r"^(?:ID|Type|Subject|Source|Status|Confidence|Created|Scope|Principal|Verified by):",
    re.IGNORECASE,
)

DEFAULT_RECALL_LIMIT = 5


def bounded_limit(limit, default: int = DEFAULT_RECALL_LIMIT) -> int:
    """Coerce any caller input to a safe non-negative result count."""
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    return max(0, value)


def _scoring_text(content: str) -> str:
    """Title and fact words only; metadata header lines removed."""
    return "\n".join(
        line for line in content.splitlines()
        if not _METADATA_LINE.match(line.strip())
    )


def valid_mutation_receipt(receipt):
    """A return value or acknowledgement alone is not successful persistence."""
    return (isinstance(receipt, dict) and receipt.get("committed") is True
            and receipt.get("status") == "committed"
            and type(receipt.get("durable")) is bool
            and isinstance(receipt.get("path"), str) and bool(receipt["path"].strip())
            and isinstance(receipt.get("warnings"), list)
            and all(isinstance(w, str) for w in receipt["warnings"]))


@runtime_checkable
class MemoryService(Protocol):
    """Storage-neutral operations required by Minisa's higher-level runtime.

    Mutations must return status=committed, committed=True, durable=bool,
    path (nonempty str), warnings (list[str]). Acknowledgement is not commit.
    Raise OSError only before commit; post-commit problems belong in receipts.
    Callers serialize selection/allocation/write via the configured root lock;
    remote backends must additionally enforce their own atomic allocation.
    """

    def recall(self, query: str, limit: int, semantic: bool = False) -> list[str]: ...

    def find_by_title(self, title: str) -> object | None: ...

    def search_titles(self, query: str, limit: int = 3) -> list[object]: ...

    def linked_memories(self, reference: object, limit: int = 3) -> list[object]: ...

    def read_memory(self, reference: object) -> str: ...

    def memory_exists(self, fact: str) -> bool: ...

    def next_memory_id(self) -> str: ...

    def create_memory(self, memory_id: str, title: str, content: str) -> dict: ...

    def find_forget_candidates(self, query: str) -> list[object]: ...

    def archive_memory(self, reference: object, query: str) -> dict: ...

    def status(self) -> dict: ...

    def inspect(self) -> dict: ...


class LocalMemoryService:
    """Adapter exposing the existing Markdown memory store via MemoryService."""

    def __init__(self, root: Path, ollama_url: str | None = None, embedding_model: str = "nomic-embed-text"):
        self.manager = MemoryManager(root)
        self.ollama_url = ollama_url
        self.embedding_model = embedding_model
        self._semantic_retriever: SemanticMemoryRetriever | None = None

    def recall(self, query: str, limit: int, semantic: bool = False) -> list[str]:
        # Contract-level fail-soft: malformed queries yield no results rather
        # than raising out of the recall path the agent depends on.
        if not isinstance(query, str):
            return []
        limit = bounded_limit(limit)
        if semantic:
            if self._semantic_retriever is None:
                from runtime.memory.embeddings import OllamaEmbeddings

                embeddings = (
                    OllamaEmbeddings(model=self.embedding_model, host=self.ollama_url)
                    if self.ollama_url
                    else None
                )
                self._semantic_retriever = SemanticMemoryRetriever(
                    self.manager,
                    embeddings=embeddings,
                )
            return self._semantic_retriever.retrieve(query, limit=limit)
        return self._keyword_recall(query, limit)

    def _read_or_none(self, path: Path) -> str | None:
        """Read a note for recall, or None if it is unreadable/corrupt.

        One bad file (binary, truncated permissions, encoding error) must
        degrade that single result instead of failing the whole recall.
        """
        try:
            return self.manager.read_recallable_memory(path)
        except (OSError, ValueError):
            return None

    def _keyword_recall(self, query: str, limit: int) -> list[str]:
        asking_about_user = any(
            phrase in query.lower()
            for phrase in ("remember about me", "know about me", "my details")
        )
        direct_titles = re.findall(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", query)
        direct_paths: list[Path] = []
        for title in direct_titles:
            path = self.find_by_title(title.strip())
            if path is not None and path not in direct_paths:
                direct_paths.append(path)
        if direct_paths:
            results: list[str] = []
            seen: set[Path] = set()
            for path in direct_paths:
                for candidate in [path, *self.linked_memories(path)]:
                    if candidate not in seen:
                        content = self._read_or_none(candidate)
                        if content is not None:
                            results.append(content)
                        seen.add(candidate)
                    if len(results) >= limit:
                        return results
            return results

        query_words = set(re.findall(r"\b[a-zA-Z0-9']+\b", query.lower()))
        if not query_words:
            return []
        scored = []
        for path in self.manager.list_active_memories():
            content = self._read_or_none(path)
            if content is None:
                continue
            if asking_about_user and "\nsubject: user\n" not in content.lower():
                continue
            # Score title and fact words; metadata header boilerplate never
            # counts as a match (a field name would otherwise rank every note).
            words = set(re.findall(r"\b[a-zA-Z0-9']+\b", _scoring_text(content).lower()))
            score = 1 if asking_about_user else len(query_words & words)
            if score:
                scored.append((score, path, content))
        scored.sort(key=lambda item: (-item[0], item[1].name))
        return [content for _, _, content in scored[:limit]]

    def find_by_title(self, title: str) -> Path | None:
        return self.manager.find_by_title(title)

    def search_titles(self, query: str, limit: int = 3) -> list[Path]:
        return self.manager.search_titles(query, limit)

    def linked_memories(self, path: Path, limit: int = 3) -> list[Path]:
        return self.manager.linked_memories(path, limit)

    def read_memory(self, path: Path) -> str:
        return self.manager.read_memory(path)

    def memory_exists(self, fact: str) -> bool:
        return self.manager.memory_exists(fact)

    def next_memory_id(self) -> str:
        return self.manager.next_memory_id()

    def create_memory(self, memory_id: str, title: str, content: str) -> dict:
        from runtime.storage import writer_lock
        with writer_lock(self.manager.root):
            self.manager.create_memory(memory_id, title, content)
            return dict(self.manager.last_receipt)

    def find_forget_candidates(self, query: str) -> list[Path]:
        return self.manager.find_forget_candidates(query)

    def archive_memory(self, path: Path, query: str) -> dict:
        return self.manager.archive_memory(path, query)

    def status(self) -> dict:
        return {
            "backend": "local",
            "available": self.manager.memory_path.is_dir(),
            "active_memories": len(self.manager.list_active_memories()),
        }

    def inspect(self) -> dict:
        status = self.status()
        if not status["available"]:
            return {**status, "records": [], "error": "memory directory unavailable"}
        records = [
            record
            for record in (
                self._read_or_none(path)
                for path in self.manager.list_active_memories()
            )
            if record is not None
        ]
        return {**status, "records": records}
